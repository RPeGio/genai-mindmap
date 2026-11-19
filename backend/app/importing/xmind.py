"""XMind（`.xmind`）→ 树。零依赖：`zipfile` + `json` / `xml.etree`。

`.xmind` 就是一个 zip，里面两代内容表：

    新版（XMind Zen / 2020+）  content.json   顶层是一个数组，每个元素是一张"画布"
    旧版（XMind 8）            content.xml    `<xmap-content><sheet><topic>…`

两代都支持，因为用户从别的工具迁过来时手里是哪一代完全不由我们决定。判定顺序是
"先 json 再 xml"：同一个文件里两代并存时（转换工具留下的）新版更完整。

新版 JSON 的形状（只列我们读的字段）：

    [ { "id": …, "class": "sheet", "title": "画布 1",
        "rootTopic": {
          "id": …, "title": "中心主题",
          "structureClass": "org.xmind.ui.logic.right",
          "children": { "attached": [ <topic>, … ], "detached": [ … ] },
          "notes": { "plain": { "content": "备注" } },
          "markers": [ { "markerId": "star-red" } ],
          "style": { "id": …, "properties": { "svg:fill": "#FF0000" } } } } ]

读到 / 丢掉的清单，每一条都有明确理由：

  - `children.attached` → 子节点（这是唯一的层级来源）。
  - `children.detached` → **丢掉**并计数：游离主题在 XMind 里不属于任何父节点，
    而我们的每个节点都必须挂在树上。塞到根下会假装它们是根的分支。
  - `notes.plain.content` → `note`（用户自己写的备注，契约里 `note` 的语义正好）。
    `realHTML` 变体去掉标签后同样处理。
  - `style.properties["svg:fill"]` → 颜色。能精确映射到调色板键才设 `color`，
    否则只把 hex 留在 `NodeStyle.color`（界面不渲染，但用户点一次保存不会丢）。
  - `markers[].markerId` → 图标，只认 `colors.xmind_icon` 里的表。
  - `structureClass` → `layout`（并在一级结构是鱼骨时把 `diagram_type` 设成
    fishbone）。
  - 多张画布 → 只导第一张并报一条提示。合并多张画布会把两张本来独立的图缝成
    一棵树，那比"少导一张"难解释得多。

单个 `content.xml` 文件也接受：用户从 zip 里解出来只拿到它，而它自证是 XMind 的
内容表，没必要逼他重新打包。
"""
from __future__ import annotations

import json
import re
import zipfile
from io import BytesIO
from xml.etree.ElementTree import Element

from app.importing.colors import normalize_hex, palette_for_hex, xmind_icon
from app.importing.detect import zip_member
from app.importing.errors import ParseFailure, UnsupportedFormat
from app.importing.model import FORMAT_NOTICE, Budget, RawMap, RawNode
from app.importing.text import (
    children_named,
    decode_text,
    first_child,
    local_name,
    parse_xml,
    text_of,
)

# 单个 zip 成员解压后的上限。见 `_read_member`：这是"压缩炸弹"的防线，
# 与 `model.RAW_NODE_HARD_CAP`（节点数）不是一回事——后者挡"很多节点"，
# 这里挡"一个成员就几 GB"。
XMIND_MEMBER_MAX_BYTES = 20 * 1024 * 1024

# `structureClass` → 我们的 `layout`。只收 XMind 官方的那几个结构类：第三方工具
# 写自定义值时按自动布局处理（`tree`），不猜。
_STRUCTURE_LAYOUTS = {
    "org.xmind.ui.map.unbalanced": "tree",
    "org.xmind.ui.map.balanced": "tree",
    "org.xmind.ui.map.clockwise": "tree",
    "org.xmind.ui.map.anticlockwise": "tree",
    "org.xmind.ui.logic.right": "logic",
    "org.xmind.ui.logic.left": "logic",
    "org.xmind.ui.org-chart.down": "vertical",
    "org.xmind.ui.org-chart.up": "vertical",
    "org.xmind.ui.tree.right": "tree",
    "org.xmind.ui.tree.left": "tree",
    "org.xmind.ui.timeline.horizontal": "timeline",
    "org.xmind.ui.timeline.vertical": "vertical",
    "org.xmind.ui.fishbone.leftHeaded": "fishbone",
    "org.xmind.ui.fishbone.rightHeaded": "fishbone",
}

