"""三段式的第三段：**只写 detail 的那一次调用**（节点够多时是**两次并发**）。

为什么单独一个文件：这一段的价值全在"错了也不能把已经成型的树弄坏"，而它的
失败面有八种，每一种都长得像"成功了一个短一点的版本"：

  1. 模型少返回几个 id（→ 缺的保持原样，记 `DETAIL_ROUND_INCOMPLETE`）；
  2. 返回不存在的 id（→ 忽略并计数，诊断里说出来）；
  3. 返回空串 / 不是字符串（→ 当没给）；
  4. 整次调用失败（→ 树照常交付，记 `DETAIL_CALL_FAILED`）；
  5. 额度用完（→ 不发抖，记 `DETAIL_SKIPPED_NO_BUDGET`）；
  6. 结构那一次自己就写够了说明（→ 根本不发第三次调用）。
  7. **一半失败、另一半成功**（→ 成功那半必须落地，失败只报它自己那一半）；
  8. **两半返回到同一个 id**（→ 保留先写的，记 `DETAIL_DUPLICATE_IDS`）。

另一半是**"按 id 对号"不能退化成"按顺序对号"**：顺序错一位，整棵树的说明会整体
串行错位，而每一段话本身都通顺——从产出上完全看不出来。所以有一条用例专门把两个
节点的说明故意写反着返回。
"""
from __future__ import annotations

import asyncio
import re
from unittest.mock import patch

import pytest

from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
from app.agent.tools import (
    DETAIL_FILL_FLOOR,
    DETAIL_MAX_CHARS,
    DETAIL_MIN_CHARS,
    DETAIL_ONLY_PROMPT,
    DETAIL_SPLIT_MIN,
    PLAN_PROMPT,
    SKELETON_TREE_RULES,
    _DetailHalf,
    _iter_paths,
    _settle_halves,
    _split_nodes,
    build_registry,
)
from app.contracts.mindmap import MAX_NODE_DETAIL_CHARS
from app.contracts.source import Source, SourceKind
from app.generation.service import GenerationError

TOPIC = "机器学习的主要方向"
BRANCHES = ("监督学习", "无监督学习", "模型与方法")

# 走**拆两半**那条路的树：5 支 × 3 要点 = 21 个节点（门槛是 `DETAIL_SPLIT_MIN`）。
# 21 是有意选的：按前序排下来，中点 11 正好落在第 3 支的孩子中间，吸附到父边界会
# 把它挪到 10（第 3 支整支留在前半）——`test_the_split_never_breaks_a_parent_apart`
# 钉的就是这件事。
WIDE_BRANCHES = ("监督学习", "无监督学习", "模型与方法", "工程实践", "评估与选择")
WIDE_POINTS = 3

LONG = ("这是一段写到八十个字以上的具体说明：它讲清这个要点是什么、怎么做、"
        "在什么条件下成立，并给出可以核对的数字或判断标准，而不是把标题换个说法。")

# 一份**超过 `SHORT_MATERIAL_CHARS`（300 字）**的材料，章节标题与 `BRANCHES` 一一对应：
# 短于阈值时整份材料会被降级成"主题补充说明"（`test_one_path` 里有那条），按支检索
# 就不再发生，而这两条用例要验的正是"证据与引用在第三段之后仍然完好"。
MATERIAL_TEXT = (
    "# 机器学习讲义\n\n"
    "## 监督学习\n\n"
    "监督学习用带标签的数据训练，分类与回归都属于它。常见模型有线性回归、决策树与"
    "神经网络；评价指标看准确率与召回率，训练集与测试集要分开，交叉验证用来估计"
    "泛化误差。正则化项抑制过拟合，学习率决定每一步走多远；类别不平衡时看 F1 "
    "而不是准确率，标注噪声会直接变成模型的上限。\n\n"
    "## 无监督学习\n\n"
    "无监督学习在无标注的数据上找结构，聚类、降维与异常检测是三类主要任务。"
    "K-means 与层次聚类按距离度量划分，PCA 把高维数据投到低维；聚类个数通常靠"
    "轮廓系数或肘部法则挑，降维之后仍要保留主要方差，异常检测把密度低处的点"
    "当作可疑样本。\n\n"
    "## 模型与方法\n\n"
    "线性模型、树模型与神经网络是三类常见载体；集成方法把多个弱模型拼成强模型。"
    "特征工程决定上限，模型容量要与数据量匹配；训练轮次、批大小与正则强度是最常"
    "调的三个参数，早停与权重衰减都能压住过拟合。\n"
)


