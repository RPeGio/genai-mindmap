"""文档导入的边界行为。

真实的 Docling 解析很慢（首次要加载模型），所以这里只测不触发解析的路径：
格式白名单、空文件、文本直通。实际解析效果由手工端到端验证覆盖。
"""
import unittest

from app.contracts.source import SourceKind
from app.ingestion.service import (
    DOCUMENT_SUFFIXES,
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


if __name__ == "__main__":
    unittest.main()
