"""多轮工具循环的行为测试。

全部用脚本化的假 provider 驱动，不连网络也不消耗模型额度。这样做的理由：
循环里真正容易错的不是"怎么调模型"，而是结果配对、幂等、去重、预算结算、
终止判断——这些跟谁产生工具调用无关，用假实现能把每条分支都精确构造出来。
"""
from __future__ import annotations

import asyncio
import json
import unittest
from typing import Any
from unittest.mock import patch

from pydantic import BaseModel, Field, ValidationError

from app.agent.contracts import (
    AgentRunRequest,
    Completion,
    RunBudget,
    ToolCall,
    ToolResult,
    ToolSpec,
)
from app.agent.provider import _parse_completion
from app.agent.service import _dedupe_key, outline_text, run
from app.agent.tools import ToolRegistry, build_registry
from app.contracts.mindmap import MindMapContent
from app.generation.service import GenerationError

# 生成工具返回的树：4 个节点、两层。用于验证工具内调用与 snapshot 帧。
TREE = {
    "title": "示例",
    "root": {"id": "root", "label": "根", "children": [
        {"id": "a", "label": "甲", "children": []},
        {"id": "b", "label": "乙", "children": [
            {"id": "b1", "label": "乙一", "children": []},
        ]},
    ]},
}


def _node_ids(root) -> set[str]:
    ids = {root.id}
    for child in root.children:
        ids |= _node_ids(child)
    return ids


def _completion(calls: list[ToolCall] | None = None, finish: str = "stop",
                content: str = "好的") -> Completion:
    calls = calls or []
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = [
            {
                "id": call.call_id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.arguments, ensure_ascii=False)},
            }
            for call in calls
        ]
    return Completion(message=message, tool_calls=calls, finish_reason=finish,
                      usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})


class ScriptedProvider:
    """按脚本依次返回预设结果；脚本项可以是 Completion 或要抛的异常。"""

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.calls = 0

    async def complete_tools(self, messages, tools, *, budget):
        self.calls += 1
        if not self._script:
            raise AssertionError("脚本已用完，循环比预期多调了一次模型")
        item = self._script.pop(0)
        # CancelledError 继承自 BaseException 而不是 Exception，用 Exception 判断
        # 会让"脚本里放一个取消异常"被当作正常返回值 return 出去。
        if isinstance(item, BaseException):
            raise item
        return item


class EchoArgs(BaseModel):
    text: str = Field(min_length=1)


class CountArgs(BaseModel):
    times: int = Field(ge=1)


def _collect(request: AgentRunRequest, provider, registry: ToolRegistry | None = None) -> list:
    """把 async 生成器收成列表。项目没装 pytest-asyncio，用 asyncio.run 更省事。"""

    async def drain():
        return [event async for event in run(request, provider=provider, tools=registry)]

    return asyncio.run(drain())


def _registry(handler, args_model=EchoArgs, name: str = "echo",
              readonly: bool = False) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ToolSpec(name=name, description="测试工具", args_model=args_model,
                               fn=handler, readonly=readonly))
    return registry


def _request(intent: str = "create", budget: RunBudget | None = None) -> AgentRunRequest:
    return AgentRunRequest(goal="给机器学习做一张导图", intent=intent,
                           budget=budget or RunBudget())


