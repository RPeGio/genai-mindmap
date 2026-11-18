"""Markdown 大纲 → 树 / 鱼骨图 / 流程图。

对应 `export.service.to_markdown` 的**三种形状**。树那一份是精确形状：

    # 标题
    <空行>
    - 根节点文字
      > 生成说明（detail，可以没有）
      - 子节点（2 个空格）
        - 孙节点（4 个空格）
    <空行>
    ## 附加关系
    <空行>
    - 甲 → 乙（是）

鱼骨与流程的导出多一行**形状标记**（第二行的 HTML 注释），容器则靠多级标题。
**它们的缩进是 4 个空格**（树仍是 2 个，见下面第 1 条）：

    # 交付延期分析
    <!-- genai-mindmap: shape=fishbone -->

    - 为什么三次交付都延期        ← 主骨上那句话（problem）
        - 人                      ← 骨（category）
            - 关键人休假          ← 原因

    # 报销流程
    <!-- genai-mindmap: shape=flowchart -->

    ### 1. 提交                   ← 阶段（序号由导出写的，导入时剥掉）
    - 填单

七条规则，都是为了"导出改一改还能导回来"：

  1. **层级靠缩进栈，不靠固定的倍数。** 导出对鱼骨与流程写 **4 个空格**（2 个只有
     CommonMark 认，Python-Markdown 按 `tab_length=4` 判层级，会把骨读到鱼头那一级
     ——codex 复审 P2-1），对树写 2 个（树的输出被逐字节钉死，见
     `export.service._markdown_tree`）。用户也很可能用编辑器重新缩进成别的宽度或
     Tab；按"除以 N"算层级会把 4 空格的文件读成假深度，然后被 8 层上限整段砍掉。
     栈只比较相邻两行的缩进大小，任何一致的缩进风格都能读出同一棵树。
  2. **标题分两档。** **第一个**标题是导图标题；**之后**任何级别的标题是**容器**
     ——它自己是一个节点，它下面的列表项是它的内容。这条是补上的：早先只把第一个
     `#` 当标题，后面所有 heading 一律 `continue` 跳过，于是流程图的 `### 1. 提交`
     在导入侧没有落点，阶段名整段消失、那几行 `- 1. 提交 → 2. 付款（通过）` 反倒
     变成了节点。树的导出只写一个 `# 标题`，所以树这条路一个字都没变。
  3. **形状只看文件头的标记，不从形状硬推断。** `<!-- genai-mindmap: shape=… -->`
     是导出写的（见 `export.service.shape_marker`）；读不到就是树。**猜是不行的**：
     一份三级缩进大纲与一张只有两条原因的鱼骨图在文本上一模一样，猜错之后用户拿到
     的是一张"结构对、形状错"的图，而错误在画布上看起来完全正常。标记缺失/被删掉
     只是退回按树导入——有损，但可解释。
  4. **`&lt;` / `&gt;` 反转义**。导出为了不让渲染器把 `<T>` 当标签吞掉而转义了
     尖括号；导入不还原的话，往返一次标题就永久多了四个字母。
     （`&amp;` 不在导出侧转义，所以这里也不处理——多做一步会把用户正文里本来
     就写着的 `&lt;` 变成 `<`。）
  5. **`- [x]` 这类待办语法映射成视觉字段**：`[x]` → 绿 + ✅（已完成），
     `[ ]` → 琥珀（待办）。这是唯一的"逆向映射"，且只认这两个确切写法；别的
     方括号前缀（`[1]`、`[注]`）原样留在标题里，不猜。
  6. **`>` 引用块是节点的 `detail`。** 导出把生成说明放在节点行下面一句引用块里
     （`export._markdown_quote`），导入把它读回 `detail` 而不是塞进标题：那一行
     标题已经装了 `note or label`，再并进 142 字的说明，大纲就没法扫读了。
     引用块归属于**它上面那个节点**（不看缩进），所以用户重排缩进也不会挂错。
  7. **骨的 `category` 与骨根 `note` 在骨行下面一行注释里**
     （`<!-- genai-mindmap: bone {"category": …, "note": …} -->`，见
     `export._markdown_bone_comment`）。这两个字段在文字槽里没有位置——那一行写的
     是 `root.label`——此前它们**静默丢失**（codex 复审 P2-2）。注释里的值是 JSON，
     所以引号、空格、换行都不用另定一套转义。
"""
from __future__ import annotations

