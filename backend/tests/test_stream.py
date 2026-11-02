"""流式生成的事件序列与 HTTP 层行为。

不访问真实模型：demo 分支直接可用，model 分支把两段生成函数 patch 掉。
覆盖三件流式特有的麻烦事——帧序不能乱、失败必须进流里、骨架不能被展开失败毁掉。
"""
import asyncio
import json
import os
import time
import unittest
from unittest.mock import AsyncMock, patch

import app.generation.stream as stream_mod
import app.generation.stream_partial as partial_mod
from app.contracts.mindmap import MindMapContent, Node
from app.generation.service import GenerationError
from app.generation.stream import generate_stream
from app.main import app

SKELETON = MindMapContent(
    title="测试导图",
    root=Node(id="root", label="根", children=[
        Node(id="b1", label="分支一"),
        Node(id="b2", label="分支二"),
    ]),
)

EXPANDED = MindMapContent(
    title="测试导图",
    root=Node(id="root", label="根", children=[
        Node(id="b1", label="分支一", children=[Node(id="b1a", label="子节点")]),
        Node(id="b2", label="分支二"),
    ]),
)


def collect(agen) -> list[str]:
    """把异步生成器一次性收成列表，测试里不必到处写 asyncio.run。"""
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
    """最小 ASGI 调用器，返回 (status, headers, body)。

    流式响应会分多条 http.response.body 消息下发，这里全部拼接。
    """
    payload = b"" if body is None else json.dumps(body, ensure_ascii=False).encode()
    path_only, _, query = path.partition("?")

    async def run():
        messages = []

        calls = {"n": 0}

        async def receive():
            # 第一次交付请求体；之后挂起不返回。Starlette 的 StreamingResponse
            # 会并发监听 http.disconnect，如果这里立刻回一条 request 消息，
            # 那个监听会变成忙循环，测试看起来像卡死。
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


