"""外部知识检索的实测脚本（一次性，不入库、不写 DB）。

跑 `knowledge.external.search_external`，逐主题记录：条数、来源、总字数、耗时、
是否全部失败。只量抓取层，不碰 `library.ingest`（那会写库）。

## 实测结果（2026-11，本机 Clash Verge 代理，18 个主题 × 2 轮）

| 指标 | 值 |
|---|---|
| 成功率（≥1 条） | 35/36 = 97.2% |
| 延迟 中位 / P90 / 最大 | 4.3–4.5s / 5.6–6.0s / 6.5s |
| 中文主题的中文内容占比 | 68%（字符口径，14 个中文主题） |
| 每次命中条数 | 恒为 3（`DEFAULT_LIMIT`） |
| 每次命中总字数 中位 | 4910 字 |
| 按源 | wikipedia 18/18、openalex 18/18、crossref 10/18、arxiv 4/18、**baike 0/18** |

唯一一次失败是首轮的"碳中和"（OpenAlex / Crossref TLS 被掐、arXiv 502，6 秒内全空），
同一主题第二轮 2.0 秒拿到 3 条 3128 字——代理节点抖动，不是主题搜不到。

快懂百科 18/18 全挂（`SSL: UNEXPECTED_EOF_WHILE_READING`）：它是"第二个中文百科
视角"，挂了不影响 ≥1 条的成功率。这条结论写进了 `tools.EXTERNAL_KNOWLEDGE_ENV`
上面那段注释（"什么时候会失败"第 2 条）。

**搜到 ≠ 搜得准**：代码术语与长问句上明显偏差（`C++ 移动语义` 三条里两条是语言学
论文、`Rust 所有权` 拿到"农业"、19 字长问句拿到"为什么足球走向世界…"）。这是
"逐字校验只保证引文出自该来源"的直接后果，也是默认开之后 `content` 里必须说
"依据来自外部检索"的理由。

用法（backend 目录下）：

    python scripts/measure_external.py

输出：每条一行 JSON（同时落 `scripts/measure_external.jsonl`），末尾一份汇总。
"""
from __future__ import annotations

import json
import re
import statistics
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.knowledge import external  # noqa: E402

CJK = re.compile(r"[\u4e00-\u9fff]")

# 主题清单：覆盖中文抽象概念 / 中文具体名词 / 英文主题 / 冷门生造 / 纯品牌名 /
# 长 query 六类。`kind` 只用于汇总分组，不参与检索。
TOPICS: list[tuple[str, str]] = [
    ("碳中和", "中文抽象概念"),
    ("内卷", "中文抽象概念"),
    ("注意力机制", "中文抽象概念"),
    ("幻觉", "中文抽象概念"),
    ("用户画像", "中文抽象概念"),
    ("最终一致性", "中文抽象概念"),
    ("量子色动力学", "中文具体名词"),
    ("C++ 移动语义", "中文具体名词"),
    ("TCP 三次握手", "中文具体名词"),
    ("光合作用", "中文具体名词"),
    ("Rust 所有权", "中文具体名词"),
    ("transformer architecture", "英文主题"),
    ("consensus algorithm", "英文主题"),
    ("Byzantine fault tolerance", "英文主题"),
    ("加密货币的量子套利", "冷门/生造"),
    ("Cursor", "纯品牌名"),
    ("茅台", "纯品牌名"),
    ("为什么中文大模型在长文本任务上表现不稳定", "长 query"),
]

OUT = Path(__file__).with_name("measure_external.jsonl")


def instrument() -> dict[str, list[int]]:
    """按源统计命中数（patch `_run_source`，它是每个源的入口）。"""
    tally: dict[str, list[int]] = {}
    original = external._run_source

    def wrapped(name: str, fetch):
        hits = original(name, fetch)
        tally.setdefault(name, []).append(len(hits))
        return hits

    external._run_source = wrapped
    return tally


def main() -> int:
    tally = instrument()
    rows: list[dict] = []
    for topic, kind in TOPICS:
        started = time.perf_counter()
        try:
            hits = external.search_external(topic)
            error = None
        except Exception as exc:  # noqa: BLE001 — 实测脚本，异常本身就是数据
            hits = []
            error = f"{type(exc).__name__}: {exc}"
        elapsed = time.perf_counter() - started
        chars = sum(len(hit.text) for hit in hits)
        cjk = len(CJK.findall("".join(hit.text for hit in hits)))
        row = {
            "topic": topic,
            "kind": kind,
            "hits": len(hits),
            "sources": sorted({hit.source_name for hit in hits}),
            "titles": [hit.title[:40] for hit in hits],
            "chars": chars,
            "cjk_chars": cjk,
            "cjk_ratio": round(cjk / chars, 3) if chars else 0.0,
            "seconds": round(elapsed, 2),
            "ok": bool(hits),
            "error": error,
        }
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)

    OUT.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                   encoding="utf-8")

    total = len(rows)
    good = [r for r in rows if r["ok"]]
    lat = sorted(r["seconds"] for r in rows)
    p90 = lat[min(len(lat) - 1, int(round(0.9 * (len(lat) - 1))))]
    zh_rows = [r for r in rows if CJK.search(r["topic"])]
    zh_ok = [r for r in rows if r["ok"] and r["chars"] and CJK.search(r["topic"])]
    summary = {
        "total": total,
        "success": len(good),
        "success_rate": round(len(good) / total, 3),
        "latency_median": statistics.median(lat),
        "latency_p90": p90,
        "latency_max": lat[-1],
        "latency_min": lat[0],
        "zh_topics": len(zh_rows),
        "zh_content_share": round(
            sum(r["cjk_chars"] for r in zh_ok) / max(sum(r["chars"] for r in zh_ok), 1), 3),
        "chars_median": statistics.median(sorted(r["chars"] for r in rows)),
        "failed_topics": [r["topic"] for r in rows if not r["ok"]],
        "errors": [r["error"] for r in rows if r["error"]],
        "per_source": {
            name: {"runs": len(counts), "with_hits": sum(1 for c in counts if c),
                   "hits": sum(counts)}
            for name, counts in tally.items()
        },
    }
    print("\nSUMMARY " + json.dumps(summary, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
