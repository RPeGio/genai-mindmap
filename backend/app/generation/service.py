"""本地演示和 OpenAI Chat Completions 兼容服务接入。"""
from __future__ import annotations

import json
import os
from typing import Any
from uuid import uuid4

import httpx
from pydantic import ValidationError

from app.contracts.mindmap import GenerateResponse, MindMapContent, Node

# 单次模型调用的超时（秒）与响应体上限（字节）：超时避免请求无限挂起，
# 上限避免异常响应把内存读满。
_TIMEOUT_SECONDS = 60.0
_MAX_RESPONSE_BYTES = 2_000_000

# 一次性生成完整导图的提示词（非流式接口、流式接口的第二段都用它）。
DETAIL_PROMPT = (
    "将用户输入整理成思维导图。只返回一个 JSON 对象，不要代码围栏。"
    "格式为 {\"title\":\"标题\",\"root\":{\"id\":\"唯一id\","
    "\"label\":\"节点文字\",\"children\":[]}}。每个节点均含 id、label、children，"
    "ID 全局唯一，层级不超过 8，节点不超过 300。不要捏造输入中没有的具体事实。"
)


# 一级分支下限：只要有一条分支就已经是「层次结构」，光杆根节点才算失败。
# 同步接口、两段式、增量式三处共用这一个值，改一处就全改 ——
# 否则同一个模型结果会被一个入口判成功、另一个判失败。
MIN_BRANCHES = 1


class GenerationError(Exception):
    pass


def _config() -> tuple[str, str, str]:
    """读模型配置。缺失或格式不对时抛 GenerationError，让上层给出可读文案。"""
    base = os.getenv("MINDMAP_LLM_BASE_URL", "").strip().rstrip("/")
    key = os.getenv("MINDMAP_LLM_API_KEY", "").strip()
    model = os.getenv("MINDMAP_LLM_MODEL", "").strip()
    if not base.startswith(("https://", "http://")) or not key or not model:
        raise GenerationError("请在后端环境变量中配置模型地址、密钥和模型名称")
    return base, key, model


def chat(system: str, user: str, *, max_tokens: int | None = None) -> dict[str, Any]:
    """调一次 Chat Completions，返回模型 content 里那段 JSON 解析后的对象。

    只负责传输与 JSON 解析；结构是否符合契约由调用方用 MindMapContent 校验。
    """
    base, key, model = _config()
    request_body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
    }
    if max_tokens is not None:
        request_body["max_tokens"] = max_tokens
    payload = json.dumps(request_body, ensure_ascii=False).encode("utf-8")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    try:
        # 调用方路由函数是同步 def，这里只用同步客户端；按流读取，
        # 一旦越过字节上限立即放弃响应，不必把超限内容读进内存。
        with httpx.Client(timeout=_TIMEOUT_SECONDS, follow_redirects=True) as client:
            with client.stream("POST", base + "/chat/completions",
                               content=payload, headers=headers) as response:
                response.raise_for_status()
                buffer = bytearray()
                for chunk in response.iter_bytes():
                    buffer.extend(chunk)
                    if len(buffer) > _MAX_RESPONSE_BYTES:
                        raise GenerationError("模型响应过大")
        body = json.loads(buffer)
        return json.loads(body["choices"][0]["message"]["content"])
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        raise GenerationError(f"模型服务返回 HTTP {code}，请检查服务配置") from exc
    except (httpx.RequestError, httpx.InvalidURL, OSError) as exc:
        raise GenerationError("无法连接模型服务或请求超时") from exc
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise GenerationError("模型返回内容不是符合约定的导图 JSON，请重试") from exc


async def chat_async(system: str, user: str, *, max_tokens: int | None = None) -> dict[str, Any]:
    """`chat()` 的异步版本，供流式接口使用。

    流式接口必须能真正取消：客户端断开时 Starlette 会 cancel 生成器，
    异步 httpx 会随之关闭连接、停止计费；同步 httpx 跑在线程里，
    取消只能丢掉结果，请求本身仍会跑完并产生费用。
    """
    import httpx

    base, key, model = _config()
    request_body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
    }
    if max_tokens is not None:
        request_body["max_tokens"] = max_tokens
    payload = json.dumps(request_body, ensure_ascii=False).encode("utf-8")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS, follow_redirects=True) as client:
            async with client.stream("POST", base + "/chat/completions",
                                     content=payload, headers=headers) as response:
                response.raise_for_status()
                buffer = bytearray()
                async for chunk in response.aiter_bytes():
                    buffer.extend(chunk)
                    if len(buffer) > _MAX_RESPONSE_BYTES:
                        raise GenerationError("模型响应过大")
        body = json.loads(buffer)
        return json.loads(body["choices"][0]["message"]["content"])
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        raise GenerationError(f"模型服务返回 HTTP {code}，请检查服务配置") from exc
    except (httpx.RequestError, httpx.InvalidURL, OSError) as exc:
        raise GenerationError("无法连接模型服务或请求超时") from exc
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise GenerationError("模型返回内容不是符合约定的导图 JSON，请重试") from exc


def _demo_content(text: str) -> MindMapContent:
    """显式的占位内容，不伪装为模型生成。"""
    label = text[:60]
    return MindMapContent(title=label, root=Node(id=str(uuid4()), label=label,
        children=[Node(id=str(uuid4()), label="请编辑此演示节点")]))


def generate(text: str) -> GenerateResponse:
    mode = os.getenv("MINDMAP_LLM_MODE", "demo").strip().lower()
    if mode == "demo":
        return GenerateResponse(mode="demo", content=_demo_content(text))
    if mode != "model":
        raise GenerationError("MINDMAP_LLM_MODE 只能是 demo 或 model")
    result = chat(DETAIL_PROMPT, text)
    try:
        content = MindMapContent.model_validate(result)
    except ValidationError as exc:
        raise GenerationError("模型返回内容不是符合约定的导图 JSON，请重试") from exc
    # 单文本接口无可验证的来源记录，不接受模型虚构的来源元数据。
    content.source_usage = []
    if len(content.root.children) < MIN_BRANCHES:
        raise GenerationError("模型只返回了一个根节点，没有任何分支，请换一种说法重试")
    return GenerateResponse(mode="model", content=content)
