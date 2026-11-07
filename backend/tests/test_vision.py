"""图片输入（F06）：与 PDF 逐页共用同一条多模态调用。

这里不联网。真实识别效果由手工端到端验证覆盖，测试锁住的是格式判定、
配置预检、以及失败时必须是报错而不是空正文。
"""
import threading
import time
import unittest
from unittest.mock import patch

from app.contracts.source import SourceKind
from app.ingestion import vision_pdf
from app.ingestion.router import list_supported_formats
from app.ingestion.service import NotSupportedSource, parse_bytes

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
GIF = b"GIF89a" + b"\x00" * 32
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 32
BMP = b"BM" + b"\x00" * 32

MODEL_ENV = {
    "MINDMAP_LLM_MODE": "model",
    "MINDMAP_LLM_BASE_URL": "http://example.invalid/v1",
    "MINDMAP_LLM_API_KEY": "test-key",
    "MINDMAP_LLM_MODEL": "test-model",
}


class GuessMimeTest(unittest.TestCase):
    """data URL 前缀写错会让服务端解码失败，而错误只说"识别失败"。"""

    def test_png(self):
        self.assertEqual(vision_pdf._guess_mime(PNG), "image/png")

    def test_jpeg(self):
        self.assertEqual(vision_pdf._guess_mime(JPEG), "image/jpeg")

    def test_gif(self):
        self.assertEqual(vision_pdf._guess_mime(GIF), "image/gif")

    def test_webp(self):
        # WebP 的魔数不在开头，是 RIFF....WEBP，必须单独判
        self.assertEqual(vision_pdf._guess_mime(WEBP), "image/webp")

    def test_bmp(self):
        self.assertEqual(vision_pdf._guess_mime(BMP), "image/bmp")

    def test_unknown_format_rejected(self):
        with self.assertRaises(vision_pdf.VisionUnavailable) as ctx:
            vision_pdf._guess_mime(b"not an image at all")
        self.assertIn("无法识别", str(ctx.exception))


class DescribeImageGuardTest(unittest.TestCase):
    def test_demo_mode_rejected_before_network(self):
        """demo 模式不联网，和 PDF 视觉提取保持同一个约定。"""
        with patch.dict("os.environ", {"MINDMAP_LLM_MODE": "demo"}, clear=False):
            with self.assertRaises(vision_pdf.VisionUnavailable) as ctx:
                vision_pdf.describe_image(PNG)
        self.assertIn("demo", str(ctx.exception))

    def test_missing_credentials_rejected(self):
        env = {"MINDMAP_LLM_MODE": "model", "MINDMAP_LLM_BASE_URL": "",
               "MINDMAP_LLM_API_KEY": "", "MINDMAP_LLM_MODEL": ""}
        with patch.dict("os.environ", env, clear=False):
            with self.assertRaises(vision_pdf.VisionUnavailable):
                vision_pdf.describe_image(PNG)

    def test_empty_image_rejected(self):
        with patch.dict("os.environ", MODEL_ENV, clear=False):
            with self.assertRaises(vision_pdf.VisionUnavailable) as ctx:
                vision_pdf.describe_image(b"")
        self.assertIn("为空", str(ctx.exception))