def _material() -> Source:
    return Source(id="s-ml", kind=SourceKind.document, status="parsed",
                  filename="讲义.md", text=MATERIAL_TEXT)


def _request(sources: list[Source] | None = None, *, calls: int = 20) -> AgentRunRequest:
    return AgentRunRequest(
        goal=TOPIC, intent="create", thinking_model="default", sources=sources or [],
        budget=RunBudget(max_model_calls=calls, max_tool_calls=8, max_seconds=60.0,
                         max_output_tokens=8192, max_repairs=1),
    )


def _ctx(sources: list[Source] | None = None, *, calls: int = 20) -> RunContext:
    payload = sources or []
    return RunContext(request=_request(payload, calls=calls),
                      sources_by_id={source.id: source for source in payload})


def _tree(*, detail: str = "") -> dict:
    """三条一级分支、每支两个要点。`detail` 是结构那一次写的说明（默认留空）。"""
    return {"title": TOPIC, "root": {
        "id": "root", "label": TOPIC, "detail": detail, "children": [
            {"id": f"b{index}", "label": title, "detail": detail, "children": [
                {"id": f"b{index}-1", "label": f"{title}·一", "detail": detail},
                {"id": f"b{index}-2", "label": f"{title}·二", "detail": detail},
            ]}
            for index, title in enumerate(BRANCHES)
        ]}}


def _wide_tree(*, detail: str = "") -> dict:
    """5 支 × 3 要点 = 21 个节点的大树（走拆两半那条路）。"""
    return {"title": TOPIC, "root": {
        "id": "root", "label": TOPIC, "detail": detail, "children": [
            {"id": f"w{index}", "label": title, "detail": detail, "children": [
                {"id": f"w{index}-{point}", "label": f"{title}·{point}", "detail": detail}
                for point in range(1, WIDE_POINTS + 1)
            ]}
            for index, title in enumerate(WIDE_BRANCHES)
        ]}}


def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)


def _by_id(root) -> dict:
    return {node.id: node for node in _walk(root)}


def _ids_in_ask(user: str) -> set[str]:
    """一次「只写说明」的 user 消息里点名的节点 id（行格式见 `_detail_ask`）。"""
    return set(re.findall(r"id=(\S+?)｜", user))


def _codes(result) -> list[str]:
    return [item["code"] for item in result.data["diagnostics"]]


def _messages(result, code: str) -> list[str]:
    return [item["message"] for item in result.data["diagnostics"] if item["code"] == code]


def _chat(seen: list[tuple[str, str]], tree: dict, answer, *, planned=BRANCHES):
    """模型替身。`answer` 可以是 dict、也可以是 `(system, user) -> Any` 的函数。"""
    async def chat(system: str, user: str, *, max_tokens=None, on_delta=None) -> dict:
        seen.append((system, user))
        if system.startswith(PLAN_PROMPT):
            return {"branches": list(planned)}
        if system.startswith(DETAIL_ONLY_PROMPT):
            return answer(system, user) if callable(answer) else answer
        return tree
    return chat


def _run(ctx: RunContext, chat, *, branches: int = 3):
    registry = build_registry("create", has_sources=bool(ctx.request.sources),
                              has_library=False)
    with patch("app.agent.tools.chat_async", new=chat):
        return asyncio.run(registry.execute(
            ToolCall(call_id="c-gen", name="mindmap_generate",
                     arguments={"topic": TOPIC, "branches": branches}), ctx))


# ---------------------------------------------------------------- 1. 提示词


def test_the_structure_prompt_forbids_detail():
    """结构那一轮**明确不写** detail。

    上一版在这里写"每个节点不少于 60 字"，实测无效（detail 反而更短）。现在这
    两句都必须在：一句说"不要写"，一句说"留空字符串"——只写"写短一点"会让模型
    写一句短语，而那既不达标，也说不清为什么会这样。
    """
    assert "这一轮不要写 detail" in SKELETON_TREE_RULES
    assert '空字符串 ""' in SKELETON_TREE_RULES
    # 被实测否掉的那句必须真的不在了（留着它只是噪声，还会与"留空"打架）
    assert "不少于 60 字" not in SKELETON_TREE_RULES


def test_the_detail_prompt_states_the_range_and_the_id_keys():
    """第三段的提示词：目标 80–150 字、返回 `{id: 说明}`、不许动引用与结构。"""
    assert f"{DETAIL_MIN_CHARS}–{DETAIL_MAX_CHARS} 字" in DETAIL_ONLY_PROMPT
    assert '{"d1a":"…","d1b":"…"}' in DETAIL_ONLY_PROMPT
    assert "不要写 source_refs" in DETAIL_ONLY_PROMPT
    assert "不许增删改名" in DETAIL_ONLY_PROMPT
    # 目标区间必须落在契约上限之内，否则提示词会叫模型写一段存不进去的正文
    assert DETAIL_MAX_CHARS < MAX_NODE_DETAIL_CHARS


