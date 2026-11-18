"""三种图型的结构适配层：遍历、定位、计数，以及生成出口的形状转换。

## 为什么需要这一层

契约层已经有三种顶层形状（`MindMapContent.root` / `FishboneContent.bones[i].root`
/ `FlowContent.stages[i].root`），而 agent 侧的每一个消费点原先都写死了
`content.root`——引用校验、节点计数、体检、展开、编辑全部假设"导图是一棵树"。
三套结构落地之后那些点必须知道"节点藏在哪儿"。

## 为什么是"读侧适配"，不是"整体转成树、处理完再转回"

两条路都试过一遍的结论，判据是**可逆性需求**，不是代码位置：

  - **生成侧必须转**（并且只转一次、单向）：组装流水线——`apply_ops` 的原子批、
    并发展开、节点额度裁剪、`normalize_ops` 的临时 id——全建立在"一棵可变的
    父子树"上，那是一千多行有实测数字钉着的机制。把它在鱼骨与流程上各实现一遍，
    重复的不是几行循环，是并发与裁剪的边界条件。
  - **读侧不能转**：`bones[i]` 是**类别**、`stages[i].id` 是**另一个命名空间**
    （`FlowStage` 的 docstring 写明了阶段 id 只被 `FlowContent.edges` 引用），
    这两件事在 `Node` 里没有任何字段能承载（`Node` 是 `extra="forbid"`）。要把
    它们转成树再转回来，就得在树之外另存一份"谁是骨、谁是阶段"的映射——同一份
    信息两份表示同时存在，任何一处改动都要双向同步，而不同步的表现是"骨少了
    一根"这种静默错误。相比之下，读侧真正需要的只有两件事：
    **遍历所有 Node** 与 **按 id 定位并就地替换**，一份实现就够。

所以这一层提供的都是"与形状无关"的操作；调用方（`tools`）只改一行的写法，
不再各写三遍。写操作（新增子节点）也走这里，但它借道的是"把目标节点当成一棵
临时子树的根、交给 `apply_ops`、再写回原位"（`tools._apply_ops_at`）——原子性
与 id 规范化仍然只有一份实现。

## 一条必须记住的边界

`replace_node` / `revalidate` 之后**形状不变**：给鱼骨图加一个子节点，产物仍然是
`FishboneContent`。三套结构各自的语义（骨要不要重排、阶段要不要串线）是阶段 3
的事，这里只保证"能定位、能改、改完还是原来那种图"。
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Literal

from app.contracts.mindmap import (
    DiagramContent,
    FishboneBone,
    FishboneContent,
    FlowContent,
    FlowStage,
    MindMapContent,
    Node,
)

# 三套结构的短名。与 `contracts.mindmap.DIAGRAM_TYPES` 同值，但那张表的类型是
# `DiagramKind`（请求/产物共用的 Literal）；这里要的是一个能作字典键的普通
# `str`，两者在运行期相同、在类型检查上是两件事。
StructureKind = Literal["tree", "fishbone", "flowchart"]


def kind_of(content: DiagramContent) -> StructureKind:
    """按**类型**判形状（不看 `diagram_type` 字段）。

    `diagram_type` 是产物自述，库里存在"标着 fishbone、形状是树"的历史记录
    （阶段 2 之前生成侧就是这么产出的），拿它做分派会让那些图走进不存在的分支。
    类型是真形状，且 `DiagramContent` 的判别本来就是靠形状做的。
    """
    if isinstance(content, FishboneContent):
        return "fishbone"
    if isinstance(content, FlowContent):
        return "flowchart"
    return "tree"


# ---------------------------------------------------------------- 读：遍历与计数


def roots(content: DiagramContent) -> list[Node]:
    """各子树自己的根，按画布上的阅读顺序。

    树只有一根；鱼骨是六根骨；流程是若干阶段。**骨与阶段本身不算"内容根"之外
    的东西**：`FishboneBone.root` / `FlowStage.root` 就是那个 Node，骨与阶段的
    容器字段（`category` / `id`）不在这份返回值里。
    """
    if isinstance(content, FishboneContent):
        return [bone.root for bone in content.bones]
    if isinstance(content, FlowContent):
        return [stage.root for stage in content.stages]
    return [content.root]


def top_branches(content: DiagramContent) -> list[Node]:
    """画布最外那一层**内容节点**：树是一级分支，鱼骨是六根骨，流程是各阶段。

    与 `roots()` 的差别只在树：树的最外层内容节点是 `root.children`（中心主题是
    标题，不是一块内容），而鱼骨/流程的 `roots()` 本身就是最外层内容。

    诊断口径（`audit_tree` 的 missing / thin、`_sparse_branch` 的判据）全部按这一
    层数——"这一支从来没用过展开"的对象是骨与阶段，不是主骨或流程本身。
    """
    if isinstance(content, MindMapContent):
        return list(content.root.children)
    return roots(content)


def walk_nodes(root: Node) -> Iterator[Node]:
    """前序（父在子前）遍历一棵子树。"""
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.children))


def iter_nodes(content: DiagramContent) -> Iterator[Node]:
    """遍历这份内容的**全部**节点，多棵并列子树按 `roots()` 的顺序相接。

    这个顺序是有意义的：诊断与"第几个节点"的措辞都按它数，而它正好等于画布上
    从上到下、从左到右的阅读顺序。
    """
    for root in roots(content):
        yield from walk_nodes(root)


def walk_depths(content: DiagramContent) -> Iterator[tuple[Node, int]]:
    """`(节点, 层号)`。层号**按每棵子树**从 1 数。

    与契约的 `_scan_tree` 同一条口径（见那里的注释）：一棵骨挂在主骨上，但画布上
    它是从自己的根展开的一列，层数限制针对的正是那个"列"。
    """
    for root in roots(content):
        stack: list[tuple[Node, int]] = [(root, 1)]
        while stack:
            node, depth = stack.pop()
            yield node, depth
            stack.extend((child, depth + 1) for child in node.children)


def count_nodes(content: DiagramContent) -> int:
    return sum(1 for _ in iter_nodes(content))


def count_refs(content: DiagramContent) -> int:
    return sum(len(node.source_refs) for node in iter_nodes(content))


def node_ids(content: DiagramContent) -> set[str]:
    return {node.id for node in iter_nodes(content)}


def find(content: DiagramContent, node_id: str) -> Node | None:
    """按 id 找节点，找不到返回 `None`。**含每棵子树的根自己。**"""
    for node in iter_nodes(content):
        if node.id == node_id:
            return node
    return None


def find_parent(content: DiagramContent, node_id: str) -> Node | None:
    """按 id 找父节点。**子树根没有父节点**（返回 `None`），哪怕它在画布上挂在
    主骨/阶段上——那层挂载关系由 `FishboneBone` / `FlowStage` 表达，不是 `Node`
    的父子关系，把它当成"父节点"会让"移到一个子树根下面"变成一个不存在的操作。
    """
    for node in iter_nodes(content):
        for child in node.children:
            if child.id == node_id:
                return node
    return None


def path_to(content: DiagramContent, node_id: str) -> list[Node] | None:
    """`子树根 → … → 目标节点` 的节点链；找不到返回 `None`。

    链首是**它所在那棵子树的根**，不是"整张图的第一个节点"：鱼骨图里一个原因的
    祖先链里不该出现另一根骨的内容。
    """
    parents: dict[str, Node] = {}
    for node in iter_nodes(content):
        for child in node.children:
            parents[child.id] = node
    target = find(content, node_id)
    if target is None:
        return None
    chain = [target]
    cursor = target
    while cursor.id in parents:
        cursor = parents[cursor.id]
        chain.append(cursor)
    chain.reverse()
    return chain


def depth_of(content: DiagramContent, node_id: str) -> int | None:
    """节点在第几层（它所在子树的根为 1）。找不到返回 `None`。"""
    for node, depth in walk_depths(content):
        if node.id == node_id:
            return depth
    return None


def index_path(content: DiagramContent, node_id: str) -> list[int] | None:
    """从**子树根**出发的子索引链（`[]` = 子树根自己）；找不到返回 `None`。

    它只服务生长预览（`NodeGrowEvent.path` 是父节点的索引链）。鱼骨与流程返回
    `None`：那条链在前端要落到"根节点 → 第 i 个子节点"的坐标上，而鱼骨/流程的
    顶层容器不是节点，预览落点没有对应的位置——宁可没有预览，也不给一个错位置。
    """
    if not isinstance(content, MindMapContent):
        return None
    if content.root.id == node_id:
        return []
    stack: list[tuple[Node, list[int]]] = [(content.root, [])]
    while stack:
        node, path = stack.pop()
        for index, child in enumerate(node.children):
            if child.id == node_id:
                return [*path, index]
            stack.append((child, [*path, index]))
    return None


# ---------------------------------------------------------------- 写：按 id 就地替换


def _map_subtree(node: Node, node_id: str, fn: Callable[[Node], Node]) -> tuple[Node, bool]:
    """在子树里找到 `node_id`，对它应用 `fn`，返回 `(新子树, 是否命中)`。

    未命中的分支**原样返回同一个对象**（不重建）：`_generate` 收尾时会拿新旧对象
    比对（`content.root.children == applied` 那类判等），无谓的复制会让"什么都没改"
    看起来像改过。
    """
    if node.id == node_id:
        return fn(node), True
    changed = False
    children: list[Node] = []
    for child in node.children:
        new_child, hit = _map_subtree(child, node_id, fn)
        changed = changed or hit
        children.append(new_child)
    if not changed:
        return node, False
    return node.model_copy(update={"children": children}), True


def replace_node(
    content: DiagramContent, node_id: str, fn: Callable[[Node], Node],
) -> DiagramContent:
    """把 `node_id` 换成 `fn(node)`，形状不变。找不到时原样返回。

    找不到返回原对象而不是抛错：调用方（`_expand` / `_edit`）在调它之前都已经
    判过存在性，这里再抛一次只会多一条不会有人读的栈。
    """
    if isinstance(content, FishboneContent):
        bones: list[FishboneBone] = []
        hit = False
        for bone in content.bones:
            root, found = _map_subtree(bone.root, node_id, fn)
            hit = hit or found
            bones.append(bone.model_copy(update={"root": root}) if found else bone)
        if not hit:
            return content
        return content.model_copy(update={"bones": bones})
    if isinstance(content, FlowContent):
        stages: list[FlowStage] = []
        hit = False
        for stage in content.stages:
            root, found = _map_subtree(stage.root, node_id, fn)
            hit = hit or found
            stages.append(stage.model_copy(update={"root": root}) if found else stage)
        if not hit:
            return content
        return content.model_copy(update={"stages": stages})
    root, hit = _map_subtree(content.root, node_id, fn)
    if not hit:
        return content
    return content.model_copy(update={"root": root})


def revalidate(content: DiagramContent) -> DiagramContent:
    """重跑一次契约校验，返回校验过的同一形状。

    `model_copy` 不做校验，而 `replace_node` 是"结构被代码改过"的入口——不校验
    就等于相信自己的算术（同 `tools._clip_tree` 结尾那一句的理由）。走
    `model_dump()` 再 `model_validate()` 而不是直接传实例：pydantic v2 默认
    `revalidate_instances="never"`，传实例会原样返回，这层校验等于不存在。
    """
    return type(content).model_validate(content.model_dump())


# ---------------------------------------------------------------- 生成出口：单向转换
#
# 这一段只服务**生成侧**：流水线内部跑在一棵树上（见模块注释），出口处按请求的
# 图型把树折成 FishboneContent / FlowContent。转换是单向的（转出去就不再转回），
# 所以不需要任何"谁是骨"的映射留在树里。


def to_fishbone(
    content: MindMapContent, *, problem: str,
) -> tuple[FishboneContent, int]:
    """树 → 鱼骨图：一级分支成为六根骨，根（中心主题）不再出现在产物里。

    三处映射都有依据：

      - `category` 取一级分支自己的标题：骨架那一次要求的就是"人 / 机 / 料 / 法 /
        环 / 测"，`_apply_skeleton` 已经把那条分支的颜色/图标套在同一个节点上；
      - `bone.color` / `bone.icon` 取节点上的值（骨架产物 → 骨的视觉元素）；
      - `bone.root` 就是那个节点本身，它的 `children` 就是原因。

    **根节点下的多余节点**（模型没照骨架写、超出六类的一级分支）挂到最后一根骨
    下：丢掉是丢内容，而"哪一根骨都不属于它"这件事没有第三种答案。调用方会在
    返回值 `extras` 非空时记一条诊断，让用户知道发生过这次搬迁。
    """
    from app.contracts.mindmap import MAX_FISHBONE_BONES

    children = list(content.root.children)
    kept = children[:MAX_FISHBONE_BONES]
    extras = children[MAX_FISHBONE_BONES:]
    if kept and extras:
        last = kept[-1]
        kept[-1] = last.model_copy(
            update={"children": [*last.children, *extras]})
    bones = [
        FishboneBone(
            category=child.label,
            root=child,
            color=child.color,
            icon=child.icon,
        )
        for child in kept
    ]
    kept_ids = {node.id for bone in bones for node in walk_nodes(bone.root)}
    return FishboneContent(
        title=content.title,
        problem=problem,
        bones=bones,
        theme=content.theme,
        # 关系线的端点是节点 id，而中心主题不在产物里：留着它是一条指向不存在
        # 节点的关系，契约层会直接拒（`关系 X 的起点节点 Y 不在导图中`）。
        relations=[
            item for item in content.relations
            if item.source in kept_ids and item.target in kept_ids
        ],
        source_usage=content.source_usage,
    ), len(extras)


def to_flow(content: MindMapContent) -> tuple[FlowContent, int]:
    """树 → 流程图：一级分支持成为**阶段**，根不再出现在产物里。

    映射的三条规则：

      - 阶段顺序 = 一级分支的顺序（执行先后由数组下标表达，见 `FlowContent` 的
        注释）；`FlowStage.id` 用 `s_` 前缀，与节点 id 分属两个命名空间——契约
        明确要求它们是两个命名空间，同值会让"这条边连的是阶段还是步骤"只能靠
        形状猜；
      - `root` 就是那条一级分支节点（阶段内部的步骤树）；
      - `edges` 的端点从节点 id 重映射到阶段 id。**指向根的边整条丢掉**：流程的
        主轴就是阶段序列，"从中心主题出发"那条边没有对应的阶段，它的信息量也不比
        数组下标多。丢掉的条数返回给调用方报诊断。

    返回 `(产物, 丢掉的边数)`。
    """
    children = list(content.root.children)
    stages: list[FlowStage] = []
    stage_of: dict[str, str] = {}
    for child in children:
        stage_id = f"s_{child.id}"
        stage_of[child.id] = stage_id
        stages.append(FlowStage(id=stage_id, root=child))
    if not stages:
        # 契约要求 `stages` 至少一个。走到这里说明上游已经放行了一棵没有一级分支
        # 的树（`MIN_BRANCHES` 门槛会先拦住），保底补一个阶段而不是抛异常——用户
        # 拿到一张"只有一段的流程"好过拿到一个 500。
        stages.append(FlowStage(id=f"s_{content.root.id}", root=content.root))
        stage_of[content.root.id] = stages[0].id

    edges = []
    dropped_edges = 0
    for edge in content.edges:
        source = stage_of.get(edge.source)
        target = stage_of.get(edge.target)
        if source is None or target is None:
            dropped_edges += 1
            continue
        edges.append(edge.model_copy(update={"source": source, "target": target}))

    node_ids = {node.id for node in walk_nodes(content.root)}
    return FlowContent(
        title=content.title,
        stages=stages,
        edges=edges,
        theme=content.theme,
        relations=[
            item for item in content.relations
            if item.source in node_ids and item.target in node_ids
        ],
        source_usage=content.source_usage,
    ), dropped_edges
