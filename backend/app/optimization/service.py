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
这一版的三层判据（成本从零往上）
======================================================================
1. **规则层**（零成本、确定性）：`check()` 的全部规则。同一个输入永远同样的
   输出，所以它敢接在生成收尾，也敢让 `/check` 反复调。
2. **候选层**（零成本）：`suggest()` 把诊断翻成"给用户看的动作"，
   `_SUGGESTION_OF_CODE` 是唯一的映射表。
3. **模型层**（**一次** LLM 调用，默认关）：只判"同级近义"，判据见
   `_model_near_pairs`。它是本功能里唯一的成本，所以有开关
   （`MINDMAP_OPTIMIZATION_LLM` 或 `suggest(use_model=...)`）、有规模下限
   （`MIN_LLM_SIBLINGS`）、失败一律返回空列表。

**模型层只在 `suggest()` 里跑，不进 `check()`**：`check()` 一旦接上概率模型，
同一个接口两次调用就会给出不同的诊断，生成收尾那个 `reported` 计数也不再可复现。
诊断是事实、建议是候选——概率模型只配产出后者。

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

import json
import os
import unicodedata
from typing import Iterator

import httpx

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

# ---------------------------------------------------------------- 模型层配置

# 唯一的成本开关，缺省关：这一层要一次网络调用，而"没配模型"在设备上是常态
# （`MINDMAP_LLM_*` 三个变量缺一个就连不上）。
LLM_ENV = "MINDMAP_OPTIMIZATION_LLM"

# 参与比较的同级节点总数下限。低于它不跑模型：8 个节点最多十几对标签，人扫一眼
# 就完了，一次网络调用的延迟换不来信息量。
MIN_LLM_SIBLINGS = 8

# 发给模型的标签上限。按遍历顺序截断：大图上"人更需要机器帮看"这个直觉恰好会让
# 一次同步请求的成本失控。
MAX_LLM_LABELS = 120

# 模型一次最多采纳几对。它偶尔会把并列的近亲概念（监督学习/无监督学习）算成冗余；
# 封顶让最坏情况也只多出 10 条待确认建议。
MAX_LLM_PAIRS = 10

# 单次调用超时。这个数由**交互形态**决定，不是模型能力：`/suggest` 是用户点了
# 才发的同步请求，等十几秒已经比"没有这条建议"更糟。实测见 `LLM_TIMEOUT_MEASURED`。
LLM_TIMEOUT_SECONDS = 20.0

#: 实测记录（DeepSeek `deepseek-chat`，直连）：43 节点的「考研数学复习计划」
#: 一次 1.4–1.8 s，输出 ~40 token。超时值是它的十倍有余，只在网络抽风时兜底。
LLM_TIMEOUT_MEASURED = "1.4–1.8 s / 43 节点 1 次调用"

# 响应体上限，与仓库里其它模型出口同口径（跑飞的响应不该把内存读满）。
_MAX_RESPONSE_BYTES = 1_000_000

_TRUTHY = {"1", "true", "yes", "on", "y", "t"}

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


def suggest(content: MindMapContent, *, use_model: bool | None = None) -> list[Suggestion]:
    """把诊断结果转成用户可确认的候选修改。

    `use_model`：`None` 走环境变量（`MINDMAP_OPTIMIZATION_LLM`，缺省关），
    `True` / `False` 显式开关（调用方与测试用）。关掉之后这一层**一次调用都不
    发生**，也不需要网络。

    建议不自动应用——见模块 docstring 的两条路。
    """
    suggestions: list[Suggestion] = []
    rule_pairs: set[frozenset[str]] = set()
    for diag in check(content):
        kind = _SUGGESTION_OF_CODE.get(diag.code)
        if kind is None:
            continue
        if diag.code == "NEAR_DUPLICATE_LABEL" and diag.evidence:
            rule_pairs.add(frozenset((diag.node_id or "", diag.evidence)))
        suggestions.append(Suggestion(
            kind=kind,
            node_id=diag.node_id,
            message=f"{_ACTION_OF_CODE[diag.code]}：{diag.message}",
            evidence=diag.evidence,
        ))

    if _should_use_model(content, use_model):
        suggestions.extend(_model_suggestions(content, rule_pairs))
    return suggestions


# ---------------------------------------------------------------- 模型层


def _llm_enabled() -> bool:
    return str(os.getenv(LLM_ENV, "")).strip().lower() in _TRUTHY