def test_the_third_call_sees_ids_labels_and_paths():
    """第三段的 user 里每个节点一行：id / label / 位置（层级路径）。

    位置不是装饰：一个 16 字的 label（"实施要点""风险"）在任何主题下都成立，不给
    层级上下文，写出来的说明会飘到别的主题上去。
    """
    seen: list[tuple[str, str]] = []
    tree = _tree()
    ctx = _ctx()
    result = _run(ctx, _chat(seen, tree, {}))
    assert result.ok, result.content

    detail_user = [user for system, user in seen
                   if system.startswith(DETAIL_ONLY_PROMPT)][0]
    assert f"共 {result.data['node_count']} 个节点需要写说明" in detail_user
    for node in _walk(result.data and ctx.candidate.root):
        assert f"id={node.id}" in detail_user
    # 层级路径：二级要点那一行必须带着它的一级分支名
    assert f"位置：{TOPIC} / {BRANCHES[0]}" in detail_user


# ---------------------------------------------------------------- 2. 按 id 对号


def test_details_are_matched_by_id_not_by_order():
    """反着返回也照样落对：**按 id 对号**，不按顺序。

    顺序对号错一位，整棵树的说明会整体串行错位，而每段话本身都通顺——从产出上
    完全看不出来。这里把两个节点的说明故意写反，断言它们各自回到了自己身上。
    """
    seen: list[tuple[str, str]] = []
    ctx = _ctx()

    async def chat(system: str, user: str, *, max_tokens=None, on_delta=None) -> dict:
        seen.append((system, user))
        if system.startswith(PLAN_PROMPT):
            return {"branches": list(BRANCHES)}
        if system.startswith(DETAIL_ONLY_PROMPT):
            return {"b0-2": "第二个要点的说明" * 10, "b0-1": "第一个要点的说明" * 10,
                    "root": LONG}
        return _tree()

    result = _run(ctx, chat)
    assert result.ok, result.content
    branch = ctx.candidate.root.children[0]
    assert branch.children[0].detail == "第一个要点的说明" * 10
    assert branch.children[1].detail == "第二个要点的说明" * 10
    # 没返回的节点保持原样（这里是空），不按"顺序"塞一段进去
    assert ctx.candidate.root.children[1].children[0].detail is None
    assert result.data["detail"]["filled"] == 3
    assert result.data["detail"]["requested"] == 1 + 3 + 6


def test_a_wrapped_or_list_shaped_answer_is_accepted():
    """多包一层、或写成 `[{"id","detail"}]` 列表，都认——形状容错，内容不编。"""
    for answer in (
        {"details": {"root": LONG}},
        [{"id": "root", "detail": LONG}],
        {"root": {"detail": LONG}},
    ):
        ctx = _ctx()
        result = _run(ctx, _chat([], _tree(), answer))
        assert result.ok, result.content
        assert ctx.candidate.root.detail == LONG, answer
        assert result.data["detail"]["filled"] == 1


# ---------------------------------------------------------------- 3. 六种兜底


def test_missing_ids_keep_their_original_detail_and_warn():
    """少返回几个 → 缺的**保持原样**（不编），记一条 warn 把 label 列出来。"""
    ctx = _ctx()
    result = _run(ctx, _chat([], _tree(), {"root": LONG, "b0": LONG}))

    assert result.ok, result.content
    assert "DETAIL_ROUND_INCOMPLETE" in _codes(result)
    assert result.data["detail"] == {
        "requested": 10, "filled": 2, "missing": 8, "unknown_keys": 0,
        "seconds": pytest.approx(result.data["detail"]["seconds"]),
        "floor": DETAIL_FILL_FLOOR, "short_nodes": 8,
    }
    assert ctx.candidate.root.children[0].children[0].detail is None
    # 用户看得见：话术里必须说出来少了几个
    assert "8 个节点的说明没写回来" in result.content


def test_unknown_ids_are_ignored_but_counted():
    """不存在的 id → 忽略、计数，并写进诊断（对不上号时这是唯一的线索）。"""
    ctx = _ctx()
    result = _run(ctx, _chat([], _tree(), {"root": LONG, "d1a": LONG, "nope": LONG}))

    assert result.ok, result.content
    assert result.data["detail"]["unknown_keys"] == 2
    assert result.data["detail"]["filled"] == 1
    assert "DETAIL_ROUND_INCOMPLETE" in _codes(result)


