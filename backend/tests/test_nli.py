"""NLI 第三层的单元测试。默认关闭，所有用例都不依赖真实模型文件。"""
import os
import unittest
from unittest.mock import patch

from app.contracts.mindmap import MindMapContent
from app.contracts.source import Source, SourceKind
from app.optimization import nli
from app.optimization.source_refs import annotate_nli


def build_content(refs):
    # source_refs 里的 source_id 必须在 source_usage 中声明，这是契约层的
    # 第二层约束（保存和读取共用），测试数据得跟着满足。
    return MindMapContent.model_validate({
        "title": "测试",
        "source_usage": [{"source_id": "s1", "kind": "document",
                          "filename": "论文.pdf", "char_count": 20}],
        "root": {
            "id": "root", "label": "根节点",
            "children": [{
                "id": "n1", "label": "大规模数据验证",
                "children": [], "source_refs": refs,
            }],
        },
    })


def build_source(text):
    return Source(kind=SourceKind.document, filename="论文.pdf", text=text,
                  status="parsed")


class DisabledTest(unittest.TestCase):
    """默认关闭时必须是零副作用：不 import 重依赖，也不产生诊断。"""

    def setUp(self):
        nli.reset()

    def tearDown(self):
        nli.reset()

    def test_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MINDMAP_NLI_ENABLED", None)
            self.assertFalse(nli.available())
            self.assertIsNone(nli.judge("claim", "evidence"))

    def test_disabled_produces_no_diagnostics(self):
        content = build_content([{"source_id": "s1", "quote": "任意引文"}])
        source = build_source("任意引文出现在这里")
        source.id = "s1"
        with patch.dict(os.environ, {"MINDMAP_NLI_ENABLED": "0"}, clear=False):
            self.assertEqual(annotate_nli(content, [source]), [])

    def test_missing_model_dir_is_not_an_error(self):
        with patch.dict(os.environ, {"MINDMAP_NLI_ENABLED": "1",
                                     "MINDMAP_NLI_MODEL_DIR": ""}, clear=False):
            self.assertFalse(nli.available())
            self.assertIsNone(nli.judge("a", "b"))

    def test_broken_model_dir_is_not_an_error(self):
        with patch.dict(os.environ, {"MINDMAP_NLI_ENABLED": "1",
                                     "MINDMAP_NLI_MODEL_DIR": r"D:\no\such\dir"},
                        clear=False):
            self.assertFalse(nli.available())   # 不抛异常


class AnnotateTest(unittest.TestCase):
    """启用后用替身替换模型，验证诊断的产出规则。"""

    def setUp(self):
        nli.reset()

    def tearDown(self):
        nli.reset()

    def _run(self, verdict, refs, text="原文里的一句话"):
        content = build_content(refs)
        source = build_source(text)
        source.id = "s1"
        with patch("app.optimization.nli.available", return_value=True), \
             patch("app.optimization.nli.judge", return_value=verdict):
            return annotate_nli(content, [source])

    def test_contradiction_becomes_warning(self):
        diagnostics = self._run(
            {"score": 0.83, "label": "contradiction", "probs": {}},
            [{"source_id": "s1", "quote": "原文里的一句话"}])
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0].code, "SOURCE_REF_CONTRADICTED")
        self.assertEqual(diagnostics[0].level.value, "warn")

    def test_entailment_is_informational(self):
        diagnostics = self._run(
            {"score": 0.95, "label": "entailment", "probs": {}},
            [{"source_id": "s1", "quote": "原文里的一句话"}])
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0].code, "SOURCE_REF_NLI")
        self.assertEqual(diagnostics[0].level.value, "info")

    def test_ref_without_quote_is_skipped(self):
        """没有引文就没有可比对的文本，不该白跑一次推理。"""
        diagnostics = self._run(
            {"score": 0.9, "label": "entailment", "probs": {}},
            [{"source_id": "s1"}])
        self.assertEqual(diagnostics, [])

    def test_judge_returning_none_is_tolerated(self):
        """推理失败只是没有评分，不能影响主流程。"""
        diagnostics = self._run(None, [{"source_id": "s1", "quote": "原文里的一句话"}])
        self.assertEqual(diagnostics, [])

    def test_cite_is_never_modified(self):
        """第三层的硬约束：绝不改变 source_refs 的有无。"""
        content = build_content([{"source_id": "s1", "quote": "原文里的一句话"}])
        source = build_source("原文里的一句话")
        source.id = "s1"
        before = len(content.root.children[0].source_refs)
        with patch("app.optimization.nli.available", return_value=True), \
             patch("app.optimization.nli.judge",
                   return_value={"score": 0.9, "label": "contradiction", "probs": {}}):
            annotate_nli(content, [source])
        self.assertEqual(len(content.root.children[0].source_refs), before)


if __name__ == "__main__":
    unittest.main()
