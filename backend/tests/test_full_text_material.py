"""「装得下的材料直接塞进 prompt」这条路的契约（`FULL_TEXT_MATERIAL_CHARS`）。

与 `test_single_call_generate.py` 的分工：那个文件钉的是"按支检索 + 三段式"这条
既有路，这个文件只钉**新加的那个分叉**，以及它必须不碰的三样东西：

  1. **闸**：材料合计 ≤ 阈值 → 整份正文进 prompt、一条检索都不发；
     > 阈值 → 完全走回按支检索（`queries`、分组表头、`BRANCHED_EVIDENCE_RULES`
     一个字节都不变）。阈值取 0/负数时这条路整个关掉，行为与加它之前相同。
  2. **`SHORT_MATERIAL_CHARS` 一个字不变**：那份材料"太短，短到一条引用就能装下
     整篇"，与"够长但还没长到模型看不住"是两件事。短材料退化**先于**全塞判定，
     即使把全塞阈值调到极大，200 字的材料也必须照旧当主题提示。
  3. **引用校验不失效**：`verify_source_refs` 查的是"quote 在不在 `Source.text` 里"，
     与送不送 prompt 无关。全塞时模型只写 quote（没有偏移），服务端在全文里唯一定位
     并补上 `start`/`end`——这条引用与按支检索那档**长得一模一样**。
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from app.agent import tools as tools_module
from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
from app.agent.tools import (
    BRANCHED_EVIDENCE_RULES,
    DETAIL_ONLY_EVIDENCE_RULES,
    DETAIL_ONLY_FULL_TEXT_RULES,
    DETAIL_ONLY_PROMPT,
    FULL_TEXT_EVIDENCE_RULES,
    FULL_TEXT_SECTION_HEADER,
    LEAF_MAX_CHARS,
    PLAN_PROMPT,
    SHORT_MATERIAL_CHARS,
    build_registry,
)
from app.contracts.source import Source, SourceKind

TOPIC = "机器学习的主要方向"
BRANCHES = ("监督学习", "无监督学习", "强化学习")

MATERIAL = (
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
    "探索与利用的平衡靠 ε-贪心或熵正则；仿真到真机的差距要靠域随机化缩小。\n"
)

# 从 MATERIAL 里逐字取的一句，用来当"模型抄回来的引文"。唯一，所以服务端能定位。
QUOTE = "监督学习用带标签的数据训练，分类与回归都属于它。"

# 一份**短于** `SHORT_MATERIAL_CHARS` 的材料：只要它不被全塞判定接走，
# `MATERIAL_TOO_SHORT` 那条退化就该原样触发。
TINY_MATERIAL = "这份材料只有几十个字，讲的是预算审批要报财务经理核准，别的没了。"


def _request(sources: list[Source]) -> AgentRunRequest:
    return AgentRunRequest(
        goal=TOPIC, intent="create", thinking_model="default", sources=sources,
        budget=RunBudget(max_model_calls=20, max_tool_calls=8, max_seconds=60.0,
                         max_output_tokens=8192, max_repairs=1),
    )


def _ctx(text: str = MATERIAL, *, source_id: str = "s-full") -> RunContext:
    source = Source(id=source_id, kind=SourceKind.document, status="parsed",
                    filename="机器学习讲义.md", text=text)
    return RunContext(request=_request([source]),
                      sources_by_id={source.id: source})


def _tree(refs: list[dict] | None = None) -> dict:
    children = [
        {"id": f"b{index}", "label": label, "detail": "", "children": [
            {"id": f"b{index}-0", "label": f"{label}·一", "detail": "",
             "source_refs": list(refs or []), "children": []},
            {"id": f"b{index}-1", "label": f"{label}·二", "detail": "",
             "source_refs": [], "children": []},
        ]}
        for index, label in enumerate(BRANCHES)
    ]
    return {"title": TOPIC, "root": {
        "id": "root", "label": TOPIC, "detail": "", "children": children}}


def _detail_answer(tree: dict) -> dict:
    return {node["id"]: "这句说明写到八十个字以上，讲清它是什么、怎么做、"
                       "有什么条件与数字，这样第三段的产出才符合长度目标。"
            for node in _walk(tree["root"]) if node.get("id")}


def _walk(node: dict):
    yield node
    for child in node.get("children") or []:
        yield from _walk(child)


def _chat(tree: dict, seen: list[tuple[str, str]]):
    async def chat(system: str, user: str, *, max_tokens=None, on_delta=None) -> dict:
        seen.append((system, user))
        if system.startswith(PLAN_PROMPT):
            return {"branches": list(BRANCHES)}
        if system.startswith(DETAIL_ONLY_PROMPT):
            return _detail_answer(tree)
        return tree
    return chat


def _run(ctx: RunContext, tree: dict, seen: list[tuple[str, str]],
         *, threshold: int, ctx_out: list | None = None):
    """跑一次 `mindmap_generate`，`threshold` 临时替换全塞的闸。

    `ctx_out` 传一个列表进去时把它 append 出来：引用挂在候选树上，而
    `ToolResult.data` 只报条数——要断言"引用真的落到树上、偏移被补上"就得摸到 ctx。
    """
    registry = build_registry("create", has_sources=True, has_library=False)
    with patch.object(tools_module, "FULL_TEXT_MATERIAL_CHARS", threshold), \
            patch("app.agent.tools.chat_async", new=_chat(tree, seen)):
        result = asyncio.run(registry.execute(
            ToolCall(call_id="c-full", name="mindmap_generate",
                     arguments={"topic": TOPIC, "branches": 3}), ctx))
    if ctx_out is not None:
        ctx_out.append(ctx)
    return result


def _structure_user(seen: list[tuple[str, str]]) -> str:
    return next(user for system, user in seen
                if not system.startswith(PLAN_PROMPT)
                and not system.startswith(DETAIL_ONLY_PROMPT))


def _structure_system(seen: list[tuple[str, str]]) -> str:
    return next(system for system, _ in seen
                if not system.startswith(PLAN_PROMPT)
                and not system.startswith(DETAIL_ONLY_PROMPT))


def _detail_systems(seen: list[tuple[str, str]]) -> list[str]:
    return [system for system, _ in seen if system.startswith(DETAIL_ONLY_PROMPT)]


def _codes(result) -> list[str]:
    return [item["code"] for item in result.data["diagnostics"]]


# ---------------------------------------------------------------- 1. 闸的两侧


def test_material_within_the_threshold_goes_in_whole_and_skips_retrieval():
    """材料装得下 → 全文进 prompt，**一条检索都不发**。"""
    calls: list[str] = []

    async def counting(sources, title, intents=()):
        calls.append(title)
        return []

    seen: list[tuple[str, str]] = []
    with patch.object(tools_module, "_search_branch", counting):
        result = _run(_ctx(), _tree(), seen, threshold=10 ** 6)

    assert result.ok, result.content
    assert calls == [], "全塞时不该按支检索"
    assert result.data["retrieval"]["mode"] == "full"
    assert result.data["retrieval"]["hits"] == 0
    assert result.data["retrieval"]["queries"] == []

    user = _structure_user(seen)
    # 正文整份在 user 里，分节表头带文件名、字数与 source_id（模型写引用只看得到它）
    assert FULL_TEXT_SECTION_HEADER.format(
        index=1, title="机器学习讲义.md", chars=len(MATERIAL.rstrip()),
        source_id="s-full") in user
    assert QUOTE in user
    assert result.data["material"]["full_text"] is True
    assert result.data["material"]["chars"] == len(MATERIAL.rstrip())


def test_material_over_the_threshold_keeps_the_branch_retrieval_path():
    """超过闸 → 完全走回按支检索：查询、分组表头、规则文本都与改动之前一致。"""
    seen: list[tuple[str, str]] = []
    result = _run(_ctx(), _tree(), seen, threshold=len(MATERIAL) - 10)

    assert result.ok, result.content
    assert result.data["retrieval"]["mode"] == "branch"
    assert result.data["retrieval"]["queries"] == list(BRANCHES)
    user = _structure_user(seen)
    assert "第 1 支「监督学习」的检索结果" in user
    assert "full_text" not in (result.data.get("material") or {})
    # 按支那套规则逐字还在，全塞那套一个字都不出现
    assert BRANCHED_EVIDENCE_RULES in _structure_system(seen)
    assert FULL_TEXT_EVIDENCE_RULES not in _structure_system(seen)


def test_threshold_zero_disables_the_whole_path():
    """闸取 0（默认值）= 关掉全塞：材料一样走检索，行为与加它之前相同。"""
    seen: list[tuple[str, str]] = []
    result = _run(_ctx(), _tree(), seen, threshold=0)

    assert result.ok, result.content
    assert result.data["retrieval"]["mode"] == "branch"
    assert "full_text" not in (result.data.get("material") or {})


# ------------------------------------------------------- 2. 短材料那条路不变


def test_short_material_still_degrades_before_the_full_text_gate():
    """短材料先被换掉，即使把闸调到极大也不进全塞分支。

    这条盯的是顺序：`_intake` 里"太短当提示"必须排在"装得下就全塞"之前。反过来的
    后果是 200 字的材料整份进 prompt、`SHORT_MATERIAL_CHARS` 那条诊断永远不触发——
    而那个阈值的意义正是"这个长度切不出可区分的检索单元"。
    """
    seen: list[tuple[str, str]] = []
    result = _run(_ctx(TINY_MATERIAL), _tree(), seen, threshold=10 ** 6)

    assert result.ok, result.content
    assert "MATERIAL_TOO_SHORT" in _codes(result)
    assert result.data["material"] == {
        "chars": len(TINY_MATERIAL), "sources": 1,
        "threshold": SHORT_MATERIAL_CHARS, "degraded": True,
    }
    assert SHORT_MATERIAL_CHARS == LEAF_MAX_CHARS == 300
    # 短材料进的是"主题补充说明"，不是全文分节
    assert "份材料「" not in _structure_user(seen)


# ---------------------------------------------------- 3. 提示词与引用的落点


def test_full_text_prompt_says_whole_document_and_the_detail_round_follows():
    """全塞的 system 说的是"整份正文"，第三段那句引导也换成全文版。"""
    seen: list[tuple[str, str]] = []
    result = _run(_ctx(), _tree(), seen, threshold=10 ** 6)

    assert result.ok, result.content
    system = _structure_system(seen)
    assert FULL_TEXT_EVIDENCE_RULES in system
    # 按支那套的两句不能再出现：它们说"已经按一级分支各检索过一次"
    assert BRANCHED_EVIDENCE_RULES not in system
    for system_text in _detail_systems(seen):
        assert DETAIL_ONLY_FULL_TEXT_RULES in system_text
        assert DETAIL_ONLY_EVIDENCE_RULES not in system_text


def test_the_citation_instruction_comes_after_the_material():
    """引用要求必须在正文**之后**再说一遍。

    只写在 system 里的实测结果是"一条引用都不写"（`scripts/measure_incontext.py`
    1 万字档：declared=0，而同一次响应里它把材料中间那个备案号抄进了 detail）。
    这条用例把那个修复钉在位置上，而不是钉在"两处都有这句话"上。
    """
    seen: list[tuple[str, str]] = []
    result = _run(_ctx(), _tree(), seen, threshold=10 ** 6)

    assert result.ok, result.content
    user = _structure_user(seen)
    material_at = user.index(QUOTE)
    instruction = user.rindex("source_refs")
    assert instruction > material_at
    assert "source_id" in user[instruction - 200:instruction + 200]


def test_quotes_from_the_full_text_survive_verification_and_get_offsets():
    """全塞时只写 quote 的引用照旧通过逐字校验，且服务端补上了偏移。

    这是"全塞不会让 `verify_source_refs` 失效"的可执行证明：它查的是"这句在不在
    `Source.text` 里"，与这份正文有没有进 prompt 无关。
    """
    seen: list[tuple[str, str]] = []
    box: list[RunContext] = []
    tree = _tree(refs=[{"source_id": "s-full", "quote": QUOTE}])
    result = _run(_ctx(), tree, seen, threshold=10 ** 6, ctx_out=box)

    assert result.ok, result.content
    assert result.data["source_refs"]["declared"] == 3
    assert result.data["source_refs"]["kept"] == 3
    saved = [ref for node in _walk(box[0].candidate.root.model_dump())
             for ref in node.get("source_refs") or []]
    assert len(saved) == 3
    for ref in saved:
        assert ref["quote"] == QUOTE
        # 偏移是服务端在全文里定位出来的（模型没写 start/end）
        assert ref["start"] == MATERIAL.index(QUOTE)
        assert ref["end"] == MATERIAL.index(QUOTE) + len(QUOTE)


def test_zero_refs_from_the_full_text_is_reported_with_its_own_code():
    """全文给了却零引用 → `MATERIAL_UNUSED`（与按支那档的 `EVIDENCE_UNUSED` 分开）。"""
    seen: list[tuple[str, str]] = []
    result = _run(_ctx(), _tree(), seen, threshold=10 ** 6)

    assert result.ok, result.content
    assert "MATERIAL_UNUSED" in _codes(result)
    assert "EVIDENCE_UNUSED" not in _codes(result)


def test_the_shipped_threshold_is_fifty_thousand_chars(monkeypatch):
    """默认值是 5 万字——这条钉的是**产品口径**，不是某个用例的方便。

    `tests/conftest.py` 里那条 autouse fixture 把它置 0（让既有用例继续走按支检索），
    所以"线上默认多少"必须单独钉一次：否则有人把它改回 0 之后全仓测试照样全绿，
    而线上那条路整个消失。依据见 `tools.FULL_TEXT_MATERIAL_CHARS` 的注释。
    """
    monkeypatch.undo()      # 撤掉 autouse fixture 的置关，看到模块里那个真值
    assert tools_module.FULL_TEXT_MATERIAL_CHARS == 50_000