def test_blank_values_count_as_missing():
    """空串 / 纯空白 / 非字符串都是"没给"：不编一段，也不把它当成功。"""
    ctx = _ctx()
    result = _run(ctx, _chat([], _tree(), {"root": "   ", "b0": 42, "b1": LONG}))

    assert result.ok, result.content
    assert ctx.candidate.root.detail is None
    assert ctx.candidate.root.children[0].detail is None
    assert ctx.candidate.root.children[1].detail == LONG
    assert result.data["detail"]["filled"] == 1


def test_an_empty_answer_warns_and_keeps_the_tree():
    """一个都对不上 → `DETAIL_ROUND_EMPTY`，树与引用照样交付。"""
    ctx = _ctx()
    result = _run(ctx, _chat([], _tree(), {}))

    assert result.ok, result.content
    assert "DETAIL_ROUND_EMPTY" in _codes(result)
    assert result.data["detail"]["filled"] == 0
    assert result.data["node_count"] == 10


def test_a_failed_detail_call_does_not_lose_the_tree():
    """第三段整次失败 → 树照常交付，只记一条 warn。

    `detail` 是可选字段：为了"说明写短了"把已经成型的结构与引用丢掉，是拿确定的
    好换不确定的好。
    """
    def boom(system: str, user: str):
        raise GenerationError("模型服务返回 HTTP 500")

    ctx = _ctx()
    result = _run(ctx, _chat([], _tree(), boom))

    assert result.ok, result.content
    assert "DETAIL_CALL_FAILED" in _codes(result)
    assert result.data["detail"]["skipped"] == "error"
    assert ctx.candidate is not None and result.data["node_count"] == 10


def test_no_budget_skips_the_third_call_without_failing():
    """额度只够骨架 + 结构 → 第三段不发，记 `DETAIL_SKIPPED_NO_BUDGET`。"""
    seen: list[tuple[str, str]] = []
    ctx = _ctx(calls=2)
    result = _run(ctx, _chat(seen, _tree(), {"root": LONG}))

    assert result.ok, result.content
    assert len(seen) == 2
    assert "DETAIL_SKIPPED_NO_BUDGET" in _codes(result)
    assert result.data["detail"]["skipped"] == "budget"
    assert result.data["detail"]["filled"] == 0


def test_nothing_to_fill_means_no_third_call():
    """结构那一次自己就写够了说明 → 不花第三次调用。

    判据是长度（`DETAIL_FILL_FLOOR`），不是"空不空"：结构那轮偶尔会写几句，
    26–28 字那种恰恰是要消灭的形态，按"空才补"会把它整批放过。
    """
    seen: list[tuple[str, str]] = []
    ctx = _ctx()
    result = _run(ctx, _chat(seen, _tree(detail=LONG), {"root": LONG}))

    assert result.ok, result.content
    assert len(seen) == 2
    assert result.data["detail"]["skipped"] == "nothing_to_fill"
    assert result.data["detail"]["requested"] == 0
    assert ctx.candidate.root.detail == LONG


def test_too_long_detail_is_clipped_to_the_contract_limit():
    """超过契约上限（2000 字）的说明被截断，而不是让整棵树校验失败。"""
    ctx = _ctx()
    result = _run(ctx, _chat([], _tree(), {"root": "字" * (MAX_NODE_DETAIL_CHARS + 500)}))

    assert result.ok, result.content
    assert len(ctx.candidate.root.detail) == MAX_NODE_DETAIL_CHARS


# ---------------------------------------------------------------- 4. 引用与结构不动


def test_the_third_call_cannot_add_or_change_source_refs():
    """第三段只读 `id → 说明`：它返回的任何引用都进不了树。

    这是"引用会不会失效"这条风险的代码侧答案：写回的字段只有 `detail`。结构那一次
    没写引用，第三段就算写了一大堆，`source_refs` 也必须还是 0——`EVIDENCE_UNUSED`
    的分母因此完全不受这一段影响。
    """
    material = _material()
    ctx = _ctx([material])

    def answer(system: str, user: str) -> dict:
        return {"root": LONG,
                "b0": {"detail": LONG,
                       "source_refs": [{"source_id": "s-ml", "quote": "监督学习用带标签的数据训练"}]}}

    seen: list[tuple[str, str]] = []
    result = _run(ctx, _chat(seen, _tree(), answer))

    assert result.ok, result.content
    assert result.data["source_refs"] == {"declared": 0, "kept": 0}
    assert "EVIDENCE_UNUSED" in _codes(result)
    refs = [ref for node in _walk(ctx.candidate.root) for ref in node.source_refs]
    assert refs == []
    # 结构也没被动过：label 与层级与结构那一次一致
    assert [child.label for child in ctx.candidate.root.children] == list(BRANCHES)
    assert [len(child.children) for child in ctx.candidate.root.children] == [2, 2, 2]


