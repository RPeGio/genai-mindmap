"""导图契约（v2，见 `docs/CONTRACT_V2.md`）。

v1 只有 `id`/`label`/`children`/`source_refs`。v2 补齐了画布与引用需要的字段：
正文 `note`、布局 `layout`、主题 `theme`、跨节点关系 `relations`，以及节点级的
样式与手动坐标。**新字段全部有默认值**，所以库里那些没有 `schema_version` 的
v1 记录不需要迁移脚本就能读进来（`migrate_content()` 负责这一步，见文件末尾），
保存时自然写成 v2。
"""
from __future__ import annotations

import unicodedata
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.contracts.source import SourceRef, SourceUsage

# 整树节点上限。1000 是**承载能力**：前端渲染、保存、导出都还扛得住，所以它是
# 契约的硬边界而不是产品口径。原先的 300 是照「一句话主题」场景拍的，一份
# 12106 字的材料实测就能长出 526 个节点——上限比材料要求还低时，被砍掉的是
# 用户的内容而不是模型的发挥。
MAX_TREE_NODES = 1000

# 层级上限。8 层是可读性约束（再深，横向滚动换来的信息量已经抵不过"看不见全貌"），
# 不是承载能力，所以它保持不变。
MAX_TREE_DEPTH = 8

# 单个节点的子节点上限。它是**硬边界**（`Node.children` 的 `max_length`），
# 与另外两个限额不同：超过不是"该告警"，而是契约层直接 422。
#
# 50 这个数是横向排布的承载能力：一级分支 50 个已经要滚动三屏，再多就不是"这个
# 主题有 50 个方面"，而是"这里没被整理过"。导入侧要用它做截断（超了砍掉并告警），
# 所以它必须有常量名——写死在 `Field(max_length=50)` 里时，导入层只能重复一个 50。
MAX_NODE_CHILDREN = 50

# 关系条数上限。200 条大约是一张 1000 节点导图里"跨分支解释性关系"的合理密度，
# 再多的画布也读不清——那时用户要的其实是子节点，不是连线。
MAX_RELATIONS = 200

# 附加边（`MindMapContent.edges`）的条数上限。200 与 `MAX_RELATIONS` 同口径：
# 一张 1000 节点的图里"跨父子节点的附加关系"超过 200 条时，画布已经读不清了——
# 那时用户要的其实是节点，不是连线。
#
# 与 `relations` 的差别只有一处，但那处很重要：**这是 warn 口径，不是硬边界**。
# relations 超过 200 由 `Field(max_length=...)` 直接拒；edges 超过 200 只报
# `EDGE_LIMIT`（见 `app/mindmaps/edges.py`），图照常能存、能画。理由是 flow
# 图里边的数量天然随分支数增长，为了几条多出来的连线把整张图判失败，代价方向
# 完全错了。硬边界在下面的 `MAX_EDGES_LIMIT`，它只挡真正会让传输/渲染出问题
# 的量级。
MAX_EDGES = 200

# `edges` 的承载硬边界 = 10 × 产品口径。它挡的是"一份 JSON 里塞十万条边"这种
# 滥用，不是"这张图画得有点密"。两个数分开，等于把"该告警"和"不能接受"分开。
MAX_EDGES_LIMIT = MAX_EDGES * 10

# 边上的文字上限。边上只该放一个词或一小句（"是" / "否" / "通过" / "驳回"），
# 40 字够写「驳回：材料不全，退回补正」。再长的内容属于节点详情（`detail`）。
MAX_EDGE_LABEL_CHARS = 40

# 最长环的节点数。流程图里的环是**回退/返工**，一屏能看懂的回路不超过十来个
# 节点；再长的环在画布上会绕成一团，用户看不出从哪里回到哪里。超了报
# `EDGE_CYCLE_TOO_LONG`（warn），不硬拒——把已经画出来的图整张判失败，代价
# 远大于"这个环有点长"这件事本身。
MAX_EDGE_CYCLE_NODES = 12

