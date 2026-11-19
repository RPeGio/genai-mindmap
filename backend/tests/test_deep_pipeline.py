"""分片生成的流水线：每一支检索完立刻展开，不等其他支。

上一版是两阶段：骨架 → `_retrieve_branches`（第一支串行预热块向量、其余并发）→
**全部检索完成** → 一次性 `gather` 展开。骨架落定到第一个子节点之间隔着"最慢的
一支检索"，用户看到的就是"节点出一批、卡一下、再出一批"。

这一轮只改**调用节奏**，语义一条不动（限额、证据只喂本支、补查、诊断都一样）。
所以本文件盯七件事：

  1. 流水线真的存在：第 0 支的展开必须在**最后一支的检索完成之前**开始；
  2. 证据仍然只喂本支：每支展开读到的 evidence 数组与最终节点都指向自己的命中；
  3. 限额不变：初次仍是 root 之下 3 层（4 层含根）/ 80 节点；
  4. 失败隔离：某支检索抛错 → 这一支按"无证据"展开，其余照常；
  5. 并发上限：同时"在跑"（检索 + 展开算一份活）的支数不超过 `DEEP_CONCURRENCY`；
  6. 补查仍在流水线里生效（它在名额内跑，不能因为去掉内层锁而死锁）；
  7. 全空兜底没丢，且**屏障只挡住空支**：全空时仍退回整树一次检索；部分空时
     不兜底、那几支按"无证据"展开，有证据的支不受影响。

第 7 条不在任务清单里，但它是这次改动最容易破坏的一条：兜底判定要等全部门落定，
而流水线不允许等。实现把判定放进了 `settle_fallback`（只挡住没拿到证据的支），
所以要有一条用例钉住"兜底仍发生"和"兜底不会在所有支上生效"。
"""
from __future__ import annotations

import asyncio
import json
import re

from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
from app.agent.tools import (
    BRANCH_EVIDENCE_RULES,
    BRANCH_NO_EVIDENCE_RULES,
    DEEP_CONCURRENCY,
    INITIAL_MAX_DEPTH,
    INITIAL_MAX_NODES,
    PLAN_PROMPT,
    build_registry,
)
from app.contracts.source import Source, SourceKind
from app.knowledge.service import EvidenceHit

BRANCHES = 8
TITLES = [f"分支{index}" for index in range(BRANCHES)]

# 每支一段独立文字：检索替身把第 i 支的命中指到第 i 段，于是"证据串了"会直接
# 表现为"节点上的编号串了"——断言的是端到端传播，不是"函数被调用过"。
PARAGRAPHS = [
    f"第{index}段：预算第{index}项由财务经理核准，超过五十万元的支出报总经理审批。"
    for index in range(20)
]
TEXT = "".join(PARAGRAPHS)
SPANS: list[tuple[int, int]] = []
_cursor = 0
for _paragraph in PARAGRAPHS:
    SPANS.append((_cursor, _cursor + len(_paragraph)))
    _cursor += len(_paragraph)


def _sources() -> list[Source]:
    return [Source(id="doc", kind=SourceKind.document, status="parsed", text=TEXT)]


def _ctx(*, max_model_calls: int = 60) -> RunContext:
    sources = _sources()
    request = AgentRunRequest(
        goal="整理管理办法", intent="create", sources=sources,
        budget=RunBudget(max_model_calls=max_model_calls, max_tool_calls=8,
                         max_output_tokens=65536),
    )
    return RunContext(request=request, candidate=None,
                      sources_by_id={source.id: source for source in sources})


def _hit(index: int) -> EvidenceHit:
    start, end = SPANS[index]
    return EvidenceHit(source_id="doc", start=start, end=end,
                       quote=TEXT[start:end], score=1.0)


def _branch_index(user: str, titles: list[str] = TITLES) -> int:
    match = re.search(r"本条一级分支：(.+)", user)
    assert match is not None, user[:200]
    return titles.index(match.group(1).strip())


def _evidence_of(user: str) -> list[dict]:
    marker = "本支检索结果（evidence 数组）：\n"
    assert marker in user, "这一支的 user 消息里没有证据段"
    return json.loads(user.split(marker, 1)[1])


def _points(index: int, count: int = 2) -> dict:
    return {"children": [
        {"label": f"要点{index}-{position}", "expandable": False}
        for position in range(count)
    ]}