def test_the_structure_response_still_carries_the_refs():
    """引用的唯一来源仍是结构那一次：它写的引用活到了树上，第三段没有把它冲掉。

    另一半（`EVIDENCE_UNUSED`）在上面那条里。两条合起来才是"引用没有失效"。
    """
    material = _material()
    ctx = _ctx([material])

    async def chat(system: str, user: str, *, max_tokens=None, on_delta=None) -> dict:
        if system.startswith(PLAN_PROMPT):
            return {"branches": list(BRANCHES)}
        if system.startswith(DETAIL_ONLY_PROMPT):
            return {"root": LONG}
        quote = "监督学习用带标签的数据训练"
        assert quote in user, user
        tree = _tree()
        tree["root"]["children"][0]["children"][0]["source_refs"] = [
            {"source_id": "s-ml", "start": 0, "end": len(quote), "quote": quote}]
        return tree

    result = _run(ctx, chat)
    assert result.ok, result.content
    refs = [ref for node in _walk(ctx.candidate.root) for ref in node.source_refs]
    assert len(refs) == 1, result.data["source_refs"]
    assert result.data["source_refs"] == {"declared": 1, "kept": 1}


# ---------------------------------------------------------------- 5. 三次调用的账


def test_each_call_is_timed_so_the_report_can_say_where_the_seconds_go():
    """`data["calls"]` 三列：骨架 / 结构 / 说明。检索在 `retrieval.seconds` 里。

    总耗时只能回答"慢不慢"，这一笔回答"慢在哪"——对比表就是按这三列拼的。
    """
    ctx = _ctx()
    result = _run(ctx, _chat([], _tree(), {"root": LONG}))

    assert result.ok, result.content
    calls = result.data["calls"]
    assert set(calls) == {"skeleton", "structure", "detail"}
    assert all(isinstance(value, float) for value in calls.values())
    assert calls["detail"] == result.data["detail"]["seconds"]


def test_the_third_call_reports_a_stage_frame():
    """接了事件通道时，第三段开始与结束各推一帧 `stage="detail"`。

    这一段要 13–46 秒，而树已经在结构那一次长完了（`node_grow` 不再推帧）：不推这
    两帧，用户看到的是"树长完了、然后卡住半分钟"。帧形状与外部检索同形（开始没有
    `count`、结束带条数），前端不用为它分叉。
    """
    frames: list[dict] = []

    async def emit(frame) -> None:
        frames.append(frame)

    ctx = _ctx()
    ctx.emit = emit
    result = _run(ctx, _chat([], _tree(), {"root": LONG, "b0": LONG}))

    assert result.ok, result.content
    stages = [frame for frame in frames if frame.get("type") == "stage"]
    assert [frame["stage"] for frame in stages] == ["detail", "detail"]
    assert stages[0].get("count") is None and "正在给 10 个节点写说明" in stages[0]["detail"]
    assert stages[1]["count"] == 2


# ---------------------------------------------------------------- 6. 拆两半（并发）


def _wide_run(seen: list[tuple[str, str]], answer, *, calls: int = 20):
    """跑一棵 21 节点的树（`_wide_tree`），返回 `(ctx, result, tree)`。"""
    ctx = _ctx(calls=calls)
    result = _run(ctx, _chat(seen, _wide_tree(), answer, planned=WIDE_BRANCHES),
                  branches=len(WIDE_BRANCHES))
    return ctx, result


