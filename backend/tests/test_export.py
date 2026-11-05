"""导出格式的纯映射（F20）。三种都不联网、不读盘。

重点盯两件事：层级是否正确，以及标签里的特殊字符会不会把输出弄成非法格式。
模型生成的标签是自由文本，出现引号、尖括号、星号、换行都完全可能。
"""
import unittest
import xml.etree.ElementTree as ET

from app.contracts.mindmap import MindMapContent
from app.export.service import to_markdown, to_opml, to_puml


def build(title="季度规划", root_label="产品", children=None):
    return MindMapContent.model_validate({
        "title": title,
        "root": {"id": "root", "label": root_label, "children": children or []},
    })


def child(node_id, label, children=None):
    return {"id": node_id, "label": label, "children": children or []}


SAMPLE = build(children=[
    child("m", "市场", [child("m1", "竞品调研"), child("m2", "用户访谈")]),
    child("t", "技术", [child("t1", "架构设计", [child("t1a", "存储选型")])]),
])


class PumlTest(unittest.TestCase):
    def test_depth_is_encoded_by_star_count(self):
        text = to_puml(SAMPLE)
        self.assertTrue(text.startswith("@startmindmap"))
        self.assertIn("* 产品", text)
        self.assertIn("** 市场", text)
        self.assertIn("**** 存储选型", text)
        self.assertTrue(text.rstrip().endswith("@endmindmap"))

    def test_asterisk_in_label_is_replaced(self):
        """星号在 mindmap 语法里是层级标记，留在正文会被解析成新节点。"""
        content = build(root_label="A*B")
        self.assertIn("·", to_puml(content))
        self.assertNotIn("* A*B", to_puml(content))


class MarkdownTest(unittest.TestCase):
    def test_title_and_indented_list(self):
        text = to_markdown(SAMPLE)
        self.assertTrue(text.startswith("# 季度规划"))
        self.assertIn("- 产品", text)
        self.assertIn("  - 市场", text)          # 第 1 层
        self.assertIn("    - 竞品调研", text)     # 第 2 层
        self.assertIn("      - 存储选型", text)   # 第 3 层，6 个空格

    def test_root_is_kept_when_title_differs(self):
        """title 与 root.label 允许不同，跳过根节点会让它的文字凭空消失。"""
        content = build(title="文档标题", root_label="根节点文字")
        text = to_markdown(content)
        self.assertIn("# 文档标题", text)
        self.assertIn("- 根节点文字", text)

    def test_angle_brackets_are_escaped(self):
        """不转义的话渲染器会把 <T> 当 HTML 标签吞掉，节点文字就残了。"""
        content = build(title="泛型 List<String>",
                        root_label="Map<K,V>",
                        children=[child("x", "a < b")])
        text = to_markdown(content)
        self.assertIn("List&lt;String&gt;", text)
        self.assertIn("Map&lt;K,V&gt;", text)
        self.assertIn("a &lt; b", text)
        self.assertNotIn("<T>", text)


class OpmlTest(unittest.TestCase):
    def test_output_is_valid_xml(self):
        root = ET.fromstring(to_opml(SAMPLE))
        self.assertEqual(root.tag, "opml")
        self.assertEqual(root.get("version"), "2.0")
        outlines = root.findall(".//outline")
        labels = [node.get("text") for node in outlines]
        self.assertIn("产品", labels)
        self.assertIn("存储选型", labels)

    def test_nesting_is_preserved(self):
        root = ET.fromstring(to_opml(SAMPLE))
        top = root.find("./body/outline")
        self.assertEqual(top.get("text"), "产品")
        first_branch = top.find("./outline")
        self.assertEqual(first_branch.get("text"), "市场")
        self.assertEqual([n.get("text") for n in first_branch.findall("./outline")],
                         ["竞品调研", "用户访谈"])

    def test_special_characters_are_escaped(self):
        """引号和尖括号不转义会产出非法 XML，导入端只报"格式错误"。"""
        content = build(title='标题 & "引号" <标签>',
                        root_label='含 "双引号" 与 <尖括号>',
                        children=[child("x", "a & b")])
        text = to_opml(content)
        root = ET.fromstring(text)   # 解析不过就是非法 XML
        self.assertEqual(root.find("./body/outline").get("text"),
                         '含 "双引号" 与 <尖括号>')
        self.assertEqual(root.find(".//head/title").text, '标题 & "引号" <标签>')


class NewlineTest(unittest.TestCase):
    def test_multiline_labels_fold_to_single_line(self):
        """标签里的换行会被折叠成空格，不能变成新的列表项或 PUML 节点。"""
        content = build(root_label="第一行\n第二行", children=[child("x", "带\n换行")])

        puml = to_puml(content)
        self.assertIn("* 第一行 第二行", puml)          # 仍在同一行
        self.assertEqual(len([line for line in puml.split("\n") if line.strip()]), 4)

        md = to_markdown(content)
        self.assertEqual(md.count("- 第一行 第二行"), 1)   # 没有拆成两个列表项
        self.assertIn("- 带 换行", md)

        ET.fromstring(to_opml(content))                 # 换行不该破坏 XML


if __name__ == "__main__":
    unittest.main()
