"""骨架冒烟测试：验证新增模块的流程能走通、桩接口按约定返回。

不访问模型、不写真实数据库（用 demo 模式 + 临时库）。
"""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.main import app
from app.generation.service import GenerationError
from app.ingestion.service import NotSupportedSource, parse_bytes
from app.contracts.source import SourceKind

BACKEND = Path(__file__).resolve().parents[1]

MAP = {
    "title": "测试导图",
    "root": {
        "id": "root",
        "label": "根",
        "children": [
            {"id": "n1", "label": "重复项", "children": []},
            {"id": "n2", "label": "重复项", "children": []},
        ],
    },
}


def request(method: str, path: str, body=None):
    """最小 ASGI 调用器，避免额外依赖。"""
    import json as _json

    payload = b"" if body is None else _json.dumps(body, ensure_ascii=False).encode()
    path_only, _, query = path.partition("?")

    async def run():
        messages = []

        async def receive():
            return {"type": "http.request", "body": payload, "more_body": False}

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
        raw = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
        return status, raw

    return asyncio.run(run())


class SkeletonTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        data = BACKEND / "data"
        data.mkdir(exist_ok=True)
        cls.tmp = tempfile.TemporaryDirectory(dir=data, ignore_cleanup_errors=True)
        cls.env = patch.dict(os.environ, {
            "MINDMAP_DB_PATH": str(Path(cls.tmp.name) / "skeleton.db"),
            "MINDMAP_LLM_MODE": "demo",
        })
        cls.env.start()

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.tmp.cleanup()

    def test_health(self):
        status, raw = request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertIn(b'"status":"ok"', raw)

    def test_text_source_then_pipeline(self):
        """文本来源 → 四段流水线 → 导图 + 阶段轨迹。"""
        status, raw = request("POST", "/api/ingestion/text", {"text": "人工智能的应用"})
        self.assertEqual(status, 200)
        source = json.loads(raw)["source"]
        self.assertEqual(source["status"], "parsed")
        status, raw = request("POST", "/api/pipeline/generate", {"sources": [source]})
        self.assertEqual(status, 200)
        content = json.loads(raw)["content"]
        self.assertEqual(content["source_usage"][0]["source_id"], source["id"])
        status, saved_raw = request("POST", "/api/mindmaps", {"content": content})
        self.assertEqual(status, 201)
        saved = json.loads(saved_raw)
        status, reloaded = request("GET", "/api/mindmaps/" + saved["id"])
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(reloaded)["content"]["source_usage"], content["source_usage"])

    def test_pipeline_returns_stages(self):
        source = {"kind": "text", "text": "人工智能的应用", "status": "parsed"}
        status, raw = request("POST", "/api/pipeline/generate", {"sources": [source]})
        self.assertEqual(status, 200)
        body = raw.decode()
        for stage in ("understand", "outline", "validate", "verify"):
            self.assertIn(stage, body)
        self.assertIn('"mode":"demo"', body)

    def test_pipeline_records_every_accepted_source(self):
        sources = [
            {"id": "one", "kind": "text", "text": "第一份", "status": "parsed"},
            {"id": "two", "kind": "markdown", "text": "第二份材料", "status": "parsed"},
        ]
        status, raw = request("POST", "/api/pipeline/generate", {"sources": sources})
        self.assertEqual(status, 200)
        usage = json.loads(raw)["content"]["source_usage"]
        self.assertEqual([(item["source_id"], item["char_count"]) for item in usage],
                         [("one", 3), ("two", 5)])

    def test_pipeline_rejects_empty_sources(self):
        status, _ = request("POST", "/api/pipeline/generate",
                            {"sources": [{"kind": "text", "status": "failed"}]})
        self.assertEqual(status, 422)

    def test_pipeline_rejects_truncation_and_unparsed_sources(self):
        sources = [
            {"id": "one", "kind": "text", "text": "a" * 20001, "status": "parsed"},
            {"id": "two", "kind": "text", "text": "important second source", "status": "parsed"},
        ]
        status, raw = request("POST", "/api/pipeline/generate", {"sources": sources})
        self.assertEqual(status, 422)
        self.assertIn("超过", json.loads(raw)["detail"])
        sources[0]["text"] = "short"
        sources[1]["status"] = "failed"
        status, raw = request("POST", "/api/pipeline/generate", {"sources": sources})
        self.assertEqual(status, 422)
        self.assertIn("第 2 个来源", json.loads(raw)["detail"])
        sources[1]["status"] = "parsed"
        sources[1]["id"] = "one"
        status, raw = request("POST", "/api/pipeline/generate", {"sources": sources})
        self.assertEqual(status, 422)
        self.assertIn("来源 ID 不能重复", json.loads(raw)["detail"])

    def test_pipeline_reports_provider_failure_as_upstream_error(self):
        source = {"kind": "text", "text": "有效文本", "status": "parsed"}
        with patch("app.pipeline.service.generate", side_effect=GenerationError("服务暂时不可用")):
            status, raw = request("POST", "/api/pipeline/generate", {"sources": [source]})
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(raw)["detail"], "服务暂时不可用")

    def test_optimization_detects_duplicate(self):
        status, raw = request("POST", "/api/optimization/check", {"content": MAP})
        self.assertEqual(status, 200)
        self.assertIn("DUPLICATE_LABEL", raw.decode())
        status, raw = request("POST", "/api/optimization/suggest", {"content": MAP})
        suggestion = json.loads(raw)["suggestions"][0]
        self.assertEqual(suggestion["evidence"], "n1")
        self.assertIsNone(suggestion["confidence"])

    def test_repeated_label_under_different_parents_is_not_flagged(self):
        content = {"title": "学科", "root": {"id": "r", "label": "学科", "children": [
            {"id": "a", "label": "物理", "children": [{"id": "a1", "label": "应用", "children": []}]},
            {"id": "b", "label": "数学", "children": [{"id": "b1", "label": "应用", "children": []}]},
        ]}}
        status, raw = request("POST", "/api/optimization/suggest", {"content": content})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["suggestions"], [])

    def test_export_puml(self):
        status, raw = request("POST", "/api/export/puml", {"content": MAP})
        self.assertEqual(status, 200)
        text = raw.decode()
        self.assertTrue(text.startswith("@startmindmap"))
        self.assertIn("@endmindmap", text)
        self.assertIn("* 根\n", text)
        self.assertNotIn("* 测试导图\n", text)

    def test_knowledge_reports_not_implemented(self):
        status, raw = request("POST", "/api/knowledge/search", {"query": "碳中和"})
        self.assertEqual(status, 200)
        self.assertIn('"implemented":false', raw.decode())

    def test_agent_returns_plan_without_map(self):
        status, raw = request("POST", "/api/agent/tasks", {"goal": "设计一款智能家居产品"})
        self.assertEqual(status, 200)
        body = raw.decode()
        self.assertIn('"implemented":false', body)
        self.assertNotIn('"root"', body)

    def test_file_upload_requires_actual_file(self):
        """文件入口改成 multipart 后，不带文件不能假装成功。"""
        status, _ = request("POST", "/api/ingestion/files", {})
        self.assertEqual(status, 422)

    def test_text_endpoint_rejects_image_kind(self):
        status, _ = request("POST", "/api/ingestion/text", {"text": "内容", "kind": "image"})
        self.assertEqual(status, 422)

    def test_bytes_parser_rejects_blank_text(self):
        with self.assertRaises(NotSupportedSource):
            parse_bytes(SourceKind.text, "empty.txt", b"  ")


if __name__ == "__main__":
    unittest.main()
