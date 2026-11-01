"""端到端生成流水线（多来源 → 导图）。

对应 docs/ARCHITECTURE_ADDENDUM.md 第 3 节：
    understand → outline → validate → verify

与现有 `POST /api/generations` 的区别：
    /generations        单段文本，同步，兼容早期前端
    /pipeline/generate  多来源（Source 列表），返回阶段轨迹与诊断
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.contracts.diagnostic import Diagnostic
from app.contracts.mindmap import MindMapContent
from app.contracts.source import Source
from app.pipeline.service import PipelineGenerationError, PipelineInputError, run

router = APIRouter(prefix="/pipeline", tags=["生成流水线"])


class StageTraceOut(BaseModel):
    stage: str
    ok: bool
    detail: str = ""


class PipelineRequest(BaseModel):
    sources: list[Source] = Field(min_length=1, max_length=50)


class PipelineResponse(BaseModel):
    mode: str
    content: MindMapContent
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    stages: list[StageTraceOut] = Field(default_factory=list)


@router.post("/generate", response_model=PipelineResponse)
def generate_from_sources(body: PipelineRequest):
    """多来源生成。demo 模式下可完整跑通，返回占位导图与真实诊断。"""
    try:
        result = run(body.sources)
    except PipelineInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PipelineGenerationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return PipelineResponse(
        mode=result.mode,
        content=result.content,
        diagnostics=result.diagnostics,
        stages=[StageTraceOut(**s.__dict__) for s in result.stages],
    )
