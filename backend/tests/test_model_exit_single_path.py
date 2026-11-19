"""模型名有**两条**出口，`deep_thinking` 必须同时接上。

这个 bug 的形状值得单独钉一遍：`provider.default_provider(deep_thinking=...)` 是
外层工具循环的出口，`generation.chat_async` 是工具内部（骨架 / 结构 / 说明 / 展开）
的出口。做「深度思考」时只接了前者——勾上之后只有"决定调哪个工具"那一次对话换了
模型，真正写内容的六次调用仍然是普通模型，而界面上 `done.model` 显示的是
`deepseek-reasoner`。**看起来全对了，只有产物是错的。**

三层防线，缺一层都会再犯：

1. `_model_for` 只有一份实现（`generation/service.py`），provider 与 chat_async 都调它；
2. tools.py 里只有 `_chat` 一个函数可以直接调 `chat_async`（AST 扫全文钉住）；
3. `_chat` 的 `deep_thinking` 从 `ctx.request` 取，调用点不各传一遍。

第 2 条是"新增调用点忘记接开关"的唯一机器可查办法：六个调用点里漏一个，
用例 1 会红，而它指向的正是那一条新加的调用。
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
import sys

import pytest

from app.agent import provider as provider_module
from app.agent import tools as tools_module
from app.agent.contracts import AgentRunRequest, RunContext
from app.generation import service as generation_service
from app.generation.service import DEEP_MODEL_ENV, _model_for, chat_async

TOOLS_SOURCE = pathlib.Path(tools_module.__file__).read_text(encoding="utf-8")


def _chat_async_calls(tree: ast.AST) -> list[ast.Call]:
    """AST 里所有直接调用 `chat_async(...)` 的位置。"""
    return [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "chat_async"
    ]


def _function_ranges(tree: ast.Module) -> dict[str, tuple[int, int]]:
    """顶层函数名 → 它的行号范围（用首尾节点算）。"""
    ranges: dict[str, tuple[int, int]] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert node.end_lineno is not None and node.lineno is not None
            ranges[node.name] = (node.lineno, node.end_lineno)
    return ranges


# ---------------------------------------------------------------- 1. 唯一出口


def test_only_the_chat_wrapper_calls_chat_async():
    """tools.py 里除 `_chat` 之外没有第二处直接调 `chat_async`。

    这条不是为了洁癖：`chat_async` 的 `deep_thinking` 缺省是 `False`，漏传一个
    调用点不会报错，只会静默降级——正是这次要修的那个 bug 的机制。
    """
    tree = ast.parse(TOOLS_SOURCE)
    ranges = _function_ranges(tree)
    assert "_chat" in ranges, "封装函数被改名了，守卫用例需要同步"

    misplaced: list[int] = []
    for call in _chat_async_calls(tree):
        owner = next(
            (name for name, (start, end) in ranges.items()
             if start <= call.lineno <= end),
            "<module>",
        )
        if owner != "_chat":
            misplaced.append(call.lineno)

    assert not misplaced, (
        "tools.py 里出现了绕过 `_chat` 的 chat_async 调用（行号 "
        f"{misplaced}）：它拿不到 `ctx.request.deep_thinking`，勾了「深度思考」"
        "也不会换模型。改成 `_chat(ctx, ...)`。"
    )


# ---------------------------------------------------------------- 2. 开关真的到请求体


@pytest.fixture
def llm_env(monkeypatch):
    """一套完整的模型配置：端点与模型名都带 deepseek，`_model_for` 会走第 2 档。"""
    monkeypatch.delenv(DEEP_MODEL_ENV, raising=False)
    monkeypatch.setenv("MINDMAP_LLM_BASE_URL", "https://api.deepseek.com/v1")
    monkeypatch.setenv("MINDMAP_LLM_API_KEY", "test-key")
    monkeypatch.setenv("MINDMAP_LLM_MODEL", "deepseek-chat")


def _capture_chat(monkeypatch) -> list[dict]:
    """把 `chat_async` 的一次性请求拦下来，返回它发给上游的请求体。"""
    sent: list[dict] = []

    async def fake_once(base: str, request_body: dict, headers: dict) -> dict:
        sent.append(request_body)
        return {"title": "t", "root": {"id": "r", "label": "t", "children": []}}

    monkeypatch.setattr(generation_service, "_chat_once", fake_once)
    return sent


def test_chat_async_sends_the_reasoner_when_deep(llm_env, monkeypatch):
    sent = _capture_chat(monkeypatch)
    asyncio.run(chat_async("sys", "user", deep_thinking=True))
    assert sent[0]["model"] == "deepseek-reasoner"


def test_chat_async_keeps_the_plain_model_without_the_flag(llm_env, monkeypatch):
    sent = _capture_chat(monkeypatch)
    asyncio.run(chat_async("sys", "user"))
    assert sent[0]["model"] == "deepseek-chat"


def test_both_model_exits_agree_on_the_same_flag(llm_env, monkeypatch):
    """同一次运行里，外层工具循环与工具内部发出的模型名必须一致。

    这正是修复前不一致的那一对：provider 走 reasoner，chat_async 走 chat。
    """
    sent = _capture_chat(monkeypatch)
    asyncio.run(chat_async("sys", "user", deep_thinking=True))

    provider = provider_module.OpenAICompatibleProvider(deep_thinking=True)
    base, _, model = generation_service._config()
    provider_model = _model_for(base, model, provider.deep_thinking)

    assert sent[0]["model"] == provider_model == "deepseek-reasoner"


# ---------------------------------------------------------------- 3. 封装取值


def test_the_wrapper_reads_the_flag_from_the_request(monkeypatch):
    """`_chat` 的 `deep_thinking` 来自 `ctx.request`，调用点一个参数都不用传。

    不勾时**不发这个键**：`chat_async` 的缺省就是 `False`，少传一次等于"与加这个
    开关之前逐字节相同"——既有那些按脚本回放的假出口签名不用改。
    """
    seen: list[bool | None] = []

    async def fake_chat(system, user, **kwargs):
        seen.append(kwargs.get("deep_thinking"))
        return {}

    monkeypatch.setattr(tools_module, "chat_async", fake_chat)
    for flag in (False, True):
        request = AgentRunRequest(goal="机器学习", deep_thinking=flag)
        ctx = RunContext(request=request)
        asyncio.run(tools_module._chat(ctx, "sys", "user"))
    assert seen == [None, True]
    # 发下去的值必须是 bool：`_model_for` 对非真值一律走"沿用原模型"那一档，
    # 漏成 None 或字符串 "false" 都会静默降级成普通模型。
    assert all(value is None or value is True for value in seen)


def test_vendor_without_deep_support_degrades_visibly(monkeypatch):
    """不认识对面是谁时两条出口都沿用原模型——不做"猜一个推理模型名"。

    这是 `_model_for` 第 3 档的语义；放在这里是因为它决定"勾了没用时界面显示什么"：
    `done.model` 会如实写着普通模型名，用户据此能判断是不是没配 `MINDMAP_LLM_MODEL_DEEP`。
    """
    monkeypatch.delenv(DEEP_MODEL_ENV, raising=False)
    monkeypatch.setenv("MINDMAP_LLM_BASE_URL", "https://example.com/v1")
    monkeypatch.setenv("MINDMAP_LLM_API_KEY", "test-key")
    monkeypatch.setenv("MINDMAP_LLM_MODEL", "chat-x")
    sent = _capture_chat(monkeypatch)

    asyncio.run(chat_async("sys", "user", deep_thinking=True))
    assert sent[0]["model"] == "chat-x"


if sys.version_info < (3, 10):  # pragma: no cover — 部署基线是 3.12
    pytest.skip("AST 守卫依赖 3.10+ 的 end_lineno", allow_module_level=True)
