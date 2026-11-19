"""结构诊断（F15）与候选建议（F16）。

位置：agent 的生成收尾（`agent/tools.py::_generate`）与 optimization 路由共用；
      `suggest()` 面向用户产出可确认的修改建议。

check() 检查单子节点、达到深度上限及同一父节点下的重复标签；
提供 sources 时还会核验引用原文。这不等于事实核验或语义蕴含判断。

重要：建议是候选，必须由用户确认后应用，不能自动覆盖手工编辑。

======================================================================
两条路不能混
======================================================================
    ops      模型改图   直接落画布     （已存在，`agent/ops.py`）
    suggest  建议       用户确认才应用  （本文件）

`check()` 的结果**永远不自动应用**。把"这棵树有问题"当成"这棵树该变成那样"，
用户会在保存时发现图莫名其妙变了，而且找不到是谁改的。

======================================================================
两层判据（都是零成本）
======================================================================
1. **规则层**（确定性）：`check()` 的全部规则。同一个输入永远同样的输出，所以它
   敢接在生成收尾，也敢让 `/check` 反复调。
2. **候选层**：`suggest()` 把诊断翻成"给用户看的动作"，`_SUGGESTION_OF_CODE`
   是唯一的映射表；转不了的码在 `NOT_SUGGESTED` 里写清理由。

======================================================================
故意不转建议的诊断
======================================================================
见 `NOT_SUGGESTED`（码 → 理由）与 `_REFERENCE_CODE_PREFIXES`。两条要点：

  · `DEPTH_LIMIT`——"已到 8 层"不是一个可执行的动作。契约本身禁止第 9 层
    （`MAX_DEPTH` 与 `contracts.mindmap` 的深度上限同值），所以既没有"再展开一层"
    可做，也没有"减一层"这种可定位的编辑——要减掉的是这一支的中间层，那是重写
    一支而不是改一个节点。它只作为 warn 诊断存在。
  · 引用类（`SOURCE_*` / `UNSOURCED_*` / `UNVERIFIED_*` / `INVALID_*` / `EMPTY_*`）
    ——`drop`（剔除无依据节点）是对内容真实性的判断：逐字命中只说明这句话出自
    那里，不说明那个来源可靠（F13 的口径），而 NLI 那一层实测里 24 条真实引用
    有 4 条被误判成相反（见 `optimization/nli.py` 的 `DEFAULT_ON_GENERATE`）。
    成批产出"删掉这些节点"的建议，代价远大于收益。它们只给诊断。

======================================================================
前置依赖：`relations` 在界面上根本不渲染
======================================================================
"建议添加连接线"这一类**因果关系**建议现在做不了，不是算法没写，是**做了用户也
看不见结果**。`frontend/src/mindmap/edits.ts` 的原话：

    `relations`（跨节点关系线）**三种形状的顶层都有**……用户既看不到那条残留的
    关系，也没法删掉它

`relations` 目前只有"删节点时被连带清理"（`_prune_dangling_relations`）这一个
读者。所以因果推断这一项的前置条件是**先把 `relations` 画出来**（F16 的界面
欠账），不是补一个检测函数。别看到这里缺一块就以为漏做了。
"""
from __future__ import annotations

import unicodedata
from typing import Iterator

from app.contracts.diagnostic import (
    Diagnostic,
    DiagnosticLevel,
    Suggestion,
    SuggestionKind,
)
from app.contracts.mindmap import MindMapContent, Node
from app.contracts.source import Source
from app.optimization.source_refs import verify_source_refs

# 与契约保持一致的硬限制，诊断时复用
MAX_DEPTH = 8

# ---------------------------------------------------------------- 规则层阈值
#
# 每个数都说清依据。这一层产出的是**提示**不是判决：判错的代价是用户多看一眼，
# 判漏的代价是"重复的两支一直留在图里"，所以宁可把低置信度那档降成 info，
# 也不要把阈值调到几乎不响——一个常年沉默的检查等于没有检查。

# 模糊比较的字符下限。2 个字之间编辑距离 1 意味着换掉一半（「定义」⇄「定理」、
# 「物理」⇄「数学」），那是两个概念；3 个字起才会出现「监督学习」⇄「有监督学习」
# 这种"同一个概念的两种写法"。
MIN_FUZZY_CHARS = 3

