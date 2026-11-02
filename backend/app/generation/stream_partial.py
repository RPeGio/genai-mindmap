"""单次调用 + 增量 JSON 解析：让节点在模型还在输出时就逐个出现在画面上。

这是唯一的流式实现。曾经还有一个「两段式」（先骨架再展开）的方案，
已经删除 —— 两套实现要维护两套测试、两套文档，而增量方案的完整耗时更短。

增量解析的难点是 JSON 还没写完。这里的做法是「补齐括号」：扫描已收到的文本，
跟踪字符串状态与括号栈，在末尾补上缺失的引号与闭括号，再交给 json.loads。
截断在 `"label":` 这种位置时补出来的 JSON 仍然非法，函数返回 None 等下一批数据，
所以不会把半截内容当成合法结构。

全程 async：客户端断开时 Starlette 会 cancel 这个生成器，`httpx.AsyncClient`
的连接随之关闭，模型侧才真正停止、不再计费。
"""
from __future__ import annotations

import json
import os
import re
import time
from collections.abc import AsyncIterator

from pydantic import ValidationError

from app.contracts.mindmap import MindMapContent
from app.generation.service import (
    DETAIL_PROMPT,
    MIN_BRANCHES,
    GenerationError,
    _config,
    _demo_content,
)

_PARSE_INTERVAL_SECONDS = 0.15   # 两次尝试解析之间的最小间隔，避免每个 chunk 都全量解析
_FINAL_GRACE_SECONDS = 60.0


def _sse(payload: dict) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


_DONE_FRAME = "data: [DONE]\n\n"


def _strip_fence(text: str) -> str:
    """模型偶尔仍会包 ```json 围栏，剥掉再解析。"""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return text
    body = stripped[3:]
    newline = body.find("\n")
    if newline >= 0:
        body = body[newline + 1:]
    if body.rstrip().endswith("```"):
        body = body.rstrip()[:-3]
    return body


def _close_brackets(text: str) -> str | None:
    """在末尾补上缺失的引号与闭括号，让不完整的 JSON 变得可解析。

    返回 None 表示这段文本已经不可能补成合法结构（例如括号先闭后开）。
    """
    stack: list[str] = []
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if not stack:
                return None
            stack.pop()
    if escaped:
        # 末尾是半个转义序列，补完的字符串没意义，放弃本轮解析。
        return None
    suffix = '"' if in_string else ""
    for opener in reversed(stack):
        suffix += "}" if opener == "{" else "]"
    return text + suffix


def parse_partial(buffer: str) -> dict | None:
    """尽最大努力把已收到的片段解析成 dict；做不到就返回 None。"""
    if not buffer.strip():
        return None
    try:
        return json.loads(buffer)
    except json.JSONDecodeError:
        pass
    candidate = _strip_fence(buffer)
    closed = _close_brackets(candidate)
    if closed is None:
        return None
    try:
        parsed = json.loads(closed)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _id_belongs_to_node(buffer: str, node_id: str, label: str) -> bool:
    """确认这个 id 与 label 属于**同一个 JSON 对象**。

    只在整个 buffer 里搜 `"id":"x"` 是不够的：已有节点 id=b，新节点 id 是 b1
    但只输出到 `"id":"b` 时，解析结果里新节点的 id 会被补齐成 b，于是整段搜索
    会命中前一个节点的 id，把新节点的文字写到旧节点上。

    做法是定位 label 在原文里的位置，往前找最近的 `{`，要求两者之间存在完整
    的 `"id":"<node_id>"`。截断的 id 找不到自己的 label，会被跳过。

    代价是节点要等 label 完全闭合才出现，不再逐字长大 —— 用准确性换掉打字机
    效果，因为「文字突然变成别人的」比「晚几十毫秒出现」更糟。
    """
    marker = f'"{label}"'
    pos = buffer.rfind(marker)
    if pos < 0:
        return False
    start = buffer.rfind("{", 0, pos)
    if start < 0:
        return False
    # 对象范围要取到 } 为止，不能截在 label 前面 —— id 完全可能写在 label 之后。
    end = buffer.find("}", pos)
    segment = buffer[start:end if end >= 0 else len(buffer)]
    pattern = r'"id"\s*:\s*"' + re.escape(node_id) + r'"'
    return re.search(pattern, segment) is not None


