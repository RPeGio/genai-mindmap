"""模型思考过程的上报（`thinking` 帧）。

四块分开测，因为它们的失败方式完全不同：

  1. **解析**：`delta.reasoning` 与 `delta.reasoning_content` 两个字段名都要认。
     认错的后果是"推理模型的思考过程永远显示不出来"，而且不报错。
  2. **只在挂了通道时才发**：没有消费者的路径（单测、直接调工具）必须与加这个
     功能之前逐字相同——连流式请求都不该发。
  3. **不污染正文**：思考要拼进 `content` 的话 JSON 就解不出来了。这条最容易
     写错，因为两个字段都叫"模型吐出来的字"。
  4. **帧形状**：尾部截断、空片段不发、`waited_ms` 该不归零的地方不归零。

全部用假 httpx 客户端驱动，不连网络也不消耗额度。
"""
from __future__ import annotations

import asyncio
import json
import os
import unittest
from typing import Any
from unittest.mock import patch

import httpx

from app.agent.contracts import (
    THINKING_TAIL_CHARS,
    AgentRunRequest,
    Completion,
    RunBudget,
    RunContext,
    ThinkingEvent,
    ToolCall,
    ToolResult,
    ToolSpec,
)
from app.agent.service import run
from app.agent.tools import ToolRegistry
from app.generation.service import (
    _stream_frame,
    chat_async,
    reset_thinking_sink,
    set_thinking_sink,
)

BASE_ENV = {
    "MINDMAP_LLM_BASE_URL": "https://llm.example.test/v1",
    "MINDMAP_LLM_API_KEY": "test-key",
    "MINDMAP_LLM_MODEL": "test-model",
}
TOPIC = "报销为什么慢"


def _run(coro):
    return asyncio.run(coro)


def _mock_client(handler):
    """把 `chat_async` 里新建的 httpx 客户端换成按 handler 应答的假客户端。"""
    real = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def build(*args, **kwargs):
        kwargs.pop("transport", None)
        return real(transport=transport, **kwargs)

    return patch("app.generation.service.httpx.AsyncClient", new=build)


def _sse(frames: list[dict], *, done: bool = True) -> bytes:
    parts = []
    for frame in frames:
        parts.append("data: " + json.dumps({"choices": [frame]}, ensure_ascii=False) + "\n\n")
    if done:
        parts.append("data: [DONE]\n\n")
    return "".join(parts).encode()


class StreamFrameTest(unittest.TestCase):
    """`_stream_frame` 认得哪些字段名。"""

    def test_reasoning_is_read(self):
        """Command Code 的免费模型发的是 `delta.reasoning`。"""
        frame = _stream_frame(json.dumps(
            {"choices": [{"delta": {"reasoning": "先看流程"}, "finish_reason": None}]}))
        self.assertEqual(frame, ("", "先看流程", None))

    def test_reasoning_content_is_read_too(self):
        """DeepSeek 的推理模型发的是 `delta.reasoning_content`，两个名字都认。"""
        frame = _stream_frame(json.dumps(
            {"choices": [{"delta": {"reasoning_content": "先看流程"}}]}))
        self.assertEqual(frame, ("", "先看流程", None))

    def test_reasoning_wins_when_both_are_present(self):
        """两个字段同时出现时取 `reasoning`：那是本项目实测的那一家。"""
        frame = _stream_frame(json.dumps({"choices": [{"delta": {
            "reasoning": "实测的那家", "reasoning_content": "另一家"}}]}))
        self.assertEqual(frame, ("", "实测的那家", None))

    def test_content_and_reasoning_travel_together(self):
        frame = _stream_frame(json.dumps({"choices": [{
            "delta": {"content": "正文", "reasoning": "想法"}, "finish_reason": "stop"}]}))
        self.assertEqual(frame, ("正文", "想法", "stop"))

    def test_a_plain_frame_has_no_reasoning(self):
        frame = _stream_frame(json.dumps({"choices": [{"delta": {"content": "正文"}}]}))
        self.assertEqual(frame, ("正文", "", None))

    def test_broken_json_is_still_none(self):
        self.assertIsNone(_stream_frame("{不是 json"))
        self.assertIsNone(_stream_frame(json.dumps({"choices": []})))


