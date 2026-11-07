import asyncio
import json
import os
import unittest
from unittest.mock import patch
import httpx
from app.contracts.mindmap import MindMapContent
from app.generation.service import (
    DETAIL_PROMPT,
    MAX_OUTPUT_TOKENS,
    MIN_BRANCHES,
    SOURCED_DETAIL_PROMPT,
    GenerationError,
    chat,
    chat_async,
    diagnose_density,
    generate,
    parse_model_json,
)


def _tree(branches: int) -> dict:
    """造一棵有 `branches` 条一级分支的树。

    一级分支下限是 3，所以「合法模型结果」的最小形态是三条空分支 —— 老用例里
    只写一条分支的 fixture 现在会被挡下，凡是断言生成成功的用例都得用这个函数。
    """
    return {"title": "测试", "root": {"id": "1", "label": "测试", "children": [
        {"id": f"b{index}", "label": f"分支{index}", "children": []}
        for index in range(1, branches + 1)
    ]}}


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
        tree = _tree(3)
        client_class = self.use_client(MockStreamResponse(self.body_of(tree)))
        result = generate("测试")
        self.assertEqual(result.mode, "model")
        self.assertEqual(result.content.root.label, "测试")
        self.assertEqual(len(result.content.root.children), 3)
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
                "children": [{"id": f"b{index}", "label": f"分支{index}", "children": [],
                              "source_refs": [{"source_id": f"another-fake-{index}",
                                               "page": -1}]}
                             for index in range(1, 4)]},
                "source_usage": [{"source_id": "fake", "kind": "text", "filename": None, "char_count": 3}]}
        self.use_client(MockStreamResponse(self.body_of(tree)))
        content = generate("测试").content
        self.assertEqual(content.source_usage, [])
        self.assertEqual(content.root.source_refs, [])
        self.assertEqual([child.source_refs for child in content.root.children],
                         [[], [], []])

    def test_root_only_tree_is_rejected(self):
        """光杆根节点不算生成成功：同步接口与流式接口用的是同一个下限。

        下限从 1 提到 3 之后，这条用例的抓手仍然是「一条分支都没有」——
        它对应的错误文案独立于「分支条数不够」，所以要单独守住。
        """
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
        tree = _tree(3)
        client_class = self.use_client(MockStreamResponse(self.body_of(tree)))
        generate("测试")
        _, _, kwargs = client_class.return_value.calls[0]
        payload = json.loads(kwargs["content"])
        self.assertEqual(payload["max_tokens"], MAX_OUTPUT_TOKENS)
        self.assertGreater(MAX_OUTPUT_TOKENS, 8192)

    def test_explicit_max_tokens_still_wins(self):
        """调用方显式指定时以调用方为准，便于个别短请求省钱。"""
        client_class = self.use_client(MockStreamResponse(self.body_of(_tree(3))))
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