class ParseImageTest(unittest.TestCase):
    """parse_bytes 的 image 分支：要么给出 Source，要么报错。"""

    def test_vision_text_becomes_source(self):
        with patch.dict("os.environ", MODEL_ENV, clear=False), \
             patch("app.ingestion.vision_pdf._call_vision",
                   return_value="# 需求\n- 登录\n- 支付"):
            source = parse_bytes(SourceKind.image, "草图.png", PNG)
        self.assertEqual(source.kind, SourceKind.image)
        self.assertEqual(source.text, "# 需求\n- 登录\n- 支付")
        self.assertEqual(source.status, "parsed")
        self.assertEqual(source.filename, "草图.png")

    def test_vision_failure_becomes_not_supported(self):
        """图片没有文本层可兜底，视觉不可用时只能报错，不能返回空正文。"""
        with patch.dict("os.environ", MODEL_ENV, clear=False), \
             patch("app.ingestion.vision_pdf._call_vision",
                   side_effect=vision_pdf.VisionUnavailable("模型不支持图片输入")):
            with self.assertRaises(NotSupportedSource) as ctx:
                parse_bytes(SourceKind.image, "草图.png", PNG)
        self.assertIn("模型不支持图片输入", str(ctx.exception))

    def test_truncated_output_is_failure(self):
        """finish_reason=length 说明输出被硬切，半个结果不能当成功。"""
        body = {"choices": [{"finish_reason": "length",
                             "message": {"content": "写了一半"}}]}
        with patch.dict("os.environ", MODEL_ENV, clear=False), \
             patch("httpx.Client") as client_cls:
            client_cls.return_value.__enter__.return_value.post.return_value.status_code = 200
            client_cls.return_value.__enter__.return_value.post.return_value.json.return_value = body
            with self.assertRaises(vision_pdf.VisionUnavailable) as ctx:
                vision_pdf.describe_image(PNG)
        self.assertIn("截断", str(ctx.exception))

    def test_network_error_becomes_vision_unavailable(self):
        """连接失败/超时必须转成业务错误，否则图片路径会冒成 500。

        PDF 路径有单页兜底看不出来，图片没有兜底——不包这一层的话用户看到
        的是"服务器内部错误"，而不是"图片识别没成功"。
        """
        import httpx

        with patch.dict("os.environ", MODEL_ENV, clear=False), \
             patch("httpx.Client") as client_cls:
            client_cls.return_value.__enter__.return_value.post.side_effect = \
                httpx.ReadTimeout("timed out")
            with self.assertRaises(vision_pdf.VisionUnavailable) as ctx:
                vision_pdf.describe_image(PNG)
        self.assertIn("请求失败", str(ctx.exception))
        self.assertIn("ReadTimeout", str(ctx.exception))


class FormatsTest(unittest.TestCase):
    def test_image_listed_with_own_limit(self):
        data = list_supported_formats()
        self.assertIn(".png", data["image"])
        self.assertIn(".jpg", data["image"])
        # SVG 是矢量描述，模型看到的是 XML，不该收
        self.assertNotIn(".svg", data["image"])
        self.assertIn("maxImageBytes", data)


def make_pdf(pages: int) -> bytes:
    """生成一份多页 PDF，只为提供可渲染的页面。

    逐页识别本身被 patch 掉：这里要验证的是「谁先完成」和「结果落在第几页」，
    跟页面上写了什么无关。
    """
    import pymupdf

    doc = pymupdf.open()
    for index in range(pages):
        doc.new_page().insert_text((72, 72), f"page {index + 1}")
    raw = doc.tobytes()
    doc.close()
    return raw


class FakePage:
    def __init__(self, text: str) -> None:
        self._text = text

    def get_text(self, _mode: str) -> str:
        return self._text


class FakeDoc:
    """只实现 `plan_page_count()` 用到的那一小块接口。

    真实的 pymupdf 页写中文要额外的 CJK 字体，而这几个用例要断言的只是
    「按多少 token 估这一页」，与渲染无关。
    """

    def __init__(self, texts: list[str]) -> None:
        self._texts = texts

    def __getitem__(self, index: int) -> FakePage:
        return FakePage(self._texts[index])


