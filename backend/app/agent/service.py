"""AI 代理（F02 / F18）。

位置：用户给一个目标（"设计一款智能家居产品"），先拆成阶段，再逐阶段生成分支。

状态：**骨架**。plan() 返回固定的三段结构，不含模型调用；
真正实现要做的是：目标 → 阶段划分 → 每阶段调用流水线 → 合并成一棵树。

设计约束：
  - 每一步都要记录来源与依据，不只是结果
  - 代理产出的是候选，用户确认后才写进导图（与 F15/F16 同一条原则）
"""
from __future__ import annotations

import asyncio
import itertools
import json
import time
from typing import Any, AsyncIterator

from pydantic import BaseModel, ConfigDict, Field

from app.agent.contracts import (
    AgentEvent,
    AgentRunRequest,
    DoneEvent,
    ErrorEvent,
    OpsEvent,
    RunBudget,
    RunContext,
    StageEvent,
    ToolCall,
    ToolProvider,
    ToolResult,
    ToolStartEvent,
    ToolEndEvent,
)
from app.agent.provider import default_provider
from app.agent.tools import ToolRegistry, build_registry
from app.contracts.mindmap import MindMapContent
from app.generation.service import GenerationError

DEFAULT_STAGES = ["目标澄清", "要点拆解", "分支展开", "风险与验证"]


class AgentPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str
    stages: list[str] = Field(default_factory=list)
    implemented: bool = False


class AgentTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str = Field(min_length=1, max_length=500)


def plan(goal: str) -> AgentPlan:
    """把目标拆成阶段。

    TODO(F02)：用模型做阶段划分，并为每个阶段生成"需要哪些输入"，
    再逐阶段调用 pipeline.run()，最后合并。
    现在返回固定阶段，只用来固定接口形状与前端交互顺序。
    """
    return AgentPlan(goal=goal, stages=list(DEFAULT_STAGES), implemented=False)


# ---------------------------------------------------------------- 多轮工具循环

_SYSTEM_BASE = (
    "你在操作一张思维导图，可以通过工具读写它。"
    "只能调用下面列出的工具；需要改图时用工具，不要只在文字里描述。"
)

# 按意图分叉指令，不是一套 system 打天下。实测教训：system 里只要写了
# "需要外部信息必须先查"，模型就会对"给考研数学复习计划生成导图"这种
# 完全不需要检索的请求也先调一次检索（10 次里 10 次都这样）。
_INTENT_RULES = {
    "create": "这次是新建导图。直接生成，不要先去检索资料。",
    "expand": "这次是展开某个已选中的节点。只补充子节点，不要重写整张图。",
    "edit": "这次是精确修改已有导图。必须按节点 id 改，禁止整张重新生成。",
}


def outline_text(content: MindMapContent | None, limit: int = 200) -> str:
    """把树压成带 id 的大纲，让模型能引用具体节点。

    没有 id 的大纲模型就只能靠文字猜"改哪一个"，而文字是会被它自己改写的。
    """
    if content is None:
        return "（当前没有导图）"
    lines: list[str] = []

    def walk(node, depth: int) -> None:
        if len(lines) >= limit:
            return
        lines.append("  " * depth + f"[{node.id}] {node.label}")
        for child in node.children:
            walk(child, depth + 1)

    walk(content.root, 0)
    if len(lines) >= limit:
        lines.append(f"……（只列前 {limit} 个节点）")
    return "\n".join(lines)


def _system_text(request: AgentRunRequest, content: MindMapContent | None) -> str:
    return "\n".join([
        _SYSTEM_BASE,
        _INTENT_RULES.get(request.intent, ""),
        "当前导图：",
        outline_text(content),
    ]).strip()


def _initial_messages(request: AgentRunRequest) -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": _system_text(request, request.snapshot)},
        {"role": "user", "content": request.goal},
    ]


def _refresh_outline(messages: list[dict[str, Any]], ctx: RunContext) -> None:
    """每轮把第一条 system 消息换成最新的树。

    不刷新的话，模型看到的大纲始终是请求开始时的快照：它刚生成的那些节点
    根本没有 id 可引用，于是下一轮只能整张重来——那正是这次改造要消灭的行为。
    原地替换而不是追加，是因为追加会让消息历史随轮数线性增长，几轮之后
    光大纲就把上下文吃满了。
    """
    messages[0] = {"role": "system", "content": _system_text(ctx.request, ctx.candidate)}


