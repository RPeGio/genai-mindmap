from fastapi import APIRouter

from app.agent.service import AgentPlan, AgentTaskRequest, plan

router = APIRouter(prefix="/agent", tags=["AI 代理"])


@router.post("/tasks", response_model=AgentPlan)
def create_task(body: AgentTaskRequest):
    """目标驱动生成（F02）。当前只返回阶段骨架，不产生导图。"""
    return plan(body.goal)
