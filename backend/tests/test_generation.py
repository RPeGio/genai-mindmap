import asyncio
import json
import os
import unittest
from unittest.mock import patch
import httpx
from app.generation.service import (
    MAX_OUTPUT_TOKENS,
    GenerationError,
    chat,
    chat_async,
    generate,
    parse_model_json,
)


def _response_body(tree: dict, finish_reason: str | None = None) -> bytes:
    """拼一个 Chat Completions 响应体；`finish_reason=None` 时整个字段不出现。"""
    choice: dict = {"message": {"content": json.dumps(tree)}}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    return json.dumps({"choices": [choice]}).encode()


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
        tree = {"title": "测试", "root": {"id": "1", "label": "测试",
                "children": [{"id": "b1", "label": "分支", "children": []}]}}
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
        tree = {"title": "测试", "root": {"id": "1", "label": "测试",
                "source_refs": [{"source_id": "fake", "quote": "编造"}],
                "children": [{"id": "b1", "label": "分支", "children": [],
                              "source_refs": [{"source_id": "another-fake", "page": -1}]}]},
                "source_usage": [{"source_id": "fake", "kind": "text", "filename": None, "char_count": 3}]}
        self.use_client(MockStreamResponse(self.body_of(tree)))
        content = generate("测试").content
        self.assertEqual(content.source_usage, [])
        self.assertEqual(content.root.source_refs, [])
        self.assertEqual(content.root.children[0].source_refs, [])

    def test_root_only_tree_is_rejected(self):
        """光杆根节点不算生成成功：同步接口与流式接口用的是同一个下限。"""
        tree = {"title": "测试", "root": {"id": "1", "label": "测试", "children": []}}
        self.use_client(MockStreamResponse(self.body_of(tree)))
        with self.assertRaises(GenerationError) as raised:
            generate("测试")
        self.assertIn("没有任何分支", str(raised.exception))

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

    def test_request_carries_explicit_output_token_limit(self):
        """必须显式给 max_tokens。

        服务端默认 8192，而 DETAIL_PROMPT 允许 300 个节点、实测 240 节点就要
        7578 token，不抬高上限就会被静默截断成半截 JSON（实测 20000 字符的
        输入撞过一次，finish_reason=length、输出正好 8192 后报废）。
        """
        tree = {"title": "测试", "root": {"id": "1", "label": "测试",
                "children": [{"id": "b1", "label": "分支", "children": []}]}}
        client_class = self.use_client(MockStreamResponse(self.body_of(tree)))
        generate("测试")
        _, _, kwargs = client_class.return_value.calls[0]
        payload = json.loads(kwargs["content"])
        self.assertEqual(payload["max_tokens"], MAX_OUTPUT_TOKENS)
        self.assertGreater(MAX_OUTPUT_TOKENS, 8192)

    def test_explicit_max_tokens_still_wins(self):
        """调用方显式指定时以调用方为准，便于个别短请求省钱。"""
        tree = {"title": "测试", "root": {"id": "1", "label": "测试",
                "children": [{"id": "b1", "label": "分支", "children": []}]}}
        client_class = self.use_client(MockStreamResponse(self.body_of(tree)))
        chat("系统", "用户", max_tokens=512)
        _, _, kwargs = client_class.return_value.calls[0]
        self.assertEqual(json.loads(kwargs["content"])["max_tokens"], 512)


class MockAsyncStreamResponse:
    """模拟 httpx 异步流式响应：既是异步上下文管理器，也按块吐字节。"""
    def __init__(self, data, status_code=200):
        self.data = data
        self.status_code = status_code
        self.request = httpx.Request("POST", "https://example.com/v1/chat/completions")
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return False
    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(f"HTTP {self.status_code}", request=self.request,
                                        response=httpx.Response(self.status_code, request=self.request))
    async def aiter_bytes(self):
        yield self.data


