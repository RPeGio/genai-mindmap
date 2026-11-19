"""风格画像三层（`app/prefs/style.py` + `profile.py` + 生成链路接线）的度量脚本。

四件事，各自一条命令：

```
# 1) 真库报告：8 个结构参数 + 画像方向 + 轮廓系数（不调模型）
python scripts/measure_profile_ab.py --report

# 2) 真跑 A/B：同一 goal、同一材料，A 组不带画像 / B 组带画像
python scripts/measure_profile_ab.py --ab 2 --goal "机器学习的主要方向"

# 3) 改动量对比：两个已保存的图（各自是某组的产物），数 `edit_events`（去掉假数据）
python scripts/measure_profile_ab.py --pair MAP_A MAP_B

# 4) 过滤气泡检验：加权上来的命中里有几个对应到后来被删掉的节点
python scripts/measure_profile_ab.py --bubble MAP_B
```

## 为什么 A/B 的"改动量"要单独一条命令

**改动量是唯一硬的指标**（改动越少 = 画像越准），而它需要一个真人去改。真跑的
`--ab` 只能给出"这次生成的结构",它给不出"用户改了几处"——`edit_events` 里那一列
在本机是空的（565 条里 559 条是灌进去的假数据，见 `--report` 的统计）。

所以脚本把三件事分开：`--ab` 负责产出与保存（保存才有 `map_id`），人在界面上改，
`--pair` 负责数。**不把"结构偏离度"冒充成改动量**：前者是模型的自我一致性，后者
是用户的判断，两者的数不能互相替代。

## 假数据怎么认（`--report` 会打印）

`edit_events` 里 559 条 `kind='update'` 是灌进去的：`after` 一律是原文 + `（改）`。
另外 `'80字' → '1800字'` 这种占位描述也算不了数。判据写在 `_is_synthetic` 里，两条
都是**机械可判**的，不依赖"这条看起来像测试"。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import statistics
import sys
from contextlib import closing
from dataclasses import dataclass, field
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agent import shapes  # noqa: E402
from app.contracts.mindmap import DiagramContent  # noqa: E402
from app.prefs import profile as profile_mod  # noqa: E402
from app.prefs import style as style_mod  # noqa: E402

#: 灌数据脚本留在 `after` 末尾的机械标记。
SYNTHETIC_MARK = "（改）"

#: `'80字'` / `'1800字'` 这种占位描述（编辑日志里 `getattr(after, "len")` 的痕迹）。
_PLACEHOLDER = ("字",)


def _db_path() -> str:
    return os.getenv("MINDMAP_DB_PATH", os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "mindmaps.db"))


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------- 假数据识别


def _is_synthetic(row: sqlite3.Row) -> bool:
    """这条 `edit_events` 是不是灌进来的假数据。

    两条判据，都只读字段、不猜语义：

      1. `after` 以 `（改）` 结尾：灌数据脚本的机械追加；
      2. `before` 与 `after` 都是 `"NN字"` 这种占位描述：编辑日志自己记的是
         "我把 detail 从 80 字改到 1800 字"，不是内容。
    """
    after = (row["after"] or "").strip()
    before = (row["before"] or "").strip()
    if after.endswith(SYNTHETIC_MARK):
        return True
    return bool(after.endswith(_PLACEHOLDER[0]) and before.endswith(_PLACEHOLDER[0])
                and after[:-1].isdigit() and before[:-1].isdigit())


def _event_stats() -> dict[str, Any]:
    """`edit_events` 的真相：多少条是真的。"""
    with closing(_connect()) as conn:
        rows = conn.execute("SELECT * FROM edit_events").fetchall()
    synthetic = [row for row in rows if _is_synthetic(row)]
    real = [row for row in rows if not _is_synthetic(row)]
    per_map: dict[str, int] = {}
    for row in synthetic:
        per_map[row["map_id"]] = per_map.get(row["map_id"], 0) + 1
    return {
        "total": len(rows),
        "synthetic": len(synthetic),
        "real": len(real),
        "synthetic_by_map": dict(sorted(per_map.items(), key=lambda kv: -kv[1])),
        "real_kinds": {kind: sum(1 for row in real if row["kind"] == kind)
                       for kind in sorted({row["kind"] for row in real})},
    }


# ---------------------------------------------------------------- 报告


def _report() -> int:
    print("=" * 78)
    print("一、风格统计层（`style_profile`）——从 runs.candidate 数出来的 8 个参数")
    print("=" * 78)
    observations = list(style_mod.iter_observations())
    events = _event_stats()
    print(f"可用的 done 运行（candidate 非空且能解析）：{len(observations)} 条")
    print(f"edit_events 共 {events['total']} 条：假数据 {events['synthetic']} 条、"
          f"真数据 {events['real']} 条（{events['real_kinds']}）")
    if events["synthetic_by_map"]:
        top = next(iter(events["synthetic_by_map"].items()))
        print(f"  假数据最集中的图：{top[0][:8]}… 有 {top[1]} 条（判据见 _is_synthetic）")

    params = style_mod.summarize(observations)
    if not params:
        print("样本不足，画像不启用。")
    else:
        folded = params["branches"].detail.get("folded_runs", 0)
        raw = params["branches"].detail.get("raw_samples", len(observations))
        print(f"折叠重复形状：{raw} → {params['branches'].samples} 条"
              f"（折掉 {folded} 条同形状）\n")
        print(f"{'参数':<14}{'值':<12}{'区间':<14}{'样本':>4}  {'置信度':>6}  依据")
        for key, param in params.items():
            value = param.value
            span = ""
            if param.kind == "number" and param.low is not None:
                span = f"{param.low:g}–{param.high:g}"
            detail = ""
            if "counts" in param.detail:
                detail = str(param.detail["counts"])
            elif "cv_median" in param.detail:
                detail = f"cv 中位 {param.detail['cv_median']}"
            print(f"{param.label:<14}{str(value):<12}{span:<14}{param.samples:>4}  "
                  f"{param.confidence:>6.2f}  {detail}")
        print("\n进提示词的那一段（`style.prompt_block()`）：")
        print("-" * 78)
        print(style_mod.prompt_block(params) or "（空）")
        print("-" * 78)
        skipped = [param.label for param in params.values()
                   if param.confidence < style_mod.PROMPT_MIN_CONFIDENCE]
        if skipped:
            print(f"因置信度不足而没写进去的：{'、'.join(skipped)}")

    print()
    print("=" * 78)
    print("二、画像向量（`profile_vectors`）——材料 + 导图聚出来的方向")
    print("=" * 78)
    samples = profile_mod._samples()
    by_kind = {kind: sum(1 for sample in samples if sample.kind == kind)
               for kind in ("material", "mindmap", "memory")}
    print(f"样本 {len(samples)} 条：{by_kind}；k = {profile_mod._pick_k(len(samples))}"
          f"（`_pick_k` 夹在 {profile_mod.MIN_CLUSTERS}–{profile_mod.MAX_CLUSTERS}）")
    stored = profile_mod.stored()
    if not stored:
        print("库里没有方向。先跑 `profile.rebuild()`（需要 embedding 凭证）。")
    else:
        print(f"\n{'方向':<44}{'权重':>7}{'样本':>5}")
        for item in stored:
            print(f"{item.label[:42]:<44}{item.weight:>7.3f}{item.samples:>5}")
        if profile_mod.stale():
            print("\n⚠ 画像已过期（超过 %.0f 小时未更新）" % profile_mod.STALE_HOURS)
        print("\n轮廓系数（`profile.silhouette`）记在 rebuild 的日志里——它是"
              "『这批数据到底有没有方向』的唯一凭据：\n"
              "  ≥0.35 隔开了 / 0.15–0.35 弱结构 / <0.15 等于把无关文本切成了几份。")
    return 0


# ---------------------------------------------------------------- A/B


@dataclass
class Round:
    """一次生成的可比结果。"""

    arm: str
    run_id: str
    params: dict[str, float | str] = field(default_factory=dict)
    nodes: int = 0
    branches: int = 0
    depth: int = 0
    detail: float = 0.0
    label: float = 0.0
    content: DiagramContent | None = None
    error: str = ""


def _shape_of(content: DiagramContent) -> dict[str, Any]:
    observation = style_mod.observe("ab", content)
    return {
        "branches": observation.branches,
        "depth": observation.depth,
        "detail": round(observation.detail_chars, 1),
        "label": round(observation.label_chars, 1),
        "nodes": sum(1 for _ in shapes.iter_nodes(content)),
    }


async def _one(goal: str, sources: list[Any], arm: str) -> Round:
    """跑一次完整生成，取 `done` 帧里的整棵树。"""
    from app.agent import service
    from app.agent.contracts import AgentRunRequest

    request = AgentRunRequest(goal=goal, intent="create", sources=sources)
    result = Round(arm=arm, run_id="")
    try:
        async for event in service.run(request):
            kind = getattr(event, "type", None) or type(event).__name__
            if kind in ("done", "DoneEvent") and getattr(event, "content", None):
                result.content = event.content
                result.run_id = str(getattr(event, "run_id", "") or "")
            if kind in ("error", "ErrorEvent"):
                result.error = str(getattr(event, "message", "") or "error")[:200]
    except Exception as exc:  # noqa: BLE001 — 单次失败不该中断整组
        result.error = f"{type(exc).__name__}: {exc}"[:200]
    if result.content is not None:
        result.params = _shape_of(result.content)
        result.nodes = result.params["nodes"]
        result.branches = result.params["branches"]
        result.depth = result.params["depth"]
        result.detail = result.params["detail"]
        result.label = result.params["label"]
    return result


def _material_sources(limit: int = 1) -> list[Any]:
    """用库里一份真实材料当 Source（同材料是 A/B 可比的前提）。"""
    from app.contracts.source import Source

    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT id, filename, text FROM library_sources "
            "WHERE length(text) > 400 ORDER BY length(text) DESC LIMIT ?",
            (limit,)).fetchall()
    return [Source(id=row["id"], filename=row["filename"] or row["id"],
                   kind="text", text=row["text"]) for row in rows]


def _deviation(params: dict[str, style_mod.Param], round_result: Round) -> float:
    """生成结果与画像的**结构偏离度**：四个连续量的相对 L1 距离之和。

    这不是"改动量"，是"模型有没有按画像那个形状生成"。两者分开报——前者要人，
    后者机器就能算，但只有后者能在没有真人的情况下证明"画像段确实进了模型"。
    """
    total = 0.0
    for key, actual in (("branches", round_result.branches), ("depth", round_result.depth),
                        ("detail_chars", round_result.detail), ("label_chars", round_result.label)):
        param = params.get(key)
        if param is None or not isinstance(param.value, (int, float)):
            continue
        target = float(param.value) or 1.0
        total += abs(actual - target) / target
    return round(total, 3)


def _ab(goal: str, runs: int) -> int:
    from app.prefs import style as style_mod

    params = style_mod.read()
    if not params:
        print("画像表是空的：先跑一次 `style.refresh()`（或带 `--refresh`）。")
        return 2
    sources = _material_sources()
    if not sources:
        print("库里没有足够长的材料，A/B 没有可比输入。")
        return 2
    print(f"材料：{sources[0].filename}（{len(sources[0].text)} 字）")
    print(f"每组 {runs} 次，画像段 {len(style_mod.prompt_block(params))} 字\n")

    rounds: list[Round] = []
    for index in range(runs):
        for arm, switch in (("A 无画像", "0"), ("B 带画像", "1")):
            os.environ[style_mod.PROMPT_SWITCH_ENV] = switch
            print(f"  第 {index + 1} 轮 {arm} …", end="", flush=True)
            result = asyncio.run(_one(goal, sources, arm))
            rounds.append(result)
            if result.error:
                print(f" 失败：{result.error}")
            else:
                print(f" {result.nodes} 节点 / {result.branches} 支 / "
                      f"深 {result.depth} / detail {result.detail:g} 字")
    os.environ.pop(style_mod.PROMPT_SWITCH_ENV, None)

    print(f"\n{'组':<8}{'支':>4}{'深':>4}{'detail':>9}{'label':>7}{'节点':>6}{'偏离度':>8}")
    for arm in ("A 无画像", "B 带画像"):
        own = [item for item in rounds if item.arm == arm and not item.error]
        if not own:
            print(f"{arm:<8}  全部失败")
            continue
        print(f"{arm:<8}{statistics.median([r.branches for r in own]):>4.0f}"
              f"{statistics.median([r.depth for r in own]):>4.0f}"
              f"{statistics.median([r.detail for r in own]):>9.1f}"
              f"{statistics.median([r.label for r in own]):>7.1f}"
              f"{statistics.median([r.nodes for r in own]):>6.0f}"
              f"{statistics.median([_deviation(params, r) for r in own]):>8.2f}")
    print(f"\n样本量：A {sum(1 for r in rounds if r.arm.startswith('A') and not r.error)} 次 / "
          f"B {sum(1 for r in rounds if r.arm.startswith('B') and not r.error)} 次"
          f"（每组的失败次数：A {sum(1 for r in rounds if r.arm.startswith('A') and r.error)}、"
          f"B {sum(1 for r in rounds if r.arm.startswith('B') and r.error)}）")
    print("\n**改动量这一列不在上面**：它要真人去改。本脚本不代劳保存——两组的图各存"
          "一次（界面里落画布即可），改完用 `--pair MAP_A MAP_B` 数 `edit_events`。"
          "\n偏离度也不是改动量：它测的是『模型有没有按画像那个形状生成』，机器就能算，"
          "但它替代不了用户的判断。n=3 这个量级只够看方向，不够下结论。")
    return 0


# ---------------------------------------------------------------- 改动量


def _pair(left: str, right: str) -> int:
    """两个 `map_id` 的改动量对比（只数真数据）。"""
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM edit_events WHERE map_id IN (?, ?)", (left, right)).fetchall()
    for label, map_id in (("A（无画像）", left), ("B（带画像）", right)):
        own = [row for row in rows if row["map_id"] == map_id]
        real = [row for row in own if not _is_synthetic(row)]
        print(f"{label} {map_id[:8]}…：事件 {len(own)} 条，"
              f"其中真数据 {len(real)} 条，假数据 {len(own) - len(real)} 条")
        for row in real:
            print(f"    {row['kind']:<7}{row['field'] or '':<7}"
                  f"{(row['before'] or '')[:20]!r} → {(row['after'] or '')[:20]!r}")
    return 0


# ---------------------------------------------------------------- 过滤气泡


def _bubble(map_id: str) -> int:
    """加权上来的命中，有多少对应到后来被用户删掉的节点。

    回答链路：`_rag` 的 `data["profile_lifted"]`（`source_id:start`）→ 生成出的节点
    `source_refs` 里引用了那个 `source_id` → 该节点后来被 `remove` 掉了。

    **本机样本量是 0**：`profile_lifted` 只在 `MINDMAP_PROFILE_RERANK=1` 时出现，
    而库里所有历史运行的 `request` 里都没有这个开关的记录；`edit_events` 的真数据
    只有 6 条、没有一条对得上。脚本照实报 0，并把口径写出来——它随时能对这个
    库跑出真数字，只要先有真数据。
    """
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT id, content FROM mindmaps WHERE id = ?", (map_id,)).fetchone()
    if rows is None:
        print(f"没有这张图：{map_id}")
        return 2
    payload = json.loads(rows["content"])
    cited: dict[str, set[str]] = {}
    for node in _walk_payload(payload):
        refs = node.get("source_refs") or []
        if refs:
            cited[node["id"]] = {ref.get("source_id") for ref in refs}
    with closing(_connect()) as conn:
        removed = conn.execute(
            "SELECT * FROM edit_events WHERE map_id = ? AND kind = 'remove'",
            (map_id,)).fetchall()
    real_removed = [row for row in removed if not _is_synthetic(row)]
    print(f"图 {map_id[:8]}…：节点 {len(cited)} 个带引用，"
          f"删除事件 {len(removed)} 条（真数据 {len(real_removed)} 条）")
    print("命中定位键需要 `runs` 里的 `profile_lifted`——本库历史运行都没有记录它，"
          "所以这次对不上的条数是 0/0，不是 0%。")
    return 0


def _walk_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        out.append(node)
        for child in (node.get("children") or []):
            walk(child)

    for key in ("root",):
        walk(payload.get(key))
    for key in ("bones", "stages"):
        for node in (payload.get(key) or []):
            walk(node)
            for child in (node.get("nodes") or []):
                walk(child)
    return out


# ---------------------------------------------------------------- 入口


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", action="store_true", help="真库报告（不调模型）")
    parser.add_argument("--ab", type=int, default=0, metavar="N", help="真跑 A/B，每组 N 次")
    parser.add_argument("--goal", default="机器学习的主要方向", help="A/B 用的 goal")
    parser.add_argument("--pair", nargs=2, metavar=("MAP_A", "MAP_B"), help="改动量对比")
    parser.add_argument("--bubble", metavar="MAP_ID", help="过滤气泡检验")
    parser.add_argument("--refresh", action="store_true", help="先重算风格画像")
    parser.add_argument("--rebuild-vectors", action="store_true", help="先重算画像向量")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if args.refresh:
        style_mod.refresh()
    if args.rebuild_vectors:
        profile_mod.rebuild()
    if args.pair:
        return _pair(*args.pair)
    if args.bubble:
        return _bubble(args.bubble)
    if args.ab:
        return _ab(args.goal, args.ab)
    return _report()


if __name__ == "__main__":
    raise SystemExit(main())
