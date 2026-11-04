"""多模态输入解析（F05 / F06 / F07 / F08）。

流程位置：
    上传内容 ──parse()──> Source(text) ──> pipeline

当前实现状态：
    text / markdown   已实现（解码 + 空白规整）
    document          PDF 走 pymupdf；DOCX / PPTX / XLSX / HTML 走 Docling
    image             待实现：视觉模型生成结构化描述
    audio             待实现：ASR 转写 + 时间戳

**PDF 为什么不用 Docling**：它的版面模型常驻 1.2GB、实测峰值 1.6–2.8GB，
2C2G 的部署机会被 OOM 直接杀掉。PDF 改用 pymupdf 只读文本层，30 页中文文档
0.38 秒 / 66MB。章节层级交给下游的模型去认，实测比版面模型更准——完整对比
见 docs/PDF-PARSER-SELECTION.md 与 docs/LLM-OUTLINE-ACCURACY.md。

**其余格式仍然走 Docling**：DOCX / PPTX / XLSX / HTML 走的是它的非版式
流水线，不加载 PDF 版面模型，内存开销可以忽略，没有理由换掉。

旧格式 .doc / .ppt / .xls 不在支持列表里：它们是二进制复合文档，Docling 要先
经 LibreOffice 转成新格式才能读。实测代价——

    磁盘      LibreOffice 装完占 1.5GB
    内存      转换时进程树峰值 720~745MB
              （Docling 主进程 365MB + soffice 子进程 355~380MB）
    耗时      是新格式的 4~9 倍（.doc 8.1s vs .docx 1.8s；.xls 4.8s vs .xlsx 0.5s）
    质量      与新格式逐字相同，没有额外收益

而且 Docling 每份文件都用独立的临时 LibreOffice profile，没有进程复用，
意味着这个开销每份文件都要付一次。2C2G 的部署机同时养 Docling 和 LibreOffice
并不现实，明确拒绝并提示另存为新格式，比"装了依赖才能用"更诚实。

未实现的格式统一抛 NotSupportedSource，由路由层转成 422，
不要返回空 Source 假装成功。
"""
from __future__ import annotations

import re
import threading

from app.contracts.source import Source, SourceKind, SourceSpan

# 单份文档最多解析的页数。超过就截断，理由见 docs/PLAN.md 的 F05 边界：
# 导图本身容纳不了几百页的细节。
MAX_PAGES = 50

