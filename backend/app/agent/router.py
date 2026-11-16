"""AI 代理路由。

`/tasks` 是早期留下的阶段规划桩，保留给旧调用方；`/runs/stream` 是真正的多轮
工具循环，以 SSE 推送进度与结果。

开流前后的错误必须走不同通道：请求体不合法、开关关闭这类问题在第一个字节发出
之前就要返回普通 HTTP 错误（422 / 503 / 409），前端能按状态码区分；一旦开始推送，
HTTP 状态码已经发出去了，后续只能以流内的 `error` 帧表达。

`/runs` 是这次新增的**读**入口（列表 / 单条 / 标记已应用或已丢弃）。没有它们，
`runs` 表就是一张"写了没人读"的死表——落库的草稿永远没人捡起来。
"""
from __future__ import annotations

import contextlib
import logging
import os
from typing import Any, AsyncIterator, Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

from app.agent import runs
from app.agent.contracts import (
    AgentEvent,
    AgentRunRequest,
    DoneEvent,
    ErrorEvent,
    SnapshotEvent,
)
from app.agent.service import AgentPlan, AgentTaskRequest, plan, run
from app.agent.stream import DONE_FRAME, encode_event
from app.core.errors import AppError
from app.generation.service import GenerationError

router = APIRouter(prefix="/agent", tags=["AI 代理"])

logger = logging.getLogger(__name__)

# SSE 的三条响应头对三条开流路径都成立，收在一处：漏掉 `X-Accel-Buffering`
# 的那条路会在反向代理后面攒够一批才发，看起来像"卡了几十秒才出结果"。
_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    # 反向代理默认攒够一批才转发，那样 SSE 会退化成一次性响应
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}

# 没带 run_id 的老客户端由服务端补一个。用 uuid4 的十六进制而不是 `run-` 前缀：
# 前者天然满足 `AgentRunRequest.run_id` 的字符集，不必为"服务端自己发的值"再放宽
# 一次校验。
_GENERATED_RUN_ID_BYTES = 16


@router.post("/tasks", response_model=AgentPlan)
def create_task(body: AgentTaskRequest):
    """目标驱动生成（F02）。当前只返回阶段骨架，不产生导图。"""
    return plan(body.goal)


def _agent_enabled() -> bool:
    """总开关。

    默认开启。旧生成通道已经删掉了，这条开关关掉就等于关掉 AI 能力：开流前
    直接返回 503（`agent_disabled`），前端拿不到任何替代入口。
    """
    return os.getenv("MINDMAP_AGENT_ENABLED", "1").strip() != "0"


def _validate(body: AgentRunRequest) -> None:
    """开流之前把自相矛盾的请求挡掉。

    这些约束放这里而不是 `AgentRunRequest` 的模型校验里：它们描述的是"这个意图
    和这份输入搭不搭"，属于调用方语义，不是字段本身不合法。
    """
    if body.intent == "create" and body.snapshot is not None:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_request", "message": "新建导图时不应带 snapshot"},
        )
    if body.intent != "create" and body.snapshot is None:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_request", "message": f"{body.intent} 必须带上当前导图快照"},
        )
    if body.intent == "expand" and len(body.selected_ids) != 1:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_request", "message": "展开节点时必须且只能选中一个节点"},
        )


def _request_summary(body: AgentRunRequest) -> dict[str, Any]:
    """落库的请求摘要。

    **只存来源的 id，不存正文**：`runs` 是运行身份的台账，不是材料仓库；正文一旦
    进这张表，删库之前它就一直在，而它本来只该活在这一次请求里。`goal` / `intent`
    存下来是因为读的那条路要用——提示条上写"上次的「XXX」生成完成了"，以及
    "查看"时按原来的意图决定这份候选算新作品还是改现有这张图。
    """
    return {
        "goal": body.goal,
        "intent": body.intent,
        "thinking_model": body.thinking_model,
        "selected_ids": list(body.selected_ids),
        "source_ids": [source.id for source in body.sources],
    }


async def _replay(record: runs.RunRecord) -> AsyncIterator[str]:
    """把一个已经有结论的 run 原样"重放"成一条流。

    帧格式与真跑一模一样（`snapshot → done → [DONE]`），所以前端那条
    "候选确认"的流程一行都不用改：它收到的还是候选。

    这条路存在的意义是**不再调模型**：同一个 run_id 已经跑完过一次，再花一遍钱
    只为得到同一棵树，是这张表要消灭的第一件事。`mode` 也照原样回填——占位树
    被重放成 `mode=model` 就是界面替它撒谎。

    `done` 但没有候选的 run 同样重放（内容是空）：它在服务端已经是一次有结论的
    运行，重跑一次并不比"如实说没产出"更可能成功。用户真想重试时，前端每次点击
    都会生成新的 run_id，走的是另外一条路。
    """
    mode = record.mode if record.mode in ("demo", "model") else None
    seq = 0
    if record.candidate is not None:
        seq += 1
        yield encode_event(SnapshotEvent(seq=seq, content=record.candidate))
        seq += 1
        yield encode_event(DoneEvent(seq=seq, content=record.candidate, mode=mode))
    else:
        yield encode_event(DoneEvent(seq=1, content=None, mode=mode))
    yield DONE_FRAME


