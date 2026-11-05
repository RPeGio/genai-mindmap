"""第三层：引用与本次提供的原文匹配，不将文本匹配宣称为事实核验。"""
from __future__ import annotations

from app.contracts.diagnostic import Diagnostic, DiagnosticLevel
from app.contracts.mindmap import MindMapContent
from app.contracts.source import Source, SourceRef


def _reference_problem(ref: SourceRef, source: Source | None) -> tuple[str, str] | None:
    if source is None or source.status != "parsed" or not (source.text or "").strip():
        return "SOURCE_TEXT_UNAVAILABLE", "未提供已解析的来源原文，无法核验引用"
    text = source.text or ""
    if ref.start is not None and ref.end is not None:
        if ref.end > len(text):
            return "SOURCE_RANGE_OUT_OF_BOUNDS", "引用字符区间超出来源原文"
        excerpt = text[ref.start:ref.end]
        if not excerpt.strip():
            return "EMPTY_SOURCE_EXCERPT", "引用字符区间只有空白"
        if ref.quote is not None and ref.quote != excerpt:
            return "SOURCE_QUOTE_MISMATCH", "引文与指定字符区间的原文不一致"
    elif ref.quote is not None:
        if ref.quote not in text:
            return "SOURCE_QUOTE_MISMATCH", "在指定来源原文中找不到该引文"
    else:
        return "UNVERIFIED_SOURCE_REF", "引用没有引文或字符区间，无法核验原文依据"

    # SourceSpan.page 记录总页数，不是页与文本的映射；只能排除明确不可能的页码。
    if ref.page is not None:
        total_pages = source.span.page if source.span else None
        if (total_pages is not None and ref.page > total_pages) or ref.page in source.failed_pages:
            return "INVALID_SOURCE_PAGE", "引用页码超出来源页数或指向解析失败的页面"
    return None


def verify_source_refs(content: MindMapContent, sources: list[Source], *,
                       remove_invalid: bool = False) -> list[Diagnostic]:
    """只检查指定来源；生成时可剔除无效引用，诊断接口始终只读。

    没有原文时不能凭 source_usage 推断已验证。未找到依据的节点只给出提示，
    不删除节点，也不据此生成自动删除建议。
    """
    by_id = {source.id: source for source in sources}
    if len(by_id) != len(sources):
        raise ValueError("核验来源 ID 不能重复")
    diagnostics: list[Diagnostic] = []
    stack = [content.root]
    while stack:
        node = stack.pop()
        verified: list[SourceRef] = []
        for ref in node.source_refs:
            problem = _reference_problem(ref, by_id.get(ref.source_id))
            if problem is not None:
                code, message = problem
                diagnostics.append(Diagnostic(
                    level=DiagnosticLevel.warn, code=code, node_id=node.id,
                    message=f"来源 {ref.source_id}：{message}", evidence=ref.quote,
                ))
                continue
            verified.append(ref)
            if ref.page is not None:
                diagnostics.append(Diagnostic(
                    level=DiagnosticLevel.info, code="SOURCE_PAGE_UNVERIFIED", node_id=node.id,
                    message=f"来源 {ref.source_id} 的引文已匹配，但当前无逐页文本映射，页码尚未核验",
                    evidence=ref.quote,
                ))
        if remove_invalid:
            node.source_refs = verified
        if not verified:
            diagnostics.append(Diagnostic(
                level=DiagnosticLevel.warn, code="UNSOURCED_NODE", node_id=node.id,
                message=f"节点「{node.label}」没有可核验的原文引用；不代表节点内容错误",
            ))
        stack.extend(reversed(node.children))
    return diagnostics


def annotate_nli(content: MindMapContent, sources: list[Source]) -> list[Diagnostic]:
    """可选层：对**已通过前两层**的引用跑语义蕴含打分。

    前两层是机械判据（字段合法、quote 逐字出现在原文里），它们管不住
    「引文逐字对得上、但节点论断与原文语义相反」这一类。这一层补的正是它。

    **只产出诊断，不改任何 source_ref。** 引用存不存在由前两层决定；这一层
    只回答"一条已存在的引用在语义上站不站得住"。判定相反时引用留着、同时
    给一条 contradiction 警告，两边都保留由人判断——概率模型不该推翻确定性判据。

    没有 quote 也没有字符区间的引用直接跳过：拿整篇原文去比一个节点标签，
    NLI 只会给 neutral，没有信息量还白花时间。

    模块未启用时立刻返回空列表，连 import 都不发生。
    """
    from app.optimization import nli

    if not nli.available():
        return []

    by_id = {source.id: source for source in sources}
    diagnostics: list[Diagnostic] = []
    stack = [content.root]
    while stack:
        node = stack.pop()
        for ref in node.source_refs:
            evidence = ref.quote
            if not evidence and ref.start is not None and ref.end is not None:
                source = by_id.get(ref.source_id)
                if source is not None:
                    evidence = (source.text or "")[ref.start:ref.end]
            if not (evidence or "").strip():
                continue

            result = nli.judge(node.label, evidence)
            if not result:
                continue

            if result["label"] == "contradiction":
                diagnostics.append(Diagnostic(
                    level=DiagnosticLevel.warn, code="SOURCE_REF_CONTRADICTED",
                    node_id=node.id,
                    message=(f"节点「{node.label}」与来源 {ref.source_id} 的引文"
                             f"语义相反（置信 {result['score']:.2f}）；"
                             f"引用本身逐字匹配，问题出在语义方向"),
                    evidence=ref.quote,
                ))
            else:
                diagnostics.append(Diagnostic(
                    level=DiagnosticLevel.info, code="SOURCE_REF_NLI",
                    node_id=node.id,
                    message=(f"来源 {ref.source_id} 引文语义判定："
                             f"{result['label']}（{result['score']:.2f}）"),
                    evidence=ref.quote,
                ))
        stack.extend(reversed(node.children))
    return diagnostics
