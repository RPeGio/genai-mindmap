"""DeepSeek Search / Fetch 接进外部检索链路的实测脚本。

它回答的全是"接进来之后到底变成了什么样"，而不是"代码有没有跑通"（那是用例的事）。
五条命令，各回答一个问题：

    python -m scripts.measure_external_deepseek --search      # 五源 vs 六源：条数/来源/耗时
    python -m scripts.measure_external_deepseek --sampling    # Search 自身：字数/站点/过门率
    python -m scripts.measure_external_deepseek --truncation  # 正文长度 → chunk 数 / 检索池占比
    python -m scripts.measure_external_deepseek --fill        # 用途 B：content 为空时补抓
    python -m scripts.measure_external_deepseek --failure     # 失败降级：base_url 指错

## 为什么必须关缓存、必须真打网

`--search` / `--sampling` / `--failure` 的每一个读数都建立在"这一次真发了请求"上：
命中缓存会让耗时变成 0、让两份池子变成同一份。`--truncation` 与 `--fill` 用的是
**同一次真实响应**（不是缓存，也不是编的夹具），因为要量的是"真实网页正文有多长、
切出来多少块"——那种数字换个主题就变。

## 主题清单复用 `measure_external_relevance.TOPICS`

同一批 20 个主题（含两个反例），不另抄一份：两份清单会分叉，而"五源当时是 49 条
/ 10 条噪声"这个基线只有在同一批主题上才可比。直接从同级模块 import。

## 读数怎么读

  - `--search`：两组各自真跑一次 `search_external`（五源 / 六源），所以两组的差异
    里混着源站抖动。**这不是 A/B 的完美设计**，是"同一时段、同一出口、同一批主题"
    的现实对照；结论只看方向与量级（差一条还是差一半），不看小数。
  - `--truncation`：chunk 数由 `library.chunks` 数（与生产入库同一个函数），
    检索池占比由 `knowledge.service.search_parent` 在**同一批来源**上算。
  - `--fill`：真实世界里 `content` 为空的命中不一定出现（这次 60 条里 0 条）。
    为零时脚本会**构造**一条空 content 的命中再调补抓——那证明的是"这条路能走通"，
    不是"它被触发过"，报告里必须如实这么写。
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Sequence

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

# 缓存必须关：见模块 docstring。
os.environ.setdefault("MINDMAP_EXTERNAL_CACHE", "0")
# 库检索与父块检索：`--truncation` 要 `search_parent` 的真实行为，不要库里的历史材料。
os.environ.setdefault("MINDMAP_LIBRARY_SEARCH", "0")
os.environ.setdefault("MINDMAP_LIBRARY_REMEMBER", "0")

from app.knowledge import external  # noqa: E402

sys.path.insert(0, str(BACKEND / "scripts"))
import measure_external_relevance as baseline  # noqa: E402

TOPICS: list[tuple[str, str]] = baseline.TOPICS
SEARCH_ENV = external.DEEPSEEK_SEARCH_ENV


def _key() -> str:
    """跑实测用的 key。**不写死**：从环境变量读，缺了直接退出。"""
    _, key = external._deepseek_config()
    if not key:
        raise SystemExit(
            "没有 key：设置 MINDMAP_DEEPSEEK_TOOLS_API_KEY 或 MINDMAP_LLM_API_KEY")
    return key


def _median(values: Sequence[float]) -> float:
    return statistics.median(values) if values else 0.0


def _label(hit: external.ExternalHit) -> str:
    """一条命中的短标签：源/站点 + 标题前 18 字。"""
    where = hit.site or hit.source_name
    return f"{hit.source_name}({where})/{hit.title[:18]}"


def _search_once(topic: str, *, six: bool) -> tuple[list[external.ExternalHit], float]:
    """跑一次 `search_external`，返回 `(命中, 秒)`。"""
    os.environ[SEARCH_ENV] = "1" if six else "0"
    started = time.perf_counter()
    hits = external.search_external(topic, limit=external.DEFAULT_LIMIT)
    return list(hits), time.perf_counter() - started


def run_search(limit_topics: int | None) -> int:
    """五源 vs 六源：条数、来源、耗时。"""
    topics = TOPICS if limit_topics is None else TOPICS[:limit_topics]
    rows: list[dict[str, Any]] = []
    for index, (topic, kind) in enumerate(topics, 1):
        five_hits, five_seconds = _search_once(topic, six=False)
        six_hits, six_seconds = _search_once(topic, six=True)
        rows.append({
            "topic": topic, "kind": kind,
            "five": five_hits, "five_s": five_seconds,
            "six": six_hits, "six_s": six_seconds,
        })
        print(f"[{index}/{len(topics)}] {topic}：五源 {len(five_hits)} 条 "
              f"{five_seconds:.2f}s / 六源 {len(six_hits)} 条 {six_seconds:.2f}s")
        for hit in five_hits:
            print(f"    五源  {_label(hit)}  {len(hit.text)} 字")
        for hit in six_hits:
            mark = " *" if hit.source_name == external.DEEPSEEK_SOURCE else "  "
            print(f"   {mark}六源  {_label(hit)}  {len(hit.text)} 字")

    print("\n## 汇总\n")
    print("| 主题 | 类型 | 五源条数 | 五源耗时 | 六源条数 | 六源耗时 | 六源里的 Search 条数 |")
    print("|---|---|---|---|---|---|---|")
    for row in rows:
        found = sum(1 for h in row["six"] if h.source_name == external.DEEPSEEK_SOURCE)
        print(f"| {row['topic']} | {row['kind']} | {len(row['five'])} | "
              f"{row['five_s']:.2f}s | {len(row['six'])} | {row['six_s']:.2f}s | {found} |")

    five_counts = [len(row["five"]) for row in rows]
    six_counts = [len(row["six"]) for row in rows]
    five_times = [row["five_s"] for row in rows]
    six_times = [row["six_s"] for row in rows]
    search_in = sum(1 for row in rows if any(
        h.source_name == external.DEEPSEEK_SOURCE for h in row["six"]))
    print(f"\n- 返回条数：五源合计 {sum(five_counts)}、六源合计 {sum(six_counts)}")
    print(f"- 至少 1 条：五源 {sum(1 for n in five_counts if n)}/{len(rows)}、"
          f"六源 {sum(1 for n in six_counts if n)}/{len(rows)}")
    print(f"- 有 Search 命中进来的主题：{search_in}/{len(rows)}")
    print(f"- 耗时中位：五源 {_median(five_times):.2f}s、六源 {_median(six_times):.2f}s；"
          f"最大 五源 {max(five_times):.2f}s、六源 {max(six_times):.2f}s")

    names: dict[str, int] = {}
    for row in rows:
        for hit in row["six"]:
            names[hit.source_name] = names.get(hit.source_name, 0) + 1
    print("- 六源里各源出现的条数：" + "、".join(
        f"{name} {count}" for name, count in sorted(names.items())))
    return 0


def run_sampling(limit_topics: int | None) -> int:
    """Search 自身：条数、正文长度、站点、权威度、分数，以及**过词面门的比例**。"""
    _key()
    topics = TOPICS if limit_topics is None else TOPICS[:limit_topics]
    os.environ[SEARCH_ENV] = "1"
    total = passed = empty_content = 0
    lengths: list[int] = []
    seconds: list[float] = []
    for index, (topic, kind) in enumerate(topics, 1):
        budget = external._Budget(2, 30.0)
        started = time.perf_counter()
        hits = external._deepseek_search(topic, budget, external.DEFAULT_LIMIT)
        elapsed = time.perf_counter() - started
        seconds.append(elapsed)
        kept = external._relevant_hits(topic, hits)
        total += len(hits)
        passed += len(kept)
        empty_content += sum(1 for hit in hits if not hit.text)
        lengths.extend(len(hit.text) for hit in hits)
        print(f"[{index}/{len(topics)}] {topic}（{kind}）：{len(hits)} 条、"
              f"过门 {len(kept)} 条、{elapsed:.2f}s")
        for hit in hits:
            verdict = "过" if hit in kept else "挡"
            print(f"    {verdict} {hit.site or '-'} | auth={hit.authority} | "
                  f"rr={hit.rerank_score:.3f} | {len(hit.text)} 字 | {hit.title[:30]}")
    print("\n## 汇总\n")
    print(f"- Search 返回合计 {total} 条，过词面门 {passed} 条"
          f"（{passed / total:.0%}）" if total else "- 一条都没有")
    print(f"- 正文长度：中位 {_median(lengths):.0f} 字、"
          f"最短 {min(lengths) if lengths else 0}、最长 {max(lengths) if lengths else 0}")
    print(f"- 耗时：中位 {_median(seconds):.2f}s、最大 {max(seconds):.2f}s")
    print(f"- 空 content 条数：{empty_content}（用途 B 的真实触发次数）")
    return 0


def _sources_of(hits: Sequence[external.ExternalHit]) -> list[Any]:
    """命中 → `Source`（走 `agent.tools._external_source`，与生产同一个入口）。"""
    from app.agent import tools as agent_tools

    return [agent_tools._external_source(hit) for hit in hits]


def run_truncation(limit_topics: int | None) -> int:
    """正文长度 → chunk 数；以及"六源 vs 五源"时检索池里的源分布。

    两条读数共用一个临时库：`library.ingest` 是生产入库的同一条路，`chunks()` 是
    同一条切块实现——自己写一个"按 300 字数一数"的近似只会得到另一个数。
    """
    from app.contracts.source import Source, SourceKind
    from app.knowledge import library, service

    _key()
    tmp = tempfile.mkdtemp(prefix="measure-deepseek-")
    os.environ["MINDMAP_DB_PATH"] = str(Path(tmp) / "probe.db")
    topics = TOPICS if limit_topics is None else TOPICS[:limit_topics]
    os.environ[SEARCH_ENV] = "1"

    def chunk_count(text: str) -> int:
        probe = Source(kind=SourceKind.web, text=text, status="parsed",
                       filename=f"probe: {len(text)}")
        return len(library.chunks(library.ingest(probe)))

    print("## 一、正文长度 → chunk 数（Search 的每一条）\n")
    print("| 主题 | 站点 | 原文长度 | 4000 字档 | 8000 字档 | 不截断 |")
    print("|---|---|---|---|---|---|")
    for topic, _kind in topics:
        budget = external._Budget(4, 30.0)
        hits = external._deepseek_search(topic, budget, external.DEFAULT_LIMIT)
        for hit in hits:
            # 拿**原文**：`_deepseek_search` 已经把正文截到 4000，从 `raw_chars`
            # 分不出"原文多长"与"含多少空白"，所以按 url 再要一份不截断的
            # （一次请求，0.005 元）。
            raw = external._deepseek_fetch(hit.url, max_chars=10**9)
            text = raw.text if raw is not None else hit.text
            print(f"| {topic} | {hit.site or '-'} | {len(text)} | "
                  f"{chunk_count(external.truncate_chars(text, 4000))} | "
                  f"{chunk_count(external.truncate_chars(text, 8000))} | "
                  f"{chunk_count(text)} |")

    print("\n## 二、检索池里各源占几条（top_k=3，纯 BM25）\n")
    print("| 主题 | 五源：源分布 | 五源：候选 chunk 数 | 六源：源分布 | 六源：候选 chunk 数 |")
    print("|---|---|---|---|---|")

    def pool(topic: str, *, six: bool) -> tuple[str, int]:
        hits, _ = _search_once(topic, six=six)
        sources = _sources_of(hits)
        chunks = sum(len(library.chunks(library.ingest(source))) for source in sources)
        found = service.search_parent(sources, topic, external.DEFAULT_LIMIT)
        names = {source.id: (source.filename or "?").split(":", 1)[0] for source in sources}
        by_source: dict[str, int] = {}
        for hit in found:
            name = names.get(hit.source_id, "?")
            by_source[name] = by_source.get(name, 0) + 1
        return "、".join(f"{k} {v}" for k, v in sorted(by_source.items())), chunks

    for topic, _kind in topics:
        five_pool, five_chunks = pool(topic, six=False)
        six_pool, six_chunks = pool(topic, six=True)
        print(f"| {topic} | {five_pool or '—'} | {five_chunks} | "
              f"{six_pool or '—'} | {six_chunks} |")
    return 0


def run_fill(limit_topics: int | None) -> int:
    """用途 B：`content` 为空时按 URL 补抓。

    先数真实世界里有几条空 `content`；一条都没有时**构造**一条（把正文清空、留下
    url 与标题），再走 `_fill_missing_content`。构造出来的那条证明的是"补抓这条路
    能走通"，报告里要如实区分这两种证据。
    """
    _key()
    topics = TOPICS[:limit_topics] if limit_topics else TOPICS
    os.environ[SEARCH_ENV] = "1"
    real_empty = 0
    sample: external.ExternalHit | None = None
    for topic, _kind in topics:
        budget = external._Budget(2, 30.0)
        hits = external._deepseek_search(topic, budget, external.DEFAULT_LIMIT)
        real_empty += sum(1 for hit in hits if not hit.text)
        if sample is None and hits:
            sample = hits[0]
    print(f"- 真实世界里 `content` 为空的命中：{real_empty} 条"
          f"（{len(topics)} 个主题、每主题最多 3 条）")
    if sample is None:
        print("- 一条 Search 命中都没拿到，无法构造样本")
        return 1

    empty_hit = replace(sample, text="", raw_chars=0)
    budget = external._Budget(3, 30.0)
    started = time.perf_counter()
    filled = external._fill_missing_content([empty_hit], budget, None)
    elapsed = time.perf_counter() - started
    print(f"- 构造的空 content 命中：{empty_hit.url}")
    print(f"- 补抓结果：{len(filled)} 条，耗时 {elapsed:.2f}s，请求 {budget.used} 次")
    if filled:
        print(f"  正文 {len(filled[0].text)} 字、标题 {filled[0].title[:40]!r}、"
              f"站点 {filled[0].site or '-'}")
    return 0 if filled else 1


def run_failure(limit_topics: int | None) -> int:
    """失败降级：把 Search 的 base_url 指到一个不存在的端口，看整条链还活不活。"""
    _key()
    topics = TOPICS if limit_topics is None else TOPICS[:limit_topics]
    good_base = os.environ.get(external.DEEPSEEK_BASE_ENV, "")
    os.environ[external.DEEPSEEK_BASE_ENV] = "http://127.0.0.1:9/v1"
    os.environ[SEARCH_ENV] = "1"
    try:
        alive = 0
        for topic, _kind in topics:
            hits, seconds = _search_once(topic, six=True)
            five_only = [hit for hit in hits
                         if hit.source_name != external.DEEPSEEK_SOURCE]
            alive += 1 if five_only else 0
            print(f"- {topic}：{len(hits)} 条（其中非 Search {len(five_only)} 条）、"
                  f"{seconds:.2f}s")
        print(f"\n- 五源仍然有结果的 主题：{alive}/{len(topics)}")
        print("- 结论：" + ("整条链没被拖垮" if alive else "五源也全空了，需要看代理"))
        return 0 if alive else 1
    finally:
        if good_base:
            os.environ[external.DEEPSEEK_BASE_ENV] = good_base
        else:
            os.environ.pop(external.DEEPSEEK_BASE_ENV, None)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--search", action="store_true", help="五源 vs 六源")
    parser.add_argument("--sampling", action="store_true", help="Search 自身的读数")
    parser.add_argument("--truncation", action="store_true", help="正文长度 → chunk 数")
    parser.add_argument("--fill", action="store_true", help="用途 B：空 content 补抓")
    parser.add_argument("--failure", action="store_true", help="失败降级")
    parser.add_argument("--topics", type=int, default=None, help="只跑前 N 个主题")
    args = parser.parse_args()
    if args.search:
        return run_search(args.topics)
    if args.sampling:
        return run_sampling(args.topics)
    if args.truncation:
        return run_truncation(args.topics)
    if args.fill:
        return run_fill(args.topics)
    if args.failure:
        return run_failure(args.topics)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
