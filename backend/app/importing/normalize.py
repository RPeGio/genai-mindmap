"""把"文件里的树"变成"能落进我们画布的树"：五件边界处理只在这里做。

五个坑，以及每个坑的具体做法：

  ① **id 重新生成**。外部文件里的 id 可能是 `1`、`node-1`、UUID，或者我们自己的
     `n_xxx`。全部换新（`agent.ops.new_node_id`，与生成侧同一个生成器），同时建
     `旧 id → 新 id` 的映射，按它重建 `edges` / `relations` 的端点。**映射只登记
     被保留的节点**：端点落在被裁掉的分支上时那条边就是 dangling，契约层会硬拒
     （`Edge.source` 找不到 → 前端 `source: undefined` → Vue Flow 抛错白屏），
     所以它必须在进契约之前消失。

  ② **限额**。1000 节点 / 8 层 / 50 子节点。三个都是**截断 + warn**，不是整份
     拒绝：用户导一张 XMind 进来，因为多了几个节点就什么都不给，比少几个节点糟
     得多。

     节点数用 **BFS（层序）** 保留，理由不是"实现简单"：
       - DFS 保留的是"第一个分支的最深处"——一张 1500 节点的图会剩下一根细长的
         辫子，另外五个一级分支整条消失，用户看到的图缺了五个主题，而它们才是
         他最先要看的东西；
       - BFS 保证每一层、每一个一级/二级分支都留下，被裁掉的是各支最深处的细节
         ——那是"没读到这里"，不是"这里没有了"；
       - 与深度上限同向：先撞节点上限时 BFS 的层数最小，图仍然读得清；DFS 则可能
         1000 个节点全花在 8 层里的一条路径上。
     代价是 BFS 会保留大量叶子而丢掉"某个分支的完整链路"，这个取舍写在 warning
     里（"按层序保留，深处与靠后的分支被裁掉"）。

     50 子上限是契约 `Node.children` 的硬边界（`max_length=50`），超过会 422，
     所以必须在构造时裁。

  ③ **`label` 长度**。生成侧限 16，**导入不套用**：用户手写的长标题被截到 16
     字是不可接受的。改用契约上限 200，超了才截并报一条聚合告警。空白标题兜成
     「（无标题）」——契约 `label` 非空，一条空标题会让整份文件 422。

  ④ **视觉字段的逆向映射**。`color` 只认调色板键或 `colors` 里那张精确色表；
     映射不到就丢掉（`None` 的语义是"没有颜色"，会跟随上级——那比猜一个灰色
     诚实）。`icon` 走 `normalize_icon`，它会把"写成文字的图标"丢掉。

  ⑤ **`source_refs` 一律清空**，并在结果里明说。外部文件的引用不在我们的坐标
     体系里：`SourceRef.start/end` 指向的是**别人那份原文**的字符偏移，而
     `source_usage` 里的 `source_id` 在这边根本不存在。带进来的直接后果是
     `MindMapContent.validate_tree` 抛"节点 X 引用的来源不在 source_usage 中"
     ——整份导入 422，而错误信息指向一个用户改不掉的地方。`source_usage` 与它
     同源同理，一起清空。
"""
from __future__ import annotations

import unicodedata
from collections import deque
from dataclasses import dataclass, field
from uuid import uuid4

from app.agent.ops import new_edge_id, new_node_id
from app.contracts.mindmap import (
    DIAGRAM_TYPES,
    EDGE_KINDS,
    MAX_EDGE_LABEL_CHARS,
    MAX_EDGES,
    MAX_NODE_CHILDREN,
    MAX_NODE_DETAIL_CHARS,
    MAX_NODE_LABEL_CHARS,
    MAX_TREE_DEPTH,
    MAX_TREE_NODES,
    NODE_EXPANDABLE,
    Edge,
    LayoutKind,
    MindMapContent,
    Node,
    NodeStyle,
    Position,
    Relation,
    RelationType,
    Theme,
    clip_label,
    normalize_color,
    normalize_icon,
)
from app.importing.colors import normalize_hex
from app.importing.model import (
    RAW_NODE_HARD_CAP,
    ImportWarning,
    RawEdge,
    RawMap,
    RawNode,
)

