"""PlantUML mindmap（`.puml`）→ 树。第五档，但导出支持它，所以导入也得有。

`export.service.to_puml` 的输出形状：

    @startmindmap
    ' 图型：鱼骨图（fishbone）
    * 根
    ** 子
    **** 孙
    ' ---- 附加关系（mindmap 语法无关系线，以下按 A --> B : label 记录）----
    ' 甲 --> 乙 : 是
    @endmindmap

星号数量 = 层级（1 个是根），所以导入是"数星号 + 建栈"。三处特别处理：

  1. **`' 图型：…（fishbone）` 读回来**。`mindmap` 语法本身分不出流程图与鱼骨图
     ——形状可以一模一样——这行注释是唯一留下的痕迹。不读它，往返一次
     `diagram_type` 就退回 `tree`，而界面上「附加关系」区块的可见性正是按它判的。
  2. **`' ----` 开头的分隔注释要跳过**。它里面也含 `-->`（"以下按 A --> B 记录"），
     不先跳过就会被当成一条边，端点是"以下按 A"和"B : label 记录"这种鬼名字。
  3. **`' 说明：…` 是上一行节点的 `detail`**。它是导出的第二个文字通道
     （`export._puml_note`），读回来之后 `.puml` 与 markdown 一样能保住 `detail`。
     早先这里把它当普通注释跳过，`docs/IMPORT.md` 的往返矩阵因此写着"detail 在、
     但导入侧不读"——那句话本身就是自相矛盾（codex 复审 P3-5）。

做不到的：导出把正文里的 `*` 换成了 `·`（星号是层级标记），这个替换是**单向**的。
所以 `.puml` 往返之后，原文里本来有星号的标题会变成间隔号。说明里的 `-->` 也被
换成 `→`（否则那行注释会被读成一条边），同样不可逆。这两点都在告警里说明，
而不是假装能还原。
"""
from __future__ import annotations

import re

from app.importing.errors import ParseFailure
from app.importing.markdown import append_text
from app.importing.model import (
    FORMAT_NOTICE,
    RAW_NODE_HARD_CAP,
    Budget,
    RawEdge,
    RawMap,
    RawNode,
)
from app.importing.text import decode_text

_START = re.compile(r"^\s*@startmindmap\b", re.IGNORECASE)
_END = re.compile(r"^\s*@endmindmap\b", re.IGNORECASE)
# 图型注释。中文全角括号与英文括号都收——用户可能手改过。
_DIAGRAM = re.compile(r"^\s*'\s*图型：\s*(?P<label>.*?)\s*[（(](?P<key>[A-Za-z_]+)[）)]")
# 说明注释：`export._puml_note` 写的第二通道（`' 说明：…`）。
_NOTE = re.compile(r"^\s*'\s*说明：\s*(?P<text>.*)$")
_SEPARATOR = re.compile(r"^\s*'\s*-{2,}")
_EDGE = re.compile(
    r"^\s*'\s*(?P<source>.+?)\s*-->\s*(?P<target>.+?)(?:\s*:\s*(?P<label>.*))?$")
_STARS = re.compile(r"^(?P<stars>\*+)(?P<rest>.*)$")

# `A --> B : 顺序` 里的中文说法 → 枚举（与 `markdown.KIND_FROM_LABEL` 同一张表）。
_KIND_FROM_LABEL = {"顺序": "flow", "条件分支": "branch", "回退": "loop"}


def _clean(text: str) -> str:
    """去掉 PlantUML 的行内标记：`*_ 文字` 里的 `_`（无边框）。

    只剥**紧跟星号的那一个** `_`：标题自己以 `_` 开头（`* _内部`）时那个下划线
    是正文，不能一起吃掉。
    """
    body = text
    if body.startswith("_"):
        body = body[1:]
    return body.strip()


def _edge_of(line: str) -> RawEdge | None:
    matched = _EDGE.match(line)
    if not matched:
        return None
    label = (matched.group("label") or "").strip() or None
    source = matched.group("source").strip()
    target = matched.group("target").strip()
    if not source or not target:
        return None
    return RawEdge(
        source=source, target=target, label=label,
        kind=_KIND_FROM_LABEL.get(label or ""),
    )


