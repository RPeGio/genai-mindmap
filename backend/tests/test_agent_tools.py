"""五个业务工具的行为测试。

生成与展开都要调模型，这里全部用 mock 替换掉 `chat_async`：工具里值得测的是
「拿到模型结果之后怎么落到候选树上」，以及各种前置条件与失败路径，调真模型
只会把不确定性引进来。

注册表按意图裁剪（`build_registry(intent)`），所以每个用例都要显式说明自己在
哪个意图下跑——创建时拿不到 `mindmap_edit`，正是这些测试要守住的行为之一。

分片生成（`mindmap_generate_deep`）的用例额外盯住三件事：分支调用是并发的、
并发被限在 3 个以内、要点不足时只补那一支而不是整张重来。这三条一旦退化，
单次调用会被打回「一次出一整棵树」，而那正是这条路径要解决的问题。
"""
from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
from app.agent.provider import _MAX_TOKENS_CAP
from app.agent.tools import (
    BRANCH_EXPAND_PROMPT,
    MAX_OUTPUT_TOKENS_CAP,
    MAX_SOURCE_CHARS,
    PLAN_PROMPT,
    SOURCE_TRUNCATED_HINT,
    build_registry,
)
from app.contracts.mindmap import MindMapContent
from app.contracts.source import Source, SourceKind
from app.generation.service import (
    DETAIL_PROMPT,
    SOURCED_DETAIL_PROMPT,
    SOURCED_THINKING_GUARD,
    THINKING_MODELS,
    GenerationError,
)

# 一级分支必须是 3 条：`mindmap_generate` 走 `validate_generated_content` 的默认
# 路径，`MIN_BRANCHES = 3` 会拦下更少的树。这里的 2 分支结构会直接变成一次
# invalid_map，测的就不再是"生成成功之后怎么落候选树"了。
TREE = {
    "title": "示例",
    "root": {
        "id": "root", "label": "根", "children": [
            {"id": "a", "label": "甲", "children": [
                {"id": "a1", "label": "甲一", "children": []},
            ]},
            {"id": "b", "label": "乙", "children": []},
            {"id": "c", "label": "丙", "children": []},
        ],
    },
}


def _content() -> MindMapContent:
    return MindMapContent.model_validate(TREE)


def _ctx(intent: str = "create", candidate: MindMapContent | None = None,
         sources: list[Source] | None = None,
         selected_ids: list[str] | None = None,
         thinking_model: str = "default",
         max_model_calls: int = 5,
         max_output_tokens: int = 32768) -> RunContext:
    sources = sources or []
    request = AgentRunRequest(
        goal="做一张导图", intent=intent, sources=sources,
        selected_ids=selected_ids or [], thinking_model=thinking_model,
        budget=RunBudget(
            max_model_calls=max_model_calls, max_output_tokens=max_output_tokens,
        ),
    )
    return RunContext(
        request=request,
        candidate=candidate,
        sources_by_id={source.id: source for source in sources},
    )


def _call(name: str, **arguments) -> ToolCall:
    return ToolCall(call_id=f"c-{name}", name=name, arguments=arguments)


def _run(coro):
    return asyncio.run(coro)


def _prompt_payload(user: str) -> list[dict]:
    """取出 `_source_prompt_input` 里那段 JSON 数组。"""
    return json.loads(user.split("\n", 1)[1])


def _plan(*titles: str) -> dict:
    """骨架响应。"""
    return {"branches": list(titles)}


def _points(count: int, prefix: str = "要点") -> dict:
    """展开响应。"""
    return {"points": [f"{prefix}{index}" for index in range(1, count + 1)]}


def _branch_title(user: str) -> str:
    """从分支展开的 user 消息里取出这次展开的是哪一支。"""
    return user.split("本条一级分支：", 1)[1].split("\n", 1)[0]


