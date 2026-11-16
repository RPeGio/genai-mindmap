"""外部检索「词面相关性门」的实测脚本：一次抓取，五种判据在同一份候选池上对照。

## 为什么不是"跑两遍对比"

"有门 / 无门"如果各联网跑一遍，两组的差异里混着**源站结果抖动**（同一主题两次
检索回来的条目可以完全不同，`measure_external.py` 已经踩过：`碳中和` 首轮 0 条、
次轮 3 条）。那不是在量门，是在量网络。

所以这里只联网**一次**：`search_external` 内部先聚合、后截断，脚本把 `_interleave`
的入参（全部候选）拦下来存盘；之后每种判据都在**同一份候选池**上就地重算。各组
用的是同一批 `ExternalHit`，差异只可能来自判据本身。

## 流程

    python scripts/measure_external_relevance.py --collect    # 联网一次，写 pool.jsonl
    python scripts/measure_external_relevance.py --report     # 离线：生产判据 vs 无门
    python scripts/measure_external_relevance.py --variants   # 离线：全部判据对照

`--collect` 之后的一切都是离线的：改判据、改阈值、改判定表，重跑 `--report` /
`--variants` 即可，不再联网。

## 实测结果（2026-11，本机 Clash Verge 代理，20 个主题，150 条候选）

同一份候选池（`measure_external_relevance.pool.jsonl`），五种判据：

| 判据 | 返回条数 | 噪声条目 | 三条全相关 | 成功率 | 误杀 |
|---|---|---|---|---|---|
| 无门 | 60 | 22 | 10/20 | 100% | 0 |
| bigram（任务最初口径） | 59 | 22 | 9/20 | 100% | 2 |
| bigram + 跨语言放行 | 60 | 23 | 9/20 | 100% | 1 |
| 整块词块 | 47 | 9 | 11/20 | 85% | 2 |
| **前缀 3 字 + 跨语言放行（生产）** | **49** | **10** | **12/20** | **95%** | **1** |

四条结论：

1. **bigram 口径被证伪**。它只挡下"完全没共享词面"的命中，而源站排序本来就很少
   返回那种东西；噪声一条没少（22→22），三条全相关反而掉了一个（10→9），还多出
   2 条误杀。值得一提它唯一的"功劳"就是误杀：`用戶畫像`（繁体）不含 `用户画像`
   （简体），一条真相关的维基条目被简繁差异挡掉了。
2. **整块词块太严**。噪声砍到 9，但把"概念 + 限定语"整类查询打死：`碳中和技术路径`
   的命中是维基 `碳中和` 词条，整块判据判它无关（既有用例
   `test_wikipedia_falls_back_to_search_when_title_misses` 钉的正是这件事）。成功率
   掉到 85%，误杀 2 条——多杀的那条是 `consensus algorithm` 的中文正文 `Raft`，
   跨语言放行能救回它。
3. **前缀 3 字是最终口径**：噪声 22→10（砍 55%）、三条全相关 10→12、成功率 95%、
   误杀 1。代价是 1 个主题返回 0 条（`zzqx 不存在的词`）——那本来是**正确结果**：
   一个不存在的词不该有任何"依据"。
4. **残余 10 条噪声里 9 条是"词面相关、语义无关"**，门一条都挡不住：`幻觉` 的戏曲
   论文、晶体学论文（正文写着"最终一致性因子为R=0.030"）、`Rust` 的小麦锈病、
   `cursor` 的光标论文、`学习路线` 的教学模式；第 10 条是跨语言放行进来的（长问句
   留下的英文诗论）。这是词面判据的天花板，不是阈值没调好。

逐主题（无门 → 生产判据）：

| 主题 | 无门 | 有门 | 有门丢掉的 |
|---|---|---|---|
| 碳中和 | 3 条 / 0 噪声 | 3 / 0 | — |
| 内卷 | 3 / 0 | 3 / 0 | — |
| 注意力机制 | 3 / 0 | 3 / 0 | — |
| 幻觉 | 3 / 2 | 3 / 2 | 闪灵 |
| 用户画像 | 3 / 0 | 3 / 0 | 人物誌（简繁误杀，席位由另一条相关命中补上）、浏览器指纹、百度指数 |
| 最终一致性 | 3 / 2 | 3 / 2 | RRM 一致性测试、快递"最后一公里" |
| 量子色动力学 | 3 / 0 | 3 / 0 | — |
| C++ 移动语义 | 3 / 2 | 1 / 0 | HTML、语义特征考察、苗瑶语、介词研究、语义记忆 |
| TCP 三次握手 | 3 / 0 | 3 / 0 | QUIC |
| 光合作用 | 3 / 0 | 3 / 0 | — |
| Rust 所有权 | 3 / 1 | 3 / 1 | 农业、加速世界（小麦锈病仍在：同形异义） |
| transformer architecture | 3 / 0 | 3 / 0 | — |
| consensus algorithm | 3 / 0 | 3 / 0 | —（`Raft` 靠跨语言放行才留住） |
| Byzantine fault tolerance | 3 / 0 | 3 / 0 | — |
| 加密货币的量子套利 | 3 / 3 | 1 / 1 | 6 条（"加密"二字留下的那条仍在） |
| Cursor | 3 / 2 | 3 / 2 | Winner's Curse、Resource Curse（光标类仍在：同形异义） |
| 茅台 | 3 / 1 | 3 / 0 | 舟台两岸 |
| 为什么中文大模型… | 3 / 3 | 1 / 1 | 6 条（"为什么足球…"被"跳过句首虚词后的 `中文大`"挡下） |
| rust 学习路线 | 3 / 3 | 1 / 1 | 陳淑樺、鲁托斯拉夫斯基等 5 条（"深度学习路线"仍留） |
| zzqx 不存在的词 | 3 / 3 | **0 / 0** | 全部 3 条 |

## 人工判定口径

`measure_external_relevance.judged.json` 是**人工判定**表，判据一句话：**这条命中
讲的是不是 query 问的那件事**，看主体对象不看词面——`Rust 所有权` 那条讲小麦锈病
的论文即使出现 `rust` 也算无关；反过来 `注意力机制` 拿到 "Attention Is All You
Need" 这种全英文命中算**相关**（它讲的就是 query 问的事，只是语言不同）。这张表是
本脚本唯一不能自动化的部分，所以它逐条写在文件里，让任何人能复核、能改判——改了
重跑 `--report` 即可。

`三条全相关` 的定义：这次返回的每一条（不足 3 条时按实际条数）都被判为相关；
返回 0 条**不算**（没有可错的，也没有对的）。

用法与产出见文件末尾 `main()`。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.knowledge import external  # noqa: E402

CJK = re.compile(r"[\u4e00-\u9fff]")

# 与 `measure_external.py` 同一批 18 个主题，外加这次实测暴露的两个反例：
#   - `rust 学习路线`：8 字材料退化成主题提示后，拿到「陳淑樺」维基页 + 音乐论文；
#   - `zzqx 不存在的词`：一个不存在的词也返回 3 条 crossref 垃圾。
# 反例必须进台账，否则"噪声条目数"这一列只能靠记忆。
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
    ("rust 学习路线", "反例：短材料退化"),
    ("zzqx 不存在的词", "反例：不存在的词"),
]

POOL = Path(__file__).with_name("measure_external_relevance.pool.jsonl")

# 池子里每条正文只留前 400 字。不是为了省磁盘：判据只看前 `RELEVANCE_CHARS`（300）
# 字，人工判定也只需要看清开头讲什么。留全文会让这个可入库的复现工件变成 600 KB
# 的外部正文副本（其中一大半是别人的版权内容）。`raw_chars` 仍保留原始长度，
# "这条本来多长"这个信息不丢。
POOL_TEXT_CHARS = 400

# 判定用：`(topic, 命中标题)` → 相关 / 无关。键是**标题**而不是序号：序号会随源的
# 返回顺序漂，标题是这条资料的身份。判定表放在同目录的 `...judged.json`（人工填，
# 理由见那个文件里的 `_README`），子串匹配、第一条命中生效。
JUDGED_PATH = Path(__file__).with_name("measure_external_relevance.judged.json")
# 人工判定的原文：`{topic: [[标题子串, 是否相关], ...]}`，按顺序匹配。
_RULES: dict[str, list[tuple[str, bool]]] = {}
# 解析结果：`(topic, title)` -> bool。缺席即"未判定"，绝不默认算相关。
JUDGED: dict[tuple[str, str], bool] = {}


def _load_rules() -> None:
    if not JUDGED_PATH.exists():
        print(f"!! 判定表缺席：{JUDGED_PATH}（所有命中都会被记为未判定）")
        return
    payload = json.loads(JUDGED_PATH.read_text(encoding="utf-8"))
    for topic, rules in payload.items():
        if topic.startswith("_"):
            continue
        _RULES[topic] = [(str(needle), bool(verdict)) for needle, verdict in rules]


def _judged(topic: str, title: str) -> bool | None:
    """查人工判定：先精确命中，再按子串顺序匹配，都没有返回 None（未判定）。"""
    if (topic, title) in JUDGED:
        return JUDGED[(topic, title)]
    for needle, verdict in _RULES.get(topic, []):
        if needle in title:
            JUDGED[(topic, title)] = verdict
            return verdict
    return None



def _pool_capture(monkeypatch_target: Any = external) -> list[dict[str, Any]]:
    """收集每次 `_interleave` 的入参（全部候选）。"""
    captured: list[dict[str, Any]] = []
    original = monkeypatch_target._interleave

    def wrapped(hits, limit, *, prefer_language=None):
        captured.append({
            "pool": [
                {
                    "source_name": hit.source_name,
                    "title": hit.title,
                    "text": hit.text[:POOL_TEXT_CHARS],
                    "url": hit.url,
                    "language": hit.language,
                    "raw_chars": hit.raw_chars,
                }
                for hit in hits
            ],
            "limit": limit,
            "prefer_language": prefer_language,
        })
        return original(hits, limit, prefer_language=prefer_language)

    monkeypatch_target._interleave = wrapped
    return captured


def collect() -> int:
    captured = _pool_capture()
    rows: list[dict[str, Any]] = []
    for topic, kind in TOPICS:
        before = len(captured)
        started = time.perf_counter()
        try:
            hits = external.search_external(topic)
            error = None
        except Exception as exc:  # noqa: BLE001 — 实测脚本，异常本身就是数据
            hits = []
            error = f"{type(exc).__name__}: {exc}"
        elapsed = time.perf_counter() - started
        pools = captured[before:]
        row = {
            "topic": topic,
            "kind": kind,
            "seconds": round(elapsed, 2),
            "error": error,
            "returned": [hit.title for hit in hits],
            "pools": pools,
        }
        rows.append(row)
        print(json.dumps({k: row[k] for k in ("topic", "seconds", "returned", "error")},
                         ensure_ascii=False), flush=True)
    POOL.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                    encoding="utf-8")
    print(f"\n写盘：{POOL}（{len(rows)} 个主题）")
    return 0


def _hit(spec: dict[str, Any]) -> external.ExternalHit:
    return external.ExternalHit(
        title=spec["title"], text=spec["text"], url=spec["url"],
        source_name=spec["source_name"], fetched_at="2026-01-01T00:00:00Z",
        language=spec["language"], raw_chars=spec.get("raw_chars", len(spec["text"])),
    )


def _judged(topic: str, title: str) -> bool | None:
    """查人工判定：先精确命中，再按子串顺序匹配，都没有返回 None（未判定）。"""
    if (topic, title) in JUDGED:
        return JUDGED[(topic, title)]
    for needle, verdict in _RULES.get(topic, []):
        if needle in title:
            JUDGED[(topic, title)] = verdict
            return verdict
    return None


def _stats(topic: str, titles: list[str]) -> dict[str, Any]:
    verdicts = [_judged(topic, title) for title in titles]
    known = [v for v in verdicts if v is not None]
    return {
        "count": len(titles),
        "noise": sum(1 for v in known if not v),
        "unjudged": sum(1 for v in verdicts if v is None),
        # 三条全相关：这次返回的**每一条**都被判相关（返回 0 条不算）
        "all_relevant": bool(titles) and all(v is True for v in known) and len(known) == len(titles),
        "relevant": sum(1 for v in known if v),
    }


def report() -> int:
    _load_rules()
    rows = [json.loads(line) for line in POOL.read_text(encoding="utf-8").splitlines() if line]
    modes = ("no_gate", "gate")
    totals = {mode: {"count": 0, "noise": 0, "all_relevant": 0, "topics_hit": 0,
                     "relevant": 0, "unjudged": 0} for mode in modes}
    table: list[dict[str, Any]] = []

    for row in rows:
        topic = row["topic"]
        pools = row["pools"]
        # 全部候选（一次检索可能有多个池子——`_interleave` 只该被调一次，这里兜住）
        pool = [_hit(spec) for item in pools for spec in item["pool"]]
        if not pool and row.get("returned"):
            print(f"!! {topic}: 池子为空但当时返回了 {row['returned']}（pool 未捕获）")
        limit = pools[0]["limit"] if pools else external.DEFAULT_LIMIT
        prefer = pools[0]["prefer_language"] if pools else None

        no_gate = external._interleave(pool, limit, prefer_language=prefer)
        # 过门集合（未截断）：这才是"门扔掉了谁"，不能拿截断后的结果反推——
        # 候选池 7 条、只返回 3 条时，另外 4 条是被**名额**挤掉的，不是被门滤掉的。
        passed = external._relevant_hits(topic, pool)
        gated = external._interleave(passed, limit, prefer_language=prefer)

        entry: dict[str, Any] = {"topic": topic, "kind": row["kind"], "seconds": row["seconds"]}
        for mode, hits in (("no_gate", no_gate), ("gate", gated)):
            titles = [hit.title for hit in hits]
            stats = _stats(topic, titles)
            entry[mode] = {"titles": titles, **stats}
            totals[mode]["count"] += stats["count"]
            totals[mode]["noise"] += stats["noise"]
            totals[mode]["relevant"] += stats["relevant"]
            totals[mode]["unjudged"] += stats["unjudged"]
            totals[mode]["topics_hit"] += 1 if titles else 0
            totals[mode]["all_relevant"] += 1 if stats["all_relevant"] else 0
        entry["dropped"] = sorted({hit.title for hit in pool} - {hit.title for hit in passed})
        # 门的两种错误分开记：误杀（真相关被扔掉）比漏放（噪声混进去）严重得多。
        entry["dropped_but_judged_relevant"] = [
            title for title in entry["dropped"] if _judged(topic, title) is True
        ]
        # 门放行了、但没进前 3 条的相关命中：那不是门的功劳也不算它的错，
        # 用于核对"三名额"本身有没有挤掉好东西。
        entry["passed_but_not_returned"] = sorted(
            {hit.title for hit in passed if _judged(topic, hit.title) is True}
            - {hit.title for hit in gated})
        table.append(entry)
        print(json.dumps(entry, ensure_ascii=False), flush=True)

    print("\n| 主题 | 无门 条数/噪声 | 有门 条数/噪声 | 有门丢掉的 |")
    print("|---|---|---|---|")
    for entry in table:
        print(f"| {entry['topic']} | {entry['no_gate']['count']}/{entry['no_gate']['noise']} "
              f"| {entry['gate']['count']}/{entry['gate']['noise']} "
              f"| {'；'.join(t[:28] for t in entry['dropped']) or '—'} |")
    print()
    for mode in modes:
        t = totals[mode]
        rate = t["topics_hit"] / len(rows)
        print(f"{mode}: 条数 {t['count']}｜噪声 {t['noise']}｜相关 {t['relevant']}"
              f"｜三条全相关 {t['all_relevant']}/{len(rows)}"
              f"｜未判定 {t['unjudged']}｜成功率 {rate:.1%}")
    killed = [(e["topic"], title) for e in table
              for title in e["dropped_but_judged_relevant"]]
    print(f"\n误杀（判为相关却被门滤掉）：{len(killed)} 条")
    for topic, title in killed:
        print(f"  - {topic} :: {title[:60]}")
    return 0


def _bigram_gate(query: str, hits: list[external.ExternalHit]) -> list[external.ExternalHit]:
    """任务最初给的口径：共享至少 1 个中文 bigram，或至少 1 个 ≥3 字符英文词。

    保留它是因为**它被证伪的过程本身是结论**：这条口径在 20 个主题上几乎不筛东西，
    却会误杀简繁差异。数字见 docstring 的对照表。
    """
    bigrams = {token for token in external._token_list(query)
               if not token.isascii() and len(token) == 2
               and not all(char in external._LEADING_FILLER_CHARS for char in token)}
    words = set(external._word_terms(query))
    if not bigrams and not words:
        return list(hits)
    kept = []
    for hit in hits:
        blob = external._hit_blob(hit)
        hit_bigrams = {token for token in external._token_list(blob)
                       if not token.isascii() and len(token) == 2}
        if bigrams & hit_bigrams or words & external._word_terms(blob):
            kept.append(hit)
    return kept


def _cross_lingual_failopen(
    query: str, hits: list[external.ExternalHit], gate,
) -> list[external.ExternalHit]:
    """门的变体：query 与命中在"有没有中文"上相反时放行（跨语言不可比）。

    量它是为了回答一个问题：跨语言放行能不能救回 `consensus algorithm` 那条中文
    正文的 `Raft`，代价是不是把 `Rust 所有权` 的小麦锈病论文放回来。
    """
    kept = gate(query, hits)
    seen = {id(hit) for hit in kept}
    query_has_cjk = bool(CJK.search(query))
    for hit in hits:
        if id(hit) in seen:
            continue
        hit_has_cjk = bool(CJK.search(external._hit_blob(hit)))
        if hit_has_cjk != query_has_cjk:
            kept.append(hit)
    return kept


def _full_run_gate(query: str, hits: list[external.ExternalHit]) -> list[external.ExternalHit]:
    """上一版：要求整个中文词块原样出现（不放行跨语言）。

    量它是为了留下"为什么从整块退回到前缀 3 字"的证据：整块判据把
    `碳中和技术路径 → 碳中和` 这类"概念 + 限定语"的查询打死了。
    """
    runs = {run for run in external._CJK_RUN_RE.findall(query) if len(run) >= 2}
    words = set(external._word_terms(query))
    if not runs and not words:
        return list(hits)
    kept = []
    for hit in hits:
        blob = external._hit_blob(hit)
        if any(run in blob for run in runs) or words & external._word_terms(blob):
            kept.append(hit)
    return kept


GATES = {
    "no_gate": lambda query, hits: list(hits),
    "bigram": _bigram_gate,
    "bigram+failopen": lambda query, hits: _cross_lingual_failopen(query, hits, _bigram_gate),
    "整块": _full_run_gate,
    "prefix3(生产)": lambda query, hits: external._relevant_hits(query, hits),
}


def variants() -> int:
    """同一份候选池上跑全部判据，出对照表。**选哪个口径由这张表决定。**"""
    _load_rules()
    rows = [json.loads(line) for line in POOL.read_text(encoding="utf-8").splitlines() if line]
    totals = {name: {"count": 0, "noise": 0, "relevant": 0, "all_relevant": 0,
                     "topics_hit": 0, "unjudged": 0, "killed": []} for name in GATES}
    for row in rows:
        topic = row["topic"]
        pools = row["pools"]
        pool = [_hit(spec) for item in pools for spec in item["pool"]]
        limit = pools[0]["limit"] if pools else external.DEFAULT_LIMIT
        prefer = pools[0]["prefer_language"] if pools else None
        for name, gate in GATES.items():
            passed = gate(topic, pool)
            titles = [hit.title for hit in external._interleave(
                passed, limit, prefer_language=prefer)]
            stats = _stats(topic, titles)
            totals[name]["count"] += stats["count"]
            totals[name]["noise"] += stats["noise"]
            totals[name]["relevant"] += stats["relevant"]
            totals[name]["unjudged"] += stats["unjudged"]
            totals[name]["topics_hit"] += 1 if titles else 0
            totals[name]["all_relevant"] += 1 if stats["all_relevant"] else 0
            passed_titles = {hit.title for hit in passed}
            totals[name]["killed"].extend(
                (topic, hit.title) for hit in pool
                if hit.title not in passed_titles and _judged(topic, hit.title) is True)
    print("| 判据 | 返回条数 | 噪声条目 | 相关条目 | 三条全相关 | 成功率 | 误杀 |")
    print("|---|---|---|---|---|---|---|")
    for name, t in totals.items():
        print(f"| {name} | {t['count']} | {t['noise']} | {t['relevant']} "
              f"| {t['all_relevant']}/{len(rows)} | {t['topics_hit'] / len(rows):.1%} "
              f"| {len(t['killed'])} |")
    for name, t in totals.items():
        if t["killed"]:
            print(f"\n{name} 误杀：")
            for topic, title in t["killed"]:
                print(f"  - {topic} :: {title[:60]}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collect", action="store_true", help="联网抓一次，写候选池")
    parser.add_argument("--report", action="store_true", help="离线重放：生产判据 vs 无门")
    parser.add_argument("--variants", action="store_true", help="离线重放：全部候选判据对照")
    args = parser.parse_args()
    if args.collect:
        return collect()
    if args.report:
        return report()
    if args.variants:
        return variants()
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
