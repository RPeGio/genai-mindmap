"""量「NLI 喂给模型的东西对不对」：旧 claim（节点标题） vs 新 claim（label + detail）。

这一层的判别力全在 hypothesis 上。拿"预算审批流程"这种名词短语当 hypothesis，模型
能回答的只有"这段引文提到这个短语了吗"——一段话几乎不可能**否定**一个名词短语的
存在，它顶多不提它。`Node.detail` 才是可判真假的论断，所以 `annotate_nli` 传的是
`_claim_of(node)`（`label` + `detail`），不是 `node.label`。

本脚本回答"改完到底有用吗"，三组：

  A 忠实论断 20 条 —— 论断与引文同向（真实生成里占绝大多数）
  B 反向论断 10 条 —— 论断与引文相反（这一层唯一声称能抓的故障）
  C 真实引用（`--refs`，可选）—— 真分片生成出来的 (label, quote)，`detail` 为空

A/B 的 `quote` 全部逐字取自 `docs/RAG-DECISIONS.md`（脚本启动时会校验）；`label`
与 `detail` 按真实生成树的形态写。C 组要先用
`scripts/measure_generation_diagnostics.py` 那套夹具导出 JSON：

    [{"label": ..., "detail": ..., "quote": ...}, ...]

没有模型就没有结论：`MINDMAP_NLI_MODEL_DIR` 未指向可用目录时，本脚本只报
"有效性无法验证"，不会编一个分布出来。

用法（在 `backend/` 下）：

    python scripts/measure_nli_claim.py --model-dir D:/models/erlangshen
    python scripts/measure_nli_claim.py --model-dir ... --refs D:/tmp/gen_refs.json
"""
from __future__ import annotations

import argparse
import json
import os
import statistics as st
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.contracts.mindmap import Node  # noqa: E402
from app.optimization import nli  # noqa: E402
from app.optimization.source_refs import _claim_of  # noqa: E402

LABELS = ("entailment", "neutral", "contradiction")
MATERIAL = BACKEND.parent / "docs" / "RAG-DECISIONS.md"