# 边的语义类型。三个值对应三种画法（前端的自定义边组件按它换样式）：
#   - `flow`：主干上的顺序跳转（默认，画实线箭头）
#   - `branch`：判断节点的条件出口（"是"/"否"两种去向，换色）
#   - `loop`：回退（虚线 + 箭头，一眼看出"流程往回走"）
#
# 为什么是 `Literal` 而不是自由字符串：颜色/线型要按它选，而自由字符串的意思
# 是"每一个没见过的值都渲染成默认样式"——那三个值就白定义了。模型写错时契约层
# 会拒，模型能照着错误信息改对；渲染层则永远只处理这三支。
EDGE_KINDS = ("flow", "branch", "loop")
EdgeKind = Literal["flow", "branch", "loop"]

# 节点标题（`label`）的**契约上限**= 200。这是给旧图留的读取口径：库里那些
# 一句话当标题的记录（实测 20–30 字，最长的一批贴着 200）必须照常打得开。
MAX_NODE_LABEL_CHARS = 200

# AI 生成时的标题上限 = 16 码点。这是**生成侧**的约束（`generation.service` 与
# `agent.tools` 的归一化都读它），不是契约校验：把 `Field(max_length=200)` 直接
# 改成 16，等于让所有旧图在下一次保存时 422——用户什么都没改，图却存不回去。
#
# 16 这个数的依据是画布：`180px` 宽的圆角框里放 14px 字，一行约 12 个字、两行
# 24 个字，16 字是"缩到能看见全貌"之后仍读得出的长度。超过它的内容属于详情
# （`detail`），不属于标题。
GENERATED_LABEL_MAX_CHARS = 16

# 节点详情上限。与 `note` 同为 2000：同一块文字在界面上占同一块地方，两个字段
# 的容量不该一个够写、一个不够写。`detail` 与 `note` 的**语义**不同（生成产出
# vs 用户备注），所以只能分开存，不能合并（见 `Node.detail` 的注释）。
MAX_NODE_DETAIL_CHARS = 2000

# 一个节点还能不能再往下展开。**这是给界面画按钮用的状态，不是给模型看的信号**
# （模型那一侧的对应物是展开响应里的布尔 `expandable`，两回事）。
#
# 存进树里而不是每次现算，是因为它有三件事现算不出来：
#
#   1. **模型说过"到底了"**。只有写那次响应的模型知道"这个要点已经是最小可执行
#      单元"，那个判断随调用一起消失；不落库，下次打开这张图就只能猜。
#   2. **被额度截断过**。截断发生在生成过程中，事后从形状上看与"模型说没有更多
#      内容"完全一样（都是个叶子），而两者对用户的意思相反：一个是"到头了"，
#      一个是"没展开完，还能再点一次"。
#   3. **层数到顶**。第 8 层是契约硬边界（`MAX_TREE_DEPTH`），加不出子节点。
#
# 三态而不是布尔：`unknown` 必须与 `no` 分开。被额度截断的节点若被记成 `no`，
# 界面上按钮灰掉、说明"已经到底了"——用户被一句假话挡住了唯一能继续的入口，
# 而这张图看起来是完整的。反过来把真的到头的节点记成 `yes`，用户点了只有"没
# 有更多内容"可选，白花一次调用。
NODE_EXPANDABLE = ("yes", "no", "unknown")

# 节点颜色的**调色板**。`Node.color` 只能取这八个键之一，不接受任意 hex。
#
# 为什么不做 `#RRGGBB`：模型自由发挥的 hex 会得到一堆脏色（同一张图里出现
# `#3f8ac9`、`#4a90d9`、`#2f7fbf` 三个几乎一样的蓝），而且换主题（暗色/亮色）
# 时无法自动适配——hex 是**结论**，不是语义。键是语义（`red` 就是"风险/告警"），
# 具体色值留给渲染层：CSS 变量一处定义，暗色主题只覆盖 `:root`，导图数据不动。
#
# 八个键是按"能表达一张图里的分类"选的，不是凑数：主色（blue）、正向/结论
# （green）、注意/待定（amber）、风险（red）、并列主题（purple）、过程/辅助
# （teal）、人/组织（pink）、背景/其他（gray）。再多就会开始重复语义，
# 而两个键意思一样时模型只会随机挑一个。
NODE_COLORS = ("blue", "green", "amber", "red", "purple", "teal", "pink", "gray")