# 编辑距离上限。只用在**纯插删**这一种变形上（见 `_near_reason`）：中文里"同一个
# 概念的两种写法"最典型的形态就是加一个限定词（数据→**预**处理、监督学习→**有**
# 监督学习），一次插入；上限 2 允许加两个字的修饰语（「交叉验证」⇄「分层交叉验证」），
# 再多就不是修饰而是另一个概念了。
MAX_EDIT_DISTANCE = 2

# 归一化编辑距离上限 = 插入字数 / 较短标签字数。3 个字里加 2 个字（0.67）已经
# 换掉了大半语义，挡掉；5 个字加 2 个（0.4）放行。
MAX_EDIT_RATIO = 0.4

# 前缀关系的长度占比下限。「监督学习」/「监督学习与无监督学习」= 4/10；
# 0.4 挡掉"短标题是长标题前缀"的偶然命中（3/12 更像"分类名 + 具体做法"）。
MIN_PREFIX_RATIO = 0.4

# 反向 / 程度对照类的修饰语。插进标签的若是这一类字，两条标签描述的是**两个并列
# 方向**而不是同一个东西，不报。依据是全库 38 张真实生成图上的实测：纯编辑距离
# 判据一共命中 43 对，其中 40 对是平行/对立命名（「监督学习」/「无监督学习」3 张图、
# 「从重处罚的适用」/「从轻处罚的适用」、「分类任务选算法」/「回归任务选算法」、
# 「业务指标监控」/「技术指标监控」、「第一条分支」/「第二条分支」…），
# 真正近义的 **0 对**。合并掉「监督学习」与「无监督学习」是丢内容，不是去重。
CONTRASTING_MODIFIERS = frozenset("无非不未否反逆伪假轻重低高多少大小弱强")

# 引用类诊断的前缀。这类码只给诊断、不给建议（理由见模块 docstring）。
_REFERENCE_CODE_PREFIXES = (
    "SOURCE_", "UNSOURCED_", "UNVERIFIED_", "INVALID_", "EMPTY_",
)

# 诊断码 → 建议动作。**动作名词**（`dedupe` / `expand` / `add_detail`），不是
# 诊断码的复述：用户看到的是"点一下会发生什么"。
_SUGGESTION_OF_CODE: dict[str, SuggestionKind] = {
    "DUPLICATE_LABEL": SuggestionKind.dedupe,
    "NEAR_DUPLICATE_LABEL": SuggestionKind.dedupe,
    "SINGLE_CHILD": SuggestionKind.expand,
}

# 建议文案的**动作**部分。诊断说"哪里不对"，建议说"点了会怎样"。
_ACTION_OF_CODE: dict[str, str] = {
    "DUPLICATE_LABEL": "合并这两个同级节点，保留一个",
    "NEAR_DUPLICATE_LABEL": "确认是不是同一件事：是就合并，不是就把说法区分开",
    "SINGLE_CHILD": "把这一支再展开一层，或者并进相邻节点",
}

# 故意不转建议的码 → 理由。有用例钉着"检查器能产出的每一个码，要么在
# `_SUGGESTION_OF_CODE` 里，要么在 `NOT_SUGGESTED` 里"，免得以后新增一条诊断
# 然后忘了决定它转不转。
NOT_SUGGESTED: dict[str, str] = {
    "DEPTH_LIMIT": (
        "「已到 8 层」不是一个可执行的动作：契约禁止第 9 层，所以没有"
        "「再展开一层」可做；而「减一层」要删的是整条中间层，那是重写一支，"
        "不是对某个节点的编辑"
    ),
}


def _iter_nodes(node: Node, depth: int = 1) -> Iterator[tuple[Node, int]]:
    yield node, depth
    for child in node.children:
        yield from _iter_nodes(child, depth + 1)


