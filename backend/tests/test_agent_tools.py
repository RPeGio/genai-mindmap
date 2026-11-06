"""四个业务工具的行为测试。

生成与展开都要调模型，这里全部用 mock 替换掉 `chat_async`：工具里值得测的是
「拿到模型结果之后怎么落到候选树上」，以及各种前置条件与失败路径，调真模型
只会把不确定性引进来。
"""
from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from app.agent.contracts import AgentRunRequest, RunContext, ToolCall
from app.agent.tools import build_registry
from app.contracts.mindmap import MindMapContent
from app.contracts.source import Source, SourceKind

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
         sources: list[Source] | None = None) -> RunContext:
    sources = sources or []
    request = AgentRunRequest(goal="做一张导图", intent=intent, sources=sources)
    return RunContext(
        request=request,
        candidate=candidate,
        sources_by_id={source.id: source for source in sources},
    )


def _call(name: str, **arguments) -> ToolCall:
    return ToolCall(call_id=f"c-{name}", name=name, arguments=arguments)


def _run(coro):
    return asyncio.run(coro)


class GenerateToolTest(unittest.TestCase):
    def test_rejects_when_intent_is_not_create(self):
        ctx = _ctx(intent="edit")
        result = _run(build_registry().execute(_call("mindmap_generate", topic="x"), ctx))
        self.assertFalse(result.ok)
        self.assertEqual(result.data["code"], "wrong_intent")

    def test_rejects_when_map_already_exists(self):
        ctx = _ctx(candidate=_content())
        result = _run(build_registry().execute(_call("mindmap_generate", topic="x"), ctx))
        self.assertFalse(result.ok)
        self.assertEqual(result.data["code"], "map_exists")

    def test_success_sets_candidate_and_revision(self):
        ctx = _ctx()
        with patch("app.agent.tools.chat_async", new=AsyncMock(return_value=TREE)):
            result = _run(build_registry().execute(_call("mindmap_generate", topic="机器学习"), ctx))
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
            result = _run(build_registry().execute(_call("mindmap_generate", topic="x"), ctx))
        self.assertFalse(result.ok)
        self.assertEqual(result.data["code"], "invalid_map")
        self.assertIsNone(ctx.candidate)


