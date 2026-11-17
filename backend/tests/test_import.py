"""导入：四种（六种）格式、五件边界处理、与导出的闭环。

结构的组织方式是"每条断言对应一个真实的坑"，不是按函数分：

    - `DetectTest`          扩展名 + 嗅探的判据，以及两者冲突时谁赢
    - `*RoundTripTest`      每种格式：本仓库导出 → 导入 → 与原树等价（闭环核心）
    - `XmindTest`           新版 content.json、旧版 content.xml、zip 里没有 XMind 成员
    - `LimitTest`           1001 节点 / 9 层 / 51 子：截断 + 告警，**不整份拒绝**
    - `SourceRefTest`       引用被清空且有告警
    - `IdTest`              id 全部重新生成、边端点跟着重建
    - `ErrorTest`           未知格式 → unsupported_format；损坏文件 → parse_failed
    - `FreemindTest`        .mm 的 icon/color/richcontent/FOLDED 映射
    - `InterfaceTest`       ASGI 打一次 multipart，确认响应形状与"只解析不落库"

闭环用例里有一条刻意留下的**不**等价，写在这里以免下一个人以为是 bug：
markdown / opml / puml 只有一个文字槽位，导出取 `note or label`，所以带 `note`
的节点往返之后 `note` 变成 `label`（文字不丢、字段位置变）。`note` 转成 `label`
这件事由 `test_note_text_survives_as_label` 明确钉住。
"""
from __future__ import annotations

import json
import unittest
import zipfile
from io import BytesIO

from fastapi.testclient import TestClient

from app.contracts.mindmap import MindMapContent
from app.export.service import to_json, to_markdown, to_opml, to_puml
from app.importing.detect import detect_format
from app.importing.errors import ParseFailure, UnsupportedFormat
from app.importing.service import parse_import
from app.main import app


def build(**overrides) -> dict:
    """一棵有代表性但不大的树：多分支、多层、带颜色/图标/正文。"""
    data = {
        "title": "季度规划",
        "root": {
            "id": "root", "label": "产品",
            "children": [
                {"id": "m", "label": "市场", "children": [
                    {"id": "m1", "label": "竞品调研"},
                    {"id": "m2", "label": "用户访谈"},
                ]},
                {"id": "t", "label": "技术", "children": [
                    {"id": "t1", "label": "架构设计", "children": [
                        {"id": "t1a", "label": "存储选型"},
                    ]},
                ]},
            ],
        },
    }
    data.update(overrides)
    return data


def parse_text(name: str, text: str):
    return parse_import(name, text.encode("utf-8"))


def shape(node) -> tuple:
    """结构 + 文字。id 不参与比较（导入一定重新生成）。"""
    return (node.label, [shape(child) for child in node.children])


def text_shape(node) -> tuple:
    """导出文字口径的结构（`note or label`），用于 markdown/opml/puml 的往返。"""
    return (node.note or node.label, [text_shape(child) for child in node.children])


def names_of(root) -> dict[str, str]:
    """`id → 导出文字`，把边的端点翻译成人读得懂的名字。"""
    out: dict[str, str] = {}
    stack = [root]
    while stack:
        node = stack.pop()
        out[node.id] = node.note or node.label
        stack.extend(node.children)
    return out


def codes(warnings) -> list[str]:
    return [warning.code for warning in warnings]


class DetectTest(unittest.TestCase):
    def test_zip_magic_wins_over_json_suffix(self):
        """题目里的那条判据：`.json` 但有 zip 魔数 → 其实是 xmind。"""
        self.assertEqual(detect_format("导出.json", b"PK\x03\x04rest"), "xmind")

    def test_strong_content_beats_a_wrong_suffix(self):
        """强内容特征只可能来自那一种格式，扩展名写错也按内容读。"""
        self.assertEqual(
            detect_format("a.json", to_opml(MindMapContent.model_validate(build())).encode()),
            "opml")
        self.assertEqual(
            detect_format("a.md", '<map><node TEXT="x"/></map>'.encode()), "freemind")

    def test_weak_content_yields_to_the_suffix(self):
        """markdown 的嗅探是最弱一档：`.json` 里的 `- 项` 仍按 JSON 读并报错。

        反过来（让 markdown 赢）的后果更糟：用户以为自己给了一份 JSON，实际拿到
        一张按列表解析出来的图，而"这不是 JSON"这句真话永远不会出现。
        """
        self.assertEqual(detect_format("a.json", "- 项\n# 标题\n".encode()), "json")
        self.assertEqual(detect_format("a.puml", "* 根\n** 子\n".encode()), "puml")

    def test_unknown_returns_none(self):
        self.assertIsNone(detect_format("a.csv", b"a,b,c\n1,2,3\n"))
        self.assertIsNone(detect_format("", b"\xff\xfe\x00\x01garbage binary"))

    def test_txt_needs_outline_content(self):
        """`.txt` 只在内容也像大纲时才算 markdown——它是所有人的默认格式。"""
        self.assertEqual(detect_format("a.txt", "- 一\n  - 二\n".encode()), "markdown")
        self.assertIsNone(detect_format("a.txt", "今天开会讨论了三件事，没有结论。".encode()))

    def test_each_format_is_recognised_from_content(self):
        content = MindMapContent.model_validate(build())
        self.assertEqual(detect_format("x", to_json(content).encode()[:8192]), "json")
        self.assertEqual(detect_format("x", to_markdown(content).encode()[:8192]), "markdown")
        self.assertEqual(detect_format("x", to_opml(content).encode()[:8192]), "opml")
        self.assertEqual(detect_format("x", to_puml(content).encode()[:8192]), "puml")


