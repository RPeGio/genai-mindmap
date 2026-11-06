"""把 `AgentEvent` 编码成 SSE 帧。

只做编码，不做传输——路由负责把生成器接到 `StreamingResponse` 上。分开的好处是
帧格式可以单独测，不必起一个 HTTP 服务。

格式沿用项目已有的 `generation/stream_partial.py`：`data: {JSON}\\n\\n`，以
`data: [DONE]` 收尾。**不**用 SSE 的 `event:` 字段另起一套命名——前端已经有
一个 `data:` 解析器，多一种帧形状只会让两处解析逻辑分叉。
"""
from __future__ import annotations

import json

from app.agent.contracts import AgentEvent

DONE_FRAME = "data: [DONE]\n\n"


def encode_event(event: AgentEvent) -> str:
    """一帧。

    `ensure_ascii=False`：中文在 sse 里原样传输，体积小一半，日志里也能直接读。
    `exclude_none=True`：可选字段不出现，前端少一层"这个字段是本来没有还是被清空"
    的判断。
    """
    payload = event.model_dump(mode="json", exclude_none=True)
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
