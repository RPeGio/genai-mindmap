"""NLI 第三层的单元测试。默认关闭，所有用例都不依赖真实模型文件。"""
import os
import unittest
from unittest.mock import patch

from app.contracts.mindmap import MindMapContent, Node
from app.contracts.source import Source, SourceKind
from app.optimization import nli
from app.optimization.source_refs import CLAIM_JOINER, _claim_of, annotate_nli


def build_content(refs, *, label="大规模数据验证", detail=None):
    # source_refs 里的 source_id 必须在 source_usage 中声明，这是契约层的
    # 第二层约束（保存和读取共用），测试数据得跟着满足。
    node: dict = {
        "id": "n1", "label": label, "children": [], "source_refs": refs,
    }
    if detail is not None:
        node["detail"] = detail
    return MindMapContent.model_validate({
        "title": "测试",
        "source_usage": [{"source_id": "s1", "kind": "document",
                          "filename": "论文.pdf", "char_count": 20}],
        "root": {"id": "root", "label": "根节点", "children": [node]},
    })


def build_source(text):
    return Source(kind=SourceKind.document, filename="论文.pdf", text=text,
                  status="parsed")


def make_node(label: str, detail: str | None = None, *, note: str | None = None,
              raw_detail: str | None = None) -> Node:
    """造节点。`raw_detail` 绕过 validator 写入空白/超长文本——契约层会把纯空白
    折成 `None`、超长直接拒收，而这一层不该假设调用方一定过过 validator。"""
    node = Node(id="n1", label=label, detail=detail, note=note)
    if raw_detail is not None:
        node = Node.model_construct(id="n1", label=label, detail=raw_detail,
                                    note=note, children=[], source_refs=[],
                                    expandable="unknown", collapsed=False,
                                    style=None, pos=None)
    return node


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