# `realHTML` 备注去标签。不用完整 HTML 解析器：备注里需要保留的只有文字，
# 而一个正则足够处理 XMind 自己产出的那几个标签（`<p>`/`<br>`/`<span>`）。
_TAG = re.compile(r"<[^>]+>")
_BREAK = re.compile(r"<\s*br\s*/?\s*>|<\s*/\s*p\s*>", re.IGNORECASE)


def _clean_html(text: str) -> str:
    """HTML 片段 → 纯文本：换行标签变空格，其余标签去掉，实体反转义。"""
    import html

    return html.unescape(_BREAK.sub(" ", _TAG.sub("", text))).strip()


def _topic_title(topic: dict) -> str:
    title = topic.get("title")
    return title.strip() if isinstance(title, str) else ""


def _topic_note(topic: dict) -> str | None:
    notes = topic.get("notes")
    if not isinstance(notes, dict):
        return None
    for key in ("plain", "realHTML"):
        block = notes.get(key)
        if isinstance(block, dict):
            content = block.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip() if key == "plain" else _clean_html(content)
    return None


def _style_of(topic: dict) -> dict | None:
    """把 XMind 的样式压成我们能存的最小形状：`{"color": "#rrggbb"}`。

    `color` 与 `NodeStyle.color` 的分工见 `contracts.mindmap.NodeStyle`：hex 不
    渲染，只用来"点一次保存不丢原色"。所以哪怕映射不到调色板键，hex 也留着。
    """
    style = topic.get("style")
    if not isinstance(style, dict):
        return None
    candidates: list[object] = []
    properties = style.get("properties")
    if isinstance(properties, dict):
        for key in ("svg:fill", "fill", "fo:background-color", "background-color"):
            candidates.append(properties.get(key))
    for key in ("fill", "color"):
        candidates.append(style.get(key))
    for value in candidates:
        hex_value = normalize_hex(value)
        if hex_value:
            return {"color": hex_value}
    return None


def _topic_color(topic: dict) -> str | None:
    """`style` 里的填充色 → 调色板键；映射不到返回 None（不猜）。"""
    style = _style_of(topic)
    if style is None:
        return None
    return palette_for_hex(style.get("color"))


def _topic_icon(topic: dict) -> str | None:
    markers = topic.get("markers")
    if not isinstance(markers, list):
        return None
    for marker in markers:
        if isinstance(marker, dict):
            icon = xmind_icon(marker.get("markerId"))
            if icon:
                return icon
    return None


def _attached(topic: dict) -> list[dict]:
    children = topic.get("children")
    if not isinstance(children, dict):
        return []
    attached = children.get("attached")
    if not isinstance(attached, list):
        return []
    return [item for item in attached if isinstance(item, dict)]


def _detached_count(topic: dict) -> int:
    children = topic.get("children")
    if not isinstance(children, dict):
        return 0
    detached = children.get("detached")
    return len(detached) if isinstance(detached, list) else 0


def _structure(topic: dict) -> str | None:
    value = topic.get("structureClass") or topic.get("structure-class")
    return value if isinstance(value, str) else None


def _layout_of(topic: dict) -> str | None:
    return _STRUCTURE_LAYOUTS.get(_structure(topic) or "")


def _build_from_topics(root_topic: dict) -> tuple[RawNode, int, bool]:
    """topic 树 → RawNode 树。返回 `(根, 丢掉的游离主题数, 是否撞了硬上限)`。"""
    budget = Budget()
    budget.take()
    # 空标题不在这里兜底：`normalize` 统一填「（无标题）」并计数。
    root = RawNode(label=_topic_title(root_topic))
    root.note = _topic_note(root_topic)
    root.color = _topic_color(root_topic)
    root.icon = _topic_icon(root_topic)
    root.style = _style_of(root_topic)

    detached = _detached_count(root_topic)
    over_cap = False
    stack: list[tuple[dict, RawNode]] = [(root_topic, root)]
    while stack:
        topic, node = stack.pop()
        for child in _attached(topic):
            if not budget.take():
                over_cap = True
                break
            child_node = RawNode(label=_topic_title(child))
            child_node.note = _topic_note(child)
            child_node.color = _topic_color(child)
            child_node.icon = _topic_icon(child)
            child_node.style = _style_of(child)
            node.children.append(child_node)
            detached += _detached_count(child)
            stack.append((child, child_node))
        if over_cap:
            break
    return root, detached, over_cap