import json
import re

from app.export.service import (
    EDGE_KIND_LABELS,
    EDGES_SECTION,
    INNER_EDGES_SECTION,
    STAGE_EDGES_SECTION,
)
from app.importing.errors import ParseFailure
from app.importing.model import (
    FORMAT_NOTICE,
    RAW_NODE_HARD_CAP,
    SHAPE_FISHBONE,
    SHAPE_FLOWCHART,
    RawBone,
    RawEdge,
    RawMap,
    RawNode,
    RawStage,
)
from app.importing.text import decode_text

_ITEM = re.compile(r"^(?P<indent>[ \t]*)(?:[-*+]|\d{1,2}[.)、])\s+(?P<text>.*)$")
_TITLE = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<text>.*?)\s*#*\s*$")
_FENCE = re.compile(r"^\s{0,3}(?:```|~~~)")

# 导出写的形状标记。**整行匹配**：`<!--` 注释在 Markdown 里可以出现在任何地方，
# 只认我们自己那一行，别人写的注释不当成形状声明。
_SHAPE_MARKER = re.compile(
    r"^\s*<!--\s*genai-mindmap:\s*shape=(?P<shape>[a-z]+)\s*-->\s*$")

# 骨行下面那行元数据注释（`export._markdown_bone_comment`）：`category` 与骨根
# `note`。值是 JSON，所以这里也按 JSON 读——导出把值里的 `>` 写成了 `\u003e`，
# 那是为了不让注释提前结束，读回来还是一个 `>`。
_BONE_META = re.compile(
    r"^\s*<!--\s*genai-mindmap:\s*bone\s+(?P<payload>\{.*\})\s*-->\s*$")

# 三个边清单小节的标题 → 是哪一层。列表按**长的在前**排：`附加关系` 是另外两个的
# 前缀，交替分支短的排前面时靠 `$` 回溯也能对上，但写清楚顺序就不依赖这条推理。
_SECTIONS = (
    (INNER_EDGES_SECTION, "inner"),
    (STAGE_EDGES_SECTION, "stage"),
    (EDGES_SECTION, "edges"),
)
_SECTION_KIND = {text: kind for text, kind in _SECTIONS}
_EDGES_HEADER = re.compile(
    r"^#{1,6}\s+(?P<text>" + "|".join(re.escape(text) for text, _ in _SECTIONS)
    + r")\s*$")

# 节点下面那句引用块（`detail`）。允许任意缩进：归属由**位置**决定（上面那个
# 节点），不由缩进决定，所以用户重排缩进不会让说明挂到别人身上。
_DETAIL = re.compile(r"^[ \t]*>[ \t]?(?P<text>.*)$")

# 阶段标题上的序号（`1. 提交`）。只剥**一个**：阶段名本身以数字开头时
# （导出写 `1. 2. 天内付款`）剥掉的是我加的那个。
_STAGE_NUMBER = re.compile(r"^\d{1,3}[.、][ \t]*")
# 阶段标题里"名字 ｜ 正文"的分隔符，与 `export.service._flow_stage_heading` 同源。
_STAGE_BODY_SEP = "｜"
# 阶段内部的边：行首是 `[1. 提交]`，括号里是那个阶段的导出名。
_INNER_SCOPE = re.compile(r"^\[(?P<stage>[^\]]+)\]\s*(?P<rest>.+)$")

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
    """还原导出时转义的尖括号（只这两个，理由见模块 docstring 第 4 条）。"""
    return text.replace("&lt;", "<").replace("&gt;", ">")


def _label_of(text: str) -> tuple[str, str | None, str | None]:
    """剥待办前缀 + 反转义。**空文字不兜底**：`normalize` 统一填「（无标题）」
    并计数——一处兜底才报得出"文件里有 N 个节点没有文字"。"""
    body, icon, color = _split_checkbox(text.strip())
    return _unescape(body).strip(), icon, color


def _append_text(existing: str | None, extra: str) -> str:
    """把一句续行拼到已有文字后面。

    引用块可以连着写好几行（用户把一句长说明手动折行了）。用空格拼起来而不是换行：
    `detail` 在画布与导出里都是**一句**话，留着换行会在下一次导出时被 `_one_line`
    折掉——那时"往返之后多了一个换行符"就成了查无可查的怪事。
    """
    part = extra.strip()
    if not part:
        return existing or ""
    return f"{existing} {part}" if existing else part


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


