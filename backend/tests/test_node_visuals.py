"""节点视觉元素（`color` / `icon`）的契约与执行器。

这里钉住四件事，每一件都对应一种"改了会静默出错"的方式：

  1. **两个字段都是可选的**。库里所有旧图都没有它们，多一个必填字段就等于
     "下次保存 422"——用户什么都没改，图却存不回去（`note` 与 `detail` 各自
     踩过一次）。
  2. **`color` 只收调色板键**。放开成任意 hex 的代价不是难看，而是换主题时
     无法适配：hex 是结论，键才是语义。
  3. **调色板的色值在前端 CSS 里**，而键在后端契约里。两边漏一个键的表现是
     "那个颜色画出来是默认灰"，没有任何报错——所以这里跨文件比一次。
  4. **`update` 的"不动"与"清空"必须分得开**。`color=""` 是清除颜色，
     `color=None`（缺省）是别碰它；合成一个语义，模型就永远摘不掉画错的图标。
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agent.ops import apply_ops
from app.contracts.mindmap import (
    MAX_NODE_ICON_CHARS,
    NODE_COLORS,
    NODE_COLOR_CSS_PREFIX,
    MindMapContent,
    Node,
    normalize_color,
    normalize_icon,
)
from app.contracts.ops import AddOp, OpBatch, UpdateChanges, UpdateOp

# 朴素的四节点树：root -> (a -> a1, b, c)。与 `test_agent_tools.TREE` 同形，
# 但这里只用它验字段，不复用那个文件的 fixture（那边每一个用例都要建 ctx）。
TREE = {
    "title": "示例",
    "root": {
        "id": "root", "label": "根", "children": [
            {"id": "a", "label": "甲", "children": [
                {"id": "a1", "label": "甲一", "children": []},
            ]},
            {"id": "b", "label": "乙", "children": []},
        ],
    },
}


def _content() -> MindMapContent:
    return MindMapContent.model_validate(TREE)


def _apply(ops: list[dict]) -> MindMapContent:
    batch = OpBatch(batch_id="t", base_revision=0, ops=ops)
    return apply_ops(_content(), batch)


def _find(root: Node, node_id: str) -> Node:
    stack = [root]
    while stack:
        node = stack.pop()
        if node.id == node_id:
            return node
        stack.extend(node.children)
    raise AssertionError(f"找不到节点 {node_id}")


# ---------------------------------------------------------------- 契约：可选与兼容


def test_visual_fields_are_optional_and_default_to_none():
    """旧图（一个字段都没有）必须能照常读进来，两个新字段缺省是 None。"""
    content = _content()
    for node in (content.root, content.root.children[0]):
        assert node.color is None
        assert node.icon is None
    # 显式写 null 也要能过（前端与导入路径都可能发 null 而不是省略字段）
    explicit = MindMapContent.model_validate({
        "title": "示例",
        "root": {"id": "r", "label": "根", "color": None, "icon": None, "children": []},
    })
    assert explicit.root.color is None


def test_visual_fields_do_not_disturb_the_rest_of_the_contract():
    """加字段不改旧行为：标题折叠、备注归一、`style` 透传都照旧。"""
    node = Node.model_validate({
        "id": "n", "label": "  甲\n乙  ", "note": "   ",
        "style": {"color": "#1e40af", "icon": "旧图标"}, "color": "red", "icon": "⚠️",
    })
    assert node.label == "甲 乙"
    assert node.note is None
    # `style` 是透传字段（hex + 任意 40 字字符串），与新的语义色互不影响
    assert node.style is not None and node.style.color == "#1e40af"
    assert (node.color, node.icon) == ("red", "⚠️")


def test_color_accepts_only_palette_keys():
    """非法值在契约层被**拒**，不是被归一。"""
    for key in NODE_COLORS:
        assert Node(id="n", label="x", color=key).color == key
    for bad in ("#2563eb", "深蓝", "Blue", "", "rgb(0,0,0)"):
        with pytest.raises(ValidationError):
            Node(id="n", label="x", color=bad)


def test_icon_length_is_capped_and_blank_normalized():
    long_icon = "🎯" * (MAX_NODE_ICON_CHARS + 1)
    with pytest.raises(ValidationError):
        Node(id="n", label="x", icon=long_icon)
    # 纯空白等于没有图标（与 note / detail 同一条归一）
    assert Node(id="n", label="x", icon="   ").icon is None
    assert Node(id="n", label="x", icon="🎯").icon == "🎯"


# ---------------------------------------------------------------- 生成侧的归一化


def test_normalize_color_drops_unknown_instead_of_failing():
    """生成侧是"丢掉"，不是"报错"——装饰字段不该废掉一次已经花掉的骨架调用。"""
    assert normalize_color("Blue ") == "blue"
    assert normalize_color("RED") == "red"
    assert normalize_color("#2563eb") is None
    assert normalize_color("深蓝") is None
    assert normalize_color(None) is None
    assert normalize_color(3) is None
    assert normalize_color("") is None


def test_normalize_icon_folds_whitespace_and_clips():
    assert normalize_icon(" 🎯 ") == "🎯"
    assert normalize_icon("🔧 ⚙️") == "🔧 ⚙️"
    assert normalize_icon("   ") is None
    assert normalize_icon(None) is None
    assert normalize_icon(42) is None
    # 长度截断（用一串符号而不是字母：字母会被下面那条规则判成"不是图标"）
    assert len(normalize_icon("★" * 40)) == MAX_NODE_ICON_CHARS


def test_normalize_icon_drops_icons_written_as_words():
    """模型会写 `"icon": "放大镜"`——那是一个词，不是图标。

    落到树上会把四个汉字挂在标题前面，看起来像标题的一部分，而用户没有任何办法
    知道"那几个字本来应该是个图标"。所以按 Unicode 类别判：含字母/数字就丢掉。
    """
    assert normalize_icon("放大镜") is None
    assert normalize_icon("magnifier") is None
    assert normalize_icon("用眼睛表示观察") is None
    assert normalize_icon("AI") is None
    # emoji 属于 `So`，照常过
    assert normalize_icon("🎯") == "🎯"
    assert normalize_icon("⚠️") == "⚠️"


# ---------------------------------------------------------------- 调色板 → CSS 变量


def test_every_palette_key_has_a_css_variable():
    """后端加的键必须在前端 CSS 里有对应变量。

    漏一个的表现是那个颜色画出来是默认灰——不报错、不告警，只是"这个颜色看起来
    没生效"。这条跨文件比对就是它能被发现的地方（读的是仓库里的源文件，不是构建
    产物，所以不需要先跑前端构建）。
    """
    from pathlib import Path

    css = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "styles.css").read_text(
        encoding="utf-8")
    for key in NODE_COLORS:
        assert f"{NODE_COLOR_CSS_PREFIX}{key}:" in css, f"styles.css 里没有 {key} 的变量"
        # 键 → 变量的映射也要在：只有变量没有映射，节点仍然画不出颜色
        assert f'[data-node-color="{key}"]' in css, f"styles.css 里没有 {key} 的映射规则"


def test_palette_size_is_a_decision_not_a_drift():
    """八个语义色是**定过**的数，不是随手长的。

    少一个（比如把 pink 删掉）会让"人/组织"这类分支无处可标；多到十几个则开始
    重复语义，而两个键意思一样时模型只会随机挑一个——同一张图里同一个意思被画成
    两种颜色，比没有颜色更糟。
    """
    assert len(NODE_COLORS) == 8
    assert len(set(NODE_COLORS)) == len(NODE_COLORS)


# ---------------------------------------------------------------- op 契约


def test_add_op_takes_palette_colors_and_rejects_hex():
    op = AddOp.model_validate({
        "op": "add", "id": "$t", "parent_id": "root", "index": 0, "label": "新",
        "color": "Teal ", "icon": "🎯",
    })
    # 大小写与空白归一，落库的永远是规范键
    assert (op.color, op.icon) == ("teal", "🎯")
    with pytest.raises(ValidationError):
        AddOp.model_validate({
            "op": "add", "id": "$t", "parent_id": "root", "index": 0, "label": "新",
            "color": "#2563eb",
        })


def test_update_changes_distinguish_untouched_from_cleared():
    """`None` = 不动，`""` = 清空。这两件事在契约层就必须分得开。"""
    assert UpdateChanges(color="").color == ""
    # 没给 color 时它是 None（"别碰它"），与空串（"清掉它"）是两种意思
    assert UpdateChanges(label="新标题").color is None
    # 只给一个空串也算"改了字段"（at_least_one 不能被空串骗过去）
    assert UpdateChanges(color="").model_dump(exclude_unset=True) == {"color": ""}
    # 一个字段都不给才报错
    with pytest.raises(ValidationError):
        UpdateChanges()


def test_update_op_color_is_normalized_and_palette_checked():
    op = UpdateOp.model_validate({"op": "update", "id": "a", "changes": {"color": " Purple"}})
    assert op.changes.color == "purple"
    with pytest.raises(ValidationError):
        UpdateOp.model_validate({"op": "update", "id": "a", "changes": {"color": "#0f0"}})


# ---------------------------------------------------------------- 执行器


def test_add_applies_color_and_icon():
    updated = _apply([{
        "op": "add", "id": "$n", "parent_id": "root", "index": 0, "label": "新分支",
        "color": "amber", "icon": "⚠️", "detail": "说明",
    }])
    added = _find(updated.root, updated.root.children[0].id)
    assert (added.label, added.color, added.icon, added.detail) == ("新分支", "amber", "⚠️", "说明")


def test_add_with_empty_color_leaves_it_unset():
    """`add` 没有"清空"语义：空串就是没有颜色，不该在树里留一个空串。"""
    updated = _apply([{
        "op": "add", "id": "$n", "parent_id": "root", "index": 0, "label": "新分支",
        "color": "",
    }])
    assert updated.root.children[0].color is None


def test_update_sets_and_clears_visuals_without_touching_note():
    """改颜色/图标不碰 `note`（用户的备注），也不碰别的字段。"""
    base = _content()
    base.root.children[0].note = "我写的备注"
    batch = OpBatch(batch_id="t", base_revision=0, ops=[
        {"op": "update", "id": "a", "changes": {"color": "red", "icon": "⚠️"}},
    ])
    updated = apply_ops(base, batch)
    node = _find(updated.root, "a")
    assert (node.color, node.icon) == ("red", "⚠️")
    assert node.note == "我写的备注"
    # 传入的树一个字都不能变（整批原子）
    assert base.root.children[0].color is None

    cleared = apply_ops(updated, OpBatch(batch_id="t2", base_revision=1, ops=[
        {"op": "update", "id": "a", "changes": {"color": "", "icon": ""}},
    ]))
    node = _find(cleared.root, "a")
    assert (node.color, node.icon) == (None, None)
    assert node.note == "我写的备注"