class MockAsyncClient:
    """模拟 httpx.AsyncClient。`stream()` 与真身一致，返回异步上下文管理器。"""
    def __init__(self, response):
        self.response = response
        self.calls = []
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return False
    def stream(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class FinishReasonTest(unittest.TestCase):
    """`finish_reason` 的三种取值。

    `provider._parse_completion()` 会拦 `length`，但工具内的生成与展开走的是
    `generation.service.chat_async()`，那条路径没有这一层。不看的话，被硬切的
    半截 JSON 会被 `_balance_brackets` 补成"合法"，半棵树照样返回成功。
    """

    TREE = {"title": "测试", "root": {"id": "1", "label": "测试",
            "children": [{"id": "b1", "label": "分支", "children": []}]}}

    def setUp(self):
        self.env = patch.dict(os.environ, {
            "MINDMAP_LLM_MODE": "model",
            "MINDMAP_LLM_BASE_URL": "https://example.com/v1",
            "MINDMAP_LLM_MODEL": "test",
            "MINDMAP_LLM_API_KEY": "not-a-real-key",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def use_async_client(self, response):
        patcher = patch("app.generation.service.httpx.AsyncClient",
                        return_value=MockAsyncClient(response))
        client_class = patcher.start()
        self.addCleanup(patcher.stop)
        return client_class

    def use_sync_client(self, response):
        patcher = patch("app.generation.service.httpx.Client", return_value=MockClient(response))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_chat_async_rejects_truncated_output(self):
        """finish_reason=length：抛错，绝不把补全括号后的半棵树当成功。"""
        self.use_async_client(MockAsyncStreamResponse(_response_body(self.TREE, "length")))
        with self.assertRaises(GenerationError) as raised:
            asyncio.run(chat_async("系统", "用户"))
        self.assertIn("截断", str(raised.exception))

    def test_chat_async_parses_without_finish_reason(self):
        """字段缺失照常解析：旧 mock 与部分兼容端点都不返回它。"""
        self.use_async_client(MockAsyncStreamResponse(_response_body(self.TREE)))
        result = asyncio.run(chat_async("系统", "用户"))
        self.assertEqual(result["title"], "测试")
        self.assertEqual(result["root"]["children"][0]["id"], "b1")

    def test_chat_async_parses_on_stop(self):
        self.use_async_client(MockAsyncStreamResponse(_response_body(self.TREE, "stop")))
        result = asyncio.run(chat_async("系统", "用户"))
        self.assertEqual(result["root"]["label"], "测试")

    def test_chat_also_rejects_truncated_output(self):
        """同步路径口径必须一致：同一个响应体不能一个入口拒、另一个入口收。"""
        self.use_sync_client(MockStreamResponse(_response_body(self.TREE, "length")))
        with self.assertRaises(GenerationError) as raised:
            chat("系统", "用户")
        self.assertIn("截断", str(raised.exception))


class ParseModelJsonTest(unittest.TestCase):
    """模型输出的 JSON 修复。

    实测两大类失败：收尾多写一个花括号（finish_reason 仍是 stop），
    以及撞 max_tokens 被硬切断（缺闭合符）。后者的合法性现在由 finish_reason
    先判掉，这里测的是"修复器本身还能用"。
    """

    TREE = ('{"title":"t","root":{"id":"1","label":"根","children":'
            '[{"id":"2","label":"分支","children":[]}]}}')

    def test_plain_json(self):
        self.assertEqual(parse_model_json(self.TREE)["title"], "t")

    def test_code_fence(self):
        self.assertEqual(parse_model_json(f"```json\n{self.TREE}\n```")["title"], "t")

    def test_extra_closing_brace_is_dropped(self):
        """末尾多一个 }：实测 100000 字符输入 3 次里 2 次这样。"""
        broken = self.TREE + "}"
        with self.assertRaises(json.JSONDecodeError):
            json.loads(broken)
        self.assertEqual(parse_model_json(broken)["title"], "t")

    def test_extra_closing_bracket_is_dropped(self):
        broken = '{"a":[1,2]]}'
        self.assertEqual(parse_model_json(broken), {"a": [1, 2]})

    def test_missing_closing_braces_are_filled(self):
        """输出被 max_tokens 切断：结构没写完，补齐后仍可用。"""
        truncated = self.TREE[:-3]          # 去掉末尾的 "}}"
        with self.assertRaises(json.JSONDecodeError):
            json.loads(truncated)
        parsed = parse_model_json(truncated)
        self.assertEqual(parsed["root"]["label"], "根")

    def test_braces_inside_strings_are_untouched(self):
        """节点文字里出现 { } 时不能被当成结构括号修掉。"""
        payload = ('{"title":"a}b","root":{"id":"1","label":"含 } 与 { 的文字",'
                   '"children":[]}}') + "}"
        parsed = parse_model_json(payload)
        self.assertEqual(parsed["title"], "a}b")
        self.assertEqual(parsed["root"]["label"], "含 } 与 { 的文字")

    def test_surrounding_prose_is_ignored(self):
        text = f"好的，结果如下：\n{self.TREE}\n以上。"
        self.assertEqual(parse_model_json(text)["title"], "t")

    def test_unrecoverable_content_still_raises(self):
        with self.assertRaises(ValueError):
            parse_model_json("完全不是 JSON")


if __name__ == "__main__":
    unittest.main()
