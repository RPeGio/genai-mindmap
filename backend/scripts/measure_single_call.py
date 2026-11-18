"""量一次生成 vs 分片：同一主题、同一模型，两边的节点数 / detail 长度 / 跨支重复 / 耗时。

    python scripts/measure_single_call.py                  # 一次生成（无材料、外部关）
    python scripts/measure_single_call.py --deep           # 分片（同前提）
    python scripts/measure_single_call.py --material FILE   # 一次生成 + 一份材料（验引用落地）

**为什么要有这个脚本**：`tests/` 里钉的是契约与接线，量不出"detail 会不会变短"
——那件事只有真模型能回答，而它正是"一次写完"这条路最大的风险。脚本把六个指标
一起打出来，任何一次模型或提示词改动之后都能重跑同一组数字。

凭据：进程环境里没有时，从**用户级**环境变量（`HKCU\\Environment`）补上。DSH /
CI 的 shell 是进程级作用域，未必继承了用户级那几项，`subprocess` 调 pwsh 在本机
也不一定找得到 PATH 里的可执行文件——直接读注册表最稳。

`--material` 会临时把 `MINDMAP_DB_PATH` 指到 `data/measure.db`（外部来源要入库才
能拿到持久 id），跑完可以直接删。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

# 脚本在 `scripts/` 下，`python scripts/xxx.py` 会把**脚本目录**放进 sys.path
# 而不是 cwd——不补这一行，`import app` 在仓库根之外直接失败。
BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

CREDENTIALS = (
    "MINDMAP_LLM_MODE", "MINDMAP_LLM_BASE_URL", "MINDMAP_LLM_API_KEY",
    "MINDMAP_LLM_MODEL", "MINDMAP_EMBEDDING_BASE_URL", "MINDMAP_EMBEDDING_API_KEY",
    "MINDMAP_EMBEDDING_PROTOCOL", "MINDMAP_EMBEDDING_MODEL", "MINDMAP_EMBEDDING_DIM",
)

FILLER = ("简要说明", "详见", "可参考", "不再赘述", "不赘述", "见教材", "参见教材",
          "此处略", "从略", "待补充", "略述", "如上", "同上")

DEFAULT_TOPIC = "机器学习的主要方向"

# 一份结构清楚、长度过 `SHORT_MATERIAL_CHARS`（300 字）的材料。章节与正文一一对应，
# 按支检索才可能真的召回——用一份没有标题行的材料量"引用落地"是量不出来的。
DEFAULT_MATERIAL = (
    "# 预算管理办法\n\n"
    "## 预算审批\n\n"
    "预算由财务经理核准后执行，超过五十万元报总经理审批。"
    "每季度末做一次预算执行分析，偏差超过百分之十的需要书面说明原因。"
    "年度预算在十一月启动编制，各部门按上一年的执行率申报，财务汇总后报董事会。\n\n"
    "## 报销时限\n\n"
    "费用报销的时限是三十个自然日，逾期不再受理；跨年度的单据必须附说明。"
    "报销单需要部门负责人签字，财务只做形式审核，实质合规由内审抽查。"
    "差旅费按标准报销，超标部分需要总经理特批，特批记录留存三年备查。\n\n"
    "## 采购合规\n\n"
    "采购合同必须经过法务审核，金额超过五十万元另行招标。"
    "供应商准入每两年复核一次，出现过重大违约的记录在案，三年内不得再次准入。\n\n"
    "## 资产处置\n\n"
    "固定资产报废需要技术部门出具评估意见，残值收入上缴财务统一管理。"
    "处置金额超过一百万元的要报董事会备案，处置过程留痕，档案保存五年。\n\n"
    "## 附则\n\n"
    "本办法由财务部负责解释，自发布之日起执行；此前发布的相关规定同时废止。"
    "各部门可以在本办法框架内制定实施细则，报财务部备案后生效。\n"
)


def _user_env(name: str) -> str:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
    except OSError:
        return ""
    return str(value)


def _fill_credentials() -> None:
    for name in CREDENTIALS:
        if not os.environ.get(name):
            value = _user_env(name)
            if value:
                os.environ[name] = value


def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)


def _report(content, result, elapsed: float) -> dict:
    from app.agent.tools import _cross_branch_duplicates

    nodes = list(_walk(content.root))
    details = [len((node.detail or "").strip()) for node in nodes if node is not content.root]
    duplicates = _cross_branch_duplicates(content.root)
    return {
        "ok": result.ok,
        "seconds": round(elapsed, 2),
        "nodes": len(nodes),
        "branches": [child.label for child in content.root.children],
        "depths": {
            depth: sum(1 for node in nodes if _depth(content.root, node) == depth)
            for depth in sorted({_depth(content.root, node) for node in nodes})
        },
        "detail_avg": round(sum(details) / max(1, len(details)), 1),
        "detail_min": min(details, default=0),
        "detail_max": max(details, default=0),
        "filler_hits": [
            node.label for node in nodes
            if any(word in (node.detail or "") for word in FILLER)
        ],
        "cross_branch_duplicates": sum(len(places) for _, places in duplicates),
        "duplicate_labels": [label for label, _ in duplicates],
        "nodes_with_refs": sum(1 for node in nodes if node.source_refs),
        "refs_total": sum(len(node.source_refs) for node in nodes),
        "data": {key: value for key, value in result.data.items()
                 if key in ("partial", "retrieval", "source_refs")},
        "diagnostics": sorted({item["code"] for item in result.data.get("diagnostics", [])}),
    }


def _depth(root, target) -> int:
    stack = [(root, 1)]
    while stack:
        node, depth = stack.pop()
        if node is target:
            return depth
        stack.extend((child, depth + 1) for child in node.children)
    return 0


async def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deep", action="store_true", help="跑分片那条路（对比基线）")
    parser.add_argument("--material", metavar="FILE", nargs="?", const="",
                        help="带材料跑一次生成（不给路径时用内置的预算管理办法）")
    parser.add_argument("--topic", default="", help="中心主题")
    parser.add_argument("--branches", type=int, default=5)
    parser.add_argument("--external", action="store_true", help="无材料时开外部检索")
    parser.add_argument("--save", default="", help="把产出树写到这个路径")
    args = parser.parse_args(argv)

    _fill_credentials()
    os.environ["MINDMAP_EXTERNAL_KNOWLEDGE"] = "1" if args.external else "0"
    os.environ.setdefault("MINDMAP_EXTERNAL_PROXY", "http://127.0.0.1:7890")

    from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
    from app.agent.tools import build_registry
    from app.contracts.source import Source, SourceKind

    topic = args.topic or ("预算管理办法" if args.material is not None else DEFAULT_TOPIC)
    sources: list[Source] = []
    if args.material is not None:
        text = (open(args.material, encoding="utf-8").read() if args.material
                else DEFAULT_MATERIAL)
        sources = [Source(id="s-measure", kind=SourceKind.document, status="parsed",
                          filename="measure.md", text=text)]
        os.environ.setdefault("MINDMAP_DB_PATH", "data/measure.db")

    request = AgentRunRequest(
        goal=topic, intent="create", thinking_model="default", sources=sources,
        budget=RunBudget(max_model_calls=40, max_tool_calls=8, max_seconds=600.0,
                         max_output_tokens=65536, max_repairs=1),
    )
    ctx = RunContext(request=request,
                     sources_by_id={source.id: source for source in sources})
    if args.deep:
        registry = build_registry("create_deep", has_sources=bool(sources),
                                  has_library=False)
        call = ToolCall(call_id="measure", name="mindmap_generate_deep",
                        arguments={"topic": topic, "branches": args.branches,
                                   "points_per_branch": 3, "max_depth": 3})
    else:
        registry = build_registry("create", has_sources=bool(sources), has_library=False)
        call = ToolCall(call_id="measure", name="mindmap_generate",
                        arguments={"topic": topic, "branches": args.branches})

    started = time.perf_counter()
    result = await registry.execute(call, ctx)
    elapsed = time.perf_counter() - started

    if not result.ok:
        print(json.dumps({"ok": False, "content": result.content,
                          "seconds": round(elapsed, 2)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(_report(ctx.candidate, result, elapsed), ensure_ascii=False, indent=2))
    if args.save:
        with open(args.save, "w", encoding="utf-8") as handle:
            json.dump(ctx.candidate.model_dump(), handle, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main(sys.argv[1:])))