class GenerateToolTest(unittest.TestCase):
    def test_rejects_when_intent_is_not_create(self):
        ctx = _ctx(intent="edit")
        result = _run(build_registry("create").execute(_call("mindmap_generate", topic="x"), ctx))
        self.assertFalse(result.ok)
        self.assertEqual(result.data["code"], "wrong_intent")

    def test_rejects_when_map_already_exists(self):
        ctx = _ctx(candidate=_content())
        result = _run(build_registry("create").execute(_call("mindmap_generate", topic="x"), ctx))
        self.assertFalse(result.ok)
        self.assertEqual(result.data["code"], "map_exists")

    def test_success_sets_candidate_and_revision(self):
        ctx = _ctx()
        with patch("app.agent.tools.chat_async", new=AsyncMock(return_value=TREE)):
            result = _run(build_registry("create").execute(_call("mindmap_generate", topic="机器学习"), ctx))
        self.assertTrue(result.ok, result.content)
        self.assertIsNotNone(ctx.candidate)
        self.assertEqual(ctx.candidate.root.id, "root")
        self.assertEqual(ctx.revision, 1)
        self.assertEqual(result.data["node_count"], 5)

    def test_invalid_model_output_is_reported_not_raised(self):
        ctx = _ctx()
        broken = {"title": "示例", "root": {"id": "root", "label": "根", "children": [
            {"id": "root", "label": "重复 id", "children": []},
        ]}}
        with patch("app.agent.tools.chat_async", new=AsyncMock(return_value=broken)):
            result = _run(build_registry("create").execute(_call("mindmap_generate", topic="x"), ctx))
        self.assertFalse(result.ok)
        self.assertEqual(result.data["code"], "invalid_map")
        self.assertIsNone(ctx.candidate)

    def test_sourced_input_carries_every_source_id_and_text(self):
        """有来源时喂给模型的必须是带 source_id 的结构化数组。

        `SOURCED_DETAIL_PROMPT` 要求模型按 source_id 写 `source_refs`。早先这里
        传的是 `_understand()` 的纯文本合并，一个 id 都没有——模型只能凭空编
        引用，引用功能等于从来没通过。
        """
        first = Source(kind=SourceKind.text, status="parsed", text="第一段原始材料。")
        second = Source(kind=SourceKind.text, status="parsed", text="第二段原始材料。")
        ctx = _ctx(sources=[first, second])
        mocked = AsyncMock(return_value=TREE)
        with patch("app.agent.tools.chat_async", new=mocked):
            result = _run(build_registry("create").execute(
                _call("mindmap_generate", topic="整理这些材料"), ctx))

        self.assertTrue(result.ok, result.content)
        system, user = mocked.await_args.args
        # 思维模型块拼在基础提示词之后，所以只断言前缀：整串还带着「思考方式」那一段
        self.assertTrue(system.startswith(SOURCED_DETAIL_PROMPT))
        self.assertIn(first.id, user)
        self.assertIn(second.id, user)
        self.assertEqual(
            _prompt_payload(user),
            [
                {"source_id": first.id, "text": "第一段原始材料。"},
                {"source_id": second.id, "text": "第二段原始材料。"},
            ],
        )

    def test_oversized_source_is_truncated_with_hint(self):
        """单个来源过长时截断并注明，否则模型会为"看不到的结尾"写引用。"""
        long_text = "字" * (MAX_SOURCE_CHARS + 50)
        source = Source(kind=SourceKind.text, status="parsed", text=long_text)
        ctx = _ctx(sources=[source])
        mocked = AsyncMock(return_value=TREE)
        with patch("app.agent.tools.chat_async", new=mocked):
            result = _run(build_registry("create").execute(
                _call("mindmap_generate", topic="整理"), ctx))

        self.assertTrue(result.ok, result.content)
        _, user = mocked.await_args.args
        text = _prompt_payload(user)[0]["text"]
        self.assertEqual(text, long_text[:MAX_SOURCE_CHARS] + SOURCE_TRUNCATED_HINT)
        self.assertLess(len(text), len(long_text))

    def test_source_without_text_is_ignored_instead_of_breaking_generation(self):
        """空来源不该拖垮整次生成：它只是没正文，不是模型出错。"""
        blank = Source(kind=SourceKind.text, status="pending", text=None)
        usable = Source(kind=SourceKind.text, status="parsed", text="有正文的一段材料。")
        ctx = _ctx(sources=[blank, usable])
        mocked = AsyncMock(return_value=TREE)
        with patch("app.agent.tools.chat_async", new=mocked):
            result = _run(build_registry("create").execute(
                _call("mindmap_generate", topic="整理"), ctx))

        self.assertTrue(result.ok, result.content)
        _, user = mocked.await_args.args
        self.assertEqual([item["source_id"] for item in _prompt_payload(user)], [usable.id])

    def test_only_blank_sources_falls_back_to_topic_prompt(self):
        ctx = _ctx(sources=[Source(kind=SourceKind.text, status="failed", text="   ")])
        mocked = AsyncMock(return_value=TREE)
        with patch("app.agent.tools.chat_async", new=mocked):
            result = _run(build_registry("create").execute(
                _call("mindmap_generate", topic="机器学习"), ctx))

        self.assertTrue(result.ok, result.content)
        system, user = mocked.await_args.args
        self.assertTrue(system.startswith(DETAIL_PROMPT))
        self.assertEqual(user, "机器学习")

    def test_thinking_model_feynman_reaches_the_prompt(self):
        """选中的思维模型必须真的进到提示词里，否则下拉框只是个装饰。

        断言用整段 prompt 而不是「费曼」三个字：表里 `label` 才是给人看的中文名，
        `prompt` 才是给模型看的思考步骤——只有后者出现在提示词里才算生效。
        """
        ctx = _ctx(thinking_model="feynman")
        mocked = AsyncMock(return_value=TREE)
        with patch("app.agent.tools.chat_async", new=mocked):
            result = _run(build_registry("create").execute(
                _call("mindmap_generate", topic="机器学习"), ctx))

        self.assertTrue(result.ok, result.content)
        system, _ = mocked.await_args.args
        self.assertIn(THINKING_MODELS["feynman"]["prompt"], system)
        self.assertIn("思考方式", system)
        # 别的模型不该混进来
        self.assertNotIn(THINKING_MODELS["critical"]["prompt"], system)

    def test_thinking_model_defaults_to_general_when_omitted(self):
        """缺省走 default：不选的时候也该有拆解步骤，而不是空一块。"""
        ctx = _ctx()
        self.assertEqual(ctx.request.thinking_model, "default")
        mocked = AsyncMock(return_value=TREE)
        with patch("app.agent.tools.chat_async", new=mocked):
            result = _run(build_registry("create").execute(
                _call("mindmap_generate", topic="机器学习"), ctx))

        self.assertTrue(result.ok, result.content)
        system, _ = mocked.await_args.args
        self.assertIn(THINKING_MODELS["default"]["prompt"], system)

    def test_sourced_path_keeps_material_bounded(self):
        """有来源时思维模型只改「整理角度」：限定句必须一起拼进去。

        少了这句，费曼模型的「必须举反例」会被当成展开许可，模型开始补写
        材料里根本没有的内容——那正是 `SOURCED_DETAIL_PROMPT` 要防的事。
        """
        ctx = _ctx(sources=[Source(kind=SourceKind.text, status="parsed", text="原文。")],
                   thinking_model="feynman")
        mocked = AsyncMock(return_value=TREE)
        with patch("app.agent.tools.chat_async", new=mocked):
            result = _run(build_registry("create").execute(
                _call("mindmap_generate", topic="整理材料"), ctx))

        self.assertTrue(result.ok, result.content)
        system, _ = mocked.await_args.args
        self.assertIn(THINKING_MODELS["feynman"]["prompt"], system)
        self.assertIn(SOURCED_THINKING_GUARD, system)
        # 限定句在思维步骤之后：读起来是"按这个方式整理，但只限材料范围内"
        self.assertGreater(system.index(SOURCED_THINKING_GUARD),
                           system.index(THINKING_MODELS["feynman"]["prompt"]))

    def test_model_call_budget_blocks_before_calling_out(self):
        """额度用完后工具不再调用模型：省下的是真金白银。"""
        ctx = _ctx()
        ctx.model_calls_used = ctx.request.budget.max_model_calls
        mocked = AsyncMock(return_value=TREE)
        with patch("app.agent.tools.chat_async", new=mocked):
            result = _run(build_registry("create").execute(
                _call("mindmap_generate", topic="x"), ctx))
        self.assertFalse(result.ok)
        self.assertEqual(result.data["code"], "budget_exhausted")
        mocked.assert_not_called()