def _attach(
    stack: list[tuple[int, RawNode]], node: RawNode, indent: int,
) -> bool:
    """按缩进把节点挂到栈上，返回它是否与**栈底**同级。

    栈底是这一层容器的根（文档根，或者一个标题容器）。所以"与栈底同级"只在文档
    这一层有意义——那是"这份大纲还有第二个顶层项目"。容器内部栈底是伪节点
    （缩进 -1），列表项不可能比它更浅，判据自然不成立。
    """
    while len(stack) > 1 and stack[-1][0] >= indent:
        stack.pop()
    same_level = len(stack) == 1 and indent <= stack[0][0]
    stack[-1][1].children.append(node)
    stack.append((indent, node))
    return same_level


def _heading_node(heading: re.Match[str]) -> RawNode:
    """一个容器标题 → 节点。标题文字是节点自己的标题（`## 人` → 节点「人」）。"""
    return RawNode(label=_unescape(heading.group("text").strip()))


def _stage_root(text: str) -> RawNode:
    """阶段标题 → 阶段根节点：`1. 提交｜把单据拍照上传` → `label="提交"`、`note=…`。

    `｜` 右边是阶段根的**正文**：`_flow_stage_heading` 在正文与标题不同时才补上它
    （"提交"与"把报销单和发票拍照上传"是同一件事的两个说法）。读回来落在 `note`：
    那是"正文"这个位置的字段，形状与导出时一致，再导一次得到的还是同一行。

    序号必须剥掉——留着它，下一次导出会写 `1. 1. 提交`。

    一个已知的窄口：阶段**标题本身**含 `｜` 时会被拆错（正文含 `｜` 没事，因为是
    `partition` 取第一个）。这个分隔符是导出侧定的约定（`_flow_stage_heading`），
    改它要同时动两边，而"标题里带全角竖线"的图在实测里没有出现过。
    """
    name = _STAGE_NUMBER.sub("", text, count=1).strip() or text
    label, _, body = name.partition(_STAGE_BODY_SEP)
    label = label.strip()
    return RawNode(label=label or name, note=body.strip() or None)


def _declared_shape(text: str) -> str | None:
    """文件头写着什么形状；没写（或写了不认识的值）返回 None。

    扫全文而不是只看前几行：标记是导出写的，位置固定在第二行，但用户可能把它挪到
    文件末尾以免破坏开头的美观。**它只是一个声明**，写在哪里都算数。
    """
    for line in text.splitlines():
        matched = _SHAPE_MARKER.match(line)
        if matched:
            return matched.group("shape")
    return None


def parse(raw: bytes) -> RawMap:
    text = decode_text(raw)
    shape = _declared_shape(text)
    if shape == SHAPE_FISHBONE:
        fishbone = _parse_fishbone(text)
        if fishbone is not None:
            return fishbone
        # 标着鱼骨但没有主骨上那句话（一个列表项都没有 / 一行 `- ` 都没有）：
        # 按树读，并把"降级"说出来。悄悄换一种形状比降级本身更难排查。
        tree = _parse_tree(text)
        tree.hints.append((
            FORMAT_NOTICE,
            "文件头声明的是鱼骨图，但没有找到主骨上的那句话（第一个列表项）；"
            "已按思维导图导入",
        ))
        return tree
    if shape == SHAPE_FLOWCHART:
        flow = _parse_flow(text)
        if flow is not None:
            return flow
        tree = _parse_tree(text)
        tree.hints.append((
            FORMAT_NOTICE,
            "文件头声明的是流程图，但没有找到阶段标题（`### 1. 阶段名`）；"
            "已按思维导图导入",
        ))
        return tree
    return _parse_tree(text)


