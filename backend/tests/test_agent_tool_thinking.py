"""工具选择轮的思考帧（`provider.complete_tools` 走流式那条路）。

与 `test_agent_thinking.py` 分开：那边测的是工具执行期间（`chat_async` 那条
出口）的思考帧，这边测**工具选择轮**——`_run_tool` 之外唯一的长静默，免费档
实测单次能到几十秒，而它以前只有心跳。两处的失败方式完全不同：那边坏在字段名
或回调，这边坏在**分片拼装**与**兜底**。

五块：

  1. **没人看就不变**：没有消费者时这条出口必须与加流式之前逐字节一致（请求体
     没有 `stream` 键、照样收整包）。这是"别为了一个预览改掉所有直接调用方"。
  2. **有人看才切流式**：`reasoning` 逐帧转进 sink，正文与工具调用都不受影响。
  3. **分片拼装**：`delta.tool_calls` 是按片发的（首片带 id 与函数名，后续片
     只有 arguments 的一段），拼错时坏的不是形状而是**参数内容**。
  4. **兜底**：服务端不认 `stream` 直接回整包 JSON、流里没有可用帧、分片缺 id
     或函数名——这三种都要退回整包读，不能因为"想显示思考"让这一轮失败。
  5. **`length` 照旧硬失败**：半截 `arguments` 拿去执行比失败危险得多，两条读法
     必须同一个判定（见 `_parse_completion` 上方那段注释）。
"""
from __future__ import annotations

import asyncio
import json
import os
import unittest
from typing import Any
from unittest.mock import patch

import httpx

from app.agent import provider as provider_module
from app.agent.contracts import (
    THINKING_TAIL_CHARS,
    AgentRunRequest,
    RunBudget,
    ThinkingEvent,
    ToolResult,
    ToolSpec,
)
from app.agent.service import run
from app.agent.tools import ToolRegistry
from app.generation.service import (
    reset_thinking_sink,
    set_thinking_sink,
)

BASE_ENV = {
    "MINDMAP_LLM_BASE_URL": "https://llm.example.test/v1",
    "MINDMAP_LLM_API_KEY": "test-key",
    "MINDMAP_LLM_MODEL": "test-model",
}
TOPIC = "报销为什么慢"


def _sse(frames: list[dict], *, done: bool = True) -> bytes:
    parts = []
    for frame in frames:
        parts.append("data: " + json.dumps({"choices": [frame]}, ensure_ascii=False) + "\n\n")
    if done:
        parts.append("data: [DONE]\n\n")
    return "".join(parts).encode()


def _whole(model: str = "test-model", *, tool_name: str = "") -> bytes:
    """整包（非流式）响应体：服务端不认 `stream` 时回的就是这个形状。"""
    message: dict[str, Any] = {"role": "assistant", "content": ""}
    if tool_name:
        message["tool_calls"] = [{
            "id": "call-whole",
            "type": "function",
            "function": {"name": tool_name, "arguments": "{}"},
        }]
    return json.dumps({
        "model": model,
        "choices": [{"message": message, "finish_reason": "tool_calls" if tool_name else "stop"}],
    }).encode()


def _client(handler):
    """按 handler 应答的假 httpx 客户端。

    用真的 `httpx.AsyncClient` + `MockTransport`，而不是手写替身：`aiter_lines()`
    对 SSE 的切行行为（`data: {...}\\n\\n`）是 httpx 自己实现的，手写替身会把
    "我的替身怎么切行"当成协议的一部分，测出来的绿跟自己实现一致、跟线上不一致。
    """
    real = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def build(*args, **kwargs):
        kwargs.pop("transport", None)
        return real(transport=transport, **kwargs)

    return build


def _record(seen: list[httpx.Request]):
    """记下每次请求的请求体，同时按顺序回放响应。"""
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 覆盖在各用例里
        seen.append(request)
        return httpx.Response(200, content=_whole())
    return handler


class _Recorder:
    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = responses
        self.requests: list[httpx.Request] = []

    @property
    def bodies(self) -> list[dict]:
        return [json.loads(request.content) for request in self.requests]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self.responses) - 1)
        return self.responses[index]