class GenerateDeepToolTest(unittest.TestCase):
    """分片生成：骨架 → 并发展开 → 确定性验收 → 局部重试。

    分片的价值全在「不合格只补那一支」上，所以这里除了正常路径，重点全在并发、
    限流、局部重试与预算不足这四处——它们一旦退化，整条路径就退回「一次出整棵树」。
    """

    def test_happy_path_builds_branches_with_points(self):
        """骨架 5 条 + 每支 3 个要点 → 1 个根、5 条一级分支、每支 3 个要点。"""
        ctx = _ctx(max_model_calls=10)
        titles = ["范围", "原理", "实现", "评估", "风险"]
        systems: list[str] = []

        async def fake(system, user, *, max_tokens=None):
            systems.append(system)
            if system.startswith(PLAN_PROMPT):
                return _plan(*titles)
            return _points(3, _branch_title(user))

        with patch("app.agent.tools.chat_async", new=AsyncMock(side_effect=fake)):
            result = _run(build_registry("create").execute(_call(
                "mindmap_generate_deep", topic="机器学习", branches=5,
                points_per_branch=3), ctx))

        self.assertTrue(result.ok, result.content)
        root = ctx.candidate.root
        self.assertEqual([branch.label for branch in root.children], titles)
        for branch in root.children:
            self.assertEqual([child.label for child in branch.children],
                             [f"{branch.label}1", f"{branch.label}2", f"{branch.label}3"])
        # 1 个根 + 5 条分支 + 15 个要点
        self.assertEqual(result.data["node_count"], 21)
        self.assertEqual(result.data["expanded_branches"], 5)
        self.assertFalse(result.data["partial"])
        self.assertEqual(ctx.revision, 1)
        # 骨架与分支用的是两套提示词：整段复用 EXPAND_PROMPT 会把"不要重复已有
        # 子节点"这种上下文带进一条还没有子节点的分支里。
        self.assertTrue(systems[0].startswith(PLAN_PROMPT))
        self.assertTrue(all(item.startswith(BRANCH_EXPAND_PROMPT) for item in systems[1:]))

    def test_branch_expansions_overlap(self):
        """并发确实发生：串行实现也能把调用次数跑对，只有重叠数能证伪它。

        每个分支都卡在「至少两个分支已经在跑」这个条件上；串行实现等不到它，
        会超时，peak 停在 1。
        """
        ctx = _ctx()
        state = {"running": 0, "peak": 0}
        release = asyncio.Event()

        async def fake(system, user, *, max_tokens=None):
            if system.startswith(PLAN_PROMPT):
                return _plan("甲", "乙", "丙")
            state["running"] += 1
            state["peak"] = max(state["peak"], state["running"])
            try:
                if state["running"] >= 2:
                    release.set()
                await asyncio.wait_for(release.wait(), timeout=1)
            finally:
                state["running"] -= 1
            return _points(2, _branch_title(user))

        with patch("app.agent.tools.chat_async", new=AsyncMock(side_effect=fake)):
            result = _run(build_registry("create").execute(_call(
                "mindmap_generate_deep", topic="机器学习", branches=3,
                points_per_branch=2), ctx))

        self.assertTrue(result.ok, result.content)
        self.assertGreaterEqual(state["peak"], 2)

    def test_concurrent_calls_are_capped_at_three(self):
        """同时进行的展开调用不超过 3 个：一次打 8 个请求容易被服务端限流。"""
        ctx = _ctx(max_model_calls=9)
        state = {"running": 0, "peak": 0}

        async def fake(system, user, *, max_tokens=None):
            if system.startswith(PLAN_PROMPT):
                return _plan(*[f"分支{index}" for index in range(1, 9)])
            state["running"] += 1
            state["peak"] = max(state["peak"], state["running"])
            await asyncio.sleep(0.02)  # 留出重叠窗口
            state["running"] -= 1
            return _points(2, _branch_title(user))

        with patch("app.agent.tools.chat_async", new=AsyncMock(side_effect=fake)):
            result = _run(build_registry("create").execute(_call(
                "mindmap_generate_deep", topic="机器学习", branches=8,
                points_per_branch=2), ctx))

        self.assertTrue(result.ok, result.content)
        self.assertLessEqual(state["peak"], 3)
        self.assertGreaterEqual(state["peak"], 2)

    def test_skeleton_with_two_branches_is_rejected(self):
        """低于下限直接失败：两条分支不成一张图，继续展开只是白烧额度。"""
        ctx = _ctx()
        mocked = AsyncMock(return_value=_plan("甲", "乙"))
        with patch("app.agent.tools.chat_async", new=mocked):
            result = _run(build_registry("create").execute(_call(
                "mindmap_generate_deep", topic="x", branches=3), ctx))

        self.assertFalse(result.ok)
        self.assertEqual(result.data["code"], "too_few_branches")
        self.assertIsNone(ctx.candidate)
        self.assertEqual(mocked.await_count, 1)  # 骨架不合格就停手

    def test_skeleton_off_by_two_still_builds(self):
        """骨架条数超出 ±1 的宽容范围也用实际返回：为条数整张重来等于再花一轮。"""
        ctx = _ctx(max_model_calls=10)

        async def fake(system, user, *, max_tokens=None):
            if system.startswith(PLAN_PROMPT):
                return _plan("甲", "乙", "丙", "丁", "戊", "己", "庚")
            return _points(2, _branch_title(user))

        with patch("app.agent.tools.chat_async", new=AsyncMock(side_effect=fake)):
            result = _run(build_registry("create").execute(_call(
                "mindmap_generate_deep", topic="x", branches=4,
                points_per_branch=2), ctx))

        self.assertTrue(result.ok, result.content)
        self.assertEqual(len(ctx.candidate.root.children), 7)
        codes = [item["code"] for item in result.data["diagnostics"]]
        self.assertIn("SKELETON_SIZE_OFF", codes)

    def test_one_branch_failure_keeps_the_others(self):
        """某一支抛异常只该让那一支空着，其他分支照常建树。"""
        ctx = _ctx()

        async def fake(system, user, *, max_tokens=None):
            if system.startswith(PLAN_PROMPT):
                return _plan("甲", "乙", "丙")
            title = _branch_title(user)
            if title == "乙":
                raise GenerationError("模型服务返回 HTTP 429")
            return _points(2, title)

        with patch("app.agent.tools.chat_async", new=AsyncMock(side_effect=fake)):
            result = _run(build_registry("create").execute(_call(
                "mindmap_generate_deep", topic="x", branches=3,
                points_per_branch=2), ctx))

        self.assertTrue(result.ok, result.content)
        self.assertEqual([len(branch.children) for branch in ctx.candidate.root.children],
                         [2, 0, 2])
        codes = [item["code"] for item in result.data["diagnostics"]]
        self.assertIn("BRANCH_FAILED", codes)
        self.assertTrue(result.data["partial"])

    def test_sparse_branch_is_retried_without_redoing_skeleton(self):
        """要点不足只补那一支：骨架调用必须只发生一次，否则就是整张重来。"""
        ctx = _ctx(max_model_calls=8)
        calls = {"plan": 0, "乙": 0}

        async def fake(system, user, *, max_tokens=None):
            if system.startswith(PLAN_PROMPT):
                calls["plan"] += 1
                return _plan("甲", "乙", "丙")
            title = _branch_title(user)
            if title != "乙":
                return _points(3, title)
            calls["乙"] += 1
            if calls["乙"] == 1:
                return {"points": ["乙要点一"]}  # 第一次只给了一个
            return {"points": ["乙要点二", "乙要点三"]}

        with patch("app.agent.tools.chat_async", new=AsyncMock(side_effect=fake)):
            result = _run(build_registry("create").execute(_call(
                "mindmap_generate_deep", topic="x", branches=3,
                points_per_branch=3), ctx))

        self.assertTrue(result.ok, result.content)
        self.assertEqual(calls["plan"], 1)
        self.assertEqual(calls["乙"], 2)
        branch = next(item for item in ctx.candidate.root.children if item.label == "乙")
        self.assertEqual([child.label for child in branch.children],
                         ["乙要点一", "乙要点二", "乙要点三"])
        self.assertEqual([item["code"] for item in result.data["diagnostics"]], [])

    def test_budget_shortfall_returns_partial_tree(self):
        """额度只够骨架 + 一支：给出已展开的部分，并显式标 partial。"""
        ctx = _ctx(max_model_calls=2)

        async def fake(system, user, *, max_tokens=None):
            if system.startswith(PLAN_PROMPT):
                return _plan("甲", "乙", "丙")
            return _points(2, _branch_title(user))

        with patch("app.agent.tools.chat_async", new=AsyncMock(side_effect=fake)):
            result = _run(build_registry("create").execute(_call(
                "mindmap_generate_deep", topic="x", branches=3,
                points_per_branch=2), ctx))

        self.assertTrue(result.ok, result.content)
        self.assertTrue(result.data["partial"])
        self.assertEqual(result.data["planned_branches"], 3)
        self.assertEqual(result.data["expanded_branches"], 1)
        self.assertEqual(len(ctx.candidate.root.children), 3)
        self.assertIn("额度不足", result.content)
        self.assertIn("1/3", result.content)
        # 没展开的分支只报「额度不足」，不该同时再报一条「要点数不足」
        codes = [item["code"] for item in result.data["diagnostics"]]
        self.assertEqual(codes.count("BRANCH_SKIPPED_BUDGET"), 2)
        self.assertNotIn("SPARSE_BRANCH", codes)

    def test_budget_exhausted_before_skeleton(self):
        ctx = _ctx(max_model_calls=1)
        ctx.model_calls_used = 1
        mocked = AsyncMock(return_value=_plan("甲", "乙", "丙"))
        with patch("app.agent.tools.chat_async", new=mocked):
            result = _run(build_registry("create").execute(_call(
                "mindmap_generate_deep", topic="x"), ctx))

        self.assertFalse(result.ok)
        self.assertEqual(result.data["code"], "budget_exhausted")
        self.assertIsNone(ctx.candidate)
        mocked.assert_not_called()

    def test_all_branches_failing_is_reported(self):
        ctx = _ctx()

        async def fake(system, user, *, max_tokens=None):
            if system.startswith(PLAN_PROMPT):
                return _plan("甲", "乙", "丙")
            raise GenerationError("模型服务返回 HTTP 500")

        with patch("app.agent.tools.chat_async", new=AsyncMock(side_effect=fake)):
            result = _run(build_registry("create").execute(_call(
                "mindmap_generate_deep", topic="x", branches=3), ctx))

        self.assertFalse(result.ok)
        self.assertEqual(result.data["code"], "all_branches_failed")
        self.assertIsNone(ctx.candidate)
        self.assertEqual(ctx.revision, 0)

    def test_every_internal_model_call_carries_the_output_token_cap(self):
        """骨架与每支展开都要带 `max_tokens`，且等于 `min(budget, 16384)`。

        不传的时候 `chat_async` 会用它自己的默认值，`RunBudget.max_output_tokens`
        就只约束了 provider 那条路——工具内这条更大的出口完全没人守。
        """
        ctx = _ctx(max_model_calls=10)
        seen: list[int | None] = []

        async def fake(system, user, *, max_tokens=None):
            seen.append(max_tokens)
            if system.startswith(PLAN_PROMPT):
                return _plan(*[f"分支{index}" for index in range(1, 7)])
            return _points(3, _branch_title(user))

        with patch("app.agent.tools.chat_async", new=AsyncMock(side_effect=fake)):
            result = _run(build_registry("create").execute(_call(
                "mindmap_generate_deep", topic="机器学习", branches=6,
                points_per_branch=3), ctx))

        self.assertTrue(result.ok, result.content)
        expected = min(ctx.request.budget.max_output_tokens, 16384)
        self.assertEqual(len(seen), 7)  # 1 次骨架 + 6 支展开
        self.assertEqual(seen, [expected] * 7)
        # 16384 必须与 provider 的上限同值，否则两条出口各守一个数
        self.assertEqual(MAX_OUTPUT_TOKENS_CAP, _MAX_TOKENS_CAP)

    def test_output_cap_follows_the_smaller_budget_value(self):
        """预算配得比上限更小时，发出去的必须是预算值，不能被上限抬高。"""
        ctx = _ctx(max_model_calls=10, max_output_tokens=2048)
        seen: list[int | None] = []

        async def fake(system, user, *, max_tokens=None):
            seen.append(max_tokens)
            if system.startswith(PLAN_PROMPT):
                return _plan("甲", "乙", "丙")
            return _points(2, _branch_title(user))

        with patch("app.agent.tools.chat_async", new=AsyncMock(side_effect=fake)):
            result = _run(build_registry("create").execute(_call(
                "mindmap_generate_deep", topic="机器学习", branches=3,
                points_per_branch=2), ctx))

        self.assertTrue(result.ok, result.content)
        self.assertEqual(seen, [2048] * 4)  # 1 骨架 + 3 展开

    def test_branch_still_short_after_retry_marks_partial(self):
        """补点后仍不足必须报 partial，并在 content 里点名哪一支、差几个。

        实测缺陷：某支最终 0 个要点，`partial=false`、`expanded_branches=3`——
        一次"完整成功"。少要点是质量波动可以接受，把它说成完整成功不行。
        """
        ctx = _ctx(max_model_calls=10)
        calls = {"乙": 0}

        async def fake(system, user, *, max_tokens=None):
            if system.startswith(PLAN_PROMPT):
                return _plan("甲", "乙", "丙")
            title = _branch_title(user)
            if title != "乙":
                return _points(3, title)
            calls["乙"] += 1
            if calls["乙"] == 1:
                return {"points": ["乙要点一"]}
            return {"points": []}  # 补点也没补出来

        with patch("app.agent.tools.chat_async", new=AsyncMock(side_effect=fake)):
            result = _run(build_registry("create").execute(_call(
                "mindmap_generate_deep", topic="x", branches=3,
                points_per_branch=3), ctx))

        self.assertTrue(result.ok, result.content)
        self.assertEqual(calls["乙"], 2)  # 首轮 + 一次补点
        self.assertTrue(result.data["partial"])
        self.assertEqual(
            [(item["label"], item["missing"]) for item in result.data["short_branches"]],
            [("乙", 2)],
        )
        self.assertIn("乙", result.content)
        self.assertIn("差 2 个", result.content)
        codes = [item["code"] for item in result.data["diagnostics"]]
        self.assertIn("SPARSE_BRANCH", codes)

    def test_retry_request_carries_the_points_already_written(self):
        """补点请求要带上该分支已有的要点。

        模型看不到自己已经写了什么，却被要求「不要重复」——那条要求它根本
        没法执行，实测同一要点被返回两次。
        """
        ctx = _ctx(max_model_calls=10)
        retry_users: list[str] = []
        calls = {"乙": 0}

        async def fake(system, user, *, max_tokens=None):
            if system.startswith(PLAN_PROMPT):
                return _plan("甲", "乙", "丙")
            title = _branch_title(user)
            if title != "乙":
                return _points(3, title)
            calls["乙"] += 1
            if calls["乙"] == 1:
                return {"points": ["乙要点一"]}
            retry_users.append(user)
            return {"points": ["乙要点二", "乙要点三"]}

        with patch("app.agent.tools.chat_async", new=AsyncMock(side_effect=fake)):
            result = _run(build_registry("create").execute(_call(
                "mindmap_generate_deep", topic="x", branches=3,
                points_per_branch=3), ctx))

        self.assertTrue(result.ok, result.content)
        self.assertEqual(len(retry_users), 1)
        self.assertIn("已有要点", retry_users[0])
        self.assertIn("乙要点一", retry_users[0])
        self.assertFalse(result.data["partial"])

    def test_duplicate_points_are_dropped_and_break_completeness(self):
        """重复要点去重后按实际数量验收，并留一条 DUPLICATE_POINT 诊断。

        不按去重后的数量判的话，「返回同一句话两次」能骗过数量验收：
        提示词要求补齐 2 个，实际树上只多了 0 个，而验收说够了。
        """
        ctx = _ctx(max_model_calls=10)
        calls = {"乙": 0}

        async def fake(system, user, *, max_tokens=None):
            if system.startswith(PLAN_PROMPT):
                return _plan("甲", "乙", "丙")
            title = _branch_title(user)
            if title != "乙":
                return _points(3, title)
            calls["乙"] += 1
            if calls["乙"] == 1:
                return {"points": ["乙要点一"]}
            return {"points": ["乙要点一", "乙要点一"]}  # 同一要点返回两次

        with patch("app.agent.tools.chat_async", new=AsyncMock(side_effect=fake)):
            result = _run(build_registry("create").execute(_call(
                "mindmap_generate_deep", topic="x", branches=3,
                points_per_branch=3), ctx))

        self.assertTrue(result.ok, result.content)
        branch = next(item for item in ctx.candidate.root.children if item.label == "乙")
        self.assertEqual([child.label for child in branch.children], ["乙要点一"])
        self.assertTrue(result.data["partial"])
        duplicated = [item for item in result.data["diagnostics"]
                      if item["code"] == "DUPLICATE_POINT"]
        self.assertEqual(len(duplicated), 1)
        self.assertIn("乙", duplicated[0]["message"])

    def test_plan_request_states_the_requested_branch_count(self):
        """骨架请求的 user 里要有期望分支数：3 支与 8 支的输入必须不同。

        只写在 system 里的话，两次请求模型看到的东西一字不差，`branches`
        这个用户选项等于没生效。
        """
        plan_users: dict[int, str] = {}
        for branches in (3, 8):
            ctx = _ctx(max_model_calls=12)
            captured: list[str] = []

            async def fake(system, user, *, max_tokens=None, _captured=captured):
                if system.startswith(PLAN_PROMPT):
                    _captured.append(user)
                    return _plan(*[f"分支{index}" for index in range(1, branches + 1)])
                return _points(2, _branch_title(user))

            with patch("app.agent.tools.chat_async", new=AsyncMock(side_effect=fake)):
                result = _run(build_registry("create").execute(_call(
                    "mindmap_generate_deep", topic="机器学习", branches=branches,
                    points_per_branch=2), ctx))

            self.assertTrue(result.ok, result.content)
            self.assertEqual(len(captured), 1)
            plan_users[branches] = captured[0]

        self.assertIn("3", plan_users[3])
        self.assertIn("8", plan_users[8])
        self.assertNotEqual(plan_users[3], plan_users[8])
        for text in plan_users.values():
            self.assertIn("20 字", text)
            self.assertIn("互不重叠", text)