# 接受解析的文档扩展名。
#
# 刻意不含 .doc / .ppt / .xls：这三个是二进制旧格式，需要 LibreOffice 先转换。
# 实测转换时进程树峰值 720~745MB、耗时是新格式的 4~9 倍，而 LibreOffice 本身
# 还要占 1.5GB 磁盘；2C2G 的部署机同时养 Docling 和它并不现实。
DOCUMENT_SUFFIXES = {".pdf", ".docx", ".pptx", ".xlsx",
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


def _parse_pdf(raw: bytes) -> tuple[str, int]:
    """PyMuPDF 抽文本，返回 (正文, 总页数)。

    只读文本层，不碰版面：实测 15 页英文双栏论文 0.19 秒 / 55MB，
    30 页中文文档 0.38 秒 / 66MB。扫描件抽不出文字，这里会返回空正文，
    由调用方拒绝。
    """
    import pymupdf

    doc = pymupdf.open(stream=raw, filetype="pdf")
    try:
        page_count = doc.page_count
        chunks = []
        for index in range(min(page_count, MAX_PAGES)):
            try:
                chunks.append(doc[index].get_text("text") or "")
            except Exception:  # noqa: BLE001 - 单页损坏不该拖垮整份文档
                continue
    finally:
        doc.close()
    return "\n\n".join(c for c in chunks if c.strip()), page_count


def _parse_pdf_source(filename: str | None, raw: bytes) -> Source:
    try:
        text, page_count = _parse_pdf(raw)
    except ImportError as exc:  # pragma: no cover - 只在缺依赖的部署上触发
        raise NotSupportedSource(
            "PDF 解析需要 pymupdf，当前环境没有安装；请执行 "
            "pip install pymupdf 后重启服务") from exc
    except Exception as exc:  # noqa: BLE001 - pymupdf 的异常类型不稳定
        raise NotSupportedSource(f"PDF 解析失败：{exc}") from exc

    text = _normalize_markdown(text)
    if not text:
        raise NotSupportedSource(
            "这份 PDF 没有文本层，可能是扫描件或纯图片版；"
            "pymupdf 只读文本层，扫描件需要 OCR 才能处理")
    return Source(kind=SourceKind.document, filename=filename, text=text,
                  span=SourceSpan(page=page_count) if page_count else None,
                  status="parsed")


def _get_converter():
    """Docling 的 DocumentConverter 初始化要十几秒，全进程复用一份。

    用锁保护，避免并发请求同时初始化两份。
    """
    global _converter
    if _converter is None:
        with _converter_lock:
            if _converter is None:
                from docling.document_converter import DocumentConverter
                _converter = DocumentConverter()
    return _converter


# 图片 alt 是纯哈希 / 哈希加扩展名时没有信息量。
# Docling 给的 alt 常是「图 3 系统架构」，值得留；机器生成的文件名
# （"2ce2f359b3d0c01fba3165739287e14e.jpeg"）留着只是噪音。
_MEANINGLESS_ALT = re.compile(r"^[0-9a-f]{16,}(\.\w{1,5})?$", re.IGNORECASE)


def _normalize_markdown(markdown: str) -> str:
    """把解析出的 Markdown 压成适合喂模型的纯文本。

    保留 `#` 标题层级 —— 那是现成的结构线索，模型可以直接映射成节点层级；
    去掉图片、空链接这类对导图没意义的标记，并压缩多余空行。
    """
    def alt_of(match: re.Match[str]) -> str:
        alt = match.group(1).strip()
        if not alt or _MEANINGLESS_ALT.match(alt):
            return ""
        return f"[图: {alt}]"

    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", alt_of, markdown)
    text = re.sub(r"[ \t]+$", "", text, flags=re.MULTILINE)       # 行尾空白
    text = re.sub(r"\n{3,}", "\n\n", text)                        # 连续空行
    return text.strip()


def _parse_office_source(filename: str | None, raw: bytes) -> Source:
    """DOCX / PPTX / XLSX / HTML 走 Docling 的非版式流水线。"""
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

    # 记下文档级页数。注意这是「整份文档多少页」，不是「某个节点出自第几页」——
    # 节点级溯源要模型在生成时标注来源，属于 RAG 阶段的事，见 docs/API.md。
    #
    # Docling 的 pages 对 DOCX/PPTX 是空列表，因为它们不是分页格式，
    # 不存在「第几页」。此时 span 留空。
    page_count = len(result.pages) if getattr(result, "pages", None) else None
    return Source(kind=SourceKind.document, filename=filename, text=text,
                  span=SourceSpan(page=page_count) if page_count else None,
                  status="parsed")


def parse_document_bytes(filename: str | None, raw: bytes) -> Source:
    """解析上传的文档，返回带正文的 Source。

    扫描件（没有文本层）会解析出空内容，这里显式拒绝 —— 返回空 Source
    会让下游生成一棵空树，比直接报错更难排查。
    """
    suffix = _suffix_of(filename)
    if suffix not in DOCUMENT_SUFFIXES:
        raise NotSupportedSource(f"暂不支持的文档格式：{suffix or '未知'}")

    if not raw:
        raise NotSupportedSource("文件内容为空")

    if suffix == ".pdf":
        return _parse_pdf_source(filename, raw)
    return _parse_office_source(filename, raw)


def parse_bytes(kind: SourceKind, filename: str | None, raw: bytes) -> Source:
    """按类型分发。

    分发顺序：
      1. markdown/text → 直接解码
      2. document      → PDF 用 pymupdf，其余用 Docling
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