def test_a_big_tree_is_written_by_two_concurrent_calls():
    """节点够多时，说明这一段发**两次**请求（总调用 3 → 4），两半的 id 不重不漏。

    为什么按节点数拆而不是按一级分支：墙钟由输出量决定。这条用例钉的是"拆了、且
    拆得干净"——两半的 id 并集必须正好是全部待补节点（少一个就是整段说明丢失），
    交集必须为空（多一个就是同一段话写两遍）。
    """
    seen: list[tuple[str, str]] = []

    def answer(system: str, user: str) -> dict:
        return {node_id: LONG for node_id in _ids_in_ask(user)}

    ctx, result = _wide_run(seen, answer)
    assert result.ok, result.content

    asks = [user for system, user in seen if system.startswith(DETAIL_ONLY_PROMPT)]
    assert len(asks) == 2, "21 个节点超过 DETAIL_SPLIT_MIN，应该拆成两次"
    halves = [_ids_in_ask(user) for user in asks]
    assert not (halves[0] & halves[1]), "两半不能有交集"
    assert halves[0] | halves[1] == set(_by_id(ctx.candidate.root))
    # 两次的规模要接近：按支分才是 15/6 那种差一倍的分法
    total = len(halves[0]) + len(halves[1])
    assert abs(len(halves[0]) - len(halves[1])) <= max(2, total // 8)
    assert result.data["detail"]["filled"] == result.data["detail"]["requested"]
    assert [half["skipped"] for half in result.data["detail"]["halves"]] == [None, None]


def test_the_split_never_breaks_a_parent_apart():
    """切点吸附到父边界：**紧挨切点的那一对不能是兄弟**。

    "同一支一起写"按支分能百分百保证，但它把两半的输出量拉成 3 : 2（慢的那半多写
    43%，并发就白并发）。按节点数均分 + 吸附到父边界两头都要：中点 11 落在第 3 支
    的孩子中间，吸附把它挪到 10——第 3 支的三个要点因此整组留在后半。

    判"兄弟"用的是**父节点的 id**，不是 `path`：一级分支的 path 都是 `(主题,)`，
    按 path 判会把 5 条分支当成兄弟。而根的孩子被劈开是**注定**的（它必然有两组
    以上的孩子），也不构成问题——各条一级分支讲的是完全不同的事，不靠同一份上下文
    区分；真正需要共享上下文的是"细分层的一组要点"，它们的父节点孩子都不跨半。
    """
    ctx, result = _wide_run([], {"root": LONG})
    assert result.ok, result.content

    root = ctx.candidate.root
    parent = {child.id: node.id for node in _walk(root) for child in node.children}
    # 用 `_iter_paths` 而不是 `_detail_targets`：后者按**当前** detail 长度过滤，而
    # 跑完之后 root 已经被填上、不再是"待补"的那个了——切分当时看到的是全部 21 个。
    targets = list(_iter_paths(root))
    halves = _split_nodes(targets)
    assert len(halves) == 2
    # 1) 切点两侧不是兄弟——这正是吸附的目的
    assert parent[halves[0][-1][0].id] != parent[halves[1][0][0].id]
    # 2) 除根以外，每个父节点的孩子都在同一半
    sides: dict[str, set[int]] = {}
    for index, half in enumerate(halves):
        for node, _ in half:
            sides.setdefault(parent.get(node.id, ""), set()).add(index)
    broken = [key for key, value in sides.items() if key != root.id and len(value) > 1]
    assert broken == [], f"这些父节点的孩子被劈开了：{broken}"
    # 中点 11 落在 w2-1 与 w2-2 之间，吸附后应该是 10
    assert [len(half) for half in halves] == [10, 11]


def test_a_small_tree_is_not_split():
    """不到 `DETAIL_SPLIT_MIN` 的树不拆：两次首 token 延迟比省下的那点输出还贵。

    这同时是一条**回归**用例：不拆时 `data["detail"]` 的键集与加并发之前逐字相同
    （没有 `halves`），与 `data["external"]` 只在开关打开时出现是同一条约定。
    """
    seen: list[tuple[str, str]] = []
    ctx = _ctx()
    result = _run(ctx, _chat(seen, _tree(), {"root": LONG}))

    assert result.ok, result.content
    assert len([user for system, user in seen
                if system.startswith(DETAIL_ONLY_PROMPT)]) == 1
    assert "halves" not in result.data["detail"]
    assert result.data["node_count"] < DETAIL_SPLIT_MIN


def test_one_half_failing_keeps_the_other_halfs_details():
    """一半失败、一半成功：成功那半的说明**必须落地**。

    这是 `asyncio.gather(return_exceptions=True)` 在这里的全部意义。没有它，第 2 半
    一个 500 就把第 1 半已经写好的二十来段话一起丢掉——那比不拆还糟（不拆时至少
    只有一次失败面）。
    """
    seen: list[tuple[str, str]] = []
    counter = {"detail": 0}

    def answer(system: str, user: str):
        counter["detail"] += 1
        if counter["detail"] == 2:
            raise GenerationError("模型服务返回 HTTP 500")
        return {node_id: LONG for node_id in _ids_in_ask(user)}

    ctx, result = _wide_run(seen, answer)
    assert result.ok, result.content

    nodes = _by_id(ctx.candidate.root)
    asks = [user for system, user in seen if system.startswith(DETAIL_ONLY_PROMPT)]
    first, second = (_ids_in_ask(user) for user in asks)
    assert all(nodes[node_id].detail == LONG for node_id in first), "第 1 半的结果不能丢"
    assert all(nodes[node_id].detail is None for node_id in second)
    assert result.data["detail"]["filled"] == len(first)
    assert "DETAIL_CALL_FAILED" in _codes(result)
    # 说清是哪一半失败的：只说"调用失败"，读者会以为是整段
    assert any("第 2 半" in message for message in _messages(result, "DETAIL_CALL_FAILED"))


def test_both_halves_failing_reports_the_same_skipped_value():
    """两半都失败 → `skipped == "error"`，与不拆时同名同义。

    读 `data.detail.skipped` 的代码（以及话术）不该为"拆了没拆"多分一个分支：两次
    都失败就是"整件事一次也没做成"，和不拆时的失败是同一件事。
    """
    def boom(system: str, user: str):
        raise GenerationError("模型服务返回 HTTP 500")

    ctx, result = _wide_run([], boom)
    assert result.ok, result.content
    assert result.data["detail"]["skipped"] == "error"
    assert result.data["detail"]["filled"] == 0
    assert len(_messages(result, "DETAIL_CALL_FAILED")) == 2
    assert "DETAIL_ROUND_EMPTY" not in _codes(result)


def test_an_incomplete_half_says_which_half_and_how_many():
    """一半少写了几个 → `DETAIL_ROUND_INCOMPLETE` 要说清"哪一半、缺几个、缺哪几个"。"""
    seen: list[tuple[str, str]] = []
    counter = {"detail": 0}

    def answer(system: str, user: str) -> dict:
        counter["detail"] += 1
        ids = sorted(_ids_in_ask(user))
        # 第 1 半少写两个，第 2 半写全
        keep = ids[:-2] if counter["detail"] == 1 else ids
        return {node_id: LONG for node_id in keep}

    ctx, result = _wide_run(seen, answer)
    assert result.ok, result.content

    assert result.data["detail"]["missing"] == 2
    message = _messages(result, "DETAIL_ROUND_INCOMPLETE")[0]
    assert "第 1 半" in message and "缺 2 个" in message
    assert "第 2 半" in message and "齐全" in message
    blockers = _by_id(ctx.candidate.root)
    assert sum(1 for node in blockers.values() if node.detail is None) == 2


def test_an_id_from_the_other_half_is_dropped_not_used():
    """一半返回到另一半的节点 → 丢弃（记进 `unknown_keys`），不留、也不覆盖。

    切分保证两半的 id 不重叠，所以交集只可能来自"模型多写了另一半的节点"。这种
    返回**必须丢**：那一半的 user 里没有这些节点的 label 与位置（`_detail_ask` 只
    列本半的清单），模型为它们写的话是凭空编的——而它长得完全像一段正常说明，
    采用它比留空更糟。这条同时钉住"两半的 `applied` 天然不相交"这个结构性保证。
    """
    seen: list[tuple[str, str]] = []
    counter = {"detail": 0}

    def answer(system: str, user: str) -> dict:
        counter["detail"] += 1
        mine = {node_id: f"第{counter['detail']}半写的说明" for node_id in _ids_in_ask(user)}
        mine["root"] = f"第{counter['detail']}半写的根说明"     # 两半都会返回到 root
        return mine

    ctx, result = _wide_run(seen, answer)
    assert result.ok, result.content

    # 只有第 1 半的清单里有 root，所以它的说明来自第 1 半；第 2 半那份被丢掉
    assert ctx.candidate.root.detail == "第1半写的根说明"
    assert result.data["detail"]["unknown_keys"] == 1
    assert result.data["detail"]["filled"] == result.data["detail"]["requested"]
    assert "DETAIL_ROUND_INCOMPLETE" not in _codes(result)


def test_only_one_half_runs_when_one_call_of_budget_is_left():
    """额度只剩一份 → 只发一次说明请求，另一半记 `budget` 并说清"请求没发出去"。"""
    seen: list[tuple[str, str]] = []
    ctx = _ctx(calls=3)     # 骨架 1 + 结构 1 + 说明 1
    result = _run(ctx, _chat(seen, _wide_tree(),
                             lambda system, user: {
                                 node_id: LONG for node_id in _ids_in_ask(user)},
                             planned=WIDE_BRANCHES),
                  branches=len(WIDE_BRANCHES))

    assert result.ok, result.content
    assert len([user for system, user in seen
                if system.startswith(DETAIL_ONLY_PROMPT)]) == 1
    halves = result.data["detail"]["halves"]
    assert halves[1]["skipped"] == "budget" and halves[1]["filled"] == 0
    assert result.data["detail"]["missing"] == halves[1]["requested"]
    message = _messages(result, "DETAIL_ROUND_INCOMPLETE")[0]
    assert "第 2 半" in message and "没有额度" in message


def test_detail_seconds_is_the_wall_clock_of_the_slower_half():
    """`data["detail"]["seconds"]` 是**墙钟**（两半取大的那个），不是两次之和。

    两半并发，把它算成 17 + 17 = 34 会让对比表整列失真——那也正是这次改动的全部
    目的所在。用真实的 `asyncio.sleep` 量：第 1 半 0.02 秒、第 2 半 0.08 秒，
    串行要 0.10 秒，并发只要 0.08 秒。
    """
    counter = {"detail": 0}

    def answer(system: str, user: str) -> dict:
        counter["detail"] += 1
        return {"__delay__": 0.02 if counter["detail"] == 1 else 0.08}

    def build(system: str, user: str):
        return answer(system, user)

    async def chat(system: str, user: str, *, max_tokens=None, on_delta=None) -> dict:
        if system.startswith(PLAN_PROMPT):
            return {"branches": list(WIDE_BRANCHES)}
        if system.startswith(DETAIL_ONLY_PROMPT):
            payload = build(system, user)
            await asyncio.sleep(payload.pop("__delay__"))
            return {node_id: LONG for node_id in _ids_in_ask(user)}
        return _wide_tree()

    ctx = _ctx()
    result = _run(ctx, chat, branches=len(WIDE_BRANCHES))
    assert result.ok, result.content

    halves = result.data["detail"]["halves"]
    assert halves[0]["seconds"] >= 0.02 and halves[1]["seconds"] >= 0.08
    assert result.data["detail"]["seconds"] == max(
        half["seconds"] for half in halves)
    # 真的并发：墙钟明显小于两半之和
    assert result.data["detail"]["seconds"] < sum(half["seconds"] for half in halves)


def test_the_stage_frames_still_report_the_whole_node_count():
    """拆成两半之后，用户看到的仍是"给 21 个节点写说明"，不是"11 个"。

    帧由 `_fill_details` 统一推、两半自己不推。前端 `stageText` 是覆盖式单值：
    两半各推一帧"正在给 11 个节点写说明"只会互相顶掉（同一句话闪两遍），而各推
    一帧"已写完 11"更糟——总共 21 个，那句话在另一半还没回来时就是谎报。
    """
    frames: list[dict] = []

    async def emit(frame) -> None:
        frames.append(frame)

    ctx = _ctx()
    ctx.emit = emit
    result = _run(ctx, _chat([], _wide_tree(),
                             lambda system, user: {
                                 node_id: LONG for node_id in _ids_in_ask(user)},
                             planned=WIDE_BRANCHES),
                  branches=len(WIDE_BRANCHES))

    assert result.ok, result.content
    stages = [frame for frame in frames if frame.get("type") == "stage"]
    assert [frame["stage"] for frame in stages] == ["detail", "detail"]
    assert stages[0].get("count") is None
    assert "正在给 21 个节点写说明" in stages[0]["detail"]
    assert stages[1]["count"] == 21


def test_cancellation_is_reraised_instead_of_being_swallowed():
    """`return_exceptions=True` 收进来的 `CancelledError` 必须原样抛回去。

    吞掉它等于让 `max_seconds` 超时与用户中途取消失效：工具还在跑、模型请求还在
    计费，而 `_run_tool` 那边以为已经停了。这条用例钉在纯函数上，不依赖调度。
    """
    with pytest.raises(asyncio.CancelledError):
        _settle_halves([_DetailHalf(requested=3), asyncio.CancelledError()],
                       [[(None, ())], [(None, ())]])


def test_an_unexpected_exception_only_costs_its_own_half():
    """不是 `GenerationError` 的错（多半是代码问题）也只影响它自己那一半。

    `failure` 里带异常类型：只看"调用失败"四个字，分不出是模型 500 还是
    `AttributeError`——后者是要去改代码的。
    """
    settled = _settle_halves([_DetailHalf(requested=2, applied={"a": LONG}),
                              RuntimeError("boom")],
                             [[(None, ())], [(None, ())]])
    assert settled[0].applied == {"a": LONG}
    assert settled[1].skipped == "error" and "RuntimeError" in settled[1].failure