class ExpandToolTest(unittest.TestCase):
    def test_success_adds_children(self):
        ctx = _ctx(intent="expand", candidate=_content())
        mocked = AsyncMock(return_value={"points": ["要点一", "要点二"]})
        with patch("app.agent.tools.chat_async", new=mocked):
            result = _run(build_registry("expand").execute(
                _call("node_expand", node_id="b", count=2), ctx))
        self.assertTrue(result.ok, result.content)
        node = next(child for child in ctx.candidate.root.children if child.id == "b")
        self.assertEqual([child.label for child in node.children], ["要点一", "要点二"])
        self.assertEqual(ctx.revision, 1)
        self.assertEqual(len(ctx.batches), 1)

    def test_unknown_node_is_rejected_without_calling_model(self):
        ctx = _ctx(intent="expand", candidate=_content())
        mocked = AsyncMock()
        with patch("app.agent.tools.chat_async", new=mocked):
            result = _run(build_registry("expand").execute(
                _call("node_expand", node_id="ghost", count=2), ctx))
        self.assertFalse(result.ok)
        self.assertEqual(result.data["code"], "node_not_found")
        mocked.assert_not_called()

    def test_empty_points_is_failure(self):
        ctx = _ctx(intent="expand", candidate=_content())
        with patch("app.agent.tools.chat_async", new=AsyncMock(return_value={"points": []})):
            result = _run(build_registry("expand").execute(
                _call("node_expand", node_id="b", count=2), ctx))
        self.assertFalse(result.ok)
        self.assertEqual(result.data["code"], "invalid_points")

    def test_count_out_of_range_is_rejected_by_schema(self):
        """count 只允许 2-4：参数模型拦下，工具根本不执行。"""
        ctx = _ctx(intent="expand", candidate=_content())
        result = _run(build_registry("expand").execute(
            _call("node_expand", node_id="b", count=9), ctx))
        self.assertFalse(result.ok)
        self.assertEqual(result.data["code"], "invalid_tool_args")

    def test_without_candidate_is_rejected(self):
        ctx = _ctx(intent="expand")
        result = _run(build_registry("expand").execute(_call("node_expand", node_id="b"), ctx))
        self.assertFalse(result.ok)
        self.assertEqual(result.data["code"], "no_candidate")