FAITHFUL = [
    dict(label="递归切块",
         detail="切块改成五级降级，依次退让到次级标点，参数是 TARGET 450、MAX 700、MIN 150、OVERLAP 100。重叠起点吸附到句首而不是定长回退，吸附不到就不重叠。",
         quote="参数：`TARGET=450 / MAX=700 / MIN=150 / OVERLAP=100`。重叠起点吸附到**句首**而非定长回退；吸附不到就不重叠，绝不从半句中间起步。"),
    dict(label="分隔符顺序",
         detail="把句末标点提到单换行之前以后，英文 PDF 的块尾句末率从 20% 提高到 79.5%。原因是 PDF 提取文本几乎每行一个换行，那是版面折行不是句子边界。",
         quote="**改分隔符顺序的效果**：把句末标点提到单换行之前，英文 PDF 的块尾句末率从 **20% → 79.5%**，块尾样本从 `'the Transformer,\\n'` 变成 `'for over 2 BLEU. '`。原因是 PDF 提取文本几乎每行一个 `\\n`，那是版面折行不是句子边界。"),
    dict(label="中文单换行占比",
         detail="中文材料里单换行这一级占比低不是缺陷。Docling 每段之间输出两个换行，空行优先于句末标点是正确的，段落边界比句子边界更强。",
         quote="**中文 L4 占比低不是缺陷**：Docling 每段之间输出 `\\n\\n`，而空行（L2）优先于句末标点（L3）是正确的——段落边界比句子边界更强。中文切点 75%–93% 来自结构层，这是设计意图。"),
    dict(label="硬切次数",
         detail="四份材料的硬切触发次数全为 0，窗口命中率 100%。所谓无标点长段只能硬切，在这批材料上没有发生。",
         quote="**硬切的真实数量是 0**。四份材料的 `_hard_cut` 触发次数全为 0，窗口命中率 100%。所谓\"无标点长段只能硬切\"在这批材料上没有发生。"),
    dict(label="词面检索盲区",
         detail="查询「谁能批钱」与原文的「审批权限」「财务经理核准」一个字都不重合，纯 BM25 是零召回。这是词面检索的结构性盲区，不是调参能补的。",
         quote="「谁能批钱」与原文的「审批权限」「财务经理核准」一个字都不重合，BM25 是零召回——这是词面检索的结构性盲区，不是调参能补的。"),
    dict(label="混合检索延迟",
         detail="embedding 单次往返冷启动 1.3 秒，检索本身是毫秒级。缓存命中以后第二次查询 0 次 HTTP。",
         quote="延迟：embedding 单次往返 1.3 秒（冷），检索本身（BM25 + 1024 维余弦）毫秒级。缓存命中后第二次查询 0 次 HTTP。"),
    dict(label="多轮判断的浪费",
         detail="那组多轮实验合计 127 次 LLM 调用、75 次检索，其中 17 个轮次是纯浪费。",
         quote="合计 127 次 LLM 调用、75 次检索，其中 17 个轮次是纯浪费。"),
    dict(label="判断器失效",
         detail="判断器不是噪声源，是系统性失效。同一 query 用 temperature=0 跑 3 次、再按默认温度跑 3 次，6 次全部输出 INSUFFICIENT，投票救不回来。",
         quote="**判断器不是噪声源，是系统性失效**。同一 query 跑 3 次（temperature=0）、再跑 3 次（默认温度），6 次全部 `INSUFFICIENT`，输出只有一种。投票救不回来——它不是在犹豫，是确定地在说\"这些片段不足以回答\"，而这正是循环继续的唯一燃料。"),
    dict(label="改写器不看材料",
         detail="改写器完全不看材料。材料是预算审批，它把 query 改成引入 T1、T2、DOI、arXiv、超导、离子阱，与材料的 2-gram 覆盖率恒为 0.0。",
         quote="**改写器完全不看材料**。材料是预算审批，它把 query 改成引入 `T₁/T₂*`、DOI/arXiv、超导/离子阱；另一组引入《中央企业合规管理办法》。与材料的 2-gram 覆盖率**恒为 0.0**。"),
    dict(label="多轮的最小硬约束",
         detail="将来真要开多轮，最小硬约束是 MAX_ROUNDS=2，20 轮降到 2 轮省 95% token、97% 耗时，答案质量无差。改写器必须喂材料上下文。",
         quote="如果将来真要开多轮，最小硬约束是：`MAX_ROUNDS=2`（20→2 轮省 95% token、97% 耗时，答案质量无差）、无进展判据用「新增区间数 = 0」而非重合率阈值、改写器必须喂材料上下文、`rewrite()` 挪到 `MAX` 判定之后（实测 1/3 的改写调用从未被检索使用）、单次 3000 prompt token 硬顶。"),
    dict(label="HyDE 的取舍",
         detail="HyDE 不做。它多花 800ms，是四方案里延迟最高的，收益只有 2 到 8 点；而向量检索本身已经在桥接语义鸿沟。",
         quote="| HyDE | **不做** | +800ms（四方案里延迟最高），收益 +2~8 点；而向量检索本身已在桥接语义鸿沟，边际收益被抵消大半 |"),
    dict(label="父子切块默认关",
         detail="父子切块已经实现但默认关闭，担心的是引用链失真：score 来自 200 字的子块，quote 却是 450 到 700 字的父块，模型从父块里挑一句与查询无关的话也照样通过逐字校验。",
         quote="| 父子切块 | **已实现、默认关、未接入**（`search_parent` + `MINDMAP_PARENT_RETRIEVAL=0`） | 当前的顾虑是父子切块会让引用链失真：`score` 来自 200 字的子块，`quote` 却是 450–700 字的父块，模型从父块里挑一句与查询无关的话也照样通过逐字校验，`verify_source_refs` 就从「证据核验」退化成「格式检查」。"),
    dict(label="93 分与 59 分",
         detail="全塞入对切片检索那组实测里，64 份文档全塞进上下文是 93 分，切成 1583 段做检索 top5 只有 59 分。它只适用于定位型检索。",
         quote="「全塞入 vs 切片检索」那组一手实测（64 份文档全塞进上下文 **93 分**，切成 1583 段做检索 top5 只有 **59 分**）**只适用于定位型检索**——材料里已有答案、检索帮模型找到它。"),
    dict(label="三种检索形态",
         detail="定位型、扩展型、循环型是三件事，不能混着谈。已有的 rag_query 是定位型，申报书要的是扩展型，多轮实验证伪的是循环型。",
         quote="我们已有的 `rag_query` 是定位型；申报书 3.3.1（\"输入碳中和，自动关联政策、技术路径、企业案例\"）要的是扩展型；2.3 那个实验证伪的是循环型。"),
    dict(label="结构引导的检索次数",
         detail="检索次数由结构决定，8 个分支就是 8 次，不由模型临场判断。没有该不该停这个问题需要回答，因为它天然有界。",
         quote="**为什么是它**：检索次数由结构决定（8 个分支就是 8 次），不由模型临场判断——没有\"该不该停\"这个问题需要回答，因为它天然有界。"),
    dict(label="引用按支回填",
         detail="引用按支回填：某支的节点只能引用该支检索给它的证据。",
         quote="3. 引用按支回填：某支的节点只能引用该支检索给它的证据"),
    dict(label="Neo4j 内存占用",
         detail="Neo4j 用 256m 堆加 256m pagecache，常驻 488 MB，两跳中位 42 ms。",
         quote="| **heap 256m / pagecache 256m** | 无 | ✅ 13.3s | **488 MB** | 42 ms |"),
    dict(label="唯一失败的配置",
         detail="唯一失败的是 heap 1g 配 1G 硬限：默认的 AlwaysPreTouch 会在启动瞬间提交整堆，1G 限额必死，报页面文件太小 errno 1455。",
         quote="最后一行是唯一的失败：`-Xms1g` 配上默认的 `AlwaysPreTouch`，启动瞬间提交整堆，1G 限额必死（`页面文件太小 errno 1455`，正好 1 GiB）。"),
    dict(label="混合检索的未验证项",
         detail="混合检索是否比纯 BM25 更准，尚无真机对照。已经证明的是零召回时能捞回，排序质量还没有数据。",
         quote="**混合检索是否比纯 BM25 更准，尚无真机对照**——已证明的是\"零召回时能捞回\"，排序质量还没有数据"),
    dict(label="搜索预算",
         detail="搜索内容要独立设量，最多占 30% 预算，否则会挤掉用户材料。冲突时不能替用户选。",
         quote="三条风险：搜索内容要独立设量（最多 30% 预算，否则挤掉用户材料）、`retrieved_at` 必记（否则三个月后用户以为是现行）、**冲突时不能替用户选**（材料说 A、网上说 B，两个都呈现并标注冲突）。"),
]

