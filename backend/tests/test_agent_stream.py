"""SSE 路由与帧编码的测试。

`run()` 在这里被替换成脚本化的假生成器：要验的是帧格式、开流前后的错误分流、
以及终态一定以 `[DONE]` 收尾，不是再验一遍循环逻辑（那份在 test_agent_loop）。
"""
from __future__ import annotations

import json
import sqlite3
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent.contracts import (
    DoneEvent,
    StageEvent,
    ToolEndEvent,
    ToolStartEvent,
)
from app.agent.router import router
from app.agent.stream import encode_event
from app.contracts.source import Source, SourceKind
from app.core.errors import register_error_handlers
from app.generation.service import GenerationError

app = FastAPI()
register_error_handlers(app)
app.include_router(router, prefix="/api")
client = TestClient(app)

URL = "/api/agent/runs/stream"


def _frames(text: str) -> list[dict]:
    """把响应体拆成帧列表，跳过 [DONE]。"""
    out: list[dict] = []
    for block in text.split("\n\n"):
        line = block.strip()
        if not line.startswith("data: "):
            continue
        payload = line[6:].strip()
        if payload == "[DONE]":
            continue
        out.append(json.loads(payload))
    return out


async def _scripted(*events):
    for event in events:
        yield event


class EncodeTest(unittest.TestCase):
    def test_frame_shape(self):
        frame = encode_event(StageEvent(seq=1, stage="准备", detail="测试"))
        self.assertTrue(frame.startswith("data: "))
        self.assertTrue(frame.endswith("\n\n"))
        payload = json.loads(frame[6:].strip())
        self.assertEqual(payload["type"], "stage")
        self.assertEqual(payload["seq"], 1)

    def test_chinese_is_not_escaped(self):
        """中文原样传输：体积小一半，日志里也能直接读。"""
        frame = encode_event(StageEvent(seq=1, stage="准备"))
        self.assertIn("准备", frame)

    def test_none_fields_are_dropped(self):
        """可选字段不出现，前端少一层"本来没有 vs 被清空"的判断。"""
        payload = json.loads(encode_event(StageEvent(seq=1, stage="x"))[6:].strip())
        self.assertNotIn("content", payload)