class RagToolTest(unittest.TestCase):
    def test_returns_hits_from_attached_sources(self):
        source = Source(kind=SourceKind.text, status="parsed",
                        text="会议决定在十月十七日发布二点零版本，请提前准备上线材料。")
        ctx = _ctx(intent="expand", sources=[source])
        result = _run(build_registry("expand", has_sources=True).execute(
            _call("rag_query", question="什么时候发布版本"), ctx))
        self.assertTrue(result.ok, result.content)
        self.assertGreaterEqual(len(result.data["hits"]), 1)
        hit = result.data["hits"][0]
        self.assertEqual(hit["source_id"], source.id)
        self.assertIn("十月十七日", hit["quote"])
        # 回给模型的必须是结构化 JSON，它要拿 source_id 和偏移去写引用
        self.assertIn("source_id", json.loads(result.content)[0])

    def test_without_sources_is_rejected(self):
        result = _run(build_registry("expand", has_sources=True).execute(
            _call("rag_query", question="任意"), _ctx(intent="expand")))
        self.assertFalse(result.ok)
        self.assertEqual(result.data["code"], "no_sources")

    def test_no_hit_is_success_with_empty_list(self):
        """检索不到不是错误：模型据此知道该换问法，而不是以为工具坏了。"""
        source = Source(kind=SourceKind.text, status="parsed", text="完全无关的一段文字。")
        ctx = _ctx(intent="expand", sources=[source])
        result = _run(build_registry("expand", has_sources=True).execute(
            _call("rag_query", question="量子色动力学的渐近自由"), ctx))
        self.assertTrue(result.ok)
        self.assertEqual(result.data["hits"], [])