# 调色板键 → 前端 CSS 变量名的前缀。**只有前缀在契约里**，具体色值在
# `frontend/src/styles.css` 的 `:root` 里定义。
#
# 放在这里是为了让"后端加了一个颜色、前端却没定义"这件事能被用例抓住
# （`tests/test_node_visuals.py` 读 styles.css 逐个比对）：静默漏一个键的表现是
# 那个颜色画出来是默认灰，而没有任何地方会报错。
NODE_COLOR_CSS_PREFIX = "--mind-color-"

# 节点的图标上限（码点）。emoji 常见 1–4 个码点（`🎯` 是 1，带肤色/ZWJ 的
# `👨‍👩‍👧` 可以到 8），16 给组合 emoji 留够余量，同时挡住"把一句话塞进图标"。
MAX_NODE_ICON_CHARS = 16

NodeColor = Literal["blue", "green", "amber", "red", "purple", "teal", "pink", "gray"]

# 图型。三个值同时是 `AgentRunRequest.diagram_type`（请求：用户要画什么）与
# `MindMapContent.diagram_type`（产物：画成了什么）的取值集合——一处定义，两处
# 引用。分成两份字面量写的后果是可预期的：某天加第四种图型，请求层收下了、
# 契约层 422，而报错会指向产物而不是那张没被加进去的表。
#
# **它不是 `layout`**：`layout` 描述排布（横向 / 纵向 / 时间线 / 手动坐标），
# 图型描述"这张图按什么读"——流程图要看条件分支与回退，鱼骨图的一级分支固定是
# 人 / 机 / 料 / 法 / 环 / 测。
DIAGRAM_TYPES = ("tree", "flowchart", "fishbone")
DiagramKind = Literal["tree", "flowchart", "fishbone"]

# 图型的中文名。导出文件头（`export.service.to_puml`）要用它：写成 `fishbone`
# 读不出意思，写成"因果图"又与界面上那个下拉的名字对不上。**只放中文名，不放
# 说明**——说明属于界面（`SourceInputs.vue` 里那四个 hint）。
DIAGRAM_LABELS = {"tree": "思维导图", "flowchart": "流程图", "fishbone": "鱼骨图"}


def normalize_color(value: object) -> str | None:
    """模型给的 `color` → 调色板里的键，或 `None`。

    三件事：非字符串/空串当没给；大小写与空白归一（模型常写 `"Blue "`）；
    **不在调色板里的一律丢掉，不报错**。

    为什么是"丢掉"而不是"报错"：这条函数只服务生成侧（骨架里的 color/icon）。
    一个 `color: "深蓝"` 或 `color: "#1e40af"` 说明模型没照提示词写，但它产出的
    结构本身是好的——为了一个装饰字段把一次已经花掉的骨架调用整批判失败，
    代价方向完全错了。契约层（`Node.color` 的 Literal）仍然是硬拒绝，
    所以脚本与前端构造的数据不会绕过白名单。
    """
    if not isinstance(value, str):
        return None
    key = value.strip().lower()
    return key if key in NODE_COLORS else None


def normalize_icon(value: object) -> str | None:
    """模型给的 `icon` → 可落库的 emoji，或 `None`。

    三件事：折掉空白、按 `MAX_NODE_ICON_CHARS` 截断、**把"写成文字的图标"丢掉**。

    第三条不是洁癖。实测模型会写 `"icon": "放大镜"` 或者 `"用眼睛表示观察"`
    ——它照着自己的话写了一个**词**。这种值落到树上会变成节点标题前面挂着四个
    汉字，看起来像标题的一部分，而用户没有任何办法知道"那个字本来应该是个图标"。
    所以按 Unicode 类别判：只要含字母或数字（`L*` / `N*`，中日韩汉字也在 `Lo`
    里）就不是图标，丢掉。emoji 是 `So`（Symbol, other），可以过。

    不追求"严格的 emoji 白名单"：那要跟着 Unicode 版本更新一张属性表，为一个
    装饰字段不值得。长度 + 字母/数字这两条挡住的是真实出现的两种坏值。
    """
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not text:
        return None
    for char in text:
        if unicodedata.category(char)[0] in ("L", "N"):
            return None
    return text[:MAX_NODE_ICON_CHARS]


