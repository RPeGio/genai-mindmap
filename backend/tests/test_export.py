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


class ShapeMarkerTest(unittest.TestCase):
    """非树形状的 markdown 第二行有一行机器可读的形状标记。

    为什么它必须有：鱼骨/流程的 markdown 里，"这几条是骨"只体现为缩进，而缩进在
    导入侧同时可能是"这个节点的子节点"。从形状硬推断形状是编造——一份三级缩进
    大纲与一张两条原因的鱼骨图在文本上一模一样。标记让导入侧不必猜。

    为什么树没有：`# 标题` + 列表项就是树，没有第二种读法；不写标记，树的输出才能
    逐字节不变（`ByteIdenticalTest`）。
    """

    def fishbone(self):
        from app.contracts.mindmap import FishboneContent
        return FishboneContent.model_validate({
            "title": "报销慢的原因分析", "problem": "为什么报销要 11 天",
            "bones": [{"category": "人", "root": {"id": "b1", "label": "人"}}],
        })

    def flow(self):
        from app.contracts.mindmap import FlowContent
        return FlowContent.model_validate({
            "title": "报销流程", "diagram_type": "flowchart",
            "stages": [{"id": "s_1", "root": {"id": "n1", "label": "提交申请"}}],
        })

    def lines_of(self, content):
        return [line for line in to_markdown(content).splitlines() if line.strip()]

    def test_fishbone_marker_is_the_second_line(self):
        lines = self.lines_of(self.fishbone())
        self.assertEqual(lines[0], "# 报销慢的原因分析")
        self.assertEqual(lines[1], "<!-- genai-mindmap: shape=fishbone -->")

    def test_flow_marker_is_the_second_line(self):
        lines = self.lines_of(self.flow())
        self.assertEqual(lines[0], "# 报销流程")
        self.assertEqual(lines[1], "<!-- genai-mindmap: shape=flowchart -->")

    def test_tree_has_no_marker(self):
        """树不写标记——"没有标记"就是树，所以树的输出一个字节都没动。"""
        self.assertNotIn("genai-mindmap", to_markdown(SAMPLE))


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
        # 原因在 depth=2（pad 8 格），它的引用块比节点行深 2 格——精确断言，
        # 免得子串匹配在缩进改宽之后仍然"碰巧通过"。
        self.assertIn("          > 两位主程同时不在", bones)
        self.assertIn("' 说明：两位主程同时不在", to_puml(fishbone))
        self.assertIn('_note="两位主程同时不在"', to_opml(fishbone))


class FishboneIndentationTest(unittest.TestCase):
    """鱼头、骨、原因必须是三级不同的缩进，**每级 4 个空格**。

    早先骨与鱼头都是顶格列表项（`- 为什么…` 与 `- 人`），普通 Markdown 阅读器把
    它们画成**同一级**的七个要点——读起来不是"一条鱼"，是"七条并列笔记"。

    缩进改成 4 个空格是 codex 复审 P2-1：2 个空格只有 CommonMark 认，Python-Markdown
    按 `tab_length=4` 判层级，把鱼头与骨读成同级（同一份文件在两个解析器里是两张图）。
    4 个空格两边都认，实测 HTML 记在 `docs/IMPORT.md`。
    """

    def content(self):
        from app.contracts.mindmap import FishboneContent
        return FishboneContent.model_validate({
            "title": "报销慢的原因分析",
            "problem": "为什么报销要 11 天",
            "bones": [
                {"category": "人", "root": {"id": "b1", "label": "人", "children": [
                    {"id": "c1", "label": "审批的人不在"},
                    {"id": "c2", "label": "交接不完整", "children": [
                        {"id": "c21", "label": "没写清谁接手"}]},
                ]}},
                {"category": "机", "root": {"id": "b2", "label": "机"}},
            ],
        })

    def test_head_bone_and_cause_are_three_levels(self):
        lines = [line for line in to_markdown(self.content()).splitlines()
                 if line.strip() and not line.startswith(("#", "<!--"))]
        self.assertEqual(lines, [
            "- 为什么报销要 11 天",
            "    - 人",
            "        - 审批的人不在",
            "        - 交接不完整",
            "            - 没写清谁接手",
            "    - 机",
        ])

    def test_every_level_is_a_multiple_of_four_spaces(self):
        """判据是**每层 4 个空格**，不是"有这么几行"。

        2 个空格在 Python-Markdown 里读不出层级（`tab_length` 是 4），所以这条
        钉的不是"缩进是多少"，而是"缩进能被两个解析器都认出来"。漏改回 2 格时
        上一行对比那条仍会通过（骨确实比鱼头深），只有这条会红。
        """
        lines = [line for line in to_markdown(self.content()).splitlines()
                 if line.strip() and not line.startswith(("#", "<!--"))]
        widths = [len(line) - len(line.lstrip(" ")) for line in lines]
        self.assertEqual(widths, [0, 4, 8, 8, 12, 4])

    def test_every_bone_is_deeper_than_the_head(self):
        """判据是**每根骨都比鱼头深**，不是"有这么几行"：漏了缩进时上一行对比的
        结果就是那句"七个同级要点"。"""
        lines = [line for line in to_markdown(self.content()).splitlines()
                 if line.strip() and not line.startswith(("#", "<!--"))]
        head = next(line for line in lines if "为什么报销要 11 天" in line)
        bones = [line for line in lines if line.strip().endswith(("- 人", "- 机"))]
        self.assertTrue(bones)
        for bone in bones:
            self.assertGreater(len(bone) - len(bone.lstrip()), len(head) - len(head.lstrip()))


