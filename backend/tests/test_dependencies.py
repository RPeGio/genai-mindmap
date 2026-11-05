"""依赖清单的完整性守卫。

为什么值得单独测：`.xls` 支持上线时漏了 `xlrd`，本机因为 Docling 的依赖链
顺手装过它所以跑得好好的，按 requirements.txt 新建环境才会缺。这类问题
在本地永远复现不出来，只能靠断言清单本身。
"""
import unittest
from pathlib import Path

REQUIREMENTS = Path(__file__).resolve().parents[1] / "requirements.txt"

# 产线代码在每个请求路径上都会用到的包。加新依赖时同步更新这里。
REQUIRED = {
    "docling": "DOCX/PPTX/XLSX/HTML 解析",
    "fastapi": "Web 框架",
    "httpx": "模型调用与语音识别请求",
    "pydantic": "契约模型",
    "pymupdf": "PDF 文本层与页面渲染",
    "python-multipart": "multipart 上传（缺它时 FastAPI 起不来）",
    "uvicorn": "ASGI 服务器",
    "xlrd": ".xls 单元格读取",
}


class RequirementsTest(unittest.TestCase):
    def setUp(self):
        self.text = REQUIREMENTS.read_text(encoding="utf-8")

    def test_all_runtime_dependencies_declared(self):
        lowered = self.text.lower()
        for package, why in REQUIRED.items():
            with self.subTest(package=package):
                self.assertIn(
                    package, lowered,
                    f"requirements.txt 缺少 {package}（用于{why}）；"
                    f"本机可能被别的包带装了，干净环境会缺")

    def test_libreoffice_is_documented_as_system_dependency(self):
        """LibreOffice 不是 pip 包，必须指向部署文档，否则部署时没人知道要装。"""
        self.assertIn("libreoffice", self.text.lower())

    def test_no_pinned_absolute_versions(self):
        """允许上下界，但不允许 == 锁死——依赖冲突时无法解析。"""
        offenders = [
            line for line in self.text.splitlines()
            if "==" in line and not line.strip().startswith("#")
        ]
        self.assertEqual(offenders, [], f"出现绝对版本锁定：{offenders}")


if __name__ == "__main__":
    unittest.main()
