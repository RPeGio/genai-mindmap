"""导图 op 执行器：整批原子、顺序敏感、失败可指认。

这里断言的全是执行器的确定性行为，不碰模型、不碰数据库——op 执行器本身就
该是纯函数，能这样被逐条钉死，是它作为"模型写坏树的最后一道闸"的前提。
"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.agent.ops import (
    apply_ops,
    find_node,
    find_parent,
    is_descendant,
    new_node_id,
    normalize_ops,
)
from app.contracts.mindmap import MindMapContent, Node
from app.contracts.ops import OpBatch, OpError
from app.contracts.source import SourceRef, SourceUsage

# 引用得先在 source_usage 里登记，否则整树校验会把节点打回去。
SOURCE_USAGE = [SourceUsage(source_id="s1", kind="text", char_count=3)]


def tree_of(*children: Node, source_usage: list[SourceUsage] | None = None) -> MindMapContent:
    """造一张以 `r` 为根的导图；结构直接可读，断言才好写。"""
    return MindMapContent(
        title="测试导图",
        root=Node(id="r", label="根", children=list(children)),
        source_usage=list(source_usage or []),
    )


def batch_of(*ops: dict) -> OpBatch:
    """把草案 dict 包成一批 op；判别联合会按 op 字段分派到具体模型。"""
    return OpBatch(batch_id="batch-1", base_revision=0, ops=list(ops))


def ids_of(nodes: list[Node]) -> list[str]:
    return [node.id for node in nodes]


class IdGenerationTest(unittest.TestCase):
    """节点 id 生成：服务端要落库，还要参与之后所有 op 的引用解析。"""

    def test_生成完整的三十二位_uuid_十六进制(self):
        node_id = new_node_id(set())
        assert node_id.startswith("n_")
        # 前端 edits.ts 截到 8 位是显示层够用；服务端截短就等于把撞 id 的概率
        # 从"不可能"抬到"迟早"，而撞 id 的表现是静默指向另一个节点。
        assert len(node_id) == 2 + 32
        int(node_id[2:], 16)

    def test_不返回已在集合里的_id(self):
        existing = {"n_" + "a" * 32}
        assert new_node_id(existing) not in existing

    def test_撞上既有_id_时重新生成(self):
        """查重必须真的重来一遍，而不是把撞车的结果原样返回。"""
        existing = {"n_" + "a" * 32}
        with patch("app.agent.ops.uuid4") as fake:
            fake.side_effect = [SimpleNamespace(hex="a" * 32), SimpleNamespace(hex="b" * 32)]
            assert new_node_id(existing) == "n_" + "b" * 32
        assert fake.call_count == 2


class TreeQueryTest(unittest.TestCase):
    """查找与祖先判定的语义，必须和 frontend/src/mindmap/edits.ts 逐条对应。"""

    def test_find_node_含根节点自身(self):
        tree = tree_of(Node(id="a", label="A", children=[Node(id="a1", label="A1")]))
        assert find_node(tree.root, "r") is tree.root
        assert find_node(tree.root, "a1").label == "A1"
        assert find_node(tree.root, "没有这个") is None

    def test_find_parent_根节点没有父节点(self):
        tree = tree_of(Node(id="a", label="A", children=[Node(id="a1", label="A1")]))
        assert find_parent(tree.root, "r") is None
        assert find_parent(tree.root, "a") is tree.root
        assert find_parent(tree.root, "a1").id == "a"
        assert find_parent(tree.root, "没有这个") is None

    def test_is_descendant_把自己的也算进去(self):
        tree = tree_of(Node(id="a", label="A", children=[Node(id="a1", label="A1")]))
        assert is_descendant(tree.root, "a", "a") is True
        assert is_descendant(tree.root, "a", "a1") is True
        assert is_descendant(tree.root, "r", "a1") is True
        assert is_descendant(tree.root, "a1", "a") is False
        # 祖先不在树里不是环，是"父节点不存在"，由调用方先报 parent_not_found
        assert is_descendant(tree.root, "没有这个", "a1") is False


class AddOpTest(unittest.TestCase):
    """新增节点：父节点、id 唯一性、插入位置三道门。"""

    def test_父节点不存在时报_parent_not_found(self):
        with pytest.raises(OpError) as raised:
            apply_ops(tree_of(Node(id="a", label="A")), batch_of(
                {"op": "add", "id": "n_new", "parent_id": "没有这个", "index": 0, "label": "新"}))
        assert raised.value.code == "parent_not_found"
        assert raised.value.path == "ops.0.parent_id"

    def test_index_等于子节点数量时追加到末尾(self):
        tree = tree_of(Node(id="a", label="A"), Node(id="b", label="B"))
        result = apply_ops(tree, batch_of(
            {"op": "add", "id": "n_new", "parent_id": "r", "index": 2, "label": "末尾"}))
        assert ids_of(result.root.children) == ["a", "b", "n_new"]

    def test_index_大于子节点数量时报_index_out_of_range(self):
        tree = tree_of(Node(id="a", label="A"))
        with pytest.raises(OpError) as raised:
            apply_ops(tree, batch_of(
                {"op": "add", "id": "n_new", "parent_id": "r", "index": 2, "label": "越界"}))
        assert raised.value.code == "index_out_of_range"
        assert raised.value.path == "ops.0.index"

    def test_id_与树里任何节点重复都报_duplicate_id(self):
        tree = tree_of(Node(id="a", label="A"))
        for duplicated in ("a", "r"):  # 子节点和根节点都算重复
            with pytest.raises(OpError) as raised:
                apply_ops(tree, batch_of(
                    {"op": "add", "id": duplicated, "parent_id": "r", "index": 0, "label": "撞车"}))
            assert raised.value.code == "duplicate_id"
            assert raised.value.path == "ops.0.id"

    def test_后面的_op_可以引用前面_add_出来的节点(self):
        """顺序敏感：模型把"先建父、再挂子"写成两条 op，两条都得活着。"""
        result = apply_ops(tree_of(), batch_of(
            {"op": "add", "id": "n_a", "parent_id": "r", "index": 0, "label": "父"},
            {"op": "add", "id": "n_b", "parent_id": "n_a", "index": 0, "label": "子"},
        ))
        assert ids_of(result.root.children) == ["n_a"]
        assert ids_of(find_node(result.root, "n_a").children) == ["n_b"]


class UpdateOpTest(unittest.TestCase):
    """改字段：None 不动、[] 清空，这个区别丢了模型就永远清不掉错引用。"""

    def test_节点不存在时报_node_not_found(self):
        with pytest.raises(OpError) as raised:
            apply_ops(tree_of(), batch_of(
                {"op": "update", "id": "没有这个", "changes": {"label": "改名"}}))
        assert raised.value.code == "node_not_found"
        assert raised.value.path == "ops.0.id"

    def test_只改_label_时引用保持原样(self):
        ref = SourceRef(source_id="s1", quote="原文")
        tree = tree_of(Node(id="a", label="旧标题", source_refs=[ref]), source_usage=SOURCE_USAGE)
        result = apply_ops(tree, batch_of(
            {"op": "update", "id": "a", "changes": {"label": "新标题"}}))
        assert result.root.children[0].label == "新标题"
        assert result.root.children[0].source_refs == [ref]

    def test_传空列表时显式清空引用(self):
        tree = tree_of(Node(id="a", label="A", source_refs=[SourceRef(source_id="s1", quote="原文")]),
                       source_usage=SOURCE_USAGE)
        result = apply_ops(tree, batch_of(
            {"op": "update", "id": "a", "changes": {"source_refs": []}}))
        assert result.root.children[0].source_refs == []

    def test_引用未登记的来源会被整树校验打回(self):
        tree = tree_of(Node(id="a", label="A"), source_usage=SOURCE_USAGE)
        with pytest.raises(OpError) as raised:
            apply_ops(tree, batch_of(
                {"op": "update", "id": "a",
                 "changes": {"source_refs": [{"source_id": "s9", "quote": "无来源"}]}}))
        assert raised.value.code == "tree_invalid"
        assert raised.value.path == "ops"


class RemoveOpTest(unittest.TestCase):
    """删除：根节点不可删，删中间节点连整棵子树一起消失。"""

    def test_删除根节点报_cannot_remove_root(self):
        with pytest.raises(OpError) as raised:
            apply_ops(tree_of(Node(id="a", label="A")), batch_of({"op": "remove", "id": "r"}))
        assert raised.value.code == "cannot_remove_root"
        assert raised.value.path == "ops.0.id"

    def test_连整棵子树一起删干净(self):
        tree = tree_of(
            Node(id="a", label="A", children=[
                Node(id="a1", label="A1", children=[Node(id="a11", label="A11")]),
                Node(id="a2", label="A2"),
            ]),
            Node(id="b", label="B"),
        )
        result = apply_ops(tree, batch_of({"op": "remove", "id": "a"}))
        assert ids_of(result.root.children) == ["b"]
        for gone in ("a", "a1", "a11", "a2"):
            assert find_node(result.root, gone) is None

    def test_删除不存在的节点报_node_not_found(self):
        with pytest.raises(OpError) as raised:
            apply_ops(tree_of(), batch_of({"op": "remove", "id": "没有这个"}))
        assert raised.value.code == "node_not_found"
        assert raised.value.path == "ops.0.id"


class MoveOpTest(unittest.TestCase):
    """换父节点：唯一能改变拓扑、也是唯一能造出环的 op。"""

    def test_移动到自己报_cycle_detected(self):
        with pytest.raises(OpError) as raised:
            apply_ops(tree_of(Node(id="a", label="A")), batch_of(
                {"op": "move", "id": "a", "new_parent_id": "a", "index": 0}))
        assert raised.value.code == "cycle_detected"
        assert raised.value.path == "ops.0.new_parent_id"

    def test_移动到自己的子孙报_cycle_detected(self):
        tree = tree_of(Node(id="a", label="A", children=[
            Node(id="a1", label="A1", children=[Node(id="a11", label="A11")])]))
        with pytest.raises(OpError) as raised:
            apply_ops(tree, batch_of(
                {"op": "move", "id": "a", "new_parent_id": "a11", "index": 0}))
        assert raised.value.code == "cycle_detected"
        assert raised.value.path == "ops.0.new_parent_id"

    def test_新父节点不存在报_parent_not_found(self):
        with pytest.raises(OpError) as raised:
            apply_ops(tree_of(Node(id="a", label="A")), batch_of(
                {"op": "move", "id": "a", "new_parent_id": "没有这个", "index": 0}))
        assert raised.value.code == "parent_not_found"
        assert raised.value.path == "ops.0.new_parent_id"

    def test_要移动的节点不存在报_node_not_found(self):
        with pytest.raises(OpError) as raised:
            apply_ops(tree_of(Node(id="a", label="A")), batch_of(
                {"op": "move", "id": "没有这个", "new_parent_id": "a", "index": 0}))
        assert raised.value.code == "node_not_found"
        assert raised.value.path == "ops.0.id"

    def test_不存在的节点移到自己先报_node_not_found(self):
        """存在性先于环检测：这条锁死判定顺序，与前端 ops.ts 一致。"""
        with pytest.raises(OpError) as raised:
            apply_ops(tree_of(), batch_of(
                {"op": "move", "id": "没有这个", "new_parent_id": "没有这个", "index": 0}))
        assert raised.value.code == "node_not_found"

    def test_移动根节点被环检测拦住(self):
        """根节点的任何候选父节点都是它的子孙，所以根本不用单独的错误码。"""
        with pytest.raises(OpError) as raised:
            apply_ops(tree_of(Node(id="a", label="A")), batch_of(
                {"op": "move", "id": "r", "new_parent_id": "a", "index": 0}))
        assert raised.value.code == "cycle_detected"
        assert raised.value.path == "ops.0.new_parent_id"

    def test_同父内移动按先移除再插入计算上界(self):
        """移除 b 之后子列表只剩 [a, c]，index=2 是合法末尾。"""
        tree = tree_of(Node(id="a", label="A"), Node(id="b", label="B"), Node(id="c", label="C"))
        result = apply_ops(tree, batch_of(
            {"op": "move", "id": "b", "new_parent_id": "r", "index": 2}))
        assert ids_of(result.root.children) == ["a", "c", "b"]

    def test_同父内移动的越界_index_报_index_out_of_range(self):
        tree = tree_of(Node(id="a", label="A"), Node(id="b", label="B"), Node(id="c", label="C"))
        with pytest.raises(OpError) as raised:
            apply_ops(tree, batch_of(
                {"op": "move", "id": "b", "new_parent_id": "r", "index": 3}))
        assert raised.value.code == "index_out_of_range"
        assert raised.value.path == "ops.0.index"

    def test_跨父移动时子树整体搬家(self):
        tree = tree_of(Node(id="a", label="A", children=[Node(id="a1", label="A1")]),
                       Node(id="b", label="B"))
        result = apply_ops(tree, batch_of(
            {"op": "move", "id": "a1", "new_parent_id": "b", "index": 0}))
        assert ids_of(result.root.children) == ["a", "b"]
        assert result.root.children[0].children == []
        assert ids_of(result.root.children[1].children) == ["a1"]


class BatchAtomicityTest(unittest.TestCase):
    """整批要么全成、要么全不成；调用方永远看不到半批生效的树。"""

    def test_中途失败时传入的树零变化(self):
        tree = tree_of(Node(id="a", label="A", source_refs=[SourceRef(source_id="s1", quote="原文")]),
                       source_usage=SOURCE_USAGE)
        before = tree.model_dump()
        batch = batch_of(
            {"op": "add", "id": "n_x", "parent_id": "r", "index": 1, "label": "第一条会成功"},
            {"op": "update", "id": "没有这个", "changes": {"label": "第二条必失败"}},
            {"op": "remove", "id": "a"},
        )

        with pytest.raises(OpError) as raised:
            apply_ops(tree, batch)

        # 下标必须指到第 2 条（0 起算的 1），模型才知道该改哪条
        assert raised.value.code == "node_not_found"
        assert raised.value.path == "ops.1.id"
        # 第 1 条的 add 不能留在树上，第 3 条更不能被执行
        assert tree.model_dump() == before

    def test_成功时也不修改传入的树(self):
        tree = tree_of(Node(id="a", label="A"))
        before = tree.model_dump()
        result = apply_ops(tree, batch_of(
            {"op": "add", "id": "n_new", "parent_id": "r", "index": 0, "label": "新"}))
        assert tree.model_dump() == before
        assert result is not tree
        assert ids_of(result.root.children) == ["n_new", "a"]


class TreeValidationTest(unittest.TestCase):
    """op 全部执行成功不等于结果合法：整树上限在最后统一收口。"""

    @staticmethod
    def build_300_node_tree() -> MindMapContent:
        """正好 300 个节点：根 + 6 个分支 + 293 个叶子，每个分支都 ≤50 子。"""
        branches = []
        for branch, leaf_count in enumerate([50, 50, 50, 50, 50, 43]):
            leaves = [Node(id=f"b{branch}_{leaf}", label="叶子") for leaf in range(leaf_count)]
            branches.append(Node(id=f"b{branch}", label="分支", children=leaves))
        tree = tree_of(*branches)
        assert 1 + len(branches) + sum(len(node.children) for node in branches) == 300
        return tree

    def test_超过_300_个节点报_tree_invalid(self):
        tree = self.build_300_node_tree()  # 300 本身合法，能构造出来
        with pytest.raises(OpError) as raised:
            apply_ops(tree, batch_of(
                {"op": "add", "id": "n_第301个", "parent_id": "b5", "index": 0, "label": "第 301 个"}))
        assert raised.value.code == "tree_invalid"
        assert raised.value.path == "ops"

    def test_超过_8_层报_tree_invalid(self):
        # 根是第 1 层；把新节点挂到第 8 层下面就是第 9 层。
        deepest = Node(id="l8", label="第 8 层")
        for level in range(7, 1, -1):
            deepest = Node(id=f"l{level}", label=f"第 {level} 层", children=[deepest])
        tree = tree_of(deepest)  # 8 层本身合法

        with pytest.raises(OpError) as raised:
            apply_ops(tree, batch_of(
                {"op": "add", "id": "n_l9", "parent_id": "l8", "index": 0, "label": "第 9 层"}))
        assert raised.value.code == "tree_invalid"
        assert raised.value.path == "ops"

        # 对照：挂到第 7 层下仍是第 8 层，必须放行。否则"超限"的断言可能只是
        # 在证明"所有 add 都失败"。
        ok = apply_ops(tree, batch_of(
            {"op": "add", "id": "n_l8_new", "parent_id": "l7", "index": 0, "label": "第 8 层"}))
        assert find_node(ok.root, "n_l8_new") is not None

    def test_单节点子节点超过_50_报_tree_invalid(self):
        tree = tree_of(*[Node(id=f"c{leaf}", label="子") for leaf in range(50)])  # 50 正好合法
        with pytest.raises(OpError) as raised:
            apply_ops(tree, batch_of(
                {"op": "add", "id": "n_第51个", "parent_id": "r", "index": 50, "label": "第 51 个"}))
        assert raised.value.code == "tree_invalid"
        assert raised.value.path == "ops"


class NormalizeOpsTest(unittest.TestCase):
    """草案里的 `$tmp1` 是占位名，要换成本次分配的真实 id 并全局改写引用。"""

    def test_临时_id_被分配成真实_id_且引用同步改写(self):
        drafts = [
            {"op": "add", "id": "$tmp1", "parent_id": "r", "index": 0, "label": "新分支"},
            {"op": "add", "id": "$tmp2", "parent_id": "$tmp1", "index": 0, "label": "子节点"},
        ]
        ops, mapping = normalize_ops(drafts, {"r"})

        assert set(mapping) == {"$tmp1", "$tmp2"}
        real_parent, real_child = mapping["$tmp1"], mapping["$tmp2"]
        assert ops[0].id == real_parent
        assert ops[1].id == real_child
        assert ops[1].parent_id == real_parent      # 引用被改写了，这是整件事的重点
        assert real_parent.startswith("n_") and len(real_parent) == 2 + 32
        assert real_parent != real_child

        # 同一条链路执行一遍：规范化之后的 op 必须真的能被执行器接受
        result = apply_ops(tree_of(), OpBatch(batch_id="b", base_revision=0, ops=ops))
        assert ids_of(find_node(result.root, real_parent).children) == [real_child]

    def test_非美元开头的_id_原样保留(self):
        ops, mapping = normalize_ops([{"op": "update", "id": "a", "changes": {"label": "改名"}}], {"r", "a"})
        assert mapping == {}
        assert ops[0].id == "a"

    def test_move_的_new_parent_id_也被改写(self):
        drafts = [
            {"op": "add", "id": "$tmp1", "parent_id": "r", "index": 0, "label": "新父"},
            {"op": "move", "id": "a", "new_parent_id": "$tmp1", "index": 0},
        ]
        ops, mapping = normalize_ops(drafts, {"r", "a"})
        assert ops[1].new_parent_id == mapping["$tmp1"]

    def test_同一临时_id_多次出现只分配一次(self):
        """模型在两处引用同一个 `$tmp1`，两次必须是同一个节点，不能各分配一个。"""
        drafts = [
            {"op": "add", "id": "$tmp1", "parent_id": "r", "index": 0, "label": "父"},
            {"op": "add", "id": "$tmp2", "parent_id": "$tmp1", "index": 0, "label": "子一"},
            {"op": "add", "id": "$tmp3", "parent_id": "$tmp1", "index": 1, "label": "子二"},
        ]
        ops, mapping = normalize_ops(drafts, {"r"})
        assert ops[1].parent_id == ops[2].parent_id == mapping["$tmp1"]

    def test_不改动传入的草案与既有_id_集合(self):
        drafts = [{"op": "add", "id": "$tmp1", "parent_id": "r", "index": 0, "label": "新"}]
        existing = {"r"}
        normalize_ops(drafts, existing)
        assert drafts[0]["id"] == "$tmp1"   # 草案不能被就地改写
        assert existing == {"r"}            # 既有 id 集合不能被塞进新 id
