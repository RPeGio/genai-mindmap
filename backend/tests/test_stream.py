"""流式生成（增量解析方案）的事件序列与 HTTP 层行为。

不访问真实模型：demo 分支直接可用，model 分支把 `_stream_chat` 换成同步假生成器。
覆盖三件流式特有的麻烦事——帧序不能乱、失败必须进流里、光杆树不能当成功。
"""
import asyncio
import json
import os
import unittest
from unittest.mock import patch

import app.generation.stream_partial as partial_mod
from app.generation.service import GenerationError
from app.generation.stream_partial import generate_stream_partial
from app.main import app


def collect(agen) -> list[str]:
    """把异步生成器一次性收成列表。"""
    async def run():
        return [frame async for frame in agen]
    return asyncio.run(run())


def parse_frames(frames: list[str]) -> list[tuple[str, dict | None]]:
    """把 SSE 帧解析成 [(type, payload)]；终止帧记作 __done__。"""
    parsed: list[tuple[str, dict | None]] = []
    for frame in frames:
        text = frame.strip()
        if not text.startswith("data: "):
            continue
        body = text[6:]
        if body == "[DONE]":
            parsed.append(("__done__", None))
            continue
        event = json.loads(body)
        parsed.append((event["type"], event))
    return parsed


def asgi_request(method: str, path: str, body=None):
    """最小 ASGI 调用器，返回 (status, headers, body)。"""
    payload = b"" if body is None else json.dumps(body, ensure_ascii=False).encode()
    path_only, _, query = path.partition("?")

    async def run():
        messages = []
        calls = {"n": 0}

        async def receive():
            # 第一次交付请求体；之后挂起不返回。StreamingResponse 会并发监听
            # http.disconnect，如果这里立刻回一条 request 消息，那个监听会变成
            # 忙循环，测试看起来像卡死。
            calls["n"] += 1
            if calls["n"] == 1:
                return {"type": "http.request", "body": payload, "more_body": False}
            await asyncio.sleep(3600)
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)

        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": method, "path": path_only, "raw_path": path_only.encode(),
            "query_string": query.encode(), "root_path": "", "scheme": "http",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 1234), "server": ("127.0.0.1", 8000),
        }
        await app(scope, receive, send)
        status = next(m["status"] for m in messages if m["type"] == "http.response.start")
        headers = {k.decode().lower(): v.decode()
                   for k, v in next(m["headers"] for m in messages
                                    if m["type"] == "http.response.start")}
        raw = b"".join(m.get("body", b"") for m in messages
                       if m["type"] == "http.response.body")
        return status, headers, raw

    return asyncio.run(run())


class PartialStreamTest(unittest.TestCase):
    """增量解析方案的事件序列与结构校验。"""

    def _run(self, payload: str):
        async def fake_stream(_system: str, _user: str):
            yield payload

        with patch.dict(os.environ, {"MINDMAP_LLM_MODE": "model"}), \
             patch.object(partial_mod, "_stream_chat", new=fake_stream):
            return parse_frames(collect(generate_stream_partial("x")))

    def test_demo_frame_order(self):
        """demo 分支：stage → skeleton → done → [DONE]，且不调用模型。"""
        with patch.dict(os.environ, {"MINDMAP_LLM_MODE": "demo"}), \
             patch.object(partial_mod, "_stream_chat") as chat_mock:
            parsed = parse_frames(collect(generate_stream_partial("测试输入")))
        self.assertEqual([k for k, _ in parsed], ["stage", "skeleton", "done", "__done__"])
        self.assertEqual(parsed[0][1]["stage"], "demo")
        chat_mock.assert_not_called()

    def test_single_node_is_rejected(self):
        """只给一个根节点不算生成成功 —— F01 要的是层次结构。"""
        kinds = [k for k, _ in self._run('{"title":"X","root":{"id":"r","label":"根"}}')]
        self.assertIn("error", kinds)
        self.assertNotIn("done", kinds)
        self.assertEqual(kinds[-1], "__done__")

    def test_single_branch_is_accepted(self):
        """只有一条分支也算层次结构，与同步接口保持一致（下限是 1）。"""
        payload = ('{"title":"X","root":{"id":"r","label":"根","children":['
                   '{"id":"b1","label":"唯一","children":[]}]}}')
        kinds = [k for k, _ in self._run(payload)]
        self.assertIn("done", kinds)
        self.assertNotIn("error", kinds)

    def test_two_branches_pass(self):
        payload = ('{"title":"X","root":{"id":"r","label":"根","children":['
                   '{"id":"b1","label":"甲","children":[]},'
                   '{"id":"b2","label":"乙","children":[]}]}}')
        parsed = self._run(payload)
        done = next(p for k, p in parsed if k == "done")
        self.assertFalse(done["degraded"])
        self.assertEqual(len(done["content"]["root"]["children"]), 2)

    def test_bad_mode_returns_error_frame(self):
        with patch.dict(os.environ, {"MINDMAP_LLM_MODE": "nonsense"}):
            kinds = [k for k, _ in parse_frames(collect(generate_stream_partial("x")))]
        self.assertIn("error", kinds)
        self.assertEqual(kinds[-1], "__done__")

    def test_stream_error_keeps_done_frame(self):
        """模型调用失败也要以 [DONE] 收尾，不允许静默断连。"""
        async def boom(_system: str, _user: str):
            raise GenerationError("模型服务返回 HTTP 401")
            yield  # pragma: no cover - 让函数成为生成器

        with patch.dict(os.environ, {"MINDMAP_LLM_MODE": "model"}), \
             patch.object(partial_mod, "_stream_chat", new=boom):
            parsed = parse_frames(collect(generate_stream_partial("x")))
        kinds = [k for k, _ in parsed]
        self.assertIn("error", kinds)
        self.assertEqual(kinds[-1], "__done__")


