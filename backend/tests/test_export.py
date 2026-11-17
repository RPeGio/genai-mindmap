"""导出格式的纯映射（F20）。四种都不联网、不读盘。

重点盯三件事：层级是否正确、标签里的特殊字符会不会把输出弄成非法格式，以及
**新增 JSON 出口之后三种文本格式的输出有没有变**（`ByteIdenticalTest`）。后一条
是"能导出什么就能导入什么"的护栏：`app/importing/` 要读得回这四种形状，任何对
导出形状的顺手改动都会让闭环断裂，而断裂的表现是"用户导出改一改就丢东西"——
那种 bug 只有逐字节用例守得住。

模型生成的标签是自由文本，出现引号、尖括号、星号、换行都完全可能。
"""
import json
import unittest
import xml.etree.ElementTree as ET

from app.contracts.mindmap import MindMapContent
from app.export.service import to_json, to_markdown, to_opml, to_puml


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
        # 五行：`@startmindmap`、图型注释、根、一个子节点、`@endmindmap`。
        # 注释行也必须是**一行**——它带着图型名，换行会把文件头撑成两行注释。
        self.assertEqual(len([line for line in puml.split("\n") if line.strip()]), 5)

        md = to_markdown(content)
        self.assertEqual(md.count("- 第一行 第二行"), 1)   # 没有拆成两个列表项
        self.assertIn("- 带 换行", md)

        ET.fromstring(to_opml(content))                 # 换行不该破坏 XML


class NoteTextTest(unittest.TestCase):
    """v2 之后节点有两个文字字段：`label` 是短标题，`note` 是正文。

    导出是给别人看的，只拿标题会得到一份没有解释的骨架，所以正文优先。
    """

    def build_with_note(self):
        return MindMapContent.model_validate({
            "title": "机器学习",
            "root": {"id": "root", "label": "机器学习", "note": "研究如何让计算机从数据里学规律。",
                     "children": [
                         {"id": "a", "label": "监督学习", "note": "给的是带标签的数据。"},
                         {"id": "b", "label": "无监督学习"},
                     ]},
        })

    def test_note_wins_over_label_in_all_formats(self):
        content = self.build_with_note()
        self.assertIn("* 研究如何让计算机从数据里学规律。", to_puml(content))
        self.assertIn("- 研究如何让计算机从数据里学规律。", to_markdown(content))
        root = ET.fromstring(to_opml(content))
        self.assertEqual(root.find("./body/outline").get("text"), "研究如何让计算机从数据里学规律。")
        self.assertIn("给的是带标签的数据。", to_markdown(content))

    def test_label_is_used_when_note_is_missing(self):
        content = self.build_with_note()
        self.assertIn("- 无监督学习", to_markdown(content))
        self.assertNotIn("无监督学习 给的是", to_markdown(content))

    def test_multiline_note_stays_on_one_line(self):
        content = MindMapContent.model_validate({
            "title": "t", "root": {"id": "r", "label": "标题", "note": "第一行\n第二行", "children": []},
        })
        self.assertIn("- 第一行 第二行", to_markdown(content))
        ET.fromstring(to_opml(content))


class ByteIdenticalTest(unittest.TestCase):
    """三种既有格式的输出**逐字节**钉死。

    加 JSON 导出（`to_json`）是为导入闭环补齐的最后一块，但它坐在
    `export/service.py` 里——同一个文件还放着另外三个函数。这条用例的作用是把
    "新增一个出口"与"改动旧出口"分开：任何对 markdown / opml / puml 的意外改动
    （哪怕只是多一个空格）都会在这里红，而不是等用户发现导出的文件与上次不同。
    """

    def test_markdown_is_byte_identical(self):
        self.assertEqual(
            to_markdown(SAMPLE),
            "# 季度规划\n\n- 产品\n  - 市场\n    - 竞品调研\n    - 用户访谈\n"
            "  - 技术\n    - 架构设计\n      - 存储选型\n")

    def test_opml_is_byte_identical(self):
        self.assertEqual(
            to_opml(SAMPLE),
            '<?xml version="1.0" encoding="UTF-8"?>\n<opml version="2.0">\n  <head>\n'
            "    <title>季度规划</title>\n  </head>\n  <body>\n"
            '    <outline text="产品">\n      <outline text="市场">\n'
            '        <outline text="竞品调研">\n        </outline>\n'
            '        <outline text="用户访谈">\n        </outline>\n      </outline>\n'
            '      <outline text="技术">\n        <outline text="架构设计">\n'
            '          <outline text="存储选型">\n          </outline>\n'
            "        </outline>\n      </outline>\n    </outline>\n  </body>\n</opml>\n")

    def test_puml_is_byte_identical(self):
        self.assertEqual(
            to_puml(SAMPLE),
            "@startmindmap\n' 图型：思维导图（tree）\n* 产品\n** 市场\n*** 竞品调研\n"
            "*** 用户访谈\n** 技术\n*** 架构设计\n**** 存储选型\n@endmindmap\n")


class JsonExportTest(unittest.TestCase):
    """JSON 是导入侧的保底：它必须能原样再导入。"""

    def test_output_round_trips_into_the_same_tree(self):
        from app.importing.service import parse_import

        text = to_json(SAMPLE)
        restored = parse_import("map.json", text.encode("utf-8")).content

        def shape(node):
            return (node.label, [shape(child) for child in node.children])

        self.assertEqual(shape(restored.root), shape(SAMPLE.root))
        self.assertEqual(restored.title, SAMPLE.title)

    def test_json_keeps_fields_the_text_formats_lose(self):
        """正文、颜色、图型、边：三种文本格式都只有一个文字槽位，JSON 全保住。"""
        content = MindMapContent.model_validate({
            "title": "t", "diagram_type": "flowchart",
            "root": {"id": "r", "label": "标题", "note": "正文",
                     "children": [{"id": "a", "label": "甲", "color": "red"},
                                  {"id": "b", "label": "乙"}]},
            "edges": [{"id": "e", "source": "a", "target": "b", "label": "是"}],
        })
        payload = json.loads(to_json(content))
        self.assertEqual(payload["root"]["note"], "正文")
        self.assertEqual(payload["root"]["children"][0]["color"], "red")
        self.assertEqual(payload["diagram_type"], "flowchart")
        self.assertEqual(payload["edges"][0]["label"], "是")
        # 中文不转义：这份文件是给人改的
        self.assertIn("正文", to_json(content))

    def test_json_is_not_affected_by_text_escaping(self):
        """尖括号与星号在 JSON 里原样保留（markdown/puml 必须转义它们）。"""
        content = MindMapContent.model_validate({
            "title": "t", "root": {"id": "r", "label": "Map<K,V> *态", "children": []}})
        payload = json.loads(to_json(content))
        self.assertEqual(payload["root"]["label"], "Map<K,V> *态")


if __name__ == "__main__":
    unittest.main()