class BasicFlowTest(unittest.TestCase):
    def test_stop_without_tools_ends_with_done(self):
        """模型直接给文字收尾：只发 done，不发工具事件。"""
        events = _collect(_request(), ScriptedProvider([_completion()]), build_registry("create"))
        types = [event.type for event in events]
        self.assertEqual(types, ["stage", "done"])
        self.assertEqual(events[-1].content, None)  # create 且没有快照

    def test_seq_is_continuous_from_one(self):
        """seq 必须从 1 连续递增：前端靠它判断丢帧和乱序。"""
        events = _collect(_request(), ScriptedProvider([_completion()]), build_registry("create"))
        self.assertEqual([event.seq for event in events], [1, 2])

    def test_tool_result_is_filled_back_by_call_id(self):
        """工具结果要按 tool_call_id 回填，且下一轮能看到它。"""
        seen_messages: list[list[dict]] = []

        class Recorder(ScriptedProvider):
            async def complete_tools(self, messages, tools, *, budget):
                seen_messages.append([dict(item) for item in messages])
                return await super().complete_tools(messages, tools, budget=budget)

        call = ToolCall(call_id="call_1", name="echo", arguments={"text": "原文"})
        provider = Recorder([_completion([call], finish="tool_calls"), _completion()])
        events = _collect(_request(), provider, _registry(_echo))

        self.assertIn("tool_start", [event.type for event in events])
        self.assertIn("tool_end", [event.type for event in events])
        # 第二轮的消息里必须有一条 role=tool 且 id 对得上
        second = seen_messages[1]
        tool_msgs = [item for item in second if item.get("role") == "tool"]
        self.assertEqual(len(tool_msgs), 1)
        self.assertEqual(tool_msgs[0]["tool_call_id"], "call_1")
        self.assertIn("echo:原文", tool_msgs[0]["content"])

    def test_finish_without_stop_and_without_tools_is_error(self):
        """既没有工具调用、finish_reason 又不是 stop：这是异常路径，不能当成功。"""
        events = _collect(_request(), ScriptedProvider([_completion(finish="content_filter")]),
                          build_registry("create"))
        self.assertEqual(events[-1].type, "error")
        self.assertEqual(events[-1].code, "unexpected_finish")


def _echo(args: EchoArgs, ctx) -> ToolResult:
    return ToolResult(ok=True, content=f"echo:{args.text}", data={"echoed": args.text})


class ToolDispatchTest(unittest.TestCase):
    def test_unknown_tool_executes_nothing_and_loop_continues(self):
        """模型编了个不存在的工具名：回一条可读错误，循环继续而不是崩掉。"""
        executed: list[str] = []

        def handler(args, ctx):
            executed.append("called")
            return ToolResult(ok=True, content="ok")

        call = ToolCall(call_id="c1", name="does_not_exist", arguments={})
        provider = ScriptedProvider([_completion([call], finish="tool_calls"), _completion()])
        events = _collect(_request(), provider, _registry(handler))

        self.assertEqual(executed, [])
        end = next(event for event in events if event.type == "tool_end")
        self.assertFalse(end.ok)
        self.assertEqual(end.result_summary["code"], "unknown_tool")
        self.assertEqual(events[-1].type, "done")

    def test_bad_arguments_executes_nothing(self):
        """参数缺必填字段：零执行，回 invalid_tool_args，循环继续。"""
        executed: list[str] = []

        def handler(args, ctx):
            executed.append("called")
            return ToolResult(ok=True, content="ok")

        call = ToolCall(call_id="c1", name="echo", arguments={})  # text 必填
        provider = ScriptedProvider([_completion([call], finish="tool_calls"), _completion()])
        events = _collect(_request(), provider, _registry(handler))

        self.assertEqual(executed, [])
        end = next(event for event in events if event.type == "tool_end")
        self.assertFalse(end.ok)
        self.assertEqual(end.result_summary["code"], "invalid_tool_args")
        self.assertEqual(events[-1].type, "done")

    def test_parse_error_is_reported_not_raised(self):
        """模型把参数 JSON 写坏了：报参数错误，不执行，也不让整轮崩。"""
        executed: list[str] = []

        def handler(args, ctx):
            executed.append("called")
            return ToolResult(ok=True, content="ok")

        call = ToolCall(call_id="c1", name="echo", arguments={},
                        raw_arguments='{"text": "半截', parse_error="函数参数不是合法 JSON")
        provider = ScriptedProvider([_completion([call], finish="tool_calls"), _completion()])
        events = _collect(_request(), provider, _registry(handler))

        self.assertEqual(executed, [])
        end = next(event for event in events if event.type == "tool_end")
        self.assertEqual(end.result_summary["code"], "invalid_tool_args")


