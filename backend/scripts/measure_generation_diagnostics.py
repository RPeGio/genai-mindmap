"""结构体检（`check`）与语义复核（`annotate_nli`）接进分片生成之后的成本实测。

**测的是成本，不是生成质量**。本机没有 `MINDMAP_LLM_*` 密钥（也没有 NLI 的
onnx 权重），所以：

  - "模型"是脚本化的（复用 `measure_deep_slicing.ScriptedModel`）：它只按固定
    规则使用**它在 prompt 里真正看到的文字**，引用照抄本支 evidence。树与引用
    因此完全确定，A/B 两次的差只可能来自这次接线本身。
  - "NLI 推理"用**等价延迟**代替（`--nli-latency`，缺省按 `source_refs.py` 里
    记的单条实测 15 ms / 44 ms 各跑一遍）。真模型是 CPU 计算而不是 sleep，墙钟
    等价、CPU 不燃烧，但**事件循环是否被堵住**这件事在两种实现下是同一个判据，
    所以它可测。

量四件事（就是决定"要不要默认开"的那四件）：

  1. `check()` 单次耗时（在同一棵真实树上重复 N 次）；
  2. `annotate_nli()` 的**判定条数**与总耗时——条数就是成本，它是 100 上限
     与实际引用条数的较小值；
  3. 接线前后的**一次生成总耗时**差（A/B，同材料同脚本模型）；
  4. NLI 期间事件循环的最大停顿（走 `to_thread` 才有意义的那一条）。

用法（在 `backend/` 下）：

    python scripts/measure_generation_diagnostics.py
    python scripts/measure_generation_diagnostics.py --branches 12 --nli-latency 0.044
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
REPO = BACKEND.parent
for entry in (BACKEND, SCRIPTS):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

# 与 `measure_deep_slicing` 一样：环境必须在 import 应用之前定下来，且数据库要隔离。
_TMP = tempfile.mkdtemp(prefix="measure-diag-")
os.environ["MINDMAP_DB_PATH"] = str(Path(_TMP) / "measure.db")
os.environ.pop("MINDMAP_HYBRID_SEARCH", None)
os.environ.setdefault("MINDMAP_PARENT_RETRIEVAL", "1")
os.environ.setdefault("MINDMAP_EXTERNAL_KNOWLEDGE", "0")
os.environ.pop("MINDMAP_NLI_ENABLED", None)
os.environ.pop("MINDMAP_NLI_ON_GENERATE", None)

from unittest.mock import patch  # noqa: E402

import measure_deep_slicing as base  # noqa: E402

from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall  # noqa: E402
from app.agent.tools import (  # noqa: E402
    MAX_NLI_CHECKS,
    build_registry,
)
from app.contracts.source import Source, SourceKind  # noqa: E402
from app.optimization import nli as nli_module  # noqa: E402
from app.optimization.service import check  # noqa: E402
from app.optimization.source_refs import annotate_nli  # noqa: E402

# 单条 NLI 推理的实测区间（`source_refs.py::MAX_NLI_CHECKS` 的注释：15–44 ms）。
NLI_FAST, NLI_SLOW = 0.015, 0.044


# ---------------------------------------------------------------- 一次真实生成


def make_ctx(text: str, titles: tuple[str, ...], source_id: str) -> RunContext:
    source = Source(id=source_id, kind=SourceKind.document, status="parsed",
                    filename="RAG-DECISIONS.md", text=text)
    request = AgentRunRequest(
        goal="RAG 与召回：一轮评估记录与取舍", intent="create", sources=[source],
        budget=RunBudget(max_model_calls=80, max_tool_calls=12, max_output_tokens=65536),
    )
    return RunContext(request=request, candidate=None,
                      sources_by_id={source.id: source})


async def generate_once(ctx: RunContext, text: str, titles: tuple[str, ...],
                        source_id: str, *, branches: int, wired: bool,
                        on_generate: bool = False):
    """跑一次 `mindmap_generate_deep`（真实检索、真实校验，脚本化模型）。"""
    model = base.ScriptedModel(spans=base.segment(text), text=text,
                               titles=titles, source_id=source_id)
    registry = build_registry("create", has_sources=True, has_library=False)
    call = ToolCall(call_id="c-deep", name="mindmap_generate_deep",
                    arguments={"topic": "RAG 与召回：一轮评估记录与取舍",
                               "branches": branches,
                               "points_per_branch": base.POINTS_PER_BRANCH,
                               "max_depth": 3})
    patches = [patch("app.agent.tools.chat_async", new=model)]
    if not wired:
        # 接线前的等价物：`check()` 与 `annotate_nli()` 各返回空列表。这样 A/B
        # 两次除这两处之外逐字节相同，时间差只能是它们带来的。
        patches += [
            patch("app.agent.tools.check", new=lambda content, sources=None: []),
            patch("app.agent.tools.annotate_nli", new=lambda content, sources: []),
            patch("app.agent.tools.nli.on_generate", new=lambda: False),
        ]
    elif on_generate:
        os.environ["MINDMAP_NLI_ENABLED"] = "1"
        os.environ["MINDMAP_NLI_ON_GENERATE"] = "1"

    started = time.perf_counter()
    for context in patches:
        context.start()
    try:
        result = await registry.execute(call, ctx)
    finally:
        for context in reversed(patches):
            context.stop()
        os.environ.pop("MINDMAP_NLI_ENABLED", None)
        os.environ.pop("MINDMAP_NLI_ON_GENERATE", None)
    return result, time.perf_counter() - started


def count_refs(root) -> int:
    stack, total = [root], 0
    while stack:
        node = stack.pop()
        total += len(node.source_refs)
        stack.extend(node.children)
    return total


def median(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def all_refs(root) -> list:
    stack, refs = [root], []
    while stack:
        node = stack.pop()
        refs.extend(node.source_refs)
        stack.extend(node.children)
    return refs


# ---------------------------------------------------------------- 1. check 单次


def bench_check(content, sources, *, rounds: int = 300) -> dict:
    started = time.perf_counter()
    for _ in range(rounds):
        check(content, sources)
    total = time.perf_counter() - started
    # 单次耗时在微秒级，一次 perf_counter 的噪声就与它同量级，所以取 N 次平均。
    return {"rounds": rounds, "mean_ms": total / rounds * 1000, "total_ms": total * 1000}


# ---------------------------------------------------------------- 2. NLI 条数


def bench_nli(content, sources, *, latency: float) -> dict:
    """用等价延迟的替身 judge 跑一遍 `annotate_nli`，数判定条数与总耗时。

    替身**每次都返回中性**：这不是在猜真实模型的判定分布，而是量"每条引用都
    会花一次推理"这个成本事实。真实判定分布见报告里的"无法实测"那一行——
    本机没有 onnx 权重，编一个分布比不给更糟。
    """
    calls: list[float] = []

    def fake_judge(claim: str, evidence: str) -> dict:
        calls.append(time.perf_counter())
        if latency:
            time.sleep(latency)
        return {"score": 0.5, "label": "neutral", "probs": {}}

    with patch("app.optimization.nli.available", return_value=True), \
            patch("app.optimization.nli.judge", new=fake_judge):
        started = time.perf_counter()
        diagnostics = annotate_nli(content, sources)
        elapsed = time.perf_counter() - started
    return {
        "judged": len(calls),
        "diagnostics": len(diagnostics),
        "contradicted": sum(1 for item in diagnostics
                            if item.code == "SOURCE_REF_CONTRADICTED"),
        "elapsed_ms": elapsed * 1000,
        "latency_ms": latency * 1000,
    }


def quote_stats(refs) -> dict:
    """分片生成的引用**有没有 quote**——`annotate_nli` 那条跳过规则的触发面。"""
    with_quote = sum(1 for ref in refs if (ref.quote or "").strip())
    with_range = sum(1 for ref in refs
                     if ref.start is not None and ref.end is not None)
    neither = sum(1 for ref in refs
                  if not (ref.quote or "").strip()
                  and (ref.start is None or ref.end is None))
    return {"total": len(refs), "with_quote": with_quote,
            "with_range": with_range, "skipped_would_be": neither}


def claim_stats(content) -> dict:
    """`nli.judge(claim, evidence)` 的两个参数分别有多长。

    claim 现在是**论断**（`label` + `detail`，见 `source_refs._claim_of`），不再是
    节点标题。这里把两边长度量出来，好判断 `MAX_CLAIM_CHARS` 的 340 字符预算有没有
    被撞到——撞到就意味着 `detail` 被截，卡在 512 token 上限上的那一对就快挤到
    premise 了。分片生成里 `detail` 为空时 claim 等于 label，与旧口径相同。
    """
    labels: list[int] = []
    claims: list[int] = []
    evidences: list[int] = []
    stack = [content.root]
    while stack:
        node = stack.pop()
        for ref in node.source_refs:
            labels.append(len(node.label))
            claims.append(len(node.label) + len(node.detail or ""))
            evidences.append(len((ref.quote or "").strip()))
        stack.extend(node.children)
    if not labels:
        return {"claims": 0, "median_label": 0, "max_label": 0,
                "median_claim": 0, "median_evidence": 0, "min_evidence": 0}
    labels.sort()
    claims.sort()
    evidences.sort()
    return {"claims": len(labels),
            "median_label": labels[len(labels) // 2],
            "max_label": labels[-1],
            "median_claim": claims[len(claims) // 2],
            "median_evidence": evidences[len(evidences) // 2],
            "min_evidence": evidences[0]}


# ---------------------------------------------------------------- 3. 事件循环


async def probe_event_loop(content, sources, *, latency: float) -> dict:
    """同一段 NLI：同步调用 vs `to_thread`，心跳能跳几下。

    这才是"要不要 `to_thread`"的决定性对照：两种写法跑的是同一个函数、同一份
    数据，只有"占不占事件循环"不同。心跳间隔 5 ms，停顿峰值 ≈ NLI 总耗时就是
    堵住了，峰值维持在几毫秒就是没堵。
    """
    result: dict[str, float] = {}
    for label, threaded in (("同步直接调", False), ("to_thread", True)):
        gaps: list[float] = []
        stop = False

        async def ticker() -> None:
            last = time.perf_counter()
            while not stop:
                await asyncio.sleep(0.005)
                now = time.perf_counter()
                gaps.append(now - last)
                last = now

        beat = asyncio.create_task(ticker())
        try:
            with patch("app.optimization.nli.available", return_value=True), \
                    patch("app.optimization.nli.judge", new=_slow_judge(latency)):
                started = time.perf_counter()
                if threaded:
                    await asyncio.to_thread(annotate_nli, content, sources)
                else:
                    annotate_nli(content, sources)
                elapsed = time.perf_counter() - started
        finally:
            stop = True
            await beat
        result[label] = max(gaps) * 1000 if gaps else 0.0
        result[f"{label}·本次耗时"] = elapsed * 1000
        result[f"{label}·心跳"] = float(len(gaps))
    return result


def _slow_judge(latency: float):
    def judge(claim: str, evidence: str) -> dict:
        if latency:
            time.sleep(latency)
        return {"score": 0.5, "label": "neutral", "probs": {}}
    return judge


async def generate_with_ticker(ctx: RunContext, text: str, titles: tuple[str, ...],
                               source_id: str, *, branches: int, latency: float,
                               on_generate: bool):
    """跑一次生成，同时用一个 5 ms 心跳量事件循环的最大停顿。"""
    gaps: list[float] = []
    stop = False

    async def ticker() -> None:
        last = time.perf_counter()
        while not stop:
            await asyncio.sleep(0.005)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    beat = asyncio.create_task(ticker())
    try:
        if on_generate:
            with patch("app.optimization.nli.available", return_value=True), \
                    patch("app.optimization.nli.judge",
                          new=lambda claim, evidence: (
                              time.sleep(latency),
                              {"score": 0.5, "label": "neutral", "probs": {}})[1]):
                result, elapsed = await generate_once(
                    ctx, text, titles, source_id, branches=branches,
                    wired=True, on_generate=True)
        else:
            result, elapsed = await generate_once(
                ctx, text, titles, source_id, branches=branches, wired=True)
    finally:
        stop = True
        await beat
    return result, elapsed, gaps


# ---------------------------------------------------------------- 报告


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--material", default=str(REPO / "docs" / "RAG-DECISIONS.md"))
    parser.add_argument("--branches", type=int, default=8)
    parser.add_argument("--repeat", type=int, default=3,
                        help="A/B 重复几次（报中位数）。单次跑出来的差值常被噪声盖住，"
                             "`check()` 只有 −4…+4 ms，而一次生成是几十到几百 ms")
    parser.add_argument("--nli-latency", type=float, default=None,
                        help=f"单条 NLI 等价延迟（秒）；缺省按 {NLI_FAST} 与 {NLI_SLOW} 各跑一遍")
    args = parser.parse_args()

    text = base.load_material(Path(args.material))
    titles = base.heading_titles(text)[:args.branches]
    if len(titles) < args.branches:
        titles = tuple(list(titles) + [
            f"补充方面{n}" for n in range(1, args.branches - len(titles) + 1)])
    source_id = "m1"

    # ---- A/B：接线前 vs 接线后 -------------------------------------------
    before_runs: list[float] = []
    after_runs: list[float] = []
    for _ in range(max(1, args.repeat)):
        _, elapsed = asyncio.run(generate_once(
            make_ctx(text, titles, source_id), text, titles, source_id,
            branches=args.branches, wired=False))
        before_runs.append(elapsed)
    for _ in range(max(1, args.repeat)):
        _, elapsed = asyncio.run(generate_once(
            make_ctx(text, titles, source_id), text, titles, source_id,
            branches=args.branches, wired=True))
        after_runs.append(elapsed)
    before_elapsed, after_elapsed = median(before_runs), median(after_runs)
    ctx = make_ctx(text, titles, source_id)
    after_result, _ = asyncio.run(generate_once(
        ctx, text, titles, source_id, branches=args.branches, wired=True))
    content = ctx.candidate
    sources = [source for source in ctx.request.sources]
    before_result, _ = asyncio.run(generate_once(
        make_ctx(text, titles, source_id), text, titles, source_id,
        branches=args.branches, wired=False))

    refs = all_refs(content.root)

    print(f"材料 {len(text):,} 字；骨架 {len(titles)} 支；"
          f"树 {_count_nodes(content.root)} 节点 / {len(refs)} 条引用（逐字校验后）")
    print()
    print(f"A/B（同材料、同脚本模型，两次的树逐节点相同；各跑 {len(before_runs)} 次取中位）")
    print(f"  接线前：{before_elapsed * 1000:8.1f} ms  "
          f"[{' '.join(f'{item * 1000:.0f}' for item in before_runs)}]"
          f"   诊断 {len(before_result.data['diagnostics'])} 条   ok={before_result.ok}")
    print(f"  接线后：{after_elapsed * 1000:8.1f} ms  "
          f"[{' '.join(f'{item * 1000:.0f}' for item in after_runs)}]"
          f"   诊断 {len(after_result.data['diagnostics'])} 条   ok={after_result.ok}")
    print(f"  差    ：{(after_elapsed - before_elapsed) * 1000:8.1f} ms"
          f"（NLI 关着，这里只有 `check()`；它与下面的单次耗时对照看）")
    print()

    # ---- 1. check() --------------------------------------------------------
    for label, refs_count in (("有 sources（含第三层）", len(sources)), ("无 sources（只结构）", 0)):
        stats = bench_check(content, sources if refs_count else None)
        print(f"`check()` {label}：{stats['rounds']} 次共 {stats['total_ms']:.1f} ms，"
              f"单次平均 {stats['mean_ms'] * 1000:.1f} µs")
    health = check(content, sources)
    codes: dict[str, int] = {}
    for item in health:
        codes[item.code] = codes.get(item.code, 0) + 1
    print(f"  这次体检的产出：{codes}")
    print()

    # ---- 2. NLI 条数与耗时 --------------------------------------------------
    print(f"`annotate_nli()`：树上有 {len(refs)} 条引用，`MAX_NLI_CHECKS` = {MAX_NLI_CHECKS}")
    quotes = quote_stats(refs)
    print(f"  引用的形态：有 quote {quotes['with_quote']}/{quotes['total']}，"
          f"有字符区间 {quotes['with_range']}/{quotes['total']}，"
          f"两者都无 {quotes['skipped_would_be']}/{quotes['total']}")
    claims = claim_stats(content)
    print(f"  判定的两边：claim = 论断（中位 {claims['median_claim']} 字，其中 label "
          f"中位 {claims['median_label']} 字、最长 {claims['max_label']} 字；上限 "
          f"{nli_module.MAX_CLAIM_CHARS} 字），premise = 引文（中位 "
          f"{claims['median_evidence']} 字、最短 {claims['min_evidence']} 字）")
    for latency in ([args.nli_latency] if args.nli_latency is not None
                    else [NLI_FAST, NLI_SLOW]):
        stats = bench_nli(content, sources, latency=latency)
        print(f"  等价延迟 {stats['latency_ms']:.0f} ms/条 → 判定 {stats['judged']} 条，"
              f"产出 {stats['diagnostics']} 条诊断（其中 contradiction "
              f"{stats['contradicted']} 条），总耗时 {stats['elapsed_ms']:.0f} ms")
    print("  注：本机没有 onnx 权重，上表的延迟是注入的等价延迟，"
          "不是真实推理耗时；真模型判决分布同样无法在本机实测。")
    print()

    # ---- 3. 事件循环停顿 ----------------------------------------------------
    loop = asyncio.run(probe_event_loop(content, sources, latency=NLI_FAST))
    print("事件循环（同一段 NLI，心跳 5 ms 一次）：")
    for label in ("同步直接调", "to_thread"):
        print(f"  {label}：本次 {loop[f'{label}·本次耗时']:.0f} ms，"
              f"心跳 {loop[f'{label}·心跳']:.0f} 次，"
              f"最大停顿 {loop[label]:.0f} ms")
    print()
    for label, on_generate in (("NLI 关", False), ("NLI 开", True)):
        result, elapsed, gaps = asyncio.run(
            generate_with_ticker(make_ctx(text, titles, source_id), text, titles,
                                 source_id, branches=args.branches,
                                 latency=NLI_FAST, on_generate=on_generate))
        peak = max(gaps) * 1000 if gaps else 0.0
        # 心跳是 5 ms 一次；峰值远大于 5 ms 说明有一段时间事件循环没被让出来。
        print(f"整次生成（{label}）：{len(gaps)} 次心跳，最大停顿 {peak:.0f} ms，"
              f"本次生成 {elapsed * 1000:.0f} ms，"
              f"nli={result.data['nli']}")
    print()

    print()
    print("开关语义（`nli.on_generate()`）：")
    for label, enabled, on_generate in (
            ("都没设", None, None),
            ("只开 ENABLED（缺省值生效）", "1", None),
            ("ENABLED=1 且 ON_GENERATE=0", "1", "0")):
        for key, value in (("MINDMAP_NLI_ENABLED", enabled),
                           ("MINDMAP_NLI_ON_GENERATE", on_generate)):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        print(f"  {label:28s} → on_generate() = {nli_module.on_generate()}，"
              f"DEFAULT_ON_GENERATE = {nli_module.DEFAULT_ON_GENERATE}")
    os.environ.pop("MINDMAP_NLI_ENABLED", None)
    os.environ.pop("MINDMAP_NLI_ON_GENERATE", None)
    print()

    # ---- 4. 真实输出 --------------------------------------------------------
    print("工具回给模型的 content（接线后，真实输出）：")
    print(f"  {after_result.content}")
    print()
    print("data 里新增的两个键：")
    print(f"  structure={after_result.data['structure']}")
    print(f"  nli={after_result.data['nli']}")
    print()
    print("诊断码分布：")
    for code, total in sorted(codes_count(after_result.data["diagnostics"]).items(),
                             key=lambda item: -item[1]):
        print(f"  {total:3d}  {code}")
    return 0


def _count_nodes(root) -> int:
    stack, total = [root], 0
    while stack:
        node = stack.pop()
        total += 1
        stack.extend(node.children)
    return total


def codes_count(diagnostics: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in diagnostics:
        counts[item["code"]] = counts.get(item["code"], 0) + 1
    return counts


if __name__ == "__main__":
    raise SystemExit(main())
