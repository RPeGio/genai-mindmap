"""可选层：NLI 语义蕴含打分。

前两层是机械判据——字段合法、`quote` 逐字出现在来源原文里。它们管不住
一种情况：引文逐字对得上，但节点论断与原文**语义相反**（原文说"只在
小数据集上验证"，节点写成"已在大规模数据上验证"）。确定性判据在纯文本
层面看不见这类问题。

**权威口径（重要）**：这一层只产出附加评分，**不改变任何 source_ref**。
引用存不存在由前两层决定；这一层只回答"这条已存在的引用，语义上站不站得住"。
理由：NLI 是概率模型，它自己也会错。让一个概率模型去推翻可复核的确定性
判据，等于把「诚实性来自判据的机械性，而不是来自模型的自觉」这条原则
反过来用。两者冲突时两边都保留，由人判断。

**hypothesis 是论断，不是标题**：消费方 `source_refs.annotate_nli` 传的是
`label` + `detail`（见那边的 `_claim_of`）。拿 4–10 个字的节点标题当 hypothesis
时，一段 200 字的引文根本不可能"否定"一个名词短语，判定只会落在 entailment /
neutral——真模型实测里 10 条写反的论断全被盖成 entailment。这一层的判别力有
多少，全看喂进来的 claim 是不是一句可判真假的论断。

**默认关闭**：模型 + onnxruntime 在 2C2G 上约 100 MB 峰值（权重 25.7 MB，
运行时另算）。部署机还要留给 Docling 和语音模块，所以默认不加载。
启用需要同时给两个环境变量：

    MINDMAP_NLI_ENABLED=1
    MINDMAP_NLI_MODEL_DIR=/path/to/model     # 含 model.onnx / tokenizer.json / config.json

关闭时**零开销**：连 onnxruntime 都不 import，`available()` 只读一个环境变量。

**两道开关，问的不是同一件事**（`MINDMAP_NLI_ENABLED` 与
`MINDMAP_NLI_ON_GENERATE`）：前者是"这台机器能不能跑 NLI"，后者是"每次生成
都跑一遍值不值"。生成路径是这一层唯一的消费方（`optimization` 路由那条只读
体检只跑机械判据），所以模型启用之后它默认就在生成收尾跑一次；实测成本
（24 条引用 0.37–1.07 s、40 条 0.62–1.79 s）低于"每次生成多等三秒"的界，
撞满 100 条上限时才需要 `MINDMAP_NLI_ON_GENERATE=0` 关掉。`available()` 为假
时它一次都不发生。

稳定接口：
    available() -> bool
    on_generate() -> bool                      # 这次生成要不要跑语义复核
    judge(claim, evidence) -> dict | None      # {"score": float, "label": str, "probs": {...}}
    reset() -> None                            # 测试用：丢弃已加载的模型

两条硬约束（有单测钉住）：
  · **绝不抛异常**——模型缺失、加载失败、推理出错一律返回 None / False，
    引用核验不能因为这一层把整次生成搞挂；
  · **关闭即零副作用**——开关没开时不做任何探测，也不 import 重依赖。
"""
from __future__ import annotations

import json
import os
import threading

