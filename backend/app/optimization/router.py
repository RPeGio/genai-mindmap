from fastapi import APIRouter
from pydantic import BaseModel

from app.contracts.diagnostic import Diagnostic, SuggestResponse
from app.contracts.mindmap import SaveRequest
from app.optimization.service import check, suggest

router = APIRouter(prefix="/optimization", tags=["逻辑优化"])


class CheckResponse(BaseModel):
    diagnostics: list[Diagnostic]


@router.post("/check", response_model=CheckResponse)
def check_map(body: SaveRequest):
    """对一棵导图做结构诊断，不修改内容。已实现两条确定性规则。"""
    return CheckResponse(diagnostics=check(body.content))


@router.post("/suggest", response_model=SuggestResponse)
def suggest_for_map(body: SaveRequest):
    """给出候选修改列表。用户确认后才能应用（当前无自动应用接口）。"""
    return SuggestResponse(suggestions=suggest(body.content),
                           diagnostics=check(body.content))
