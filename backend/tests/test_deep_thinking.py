"""「深度思考」这一档：只切 model 名，别的都不动。

三条界线必须钉死，否则这个开关会变成"看起来能勾、勾了不知道为什么没变"的控件：

1. 请求契约里 `deep_thinking` 缺省 `False`，不发这个字段与加它之前逐字节相同；
2. `False` 时发出去的 model 名**必须**还是 `MINDMAP_LLM_MODEL`（老路径不能漂移）；
3. 真走 reasoner 时，`reasoning` 要在**成功**的 `done` 帧里也带着，而且不按
   `MODEL_TEXT_LIMIT`（4000 字）截——实测思考过程就是 3000–8000 字，用它去截
   等于每次都砍掉一半。

打到 api.deepseek.com 的真调用需要密钥，本机没有；这里用假 provider 驱动循环，
验的是**协议与帧**，以及 `_model_for` 这个纯函数的每一档。真实上游那一次走
`.visioncheck/verify_deep_thinking.py` 的 mock 端点：它记录收到的 `model` 字段。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.agent import provider as provider_module
from app.agent.contracts import AgentRunRequest, Completion, DoneEvent, RunBudget, ToolCall
from app.agent.service import REASONING_TEXT_LIMIT, run
from app.contracts.mindmap import MindMapContent
# 模型名的判定只有一份，在 generation 那边：两条出口（provider 与 chat_async）
# 都从那里取名字，所以这里也从那里导入。
from app.generation.service import DEEP_MODEL_ENV, _model_for

TREE = {
    "title": "机器学习",
    "root": {"id": "r", "label": "机器学习", "children": []},
}


def collect(request: AgentRunRequest, provider) -> list:
    """把 async 生成器收成列表（项目没装 pytest-asyncio，用 asyncio.run 更省事）。"""

    async def drain() -> list:
        return [event async for event in run(request, provider=provider)]

    return asyncio.run(drain())


def done_of(events: list) -> DoneEvent:
    dones = [event for event in events if isinstance(event, DoneEvent)]
    assert dones, "这一轮没有 done 帧"
    return dones[-1]


# ---------------------------------------------------------------- 契约


def test_deep_thinking_defaults_to_false():
    """老前端不带这个字段时行为不变——缺省值就是"不勾"。"""
    request = AgentRunRequest(goal="机器学习")
    assert request.deep_thinking is False


def test_deep_thinking_is_a_per_run_flag_not_a_pref():
    """它不进 `prefs_applied`：这一次的开关不该被记成"用户的长期选择"。"""
    request = AgentRunRequest(goal="机器学习", deep_thinking=True)
    assert request.prefs_applied == []


def test_deep_thinking_can_be_turned_on():
    request = AgentRunRequest(goal="机器学习", deep_thinking=True)
    assert request.deep_thinking is True


# ---------------------------------------------------------------- 模型名


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(DEEP_MODEL_ENV, raising=False)


def test_model_name_unchanged_when_not_deep():
    """不勾时必须原样发 `MINDMAP_LLM_MODEL`——这条是"不动生成链路"的底线。"""
    assert _model_for("https://api.deepseek.com", "deepseek-chat", False) == "deepseek-chat"


def test_model_name_switches_to_reasoner_on_deepseek():
    assert _model_for("https://api.deepseek.com", "deepseek-chat", True) == "deepseek-reasoner"


def test_explicit_deep_model_wins(monkeypatch):
    """换供应商时唯一该改的地方：`MINDMAP_LLM_MODEL_DEEP`。"""
    monkeypatch.setenv(DEEP_MODEL_ENV, "my-reasoner")
    assert _model_for("https://example.com/v1", "chat-x", True) == "my-reasoner"


def test_unknown_endpoint_keeps_its_own_model():
    """不认识对面是谁时**不猜**：猜一个 `deepseek-reasoner` 只会换回一个 400。"""
    assert _model_for("https://example.com/v1", "chat-x", True) == "chat-x"


def test_provider_remembers_the_model_it_sent():
    """provider 记下真发出去的名字，`done` 帧据此告诉界面"这次走的哪条路"。"""
    fake = provider_module.OpenAICompatibleProvider(deep_thinking=True)
    assert fake.model_name is None  # 还没发请求，就是"没说"
    fake.model_name = _model_for("https://api.deepseek.com", "deepseek-chat", fake.deep_thinking)
    assert fake.model_name == "deepseek-reasoner"


def test_default_provider_is_off_unless_asked():
    """`default_provider()` 不带参数时是普通档：`_loop` 只在请求说要时才传 True。"""
    assert provider_module.default_provider().deep_thinking is False
    assert provider_module.default_provider(deep_thinking=True).deep_thinking is True


# ---------------------------------------------------------------- 帧


class _ReasoningProvider:
    """按脚本回放：一帧带思考过程的收尾（树已经在 snapshot 里）。"""

    def __init__(self, reasoning: str) -> None:
        self.reasoning = reasoning
        self.seen: list[list[dict]] = []

    async def complete_tools(self, messages, tools, *, budget: RunBudget) -> Completion:
        self.seen.append([dict(message) for message in messages])
        return Completion(
            message={"role": "assistant", "content": "", "reasoning_content": self.reasoning},
            tool_calls=[],
            finish_reason="stop",
            usage={},
        )


def test_reasoning_survives_a_successful_run():
    """成功时反而藏起来是旧行为，这条是它的反例：树已经落，思考过程照样给。"""
    provider = _ReasoningProvider("先看这块材料，再定六个分支……")
    request = AgentRunRequest(goal="机器学习", deep_thinking=True, snapshot=MindMapContent(**TREE))
    done = done_of(collect(request, provider))

    assert isinstance(done.content, MindMapContent)
    assert done.reasoning == "先看这块材料，再定六个分支……"


def test_long_reasoning_is_not_clipped_at_the_summary_limit():
    """8000 字的思考过程要**整段**过去：`MODEL_TEXT_LIMIT`（4000）不该管它。"""
    long_reasoning = "思" * 8000
    request = AgentRunRequest(goal="机器学习", deep_thinking=True, snapshot=MindMapContent(**TREE))
    done = done_of(collect(request, _ReasoningProvider(long_reasoning)))

    assert len(done.reasoning) == 8000
    assert done.reasoning == long_reasoning


def test_runaway_reasoning_is_bounded_and_says_so():
    """跑飞时留一条上限，并且**在文本里写明**截了——不要静默少一段。"""
    request = AgentRunRequest(goal="机器学习", deep_thinking=True, snapshot=MindMapContent(**TREE))
    done = done_of(collect(request, _ReasoningProvider("思" * (REASONING_TEXT_LIMIT + 500))))

    assert done.reasoning.startswith("思" * REASONING_TEXT_LIMIT)
    assert "已截断" in done.reasoning


def test_done_frame_carries_no_model_name_from_an_anonymous_provider():
    """按脚本回放的 provider 没有 `model_name`：帧里整条不存在，别写成"chat"。"""
    request = AgentRunRequest(goal="机器学习", deep_thinking=True, snapshot=MindMapContent(**TREE))
    done = done_of(collect(request, _ReasoningProvider("想了")))

    assert done.model is None
    payload = done.model_dump(mode="json", exclude_none=True)
    assert "model" not in payload


def test_reasoning_content_goes_back_to_the_model_verbatim():
    """思考模式 + tools 的硬要求：历史轮次的 `reasoning_content` 必须原样回传。

    不回传时上游直接 400，而那句报错只读得成"模型服务出错"。回传的实现是
    `messages.append(completion.message)`——这里钉住它确实把 `reasoning_content`
    带进了下一轮请求。
    """

    class _TwoTurn(_ReasoningProvider):
        def __init__(self) -> None:
            super().__init__("第一轮的想法")
            self.calls = 0

        async def complete_tools(self, messages, tools, *, budget: RunBudget) -> Completion:
            self.calls += 1
            self.seen.append([dict(message) for message in messages])
            if self.calls == 1:
                return Completion(
                    message={"role": "assistant", "content": "",
                             "reasoning_content": "第一轮的想法",
                             "tool_calls": [{
                                 "id": "c1", "type": "function",
                                 "function": {"name": "audit_tree", "arguments": "{}"},
                             }]},
                    tool_calls=[ToolCall(call_id="c1", name="audit_tree", arguments={})],
                    finish_reason="tool_calls",
                    usage={},
                )
            return Completion(
                message={"role": "assistant", "content": "", "reasoning_content": "收尾"},
                tool_calls=[],
                finish_reason="stop",
                usage={},
            )

    provider = _TwoTurn()
    request = AgentRunRequest(goal="机器学习", snapshot=MindMapContent(**TREE), deep_thinking=True)
    events = collect(request, provider)
    assert any(isinstance(event, DoneEvent) for event in events)

    assistant = [message for message in provider.seen[1] if message.get("role") == "assistant"]
    assert assistant and assistant[0].get("reasoning_content") == "第一轮的想法"


def test_reasoning_frame_size_is_sane():
    """一帧 8000 字中文的 JSON 就是几十 KB，不碰任何一层上限。

    SSE 按 `\\n\\n` 切帧、前端 `postSse` 不设单帧长度上限；这里只是把"多大"钉成
    一个可复算的数字，免得将来有人把 20000 改成 2 亿还说"只是不截而已"。
    """
    payload = {"type": "done", "seq": 9, "reasoning": "思" * 8000}
    encoded = f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()
    assert 20_000 < len(encoded) < 40_000
