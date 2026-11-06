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


def _registry(handler, args_model=EchoArgs, name: str = "echo") -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ToolSpec(name=name, description="测试工具", args_model=args_model, fn=handler))
    return registry


def _request(intent: str = "create", budget: RunBudget | None = None) -> AgentRunRequest:
    return AgentRunRequest(goal="给机器学习做一张导图", intent=intent,
                           budget=budget or RunBudget())


class BasicFlowTest(unittest.TestCase):
    def test_stop_without_tools_ends_with_done(self):
        """模型直接给文字收尾：只发 done，不发工具事件。"""
        events = _collect(_request(), ScriptedProvider([_completion()]), build_registry())
        types = [event.type for event in events]
        self.assertEqual(types, ["stage", "done"])
        self.assertEqual(events[-1].content, None)  # create 且没有快照

    def test_seq_is_continuous_from_one(self):
        """seq 必须从 1 连续递增：前端靠它判断丢帧和乱序。"""
        events = _collect(_request(), ScriptedProvider([_completion()]), build_registry())
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
                          build_registry())
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

    def test_same_arguments_different_ids_runs_once(self):
        """不同 call_id 但参数完全一致（模型并行发两次同请求）：也只执行一次。

        实测模型会并行发起两个 node_expand(n1)，执行两次就会加两遍子节点。
        """
        executed: list[str] = []

        def handler(args, ctx):
            executed.append(args.text)
            return ToolResult(ok=True, content="ok")

        first = ToolCall(call_id="c1", name="echo", arguments={"text": "同样"})
        second = ToolCall(call_id="c2", name="echo", arguments={"text": "同样"})
        provider = ScriptedProvider([_completion([first, second], finish="tool_calls"), _completion()])
        _collect(_request(), provider, _registry(handler))
        self.assertEqual(executed, ["同样"])

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
            async for _ in run(_request(), provider=provider, tools=build_registry()):
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
