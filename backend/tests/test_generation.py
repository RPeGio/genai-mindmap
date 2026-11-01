import json
import os
import unittest
from unittest.mock import patch
from app.generation.service import GenerationError, generate


class MockResponse:
    def __init__(self, data):
        self.data = data
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self, size):
        return self.data


class GenerationTest(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"MINDMAP_LLM_MODE": "model", "MINDMAP_LLM_BASE_URL": "https://example.com/v1", "MINDMAP_LLM_MODEL": "test", "MINDMAP_LLM_API_KEY": "not-a-real-key"})
        self.env.start()
        self.addCleanup(self.env.stop)

    @patch("app.generation.service.urlopen")
    def test_model_response(self, urlopen):
        tree = {"title": "测试", "root": {"id": "1", "label": "测试", "children": []}}
        body = {"choices": [{"message": {"content": json.dumps(tree)}}]}
        urlopen.return_value = MockResponse(json.dumps(body).encode())
        result = generate("测试")
        self.assertEqual(result.mode, "model")
        self.assertEqual(result.content.root.label, "测试")
        self.assertEqual(urlopen.call_args.args[0].full_url, "https://example.com/v1/chat/completions")

    @patch("app.generation.service.urlopen")
    def test_invalid_model_response_is_not_treated_as_success(self, urlopen):
        urlopen.return_value = MockResponse(b'{"choices":[{"message":{"content":"not json"}}]}')
        with self.assertRaises(GenerationError):
            generate("测试")

    @patch("app.generation.service.urlopen")
    def test_model_cannot_claim_unverified_source_metadata(self, urlopen):
        tree = {"title": "测试", "root": {"id": "1", "label": "测试", "children": []},
                "source_usage": [{"source_id": "fake", "kind": "text", "filename": None, "char_count": 3}]}
        body = {"choices": [{"message": {"content": json.dumps(tree)}}]}
        urlopen.return_value = MockResponse(json.dumps(body).encode())
        self.assertEqual(generate("测试").content.source_usage, [])


if __name__ == "__main__":
    unittest.main()