async def _frames(body: AgentRunRequest, run_id: str) -> AsyncIterator[str]:
    """把循环产出的事件翻成帧，并保证流一定以 `[DONE]` 收尾。

    开流之后抛异常会让客户端看到一条断掉的连接，前端无法区分"后端崩了"和
    "网络断了"；所以这里把异常收成一条 error 帧，让前端拿到明确的失败原因。

    终态在这里判、也在这里落库（`finally`，三条出路都写得到）：

      - 发出过 `done` 帧 → `done`
      - 发出过 `error` 帧 → `failed`
      - 什么都没发就断了 → `cancelled`

    判据故意是"**帧**发没发出去"，而不是"抛了哪种异常"：客户端断开时 Starlette
    是关生成器，抛的是 `GeneratorExit`（`BaseException`），`except Exception`
    接不到；用帧作判据，取消、断开、超时被掐这三条路自动归进同一格。
    """
    seq = 0
    # 只在正常收尾或已处理过的错误之后补 [DONE]。放在 finally 里会在 aclose()
    # 触发 GeneratorExit 时也去 yield，Python 直接抛
    # "async generator ignored GeneratorExit"，底层生成器的清理也被跳过。
    finished = False
    resolved = runs.STATUS_CANCELLED
    failure: dict[str, Any] | None = None
    mode: str | None = None
    # 候选的两个来源，按可信度排：`ctx` 是权威（含 edit/expand 那些只走 op 批次的
    # 改动），`observed` 是兜底（只在 `run()` 的收尾回调没能执行时才有用，比如
    # 任务被硬取消导致内层生成器没能跑完 finally）。多这一层不重复：一个来源是
    # "循环知道什么"，一个是"帧里已经发出去什么"，后者永远不会比前者更新。
    captured: dict[str, Any] = {"ctx": None, "observed": None}

    def on_close(candidate: Any) -> None:
        # 同步、只做赋值：取消路径上第一个 await 会立刻再抛一次 CancelledError。
        captured["ctx"] = candidate

    try:
        # `aclosing` 不是为了好看：客户端断开时这里被关掉，不显式关内层生成器，
        # 它的收尾（取消工具任务、交回候选）要等垃圾回收才执行——那段时间工具里
        # 的模型请求还在跑、还在计费。
        async with contextlib.aclosing(run(body, on_close=on_close)) as events:
            async for event in events:
                seq = max(seq, getattr(event, "seq", 0))
                if isinstance(event, DoneEvent):
                    resolved = runs.STATUS_DONE
                    mode = event.mode
                    captured["observed"] = event.content
                elif isinstance(event, ErrorEvent):
                    resolved = runs.STATUS_FAILED
                    failure = {"code": event.code, "message": event.message}
                elif isinstance(event, SnapshotEvent):
                    captured["observed"] = event.content
                yield encode_event(event)
        finished = True
    except GenerationError as exc:
        resolved = runs.STATUS_FAILED
        failure = {"code": "generation_failed", "message": str(exc)}
        yield encode_event(ErrorEvent(seq=seq + 1, code="generation_failed", message=str(exc)))
        finished = True
    except Exception as exc:  # noqa: BLE001 — 兜底：任何未预期异常都要变成可读的一帧
        resolved = runs.STATUS_FAILED
        failure = {"code": "agent_failed", "message": f"{type(exc).__name__}: {exc}"}
        yield encode_event(ErrorEvent(
            seq=seq + 1,
            code="agent_failed",
            message=f"生成过程中出错：{type(exc).__name__}: {exc}",
        ))
        finished = True
    finally:
        candidate = captured["ctx"] if captured["ctx"] is not None else captured["observed"]
        # 落库失败不该把一条已经跑完的流变成断开的连接：这是收尾，不是结果。
        # 拦下它并留下日志，用户至少还能从 done 帧拿到候选。
        try:
            runs.finish(run_id, resolved, candidate=candidate, failure=failure, mode=mode)
        except Exception:  # noqa: BLE001 — 见上
            logger.exception("run %s 的终态没有落库", run_id)
    if finished:
        yield DONE_FRAME