def _normalize_label(text: str) -> str:
    """归一化到"只留文字"：全半角、大小写、空白、标点符号全部抹平。

    NFKC 把全角折成半角（`Ａ`→`A`、`（`→`(`），`casefold` 处理拉丁文大小写，
    `P*`（标点）与 `S*`（符号）按**类别**整类删——枚举字符必然漏（半角括号与
    全角方括号在 NFKC 之后也不同形）。
    """
    folded = unicodedata.normalize("NFKC", text).casefold()
    return "".join(
        char for char in folded
        if not char.isspace() and not unicodedata.category(char).startswith(("P", "S"))
    )


def _inserted_text(shorter: str, longer: str) -> str | None:
    """`longer` 是否只是 `shorter` 中间插了几个字；是则返回插入的那一段。

    返回 None 表示不是"纯插入"关系（有替换、或者顺序变了）。这一判定比编辑距离
    严格得多，而严格正是这里的要点：替换型差异（重→轻、分类→回归、题→模）在真实
    图上几乎全是**平行命名**，把「从重处罚的适用」与「从轻处罚的适用」判成重复，
    用户点一下就会丢一条刑法情节。
    """
    if len(longer) <= len(shorter):
        return None
    head = 0
    while head < len(shorter) and shorter[head] == longer[head]:
        head += 1
    tail = 0
    while (tail < len(shorter) - head
           and shorter[-1 - tail] == longer[-1 - tail]):
        tail += 1
    if head + tail != len(shorter):
        return None          # 中间还有对不上的部分 → 不是纯插入
    return longer[head:len(longer) - tail]


def _near_reason(left: str, right: str) -> tuple[DiagnosticLevel, str] | None:
    """两条同级标签"近义"到什么程度；不近义返回 None。

    判据按可信度从高到低排，命中即返回：

      1. 归一化后相同（只差标点/空格/全半角）→ **warn**：字面已经一样，只剩排版；
      2. 一方是另一方的前缀 → **info**，「监督学习」/「监督学习与无监督学习」；
      3. 一方是另一方**插入**少量限定词的结果 → **info**，「数据处理」/「数据预处理」、
         「监督学习」/「有监督学习」。插入的词属于反向/程度对照类
         （`CONTRASTING_MODIFIERS`）时不报——那是两个并列方向。

    2、3 只给 info 而不是 warn，因为它们**也可能是上下位关系**（「解方程」/
    「解方程组的」）：字面包含是事实，"是不是同一件事"还得人看一眼。warn 留给
    "字面已经相同"这一档。

    **替换型差异一律不报**，这是全库实测的结论而不是保守：38 张真实生成图上，
    带替换的命中 40 对全是平行/对立命名，真正近义 0 对。换字型近义（评估/评价、
    整理/管理）留给模型层——规则层看不见"两个字是不是同义词"。

    调用方负责先排掉"逐字相同"的那一对：那是 `DUPLICATE_LABEL` 的地盘，
    同一个节点不能既报重复又报近义。
    """
    left_raw, right_raw = left.strip(), right.strip()
    if left_raw == right_raw:
        return None
    left_norm, right_norm = _normalize_label(left_raw), _normalize_label(right_raw)
    if left_norm and left_norm == right_norm:
        return DiagnosticLevel.warn, "去掉标点与空格后完全相同"

    shorter, longer = sorted((left_norm, right_norm), key=len)
    if len(shorter) < MIN_FUZZY_CHARS:
        return None
    if longer.startswith(shorter) and len(shorter) / len(longer) >= MIN_PREFIX_RATIO:
        return (DiagnosticLevel.info,
                f"文字上「{shorter}」是「{longer}」的前缀")

    inserted = _inserted_text(shorter, longer)
    if inserted is None:
        return None
    if len(inserted) > MAX_EDIT_DISTANCE:
        return None
    if len(inserted) / len(shorter) > MAX_EDIT_RATIO:
        return None
    if set(inserted) & CONTRASTING_MODIFIERS:
        return None
    return DiagnosticLevel.info, f"只多了「{inserted}」这 {len(inserted)} 个字"