class JsonRoundTripTest(unittest.TestCase):
    """JSON 是唯一无损的一条路：结构、视觉、图型、边全部保住。"""

    def rich(self) -> MindMapContent:
        return MindMapContent.model_validate(build(
            diagram_type="flowchart",
            theme={"preset": "slate", "direction": "left"},
            edges=[{"id": "e1", "source": "m1", "target": "t1", "label": "是",
                    "kind": "branch"}],
            root={
                "id": "root", "label": "产品", "note": "根备注", "detail": "根详情",
                "children": [
                    {"id": "m", "label": "市场", "color": "green", "icon": "🚀",
                     "collapsed": True, "children": [{"id": "m1", "label": "竞品调研"}]},
                    {"id": "t", "label": "技术", "children": [{"id": "t1", "label": "架构设计"}]},
                ],
            },
        ))

    def test_roundtrip_is_lossless(self):
        original = self.rich()
        decoded = parse_text("map.json", to_json(original))
        result = decoded.content

        self.assertEqual(decoded.source_format, "json")
        self.assertEqual(shape(result.root), shape(original.root))
        self.assertEqual(result.title, original.title)
        self.assertEqual(result.diagram_type, "flowchart")
        self.assertEqual(result.theme.direction, "left")
        self.assertEqual(len(result.edges), 1)
        names = names_of(result.root)
        self.assertEqual(names[result.edges[0].source], "竞品调研")
        self.assertEqual(names[result.edges[0].target], "架构设计")
        self.assertEqual(result.root.note, "根备注")
        self.assertEqual(result.root.detail, "根详情")

    def test_visual_fields_survive(self):
        result = parse_text("map.json", to_json(self.rich())).content
        branch = result.root.children[0]
        self.assertEqual(branch.color, "green")
        self.assertEqual(branch.icon, "🚀")
        self.assertTrue(branch.collapsed)

    def test_save_request_and_map_record_envelopes(self):
        """`{"content": …}`（保存请求体）与带 id/version 的库记录都要读得出来。"""
        body = build()
        for envelope in ({"content": body},
                         {"id": "abc", "version": 3, "content": body, "updated_at": "x"}):
            result = parse_text("saved.json", json.dumps(envelope, ensure_ascii=False)).content
            self.assertEqual(result.title, "季度规划")

    def test_layout_manual_with_positions_is_kept(self):
        body = build()
        body["layout"] = "manual"
        body["root"]["pos"] = {"x": 10, "y": 20}
        result = parse_text("manual.json", json.dumps(body, ensure_ascii=False))
        self.assertEqual(result.content.layout.value, "manual")
        self.assertEqual(result.content.root.pos.x, 10)

    def test_manual_layout_without_positions_is_downgraded(self):
        """契约要求 manual 至少一个坐标，否则整份 422——所以降级而不是失败。"""
        body = build()
        body["layout"] = "manual"
        result = parse_text("manual.json", json.dumps(body, ensure_ascii=False))
        self.assertEqual(result.content.layout.value, "tree")
        self.assertIn("fields_dropped", codes(result.warnings))

    def test_positions_are_dropped_when_layout_is_not_manual(self):
        body = build()
        body["root"]["pos"] = {"x": 1, "y": 2}
        result = parse_text("pos.json", json.dumps(body, ensure_ascii=False))
        self.assertIsNone(result.content.root.pos)
        self.assertIn("fields_dropped", codes(result.warnings))

    def test_relations_are_rebuilt_with_import_provenance(self):
        body = build()
        body["relations"] = [
            {"id": "r1", "source": "m1", "target": "t1", "type": "cause", "label": "导致"},
            {"id": "r2", "source": "m1", "target": "nowhere", "type": "cause"},
        ]
        result = parse_text("rel.json", json.dumps(body, ensure_ascii=False))
        self.assertEqual(len(result.content.relations), 1)
        relation = result.content.relations[0]
        self.assertEqual(relation.provenance, "import")
        self.assertEqual(names_of(result.content.root)[relation.source], "竞品调研")
        self.assertIn("relations_dropped", codes(result.warnings))


