"""文档导入的边界行为。

真实的 Docling 解析很慢（首次要加载模型），所以这里只测不触发解析的路径：
格式白名单、空文件、文本直通。实际解析效果由手工端到端验证覆盖。
"""
import unittest

from app.contracts.source import SourceKind
from app.ingestion.service import (
    DOCUMENT_SUFFIXES,
    MAX_PAGES,
    NotSupportedSource,
    parse_bytes,
    parse_document_bytes,
    parse_text,
)


class TextSourceTest(unittest.TestCase):
    def test_text_passes_through(self):
        source = parse_text("  一段文本  ")
        self.assertEqual(source.text, "一段文本")
        self.assertEqual(source.status, "parsed")

    def test_blank_text_rejected(self):
        with self.assertRaises(NotSupportedSource):
            parse_text("   ")

    def test_text_endpoint_rejects_document_kind(self):
        """文本入口不该接受 document 类型，避免绕过格式检查。"""
        with self.assertRaises(NotSupportedSource):
            parse_text("x", SourceKind.document)


class MarkdownCleanupTest(unittest.TestCase):
    """解析结果转文本时的清洗规则。"""

    def test_image_alt_is_kept_url_dropped(self):
        from app.ingestion.service import _normalize_markdown
        out = _normalize_markdown("正文\n\n![图 3 系统架构](http://x/y.png)\n\n后续")
        self.assertIn("[图: 图 3 系统架构]", out)
        self.assertNotIn("http://x/y.png", out)

    def test_image_without_alt_disappears(self):
        from app.ingestion.service import _normalize_markdown
        out = _normalize_markdown("正文\n\n![](http://x/y.png)\n\n后续")
        self.assertNotIn("http://x/y.png", out)
        self.assertNotIn("[图:", out)

    def test_blank_lines_are_squeezed(self):
        from app.ingestion.service import _normalize_markdown
        self.assertEqual(_normalize_markdown("a\n\n\n\n\nb"), "a\n\nb")


class DocumentGuardTest(unittest.TestCase):
    """这些分支在真正调用 Docling 之前就返回，跑得很快。"""

    def test_unsupported_suffix_rejected(self):
        with self.assertRaises(NotSupportedSource) as raised:
            parse_document_bytes("virus.exe", b"MZ\x90\x00")
        self.assertIn("暂不支持", str(raised.exception))

    def test_no_suffix_rejected(self):
        with self.assertRaises(NotSupportedSource):
            parse_document_bytes("README", b"hello")

    def test_empty_file_rejected(self):
        with self.assertRaises(NotSupportedSource) as raised:
            parse_document_bytes("a.pdf", b"")
        self.assertIn("为空", str(raised.exception))

    def test_supported_suffixes_include_core_formats(self):
        for suffix in (".pdf", ".docx", ".pptx"):
            self.assertIn(suffix, DOCUMENT_SUFFIXES)

    def test_legacy_office_formats_rejected(self):
        """旧版二进制格式需要 LibreOffice，刻意不支持，并提示另存为新格式。"""
        for suffix in (".doc", ".ppt", ".xls"):
            with self.subTest(suffix=suffix):
                with self.assertRaises(NotSupportedSource):
                    parse_document_bytes("a" + suffix, b"x")

    def test_bytes_dispatch_by_kind(self):
        """parse_bytes 会把 document 交给文档解析器，而不是当成文本。"""
        with self.assertRaises(NotSupportedSource) as raised:
            parse_bytes(SourceKind.document, "a.exe", b"x")
        self.assertIn("暂不支持", str(raised.exception))

    def test_image_and_audio_still_pending(self):
        for kind in (SourceKind.image, SourceKind.audio):
            with self.assertRaises(NotSupportedSource):
                parse_bytes(kind, None, b"x")


def make_pdf(pages: int) -> bytes:
    """生成一份带文本层的 PDF，用于测分页与截断。"""
    import pymupdf

    doc = pymupdf.open()
    for index in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"page {index + 1} content")
    raw = doc.tobytes()
    doc.close()
    return raw


class PdfPageLimitTest(unittest.TestCase):
    """超过 MAX_PAGES 时必须显式标注截断，不能静默丢内容。

    验收时上传 51 页 PDF，接口返回成功但只有前 50 页进了正文，
    响应里没有任何字段说明这件事——用户拿到的是一份缺了尾巴的正文。
    """

    def test_within_limit_is_not_truncated(self):
        source = parse_document_bytes("small.pdf", make_pdf(5))
        self.assertEqual(source.parsed_pages, 5)
        self.assertFalse(source.truncated)
        self.assertEqual(source.span.page, 5)
        self.assertIn("page 1 content", source.text)

    def test_exactly_at_limit_is_not_truncated(self):
        source = parse_document_bytes("edge.pdf", make_pdf(MAX_PAGES))
        self.assertEqual(source.parsed_pages, MAX_PAGES)
        self.assertFalse(source.truncated)
        self.assertEqual(source.span.page, MAX_PAGES)

    def test_over_limit_is_marked_truncated(self):
        over = MAX_PAGES + 1
        source = parse_document_bytes("big.pdf", make_pdf(over))

        # 正文只到第 MAX_PAGES 页
        self.assertEqual(source.parsed_pages, MAX_PAGES)
        self.assertIn(f"page {MAX_PAGES} content", source.text)
        self.assertNotIn(f"page {over} content", source.text)

        # 但必须明确告诉调用方：总共多少页、解析了多少页、内容被截断了
        self.assertEqual(source.span.page, over)
        self.assertTrue(source.truncated)
        self.assertEqual(source.status, "parsed")


if __name__ == "__main__":
    unittest.main()