def clip_label(text: str, limit: int = GENERATED_LABEL_MAX_CHARS) -> str:
    """按**码点**截断标题，返回不超过 `limit` 个码点的字符串。

    按码点而不是 UTF-16 下标：`Array.from` / Python 的 `str` 都以码点为单位，
    而 JS 的 `length` 会把一个 emoji 数成 2——用码点才能让两端对"16 字"给出同
    一个答案（前端 `edits.ts::charLength` 也是这个口径）。

    省略号**不**追加：它是 1 个码点，加上去会让"16 字"变成 17 字，于是再截一次、
    再判一次，最后谁也不知道上限到底是几。要显示省略号的地方（画布）自己加。
    """
    if limit <= 0:
        return ""
    chars = list(text or "")
    return "".join(chars[:limit])


def _fold(text: str) -> str:
    """把换行与连续空白折成单空格。

    导出三种格式里换行都会破坏结构（缩进、XML 属性），而节点标题里带换行的来源
    很具体：模型偶尔把一句话拆成两行返回。折在契约层比在三个导出函数里各折一次
    可靠，也让"同一个节点在画布和导出里文字一致"这件事有单一实现。
    """
    return " ".join(text.split())


class Position(BaseModel):
    """节点坐标。只有 `layout == "manual"` 时有意义——自动布局的坐标是算出来的，
    存进数据只会在下次换布局时被当成用户意图。"""
    model_config = ConfigDict(extra="forbid")
    x: float = Field(ge=-1e6, le=1e6)
    y: float = Field(ge=-1e6, le=1e6)


class NodeStyle(BaseModel):
    """节点样式（v2 预留字段，界面对它只做透传保存）。

    `style.color` 与 `Node.color` **不是同一个东西**，两者的关系必须写清楚，
    否则下一个人会以为它们是"新旧两版"而随手改坏一处：

      - `NodeStyle.color` 是 `#RRGGBB`，来源是导入/将来的取色器，**界面不渲染**。
        它是"用户画的那个色"，逐像素还原用。
      - `Node.color` 是**调色板键**（`blue`/`green`/…），界面按它渲染，能跟着
        主题（暗色/亮色）自动适配。

    两个字段同时存在时 **`Node.color` 优先**：语义色是渲染意图，hex 是历史值。
    `Node.color` 为 `None` 时才回落到…也不回落——`style.color` 仍然不渲染，
    它只是被保留下来，免得用户点一次保存就把导入的颜色丢掉。
    """

    model_config = ConfigDict(extra="forbid")
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    icon: str | None = Field(default=None, max_length=40)
    bold: bool = False
    italic: bool = False
    shape: Literal["rounded", "rect", "pill"] = "rounded"


class RelationType(str, Enum):
    cause = "cause"        # A 导致 B
    ref = "ref"            # A 引用/参见 B
    conflict = "conflict"  # A 与 B 冲突
    depends = "depends"    # A 依赖 B


class Relation(BaseModel):
    """跨节点的关系线。来源与目标是节点 id，必须都在树里。"""
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=100)
    source: str = Field(min_length=1, max_length=100)
    target: str = Field(min_length=1, max_length=100)
    type: RelationType
    label: str | None = Field(default=None, max_length=100)
    evidence: str | None = Field(default=None, max_length=300)
    provenance: Literal["user", "ai", "import"] = "user"

    @field_validator("evidence")
    @classmethod
    def blank_evidence_as_none(cls, value: str | None) -> str | None:
        return value if value and value.strip() else None