def parse(raw: bytes) -> RawMap:
    text = decode_text(raw)
    lines = text.splitlines()
    if not any(_START.match(line) for line in lines):
        raise ParseFailure("不是 PlantUML mindmap：找不到 @startmindmap")

    started = False
    nodes: list[tuple[int, RawNode]] = []
    edges: list[RawEdge] = []
    diagram_type: str | None = None
    # 两个上限共用这一个标志：行数超了（下面）、预算超了（组树时）都是"没读全"。
    over_cap = False

    for line in lines:
        if not started:
            started = bool(_START.match(line))
            continue
        if _END.match(line):
            break
        if not line.strip():
            continue
        if _SEPARATOR.match(line):
            continue
        hint = _DIAGRAM.match(line)
        if hint:
            diagram_type = hint.group("key")
            continue
        if line.lstrip().startswith("'"):
            note = _NOTE.match(line)
            if note and nodes:
                # `' 说明：…` 是 `export._puml_note` 写的第二通道，挂在**它上面
                # 那一行节点**上（导出就是紧跟着节点行写的）。早先这里只把它当
                # 普通注释跳过，于是 `detail` 落在文件里却读不回来——文档的往返
                # 矩阵因此自相矛盾（codex 复审 P3-5）。
                nodes[-1][1].detail = append_text(
                    nodes[-1][1].detail, note.group("text"))
                continue
            edge = _edge_of(line)
            if edge is not None:
                edges.append(edge)
            continue
        matched = _STARS.match(line.lstrip())
        if not matched:
            # `+` / `-` 前缀（PlantUML 用它表示"画在左/右侧"）与 `!: ` 之类的
            # 装饰行都不认。跳过而不是报错：一张从别处来的 puml 里总有几行我们
            # 不懂的标记，为它整份失败不划算。
            continue
        text_body = _clean(matched.group("rest"))
        if not text_body:
            continue
        # 行级上限，与 `RAW_NODE_HARD_CAP` 同一个数字：一份 10MB 的 puml 能有几十万
        # 行节点，`nodes` 列表本身就会先把内存吃掉（每项一个 tuple + RawNode 的
        # 全部字段）。截在这里，`normalize` 才有机会按 1000 条裁。
        if len(nodes) >= RAW_NODE_HARD_CAP:
            over_cap = True
            break
        nodes.append((len(matched.group("stars")), RawNode(label=text_body)))

    if not nodes:
        raise ParseFailure(
            "PlantUML mindmap 里没有找到任何节点行（形如 `* 标题`）；"
            "第 1 个节点行应该在 @startmindmap 之后")

    budget = Budget()
    budget.take()
    root = nodes[0][1]
    stack: list[tuple[int, RawNode]] = [(nodes[0][0], root)]
    extra_roots = 0
    for depth, node in nodes[1:]:
        if not budget.take():
            over_cap = True
            break
        while len(stack) > 1 and stack[-1][0] >= depth:
            stack.pop()
        if len(stack) == 1 and depth <= stack[0][0]:
            extra_roots += 1
        stack[-1][1].children.append(node)
        stack.append((depth, node))

    hints: list[tuple[str, str]] = []
    if extra_roots:
        hints.append((
            FORMAT_NOTICE,
            f"有 {extra_roots} 个节点与根同级，已归到根节点下（我们的导图只有一个根）",
        ))
    if "·" in text:
        hints.append((
            FORMAT_NOTICE,
            "PlantUML 没有转义星号的语法，导出时正文里的 `*` 被写成了 `·`；"
            "这个替换不可逆，所以标题里的 `·` 可能本来是星号",
        ))
    if diagram_type in ("tree", "flowchart", "fishbone"):
        return RawMap(
            root=root, diagram_type=diagram_type, edges=edges, hints=hints,
            over_hard_cap=over_cap)
    return RawMap(root=root, edges=edges, hints=hints, over_hard_cap=over_cap)