def _shape_diagnostics(root: Node) -> list[Diagnostic]:
    """形态类：单子节点 / 到深度上限。"""
    diagnostics: list[Diagnostic] = []
    for node, depth in _iter_nodes(root):
        if len(node.children) == 1:
            diagnostics.append(Diagnostic(
                level=DiagnosticLevel.info,
                code="SINGLE_CHILD",
                node_id=node.id,
                message=f"节点「{node.label}」只有一个子节点，可考虑展开或合并",
            ))
        if depth >= MAX_DEPTH:
            diagnostics.append(Diagnostic(
                level=DiagnosticLevel.warn,
                code="DEPTH_LIMIT",
                node_id=node.id,
                message=f"节点「{node.label}」已到 {MAX_DEPTH} 层上限",
            ))
    return diagnostics


def _duplicate_diagnostics(root: Node) -> list[Diagnostic]:
    """只比较同级节点，不同分支的相同文字可能含义不同。"""
    diagnostics: list[Diagnostic] = []
    for parent, _ in _iter_nodes(root):
        seen: dict[str, str] = {}
        for node in parent.children:
            key = node.label.strip()
            if key in seen:
                diagnostics.append(Diagnostic(
                    level=DiagnosticLevel.warn,
                    code="DUPLICATE_LABEL",
                    node_id=node.id,
                    message=f"同级节点「{key}」与 {seen[key]} 文字相同，请确认是否重复",
                    evidence=seen[key],
                ))
            else:
                seen[key] = node.id
    return diagnostics


def _near_duplicate_diagnostics(root: Node) -> list[Diagnostic]:
    """同级**近义**冗余（规则层，零成本）。

    报告在**后一个**节点上（`node_id` = 后者、`evidence` = 前者的 id），与
    `DUPLICATE_LABEL` 同口径：一条建议只需要一个"要动它"的落点。
    """
    diagnostics: list[Diagnostic] = []
    for parent, _ in _iter_nodes(root):
        children = parent.children
        for index, right in enumerate(children):
            for left in children[:index]:
                reason = _near_reason(left.label, right.label)
                if reason is None:
                    continue
                level, why = reason
                diagnostics.append(Diagnostic(
                    level=level,
                    code="NEAR_DUPLICATE_LABEL",
                    node_id=right.id,
                    message=(f"同级节点「{right.label.strip()}」与「{left.label.strip()}」"
                             f"{why}，请确认是否指的是同一件事"),
                    evidence=left.id,
                ))
    return diagnostics


def check(content: MindMapContent, sources: list[Source] | None = None) -> list[Diagnostic]:
    """结构诊断；只有显式提供原文时才执行引用校验。

    严格确定性：同一个输入永远同样的诊断与顺序。模型层不在这一层——`check()`
    会被生成收尾反复调用，也可能被前端缓存。
    """
    root = content.root
    diagnostics: list[Diagnostic] = []
    diagnostics.extend(_shape_diagnostics(root))
    diagnostics.extend(_duplicate_diagnostics(root))
    diagnostics.extend(_near_duplicate_diagnostics(root))
    if sources is not None:
        diagnostics.extend(verify_source_refs(content, sources))
    return diagnostics



def suggest(content: MindMapContent) -> list[Suggestion]:
    """把诊断结果转成用户可确认的候选修改。

    `_SUGGESTION_OF_CODE` 是唯一的映射表；转不了的码（`DEPTH_LIMIT`、引用类）在
    `NOT_SUGGESTED` 里写清了理由。建议不自动应用——见模块 docstring 的两条路。
    """
    suggestions: list[Suggestion] = []
    for diag in check(content):
        kind = _SUGGESTION_OF_CODE.get(diag.code)
        if kind is None:
            continue
        suggestions.append(Suggestion(
            kind=kind,
            node_id=diag.node_id,
            message=f"{_ACTION_OF_CODE[diag.code]}：{diag.message}",
            evidence=diag.evidence,
        ))
    return suggestions


def _not_converted_reason(code: str) -> str | None:
    """这个诊断码为什么不转建议（`None` 表示它其实该转）。测量脚本用它解释缺口。"""
    if code in NOT_SUGGESTED:
        return NOT_SUGGESTED[code]
    if code.startswith(_REFERENCE_CODE_PREFIXES):
        return "引用类诊断：只说明文献是否可核验，动作是补证据而不是合并/展开"
    return None