def _dedupe_key(call: ToolCall) -> str:
    """同工具同参数的指纹。

    规范化用 sort_keys：模型两次给的参数键序可能不同，但那显然是同一次调用。
    """
    return call.name + "\0" + json.dumps(call.arguments, sort_keys=True, ensure_ascii=False, default=str)


def _args_summary(call: ToolCall, width: int = 80) -> dict[str, Any]:
    """事件流里只放参数摘要：完整参数可能带整份来源正文。"""
    summary: dict[str, Any] = {}
    for key, value in list(call.arguments.items())[:6]:
        text = value if isinstance(value, (int, float, bool)) else str(value)
        if isinstance(text, str) and len(text) > width:
            text = text[:width] + "…"
        summary[key] = text
    return summary


def _merge_usage(total: dict[str, Any], usage: dict[str, Any]) -> dict[str, Any]:
    merged = dict(total)
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if isinstance(usage.get(key), int):
            merged[key] = int(merged.get(key, 0)) + int(usage[key])
    return merged


def _remaining_seconds(budget: RunBudget, started: float) -> float:
    return budget.max_seconds - (time.monotonic() - started)


def _budget_limit(budget: RunBudget, model_calls: int, tool_calls: int, started: float) -> str | None:
    if model_calls >= budget.max_model_calls:
        return "max_model_calls"
    if tool_calls >= budget.max_tool_calls:
        return "max_tool_calls"
    if _remaining_seconds(budget, started) <= 0:
        return "max_seconds"
    return None


_BUDGET_HINT = {
    "max_model_calls": "模型调用次数已用完",
    "max_tool_calls": "工具调用次数已用完",
    "max_seconds": "这次运行超过了时间上限",
}


async def _execute_once(call: ToolCall, ctx: RunContext, registry: ToolRegistry) -> ToolResult:
    """带两级幂等的执行。

    call_id 幂等是协议要求的：模型偶尔会把同一个 tool_call 重复发一遍。
    参数级去重是防"同一轮里两个不同 id 但参数完全一样"的调用——实测模型
    并行发起两个 node_expand(n1) 是会发生的事，执行两次就会加两遍子节点。
    """
    key = _dedupe_key(call)

    cached = ctx.executed.get(call.call_id)
    if cached is not None:
        cached_key, cached_result = cached
        if cached_key != key:
            # 同一个 call_id 换了参数是协议违规。返回旧结果会让模型以为新参数
            # 生效了，而实际上没有任何改动发生。
            return ToolResult(
                ok=False,
                content=f"call_id {call.call_id} 已经用于另一次参数不同的调用，请换一个新的 call_id 重试。",
                data={"code": "call_id_conflict"},
            )
        return cached_result

    spec = registry.get(call.name)
    readonly = spec is not None and spec.readonly
    if readonly:
        # 只读工具按参数去重是安全的：同样的查询必然得到同样的结果。
        # 写操作不能这样——即使参数一字不差，第二次执行时树已经变了，
        # 返回上次的结果等于谎报成功（实测：改成 X、Y、再改回 X，第三次被
        # 缓存吃掉，模型以为改成功了，实际停在 Y）。
        hit = ctx.dedupe.get(key)
        if hit is not None:
            ctx.executed[call.call_id] = (key, hit)
            return hit

    result = await registry.execute(call, ctx)
    ctx.executed[call.call_id] = (key, result)
    if readonly:
        ctx.dedupe[key] = result
    return result


