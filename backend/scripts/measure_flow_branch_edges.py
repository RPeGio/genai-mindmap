"""实测：流程图的判定节点到底写出了几条出边。

## 为什么要这个脚本

阶段 2 的实测结论是：模型标了 5 个 `role="decision"` 的节点，而产出里 `branch`
边**恒为 0**。原因不是模型没标，是契约没有位置——`FlowContent.edges` 的端点是
**阶段 id**，而判定节点在阶段**内部**，它的「是 / 否」两条出边在两个数组里都
无处可写。

阶段 3 补了两样东西：契约上的 `FlowStage.edges`（阶段内的边，端点用该阶段的
节点 id），以及生成侧从模型响应里解析 `branch` 的那一步。这个脚本量的就是
**那一步到底有没有东西可解析**：

  - 一次生成里有几个 `role="decision"` 的节点；
  - 每个 decision 有几条出边（目标应该是 2 条，带「是」/「否」）；
  - 出边的 `target` 是不是**真实节点 id**（而不是模型抄过来的标题）；
  - 有几条没解析出来（`FLOW_BRANCH_UNRESOLVED`），有几条被降级成阶段级
    （`FLOW_EDGE_DEMOTED`）。

它是**生成侧**的实测，不走 agent 循环：`_generate` 就是 `mindmap_generate`
背后的那条函数，骨架 / 结构 / 说明三次调用都在里面。

## 用法（在 `backend/` 下）

    python scripts/measure_flow_branch_edges.py            # 默认跑 2 次
    python scripts/measure_flow_branch_edges.py --runs 3
    python scripts/measure_flow_branch_edges.py --topic "报销审批流程"

**没有模型密钥时它会明确说"无法实测"并给出现在能确认的部分**（契约与解析链
是用例钉住的，不依赖模型）。这台机器（CI / 无密钥的环境）就是这种情况：
`MINDMAP_LLM_API_KEY` 为空时跑不出任何模型行为，报告里必须如实说这一条，不能
拿一份假的采样顶上去。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

DEFAULT_TOPIC = "用户注册流程"
DEFAULT_RUNS = 2
# 与用户在界面上选"流程图"时那条路一致：图型写在请求层（模型看不见下拉框）。
DIAGRAM_TYPE = "flowchart"


@dataclass
class RunReport:
    """一次生成的可核对结果。字段都是"能照着复现"的原话，不是评分。"""

    run: int
    ok: bool
    content: str
    nodes: int = 0
    stages: int = 0
    decisions: list[str] = field(default_factory=list)
    out_edges: dict[str, list[tuple[str, str, str]]] = field(default_factory=dict)
    stage_edges: int = 0
    stage_between_edges: int = 0
    dangling_targets: list[str] = field(default_factory=list)
    codes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if not self.ok:
            return f"第 {self.run} 次：生成失败 —— {self.content[:200]}"
        counts = ", ".join(
            f"「{label}」→ {target}" for label in self.decisions
            for target, label, _ in self.out_edges.get(label, [])
        ) or "（无）"
        return (
            f"第 {self.run} 次：{self.nodes} 个节点 / {self.stages} 个阶段 / "
            f"{len(self.decisions)} 个 decision\n"
            f"  每个 decision 的出边数："
            + ("、".join(f"{label}={len(self.out_edges.get(label, []))}"
                        for label in self.decisions) or "（没有 decision）")
            + f"\n  阶段内边 {self.stage_edges} 条，阶段间边 {self.stage_between_edges} 条\n"
            + (f"  没有解析成功的出口：{len(self.dangling_targets)} 条\n"
               if self.dangling_targets else "")
            + f"  出口明细：{counts}\n"
            f"  诊断：{'、'.join(self.codes) or '（无）'}"
        )


def _has_key() -> bool:
    return bool(os.getenv("MINDMAP_LLM_API_KEY", "").strip())


async def _one_run(run: int, topic: str) -> RunReport:
    """跑一次 `_generate`，把"判定节点的出边"这件事实数出来。"""
    from app.agent.contracts import AgentRunRequest, RunBudget, RunContext
    from app.agent.tools import GenerateArgs, _generate

    request = AgentRunRequest(
        goal=topic, intent="create", diagram_type=DIAGRAM_TYPE,
        budget=RunBudget(max_model_calls=8, max_output_tokens=65536),
    )
    ctx = RunContext(request=request)
    result = await _generate(GenerateArgs(topic=topic, branches=6), ctx)
    report = RunReport(run=run, ok=result.ok, content=result.content)
    report.codes = sorted({item["code"] for item in result.data.get("diagnostics", [])})
    if not result.ok:
        return report

    from app.agent import shapes
    from app.contracts.mindmap import FlowContent
    from app.agent.shapes import walk_nodes

    content = ctx.candidate
    assert isinstance(content, FlowContent)  # 图型写在请求层，产物必须是流程
    report.stages = len(content.stages)
    report.nodes = shapes.count_nodes(content)
    report.stage_between_edges = len(content.edges)
    known = {node.id for node in shapes.iter_nodes(content)}
    by_id = {node.id: node for node in shapes.iter_nodes(content)}

    for stage in content.stages:
        report.stage_edges += len(stage.edges)
        for edge in stage.edges:
            source = by_id.get(edge.source)
            label = source.label if source is not None else f"<{edge.source}>"
            report.out_edges.setdefault(label, []).append(
                (edge.target, edge.label or "", edge.kind or "flow"))
            if edge.target not in known:
                report.dangling_targets.append(f"{label}→{edge.target}")

    for stage in content.stages:
        for node in walk_nodes(stage.root):
            if node.role == "decision":
                report.decisions.append(node.label)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topic", default=DEFAULT_TOPIC)
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    args = parser.parse_args()

    if not _has_key():
        print(
            "无法实测：这台机器没有 MINDMAP_LLM_API_KEY，一次真实模型调用都发不出去。\n"
            "（`_generate` 的第一句就会抛 `GenerationError`，拿不到任何采样。）\n\n"
            "现在**能**确认的部分不依赖模型，由用例钉着：\n"
            "  - `FlowStage.edges` 接受阶段内的边（端点必须是该阶段的节点 id）；\n"
            "  - `_flow_branch_edges` 能把 `branch` 的「是 / 否」解析成真实节点 id，\n"
            "    写歪的目标丢掉并记 `FLOW_BRANCH_UNRESOLVED`；\n"
            "  - `shapes.to_flow` 按端点归属分流到 `FlowStage.edges` / `FlowContent.edges`。\n"
            "  见 `tests/test_diagram_contracts.py::TestFlowStageEdges`、\n"
            "  `tests/test_agent_shapes.py::TestFlowBranchEdges`。\n\n"
            "配上密钥后重跑本脚本，就能拿到模型侧的采样：\n"
            "  MINDMAP_LLM_API_KEY=… python scripts/measure_flow_branch_edges.py"
        )
        return 2

    reports = [asyncio.run(_one_run(run, args.topic)) for run in range(1, args.runs + 1)]
    if args.json:
        print(json.dumps([report.__dict__ for report in reports], ensure_ascii=False, indent=2))
        return 0

    print(f"主题：{args.topic}｜图型：{DIAGRAM_TYPE}｜{len(reports)} 次采样\n")
    for report in reports:
        print(report.summary())
        print()
    total_decisions = sum(len(report.decisions) for report in reports)
    total_edges = sum(len(items) for report in reports
                      for items in report.out_edges.values())
    print(
        f"合计：{total_decisions} 个 decision / {total_edges} 条阶段内出边"
        + ("（每个 decision 平均 "
           f"{total_edges / total_decisions:.2f} 条）" if total_decisions else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
