"""增量 JSON 解析：模型还没写完，就把已经完整的节点挑出来。

服务于 agent 的节点生长预览（`agent/tools.py` 的 `_GenerateGrowStream`）：它一边
收模型的分片正文，一边调用这里，把"已经写完整"的节点报成 `node_grow` 帧。

增量解析的难点是 JSON 还没写完。这里的做法是「补齐括号」：扫描已收到的文本，
跟踪字符串状态与括号栈，在末尾补上缺失的引号与闭括号，再交给 json.loads。
截断在 `"label":` 这种位置时补出来的 JSON 仍然非法，函数返回 None 等下一批数据，
所以不会把半截内容当成合法结构。

这个文件原先还带一条完整的 SSE 出口（`generate_stream_partial` 及其帧拼装），
随 `POST /api/generations/stream` 一起删掉了。留下的是纯解析，与"用不用 SSE、
走哪条路由"无关——预览机制换了三轮，它一次都没改过。
"""
from __future__ import annotations

import json
import re


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


__all__ = [
    "parse_partial",
    "collect_new_nodes",
]