class MarkdownRoundTripTest(unittest.TestCase):
    def setUp(self):
        self.original = MindMapContent.model_validate(build())

    def test_tree_and_title_are_equivalent(self):
        decoded = parse_text("map.md", to_markdown(self.original))
        self.assertEqual(decoded.source_format, "markdown")
        self.assertEqual(shape(decoded.content.root), shape(self.original.root))
        self.assertEqual(decoded.content.title, "季度规划")
        self.assertEqual(decoded.warnings, [])

    def test_note_text_survives_as_label(self):
        """markdown 只有一个文字槽位：正文往返之后落在 `label` 里。

        文字没丢、位置变了——这不是 bug，是格式能力。`note` 为空是为了让"哪部分
        是标题"这个区分也可以被断言到（它确实丢了）。
        """
        original = MindMapContent.model_validate({
            "title": "机器学习",
            "root": {"id": "r", "label": "机器学习", "note": "研究如何让计算机学规律。",
                     "children": [{"id": "c", "label": "监督学习", "note": "带标签的数据。"}]},
        })
        result = parse_text("note.md", to_markdown(original)).content
        self.assertEqual(result.root.label, "研究如何让计算机学规律。")
        self.assertIsNone(result.root.note)
        self.assertEqual(result.root.children[0].label, "带标签的数据。")

    def test_edges_are_read_back(self):
        original = MindMapContent.model_validate(build(
            edges=[{"id": "e1", "source": "m1", "target": "t1", "label": "是"},
                   {"id": "e2", "source": "t1", "target": "m2", "kind": "loop"}]))
        result = parse_text("edges.md", to_markdown(original)).content
        self.assertEqual(len(result.edges), 2)
        names = names_of(result.root)
        pairs = {(names[edge.source], names[edge.target], edge.label, edge.kind)
                 for edge in result.edges}
        self.assertIn(("竞品调研", "架构设计", "是", None), pairs)
        # 没 label 的边导出成"回退"，导入要把它读回 `loop`
        self.assertIn(("架构设计", "用户访谈", "回退", "loop"), pairs)

    def test_four_space_indent_is_read_as_the_same_tree(self):
        """用户用编辑器重新缩进成 4 空格，层级不能变成两倍。"""
        text = "# 标题\n\n- 根\n    - 子\n        - 孙\n"
        result = parse_text("x.md", text).content
        self.assertEqual(shape(result.root), ("根", [("子", [("孙", [])])]))

    def test_checkbox_maps_to_visual_fields_and_leaves_label_clean(self):
        text = "# 待办\n\n- 发布\n  - [x] 写测试\n  - [ ] 补文档\n"
        result = parse_text("todo.md", text).content
        done = result.root.children[0]
        self.assertEqual(done.label, "写测试")
        self.assertEqual(done.icon, "✅")
        self.assertEqual(done.color, "green")
        todo = result.root.children[1]
        self.assertEqual(todo.label, "补文档")
        self.assertEqual(todo.color, "amber")

    def test_escaped_angle_brackets_are_restored(self):
        original = MindMapContent.model_validate({
            "title": "泛型", "root": {"id": "r", "label": "Map<K,V>", "children": []}})
        result = parse_text("g.md", to_markdown(original)).content
        self.assertEqual(result.root.label, "Map<K,V>")

    def test_code_fence_content_is_ignored(self):
        text = "# 标题\n\n- 根\n\n```\n- 这不是节点\n```\n"
        result = parse_text("f.md", text).content
        self.assertEqual(len(result.root.children), 0)

    def test_multiple_top_level_items_are_attached_under_root(self):
        text = "# 标题\n\n- 甲\n- 乙\n"
        result = parse_text("multi.md", text)
        self.assertEqual([child.label for child in result.content.root.children], ["乙"])
        self.assertIn("format_notice", codes(result.warnings))

    def test_no_list_items_is_a_parse_failure(self):
        with self.assertRaises(ParseFailure):
            parse_text("empty.md", "# 只有标题\n\n一段话。\n")