def _parse_tree(text: str) -> RawMap:
    """经典路径：一棵树，外加可选的标题容器。**树的既有输入逐字段不变。**"""
    title: str | None = None
    items = 0
    edge_texts: list[str] = []
    section: str | None = None
    in_fence = False
    over_cap = False

    root: RawNode | None = None
    #: 文档级缩进栈（栈底是根节点）
    stack: list[tuple[int, RawNode]] = []
    #: 容器内缩进栈（栈底是那个容器，缩进记 -1 当哨兵）
    scoped: list[tuple[int, RawNode]] = []
    #: 标题容器栈，按 heading 级别嵌套：`## 人` 下面再来一个 `### 细节` 时，
    #: 后者是前者的子节点。
    headings: list[tuple[int, RawNode]] = []
    containers: list[RawNode] = []
    top_level = 0
    #: 下一行 `>` 引用块挂在谁身上。**所有节点行的最后一行**，与缩进无关。
    owner: RawNode | None = None

    for line in text.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence or not line.strip():
            continue
        if _SHAPE_MARKER.match(line):
            continue
        header = _EDGES_HEADER.match(line)
        if header:
            section = _SECTION_KIND[header.group("text")]
            continue
        heading = _TITLE.match(line)
        if heading and section is not None:
            # 附加关系区遇到下一个标题就结束：用户可能在自己的大纲里接着写
            # 「## 备注」，把那些行当边解析会凭空造出几条连不上的关系。
            section = None
        if section is not None:
            item = _ITEM.match(line)
            if item:
                edge_texts.append(item.group("text"))
            continue
        if heading:
            body = _unescape(heading.group("text").strip())
            if title is None:
                title = body or None
                continue
            node = _heading_node(heading)
            level = len(heading.group("hashes"))
            while headings and headings[-1][0] >= level:
                headings.pop()
            if headings:
                headings[-1][1].children.append(node)
            else:
                containers.append(node)
            headings.append((level, node))
            scoped = [(-1, node)]
            owner = node
            continue
        detail = _DETAIL.match(line)
        if detail:
            if owner is not None:
                owner.detail = _append_text(
                    owner.detail, _unescape(detail.group("text")))
            continue
        item = _ITEM.match(line)
        if item:
            # 行级上限。一份 10MB 的 markdown 可以有几十万行 `- x`，而节点预算是
            # 在构造 RawNode 时才扣的——不在收集这一层截住，列表本身就会先把内存
            # 吃掉（每项一个 tuple）。与 `RAW_NODE_HARD_CAP` 同一个数字。
            if items >= RAW_NODE_HARD_CAP:
                over_cap = True
                break
            items += 1
            label, icon, color = _label_of(item.group("text"))
            node = RawNode(label=label, icon=icon, color=color)
            indent = _indent_width(item.group("indent"))
            if headings:
                _attach(scoped, node, indent)
            elif root is None:
                root = node
                stack.append((indent, node))
            elif _attach(stack, node, indent):
                # 与根同缩进的第二个顶层项目。我们的模型只有一个根，丢内容比挂错
                # 位置糟，所以归到根节点下面并报一条提示。
                top_level += 1
            owner = node

    if root is None:
        if not containers:
            raise ParseFailure(
                "Markdown 里没有找到任何列表项：导入需要一份 `- 标题` 形式的大纲，"
                "或者用本仓库导出的 Markdown")
        # 只有标题容器（`### 1. 提交` 这类）：容器要有一个父亲，拿导图标题当根。
        # 这里 `title` 一定非 None——容器只可能在"第一个标题已经被当成标题"之后
        # 出现，否则那个标题会先被上面那条分支收走。
        root = RawNode(label=title or "")
        root.children.extend(containers)
    elif containers:
        # 既有文档级列表项又有标题容器（手写的混合文件）：容器挂到根下面，
        # 文字一个都不丢。
        root.children.extend(containers)

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


def _bone_payload(text: str) -> dict[str, str]:
    """骨注释里的 JSON → `{category, note}`。坏 JSON 当作没有（不猜、不报错）。

    只取这两个键：注释是导出写的，但它躺在**用户可编辑**的文件里，多出来的键
    没有落点，硬吞进 `RawNode` 的某个字段等于让文件决定语义。
    """
    try:
        payload = json.loads(text)
    except ValueError:
        return {}
    if not isinstance(payload, dict):
        return {}
    return {key: value for key, value in payload.items()
            if key in ("category", "note")
            and isinstance(value, str) and value.strip()}


