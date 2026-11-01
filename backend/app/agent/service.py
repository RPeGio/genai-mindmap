"""AI 代理（F02 / F18）。

位置：用户给一个目标（"设计一款智能家居产品"），先拆成阶段，再逐阶段生成分支。

状态：**骨架**。plan() 返回固定的三段结构，不含模型调用；
真正实现要做的是：目标 → 阶段划分 → 每阶段调用流水线 → 合并成一棵树。

设计约束：
  - 每一步都要记录来源与依据，不只是结果
  - 代理产出的是候选，用户确认后才写进导图（与 F15/F16 同一条原则）
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_STAGES = ["目标澄清", "要点拆解", "分支展开", "风险与验证"]


class AgentPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str
    stages: list[str] = Field(default_factory=list)
    implemented: bool = False


class AgentTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str = Field(min_length=1, max_length=500)


def plan(goal: str) -> AgentPlan:
    """把目标拆成阶段。

    TODO(F02)：用模型做阶段划分，并为每个阶段生成"需要哪些输入"，
    再逐阶段调用 pipeline.run()，最后合并。
    现在返回固定阶段，只用来固定接口形状与前端交互顺序。
    """
    return AgentPlan(goal=goal, stages=list(DEFAULT_STAGES), implemented=False)
