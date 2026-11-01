"""多模态输入解析（F05 / F06 / F07 / F08）。

流程位置：
    上传内容 ──parse()──> Source(text) ──> pipeline

当前实现状态：
    text / markdown   已实现（解码 + 空白规整）
    document(PDF)     待实现：PyMuPDF
    document(DOCX)    待实现：python-docx
    document(PPTX)    待实现：python-pptx
    image             待实现：视觉模型生成结构化描述
    audio             待实现：ASR 转写 + 时间戳

未实现的格式统一抛 NotSupportedSource，由路由层转成 501，
不要返回空 Source 假装成功。
"""
from __future__ import annotations

from app.contracts.source import Source, SourceKind


class NotSupportedSource(Exception):
    """该来源类型已规划但尚未实现。"""


def parse_text(text: str, kind: SourceKind = SourceKind.text) -> Source:
    """纯文本直通，去掉首尾空白；空串视为失败。"""
    if kind not in (SourceKind.text, SourceKind.markdown):
        raise NotSupportedSource("文本入口仅支持 text 或 markdown")
    cleaned = (text or "").strip()
    if not cleaned:
        raise NotSupportedSource("文本为空")
    return Source(kind=kind, text=cleaned, status="parsed")


def parse_bytes(kind: SourceKind, filename: str | None, raw: bytes) -> Source:
    """按类型分发。

    分发顺序：
      1. markdown/text → 直接解码
      2. document      → 按扩展名选解析器（pdf/docx/pptx）
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
        # TODO(F05): 按 filename 后缀分发到 _parse_pdf / _parse_docx / _parse_pptx
        raise NotSupportedSource("文档解析待实现（PyMuPDF / python-docx / python-pptx）")

    if kind is SourceKind.image:
        # TODO(F06): 视觉模型输出结构化描述，保留"上图分为几个区域"这类空间关系
        raise NotSupportedSource("图片理解待实现")

    if kind is SourceKind.audio:
        # TODO(F07): ASR 转写 + 分段，保留时间戳写进 SourceSpan
        raise NotSupportedSource("语音转写待实现")

    raise NotSupportedSource(f"未知来源类型：{kind}")
