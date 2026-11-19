"""F15 的检测规则与建议映射（`optimization/service.py`）。

三层各自钉住的东西：

  1. **规则层**（`check()`）：每条判据的边界——什么时候该报、什么时候**故意不报**。
     不报的那些用例不是凑数：近义检测最容易的失败方式是"平行命名被当成重复"
     （「监督学习」/「无监督学习」），那会让用户点一次就丢半条分支。
  2. **建议层**（`suggest()`）：每个诊断码都能落到一个**动作**上，或者有一份
     写下来的"为什么不转"。`DEPTH_LIMIT` 是后者的样本。
  3. **模型层**（`_model_near_pairs`）：一次调用、可关、绝不抛异常，且它的答案
     要过"同一同级组"这道校验。

真模型的调用不打桩的只有一条 live 用例（`test_live_*`），没有
`MINDMAP_LLM_API_KEY` 时跳过——离线跑全量套件不该产生网络请求。
"""
from __future__ import annotations


import pytest

from app.contracts.diagnostic import DiagnosticLevel, SuggestionKind
from app.contracts.mindmap import MindMapContent
from app.contracts.source import Source, SourceKind
from app.optimization.service import (
    NOT_SUGGESTED,
    _SUGGESTION_OF_CODE,
    _near_reason,
    _not_converted_reason,
    check,
    suggest,
)


def _map(root: dict, *, title: str = "图") -> MindMapContent:
    return MindMapContent.model_validate({"title": title, "root": root})


def _leaf(node_id: str, label: str, **extra) -> dict:
    return {"id": node_id, "label": label, "children": [], **extra}


def _node(node_id: str, label: str, children: list[dict], **extra) -> dict:
    return {"id": node_id, "label": label, "children": children, **extra}


def _codes(diagnostics) -> list[str]:
    return [item.code for item in diagnostics]


def _of_code(diagnostics, code: str):
    return [item for item in diagnostics if item.code == code]


def _kinds(suggestions) -> dict[str | None, SuggestionKind]:
    return {item.node_id: item.kind for item in suggestions}


def _chain(depth: int) -> MindMapContent:
    """一条 `depth` 层的链（root 算第 1 层）。契约上限正好 8 层。"""
    node = _leaf(f"n{depth}", f"第{depth}层")
    for level in range(depth - 1, 0, -1):
        node = _node(f"n{level}", f"第{level}层", [node])
    return _map(node)


# ---------------------------------------------------------------- 1. 建议映射


def test_depth_limit_is_reported_but_never_turned_into_a_suggestion():
    """`DEPTH_LIMIT` 只有诊断，没有建议——"已到 8 层"不是一个可执行的动作。

    这条用例钉两件事：诊断还在（生成收尾看它），建议里没有它。
    """
    content = _chain(8)

    diagnostics = check(content)
    depth = _of_code(diagnostics, "DEPTH_LIMIT")
    assert [item.node_id for item in depth] == ["n8"]
    assert depth[0].level is DiagnosticLevel.warn

    assert "DEPTH_LIMIT" not in _SUGGESTION_OF_CODE
    assert "8 层" in NOT_SUGGESTED["DEPTH_LIMIT"]
    # 8 层那条链上每层只有一个子节点 → 建议全是 expand，没有一条来自 DEPTH_LIMIT
    suggestions = suggest(content)
    assert suggestions and all(item.kind is SuggestionKind.expand for item in suggestions)


def test_single_child_becomes_an_expand_suggestion():
    """深处的那条单子节点 → `expand`：缺的是内容，不是层级归并。"""
    content = _map(_node("r", "根", [
        _node("b", "分支", [_node("p", "要点", [_leaf("q", "细节")])]),
        _node("c", "另一支", [_leaf("c1", "甲"), _leaf("c2", "乙"), _leaf("c3", "丙")]),
    ]))

    assert _kinds(suggest(content))["p"] is SuggestionKind.expand


def _closure_codes() -> set[str]:
    """这一版的检查器能产出的**全部**诊断码：几张小图各跑一遍取并集。

    加一条新规则时，这里要多一个夹具——那正是这条用例的作用：新码不许悬空。
    """
    contents = [
        # 形态（单子节点）+ 同级重复
        _map(_node("r", "根", [
            _node("b", "只有一个孩子", [_leaf("b1", "孤子", detail="有说明")]),
            _node("c", "深支", [_node("c1", "一级", [
                _node("c11", "二级", [_leaf("c111", "三层", detail="有说明")]),
                _leaf("c12", "二级之二", detail="有说明"),
                _leaf("c13", "二级之三", detail="有说明"),
            ])]),
            _leaf("d", "重复项"),
            _leaf("e", "重复项"),
        ])),
        # 深度上限
        _chain(8),
        # 同级近义
        _map(_node("r", "根", [_leaf("a", "数据处理"), _leaf("b", "数据预处理")])),
    ]
    codes: set[str] = set()
    for content in contents:
        codes |= set(_codes(check(content)))
    # 引用类
    source = Source(id="s1", kind=SourceKind.document, status="parsed",
                    filename="a.txt", text="一句原文。")
    codes |= set(_codes(check(contents[0], [source])))
    return codes


