"""导出（F20 / F10）。

四种格式，都是 MindMapContent → 文本的纯映射，不调模型也不读盘：
    PUML      PlantUML `@startmindmap`，能贴进语雀 / Typora / Confluence
    Markdown  缩进大纲，最通用的中间格式
    OPML      能直接导入 XMind 等导图工具
    JSON      `MindMapContent` 的序列化——**唯一无损的一种**，也是导入的保底：
              另外三种只有两个文字槽位（`note or label` 合成一行，`detail` 单列
              一个通道，见下面"节点的 detail 怎么放"），往返时 `note` 与 `label`
              的区分、`color` / `icon`、附加边都会退化

新增 JSON 的理由是"能导出什么就能导入什么"：`app/importing/` 支持 JSON，若导出
侧不产出它，用户手里就没有一份"改完还能原样导回来"的文件，而这正是四种格式里
唯一能保证"结构 + 视觉 + 边 + 图型"一条都不丢的那一个。

**另外三种格式逐字节不变**——JSON 是新增的出口，不碰既有输出。
`tests/test_export.py::ByteIdenticalTest` 钉着这件事。

关于 PUML 的定位（与 docs/ARCHITECTURE.md 第 5 条一致）：
    puml 只作**导出格式**，不作为渲染主链路。
    原因：plantuml-encoder 只做压缩编码，出图要靠 PlantUML 服务端（JVM）；
          viz.js 是 Graphviz 的 WASM，只认 DOT，解析不了 PlantUML 语法。
    若要在页面内预览，需要额外部署 plantuml-server，并且预览只能是图片。

**附加边怎么处理**——文本格式不能凭空多出"任意连线"这个能力，所以各自的落点不同，
但有一条共同底线：**没有边的图，输出与加 edges 之前逐字节相同**（有回归用例钉着）。

  - **树**（`MindMapContent.edges`，端点是节点 id）：
    - puml：`mindmap` 语法里没有关系线（`*` 只表达缩进），任何 `A --> B` 都会被
      PlantUML 当成语法错误。所以边以 **PlantUML 注释行**（`' ` 前缀）附在
      `@endmindmap` 之前，内容仍然是 `A --> B : label` 这一行原文——文件保持
      可解析，人要找的关系也一眼能 grep 到。
    - markdown / opml：树形格式表达不了环，所以**文末单列一节「附加关系」**
      （`A → B（是）`）。硬塞进缩进列表会把"这是汇合/回退"读成"这是下一层"，
      而那条层级本来就已经存在了。
    - json：`edges` 是结构化的数组，原样进出，无损。
  - **流程图**：**两处数组、两个命名空间**（`FlowContent.edges` 的端点是阶段 id，
    `FlowStage.edges` 的端点是该阶段的节点 id），所以文末是**两节**，各自标清端点
    属于哪一层。puml 那三种格式里同样是"列表 + 边清单"，不是真的 activity 图
    （取舍见 `to_puml_flow` 的 docstring）。
  - **鱼骨图**：顶层结构里没有 `edges`，没有边清单可写。

**三种图型都要能导出**。此前 `to_puml` / `to_markdown` / `to_opml` 的入参标着
`MindMapContent` 却真的去读 `content.root`——一份 `FlowContent` 走到这里就是
`AttributeError`（500）。形状分派放在这三个公开函数的第一行，树的实现原封不动
搬到 `_puml_tree` / `_markdown_tree` / `_opml_tree`（逐字节不变的前提就是这个"不动"）。

**节点的 `detail` 怎么放**——`detail` 是生成的核心产物（"这一步到底做什么"），
`note` 是用户备注，两者都可能为空，而三种文本格式各自只有**一个标题槽**。所以
`note or label` 继续占那个槽（`_node_text`，一行），`detail` 走各格式的**第二个
通道**，一条都不进大纲层级：

  - markdown：节点行下面一行引用块（`  > …`）。引用块不占列表缩进，读起来是
    "这一项的说明"，也不可能被当成下一层节点。
  - puml：节点行下面一行注释（`' 说明：…`）。文件仍能解析、能出图，那句话还能
    grep 到；正文里的 `-->` 会被压成 `→`（理由见 `_puml_note`）。
  - opml：`_note` 属性（OmniOutliner 的备注约定，见 `_opml_note`）。

`note` 与 `label` 仍然合成一行：它们争的是同一个位置——画布上的那一行字——而
`detail` 是另一层信息，本来就不该挤进标题。**没有 `detail` 的图输出逐字节不变**
（`tests/test_export.py::ByteIdenticalTest` 钉着这件事）。

**形状标记**。markdown 的树输出只有一个 `# 标题`，鱼骨与流程却在用多级 heading /
缩进分节——导入侧只看得到文字，看不出"这几条是一根骨"。所以非树形状的 markdown
第二行写一行 HTML 注释 `<!-- genai-mindmap: shape=fishbone -->`：渲染时不可见，
`grep` 得到，导入侧读它就能精确还原形状（见 `shape_marker`）。树不写——"没有标记"
就是树，所以树的输出一个字节都没动。
"""
from __future__ import annotations

import json
from xml.sax.saxutils import escape, quoteattr