def _install(monkeypatch, *, search, chat, whole=None, text_search=None) -> None:
    monkeypatch.delenv("MINDMAP_QUERY_EXPANSION", raising=False)
    monkeypatch.setenv("MINDMAP_SOURCE_MODE", "rag")
    monkeypatch.setattr("app.agent.tools._search_branch", search)
    monkeypatch.setattr("app.agent.tools.chat_async", chat)
    if whole is not None:
        monkeypatch.setattr("app.agent.tools._retrieve_whole", whole)
    if text_search is not None:
        monkeypatch.setattr("app.agent.tools._search_text", text_search)


def _execute(ctx: RunContext, branches: int = BRANCHES):
    registry = build_registry("create_deep", has_sources=True)
    return asyncio.run(registry.execute(
        ToolCall(call_id="c-pipeline", name="mindmap_generate_deep",
                 arguments={"topic": "管理办法", "branches": branches,
                            "points_per_branch": 2}),
        ctx,
    ))


def _codes(result) -> list[str]:
    return [item["code"] for item in result.data["diagnostics"]]


# ---------------------------------------------------------------- 1. 流水线存在


def test_first_branch_expands_before_the_last_search_finishes(monkeypatch):
    """第 0 支的展开必须发生在最后一支的检索完成**之前**。

    最后一支的检索卡在"某支展开已经开始"这个条件上：两阶段实现里没有任何一支
    能先展开，它只能超时（`timed_out` 为真）。这比比时间戳稳——它不依赖调度
    顺序，也不靠 sleep 碰运气。
    """
    expand_started = asyncio.Event()
    state: dict = {"timed_out": False, "order": []}

    async def search(_sources, title, _intents=()):
        index = TITLES.index(title)
        if index == BRANCHES - 1:
            try:
                await asyncio.wait_for(expand_started.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                state["timed_out"] = True
        else:
            await asyncio.sleep(0.01)
        state["order"].append(("search_end", index))
        return [_hit(index)]

    async def chat(system, user, *, max_tokens=None, on_delta=None):
        if system.startswith(PLAN_PROMPT):
            return {"branches": list(TITLES)}
        index = _branch_index(user)
        expand_started.set()
        state["order"].append(("expand_start", index))
        await asyncio.sleep(0.02)
        return _points(index)

    _install(monkeypatch, search=search, chat=chat)
    ctx = _ctx()
    result = _execute(ctx)

    assert result.ok, result.content
    assert not state["timed_out"], "最后一支的检索等不到任何展开：这就是两阶段"
    assert (state["order"].index(("expand_start", 0))
            < state["order"].index(("search_end", BRANCHES - 1)))


# ---------------------------------------------------------------- 2. 证据只喂本支


def test_each_branch_sees_only_its_own_hits(monkeypatch):
    """每支展开读到的 evidence 数组、以及最终树上的节点，都只指向自己的命中。

    替身模型从 user 消息里的 evidence 派生 label，所以"证据串了"会一路传到树上：
    断言 `root.children[i].children[0].label` 比只断言"函数被调过"硬得多。
    """
    seen: dict[int, list[int]] = {}

    async def search(_sources, title, _intents=()):
        return [_hit(TITLES.index(title))]

    async def chat(system, user, *, max_tokens=None, on_delta=None):
        if system.startswith(PLAN_PROMPT):
            return {"branches": list(TITLES)}
        index = _branch_index(user)
        starts = [entry["start"] for entry in _evidence_of(user)]
        seen[index] = starts
        return {"children": [
            {"label": f"要点{start}", "expandable": False} for start in starts
        ]}

    _install(monkeypatch, search=search, chat=chat)
    ctx = _ctx()
    result = _execute(ctx)

    assert result.ok, result.content
    for index in range(BRANCHES):
        # 这一支看到的证据：恰好一条，且是它自己那段
        assert seen[index] == [SPANS[index][0]]
        # 落到树上的节点也还是它自己那段（证据 → prompt → 节点 的整条链没串）
        assert ctx.candidate.root.children[index].children[0].label == (
            f"要点{SPANS[index][0]}")
    assert result.data["retrieval"]["branches_with_hits"] == BRANCHES


# ---------------------------------------------------------------- 3. 限额不变


def test_initial_limits_survive_the_pipeline(monkeypatch):
    """初次限额还是 3 层（root 之下，即 4 层含根）/ 80 节点。

    满额 4 层、80 节点的完整用例在 `test_generation_limits.py`（那边构造了一棵
    刚好顶到上限的树）。这里只钉住"流水线没有顺手改掉这两个数"——改调用节奏
    最容易发生的事故就是有人为了让流水线跑完而放宽额度。
    """
    async def search(_sources, title, _intents=()):
        return [_hit(TITLES.index(title))]

    async def chat(system, user, *, max_tokens=None, on_delta=None):
        if system.startswith(PLAN_PROMPT):
            return {"branches": list(TITLES)}
        return _points(_branch_index(user))

    _install(monkeypatch, search=search, chat=chat)
    ctx = _ctx()
    result = _execute(ctx)

    assert result.ok, result.content
    assert result.data["depth_limit"] == INITIAL_MAX_DEPTH      # 4 层含根
    assert INITIAL_MAX_DEPTH + 1 == 4
    assert result.data["node_limit"] == INITIAL_MAX_NODES       # 80
    assert result.data["node_count"] <= INITIAL_MAX_NODES


# ---------------------------------------------------------------- 4. 失败隔离


def test_search_failure_degrades_only_that_branch(monkeypatch):
    """第 3 支检索抛错 → 它按"无证据"展开，其余支照常带证据。"""
    failed_index = 2
    systems: dict[int, str] = {}

    async def search(_sources, title, _intents=()):
        index = TITLES.index(title)
        if index == failed_index:
            raise RuntimeError("模拟检索服务不可用")
        return [_hit(index)]

    async def chat(system, user, *, max_tokens=None, on_delta=None):
        if system.startswith(PLAN_PROMPT):
            return {"branches": list(TITLES)}
        index = _branch_index(user)
        systems[index] = system
        return _points(index)

    _install(monkeypatch, search=search, chat=chat)
    ctx = _ctx()
    result = _execute(ctx)

    assert result.ok, result.content
    # 这一支仍然被展开（只是没有证据），其余支的提示词里带证据规则
    assert failed_index in systems, "检索失败不该让这一支整支不展开"
    assert BRANCH_NO_EVIDENCE_RULES in systems[failed_index]
    assert BRANCH_EVIDENCE_RULES not in systems[failed_index]
    for index in range(BRANCHES):
        if index != failed_index:
            assert BRANCH_EVIDENCE_RULES in systems[index]
    # 诊断把它记成"检索出错"，而不是"没命中"
    assert "BRANCH_RETRIEVAL_FAILED" in _codes(result)
    assert "BRANCH_NO_EVIDENCE" not in _codes(result)
    assert result.data["retrieval"]["failed_branches"] == [TITLES[failed_index]]
    # 这一支照样写满了要点：展开没被检索失败拖垮
    assert len(ctx.candidate.root.children[failed_index].children) == 2


# ---------------------------------------------------------------- 5. 并发上限


def test_pipeline_concurrency_is_capped(monkeypatch):
    """同时"在跑"的支数不超过 `DEEP_CONCURRENCY`（检索 + 展开算一份活）。

    12 支故意超过上限：名额必须包住"检索 + 展开"整体，而不是只包展开——只包
    展开时会有 12 支同时在做检索，这个计数器立刻破 10。
    """
    branches = 12
    titles = [f"分支{index}" for index in range(branches)]
    active: dict[str, int] = {"now": 0, "peak": 0}

    async def search(_sources, title, _intents=()):
        active["now"] += 1
        active["peak"] = max(active["peak"], active["now"])
        await asyncio.sleep(0.01)
        return [_hit(0)]

    async def chat(system, user, *, max_tokens=None, on_delta=None):
        if system.startswith(PLAN_PROMPT):
            return {"branches": list(titles)}
        await asyncio.sleep(0.01)
        active["now"] -= 1          # 检索 + 展开这一份活跑完了
        return _points(_branch_index(user, titles))

    _install(monkeypatch, search=search, chat=chat)
    ctx = _ctx(max_model_calls=80)
    result = _execute(ctx, branches=branches)

    assert result.ok, result.content
    assert active["peak"] <= DEEP_CONCURRENCY
    assert active["peak"] >= 2, "完全没有重叠，说明这不是并发的流水线"


# ---------------------------------------------------------------- 6. 补查


def test_supplementary_search_still_runs(monkeypatch):
    """补查在流水线里仍然生效：它在名额内跑，去掉内层锁之后不能死锁或失效。

    补查那一轮的 user 消息与首轮**同形**（都是"请给出 N 个子要点"），所以替身
    必须靠证据区分：`_search_text` 补进来的那条命中只出现在第二轮里。
    """
    supplement_index = 1

    async def search(_sources, title, _intents=()):
        return [_hit(TITLES.index(title))]

    async def text_search(_sources, _query):
        return [_hit(5)]           # 补查命中另一段，与首路不同

    async def chat(system, user, *, max_tokens=None, on_delta=None):
        if system.startswith(PLAN_PROMPT):
            return {"branches": list(TITLES)}
        index = _branch_index(user)
        if SPANS[5][0] in [entry["start"] for entry in _evidence_of(user)]:
            return {"children": [{"label": f"补充{index}", "expandable": False}]}
        if index == supplement_index:
            return {"needs_evidence": True,
                    "children": [{"label": "需依据要点", "expandable": False},
                                 {"label": "另一要点", "expandable": False}]}
        return _points(index)

    _install(monkeypatch, search=search, chat=chat, text_search=text_search)
    ctx = _ctx()
    result = _execute(ctx)

    assert result.ok, result.content
    assert result.data["retrieval"]["supplementary"]["used"] >= 1
    assert TITLES[supplement_index] in result.data["retrieval"]["supplementary"]["branches"]
    assert "BRANCH_SUPPLEMENTARY_HITS" in _codes(result)
    # 合并而不是替换：原来两个要点，补出一个，一共三个
    assert len(ctx.candidate.root.children[supplement_index].children) == 3


# ---------------------------------------------------------------- 7. 全空兜底


def test_all_empty_falls_back_to_whole_tree(monkeypatch):
    """所有支都空命中 → 仍退回整树一次检索，且每支都拿到同一批兜底证据。

    这条降级要等全部门落定才成立，是一处**必须存在**的屏障。它只挡住没拿到证据
    的支，所以这里全部支都在等——成本与上一版一致，收益是"部分空"时不必等。
    """
    systems: dict[int, str] = {}

    async def search(_sources, title, _intents=()):
        return []

    async def whole(_sources, _topic, _titles):
        return [_hit(3)]

    async def chat(system, user, *, max_tokens=None, on_delta=None):
        if system.startswith(PLAN_PROMPT):
            return {"branches": list(TITLES)}
        index = _branch_index(user)
        systems[index] = system
        assert _evidence_of(user)         # 兜底证据拼在 user 末尾
        return _points(index)

    _install(monkeypatch, search=search, chat=chat, whole=whole)
    ctx = _ctx()
    result = _execute(ctx)

    assert result.ok, result.content
    retrieval = result.data["retrieval"]
    assert retrieval["mode"] == "whole"
    assert retrieval["branches_with_hits"] == BRANCHES
    assert retrieval["empty_branches"] == []      # 兜底之后不再是"没命中"
    assert "BRANCH_RETRIEVAL_FALLBACK" in _codes(result)
    for index in range(BRANCHES):
        assert BRANCH_EVIDENCE_RULES in systems[index]


def test_partial_empty_does_not_fall_back(monkeypatch):
    """只有一部分支空 → **不**兜底：空的那几支按"无证据"展开，有证据的照常。

    与上一条是一对：把兜底写成"每支自己空了就查一次整树"会在这里破——那会让
    只有 1 支空时也白花一次整树检索，并且把别支的证据喂给它（违反"证据只喂本支"）。
    """
    empty = {0, 4}
    systems: dict[int, str] = {}

    async def search(_sources, title, _intents=()):
        index = TITLES.index(title)
        return [] if index in empty else [_hit(index)]

    async def whole(_sources, _topic, _titles):   # pragma: no cover — 不该被调用
        raise AssertionError("部分支空命中时不该走整树兜底")

    async def chat(system, user, *, max_tokens=None, on_delta=None):
        if system.startswith(PLAN_PROMPT):
            return {"branches": list(TITLES)}
        index = _branch_index(user)
        systems[index] = system
        return _points(index)

    _install(monkeypatch, search=search, chat=chat, whole=whole)
    ctx = _ctx()
    result = _execute(ctx)

    assert result.ok, result.content
    retrieval = result.data["retrieval"]
    assert retrieval["mode"] == "branch"
    assert retrieval["empty_branches"] == [TITLES[index] for index in sorted(empty)]
    assert "BRANCH_NO_EVIDENCE" in _codes(result)
    assert BRANCH_EVIDENCE_RULES not in systems[0]
    assert BRANCH_NO_EVIDENCE_RULES in systems[0]
    assert BRANCH_EVIDENCE_RULES in systems[1]