class OpmlRoundTripTest(unittest.TestCase):
    def test_tree_title_and_edges_roundtrip(self):
        original = MindMapContent.model_validate(build(
            edges=[{"id": "e1", "source": "m1", "target": "t1", "label": "驳回"},
                   {"id": "e2", "source": "t1", "target": "m2", "kind": "loop"}]))
        decoded = parse_text("map.opml", to_opml(original))
        self.assertEqual(decoded.source_format, "opml")
        self.assertEqual(shape(decoded.content.root), shape(original.root))
        self.assertEqual(decoded.content.title, "季度规划")
        names = names_of(decoded.content.root)
        pairs = {(names[edge.source], names[edge.target], edge.label, edge.kind)
                 for edge in decoded.content.edges}
        self.assertIn(("竞品调研", "架构设计", "驳回", None), pairs)
        self.assertIn(("架构设计", "用户访谈", "回退", "loop"), pairs)

    def test_edges_section_is_not_a_branch(self):
        original = MindMapContent.model_validate(build(
            edges=[{"id": "e1", "source": "m1", "target": "t1", "label": "是"}]))
        result = parse_text("map.opml", to_opml(original)).content
        labels = [node.label for node in result.root.children]
        self.assertNotIn("附加关系", labels)

    def test_multiple_top_level_outlines_are_attached(self):
        text = """<?xml version="1.0" encoding="UTF-8"?>
        <opml version="2.0"><head><title>t</title></head><body>
          <outline text="甲"/>
          <outline text="乙"><outline text="乙一"/></outline>
        </body></opml>"""
        result = parse_text("multi.opml", text)
        self.assertEqual(result.content.root.label, "甲")
        self.assertEqual([child.label for child in result.content.root.children], ["乙"])
        self.assertEqual(result.content.root.children[0].children[0].label, "乙一")
        self.assertIn("format_notice", codes(result.warnings))

    def test_doctype_is_refused(self):
        """实体递归能把 1KB 的文件撑成 GB 级内存；我们不产出 DOCTYPE，直接拒。"""
        text = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">]>'
                '<opml version="2.0"><body><outline text="&lol;"/></body></opml>')
        with self.assertRaises(ParseFailure):
            parse_text("bomb.opml", text)


class PumlRoundTripTest(unittest.TestCase):
    def test_diagram_type_is_read_from_the_header_comment(self):
        original = MindMapContent.model_validate(build(diagram_type="fishbone"))
        result = parse_text("map.puml", to_puml(original))
        self.assertEqual(result.source_format, "puml")
        self.assertEqual(result.content.diagram_type, "fishbone")

    def test_tree_is_equivalent(self):
        original = MindMapContent.model_validate(build())
        result = parse_text("map.puml", to_puml(original)).content
        self.assertEqual(shape(result.root), shape(original.root))

    def test_separator_comment_is_not_read_as_an_edge(self):
        original = MindMapContent.model_validate(build(
            edges=[{"id": "e1", "source": "m1", "target": "t1", "label": "是"}]))
        result = parse_text("map.puml", to_puml(original)).content
        self.assertEqual(len(result.edges), 1)
        self.assertEqual(result.edges[0].label, "是")

    def test_missing_start_marker_is_a_parse_failure(self):
        with self.assertRaises(ParseFailure):
            parse_text("x.puml", "* 根\n** 子\n")


def xmind_zip(sheets: list[dict]) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("content.json", json.dumps(sheets, ensure_ascii=False))
        archive.writestr("metadata.json", "{}")
    return buffer.getvalue()


def xmind_sheet(root_topic: dict, title: str = "画布 1") -> dict:
    return {"id": "sheet-1", "class": "sheet", "title": title, "rootTopic": root_topic}