class EditToolTest(unittest.TestCase):
    def test_update_changes_label(self):
        ctx = _ctx(intent="edit", candidate=_content())
        result = _run(build_registry("edit").execute(_call(
            "mindmap_edit", ops=[{"op": "update", "id": "b", "changes": {"label": "乙改"}}]), ctx))
        self.assertTrue(result.ok, result.content)
        node = next(child for child in ctx.candidate.root.children if child.id == "b")
        self.assertEqual(node.label, "乙改")
        self.assertIn("b", result.data["changed_ids"])

    def test_remove_drops_subtree(self):
        ctx = _ctx(intent="edit", candidate=_content())
        result = _run(build_registry("edit").execute(
            _call("mindmap_edit", ops=[{"op": "remove", "id": "a"}]), ctx))
        self.assertTrue(result.ok, result.content)
        self.assertEqual([child.id for child in ctx.candidate.root.children], ["b", "c"])

    def test_move_into_own_descendant_is_rejected(self):
        ctx = _ctx(intent="edit", candidate=_content())
        result = _run(build_registry("edit").execute(_call(
            "mindmap_edit", ops=[{"op": "move", "id": "a", "new_parent_id": "a1"}]), ctx))
        self.assertFalse(result.ok)
        self.assertEqual(result.data["code"], "cycle_detected")
        self.assertIn("path", result.data)

    def test_add_without_index_appends(self):
        """模型常只给 parent_id 和 label，缺 index 不该被判成参数错误。"""
        ctx = _ctx(intent="edit", candidate=_content())
        result = _run(build_registry("edit").execute(_call(
            "mindmap_edit", ops=[{"op": "add", "id": "$new", "parent_id": "b", "label": "丙"}]), ctx))
        self.assertTrue(result.ok, result.content)
        node = next(child for child in ctx.candidate.root.children if child.id == "b")
        self.assertEqual([child.label for child in node.children], ["丙"])

    def test_empty_ops_is_rejected_by_schema(self):
        ctx = _ctx(intent="edit", candidate=_content())
        result = _run(build_registry("edit").execute(_call("mindmap_edit", ops=[]), ctx))
        self.assertFalse(result.ok)
        self.assertEqual(result.data["code"], "invalid_tool_args")

    def test_without_candidate_is_rejected(self):
        result = _run(build_registry("edit").execute(
            _call("mindmap_edit", ops=[{"op": "remove", "id": "a"}]), _ctx(intent="edit")))
        self.assertFalse(result.ok)
        self.assertEqual(result.data["code"], "no_candidate")

    def test_failure_keeps_candidate_untouched(self):
        """一条 op 失败时候选树必须原样，不能留下改了一半的中间态。"""
        ctx = _ctx(intent="edit", candidate=_content())
        before = ctx.candidate.model_dump()
        result = _run(build_registry("edit").execute(_call("mindmap_edit", ops=[
            {"op": "update", "id": "b", "changes": {"label": "先改"}},
            {"op": "remove", "id": "ghost"},
        ]), ctx))
        self.assertFalse(result.ok)
        self.assertEqual(ctx.candidate.model_dump(), before)
        self.assertEqual(ctx.revision, 0)


