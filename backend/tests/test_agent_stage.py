"""检索期间的进度信号（`stage` 帧）：通道类型、转发、时序与"关掉就一个字都不多"。

五组，各自钉一件事：

  1. `_emit_stage` 在通道为 None 时**一次 await 都不发**——没有 SSE 消费者的路径
     （直接调工具的用例、库内调用）不该因为界面需要而多出任何行为；
  2. 有通道时帧形状固定：`{type, stage, detail}`，结束帧多一个 `count`；
  3. `service._stage_frame` / `_tool_frame`：认得出、认不出的都不抛，seq 只在
     校验通过之后才取号；
  4. 端到端：外部检索真的慢的时候，开始帧**在检索结束之前**就到消费者手里——
     这正是这条功能存在的理由（原先 4–6 秒静默）；结束帧带命中条数；
  5. 关掉外部检索时一帧都没有（与 `202a7bd` 的行为一致）。

全部离线：假 provider 出工具调用、假 `chat_async` 出树、假 `search_external` 出
命中并可控地慢。真 HTTP 由 `conftest.no_real_http` 拦。
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from unittest.mock import patch

import pytest

from app.agent import service as agent_service
from app.agent import tools as agent_tools
from app.agent.contracts import (
    AgentRunRequest,
    Completion,
    NodeGrowEvent,
    RunBudget,
    RunContext,
    StageEvent,
    ToolCall,
)
from app.agent.service import run
from app.agent.stream import encode_event
from app.knowledge import external

TOPIC = "碳排放交易"


# ---------------------------------------------------------------- 公共夹具


def _run(coro):
    return asyncio.run(coro)


def _ctx(emit=None) -> RunContext:
    return RunContext(
        request=AgentRunRequest(
            goal=TOPIC, intent="create",
            budget=RunBudget(max_model_calls=10, max_tool_calls=5, max_seconds=60.0,
                             max_output_tokens=4096, max_repairs=1),
        ),
        emit=emit,
    )


def _collector() -> tuple[list[dict], Any]:
    frames: list[dict] = []

    async def emit(payload: dict) -> None:
        frames.append(dict(payload))

    return frames, emit


def _hit() -> external.ExternalHit:
    return external.ExternalHit(
        title="碳排放交易", text="碳排放交易是用市场机制减排的政策工具。",
        url="https://example.invalid/ets", source_name="wikipedia",
        fetched_at="2026-01-01T00:00:00Z", language="zh",
        raw_chars=20,
    )


def _tree() -> dict[str, Any]:
    return {
        "title": TOPIC,
        "root": {"id": "root", "label": TOPIC, "children": [
            {"id": "a", "label": "总量控制", "children": []},
            {"id": "b", "label": "配额分配", "children": []},
            {"id": "c", "label": "价格机制", "children": []},
        ]},
    }


async def _deep_chat(system: str, user: str, **kwargs: Any) -> Any:
    """分片生成的模型替身：骨架三条、每支一个要点。

    这些用例测的是**外部检索的进度帧**（`_generate_deep` 里那两帧），所以替身只要
    让流程跑完、让外部命中真的进入检索即可。分支标题与 `_hit()` 的正文有共同词项，
    于是按支检索会命中——这也是"外部来源真的成了依据"的前提。
    """
    if system.startswith(agent_tools.PLAN_PROMPT):
        return {"branches": ["总量控制", "配额分配", "价格机制"]}
    return {"children": [{"label": "要点一", "expandable": False}]}


def _tool_completion(call: ToolCall) -> Completion:
    return Completion(
        message={
            "role": "assistant", "content": "",
            "tool_calls": [{
                "id": call.call_id, "type": "function",
                "function": {"name": call.name,
                             "arguments": json.dumps(call.arguments, ensure_ascii=False)},
            }],
        },
        tool_calls=[call], finish_reason="tool_calls",
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    )


def _stop_completion() -> Completion:
    return Completion(message={"role": "assistant", "content": "好了"}, finish_reason="stop")


class _ScriptedProvider:
    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.calls = 0

    async def complete_tools(self, messages, tools, *, budget):
        self.calls += 1
        if not self._script:
            raise AssertionError("脚本已用完，循环比预期多调了一次模型")
        return self._script.pop(0)


def _generate_once(ctx: RunContext) -> Any:
    async def chat(system: str, user: str, **kwargs: Any) -> Any:
        return await _deep_chat(system, user)

    with patch.object(agent_tools, "chat_async", chat):
        return _run(agent_tools.build_registry("create_deep").execute(
            ToolCall(call_id="c-gen", name="mindmap_generate_deep",
                     arguments={"topic": TOPIC, "branches": 3,
                                "points_per_branch": 2, "max_depth": 2}), ctx))


# ---------------------------------------------------------------- 1 通道为 None


def test_emit_stage_is_a_no_op_without_a_channel():
    """通道为 None：不取帧、不 await、不产生任何东西。"""
    ctx = _ctx(emit=None)
    _run(agent_tools._emit_stage(ctx, "external_search", "正在检索外部资料…"))
    _run(agent_tools._emit_stage(ctx, "external_search", "已找到 1 条外部资料", count=1))
    assert ctx.emit is None


def test_generation_without_channel_produces_no_frames(monkeypatch):
    """没有消费者的 `mindmap_generate`：外部检索照跑，但一帧都不推。

    这条钉的是"通道关掉时行为与加这个功能之前逐字相同"——不是"少一帧"，是
    连一次 `await emit` 都不多。
    """
    monkeypatch.setenv(agent_tools.EXTERNAL_KNOWLEDGE_ENV, "1")
    monkeypatch.setattr(agent_tools.external_knowledge, "search_external",
                        lambda *args, **kwargs: [_hit()])

    result = _generate_once(_ctx(emit=None))

    assert result.ok, result.content
    assert result.data["external"]["sources"] == 1


# ---------------------------------------------------------------- 2 帧形状


def test_emit_stage_sends_start_frame_without_count():
    frames, emit = _collector()
    _run(agent_tools._emit_stage(_ctx(emit), agent_tools.EXTERNAL_SEARCH_STAGE,
                                 "正在检索外部资料…"))

    assert frames == [{"type": "stage", "stage": "external_search",
                       "detail": "正在检索外部资料…"}]
    # 开始帧**不带** count 键：前端靠"有没有这个字段"区分开始与结束
    assert "count" not in frames[0]


def test_emit_stage_sends_end_frame_with_count_including_zero():
    frames, emit = _collector()
    ctx = _ctx(emit)
    _run(agent_tools._emit_stage(ctx, "external_search", "已找到 0 条外部资料", count=0))

    assert frames[0]["count"] == 0
    assert frames[0]["detail"] == "已找到 0 条外部资料"


def test_stage_text_separates_the_three_empty_cases():
    """四种结果四句话：把"找到了却没能入库"说成"没搜到"是谎报。"""
    fetch = agent_tools._ExternalFetch
    source = agent_tools._external_source(_hit())
    assert agent_tools._external_stage_text(fetch([source], 1, 0.1)) == "已找到 1 条外部资料"
    assert agent_tools._external_stage_text(fetch([], 2, 0.1)) == "找到 2 条外部资料，但没能存进资料库"
    assert agent_tools._external_stage_text(fetch([], 0, 0.1, "ConnectError")) == "外部检索没成功，按主题生成"
    assert agent_tools._external_stage_text(fetch([], 0, 0.1)) == "没有搜到外部资料，按主题生成"


# ---------------------------------------------------------------- 3 分派与校验


def _seq():
    import itertools
    return itertools.count(1)


def test_stage_frame_assigns_seq_only_after_validation():
    """校验失败的帧不占号：前端"seq 连续"这条判断不能被一串缺号误导成丢帧。"""
    seq = _seq()
    assert agent_service._stage_frame({"type": "stage"}, seq) is None       # 没有 stage
    assert agent_service._stage_frame({"type": "stage", "stage": ""}, seq) is None
    assert agent_service._stage_frame(
        {"type": "stage", "stage": "x", "count": "3"}, seq) is None          # 类型不对
    assert agent_service._stage_frame(
        {"type": "stage", "stage": "x", "count": True}, seq) is None         # bool 不是 count
    assert agent_service._stage_frame(
        {"type": "stage", "stage": "x", "count": -1}, seq) is None           # 负数
    event = agent_service._stage_frame({"type": "stage", "stage": "x"}, seq)
    assert isinstance(event, StageEvent) and event.seq == 1


def test_tool_frame_dispatches_both_kinds():
    seq = _seq()
    grow = agent_service._tool_frame({"type": "node_grow", "path": None, "label": "主题"}, seq)
    stage = agent_service._tool_frame(
        {"type": "stage", "stage": "external_search", "detail": "…", "count": 0}, seq)
    unknown = agent_service._tool_frame({"type": "其它"}, _seq())

    assert isinstance(grow, NodeGrowEvent) and grow.seq == 1
    assert isinstance(stage, StageEvent) and stage.seq == 2 and stage.count == 0
    assert unknown is None


def test_stage_frame_is_encoded_on_the_existing_envelope():
    """沿用 `data: {JSON}` 信封与 `exclude_none`：开始帧里不该出现 count。"""
    frame = encode_event(StageEvent(seq=1, stage="external_search", detail="正在检索外部资料…"))
    payload = json.loads(frame[6:].strip())

    assert payload == {"seq": 1, "type": "stage", "stage": "external_search",
                       "detail": "正在检索外部资料…"}
    assert "count" not in payload


# ---------------------------------------------------------------- 4 端到端时序


def _drain_with_stamps(monkeypatch, search: Any, *, external_on: bool = True) -> list[tuple]:
    """跑一次完整 run，记录每帧到达的时刻（相对开始）。

    返回 `[(类型, 阶段, 到达秒, 事件), ...]`。`search` 若要记自己的起止时刻，用
    `_CLOCK["origin"]` 作零点——两边的秒数才是可比的。
    """
    monkeypatch.setenv(agent_tools.EXTERNAL_KNOWLEDGE_ENV, "1" if external_on else "0")
    monkeypatch.setattr(agent_tools.external_knowledge, "search_external", search)

    async def chat(system: str, user: str, **kwargs: Any) -> Any:
        return await _deep_chat(system, user)

    call = ToolCall(call_id="c-gen", name="mindmap_generate_deep",
                    arguments={"topic": TOPIC, "branches": 3,
                               "points_per_branch": 2, "max_depth": 2})
    provider = _ScriptedProvider([_tool_completion(call), _stop_completion()])
    request = AgentRunRequest(goal=TOPIC, intent="create",
                              budget=RunBudget(max_model_calls=10, max_tool_calls=5,
                                               max_seconds=60.0, max_output_tokens=4096))

    async def drain():
        stamps = []
        _CLOCK["origin"] = time.monotonic()
        with patch.object(agent_tools, "chat_async", chat):
            async for event in run(request, provider=provider,
                                   tools=agent_tools.build_registry("create_deep")):
                stamps.append((event.type, getattr(event, "stage", ""),
                               time.monotonic() - _CLOCK["origin"], event))
        return stamps

    return _run(drain())


# 零点是"这次 run 开始的时刻"，由 `_drain_with_stamps` 写入，假 search 用它换算。
_CLOCK: dict[str, float] = {}


def test_start_frame_arrives_before_the_search_finishes(monkeypatch):
    """检索要 0.2 秒：开始帧必须在它**结束之前**到消费者手里。

    这正是这条功能存在的理由——原先这 4–6 秒里界面上什么都不动。断言比较的是
    "帧到达时刻"与"真假检索的结束时刻"，不是"帧序靠前"：帧序靠前也可能是一起
    憋到最后才发。
    """
    marks: dict[str, float] = {}

    def slow_search(query: str, *, limit: int = 3):
        marks["start"] = time.monotonic() - _CLOCK["origin"]
        time.sleep(0.2)
        marks["end"] = time.monotonic() - _CLOCK["origin"]
        return [_hit()]

    stamps = _drain_with_stamps(monkeypatch, slow_search)
    begins = [stamp for stamp in stamps if stamp[1] == "external_search"
              and "count" not in stamp[3].model_dump(exclude_none=True)]
    ends = [stamp for stamp in stamps if stamp[1] == "external_search"
            and "count" in stamp[3].model_dump(exclude_none=True)]

    assert len(begins) == 1 and len(ends) == 1
    assert begins[0][3].detail == "正在检索外部资料…"
    assert ends[0][3].count == 1
    assert ends[0][3].detail == "已找到 1 条外部资料"
    # 开始帧在检索**结束之前**到达，且不晚于检索开始太久（这才是"边等边报"）
    assert begins[0][2] < marks["end"]
    assert begins[0][2] <= marks["start"] + 0.05
    # 结束帧在检索之后
    assert ends[0][2] >= marks["end"]


def test_frames_keep_the_shared_seq_counter(monkeypatch):
    """stage 帧与其它帧共用 seq：另起一套编号会让前端把正常帧当成丢帧。"""
    stamps = _drain_with_stamps(monkeypatch, lambda *a, **k: [_hit()])
    events = [stamp[3] for stamp in stamps]

    assert [event.seq for event in events] == list(range(1, len(events) + 1))
    kinds = [stamp[0] for stamp in stamps]
    assert kinds[0] == "stage"                      # 准备
    # 检索帧都夹在这一次工具调用里（tool_start 之后、tool_end 之前）
    search_indexes = [index for index, stamp in enumerate(stamps)
                      if stamp[1] == "external_search"]
    assert kinds.index("tool_start") < min(search_indexes)
    assert max(search_indexes) < kinds.index("tool_end")


def test_search_failure_still_sends_the_end_frame(monkeypatch):
    """检索抛错也要收尾：留下的开始帧会让界面永远停在"正在检索外部资料…"。"""

    def boom(*args, **kwargs):
        raise RuntimeError("代理没开")

    stamps = _drain_with_stamps(monkeypatch, boom)
    stages = [stamp[3] for stamp in stamps if stamp[1] == "external_search"]

    # 开始帧（无 count）+ 结束帧（count=0），一个都不能少
    assert [stage.count for stage in stages] == [None, 0]
    assert stages[-1].detail == "外部检索没成功，按主题生成"


def test_no_frames_when_external_search_is_off(monkeypatch):
    """关掉外部检索：一帧 stage 都没有（与 `202a7bd` 的行为一致）。"""
    called: list[str] = []

    def forbidden(*args, **kwargs):
        called.append("called")
        return []

    stamps = _drain_with_stamps(monkeypatch, forbidden, external_on=False)

    assert called == [], "开关关着时一次外部请求都不该发"
    assert [stamp[3] for stamp in stamps if stamp[1] == "external_search"] == []


@pytest.mark.parametrize("intent", ["create_deep"])
def test_stage_frames_do_not_leak_into_tool_result(monkeypatch, intent):
    """进度信号只走事件通道，不进 `ToolResult.data`：契约里没有它的位置。"""
    monkeypatch.setenv(agent_tools.EXTERNAL_KNOWLEDGE_ENV, "1")
    monkeypatch.setattr(agent_tools.external_knowledge, "search_external",
                        lambda *args, **kwargs: [_hit()])
    frames, emit = _collector()
    ctx = _ctx(emit)

    async def chat(system: str, user: str, **kwargs: Any) -> Any:
        return await _deep_chat(system, user)

    with patch.object(agent_tools, "chat_async", chat):
        result = _run(agent_tools.build_registry(intent).execute(
            ToolCall(call_id="c-gen", name="mindmap_generate_deep",
                     arguments={"topic": TOPIC, "branches": 3,
                                "points_per_branch": 2, "max_depth": 2}), ctx))

    assert result.ok
    assert "stage" not in json.dumps(result.data, ensure_ascii=False)
    assert [frame["stage"] for frame in frames if frame["type"] == "stage"] == [
        "external_search", "external_search"]