class StreamRouteTest(unittest.TestCase):
    def test_normal_stream_ends_with_done_and_marker(self):
        events = [
            StageEvent(seq=1, stage="准备"),
            ToolStartEvent(seq=2, call_id="c1", name="mindmap_generate"),
            ToolEndEvent(seq=3, call_id="c1", name="mindmap_generate", ok=True),
            DoneEvent(seq=4),
        ]
        with patch("app.agent.router.run", return_value=_scripted(*events)):
            response = client.post(URL, json={"goal": "做一张导图", "intent": "create"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.text.rstrip().endswith("data: [DONE]"))
        frames = _frames(response.text)
        self.assertEqual([frame["type"] for frame in frames],
                         ["stage", "tool_start", "tool_end", "done"])

    def test_generation_error_inside_stream_becomes_error_frame(self):
        """开流之后上游失败：必须变成一帧，而不是断掉的连接。"""
        async def boom():
            yield StageEvent(seq=1, stage="准备")
            raise GenerationError("模型服务不可用")

        with patch("app.agent.router.run", return_value=boom()):
            response = client.post(URL, json={"goal": "x", "intent": "create"})
        frames = _frames(response.text)
        self.assertEqual(frames[-1]["type"], "error")
        self.assertEqual(frames[-1]["code"], "generation_failed")
        self.assertTrue(response.text.rstrip().endswith("data: [DONE]"))

    def test_unexpected_exception_is_also_a_frame(self):
        async def boom():
            raise RuntimeError("内部错误")
            yield  # pragma: no cover

        with patch("app.agent.router.run", return_value=boom()):
            response = client.post(URL, json={"goal": "x", "intent": "create"})
        frames = _frames(response.text)
        self.assertEqual(frames[-1]["type"], "error")
        self.assertEqual(frames[-1]["code"], "agent_failed")
        self.assertIn("RuntimeError", frames[-1]["message"])

    def test_agent_disabled_returns_503_before_stream(self):
        with patch.dict("os.environ", {"MINDMAP_AGENT_ENABLED": "0"}):
            response = client.post(URL, json={"goal": "x", "intent": "create"})
        self.assertEqual(response.status_code, 503)

    def test_create_with_snapshot_is_rejected(self):
        snapshot = {"title": "示例", "root": {"id": "root", "label": "根", "children": []}}
        response = client.post(URL, json={"goal": "x", "intent": "create", "snapshot": snapshot})
        self.assertEqual(response.status_code, 422)

    def test_expand_without_snapshot_is_rejected(self):
        response = client.post(URL, json={"goal": "x", "intent": "expand", "selected_ids": ["a"]})
        self.assertEqual(response.status_code, 422)

    def test_expand_requires_exactly_one_selection(self):
        snapshot = {"title": "示例", "root": {"id": "root", "label": "根", "children": []}}
        response = client.post(URL, json={
            "goal": "x", "intent": "expand", "snapshot": snapshot, "selected_ids": [],
        })
        self.assertEqual(response.status_code, 422)

    def test_empty_goal_is_rejected(self):
        response = client.post(URL, json={"goal": "", "intent": "create"})
        self.assertEqual(response.status_code, 422)


class LegacyRouteTest(unittest.TestCase):
    def test_old_tasks_route_still_works(self):
        """旧的阶段规划桩必须还能用：它被 test_skeleton 断言着。"""
        response = client.post("/api/agent/tasks", json={"goal": "做一个产品"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["implemented"])


if __name__ == "__main__":
    unittest.main()


class SourcesFrameTest(unittest.TestCase):
    """材料入库后先发一帧 `sources`，把临时 id 换成持久 id。

    这一帧必须排在 `run()` 的产物之前：前端要在收到任何 `node_grow` / `snapshot`
    之前就知道"手上的来源 id 变了"，否则第一张图里的引用就已经指错。
    """

    def test_sources_frame_comes_first_and_carries_the_mapping(self):
        source = Source(kind=SourceKind.text, status="parsed", text="# 材料\n\n正文若干。")
        seen: list = []
        kwargs_seen: dict = {}

        async def fake_run(body, **kwargs):
            # _frames 用 first_seq 让编号接着 sources 那帧往下走。
            kwargs_seen.update(kwargs)
            seen.append(body)
            yield DoneEvent(seq=kwargs.get("first_seq", 1))

        with patch("app.agent.router.run", side_effect=fake_run):
            response = client.post(URL, json={
                "goal": "整理材料", "intent": "create",
                "sources": [source.model_dump(mode="json")],
            })

        frames = _frames(response.text)
        self.assertEqual(frames[0]["type"], "sources")
        self.assertEqual(list(frames[0]["mapping"]), [source.id])
        # 换过 id 的 request 才交给循环：库里记的是新 id，导图引用要对得上。
        self.assertEqual([item.id for item in seen[0].sources], [frames[0]["mapping"][source.id]])
        # 编号必须接着 sources 往下走：两帧都叫 seq=1 时，按 seq 排序的客户端会乱序。
        self.assertEqual([frame["seq"] for frame in frames], [1, kwargs_seen["first_seq"]])

    def test_no_sources_frame_when_there_is_nothing_to_remap(self):
        with patch("app.agent.router.run", return_value=_scripted(DoneEvent(seq=1))):
            response = client.post(URL, json={"goal": "做一张导图", "intent": "create"})

        self.assertNotIn("sources", [frame["type"] for frame in _frames(response.text)])

    def test_stream_still_ends_with_done_when_ingest_fails(self):
        """入库失败不能把流打断：材料照样用，只是没进库。"""
        source = Source(kind=SourceKind.text, status="parsed", text="# 材料\n\n正文。")

        def boom(sources):
            raise sqlite3.OperationalError("database is locked")

        with patch("app.agent.router.remember_sources", boom):
            with patch("app.agent.router.run", return_value=_scripted(DoneEvent(seq=1))):
                response = client.post(URL, json={
                    "goal": "整理", "intent": "create",
                    "sources": [source.model_dump(mode="json")],
                })

        self.assertEqual(response.status_code, 200)
        frames = _frames(response.text)
        # 入库抛的是 sqlite3 错误，被包成 error 帧后仍然以尾。
        self.assertEqual(frames[-1]["type"], "error")
        self.assertTrue(response.text.rstrip().endswith("data: [DONE]"))