class SelectionScopeTest(unittest.TestCase):
    """选中集合划定的可改范围。

    两层防线都要测：注册表按意图裁剪，执行层再按选中子树收窄。只测其中一层，
    另一层配错时就没有任何东西守着"别改用户没选中的分支"。
    """

    def test_expand_selection_rejects_edit_of_other_branch(self):
        ctx = _ctx(intent="expand", candidate=_content(), selected_ids=["a"])
        before = ctx.candidate.model_dump()
        ops = [{"op": "remove", "id": "b"}]

        # 第一层：expand 意图的注册表里压根没有 mindmap_edit
        registry = build_registry("expand")
        self.assertNotIn("mindmap_edit", registry.names())
        blocked = _run(registry.execute(_call("mindmap_edit", ops=ops), ctx))
        self.assertFalse(blocked.ok)
        self.assertEqual(blocked.data["code"], "unknown_tool")

        # 第二层：就算注册表被误配（这里直接喂一个 edit 的 registry），执行层也拦
        executed = _run(build_registry("edit").execute(_call("mindmap_edit", ops=ops), ctx))
        self.assertFalse(executed.ok)
        self.assertEqual(executed.data["code"], "out_of_selection")

        self.assertEqual(ctx.candidate.model_dump(), before)
        self.assertEqual(ctx.revision, 0)

    def test_edit_inside_selection_is_allowed(self):
        """收窄不等于禁用：选中 A 时改 A 的子树必须照常成功。"""
        ctx = _ctx(intent="edit", candidate=_content(), selected_ids=["a"])
        result = _run(build_registry("edit").execute(_call(
            "mindmap_edit", ops=[{"op": "update", "id": "a1", "changes": {"label": "甲一改"}}]), ctx))
        self.assertTrue(result.ok, result.content)
        node = next(child for child in ctx.candidate.root.children if child.id == "a")
        self.assertEqual(node.children[0].label, "甲一改")

    def test_move_target_outside_selection_is_rejected(self):
        """move 的落脚点也在范围里：不能把选中子树挂到未选中的分支下。"""
        ctx = _ctx(intent="edit", candidate=_content(), selected_ids=["a"])
        before = ctx.candidate.model_dump()
        result = _run(build_registry("edit").execute(_call(
            "mindmap_edit", ops=[{"op": "move", "id": "a1", "new_parent_id": "b"}]), ctx))
        self.assertFalse(result.ok)
        self.assertEqual(result.data["code"], "out_of_selection")
        self.assertEqual(ctx.candidate.model_dump(), before)

    def test_add_under_unselected_parent_is_rejected(self):
        """add 看的是 parent_id：新节点的 id 是临时名，比它没有意义。"""
        ctx = _ctx(intent="edit", candidate=_content(), selected_ids=["a"])
        result = _run(build_registry("edit").execute(_call("mindmap_edit", ops=[
            {"op": "add", "id": "$new", "parent_id": "b", "label": "丙"},
        ]), ctx))
        self.assertFalse(result.ok)
        self.assertEqual(result.data["code"], "out_of_selection")
        self.assertEqual(ctx.revision, 0)