# ---- 告警码 ----
#
# 与 `mindmaps/edges.py` 的三个码同一个风格：码是稳定的（前端可以按码做分组或
# 折叠），文案是给人读的。**不加 `level` 字段**：导入没有 info 档——用户上传
# 一个文件，凡是我们动过的地方都值得他知道。
TRUNCATED_NODES = "truncated_nodes"
TRUNCATED_DEPTH = "truncated_depth"
TRUNCATED_CHILDREN = "truncated_children"
LABEL_TRUNCATED = "label_truncated"
LABEL_MISSING = "label_missing"
TITLE_TRUNCATED = "title_truncated"
TEXT_TRUNCATED = "text_truncated"
SOURCE_REFS_CLEARED = "source_refs_cleared"
EDGES_DROPPED = "edges_dropped"
RELATIONS_DROPPED = "relations_dropped"
FIELDS_DROPPED = "fields_dropped"
DUPLICATE_IDS = "duplicate_ids"
HARD_CAP = "hard_cap"

# 契约字段的取值集合，用来判断"文件给的值能不能原样收下"。
_LAYOUTS = {item.value for item in LayoutKind}
_THEMES_PRESETS = {"default", "slate", "forest", "sunset", "mono"}
_FONTS = {"system", "serif", "mono"}
_DIRECTIONS = {"right", "left", "both"}
_SHAPES = {"rounded", "rect", "pill"}
_RELATION_TYPES = {item.value for item in RelationType}

# 位置字段的上下界，与 `contracts.mindmap.Position` 的 Field 一致。
_POS_LIMIT = 1e6


def one_line(text: object) -> str:
    """把任意值折成单行字符串，并**去掉控制字符**。

    非字符串按空处理（外部文件里 label 可能是数字）。

    去控制字符不是洁癖：markdown / JSON 里可以塞进 NUL、BEL、ESC。它们进了
    `label` 之后有三个真实后果——落库的 JSON 里是多字节转义（排查时看不见）、
    前端把它渲染成一个看不见的方块、终端里 `\\x1b[31m` 会让导出文件带颜色。
    折成空格而不是删掉：`abc\\x00def` 是"两个词"，不是"一个词 abcdef"。
    """
    if isinstance(text, str):
        cleaned = "".join(
            " " if unicodedata.category(ch) == "Cc" else ch for ch in text)
        return " ".join(cleaned.split())
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return str(text)
    return ""


def _clip_text(text: object, limit: int) -> tuple[str | None, bool]:
    """折行 + 按码点截断。返回 `(值, 是否截过)`；空文本折成 None。"""
    folded = one_line(text)
    if not folded:
        return None, False
    if len(folded) <= limit:
        return folded, False
    return clip_label(folded, limit), True


@dataclass
class _Survey:
    """整棵 raw 树的体检数字。先数一遍，才能报出准确的"文件里有多少"。"""

    total: int = 0
    max_depth: int = 0
    # `(label, 子节点总数)`，只登记超过 50 的节点。
    over_children: list[tuple[str, int]] = field(default_factory=list)
    has_pos: bool = False


def survey(root: RawNode) -> _Survey:
    """数节点、量深度、找出子节点超限的节点。

    用显式栈而不是递归：外部文件的深度不受我们控制（一份缩进 500 层的 markdown
    完全可能），递归会在 `RecursionError` 上崩掉——那是一个 500 而不是 400，
    用户拿不到任何可操作的信息。
    """
    stats = _Survey()
    stack: list[tuple[RawNode, int]] = [(root, 1)]
    while stack:
        node, depth = stack.pop()
        stats.total += 1
        stats.max_depth = max(stats.max_depth, depth)
        # 必须是**有效**坐标：`{"x": "左边"}` 这种值会让"有坐标"成立，于是一张
        # 声明 manual 的图被原样收下，然后 `validate_tree` 抛"手动布局至少需要
        # 一个节点带坐标"——错误指向一个用户改不掉的地方。
        if _pos(node.pos) is not None:
            stats.has_pos = True
        if len(node.children) > MAX_NODE_CHILDREN:
            stats.over_children.append((one_line(node.label), len(node.children)))
        for child in node.children:
            stack.append((child, depth + 1))
    return stats


