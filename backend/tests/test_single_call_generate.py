"""整树一次生成的契约：骨架从哪来、证据怎么给、一次写完、旧契约不回归。

这条路（`mindmap_generate`）与分片（`mindmap_generate_deep`）共用骨架与检索，
差别只有生成调用几次。所以这个文件盯四件事：

  1. **骨架的两个来源**：有材料时用材料自带的章节（`_skeleton_outline`，覆盖率
     ≥60% 才拼进去），没有材料时由模型从主题定。两条路走的是同一次调用。
  2. **证据按支给、每支各检索一次**：退化成"一次主题检索 top_k=5"的表现是
     `queries` 只有一条，而那正是当年整树路径覆盖 5.1% 的原因。
  3. **一次写完**：骨架一次 + 生成一次，就这么多。
  4. **三条旧契约不回归**：`EVIDENCE_UNUSED`、`partial`、`node_grow` 预览，以及
     初次限额（80 节点 / 4 层）一个都没动。

第 4 条里的限额是硬约束：一次响应装不下更多，改了它这条路的全部前提就没了。
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

from app.agent import tools as tools_module
from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
from app.agent.tools import (
    INITIAL_MAX_DEPTH,
    INITIAL_MAX_NODES,
    OUTLINE_HEADER,
    PLAN_PROMPT,
    build_registry,
)
from app.contracts.source import Source, SourceKind
from app.generation.service import SalvagedOutput

TOPIC = "机器学习的主要方向"
BRANCHES = ("监督学习", "无监督学习", "模型与方法")

# 章节结构清楚的材料：`_skeleton_outline` 的覆盖率会接近 100%，目录一定被拼进去。
OUTLINED_MATERIAL = (
    "# 机器学习讲义\n\n"
    "## 监督学习\n\n"
    "监督学习用带标签的数据训练，分类与回归都属于它。常见模型有线性回归、"
    "决策树与神经网络，评价指标看准确率与召回率。训练集与测试集要分开，"
    "交叉验证用来估计泛化误差；正则化项抑制过拟合，学习率决定每一步走多远。"
    "类别不平衡时看 F1 而不是准确率，标注噪声会直接变成模型的上限。\n\n"
    "## 无监督学习\n\n"
    "无监督学习在无标注的数据上找结构，聚类、降维与异常检测是三类主要任务。"
    "K-means 与层次聚类按距离度量划分，PCA 把高维数据投到低维。"
    "聚类个数通常靠轮廓系数或肘部法则挑，降维之后仍要保留主要方差；"
    "异常检测把密度低处的点当作可疑样本，阈值要按业务代价来定。\n\n"
    "## 强化学习\n\n"
    "强化学习靠与环境交互的奖励信号学习策略，马尔可夫决策过程是它的形式化框架。"
    "值函数方法与策略梯度是两条主要路线。回报折扣因子决定看多远，"
    "探索与利用的平衡靠 ε-贪心或熵正则；仿真到真机的差距要靠域随机化缩小，"
    "奖励设计错了会得到一个会钻空子的策略。\n"
)


def _request(sources: list[Source] | None = None) -> AgentRunRequest:
    return AgentRunRequest(
        goal=TOPIC, intent="create", thinking_model="default",
        sources=sources or [],
        budget=RunBudget(max_model_calls=20, max_tool_calls=8, max_seconds=60.0,
                         max_output_tokens=8192, max_repairs=1),
    )


def _ctx(sources: list[Source] | None = None) -> RunContext:
    payload = sources or []
    return RunContext(
        request=_request(payload),
        sources_by_id={source.id: source for source in payload},
    )


def _material(text: str = OUTLINED_MATERIAL, *, source_id: str = "s-ml") -> Source:
    return Source(id=source_id, kind=SourceKind.document, status="parsed",
                  filename="机器学习讲义.md", text=text)


def _run(ctx: RunContext, chat, *, arguments: dict | None = None):
    args = {"topic": TOPIC, "branches": 3}
    args |= arguments or {}
    registry = build_registry("create", has_sources=bool(ctx.request.sources),
                              has_library=False)
    with patch("app.agent.tools.chat_async", new=chat):
        return asyncio.run(registry.execute(
            ToolCall(call_id="c-gen", name="mindmap_generate", arguments=args), ctx))


def _codes(result) -> list[str]:
    return [item["code"] for item in result.data["diagnostics"]]


def _tree(children: list[dict], *, title: str = TOPIC) -> dict:
    return {"title": title, "root": {
        "id": "root", "label": title, "detail": "中心主题", "children": children}}


def _branch(index: int, label: str, points: list[tuple[str, str]],
            *, refs: list[dict] | None = None) -> dict:
    """一条一级分支 + 它的要点。`points` 是 `(label, detail)`。"""
    return {"id": f"b{index}", "label": label, "children": [
        {"id": f"b{index}-{position}", "label": point_label, "detail": detail,
         "source_refs": refs or []}
        for position, (point_label, detail) in enumerate(points)
    ]}


def _two_points(label: str) -> list[tuple[str, str]]:
    return [(f"{label}·一", "一句具体的说明。"), (f"{label}·二", "另一句具体的说明。")]


def _chat(tree: dict, *, seen: list[tuple[str, str]] | None = None,
          on_delta_chunks: int = 0):
    """模型替身：骨架回 `BRANCHES`，生成回 `tree`。

    `on_delta_chunks > 0` 时把整棵树的 JSON 按这个段数喂给 `on_delta`——预览那条
    路只有真收到增量才会往外推帧，假流式是测不出 `node_grow` 的。
    """
    async def chat(system: str, user: str, *, max_tokens=None, on_delta=None) -> dict:
        if seen is not None:
            seen.append((system, user))
        if system.startswith(PLAN_PROMPT):
            return {"branches": list(BRANCHES)}
        if on_delta is not None and on_delta_chunks:
            text = json.dumps(tree, ensure_ascii=False)
            size = max(1, len(text) // on_delta_chunks)
            for start in range(0, len(text), size):
                await on_delta(text[start:start + size])
        return tree
    return chat


def _three_branches(tree: dict | None = None) -> dict:
    return tree or _tree([
        _branch(index, label, _two_points(label))
        for index, label in enumerate(BRANCHES)
    ])


# ---------------------------------------------------------------- 1. 骨架从哪来


def test_skeleton_uses_the_material_outline():
    """有材料且目录覆盖率够 → 骨架那一次的 user 里有材料自带的章节。

    目录进的是 `_plan_ask` 的 user 消息（不是 system）：它的语义是"这一次的输入
    长什么样"，而 system 是跨请求稳定的规则。
    """
    seen: list[tuple[str, str]] = []
    result = _run(_ctx([_material()]), _chat(_three_branches(), seen=seen))

    assert result.ok, result.content
    plan_system, plan_user = seen[0]
    assert plan_system.startswith(PLAN_PROMPT)
    assert OUTLINE_HEADER in plan_user
    # 三个章节名都在（正文一行都不进 prompt）
    for chapter in ("监督学习", "无监督学习", "强化学习"):
        assert chapter in plan_user
    assert "K-means 与层次聚类" not in plan_user


def test_skeleton_falls_back_to_the_topic_without_material():
    """无材料 → 没有目录可拼，骨架由模型从主题定；这条路一次生成照常跑完。"""
    seen: list[tuple[str, str]] = []
    result = _run(_ctx(), _chat(_three_branches(), seen=seen))

    assert result.ok, result.content
    assert OUTLINE_HEADER not in seen[0][1]
    assert TOPIC in seen[0][1]
    # 没有材料时骨架的 system 里有那两句纯文本契约（别写引用、可以自由展开）
    assert "不要写 source_refs" in seen[0][0]
    assert result.data["retrieval"]["mode"] == "off"
    assert result.data["node_count"] == 1 + len(BRANCHES) * 3


# ---------------------------------------------------------------- 2. 证据按支给


def test_each_branch_is_retrieved_once_and_grouped_for_the_model():
    """三条分支 → 三次检索；生成那一次的 user 里三组证据，各带分支归属。

    这条与 `test_one_path.py` 里的同名断言分工不同：那条验"带材料时会检索"，
    这条验**形状**——分组表头里必须有分支标题，否则模型无法知道哪条命中属于哪支
    （分片那条路不需要这层，它每次只给一支看）。
    """
    calls: list[str] = []
    real_search = tools_module._search_branch

    async def counting(sources, title, intents=()):
        calls.append(title)
        return await real_search(sources, title, intents)

    seen: list[tuple[str, str]] = []
    with patch.object(tools_module, "_search_branch", counting):
        result = _run(_ctx([_material()]), _chat(_three_branches(), seen=seen))

    assert result.ok, result.content
    assert calls == list(BRANCHES)
    generate_user = seen[-1][1]
    for index, title in enumerate(BRANCHES, start=1):
        assert f"第 {index} 支「{title}」的检索结果" in generate_user
    # 每条 query 都是分支标题本身（不拼主题，那是实测选出来的）
    assert result.data["retrieval"]["queries"] == list(BRANCHES)
    assert result.data["retrieval"]["top_k"] == 3


def test_generation_is_a_single_call_after_the_skeleton():
    """骨架一次 + 生成一次：这条路的总调用数是 2，与分支数无关。"""
    seen: list[tuple[str, str]] = []
    result = _run(_ctx(), _chat(_three_branches(), seen=seen))

    assert result.ok, result.content
    assert len(seen) == 2, [system[:20] for system, _ in seen]
    # 生成那一次的 user 里必须带着骨架清单：模型据此写出一级分支，而不是自己再定
    generate_user = seen[-1][1]
    for index, title in enumerate(BRANCHES, start=1):
        assert f"{index}. {title}" in generate_user
    assert "不要增删改名" in generate_user


# ---------------------------------------------------------------- 3. 跨支重复


def test_cross_branch_duplicates_are_reported_but_kept():
    """两支写了一字不差的同一节点 → 报一条诊断、保留两个节点、计数进 `data`。

    这是分片那条路的实测缺陷（56 节点的图里 6 处以上），也是这次回到一次生成
    要说清的收益。代码不做删除：删哪个是用户的决定。
    """
    same = ("无监督学习：无标注找结构", "在无标注数据上找结构，聚类与降维是主要任务。")
    tree = _tree([
        _branch(0, BRANCHES[0], [same, ("监督学习·二", "有标注训练。")]),
        _branch(1, BRANCHES[1], [same, ("无监督学习·二", "无标注找结构。")]),
        _branch(2, BRANCHES[2], [("模型与方法·一", "模型是载体。")]),
    ])
    ctx = _ctx()
    result = _run(ctx, _chat(tree))

    assert result.ok, result.content
    assert "CROSS_BRANCH_DUPLICATE" in _codes(result)
    # 计数是**节点数**：一个说法写了两遍 = 2 个重复节点（库里那张图是 3 个说法
    # 各写 2 遍 = 6，与用户实测的口径一致）。
    assert result.data["cross_branch_duplicates"] == 2
    labels = [child.label for child in ctx.candidate.root.children[1].children]
    assert same[0] in labels, "重复的节点被删掉了——这条诊断只报不删"
    # 两个都还在树上
    kept = [
        node.label
        for branch in ctx.candidate.root.children for node in branch.children
        if node.label == same[0]
    ]
    assert len(kept) == 2


def test_same_label_with_different_detail_still_counts_as_a_duplicate():
    """同名不同说明**也算重复**：判据是 label 级，这是拿真实数据校准出来的。

    库里那三张 `机器学习的主要方向`（56/57 节点）各有 6 个跨支重复节点，而它们的
    detail **各不相同**——按"连 detail 也要相同"判，这条诊断对真实缺陷完全瞎掉。
    同一个概念挂在两条一级分支下，无论说明怎么写，用户都会看到两块内容讲同一件事。

    保守的那一半在**不做语义归一**：`无监督学习` 与 `非监督学习` 不算重复。
    """
    tree = _tree([
        _branch(0, BRANCHES[0], [("迁移学习", "它是监督学习的一种延伸：先在大数据集上预训练。")]),
        _branch(1, BRANCHES[1], [("迁移学习", "它也能和强化学习结合：把仿真里学到的策略搬过来。")]),
        _branch(2, BRANCHES[2], [("模型与方法·一", "模型是载体。")]),
    ])
    result = _run(_ctx(), _chat(tree))

    assert result.ok, result.content
    assert "CROSS_BRANCH_DUPLICATE" in _codes(result)
    assert result.data["cross_branch_duplicates"] == 2


def test_near_synonyms_are_not_a_duplicate():
    """同义不同名不算重复：判据只做空白折叠，不做语义归一。

    这条是"宁可漏、不误报"的那一半——误报会让整条信号变成需要人工逐条确认的
    噪声，而这条诊断本身不删任何节点，价值全在"值得看一眼"。
    """
    tree = _tree([
        _branch(0, BRANCHES[0], [("非监督学习", "在无标注数据上找结构。")]),
        _branch(1, BRANCHES[1], [("自监督学习", "同样是在无标注数据上找结构。")]),
        _branch(2, BRANCHES[2], [("模型与方法·一", "模型是载体。")]),
    ])
    result = _run(_ctx(), _chat(tree))

    assert result.ok, result.content
    assert "CROSS_BRANCH_DUPLICATE" not in _codes(result)
    assert result.data["cross_branch_duplicates"] == 0


# ---------------------------------------------------------------- 4. 旧契约不回归


def test_evidence_unused_still_fires():
    """证据拼进了 prompt、树上一条引用都没有 → `EVIDENCE_UNUSED` 必须报。

    这是 `rag` 形态下唯一没有信号的失败面：模型看着证据却没写引用，而产出与
    "本来就没有证据"完全一样。
    """
    tree = _three_branches()  # 三个分支都不带 source_refs
    result = _run(_ctx([_material()]), _chat(tree))

    assert result.ok, result.content
    assert result.data["retrieval"]["branches_with_hits"] >= 1
    assert "EVIDENCE_UNUSED" in _codes(result)
    assert result.data["source_refs"] == {"declared": 0, "kept": 0}


def test_partial_is_reported_when_the_output_is_truncated():
    """输出被长度上限截断（`SalvagedOutput`）→ `partial=true` + 一条 warn。

    截断本身不是失败（回收出来的树仍然可用），但 `ok=true` 与"完整成功"长得
    一模一样，差别只有这个字段与那条诊断。
    """
    tree = SalvagedOutput(_three_branches(), recovered=7)
    result = _run(_ctx(), _chat(tree))

    assert result.ok, result.content
    assert result.data["partial"] is True
    assert "OUTPUT_TRUNCATED_RECOVERED" in _codes(result)


def test_node_grow_streams_the_whole_tree():
    """开了事件通道时，整树响应每写出一个完整节点就推一帧 `node_grow`。

    第一帧必须是根（`path=None`），之后每一帧的 `path` 是**父节点的索引链**——
    这条与分片那条路的预览协议同形，前端不必分叉。
    """
    frames: list[dict] = []

    async def emit(frame) -> None:
        frames.append(frame)

    ctx = _ctx()
    ctx.emit = emit
    result = _run(ctx, _chat(_three_branches(), on_delta_chunks=12))

    assert result.ok, result.content
    grows = [frame for frame in frames if frame.get("type") == "node_grow"]
    assert grows, frames
    assert grows[0]["path"] is None and grows[0]["label"] == TOPIC
    # 一级分支挂在根上（`path=()`），二级挂在它的分支索引下。帧顺序跟着 JSON
    # 文本的增量解析走（深度优先），所以只断言"这几类帧都在"，不钉先后。
    assert [frame["path"] for frame in grows].count([]) == len(BRANCHES)
    assert any(len(frame["path"] or []) == 1 for frame in grows)


def test_initial_limits_are_unchanged():
    """初次限额一个都没动：80 个节点、root 之下 3 层（含根 4 层）。

    一次响应装不下更多——这正是分片那条路存在的理由，也是它在真实用法（100%
    无材料）里没收益的原因。数字改了，这条路的全部前提就没了。
    """
    assert INITIAL_MAX_NODES == 80
    assert INITIAL_MAX_DEPTH == 3
    result = _run(_ctx(), _chat(_three_branches()))
    assert result.ok, result.content
    assert result.data["node_limit"] == INITIAL_MAX_NODES
    assert result.data["depth_limit"] == INITIAL_MAX_DEPTH
    assert result.data["partial"] is False


def test_over_the_node_limit_is_clipped_and_marked_partial():
    """超过 80 个节点 → 裁到 80、记一条 `TREE_CLIPPED_TO_INITIAL`、标 `partial`。

    裁剪而不是判失败：这次调用已经花掉了，前 80 个节点是一棵合法的、父在子前的
    树；判失败就什么都没有，而下一轮大概率还是给这么多。
    """
    wide = _tree([
        _branch(index, f"分支{index}", [
            (f"要点{index}-{position}", "一句具体的说明。") for position in range(6)
        ])
        for index in range(20)
    ])  # 1 + 20 + 120 = 141 个节点
    result = _run(_ctx(), _chat(wide))

    assert result.ok, result.content
    assert result.data["node_count"] == INITIAL_MAX_NODES
    assert result.data["cut_nodes"] > 0
    assert result.data["partial"] is True
    assert "TREE_CLIPPED_TO_INITIAL" in _codes(result)


@pytest.mark.parametrize("branches", [3, 6])
def test_branch_count_only_changes_the_skeleton_size(branches):
    """分支数变化只影响骨架规模，不改变"两次调用"这条结构。"""
    titles = tuple(f"分支{index}" for index in range(branches))
    tree = _tree([
        _branch(index, title, _two_points(title))
        for index, title in enumerate(titles)
    ])
    seen: list[tuple[str, str]] = []

    async def chat(system: str, user: str, *, max_tokens=None, on_delta=None) -> dict:
        if seen is not None:
            seen.append((system, user))
        if system.startswith(PLAN_PROMPT):
            return {"branches": list(titles)}
        return tree

    result = _run(_ctx(), chat, arguments={"branches": branches})
    assert result.ok, result.content
    assert len(seen) == 2
    assert result.data["planned_branches"] == branches
