"""我们自己的 JSON → 树。**这是唯一无损的一条路**，也是"导出再导入"的保底。

支持三种外壳，都是真实会遇到的：

    {"title": …, "root": …}                      # 裸的 MindMapContent
    {"content": {…}}                             # 保存请求体 `SaveRequest`
    {"id": …, "version": 3, "content": {…}}      # 库里的 `MapRecord`

字段逐个挑，**不把整份 dict 交给 pydantic**。原因不是洁癖：`MindMapContent` 是
`extra="forbid"`，而外部 JSON 里多一个 `"highlight"` 就会让整份导入 422，错误
信息指向一条用户改不掉的字段名。逐字段挑的代价是"我们多了一个字段要在这里补"
——这件事有 `test_import_json_roundtrip` 钉着（导出加了字段而导入不认，往返用例
立刻红）。

取舍记在这里：

  - `source_refs` / `source_usage` **只计数、不保留**（理由见
    `normalize` 模块 docstring 的第 ⑤ 条）。
  - `color` 是调色板键就留；是别的值（hex、中文色名）时**不塞进 `color`**——
    `color` 的语义是"渲染意图"，猜错会让整张图染错色。能认出 hex 就存进
    `style.color`（`NodeStyle.color` 的注释写明它就是为导入准备的，界面不渲染，
    但用户点一次保存不会丢）。
  - `layout` / `theme` / `diagram_type` 原样交给 `normalize` 校验：那里才知道
    `manual` 与坐标的联动约束。
"""
from __future__ import annotations

import json

from app.contracts.mindmap import NODE_COLORS, NODE_EXPANDABLE
from app.importing.colors import normalize_hex
from app.importing.errors import ParseFailure, UnsupportedFormat
from app.importing.model import Budget, RawEdge, RawMap, RawNode
from app.importing.text import decode_text


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _raw_node(data: dict) -> RawNode:
    """一个节点 dict → RawNode。未知键全部丢掉。"""
    node = RawNode(label=_text(data.get("label")) or "")
    old_id = data.get("id")
    if isinstance(old_id, str) and old_id.strip():
        node.old_id = old_id.strip()
    node.note = _text(data.get("note"))
    node.detail = _text(data.get("detail"))

    color = data.get("color")
    if isinstance(color, str):
        key = color.strip().lower()
        if key in NODE_COLORS:
            node.color = key
    style = data.get("style")
    if isinstance(style, dict):
        copied = {k: style.get(k) for k in ("color", "icon", "bold", "italic", "shape")}
        # 调色板键之外的颜色：认得 hex 就塞进 `style.color` 保留原值。
        if node.color is None:
            hex_value = normalize_hex(color)
            if hex_value:
                copied["color"] = hex_value
        node.style = copied

    icon = data.get("icon")
    if isinstance(icon, str) and icon.strip():
        node.icon = icon.strip()
    node.collapsed = bool(data.get("collapsed"))
    expandable = data.get("expandable")
    if isinstance(expandable, str) and expandable in NODE_EXPANDABLE:
        node.expandable = expandable
    pos = data.get("pos")
    if isinstance(pos, dict):
        node.pos = pos
    return node


def _parse_node(data: dict, budget: Budget) -> tuple[RawNode, int, bool]:
    """节点 dict → RawNode 树。返回 `(根, 引用条数, 是否撞硬上限)`。"""
    root = _raw_node(data)
    refs = len(data.get("source_refs") or []) if isinstance(data.get("source_refs"), list) else 0
    over_cap = False
    stack: list[tuple[dict, RawNode]] = [(data, root)]
    while stack:
        source, node = stack.pop()
        children = source.get("children")
        if not isinstance(children, list):
            continue
        for child in children:
            if not isinstance(child, dict):
                continue
            if not budget.take():
                over_cap = True
                break
            child_node = _raw_node(child)
            if isinstance(child.get("source_refs"), list):
                refs += len(child["source_refs"])
            node.children.append(child_node)
            stack.append((child, child_node))
        if over_cap:
            break
    return root, refs, over_cap


def _raw_edges(body: dict) -> list[RawEdge]:
    edges: list[RawEdge] = []
    items = body.get("edges")
    if not isinstance(items, list):
        return edges
    for item in items:
        if not isinstance(item, dict):
            continue
        source = item.get("source")
        target = item.get("target")
        if not isinstance(source, str) or not isinstance(target, str):
            continue
        label = item.get("label")
        kind = item.get("kind")
        edges.append(RawEdge(
            source=source, target=target,
            label=label if isinstance(label, str) else None,
            kind=kind if isinstance(kind, str) else None,
        ))
    return edges


def _unwrap(document: object) -> dict:
    """剥掉 `{"content": …}` / `MapRecord` 的外壳。"""
    if isinstance(document, list):
        raise UnsupportedFormat(
            "JSON 顶层是数组：这不是一份导图。导入需要 MindMapContent（含 root 的对象）")
    if not isinstance(document, dict):
        raise UnsupportedFormat("JSON 顶层不是对象，读不出导图结构")
    inner = document.get("content")
    if isinstance(inner, dict):
        return inner
    return document


def parse(raw: bytes) -> RawMap:
    text = decode_text(raw)
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ParseFailure(
            f"JSON 第 {exc.lineno} 行第 {exc.colno} 列解析失败：{exc.msg}") from exc

    body = _unwrap(document)
    root_data = body.get("root")
    if not isinstance(root_data, dict):
        raise UnsupportedFormat(
            "这份 JSON 里没有 root 节点：它不是我们的导图格式"
            "（需要 {title, root: {id, label, children}}）")

    budget = Budget()
    budget.take()
    root, refs, over_cap = _parse_node(root_data, budget)

    relations = body.get("relations")
    counters: dict[str, int] = {}
    if refs:
        counters["source_refs"] = refs
    usage = body.get("source_usage")
    if isinstance(usage, list) and usage:
        counters["source_usage"] = len(usage)

    layout = body.get("layout")
    diagram_type = body.get("diagram_type")
    theme = body.get("theme")
    return RawMap(
        root=root,
        title=_text(body.get("title")),
        layout=layout if isinstance(layout, str) else None,
        diagram_type=diagram_type if isinstance(diagram_type, str) else None,
        theme=theme if isinstance(theme, dict) else None,
        relations=[item for item in (relations or []) if isinstance(item, dict)]
        if isinstance(relations, list) else [],
        edges=_raw_edges(body),
        counters=counters,
        over_hard_cap=over_cap,
    )
