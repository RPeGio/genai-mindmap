"""通过 ASGI 请求验证演示生成和持久化，不访问第三方模型。"""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from app.main import app

BACKEND = Path(__file__).resolve().parents[1]


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        data = BACKEND / "data"
        data.mkdir(exist_ok=True)
        cls.tmp = tempfile.TemporaryDirectory(dir=data, ignore_cleanup_errors=True)
        cls.env = patch.dict(os.environ, {"MINDMAP_DB_PATH": str(Path(cls.tmp.name) / "test.db"),
                                         "MINDMAP_LLM_MODE": "demo"})
        cls.env.start()

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.tmp.cleanup()

    def call(self, path, method="GET", data=None):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else b""

        async def run():
            messages = []
            delivered = False

            async def receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": payload, "more_body": False}
                return {"type": "http.disconnect"}

            async def send(message):
                messages.append(message)

            scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                     "method": method, "scheme": "http", "path": "/api" + path,
                     "raw_path": ("/api" + path).encode(), "query_string": b"",
                     "headers": [(b"content-type", b"application/json")],
                     "client": ("127.0.0.1", 1), "server": ("test", 80)}
            await app(scope, receive, send)
            status = next(m["status"] for m in messages if m["type"] == "http.response.start")
            body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
            return status, json.loads(body)

        return asyncio.run(run())

    def test_generate_save_load_update_and_conflict(self):
        status, generated = self.call("/generations", "POST", {"text": "机器学习"})
        self.assertEqual((status, generated["mode"]), (200, "demo"))
        content = generated["content"]
        self.assertEqual(content["root"]["label"], "机器学习")
        status, saved = self.call("/mindmaps", "POST", {"content": content})
        self.assertEqual(status, 201)
        map_id = saved["id"]
        status, loaded = self.call("/mindmaps/" + map_id)
        self.assertEqual((status, loaded["content"]), (200, content))
        content["root"]["label"] = "已编辑"
        status, updated = self.call("/mindmaps/" + map_id, "PUT", {"version": 1, "content": content})
        self.assertEqual((status, updated["version"]), (200, 2))
        # 冲突时错误信息要能看见（统一错误格式的 code / message）
        status, conflict = self.call("/mindmaps/" + map_id, "PUT", {"version": 1, "content": content})
        self.assertEqual((status, conflict["code"]), (409, "version_conflict"))
        self.assertEqual(conflict["message"], "导图已被修改，请重新打开后再保存")
        self.assertEqual(self.call("/mindmaps/" + map_id)[1]["content"]["root"]["label"], "已编辑")

    def test_validation_and_not_found(self):
        self.assertEqual(self.call("/generations", "POST", {"text": "   "})[0], 422)
        bad = {"title": "重复", "root": {"id": "x", "label": "根", "children": [
            {"id": "x", "label": "子", "children": []}]}}
        self.assertEqual(self.call("/mindmaps", "POST", {"content": bad})[0], 422)
        self.assertEqual(self.call("/mindmaps/does-not-exist")[0], 404)


if __name__ == "__main__":
    unittest.main()