def _call_complete_tools(sink: Any = None):
    """跑一轮 `complete_tools`。产品入口是 `agent.service`，这里直接调最省事。"""
    provider = provider_module.default_provider()
    budget = RunBudget()

    async def call():
        if sink is None:
            return await provider.complete_tools(
                [{"role": "user", "content": "hi"}], [], budget=budget)
        token = set_thinking_sink(sink)
        try:
            return await provider.complete_tools(
                [{"role": "user", "content": "hi"}], [], budget=budget)
        finally:
            reset_thinking_sink(token)

    return asyncio.run(call())


class NoConsumerTest(unittest.TestCase):
    """没有消费者时，这条出口与加流式之前逐字节一致。"""

    def test_the_request_body_has_no_stream_key(self):
        recorder = _Recorder([httpx.Response(200, content=_whole())])
        with patch.dict(os.environ, BASE_ENV), patch.object(
                provider_module.httpx, "AsyncClient", _client(recorder)):
            completion = _call_complete_tools()
        self.assertNotIn("stream", recorder.bodies[0])
        self.assertEqual(completion.tool_calls, [])
        self.assertEqual(completion.finish_reason, "stop")

    def test_the_whole_body_is_still_parsed_with_tools(self):
        """整包那条读法不能退化：工具调用照旧从 `message.tool_calls` 里读。"""
        recorder = _Recorder([httpx.Response(200, content=_whole(tool_name="think"))])
        with patch.dict(os.environ, BASE_ENV), patch.object(
                provider_module.httpx, "AsyncClient", _client(recorder)):
            completion = _call_complete_tools()
        self.assertEqual([call.name for call in completion.tool_calls], ["think"])
        self.assertEqual(completion.tool_calls[0].call_id, "call-whole")


class ConsumerTest(unittest.TestCase):
    """有人看思考的时候才切 SSE。"""

    def test_thoughts_reach_the_sink_and_the_call_survives(self):
        seen: list[str] = []

        async def sink(text: str) -> None:
            seen.append(text)

        recorder = _Recorder([httpx.Response(200, content=_sse([
            {"delta": {"reasoning": "先看"}},
            {"delta": {"reasoning": "这个工具"}},
            {"delta": {"tool_calls": [{"index": 0, "id": "c-1",
                                       "function": {"name": "think", "arguments": ""}}]}},
            {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{}"}}]},
             "finish_reason": "tool_calls"},
        ]))])
        with patch.dict(os.environ, BASE_ENV), patch.object(
                provider_module.httpx, "AsyncClient", _client(recorder)):
            completion = _call_complete_tools(sink)

        self.assertEqual(seen, ["先看", "这个工具"])
        self.assertTrue(recorder.bodies[0]["stream"])
        self.assertEqual([call.name for call in completion.tool_calls], ["think"])
        self.assertEqual(completion.tool_calls[0].arguments, {})

    def test_a_broken_sink_does_not_break_the_call(self):
        """sink 自己抛错只是"不再显示思考"，工具调用照收。"""
        calls: list[str] = []

        async def sink(text: str) -> None:
            calls.append(text)
            raise RuntimeError("界面挂了")

        recorder = _Recorder([httpx.Response(200, content=_sse([
            {"delta": {"reasoning": "第一"}},
            {"delta": {"reasoning": "第二"}},
            {"delta": {"tool_calls": [{"index": 0, "id": "c-1",
                                       "function": {"name": "think", "arguments": "{}"}}]},
             "finish_reason": "tool_calls"},
        ]))])
        with patch.dict(os.environ, BASE_ENV), patch.object(
                provider_module.httpx, "AsyncClient", _client(recorder)):
            completion = _call_complete_tools(sink)

        self.assertEqual(calls, ["第一"])
        self.assertEqual(len(completion.tool_calls), 1)


