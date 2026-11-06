"""AI 代理路由。

`/tasks` 是早期留下的阶段规划桩，保留给旧调用方；`/runs/stream` 是真正的多轮
工具循环，以 SSE 推送进度与结果。

开流前后的错误必须走不同通道：请求体不合法、开关关闭这类问题在第一个字节发出
之前就要返回普通 HTTP 错误（422 / 503），前端能按状态码区分；一旦开始推送，
HTTP 状态码已经发出去了，后续只能以流内的 `error` 帧表达。
"""
from __future__ import annotations

import os
from typing import AsyncIterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.agent.contracts import AgentEvent, AgentRunRequest, ErrorEvent
from app.agent.service import AgentPlan, AgentTaskRequest, plan, run
from app.agent.stream import DONE_FRAME, encode_event
from app.generation.service import GenerationError

router = APIRouter(prefix="/agent", tags=["AI 代理"])


@router.post("/tasks", response_model=AgentPlan)
def create_task(body: AgentTaskRequest):
    """目标驱动生成（F02）。当前只返回阶段骨架，不产生导图。"""
    return plan(body.goal)


def _agent_enabled() -> bool:
    """总开关。

    默认开启：新能力上线后要能一条环境变量关掉回到旧路径，而不是回滚代码。
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
        async for event in run(body):
            seq = max(seq, getattr(event, "seq", 0))
            yield encode_event(event)
        finished = True
    except GenerationError as exc:
        yield encode_event(ErrorEvent(seq=seq + 1, code="generation_failed", message=str(exc)))
        finished = True
    except Exception as exc:  # noqa: BLE001 — 兜底：任何未预期异常都要变成可读的一帧
        yield encode_event(ErrorEvent(
            seq=seq + 1,
            code="agent_failed",
            message=f"生成过程中出错：{type(exc).__name__}: {exc}",
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