class StreamEventTest(unittest.TestCase):
    def test_demo_frame_order(self):
        """demo 分支：stage → skeleton → done → [DONE]，且不调用模型。"""
        with patch.dict(os.environ, {"MINDMAP_LLM_MODE": "demo"}), \
             patch.object(stream_mod, "build_skeleton", new=AsyncMock()) as build_mock:
            parsed = parse_frames(collect(generate_stream("测试输入")))

        self.assertEqual([k for k, _ in parsed], ["stage", "skeleton", "done", "__done__"])
        self.assertEqual(parsed[0][1]["stage"], "demo")
        self.assertFalse(parsed[2][1]["degraded"])
        build_mock.assert_not_called()

    def test_model_skeleton_precedes_done(self):
        """骨架帧必须早于 done，且骨架里的一级分支没有子节点。"""
        with patch.dict(os.environ, {"MINDMAP_LLM_MODE": "model"}), \
             patch.object(stream_mod, "build_skeleton", new=AsyncMock(return_value=SKELETON)), \
             patch.object(stream_mod, "expand_skeleton", new=AsyncMock(return_value=EXPANDED)):
            parsed = parse_frames(collect(generate_stream("x")))

        kinds = [k for k, _ in parsed]
        self.assertEqual(kinds[-1], "__done__")
        self.assertLess(kinds.index("skeleton"), kinds.index("done"))

        skeleton_payload = parsed[kinds.index("skeleton")][1]["content"]
        self.assertEqual(len(skeleton_payload["root"]["children"]), 2)
        self.assertEqual(skeleton_payload["root"]["children"][0]["children"], [])

        done_payload = parsed[kinds.index("done")][1]
        self.assertFalse(done_payload["degraded"])
        self.assertEqual(len(done_payload["content"]["root"]["children"][0]["children"]), 1)

    def test_skeleton_failure_becomes_error_event(self):
        """骨架段失败：错误进流里，仍然以 [DONE] 收尾，不会静默断连。"""
        with patch.dict(os.environ, {"MINDMAP_LLM_MODE": "model"}), \
             patch.object(stream_mod, "build_skeleton",
                          side_effect=GenerationError("模型服务返回 HTTP 401")):
            parsed = parse_frames(collect(generate_stream("x")))

        kinds = [k for k, _ in parsed]
        self.assertIn("error", kinds)
        self.assertEqual(kinds[-1], "__done__")
        error = next(p for k, p in parsed if k == "error")
        self.assertIn("401", error["message"])

    def test_expand_failure_degrades_to_skeleton(self):
        """展开段失败：骨架已经在屏幕上了，退回它并标记 degraded。"""
        with patch.dict(os.environ, {"MINDMAP_LLM_MODE": "model"}), \
             patch.object(stream_mod, "build_skeleton", new=AsyncMock(return_value=SKELETON)), \
             patch.object(stream_mod, "expand_skeleton",
                          side_effect=GenerationError("展开超时")):
            parsed = parse_frames(collect(generate_stream("x")))

        done = next(p for k, p in parsed if k == "done")
        self.assertTrue(done["degraded"])
        self.assertIn("展开超时", done["reason"])
        # 退化结果是骨架：分支存在但都还没有子节点
        self.assertEqual(len(done["content"]["root"]["children"]), 2)
        self.assertEqual(done["content"]["root"]["children"][0]["children"], [])

    def test_empty_skeleton_is_rejected(self):
        """模型只给了一个光杆根节点时不往下走，直接报错。"""
        lonely = MindMapContent(title="空", root=Node(id="root", label="空"))
        with patch.dict(os.environ, {"MINDMAP_LLM_MODE": "model"}), \
             patch.object(stream_mod, "build_skeleton", new=AsyncMock(return_value=lonely)):
            parsed = parse_frames(collect(generate_stream("x")))

        kinds = [k for k, _ in parsed]
        self.assertIn("error", kinds)
        self.assertNotIn("done", kinds)
        self.assertEqual(kinds[-1], "__done__")

    def test_heartbeat_while_waiting(self):
        """等待期间要有心跳，否则前端会以为连接死了。"""
        async def slow(_text: str) -> MindMapContent:
            # mock 会把被调参数透传给 side_effect，所以这里必须接一个参数
            await asyncio.sleep(0.5)
            return SKELETON

        with patch.dict(os.environ, {"MINDMAP_LLM_MODE": "model"}), \
             patch.object(stream_mod, "_HEARTBEAT_SECONDS", 0.1), \
             patch.object(stream_mod, "build_skeleton", new=AsyncMock(side_effect=slow)), \
             patch.object(stream_mod, "expand_skeleton", new=AsyncMock(return_value=EXPANDED)):
            parsed = parse_frames(collect(generate_stream("x")))

        self.assertIn("heartbeat", [k for k, _ in parsed])
        beat = next(p for k, p in parsed if k == "heartbeat")
        self.assertGreater(beat["waitedMs"], 0)

    def test_bad_mode_returns_error_frame(self):
        """模式配置错时也走流，不让连接无声中断。"""
        with patch.dict(os.environ, {"MINDMAP_LLM_MODE": "nonsense"}):
            parsed = parse_frames(collect(generate_stream("x")))

        kinds = [k for k, _ in parsed]
        self.assertIn("error", kinds)
        self.assertEqual(kinds[-1], "__done__")


class StreamEndpointTest(unittest.TestCase):
    def test_sse_response_headers(self):
        """响应头要让浏览器与反向代理都别缓冲。"""
        with patch.dict(os.environ, {"MINDMAP_LLM_MODE": "demo"}):
            status, headers, raw = asgi_request("POST", "/api/generations/stream",
                                                {"text": "测试"})

        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", headers["content-type"])
        self.assertEqual(headers["x-accel-buffering"], "no")
        self.assertIn(b"[DONE]", raw)
        # 帧序：第一条必须是 stage，最后一条必须是终止帧
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