from app.contracts.mindmap import (
    DIAGRAM_LABELS,
    Edge,
    FishboneBone,
    FishboneContent,
    FlowContent,
    FlowStage,
    MindMapContent,
    Node,
)

# 文末那一节的标题。markdown 用它做二级标题，opml 用它做最后那棵 outline 的
# `text`。两处共读一个常量：改一处忘一处时，导出的两种格式会出现两个名字。
EDGES_SECTION = "附加关系"

# 流程图那两层各自的节标题。**分开两个常量而不是拼后缀**：markdown 的标题、
# opml 的 outline text、puml 的注释行三处都读它，拼出来的一致性是靠常量保证的。
STAGE_EDGES_SECTION = f"{EDGES_SECTION}（阶段之间）"
INNER_EDGES_SECTION = f"{EDGES_SECTION}（阶段内部）"


def shape_marker(diagram_type: str) -> str:
    """Markdown 文件里的形状标记行（不带换行）。

    为什么需要它：鱼骨与流程的 markdown 里，"这几条是六根骨"只体现为缩进，而缩进
    在导入侧同时可能是"这个节点的子节点"。**从形状硬推断形状是编造**——一份普通的
    三级缩进大纲与一张只有两条原因的鱼骨图，在文本上可以一模一样。写上这一行之后，
    导入侧不必猜：`importing/markdown.py` 读它，读不到就按树处理（老文件与手写
    大纲的行为一个字节没变）。

    为什么是 HTML 注释：渲染时不可见（不影响任何 Markdown 阅读器），是标准语法
    （不是自造的行内约定），`grep 'shape='` 能直接找到，删掉它只是退回"按树导入"。
    位置在**第二行**：第一行留给 `# 标题`，那是所有读者（与人）第一眼看的东西。

    只写给非树形状——树不写，所以树的 markdown 输出逐字节不变。
    """
    return f"<!-- genai-mindmap: shape={diagram_type} -->"


# `kind` → 中文说法，只在边**没有 label** 时补上去。有 label 时（"是"/"驳回"）
# 那两个字已经表达了分支朝向，再补一个"条件分支"只是把行拉长。
#
# 它是**两边共读**的：`app/importing/markdown.py` 用它的反表把"顺序/条件分支/
# 回退"读回 `kind`。所以它必须公开——私有名会让导入侧只能复制一份，而复制出来
# 的那份在某天改了文案之后不会有人记得同步，"回退线往返之后变成实线"就是这么
# 发生的。
EDGE_KIND_LABELS = {
    "flow": "顺序",
    "branch": "条件分支",
    "loop": "回退",
}


def _depth_marks(depth: int) -> str:
    """PlantUML mindmap 用星号数量表示层级。"""
    return "*" * max(1, depth)


def _node_text(node: Node) -> str:
    """节点导出文字：正文优先。

    `label` 是画布上的短标题（≤200 字），`note` 才是生成的完整表述。只导出
    `label` 的话，别人拿到的是一份只有骨架、没有解释的大纲。

    `detail` **不在这里**：它是第二个通道，见模块 docstring 的
    "节点的 detail 怎么放"。混进同一行会让每个节点变成"标题 + 一段 142 字的话"，
    而大纲的全部价值在于能一眼扫过。
    """
    return node.note if node.note else node.label


def _one_line(text: str) -> str:
    """压成单行。导出格式里换行会破坏缩进与 XML 结构。"""
    return " ".join(text.split())


def _sanitize(text: str) -> str:
    """PUML 专用：星号在 mindmap 语法里是层级标记，出现在正文会被解析成新节点。"""
    return _one_line(text).replace("*", "·")


def _escape_markdown(text: str) -> str:
    """转义会破坏 Markdown 结构的字符。

    尖括号不转义会被渲染器当成 HTML 标签——`List<T>` 里的 `<T>` 直接消失，
    节点文字就残了。节点总在 `- ` 之后，不占行首，所以 `#` 和 `>` 不用管。
    """
    return _one_line(text).replace("<", "&lt;").replace(">", "&gt;")


def _markdown_quote(detail: str | None, pad: str) -> list[str]:
    """`detail` 在 Markdown 里的落点：紧跟节点行的一句引用块。

    为什么不拼进节点行：那一行已经有 `note or label`（见 `_node_text`），再塞一段
    生成说明，得到的是"标题 + 一段话"糊成一行的长文本，而 `detail` 实测平均 142 字
    ——一份 100 个节点的图会变成一串读不下去的段落。

    为什么是引用块：它是 Markdown 里唯一"明显属于上一项、又可以不进列表缩进层级"
    的写法。它由 `>` 起头，所以导入侧一个正则就认得回来（`importing/markdown.py`
    的 `_DETAIL`），而 `- ` 行无论怎么写都不会被误认成它。

    `pad` 是**节点自己那一行的缩进 + 2 格**：渲染器据此把它归进那个列表项内部；
    不缩进的话它会掉到列表外面，看起来像全图共有的注释。
    """
    if not detail:
        return []
    return [f"{pad}> {_escape_markdown(detail)}"]


def _sanitize_comment(text: str) -> str:
    """PUML 注释行的清洗，理由见 `_puml_note`。"""
    return _one_line(text).replace("*", "·").replace("-->", "→")