@dataclass
class _Tally:
    """构建过程中的计数。全部汇成告警，不静默丢东西。"""

    clipped_labels: int = 0
    missing_labels: int = 0
    clipped_texts: int = 0
    duplicate_ids: int = 0
    kept: int = 0
    positions: int = 0


def _style(value: object) -> NodeStyle | None:
    """只保留 `NodeStyle` 认识的四个字段，其余丢掉。

    `NodeStyle` 是 `extra="forbid"`：外部文件里的 `{"fontSize": 14}` 会让整个
    构造 422，而它只是个装饰字段。所以这里逐字段挑，挑不出东西就返回 None。
    """
    if not isinstance(value, dict):
        return None
    data: dict[str, object] = {}
    color = normalize_hex(value.get("color"))
    if color:
        data["color"] = color
    icon = value.get("icon")
    if isinstance(icon, str) and 0 < len(icon) <= 40:
        data["icon"] = icon
    for flag in ("bold", "italic"):
        if isinstance(value.get(flag), bool):
            data[flag] = value[flag]
    shape = value.get("shape")
    if isinstance(shape, str) and shape in _SHAPES:
        data["shape"] = shape
    return NodeStyle(**data) if data else None


def _pos(value: object) -> Position | None:
    """坐标：两个数字、在 ±1e6 内才算数。"""
    if not isinstance(value, dict):
        return None
    try:
        x = float(value["x"])
        y = float(value["y"])
    except (KeyError, TypeError, ValueError):
        return None
    if abs(x) > _POS_LIMIT or abs(y) > _POS_LIMIT:
        return None
    return Position(x=x, y=y)


def _node(raw: RawNode, taken: set[str], tally: _Tally, keep_pos: bool) -> Node:
    """一个 raw 节点 → 契约 `Node`（子节点留空，由 BFS 填）。"""
    label, clipped = _clip_text(raw.label, MAX_NODE_LABEL_CHARS)
    if not label:
        # 契约 `label` 非空：一条空标题会让整份文件 422，而用户没法定位到它。
        label = "（无标题）"
        tally.missing_labels += 1
    elif clipped:
        tally.clipped_labels += 1

    note, note_clipped = _clip_text(raw.note, MAX_NODE_DETAIL_CHARS)
    detail, detail_clipped = _clip_text(raw.detail, MAX_NODE_DETAIL_CHARS)
    if note_clipped or detail_clipped:
        tally.clipped_texts += 1

    pos = _pos(raw.pos) if keep_pos else None
    if pos is not None:
        tally.positions += 1

    kwargs: dict[str, object] = {
        "id": new_node_id(taken),
        "label": label,
        "note": note,
        "detail": detail,
        "collapsed": bool(raw.collapsed),
        "color": normalize_color(raw.color),
        "icon": normalize_icon(raw.icon),
        "style": _style(raw.style),
        "pos": pos,
    }
    if raw.expandable in NODE_EXPANDABLE:
        kwargs["expandable"] = raw.expandable
    return Node(**kwargs)


def _title(raw: RawMap) -> tuple[str, bool]:
    """标题：文件给的 → 根节点文字 → 「导入的导图」；超 200 截断。"""
    text = one_line(raw.title) or one_line(raw.root.label) or "导入的导图"
    if len(text) <= 200:
        return text, False
    return clip_label(text, 200), True


def _theme(raw_theme: object) -> Theme:
    """只保留 `Theme` 认识的字段。认不出的值丢掉（不猜默认值之外的语义）。"""
    if not isinstance(raw_theme, dict):
        return Theme()
    data: dict[str, object] = {}
    preset = raw_theme.get("preset")
    if isinstance(preset, str) and preset in _THEMES_PRESETS:
        data["preset"] = preset
    accent = normalize_hex(raw_theme.get("accent"))
    if accent:
        data["accent"] = accent
    font = raw_theme.get("font")
    if isinstance(font, str) and font in _FONTS:
        data["font"] = font
    direction = raw_theme.get("direction")
    if isinstance(direction, str) and direction in _DIRECTIONS:
        data["direction"] = direction
    return Theme(**data)