class IdempotencyTest(unittest.TestCase):
    def test_same_call_id_runs_once(self):
        """同一个 call_id 出现两次，工具只该执行一次。"""
        executed: list[str] = []

        def handler(args, ctx):
            executed.append(args.text)
            return ToolResult(ok=True, content="ok")

        call = ToolCall(call_id="dup", name="echo", arguments={"text": "一次"})
        provider = ScriptedProvider([
            _completion([call, call], finish="tool_calls"),
            _completion(),
        ])
        _collect(_request(), provider, _registry(handler))
        self.assertEqual(executed, ["一次"])

    def test_readonly_tool_dedupes_same_arguments(self):
        """只读工具按参数去重：同样的查询必然得到同样的结果，重复执行没有意义。

        实测模型会并行发起两个检索请求，去重能省掉一次真实的网络往返。
        """
        executed: list[str] = []

        def handler(args, ctx):
            executed.append(args.text)
            return ToolResult(ok=True, content="ok")

        first = ToolCall(call_id="c1", name="echo", arguments={"text": "同样"})
        second = ToolCall(call_id="c2", name="echo", arguments={"text": "同样"})
        provider = ScriptedProvider([_completion([first, second], finish="tool_calls"), _completion()])
        _collect(_request(), provider, _registry(handler, readonly=True))
        self.assertEqual(executed, ["同样"])

    def test_write_tool_is_never_deduped(self):
        """写操作绝不能按参数去重。

        模型完全可能先改成 X、再改成 Y、再改回 X，第三次参数与第一次相同，
        但那时树的状态已经不是第一次的样子。走上缓存会直接返回旧结果，
        模型以为改成功了，实际停在 Y——比报错危险得多。
        """
        executed: list[str] = []

        def handler(args, ctx):
            executed.append(args.text)
            return ToolResult(ok=True, content="ok")

        calls = [ToolCall(call_id=f"c{index}", name="echo", arguments={"text": "同样"})
                 for index in range(2)]
        provider = ScriptedProvider([_completion(calls, finish="tool_calls"), _completion()])
        _collect(_request(), provider, _registry(handler, readonly=False))
        self.assertEqual(executed, ["同样", "同样"])

    def test_same_call_id_with_different_arguments_is_conflict(self):
        """同一个 call_id 换参数是协议违规，必须报错而不是返回旧结果。"""
        executed: list[str] = []

        def handler(args, ctx):
            executed.append(args.text)
            return ToolResult(ok=True, content="ok")

        first = ToolCall(call_id="same", name="echo", arguments={"text": "甲"})
        second = ToolCall(call_id="same", name="echo", arguments={"text": "乙"})
        provider = ScriptedProvider([_completion([first, second], finish="tool_calls"), _completion()])
        events = _collect(_request(), provider, _registry(handler))

        self.assertEqual(executed, ["甲"])
        ends = [event for event in events if event.type == "tool_end"]
        self.assertTrue(ends[0].ok)
        self.assertFalse(ends[1].ok)
        self.assertEqual(ends[1].result_summary["code"], "call_id_conflict")

    def test_dedupe_key_ignores_key_order(self):
        """参数键序不同但内容相同，指纹必须一致。"""
        one = ToolCall(call_id="a", name="echo", arguments={"x": 1, "y": 2})
        two = ToolCall(call_id="b", name="echo", arguments={"y": 2, "x": 1})
        self.assertEqual(_dedupe_key(one), _dedupe_key(two))