def _puml_note(detail: str | None) -> list[str]:
    """`detail` 在 PlantUML 里的落点：节点行下面一行注释。

    `'` 是 PlantUML 的注释符，所以文件仍然能解析、能出图，而那句话还能被 grep 到。

    正文里的两种字符要压掉，每一种都有具体后果：
      - `*` 在 mindmap 语法里是层级标记（与 `_sanitize` 同一条理由）；
      - `-->` 会被 `importing/puml.py` 当成一条边（它把任何含 `-->` 的注释行读成
        `A --> B : label`），一句说明凭空长出一条关系，比丢一个箭头糟得多。
        所以它换成 `→`——与 `*` → `·` 同样是一个**单向**替换，puml 的往返用例
        本来就已经承认这条路有损。
    """
    if not detail:
        return []
    return [f"' 说明：{_sanitize_comment(detail)}"]


def _opml_note(detail: str | None) -> str:
    """`detail` 在 OPML 里的落点：`_note` 属性（含前导空格，直接拼在标签里）。

    `_note` 是 OmniOutliner 用了几十年的备注约定，XMind / 幕布 / 其它 OPML 阅读器
    见到不认识也不会报错——它就是一个普通属性，不是非法结构。

    **没有 `detail` 时返回空串**，所以树的既有输出一个字节都不变。
    """
    if not detail:
        return ""
    return f" _note={quoteattr(_one_line(detail))}"


def _edge_suffix(edge: Edge) -> str:
    """边行末尾那段说明：有 label 用它，没有就用 kind 的中文说法，都没有就空。

    为什么 label 优先于 kind：label 是**这一次**跳转的理由（"是"/"驳回"），
    kind 只是它的类别。两者都写会得到「驳回（回退）」，读起来像同义重复。
    """
    if edge.label:
        return f"（{edge.label}）"
    if edge.kind:
        return f"（{EDGE_KIND_LABELS.get(edge.kind, edge.kind)}）"
    return ""


def _node_names(content: MindMapContent) -> dict[str, str]:
    """节点 id → 导出文字。边要用它把 id 换成人看得懂的名字。

    用**同一份** `_node_text`（正文优先）而不是 `label`：同一张图里，树行显示的是
    "研究如何让计算机从数据里学规律。"，边行却写"机器学习"，读的人会以为是两个
    不同的节点。
    """
    names: dict[str, str] = {}
    stack = [content.root]
    while stack:
        node = stack.pop()
        names[node.id] = _node_text(node)
        stack.extend(node.children)
    return names


def _edge_line(edge: Edge, names: dict[str, str]) -> str:
    """`A → B（是）`。端点用导出文字；万一 id 不在树里（不该发生），退回 id。"""
    return (f"{names.get(edge.source, edge.source)} → "
            f"{names.get(edge.target, edge.target)}{_edge_suffix(edge)}")


def to_puml(content: MindMapContent | FishboneContent | FlowContent) -> str:
    """导出 PlantUML 文本。**按形状分派**，树的输出逐字节不变。

    三种形状都走 `@startmindmap`——包括流程图。理由见 `_puml_flow` 的 docstring：
    "真的 activity 图"需要一次结构重写，而现在的数据（阶段是并列的步骤树，不是一个
    线性的判定链）撑不起那个重写。
    """
    if isinstance(content, FishboneContent):
        return _puml_fishbone(content)
    if isinstance(content, FlowContent):
        return _puml_flow(content)
    return _puml_tree(content)


def _puml_tree(content: MindMapContent) -> str:
    """把导图转成 PlantUML mindmap 语法。

    附加边以 **PlantUML 注释行**（`' ` 前缀）附在 `@endmindmap` 之前，行内是
    `A --> B : label` 的原文。`mindmap` 语法没有关系线，裸写 `A --> B` 会让
    PlantUML 报语法错；加一个注释符之后文件仍然可解析，而那一行文字还能被
    grep 到、也能直接粘进 class/activity 图。这是一个明确的取舍：**保住文件
    可用**比让那行"看起来更像图"更重要。

    图型写在**文件头**的第二行：拿到这个 `.puml` 的人看不到 `diagram_type`
    字段，而 `mindmap` 语法本身分不出流程图与思维导图——形状可以一模一样。
    写上之后"这张图本来是按鱼骨图生成的"至少留下过痕迹。
    """
    lines: list[str] = [
        "@startmindmap",
        f"' 图型：{DIAGRAM_LABELS.get(content.diagram_type, content.diagram_type)}"
        f"（{content.diagram_type}）",
    ]
    lines.append(f"* {_sanitize(_node_text(content.root))}")
    lines += _puml_note(content.root.detail)

    def walk(node: Node, depth: int) -> None:
        for child in node.children:
            lines.append(f"{_depth_marks(depth)} {_sanitize(_node_text(child))}")
            lines.extend(_puml_note(child.detail))
            walk(child, depth + 1)

    # 根节点已输出，子节点从第 2 层开始
    walk(content.root, 2)
    if content.edges:
        names = _node_names(content)
        lines.append(f"' ---- {EDGES_SECTION}（mindmap 语法无关系线，以下按 A --> B : label 记录）----")
        for edge in content.edges:
            arrow = "-->"
            text = f"{_sanitize(names.get(edge.source, edge.source))} {arrow} " \
                   f"{_sanitize(names.get(edge.target, edge.target))}"
            if edge.label:
                text += f" : {_sanitize(edge.label)}"
            elif edge.kind:
                text += f" : {EDGE_KIND_LABELS.get(edge.kind, edge.kind)}"
            lines.append(f"' {text}")
    lines.append("@endmindmap")
    return "\n".join(lines) + "\n"