ENABLED_ENV = "MINDMAP_NLI_ENABLED"
MODEL_DIR_ENV = "MINDMAP_NLI_MODEL_DIR"
MIN_FREE_MB_ENV = "MINDMAP_NLI_MIN_FREE_MB"
# 生成路径上的第二道闸：`ENABLED_ENV` 只回答"这台机器能不能跑 NLI"，回答不了
# "每次生成都跑一遍值不值"。
ON_GENERATE_ENV = "MINDMAP_NLI_ON_GENERATE"
# 缺省值按实测（`scripts/measure_generation_diagnostics.py`，脚本化分片生成 +
# 等价延迟）：8 支树 24 条引用 → 0.37–1.07 s；20 支（工具上限）40 条 → 0.62–1.79 s。
# 都低于 3 s，所以模型已启用时**默认就跑**——否则这个开关形同虚设，"接了"与
# "没接"在部署上无法区分。撞满 `MAX_NLI_CHECKS=100` 时是 1.5–4.4 s，那种图
# （层数更深、每节点都带引用）要省时间就把 `ON_GENERATE_ENV` 设成 0。
#
# **上面那组数字是"等价延迟"注入出来的，不是真模型**。真模型（Erlangshen int8，
# 98.3 MB）在本机实测：引文 238 字（分片生成的真实中位）单条 36–55 ms、300 字
# 55 ms、400 字以上跳到 700–1160 ms（同样的台阶在 fp32 上也出现，位置不同——
# 更像本机 CPU 线程调度的产物，不是模型固有；但只要父块级引文（450–700 字）
# 进来了就一定会踩到）。按 238 字算，24 条引用是 **约 0.9–1.3 s**、100 条是
# **3.6–5.5 s**，与上表的量级一致；踩到台阶的图上限就不可控了。
#
# **默认关**，依据是真模型实测（Erlangshen int8，98.3 MB，`scripts/measure_nli_claim.py`）：
#   - 召回：10 条人工写反的论断，claim 改成 `label + detail` 后 contradiction 概率
#     从 5–7% 抬到 16–39%，但 **top-1 只抓到 1/10**（mDeBERTa 一条没抓到）。
#     降阈值救不了——0.3 只多抓 1 条，噪声一条不少。
#   - 假 warn：24 条真实引用里 **4 条**被判 contradiction，全是 markdown 表格行
#     片段（`| 扫描件 PDF | 0 |`、`| 4 | 单换行 \n |`）。逐条报 warn 会淹掉真问题。
#   - 成本：238 字引文 36–55 ms/条，24 条 **约 10.6 s**、100 条约 44 s；引文过
#     300 字有台阶跳到 700–1160 ms，而父块级引文（450–700 字）必然踩到。
#
# 关掉不等于这层没用，而是**它现在消费错了东西**：`annotate_nli` 只读
# `contradiction`，而新 claim 真正多出来的信号是 `entailment`（mDeBERTa 上
# 50%→80%）。要救这一层该消费的是 **neutral**——"引文没支持这条论断，该补证据"，
# 那是可操作的；contradiction 人看到也只能自己去核。改 claim 是必要条件，不是
# 充分条件，所以先把默认关掉，等消费端改完再翻回来。
DEFAULT_ON_GENERATE = False

# 实测占用：英文 mobilebert-uncased-mnli（25.7 MB int8）加载后 +83.5 MB；
# 中文 Erlangshen-Roberta-110M-NLI（98.3 MB int8）加载后 +140.3 MB。
# 闸门按最大的那个留两倍余量，同时给 Docling（常驻约 350 MB）和语音模块
# （峰值约 250 MB，与 NLI 不同时发生）留出空间。
DEFAULT_MIN_FREE_MB = 300

# cross-encoder 的位置编码上限是 512 token，字符级预截断只是"别把整篇文章塞进去"。
#
# **真正生效的是 token 上限，不是这两个字符数**：`enable_truncation(max_length=512)`
# 默认按 longest_first 截——超了先截最长的那一段，而最长的那段通常是 premise
# （引文）。所以字符预截断给多了，代价不是"多算几个 token"，而是**静默丢掉引文的
# 后半段**。实测（Erlangshen，bert-base-chinese 词表）：1200 个汉字 → 1200+ token，
# 单句编码就已经撞满 512，注释里原先写的"中文约 1:1.5"是高估——实测 1 汉字 ≈ 1.05
# token（子词合并抵消了 `##` 前缀，净结果接近一字一 token）。
MAX_EVIDENCE_CHARS = 1200

