"""Markdown 大纲 → 树。

对应 `export.service.to_markdown` 的精确形状：

    # 标题
    <空行>
    - 根节点文字
      - 子节点（2 个空格）
        - 孙节点（4 个空格）
    <空行>
    ## 附加关系
    <空行>
    - 甲 → 乙（是）

四条规则，都是为了"导出改一改还能导回来"：

  1. **层级靠缩进栈，不靠固定的 2 空格倍数。** 导出写的是 2 空格，但用户很可能
     用编辑器重新缩进成 4 空格或 Tab；按"除以 2"算层级会把 4 空格的文件读成
     8 层假深度，然后被 8 层上限整段砍掉。栈只比较相邻两行的缩进大小，任何
     一致的缩进风格都能读出同一棵树。
  2. **`# 标题` 是第一行标题**，`## 附加关系` 之后的行是边，不是树。
  3. **`&lt;` / `&gt;` 反转义**。导出为了不让渲染器把 `<T>` 当标签吞掉而转义了
     尖括号；导入不还原的话，往返一次标题就永久多了四个字母。
     （`&amp;` 不在导出侧转义，所以这里也不处理——多做一步会把用户正文里本来
     就写着的 `&lt;` 变成 `<`。）
  4. **`- [x]` 这类待办语法映射成视觉字段**：`[x]` → 绿 + ✅（已完成），
     `[ ]` → 琥珀（待办）。这是唯一的"逆向映射"，且只认这两个确切写法；别的
     方括号前缀（`[1]`、`[注]`）原样留在标题里，不猜。
"""
from __future__ import annotations

import re

from app.export.service import EDGE_KIND_LABELS, EDGES_SECTION
from app.importing.errors import ParseFailure
from app.importing.model import FORMAT_NOTICE, RAW_NODE_HARD_CAP, RawEdge, RawMap, RawNode
from app.importing.text import decode_text

_ITEM = re.compile(r"^(?P<indent>[ \t]*)(?:[-*+]|\d{1,2}[.)、])\s+(?P<text>.*)$")
_TITLE = re.compile(r"^#{1,6}\s+(?P<text>.*?)\s*#*\s*$")
_FENCE = re.compile(r"^\s{0,3}(?:```|~~~)")
_EDGES_HEADER = re.compile(r"^#{1,6}\s+" + re.escape(EDGES_SECTION) + r"\s*$")

_DONE = re.compile(r"^\[[xX]\]\s*")
_OPEN = re.compile(r"^\[\s?\]\s*")

# 边行末尾的 `（标签）`。全角与半角括号都收：导出写全角，但用户可能改成半角。
_EDGE_LABEL = re.compile(r"[（(]([^（）()]*)[）)]\s*$")

# 箭头。导出写 `→`；用户手改时很可能写成 `->` 或 `-->`。
_ARROWS = ("→", "-->", "->", "⇒", "=>")

# `kind` 的中文说法 → 枚举。表从 `export.service.EDGE_KIND_LABELS` 反着建，
# 不另写一份：导出在边没有 label 时写"顺序/条件分支/回退"，导入必须把它读回
# kind。两边各写一份的结局是某天改了文案之后，回退线往返一次就从虚线变实线。
KIND_FROM_LABEL = {label: kind for kind, label in EDGE_KIND_LABELS.items()}


def _indent_width(prefix: str) -> int:
    """Tab 按 4 个空格记。只有相对大小有意义，具体数值不重要。"""
    return len(prefix.replace("\t", "    "))


def _split_checkbox(text: str) -> tuple[str, str | None, str | None]:
    """剥掉待办前缀，返回 `(正文, 图标, 颜色)`。"""
    if _DONE.match(text):
        return _DONE.sub("", text, count=1).strip(), "✅", "green"
    if _OPEN.match(text):
        return _OPEN.sub("", text, count=1).strip(), None, "amber"
    return text, None, None


def _unescape(text: str) -> str:
    """还原导出时转义的尖括号（只这两个，理由见模块 docstring 第 3 条）。"""
    return text.replace("&lt;", "<").replace("&gt;", ">")