class ToolCallAssemblyTest(unittest.TestCase):
    """`delta.tool_calls` 分片拼装。"""

    def _run_stream(self, frames: list[dict], sink=None):
        recorder = _Recorder([httpx.Response(200, content=_sse(frames))])
        with patch.dict(os.environ, BASE_ENV), patch.object(
                provider_module.httpx, "AsyncClient", _client(recorder)):
            return _call_complete_tools(sink or _noop_sink()), recorder

    def test_arguments_are_joined_across_chunks(self):
        """参数是一段一段发出来的：拼错一个字符，工具收到就是坏参数。"""
        completion, _ = self._run_stream([
            {"delta": {"tool_calls": [{"index": 0, "id": "c-1",
                                       "function": {"name": "add_node",
                                                    "arguments": '{"lab'}}]}},
            {"delta": {"tool_calls": [{"index": 0,
                                       "function": {"arguments": 'el": "'}}]}},
            {"delta": {"tool_calls": [{"index": 0,
                                       "function": {"arguments": '预算"}'}}]}},
            {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": ""}}]},
             "finish_reason": "tool_calls"},
        ])
        call = completion.tool_calls[0]
        self.assertEqual(call.raw_arguments, '{"label": "预算"}')
        self.assertEqual(call.arguments, {"label": "预算"})
        self.assertIsNone(call.parse_error)

    def test_two_calls_are_kept_apart(self):
        """`index` 是唯一稳定的键：按到达顺序拼会把两个调用的参数揉成一份。"""
        completion, _ = self._run_stream([
            {"delta": {"tool_calls": [{"index": 0, "id": "c-1",
                                       "function": {"name": "first", "arguments": "{}"}}]}},
            {"delta": {"tool_calls": [{"index": 1, "id": "c-2",
                                       "function": {"name": "second", "arguments": "{}"}}]}},
            {"finish_reason": "tool_calls"},
        ])
        self.assertEqual([call.name for call in completion.tool_calls], ["first", "second"])
        self.assertEqual([call.call_id for call in completion.tool_calls], ["c-1", "c-2"])

    def test_broken_arguments_are_reported_not_raised(self):
        """参数不是合法 JSON 时交给执行层回一条具体错误，别在这里抛。"""
        completion, _ = self._run_stream([
            {"delta": {"tool_calls": [{"index": 0, "id": "c-1",
                                       "function": {"name": "think",
                                                    "arguments": "{半截"}}]}},
            {"finish_reason": "tool_calls"},
        ])
        call = completion.tool_calls[0]
        self.assertEqual(call.arguments, {})
        self.assertIn("不是合法 JSON", call.parse_error or "")

    def test_the_message_carries_the_raw_calls(self):
        """`message` 要原样进下一轮 messages：缺了它模型看不到自己刚才要调什么。"""
        completion, _ = self._run_stream([
            {"delta": {"tool_calls": [{"index": 0, "id": "c-1",
                                       "function": {"name": "think", "arguments": "{}"}}]}},
            {"finish_reason": "tool_calls"},
        ])
        self.assertEqual(completion.message["tool_calls"], [{
            "id": "c-1", "type": "function",
            "function": {"name": "think", "arguments": "{}"},
        }])


class FallbackTest(unittest.TestCase):
    """流式这条路认栽的三种情况，都要退回整包读。"""

    def test_a_server_that_ignores_stream_still_works(self):
        """有的兼容端点不理 `stream`，直接回整包 JSON。"""
        recorder = _Recorder([httpx.Response(200, content=_whole(tool_name="think"))])
        with patch.dict(os.environ, BASE_ENV), patch.object(
                provider_module.httpx, "AsyncClient", _client(recorder)):
            completion = _call_complete_tools(_noop_sink())
        # 第一次带 stream，第二次退回整包
        self.assertTrue(recorder.bodies[0]["stream"])
        self.assertEqual(len(recorder.requests), 2)
        self.assertEqual([call.name for call in completion.tool_calls], ["think"])

    def test_a_fragment_without_a_name_falls_back(self):
        """缺函数名的分片拼不出可执行的调用——退回整包读，别猜。"""
        recorder = _Recorder([
            httpx.Response(200, content=_sse([
                {"delta": {"tool_calls": [{"index": 0, "id": "c-1",
                                           "function": {"arguments": "{}"}}]}},
                {"finish_reason": "tool_calls"},
            ])),
            httpx.Response(200, content=_whole(tool_name="think")),
        ])
        with patch.dict(os.environ, BASE_ENV), patch.object(
                provider_module.httpx, "AsyncClient", _client(recorder)):
            completion = _call_complete_tools(_noop_sink())
        self.assertEqual(len(recorder.requests), 2)
        self.assertEqual([call.name for call in completion.tool_calls], ["think"])

    def test_a_stream_with_no_usable_frame_falls_back(self):
        recorder = _Recorder([
            httpx.Response(200, content=b""),
            httpx.Response(200, content=_whole(tool_name="think")),
        ])
        with patch.dict(os.environ, BASE_ENV), patch.object(
                provider_module.httpx, "AsyncClient", _client(recorder)):
            completion = _call_complete_tools(_noop_sink())
        self.assertEqual(len(recorder.requests), 2)
        self.assertEqual([call.name for call in completion.tool_calls], ["think"])