def to_markdown(content: MindMapContent | FishboneContent | FlowContent) -> str:
    """导出 Markdown 大纲。**按形状分派**，树的输出逐字节不变。"""
    if isinstance(content, FishboneContent):
        return _markdown_fishbone(content)
    if isinstance(content, FlowContent):
        return _markdown_flow(content)
    return _markdown_tree(content)


def _markdown_tree(content: MindMapContent) -> str:
    """导出 Markdown 大纲：标题 + 缩进列表。

    根节点也写成列表项而不是跳过——否则 `title` 和 `root.label` 不一致时
    （生成器允许不同），根节点的文字会在导出结果里消失。

    附加边**不缩进进树**，而是文末单列一节：树里的一条缩进表示"这是下一层"，
    把"从第 5 步退回第 2 步"画成缩进，读者会把它当成第 6 步的子节点。

    节点的 `detail` 以引用块跟在它自己那一行下面（见 `_markdown_quote`）。

    **缩进是 2 个空格，而且这一条没有跟鱼骨/流程一起改成 4 个（故意的）**：
    树的 markdown 输出被 `tests/test_export.py::ByteIdenticalTest` 逐字节钉死，
    本轮修复的边界也明确写着"树的既有行为逐字节不变"。代价是**已知的**：
    同一份文件在 Python-Markdown 下会被读成同级列表（它按 `tab_length=4` 判层级，
    CommonMark 从 2 格起算）。实测 HTML 与"改 4 格要动哪几处"记在
    `docs/IMPORT.md` 的「两个 Markdown 解析器」一节——它是一个待决策项，不是漏改。
    """
    lines: list[str] = [f"# {_escape_markdown(content.title)}", ""]
    lines.append(f"- {_escape_markdown(_node_text(content.root))}")
    lines += _markdown_quote(content.root.detail, "  ")

    def walk(node: Node, depth: int) -> None:
        pad = "  " * depth
        for child in node.children:
            lines.append(f"{pad}- {_escape_markdown(_node_text(child))}")
            lines.extend(_markdown_quote(child.detail, pad + "  "))
            walk(child, depth + 1)

    walk(content.root, 1)
    if content.edges:
        names = _node_names(content)
        lines += ["", f"## {EDGES_SECTION}", ""]
        for edge in content.edges:
            lines.append(f"- {_escape_markdown(_edge_line(edge, names))}")
    return "\n".join(lines) + "\n"


def to_opml(content: MindMapContent | FishboneContent | FlowContent) -> str:
    """导出 OPML 2.0。**按形状分派**，树的输出逐字节不变。"""
    if isinstance(content, FishboneContent):
        return _opml_fishbone(content)
    if isinstance(content, FlowContent):
        return _opml_flow(content)
    return _opml_tree(content)


def _opml_tree(content: MindMapContent) -> str:
    """导出 OPML 2.0，XMind / MindManager / 幕布都能直接导入。

    节点文字用 quoteattr 转义：模型生成的标签里出现引号或尖括号时，手写
    字符串拼接会产出非法 XML，导入端只会报"文件格式错误"，很难追到根因。

    附加边放在 `body` 末尾的**另一棵 outline**（`text="附加关系"`）里。OPML 的
    body 允许多个顶层 outline，所以这是合法结构；不塞进主树，理由同 markdown
    ——缩进在 OPML 里也只有一个意思：父子层级。
    """

    def outline(node: Node, depth: int) -> list[str]:
        pad = "  " * depth
        parts = [f"{pad}<outline text={quoteattr(_one_line(_node_text(node)))}{_opml_note(node.detail)}>"]
        for child in node.children:
            parts.extend(outline(child, depth + 1))
        parts.append(f"{pad}</outline>")
        return parts

    lines: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<opml version="2.0">',
        "  <head>",
        f"    <title>{escape(_one_line(content.title))}</title>",
        "  </head>",
        "  <body>",
    ]
    lines.extend(outline(content.root, 2))
    if content.edges:
        names = _node_names(content)
        lines.append(f'    <outline text="{EDGES_SECTION}">')
        for edge in content.edges:
            lines.append(
                f"      <outline text={quoteattr(_one_line(_edge_line(edge, names)))} />")
        lines.append("    </outline>")
    lines.extend(("  </body>", "</opml>"))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 鱼骨图
#
# 鱼骨图的顶层结构里**没有 `edges`**（只有 `relations`，而它界面不渲染、也不导出），
# 所以这三种格式对它来说只有"骨架"一件事：`problem` 是鱼头，六根骨是类别，骨下面挂
# 原因。形状与树不同（没有 `root`），所以不能走树那三个实现。