def _sibling_groups(
    root: Node, *, limit: int | None = None,
) -> list[list[tuple[str, str]]]:
    """参与模型比较的同级组，每组是 `(标签, 节点 id)`，顺序即发给模型的行号。

    标签与 id 一次取出、不再重走一遍树：`_model_suggestions` 要按行号把答案换回
    节点，而两次独立遍历只要有一处 `limit` 截断不同，行号就会错位——那种错位
    表现是"建议指向了别的分支"，看不出来是 bug。

    `limit` 按遍历顺序截到标签总量不超过它：`/suggest` 是同步请求，标签越多 token
    越多，而"这张图有 300 个节点"那种情况本来也该先分片体检。
    """
    groups: list[list[tuple[str, str]]] = []
    total = 0
    for node, _ in _iter_nodes(root):
        items: list[tuple[str, str]] = []
        for child in node.children:
            label = child.label.strip()
            if label and label not in {item[0] for item in items}:
                items.append((label, child.id))
        if len(items) < 2:
            continue
        if limit is not None:
            room = limit - total
            if room < 2:
                break
            items = items[:room]
        groups.append(items)
        total += len(items)
        if limit is not None and total >= limit:
            break
    return groups


def _sibling_label_groups(root: Node, *, limit: int | None = None) -> list[list[str]]:
    """`_sibling_groups` 的标签视图（测量脚本与"要不要跑模型"的判断用）。"""
    return [[label for label, _ in group]
            for group in _sibling_groups(root, limit=limit)]


def _should_use_model(content: MindMapContent, use_model: bool | None) -> bool:
    """这一次要不要跑模型。三个条件缺一不可，前一个是成本闸、后一个是规模闸。

      1. 开关（参数优先，其次环境变量，缺省关）；
      2. 有可比较的同级组——整棵树全是单子节点链时没有"同级"可言；
      3. 参与比较的同级节点总数 ≥ `MIN_LLM_SIBLINGS`。小图上人一眼扫完，
         一次网络调用的延迟换不来信息量。
    """
    if not (use_model if use_model is not None else _llm_enabled()):
        return False
    groups = _sibling_label_groups(content.root, limit=MAX_LLM_LABELS)
    return sum(len(group) for group in groups) >= MIN_LLM_SIBLINGS