def _resolve(endpoint: str, id_map: dict[str, str], label_map: dict[str, str]) -> str | None:
    """边的端点：先按旧 id 查，再按文字查。

    两条路都要：JSON 的边是 id，markdown / opml / puml 的边是**节点文字**（导出
    时把 id 换成了人看得懂的名字）。`label_map` 的键是折叠过的原始文字，所以这里
    也折叠一次——用户把 markdown 里的多行标题合并回一行之后，边仍然连得上。
    """
    found = id_map.get(endpoint)
    if found is not None:
        return found
    return label_map.get(one_line(endpoint))


@dataclass
class _EdgeResult:
    edges: list[Edge] = field(default_factory=list)
    dropped: int = 0
    reasons: list[str] = field(default_factory=list)


def _rebuild_edges(
    raw_edges: list[RawEdge], id_map: dict[str, str], label_map: dict[str, str],
) -> _EdgeResult:
    """按新 id 重建附加边，丢 dangling / 自环 / 重复 / 超限。"""
    result = _EdgeResult()
    taken: set[str] = set()
    seen: set[tuple[str, str, str | None]] = set()
    for raw_edge in raw_edges:
        source = _resolve(raw_edge.source, id_map, label_map)
        target = _resolve(raw_edge.target, id_map, label_map)
        if source is None or target is None:
            result.dropped += 1
            result.reasons.append("端点找不到")
            continue
        if source == target:
            result.dropped += 1
            result.reasons.append("自环")
            continue
        label, _ = _clip_text(raw_edge.label, MAX_EDGE_LABEL_CHARS)
        key = (source, target, label)
        if key in seen:
            result.dropped += 1
            result.reasons.append("重复")
            continue
        if len(result.edges) >= MAX_EDGES:
            result.dropped += 1
            result.reasons.append("超过 200 条")
            continue
        seen.add(key)
        kind = raw_edge.kind if raw_edge.kind in EDGE_KINDS else None
        result.edges.append(Edge(
            id=new_edge_id(taken), source=source, target=target, label=label, kind=kind))
    return result


def _new_relation_id(taken: set[str]) -> str:
    """关系 id。`r_` 前缀与节点（`n_`）、边（`e_`）分开，排查时一眼分得清。"""
    while True:
        candidate = f"r_{uuid4().hex}"
        if candidate not in taken:
            return candidate


def _rebuild_relations(
    raw_relations: list[dict], id_map: dict[str, str],
) -> tuple[list[Relation], int]:
    """重建跨节点关系。端点要重映射，`type` 不在枚举里的丢掉。"""
    kept: list[Relation] = []
    taken: set[str] = set()
    dropped = 0
    for item in raw_relations:
        if not isinstance(item, dict):
            dropped += 1
            continue
        source = _resolve(str(item.get("source") or ""), id_map, {})
        target = _resolve(str(item.get("target") or ""), id_map, {})
        kind = item.get("type")
        if source is None or target is None or source == target or kind not in _RELATION_TYPES:
            dropped += 1
            continue
        label, _ = _clip_text(item.get("label"), 100)
        evidence, _ = _clip_text(item.get("evidence"), 300)
        kept.append(Relation(
            id=_new_relation_id(taken), source=source, target=target, type=kind,
            label=label, evidence=evidence,
            # 外部文件带来的关系不是用户在这张图上画的，也不是模型给的：
            # `provenance` 的第三个取值就是为导入准备的。
            provenance="import",
        ))
    return kept, dropped


def _layout_and_pos(raw: RawMap, stats: _Survey) -> tuple[LayoutKind, bool]:
    """布局与"要不要保留坐标"。

    契约对两者的约束是双向的：`manual` 必须至少有一个坐标，非 `manual` 不许有
    坐标（`validate_tree` 两条都会硬拒）。所以：

      - 文件说 manual 但一个有效坐标都没有 → 降级 `tree`；
      - 文件没说 manual 却带着坐标 → 丢掉坐标（保留布局）。
    """
    layout = LayoutKind(raw.layout) if raw.layout in _LAYOUTS else LayoutKind.tree
    if layout is LayoutKind.manual:
        return (LayoutKind.manual, True) if stats.has_pos else (LayoutKind.tree, False)
    return layout, False


