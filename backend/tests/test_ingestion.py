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

    def test_legacy_office_formats_are_supported(self):
        """旧格式现在支持：.doc/.ppt 经 LibreOffice 转 PDF 再走视觉提取，
        .xls 用 xlrd 直读。依赖缺失时会报错并给出安装命令，不静默降级。"""
        for suffix in (".doc", ".ppt", ".xls"):
            with self.subTest(suffix=suffix):
                self.assertIn(suffix, DOCUMENT_SUFFIXES)

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


class PdfVisionTest(unittest.TestCase):
    """PDF 主路径是视觉提取，文本层只在模型不可用时兜底。"""

    def test_vision_output_is_used(self):
        from unittest.mock import patch

        with patch("app.ingestion.vision_pdf.extract_pages") as extract:
            extract.return_value = (["# 第一章\n\n正文内容", "## 1.1 小节\n\n更多"], 2, 2, [])
            source = parse_document_bytes("a.pdf", make_pdf(2))
        self.assertIn("# 第一章", source.text)
        self.assertIn("## 1.1 小节", source.text)
        self.assertFalse(source.truncated)
        self.assertEqual(source.parsed_pages, 2)
        self.assertEqual(source.span.page, 2)

    def test_vision_failure_falls_back_to_text_layer(self):
        from unittest.mock import patch

        from app.ingestion.vision_pdf import VisionUnavailable

        with patch("app.ingestion.vision_pdf.extract_pages") as extract:
            extract.side_effect = VisionUnavailable("未配置模型地址")
            source = parse_document_bytes("a.pdf", make_pdf(3))
        # 回落到 pymupdf 抽出的文本层
        self.assertIn("page 1 content", source.text)
        self.assertEqual(source.status, "parsed")

    def test_vision_returns_nothing_falls_back(self):
        from unittest.mock import patch

        with patch("app.ingestion.vision_pdf.extract_pages") as extract:
            extract.return_value = (["", ""], 2, 2, [1, 2])
            source = parse_document_bytes("a.pdf", make_pdf(2))
        self.assertIn("page 1 content", source.text)

    def test_vision_failure_without_text_layer_raises(self):
        from unittest.mock import patch

        import pymupdf

        from app.ingestion.vision_pdf import VisionUnavailable

        # 造一份纯图片 PDF：整页是一张图，没有文本层
        doc = pymupdf.open()
        page = doc.new_page()
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 40), False)
        pix.clear_with(200)
        page.insert_image(pymupdf.Rect(50, 50, 250, 250), pixmap=pix)
        raw = doc.tobytes()
        doc.close()

        with patch("app.ingestion.vision_pdf.extract_pages") as extract:
            extract.side_effect = VisionUnavailable("网络不通")
            with self.assertRaises(NotSupportedSource) as raised:
                parse_document_bytes("scan.pdf", raw)
        self.assertIn("没有文本层", str(raised.exception))

    def test_failed_page_is_filled_from_text_layer(self):
        """视觉识别失败的页，若同一页有文本层就用它补回来。

        原 PDF 有文本层说明这页本来就有文字，只是渲染或模型那侧出了问题；
        直接丢掉等于白丢一页正文。
        """
        from unittest.mock import patch

        with patch("app.ingestion.vision_pdf.extract_pages") as extract:
            # 3 页里第 2 页识别失败
            extract.return_value = (["# 第一页", "", "# 第三页"], 3, 3, [2])
            source = parse_document_bytes("a.pdf", make_pdf(3))

        self.assertIn("# 第一页", source.text)
        self.assertIn("# 第三页", source.text)
        # 第 2 页由文本层补回：make_pdf 每页写的是 "page N content"
        self.assertIn("page 2 content", source.text)
        self.assertEqual(source.failed_pages, [])
        self.assertFalse(source.truncated)
        self.assertEqual(source.parsed_pages, 3)

    def test_unrecoverable_page_is_reported(self):
        """既识别失败又没有文本层时，页码要明确报出来，不能只标一个 truncated。"""
        from unittest.mock import patch

        with patch("app.ingestion.service._parse_pdf", return_value=(["", "", ""], 3)), \
             patch("app.ingestion.vision_pdf.extract_pages") as extract:
            extract.return_value = (["# 第一页", "", "# 第三页"], 3, 3, [2])
            source = parse_document_bytes("a.pdf", b"%PDF-fake")

        self.assertIn("# 第一页", source.text)
        self.assertEqual(source.failed_pages, [2])
        # 页数没超上限，所以不算"截断"——这两个信号必须分开
        self.assertFalse(source.truncated)
        # parsed_pages 是处理边界（3 页都处理了），不是"有内容的页数"
        self.assertEqual(source.parsed_pages, 3)
        self.assertEqual(source.span.page, 3)

    def test_partial_failure_does_not_fake_truncated_hint(self):
        """3 页里 1 页失败，不能给出"共 3 页仅处理前 3 页"这种自相矛盾的组合。"""
        from unittest.mock import patch

        with patch("app.ingestion.service._parse_pdf", return_value=(["", "", ""], 3)), \
             patch("app.ingestion.vision_pdf.extract_pages") as extract:
            extract.return_value = (["# 一", "", "# 三"], 3, 3, [2])
            source = parse_document_bytes("a.pdf", b"%PDF-fake")

        # truncated 为假时前端不渲染"仅处理前 N 页"，缺页改由 failed_pages 说明
        self.assertFalse(source.truncated)
        self.assertEqual(source.failed_pages, [2])
        self.assertEqual(source.span.page, 3)
        # 处理边界是 3（全部处理过），但正文里只有两页的内容
        self.assertEqual(source.parsed_pages, 3)
        self.assertNotIn("page 2", source.text or "")

    def test_fallback_reports_pages_without_text_layer(self):
        """视觉整体不可用、退回文本层时，文本层里空白的页同样要报出来。"""
        from unittest.mock import patch

        # 3 页里第 2 页没有文本层（扫描页），另外两页有
        with patch("app.ingestion.service._parse_pdf",
                   return_value=(["page 1 text", "", "page 3 text"], 3)):
            source = parse_document_bytes("a.pdf", b"%PDF-fake")

        self.assertIn("page 1 text", source.text)
        self.assertEqual(source.failed_pages, [2])
        self.assertEqual(source.parsed_pages, 3)
        self.assertFalse(source.truncated)

    def test_page_limit_marks_truncated(self):
        from unittest.mock import patch

        with patch("app.ingestion.vision_pdf.extract_pages") as extract:
            # 25 页的文档，视觉只处理前 20 页
            extract.return_value = (["# 内容"] * 20, 25, 20, [])
            source = parse_document_bytes("a.pdf", make_pdf(25))
        self.assertEqual(source.span.page, 25)
        self.assertEqual(source.parsed_pages, 20)
        self.assertTrue(source.truncated)


