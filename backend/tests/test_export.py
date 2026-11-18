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


class TwoLayerExportTest(unittest.TestCase):
    """流程图与鱼骨图也要能导出，而且**阶段内部的边不能丢**。

    在此之前三个文本导出函数的入参标着 `MindMapContent` 却真的去读 `content.root`：
    一份 `FlowContent` 走到那里就是 `AttributeError`（接口 500）。这是同一处空洞的另
    一半——契约上早就有两种形状了，导出侧一处都没跟上。

    流程图还有第二个问题：它的边分成**两个数组、两个命名空间**（`content.edges` 的
    端点是阶段 id，`stages[i].edges` 的端点是该阶段的节点 id）。只导一处的话，
    判定节点的「是 / 否」在导出文件里一条都不剩，而"图上明明有两条线"是用户会
    立刻发现的那种缺失。
    """

    def flow(self):
        from app.contracts.mindmap import FlowContent
        return FlowContent.model_validate({
            "title": "报销流程",
            "diagram_type": "flowchart",
            "stages": [
                {"id": "s_1", "root": {"id": "n1", "label": "提交申请", "children": [
                    {"id": "n11", "label": "填单", "children": []},
                    {"id": "n12", "label": "审不审", "children": []},
                ]}, "edges": [
                    {"id": "e_i1", "source": "n12", "target": "n11", "label": "否",
                     "kind": "branch"},
                ]},
                {"id": "s_2", "root": {"id": "n2", "label": "付款", "children": []}},
            ],
            "edges": [{"id": "e_1", "source": "s_1", "target": "s_2", "label": "通过"}],
        })

    def fishbone(self):
        from app.contracts.mindmap import FishboneContent
        return FishboneContent.model_validate({
            "title": "报销慢的原因分析",
            "diagram_type": "fishbone",
            "problem": "为什么报销要 11 天",
            "bones": [
                {"category": "人", "root": {"id": "b1", "label": "人", "children": [
                    {"id": "c1", "label": "审批的人不在", "children": []}]}},
                {"category": "机", "root": {"id": "b2", "label": "机", "children": []}},
            ],
        })

    # ---- 流程图 ----

    def test_flow_markdown_keeps_both_edge_layers(self):
        text = to_markdown(self.flow())
        self.assertTrue(text.startswith("# 报销流程"))
        self.assertIn("### 1. 提交申请", text)
        # 阶段之间的边：端点换成阶段名，读得懂"谁跳到谁"
        self.assertIn("## 附加关系（阶段之间）", text)
        self.assertIn("- 1. 提交申请 → 2. 付款（通过）", text)
        # 阶段内部的边：端点用节点标题，行首带阶段名（否则两条 n1→n12 看起来一样）
        self.assertIn("## 附加关系（阶段内部）", text)
        self.assertIn("- [1. 提交申请] 审不审 → 填单（否）", text)

    def test_flow_puml_is_mindmap_syntax_with_edge_comments(self):
        text = to_puml(self.flow())
        self.assertTrue(text.startswith("@startmindmap"))
        self.assertTrue(text.rstrip().endswith("@endmindmap"))
        # 阶段按顺序排成分支，读得出先后
        self.assertIn("** 1. 提交申请", text)
        self.assertIn("*** 填单", text)
        # 两段边清单都是注释行：文件仍可解析（mindmap 语法里 `A --> B` 是语法错）
        self.assertIn("' 1. 提交申请 --> 2. 付款 : 通过", text)
        self.assertIn("' [1. 提交申请] 审不审 → 填单（否）", text)
        # 文件头必须说明"这不是 activity 图"——不写的话读的人会以为丢了判定分支
        self.assertIn("不是 PlantUML activity 图", text)

    def test_flow_opml_is_valid_xml_with_edge_outlines(self):
        root = ET.fromstring(to_opml(self.flow()))
        labels = [node.get("text") for node in root.findall(".//outline")]
        self.assertIn("1. 提交申请", labels)
        self.assertIn("填单", labels)
        self.assertIn("附加关系（阶段之间）", labels)
        self.assertIn("附加关系（阶段内部）", labels)
        self.assertIn("1. 提交申请 → 2. 付款（通过）", labels)
        self.assertIn("[1. 提交申请] 审不审 → 填单（否）", labels)

    def test_flow_without_inner_edges_has_no_inner_section(self):
        from app.contracts.mindmap import FlowContent
        content = FlowContent.model_validate({
            "title": "t", "diagram_type": "flowchart",
            "stages": [{"id": "s_1", "root": {"id": "n1", "label": "甲", "children": []}}],
        })
        self.assertNotIn("附加关系（阶段内部）", to_markdown(content))
        self.assertNotIn("附加关系", to_puml(content))

    # ---- 鱼骨图 ----

    def test_fishbone_exports_the_problem_as_the_head(self):
        content = self.fishbone()
        self.assertIn("* 为什么报销要 11 天", to_puml(content))
        self.assertIn("** 人", to_puml(content))
        md = to_markdown(content)
        self.assertIn("- 为什么报销要 11 天", md)
        self.assertIn("  - 审批的人不在", md)
        # 鱼骨图没有 `edges`，三种格式都不该冒出边清单
        self.assertNotIn("附加关系", md)

    def test_fishbone_opml_nesting_is_preserved(self):
        root = ET.fromstring(to_opml(self.fishbone()))
        head = root.find("./body/outline")
        self.assertEqual(head.get("text"), "为什么报销要 11 天")
        bones = [node.get("text") for node in head.findall("./outline")]
        self.assertEqual(bones, ["人", "机"])
        self.assertEqual([node.get("text") for node in head.findall("./outline/outline")],
                         ["审批的人不在"])