def _fishbone_lines(
    content: FishboneContent,
) -> list[tuple[int, str, str | None, FishboneBone | None]]:
    """鱼骨图 → `(层号, 文字, 详情, 骨)`：0 是鱼头，1 是骨，2 起是骨下面的原因。

    鱼头用 `problem ?? title`：`title` 是文件名（"报销慢的原因分析"），`problem` 才是
    这张图的论点（"为什么报销要 11 天"）。导出给别人看时，后者才是那句话。

    第三项是节点的 `detail`。鱼头那一行永远是 `None`：主骨上那句话是 `problem`
    ——一个字符串字段，它下面没有节点，也就没有详情可带。骨带的是**骨根节点**的
    `detail`（`bone.root.detail`）。

    第四项只有骨那一层非空。**`category` 与骨根的 `note` 不在这个行表里**：
    它们不是"画在骨上那行字"（那是 `root.label`），而是同一根骨上另外两个字段
    ——markdown 把它们写进骨行下面一行 HTML 注释（`_markdown_bone_comment`），
    opml / puml 的调用方忽略这一项。
    """
    head = content.problem.strip() or content.title
    out: list[tuple[int, str, str | None, FishboneBone | None]] = [(0, head, None, None)]
    for bone in content.bones:
        label = bone.root.label.strip() or bone.category
        out.append((1, label, bone.root.detail, bone))
        stack = [(child, 2) for child in reversed(bone.root.children)]
        while stack:
            node, depth = stack.pop()
            out.append((depth, _node_text(node), node.detail, None))
            stack.extend((child, depth + 1) for child in reversed(node.children))
    return out


def _puml_fishbone(content: FishboneContent) -> str:
    """鱼骨图 → PlantUML mindmap 语法。鱼头当根、骨当一级分支。"""
    lines: list[str] = [
        "@startmindmap",
        f"' 图型：{DIAGRAM_LABELS.get(content.diagram_type, content.diagram_type)}"
        f"（{content.diagram_type}）",
    ]
    for depth, text, detail, _bone in _fishbone_lines(content):
        lines.append(f"{_depth_marks(depth + 1)} {_sanitize(text)}")
        lines += _puml_note(detail)
    lines.append("@endmindmap")
    return "\n".join(lines) + "\n"


def _markdown_bone_comment(bone: FishboneBone) -> str | None:
    """一根骨的 `category` 与骨根 `note` 在 Markdown 里的落点：一行 HTML 注释。

    为什么必须另开一个通道：`_fishbone_lines` 那一行的文字槽装的是 `root.label`
    （画在骨上的那行字），而 `category` 与 `root.note` 是同一根骨上**另外两个字段**。
    此前这两个字段在 markdown 里没有任何落点——一份 `category=People`、
    `label=Staff`、另有 `note` 的骨，往返之后变成 `category=Staff`、正文消失，
    **而且没有告警**（codex 复审 P2-2）。数据丢失且不报警比丢得明明白白更糟。

    为什么是 HTML 注释：它渲染不可见（`shape_marker` 同理，读者看到的还是大纲），
    是标准语法，且不会被 `- ` 行、`>` 行、heading 任一条解析规则误认。位置在
    **骨行下面、与 `detail` 的引用块同缩进**：先注释后引用块，读起来是"这一项的
    元数据、然后是这一项的说明"。

    为什么是 JSON：值里有空格、冒号、中文引号都不需要再定一份转义规则，
    `json.loads` 就是唯一实现（导出的 `to_json` 也用它）。`>` 写成 `\\u003e`
    ——JSON 里两种写法等价，但注释里一旦出现 `-->` 就提前结束，后半行会变成
    正文，Markdown 结构当场断掉。

    **没有 category / note 的骨返回 None**，所以既有输出一个字节都没变。
    """
    payload: dict[str, str] = {}
    if bone.category and bone.category != bone.root.label.strip():
        payload["category"] = bone.category
    if bone.root.note:
        payload["note"] = bone.root.note
    if not payload:
        return None
    body = json.dumps(payload, ensure_ascii=False, separators=(", ", ": "))
    return f"<!-- genai-mindmap: bone {body.replace('>', chr(92) + 'u003e')} -->"


