"""多模态输入解析（F05 / F06 / F07 / F08）。

流程位置：
    上传内容 ──parse()──> Source(text) ──> pipeline

当前实现状态：
    text / markdown   已实现（解码 + 空白规整）
    document          已实现：Docling 统一处理 PDF / DOCX / PPTX / XLSX 等
    image             待实现：视觉模型生成结构化描述
    audio             待实现：ASR 转写 + 时间戳

文档解析为什么选 Docling 而不是轻量抽取库：它做版面分析与表格识别，
双栏排版的论文不会把左右栏文字交错拼在一起，表格也能还原成结构化文本。
代价是首次调用要加载模型（十几秒）并占用较多内存，因此转换器做成
进程内单例，第二次之后每次转换在一两秒内。

未实现的格式统一抛 NotSupportedSource，由路由层转成 501，
不要返回空 Source 假装成功。
"""
from __future__ import annotations

import re
import threading

from app.contracts.source import Source, SourceKind

# 单份文档最多解析的页数。超过就截断，理由见 docs/PLAN.md 的 F05 边界：
# 导图本身容纳不了几百页的细节，而解析每一页都要跑版面模型，纯属浪费。
MAX_PAGES = 50

# Docling 支持的文档扩展名。列表外的类型一律拒绝，避免让解析器去猜。
DOCUMENT_SUFFIXES = {".pdf", ".docx", ".pptx", ".xlsx", ".doc", ".ppt", ".xls",
                     ".html", ".htm", ".md", ".asciidoc", ".adoc"}

_converter = None
_converter_lock = threading.Lock()
# 解析串行化：DocumentConverter 是进程内单例，多线程同时调用它内部状态不安全。
# 两个用户的项目里排队解析可以接受，换来的是不会偶发解析错乱。
_parse_lock = threading.Lock()


class NotSupportedSource(Exception):
    """该来源类型已规划但尚未实现，或格式不被支持。"""


def parse_text(text: str, kind: SourceKind = SourceKind.text) -> Source:
    """纯文本直通，去掉首尾空白；空串视为失败。"""
    if kind not in (SourceKind.text, SourceKind.markdown):
        raise NotSupportedSource("文本入口仅支持 text 或 markdown")
    cleaned = (text or "").strip()
    if not cleaned:
        raise NotSupportedSource("文本为空")
    return Source(kind=kind, text=cleaned, status="parsed")


def _suffix_of(filename: str | None) -> str:
    if not filename:
        return ""
    dot = filename.rfind(".")
    return filename[dot:].lower() if dot >= 0 else ""


def _get_converter():
    """Docling 的 DocumentConverter 初始化要十几秒（加载版面模型），全进程复用一份。

    用锁保护，避免并发请求同时初始化两份。
    """
    global _converter
    if _converter is None:
        with _converter_lock:
            if _converter is None:
                from docling.document_converter import DocumentConverter
                _converter = DocumentConverter()
    return _converter


def _normalize_markdown(markdown: str) -> str:
    """把 Docling 的 Markdown 压成适合喂模型的纯文本。

    保留 `#` 标题层级 —— 那是现成的结构线索，模型可以直接映射成节点层级；
    去掉图片、空链接这类对导图没意义的标记，并压缩多余空行。
    """
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", markdown)          # 图片
    text = re.sub(r"[ \t]+$", "", text, flags=re.MULTILINE)       # 行尾空白
    text = re.sub(r"\n{3,}", "\n\n", text)                        # 连续空行
    return text.strip()


def parse_document_bytes(filename: str | None, raw: bytes) -> Source:
    """用 Docling 解析 PDF / DOCX / PPTX 等文档，返回带 Markdown 正文的 Source。

    扫描件（没有文本层）会解析出空内容，这里显式拒绝 —— 返回空 Source
    会让下游生成一棵空树，比直接报错更难排查。
    """
    suffix = _suffix_of(filename)
    if suffix not in DOCUMENT_SUFFIXES:
        raise NotSupportedSource(f"暂不支持的文档格式：{suffix or '未知'}")

    if not raw:
        raise NotSupportedSource("文件内容为空")

    import io

    from docling_core.types.io import DocumentStream

    converter = _get_converter()
    try:
        # convert() 只接受 Path / str / DocumentStream / HttpSource，
        # 裸 BytesIO 会被 pydantic 拒掉，必须包成 DocumentStream。
        # 走内存不落盘：上传的文件没必要在内核上留痕。
        stream = DocumentStream(name=filename or "upload", stream=io.BytesIO(raw))
        with _parse_lock:
            result = converter.convert(stream, max_num_pages=MAX_PAGES)
        markdown = result.document.export_to_markdown()
    except NotSupportedSource:
        raise
    except Exception as exc:  # noqa: BLE001 - 第三方库异常类型很杂，统一转成可读文案
        raise NotSupportedSource(f"文档解析失败：{exc}") from exc

    text = _normalize_markdown(markdown)
    if not text:
        raise NotSupportedSource(
            "这份文档没有解析出任何文字，可能是扫描件或纯图片版，暂不支持")

    return Source(kind=SourceKind.document, filename=filename, text=text,
                  status="parsed")


def parse_bytes(kind: SourceKind, filename: str | None, raw: bytes) -> Source:
    """按类型分发。

    分发顺序：
      1. markdown/text → 直接解码
      2. document      → Docling（PDF / DOCX / PPTX / XLSX / HTML …）
      3. image/audio   → 交给模型或多模态服务
    """
    if kind in (SourceKind.text, SourceKind.markdown):
        try:
            source = parse_text(raw.decode("utf-8"), kind)
            source.filename = filename
            return source
        except UnicodeDecodeError as exc:
            raise NotSupportedSource("文件不是 UTF-8 文本") from exc

    if kind is SourceKind.document:
        return parse_document_bytes(filename, raw)

    if kind is SourceKind.image:
        # TODO(F06): 视觉模型输出结构化描述，保留"上图分为几个区域"这类空间关系
        raise NotSupportedSource("图片理解待实现")

    if kind is SourceKind.audio:
        # TODO(F07): ASR 转写 + 分段，保留时间戳写进 SourceSpan
        raise NotSupportedSource("语音转写待实现")

    raise NotSupportedSource(f"未知来源类型：{kind}")
