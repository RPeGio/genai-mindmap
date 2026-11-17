"""AI 代理路由。

`/tasks` 是早期留下的阶段规划桩，保留给旧调用方；`/runs/stream` 是真正的多轮
工具循环，以 SSE 推送进度与结果。

开流前后的错误必须走不同通道：请求体不合法、开关关闭这类问题在第一个字节发出
之前就要返回普通 HTTP 错误（422 / 503），前端能按状态码区分；一旦开始推送，
HTTP 状态码已经发出去了，后续只能以流内的 `error` 帧表达。
"""
from __future__ import annotations

import logging
import os
from typing import AsyncIterator

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from app.agent.contracts import AgentEvent, AgentRunRequest, ErrorEvent, SourcesEvent
from app.agent.service import AgentPlan, AgentTaskRequest, plan, run
from app.agent.stream import DONE_FRAME, encode_event
from app.generation.service import GenerationError
from app.knowledge.remember import remember_sources

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent", tags=["AI 代理"])


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


async def _frames(body: AgentRunRequest) -> AsyncIterator[str]:
    """把循环产出的事件翻成帧，并保证流一定以 `[DONE]` 收尾。

    开流之后抛异常会让客户端看到一条断掉的连接，前端无法区分"后端崩了"和
    "网络断了"；所以这里把异常收成一条 error 帧，让前端拿到明确的失败原因。
    """
    seq = 0
    # 只在正常收尾或已处理过的错误之后补 [DONE]。放在 finally 里会在 aclose()
    # 触发 GeneratorExit 时也去 yield，Python 直接抛
    # "async generator ignored GeneratorExit"，底层生成器的清理也被跳过。
    finished = False
    try:
        # 材料入库排在事件流之前：入库会把临时 id 换成库里的持久 id，换完的
        # id 必须同时送到前端（`sources` 帧）和生成上下文（重排后的 request）。
        # 两边不一致时，导图里记的 `source_refs.source_id` 从这一轮起就指不到原文。
        #
        # 放线程池里跑：入库是同步 sqlite 写。但入库是"第一帧之前"的工作，几十份
        # 材料串起来可能安静好几秒——客户端这时连"连接已建立"都还看不到。所以先
        # 刷一行 SSE 注释帧把首字节送出去：`postSse` 只认 `data: ` 行，注释会被
        # 安全忽略，也不需要占 `seq`（`sources` 帧仍是 seq 1）。
        yield ": 正在把材料收进知识库\n\n"
        remembered = await run_in_threadpool(remember_sources, body.sources)
        if remembered.mapping:
            seq += 1
            yield encode_event(SourcesEvent(seq=seq, mapping=remembered.mapping))
            body = body.model_copy(update={"sources": remembered.sources})
        # `first_seq=seq + 1`：这一帧已经占掉 `seq`，循环里的编号接着往下发，
        # 否则 `sources` 和首个 `stage` 会同时叫 `seq=1`（客户端按 seq 排序时会困惑）。
        async for event in run(body, first_seq=seq + 1):
            seq = max(seq, getattr(event, "seq", 0))
            yield encode_event(event)
        finished = True
    except GenerationError as exc:
        yield encode_event(ErrorEvent(seq=seq + 1, code="generation_failed", message=str(exc)))
        finished = True
    except Exception as exc:  # noqa: BLE001 — 兜底：任何未预期异常都要变成可读的一帧
        # 只给用户一句能看懂的话。异常类型与原文（可能带绝对路径、库结构、上游
        # 地址）只进日志：出站帧是要给浏览器看的，不是排障现场。
        logger.exception("生成过程中出现未预期异常（%s），已收成 agent_failed 帧",
                         type(exc).__name__)
        yield encode_event(ErrorEvent(
            seq=seq + 1,
            code="agent_failed",
            message="生成过程中出错了，请稍后重试。",
        ))
        finished = True
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

    return StreamingResponse(
        _frames(body),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # 反向代理默认攒够一批才转发，那样 SSE 会退化成一次性响应
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


__all__ = ["router", "AgentEvent"]
