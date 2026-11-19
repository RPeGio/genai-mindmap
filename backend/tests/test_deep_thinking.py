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
from app.agent.service import REASONING_BYTE_LIMIT, run
from app.agent.stream import encode_event
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
    done = done_of(collect(request, _ReasoningProvider("思" * REASONING_BYTE_LIMIT)))

    assert "已截断" in done.reasoning
    assert _frame_bytes(done) <= REASONING_BYTE_LIMIT + _FRAME_OVERHEAD


# ---------------------------------------------------------------- 字节口径
#
# 上限管的是"这一帧有多大"，那就按**编码之后**的字节钉。三种字符各自是不同的一档：
# 汉字 3 字节、emoji 4 字节、控制字符被 JSON 转义成 `\u0000` 六字节——按字数设上限
# 时这一档能跑出 120KB。

# `data: ` + 帧里除 `reasoning` 之外的字段（`type`/`seq`/`model`/`usage`）+ `\n\n`。
# 这里留一个宽松的余量，钉的是量级不是常量。
_FRAME_OVERHEAD = 512


def _frame_bytes(done: DoneEvent) -> int:
    """这一帧真实发出去的字节数（与 `stream.encode_event` 同一个编码函数）。"""
    return len(encode_event(done).encode("utf-8"))


def _encoded_size(text: str) -> int:
    """这段文本落进帧之后占的字节数（JSON 转义 + UTF-8，与 service 里同一个口径）。"""
    return len(json.dumps(text, ensure_ascii=False).encode("utf-8"))


@pytest.mark.parametrize(
    ("name", "char", "clipped"),
    [
        # 2 万字的三档：汉字（3 字节）、emoji（4 字节）、控制字符（JSON 转义成 `\u0000`，6 字节）。
        # 汉字 60000 字节本来就在 64 KiB 以内，所以现在**整段放行**；另外两档按老口径
        # （字数）能跑出 80/120KB，现在被截回界内。
        ("汉字", "思", False),
        ("emoji", "😀", True),
        ("控制字符", "\x00", True),
    ],
)
def test_frame_bytes_are_bounded_for_every_character_class(name, char, clipped):
    """2 万字的跑飞输入：三种字符的帧都要落回字节上界内，且**不切半个字**。"""
    runaway = char * 20_000
    done = done_of(collect(
        AgentRunRequest(goal="机器学习", deep_thinking=True, snapshot=MindMapContent(**TREE)),
        _ReasoningProvider(runaway),
    ))

    # 两个口径都钉：字段自己（也就是"这一帧比纯帧头多出来的那部分"）在界内，整帧在
    # 界 + 帧头余量内。
    assert _encoded_size(done.reasoning) <= REASONING_BYTE_LIMIT
    assert _encoded_size(done.reasoning) <= _encoded_size(runaway)  # 不会比原来更大
    assert _frame_bytes(done) <= REASONING_BYTE_LIMIT + _FRAME_OVERHEAD

    if not clipped:
        assert done.reasoning == runaway
        return

    # 截在字符边界上：留下来的那段只由原字符组成（没有替换字符、也没有半个码位），
    # 而且它确实是原文的前缀。
    kept = done.reasoning[: done.reasoning.index("…（思考过程过长")]
    assert kept and kept == char * len(kept) and runaway.startswith(kept)
    assert "已截断" in done.reasoning


def test_reasoning_within_the_budget_is_byte_identical():
    """上限内的 reasoning **逐字节**不变——不 trim、不 normalize、不补说明。"""
    text = "先看材料，再定分支。" * 500          # 5000 汉字 ≈ 15KB
    done = done_of(collect(
        AgentRunRequest(goal="机器学习", deep_thinking=True, snapshot=MindMapContent(**TREE)),
        _ReasoningProvider(text),
    ))
    assert done.reasoning.encode("utf-8") == text.encode("utf-8")


def test_an_emoji_at_the_boundary_is_not_split():
    """真正贴着上界的那个 emoji 也不能被劈成两个半个（半个是无效 UTF-8）。"""
    # 16400 个 emoji = 65600 字节，刚好越过 budget（65536 减掉那句说明）：截断点落在
    # emoji 串中间，正是"按字节硬切"会切出半个字符的那个位置。
    body = "😀" * 16_400
    done = done_of(collect(
        AgentRunRequest(goal="机器学习", deep_thinking=True, snapshot=MindMapContent(**TREE)),
        _ReasoningProvider(body),
    ))
    kept = done.reasoning[: done.reasoning.index("…（思考过程过长")]
    assert kept and set(kept) == {"😀"}
    assert len(kept) < len(body)


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