def collect_new_nodes(obj: dict, seen: dict[str, str],
                      raw: str = "") -> list[tuple[str | None, dict]]:
    """挑出尚未推送过的节点，保持出现顺序。

    `raw` 是模型到目前为止的原始输出，用来判断某个节点是不是真的写完了；
    拿不到原文时（例如单元测试）退化成「只推新 id」，不做文字更新。
    """
    picked: list[tuple[str | None, dict]] = []

    def walk(node: object, parent_id: str | None) -> None:
        if not isinstance(node, dict):
            return
        node_id = node.get("id")
        label = node.get("label")
        if isinstance(node_id, str) and isinstance(label, str) and label.strip():
            if node_id not in seen and _id_belongs_to_node(raw, node_id, label):
                seen[node_id] = label
                picked.append((parent_id, {"id": node_id, "label": label,
                                           "children": []}))
        for child in node.get("children") or []:
            walk(child, node_id if isinstance(node_id, str) else parent_id)

    walk(obj.get("root"), None)
    return picked


async def _stream_chat(system: str, user: str) -> AsyncIterator[str]:
    """异步流式调用 Chat Completions，逐段产出 content。"""
    import httpx

    base, key, model = _config()
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
        "stream": True,
    }
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=_FINAL_GRACE_SECONDS,
                                     follow_redirects=True) as client:
            async with client.stream("POST", base + "/chat/completions",
                                     content=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                     headers=headers) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                        delta = chunk["choices"][0].get("delta", {}).get("content")
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
                    if delta:
                        yield delta
    except httpx.HTTPStatusError as exc:
        raise GenerationError(
            f"模型服务返回 HTTP {exc.response.status_code}，请检查服务配置") from exc
    except (httpx.RequestError, httpx.InvalidURL, OSError) as exc:
        raise GenerationError("无法连接模型服务或请求超时") from exc


async def _stream_events(text: str) -> AsyncIterator[str]:
    mode = os.getenv("MINDMAP_LLM_MODE", "demo").strip().lower()

    if mode == "demo":
        yield _sse({"type": "stage", "stage": "demo", "progress": 0.5,
                    "detail": "演示模式，不调用模型"})
        content = _demo_content(text)
        yield _sse({"type": "skeleton", "content": content.model_dump()})
        yield _sse({"type": "done", "mode": "demo", "content": content.model_dump(),
                    "latencyMs": 0, "degraded": False})
        return

    if mode != "model":
        raise GenerationError("MINDMAP_LLM_MODE 只能是 demo 或 model")

    started = time.monotonic()
    yield _sse({"type": "stage", "stage": "outline", "progress": 0.1,
                "detail": "正在生成（节点会边生成边出现）"})

    buffer = ""
    seen: dict[str, str] = {}
    last_parse = 0.0
    last_len = 0
    pushed = 0

    async for delta in _stream_chat(DETAIL_PROMPT, text):
        buffer += delta
        now = time.monotonic()
        # 两个条件都满足才解析：既隔了足够时间，又攒了足够多新字符。
        if now - last_parse < _PARSE_INTERVAL_SECONDS and len(buffer) - last_len < 40:
            continue
        last_parse = now
        last_len = len(buffer)
        partial = parse_partial(buffer)
        if partial is None:
            continue
        for parent_id, node in collect_new_nodes(partial, seen, buffer):
            pushed += 1
            yield _sse({"type": "node", "parentId": parent_id, "node": node,
                        "index": pushed})

    # 收尾：必须拿到完整合法结构，否则说明这次生成不可用
    try:
        raw = json.loads(_strip_fence(buffer))
        content = MindMapContent.model_validate(raw)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise GenerationError("模型返回内容不是符合约定的导图 JSON，请重试") from exc
    content.source_usage = []

    # 光有根节点不算生成成功：F01 要的是层次结构，不是一条标题。
    if len(content.root.children) < MIN_BRANCHES:
        raise GenerationError("模型只返回了一个根节点，没有任何分支，请换一种说法重试")

    latency = int((time.monotonic() - started) * 1000)
    yield _sse({"type": "done", "mode": "model", "content": content.model_dump(),
                "latencyMs": latency, "degraded": False, "streamedNodes": pushed})


async def generate_stream_partial(text: str) -> AsyncIterator[str]:
    """流式生成的唯一出口：若干 `data: {json}`，末尾必然是 `data: [DONE]`。

    被取消时不吞 CancelledError，让它继续上抛，连接才会真的断开。
    """
    try:
        async for frame in _stream_events(text):
            yield frame
    except GenerationError as exc:
        yield _sse({"type": "error", "code": "generation_failed", "message": str(exc)})
    except BaseException:  # noqa: BLE001 - 含 CancelledError，必须原样上抛
        raise
    yield _DONE_FRAME


__all__ = [
    "generate_stream_partial",
    "parse_partial",
    "collect_new_nodes",
]
