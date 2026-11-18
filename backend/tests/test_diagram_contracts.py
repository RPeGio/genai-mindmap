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
    FishboneContent,
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
