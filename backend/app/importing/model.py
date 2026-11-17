"""导入的中间表示：解析器只产出这些结构，限额与清洗只在一处做。

为什么要多这一层 `RawNode` / `RawMap`，而不是让每个解析器直接构造 `Node`：

  1. **五件边界处理（id 重生成、1000/8/50 限额、label 上限、视觉字段映射、
     引用清空）只能有一份实现。** 六个格式各写一遍，结局一定是 markdown 那条
     路漏了 50 子上限、xmind 那条路忘了清 `source_refs`——而两处错误都不会
     报错，只会在某天保存时 422 或让引用核验崩。
  2. **BFS 截断要求先有整棵树。** XML/JSON 天然是递归/DFS 解析，边解析边截断
     就只能截成 DFS（一条分支挖到底）。先把文件读成一棵"形状忠实、但没有限额"
     的中间树，再由 `normalize` 按层序裁，才能保证"每个一级分支都留下"。
  3. **解析期的上限是另一件事**：`RAW_NODE_HARD_CAP` 挡的是"十万节点的文件"，
     它属于"读得进内存吗"，与契约的 1000 条不是同一个数字，也不该共用一个常量。

`counters` / `hints` 是给解析器留下"这件事我看到了、但归一化层不知道"的痕迹：
XMind 的多画布、游离主题、被丢掉的 hex 颜色。归一化层把它们原样转成 warning
——**降级要留下降过级的痕迹**（`AGENTS.md` 的第 5 条约定）。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RawNode:
    """文件里的一个节点。字段全是"文件说了什么"，不做任何契约校验。"""

    label: str
    # 文件里的原始 id。只有我们自己的 JSON 有它；markdown / opml / puml 的边
    # 引用的是**文字**，那两个字段因此留空，边的端点在 normalize 层按 label 查。
    old_id: str | None = None
    note: str | None = None
    detail: str | None = None
    # 原始色值：可能是调色板键（我们自己的 JSON），也可能是 `#RRGGBB`（XMind /
    # FreeMind）。归一化层只认调色板键，hex 由 `colors.palette_for_hex` 精确
    # 映射，映射不到就丢掉——**不猜**。
    color: str | None = None
    icon: str | None = None
    collapsed: bool = False
    expandable: str | None = None
    # `NodeStyle` / `Position` 的原始 dict。JSON 才有；外部格式没有对应概念。
    style: dict | None = None
    pos: dict | None = None
    children: list[RawNode] = field(default_factory=list)


@dataclass
class RawEdge:
    """附加关系。`source` / `target` 可能是旧 id，也可能是节点文字。

    两种都收：JSON 的边是 id，markdown/opml/puml 的边是文字。normalize 先按
    旧 id 查映射，查不到再按文字查——一条路覆盖两种来源，不需要每个解析器各自
    决定"边该用什么当端点"。
    """

    source: str
    target: str
    label: str | None = None
    kind: str | None = None


@dataclass
class RawMap:
    root: RawNode
    title: str | None = None
    layout: str | None = None
    diagram_type: str | None = None
    theme: dict | None = None
    # `Relation` 的原始 dict：两端也要按 id 重映射，所以不能在这里变成对象。
    relations: list[dict] = field(default_factory=list)
    edges: list[RawEdge] = field(default_factory=list)
    # 解析器已经能定性的告警，`(code, message)`。直接进结果。
    hints: list[tuple[str, str]] = field(default_factory=list)
    # 解析器的计数（目前只有 `source_refs`）：归一化层看不到原始 JSON 里的引用
    # 数组，而"导入了 12 处引用并清空"这句话有用例钉着，必须数得出来。
    counters: dict[str, int] = field(default_factory=dict)
    # 解析期就撞上 `RAW_NODE_HARD_CAP` 时置真：这条要说出来，否则用户会以为
    # "文件里只有 5000 个节点"，而实际是他那份文件被读了一半。
    over_hard_cap: bool = False


@dataclass(frozen=True)
class ImportWarning:
    """一条导入告警。**只有 code 与 message**：定位信息（第几行、哪个节点）

    写进 message 正文，而不是加 `line` / `node` 字段——多出来的字段会让响应
    契约与前端类型各漂一次，而"第 12 行"写在句子里读起来并不更差。
    """

    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


# 解析期的硬上限。它与契约的 `MAX_TREE_NODES`（1000）**不是同一个数字**：
#
#   1000 是承载能力（前端渲染、保存、导出都扛得住），
#   5000 是"读得进内存吗"——一份 10MB 的 markdown 能造出几十万个列表项，先建
#        全树再按 BFS 裁会把这些节点全建出来。到 5000 就停，报一条"文件太大，
#        只读了前 5000 个"。
#
# 选 5 倍而不是相等：BFS 截断要从整棵树里挑"层序最靠前的 1000 个"，而解析顺序
# 是 DFS（XML/JSON 天然递归）。若解析期也只留 1000 个，前 1000 个可能全部落在
# 第一个分支里，BFS 就退化成"按解析顺序保留"，截断策略等于没生效。
RAW_NODE_HARD_CAP = 5000

# 解析器自己发现、又不属于五件边界处理的告警码：多画布只导第一张、游离主题丢掉、
# 多个顶层节点归到根下。**放这里而不是各解析器里写字面量**：`normalize` 也会读它
# （hints 原样进结果），两处拼写不一致的后果是前端按码分组时漏掉一整类提示。
FORMAT_NOTICE = "format_notice"


class Budget:
    """解析期的节点计数器。

    每个解析器都要有它：外部文件的节点数不受我们控制，而"先建全树再截断"意味着
    恶意或误会的一份文件能把内存吃满。`take()` 返回 False 时解析器停止造节点，
    并把 `RawMap.over_hard_cap` 置真——那条告警必须说出来，否则用户会以为
    "文件里只有 5000 个节点"。
    """

    __slots__ = ("cap", "used")

    def __init__(self, cap: int = RAW_NODE_HARD_CAP) -> None:
        self.cap = cap
        self.used = 0

    def take(self) -> bool:
        if self.used >= self.cap:
            return False
        self.used += 1
        return True