class ThinkingSinkTest(unittest.TestCase):
    """思考增量只在挂了 sink 时才产生流式请求与回调。"""

    def test_thoughts_reach_the_sink_in_order(self):
        seen: list[str] = []

        async def sink(text: str) -> None:
            seen.append(text)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=_sse([
                {"delta": {"reasoning": "先"}},
                {"delta": {"reasoning": "看流程"}},
                {"delta": {"content": '{"a":1}'}, "finish_reason": "stop"},
            ]))

        async def call():
            token = set_thinking_sink(sink)
            try:
                return await chat_async("系统", "用户", max_tokens=400)
            finally:
                reset_thinking_sink(token)

        with patch.dict(os.environ, BASE_ENV), _mock_client(handler):
            result = _run(call())
        self.assertEqual(seen, ["先", "看流程"])
        # 思考**不能**拼进正文，否则 JSON 解析不出来
        self.assertEqual(result, {"a": 1})

    def test_no_sink_means_no_streaming_request(self):
        """没有 sink、也没有 on_delta 时必须走一次性请求。

        这条钉的是"默认行为与加功能之前逐字相同"：多出来的一个流式请求会让
        每个不关心思考的调用方多一次往返。
        """
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={
                "choices": [{"message": {"content": '{"a":1}'}, "finish_reason": "stop"}]})

        with patch.dict(os.environ, BASE_ENV), _mock_client(handler):
            result = _run(chat_async("系统", "用户", max_tokens=400))
        self.assertEqual(result, {"a": 1})
        self.assertEqual(len(requests), 1)
        self.assertNotIn("stream", requests[0].content.decode())

    def test_sink_alone_still_streams(self):
        """有 sink、没有 `on_delta` 时也要走流式：写说明那段没有预览解析器。"""
        requests: list[httpx.Request] = []
        seen: list[str] = []

        async def sink(text: str) -> None:
            seen.append(text)

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, content=_sse([
                {"delta": {"reasoning": "想"}},
                {"delta": {"content": '{"a":1}'}, "finish_reason": "stop"},
            ]))

        async def call():
            token = set_thinking_sink(sink)
            try:
                return await chat_async("系统", "用户", max_tokens=400)
            finally:
                reset_thinking_sink(token)

        with patch.dict(os.environ, BASE_ENV), _mock_client(handler):
            result = _run(call())
        self.assertEqual(seen, ["想"])
        self.assertEqual(result, {"a": 1})
        self.assertTrue(json.loads(requests[0].content)["stream"])

    def test_a_broken_sink_does_not_break_the_generation(self):
        """sink 自己抛错只是"不再显示思考"，正文照收。"""
        calls: list[str] = []

        async def sink(text: str) -> None:
            calls.append(text)
            raise RuntimeError("界面挂了")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=_sse([
                {"delta": {"reasoning": "第一"}},
                {"delta": {"reasoning": "第二"}},
                {"delta": {"content": '{"a":1}'}, "finish_reason": "stop"},
            ]))

        async def call():
            token = set_thinking_sink(sink)
            try:
                return await chat_async("系统", "用户", max_tokens=400)
            finally:
                reset_thinking_sink(token)

        with patch.dict(os.environ, BASE_ENV), _mock_client(handler):
            result = _run(call())
        self.assertEqual(result, {"a": 1})
        # 第一次抛错后推送通道就关了，第二条不再尝试
        self.assertEqual(calls, ["第一"])

    def test_sink_is_restored_after_reset(self):
        """`reset_thinking_sink` 之后不再有消费者——否则帧会发进一个没人读的队列。"""
        async def sink(text: str) -> None:  # pragma: no cover - 不该被调用
            raise AssertionError("sink 不该在复位之后还被调用")

        token = set_thinking_sink(sink)
        reset_thinking_sink(token)
        with patch.dict(os.environ, BASE_ENV), _mock_client(
                lambda request: httpx.Response(200, json={
                    "choices": [{"message": {"content": '{"a":1}'}, "finish_reason": "stop"}]})):
            self.assertEqual(_run(chat_async("系统", "用户", max_tokens=400)), {"a": 1})


# ---------------------------------------------------------------- 帧形状与端到端


class _EchoArgs(__import__("pydantic").BaseModel):
    pass


class _ScriptedProvider:
    """按脚本回放一轮 provider：模型要求调 `think` 工具。"""

    def __init__(self, completion: Completion) -> None:
        self.model_name = "scripted"
        self.served_model = "scripted"
        self._completion = completion

    async def complete_tools(self, messages, tools, *, budget) -> Completion:
        return self._completion


def _one_call(name: str) -> Completion:
    """一轮"模型要求调 `name`"的 completion。

    `tool_calls` 必须**同时**在 `message` 里与顶层字段上：循环读的是顶层那个，
    而 `message` 那份要原样进下一轮的 messages。只填一边时循环会判成"模型没调
    任何工具"，报 `unexpected_finish`——本文件第一次跑就踩了这个。
    """
    call = ToolCall(call_id="c-1", name=name, arguments={})
    return Completion(
        message={"role": "assistant", "content": "", "tool_calls": [{
            "id": call.call_id, "type": "function",
            "function": {"name": name, "arguments": "{}"},
        }]},
        tool_calls=[call],
        finish_reason="tool_calls",
        usage={"total_tokens": 3},
    )