class Theme(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preset: Literal["default", "slate", "forest", "sunset", "mono"] = "default"
    accent: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    font: Literal["system", "serif", "mono"] = "system"
    direction: Literal["right", "left", "both"] = "right"


class LayoutKind(str, Enum):
    tree = "tree"          # 经典思维导图（左右/单向）
    vertical = "vertical"  # 根在上、子在下（画布上的"向下"）
    logic = "logic"        # 逻辑结构图（向右缩进的清单，与 vertical 不同）
    timeline = "timeline"
    fishbone = "fishbone"  # L3，不进 v1.0.0 的 UI
    manual = "manual"      # 用户拖拽后进入，pos 生效


class Edge(BaseModel):
    """跨父子节点的附加关系：流程图里的条件跳转、汇合、回退。

    **为什么是「树 + 附加边」而不是扁平的 `nodes + edges`**：树这一层被每一处
    依赖——`_generate_deep` 的骨架与按支展开、`INITIAL_MAX_NODES` / `MAX_DEPTH`
    的限额、`node_expand`、`expandable` 三态、`child_counts` 与 `path` 索引链、
    前端的 `flatten()` 与 tidy 布局、三种导出。全推倒的代价远超收益，而表达能力
    并不缺：**主干顺序 = 父链**，**条件分支 = 子节点**，**汇合与回退 = 这条边**。

    三件事刻意不做，每一条都有具体后果：

      - **不要求同父**。连到另一条 subtree 上正是"汇合"的定义；加一条"必须
        同父"等于把这个字段能表达的东西全部禁掉，流程图又退化回树。
      - **不要求无环**。A→B→A 是流程图的核心需求（驳回、返工、重试）。上限只有
        "最长环 ≤ `MAX_EDGE_CYCLE_NODES`"，而且是 warn。
      - **不参与布局**。节点坐标仍由树算出（`frontend/src/canvas/layout/tidy.ts`），
        边只画连线。边参与布局的话，用户每加一条线整个画布都会重排，刚记住的
        位置就没了——这是"树 + 边"能成立的前提之一。
    """

    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=100)
    source: str = Field(min_length=1, max_length=100)
    target: str = Field(min_length=1, max_length=100)
    # 边上的文字：「是」/「否」/「通过」/「驳回」。`None` 是"这条边没有文字"，
    # 不是空串——空串会让前端多一个"有标签但画出来什么都没有"的分支。
    label: str | None = Field(default=None, max_length=MAX_EDGE_LABEL_CHARS)
    # 语义类型，决定画法（见 `EDGE_KINDS`）。`None` 等于 `flow`：多数边就是普通
    # 的顺序箭头，逼调用方为每一条都声明 kind 只会得到一堆 `flow`。
    kind: EdgeKind | None = None

    @field_validator("label")
    @classmethod
    def blank_label_as_none(cls, value: str | None) -> str | None:
        """纯空白的标签等于没有标签（与 `Node.note` / `Node.detail` 同一条规矩）。"""
        return value if value and value.strip() else None


