"""外部检索「相关性判据」的实测脚本：一次抓取，多组判据在同一份候选池上对照。

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
    python scripts/measure_external_relevance.py --rerank     # 三组：无门 / 门 / 门+rerank
    python scripts/measure_external_relevance.py --sweep      # 离线：阈值扫描（用分数缓存）
    python scripts/measure_external_relevance.py --probe-text-length  # 联网：分数对文本长度的敏感度

`--collect` 之后的一切都是离线的：改判据、改阈值、改判定表，重跑 `--report` /
`--variants` 即可，不再联网。`--rerank` / `--probe-text-length` 是例外——它们打的是
**rerank 模型**（`qwen3-rerank`），不是外部源：候选池没有变，多出来的是另一份服务
给的相关性分数。分数按主题缓存在 `measure_external_relevance.rerank.json`（带池子
摘要，池子换了自动作废），所以 `--sweep` 完全离线，`--rerank` 第二次起也不再联网。

## 实测结果（2026-11，本机 Clash Verge 代理，20 个主题，139 条候选）

同一份候选池（`measure_external_relevance.pool.jsonl`），五种词面判据：

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

## 第三组：语义重排（2026-11，同一份候选池 + `qwen3-rerank` 的分数）

`--rerank`。三组用同一批候选、同一个 `limit=3`；第二组就是生产实现。第三组把
"过门候选按 rerank 分数降序取前 `limit` 条"接在门之后——**这就是任务里说的那个
形状**，也是唯一被认真考虑过的形状。

| 组 | 返回条数 | 噪声条目 | 相关条目 | 三条全相关 | 成功率 | 重复标题 |
|---|---|---|---|---|---|---|
| 无门 | 60 | 22 | 38 | 10/20 | 100% | 2 |
| 词面门（生产） | 49 | 10 | 39 | 12/20 | 95% | 3 |
| **词面门 + rerank** | **49** | **9** | **40** | **12/20** | **95%** | **5** |
| 词面门 + rerank → 再交替（诊断） | 49 | 10 | 39 | 12/20 | 95% | 5 |

耗时（20 个主题，秒）：外部检索中位 **4.20** / P90 **5.71**；rerank 中位 **0.74** /
P90 **0.97**；合计中位 **5.08** / P90 **6.60**。真正触发 rerank 的主题 14/20
（另外 6 个主题的过门候选 ≤ `limit`，按门槛不调用）。

**结论：不值得接，所以没有接。** 任务的判据是"全相关 16/20 以上、噪声 5 条以下"，
实测是 **12/20（不动）**、**9 条（−1）**——按它自己的判据，这叫"只动了一两条"。
代价是每次外部检索多 0.74 秒中位（+18%），以及**重复标题 3→5**：重排取代了
`_interleave`，同一标题来自两个源时两条都会进结果（`碳中和` 与
`注意力机制` 实测各出现一次同名重复）。这两条都不能靠"再调调"消掉。

四条读数值得单独记下来，它们解释了这个结果，也决定了下一步该往哪走：

1. **重排在最脏的地方根本没机会开口**。触发门槛是"过门候选 > `limit`"，而最脏的三个
   主题恰好过门候选分别是 3（`最终一致性`）、1（`rust 学习路线`）、0（`zzqx`）——
   一次都不触发。排序能做的只是"从池子里挑"，池子已经被词面门砍到只有 `limit` 条时，
   它连改顺序的余地都没有。
2. **16/20 是这份候选池的物理上限，不是排序没做好**。`--rerank` 会打印这条上限：
   池内**一条相关候选都没有**的主题有 4 个（`加密货币的量子套利`、
   `为什么中文大模型…`、`rust 学习路线`、`zzqx 不存在的词`）——任何排序、任何阈值、
   任何模型都救不回它们，除非换源或扩源。20 − 4 = 16。
3. **只有阈值能真的减噪声，而阈值是另一个功能**（`--sweep`，完全离线）。绝对阈值最好
   的一格是 `0.45`：45 条 / 噪声 5 / 13-20 全相关 / 成功率 **80%**（门是 95%）；
   相对阈值（`分数 ≥ α × 本主题最高分`，解决"不同 query 分数尺度不同"）最好的一格是
   `0.90×top1`：27 条 / 噪声 4 / 15/20 / 成功率 95%——**仍然够不到 16/20**，而且返回
   条数从 49 腰斩到 27。也就是说：按这套判据 + 这份池子，"全相关 16/20 且噪声 ≤5"
   这个组合在语义判据下也拿不到；最接近的一格是 15/20、4 条噪声、每条主题平均 1.4 条依据。
4. **词面门在语义阈值面前是冗余的**。`--sweep` 的 `纯语义+过滤`（不要词面门，直接按
   分数阈值筛）与 `门+重排+过滤` 在**每一个阈值上读数完全相同**（45 条 / 噪声 5 /
   13-20 / 80%）。所以真要走语义判据，那是**替换**词面门，不是叠加在它后面——
   任务里"接在门之后"的形状，从数据上看是给一个已经没活可干的判据再加一层。

## 自评：rerank 分得开"同形异义"到什么程度

拿两条最刺眼的样本来量（分数来自 `qwen3-rerank`，正文取池内的 400 字上限）：

| 主题 | 命中 | 人工判定 | 分数 |
|---|---|---|---|
| 最终一致性 | 维基`最终一致性`（分布式一致性模型） | 相关 | **0.9351** |
| 最终一致性 | 钒钨八核簇合物（单晶精修，正文"最终一致性因子为Ｒ＝0.030"） | 无关 | **0.7108** |
| 最终一致性 | 配合物[Cu2(TS)(H2O)]（同样是晶体学） | 无关 | 0.6489 |
| rust 学习路线 | 《"梳理·探究·训练教学模式"与"深度学习路线"的整合研究与实践》 | 无关 | **0.3383** |
| rust 学习路线 | 陳淑樺（台湾歌手） | 无关 | 0.1628 |

三件事：

- **它分得开，但分得不够。** 晶体学论文 0.7108 排在维基词条之后（对了），可它比
  `TCP 三次握手` 的真相关命中（`传输控制协议` 0.4738）、`Rust 所有权` 的真相关
  （0.5635）、`C++ 移动语义` 的真相关（0.6626）都**高**。既然是同一个模型、同一把
  尺子，那就意味着**没有任何绝对阈值能把它切掉而不切掉别的主题的真依据**——
  `--sweep` 里 `0.75` 那一格把噪声压到 2 条，代价是成功率 65%。这是"绝对分数不可跨
  查询比较"的直接后果，也是相对阈值存在的理由。
- **相对阈值能切掉它**（0.7108 / 0.9351 = 0.76，低于 0.90），同一条线还顺手切掉了
  `幻觉` 的戏曲论文（0.7973 / 0.8964 = 0.89）和 `Rust 所有权` 的小麦锈病
  （0.3521 / 0.6151 = 0.57）。所以"同形异义"在**同一主题内**是可分的，跨主题不可比。
- **教学模式论文这一条，rerank 是靠"绝对分低"而不是靠"看出它不一样"过关的**：
  0.3383 是那个主题的最高分（其余 8 条更低），所以排序在它身上什么也做不了——它
  就是那个主题的"第一名"。这条只能靠阈值（0.45 以上一律丢掉）或干脆承认
  `rust 学习路线` 这类短材料退化查询本来就搜不到东西。

**分数与批次无关**（实测：同一条候选单独打分与混在 8 条里打分，到 6 位小数完全一致），
所以本脚本"整池打分"与生产"只打门后那几条"是同一件事，上面的数字可以直接搬到实现里。

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
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.knowledge import external, rerank  # noqa: E402

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

# ---------------------------------------------------------------- 第三组：语义重排
#
# 这一组与上面五组有一处口径差别必须写在最前面：**它要联网**（打的是 rerank 模型
# 接口，不是外部源）。池子仍然是同一份 `pool.jsonl`，所以"候选完全一样"这一条成立，
# 只是多了一份来自另一个服务的分数。
#
# 分数缓存（`measure_external_relevance.rerank.json`）按主题存 `[分数...]`，与池子里
# 候选的**顺序**一一对应，并带一个池子摘要：池子换了就自动作废重打，不会拿旧分数
# 去解释新候选。缓存让"改判据不改分数"成为可能——与池子让"改判据不联网"是同一件事。

# 送进重排的文本：`标题 + 正文前 800 字`。这是任务里给的那个形状，也是"如果接"时
# 唯一说得通的口径——800 而不是全文：rerank 对长文会稀释（一条 8000 字正文里只要开头
# 200 字相关、后面全是无关内容，分数被平均掉），而 `title` 是这条命中身份的浓缩。
RERANK_TEXT_CHARS = 800

# 池子每条正文只留 400 字（`POOL_TEXT_CHARS`，理由见那里），所以这一组实际送进重排的
# 正文最多 400 字，比那个 800 字的口径少一半——**这是本组唯一与设想口径不一致的地方**。
# 收窄它的办法是量一次"文本变短，分数会不会变"（`--probe-text-length`）：63/139 条候选
# 的原文超过 800 字，会受影响；两条最刺眼的样本（晶体学 200 字、教学模式 265 字）
# 本身短于 400，根本不会被截断。
RERANK_CACHE = Path(__file__).with_name("measure_external_relevance.rerank.json")


def _pool_digest(specs: list[dict[str, Any]]) -> str:
    """候选池的摘要：源、标题、URL 三者按顺序拼。用来判断缓存是否还配得上这批候选。"""
    raw = "\n".join(f"{s['source_name']}|{s['title']}|{s['url']}" for s in specs)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _rerank_documents(specs: list[dict[str, Any]]) -> list[str]:
    return [f"{spec['title']}\n{spec['text'][:RERANK_TEXT_CHARS]}" for spec in specs]


def _cache_read() -> dict[str, Any]:
    if not RERANK_CACHE.exists():
        return {"model": None, "topics": {}}
    try:
        payload = json.loads(RERANK_CACHE.read_text(encoding="utf-8"))
    except ValueError:
        return {"model": None, "topics": {}}
    if not isinstance(payload, dict) or not isinstance(payload.get("topics"), dict):
        return {"model": None, "topics": {}}
    return payload


def _cache_write(cache: dict[str, Any]) -> None:
    RERANK_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1) + "\n",
                            encoding="utf-8")


def _rerank_scores(
    topic: str, specs: list[dict[str, Any]], *, cache: dict[str, Any], refresh: bool,
) -> tuple[list[float], float, bool]:
    """一个主题的候选分数：`(分数, 耗时秒, 是否命中缓存)`。分数与 `specs` 等长同序。"""
    digest = _pool_digest(specs)
    entry = cache["topics"].get(topic)
    if (not refresh and isinstance(entry, dict) and entry.get("digest") == digest
            and isinstance(entry.get("scores"), list) and len(entry["scores"]) == len(specs)):
        return [float(value) for value in entry["scores"]], float(entry.get("seconds") or 0.0), True

    started = time.perf_counter()
    scores = rerank.score_documents(topic, _rerank_documents(specs))
    elapsed = time.perf_counter() - started
    cache["model"] = rerank.model_name()
    cache["topics"][topic] = {"digest": digest, "scores": scores,
                              "seconds": round(elapsed, 3), "documents": len(specs)}
    _cache_write(cache)
    return scores, elapsed, False


def _ranked(gated: list[external.ExternalHit], scores: dict[int, float],
            limit: int) -> list[external.ExternalHit]:
    """按分数降序取前 `limit` 条。稳定排序：同分保持上游（源站）顺序。

    这是任务给的形状：**重排取代 `_interleave`**，不再保源多样性。所以这一组除了
    "噪声 / 三条全相关"之外还要看**重复标题**——同一标题来自两个源时，两条都会进结果，
    而 `_interleave` 的轮流取名额天然把它们摊开。`gate+rerank→interleave` 那一列是把
    两种做法都算出来的诊断：先按分数排，再让 `_interleave` 轮流取。
    """
    ordered = sorted(gated, key=lambda hit: -scores[id(hit)])
    return ordered[:limit]


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


def rerank_report(*, refresh: bool = False) -> int:
    """同一份候选池上的三组：无门 / 词面门 / 词面门 + rerank。"""
    _load_rules()
    rows = [json.loads(line) for line in POOL.read_text(encoding="utf-8").splitlines() if line]
    cache = _cache_read()
    groups = ("no_gate", "gate", "gate+rerank", "gate+rerank→interleave")
    totals = {name: {"count": 0, "noise": 0, "relevant": 0, "all_relevant": 0,
                     "topics_hit": 0, "unjudged": 0, "duplicates": 0} for name in groups}
    external_seconds: list[float] = []
    rerank_seconds: list[float] = []
    total_seconds: list[float] = []
    separability: list[dict[str, Any]] = []
    rerank_calls = 0

    for row in rows:
        topic = row["topic"]
        pools = row["pools"]
        specs = [spec for item in pools for spec in item["pool"]]
        pool = [_hit(spec) for spec in specs]
        limit = pools[0]["limit"] if pools else external.DEFAULT_LIMIT
        prefer = pools[0]["prefer_language"] if pools else None
        scores, seconds, cached = _rerank_scores(topic, specs, cache=cache, refresh=refresh)
        by_id = {id(hit): index for index, hit in enumerate(pool)}

        gated = external._relevant_hits(topic, pool)
        reranked = _ranked(gated, {id(hit): scores[index] for index, hit in enumerate(pool)}, limit)
        # 诊断列：先按分数排，再交给 `_interleave` 轮流取（保源多样性，代价是噪声回补）
        scored_order = sorted(gated, key=lambda hit: -scores[by_id[id(hit)]])
        rerank_then_interleave = external._interleave(
            scored_order, limit, prefer_language=prefer)

        called = len(gated) > limit
        if called:
            rerank_calls += 1
        entry: dict[str, Any] = {"topic": topic, "kind": row["kind"],
                                 "seconds_external": row["seconds"],
                                 "seconds_rerank": round(seconds, 3),
                                 "rerank_cached": cached, "rerank_called": called,
                                 "candidates": len(pool), "gated": len(gated)}
        for name, hits in (("no_gate", external._interleave(pool, limit, prefer_language=prefer)),
                           ("gate", external._interleave(gated, limit, prefer_language=prefer)),
                           ("gate+rerank", reranked),
                           ("gate+rerank→interleave", rerank_then_interleave)):
            titles = [hit.title for hit in hits]
            stats = _stats(topic, titles)
            entry[name] = {"titles": titles, "scores": [round(scores[by_id[id(hit)]], 4) for hit in hits],
                           **stats}
            totals[name]["count"] += stats["count"]
            totals[name]["noise"] += stats["noise"]
            totals[name]["relevant"] += stats["relevant"]
            totals[name]["unjudged"] += stats["unjudged"]
            totals[name]["duplicates"] += len(titles) - len(set(titles))
            totals[name]["topics_hit"] += 1 if titles else 0
            totals[name]["all_relevant"] += 1 if stats["all_relevant"] else 0

        entry["dropped_by_rerank"] = sorted({hit.title for hit in gated} - {hit.title for hit in reranked})
        # 区分度：过门之后，"判定为相关"的最低分与"判定为噪声"的最高分谁高。
        # `min_relevant > max_noise` 说明这个主题上**存在一个阈值**能把两者分开。
        relevant_scores = [scores[by_id[id(hit)]] for hit in gated if _judged(topic, hit.title)]
        noise_scores = [scores[by_id[id(hit)]] for hit in gated if _judged(topic, hit.title) is False]
        entry["min_relevant_score"] = round(min(relevant_scores), 4) if relevant_scores else None
        entry["max_noise_score"] = round(max(noise_scores), 4) if noise_scores else None
        separability.append({
            "topic": topic,
            "min_relevant": min(relevant_scores) if relevant_scores else None,
            "max_noise": max(noise_scores) if noise_scores else None,
            "separable": bool(relevant_scores and noise_scores
                              and min(relevant_scores) > max(noise_scores)),
        })

        external_seconds.append(float(row["seconds"]))
        rerank_seconds.append(seconds if called else 0.0)
        total_seconds.append(float(row["seconds"]) + (seconds if called else 0.0))
        print(json.dumps(entry, ensure_ascii=False), flush=True)

    print("\n| 组 | 返回条数 | 噪声条目 | 相关条目 | 三条全相关 | 成功率 | 重复标题 |")
    print("|---|---|---|---|---|---|---|")
    for name in groups:
        t = totals[name]
        print(f"| {name} | {t['count']} | {t['noise']} | {t['relevant']} "
              f"| {t['all_relevant']}/{len(rows)} "
              f"| {t['topics_hit'] / len(rows):.1%} | {t['duplicates']} |")

    print("\n耗时（秒，20 个主题）：")
    print("| | 中位 | P90 | 均值 | 最大 |")
    print("|---|---|---|---|---|")
    for label, values in (("外部检索（实测 collect）", external_seconds),
                          ("其中 rerank", rerank_seconds),
                          ("合计", total_seconds)):
        print(f"| {label} | {_pct(values, 0.5):.2f} | {_pct(values, 0.9):.2f} "
              f"| {sum(values) / len(values):.2f} | {max(values):.2f} |")
    print(f"\n真正触发 rerank 的主题：{rerank_calls}/{len(rows)}"
          "（门槛是「过门候选数 > limit」）")

    print("\n区分度（过门池内，人工判定相关的最低分 vs 噪声的最高分）：")
    overlapping = [item for item in separability if not item["separable"]]
    for item in separability:
        if item["min_relevant"] is None or item["max_noise"] is None:
            continue
        flag = "可分" if item["separable"] else "重叠"
        print(f"  {flag}  {item['topic']}: 相关最低 {item['min_relevant']:.3f}"
              f" / 噪声最高 {item['max_noise']:.3f}")
    print(f"\n存在一个阈值能分开（min相关 > max噪声）的主题："
          f"{len(separability) - len(overlapping)}/{len([s for s in separability if s['min_relevant'] is not None and s['max_noise'] is not None])}")

    # 召回上限：排序只能从池子里挑，"全相关"要求返回的**每一条**都相关，所以只要池内
    # 有 1 条相关，这个主题就还有救；池内一条相关都没有的主题，任何排序都救不回来。
    # 把这条上限算出来，才知道 16/20 这个门槛是"排序不行"还是"池子里没有"。
    ceiling = 0
    empty: list[str] = []
    for row in rows:
        topic = row["topic"]
        titles = [spec["title"] for item in row["pools"] for spec in item["pool"]]
        if any(_judged(topic, title) is True for title in titles):
            ceiling += 1
        else:
            empty.append(topic)
    print(f"\n三条全相关的上限（池内至少有 1 条相关候选的主题）：{ceiling}/{len(rows)}")
    print(f"  池内一条相关都没有（任何排序都救不回来）：{'、'.join(empty)}")
    return 0


# 阈值扫描的网格。它不是"调参找最优"，而是回答一个是非题：**有没有任何一个绝对
# 分数阈值**能把噪声压到 5 条以下而不把成功率打穿。范围覆盖实测分数的实际分布
# （噪声最高 0.83、真相关最低 0.47–0.57），所以"扫完没有"这个结论是可信的。
THRESHOLDS = (0.2, 0.3, 0.35, 0.4, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75)

# 相对阈值：`分数 >= 比例 × 本主题最高分`。绝对阈值有一个天然缺陷——模型对不同
# query 的分数尺度不一样（`最终一致性` 的相关命中 0.94、`TCP 三次握手` 的只有 0.47），
# 一条线横着切必然在一个主题上太松、在另一个上太紧。相对阈值让每个主题自己定尺度。
RATIOS = (0.70, 0.75, 0.80, 0.85, 0.90, 0.95)


def _keep_relative(candidates, scores: dict[int, float], ratio: float) -> list:
    if not candidates:
        return []
    top = max(scores[id(hit)] for hit in candidates)
    return [hit for hit in candidates if scores[id(hit)] >= ratio * top]


def sweep_report() -> int:
    """三种"语义判据"在不同阈值下的成绩：`门+重排+过滤` / `纯语义+过滤` / `相对阈值`。

    为什么要扫阈值：重排只改顺序，不改"谁在里面"。这条链上真正能减噪声的动作只有
    **丢掉低分候选**，而丢不丢得对，完全取决于分数有没有一条能把相关与噪声分开的线。
    把这条线扫出来，才知道"接重排"该接成什么样（或者根本不该接）。
    """
    _load_rules()
    rows = [json.loads(line) for line in POOL.read_text(encoding="utf-8").splitlines() if line]
    cache = _cache_read()
    modes = ("门+重排+过滤", "纯语义+过滤", "门+相对阈值")
    cuts: dict[str, tuple[float, ...]] = {"门+重排+过滤": THRESHOLDS,
                                          "纯语义+过滤": THRESHOLDS,
                                          "门+相对阈值": RATIOS}
    totals = {(mode, cut): {"count": 0, "noise": 0, "relevant": 0, "all_relevant": 0,
                            "topics_hit": 0, "duplicates": 0}
              for mode in modes for cut in cuts[mode]}
    for row in rows:
        topic = row["topic"]
        pools = row["pools"]
        specs = [spec for item in pools for spec in item["pool"]]
        pool = [_hit(spec) for spec in specs]
        limit = pools[0]["limit"] if pools else external.DEFAULT_LIMIT
        scores, _seconds, _cached = _rerank_scores(topic, specs, cache=cache, refresh=False)
        gated = external._relevant_hits(topic, pool)
        by_id = {id(hit): index for index, hit in enumerate(pool)}
        all_scores = {id(hit): scores[by_id[id(hit)]] for hit in pool}
        for mode in modes:
            for cut in cuts[mode]:
                if mode == "门+相对阈值":
                    kept = _keep_relative(gated, all_scores, cut)
                else:
                    candidates = gated if mode == "门+重排+过滤" else pool
                    kept = [hit for hit in candidates if all_scores[id(hit)] >= cut]
                hits = _ranked(kept, all_scores, limit)
                titles = [hit.title for hit in hits]
                stats = _stats(topic, titles)
                bucket = totals[(mode, cut)]
                bucket["count"] += stats["count"]
                bucket["noise"] += stats["noise"]
                bucket["relevant"] += stats["relevant"]
                bucket["duplicates"] += len(titles) - len(set(titles))
                bucket["topics_hit"] += 1 if titles else 0
                bucket["all_relevant"] += 1 if stats["all_relevant"] else 0
    print("| 方式 | 阈值 | 返回条数 | 噪声条目 | 相关条目 | 三条全相关 | 成功率 | 重复标题 |")
    print("|---|---|---|---|---|---|---|---|")
    for mode in modes:
        for cut in cuts[mode]:
            t = totals[(mode, cut)]
            label = f"{cut:.2f}" if mode != "门+相对阈值" else f"{cut:.2f}×top1"
            print(f"| {mode} | {label} | {t['count']} | {t['noise']} | {t['relevant']} "
                  f"| {t['all_relevant']}/{len(rows)} | {t['topics_hit'] / len(rows):.1%} "
                  f"| {t['duplicates']} |")
    return 0


# 400 → 800 的探针：池子里每条正文只留 400 字，而生产送 800 字。这一组回答的是
# "少了那 400 字，分数会不会变"。挑的是最刺眼的两条噪声——它们的分数决定了整件事的
# 结论，所以必须验一下这个偏差有没有把结论带歪。
PROBE_SAMPLES: dict[str, list[str]] = {
    "最终一致性": ["八核簇合物", "配合物[Cu2"],
    "rust 学习路线": ["梳理·探究", "陳淑樺"],
}


def probe_text_length() -> int:
    """同一条候选、同一条 query，"送 200 字"与"送 400 字"的分数差。

    池子里每条正文只有 400 字（`POOL_TEXT_CHARS`），所以这个探针量的是**文本长度
    敏感性**，不是"400 与 800 的差"：它说明这条候选的分数会不会因为上下文变短而
    漂移，从而说明"池子截断"有没有把结论带歪。真要量 800 字得重新抓一次外部源，
    那会换掉整份候选池——代价远大于它能回答的问题。
    """
    cache = _cache_read()
    rows = [json.loads(line) for line in POOL.read_text(encoding="utf-8").splitlines() if line]
    for row in rows:
        needles = PROBE_SAMPLES.get(row["topic"])
        if not needles:
            continue
        specs = [spec for item in row["pools"] for spec in item["pool"]]
        scores, _seconds, _cached = _rerank_scores(row["topic"], specs, cache=cache, refresh=False)
        documents = _rerank_documents(specs)
        short = rerank.score_documents(
            row["topic"], [doc[: len(spec["title"]) + 200] for doc, spec in zip(documents, specs)])
        print(f"\n{row['topic']}（标题 + 200 字 vs 标题 + 400 字）")
        for index, spec in enumerate(specs):
            if not any(needle in spec["title"] for needle in needles):
                continue
            print(f"  {spec['title'][:44]:46s} 原始长度 {spec['raw_chars']:5d} "
                  f"| 400字 {scores[index]:.4f} | 200字 {short[index]:.4f}")
    return 0


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
    parser.add_argument("--rerank", action="store_true",
                        help="三组对照：无门 / 词面门 / 词面门 + rerank（要联网，分数有缓存）")
    parser.add_argument("--sweep", action="store_true",
                        help="绝对阈值扫描：门+重排+过滤 / 纯语义+过滤（离线，用分数缓存）")
    parser.add_argument("--probe-text-length", action="store_true",
                        help="同一条候选在 200 字与 400 字正文下的分数（要联网）")
    parser.add_argument("--refresh-scores", action="store_true",
                        help="忽略分数缓存，重新打一遍 rerank")
    args = parser.parse_args()
    if args.collect:
        return collect()
    if args.report:
        return report()
    if args.variants:
        return variants()
    if args.rerank:
        return rerank_report(refresh=args.refresh_scores)
    if args.sweep:
        return sweep_report()
    if args.probe_text_length:
        return probe_text_length()
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