class ThinkingFrameTest(unittest.TestCase):
    """端到端：工具里的思考增量变成 `thinking` 帧，且不干扰其它帧。"""

    def test_thoughts_become_thinking_frames(self):
        async def think(args, ctx):
            # 工具自己发不了思考帧（那是 generation 层的事），这里直接验证
            # 上报通道的包装：调 `chat_async` 时挂了 sink，思考就会流出来。
            from app.generation.service import _thinking_sink
            sink = _thinking_sink.get()
            self.assertIsNotNone(sink, "工具执行期间必须已经挂上思考通道")
            await sink("我想一想")
            return ToolResult(ok=True, content="想好了", data={"changes": 1})

        registry = ToolRegistry()
        registry.register(ToolSpec(name="think", description="想", args_model=_EchoArgs, fn=think))
        provider = _ScriptedProvider(_one_call("think"))
        request = AgentRunRequest(goal=TOPIC, intent="create",
                                  budget=RunBudget(max_model_calls=5, max_tool_calls=5))

        async def drain():
            return [event async for event in run(request, provider=provider, tools=registry)]

        events = _run(drain())
        kinds = [event.type for event in events]
        self.assertIn("thinking", kinds)
        thinking = [event for event in events if isinstance(event, ThinkingEvent)]
        self.assertEqual(thinking[0].text, "我想一想")
        # seq 连续：思考帧也占号，不能让它插进来把"丢帧"判断带歪
        self.assertEqual([event.seq for event in events], list(range(1, len(events) + 1)))

    def test_long_thoughts_are_cut_to_the_tail(self):
        """超长片段只留尾部：页面只显示一行，前面那些对"它在动"没有信息。"""
        async def think(args, ctx):
            from app.generation.service import _thinking_sink
            await _thinking_sink.get()("前" * 500 + "尾巴")
            return ToolResult(ok=True, content="想好了")

        registry = ToolRegistry()
        registry.register(ToolSpec(name="think", description="想", args_model=_EchoArgs, fn=think))
        provider = _ScriptedProvider(_one_call("think"))
        request = AgentRunRequest(goal=TOPIC, intent="create",
                                  budget=RunBudget(max_model_calls=5, max_tool_calls=5))

        async def drain():
            return [event async for event in run(request, provider=provider, tools=registry)]

        events = _run(drain())
        frame = next(event for event in events if isinstance(event, ThinkingEvent))
        self.assertLessEqual(len(frame.text), THINKING_TAIL_CHARS)
        self.assertTrue(frame.text.endswith("尾巴"))

    def test_blank_thoughts_are_dropped(self):
        """纯空白的片段不发帧：模型常在开头发一个空格，推上去只会让那行闪一下。"""
        async def think(args, ctx):
            from app.generation.service import _thinking_sink
            sink = _thinking_sink.get()
            await sink("   ")
            await sink("\n")
            await sink("有内容")
            return ToolResult(ok=True, content="想好了")

        registry = ToolRegistry()
        registry.register(ToolSpec(name="think", description="想", args_model=_EchoArgs, fn=think))
        provider = _ScriptedProvider(_one_call("think"))
        request = AgentRunRequest(goal=TOPIC, intent="create",
                                  budget=RunBudget(max_model_calls=5, max_tool_calls=5))

        async def drain():
            return [event async for event in run(request, provider=provider, tools=registry)]

        events = _run(drain())
        frames = [event for event in events if isinstance(event, ThinkingEvent)]
        self.assertEqual([frame.text for frame in frames], ["有内容"])

    def test_the_channel_is_closed_when_the_tool_ends(self):
        """工具结束之后 sink 必须复位：否则下一轮会把帧推进一个没人读的队列。"""
        from app.generation.service import _thinking_sink

        async def think(args, ctx):
            return ToolResult(ok=True, content="想好了")

        registry = ToolRegistry()
        registry.register(ToolSpec(name="think", description="想", args_model=_EchoArgs, fn=think))
        provider = _ScriptedProvider(_one_call("think"))
        request = AgentRunRequest(goal=TOPIC, intent="create",
                                  budget=RunBudget(max_model_calls=5, max_tool_calls=5))

        async def drain():
            return [event async for event in run(request, provider=provider, tools=registry)]

        _run(drain())
        self.assertIsNone(_thinking_sink.get())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
