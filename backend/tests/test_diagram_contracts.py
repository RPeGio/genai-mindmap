"""三种图型各自的顶层结构：鱼骨 / 流程 / 与树的联合。

与 `test_contract_v2.py`（树那一套 v1→v2 的默认值与校验）、`test_edges.py`（附加边）
分工不同：这里只钉"鱼骨与流程的**形状**"，以及它们与树怎么被同一个入口分开读。

写这些用例的动机是先前的坑：鱼骨图与流程图在阶段 2 之前是"树换皮"——一张
`diagram_type == "fishbone"` 的图，形状仍是一棵树，于是"这一根骨下有几个原因"
只能靠数一级子节点的个数猜。形状进契约之后，猜的地方变成了读字段。
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.contracts.mindmap import (
    FISHBONE_CATEGORIES,
    MAX_FISHBONE_BONES,
    MAX_FLOW_STAGES,
    FishboneContent,
    FlowContent,
)


def node(node_id: str, label: str | None = None, children=None, **extra):
    return {"id": node_id, "label": label or node_id, "children": children or [], **extra}


def bone(category: str, root=None, **extra):
    return {"category": category, "root": root or node(category), **extra}


def fishbone(**extra):
    return {
        "title": "报销慢的原因",
        "problem": "为什么报销要 11 天",
        "bones": [bone("人"), bone("机"), bone("料")],
        **extra,
    }


class TestFishboneShape:
    def test_six_categories_fit(self):
        """5M1E 的六类各一根骨：这是提示词要求的标准形状，必须能一次装下。"""
        parsed = FishboneContent.model_validate(fishbone(
            bones=[bone(category) for category in FISHBONE_CATEGORIES]))
        assert [item.category for item in parsed.bones] == list(FISHBONE_CATEGORIES)
        assert parsed.diagram_type == "fishbone"

    def test_fewer_than_six_is_legal(self):
        """少于此数完全合法：很多问题的成因只有三类，凑到六根等于让模型编。"""
        parsed = FishboneContent.model_validate(fishbone())
        assert len(parsed.bones) == 3

    def test_causes_are_root_children(self):
        """`causes` 不是独立字段，它就是 `root.children`——这一条是设计前提。"""
        parsed = FishboneContent.model_validate(fishbone(bones=[
            bone("人", node("人", "人", [node("c1", "审批人休假"),
                                          node("c2", "岗位缺人")])),
        ]))
        assert [child.label for child in parsed.bones[0].root.children] == ["审批人休假", "岗位缺人"]

    def test_bone_carries_its_own_visuals(self):
        """骨的颜色/图标与 `root.color` 分开：类别是骨架，不是第一个原因。"""
        parsed = FishboneContent.model_validate(fishbone(bones=[
            bone("机", node("机", "机", color="blue"), color="amber", icon="⚙"),
        ]))
        assert parsed.bones[0].color == "amber"
        assert parsed.bones[0].root.color == "blue"

    def test_blank_icon_and_color_are_optional(self):
        parsed = FishboneContent.model_validate(fishbone(bones=[
            bone("料", icon="   "),
        ]))
        assert parsed.bones[0].icon is None
        assert parsed.bones[0].color is None


class TestFishboneValidation:
    def test_duplicate_category_is_rejected(self):
        """两根骨挂同一个类别，图上会出现两条同名的横骨，用户分不清哪条是哪条。"""
        with pytest.raises(ValidationError) as caught:
            FishboneContent.model_validate(fishbone(bones=[bone("人"), bone("人")]))
        assert "骨的类别不能重复" in str(caught.value)

    def test_category_is_folded(self):
        parsed = FishboneContent.model_validate(fishbone(bones=[bone("人 为\n因素")]))
        assert parsed.bones[0].category == "人 为 因素"

    def test_blank_category_is_rejected(self):
        with pytest.raises(ValidationError):
            FishboneContent.model_validate(fishbone(bones=[bone("   ")]))

    def test_node_ids_are_unique_across_bones(self):
        """六根骨是并列的子树，但 id 必须全局唯一：前端按 id 建索引。"""
        with pytest.raises(ValidationError) as caught:
            FishboneContent.model_validate(fishbone(bones=[
                bone("人", node("x", "人")), bone("机", node("x", "机")),
            ]))
        assert "节点 ID 必须唯一" in str(caught.value)

    def test_bone_count_is_bounded(self):
        many = [bone(f"类{i}") for i in range(MAX_FISHBONE_BONES + 1)]
        with pytest.raises(ValidationError):
            FishboneContent.model_validate(fishbone(bones=many))

    def test_node_budget_is_shared_across_bones(self):
        """节点上限按六棵子树**合计**算：画布承受的是整张图，不是一根骨。

        每根骨 1 + 50 + 150 = 201 个节点，单根远低于上限；六根合计 1206，超。
        这正是"按骨各自检查"会放过去的那一格。
        """
        def wide(index: int):
            children = [node(f"m{index}_{i}", children=[node(f"g{index}_{i}_{j}")
                                                        for j in range(3)])
                        for i in range(50)]
            return node(f"b{index}", children=children)

        bones = [bone(category, wide(index))
                 for index, category in enumerate(FISHBONE_CATEGORIES)]
        with pytest.raises(ValidationError) as caught:
            FishboneContent.model_validate(fishbone(bones=bones))
        assert "最多 1000 个节点" in str(caught.value)

    def test_source_refs_must_point_into_source_usage(self):
        with pytest.raises(ValidationError) as caught:
            FishboneContent.model_validate(fishbone(bones=[
                bone("人", node("人", "人", source_refs=[{"source_id": "ghost"}])),
            ]))
        assert "不在 source_usage 中" in str(caught.value)

    def test_relations_may_cross_bones(self):
        """跨骨的因果关系是鱼骨图的核心表达：某根骨的原因导致另一根骨的现象。"""
        parsed = FishboneContent.model_validate(fishbone(
            bones=[bone("人", node("人", "人", [node("a")])),
                   bone("机", node("机", "机", [node("b")]))],
            relations=[{"id": "r1", "source": "a", "target": "b", "type": "cause"}],
        ))
        assert parsed.relations[0].type.value == "cause"

    def test_relation_to_missing_node_is_rejected(self):
        with pytest.raises(ValidationError) as caught:
            FishboneContent.model_validate(fishbone(
                relations=[{"id": "r1", "source": "人", "target": "nope", "type": "ref"}]))
        assert "不在导图中" in str(caught.value)

    def test_empty_problem_is_rejected(self):
        with pytest.raises(ValidationError):
            FishboneContent.model_validate(fishbone(problem="   "))

    def test_problem_is_folded(self):
        parsed = FishboneContent.model_validate(fishbone(problem="为什么\n报销要 11 天"))
        assert parsed.problem == "为什么 报销要 11 天"

    def test_tree_fields_are_not_accepted(self):
        """`extra="forbid"` 让"鱼骨"与"树"的形状互斥——联合类型靠这一点判别。"""
        with pytest.raises(ValidationError):
            FishboneContent.model_validate(fishbone(root=node("r")))


def stage(stage_id: str, root=None, **extra):
    return {"id": stage_id, "root": root or node(stage_id, stage_id), **extra}


def flow(**extra):
    return {
        "title": "报销流程",
        "stages": [
            stage("提交", node("提交", "提交", [node("填单"), node("上传发票")])),
            stage("审查", node("审查", "审查", [node("形式审查"), node("实质审查")])),
            stage("付款", node("付款", "付款", [node("打款")])),
        ],
        **extra,
    }


class TestFlowShape:
    def test_stage_order_is_the_array_order(self):
        """"先后"是数组下标，不需要另一个 `order` 字段：两个来源说同一件事时，
        它们迟早会互相矛盾。"""
        parsed = FlowContent.model_validate(flow())
        assert [item.id for item in parsed.stages] == ["提交", "审查", "付款"]
        assert parsed.diagram_type == "flowchart"

    def test_steps_are_root_children(self):
        parsed = FlowContent.model_validate(flow())
        assert [child.label for child in parsed.stages[0].root.children] == ["填单", "上传发票"]

    def test_edges_connect_stages(self):
        parsed = FlowContent.model_validate(flow(edges=[
            {"id": "e1", "source": "提交", "target": "审查", "kind": "flow"},
            {"id": "e2", "source": "审查", "target": "付款", "label": "通过"},
        ]))
        assert [(edge.source, edge.target) for edge in parsed.edges] == [
            ("提交", "审查"), ("审查", "付款")]

    def test_rollback_cycle_is_legal(self):
        """驳回 / 返工 / 重试就是"付款 → 提交"。环在这里是核心需求，不是坏数据。"""
        parsed = FlowContent.model_validate(flow(edges=[
            {"id": "e1", "source": "提交", "target": "审查"},
            {"id": "e2", "source": "审查", "target": "付款"},
            {"id": "e3", "source": "付款", "target": "提交", "label": "驳回", "kind": "loop"},
        ]))
        assert len(parsed.edges) == 3

    def test_duplicate_edges_are_deduped(self):
        parsed = FlowContent.model_validate(flow(edges=[
            {"id": "e1", "source": "提交", "target": "审查", "label": "是"},
            {"id": "e2", "source": "提交", "target": "审查", "label": "是"},
        ]))
        assert [edge.id for edge in parsed.edges] == ["e1"]


class TestFlowValidation:
    def test_stage_ids_must_be_unique(self):
        with pytest.raises(ValidationError) as caught:
            FlowContent.model_validate({"title": "流", "stages": [
                stage("s", node("a")), stage("s", node("b"))]})
        assert "阶段 ID 必须唯一" in str(caught.value)

    def test_edge_endpoints_are_stages_not_nodes(self):
        """端点命名空间是**阶段**：拿一个真实存在的步骤 id 当端点也要拒。

        这一条是流程与树最容易串的地方——两边的 `Edge` 是同一个类，但"连的是阶段
        还是步骤"完全不同。放过它，前端会在阶段列表里找不到端点，画出一条悬空主轴。
        """
        with pytest.raises(ValidationError) as caught:
            FlowContent.model_validate(flow(edges=[
                {"id": "e1", "source": "填单", "target": "审查"}]))
        assert "起点阶段 填单 不在导图中" in str(caught.value)

    def test_edge_to_missing_stage_is_rejected(self):
        with pytest.raises(ValidationError) as caught:
            FlowContent.model_validate(flow(edges=[
                {"id": "e1", "source": "提交", "target": "不存在的阶段"}]))
        assert "终点阶段" in str(caught.value)

    def test_self_loop_between_a_stage_and_itself_is_rejected(self):
        with pytest.raises(ValidationError) as caught:
            FlowContent.model_validate(flow(edges=[
                {"id": "e1", "source": "提交", "target": "提交"}]))
        assert "同一个阶段" in str(caught.value)

    def test_node_ids_are_unique_across_stages(self):
        with pytest.raises(ValidationError) as caught:
            FlowContent.model_validate({"title": "流", "stages": [
                stage("a", node("x")), stage("b", node("x"))]})
        assert "节点 ID 必须唯一" in str(caught.value)

    def test_stage_count_is_bounded(self):
        many = [stage(f"s{i}", node(f"n{i}")) for i in range(MAX_FLOW_STAGES + 1)]
        with pytest.raises(ValidationError):
            FlowContent.model_validate({"title": "流", "stages": many})

    def test_node_budget_is_shared_across_stages(self):
        """节点上限按全部阶段**合计**算（与鱼骨同一口径）。"""
        def wide(index: int):
            children = [node(f"m{index}_{i}", children=[node(f"g{index}_{i}_{j}")
                                                        for j in range(3)])
                        for i in range(50)]
            return node(f"s{index}", children=children)

        # 每阶段 201 个节点，六个阶段 1206 > 1000；单阶段远低于上限。
        stages = [stage(f"s{index}", wide(index)) for index in range(6)]
        with pytest.raises(ValidationError) as caught:
            FlowContent.model_validate({"title": "流", "stages": stages})
        assert "最多 1000 个节点" in str(caught.value)

    def test_relations_still_point_at_nodes(self):
        """`relations` 端点是**节点**（跨阶段的解释性关系），与 `edges` 相反。"""
        parsed = FlowContent.model_validate(flow(relations=[
            {"id": "r1", "source": "填单", "target": "打款", "type": "cause"}]))
        assert parsed.relations[0].source == "填单"

    def test_relation_endpoint_must_be_a_node(self):
        """与上一条相反：`relations` 只认**节点** id，阶段 id 不算。

        用一份阶段 id 与节点 id 不同的流程来钉：拿 `s1`（只作为阶段存在）当关系
        起点要拒，否则"跨阶段解释关系"会连到画不出来的东西上。
        """
        with pytest.raises(ValidationError) as caught:
            FlowContent.model_validate({
                "title": "流",
                "stages": [{"id": "s1", "root": node("n1", "提交")},
                           {"id": "s2", "root": node("n2", "付款")}],
                "relations": [{"id": "r1", "source": "s1", "target": "n2", "type": "ref"}],
            })
        assert "不在导图中" in str(caught.value)

    def test_tree_fields_are_not_accepted(self):
        with pytest.raises(ValidationError):
            FlowContent.model_validate(flow(root=node("r")))