class Node(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=MAX_NODE_LABEL_CHARS)
    children: list[Node] = Field(default_factory=list, max_length=MAX_NODE_CHILDREN)
    # 正文。`label` 是短标题（画布上只放得下 200 字以内的两行），长的内容放这里。
    # 渲染取值收敛为 `note or label`，两处都空是不可能的（label 非空）。
    #
    # **`note` 是用户自己写的备注**：`agent/ops.py` 的 add/update 之外，只有
    # `stores/mindmap.ts::commitNote`（界面上的编辑框）会动它。所以任何"模型生成"
    # 的内容都**不许**写进 `note`——重新生成一次就会把用户敲的字整段盖掉，而那种
    # 丢失在界面上看不出来（它长得就像"模型又写了一遍"）。
    note: str | None = Field(default=None, max_length=MAX_NODE_DETAIL_CHARS)
    # 生成出来的说明。与 `note` 同容量、不同语义：`detail` 归生成（整树、分片
    # 展开、单节点展开都会填），`note` 归用户。界面上两者分区展示。
    #
    # 上限 2000 与 `note` 一致；超出的部分不静默截断——`Field` 会直接拒收，
    # 因为一条 3000 字的 detail 说明提示词本身没被遵守，该在生成侧归一化
    # （`tools._slot_from` 已经截了），不该让契约替它兜。
    detail: str | None = Field(default=None, max_length=MAX_NODE_DETAIL_CHARS)
    # 这一支还能不能再展开。三态的判定表见模块顶部 `NODE_EXPANDABLE` 的注释：
    #
    #   - `yes`：还能展开——有子节点但没长完，或者本身就是叶子；
    #   - `no`：已经到底——模型明确说没有更细的内容，或已经到第 8 层；
    #   - `unknown`：不确定——**被额度截断的不算 `no`**，那是"没展开完"，不是
    #     "没得展开"；旧图没有这个字段时也是它。
    #
    # 缺省取 `unknown` 而不是 `yes`：库里那些 v1/v2 记录从来没被问过这件事，
    # 把没问过的记成"还能展开"会让界面替旧图许下一个它兑现不了的承诺。
    expandable: Literal["yes", "no", "unknown"] = "unknown"
    collapsed: bool = False
    # 视觉元素：语义色 + 图标。**两个都可选、缺省 None**，所以库里所有旧图
    # 不需要迁移脚本就能读进来（与 `note` / `detail` / `expandable` 同一条规矩：
    # 新字段一律有默认值）。
    #
    # `color` 只能是 `NODE_COLORS` 里的键。用 `Literal` 而不是 pattern：非法值
    # 会在契约层直接被拒，而不是被画成一个脏色。生成侧另有一层归一化
    # （`normalize_color`），模型写了个不认识的键会被丢掉而不是让整棵树失败。
    #
    # 为什么加在 `Node` 上而不是继续用 `style.color`：语义色是**渲染意图**，
    # 见 `NodeStyle` 的 docstring。
    color: NodeColor | None = None
    # 图标。目前是 emoji（零依赖、跨平台、`<span>` 直接渲染），不是图标库的 id：
    # 前端没有图标库（`package.json` 里没有 `lucide-vue-next` 之类），为一个
    # 纯装饰字段新增一个依赖不划算。将来真上了图标库，这个字段可以保持
    # "字符串"不变——那时它承载的就是库里的 id。
    icon: str | None = Field(default=None, max_length=MAX_NODE_ICON_CHARS)
    style: NodeStyle | None = None
    source_refs: list[SourceRef] = Field(default_factory=list, max_length=20)
    pos: Position | None = None

    @field_validator("label")
    @classmethod
    def fold_label(cls, value: str) -> str:
        folded = _fold(value)
        if not folded:
            raise ValueError("节点标题不能为空")
        return folded

    @field_validator("icon")
    @classmethod
    def blank_icon_as_none(cls, value: str | None) -> str | None:
        """纯空白的图标等于没有图标。

        与 `note` / `detail` 同一条理由：留一个空串会让前端多一个"有图标但画出来
        什么都没有"的分支（`<span>` 里塞一个空格），而 `None` 的语义是明确的。
        """
        return value if value and value.strip() else None

    @field_validator("note", "detail")
    @classmethod
    def blank_text_as_none(cls, value: str | None) -> str | None:
        """纯空白的正文等于没写。

        留一个空串会让前端多一个"有正文但显示为空"的分支（详情区显示一块空白，
        回退逻辑永远不触发），而 `null` 的语义是明确的"没有这段内容"。
        """
        return value if value and value.strip() else None


