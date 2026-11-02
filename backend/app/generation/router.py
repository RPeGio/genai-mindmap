from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.contracts.mindmap import GenerateRequest, GenerateResponse
from app.generation.service import GenerationError, generate
from app.generation.stream_partial import generate_stream_partial

router = APIRouter(prefix="/generations", tags=["AI 生成"])


@router.post("", response_model=GenerateResponse)
def generate_map(body: GenerateRequest):
    """一次性生成：等模型吐完再返回，适合脚本调用与短输入。"""
    try:
        return generate(body.text)
    except GenerationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/stream")
async def generate_map_stream(body: GenerateRequest):
    """流式生成：一次调用，节点边生成边出现。帧格式见 stream_partial.py 的模块注释。

    请求体校验失败时仍返回普通 JSON 错误（流还没开始，走统一错误处理器）；
    一旦开始推送，后续错误只能以 `{"type":"error"}` 事件的形式出现在流里。

    用 async 定义是必须的：客户端断开时 Starlette 会 cancel 这个生成器，
    异步 httpx 的连接随之关闭，模型侧才真正停止计费。
    """
    return StreamingResponse(
        generate_stream_partial(body.text),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # 反向代理默认会攒够一批才转发，那样 SSE 就退化成一次性响应了。
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )