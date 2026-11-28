"""拆不拆：`mindmap_generate_deep` → `plan_skeleton` + `expand_branch` 的三项量测。

任务要回答的三个问题，每一个都能变成数字，所以这里不写判断、只跑数：

  ① 并发展开会不会变成串行
     `service._loop` 里同一次 completion 的多个 `tool_call` 是 `for` 循环逐个
     `await` 的（`service.py` 的 `for call in completion.tool_calls:`）。本脚本的
     A 段直接测：一次 completion 带 3 个「各 sleep D 秒」的工具调用，墙钟是 D 还是 3D。

  ② 模型会不会自己把循环跑完整
     用**原型工具**（本脚本内定义，一个字节都不进 `app/`）跑五种剧本：全部展开 /
     只展开 2 支 / 漏掉中间一支 / 重复展开同一支 / 先展 2 支再 audit_tree 补齐。
     看最终交出的树、以及事件流里有没有任何一帧说"少了一支"。最后那一组用生产的
     `audit_tree` 实现，回答的是"检测交给代码之后，模型补得完吗"。

  ③ 预算与单次成本
     数模型往返（provider 调用）、工具调用、检索次数、墙钟。检索/模型各自加一段
     人为延迟，用来模拟真实环境里那一次网络往返——离线脚本量不了真实延迟，
     但能精确量"这些往返是并发的还是串行的"，而那才是拆分的全部风险所在。

对照组是**现在这一版**：一次 `mindmap_generate_deep` 调用，工具内部 `gather` 8 支。

用法（在 backend/ 下）：

    python scripts/measure_split_verdict.py
    python scripts/measure_split_verdict.py --search-delay 0.7 --model-delay 0.3
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from pydantic import BaseModel, ConfigDict, Field  # noqa: E402

from app.agent import tools as agent_tools  # noqa: E402
from app.agent.contracts import (  # noqa: E402
    AgentRunRequest,
    Completion,
    RunBudget,
    ToolCall,
    ToolResult,
    ToolSpec,
)
from app.agent.ops import apply_ops, find_node, new_node_id, normalize_ops  # noqa: E402
from app.agent.service import run  # noqa: E402
from app.agent.tools import (  # noqa: E402
    BRANCH_EVIDENCE_RULES,
    BRANCH_EXPAND_PROMPT,
    BRANCH_NO_EVIDENCE_RULES,
    DEEP_MAX_BRANCHES,
    MAX_CHILDREN,
    PLAN_PROMPT,
    ToolRegistry,
    _TOOL_SPECS,
    _branch_evidence,
    _candidate_slots,
    _fail,
    _fill_branch_colors,
    _max_tokens,
    _node_ids,
    _plan_ask,
    _search_branch,
    _skeleton_branches,
    build_registry,
)
from app.contracts.mindmap import MindMapContent, Node, clip_label  # noqa: E402
from app.contracts.ops import OpBatch  # noqa: E402
from app.contracts.source import Source, SourceKind  # noqa: E402

MATERIAL = REPO / "docs" / "RAG-DECISIONS.md"
TOPIC = "RAG 与召回：一轮评估记录与取舍"
BRANCHES = 8
POINTS = 3


def _completion(calls: list[ToolCall] | None = None) -> Completion:
    calls = calls or []
    message: dict[str, Any] = {"role": "assistant", "content": ""}
    if calls:
        message["tool_calls"] = [
            {"id": call.call_id, "type": "function",
             "function": {"name": call.name,
                          "arguments": json.dumps(call.arguments, ensure_ascii=False)}}
            for call in calls
        ]
    return Completion(message=message, tool_calls=calls,
                      finish_reason="tool_calls" if calls else "stop",
                      usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})


def _drain(request: AgentRunRequest, provider, registry: ToolRegistry | None = None) -> list:
    async def collect():
        return [event async for event in run(request, provider=provider, tools=registry)]

    return asyncio.run(collect())


@dataclass
class Probe:
    """一次运行的观测量。"""

    name: str
    elapsed: float = 0.0
    rounds: int = 0          # provider.complete_tools 被调用几次 = 模型往返次数
    tool_starts: int = 0
    tool_ends: int = 0
    searches: int = 0
    model_calls: int = 0     # chat_async 被调用几次（工具内部）
    errors: list[str] = field(default_factory=list)
    nodes: int = 0
    branch_points: list[int] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    finished: bool = False


# ---------------------------------------------------------------- A：tool_call 并发吗


class SlowArgs(BaseModel):
    delay: float = Field(gt=0)
    tag: str = Field(default="x")


def measure_tool_call_concurrency(count: int, delay: float) -> tuple[float, list[str]]:
    """一次 completion 里放 `count` 个各睡 `delay` 秒的调用，返回墙钟与工具执行顺序。

    每个调用的参数**必须不同**（`tag`）：`service._execute_once` 的防线一会把
    "连续三次指纹相同的调用"判成 `duplicate_call` 直接拒掉，那样量到的只是拒绝速度。
    """

    order: list[str] = []

    async def slow(args: SlowArgs, ctx):
        order.append(f"start:{args.tag}")
        await asyncio.sleep(args.delay)
        order.append(f"end:{args.tag}")
        return ToolResult(ok=True, content="ok", data={})

    registry = ToolRegistry()
    registry.register(ToolSpec(name="slow", description="量测用", args_model=SlowArgs, fn=slow))
    calls = [ToolCall(call_id=f"c{index}", name="slow",
                      arguments={"delay": delay, "tag": f"t{index}"})
             for index in range(count)]
    provider = _ScriptedProvider([_completion(calls), _completion([])])
    request = AgentRunRequest(goal="量测", intent="create",
                              budget=RunBudget(max_tool_calls=20, max_seconds=120))
    started = time.perf_counter()
    events = _drain(request, provider, registry)
    elapsed = time.perf_counter() - started
    errors = [event.message for event in events if event.type == "error"]
    return elapsed, order + errors


# ---------------------------------------------------------------- 脚本化模型


class _ScriptedProvider:
    """按脚本回放 `complete_tools`；脚本用完就断言，避免循环偷偷多跑一轮。"""

    def __init__(self, items: list[Any]) -> None:
        self._items = list(items)
        self.rounds = 0

    async def complete_tools(self, messages, tools, *, budget):
        self.rounds += 1
        if not self._items:
            raise AssertionError("脚本已用完：循环比预期多跑了一轮")
        item = self._items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class ModelStub:
    """工具内部那一次 `chat_async` 的替身：按 user 消息决定返回什么。

    骨架那一次回 `{"branches":[...]}`（与真实模型同形状）；分支展开那一次回
    `{"children":[...]}`，**每支固定 `POINTS` 个**，这样两条路的产出可以逐节点比。
    """

    def __init__(self, titles: tuple[str, ...], points: int, delay: float) -> None:
        self.titles = list(titles)
        self.points = points
        self.delay = delay
        self.calls = 0

    async def __call__(self, system: str, user: str, *, max_tokens=None, on_delta=None):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if system.startswith(PLAN_PROMPT):
            return {"branches": list(self.titles)}
        if "本条一级分支：" in user:
            title = user.split("本条一级分支：", 1)[1].splitlines()[0].strip()
            return {"children": [
                {"label": f"{title}-{index + 1}", "expandable": False}
                for index in range(self.points)
            ]}
        return {"children": []}


# ---------------------------------------------------------------- B：原型工具


class PlanSkeletonArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    topic: str = Field(min_length=1)
    branches: int = Field(default=BRANCHES, ge=1, le=DEEP_MAX_BRANCHES)


class ExpandBranchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    branch_id: str = Field(min_length=1)
    points_per_branch: int = Field(default=POINTS, ge=1, le=5)


async def _tool_plan_skeleton(args: PlanSkeletonArgs, ctx) -> ToolResult:
    """原型：只出骨架，把根与一级分支写进 `ctx.candidate`。

    与生产实现的差别只有一处：**不**检索、**不**展开、**不**验收。这三件事正是
    拆分方案要交给模型去编排的部分。
    """
    if ctx.request.intent != "create":
        return _fail("wrong_intent", "只有新建导图时可用。")
    if ctx.candidate is not None and ctx.candidate.root.children:
        return _fail("map_exists", "已经有一张导图了。")
    if not ctx.reserve_model_call():
        return _fail("budget_exhausted", "模型调用额度已用完，连骨架都出不了。")
    ctx.extra_model_calls += 1
    raw = await agent_tools.chat_async(
        PLAN_PROMPT, _plan_ask(args.topic, args.branches), max_tokens=_max_tokens(ctx))
    branches = _fill_branch_colors(_skeleton_branches(raw))[:DEEP_MAX_BRANCHES]
    # 一级分支没有下限（`MIN_BRANCHES` 已从产品口径里删掉），测量脚本跟着放行；
    # 只拦"一条都没有"——那是 `validate_generated_content` 唯一还会拒的形状。
    if not branches:
        return _fail("too_few_branches", "模型一条一级分支都没给。")

    label = clip_label(args.topic.strip()) or "思维导图"
    taken: set[str] = set()
    root_id = new_node_id(taken)
    taken.add(root_id)
    children: list[Node] = []
    planned: list[dict[str, str]] = []
    for branch in branches:
        branch_id = new_node_id(taken)
        taken.add(branch_id)
        children.append(Node(id=branch_id, label=branch.label, children=[],
                             color=branch.color, icon=branch.icon))
        planned.append({"id": branch_id, "label": branch.label})
    ctx.candidate = MindMapContent(
        title=label, root=Node(id=root_id, label=label, children=children),
        diagram_type="tree")
    ctx.revision += 1
    return ToolResult(
        ok=True,
        content=json.dumps({"branches": planned}, ensure_ascii=False),
        data={"branches": planned},
    )


async def _tool_expand_branch(args: ExpandBranchArgs, ctx) -> ToolResult:
    """原型：只展开指定的那一支，检索照旧在工具内部做。"""
    if ctx.candidate is None:
        return _fail("no_candidate", "还没有骨架，请先调用 plan_skeleton。")
    root = ctx.candidate.root
    node = find_node(root, args.branch_id)
    if node is None or node is root:
        return _fail("branch_not_found", f"找不到分支 {args.branch_id}。")
    if node.children:
        return _fail("already_expanded", f"分支「{node.label}」已经展开过了。")

    sources = [item for item in ctx.request.sources if (item.text or "").strip()]
    hits = await _search_branch(sources, node.label) if sources else None
    evidence = _branch_evidence(hits, sources) if hits else None
    if evidence is not None:
        ctx.record_evidence(evidence.spans)

    need = min(args.points_per_branch, MAX_CHILDREN)
    if not ctx.reserve_model_call():
        return _fail("budget_exhausted", "模型调用额度已用完，这一支没有展开。")
    ctx.extra_model_calls += 1
    system = BRANCH_EXPAND_PROMPT + (
        BRANCH_EVIDENCE_RULES if evidence is not None else BRANCH_NO_EVIDENCE_RULES)
    suffix = ("\n本支检索结果（evidence 数组）：\n" + evidence.text) if evidence else ""
    ask = (f"整体主题：{root.label}\n"
           f"本条一级分支：{node.label}\n"
           f"请给出 {need} 个子要点。{suffix}")
    raw = await agent_tools.chat_async(system, ask, max_tokens=_max_tokens(ctx))
    slots = _candidate_slots(raw, need, 0, evidence=evidence)

    drafts = [{
        "op": "add", "id": f"$d{position}", "parent_id": node.id, "index": position,
        "label": slot.label, "detail": slot.detail, "source_refs": list(slot.source_refs),
    } for position, slot in enumerate(slots)]
    content = ctx.candidate
    if drafts:
        ops, _ = normalize_ops(drafts, _node_ids(content.root))
        batch = OpBatch(batch_id=f"b{ctx.revision + 1}", base_revision=ctx.revision, ops=ops)
        try:
            content = apply_ops(content, batch)
        except Exception as exc:  # noqa: BLE001 - 原型只关心量测
            return _fail("op_failed", f"{type(exc).__name__}: {exc}")
    ctx.candidate = content
    ctx.revision += 1
    return ToolResult(
        ok=True,
        content=f"已展开分支「{node.label}」，新增 {len(slots)} 个子节点。",
        data={"branch_id": node.id, "points": len(slots)},
    )


def _split_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ToolSpec(name="plan_skeleton", description="定骨架",
                               args_model=PlanSkeletonArgs, fn=_tool_plan_skeleton))
    registry.register(ToolSpec(name="expand_branch", description="展开一支",
                               args_model=ExpandBranchArgs, fn=_tool_expand_branch))
    # 审计工具直接用**生产实现**（`app/agent/tools.py` 的 `audit_tree`）：
    # 这几个剧本要量的正是"它给的信息够不够模型补完"，换成脚本里的假实现就等于
    # 自己给自己出题。
    registry.register(_TOOL_SPECS["audit_tree"]())
    return registry


def _missing_ids(messages: list[dict[str, Any]]) -> list[str]:
    """从最近一条 `audit_tree` 的文字结果里取出"还没展开"的分支 id。

    只从 **content** 里抽，不读 `data`：模型看到的就是那段文字（工具结果的
    `data` 不进消息历史），"审计结论是否可执行"必须在同一份可见信息上量。
    """
    for message in reversed(messages):
        if message.get("role") != "tool":
            continue
        text = str(message.get("content") or "")
        if "一级分支没有任何子节点" not in text:
            return []
        return re.findall(r"\((n_[0-9a-f]+)\)", text)
    return []


def _planned_ids(messages: list[dict[str, Any]]) -> list[str] | None:
    """从 tool 消息里读出骨架给的 id 列表；模型就是这样看到它们的。

    倒着找**第一条能解析出 branches 的** tool 消息，而不是只看最近一条：
    `expand_branch` 的结果是自然语言（"已展开分支「…」，新增 3 个子节点。"），
    只看最近一条的话，展开过一次之后就再也找不到骨架了。
    """
    for message in reversed(messages):
        if message.get("role") != "tool":
            continue
        try:
            payload = json.loads(str(message.get("content") or ""))
        except ValueError:
            continue
        branches = payload.get("branches") if isinstance(payload, dict) else None
        if isinstance(branches, list):
            return [item["id"] for item in branches if isinstance(item, dict)]
    return None


class SplitProvider:
    """模拟"拆分后的模型"：按 `behavior` 决定发几支、要不要重复、什么时候收尾。

    它只看得到 `messages`——与真实模型唯一的差别是它不会忘记、也不会数错，
    而那两件事正是本次要量的东西，所以它们被写成显式的 `behavior`。
    """

    def __init__(self, behavior: str, delay: float) -> None:
        self.behavior = behavior
        self.delay = delay
        self.rounds = 0
        self._messages: list[dict[str, Any]] = []

    async def complete_tools(self, messages, tools, *, budget):
        self.rounds += 1
        self._messages = messages
        if self.delay:
            await asyncio.sleep(self.delay)
        planned = _planned_ids(messages)
        if planned is None:
            return _completion([ToolCall(
                call_id=f"plan-{self.rounds}", name="plan_skeleton",
                arguments={"topic": TOPIC, "branches": BRANCHES})])
        calls = self._calls(planned)
        return _completion(calls)

    def _calls(self, planned: list[str]) -> list[ToolCall]:
        def call(branch_id: str, tag: str) -> ToolCall:
            return ToolCall(call_id=f"{tag}-{self.rounds}-{branch_id[:6]}",
                            name="expand_branch",
                            arguments={"branch_id": branch_id,
                                       "points_per_branch": POINTS})

        if self.behavior == "all":
            return [call(item, "x") for item in planned] if self.rounds == 2 else []
        if self.behavior == "partial":       # 只展开前 2 支，然后自己觉得做完了
            return [call(item, "x") for item in planned[:2]] if self.rounds == 2 else []
        if self.behavior == "skip_middle":   # 漏掉第 3 支
            return ([call(item, "x") for index, item in enumerate(planned) if index != 2]
                    if self.rounds == 2 else [])
        if self.behavior == "duplicate":     # 第一支展开两次（参数一字不差）
            if self.rounds in (2, 3):
                return [call(planned[0], "x")]
            return []
        if self.behavior == "audit_loop":
            # 诚实模型走完整循环：先展开 2 支 → 调 audit_tree → 照着 missing 补齐。
            # 这是 `audit_tree` 唯一要证明的事：它给的信息足够让模型把剩下的补完。
            if self.rounds == 2:
                return [call(item, "x") for item in planned[:2]]
            if self.rounds == 3:
                return [ToolCall(call_id="audit-1", name="audit_tree", arguments={})]
            if self.rounds == 4:
                return [call(item, "x") for item in _missing_ids(self._messages)]
            return []
        raise AssertionError(f"未知行为 {self.behavior}")


# ---------------------------------------------------------------- 运行与统计


def _tree_stats(content: MindMapContent | None) -> tuple[int, list[int]]:
    if content is None:
        return 0, []
    total = 0
    stack = [content.root]
    while stack:
        node = stack.pop()
        total += 1
        stack.extend(node.children)
    return total, [len(branch.children) for branch in content.root.children]


def run_case(name: str, *, behavior: str | None, source: Source, titles: tuple[str, ...],
             search_delay: float, model_delay: float, registry: ToolRegistry) -> Probe:
    """跑一次运行：`behavior is None` 表示走**现在这一版**（一次工具调用）。"""
    request = AgentRunRequest(goal=TOPIC, intent="create", sources=[source],
                              budget=RunBudget(max_model_calls=100, max_tool_calls=50,
                                               max_seconds=600))
    probe = Probe(name=name)
    model = ModelStub(titles, POINTS, model_delay)

    def measuring_search(sources, query, top_k=5, *, filters=None, library=False):
        probe.searches += 1
        if search_delay:
            time.sleep(search_delay)      # 模拟一次真实的 embedding 往返（在线程里阻塞）
        from app.knowledge.service import search_parent as real_search_parent
        return real_search_parent(sources, query, top_k, filters=filters, library=library)

    if behavior is None:
        provider = _ScriptedProvider([
            _completion([ToolCall(call_id="deep", name="mindmap_generate_deep",
                                  arguments={"topic": TOPIC, "branches": BRANCHES,
                                             "points_per_branch": POINTS, "max_depth": 2})]),
            _completion([]),
        ])
    else:
        provider = SplitProvider(behavior, model_delay)

    with patch("app.agent.tools.chat_async", new=model), \
         patch("app.agent.tools.search_parent", new=measuring_search):
        started = time.perf_counter()
        events = _drain(request, provider, registry)
        probe.elapsed = time.perf_counter() - started

    probe.rounds = provider.rounds
    probe.model_calls = model.calls
    content = events[-1].content if events and events[-1].type == "done" else None
    probe.nodes, probe.branch_points = _tree_stats(content)
    probe.finished = content is not None
    for event in events:
        if event.type == "tool_end":
            probe.tool_ends += 1
        elif event.type == "tool_start":
            probe.tool_starts += 1
        elif event.type == "error":
            probe.errors.append(f"{event.code}: {event.message}")
        elif event.type == "stage":
            probe.notes.append(f"{event.stage}: {event.detail}")
    return probe


# ---------------------------------------------------------------- 报告


def report_header(text: str) -> None:
    print()
    print("=" * 78)
    print(text)
    print("=" * 78)


def main() -> None:
    parser = argparse.ArgumentParser(description="拆分方案的三项量测")
    parser.add_argument("--search-delay", type=float, default=0.7,
                        help="每次检索外加的延迟（秒），模拟一次 embedding 往返")
    parser.add_argument("--model-delay", type=float, default=0.3,
                        help="每次模型往返/工具内模型调用外加的延迟（秒）")
    parser.add_argument("--concurrency-delay", type=float, default=0.3)
    args = parser.parse_args()

    text = MATERIAL.read_text(encoding="utf-8")
    source = Source(id="m1", kind=SourceKind.document, status="parsed",
                    filename=MATERIAL.name, text=text)
    titles = tuple(line[3:].strip() for line in text.splitlines()
                   if line.startswith("## "))
    titles = titles[:BRANCHES] + tuple(
        f"补充分支{index + 1}" for index in range(max(0, BRANCHES - len(titles))))

    report_header("材料与参数")
    print(f"材料：{MATERIAL.name}，{len(text):,} 字，## ×{text.count(chr(10) + '## ')}")
    print(f"骨架：{BRANCHES} 支 → {'、'.join(titles[:BRANCHES])}")
    print(f"延迟假设：检索 {args.search_delay}s / 模型 {args.model_delay}s")

    # ---- A：一次 completion 里多个 tool_call 是并发还是串行 -------------------
    report_header("① 同一次 completion 里的多个 tool_call：并发还是串行")
    for count in (1, 3):
        elapsed, order = measure_tool_call_concurrency(count, args.concurrency_delay)
        verdict = "串行" if elapsed > args.concurrency_delay * count * 0.8 else "并发"
        print(f"{count} 个 tool_call（各 {args.concurrency_delay}s）：墙钟 {elapsed:.2f}s"
              f" → {verdict}")
        print(f"    执行顺序：{' | '.join(order)}")

    # ---- B：四种"模型不听话"的剧本 ------------------------------------------
    report_header("② 拆开后模型少展开 / 漏支 / 重复展开，会发生什么")
    probes: list[Probe] = []
    probes.append(run_case("现在这一版（一次调用）", behavior=None, source=source,
                           titles=titles, search_delay=args.search_delay,
                           model_delay=args.model_delay,
                           registry=build_registry("create", has_library=False)))
    for behavior, label in (
        ("all", "拆分：展开全部 8 支"),
        ("partial", "拆分：只展开前 2 支就收尾"),
        ("skip_middle", "拆分：漏掉第 3 支"),
        ("duplicate", "拆分：重复展开同一支"),
        ("audit_loop", "拆分：先展 2 支 → audit_tree → 补齐"),
    ):
        probes.append(run_case(label, behavior=behavior, source=source, titles=titles,
                               search_delay=args.search_delay,
                               model_delay=args.model_delay,
                               registry=_split_registry()))

    columns = ("剧本", "墙钟", "模型往返", "工具调用", "工具内模型", "检索", "节点",
               "各支要点", "结束方式")
    rows = []
    for probe in probes:
        rows.append((
            probe.name, f"{probe.elapsed:.2f}s", probe.rounds, probe.tool_starts,
            probe.model_calls, probe.searches, probe.nodes,
            ",".join(str(item) for item in probe.branch_points) or "-",
            ("done" if probe.finished else "error") + (
                f"（{'; '.join(probe.errors)}）" if probe.errors else ""),
        ))
    widths = [max(len(str(row[index])) for row in [columns, *rows])
              for index in range(len(columns))]
    for index, row in enumerate([columns, *rows]):
        line = "  ".join(str(value).ljust(widths[position])
                         for position, value in enumerate(row))
        print(line)
        if index == 0:
            print("-" * len(line))

    # ---- C：结论所需的读数 ---------------------------------------------------
    report_header("③ 结论读数")
    baseline, full, partial = probes[0], probes[1], probes[2]
    print(f"现方案：1 次工具调用 / {baseline.rounds} 次模型往返 / "
          f"{baseline.searches} 次检索，墙钟 {baseline.elapsed:.2f}s，"
          f"{baseline.nodes} 个节点、各支要点 {baseline.branch_points}")
    print(f"拆开后（听话）：{full.tool_starts} 次工具调用 / {full.rounds} 次模型往返 / "
          f"{full.searches} 次检索，墙钟 {full.elapsed:.2f}s，"
          f"{full.nodes} 个节点、各支要点 {full.branch_points}")
    print(f"墙钟倍数：拆开后是现方案的 {full.elapsed / baseline.elapsed:.2f}×"
          if baseline.elapsed else "墙钟：基线为 0")
    print(f"少展开剧本：{partial.nodes} 个节点、各支要点 {partial.branch_points}，"
          f"结束方式 {'done' if partial.finished else 'error'}"
          f"，事件流里的报错 {partial.errors or '无'}")
    audited = probes[-1]
    print(f"查了审计再补的剧本：{audited.nodes} 个节点、各支要点 {audited.branch_points}，"
          f"结束方式 {'done' if audited.finished else 'error'}")
    print(f"预算：拆开后工具调用 {full.tool_starts} 次（max_tool_calls=50），"
          f"模型往返 {full.rounds} 次（max_model_calls=100）")
    print()
    print("少展开剧本里出现过的 stage 帧（找 NO_PROGRESS / 收尾校验）：")
    for note in partial.notes:
        print(f"    {note}")


if __name__ == "__main__":
    main()
