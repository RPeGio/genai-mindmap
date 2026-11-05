from fastapi import APIRouter
from pydantic import BaseModel, Field, model_validator

from app.contracts.diagnostic import Diagnostic, SuggestResponse
from app.contracts.mindmap import SaveRequest
from app.contracts.source import Source
from app.optimization.service import check, suggest

router = APIRouter(prefix="/optimization", tags=["逻辑优化"])


class CheckResponse(BaseModel):
    diagnostics: list[Diagnostic]


class CheckRequest(SaveRequest):
    sources: list[Source] | None = Field(default=None, max_length=50)

    @model_validator(mode="after")
    def unique_sources(self) -> "CheckRequest":
        if self.sources is not None and len({s.id for s in self.sources}) != len(self.sources):
            raise ValueError("核验来源 ID 不能重复")
        return self


@router.post("/check", response_model=CheckResponse)
def check_map(body: CheckRequest):
    """只读诊断；可传 sources，按原文核验节点引用。"""
    return CheckResponse(diagnostics=check(body.content, body.sources))


@router.post("/suggest", response_model=SuggestResponse)
def suggest_for_map(body: CheckRequest):
    """给出候选修改列表。用户确认后才能应用（当前无自动应用接口）。"""
    return SuggestResponse(suggestions=suggest(body.content),
                           diagnostics=check(body.content, body.sources))