class PartialParserTest(unittest.TestCase):
    """增量 JSON 解析与节点去重。"""

    def test_parse_partial_closes_brackets(self):
        raw = '{"title":"X","root":{"id":"r","label":"根","children":[{"id":"b1","label":"分'
        obj = partial_mod.parse_partial(raw)
        self.assertIsNotNone(obj)
        nodes = partial_mod.collect_new_nodes(obj, {}, raw)
        # 根节点 id 与 label 都闭合，可以推；b1 的 label 还没写完，不推。
        self.assertEqual([n[1]["id"] for n in nodes], ["r"])

    def test_unparseable_fragment_returns_none(self):
        self.assertIsNone(partial_mod.parse_partial('{"title":"X","root":{"label":'))

    def test_truncated_id_is_not_pushed(self):
        """label 先于 id 输出时，半截 id（b）不该被当成一个节点推出去。"""
        seen: dict[str, str] = {}
        pushed: list[str] = []
        buffer = ""
        for piece in ['{"title":"X","root":{"id":"root","label":"根","children":[',
                      '{"label":"监督学习","id":"b',
                      '1","children":[]}]}}']:
            buffer += piece
            obj = partial_mod.parse_partial(buffer)
            if not obj:
                continue
            for _, node in partial_mod.collect_new_nodes(obj, seen, buffer):
                pushed.append(node["id"])
        self.assertNotIn("b", pushed)
        self.assertIn("b1", pushed)

    def test_colliding_id_does_not_overwrite_existing_node(self):
        """已有 id b，新节点 id b1 只输出到 b 时，不能把文字写到旧节点上。"""
        seen = {"b": "分支甲"}
        obj = {"root": {"id": "r", "label": "根", "children": [
            {"id": "b", "label": "分支甲", "children": []},
            {"id": "b", "label": "监", "children": []},
        ]}}
        picked = partial_mod.collect_new_nodes(obj, seen, '{"id":"b","label":"分支甲"}')
        self.assertEqual(picked, [])
        self.assertEqual(seen["b"], "分支甲")

    def test_existing_node_is_not_repushed(self):
        """已经推过的节点不再重复推：文字一次成型，不做逐字更新。"""
        seen = {"b": "监督"}
        obj = {"root": {"id": "r", "label": "根", "children": [
            {"id": "b", "label": "监督学习", "children": []},
        ]}}
        picked = partial_mod.collect_new_nodes(obj, seen, '{"id":"b","label":"监督学习"}')
        self.assertEqual(picked, [])


class StreamEndpointTest(unittest.TestCase):
    """HTTP 层：响应头与错误路径。"""

    def test_sse_response_headers(self):
        with patch.dict(os.environ, {"MINDMAP_LLM_MODE": "demo"}):
            status, headers, raw = asgi_request("POST", "/api/generations/stream",
                                                {"text": "测试"})
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", headers["content-type"])
        self.assertEqual(headers["x-accel-buffering"], "no")
        text = raw.decode()
        self.assertTrue(text.startswith("data: "))
        self.assertTrue(text.rstrip().endswith("data: [DONE]"))

    def test_validation_failure_returns_json_not_stream(self):
        """流还没开始就校验失败，应该走统一错误体而不是 SSE。"""
        status, headers, raw = asgi_request("POST", "/api/generations/stream", {})
        self.assertEqual(status, 422)
        self.assertIn("application/json", headers["content-type"])
        body = json.loads(raw)
        self.assertEqual(body["code"], "validation_error")
        self.assertTrue(body["request_id"])


if __name__ == "__main__":
    unittest.main()