def _markdown_fishbone(content: FishboneContent) -> str:
    """鱼骨图 → Markdown 大纲。骨是鱼头的**子项**，原因再按缩进分层。

    骨与原因必须是**两级不同的缩进**（`    - 人` / `        - 审批的人不在`）。此前
    两者都写成顶格列表项（`- 人` 与 `    - 原因` 之间只差原因的缩进），普通 Markdown
    阅读器把鱼头与六根骨画成**同一级**的七个要点——读起来不是"一条鱼"，是"七条
    并列笔记"。

    **每层 4 个空格，不是 2 个。** 2 个空格是 CommonMark 允许的最小值，而
    Python-Markdown（GitHub 之外的另一个大用户，也是本仓库 `pip` 里装着的那个）
    按 `tab_length=4` 判层级：`  - 骨` 比上一行深 2 格，它认不出这是子列表，
    于是把鱼头和六根骨画成**同一级**——同一份文件在两个解析器里是两张图
    （codex 复审 P2-1）。4 个空格两边都认（实测见 `docs/IMPORT.md`）。

    缩进只影响渲染，不影响导入：`importing/markdown.py` 的层级靠**缩进栈**比较
    相邻两行的相对大小，不按"除以几"算层，所以 2 空格的老文件、4 空格的新文件、
    用户手改成 Tab 的文件读出来是同一棵树。

    第二行是形状标记（见 `shape_marker`）：缩进能表达"谁挂在谁下面"，表达不了
    "这几条是骨"。标记让导入侧不必猜。骨的 `category` / `note` 走骨行下面那行
    注释（`_markdown_bone_comment`）。
    """
    lines: list[str] = [
        f"# {_escape_markdown(content.title)}",
        shape_marker(content.diagram_type),
        "",
    ]
    for depth, text, detail, bone in _fishbone_lines(content):
        pad = "    " * depth
        lines.append(f"{pad}- {_escape_markdown(text)}")
        if bone is not None:
            comment = _markdown_bone_comment(bone)
            if comment is not None:
                # 与引用块同缩进（`pad + "  "`）：缩进到 `- ` 之后的那一层，
                # 渲染时仍在这一项内部；再深一格（`pad + "    "`）会被
                # Python-Markdown 当成缩进代码块，`>` 变成字面文本。
                lines.append(f"{pad}  {comment}")
        lines += _markdown_quote(detail, pad + "  ")
    return "\n".join(lines) + "\n"


def _opml_fishbone(content: FishboneContent) -> str:
    """鱼骨图 → OPML。鱼头是唯一顶层 outline，骨与原因嵌套在下面。

    行列表（`_fishbone_lines`）先按层号折回一棵树再递归输出：手写"开标签 / 关标签"
    的游标版本只要有一处深度跳变就会产出不闭合的 XML，而 OPML 的解析器只会报
    "文件格式错误"，很难追到根因。
    """
    rows = _fishbone_lines(content)
    head: dict = {"text": rows[0][1], "note": rows[0][2], "children": []}
    stack: list[tuple[dict, int]] = [(head, 0)]
    for depth, text, detail, _bone in rows[1:]:
        while len(stack) > 1 and stack[-1][1] >= depth:
            stack.pop()
        item: dict = {"text": text, "note": detail, "children": []}
        stack[-1][0]["children"].append(item)
        stack.append((item, depth))

    def emit(item: dict, depth: int) -> list[str]:
        pad = "  " * depth
        text = quoteattr(_one_line(item["text"]))
        note = _opml_note(item["note"])
        if not item["children"]:
            return [f"{pad}<outline text={text}{note} />"]
        parts = [f"{pad}<outline text={text}{note}>"]
        for child in item["children"]:
            parts.extend(emit(child, depth + 1))
        parts.append(f"{pad}</outline>")
        return parts

    lines: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<opml version="2.0">',
        "  <head>",
        f"    <title>{escape(_one_line(content.title))}</title>",
        "  </head>",
        "  <body>",
    ]
    lines.extend(emit(head, 2))
    lines.extend(("  </body>", "</opml>"))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 流程图
#
# **两处数组、两个命名空间**（契约 `FlowStage`）：`content.edges` 的端点是**阶段 id**，
# `stages[i].edges` 的端点是**该阶段自己的节点 id**。所以导出的边清单是**两节**，
# 各自把"端点是什么"写在标题里——只写一节的话，读的人拿一串 `s_1 → s_2` 与
# `n1 → n12` 混在一起，分不出哪条是主轴、哪条是判定出口。
#
# 关于 puml：**不硬转 activity 图**。理由在 `_puml_flow` 的 docstring 里。

def _flow_stage_name(stage: FlowStage, index: int) -> str:
    """阶段的显示名：`1. 提交申请`。序号是顺序的唯一可见痕迹（数组下标没人看得见）。

    边清单用它（要短），结构行用 `_flow_stage_heading`（要带上阶段根的正文）。
    """
    return f"{index + 1}. {stage.root.label.strip()}"


def _flow_stage_heading(stage: FlowStage, index: int) -> str:
    """阶段的**结构行**标题：名字，必要时补上阶段根的正文。

    阶段根的 `note` / `detail` 是别人读得懂的完整表述（"提交申请" vs "把报销单和
    发票拍照上传"）。只写 `label` 会把它丢掉；把根再单列一行列表项又会在每个阶段
    下面重复一遍标题。所以合成一行——正文与标题不同时才补，相同时输出与只写标题
    完全一致。
    """
    name = _flow_stage_name(stage, index)
    body = _node_text(stage.root).strip()
    return name if body == stage.root.label.strip() else f"{name}｜{body}"


def _flow_node_names(stage: FlowStage) -> dict[str, str]:
    """一个阶段里 节点 id → 导出文字。阶段内边的端点用的是这一套。"""
    names: dict[str, str] = {}
    stack = [stage.root]
    while stack:
        node = stack.pop()
        names[node.id] = _node_text(node)
        stack.extend(node.children)
    return names


def _flow_stage_names(content: FlowContent) -> dict[str, str]:
    """阶段 id → 阶段名（阶段之间那一层的边长这样）。"""
    return {stage.id: _flow_stage_name(stage, index)
            for index, stage in enumerate(content.stages)}


