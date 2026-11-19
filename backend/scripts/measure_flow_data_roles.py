"""实测：流程图生成时，模型会不会**自己**标出 `data` / `database`。

## 为什么要这个脚本

`role` 从四个值扩到六个（加了平行四边形 = 输入输出、圆柱 = 存储）。契约与渲染是
确定的，不确定的是**提示词够不够**：`decision` 那次的经验是"同一句话写在 system
里，两次采样一个都没标出来；写到 user 末尾才有"，所以新形状的判据也写在 user
末尾（见 `tools._generate` 里那段注释）。

但这个结论不能靠推理沿用。这个脚本量的就是**那段话到底有没有用**：

  - 一次流程里出现了几个 `data`、几个 `database`；
  - 有没有出现"整张图一个都没有"（那就说明判据还不够明确）；
  - 有没有把普通步骤也标上（标满 = 形状退化成装饰）；
  - 每个角色落在哪些标题上（人要能照着这个判断标得对不对）。

## 用法（在 `backend/` 下）

    python scripts/measure_flow_data_roles.py                # 默认跑 2 次
    python scripts/measure_flow_data_roles.py --runs 3
    python scripts/measure_flow_data_roles.py --topic "报销审批流程，用流程图"
    python scripts/measure_flow_data_roles.py --system-only   # 对照组，见下

## 对照组：判据是不是"只写在 system 就够了"

`--system-only` 走**同一条提示词的前半段**（`DETAIL_PROMPT + FLOW_TREE_RULES`
含 role 六值说明 + `DETAIL_NO_SOURCE_RULES`），user 只有主题与已定好的一级分支，
**末尾不加**那段判据——也就是把 `tools._generate` 里那段 user 追加去掉。

它回答的是"阶段 2 那条经验对**新形状**还成不成立"：`decision` 当年写在 system 里
两次采样一个都没标出来，必须写到 user 末尾。新形状的结论见下面 §实测。

**没有模型密钥时它会明确说"无法实测"并给出现在能确认的部分**（契约与形状映射是
用例钉住的，不依赖模型）。这台机器（CI / 无密钥的环境）就是这种情况：
`MINDMAP_LLM_API_KEY` 为空时跑不出任何模型行为，报告里必须如实说这一条。
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

DEFAULT_TOPIC = "用户注册流程，用流程图"
DEFAULT_RUNS = 2
# 与用户在界面上选"流程图"时那条路一致：图型写在请求层（模型看不见下拉框）。
DIAGRAM_TYPE = "flowchart"
DATA_ROLES = ("data", "database")
# 对照组的骨架：与实测同一个主题，四个阶段是这条流程的常见切法。
CONTROL_SKELETON = ("填写注册信息", "校验信息", "创建账号", "完成注册")


@dataclass
class RunReport:
    """一次生成的可核对结果。字段都是"能照着复现"的原话，不是评分。"""

    run: int
    ok: bool
    content: str
    nodes: int = 0
    stages: int = 0
    tagged: dict[str, list[str]] = field(default_factory=dict)
    plain: list[str] = field(default_factory=list)
    codes: list[str] = field(default_factory=list)

    @property
    def data_count(self) -> int:
        return len(self.tagged.get("data", ()))

    @property
    def database_count(self) -> int:
        return len(self.tagged.get("database", ()))

    def summary(self) -> str:
        if not self.ok:
            return f"第 {self.run} 次：生成失败 —— {self.content[:200]}"
        lines = [
            f"第 {self.run} 次：{self.nodes} 个节点 / {self.stages} 个阶段 / "
            f"data {self.data_count} 个 / database {self.database_count} 个",
        ]
        for role in (*DATA_ROLES, "decision", "start", "end"):
            labels = self.tagged.get(role) or []
            if labels:
                lines.append(f"  {role}：{'、'.join(labels)}")
        if not self.data_count and not self.database_count:
            lines.append("  **一个 data / database 都没标出来**")
        lines.append(f"  未标角色的节点：{len(self.plain)} 个")
        lines.append(f"  诊断：{'、'.join(self.codes) or '（无）'}")
        return "\n".join(lines)


def _has_key() -> bool:
    return bool(os.getenv("MINDMAP_LLM_API_KEY", "").strip())


async def _one_run_system_only(run: int, topic: str) -> RunReport:
    """对照组：判据只在 system，user 末尾那段判据不加。

    输入是真实链路的子集（system 与 `_generate` 逐字同源，user 只少那一段），
    所以两次采样之间的差别只有一个变量：**那句话在不在 user 末尾**。
    """
    from app.agent import tools

    system = (tools.DETAIL_PROMPT + "\n" + tools.FLOW_TREE_RULES
              + "\n" + tools.DETAIL_NO_SOURCE_RULES)
    user = (
        f"主题：{topic}"
        "\n\n这次的一级分支（已定好，按此顺序，不要增删改名）：\n"
        + "\n".join(f"- {title}" for title in CONTROL_SKELETON)
    )
    raw = await tools.chat_async(system, user, max_tokens=4096)
    report = RunReport(run=run, ok=bool(raw), content="模型没有返回可解析的 JSON")
    if not report.ok:
        return report

    roles: dict[str, list[str]] = {}
    total = 0

    def walk(node: object) -> None:
        nonlocal total
        if not isinstance(node, dict):
            return
        total += 1
        role = node.get("role")
        label = node.get("label")
        if isinstance(role, str) and isinstance(label, str):
            if role in ("data", "database"):
                roles.setdefault(role, []).append(label)
            else:
                roles.setdefault(role, []).append(label)
        for child in node.get("children") or ():
            walk(child)

    walk((raw or {}).get("root"))
    report.nodes = total
    # `start` / `end` / `decision` 也一起收：对照组要判断的是"判据的有效性"，
    # 顺带能看出 role 这一段整体有没有生效。
    report.tagged = {key: list(value) for key, value in roles.items()}
    plain = total - sum(len(value) for value in roles.values())
    report.plain = ["（未标角色）"] * max(plain, 0)
    return report


async def _one_run(run: int, topic: str) -> RunReport:
    """跑一次 `_generate`，把"模型标了哪些数据形状"这件事实数出来。"""
    from app.agent.contracts import AgentRunRequest, RunBudget, RunContext
    from app.agent.tools import GenerateArgs, _generate

    request = AgentRunRequest(
        goal=topic, intent="create", diagram_type=DIAGRAM_TYPE,
        budget=RunBudget(max_model_calls=8, max_output_tokens=65536),
    )
    ctx = RunContext(request=request)
    result = await _generate(GenerateArgs(topic=topic, branches=5), ctx)
    report = RunReport(run=run, ok=result.ok, content=result.content)
    report.codes = sorted({item["code"] for item in result.data.get("diagnostics", [])})
    if not result.ok:
        return report

    from app.agent import shapes
    from app.contracts.mindmap import FlowContent

    content = ctx.candidate
    assert isinstance(content, FlowContent)  # 图型写在请求层，产物必须是流程
    report.stages = len(content.stages)
    report.nodes = shapes.count_nodes(content)
    for stage in content.stages:
        for node in shapes.walk_nodes(stage.root):
            if node.role is None or node.role == "step":
                report.plain.append(node.label)
            else:
                report.tagged.setdefault(node.role, []).append(node.label)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topic", default=DEFAULT_TOPIC)
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    parser.add_argument(
        "--system-only", action="store_true",
        help="对照组：判据只写在 system，user 末尾不加（见模块 docstring）",
    )
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    args = parser.parse_args()

    if not _has_key():
        print(
            "无法实测：这台机器没有 MINDMAP_LLM_API_KEY，一次真实模型调用都发不出去。\n"
            "（`_generate` 的第一句就会抛 `GenerationError`，拿不到任何采样。）\n\n"
            "现在**能**确认的部分不依赖模型，由用例钉着：\n"
            "  - `Node.role` 收 data / database（`tests/test_diagram_contracts.py`）；\n"
            "  - `nodeShapeOf('data') === 'parallelogram'`、\n"
            "    `nodeShapeOf('database') === 'cylinder'`（前端 `tests/node-shape.spec.ts`）；\n"
            "  - 外接框 `DATA_SIZE` / `DATABASE_SIZE` 只进 `layoutFlow`，\n"
            "    `NODE_SIZE` 一个字没动。\n\n"
            "配上密钥后重跑本脚本，就能拿到模型侧的采样：\n"
            "  MINDMAP_LLM_API_KEY=… python scripts/measure_flow_data_roles.py"
        )
        return 2

    runner = _one_run_system_only if args.system_only else _one_run
    reports = [asyncio.run(runner(run, args.topic)) for run in range(1, args.runs + 1)]
    if args.json:
        print(json.dumps([report.__dict__ for report in reports], ensure_ascii=False, indent=2))
        return 0

    mode = "对照组（判据只在 system）" if args.system_only else "正式（判据在 system + user 末尾）"
    print(f"主题：{args.topic}｜图型：{DIAGRAM_TYPE}｜{mode}｜{len(reports)} 次采样\n")
    for report in reports:
        print(report.summary())
        print()
    total_data = sum(report.data_count for report in reports)
    total_db = sum(report.database_count for report in reports)
    runs_with_any = sum(
        1 for report in reports if report.data_count or report.database_count)
    print(
        f"合计：data {total_data} 个 / database {total_db} 个；"
        f"{len(reports)} 次里有 {runs_with_any} 次至少标了一个"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