class PartialStreamTest(unittest.TestCase):
    """增量解析方案（默认实现）的行为。"""

    def _kinds(self, frames):
        return [k for k, _ in parse_frames(frames)]

    def _run(self, payload: str):
        async def fake_stream(_system, _user):
            yield payload

        with patch.dict(os.environ, {"MINDMAP_LLM_MODE": "model"}), \
             patch.object(partial_mod, "_stream_chat", new=fake_stream):
            return parse_frames(collect(partial_mod.generate_stream_partial("x")))

    def test_single_node_is_rejected(self):
        """只给一个根节点不算生成成功 —— F01 要的是层次结构。"""
        parsed = self._run('{"title":"X","root":{"id":"r","label":"根"}}')
        kinds = [k for k, _ in parsed]
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
        kinds = [k for k, _ in parsed]
        self.assertIn("done", kinds)
        done = next(p for k, p in parsed if k == "done")
        self.assertFalse(done["degraded"])

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

    def test_parse_partial_closes_brackets(self):
        """补齐括号的基本能力：截断在字符串中间也能解析出已有节点。"""
        raw = '{"title":"X","root":{"id":"r","label":"根","children":[{"id":"b1","label":"分'
        obj = partial_mod.parse_partial(raw)
        self.assertIsNotNone(obj)
        nodes = partial_mod.collect_new_nodes(obj, {}, raw)
        self.assertEqual([n[1]["id"] for n in nodes], ["r", "b1"])

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

    def test_label_growth_still_passes(self):
        """正常打字机：同一个 id 的文字变长要能继续推。"""
        seen = {"b": "监督"}
        obj = {"root": {"id": "r", "label": "根", "children": [
            {"id": "b", "label": "监督学习", "children": []},
        ]}}
        picked = partial_mod.collect_new_nodes(obj, seen, '{"id":"b","label":"监督学习"}')
        self.assertEqual([n[1]["label"] for n in picked], ["监督学习"])

    def test_unparseable_fragment_returns_none(self):
        """截断在 "label": 这种位置补不出合法 JSON，应返回 None 而不是猜。"""
        self.assertIsNone(partial_mod.parse_partial('{"title":"X","root":{"label":'))


class SkeletonConsistencyTest(unittest.TestCase):
    """两段式：展开结果必须保留骨架，否则退回骨架并标记降级。"""

    def test_expand_returning_other_tree_degrades(self):
        other = MindMapContent(title="另一棵树", root=Node(id="zzz", label="别的东西",
            children=[Node(id="y1", label="无关"), Node(id="y2", label="无关2")]))
        with patch.dict(os.environ, {"MINDMAP_LLM_MODE": "model"}), \
             patch.object(stream_mod, "build_skeleton", new=AsyncMock(return_value=SKELETON)), \
             patch.object(stream_mod, "expand_skeleton", new=AsyncMock(return_value=other)):
            parsed = parse_frames(collect(generate_stream("x")))
        done = next(p for k, p in parsed if k == "done")
        self.assertTrue(done["degraded"])
        self.assertEqual(done["content"]["title"], SKELETON.title)

    def test_expand_changing_root_id_degrades(self):
        """根节点 id 变了等于换了一棵树，必须降级。"""
        other_root_id = MindMapContent(title=SKELETON.title, root=Node(
            id="different-root", label=SKELETON.root.label,
            children=[Node(id="b1", label="分支一"), Node(id="b2", label="分支二")]))
        with patch.dict(os.environ, {"MINDMAP_LLM_MODE": "model"}), \
             patch.object(stream_mod, "build_skeleton", new=AsyncMock(return_value=SKELETON)), \
             patch.object(stream_mod, "expand_skeleton", new=AsyncMock(return_value=other_root_id)):
            parsed = parse_frames(collect(generate_stream("x")))
        done = next(p for k, p in parsed if k == "done")
        self.assertTrue(done["degraded"])

    def test_expand_rewriting_labels_degrades(self):
        """id 没变但分支文字被改写，同样算没保留骨架。"""
        rewritten = MindMapContent(title=SKELETON.title, root=Node(
            id=SKELETON.root.id, label=SKELETON.root.label,
            children=[Node(id="b1", label="被改写过的分支一"),
                      Node(id="b2", label="被改写过的分支二")]))
        with patch.dict(os.environ, {"MINDMAP_LLM_MODE": "model"}), \
             patch.object(stream_mod, "build_skeleton", new=AsyncMock(return_value=SKELETON)), \
             patch.object(stream_mod, "expand_skeleton", new=AsyncMock(return_value=rewritten)):
            parsed = parse_frames(collect(generate_stream("x")))
        done = next(p for k, p in parsed if k == "done")
        self.assertTrue(done["degraded"])

    def test_expand_keeping_skeleton_passes(self):
        with patch.dict(os.environ, {"MINDMAP_LLM_MODE": "model"}), \
             patch.object(stream_mod, "build_skeleton", new=AsyncMock(return_value=SKELETON)), \
             patch.object(stream_mod, "expand_skeleton", new=AsyncMock(return_value=EXPANDED)):
            parsed = parse_frames(collect(generate_stream("x")))
        done = next(p for k, p in parsed if k == "done")
        self.assertFalse(done["degraded"])
        self.assertEqual(len(done["content"]["root"]["children"][0]["children"]), 1)


if __name__ == "__main__":
    unittest.main()