class MindMapContent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[2] = 2
    title: str = Field(min_length=1, max_length=200)
    root: Node
    layout: LayoutKind = LayoutKind.tree
    theme: Theme = Field(default_factory=Theme)
    relations: list[Relation] = Field(default_factory=list, max_length=MAX_RELATIONS)
    source_usage: list[SourceUsage] = Field(default_factory=list, max_length=50)
    # 跨父子节点的附加边。**默认空列表**，旧图缺这个键时 Pydantic 直接补一个空
    # 列表——这就是"旧图零迁移"的全部实现，不需要改库、不需要停机、不需要
    # `schema_version` 升级（树 + 边的**树**那一半形状没变，读完写回时多出来的
    # `edges: []` 是无害的默认值）。
    edges: list[Edge] = Field(default_factory=list)
    # 这张图**是什么图型**。加它的理由是产物无法自证：一张鱼骨图与一张思维导图
    # 在树形状上可以长得一模一样（一级分支六条、下面挂要点），界面上唯一的区别
    # 只存在于生成它那一次调用用了哪份提示词——那个信息随调用一起消失了。
    #
    # 三处读者：
    #   1. 前端据此决定要不要摆「附加关系」区块（那个区块只对流程图有意义）；
    #   2. 导出（PUML 文件头注明图型），读的人不必从形状去猜；
    #   3. 排查"我要的鱼骨图怎么成了导图"时，那张图自己说得出来。
    #
    # 默认 `tree`：库里所有旧记录都没有这个键，Pydantic 的字段默认值把缺的补成
    # `tree`——**零迁移**，与 `edges` 同一条规矩（不改库、不升 `schema_version`）。
    # 而"旧图本来就是思维导图"这句是真的：它们全部由 `tree` 那条提示词生成。
    diagram_type: DiagramKind = "tree"

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("导图标题不能为空")
        return text

    @field_validator("edges")
    @classmethod
    def dedupe_edges(cls, value: list[Edge]) -> list[Edge]:
        """按 `(source, target, label)` 去重，**保留第一条**。

        为什么去重键里有 `label`：判断节点的两个出口指向同一个目标是完全正常的
        流程（"是"和"否"都回到主流程），把它们当成重复边删掉，图上就少了一条
        分支说明。反过来，两个端点与文字都一样的边没有任何表达差异，留着只会让
        画布上叠一条看不出来的重线。

        为什么是"去重"而不是"报错"：重复边是**幂等**输入——模型重试一次、用户
        点两次"连线"都会产生它，而它不改变图的意思。为一条信息量为零的边让整张
        图 422，代价方向错了。真正需要硬拒的是 dangling 与自环（见 `validate_tree`）。
        """
        unique: list[Edge] = []
        seen: set[tuple[str, str, str | None]] = set()
        for edge in value:
            key = (edge.source, edge.target, edge.label)
            if key in seen:
                continue
            seen.add(key)
            unique.append(edge)
        return unique

    @model_validator(mode="after")
    def validate_tree(self) -> MindMapContent:
        # 第二层：保存、读取和所有接收导图的接口共用同一关联约束。
        source_ids = {source.source_id for source in self.source_usage}
        if len(source_ids) != len(self.source_usage):
            raise ValueError("来源摘要中的来源 ID 不能重复")
        seen: set[str] = set()
        has_position = False
        stack = [(self.root, 1)]
        while stack:
            node, depth = stack.pop()
            if node.id in seen:
                raise ValueError("节点 ID 必须唯一")
            seen.add(node.id)
            if node.pos is not None:
                has_position = True
            if len(seen) > MAX_TREE_NODES or depth > MAX_TREE_DEPTH:
                raise ValueError(
                    f"导图最多 {MAX_TREE_NODES} 个节点，深度不超过 {MAX_TREE_DEPTH} 层")
            for ref in node.source_refs:
                if ref.source_id not in source_ids:
                    raise ValueError(f"节点 {node.id} 引用的来源 {ref.source_id} 不在 source_usage 中")
            stack.extend((child, depth + 1) for child in node.children)

        # 坐标只在手动布局下有意义：自动布局的图带着历史坐标，换布局时会被
        # 当成"用户想把这个节点放这里"，画出来是一团乱麻。
        if self.layout is LayoutKind.manual:
            if not has_position:
                raise ValueError("手动布局至少需要一个节点带坐标")
        elif has_position:
            raise ValueError("只有手动布局才允许保存节点坐标")

        relation_ids: set[str] = set()
        relation_keys: set[tuple[str, str, RelationType]] = set()
        for relation in self.relations:
            if relation.id in relation_ids:
                raise ValueError("关系 ID 必须唯一")
            relation_ids.add(relation.id)
            if relation.source not in seen:
                raise ValueError(f"关系 {relation.id} 的起点节点 {relation.source} 不在导图中")
            if relation.target not in seen:
                raise ValueError(f"关系 {relation.id} 的终点节点 {relation.target} 不在导图中")
            if relation.source == relation.target:
                raise ValueError(f"关系 {relation.id} 的起点与终点不能是同一个节点")
            key = (relation.source, relation.target, relation.type)
            if key in relation_keys:
                raise ValueError("同一对节点之间的同类关系不能重复添加")
            relation_keys.add(key)

        if len(self.edges) > MAX_EDGES_LIMIT:
            raise ValueError(f"附加边最多 {MAX_EDGES_LIMIT} 条")
        edge_ids: set[str] = set()
        for edge in self.edges:
            if edge.id in edge_ids:
                raise ValueError(f"边 ID 必须唯一，重复的是 {edge.id}")
            edge_ids.add(edge.id)
            # dangling 在这里**硬拒**，与 EDGE_LIMIT / EDGE_CYCLE_TOO_LONG 那两个
            # 只报 warn 的码不同。理由不是严格，是渲染：前端按 id 找节点算端点，
            # 找不到就是一条 `source: undefined` 的边，Vue Flow 会抛错而不是跳过，
            # 整张画布白屏。所以"指向不存在的节点"永远不能进到落库/渲染的图里；
            # 生成路径会在构造之前先把这种边滤掉并报 `EDGE_DANGLING`（见
            # `app/mindmaps/edges.py::clean_edges`），两条路都不让脏边活下来。
            if edge.source not in seen:
                raise ValueError(f"边 {edge.id} 的起点节点 {edge.source} 不在导图中")
            if edge.target not in seen:
                raise ValueError(f"边 {edge.id} 的终点节点 {edge.target} 不在导图中")
            # 自环画不出来——它只会渲染成一个点，用户看不出那里有一条线，
            # 却会因为"边数 +1"而困惑。而"节点到自己"这件事在图上没有语义。
            if edge.source == edge.target:
                raise ValueError(f"边 {edge.id} 的起点与终点不能是同一个节点（自环无法绘制）")
        return self


