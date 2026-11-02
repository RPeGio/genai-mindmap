from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.contracts.mindmap import GenerateRequest, GenerateResponse
from app.generation.service import GenerationError, generate
from app.generation.stream import generate_stream
from app.generation.stream_partial import generate_stream_partial

router = APIRouter(prefix="/generations", tags=["AI 生成"])


@router.post("", response_model=GenerateResponse)
def generate_map(body: GenerateRequest):
    """一次性生成：等模型吐完再返回，适合脚本调用与短输入。"""
    try:
        return generate(body.text)
    except GenerationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def _pick_stream_impl():
    """流式实现二选一，用环境变量切换。

    MINDMAP_STREAM_IMPL=partial   单次调用 + 增量 JSON 解析（默认）
                                 实测 deepseek-chat 下首节点 1.4–2.6 秒、完成 2.9–4.2 秒
    MINDMAP_STREAM_IMPL=two-phase 两次调用，先骨架后细节
                                 首节点 1.5–1.9 秒、完成 5.4–5.9 秒
                                 推理模型（如 deepseek-flash）下首屏更快：骨架请求的思考量小，
                                 增量方案要等整段思考结束才开始出节点。两者都能用，不是二选一
    """
    import os

    impl = os.getenv("MINDMAP_STREAM_IMPL", "partial").strip().lower()
    return generate_stream if impl == "two-phase" else generate_stream_partial


@router.post("/stream")
async def generate_map_stream(body: GenerateRequest):
    """流式生成。两条实现的事件帧格式一致：若干 `data: {json}`，末尾 `data: [DONE]`。

    请求体校验失败时仍返回普通 JSON 错误（流还没开始，走统一错误处理器）；
    一旦开始推送，后续错误只能以 `{"type":"error"}` 事件的形式出现在流里。

    用 async 定义是必须的：客户端断开时 Starlette 会 cancel 这个生成器，
    异步 httpx 的连接随之关闭，模型侧才真正停止计费。
    """
    return StreamingResponse(
        _pick_stream_impl()(body.text),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # 反向代理默认会攒够一批才转发，那样 SSE 就退化成一次性响应了。
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