# hypothesis（论断）的字符上限。这个数是从 token 预算反推的，不是拍的：
#
#     claim 额度 = 512（tokenizer 硬上限）
#                − 168（引文实测中位：238 字 → 168 token）
#                −   3（[CLS] / [SEP] / [SEP]）
#                ≈ 341 token ≈ 340 字符（中文 1 字符 ≈ 1.05 token）
#
# 实测对照（同一段 238 字引文）：claim 320 字 → 合计 440 token，安全，premise 可见
# 窗口完整；claim 423 字 → 合计 592 token，被截到 512，premise 可见窗口从 164 token
# 掉到 84 token——**证据被砍掉一半，比论断被截更糟**。所以 340 是"论断能写满三个
# 句子、又不挤占证据"的上界。上一版的 200 只够写一句半，配上原来那个"claim 是节点
# 标签"的用法（几个字）从未被撞到过，接上 `detail` 之后就必须重算。
MAX_CLAIM_CHARS = 340

_TRUTHY = {"1", "true", "yes", "on", "y", "t"}

_lock = threading.Lock()
_cache: dict[str, object] = {"tried": False, "backend": None, "reason": ""}


def _enabled() -> bool:
    return str(os.getenv(ENABLED_ENV, "")).strip().lower() in _TRUTHY


def _model_dir() -> str:
    return str(os.getenv(MODEL_DIR_ENV, "")).strip()


def _min_free_mb() -> int:
    raw = str(os.getenv(MIN_FREE_MB_ENV, "")).strip()
    if not raw:
        return DEFAULT_MIN_FREE_MB
    try:
        return max(64, int(raw))
    except ValueError:
        return DEFAULT_MIN_FREE_MB