def _label_of(text: str) -> tuple[str, str | None, str | None]:
    """剥待办前缀 + 反转义。**空文字不兜底**：`normalize` 统一填「（无标题）」
    并计数——一处兜底才报得出"文件里有 N 个节点没有文字"。"""
    body, icon, color = _split_checkbox(text.strip())
    return _unescape(body).strip(), icon, color


def edge_of(text: str) -> RawEdge | None:
    """`甲 → 乙（是）` → RawEdge。认不出箭头返回 None（该行不是边）。

    markdown 与 opml 共用：导出的边行在两种格式里是同一段文字（`_edge_line`），
    所以解析也必须只有一份。两处各写一遍的结局是 `→` 有一边不认、或者尾部
    `（是）` 在一处被当成节点名的一部分。
    """
    body = text.strip()
    for arrow in _ARROWS:
        if arrow in body:
            left, _, right = body.partition(arrow)
            break
    else:
        return None
    label: str | None = None
    matched = _EDGE_LABEL.search(right)
    if matched:
        label = matched.group(1).strip() or None
        right = right[:matched.start()]
    source, target = left.strip(), right.strip()
    if not source or not target:
        return None
    kind = KIND_FROM_LABEL.get(label or "")
    return RawEdge(source=source, target=target, label=label, kind=kind)


def parse(raw: bytes) -> RawMap:
    text = decode_text(raw)
    title: str | None = None
    items: list[tuple[int, str, str | None, str | None, int]] = []
    edge_texts: list[str] = []
    in_fence = False
    in_edges = False
    over_cap = False

    for lineno, line in enumerate(text.splitlines(), 1):
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence or not line.strip():
            continue
        if _EDGES_HEADER.match(line):
            in_edges = True
            continue
        heading = _TITLE.match(line)
        if in_edges:
            # 附加关系区遇到下一个标题就结束：用户可能在自己的大纲里接着写
            # 「## 备注」，把那些行当边解析会凭空造出几条连不上的关系。
            if heading:
                in_edges = False
                continue
            item = _ITEM.match(line)
            if item:
                edge_texts.append(item.group("text"))
            continue
        if heading:
            if title is None:
                title = _unescape(heading.group("text").strip()) or None
            continue
        item = _ITEM.match(line)
        if item:
            # 行级上限。一份 10MB 的 markdown 可以有几十万行 `- x`，而节点预算是
            # 在构造 RawNode 时才扣的——不在收集这一层截住，列表本身就会先把内存
            # 吃掉（每项一个 tuple）。与 `RAW_NODE_HARD_CAP` 同一个数字。
            if len(items) >= RAW_NODE_HARD_CAP:
                over_cap = True
                break
            label, icon, color = _label_of(item.group("text"))
            items.append((_indent_width(item.group("indent")), label, icon, color, lineno))

    if not items:
        raise ParseFailure(
            "Markdown 里没有找到任何列表项：导入需要一份 `- 标题` 形式的大纲，"
            "或者用本仓库导出的 Markdown")

    indent, label, icon, color, _ = items[0]
    root = RawNode(label=label, icon=icon, color=color)
    stack: list[tuple[int, RawNode]] = [(indent, root)]
    top_level = 0
    for item_indent, item_label, item_icon, item_color, _ in items[1:]:
        while len(stack) > 1 and stack[-1][0] >= item_indent:
            stack.pop()
        if len(stack) == 1 and item_indent <= stack[0][0]:
            # 与根同缩进的第二个顶层项目。我们的模型只有一个根，丢内容比挂错
            # 位置糟，所以归到根节点下面并报一条提示。
            top_level += 1
        node = RawNode(label=item_label, icon=item_icon, color=item_color)
        stack[-1][1].children.append(node)
        stack.append((item_indent, node))

    edges: list[RawEdge] = []
    for edge_text in edge_texts:
        edge = edge_of(edge_text)
        if edge is not None:
            edges.append(edge)

    hints: list[tuple[str, str]] = []
    if top_level:
        hints.append((
            FORMAT_NOTICE,
            f"有 {top_level} 个项目与根节点同级，已归到根节点下（我们的导图只有一个根）",
        ))

    return RawMap(root=root, title=title, edges=edges, hints=hints,
                  over_hard_cap=over_cap)