def _parse_fishbone(text: str) -> RawMap | None:
    """鱼骨图 markdown → `RawMap(shape="fishbone")`。没有列表项时返回 None。

    形状在文件里就是缩进：**第一个**列表项是主骨上那句话，它的直接子项是骨，骨下面
    的原因照旧挂。所以这里不需要另写一套结构解析——`_attach` 就是全部。

    骨行下面那行 `<!-- genai-mindmap: bone {…} -->` 补上两个没有文字槽的字段：
    `category`（数据的分类）与骨根的 `note`（用户备注）。此前它们静默丢失
    （codex 复审 P2-2）——一份 `category=People`、`label=Staff` 的骨导回来变成
    `category=Staff`，正文消失且无告警。
    """
    title: str | None = None
    head: RawNode | None = None
    stack: list[tuple[int, RawNode]] = []
    containers: list[RawNode] = []
    headings: list[tuple[int, RawNode]] = []
    items = 0
    in_fence = False
    over_cap = False
    owner: RawNode | None = None
    #: 骨节点 → 那行注释里的 `{category, note}`。键是对象本身（`id()`）：注释挂在
    #: 它上面那个节点身上，而节点在 `head.children` 里活到函数末尾，不会被回收后
    #: 让 id 复用。
    bone_meta: dict[int, dict[str, str]] = {}

    for line in text.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence or not line.strip():
            continue
        if _SHAPE_MARKER.match(line):
            continue
        meta = _BONE_META.match(line)
        if meta:
            payload = _bone_payload(meta.group("payload"))
            if payload and owner is not None:
                bone_meta[id(owner)] = payload
            continue
        header = _EDGES_HEADER.match(line)
        if header:
            # 鱼骨图的顶层结构里没有 `edges`（契约 `FishboneContent`）。边清单小节
            # 照旧跳过：把那些行读成节点，会在鱼骨图里凭空长出几条"原因"。
            continue
        heading = _TITLE.match(line)
        if heading:
            body = _unescape(heading.group("text").strip())
            if title is None:
                title = body or None
                continue
            node = _heading_node(heading)
            level = len(heading.group("hashes"))
            while headings and headings[-1][0] >= level:
                headings.pop()
            if headings:
                headings[-1][1].children.append(node)
            else:
                containers.append(node)
            headings.append((level, node))
            owner = node
            continue
        detail = _DETAIL.match(line)
        if detail:
            if owner is not None:
                owner.detail = _append_text(
                    owner.detail, _unescape(detail.group("text")))
            continue
        item = _ITEM.match(line)
        if item:
            if items >= RAW_NODE_HARD_CAP:
                over_cap = True
                break
            items += 1
            label, icon, color = _label_of(item.group("text"))
            node = RawNode(label=label, icon=icon, color=color)
            indent = _indent_width(item.group("indent"))
            if head is None:
                head = node
                stack.append((indent, node))
            else:
                _attach(stack, node, indent)
            owner = node

    if head is None:
        return None

    # 手写的标题容器挂到主骨下面（正常导出里没有它们），然后每根骨 = 主骨的一个
    # 直接子项。**`problem` 就是主骨那句话**：它在契约里是一个字符串字段，所以
    # 主骨这个节点本身不会留在结果里——它的 `detail`（如果有）随之丢掉。
    head.children.extend(containers)
    bones: list[RawBone] = []
    for child in head.children:
        meta = bone_meta.get(id(child), {})
        if meta.get("note") and child.note is None:
            child.note = meta["note"]
        # `category` 缺省取骨行那行字：那正是"只有 label 时的类别"，与加注释之前
        # 的行为逐字段相同。
        bones.append(RawBone(root=child, category=meta.get("category") or child.label))

    hints: list[tuple[str, str]] = []
    if not bones:
        # 只有主骨的草稿是合法的（契约允许 `bones: []`），但"这份文件里一个字都没
        # 有六根骨"值得说一句：读的人会以为导入丢了东西。
        hints.append((
            FORMAT_NOTICE,
            "这份鱼骨图只有主骨上的那句话，没有任何骨（类别）；"
            "如果文件里本来有骨，检查它们是不是少了缩进",
        ))

    return RawMap(
        title=title,
        problem=head.label,
        bones=bones,
        shape=SHAPE_FISHBONE,
        hints=hints,
        over_hard_cap=over_cap,
    )


