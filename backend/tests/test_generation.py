import json
import os
import unittest
from unittest.mock import patch
import httpx
from app.generation.service import GenerationError, generate


class MockStreamResponse:
    """模拟 httpx 流式响应：既是上下文管理器，也按块吐字节。"""
    def __init__(self, data, status_code=200):
        self.data = data
        self.status_code = status_code
        self.request = httpx.Request("POST", "https://example.com/v1/chat/completions")
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(f"HTTP {self.status_code}", request=self.request,
                                        response=httpx.Response(self.status_code, request=self.request))
    def iter_bytes(self):
        yield self.data


class MockClient:
    """模拟 httpx.Client：记录调用参数，返回预设响应或抛出预设异常。"""
    def __init__(self, response):
        self.response = response
        self.calls = []
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def stream(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class GenerationTest(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"MINDMAP_LLM_MODE": "model", "MINDMAP_LLM_BASE_URL": "https://example.com/v1", "MINDMAP_LLM_MODEL": "test", "MINDMAP_LLM_API_KEY": "not-a-real-key"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def use_client(self, response):
        """把 service 里的 httpx.Client 换成假客户端，返回该 patch 对象。"""
        patcher = patch("app.generation.service.httpx.Client", return_value=MockClient(response))
        client_class = patcher.start()
        self.addCleanup(patcher.stop)
        return client_class

    @staticmethod
    def body_of(tree):
        return json.dumps({"choices": [{"message": {"content": json.dumps(tree)}}]}).encode()

    def test_model_response(self):
        tree = {"title": "测试", "root": {"id": "1", "label": "测试", "children": []}}
        client_class = self.use_client(MockStreamResponse(self.body_of(tree)))
        result = generate("测试")
        self.assertEqual(result.mode, "model")
        self.assertEqual(result.content.root.label, "测试")
        self.assertEqual(client_class.call_args.kwargs["timeout"], 60.0)
        method, url, kwargs = client_class.return_value.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://example.com/v1/chat/completions")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer not-a-real-key")

    def test_invalid_model_response_is_not_treated_as_success(self):
        self.use_client(MockStreamResponse(b'{"choices":[{"message":{"content":"not json"}}]}'))
        with self.assertRaises(GenerationError) as raised:
            generate("测试")
        self.assertIn("不是符合约定的导图 JSON", str(raised.exception))

    def test_model_cannot_claim_unverified_source_metadata(self):
        tree = {"title": "测试", "root": {"id": "1", "label": "测试", "children": []},
                "source_usage": [{"source_id": "fake", "kind": "text", "filename": None, "char_count": 3}]}
        self.use_client(MockStreamResponse(self.body_of(tree)))
        self.assertEqual(generate("测试").content.source_usage, [])

    def test_model_service_http_error_is_reported(self):
        self.use_client(MockStreamResponse(b"", status_code=503))
        with self.assertRaises(GenerationError) as raised:
            generate("测试")
        self.assertIn("HTTP 503", str(raised.exception))

    def test_model_connection_error_is_reported(self):
        self.use_client(httpx.ConnectTimeout("连接超时"))
        with self.assertRaises(GenerationError) as raised:
            generate("测试")
        self.assertIn("无法连接模型服务或请求超时", str(raised.exception))

    def test_oversized_model_response_is_rejected(self):
        self.use_client(MockStreamResponse(b"x" * 2_000_001))
        with self.assertRaises(GenerationError) as raised:
            generate("测试")
        self.assertIn("模型响应过大", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