class BudgetTest(unittest.TestCase):
    def test_model_call_budget_exhausted_is_error_not_done(self):
        """模型一直要调工具：到上限就报错，不能静默截断后假装成功。"""
        def handler(args, ctx):
            return ToolResult(ok=True, content="ok")

        calls = [ToolCall(call_id=f"c{index}", name="echo", arguments={"text": str(index)})
                 for index in range(10)]
        script = [_completion([call], finish="tool_calls") for call in calls]
        events = _collect(
            _request(budget=RunBudget(max_model_calls=2, max_tool_calls=50)),
            ScriptedProvider(script), _registry(handler),
        )
        self.assertEqual(events[-1].type, "error")
        self.assertEqual(events[-1].code, "budget_exhausted")
        self.assertEqual(events[-1].limit, "max_model_calls")

    def test_tool_call_budget_exhausted_is_error(self):
        """一次响应里塞超过上限的工具调用：超出的那些不执行。"""
        executed: list[str] = []

        def handler(args, ctx):
            executed.append(args.text)
            return ToolResult(ok=True, content="ok")

        calls = [ToolCall(call_id=f"c{index}", name="echo", arguments={"text": str(index)})
                 for index in range(4)]
        events = _collect(
            _request(budget=RunBudget(max_model_calls=5, max_tool_calls=2)),
            ScriptedProvider([_completion(calls, finish="tool_calls")]), _registry(handler),
        )
        self.assertEqual(len(executed), 2)
        self.assertEqual(events[-1].code, "budget_exhausted")
        self.assertEqual(events[-1].limit, "max_tool_calls")

    def test_repair_budget_limits_failed_tool_calls(self):
        """max_repairs 数的是工具失败次数，与模型调用次数无关。"""
        def broken(args, ctx):
            return ToolResult(ok=False, content="参数不对", data={"code": "nope"})

        calls = [ToolCall(call_id=f"c{index}", name="echo", arguments={"text": str(index)})
                 for index in range(4)]
        script = [_completion([call], finish="tool_calls") for call in calls]
        events = _collect(
            _request(budget=RunBudget(max_model_calls=10, max_tool_calls=50, max_repairs=1)),
            ScriptedProvider(script), _registry(broken),
        )
        failures = [event for event in events if event.type == "tool_end" and not event.ok]
        # 容忍 1 次，第 2 次就停。两次失败都必须有配对的 tool_end，
        # 否则前端会一直停在"执行中"。
        self.assertEqual(len(failures), 2)
        self.assertEqual([event.type for event in events].count("tool_start"), 2)
        self.assertEqual(events[-1].code, "too_many_failures")
        self.assertEqual(events[-1].limit, "max_repairs")

    def test_budget_one_allows_exactly_one_real_model_call(self):
        """预算 1：外层调用 + 工具内部调用合计只能 1 次。

        这是实测缺陷的回归：判停只看外层轮数时，预算 1 的 run 仍然发出了
        4 次真实请求——外层 1 次，`mindmap_generate` 内部又调了 3 次。
        """
        chats: list[str] = []

        async def fake_chat(system, user, *, max_tokens=None):
            chats.append(user)
            return TREE

        call = ToolCall(call_id="c1", name="mindmap_generate", arguments={"topic": "机器学习"})
        provider = ScriptedProvider([_completion([call], finish="tool_calls"), _completion()])
        with patch("app.agent.tools.chat_async", new=fake_chat):
            events = _collect(
                _request(budget=RunBudget(max_model_calls=1)),
                provider, build_registry("create"),
            )

        self.assertEqual(provider.calls + len(chats), 1)
        # 唯一那次额度被外层用掉，工具必须直接失败而不是先花掉再上报
        self.assertEqual(chats, [])
        self.assertEqual(events[-1].type, "error")
        self.assertEqual(events[-1].limit, "max_model_calls")

    def test_budget_two_lets_the_tool_spend_the_second_call(self):
        """对照：预算 2 时工具能拿到第二次额度并真的生成。

        没有这一条，预算 1 的"工具内部零次调用"可能只是因为工具永远不调模型。
        """
        chats: list[str] = []

        async def fake_chat(system, user, *, max_tokens=None):
            chats.append(user)
            return TREE

        call = ToolCall(call_id="c1", name="mindmap_generate", arguments={"topic": "机器学习"})
        provider = ScriptedProvider([_completion([call], finish="tool_calls"), _completion()])
        with patch("app.agent.tools.chat_async", new=fake_chat):
            events = _collect(
                _request(budget=RunBudget(max_model_calls=2)),
                provider, build_registry("create"),
            )

        self.assertEqual(len(chats), 1)
        self.assertEqual(provider.calls + len(chats), 2)
        snapshot = next(event for event in events if event.type == "snapshot")
        self.assertEqual(_node_ids(snapshot.content.root), {"root", "a", "b", "b1"})