# 控制组：引文照抄原文，detail 写成与原文**相反**的方向。真实生成里这就是
# "引文逐字对得上、论断把方向写反"——这一层唯一声称能抓的故障。
CONTRA = [
    dict(label="词面检索盲区",
         detail="查询「谁能批钱」在纯 BM25 与混合检索下都能召回 5 条，词面重合度很高，说明调参可以补齐这个词面差距。",
         quote=FAITHFUL[4]["quote"]),
    dict(label="多轮判断",
         detail="多轮判断的循环在第 1 轮就收敛，完全无关的材料也只用了 605 token，没有浪费。",
         quote=FAITHFUL[6]["quote"]),
    dict(label="HyDE 的取舍",
         detail="HyDE 是四方案里延迟最低的，只多花 8ms，收益有 20 到 30 点，所以已经采纳。",
         quote=FAITHFUL[10]["quote"]),
    dict(label="硬切次数",
         detail="四份材料都出现了大量硬切，窗口命中率只有 30%。",
         quote=FAITHFUL[3]["quote"]),
    dict(label="中文切块",
         detail="中文材料的切点主要来自句末标点，结构层没有起作用，这是缺陷。",
         quote=FAITHFUL[2]["quote"]),
    dict(label="分隔符顺序",
         detail="把句末标点提到单换行之前以后，英文 PDF 的块尾句末率从 79.5% 降到 20%。",
         quote=FAITHFUL[1]["quote"]),
    dict(label="父子切块",
         detail="父子切块已经默认开启并接入生成流程，引用链不会失真，可以直接用。",
         quote=FAITHFUL[11]["quote"]),
    dict(label="全塞入与切片检索",
         detail="这组实测证明切片检索优于全塞入，所以扩展型检索也该切片。",
         quote=FAITHFUL[12]["quote"]),
    dict(label="Neo4j 内存占用",
         detail="2C2G 装不下 Neo4j，heap 256m 也会启动即死，所以只能换掉它。",
         quote="**所以 2C2G 跑得起来。** 256m 堆常驻约 490MB，2G 总账：OS 200~300 + Neo4j 490~530 + Python 后端 600~1000 + nginx 20 = 1.31~1.85 GB，余量 400~600 MB。共存实测也过了（Neo4j 234MB + Python 900MB 在 2G 内，查询仍正常）。"),
    dict(label="结构引导的检索次数",
         detail="检索次数由模型临场判断，没有上界，所以这条路线会发散。",
         quote=FAITHFUL[15]["quote"]),
]