class LengthTest(unittest.TestCase):
    """`finish_reason == "length"` 在流式路径上同样是硬失败。"""

    def test_a_truncated_tool_call_is_not_executed(self):
        recorder = _Recorder([httpx.Response(200, content=_sse([
            {"delta": {"tool_calls": [{"index": 0, "id": "c-1",
                                       "function": {"name": "add_node",
                                                    "arguments": '{"label": "预'}}]}},
            {"finish_reason": "length"},
        ]))])
        with patch.dict(os.environ, BASE_ENV), patch.object(
                provider_module.httpx, "AsyncClient", _client(recorder)):
            with self.assertRaises(provider_module.GenerationError) as caught:
                _call_complete_tools(_noop_sink())
        self.assertIn("长度上限截断", str(caught.exception))
        # 硬失败不重试：再发一次只会再花一次钱，结论一样
        self.assertEqual(len(recorder.requests), 1)


class StreamChunkTest(unittest.TestCase):
    """`_stream_chunk` 的键始终齐全——两条出口都靠这个形状。"""

    def test_every_key_is_always_there(self):
        from app.generation.service import _stream_chunk

        chunk = _stream_chunk(json.dumps({"choices": [{"delta": {"reasoning": "想"}}]}))
        self.assertEqual(chunk["thought"], "想")
        self.assertEqual(chunk["text"], "")
        self.assertIsNone(chunk["reason"])
        self.assertEqual(chunk["tool_calls"], [])
        self.assertEqual(chunk["usage"], {})

    def test_usage_and_model_are_read_from_the_envelope(self):
        from app.generation.service import _stream_chunk

        chunk = _stream_chunk(json.dumps({
            "model": "m-1",
            "choices": [{"delta": {}}],
            "usage": {"total_tokens": 7},
        }))
        self.assertEqual(chunk["model"], "m-1")
        self.assertEqual(chunk["usage"], {"total_tokens": 7})


# ---------------------------------------------------------------- 端到端


def _noop_sink():
    async def sink(_text: str) -> None:
        return None
    return sink


class _EchoArgs(__import__("pydantic").BaseModel):
    pass


class ToolSelectionThinkingTest(unittest.TestCase):
    """工具选择轮真的会出 `thinking` 帧（这条是 PR#31 存在的理由）。"""

    def test_the_tool_selection_round_reports_its_thoughts(self):
        async def think(args, ctx):
            return ToolResult(ok=True, content="想好了")

        registry = ToolRegistry()
        registry.register(ToolSpec(name="think", description="想", args_model=_EchoArgs, fn=think))

        def handler(request: httpx.Request) -> httpx.Response:
            # 每轮都要求再调一次 `think`：循环的模型调用额度会把它停住，
            # 中间那些轮次的思考帧就是这条用例要看的东西。
            return httpx.Response(200, content=_sse([
                {"delta": {"reasoning": "先想想再动手"}},
                {"delta": {"tool_calls": [{"index": 0, "id": "c-1",
                                           "function": {"name": "think", "arguments": "{}"}}]}},
                {"finish_reason": "tool_calls"},
            ]))

        request = AgentRunRequest(
            goal=TOPIC, intent="create",
            budget=RunBudget(max_model_calls=3, max_tool_calls=3))

        async def drain():
            return [event async for event in run(
                request, provider=provider_module.default_provider(), tools=registry)]

        with patch.dict(os.environ, BASE_ENV), patch.object(
                provider_module.httpx, "AsyncClient", _client(handler)):
            events = _run_with_timeout(drain())

        thinking = [event for event in events if isinstance(event, ThinkingEvent)]
        self.assertTrue(thinking, "工具选择轮必须能出思考帧")
        self.assertEqual(thinking[0].text, "先想想再动手")
        for frame in thinking:
            self.assertLessEqual(len(frame.text), THINKING_TAIL_CHARS)
        # seq 仍然连续：思考帧也占号
        self.assertEqual([event.seq for event in events], list(range(1, len(events) + 1)))


def _run_with_timeout(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=60))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