class OutlineRefreshTest(unittest.TestCase):
    def test_second_turn_sees_nodes_created_in_first_turn(self):
        """第二轮必须看到上一轮产出的节点 id。

        不刷新大纲的话，模型拿到的大纲永远是请求开始时的快照：它刚生成的节点
        没有 id 可引用，于是只能整张重来——那正是这次改造要消灭的行为。
        """
        seen: list[str] = []

        class Recorder(ScriptedProvider):
            async def complete_tools(self, messages, tools, *, budget):
                seen.append(messages[0]["content"])
                return await super().complete_tools(messages, tools, budget=budget)

        def generate(args, ctx):
            ctx.candidate = MindMapContent.model_validate({
                "title": "新图",
                "root": {"id": "root", "label": "根", "children": [
                    {"id": "fresh-node", "label": "刚生成的", "children": []},
                ]},
            })
            return ToolResult(ok=True, content="已生成")

        call = ToolCall(call_id="c1", name="echo", arguments={"text": "生成"})
        provider = Recorder([_completion([call], finish="tool_calls"), _completion()])
        _collect(_request(), provider, _registry(generate))

        self.assertEqual(len(seen), 2)
        self.assertNotIn("fresh-node", seen[0])
        self.assertIn("[fresh-node] 刚生成的", seen[1])

    def test_tool_internal_model_calls_count_against_budget(self):
        """工具内部直接调模型也要计入总预算。

        生成和展开工具各自会再调一次模型。不计数的话 max_model_calls 只约束了
        外层轮数，实际花费可以翻好几倍。
        """
        def costly(args, ctx):
            ctx.extra_model_calls += 1
            return ToolResult(ok=True, content="ok")

        calls = [ToolCall(call_id=f"c{index}", name="echo", arguments={"text": str(index)})
                 for index in range(3)]
        script = [_completion([call], finish="tool_calls") for call in calls]
        events = _collect(
            _request(budget=RunBudget(max_model_calls=2, max_tool_calls=50)),
            ScriptedProvider(script), _registry(costly),
        )
        self.assertEqual(events[-1].type, "error")
        self.assertEqual(events[-1].limit, "max_model_calls")


class SnapshotEmissionTest(unittest.TestCase):
    """snapshot 帧定义了就必须真的发出来。

    只定义不发射时，前端只能在 done 时一次性拿到内容，"边生成边看"无从谈起。
    """

    def test_generate_success_emits_snapshot_with_valid_tree(self):
        async def fake_chat(system, user, *, max_tokens=None):
            return TREE

        call = ToolCall(call_id="c1", name="mindmap_generate", arguments={"topic": "机器学习"})
        provider = ScriptedProvider([_completion([call], finish="tool_calls"), _completion()])
        with patch("app.agent.tools.chat_async", new=fake_chat):
            events = _collect(_request(), provider, build_registry("create"))

        types = [event.type for event in events]
        # 顺序即契约：tool_end 之后、done 之前；generate 不产生 op 批次，
        # 所以这里看不到 ops 帧
        self.assertEqual(types, ["stage", "tool_start", "tool_end", "snapshot", "done"])
        self.assertEqual([event.seq for event in events], [1, 2, 3, 4, 5])

        snapshot = next(event for event in events if event.type == "snapshot")
        # 整树校验：能重新走一遍契约（id 唯一、层数、children 上限）才算完整，
        # 补括号拼出来的半棵树过不了这一关
        replay = MindMapContent.model_validate(snapshot.content.model_dump())
        self.assertEqual(_node_ids(replay.root), {"root", "a", "b", "b1"})
        # snapshot 与 done 必须是同一棵树：两个来源不一致会让前端闪一下
        self.assertEqual(events[-1].content.model_dump(), snapshot.content.model_dump())

    def test_snapshot_is_not_emitted_when_candidate_already_exists(self):
        """expand / edit 一开始就有候选树：不重复发 snapshot。"""
        tree = MindMapContent.model_validate(TREE)

        def handler(args, ctx):
            ctx.candidate = tree.model_copy(deep=True)
            return ToolResult(ok=True, content="ok")

        call = ToolCall(call_id="c1", name="echo", arguments={"text": "改"})
        provider = ScriptedProvider([_completion([call], finish="tool_calls"), _completion()])
        request = AgentRunRequest(goal="展开节点", intent="edit", snapshot=tree)
        events = _collect(request, provider, _registry(handler))
        self.assertNotIn("snapshot", [event.type for event in events])


