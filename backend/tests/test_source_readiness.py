"""请求带了材料却没有一份能参与校验时，必须有一条诊断说清原因。

`_verify_sources` 的判据是"`status == "parsed"` 且正文非空白"，不合格的来源被
**静默剔除**：不抛异常、不记日志，引用在 `remove_invalid` 那一步消失。而契约里
`Source.status` 的默认值是 `"pending"`（`app/contracts/source.py`），所以一个
没显式标成 `parsed` 的调用方会看到"材料在请求里、引用一条都没有、什么都查不到"。

这组用例钉的就是这条路：材料被剔除时输出 `SOURCE_NOT_PARSED`，没被剔除时不出。
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, patch

from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
from app.agent.tools import build_registry
from app.contracts.source import Source, SourceKind
from app.generation.service import SOURCE_MODE_ENV, SOURCE_MODE_FULL

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


def _tree() -> dict:
    """三条一级分支（`MIN_BRANCHES` 的下限），不带引用。"""
    return {
        "title": "制度要点",
        "root": {
            "id": "root", "label": "制度要点",
            "children": [
                {"id": f"b{index}", "label": f"分支{index}",
                 "children": [{"id": f"b{index}-1", "label": f"要点{index}", "children": []}]}
                for index in range(3)
            ],
        },
    }


def _diagnostics(sources: list[Source]) -> list[dict]:
    ctx = _ctx(sources)
    mocked = AsyncMock(return_value=_tree())
    with patch.dict(os.environ, {SOURCE_MODE_ENV: SOURCE_MODE_FULL}), \
            patch("app.agent.tools.chat_async", new=mocked):
        result = asyncio.run(
            build_registry("create", has_sources=bool(sources)).execute(
                ToolCall(call_id="c-gen", name="mindmap_generate",
                         arguments={"topic": "制度要点"}),
                ctx,
            )
        )
    return result.data["diagnostics"]


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