def check_quotes() -> int:
    """每一条 `quote` 都必须逐字出现在材料里——手抄错一个字符，整张表就不可信。"""
    if not MATERIAL.exists():
        print(f"（材料不存在：{MATERIAL}，跳过逐字校验）")
        return 0
    text = MATERIAL.read_text(encoding="utf-8")
    missing = [row["label"] for row in FAITHFUL + CONTRA if text.find(row["quote"]) < 0]
    if missing:
        print(f"[warn] 这些 quote 在材料里定位不到：{missing}")
    return len(FAITHFUL) + len(CONTRA) - len(missing)


def judge_group(rows: list[dict], *, use_detail: bool, repeats: int) -> dict:
    counts = dict.fromkeys(LABELS, 0)
    times: list[float] = []
    verdicts: list[dict] = []
    for row in rows:
        node = Node(id="n1", label=row["label"], detail=row.get("detail") or None)
        claim = _claim_of(node) if use_detail else row["label"]
        samples = []
        result = None
        for _ in range(repeats):
            started = time.perf_counter()
            result = nli.judge(claim, row["quote"])
            samples.append((time.perf_counter() - started) * 1000)
        times.append(st.median(samples))
        if not result:
            counts["neutral"] += 1
            continue
        counts[result["label"]] = counts.get(result["label"], 0) + 1
        verdicts.append({"label": row["label"], "claim": claim,
                         "verdict": result["label"], "score": result["score"],
                         "probs": result["probs"]})
    return {"counts": counts, "times": times, "verdicts": verdicts}


def report(name: str, rows: list[dict], *, repeats: int) -> dict:
    old = judge_group(rows, use_detail=False, repeats=repeats)
    new = judge_group(rows, use_detail=True, repeats=repeats)
    total = len(rows) or 1
    out = {}
    for tag, stats in (("旧 claim（label）", old), ("新 claim（label+detail）", new)):
        counts = stats["counts"]
        line = "  ".join(f"{key[:4]}={counts[key]:2d}({counts[key] / total:4.0%})"
                         for key in LABELS)
        print(f"  {tag:24s} {line}   单条 {st.median(stats['times']):5.1f} ms")
        out[tag] = {"counts": dict(counts),
                    "ms_median": round(st.median(stats["times"]), 1)}
    out["verdicts"] = new["verdicts"]
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", default=os.getenv("MINDMAP_NLI_MODEL_DIR", ""))
    parser.add_argument("--refs", default="",
                        help="真实引用的 JSON（[{label, detail, quote}, ...]），可选")
    parser.add_argument("--repeats", type=int, default=3,
                        help="每条重复几次取耗时中位（int8 推理的墙钟抖动很大）")
    args = parser.parse_args()

    print(f"逐字校验：{check_quotes()}/{len(FAITHFUL) + len(CONTRA)} 条 quote 命中材料")
    if not args.model_dir:
        print("未给 --model-dir：**有效性无法在本机验证**（没有模型权重就没有结论）。")
        return 2
    os.environ["MINDMAP_NLI_ENABLED"] = "1"
    os.environ["MINDMAP_NLI_MODEL_DIR"] = args.model_dir
    nli.reset()
    started = time.perf_counter()
    if not nli.available():
        print(f"模型不可用（{nli.info()['reason']}）：有效性无法在本机验证。")
        return 2
    load = time.perf_counter() - started
    model_file = next(Path(args.model_dir).glob("*.onnx"), None)
    size = model_file.stat().st_size / 1048576 if model_file else 0.0
    print(f"模型 {model_file.name if model_file else '?'} {size:.1f} MB；"
          f"首次加载 {load:.2f} s；claim 上限 {nli.MAX_CLAIM_CHARS} 字符\n")

    result = {"model": args.model_dir, "size_mb": round(size, 1),
              "load_s": round(load, 3), "groups": {}}
    for title, rows in (("A 忠实论断 20 条", FAITHFUL), ("B 反向论断 10 条", CONTRA)):
        print(f"--- {title}")
        result["groups"][title] = report(title, rows, repeats=args.repeats)
    if args.refs:
        rows = json.loads(Path(args.refs).read_text(encoding="utf-8"))
        print(f"--- C 真实引用 {len(rows)} 条（detail 空，旧 = 新）")
        result["groups"]["C 真实引用"] = report("C", rows, repeats=args.repeats)
    print("\n注意：A/B 的 label 与 detail 是按真实生成树形态人工写的，quote 逐字来自材料；"
          "C 组来自脚本化分片生成（本机没有 MINDMAP_LLM_* 密钥，模型是脚本）。"
          "这三组量的是「claim 该喂什么」，不是「这张图的引用对不对」。")
    nli.reset()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