class ProviderParsingTest(unittest.TestCase):
    def test_length_finish_reason_refuses_to_parse_tools(self):
        """输出被硬切时，半截参数不能拿去执行。"""
        payload = {
            "choices": [{
                "finish_reason": "length",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"id": "c1", "type": "function",
                                    "function": {"name": "echo", "arguments": '{"text": "半'}}],
                },
            }],
            "usage": {},
        }
        with self.assertRaises(GenerationError):
            _parse_completion(bytearray(json.dumps(payload).encode()))

    def test_broken_arguments_become_parse_error_not_exception(self):
        """参数 JSON 坏掉：标记 parse_error 交给执行层，不在解析层抛。"""
        payload = {
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"id": "c1", "type": "function",
                                    "function": {"name": "echo", "arguments": '{"text": '}}],
                },
            }],
            "usage": {},
        }
        completion = _parse_completion(bytearray(json.dumps(payload).encode()))
        self.assertEqual(len(completion.tool_calls), 1)
        self.assertIsNotNone(completion.tool_calls[0].parse_error)
        self.assertEqual(completion.tool_calls[0].arguments, {})

    def test_valid_tool_call_is_decoded(self):
        payload = {
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"id": "c1", "type": "function",
                                    "function": {"name": "echo", "arguments": '{"text": "中文"}'}}],
                },
            }],
            "usage": {"total_tokens": 3},
        }
        completion = _parse_completion(bytearray(json.dumps(payload).encode()))
        self.assertEqual(completion.finish_reason, "tool_calls")
        self.assertEqual(completion.tool_calls[0].arguments["text"], "中文")
        self.assertEqual(completion.usage["total_tokens"], 3)


class CancelTest(unittest.TestCase):
    def test_cancelled_error_propagates(self):
        """取消必须穿透循环，不能被吞成一次正常结束。"""
        async def scenario():
            provider = ScriptedProvider([asyncio.CancelledError()])
            async for _ in run(_request(), provider=provider, tools=build_registry("create")):
                pass

        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(scenario())


class OutlineTest(unittest.TestCase):
    def test_outline_carries_node_ids(self):
        """大纲必须带 id：否则模型只能靠文字猜改哪一个，而文字会被它自己改写。"""
        content = MindMapContent.model_validate({
            "title": "示例",
            "root": {"id": "root", "label": "根", "children": [
                {"id": "n1", "label": "分支", "children": [
                    {"id": "n2", "label": "叶", "children": []},
                ]},
            ]},
        })
        text = outline_text(content)
        self.assertIn("[root] 根", text)
        self.assertIn("[n1] 分支", text)
        self.assertIn("[n2] 叶", text)
        self.assertIn("  ", text)  # 层级靠缩进表达

    def test_outline_of_none(self):
        self.assertIn("没有导图", outline_text(None))


class RegistryTest(unittest.TestCase):
    def test_fresh_registry_starts_empty(self):
        """注册表的初始状态是空的；业务工具由 build_registry() 填进去。

        这里断言的是 ToolRegistry 本身，不是 build_registry()——后者会随
        工具集演进，拿它当"空表"用迟早失效。
        """
        registry = ToolRegistry()
        self.assertEqual(registry.names(), [])
        self.assertEqual(registry.specs(), [])

    def test_duplicate_registration_is_rejected(self):
        registry = _registry(_echo)
        with self.assertRaises(ValueError):
            registry.register(ToolSpec(name="echo", description="重复", args_model=EchoArgs, fn=_echo))

    def test_schema_comes_from_args_model(self):
        """工具 schema 由参数模型生成，文档与校验同源，不会各写一份漂掉。"""
        schema = _registry(_echo).specs()[0]
        self.assertEqual(schema["function"]["name"], "echo")
        self.assertIn("text", schema["function"]["parameters"]["properties"])
        self.assertIn("required", schema["function"]["parameters"])

    def test_tool_returning_wrong_type_raises(self):
        """工具返回值写错要立刻暴露，不能让它以 None 传下去。"""
        def broken(args, ctx):
            return "不是 ToolResult"

        registry = _registry(broken)
        call = ToolCall(call_id="c1", name="echo", arguments={"text": "x"})

        async def scenario():
            return await registry.execute(call, ctx=None)

        with self.assertRaises(TypeError):
            asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