def _parse_flow(text: str) -> RawMap | None:
    """流程图 markdown → `RawMap(shape="flowchart")`。没有阶段标题时返回 None。

    三条格式约定，全部来自 `export.service._markdown_flow`：

      - `### 1. 阶段名` 是阶段的**标题**（不是列表项，也不是普通小标题）。标题里的
        序号由导出按数组顺序写的，导入剥掉它——留着的话下一次导出会变成 `1. 1. …`。
      - 阶段之间的边在 `## 附加关系（阶段之间）` 一节，端点写的是**阶段名**。
        所以每个阶段把"导出时它叫什么"（`1. 提交`）记进 `RawStage.old_id`：
        `normalize._normalize_flow` 拿它建阶段 id 映射，那批边才解析得到端点。
      - 阶段内部的边在 `## 附加关系（阶段内部）` 一节，行首 `[1. 提交]` 指明属于
        哪个阶段——同一份文件里两个阶段各有一条 `审不审 → 填单` 时，这是唯一的
        区分，所以这一层不能拍平。
    """
    title: str | None = None
    stages: list[RawStage] = []
    stage_edge_texts: list[str] = []
    inner_edge_texts: list[str] = []
    section: str | None = None
    scoped: list[tuple[int, RawNode]] = []
    items = 0
    in_fence = False
    over_cap = False
    owner: RawNode | None = None

    for line in text.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence or not line.strip():
            continue
        if _SHAPE_MARKER.match(line):
            continue
        header = _EDGES_HEADER.match(line)
        if header:
            section = _SECTION_KIND[header.group("text")]
            continue
        heading = _TITLE.match(line)
        if heading and section is not None:
            section = None
        if section is not None:
            item = _ITEM.match(line)
            if item:
                # 树那一路的 `## 附加关系`（没有后缀）在这里当阶段之间的清单：
                # 流程图只有两层边，一层都没标后缀时按主轴读是最不容易错的选择。
                target = inner_edge_texts if section == "inner" else stage_edge_texts
                target.append(item.group("text"))
            continue
        if heading:
            body = _unescape(heading.group("text").strip())
            if title is None:
                title = body or None
                continue
            node = _stage_root(body)
            # `old_id` 装"导出时这个阶段叫什么"：`_flow_stage_name` 写的就是
            # `f"{index + 1}. {label}"`，而阶段之间的边正是按这个名字写的端点。
            stages.append(RawStage(
                root=node, old_id=f"{len(stages) + 1}. {node.label}"))
            scoped = [(-1, node)]
            owner = node
            continue
        detail = _DETAIL.match(line)
        if detail:
            if owner is not None:
                owner.detail = _append_text(
                    owner.detail, _unescape(detail.group("text")))
            continue
        item = _ITEM.match(line)
        if item:
            if items >= RAW_NODE_HARD_CAP:
                over_cap = True
                break
            if not stages:
                # 阶段标题之前就有列表项：这份文件不像我们的流程图导出，
                # 交给调用方降级成树（那里它们还有地方落）。
                return None
            items += 1
            label, icon, color = _label_of(item.group("text"))
            node = RawNode(label=label, icon=icon, color=color)
            _attach(scoped, node, _indent_width(item.group("indent")))
            owner = node

    if not stages:
        return None

    hints: list[tuple[str, str]] = []
    by_name: dict[str, int] = {}
    for index, stage in enumerate(stages):
        for key in (stage.old_id, stage.root.label):
            if key:
                by_name.setdefault(key, index)
    orphans = 0
    for text_row in inner_edge_texts:
        matched = _INNER_SCOPE.match(text_row.strip())
        if not matched:
            orphans += 1
            continue
        index = by_name.get(matched.group("stage").strip())
        if index is None:
            orphans += 1
            continue
        edge = edge_of(matched.group("rest"))
        if edge is None:
            orphans += 1
            continue
        # 阶段根在文件里有**两个**名字：标题上的 `label`，和正文（`note`）。
        # 阶段内部的边端点用的是后者——`export._flow_node_names` 与画布一样取
        # "正文优先"——而导入之后阶段根的标题是前者。不换回来，端点就找不到：
        # 一条 `查单据与金额 → 形式审查` 会因为图上只有「审查」这个节点而被丢掉。
        stage = stages[index]
        rename = {stage.root.note: stage.root.label} if stage.root.note else {}
        edge.source = rename.get(edge.source, edge.source)
        edge.target = rename.get(edge.target, edge.target)
        stages[index].edges.append(edge)

    if orphans:
        hints.append((
            FORMAT_NOTICE,
            f"有 {orphans} 条阶段内部的边没有导入：行首的 `[阶段名]` 对不上任何"
            "阶段标题，或者括号后面不是一条 `甲 → 乙` 形式的边",
        ))

    edges: list[RawEdge] = []
    for text_row in stage_edge_texts:
        edge = edge_of(text_row)
        if edge is not None:
            edges.append(edge)

    return RawMap(
        title=title,
        stages=stages,
        edges=edges,
        shape=SHAPE_FLOWCHART,
        hints=hints,
        over_hard_cap=over_cap,
    )
