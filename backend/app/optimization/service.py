"""结构诊断（F15）与候选建议（F16）。

位置：agent 的生成收尾（`agent/tools.py::_generate`）与 optimization 路由共用；
      `suggest()` 面向用户产出可确认的修改建议。

check() 检查单子节点、达到深度上限及同一父节点下的重复标签；
提供 sources 时还会核验引用原文。这不等于事实核验或语义蕴含判断。

重要：建议是候选，必须由用户确认后应用，不能自动覆盖手工编辑。
"""
from __future__ import annotations

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


def _iter_nodes(node: Node, depth: int = 1):
    yield node, depth
    for child in node.children:
        yield from _iter_nodes(child, depth + 1)


def check(content: MindMapContent, sources: list[Source] | None = None) -> list[Diagnostic]:
    """结构诊断；只有显式提供原文时才执行第三层引用校验。"""
    diagnostics: list[Diagnostic] = []

    # 规则 1：单节点分支（只有一个子节点，通常是层级没展开）
    for node, depth in _iter_nodes(content.root):
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

    # 规则 2：只比较同级节点，不同分支的相同文字可能含义不同。
    for parent, _ in _iter_nodes(content.root):
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

    if sources is not None:
        diagnostics.extend(verify_source_refs(content, sources))
    return diagnostics


def suggest(content: MindMapContent) -> list[Suggestion]:
    """把诊断结果转成用户可确认的候选修改。

    现在只把重复标签转成 dedupe 建议；涉及内容改写的建议需要模型，
    留给 F16 的后续实现。
    """
    suggestions: list[Suggestion] = []
    diagnostics = check(content)
    for diag in diagnostics:
        if diag.code == "DUPLICATE_LABEL":
            suggestions.append(Suggestion(
                kind=SuggestionKind.dedupe,
                node_id=diag.node_id,
                message=diag.message,
                evidence=diag.evidence,
            ))
    return suggestions
