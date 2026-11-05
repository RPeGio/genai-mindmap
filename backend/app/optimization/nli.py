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

**默认关闭**：模型 + onnxruntime 在 2C2G 上约 100 MB 峰值（权重 25.7 MB，
运行时另算）。部署机还要留给 Docling 和语音模块，所以默认不加载。
启用需要同时给两个环境变量：

    MINDMAP_NLI_ENABLED=1
    MINDMAP_NLI_MODEL_DIR=/path/to/model     # 含 model.onnx / tokenizer.json / config.json

关闭时**零开销**：连 onnxruntime 都不 import，`available()` 只读一个环境变量。

稳定接口：
    available() -> bool
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

# 2C2G 上 Docling 常驻约 350 MB、语音转写峰值约 250 MB。留 700 MB 余量
# 再加载 NLI，避免把部署机推到 OOM。
DEFAULT_MIN_FREE_MB = 700

# cross-encoder 的位置编码上限是 512 token，中文约 1:1.5，截到 1200 字符
# 既能覆盖一页正文的主体，又不会让单次推理拖到秒级。
MAX_EVIDENCE_CHARS = 1200
MAX_CLAIM_CHARS = 200

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
        if _cache["tried"]:
            return _cache["backend"]
        _cache["tried"] = True

        if not _enabled():
            _cache["reason"] = f"未开启（{ENABLED_ENV} 未设）"
            return None
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
    if not _enabled():
        return False
    return _load() is not None


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
        "model_dir": _model_dir(),
        "reason": _cache["reason"],
        "min_free_mb": _min_free_mb(),
    }