async def run(
    request: AgentRunRequest,
    *,
    provider: ToolProvider | None = None,
    tools: ToolRegistry | None = None,
) -> AsyncIterator[AgentEvent]:
    """跑一次多轮工具循环，边跑边发事件。

    终止只有三种：模型以 stop 收尾（done）、预算耗尽（error）、上游失败（error）。
    不存在"静默截断后假装成功"——那会让前端拿到一棵看起来正常、实际缺了一半
    的树，比直接报错难排查得多。
    """
    active_provider = provider or default_provider()
    registry = tools if tools is not None else build_registry()
    budget = request.budget
    ctx = RunContext(
        request=request,
        candidate=request.snapshot,
        sources_by_id={source.id: source for source in request.sources},
    )
    messages = _initial_messages(request)
    seq = itertools.count(1)
    started = time.monotonic()
    model_calls = 0
    tool_calls = 0
    failures = 0
    usage_total: dict[str, Any] = {}

    yield StageEvent(
        seq=next(seq),
        stage="准备",
        detail=f"{request.intent} 意图，{len(registry.names())} 个工具可用",
    )

    while True:
        limit = _budget_limit(budget, model_calls + ctx.extra_model_calls, tool_calls, started)
        if limit is not None:
            yield ErrorEvent(
                seq=next(seq), code="budget_exhausted",
                message=_BUDGET_HINT[limit], limit=limit,
            )
            return

        # 每轮刷新大纲：模型必须看到上一轮产出节点的真实 id，否则它无法精确改
        _refresh_outline(messages, ctx)
        model_calls += 1
        try:
            # 用剩余时间做超时，而不是让单次调用跑到 provider 自己的 60 秒上限：
            # 否则总时限形同虚设，一轮慢调用就能把预算耗尽而循环还在等。
            completion = await asyncio.wait_for(
                active_provider.complete_tools(messages, registry.specs(), budget=budget),
                timeout=max(0.1, _remaining_seconds(budget, started)),
            )
        except asyncio.CancelledError:
            # 必须原样抛出：吞掉它会让"用户点了取消"变成一次正常结束
            raise
        except (asyncio.TimeoutError, TimeoutError):
            yield ErrorEvent(
                seq=next(seq), code="budget_exhausted",
                message=_BUDGET_HINT["max_seconds"], limit="max_seconds",
            )
            return
        except GenerationError as exc:
            yield ErrorEvent(seq=next(seq), code="generation_failed", message=str(exc))
            return

        messages.append(completion.message)
        usage_total = _merge_usage(usage_total, completion.usage)

        if not completion.tool_calls:
            if completion.finish_reason == "stop":
                yield DoneEvent(seq=next(seq), content=ctx.candidate, usage=usage_total)
            else:
                yield ErrorEvent(
                    seq=next(seq), code="unexpected_finish",
                    message=f"模型以 {completion.finish_reason or '未知原因'} 结束，且没有调用任何工具",
                )
            return

        for call in completion.tool_calls:
            if tool_calls >= budget.max_tool_calls:
                yield ErrorEvent(
                    seq=next(seq), code="budget_exhausted",
                    message=_BUDGET_HINT["max_tool_calls"], limit="max_tool_calls",
                )
                return

            yield ToolStartEvent(
                seq=next(seq), call_id=call.call_id, name=call.name,
                args_summary=_args_summary(call),
            )
            begun = time.monotonic()
            try:
                result = await asyncio.wait_for(
                    _execute_once(call, ctx, registry),
                    timeout=max(0.1, _remaining_seconds(budget, started)),
                )
            except (asyncio.TimeoutError, TimeoutError):
                yield ErrorEvent(
                    seq=next(seq), code="budget_exhausted",
                    message=_BUDGET_HINT["max_seconds"], limit="max_seconds",
                )
                return
            tool_calls += 1
            if not result.ok:
                # 模型给错参数是常态，它看到错误后下一轮通常能改对；但一直错下去
                # 就是在烧调用，超过容忍次数直接收尾。
                failures += 1
                if failures > budget.max_repairs:
                    yield ErrorEvent(
                        seq=next(seq), code="too_many_failures",
                        message=f"工具连续失败 {failures} 次仍未成功，已停止。",
                        limit="max_repairs",
                    )
                    return
            yield ToolEndEvent(
                seq=next(seq), call_id=call.call_id, name=call.name, ok=result.ok,
                result_summary=result.data,
                latency_ms=int((time.monotonic() - begun) * 1000),
            )

            # 工具若产生了 op 批次，单独发帧：前端据此增量应用，不必等整棵树。
            # 用 data 而不是解析 content，省掉一次 JSON 往返，也避免把回给模型的
            # 自然语言当成协议数据。
            for batch in result.data.get("batches") or []:
                yield OpsEvent(seq=next(seq), call_id=call.call_id, batch=batch)

            messages.append({
                "role": "tool",
                "tool_call_id": call.call_id,
                "content": result.content,
            })