def _sheets_of(document: object) -> list[dict]:
    """从 content.json 的三种形态里取出画布列表。"""
    if isinstance(document, list):
        return [item for item in document if isinstance(item, dict)]
    if isinstance(document, dict):
        sheets = document.get("sheets")
        if isinstance(sheets, list):
            return [item for item in sheets if isinstance(item, dict)]
        # 只有一个 topic 的情况：`{"rootTopic": …}` 或 topic 本身。
        return [document]
    return []


def _parse_json(text: str) -> RawMap:
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ParseFailure(
            f"content.json 第 {exc.lineno} 行第 {exc.colno} 列解析失败：{exc.msg}") from exc

    sheets = _sheets_of(document)
    if not sheets:
        raise UnsupportedFormat(
            "content.json 里没有任何画布（它既不是 XMind 的数组格式，也不是单个主题）")
    sheet = sheets[0]
    root_topic = sheet.get("rootTopic")
    if not isinstance(root_topic, dict):
        # 少数工具把 topic 直接放在顶层：有 title 或 children 就当主题用。
        root_topic = sheet if ("children" in sheet or "title" in sheet) else None
    if not isinstance(root_topic, dict):
        raise UnsupportedFormat("content.json 里没有 rootTopic（找不到中心主题）")

    root, detached, over_cap = _build_from_topics(root_topic)
    hints: list[tuple[str, str]] = []
    if len(sheets) > 1:
        hints.append((
            FORMAT_NOTICE,
            f"这个文件有 {len(sheets)} 张画布，只导入了第一张；"
            "多张画布合并成一棵树会让两张本来独立的图混在一起",
        ))
    if detached:
        hints.append((
            FORMAT_NOTICE,
            f"{detached} 个游离主题（floating topic）没有导入：它们不属于任何父节点，"
            "而我们的每个节点都必须挂在树上",
        ))

    layout = _layout_of(root_topic)
    title = _topic_title(root_topic) or None
    return RawMap(
        root=root,
        title=title,
        layout=layout,
        diagram_type="fishbone" if layout == "fishbone" else None,
        hints=hints,
        over_hard_cap=over_cap,
    )


def _xml_topics(topic: Element) -> tuple[list[Element], int]:
    """旧版 `<children><topics type="attached">` 里的子主题，以及游离主题数。"""
    attached: list[Element] = []
    detached = 0
    for children in children_named(topic, "children"):
        for topics in children_named(children, "topics"):
            kind = topics.get("type") or "attached"
            items = children_named(topics, "topic")
            if kind == "detached":
                detached += len(items)
            else:
                attached.extend(items)
    return attached, detached


def _xml_note(topic: Element) -> str | None:
    notes = first_child(topic, "notes")
    if notes is None:
        return None
    plain = first_child(notes, "plain")
    text = text_of(plain) if plain is not None else text_of(notes)
    return text or None


def _xml_icon(topic: Element) -> str | None:
    refs = first_child(topic, "marker-refs")
    if refs is None:
        return None
    for ref in children_named(refs, "marker-ref"):
        icon = xmind_icon(ref.get("marker-id"))
        if icon:
            return icon
    return None


