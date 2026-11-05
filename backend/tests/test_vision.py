"""图片输入（F06）：与 PDF 逐页共用同一条多模态调用。

这里不联网。真实识别效果由手工端到端验证覆盖，测试锁住的是格式判定、
配置预检、以及失败时必须是报错而不是空正文。
"""
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


if __name__ == "__main__":
    unittest.main()
