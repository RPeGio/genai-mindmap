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

**但 Docling 的版面模型常驻约 1.2GB，峰值 1.6GB**，2C2G 的部署机会被直接
OOM 杀掉。所以 PDF 解析提供两档后端，由 `MINDMAP_PDF_PARSER` 选择：

    auto     默认。探测到可用内存不足 Docling 的下限就自动降级到 light
    docling  强制走 Docling，版面与表格质量最好
    light    强制走 pypdf 纯文本抽取，内存几十 MB，但表格会散、双栏会串

只有 PDF 需要做这个取舍：DOCX / PPTX / XLSX / HTML 走 Docling 的非版式
流水线，不加载版面模型，内存占用可以忽略，一律交给 Docling。

未实现的格式统一抛 NotSupportedSource，由路由层转成 501，
不要返回空 Source 假装成功。
"""
from __future__ import annotations

import os
import re
import threading

from app.contracts.source import Source, SourceKind, SourceSpan

# 单份文档最多解析的页数。超过就截断，理由见 docs/PLAN.md 的 F05 边界：
# 导图本身容纳不了几百页的细节，而解析每一页都要跑版面模型，纯属浪费。
MAX_PAGES = 50

# 接受解析的文档扩展名。
#
# 刻意不含 .doc / .ppt / .xls：这三个是二进制旧格式，Docling 要靠 LibreOffice
# 先转成新格式才能读，而 LibreOffice 在 Linux 上要装几百 MB 系统包，
# 2C2G 的部署机扛不住。与其"装了依赖才能用"，不如明确拒绝并提示另存为新格式。
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


# Docling 跑 PDF 的实测峰值：import 后 345MB，加载版面模型后 1.2GB，
# 解析瞬间 1.63GB。给解释器、FastAPI、SQLite 留 500MB 余量，
# 可用内存低于这个数就别开 Docling。
_DOCLING_MIN_AVAILABLE_MB = 2200


def available_memory_mb() -> int | None:
    """读 /proc/meminfo 的 MemAvailable，拿不到就返回 None。

    只看 Linux：这是部署机的形态，2C2G 的云主机一定跑 Linux。
    其他平台返回 None，交由调用方按"不降级"处理——开发机上没人为内存发愁。
    """
    try:
        with open("/proc/meminfo", encoding="ascii", errors="ignore") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def resolve_pdf_parser() -> str:
    """决定这次 PDF 由谁解析，返回 "docling" / "pymupdf" / "light" / "docmind"。

    显式设置 `MINDMAP_PDF_PARSER` 时无条件服从——运维判断优先于自动探测，
    只有 auto 或未设置才看内存。

    auto **不会**自动选 docmind：它按页计费，替用户做主花钱不合适。
    想走云端就显式写 `MINDMAP_PDF_PARSER=docmind`。

    内存不足时优先降级到 pymupdf 而不是 pypdf：同样是纯文本抽取，
    pymupdf 在实测里快一个数量级（0.38s vs 5.89s，30 页中文文档），
    内存相当。没装 pymupdf 才退回 pypdf。
    """
    choice = os.getenv("MINDMAP_PDF_PARSER", "auto").strip().lower()
    if choice in ("docling", "light", "docmind", "pymupdf"):
        return choice
    free = available_memory_mb()
    if free is None or free >= _DOCLING_MIN_AVAILABLE_MB:
        return "docling"
    return "pymupdf" if _has_pymupdf() else "light"


def _has_pymupdf() -> bool:
    import importlib.util
    return importlib.util.find_spec("pymupdf") is not None


def docmind_credentials() -> tuple[str, str] | None:
    """读文档智能的 AccessKey；两个都配齐才算数。"""
    ak = os.getenv("MINDMAP_DOCMIND_AK", "").strip()
    sk = os.getenv("MINDMAP_DOCMIND_SK", "").strip()
    return (ak, sk) if ak and sk else None


def _parse_pdf_light(raw: bytes) -> tuple[str, int]:
    """pypdf 纯文本抽取，返回 (正文, 总页数)。

    只用文本层，不碰版面：内存几十 MB、单页毫秒级，2C2G 机器上唯一跑得动的档。
    代价是双栏排版会左右串行、表格会退化成一行一格。
    """
    import io

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(raw))
    if reader.is_encrypted:
        # 空密码能开的 PDF 很常见（只是设了权限位），先试一次再放弃。
        try:
            reader.decrypt("")
        except Exception as exc:  # noqa: BLE001 - pypdf 异常类型不稳定
            raise NotSupportedSource("这份 PDF 已加密，需要密码才能解析") from exc

    page_count = len(reader.pages)
    chunks: list[str] = []
    for page in reader.pages[:MAX_PAGES]:
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001 - 单页坏掉不该拖垮整份文档
            continue
        if text.strip():
            chunks.append(text)
    return "\n\n".join(chunks), page_count


def _parse_pdf_light_source(filename: str | None, raw: bytes) -> Source:
    """本地轻量链路：pypdf 抽文本，套上和 Docling 一致的后处理与校验。"""
    text, page_count = _parse_pdf_light(raw)
    text = _normalize_markdown(text)
    if not text:
        raise NotSupportedSource(
            "这份 PDF 没有文本层，可能是扫描件或纯图片版；"
            "轻量解析器读不出文字，需要 OCR 才能处理")
    return Source(kind=SourceKind.document, filename=filename, text=text,
                  span=SourceSpan(page=page_count) if page_count else None,
                  status="parsed")


def _parse_pdf_pymupdf(raw: bytes) -> tuple[str, int]:
    """PyMuPDF 抽文本，返回 (正文, 总页数)。

    选它的理由见 docs/PDF-PARSER-SELECTION.md：同样是纯文本抽取，
    30 页中文文档 0.38 秒 / 66MB，比 pypdf 快一个数量级，内存相当。
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