class DetailExportTest(unittest.TestCase):
    """`detail`（生成说明）在三种文本格式里各有一个落点。

    这一路以前是"三种文本格式全丢 detail，只有 JSON 保得住"。丢的不是装饰字段：
    `detail` 是生成的核心产物（实测平均 142 字），而 `note` 可能根本是空的——读者
    拿到的文件里，一个节点只剩一行标题。

    三种格式各用自己那条不破坏结构的通道，**都不进大纲层级**：引用块（markdown）、
    注释行（puml）、`_note` 属性（opml）。
    """

    def content(self):
        return MindMapContent.model_validate({
            "title": "报销",
            "root": {"id": "r", "label": "提交申请", "detail": "把报销单和发票拍照上传。",
                     "children": [{"id": "a", "label": "填单",
                                   "detail": "写清事由与金额。"}]},
        })

    def test_markdown_puts_detail_in_a_quote_under_the_node(self):
        lines = to_markdown(self.content()).splitlines()
        self.assertIn("- 提交申请", lines)
        self.assertEqual(lines[lines.index("- 提交申请") + 1],
                         "  > 把报销单和发票拍照上传。")
        self.assertIn("  - 填单", lines)
        self.assertIn("    > 写清事由与金额。", lines)

    def test_puml_puts_detail_in_a_comment(self):
        text = to_puml(self.content())
        self.assertIn("* 提交申请", text)
        self.assertIn("' 说明：把报销单和发票拍照上传。", text)
        self.assertIn("** 填单", text)
        # 注释行不破坏语法：文件仍然以 @endmindmap 收尾
        self.assertTrue(text.rstrip().endswith("@endmindmap"))

    def test_puml_detail_never_looks_like_an_edge(self):
        """导入侧把任何含 `-->` 的注释行当边读（`importing/puml.py` 的 `_EDGE`）。

        一句说明里写着箭头时，那句注释会凭空长出一条关系——画出来是一条指不到
        任何节点的线。所以导出就把箭头压成 `→`，与 `*` → `·` 同一条取舍。
        """
        content = MindMapContent.model_validate({
            "title": "t",
            "root": {"id": "r", "label": "根", "detail": "先 A --> B 再回 C",
                     "children": []},
        })
        comments = [line for line in to_puml(content).splitlines()
                    if line.startswith("' 说明：")]
        self.assertEqual(comments, ["' 说明：先 A → B 再回 C"])

    def test_opml_puts_detail_in_the_note_attribute(self):
        root = ET.fromstring(to_opml(self.content()))
        node = root.find("./body/outline")
        self.assertEqual(node.get("text"), "提交申请")
        self.assertEqual(node.get("_note"), "把报销单和发票拍照上传。")
        self.assertEqual(node.find("./outline").get("_note"), "写清事由与金额。")

    def test_formats_without_detail_do_not_grow_the_attribute(self):
        """没有 `detail` 的节点不写空 `_note`：一个空属性会让"没有备注"与
        "备注是空串"在文件里长得一样。"""
        root = ET.fromstring(to_opml(SAMPLE))
        self.assertIsNone(root.find("./body/outline").get("_note"))

    def test_flow_and_fishbone_nodes_carry_detail_too(self):
        from app.contracts.mindmap import FishboneContent, FlowContent
        flow = FlowContent.model_validate({
            "title": "流程", "diagram_type": "flowchart",
            "stages": [{"id": "s_1", "root": {
                "id": "n1", "label": "提交", "detail": "阶段自己的说明",
                "children": [{"id": "n11", "label": "填单", "detail": "步骤说明"}]}}],
        })
        text = to_markdown(flow)
        self.assertIn("> 阶段自己的说明", text)
        self.assertIn("  > 步骤说明", text)
        self.assertIn("' 说明：步骤说明", to_puml(flow))

        fishbone = FishboneContent.model_validate({
            "title": "鱼骨", "problem": "为什么慢",
            "bones": [{"category": "人", "root": {
                "id": "b1", "label": "人",
                "children": [{"id": "c1", "label": "关键人休假",
                              "detail": "两位主程同时不在"}]}}],
        })
        bones = to_markdown(fishbone)
        self.assertIn("    > 两位主程同时不在", bones)
        self.assertIn("' 说明：两位主程同时不在", to_puml(fishbone))
        self.assertIn('_note="两位主程同时不在"', to_opml(fishbone))


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
