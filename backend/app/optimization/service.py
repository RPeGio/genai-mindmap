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

# 引用类诊断的前缀。这类码只给诊断、不给建议（理由见模块 docstring）。
_REFERENCE_CODE_PREFIXES = (
    "SOURCE_", "UNSOURCED_", "UNVERIFIED_", "INVALID_", "EMPTY_",
)

# 诊断码 → 建议动作。**动作名词**（`dedupe` / `expand` / `add_detail`），不是
# 诊断码的复述：用户看到的是"点一下会发生什么"。
_SUGGESTION_OF_CODE: dict[str, SuggestionKind] = {
    "DUPLICATE_LABEL": SuggestionKind.dedupe,
    "SINGLE_CHILD": SuggestionKind.expand,
}

# 建议文案的**动作**部分。诊断说"哪里不对"，建议说"点了会怎样"。
_ACTION_OF_CODE: dict[str, str] = {
    "DUPLICATE_LABEL": "合并这两个同级节点，保留一个",
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


def check(content: MindMapContent, sources: list[Source] | None = None) -> list[Diagnostic]:
    """结构诊断；只有显式提供原文时才执行引用校验。

    严格确定性：同一个输入永远同样的诊断与顺序。模型层不在这一层——`check()`
    会被生成收尾反复调用，也可能被前端缓存。
    """
    root = content.root
    diagnostics: list[Diagnostic] = []
    diagnostics.extend(_shape_diagnostics(root))
    diagnostics.extend(_duplicate_diagnostics(root))
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