class XmindTest(unittest.TestCase):
    def test_new_version_content_json(self):
        payload = xmind_zip([xmind_sheet({
            "id": "r", "title": "中心主题",
            "children": {"attached": [
                {"id": "a", "title": "分支甲", "children": {"attached": [
                    {"id": "a1", "title": "子甲一"}]}},
                {"id": "b", "title": "分支乙"},
            ]},
        })])
        result = parse_import("测试.xmind", payload)
        self.assertEqual(result.source_format, "xmind")
        self.assertEqual(shape(result.content.root),
                         ("中心主题", [("分支甲", [("子甲一", [])]), ("分支乙", [])]))
        self.assertEqual(result.content.title, "中心主题")

    def test_notes_style_markers_and_structure_class(self):
        payload = xmind_zip([xmind_sheet({
            "id": "r", "title": "根",
            "structureClass": "org.xmind.ui.logic.right",
            "children": {"attached": [
                {"id": "a", "title": "甲",
                 "notes": {"plain": {"content": "甲备注"}},
                 "markers": [{"markerId": "star-red"}],
                 "style": {"properties": {"svg:fill": "#FF0000"}}},
                {"id": "b", "title": "乙",
                 "style": {"properties": {"svg:fill": "#123456"}}},
            ]},
        }, title="我的画布")])
        result = parse_import("s.xmind", payload).content
        first, second = result.root.children
        self.assertEqual(first.note, "甲备注")
        self.assertEqual(first.color, "red")
        self.assertEqual(first.icon, "⭐")
        # 映射不到调色板的 hex 只留在 style.color（界面不渲染，但保存不丢）
        self.assertIsNone(second.color)
        self.assertEqual(second.style.color, "#123456")
        self.assertEqual(result.layout.value, "logic")

    def test_fishbone_structure_sets_diagram_type(self):
        payload = xmind_zip([xmind_sheet({
            "id": "r", "title": "鱼骨",
            "structureClass": "org.xmind.ui.fishbone.leftHeaded",
            "children": {"attached": [{"id": "a", "title": "人"}]},
        })])
        result = parse_import("f.xmind", payload).content
        self.assertEqual(result.diagram_type, "fishbone")
        self.assertEqual(result.layout.value, "fishbone")

    def test_multiple_sheets_only_the_first_is_imported(self):
        payload = xmind_zip([
            xmind_sheet({"id": "r1", "title": "第一张"}, title="A"),
            xmind_sheet({"id": "r2", "title": "第二张"}, title="B"),
        ])
        result = parse_import("two.xmind", payload)
        self.assertEqual(result.content.root.label, "第一张")
        self.assertIn("format_notice", codes(result.warnings))

    def test_detached_topics_are_dropped_with_a_notice(self):
        payload = xmind_zip([xmind_sheet({
            "id": "r", "title": "根",
            "children": {"attached": [{"id": "a", "title": "甲"}],
                         "detached": [{"id": "d", "title": "游离"}]},
        })])
        result = parse_import("d.xmind", payload)
        self.assertEqual(len(result.content.root.children), 1)
        self.assertIn("format_notice", codes(result.warnings))

    def test_old_version_content_xml(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <xmap-content xmlns="urn:xmind:xmap:xmlns:content:2.0" version="2.0">
          <sheet id="s"><title>旧画布</title>
            <topic id="r" structure-class="org.xmind.ui.org-chart.down">
              <title>旧中心</title>
              <children><topics type="attached">
                <topic id="a"><title>旧分支</title>
                  <notes><plain>旧备注</plain></notes>
                  <marker-refs><marker-ref marker-id="priority-2"/></marker-refs>
                  <children><topics type="attached">
                    <topic id="a1"><title>旧子</title></topic>
                  </topics></children>
                </topic>
              </topics></children>
            </topic>
          </sheet>
        </xmap-content>
        """
        result = parse_text("old.xmind", xml)
        self.assertEqual(result.source_format, "xmind")
        self.assertEqual(shape(result.content.root),
                         ("旧中心", [("旧分支", [("旧子", [])])]))
        self.assertEqual(result.content.root.children[0].note, "旧备注")
        self.assertEqual(result.content.layout.value, "vertical")
        self.assertEqual(result.content.title, "旧画布")

    def test_bare_content_xml_is_accepted(self):
        """用户从 `.xmind` 里解出单个 content.xml 也能导入。"""
        xml = ('<xmap-content xmlns="urn:xmind:xmap:xmlns:content:2.0">'
               '<sheet><topic><title>裸根</title></topic></sheet></xmap-content>')
        self.assertEqual(parse_text("content.xml", xml).content.root.label, "裸根")

    def test_zip_without_xmind_members_is_unsupported(self):
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("word/document.xml", "<w:document/>")
        with self.assertRaises(UnsupportedFormat):
            parse_import("fake.xmind", buffer.getvalue())

    def test_broken_zip_is_a_parse_failure(self):
        with self.assertRaises(ParseFailure):
            parse_import("bad.xmind", b"PK\x03\x04" + b"\x00" * 40)


class FreemindTest(unittest.TestCase):
    XML = """<map version="1.0.1">
      <node TEXT="根" COLOR="#00B050" FOLDED="true">
        <node TEXT="子">
          <icon BUILTIN="idea"/>
          <richcontent TYPE="NODE"><html><body><p>子备注</p></body></html></richcontent>
          <node TEXT="孙"/>
        </node>
        <node TEXT="未映射色" COLOR="#7f7f7f">
          <icon BUILTIN="不认识的图标"/>
        </node>
      </node>
    </map>"""

    def test_tree_visuals_and_note(self):
        result = parse_text("map.mm", self.XML)
        self.assertEqual(result.source_format, "freemind")
        self.assertEqual(shape(result.content.root),
                         ("根", [("子", [("孙", [])]), ("未映射色", [])]))
        root = result.content.root
        self.assertEqual(root.color, "green")
        self.assertTrue(root.collapsed)
        child = root.children[0]
        self.assertEqual(child.icon, "💡")
        self.assertEqual(child.note, "子备注")
        # 精确表里没有的色值：不猜，只留在 style.color
        unmapped = root.children[1]
        self.assertIsNone(unmapped.color)
        self.assertIsNone(unmapped.icon)
        self.assertEqual(unmapped.style.color, "#7f7f7f")

    def test_multiple_roots_are_attached_under_first(self):
        xml = '<map><node TEXT="甲"/><node TEXT="乙"><node TEXT="乙一"/></node></map>'
        result = parse_text("multi.mm", xml)
        self.assertEqual(result.content.root.label, "甲")
        self.assertEqual(result.content.root.children[0].label, "乙")
        self.assertEqual(result.content.root.children[0].children[0].label, "乙一")

    def test_missing_text_attribute_gets_a_placeholder(self):
        result = parse_text("blank.mm", '<map><node TEXT="根"><node/></node></map>')
        self.assertEqual(result.content.root.children[0].label, "（无标题）")
        self.assertIn("label_missing", codes(result.warnings))


class LimitTest(unittest.TestCase):
    def test_1001_nodes_are_truncated_by_level_order(self):
        """20 个分支 × 50 个孩子 = 1021 个节点（每个节点都没超 50 子）。"""
        root = {"id": "r", "label": "根", "children": [
            {"id": f"b{i}", "label": f"分支{i}", "children": [
                {"id": f"b{i}-{j}", "label": f"子{i}-{j}"} for j in range(50)]}
            for i in range(20)]}
        result = parse_text("big.json", json.dumps({"root": root}, ensure_ascii=False))
        self.assertEqual(len(result.content.root.children), 20)
        kept = 1 + sum(1 + len(branch.children) for branch in result.content.root.children)
        self.assertEqual(kept, 1000)
        self.assertIn("truncated_nodes", codes(result.warnings))
        # BFS：每个一级分支都留下，被裁掉的是各支最深处的子节点
        self.assertTrue(all(branch.children for branch in result.content.root.children))

    def test_nine_levels_are_cut_at_eight(self):
        level: dict = {"id": "n9", "label": "第9层"}
        for depth in range(8, 0, -1):
            level = {"id": f"n{depth}", "label": f"第{depth}层", "children": [level]}
        result = parse_text("deep.json", json.dumps({"root": level}, ensure_ascii=False))
        self.assertIn("truncated_depth", codes(result.warnings))

        deepest = 0
        stack = [(result.content.root, 1)]
        while stack:
            node, depth = stack.pop()
            deepest = max(deepest, depth)
            stack.extend((child, depth + 1) for child in node.children)
        self.assertEqual(deepest, 8)

    def test_51_children_are_cut_to_50(self):
        root = {"id": "r", "label": "根", "children": [
            {"id": f"k{i}", "label": f"子{i}"} for i in range(51)]}
        result = parse_text("wide.json", json.dumps({"root": root}, ensure_ascii=False))
        self.assertEqual(len(result.content.root.children), 50)
        self.assertIn("truncated_children", codes(result.warnings))

    def test_truncation_never_rejects_the_whole_file(self):
        """题目里那条要求：多几个节点也要给一张图，而不是什么都不给。"""
        root = {"id": "r", "label": "根", "children": [
            {"id": f"k{i}", "label": f"子{i}"} for i in range(3000)]}
        result = parse_text("huge.json", json.dumps({"root": root}, ensure_ascii=False))
        self.assertGreater(len(result.content.root.children), 0)
        self.assertTrue(result.warnings)

    def test_long_labels_are_kept_up_to_200_not_16(self):
        """导入不套用生成侧的 16 字上限——用户手写的长标题不能被截。"""
        long_label = "这是一个用户手写的、明显超过十六个字但仍然完整的节点标题"
        result = parse_text("long.json", json.dumps(
            {"root": {"id": "r", "label": long_label}}, ensure_ascii=False))
        self.assertEqual(result.content.root.label, long_label)
        self.assertNotIn("label_truncated", codes(result.warnings))

    def test_labels_over_200_are_clipped_with_a_warning(self):
        result = parse_text("long.json", json.dumps(
            {"root": {"id": "r", "label": "长" * 260}}, ensure_ascii=False))
        self.assertEqual(len(result.content.root.label), 200)
        self.assertIn("label_truncated", codes(result.warnings))


class SourceRefTest(unittest.TestCase):
    def test_refs_and_usage_are_cleared_with_a_warning(self):
        body = {
            "title": "带引用",
            "source_usage": [{"source_id": "s1", "kind": "text",
                              "filename": None, "char_count": 10}],
            "root": {"id": "r", "label": "根",
                     "source_refs": [{"source_id": "s1", "quote": "abc"}],
                     "children": [{"id": "c", "label": "子",
                                   "source_refs": [{"source_id": "s1",
                                                    "start": 0, "end": 3}]}]},
        }
        result = parse_text("refs.json", json.dumps(body, ensure_ascii=False))
        self.assertEqual(result.content.root.source_refs, [])
        self.assertEqual(result.content.root.children[0].source_refs, [])
        self.assertEqual(result.content.source_usage, [])
        self.assertIn("source_refs_cleared", codes(result.warnings))

    def test_content_still_validates_without_refs(self):
        """清空之后 `validate_tree` 不会因为"引用了没有 source_usage 的来源"而炸。"""
        body = build()
        body["root"]["source_refs"] = [{"source_id": "外部来源", "quote": "x"}]
        self.assertIsNotNone(parse_text("r.json", json.dumps(body)).content)


class IdTest(unittest.TestCase):
    def test_all_ids_are_regenerated(self):
        original = MindMapContent.model_validate(build())
        result = parse_text("map.json", to_json(original)).content
        old_ids = set(names_of(original.root))
        new_ids = set(names_of(result.root))
        self.assertTrue(old_ids.isdisjoint(new_ids))
        self.assertEqual(len(new_ids), len(old_ids))

    def test_edges_follow_the_new_ids(self):
        original = MindMapContent.model_validate(build(
            edges=[{"id": "e1", "source": "m1", "target": "t1", "label": "是"}]))
        result = parse_text("map.json", to_json(original)).content
        edge = result.edges[0]
        node_ids = set(names_of(result.root))
        self.assertIn(edge.source, node_ids)
        self.assertIn(edge.target, node_ids)
        self.assertNotEqual(edge.id, "e1")
        names = names_of(result.root)
        self.assertEqual((names[edge.source], names[edge.target]), ("竞品调研", "架构设计"))

    def test_edge_to_a_cut_node_is_dropped(self):
        """端点被限额裁掉时那条边必须消失：dangling 会让前端白屏（契约硬拒）。"""
        root = {"id": "r", "label": "根", "children": [
            {"id": f"k{i}", "label": f"子{i}"} for i in range(60)]}
        body = {"root": root,
                "edges": [{"id": "e1", "source": "k0", "target": "k59"}]}
        result = parse_text("cut.json", json.dumps(body, ensure_ascii=False))
        self.assertEqual(result.content.edges, [])
        self.assertIn("edges_dropped", codes(result.warnings))

    def test_self_loop_and_duplicate_edges_are_dropped(self):
        body = build()
        body["edges"] = [
            {"id": "e1", "source": "m1", "target": "m1"},
            {"id": "e2", "source": "m1", "target": "t1", "label": "是"},
            {"id": "e3", "source": "m1", "target": "t1", "label": "是"},
        ]
        result = parse_text("loop.json", json.dumps(body, ensure_ascii=False))
        self.assertEqual(len(result.content.edges), 1)
        self.assertIn("edges_dropped", codes(result.warnings))


class ErrorTest(unittest.TestCase):
    def test_unknown_format(self):
        with self.assertRaises(UnsupportedFormat) as caught:
            parse_import("数据.csv", b"a,b\n1,2\n")
        self.assertEqual(caught.exception.code, "unsupported_format")

    def test_broken_json_points_at_the_line(self):
        with self.assertRaises(ParseFailure) as caught:
            parse_import("bad.json", b'{\n  "root": {,\n}')
        self.assertEqual(caught.exception.code, "parse_failed")
        self.assertIn("第 2 行", str(caught.exception))

    def test_broken_xml_points_at_the_line(self):
        with self.assertRaises(ParseFailure) as caught:
            parse_import("bad.opml", b'<opml><body>\n<outline text="x">\n')
        self.assertIn("第", str(caught.exception))

    def test_empty_file(self):
        with self.assertRaises(ParseFailure):
            parse_import("empty.json", b"")

    def test_json_without_root_is_unsupported_not_a_crash(self):
        with self.assertRaises(UnsupportedFormat):
            parse_import("a.json", b'{"hello": "world"}')


class InterfaceTest(unittest.TestCase):
    def test_multipart_returns_content_warnings_and_format(self):
        content = MindMapContent.model_validate(build(
            edges=[{"id": "e1", "source": "m1", "target": "t1", "label": "是"}]))
        payload = to_markdown(content).encode("utf-8")
        with TestClient(app) as client:
            response = client.post(
                "/api/mindmaps/import",
                files={"file": ("规划.md", payload, "text/markdown")})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["source_format"], "markdown")
        self.assertEqual(body["content"]["title"], "季度规划")
        self.assertEqual(body["warnings"], [])
        self.assertEqual(len(body["content"]["edges"]), 1)

    def test_warnings_have_only_code_and_message(self):
        body = build()
        body["root"]["source_refs"] = [{"source_id": "x", "quote": "y"}]
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        with TestClient(app) as client:
            response = client.post(
                "/api/mindmaps/import",
                files={"file": ("r.json", payload, "application/json")})
        self.assertEqual(response.status_code, 200)
        warnings = response.json()["warnings"]
        self.assertTrue(warnings)
        self.assertEqual(set(warnings[0]), {"code", "message"})

    def test_unknown_format_returns_400_unsupported(self):
        with TestClient(app) as client:
            response = client.post(
                "/api/mindmaps/import",
                files={"file": ("a.csv", b"a,b\n1,2\n", "text/csv")})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "unsupported_format")

    def test_broken_file_returns_400_parse_failed(self):
        with TestClient(app) as client:
            response = client.post(
                "/api/mindmaps/import",
                files={"file": ("a.json", b'{"root": {', "application/json")})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "parse_failed")

    def test_import_does_not_touch_the_database(self):
        """只解析不落库：导入一次之后列表里不该多出记录。"""
        content = MindMapContent.model_validate(build())
        with TestClient(app) as client:
            before = client.get("/api/mindmaps").json()
            response = client.post(
                "/api/mindmaps/import",
                files={"file": ("m.json", to_json(content).encode(), "application/json")})
            after = client.get("/api/mindmaps").json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(after), len(before))


class HardeningTest(unittest.TestCase):
    """外部文件能带进来的三类"脏"东西：控制字符、压缩炸弹、超长清单。

    这一组不是为了"更严格"，是为了**不在 2C2G 的机器上 OOM、不让终端控制序列
    落到 label 里**。每一条都在 `app/importing/` 里有对应的一道闸。
    """

    def test_control_characters_are_stripped_from_labels(self):
        # NUL 与 ESC（`\x1b[31m` 的前导）都能塞进 JSON 字符串
        body = {"root": {"id": "r", "label": "前\x00后\x1b[31m红"}}
        result = parse_text("ctl.json", json.dumps(body, ensure_ascii=False))
        label = result.content.root.label
        self.assertNotIn("\x00", label)
        self.assertNotIn("\x1b", label)
        # 折成空格而不是删掉：`前 后` 是两个词，不是"前后"一个词
        self.assertEqual(label, "前 后 [31m红")

    def test_zip_member_larger_than_the_limit_is_refused(self):
        """10MB 的 zip 能解出 10GB。必须在读之前看中央目录里的 file_size。"""
        payload = b"a" * (21 * 1024 * 1024)  # 高度可压缩，zip 本身很小
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("content.json", payload)
        self.assertLess(len(buffer.getvalue()), 1024 * 1024)

        with self.assertRaises(ParseFailure) as caught:
            parse_import("bomb.xmind", buffer.getvalue())
        self.assertIn("上限", str(caught.exception))

    def test_markdown_with_too_many_items_stops_reading(self):
        """一份 10MB 的 markdown 能有几十万行 `- x`；解析期就要截住。"""
        text = "# 标题\n\n- 根\n" + "".join(f"  - 项{i}\n" for i in range(5100))
        result = parse_text("huge.md", text)
        self.assertIn("hard_cap", codes(result.warnings))
        # 仍然给出一张能用的图，而不是整份失败
        self.assertEqual(len(result.content.root.children), 50)

    def test_puml_with_too_many_lines_stops_reading(self):
        text = "@startmindmap\n* 根\n" + "".join(f"** 项{i}\n" for i in range(5100))
        text += "@endmindmap\n"
        result = parse_text("huge.puml", text)
        self.assertIn("hard_cap", codes(result.warnings))
        self.assertEqual(len(result.content.root.children), 50)


if __name__ == "__main__":
    unittest.main()