class FishboneBoneFieldsTest(unittest.TestCase):
    """骨的 `category` 与骨根 `note` **不能静默丢失**（codex 复审 P2-2）。

    这两个字段在 markdown 里没有文字槽：骨那一行装的是 `root.label`（画在骨上的
    那行字）。此前一次往返会把 `category=People`、`label=Staff`、另有 `note` 的骨
    变成 `category=Staff`、正文消失，**而且没有任何告警**——数据丢了还说不清。

    落点是骨行下面一行 HTML 注释（`export._markdown_bone_comment`）：渲染不可见、
    是标准语法、不会被 `- ` / `>` / heading 任一条解析规则误认。
    """

    def content(self, **bone_overrides):
        from app.contracts.mindmap import FishboneContent
        root = {"id": "b1", "label": "Staff", "children": [{"id": "c1", "label": "休假"}]}
        root.update(bone_overrides.pop("root", {}))
        bone = {"category": "People", "root": root}
        bone.update(bone_overrides)
        return FishboneContent.model_validate({
            "title": "交付延期", "problem": "为什么延期", "bones": [bone]})

    def test_category_and_note_are_written_where_import_can_read_them(self):
        lines = to_markdown(self.content(root={"note": "两位主程同时不在"})).splitlines()
        self.assertIn("    - Staff", lines)
        self.assertEqual(
            lines[lines.index("    - Staff") + 1],
            '      <!-- genai-mindmap: bone {"category": "People", '
            '"note": "两位主程同时不在"} -->')

    def test_bone_without_extra_fields_writes_no_comment(self):
        """`category` 与 `label` 相同、`note` 为空：输出里一行注释都不多。

        这不是省字节，是**不给既有文件制造噪声**：往返路径上格式多一行之后，
        用户下一次 `git diff` 会看到一份与上次不同的导出。
        """
        text = to_markdown(self.content(root={"label": "People"}))
        self.assertNotIn("genai-mindmap: bone", text)

    def test_comment_value_cannot_end_the_comment_early(self):
        """注释里出现 `-->` 会提前收尾，后半行变成正文、结构当场断掉。

        所以值里的 `>` 写成 JSON 的 `\\u003e`（两种写法在 JSON 里等价），
        `json.loads` 还原之后仍是 `>`。
        """
        text = to_markdown(self.content(root={"note": "先 A --> B"}))
        self.assertNotIn("--> B", text.split("genai-mindmap: bone")[1].split("\n")[0])
        payload = text.split("genai-mindmap: bone ")[1].split(" -->")[0]
        self.assertEqual(json.loads(payload),
                         {"category": "People", "note": "先 A --> B"})


class EdgeSuffixDelimiterTest(unittest.TestCase):
    """边标签里的括号不能把整条边弄丢（codex 复审 P2-3）。

    `_edge_suffix` 默认用全角圆括号；**只有它装不下时才换**（标签里已有圆括号、
    或圆括号配不平）。换一对之后导出保证分隔符不出现在标签里，导入从右端一找
    就是边界。
    """

    def flow(self, label, kind=None):
        from app.contracts.mindmap import FlowContent
        edge = {"id": "e1", "source": "s1", "target": "s2"}
        if label is not None:
            edge["label"] = label
        if kind is not None:
            edge["kind"] = kind
        return FlowContent.model_validate({
            "title": "流程",
            "stages": [{"id": "s1", "root": {"id": "n1", "label": "提交"}},
                       {"id": "s2", "root": {"id": "n2", "label": "审查"}}],
            "edges": [edge]})

    def suffix_of(self, label, kind=None):
        line = [row for row in to_markdown(self.flow(label, kind)).splitlines()
                if row.startswith("- ") and "→" in row][0]
        return line.split("→", 1)[1].strip()

    def test_balanced_parentheses_keep_the_default_delimiters(self):
        """`Rejected (missing file)` 的圆括号成对：默认格式照用，输出没变。"""
        self.assertEqual(self.suffix_of("Rejected (missing file)"),
                         "2. 审查（Rejected (missing file)）")

    def test_unbalanced_parentheses_switch_to_the_next_pair(self):
        self.assertEqual(self.suffix_of("smile :("), "2. 审查〔smile :(〕")
        self.assertEqual(self.suffix_of("O型）"), "2. 审查〔O型）〕")

    def test_full_width_parentheses_switch_too(self):
        self.assertEqual(self.suffix_of("驳回（材料不全）"), "2. 审查〔驳回（材料不全）〕")

    def test_kind_labels_never_need_the_second_pair(self):
        self.assertEqual(self.suffix_of(None, "loop"), "2. 审查（回退）")


class MarkdownEscapeTest(unittest.TestCase):
    """`&` 必须先转义，否则往返会改掉用户写的字（codex 复审 P2-4）。"""

    def test_ampersand_is_escaped_before_angle_brackets(self):
        content = MindMapContent.model_validate({
            "title": "t",
            "root": {"id": "r", "label": "根", "detail": "原文 &lt;token&gt; 与 a<b",
                     "children": []},
        })
        text = to_markdown(content)
        self.assertIn("&amp;lt;token&amp;gt;", text)
        self.assertIn("a&lt;b", text)
        # 渲染器看到的是**原文**，不是把 `&lt;` 变成 `<` 之后的字
        self.assertNotIn("> 原文 &lt;", text)

    def test_plain_tree_without_specials_is_unchanged(self):
        """没有 `&` / `<` / `>` 的图一个字节都没变（`ByteIdenticalTest` 的另一面）。"""
        self.assertNotIn("&amp;", to_markdown(SAMPLE))


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
