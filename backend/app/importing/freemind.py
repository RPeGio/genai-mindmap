"""FreeMind（`.mm`）→ 树。

形状是递归的节点元素：

    <map version="1.0.1">
      <node TEXT="根" COLOR="#ff0000" FOLDED="true">
        <icon BUILTIN="idea"/>
        <richcontent TYPE="NODE"><html><body><p>备注</p></body></html></richcontent>
        <node TEXT="子"/>
      </node>
    </map>

读什么、丢什么：

  - `TEXT` → `label`。没有 TEXT 的节点按「（无标题）」处理（契约 label 非空）。
  - `COLOR`（hex）→ `color`：只走 `colors.palette_for_hex` 的精确表；映射不到的
    hex 留在 `style.color`，`color` 设空——**不猜**（把 `#7f7f7f` 猜成 gray 会让
    一整类颜色长得一样，而用户看不出是被我们改过）。
  - `<icon BUILTIN="…">` → `icon`，只认 `colors.freemind_icon` 的封闭枚举。
  - `<richcontent>` → `note`（FreeMind 里那是用户写在节点上的长文本，语义与
    `note` 一致）。HTML 标签去掉、实体反转义。
  - `FOLDED="true"` → `collapsed`。
  - `POSITION`（left/right）→ **丢掉**：它是"这一支画在左还是右"，而我们的
    `theme.direction` 是整图口径。把局部朝向抬成全局朝向是猜，猜错的图上所有
    节点都会换边。
  - `<attribute>`、`LINK`、`<font>` → 丢掉：契约里没有对应字段，编一个出来只会
    让保存时多出后端不认识的东西。
"""
from __future__ import annotations

import html
import re
from xml.etree.ElementTree import Element

from app.importing.colors import freemind_icon, normalize_hex, palette_for_hex
from app.importing.errors import ParseFailure, UnsupportedFormat
from app.importing.model import FORMAT_NOTICE, Budget, RawMap, RawNode
from app.importing.text import children_named, decode_text, local_name, parse_xml

_TAG = re.compile(r"<[^>]+>")
_BREAK = re.compile(r"<\s*br\s*/?\s*>|<\s*/\s*p\s*>", re.IGNORECASE)


def _clean_html(text: str) -> str:
    return html.unescape(_BREAK.sub(" ", _TAG.sub("", text))).strip()


def _note_of(node: Element) -> str | None:
    for child in node:
        if local_name(child.tag) == "richcontent":
            text = _clean_html("".join(child.itertext()))
            if text:
                return text
    return None


def _icon_of(node: Element) -> str | None:
    for child in node:
        if local_name(child.tag) == "icon":
            icon = freemind_icon(child.get("BUILTIN"))
            if icon:
                return icon
    return None


def _raw_node(node: Element) -> RawNode:
    # 空 TEXT 不在这里兜底：`normalize` 会统一填「（无标题）」并计数，一处兜底
    # 才能报出"文件里有 N 个节点没有文字"。这里兜了，那条告警永远数到 0。
    label = (node.get("TEXT") or "").strip()
    raw = RawNode(label=label)
    raw.note = _note_of(node)
    raw.icon = _icon_of(node)
    color = node.get("COLOR")
    if color:
        palette = palette_for_hex(color)
        if palette:
            raw.color = palette
        hex_value = normalize_hex(color)
        if hex_value:
            raw.style = {"color": hex_value}
    raw.collapsed = (node.get("FOLDED") or "").lower() == "true"
    return raw


def _attach_subtree(element: Element, node: RawNode, budget: Budget) -> bool:
    """把一个元素的 `<node>` 子树挂到 `node` 上。撞到预算返回 True。"""
    stack: list[tuple[Element, RawNode]] = [(element, node)]
    while stack:
        current, target = stack.pop()
        for child in children_named(current, "node"):
            if not budget.take():
                return True
            child_node = _raw_node(child)
            target.children.append(child_node)
            stack.append((child, child_node))
    return False


def parse(raw: bytes) -> RawMap:
    text = decode_text(raw)
    document = parse_xml(text, "FreeMind")
    if local_name(document.tag) != "map":
        raise UnsupportedFormat(
            f"这不是 FreeMind 文件：根元素是 <{local_name(document.tag) or '?'}>，"
            "应该是 <map>")
    nodes = children_named(document, "node")
    if not nodes:
        raise ParseFailure("FreeMind 文件里没有 <node>（没有任何节点）")

    budget = Budget()
    budget.take()
    root = _raw_node(nodes[0])
    over_cap = _attach_subtree(nodes[0], root, budget)

    hints: list[tuple[str, str]] = []
    if len(nodes) > 1 and not over_cap:
        # 多根 `.mm` 是合法的（FreeMind 允许一个 map 里有多个 `<node>`），但我们的
        # 模型只有一个根：其余挂到根下面并说出来。
        attached = 0
        for extra in nodes[1:]:
            if not budget.take():
                over_cap = True
                break
            extra_node = _raw_node(extra)
            root.children.append(extra_node)
            attached += 1
            if _attach_subtree(extra, extra_node, budget):
                over_cap = True
                break
        hints.append((
            FORMAT_NOTICE,
            f"这个文件有 {len(nodes)} 个顶层节点，已把其余 {attached} 个挂到根节点下"
            "（我们的导图只有一个根）",
        ))

    return RawMap(root=root, title=None, hints=hints, over_hard_cap=over_cap)