@router.post("/runs/stream")
async def stream_run(body: AgentRunRequest) -> StreamingResponse:
    if not _agent_enabled():
        raise HTTPException(
            status_code=503,
            detail={"code": "agent_disabled", "message": "AI 代理功能当前已关闭"},
        )
    _validate(body)

    run_id = body.run_id or uuid4().hex[:_GENERATED_RUN_ID_BYTES * 2]
    claim = runs.begin(run_id, _request_summary(body))
    if not claim.claimed:
        record = claim.record
        if record.status == runs.STATUS_RUNNING:
            # 同一个 run 并发跑两次会互相打架：两边都往同一个候选上写，谁也说不清
            # 最终那棵树是谁的。409 是开流之前的错误，前端能按状态码分辨，不必
            # 从一条流里去猜。
            #
            # 用 `AppError` 而不是 `HTTPException(detail={...})`：后者的处理链只认
            # `detail` 是字符串时的那几种 code，传字典出去顶层 `code` 会退化成
            # `http_409`，前端拿到的就是一句没有信息量的兜底文案。
            raise AppError(
                code="run_in_progress",
                message="这次生成（同一个 run_id）已经在跑了，重复提交不会再开一次。"
                        "等它结束，或到页面上读取它的结果。",
                status=409,
            )
        # `done`：把已有候选重放出来，一次模型调用都不发。
        return StreamingResponse(_replay(record), media_type="text/event-stream",
                                 headers=_SSE_HEADERS)

    return StreamingResponse(_frames(body, run_id), media_type="text/event-stream",
                             headers=_SSE_HEADERS)


# ---------------------------------------------------------------- 读：run 的台账

class RunSummary(BaseModel):
    """列表里的一行：**不带候选正文**。

    候选是一整棵树。20 条一起下发，等于每次进页面先传几十万字节，只为显示一句
    "上次有一次生成完成了"。真要那份内容时点「查看」，走 `GET /runs/{run_id}`。
    """

    model_config = ConfigDict(extra="forbid")
    run_id: str
    status: str
    #: 原样回填给前端：`create` 的候选落地时不能继承当前图的身份，`edit`/`expand` 要。
    intent: str
    goal: str
    created_at: str
    updated_at: str
    applied_at: str | None = None
    dismissed_at: str | None = None


class RunDetail(RunSummary):
    """单条：带候选正文与失败原因。"""

    model_config = ConfigDict(extra="forbid")
    candidate: Any | None = None
    failure: dict[str, Any] | None = None
    mode: str | None = None


def _summary(record: runs.RunRecord) -> RunSummary:
    request = record.request
    return RunSummary(
        run_id=record.run_id,
        status=record.status,
        intent=str(request.get("intent") or "create"),
        goal=str(request.get("goal") or ""),
        created_at=record.created_at,
        updated_at=record.updated_at,
        applied_at=record.applied_at,
        dismissed_at=record.dismissed_at,
    )


@router.get("/runs", response_model=list[RunSummary])
def list_runs(
    status_filter: Literal["done", "cancelled", "failed", "running"] | None = Query(
        default=None, alias="status",
        description="只看某一种状态。缺省 = 有草稿且还没被处理过的（done + cancelled）。",
    ),
    unapplied: bool = Query(
        default=True,
        description="只列有草稿、且用户既没应用也没丢弃的；置 false 则把这段台账原样列出来。",
    ),
    limit: int = Query(default=20, ge=1, le=100),
) -> list[RunSummary]:
    """运行台账。默认回答的是"上次跑完、我还没处理的是哪一次"。

    `status` 缺省时把 `done` 与 `cancelled` 一起算：**被取消但已经产出草稿的那次
    同样要能被捡起来**——否则"关掉页面那 40 秒"照样白花，只是从内存白花变成写进
    数据库白花。

    这条查询就是这张表草稿列的**唯一读者**。没有它，`candidate` 写进去之后再没人看过。
    """
    statuses: tuple[str, ...] = (
        (status_filter,) if status_filter else (runs.STATUS_DONE, runs.STATUS_CANCELLED)
    )
    records = runs.pending(limit=limit, statuses=statuses, unresolved_only=unapplied)
    return [_summary(record) for record in records]


@router.get("/runs/{run_id}", response_model=RunDetail)
def get_run(run_id: str) -> RunDetail:
    record = runs.get(run_id)
    if record is None:
        raise HTTPException(status_code=404,
                            detail={"code": "run_not_found", "message": "没有这次运行的记录"})
    return RunDetail(
        **_summary(record).model_dump(),
        candidate=None if record.candidate is None else record.candidate.model_dump(mode="json"),
        failure=record.failure,
        mode=record.mode,
    )


@router.post("/runs/{run_id}/applied", status_code=status.HTTP_204_NO_CONTENT)
def mark_run_applied(run_id: str) -> Response:
    """候选进了画布。前端在「应用」成功后调用一次，幂等。

    "done 但已应用"与"done 但没应用"只能靠这个时间戳分开：候选可能被 PUT 到一张
    已有导图上（推不出新记录），也可能应用之后又被撤销。用 `mindmaps` 表反推，
    得到的全是猜。
    """
    if not runs.mark_applied(run_id):
        raise HTTPException(status_code=404,
                            detail={"code": "run_not_found", "message": "没有这次运行的记录"})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/runs/{run_id}/dismissed", status_code=status.HTTP_204_NO_CONTENT)
def mark_run_dismissed(run_id: str) -> Response:
    """用户主动丢弃了这份候选。与"应用过"分开记：下次进页面不该再问同一次。"""
    if not runs.mark_dismissed(run_id):
        raise HTTPException(status_code=404,
                            detail={"code": "run_not_found", "message": "没有这次运行的记录"})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["router", "AgentEvent"]