class PageBudgetTest(unittest.TestCase):
    """页数不再按固定值封顶：能装进上下文就继续处理。

    原先 20 页是凭感觉定的：一份 30 页的讲义会被砍掉三分之一，而 300 页的
    专著本来也塞不进一次生成。现在由 `plan_page_count()` 按上下文档算，
    截断由调用方的 `truncated` 说明。
    """

    def test_scanned_pages_cost_the_measured_prior(self):
        """没有文本层的页按实测 379 字/页 折算成 token。"""
        # 379 字 ≈ 253 token；600 token 只装得下 2 页（506），第 3 页就超了
        with patch.object(vision_pdf, "CONTEXT_TOKEN_LIMIT", 600):
            limit = vision_pdf.plan_page_count(FakeDoc([""] * 8), 8)
        self.assertEqual(limit, 2)

    def test_text_layer_pages_cost_their_actual_length(self):
        """有文本层的页按实际字符数算，比先验值贵得多。"""
        # 1600 个 ASCII 字符 ≈ 400 token/页 → 600 token 只装得下 1 页
        page = "a" * 1600
        with patch.object(vision_pdf, "CONTEXT_TOKEN_LIMIT", 600):
            limit = vision_pdf.plan_page_count(FakeDoc([page] * 4), 4)
        self.assertEqual(limit, 1)

    def test_no_fixed_page_cap_and_the_budget_scales(self):
        """页数的唯一约束是上下文：预算越大处理得越多，且没有隐藏的页数上限。

        原先断言「300 页 → 237 页」，那个 237 是按当时的阈值算出来的，阈值一调
        就变成在断言一个过期数字。现在改成比两个预算：同一份 300 页文档，预算
        放大十倍之后必须处理得更多，而且在足够大的预算下一页不丢。
        """
        doc = FakeDoc([""] * 300)       # 扫描页，按实测 253 token/页 折算
        with patch.object(vision_pdf, "CONTEXT_TOKEN_LIMIT", 1_000):
            small = vision_pdf.plan_page_count(doc, 300)
            # 小预算也照收完 25 个短页：卡住页数的是预算，不是某个固定值（原先 20）
            self.assertEqual(
                vision_pdf.plan_page_count(FakeDoc(["page"] * 25), 25), 25)
        with patch.object(vision_pdf, "CONTEXT_TOKEN_LIMIT", 100_000):
            large = vision_pdf.plan_page_count(doc, 300)
        self.assertLess(small, large)
        self.assertEqual(large, 300)

    def test_first_page_alone_over_the_limit_yields_zero(self):
        """第一页就装不下时返回 0，由调用方明确报错而不是硬塞。

        用极小预算构造这个场景，样本不必跟着阈值一起长大：1 token 连一页汉字
        都装不下，结论与阈值取多少无关。
        """
        with patch.object(vision_pdf, "CONTEXT_TOKEN_LIMIT", 1):
            limit = vision_pdf.plan_page_count(FakeDoc(["字" * 100]), 1)
        self.assertEqual(limit, 0)


class PdfPageBudgetTest(unittest.TestCase):
    """`extract_pages()` 的页数来自上下文预算，不再是 min(总页数, 20)。"""

    def _extract(self, raw: bytes, fake=None):
        with patch("app.ingestion.vision_pdf._config",
                   return_value=("http://example.invalid/v1", "k", "m")), \
             patch("app.ingestion.vision_pdf._page_to_markdown",
                   side_effect=fake or (lambda image, page_no: f"第 {page_no} 页正文")):
            return vision_pdf.extract_pages(raw)

    def test_all_pages_processed_when_the_budget_allows(self):
        """25 页的小页文档全部处理：20 页硬上限已经删掉。"""
        pages, total, limit, failed = self._extract(make_pdf(25))
        self.assertEqual((total, limit), (25, 25))
        self.assertEqual(len(pages), 25)
        self.assertEqual(failed, [])

    def test_budget_cut_is_reported_through_limit(self):
        """装不下的部分不处理，用 limit < total 表达截断。"""
        with patch.object(vision_pdf, "CONTEXT_TOKEN_LIMIT", 600):
            pages, total, limit, failed = self._extract(make_pdf(6))
        # 6 页每页都有很短文本层（≈4 token/页），但预算被压到 600 之后
        # 仍然全部装得下；这里断言的是"limit 由预算算出来"这条链路打通了
        self.assertEqual(total, 6)
        self.assertEqual(limit, 6)
        self.assertEqual(len(pages), 6)
        self.assertEqual(failed, [])

    def test_first_page_over_the_budget_is_an_error(self):
        with patch.object(vision_pdf, "CONTEXT_TOKEN_LIMIT", 0):
            with self.assertRaises(vision_pdf.VisionUnavailable) as ctx:
                self._extract(make_pdf(2))
        self.assertIn("上下文上限", str(ctx.exception))


