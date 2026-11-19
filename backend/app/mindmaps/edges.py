"""附加边（`MindMapContent.edges`）的诊断与裁剪。

位置：mindmaps 层。契约（`Edge` / `MindMapContent.edges`）在
`app/contracts/mindmap.py`，这里只放"已有的一批边有什么毛病"与"模型给的这批边
要怎么变成能落树的那一批"。

**为什么这里和契约层分两套判据**（读这个文件最容易困惑的一点）：

  契约层（`MindMapContent.validate_tree`）是**渲染安全**的底线，只能是硬拒：
  dangling（端点不存在）与自环会让前端 `source: undefined`、Vue Flow 直接抛错——
  整张画布白屏，不是"少画一条线"。所以那两个无论如何都进不了落库的图。

  这一层是**降级**：模型一次响应里的边有一两条写错端点、多几条、绕了个太长的圈，
  不该把已经花掉的骨架与展开调用整批判废。所以这里把它们**丢掉并记诊断**，
  交出去的树是完整的、边是干净的。三个码的级别也都是 `warn`。

  两条路不冲突：生成路径先过这一层（脏边在此消失），契约层再兜一次（万一有人
  绕过了这一层）。用户看到的永远是"图是好的 + 有一条告警说哪几条边没画出来"。

关于环：`A→B→A` 是流程图的核心需求（驳回/返工/重试），所以**允许环**，只限制
"最长环 ≤ `MAX_EDGE_CYCLE_NODES`"。判定用的是"存在性搜索"而不是"求最长环"：

  求最长简单环是 NP-hard，而我们要回答的只是"有没有一个超过 12 个节点的环"。
  所以对每个起点做一遍「路径长度 ≥ 12 的简单回路存在性搜索」，找到就立刻停。
  代价上界由 `_SEARCH_BUDGET` 兜住（步数，不是时间）：真实流程图的出度 2–3，
  一条 20 节点的环十几步就撞上了；只有稠密到不可能是流程图的图才会撞预算，
  那时按"没发现"处理——**宁可漏报，不误报**：报一条假的"环太长"会让用户去删
  一条本来正确的回退线。
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence

from app.contracts.diagnostic import Diagnostic, DiagnosticLevel
from app.contracts.mindmap import MAX_EDGE_CYCLE_NODES, MAX_EDGES, Edge

# 三个诊断码。定义在这里而不是 `contracts/diagnostic.py`：那个文件是**模型**
# （Diagnostic / Suggestion），码是各处自己声明的字面量（`EVIDENCE_UNUSED` 等
# 也散在 tools 里）。集中放这一个文件，是为了让"边的三个码"只有一处拼写，
# 用例、工具、日志都读同一份常量。
EDGE_DANGLING = "EDGE_DANGLING"
EDGE_LIMIT = "EDGE_LIMIT"
EDGE_CYCLE_TOO_LONG = "EDGE_CYCLE_TOO_LONG"

# 「这个端点解析不出来」的哨兵。生成侧（`tools._skeleton_edges`）把解析失败的按钮
# 端点塞成它，`clean_edges` 就会按 dangling 处理并记一条 `EDGE_DANGLING`。
#
# 为什么需要这个哨兵：最初的写法是把解析失败的边塞成"指向自己"，指望它被自环那
# 一条规则丢掉——**但自环是静默丢弃的**，于是"模型写了 `target: 9` 而只有 3 条分支"
# 这件事在诊断里一个字都没有，图默默少了一条流程分支。用一个不可能出现在树里的
# 哨兵，才能让它落进"端点不存在"那一支（`$` 前缀是临时 id 的域，真实节点 id 由
# `new_node_id` 生成，不带 `$`）。
UNRESOLVED_ENDPOINT = "$unresolved"

# 环搜索的步数预算。取值依据：200 条边、出度 ≤ 3 的图上，一次完整的"长度 ≥ 12
# 回路存在性"搜索实测在几百步内结束；20 万步是"稠密到不像流程图"的图才会撞到的量级。
# 撞到就返回"没发现"，并且不影响正确性——它只影响一条 warn 报不报。
_SEARCH_BUDGET = 200_000


def _adjacency(edges: Iterable[Edge]) -> dict[str, list[str]]:
    """`source → [target, ...]`。重复目标保留：多重边在环判定里没有额外意义，
    但去重会掩盖"平行边"这件事，而那正是我们允许的形态。"""
    graph: dict[str, list[str]] = {}
    for edge in edges:
        graph.setdefault(edge.source, []).append(edge.target)
    return graph


def _has_path_at_least(
    graph: dict[str, list[str]], start: str, goal: str, min_len: int,
) -> bool:
    """从 `start` 到 `goal` 是否存在一条**边数 ≥ `min_len`** 的简单路径。

    显式栈 + 路径集合：树深的上界是节点数，递归也能跑，但这里处理的是"模型给的、
    尚未校验的边"，不该依赖任何上界成立。`min_len` 是下界，所以不剪枝"太短"的
    分支——那正是要找的东西；剪的是"已经重复"的节点（简单路径的定义）。
    """
    if start == goal:
        return min_len <= 0
    stack: list[tuple[str, frozenset[str]]] = [(start, frozenset((start,)))]
    steps = 0
    while stack:
        steps += 1
        if steps > _SEARCH_BUDGET:
            return False
        node, visited = stack.pop()
        hops = len(visited)  # 已走过的边数 = 已访问节点数 - 1
        for nxt in graph.get(node, ()):
            if nxt == goal:
                if hops >= min_len:
                    return True
                # 长度不够就**不**在这里回去：继续往下绕一圈可能就够长了。
                continue
            if nxt in visited:
                continue
            stack.append((nxt, visited | {nxt}))
    return False


def has_long_cycle(edges: Sequence[Edge], limit: int = MAX_EDGE_CYCLE_NODES) -> bool:
    """图里有没有一个**边数 > `limit`** 的简单环。

    做法：对每条边 `s → v`，问"从 `v` 回到 `s` 有没有一条边数 ≥ `limit` 的简单
    路径"——有的话，加上这条边就是一个 `limit + 1` 以上的环。
    """
    graph = _adjacency(edges)
    for source, targets in graph.items():
        for target in targets:
            if target == source:
                continue
            if _has_path_at_least(graph, target, source, limit):
                return True
    return False


def edge_diagnostics(
    edges: Sequence[Edge], node_ids: set[str] | None = None,
) -> list[Diagnostic]:
    """一批边的三条告警（都是 `warn`，都不拒收）。

    `node_ids` 给了才查 dangling：契约层拿不到"树里有哪些 id"以外的信息，而
    导出/前端可能只想问"这批边超没超限"，不该被迫先构造一棵树。
    """
    diagnostics: list[Diagnostic] = []
    if node_ids is not None:
        dangling = [edge for edge in edges
                    if edge.source not in node_ids or edge.target not in node_ids]
        if dangling:
            names = "、".join(edge.id for edge in dangling[:5])
            more = f" 等 {len(dangling)} 条" if len(dangling) > 5 else ""
            diagnostics.append(Diagnostic(
                level=DiagnosticLevel.warn, code=EDGE_DANGLING,
                message=(f"{len(dangling)} 条附加边指向不存在的节点（{names}{more}），"
                         "这些边不会被画出来"),
            ))
    if len(edges) > MAX_EDGES:
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.warn, code=EDGE_LIMIT,
            message=(f"附加边有 {len(edges)} 条，超过建议上限 {MAX_EDGES} 条；"
                     "画布上已经很难读清，多出来的连线建议合并成节点"),
        ))
    if has_long_cycle(edges):
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.warn, code=EDGE_CYCLE_TOO_LONG,
            message=(f"存在一个超过 {MAX_EDGE_CYCLE_NODES} 个节点的环：回退路径绕得太远，"
                     "画布上会绕成一团，看不出从哪里回到哪里"),
        ))
    return diagnostics


def clean_edges(
    edges: Iterable[Edge], node_ids: set[str],
) -> tuple[list[Edge], list[Diagnostic]]:
    """把模型给的这批边清成**能落树**的那一批，并回报被丢掉的。

    四件事，顺序不能换：

      1. **dangling 丢掉 + `EDGE_DANGLING`**（一条聚合告警，不是每条一条：20 条
         脏边刷 20 行会把真正有用的诊断挤掉）。必须排在自环**之前**：解析失败的
         端点是一条哨兵边（两端都是 `UNRESOLVED_ENDPOINT`），先判自环的话它会被
         静默丢掉，而"模型写了个不存在的分支序号"这件事就再也没有痕迹了。
      2. **自环丢掉**（静默）。它到不了契约层（那里硬拒），而在这里报码会给前端
         多一个需求里没定义的码——三个码的码表已经够用。
      3. **去重**（`(source, target, label)`，静默）。重复边不改变图的意思。
      4. **截到 `MAX_EDGES` + `EDGE_LIMIT`**，再逐条加入、形成超长环的那条丢掉
         并报 `EDGE_CYCLE_TOO_LONG`。

    第 4 步是**增量**的：从空图开始一条条加，加之前问"加上它会不会形成一个超过
    12 个节点的环"。这样最终图里必然没有超长环，而不需要事后再去找、再决定丢
    哪一条（丢"刚加的那条"是最不伤已经画好的结构的选法）。
    """
    kept: list[Edge] = []
    dangling: list[Edge] = []
    long_cycles: list[Edge] = []
    seen_keys: set[tuple[str, str, str | None]] = set()
    seen_ids: set[str] = set()

    for edge in edges:
        # 顺序不能反：dangling 先判。解析失败的端点是一条哨兵边（两端都是
        # `UNRESOLVED_ENDPOINT`），先判自环的话它会被"静默丢弃"那条规则吃掉，
        # 于是"模型写了一个不存在的分支序号"在诊断里一个字都没有。
        if edge.source not in node_ids or edge.target not in node_ids:
            dangling.append(edge)
            continue
        if edge.source == edge.target:
            continue
        key = (edge.source, edge.target, edge.label)
        if key in seen_keys or edge.id in seen_ids:
            continue
        seen_keys.add(key)
        seen_ids.add(edge.id)
        kept.append(edge)

    diagnostics: list[Diagnostic] = []
    if dangling:
        names = "、".join(edge.id for edge in dangling[:5])
        more = f" 等 {len(dangling)} 条" if len(dangling) > 5 else ""
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.warn, code=EDGE_DANGLING,
            message=(f"{len(dangling)} 条附加边指向不存在的节点（{names}{more}），"
                     "已丢弃；其余边照常保留"),
        ))

    if len(kept) > MAX_EDGES:
        dropped = len(kept) - MAX_EDGES
        kept = kept[:MAX_EDGES]
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.warn, code=EDGE_LIMIT,
            message=(f"模型给了 {MAX_EDGES + dropped} 条附加边，超过上限 {MAX_EDGES} 条，"
                     f"已保留前 {MAX_EDGES} 条"),
        ))

    accepted: list[Edge] = []
    for edge in kept:
        # 增量判环：新边 `s → t` 加上"从 t 回到 s 的一条 ≥12 步的简单路径"
        # 就是一个超长环。只在**已经接受的边**上看，所以判据与最终图一致。
        if _has_path_at_least(
            _adjacency(accepted), edge.target, edge.source, MAX_EDGE_CYCLE_NODES,
        ):
            long_cycles.append(edge)
            continue
        accepted.append(edge)

    if long_cycles:
        names = "、".join(edge.id for edge in long_cycles[:3])
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.warn, code=EDGE_CYCLE_TOO_LONG,
            message=(f"{len(long_cycles)} 条附加边会形成超过 {MAX_EDGE_CYCLE_NODES} "
                     f"个节点的环（{names}），已丢弃；其余边照常保留"),
        ))
    return accepted, diagnostics