class ClaimTest(unittest.TestCase):
    """喂给 NLI 的 hypothesis 是**论断**（`label` + `detail`），不是节点标题。

    这一层的判别力全在 claim 上：拿"预算审批流程"这种名词短语当 hypothesis，
    模型能回答的只有"这段引文提到这个短语了吗"——引文几乎不可能"否定"一个名词
    短语的存在，于是这一层只剩盖章。`detail` 才是可判真假的论断。
    """

    def test_claim_is_label_plus_detail(self):
        node = make_node("预算审批流程",
                         "五万元以内由财务经理核准，超过五十万元报总经理审批。")
        self.assertEqual(
            _claim_of(node),
            f"预算审批流程{CLAIM_JOINER}五万元以内由财务经理核准，超过五十万元报总经理审批。")

    def test_claim_without_detail_is_the_label(self):
        """一级分支与旧图没有 `detail`：退回 `label`，不崩，也不多一个分隔符。"""
        self.assertEqual(_claim_of(make_node("预算审批流程")), "预算审批流程")

    def test_blank_detail_is_ignored(self):
        """纯空白等于没写。契约的 validator 会折成 `None`，但这一层自己也要挡住。"""
        node = make_node("预算审批流程", raw_detail="  \n\t ")
        self.assertEqual(_claim_of(node), "预算审批流程")

    def test_label_ending_with_a_period_gets_no_second_joiner(self):
        node = make_node("谁可以批钱？", "五万元以内由财务经理核准。")
        self.assertEqual(_claim_of(node), "谁可以批钱？五万元以内由财务经理核准。")

    def test_long_detail_is_cut_at_a_sentence_boundary(self):
        """截断点落在句号上：半句论断在 NLI 上制造的是噪声，不是信息。"""
        sentences = [f"{char * 100}。" for char in "甲乙丙丁"]
        node = make_node("预算审批流程", "".join(sentences))
        claim = _claim_of(node)

        self.assertLessEqual(len(claim), nli.MAX_CLAIM_CHARS)
        self.assertTrue(claim.endswith("。"))
        self.assertNotIn("丁", claim)          # 第四句连一个字都不该进来
        self.assertEqual(claim.count("。"), 4)  # label 的分隔符 + 三整句

    def test_single_sentence_over_budget_falls_back_to_a_clause(self):
        """第一句本身超预算（detail 可能是一整段）：退到句内标点，而不是硬切。"""
        node = make_node("预算审批流程", f"{'甲' * 200}，{'乙' * 200}。")
        claim = _claim_of(node)

        self.assertLessEqual(len(claim), nli.MAX_CLAIM_CHARS)
        self.assertTrue(claim.endswith("，"))
        self.assertNotIn("乙", claim)

    def test_without_any_punctuation_it_hard_cuts(self):
        """连逗号都没有：只能按字符数切。这条钉的是"不抛异常、不超预算"。"""
        node = make_node("预算审批流程", "甲" * 900)
        claim = _claim_of(node)
        self.assertLessEqual(len(claim), nli.MAX_CLAIM_CHARS)
        self.assertTrue(claim.startswith("预算审批流程。"))

    def test_label_is_kept_whole_when_detail_is_cut(self):
        """`label` 全留：它被截断的话，人核那条 warn 时对不上是哪个节点。"""
        label = "标" * 100
        claim = _claim_of(make_node(label, "。".join(["内容" * 30] * 8)))
        self.assertTrue(claim.startswith(label))
        self.assertEqual(claim[len(label)], CLAIM_JOINER)

    def test_claim_never_exceeds_the_budget(self):
        """契约允许的极端输入：200 字 label + 2000 字 detail。"""
        node = make_node("长" * 200, "内" * 2000)
        self.assertLessEqual(len(_claim_of(node)), nli.MAX_CLAIM_CHARS)

    def test_note_is_not_part_of_the_claim(self):
        """`note` 是用户自己写的备注，不参与语义判定（也不该被模型评分）。"""
        node = make_node("预算审批流程", note="用户随手记的一句话")
        self.assertEqual(_claim_of(node), "预算审批流程")

    def test_claim_budget_stays_inside_the_token_window(self):
        """上限来自 512 token 的反推，不是随手给的数（依据见 `nli.MAX_CLAIM_CHARS`）。

        实测：引文 205–238 字 → 142–168 token；claim 340 字符 ≈ 300 token，两段
        合计 470 token 仍在 512 以内。再往上加就开始挤占 premise——tokenizer 的
        `longest_first` 会先截最长的序列，也就是**证据**，那比截论断更糟。
        """
        self.assertGreater(nli.MAX_CLAIM_CHARS, 200)   # 旧值只够一句半
        self.assertLessEqual(nli.MAX_CLAIM_CHARS, 341)


class AnnotateClaimTest(unittest.TestCase):
    """`annotate_nli` 真的把论断传下去了——不是只在构造函数里能拼。"""

    def setUp(self):
        nli.reset()

    def tearDown(self):
        nli.reset()

    def _run(self, content, *, verdict=None):
        source = build_source("原文里的一句话")
        source.id = "s1"
        seen: list[tuple[str, str]] = []

        def fake_judge(claim: str, evidence: str) -> dict | None:
            seen.append((claim, evidence))
            return verdict

        with patch("app.optimization.nli.available", return_value=True), \
                patch("app.optimization.nli.judge", new=fake_judge):
            diagnostics = annotate_nli(content, [source])
        return diagnostics, seen

    def test_judge_receives_label_plus_detail(self):
        content = build_content([{"source_id": "s1", "quote": "原文里的一句话"}],
                                label="大规模数据验证",
                                detail="实验只在小型数据集上验证，尚未覆盖大规模语料。")
        _, seen = self._run(
            content, verdict={"score": 0.9, "label": "contradiction", "probs": {}})

        self.assertEqual(len(seen), 1)
        claim, evidence = seen[0]
        self.assertEqual(claim, "大规模数据验证。实验只在小型数据集上验证，尚未覆盖大规模语料。")
        self.assertEqual(evidence, "原文里的一句话")

    def test_judge_receives_the_label_when_detail_is_missing(self):
        content = build_content([{"source_id": "s1", "quote": "原文里的一句话"}])
        _, seen = self._run(
            content, verdict={"score": 0.5, "label": "neutral", "probs": {}})
        self.assertEqual(seen[0][0], "大规模数据验证")


if __name__ == "__main__":
    unittest.main()