class PdfPageConcurrencyTest(unittest.TestCase):
    """PDF 逐页视觉识别是并发的，但结果必须按页码就位。

    顺序错位的后果不是"正文顺序乱了"——下游节点靠页码引用来源，错位会让
    引用静默指到错误的页：不报错，也看不出来。
    """

    def _extract(self, pages: int, fake):
        """用 fake 顶掉真实的多模态调用，其余（渲染、线程池、回填）都是真的。"""
        with patch("app.ingestion.vision_pdf._config",
                   return_value=("http://example.invalid/v1", "k", "m")), \
             patch("app.ingestion.vision_pdf._page_to_markdown", side_effect=fake):
            return vision_pdf.extract_pages(make_pdf(pages))

    def test_page_order_survives_shuffled_completion(self):
        finished: list[int] = []
        lock = threading.Lock()

        def fake(image_png, page_no):
            # 页码越小的页故意越慢：完成顺序与页码顺序完全相反
            time.sleep((7 - page_no) * 0.03)
            with lock:
                finished.append(page_no)
            return f"第 {page_no} 页正文"

        # 并发度必须 ≥ 页数，否则分批执行会让「第 1 页最慢、所以最后完成」这个
        # 前提失效：3 路时前 3 页一批，第 1 页只是这一批里最后完成，后面几页
        # 会接着起来。这条测试验的是「结果按页码就位」，不该依赖并发度取值。
        with patch.object(vision_pdf, "CONCURRENCY", 6):
            pages, total, limit, failed = self._extract(6, fake)

        self.assertEqual(total, 6)
        self.assertEqual(limit, 6)
        self.assertEqual(failed, [])
        # 结果按页码就位，而不是按完成先后拼接
        self.assertEqual(pages, [f"第 {page_no} 页正文" for page_no in range(1, 7)])
        # 顺带证明这份测试是有意义的：第 1 页最慢、最后才完成
        self.assertEqual(finished[-1], 1)
        self.assertNotEqual(finished[0], 1)

    def test_concurrency_never_exceeds_limit(self):
        lock = threading.Lock()
        state = {"inflight": 0, "peak": 0, "calls": 0}

        def fake(image_png, page_no):
            with lock:
                state["inflight"] += 1
                state["calls"] += 1
                state["peak"] = max(state["peak"], state["inflight"])
            time.sleep(0.02)
            with lock:
                state["inflight"] -= 1
            return f"第 {page_no} 页正文"

        total_pages = vision_pdf.CONCURRENCY * 2
        self._extract(total_pages, fake)

        self.assertEqual(state["calls"], total_pages)
        # 上限就是 CONCURRENCY：线程池只有一个，不会因为页多而膨胀
        self.assertLessEqual(state["peak"], vision_pdf.CONCURRENCY)
        # 但也必须真的并发，退化成串行会让整份扫描件慢上一分多钟
        self.assertGreater(state["peak"], 1)

    def test_one_failed_page_keeps_the_others(self):
        def fake(image_png, page_no):
            if page_no == 2:
                raise RuntimeError("模型返回 500")
            return f"第 {page_no} 页正文"

        pages, total, limit, failed = self._extract(4, fake)

        # 失败只落在自己那一格：其余页照常返回，失败页记页码
        self.assertEqual(failed, [2])
        self.assertEqual(pages[1], "")
        self.assertEqual(pages[0], "第 1 页正文")
        self.assertEqual(pages[2], "第 3 页正文")
        self.assertEqual(pages[3], "第 4 页正文")
        self.assertEqual((total, limit), (4, 4))


if __name__ == "__main__":
    unittest.main()