class BranchFloorTest(unittest.TestCase):
    """一级分支下限（`MIN_BRANCHES = 3`）：只作用于模型整图生成这条路径。

    下限是**质量**要求而不是合法性要求：`MindMapContent.validate_tree()` 不加这道
    门槛，旧图和用户手工删到一条分支的图照常能存能开；demo 也显式跳过。
    """

    def setUp(self):
        self.env = patch.dict(os.environ, {
            "MINDMAP_LLM_MODE": "model",
            "MINDMAP_LLM_BASE_URL": "https://example.com/v1",
            "MINDMAP_LLM_MODEL": "test",
            "MINDMAP_LLM_API_KEY": "not-a-real-key",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def use_tree(self, tree):
        patcher = patch("app.generation.service.httpx.Client",
                        return_value=MockClient(MockStreamResponse(GenerationTest.body_of(tree))))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_single_branch_is_rejected(self):
        """一条分支就是一条链，不是层次结构。

        文案必须可操作：说清模型给了几条、要求几条，并给出「换一种说法」或
        「补充材料」这两条出路，而不是干巴巴一句「不合格」。
        """
        self.use_tree(_tree(1))
        with self.assertRaises(GenerationError) as raised:
            generate("测试")
        message = str(raised.exception)
        self.assertIn("分支", message)
        self.assertIn("1 条", message)
        self.assertIn("换一种说法", message)
        self.assertIn("补充更多材料", message)

    def test_two_branches_are_rejected(self):
        self.use_tree(_tree(2))
        with self.assertRaises(GenerationError) as raised:
            generate("测试")
        self.assertIn("2 条", str(raised.exception))

    def test_three_branches_pass(self):
        self.use_tree(_tree(3))
        result = generate("测试")
        self.assertEqual(len(result.content.root.children), 3)
        # 密度诊断不进 GenerateResponse：既有 diagnostics 断言不能改口径。
        self.assertEqual(result.diagnostics, [])

    def test_demo_single_child_placeholder_still_passes(self):
        """demo 是固定占位结构，本来就只有一个子节点，不该被模型质量门槛打回。"""
        with patch.dict(os.environ, {"MINDMAP_LLM_MODE": "demo"}):
            result = generate("测试")
        self.assertEqual(result.mode, "demo")
        self.assertEqual(len(result.content.root.children), 1)

    def test_contract_still_accepts_a_single_branch_tree(self):
        """契约校验不设分支下限：旧图与手工编辑后的图必须照常能存能开。"""
        content = MindMapContent.model_validate(_tree(1))
        self.assertEqual(len(content.root.children), 1)

    def test_min_branch_constant(self):
        self.assertEqual(MIN_BRANCHES, 3)


class PromptQualityTest(unittest.TestCase):
    """prompt 里的数量约束与引用正反例：模型只照字面执行，不写就是没有。"""

    def test_both_prompts_state_the_point_and_branch_floor(self):
        for prompt in (DETAIL_PROMPT, SOURCED_DETAIL_PROMPT):
            self.assertIn("至少 2 个要点", prompt)
            self.assertIn("不少于 3 条", prompt)

    def test_material_branch_forbids_padding_to_hit_the_count(self):
        """材料分支原先只说「忠实整理」，要点数量一个字都没提。

        补数量时最怕把模型推成「凑数」——依据不足要宁可少给。
        """
        self.assertIn("不要为了凑数补写", DETAIL_PROMPT)
        self.assertIn("依据不足时宁可", DETAIL_PROMPT)

    def test_sourced_prompt_shows_verbatim_quote_example(self):
        """`quote` 退化成概括是最常见的一种坏引用，必须有正反例压住。"""
        original = "会议决定在十月十七日发布二点零版本。"
        self.assertIn("❌", SOURCED_DETAIL_PROMPT)
        self.assertIn("✅", SOURCED_DETAIL_PROMPT)
        self.assertIn(original, SOURCED_DETAIL_PROMPT)

        good_line = SOURCED_DETAIL_PROMPT.split("✅", 1)[1].split("\n", 1)[0]
        good_quote = good_line.split('quote="', 1)[1].split('"', 1)[0]
        # 正向示例必须是原文里的连续片段，逐字可查
        self.assertIn(good_quote, original)

        bad_line = SOURCED_DETAIL_PROMPT.split("❌", 1)[1].split("\n", 1)[0]
        bad_quote = bad_line.split('quote="', 1)[1].split('"', 1)[0]
        # 反向示例是概括出来的话，原文里逐字找不到
        self.assertNotIn(bad_quote, original)


class DensityDiagnosticTest(unittest.TestCase):
    """`diagnose_density()` 的规则：只出 warn 提示，不构成失败。"""

    @staticmethod
    def _content(sparse_children: int, dense_children: int) -> MindMapContent:
        return MindMapContent.model_validate({
            "title": "测试",
            "root": {"id": "r", "label": "根", "children": [
                {"id": "sparse", "label": "稀疏分支", "children": [
                    {"id": f"s{index}", "label": f"要点{index}", "children": []}
                    for index in range(1, sparse_children + 1)]},
                {"id": "dense", "label": "充实分支", "children": [
                    {"id": f"d{index}", "label": f"要点{index}", "children": []}
                    for index in range(1, dense_children + 1)]},
            ]},
        })

    def test_sparse_branch_is_reported_with_its_count(self):
        diagnostics = diagnose_density(self._content(0, 2))
        self.assertEqual(len(diagnostics), 1)
        item = diagnostics[0]
        self.assertEqual(item.code, "SPARSE_BRANCH")
        self.assertEqual(item.level.value, "warn")
        self.assertEqual(item.node_id, "sparse")
        self.assertIn("稀疏分支", item.message)
        self.assertIn("0 个要点", item.message)

    def test_minimum_is_configurable(self):
        diagnostics = diagnose_density(self._content(0, 2), minimum=3)
        self.assertEqual([item.node_id for item in diagnostics], ["sparse", "dense"])

    def test_dense_tree_has_no_diagnostic(self):
        self.assertEqual(diagnose_density(self._content(2, 3)), [])


if __name__ == "__main__":
    unittest.main()