def _parse_pdf_pymupdf_source(filename: str | None, raw: bytes) -> Source:
    try:
        text, page_count = _parse_pdf_pymupdf(raw)
    except ImportError as exc:  # pragma: no cover - 只在缺依赖的部署上触发
        raise NotSupportedSource(
            "MINDMAP_PDF_PARSER=pymupdf 需要安装 pymupdf，当前环境没有；"
            "可改用 MINDMAP_PDF_PARSER=light") from exc

    text = _normalize_markdown(text)
    if not text:
        raise NotSupportedSource(
            "这份 PDF 没有文本层，可能是扫描件或纯图片版；"
            "pymupdf 只读文本层，扫描件需要 OCR 才能处理")
    return Source(kind=SourceKind.document, filename=filename, text=text,
                  span=SourceSpan(page=page_count) if page_count else None,
                  status="parsed")


def _parse_pdf_docmind(filename: str | None, raw: bytes) -> Source:
    """云端链路：阿里云文档智能转 Markdown。

    把 DocMindUnavailable 翻译成 NotSupportedSource —— 路由层只认得后者，
    且两者对用户来说都是「这次没解析成，原因写在 detail 里」。
    """
    from app.ingestion import docmind

    credentials = docmind_credentials()
    if credentials is None:
        raise NotSupportedSource(
            "MINDMAP_PDF_PARSER=docmind 需要同时配置 MINDMAP_DOCMIND_AK 与 "
            "MINDMAP_DOCMIND_SK，当前缺少其中一个")

    try:
        markdown = docmind.parse_pdf_via_docmind(filename, raw, *credentials)
    except docmind.DocMindUnavailable as exc:
        raise NotSupportedSource(str(exc)) from exc

    text = _normalize_markdown(markdown)
    if not text:
        raise NotSupportedSource(
            "文档智能没有从这份 PDF 里读出文字，可能是无法识别内容的扫描件")

    page_count = docmind.page_count_of_pdf(raw)
    return Source(kind=SourceKind.document, filename=filename, text=text,
                  span=SourceSpan(page=page_count) if page_count else None,
                  status="parsed")


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


# 图片 alt 是纯哈希 / 哈希加扩展名时没有信息量。
# Docling 给的 alt 常是「图 3 系统架构」，值得留；云端文档智能给的是
# "2ce2f359b3d0c01fba3165739287e14e.jpeg" 这种 OSS 文件名，留着只是噪音。
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

    # 图片的 URL 对导图没意义，但 alt 里常写着「图 3 系统架构」这类信息，
    # 直接整段删掉会丢线索，所以保留成 [图: ...]。
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", alt_of, markdown)
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

    # PDF 的三条链路：云端 DocMind / 本地 Docling / 本地 pypdf。
    # 非 PDF 的格式没有这个岔路，Docling 不加载版面模型，内存开销可以忽略。
    if suffix == ".pdf":
        parser = resolve_pdf_parser()
        if parser == "docmind":
            return _parse_pdf_docmind(filename, raw)
        if parser == "pymupdf":
            return _parse_pdf_pymupdf_source(filename, raw)
        if parser == "light":
            return _parse_pdf_light_source(filename, raw)

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
        message = str(exc)
        # Docling 处理 PDF 时要加载版面模型；离线或缓存缺失时它抛的异常里
        # 会带 model / download / cache 之类的字样。这里做一次启发式识别，
        # 把「解析失败：<一堆堆栈>」换成能指导用户的一句话。
        lowered = message.lower()
        if any(key in lowered for key in ("model", "download", "cache", "artifact")):
            raise NotSupportedSource(
                "解析 PDF 需要下载版面模型，当前环境缺少模型缓存。"
                "请在有网环境跑一次解析完成下载，或把 ~/.cache/docling/models "
                "预置到部署机；DOCX/PPTX/HTML 不需要模型，可先改用这些格式"
            ) from exc
        raise NotSupportedSource(f"文档解析失败：{message}") from exc

    text = _normalize_markdown(markdown)
    if not text:
        raise NotSupportedSource(
            "这份文档没有解析出任何文字，可能是扫描件或纯图片版，暂不支持")

    # 记下文档级页数。注意这是「整份文档多少页」，不是「某个节点出自第几页」——
    # 节点级溯源要模型在生成时标注来源，属于 RAG 阶段的事，见 docs/API.md。
    #
    # 只有 PDF 才有页概念：Docling 的 pages 对 DOCX/PPTX 是空列表，
    # 因为它们不走 PDF 的版面流水线，不存在「第几页」。此时 span 留空。
    page_count = len(result.pages) if getattr(result, "pages", None) else None
    span = SourceSpan(page=page_count) if page_count else None

    return Source(kind=SourceKind.document, filename=filename, text=text,
                  span=span, status="parsed")


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
