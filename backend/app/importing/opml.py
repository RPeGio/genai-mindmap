"""OPML → 树。与 `export.service.to_opml` 一一对应。

导出的形状：

    <opml version="2.0">
      <head><title>标题</title></head>
      <body>
        <outline text="根">
          <outline text="子" />
        </outline>
        <outline text="附加关系">
          <outline text="甲 → 乙（是）" />
        </outline>
      </body>
    </opml>

三件事必须对上，否则"导出→导入"会静默丢东西：

  1. **`text` 属性是节点文字**（不是 `title`、不是元素内容）。XMind 导出的 OPML
     也写 `text`，所以这一条同时对得上别人的文件。
  2. **附加关系是 body 下的**另一棵** outline**，`text` 等于 `附加关系`。它不能
     当成根节点的兄弟混进树里，否则图上会多出一个叫"附加关系"的分支。
  3. **多个顶层 outline** 是合法 OPML，而我们的模型只有一个根：第一个当根，
     其余的挂到它下面并报一条提示——丢内容比挂错位置糟。

嵌套用显式栈而不是递归：`<outline>` 的层数由文件决定，一万层就能把递归打穿，
而那是 500（用户拿不到任何可操作的信息）而不是 400。
"""
from __future__ import annotations

from xml.etree.ElementTree import Element

from app.export.service import EDGES_SECTION
from app.importing.errors import ParseFailure
from app.importing.markdown import edge_of
from app.importing.model import FORMAT_NOTICE, Budget, RawMap, RawNode
from app.importing.text import children_named, decode_text, local_name, parse_xml, text_of


def _label(element: Element) -> str:
    """节点文字：`text` 属性优先，`title` 兜底（少数工具只写 title）。"""
    for key in ("text", "title", "_text"):
        value = element.get(key)
        if value and value.strip():
            return value.strip()
    return ""


def _detail(element: Element) -> str | None:
    """`_note` 属性 → `detail`。导出写的就是它（`export._opml_note`）。

    早先这里一个属性都不读，`_note` 落在文件里却没人看——`docs/IMPORT.md` 的
    往返矩阵因此自相矛盾（矩阵说 `detail` 在"往返之后还剩什么"里，括号里又写
    "导入侧不读它"，而实测导回来是空的；codex 复审 P3-5）。

    `_note` 是 OmniOutliner 用了几十年的备注约定，XMind / 幕布见到也只是一个普通
    属性——读它不会误伤别人的文件，读到空值就当没有。
    """
    value = element.get("_note")
    return value.strip() or None if value else None


def parse(raw: bytes) -> RawMap:
    text = decode_text(raw)
    document = parse_xml(text, "OPML")
    if local_name(document.tag) != "opml":
        raise ParseFailure(
            f"这不是 OPML：根元素是 <{local_name(document.tag) or '?'}>，应该是 <opml>")

    title = ""
    for head in children_named(document, "head"):
        titles = children_named(head, "title")
        if titles:
            title = text_of(titles[0])
            break

    bodies = children_named(document, "body")
    if not bodies:
        raise ParseFailure("OPML 里没有 <body>：这份文件里没有任何大纲")
    outlines = children_named(bodies[0], "outline")
    if not outlines:
        raise ParseFailure("OPML 的 <body> 里没有任何 <outline>")

    tree_outlines = [item for item in outlines if _label(item) != EDGES_SECTION]
    edge_outlines = [item for item in outlines if _label(item) == EDGES_SECTION]
    if not tree_outlines:
        raise ParseFailure(
            f"OPML 里只有一个「{EDGES_SECTION}」大纲，没有节点树")

    budget = Budget()
    budget.take()  # 根节点自己
    first = tree_outlines[0]
    # 文字为空不在这里兜底：`normalize` 统一填「（无标题）」并计数。
    root = RawNode(label=_label(first), detail=_detail(first))
    over_cap = False

    stack: list[tuple[Element, RawNode]] = [(first, root)]
    while stack:
        element, node = stack.pop()
        for child in children_named(element, "outline"):
            if not budget.take():
                over_cap = True
                break
            child_node = RawNode(label=_label(child), detail=_detail(child))
            node.children.append(child_node)
            stack.append((child, child_node))
        if over_cap:
            break

    hints: list[tuple[str, str]] = []
    if len(tree_outlines) > 1:
        attached = 0
        for extra in tree_outlines[1:]:
            if not budget.take():
                over_cap = True
                break
            extra_node = RawNode(label=_label(extra), detail=_detail(extra))
            root.children.append(extra_node)
            attached += 1
            # 额外顶层大纲自己的子树也要搬过来，否则它们会被静默丢掉。
            extra_stack: list[tuple[Element, RawNode]] = [(extra, extra_node)]
            while extra_stack:
                element, node = extra_stack.pop()
                for child in children_named(element, "outline"):
                    if not budget.take():
                        over_cap = True
                        break
                    child_node = RawNode(label=_label(child), detail=_detail(child))
                    node.children.append(child_node)
                    extra_stack.append((child, child_node))
                if over_cap:
                    break
        hints.append((
            FORMAT_NOTICE,
            f"这个 OPML 有 {len(tree_outlines)} 个顶层大纲，已把其余 {attached} 个"
            "挂到根节点下（我们的导图只有一个根）",
        ))

    edges = []
    for shelf in edge_outlines:
        for line in children_named(shelf, "outline"):
            edge = edge_of(_label(line))
            if edge is not None:
                edges.append(edge)

    return RawMap(
        root=root, title=title or None, edges=edges, hints=hints, over_hard_cap=over_cap)