def available_memory_mb() -> float | None:
    """当前可用内存（MB）。Linux 读 /proc/meminfo，其他平台返回 None 表示不检查。"""
    try:
        with open("/proc/meminfo", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _softmax(values: list[float]) -> list[float]:
    if not values:
        return []
    peak = max(values)
    shifted = [value - peak for value in values]
    total = sum(2.718281828459045 ** value for value in shifted)
    return [(2.718281828459045 ** value) / total for value in shifted]


def _read_labels(model_dir: str) -> list[str]:
    """标签顺序只能从 config.json 的 id2label 读——写死会在换模型时静默错位。"""
    path = os.path.join(model_dir, "config.json")
    with open(path, encoding="utf-8") as handle:
        config = json.load(handle)
    mapping = config.get("id2label") or {}
    return [str(mapping[str(index)]).lower() for index in range(len(mapping))]


def _find_model_file(model_dir: str) -> str | None:
    """优先 model.onnx；目录里只有一个 .onnx 时就用它。

    导出工具常把量化后的权重命名成 `*_quantized.onnx` 之类，逼用户改名是
    无谓的摩擦，但目录里有多个 .onnx 时必须要求显式命名——猜错加载的是
    另一个模型，标签顺序也会跟着错，而报错信息只会说"推理失败"。
    """
    preferred = os.path.join(model_dir, "model.onnx")
    if os.path.exists(preferred):
        return preferred
    try:
        candidates = [name for name in os.listdir(model_dir) if name.endswith(".onnx")]
    except OSError:
        return None
    if len(candidates) == 1:
        return os.path.join(model_dir, candidates[0])
    return None


class _OnnxBackend:
    """ONNX cross-encoder。MNLI 惯例：premise 是原文，hypothesis 是论断。"""

    def __init__(self, model_dir: str, model_file: str) -> None:
        import onnxruntime  # 延迟到真正加载时才 import
        from tokenizers import Tokenizer

        self._session = onnxruntime.InferenceSession(
            model_file, providers=["CPUExecutionProvider"])
        self._tokenizer = Tokenizer.from_file(os.path.join(model_dir, "tokenizer.json"))
        self._tokenizer.enable_truncation(max_length=512)
        self._labels = _read_labels(model_dir)
        self._inputs = {item.name for item in self._session.get_inputs()}

    def judge(self, claim: str, evidence: str) -> dict:
        import numpy as np

        encoded = self._tokenizer.encode(evidence[:MAX_EVIDENCE_CHARS],
                                         claim[:MAX_CLAIM_CHARS])
        feeds = {
            "input_ids": np.array([encoded.ids], dtype=np.int64),
            "attention_mask": np.array([encoded.attention_mask], dtype=np.int64),
        }
        if "token_type_ids" in self._inputs:
            feeds["token_type_ids"] = np.array([encoded.type_ids], dtype=np.int64)

        logits = self._session.run(None, feeds)[0][0]
        probs = _softmax([float(value) for value in logits])
        best = max(range(len(probs)), key=lambda index: probs[index])
        label = self._labels[best] if best < len(self._labels) else "unknown"
        return {
            "score": round(probs[best], 4),
            "label": label,
            "probs": {name: round(probs[index], 4)
                      for index, name in enumerate(self._labels) if index < len(probs)},
        }


def _load() -> object | None:
    """加载一次并缓存。任何失败都只记录原因，不抛异常。"""
    with _lock:
        # 关闭状态**不写缓存**：否则关闭期间只要被调过一次（judge 会走到这里），
        # tried 就被钉成 True，之后即使把开关打开也永远返回不可用。生产上环境
        # 变量不会中途改，但测试和多环境切换会踩，而且症状是"开了但不管用"。
        if not _enabled():
            _cache["reason"] = f"未开启（{ENABLED_ENV} 未设）"
            return None
        if _cache["tried"]:
            return _cache["backend"]
        _cache["tried"] = True
        model_dir = _model_dir()
        if not model_dir:
            _cache["reason"] = f"未设置 {MODEL_DIR_ENV}"
            return None
        model_file = _find_model_file(model_dir)
        if not model_file:
            _cache["reason"] = "模型目录里没有可用的 .onnx 文件"
            return None

        free = available_memory_mb()
        if free is not None and free < _min_free_mb():
            _cache["reason"] = f"可用内存 {free:.0f} MB 低于 {_min_free_mb()} MB 阈值"
            return None

        try:
            _cache["backend"] = _OnnxBackend(model_dir, model_file)
            _cache["reason"] = ""
        except Exception as exc:  # noqa: BLE001 - 加载失败等于没有这一层
            _cache["reason"] = f"加载失败：{type(exc).__name__}"
            _cache["backend"] = None
        return _cache["backend"]


def available() -> bool:
    return _load() is not None


def on_generate() -> bool:
    """这次生成要不要跑语义复核。

    与 `available()` 是两道独立的闸，缺一不可：

      - `available()` 为假（没开 `ENABLED_ENV` 或模型加载失败）→ 恒为假。生成
        路径连 `annotate_nli` 都不会调进去，重依赖也一个都不 import。
      - 模型可用但 `ON_GENERATE_ENV` 显式设成 0 → 假。这条留在人手里的理由只有
        成本：一次生成的引用条数 × 单条推理时间，实测见 `DEFAULT_ON_GENERATE`。

    **不探测模型**：这里只读环境变量，与 `available()` 分开正是为了"开关关着时
    连一次加载尝试都不发生"——把两者合并成一个函数就会在判定阶段把 100 MB 权重
    拉进内存，只为了回答一个"要不要用它"的问题。
    """
    if not _enabled():
        return False
    raw = str(os.getenv(ON_GENERATE_ENV, "")).strip().lower()
    if not raw:
        return DEFAULT_ON_GENERATE
    return raw in _TRUTHY


def judge(claim: str, evidence: str) -> dict | None:
    """语义蕴含打分。不可用时返回 None，绝不抛异常。"""
    backend = _load()
    if backend is None:
        return None
    if not (claim or "").strip() or not (evidence or "").strip():
        return None
    try:
        return backend.judge(claim, evidence)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - 推理失败只是没有评分
        return None


def reset() -> None:
    """丢弃已加载的模型（测试用；生产上等于重启服务）。"""
    with _lock:
        _cache["tried"] = False
        _cache["backend"] = None
        _cache["reason"] = ""


def info() -> dict:
    return {
        "enabled": _enabled(),
        "available": available(),
        "on_generate": on_generate(),
        "model_dir": _model_dir(),
        "reason": _cache["reason"],
        "min_free_mb": _min_free_mb(),
    }