def _parse_xml_content(text: str) -> RawMap:
    document = parse_xml(text, "XMind content.xml")
    if local_name(document.tag) != "xmap-content":
        raise UnsupportedFormat(
            f"这不是 XMind 的内容表：根元素是 <{local_name(document.tag) or '?'}>，"
            "应该是 <xmap-content>")
    sheets = children_named(document, "sheet")
    if not sheets:
        raise ParseFailure("XMind content.xml 里没有 <sheet>（没有画布）")
    sheet = sheets[0]
    root_topic = first_child(sheet, "topic")
    if root_topic is None:
        raise ParseFailure("XMind content.xml 的 <sheet> 里没有 <topic>（找不到中心主题）")

    budget = Budget()
    budget.take()
    root = RawNode(label=text_of(first_child(root_topic, "title")))
    root.note = _xml_note(root_topic)
    root.icon = _xml_icon(root_topic)

    detached = 0
    over_cap = False
    stack: list[tuple[Element, RawNode]] = [(root_topic, root)]
    while stack:
        topic, node = stack.pop()
        items, floating = _xml_topics(topic)
        detached += floating
        for child in items:
            if not budget.take():
                over_cap = True
                break
            child_node = RawNode(label=text_of(first_child(child, "title")))
            child_node.note = _xml_note(child)
            child_node.icon = _xml_icon(child)
            node.children.append(child_node)
            stack.append((child, child_node))
        if over_cap:
            break

    hints: list[tuple[str, str]] = []
    if len(sheets) > 1:
        hints.append((
            FORMAT_NOTICE,
            f"这个文件有 {len(sheets)} 张画布，只导入了第一张",
        ))
    if detached:
        hints.append((
            FORMAT_NOTICE,
            f"{detached} 个游离主题没有导入：它们不属于任何父节点",
        ))

    structure = root_topic.get("structure-class")
    layout = _STRUCTURE_LAYOUTS.get(structure or "")
    title = text_of(first_child(sheet, "title")) or None
    return RawMap(
        root=root,
        title=title,
        layout=layout,
        diagram_type="fishbone" if layout == "fishbone" else None,
        hints=hints,
        over_hard_cap=over_cap,
    )


def _read_member(archive: zipfile.ZipFile, name: str) -> bytes:
    """读一个 zip 成员，**限制解压后大小**。

    为什么必须有这一道：`archive.read(name)` 会把整个成员解进内存，而一个 10MB
    的 zip 用 1000:1 的压缩比能解出 10GB——2C2G 的机器当场 OOM。四万行的
    `content.json` 已经是一张很大的导图，20MB 给足了余量。

    `file_size` 来自中央目录，理论上可以被伪造，所以读完再按实际长度判一次；
    两次都过不去就当"压缩炸弹"拒绝。
    """
    info = archive.getinfo(name)
    if info.file_size > XMIND_MEMBER_MAX_BYTES:
        raise ParseFailure(
            f"XMind 的 {name} 解压后有 {info.file_size // (1024 * 1024)}MB，"
            f"超过 {XMIND_MEMBER_MAX_BYTES // (1024 * 1024)}MB 上限；"
            "这看起来不是一份正常的导图文件")
    with archive.open(name) as handle:
        data = handle.read(XMIND_MEMBER_MAX_BYTES + 1)
    if len(data) > XMIND_MEMBER_MAX_BYTES:
        raise ParseFailure(
            f"XMind 的 {name} 解压后超过 {XMIND_MEMBER_MAX_BYTES // (1024 * 1024)}MB 上限")
    return data


def _from_zip(raw: bytes) -> RawMap:
    try:
        archive = zipfile.ZipFile(BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise ParseFailure(
            f"这是个 zip 但打不开（{exc}）：文件可能没下载完或者损坏了") from exc

    names = archive.namelist()
    json_name = zip_member(names, "content.json")
    xml_name = zip_member(names, "content.xml")
    if json_name is None and xml_name is None:
        raise UnsupportedFormat(
            "这是一个 zip，但里面没有 XMind 的 content.json / content.xml；"
            "如果这是 .docx / .xlsx / .pptx，请改用对应的材料上传入口")

    try:
        if json_name is not None:
            return _parse_json(_read_member(archive, json_name).decode("utf-8-sig"))
        assert xml_name is not None
        return _parse_xml_content(decode_text(_read_member(archive, xml_name)))
    except (KeyError, RuntimeError, zipfile.BadZipFile) as exc:
        # 加密的成员、CRC 对不上的成员都会在这里抛。它们是"文件坏了"，不是"格式不支持"。
        raise ParseFailure(f"读不出 XMind 的内容表：{exc}") from exc


def parse(raw: bytes) -> RawMap:
    if raw[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08", b"PK\x06\x06"):
        return _from_zip(raw)
    if not raw.strip():
        raise ParseFailure("文件是空的")
    return _parse_xml_content(decode_text(raw))
