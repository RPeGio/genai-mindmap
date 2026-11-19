"""请求带了材料却没有一份能参与校验时，必须有一条诊断说清原因。

`_verify_sources` 的判据是"`status == "parsed"` 且正文非空白"，不合格的来源被
**静默剔除**：不抛异常、不记日志，引用在 `remove_invalid` 那一步消失。而契约里
`Source.status` 的默认值是 `"pending"`（`app/contracts/source.py`），所以一个
没显式标成 `parsed` 的调用方会看到"材料在请求里、引用一条都没有、什么都查不到"。

这组用例钉的就是这条路：材料被剔除时输出 `SOURCE_NOT_PARSED`，没被剔除时不出。
"""
from __future__ import annotations

from app.agent.contracts import AgentRunRequest, RunBudget, RunContext
from app.agent.tools import _source_readiness_diagnostic
from app.contracts.source import Source, SourceKind

MATERIAL = "预算由财务经理核准后执行，超过五十万元报总经理审批。"


def _source(status: str = "parsed", text: str = MATERIAL, source_id: str = "doc-1") -> Source:
    return Source(id=source_id, kind=SourceKind.text, status=status,
                  filename="制度.md", text=text)


def _ctx(sources: list[Source]) -> RunContext:
    request = AgentRunRequest(
        goal="整理材料", intent="create", sources=sources,
        budget=RunBudget(max_model_calls=8, max_tool_calls=4, max_output_tokens=32768),
    )
    return RunContext(request=request, candidate=None,
                      sources_by_id={source.id: source for source in sources})


def _diagnostics(sources: list[Source]) -> list[dict]:
    """直接调那个纯函数：它只依赖 `ctx.request.sources` 与 `_verify_sources`。

    上一版走 `mindmap_generate` 工具并 patch `SOURCE_MODE_FULL`——那个工具与
    那套形态开关都已在「只留一条生成路」里删掉，而诊断本身还在
    （`_generate_deep` 收尾会调它）。直接调函数比驱动一次生成更贴近被测对象。
    """
    diag = _source_readiness_diagnostic(_ctx(sources))
    return [] if diag is None else [diag.model_dump()]


def _readiness(sources: list[Source]) -> dict | None:
    for item in _diagnostics(sources):
        if item["code"] == "SOURCE_NOT_PARSED":
            return item
    return None


def test_pending_source_is_reported() -> None:
    """材料没标 parsed：必须有诊断，且说清"一份都没参与"。"""
    item = _readiness([_source(status="pending")])
    assert item is not None
    assert "1 份未标记为已解析" in item["message"]
    assert "都不会参与校验" in item["message"] or "都没有参与校验" in item["message"]
    assert "不会有任何引用" in item["message"]


def test_parsed_source_is_not_reported() -> None:
    assert _readiness([_source(status="parsed")]) is None


def test_blank_text_is_reported_as_empty() -> None:
    item = _readiness([_source(status="parsed", text="   ")])
    assert item is not None
    assert "1 份正文为空" in item["message"]


def test_partial_drop_names_the_remaining_count() -> None:
    """一份可用、一份不可用：诊断说明只有其余那份能进引用，而不是"没有引用"。"""
    item = _readiness([_source(source_id="doc-1"),
                       _source(status="pending", source_id="doc-2")])
    assert item is not None
    assert "有 1 份没有参与校验" in item["message"]
    assert "其余 1 份" in item["message"]


def test_no_sources_means_no_diagnostic() -> None:
    assert _readiness([]) is None
