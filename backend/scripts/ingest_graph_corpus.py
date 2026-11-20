"""把语料重新灌进知识图谱，并量出这一趟花了多少钱。

两批材料：

  - `docs/` 的那四份（`AGENT_PLAN` / `PLAN` / `ARCHITECTURE` / `RAG-DECISIONS`）：
    这是 `docs/RAG-DECISIONS.md` 第 8 节与 `FEATURES_AND_OWNERS.md` F14 记录的那批，
    1130 块 / 1406 实体 / 993 边的来源；
  - `library_sources` 里现有的全部（外部检索抓回来的正文，1000–8000 字不等）。

**为什么要重灌而不是用现成那张图**：旧图的边指向 `D:/neo4j/eval-library.db` 里的
`source_id`（当年评测用的是另一套库），在当前 `mindmaps.db` 下解析不到任何来源
——图能走通，但每条边都指不回原文。要测"图谱生成的节点有没有材料依据"，边必须
指得回本库的 `source_id`。

**幂等**：`library.ingest` 按 `content_hash` 去重，`graph_store` 的边 `UNIQUE` 带
`(src, dst, relation, source_id, chunk_start)`，所以同一份正文重复灌不会长出新边。
`--reset` 是另一件事：它把整张图清空（旧图那批指错库的边只能这样处理）。

用法：

    python scripts/ingest_graph_corpus.py --reset
    python scripts/ingest_graph_corpus.py --docs-only
    python scripts/ingest_graph_corpus.py --report data/graph-ingest-report.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.contracts.source import Source, SourceKind  # noqa: E402
from app.knowledge import graph_extract, graph_ingest, graph_store, library  # noqa: E402

DOCS = ["AGENT_PLAN.md", "PLAN.md", "ARCHITECTURE.md", "RAG-DECISIONS.md"]
DOCS_DIR = BACKEND.parent / "docs"

# 一份材料最多抽多少块。默认 20（`graph_ingest.DEFAULT_MAX_CHUNKS`）是给"上传一份
# 文件"那条路定的成本闸；这里是一次性的建图，闸门要放开到整份材料。
MAX_CHUNKS = 400

# 折算口径：中文按 1.5 字/token（DeepSeek 官方文档给的经验值，与
# `docs/RAG-DECISIONS.md` 第 8 节的 165k 用的是同一个数）。脚本量的是**字**，
# token 是折算值，报告里两个都写。
CHARS_PER_TOKEN = 1.5


class Meter:
    """包一层 `graph_extract.chat_async`，量出真实的输入/输出字符数。

    **不改产品代码**：`extract_graph` 内部直接调模块级的 `chat_async`，在脚本进程
    里把它换掉就能计量，退出时进程结束、改动消失。代价是这只在"同一进程内调用"
    时有效——`ingest_graph` 正是同进程调用，所以够用。
    """

    def __init__(self) -> None:
        self.calls = 0
        self.in_chars = 0
        self.out_chars = 0
        self.failed = 0

    def install(self) -> None:
        original = graph_extract.chat_async

        async def counted(system: str, user: str, **kwargs):
            self.calls += 1
            self.in_chars += len(system) + len(user)
            try:
                result = await original(system, user, **kwargs)
            except Exception:
                self.failed += 1
                raise
            self.out_chars += len(result) if isinstance(result, str) else len(str(result))
            return result

        graph_extract.chat_async = counted  # type: ignore[assignment]

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "failed": self.failed,
            "in_chars": self.in_chars,
            "out_chars": self.out_chars,
            "in_tokens_est": round(self.in_chars / CHARS_PER_TOKEN),
            "out_tokens_est": round(self.out_chars / CHARS_PER_TOKEN),
            "total_tokens_est": round(
                (self.in_chars + self.out_chars) / CHARS_PER_TOKEN),
        }


def _reset_graph() -> int:
    """清空整张图。分批删：一次 `DETACH DELETE` 上万节点会把事务撑爆。"""
    total = 0
    driver = graph_store._driver()
    with driver.session() as session:
        while True:
            record = session.run(
                "MATCH (n) WITH n LIMIT 5000 DETACH DELETE n RETURN count(*) AS c"
            ).single()
            removed = int(record["c"]) if record else 0
            total += removed
            if removed == 0:
                break
    return total


def _doc_sources() -> list[Source]:
    sources = []
    for name in DOCS:
        path = DOCS_DIR / name
        if not path.exists():
            print(f"跳过（不存在）：{path}")
            continue
        text = path.read_text(encoding="utf-8")
        sources.append(Source(
            kind=SourceKind.markdown, filename=name, text=text, status="parsed",
        ))
    return sources


def _library_sources() -> list[Source]:
    """`library_sources` 里现有的每一份，**按原 id 回填**（`source_id=` 显式给）。

    显式给 id 的理由：这些正文已经在库里了，再走一次 `library.ingest` 会按
    `content_hash` 命中同一行（幂等），但走一遍就多一次写库，而 `ingest_graph`
    的 `source_id` 参数正是为"重灌已有正文"准备的。
    """
    sources = []
    for item in library.list_sources(limit=library.count()):
        source = library.load(str(item["id"]))
        if source is not None and (source.text or "").strip():
            sources.append(source)
    return sources


async def _ingest(sources: list[Source], meter: Meter) -> list[dict]:
    rows = []
    for index, source in enumerate(sources, start=1):
        # 库里已有的来源显式带 id（重灌），docs 那批让 `library.ingest` 分配
        explicit = source.id if library.load(source.id) is not None else None
        started = time.perf_counter()
        stats = await graph_ingest.ingest_graph(
            source, max_chunks=MAX_CHUNKS, source_id=explicit)
        elapsed = time.perf_counter() - started
        chars = len(source.text or "")
        rows.append({
            "filename": source.filename,
            "source_id": explicit or "（新建）",
            "chars": chars,
            "chunks": stats["chunks"],
            "failed": stats["failed"],
            "aborted": stats["aborted"],
            "entities": stats["entities"],
            "edges": stats["edges"],
            "seconds": round(elapsed, 1),
        })
        notes = ""
        if stats["aborted"]:
            notes += "（中止）"
        if stats["failed"]:
            notes += f"（{stats['failed']} 块失败）"
        print(
            f"[{index}/{len(sources)}] {source.filename!r} {chars} 字 → "
            f"{stats['chunks']} 块 / 实体 {stats['entities']} / 边 {stats['edges']}"
            f"{notes} {elapsed:.1f}s",
            flush=True,
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="先清空整张图")
    parser.add_argument("--docs-only", action="store_true", help="只灌 docs 那四份")
    parser.add_argument("--library-only", action="store_true", help="只灌 library_sources")
    parser.add_argument("--report", default="", help="报告写到哪里（JSON）")
    args = parser.parse_args()

    if not graph_store.configured():
        print("没配 MINDMAP_NEO4J_PASSWORD：图存储未配置，先设环境变量。")
        return 2

    if args.reset:
        removed = _reset_graph()
        print(f"已清空图：{removed} 个节点（连同边与别名）")

    sources: list[Source] = []
    if not args.library_only:
        sources.extend(_doc_sources())
    if not args.docs_only:
        sources.extend(_library_sources())

    meter = Meter()
    meter.install()
    started = time.perf_counter()
    rows = asyncio.run(_ingest(sources, meter))
    elapsed = time.perf_counter() - started

    stats = graph_store.stats()
    report = {
        "materials": len(rows),
        "chars": sum(row["chars"] for row in rows),
        "chunks": sum(row["chunks"] for row in rows),
        "failed_chunks": sum(row["failed"] for row in rows),
        "seconds": round(elapsed, 1),
        "extraction": meter.as_dict(),
        "graph": stats,
        "per_material": rows,
    }
    print("\n=== 汇总 ===")
    print(json.dumps({k: v for k, v in report.items() if k != "per_material"},
                     ensure_ascii=False, indent=2))
    if args.report:
        path = Path(args.report)
        if not path.is_absolute():
            path = BACKEND / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"报告已写入 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