def normalize(raw: RawMap, *, source_format: str) -> tuple[MindMapContent, list[ImportWarning]]:
    """`RawMap` → `MindMapContent` + 告警列表。

    这是唯一的入口：`service.parse_import` 只调它，六个解析器都碰不到契约。
    """
    warnings: list[ImportWarning] = [
        ImportWarning(code=code, message=message) for code, message in raw.hints
    ]
    if raw.over_hard_cap:
        warnings.append(ImportWarning(
            code=HARD_CAP,
            message=(f"文件超过 {RAW_NODE_HARD_CAP} 个节点，只读取了前 {RAW_NODE_HARD_CAP} 个；"
                     "剩下的部分没有被解析，导入结果可能缺少靠后的分支"),
        ))

    stats = survey(raw.root)
    layout, keep_pos = _layout_and_pos(raw, stats)

    title, title_clipped = _title(raw)
    if title_clipped:
        warnings.append(ImportWarning(
            code=TITLE_TRUNCATED, message="导图标题超过 200 字，已截断"))

    # ---- ① ② ③：BFS 构造 + 重新生成 id ----
    tally = _Tally()
    taken: set[str] = set()
    id_map: dict[str, str] = {}
    label_map: dict[str, str] = {}

    root = _node(raw.root, taken, tally, keep_pos)
    tally.kept = 1
    if raw.root.old_id:
        id_map.setdefault(raw.root.old_id, root.id)
    for key in {one_line(raw.root.label), root.label}:
        if key:
            label_map.setdefault(key, root.id)

    queue: deque[tuple[RawNode, Node, int]] = deque([(raw.root, root, 1)])
    while queue and tally.kept < MAX_TREE_NODES:
        raw_node, node, depth = queue.popleft()
        for raw_child in raw_node.children[:MAX_NODE_CHILDREN]:
            if tally.kept >= MAX_TREE_NODES:
                break
            if depth + 1 > MAX_TREE_DEPTH:
                continue
            child = _node(raw_child, taken, tally, keep_pos)
            node.children.append(child)
            tally.kept += 1
            if raw_child.old_id:
                if raw_child.old_id in id_map:
                    tally.duplicate_ids += 1
                else:
                    id_map[raw_child.old_id] = child.id
            for key in {one_line(raw_child.label), child.label}:
                if key:
                    label_map.setdefault(key, child.id)
            queue.append((raw_child, child, depth + 1))

    # ---- 限额告警。判据是**文件自身的属性**，不是"我们丢了多少" ----
    if stats.total > MAX_TREE_NODES:
        warnings.append(ImportWarning(
            code=TRUNCATED_NODES,
            message=(f"文件共有 {stats.total} 个节点，超过上限 {MAX_TREE_NODES}；"
                     f"已按层序保留前 {tally.kept} 个，深处与靠后的分支被裁掉"),
        ))
    if stats.max_depth > MAX_TREE_DEPTH:
        warnings.append(ImportWarning(
            code=TRUNCATED_DEPTH,
            message=(f"文件最深 {stats.max_depth} 层，超过上限 {MAX_TREE_DEPTH} 层；"
                     f"第 {MAX_TREE_DEPTH + 1} 层及以下的节点已丢弃（共 {stats.total} 个节点里"
                     "有一部分在这个范围内）"),
        ))
    if stats.over_children:
        names = "、".join(f"「{label or '（无标题）'}」（{count} 个）"
                         for label, count in stats.over_children[:3])
        more = (f" 等 {len(stats.over_children)} 个节点"
                if len(stats.over_children) > 3 else "")
        warnings.append(ImportWarning(
            code=TRUNCATED_CHILDREN,
            message=(f"有子节点超过上限 {MAX_NODE_CHILDREN} 个的节点：{names}{more}；"
                     f"每个节点只保留前 {MAX_NODE_CHILDREN} 个子节点"),
        ))
    if tally.clipped_labels:
        warnings.append(ImportWarning(
            code=LABEL_TRUNCATED,
            message=(f"{tally.clipped_labels} 个节点的标题超过 {MAX_NODE_LABEL_CHARS} 字，"
                     "已截到上限"),
        ))
    if tally.missing_labels:
        warnings.append(ImportWarning(
            code=LABEL_MISSING,
            message=(f"{tally.missing_labels} 个节点在文件里没有文字，已填为「（无标题）」。"
                     "节点标题不能为空，空标题会让整份导图存不回去"),
        ))
    if tally.clipped_texts:
        warnings.append(ImportWarning(
            code=TEXT_TRUNCATED,
            message=(f"{tally.clipped_texts} 个节点的正文超过 {MAX_NODE_DETAIL_CHARS} 字，"
                     "已截到上限"),
        ))
    if tally.duplicate_ids:
        warnings.append(ImportWarning(
            code=DUPLICATE_IDS,
            message=(f"文件里有 {tally.duplicate_ids} 个重复的节点 id；"
                     "指向同一个 id 的附加关系只连到第一个节点上"),
        ))

    # ---- ④：顺手记下"布局被动过" ----
    if raw.layout in _LAYOUTS and LayoutKind(raw.layout) is LayoutKind.manual \
            and not keep_pos:
        warnings.append(ImportWarning(
            code=FIELDS_DROPPED,
            message="文件声明的是手动布局，但没有任何有效坐标；已按自动布局导入",
        ))
    elif raw.layout not in (None, *_LAYOUTS):
        warnings.append(ImportWarning(
            code=FIELDS_DROPPED,
            message=f"文件里的布局值「{raw.layout}」不认识，已按思维导图布局导入",
        ))
    elif raw.layout in _LAYOUTS and LayoutKind(raw.layout) is not LayoutKind.manual \
            and stats.has_pos:
        warnings.append(ImportWarning(
            code=FIELDS_DROPPED,
            message="文件里带着节点坐标，但布局不是手动布局；坐标已丢弃"
                    "（自动布局下它们会被当成用户意图，画出来是一团乱麻）",
        ))
    elif stats.has_pos:
        # 文件没写 layout，但每个节点都带坐标。契约不允许"自动布局 + 坐标"，
        # 所以坐标丢掉；这里说出来，免得用户以为我们读漏了。
        warnings.append(ImportWarning(
            code=FIELDS_DROPPED,
            message="文件里带着节点坐标，但没有声明手动布局；坐标已丢弃",
        ))

    # ---- ⑤：引用与来源摘要一律清空 ----
    refs_cleared = raw.counters.get("source_refs", 0)
    usage_cleared = raw.counters.get("source_usage", 0)
    if refs_cleared or usage_cleared:
        parts = []
        if refs_cleared:
            parts.append(f"{refs_cleared} 处节点引用")
        if usage_cleared:
            parts.append(f"{usage_cleared} 条来源摘要")
        warnings.append(ImportWarning(
            code=SOURCE_REFS_CLEARED,
            message=(f"导入的引用已清空（{'、'.join(parts)}）：外部文件的字符偏移与"
                     "来源 ID 指向的是别处的原文，带进来会让引用核验整份失败"),
        ))

    # ---- 边与关系 ----
    edge_result = _rebuild_edges(raw.edges, id_map, label_map)
    if edge_result.dropped:
        reasons = "、".join(sorted(set(edge_result.reasons)))
        warnings.append(ImportWarning(
            code=EDGES_DROPPED,
            message=(f"{edge_result.dropped} 条附加关系没有导入（{reasons}）；"
                     f"其余 {len(edge_result.edges)} 条已保留"),
        ))
    relations, relations_dropped = _rebuild_relations(raw.relations, id_map)
    if relations_dropped:
        warnings.append(ImportWarning(
            code=RELATIONS_DROPPED,
            message=(f"{relations_dropped} 条跨节点关系没有导入（端点找不到或类型不认识）；"
                     f"其余 {len(relations)} 条已保留"),
        ))

    diagram_type = raw.diagram_type if raw.diagram_type in DIAGRAM_TYPES else "tree"
    # 最后一道保险：文件里**有**有效坐标，但带坐标的那个节点正好被限额裁掉了
    # （1000 节点截断时完全可能）。契约要求 manual 至少一个坐标，否则整份 422，
    # 所以这里退回自动布局，而不是把一张"差一个坐标"的图判死。
    if layout is LayoutKind.manual and tally.positions == 0:
        layout = LayoutKind.tree
        warnings.append(ImportWarning(
            code=FIELDS_DROPPED,
            message="原来的手动布局坐标都在被裁掉的节点上，已按自动布局导入",
        ))
    content = MindMapContent(
        title=title,
        root=root,
        layout=layout,
        theme=_theme(raw.theme),
        relations=relations,
        edges=edge_result.edges,
        diagram_type=diagram_type,
    )
    return content, warnings