def _flow_stage_edge_lines(content: FlowContent) -> list[str]:
    """**阶段之间**的边清单：`提交申请 → 付款（通过）`。"""
    names = _flow_stage_names(content)
    return [f"{names.get(edge.source, edge.source)} → "
            f"{names.get(edge.target, edge.target)}{_edge_suffix(edge)}"
            for edge in content.edges]


def _flow_inner_edge_lines(content: FlowContent) -> list[str]:
    """**阶段内部**的边清单：`[1. 提交申请] 审不审 → 补材料（否）`。

    行首带阶段名不是冗余：阶段内边的端点只在该阶段内有效，两条不同阶段里的
    `n1 → n12` 在清单上是两行**看起来一样**的文字，括号里的阶段名是唯一的区分。
    """
    lines: list[str] = []
    for index, stage in enumerate(content.stages):
        names = _flow_node_names(stage)
        prefix = f"[{_flow_stage_name(stage, index)}] "
        for edge in stage.edges:
            lines.append(f"{prefix}{names.get(edge.source, edge.source)} → "
                         f"{names.get(edge.target, edge.target)}{_edge_suffix(edge)}")
    return lines


def _puml_flow(content: FlowContent) -> str:
    """流程图 → PlantUML。**仍是 mindmap 语法 + 两段边清单，不是真的 activity 图。**

    这是一个明确拒绝了的"更好的方案"。PlantUML 的 activity 语法确实能表达判定
    （`if (...) then (是) ... else (否) ... endif`），但硬转的代价不是写几行字符串：

      - activity 图是一串**线性语句**，而我们的数据是"阶段是一棵步骤树"。要把树
        拉平成语句序列，得先定义"哪些子节点是顺序、哪些是并列"，而数据里没有这个
        区分（`Node.role` 只有 step / decision / start / end）；
      - 判定节点的 `if / else` 需要**成对**的两个出口，也就是要知道哪条是「是」、
        哪条是「否」、两条各自汇合到哪里。数据里只有 `Edge.label` 的自由文字
        （"通过" / "驳回" / "是"），没有结构化配对——猜错一次就会导出一份
        `else` 挂错分支的图，而那种错在文本里看不出来；
      - `FlowStage.edges` 的端点可以指向阶段里的任意两个节点（含"步骤顺序跳转"），
        不一定是判定的两个出口。

    所以这里给的是**诚实的降级**：图仍然按阶段顺序排成 mindmap（读得出先后），
    两层边各一段注释清单（grep 得到、能直接粘进真正的 activity 图），文件头写明
    "这不是 activity 图"。要真图形，得先在契约里加"判定节点的两个出口"这层结构化
    信息，那是另一次改动。
    """
    lines: list[str] = [
        "@startmindmap",
        f"' 图型：{DIAGRAM_LABELS.get(content.diagram_type, content.diagram_type)}"
        f"（{content.diagram_type}）",
        "' 注意：这是 mindmap 语法，不是 PlantUML activity 图。阶段顺序按下面的分支顺序读，"
        "两层附加边在文件末尾按 A --> B : label 记录（不在图上）。",
    ]
    lines.append(f"* {_sanitize(content.title)}")
    for index, stage in enumerate(content.stages):
        lines.append(f"** {_sanitize(_flow_stage_heading(stage, index))}")
        lines += _puml_note(stage.root.detail)
        stack = [(child, 3) for child in reversed(stage.root.children)]
        while stack:
            node, depth = stack.pop()
            lines.append(f"{_depth_marks(depth)} {_sanitize(_node_text(node))}")
            lines += _puml_note(node.detail)
            stack.extend((child, depth + 1) for child in reversed(node.children))

    if content.edges:
        lines.append(
            f"' ---- {STAGE_EDGES_SECTION}（mindmap 语法无关系线，"
            "以下按 A --> B : label 记录）----")
        for edge in content.edges:
            names = _flow_stage_names(content)
            text = (f"{_sanitize(names.get(edge.source, edge.source))} --> "
                    f"{_sanitize(names.get(edge.target, edge.target))}")
            if edge.label:
                text += f" : {_sanitize(edge.label)}"
            elif edge.kind:
                text += f" : {EDGE_KIND_LABELS.get(edge.kind, edge.kind)}"
            lines.append(f"' {text}")
    inner = _flow_inner_edge_lines(content)
    if inner:
        lines.append(f"' ---- {INNER_EDGES_SECTION}（端点用该阶段的节点 id）----")
        for line in inner:
            lines.append(f"' {_sanitize(line)}")
    lines.append("@endmindmap")
    return "\n".join(lines) + "\n"


