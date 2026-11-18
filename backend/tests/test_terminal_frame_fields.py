"""四条收尾路径的 `done` 帧必须同形。

同一个"生成成功"，前端拿到的帧不该因为**这次是怎么停的**而少字段：

| 收尾方式 | 位置 |
| --- | --- |
| 模型自己 stop | `_loop` 里那条 `DoneEvent` |
| 预算耗尽（有候选） | `_budget_stop` |
| 空转停止（有候选） | `_no_progress_stop` |
| 同一个 run_id 再投一次 | `router._replay` |

后三条原先都不带 `model` / `reasoning`：用户看到的是"同样落到画布，一次能展开
思考过程、一次什么都没有"，而界面上没有任何线索说明差别来自哪种收尾。重放那条
更隐蔽——同一个 run 第一次有、第二次没有，看起来像重放丢了东西。

`model` 只能从 provider 上读（模型名是它在**发请求那一刻**决定的），`reasoning`
取最后一次响应的 `reasoning_content`：预算/空转都是"这一轮跑完、可以交付"，
最后那次思考才是决定它的那次。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from pydantic import BaseModel, Field

from app.agent import router as agent_router
from app.agent import runs
from app.agent.contracts import (
    AgentRunRequest,
    Completion,
    DoneEvent,
    RunBudget,
    ToolCall,
    ToolResult,
    ToolSpec,
)
from app.agent.service import NO_PROGRESS_ABORT, run
from app.agent.tools import ToolRegistry
from app.contracts.mindmap import MindMapContent

TREE: MindMapContent = MindMapContent(**{
    "title": "示例",
    "root": {"id": "root", "label": "根", "children": []},
})

REASONING = "先看大纲，再决定改哪一支。"


def _collect(request: AgentRunRequest, provider: Any, registry: ToolRegistry) -> list:
    async def drain() -> list:
        return [event async for event in run(request, provider=provider, tools=registry)]

    return asyncio.run(drain())


class _NamedProvider:
    """带 `model_name` 的假 provider：真 provider 在发请求时把它记在实例上。"""

    model_name = "deepseek-reasoner"

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.calls = 0

    async def complete_tools(self, messages, tools, *, budget):
        self.calls += 1
        if not self._script:
            return _with_reasoning()
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _with_reasoning() -> Completion:
    return Completion(
        message={"role": "assistant", "content": "", "reasoning_content": REASONING},
        tool_calls=[], finish_reason="stop", usage={},
    )


class _NoopArgs(BaseModel):
    note: str = Field(default="x")


def _noop_registry() -> ToolRegistry:
    """一个"什么都不改树"的工具：空转判据（树的内容指纹没变）靠它累积。"""
    registry = ToolRegistry()
    registry.register(ToolSpec(
        name="peek", description="只看不改",
        args_model=_NoopArgs,
        fn=lambda args, ctx: ToolResult(ok=True, content="看过了", data={}),
        readonly=True,
    ))
    return registry


def _grow_registry() -> ToolRegistry:
    """写一次候选树就再也不动它：下一轮预算耗尽时树已经在，走 done 而不是 error。"""
    registry = ToolRegistry()

    def grow(args: _NoopArgs, ctx) -> ToolResult:
        ctx.candidate = TREE
        ctx.revision += 1
        return ToolResult(ok=True, content="已生成", data={})

    registry.register(ToolSpec(
        name="grow", description="写候选树", args_model=_NoopArgs, fn=grow))
    return registry


def _tool_call(name: str) -> ToolCall:
    return ToolCall(call_id=f"call-{name}", name=name, arguments={"note": "x"})


def _completion_with_call(name: str) -> Completion:
    call = _tool_call(name)
    return Completion(
        message={
            "role": "assistant", "content": "", "reasoning_content": REASONING,
            "tool_calls": [{
                "id": call.call_id, "type": "function",
                "function": {"name": name, "arguments": json.dumps(call.arguments)},
            }],
        },
        tool_calls=[call], finish_reason="tool_calls", usage={},
    )


def _last_done(events: list) -> DoneEvent:
    dones = [event for event in events if isinstance(event, DoneEvent)]
    assert dones, "这一轮没有 done 帧"
    return dones[-1]


# ---------------------------------------------------------------- 预算耗尽


def test_budget_exhausted_done_carries_model_and_reasoning():
    """额度用尽但树已经在：这是"交付"，不是失败，帧要与正常收尾同形。"""
    provider = _NamedProvider([_completion_with_call("grow")])
    request = AgentRunRequest(
        goal="整理", budget=RunBudget(max_model_calls=1, max_tool_calls=50))
    done = _last_done(_collect(request, provider, _grow_registry()))

    assert isinstance(done.content, MindMapContent)
    assert done.mode == "model"
    assert done.model == "deepseek-reasoner"
    assert done.reasoning == REASONING


# ---------------------------------------------------------------- 空转停止


def test_no_progress_stop_done_carries_model_and_reasoning():
    """连续不改树到硬阈值：同样是一条成功（有候选），字段一个都不能少。"""
    provider = _NamedProvider(
        [_completion_with_call("peek") for _ in range(NO_PROGRESS_ABORT + 2)])
    request = AgentRunRequest(
        goal="整理",
        budget=RunBudget(max_model_calls=NO_PROGRESS_ABORT + 4, max_tool_calls=50),
        snapshot=TREE,
    )
    done = _last_done(_collect(request, provider, _noop_registry()))

    assert isinstance(done.content, MindMapContent)
    assert done.model == "deepseek-reasoner"
    assert done.reasoning == REASONING


# ---------------------------------------------------------------- 重放


def _frames_of(record: runs.RunRecord) -> list[dict]:
    async def drain() -> list[str]:
        return [frame async for frame in agent_router._replay(record)]

    frames: list[dict] = []
    for raw in asyncio.run(drain()):
        if not raw.startswith("data:"):
            continue
        body = raw[len("data:"):].strip()
        if body and body != "[DONE]":
            frames.append(json.loads(body))
    return frames


def test_replay_returns_the_same_model_and_reasoning():
    """同一个 run_id 第二次投递：帧要与第一次一样，包括"这次走的哪条路"。"""
    claim = runs.begin("run-replay", {"intent": "create", "goal": "整理"})
    assert claim.claimed
    runs.finish(
        "run-replay", runs.STATUS_DONE,
        candidate=TREE, mode="model", model="deepseek-reasoner", reasoning=REASONING,
    )

    record = runs.get("run-replay")
    assert record is not None
    done = [frame for frame in _frames_of(record) if frame["type"] == "done"][-1]
    assert done["model"] == "deepseek-reasoner"
    assert done["reasoning"] == REASONING
    assert done["mode"] == "model"


def test_replay_of_an_old_row_omits_the_two_fields():
    """老行（这两列还是 NULL）重放时整条字段不存在，而不是写成空串或 "chat"。"""
    runs.begin("run-legacy", {"intent": "create", "goal": "整理"})
    runs.finish("run-legacy", runs.STATUS_DONE, candidate=TREE, mode="model")

    record = runs.get("run-legacy")
    assert record is not None
    done = [frame for frame in _frames_of(record) if frame["type"] == "done"][-1]
    assert "model" not in done
    # `reasoning` 在契约里是 `str = ""`（不是 None）：空串会出现在帧里，
    # 但前端读到的就是"没有思考过程"，与加这个字段之前一样。
    assert done.get("reasoning", "") == ""