# v1 记录里缺的字段。只补顶层：Node 的新字段由 Pydantic 默认值补齐，不需要
# 递归改写——那会让"迁移"变成"重写整棵树"，一旦某个字段语义变过就再难回头。
#
# `edges` **不在**这张表里：它有字段默认值（`default_factory=list`），旧图缺这个键
# 时 Pydantic 直接补空列表，不需要迁移脚本改写库里那份 JSON。这就是"旧图零迁移"。
V1_TO_V2_DEFAULTS: dict[str, object] = {
    "schema_version": 2,
    "layout": LayoutKind.tree.value,
    "theme": {},
    "relations": [],
}


def migrate_content(data: dict) -> dict:
    """把库里读出来的 v1 记录补成 v2 形状；已经是 v2 就原样返回。

    幂等，且**只补缺、不覆盖**：调用方（仓储层）拿到的 dict 可能已经是 v2，
    也可能带着未来版本才有的字段，这里不做裁剪。
    """
    if not isinstance(data, dict):
        raise TypeError("导图内容必须是对象")
    if data.get("schema_version") == 2:
        return data
    return {**{k: v for k, v in V1_TO_V2_DEFAULTS.items() if k not in data}, **data}


class SaveRequest(BaseModel):
    content: MindMapContent


class UpdateRequest(SaveRequest):
    version: int = Field(ge=1)


class MapRecord(BaseModel):
    id: str
    version: int
    content: MindMapContent
    updated_at: str