class LegacyOfficeTest(unittest.TestCase):
    """旧格式的三条链路：.doc/.ppt 转 PDF 后走视觉，.xls 用 xlrd。"""

    def test_doc_without_libreoffice_reports_install_hint(self):
        """没装 LibreOffice 时要给出安装命令，而不是笼统的"解析失败"。"""
        from unittest.mock import patch

        from app.ingestion.legacy_office import LegacyFormatUnavailable

        with patch("app.ingestion.legacy_office.soffice_cmd", return_value=None):
            with self.assertRaises(NotSupportedSource) as raised:
                parse_document_bytes("a.doc", b"\xd0\xcf\x11\xe0fake")
        message = str(raised.exception)
        self.assertIn("LibreOffice", message)
        self.assertIn("apt install", message)

    def test_ppt_conversion_failure_is_reported(self):
        from unittest.mock import patch

        from app.ingestion.legacy_office import LegacyFormatUnavailable

        with patch("app.ingestion.legacy_office.convert_to_pdf",
                   side_effect=LegacyFormatUnavailable("转换超时")):
            with self.assertRaises(NotSupportedSource) as raised:
                parse_document_bytes("a.ppt", b"\xd0\xcf\x11\xe0fake")
        self.assertIn("转换超时", str(raised.exception))

    def test_converted_pdf_goes_through_vision(self):
        """转换成功后应当复用 PDF 的视觉提取链路。"""
        from unittest.mock import patch

        with patch("app.ingestion.legacy_office.convert_to_pdf",
                   return_value=make_pdf(2)) as convert, \
             patch("app.ingestion.vision_pdf.extract_pages") as extract:
            extract.return_value = (["# 第一章", "## 1.1"], 2, 2, [])
            source = parse_document_bytes("a.doc", b"\xd0\xcf\x11\xe0fake")
        convert.assert_called_once()
        self.assertIn("# 第一章", source.text)
        self.assertEqual(source.span.page, 2)

    def test_xls_cell_formatting(self):
        """日期的序列号、整数的多余小数都要在输出前处理掉。"""
        from app.ingestion.legacy_office import _cell_text

        class Cell:
            def __init__(self, ctype, value):
                self.ctype = ctype
                self.value = value

        import xlrd

        book = type("Book", (), {"datemode": 0})()
        self.assertEqual(_cell_text(book, Cell(xlrd.XL_CELL_NUMBER, 12.0)), "12")
        self.assertEqual(_cell_text(book, Cell(xlrd.XL_CELL_NUMBER, 3.5)), "3.5")
        self.assertEqual(_cell_text(book, Cell(xlrd.XL_CELL_TEXT, "苹果")), "苹果")
        self.assertEqual(_cell_text(book, Cell(xlrd.XL_CELL_EMPTY, "")), "")
        self.assertEqual(_cell_text(book, Cell(xlrd.XL_CELL_BOOLEAN, 1)), "TRUE")
        # 46096 是 Excel 日期序列号，对应 2026-03-15
        self.assertEqual(_cell_text(book, Cell(xlrd.XL_CELL_DATE, 46096.0)), "2026-03-15")

    def test_xls_empty_content_is_rejected(self):
        from unittest.mock import patch

        from app.ingestion.legacy_office import LegacyFormatUnavailable

        with patch("app.ingestion.legacy_office.read_xls",
                   side_effect=LegacyFormatUnavailable("没有可读的单元格")):
            with self.assertRaises(NotSupportedSource):
                parse_document_bytes("a.xls", b"\xd0\xcf\x11\xe0fake")


if __name__ == "__main__":
    unittest.main()