class DeepBudgetFloorTest(unittest.TestCase):
    def test_default_budget_covers_a_full_deep_run(self):
        """默认预算要够一次完整的五支分片。

        分片的一次调用量是「1 次骨架 + N 次展开 + 最多 N 次补点」，外层每轮还要先
        占 1 次。默认值给 5 的时候，骨架加三支就用完了，五支必然只展开三支——
        这不是模型不听话，是预算本身不够。
        """
        budget = RunBudget()
        # 分片默认 5 支；常见上界是 1 骨架 + 5 展开 + 2 补点
        self.assertGreaterEqual(budget.max_model_calls, 2 + 5)

    def test_default_budget_covers_eight_expansions(self):
        """默认预算要覆盖「1 外层 + 1 骨架 + 8 展开」= 10 次调用。

        这是实测缺陷的回归：六支分片是 1 + 1 + 6 + 1(收尾轮) = 9 次，默认给 8
        时收尾那一轮必然 `budget_exhausted`——树已经建好了，却连 done 都发不出来。
        """
        self.assertGreaterEqual(RunBudget().max_model_calls, 1 + 1 + 8)

    def test_partial_is_reported_rather_than_faked(self):
        """额度不够时必须如实标注部分完成，不能装成完整成功。"""
        ctx = _ctx(intent="create", max_model_calls=2)
        titles = {"branches": ["一", "二", "三", "四", "五"]}
        with patch("app.agent.tools.chat_async", new=AsyncMock(return_value=titles)):
            result = _run(build_registry("create").execute(
                _call("mindmap_generate_deep", topic="主题", branches=5), ctx))
        self.assertTrue(result.ok)
        self.assertTrue(result.data.get("partial"))
        self.assertIn("额度", result.content)


class RegistryTest(unittest.TestCase):
    def test_create_intent_offers_both_generate_tools(self):
        """create 下两个生成工具同时在场：简单/复杂由模型按描述自选。"""
        self.assertEqual(
            build_registry("create").names(),
            ["mindmap_generate", "mindmap_generate_deep"],
        )

    def test_deep_generate_is_absent_when_sources_are_attached(self):
        """带材料时不给分片生成。

        它的入参里没有任何来源字段，骨架与展开都只看主题：产出的树与材料无关，
        却会以 ok=True 成功返回。看不到这个工具，模型就不会选中它。
        """
        with_sources = build_registry("create", has_sources=True).names()
        self.assertNotIn("mindmap_generate_deep", with_sources)
        self.assertIn("mindmap_generate", with_sources)

        without_sources = build_registry("create", has_sources=False).names()
        self.assertIn("mindmap_generate_deep", without_sources)

    def test_deep_generate_is_absent_for_expand_and_edit(self):
        """分片生成只在 create 里有：expand / edit 下连入口都不该出现。"""
        self.assertNotIn("mindmap_generate_deep", build_registry("expand").names())
        self.assertNotIn("mindmap_generate_deep", build_registry("expand", has_sources=True).names())
        self.assertNotIn("mindmap_generate_deep", build_registry("edit").names())
        self.assertNotIn("mindmap_generate_deep", build_registry("edit", has_sources=True).names())

    def test_expand_intent_never_offers_edit(self):
        """expand 下模型看不到 mindmap_edit，就不会去试"删掉另一个分支"。"""
        names = build_registry("expand").names()
        self.assertEqual(names, ["node_expand"])
        self.assertNotIn("mindmap_edit", names)

    def test_expand_intent_adds_rag_only_when_sources_exist(self):
        self.assertEqual(
            build_registry("expand", has_sources=True).names(),
            ["node_expand", "rag_query"],
        )

    def test_edit_intent_offers_edit_but_not_generate(self):
        names = build_registry("edit", has_sources=True).names()
        self.assertIn("mindmap_edit", names)
        self.assertNotIn("mindmap_generate", names)
        self.assertIn("rag_query", names)

    def test_edit_without_sources_has_no_rag(self):
        """没有来源时的检索必然返回"没找到"，注册它只会诱导模型乱查。"""
        self.assertEqual(build_registry("edit").names(), ["mindmap_edit"])

    def test_unknown_tool_lists_available(self):
        result = _run(build_registry("create").execute(_call("generate_mindmap", topic="x"), _ctx()))
        self.assertFalse(result.ok)
        self.assertEqual(result.data["code"], "unknown_tool")
        self.assertIn("mindmap_generate", result.content)


if __name__ == "__main__":
    unittest.main()