def _model_near_pairs(groups: list[list[str]]) -> list[tuple[int, str, str, str]]:
    """一次 LLM 调用。返回 `(行号, 标签A, 标签B, 理由)`，行号从 0 起；失败返回空。

    为什么让模型做**确认**而不是读整棵树：规则层已经把"字面上像"的全抓了，留给
    模型的只剩"字面上不像、语义上是同一件事"那一小撮（正则化/规范化、降维/维数
    约简）。所以只喂同级标签，并要求它**按行**回答。

    按行（行号进、行号出）不是美学选择。第一版发的是嵌套 JSON、只要标签对，结果
    在两张真实图上模型的 11 对里 **11 对全部跨行**：它把"父标签 + 子标签"当成
    "说的是同一件事"（「按任务选模型族」/「按输出类型匹配模型族」正是父子），
    而那两条根本不是同级冗余。行号把"同一行内"变成它的输出约束，调用方按行号
    取节点，就不再需要事后猜"这两个标签是不是同级"。

    **绝不抛异常**（与 `optimization/nli.py` 同一条硬约束）：体检是附加信息，
    模型服务挂了不该让 `/suggest` 变成 500。配置缺失、网络错误、非 JSON 输出、
    响应过大，全部收敛成"这一层没有结论"。
    """
    if not groups:
        return []
    # 延迟导入：关闭时连 `generation` 那一坨都不进 import 图（与 nli.py 同一条
    # "关闭即零开销"的规矩）。配置口径也必须复用 `_config()`——两处各读一遍环境
    # 变量，迟早出现"生成能连、体检连不上"的分歧。
    from app.generation.service import GenerationError, _config, parse_model_json

    try:
        base, key, model = _config()
    except GenerationError:
        return []

    system = (
        "你是中文思维导图的结构审校员。用户按行给出若干组标签，每行一组，行号从 1 "
        "开始，行内是**同一个父节点下的所有子节点标签**，用 | 分隔。\n"
        "任务：只在**同一行内部**找**重复**的标签对——两个标签在读者看来指的是同一"
        "件事，只留一个不丢信息。判断标准是这一问：**把这两个合成一个，读者会丢掉"
        "一个独立的信息点吗？** 会丢 → 不是重复，不要报。\n"
        "不要报的情况（都比「重复」常见）：同一主题的两个不同方面（「定义与基本概念」"
        "与「理论基础」）、上下位、因果、并列的近亲概念（监督学习 / 无监督学习）、"
        "同一模板的两个实例（按题型分配 / 按模块分配）、一个是对另一个的概述。\n"
        "不同行之间永远不算：它们不在同一个父节点下，含义可以不同。\n"
        "只输出 JSON，不要解释：{\"pairs\": [{\"line\": 1, \"a\": \"标签原文\", "
        "\"b\": \"标签原文\", \"why\": \"合并之后为什么不会丢信息\"}]}；一对都没有就"
        "输出 {\"pairs\": []}。a、b 必须逐字出现在该行里。"
    )
    lines = [f"{index}: {' | '.join(group)}" for index, group in enumerate(groups, 1)]
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": "\n".join(lines)}],
        # 0 温：同一张图点两次检查该给同一批建议，否则用户会以为是随机的。
        "temperature": 0.0,
        "max_tokens": 512,
    }
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    try:
        with httpx.Client(timeout=LLM_TIMEOUT_SECONDS, follow_redirects=True) as client:
            with client.stream("POST", base + "/chat/completions",
                               content=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                               headers=headers) as response:
                response.raise_for_status()
                buffer = bytearray()
                for chunk in response.iter_bytes():
                    buffer.extend(chunk)
                    if len(buffer) > _MAX_RESPONSE_BYTES:
                        return []
    except (httpx.HTTPError, OSError):
        return []

    try:
        text = json.loads(buffer)["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        return []
    try:
        parsed = parse_model_json(text or "")
    except GenerationError:
        return []
    pairs = parsed.get("pairs") if isinstance(parsed, dict) else None
    if not isinstance(pairs, list):
        return []

    findings: list[tuple[int, str, str, str]] = []
    for item in pairs:
        if not isinstance(item, dict):
            continue
        line, left, right = item.get("line"), item.get("a"), item.get("b")
        if not isinstance(left, str) or not isinstance(right, str):
            continue
        left, right = left.strip(), right.strip()
        if not left or not right or left == right:
            continue
        # 行号可以是数字也可以是数字字符串（模型两种都写）。
        try:
            index = int(line) - 1
        except (TypeError, ValueError):
            # 没给行号的答案不采纳：行号是"这两个是同级"的唯一凭据。
            continue
        why = item.get("why")
        findings.append((index, left, right, why if isinstance(why, str) else ""))
        if len(findings) >= MAX_LLM_PAIRS:
            break
    return findings


def _group_nodes(content: MindMapContent, limit: int) -> dict[int, dict[str, str]]:
    """行号 → `{标签: 节点 id}`。与 `_sibling_groups` 同一份遍历结果。"""
    return {
        index: {label: node_id for label, node_id in group}
        for index, group in enumerate(_sibling_groups(content.root, limit=limit))
    }


def _model_suggestions(
    content: MindMapContent, rule_pairs: set[frozenset[str]],
) -> list[Suggestion]:
    """模型层 → `dedupe` 建议。

    四条过滤，缺一条就会被模型的越界答案污染：

      1. 行号必须落在发出去的行里，且两个标签**逐字属于**那一行（模型会答父标签
         或别的行，实测第一版 11/11 都是这种）；
      2. 规则层已经报过的那一对不重复报（同一个问题两条建议，用户点两次）；
      3. 同一个节点最多落一条建议——一次合并只能有一个落点。

    `confidence` 保持 None：模型给的是判定，不是校准过的概率（契约注释里那句
    "规则建议没有经过概率校准"同样适用）。
    """
    groups = _sibling_label_groups(content.root, limit=MAX_LLM_LABELS)
    findings = _model_near_pairs(groups)
    if not findings:
        return []

    members = _group_nodes(content, MAX_LLM_LABELS)
    suggestions: list[Suggestion] = []
    taken: set[str] = set()
    for index, left, right, why in findings:
        ids = members.get(index, {})
        if left not in ids or right not in ids:
            continue
        left_id, right_id = ids[left], ids[right]
        if left_id == right_id or frozenset((left_id, right_id)) in rule_pairs:
            continue
        if left_id in taken or right_id in taken:
            continue
        taken.update((left_id, right_id))
        reason = f"（模型判定：{why}）" if why else ""
        suggestions.append(Suggestion(
            kind=SuggestionKind.dedupe,
            node_id=right_id,
            message=(f"{_ACTION_OF_CODE['NEAR_DUPLICATE_LABEL']}："
                     f"同级节点「{right}」与「{left}」近义{reason}"),
            evidence=left_id,
        ))
    return suggestions


def _not_converted_reason(code: str) -> str | None:
    """这个诊断码为什么不转建议（`None` 表示它其实该转）。测量脚本用它解释缺口。"""
    if code in NOT_SUGGESTED:
        return NOT_SUGGESTED[code]
    if code.startswith(_REFERENCE_CODE_PREFIXES):
        return "引用类诊断：只说明文献是否可核验，动作是补证据而不是合并/展开"
    return None