def _markdown_flow(content: FlowContent) -> str:
    """流程图 → Markdown：阶段是小节（`###`），两步之外的边各一节。

    阶段用 `###` 而不是列表项：阶段是**图的骨架**（数组顺序就是执行顺序），把它
    降成一列缩进之后，"这里是第二个阶段"只能从缩进层级去数。树（`-` 列表）的
    逐字节输出不受影响——这条分派在 `to_markdown` 的第一行。

    阶段内部的步骤树按**每层 4 个空格**缩进，与鱼骨同一条理由：2 个空格只有
    CommonMark 认，Python-Markdown 按 `tab_length=4` 判层级，会把它读成同一级
    （见 `_markdown_fishbone`）。阶段标题上的 `###` 让阶段边界不依赖缩进，
    但阶段**内部**的三层步骤仍要靠缩进表达。

    第二行的形状标记让导入侧知道"这些 `###` 是阶段，不是普通小标题"（见
    `shape_marker`）；阶段根自己的 `detail` 以引用块挂在标题下面。
    """
    lines: list[str] = [
        f"# {_escape_markdown(content.title)}",
        shape_marker(content.diagram_type),
    ]
    for index, stage in enumerate(content.stages):
        lines += ["", f"### {_escape_markdown(_flow_stage_heading(stage, index))}", ""]
        quotes = _markdown_quote(stage.root.detail, "")
        if quotes:
            lines += quotes + [""]
        stack = [(child, 0) for child in reversed(stage.root.children)]
        while stack:
            node, depth = stack.pop()
            pad = "    " * depth
            lines.append(f"{pad}- {_escape_markdown(_node_text(node))}")
            lines.extend(_markdown_quote(node.detail, pad + "  "))
            stack.extend((child, depth + 1) for child in reversed(node.children))

    # 每个阶段块尾部都留了一个空行（阶段之间的分隔），最后一节要多删一个，
    # 否则边清单前面会出现两个连续空行。
    while lines and not lines[-1]:
        lines.pop()

    stage_lines = _flow_stage_edge_lines(content)
    if stage_lines:
        lines += ["", f"## {STAGE_EDGES_SECTION}", ""]
        lines += [f"- {_escape_markdown(line)}" for line in stage_lines]
    inner_lines = _flow_inner_edge_lines(content)
    if inner_lines:
        lines += ["", f"## {INNER_EDGES_SECTION}", ""]
        lines += [f"- {_escape_markdown(line)}" for line in inner_lines]
    return "\n".join(lines) + "\n"


def _opml_flow(content: FlowContent) -> str:
    """流程图 → OPML：标题 → 各阶段（阶段名 + 步骤树）→ 两棵边清单 outline。"""
    lines: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<opml version="2.0">',
        "  <head>",
        f"    <title>{escape(_one_line(content.title))}</title>",
        "  </head>",
        "  <body>",
        f"    <outline text={quoteattr(_one_line(content.title))}>",
    ]

    def outline(node: Node, depth: int) -> list[str]:
        pad = "  " * depth
        parts = [f"{pad}<outline text={quoteattr(_one_line(_node_text(node)))}{_opml_note(node.detail)}>"]
        for child in node.children:
            parts.extend(outline(child, depth + 1))
        parts.append(f"{pad}</outline>")
        return parts

    for index, stage in enumerate(content.stages):
        lines.append(
            f"      <outline text={quoteattr(_one_line(_flow_stage_heading(stage, index)))}"
            f"{_opml_note(stage.root.detail)}>")
        for child in stage.root.children:
            lines.extend(outline(child, 4))
        lines.append("      </outline>")
    lines.append("    </outline>")

    for title, rows in (
        (STAGE_EDGES_SECTION, _flow_stage_edge_lines(content)),
        (INNER_EDGES_SECTION, _flow_inner_edge_lines(content)),
    ):
        if not rows:
            continue
        lines.append(f"    <outline text={quoteattr(title)}>")
        for row in rows:
            lines.append(f"      <outline text={quoteattr(_one_line(row))} />")
        lines.append("    </outline>")
    lines.extend(("  </body>", "</opml>"))
    return "\n".join(lines) + "\n"


def to_json(content: MindMapContent) -> str:
    """导出 `MindMapContent` 的 JSON 序列化——**唯一无损的格式**。

    为什么它比另外三种重要：markdown / opml / puml 的**标题槽**只有一个，而节点有
    三个文字字段——`label`（短标题）、`note`（用户备注）、`detail`（生成说明）。
    导出时 `note or label` 合占那个标题槽（正文优先），`detail` 另走一个通道
    （引用块 / 注释行 / `_note` 属性，见模块 docstring）。所以文字不会丢，丢的是
    **`note` 与 `label` 的区分**（合成一句之后回来只能落在 `label` 里），
    `color` / `icon` / `diagram_type` / 附加边也一并退化。

    JSON 把这些全部保住，所以它是"导出 → 改 → 导入"的保底：结构、视觉、边、
    图型、主题、布局一条不少。唯一**不**保留的是 `source_refs` 与 `source_usage`
    ——导入侧会清空它们（外部文件的字符偏移指向的是别处的原文），这是刻意的，
    见 `app/importing/normalize.py` 的第 ⑤ 条。

    `model_dump(mode="json")` 而不是手拼 dict：契约加字段时这里自动跟上，不会
    出现"导出少一个字段、导入时它悄悄变默认值"。

    `indent=2` + `ensure_ascii=False`：这份文件是给人改的，中文标题不该变成
    `\\u5b63\\u5ea6`。行尾补一个换行，与另外三种格式一致（`git diff` 少一行噪声）。
    """
    return json.dumps(content.model_dump(mode="json"),
                      ensure_ascii=False, indent=2) + "\n"