def test_every_emitted_code_is_mapped_or_has_a_written_reason():
    """检查器能产出的每一个码：要么有动作，要么有理由。一个都不许悬空。

    没有这条，以后新增一条诊断然后忘了决定"转不转"，表现是"这条诊断永远不出现在
    建议面板里"——安静得像没做。
    """
    codes = _closure_codes()
    explicit = {"SINGLE_CHILD", "DUPLICATE_LABEL", "NEAR_DUPLICATE_LABEL",
                "DEPTH_LIMIT"}
    assert explicit <= codes, codes

    for code in sorted(codes):
        mapped = code in _SUGGESTION_OF_CODE
        explained = _not_converted_reason(code) is not None
        assert mapped or explained, f"{code} 既没有建议映射，也没有写清为什么不转"
        assert not (mapped and code in NOT_SUGGESTED)


def test_suggestion_kinds_are_action_words():
    """`expand` / `add_detail` 是这一轮补的动作名词，值是给人看的动词。"""
    assert SuggestionKind.expand.value == "expand"
    assert SuggestionKind.add_detail.value == "add_detail"
    used = set(_SUGGESTION_OF_CODE.values())
    assert {SuggestionKind.dedupe, SuggestionKind.expand} <= used


# ---------------------------------------------------------------- 2. 近义（规则层）


def test_identical_labels_stay_duplicate_and_are_not_reported_twice():
    """逐字相同是 `DUPLICATE_LABEL` 的地盘，近义那条不能再来一遍。

    两条诊断落在同一个节点上、只差一个码，是最典型的"重复报"。
    """
    content = _map(_node("r", "根", [_leaf("a", "数据"), _leaf("b", "数据")]))
    assert _codes(check(content)) == ["DUPLICATE_LABEL"]
    assert [item.kind for item in suggest(content)] == [SuggestionKind.dedupe]


def test_labels_that_differ_only_in_punctuation_are_near_duplicates():
    """去标点/空格/全半角后相同 → warn（字面已经不是"不同"，只剩排版）。"""
    content = _map(_node("r", "根", [_leaf("a", "监督学习"), _leaf("b", "（监督）学习")]))
    near = _of_code(check(content), "NEAR_DUPLICATE_LABEL")
    assert len(near) == 1
    assert near[0].level is DiagnosticLevel.warn
    assert near[0].node_id == "b" and near[0].evidence == "a"
    assert "去掉标点与空格后完全相同" in near[0].message


def test_full_width_and_case_are_normalized_too():
    assert _near_reason("ML 基础", "ml基础") is not None
    assert _near_reason("ＡＢＣ处理", "ABC处理") is not None


def test_prefix_relation_is_reported_at_info_level():
    """一方是另一方的前缀 → info：字面包含是事实，"是不是同一件事"还要人看。"""
    content = _map(_node("r", "根", [
        _leaf("a", "监督学习"), _leaf("b", "监督学习与无监督学习"),
    ]))
    near = _of_code(check(content), "NEAR_DUPLICATE_LABEL")
    assert len(near) == 1
    assert near[0].level is DiagnosticLevel.info
    assert near[0].node_id == "b" and near[0].evidence == "a"
    assert "前缀" in near[0].message


@pytest.mark.parametrize("left,right", [
    ("监督学习", "有监督学习"),      # 任务说明里的第一个"模型层"例子，规则层其实抓得到
    ("数据处理", "数据预处理"),
])
def test_inserted_modifier_counts_as_near_duplicate(left, right):
    """加一个限定词 = 同一个概念的两种写法。

    这一档在生成的图上很罕见（提示词本来就要求每支给不同的要点），但用户手工
    在原地补一个更细的分支时极常见。
    """
    near = _of_code(check(_map(_node("r", "根", [
        _leaf("a", left), _leaf("b", right)]))), "NEAR_DUPLICATE_LABEL")
    assert len(near) == 1
    assert near[0].node_id == "b"
    assert "只多了" in near[0].message


@pytest.mark.parametrize("left,right", [
    ("监督学习", "无监督学习"),        # 反向修饰语：合并掉一条就是丢内容
    ("从重处罚的适用", "从轻处罚的适用"),
    ("分类任务选算法", "回归任务选算法"),
    ("按题型分配", "按模块分配"),      # 同一模板的两个实例（带替换）
    ("错题本管理", "错题本整理"),      # 换字型近义：规则层看不见，留给模型层
    ("机器学习", "深度学习"),          # 上下位
    ("定义", "定理"),                  # 太短：距离 1 就是换掉一半
    ("交叉验证", "分层交叉验证"),      # 插入占比 0.5，更像上下位
])
def test_pairs_that_must_not_be_flagged(left, right):
    """**故意不报**的那些形状。每一条都能在真实库里找到同形样本。

    「监督学习」/「无监督学习」尤其重要：它是"平行命名"这一类的代表，全库实测
    38 张真实生成图上，纯编辑距离判据命中 43 对、这类占 40 对、真正近义 0 对。
    合掉一支等于删内容。
    """
    assert _near_reason(left, right) is None


def test_near_duplicates_are_only_compared_within_one_parent():
    """跨分支的近名不报：不同父节点下的「数据处理」「数据预处理」可以各自成立。"""
    content = _map(_node("r", "学科", [
        _node("a", "物理", [_leaf("a1", "数据处理")]),
        _node("b", "数学", [_leaf("b1", "数据预处理")]),
    ]))
    assert _of_code(check(content), "NEAR_DUPLICATE_LABEL") == []


def test_near_duplicate_becomes_a_dedupe_suggestion():
    content = _map(_node("r", "根", [
        _leaf("a", "数据处理"), _leaf("b", "数据预处理"),
    ]))
    suggestion = suggest(content)[0]
    assert suggestion.kind is SuggestionKind.dedupe
    assert suggestion.node_id == "b"
    assert suggestion.evidence == "a"
    # 规则建议没有概率校准，不许伪造可信度
    assert suggestion.confidence is None