class ExpandToolTest(unittest.TestCase):
    def test_success_adds_children(self):
        ctx = _ctx(intent="expand", candidate=_content())
        mocked = AsyncMock(return_value={"points": ["要点一", "要点二"]})
        with patch("app.agent.tools.chat_async", new=mocked):
            result = _run(build_registry().execute(
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
            result = _run(build_registry().execute(
                _call("node_expand", node_id="ghost", count=2), ctx))
        self.assertFalse(result.ok)
        self.assertEqual(result.data["code"], "node_not_found")
        mocked.assert_not_called()

    def test_empty_points_is_failure(self):
        ctx = _ctx(intent="expand", candidate=_content())
        with patch("app.agent.tools.chat_async", new=AsyncMock(return_value={"points": []})):
            result = _run(build_registry().execute(
                _call("node_expand", node_id="b", count=2), ctx))
        self.assertFalse(result.ok)
        self.assertEqual(result.data["code"], "invalid_points")

    def test_count_out_of_range_is_rejected_by_schema(self):
        """count 只允许 2-4：参数模型拦下，工具根本不执行。"""
        ctx = _ctx(intent="expand", candidate=_content())
        result = _run(build_registry().execute(
            _call("node_expand", node_id="b", count=9), ctx))
        self.assertFalse(result.ok)
        self.assertEqual(result.data["code"], "invalid_tool_args")

    def test_without_candidate_is_rejected(self):
        ctx = _ctx(intent="expand")
        result = _run(build_registry().execute(_call("node_expand", node_id="b"), ctx))
        self.assertFalse(result.ok)
        self.assertEqual(result.data["code"], "no_candidate")


class RagToolTest(unittest.TestCase):
    def test_returns_hits_from_attached_sources(self):
        source = Source(kind=SourceKind.text, status="parsed",
                        text="会议决定在十月十七日发布二点零版本，请提前准备上线材料。")
        ctx = _ctx(sources=[source])
        result = _run(build_registry().execute(
            _call("rag_query", question="什么时候发布版本"), ctx))
        self.assertTrue(result.ok, result.content)
        self.assertGreaterEqual(len(result.data["hits"]), 1)
        hit = result.data["hits"][0]
        self.assertEqual(hit["source_id"], source.id)
        self.assertIn("十月十七日", hit["quote"])
        # 回给模型的必须是结构化 JSON，它要拿 source_id 和偏移去写引用
        self.assertIn("source_id", json.loads(result.content)[0])

    def test_without_sources_is_rejected(self):
        result = _run(build_registry().execute(_call("rag_query", question="任意"), _ctx()))
        self.assertFalse(result.ok)
        self.assertEqual(result.data["code"], "no_sources")

    def test_no_hit_is_success_with_empty_list(self):
        """检索不到不是错误：模型据此知道该换问法，而不是以为工具坏了。"""
        source = Source(kind=SourceKind.text, status="parsed", text="完全无关的一段文字。")
        ctx = _ctx(sources=[source])
        result = _run(build_registry().execute(
            _call("rag_query", question="量子色动力学的渐近自由"), ctx))
        self.assertTrue(result.ok)
        self.assertEqual(result.data["hits"], [])


class EditToolTest(unittest.TestCase):
    def test_update_changes_label(self):
        ctx = _ctx(intent="edit", candidate=_content())
        result = _run(build_registry().execute(_call(
            "mindmap_edit", ops=[{"op": "update", "id": "b", "changes": {"label": "乙改"}}]), ctx))
        self.assertTrue(result.ok, result.content)
        node = next(child for child in ctx.candidate.root.children if child.id == "b")
        self.assertEqual(node.label, "乙改")
        self.assertIn("b", result.data["changed_ids"])

    def test_remove_drops_subtree(self):
        ctx = _ctx(intent="edit", candidate=_content())
        result = _run(build_registry().execute(
            _call("mindmap_edit", ops=[{"op": "remove", "id": "a"}]), ctx))
        self.assertTrue(result.ok, result.content)
        self.assertEqual([child.id for child in ctx.candidate.root.children], ["b"])

    def test_move_into_own_descendant_is_rejected(self):
        ctx = _ctx(intent="edit", candidate=_content())
        result = _run(build_registry().execute(_call(
            "mindmap_edit", ops=[{"op": "move", "id": "a", "new_parent_id": "a1"}]), ctx))
        self.assertFalse(result.ok)
        self.assertEqual(result.data["code"], "cycle_detected")
        self.assertIn("path", result.data)

    def test_add_without_index_appends(self):
        """模型常只给 parent_id 和 label，缺 index 不该被判成参数错误。"""
        ctx = _ctx(intent="edit", candidate=_content())
        result = _run(build_registry().execute(_call(
            "mindmap_edit", ops=[{"op": "add", "id": "$new", "parent_id": "b", "label": "丙"}]), ctx))
        self.assertTrue(result.ok, result.content)
        node = next(child for child in ctx.candidate.root.children if child.id == "b")
        self.assertEqual([child.label for child in node.children], ["丙"])

    def test_empty_ops_is_rejected_by_schema(self):
        ctx = _ctx(intent="edit", candidate=_content())
        result = _run(build_registry().execute(_call("mindmap_edit", ops=[]), ctx))
        self.assertFalse(result.ok)
        self.assertEqual(result.data["code"], "invalid_tool_args")

    def test_without_candidate_is_rejected(self):
        result = _run(build_registry().execute(
            _call("mindmap_edit", ops=[{"op": "remove", "id": "a"}]), _ctx(intent="edit")))
        self.assertFalse(result.ok)
        self.assertEqual(result.data["code"], "no_candidate")

    def test_failure_keeps_candidate_untouched(self):
        """一条 op 失败时候选树必须原样，不能留下改了一半的中间态。"""
        ctx = _ctx(intent="edit", candidate=_content())
        before = ctx.candidate.model_dump()
        result = _run(build_registry().execute(_call("mindmap_edit", ops=[
            {"op": "update", "id": "b", "changes": {"label": "先改"}},
            {"op": "remove", "id": "ghost"},
        ]), ctx))
        self.assertFalse(result.ok)
        self.assertEqual(ctx.candidate.model_dump(), before)
        self.assertEqual(ctx.revision, 0)


class RegistryTest(unittest.TestCase):
    def test_all_four_tools_registered(self):
        self.assertEqual(
            build_registry().names(),
            ["mindmap_edit", "mindmap_generate", "node_expand", "rag_query"],
        )

    def test_unknown_tool_lists_available(self):
        result = _run(build_registry().execute(_call("generate_mindmap", topic="x"), _ctx()))
        self.assertFalse(result.ok)
        self.assertEqual(result.data["code"], "unknown_tool")
        self.assertIn("mindmap_generate", result.content)


if __name__ == "__main__":
    unittest.main()
