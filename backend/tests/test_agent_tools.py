"""四个业务工具的行为测试。

生成与展开都要调模型，这里全部用 mock 替换掉 `chat_async`：工具里值得测的是
「拿到模型结果之后怎么落到候选树上」，以及各种前置条件与失败路径，调真模型
只会把不确定性引进来。

注册表按意图裁剪（`build_registry(intent)`），所以每个用例都要显式说明自己在
哪个意图下跑——创建时拿不到 `mindmap_edit`，正是这些测试要守住的行为之一。
"""
from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from app.agent.contracts import AgentRunRequest, RunContext, ToolCall
from app.agent.tools import MAX_SOURCE_CHARS, SOURCE_TRUNCATED_HINT, build_registry
from app.contracts.mindmap import MindMapContent
from app.contracts.source import Source, SourceKind
from app.generation.service import DETAIL_PROMPT, SOURCED_DETAIL_PROMPT

TREE = {
    "title": "示例",
    "root": {
        "id": "root", "label": "根", "children": [
            {"id": "a", "label": "甲", "children": [
                {"id": "a1", "label": "甲一", "children": []},
            ]},
            {"id": "b", "label": "乙", "children": []},
        ],
    },
}


def _content() -> MindMapContent:
    return MindMapContent.model_validate(TREE)


def _ctx(intent: str = "create", candidate: MindMapContent | None = None,
         sources: list[Source] | None = None,
         selected_ids: list[str] | None = None) -> RunContext:
    sources = sources or []
    request = AgentRunRequest(
        goal="做一张导图", intent=intent, sources=sources,
        selected_ids=selected_ids or [],
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
        self.assertEqual(result.data["node_count"], 4)

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
        self.assertEqual(system, SOURCED_DETAIL_PROMPT)
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
        self.assertEqual(system, DETAIL_PROMPT)
        self.assertEqual(user, "机器学习")

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
        self.assertEqual([child.id for child in ctx.candidate.root.children], ["b"])

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


class RegistryTest(unittest.TestCase):
    def test_create_intent_only_offers_generate(self):
        self.assertEqual(build_registry("create").names(), ["mindmap_generate"])

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
