"""诊断与建议模型（F15 / F16）。

诊断（Diagnostic）是流水线内部产物，表示"这棵树有问题"；
建议（Suggestion）是面向用户的候选修改，必须由用户确认后才应用。
"""
from __future__ import annotations

from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class DiagnosticLevel(str, Enum):
    info = "info"
    warn = "warn"
    error = "error"


class Diagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid")
    level: DiagnosticLevel = DiagnosticLevel.info
    code: str                      # 如 UNSOURCED_NODE / DUPLICATE_BRANCH / DEPTH_JUMP
    node_id: str | None = None
    message: str
    evidence: str | None = None    # 原文片段或相似节点 id


class SuggestionKind(str, Enum):
    dedupe = "dedupe"          # 合并重复分支
    merge = "merge"            # 层级归并
    link = "link"              # 补跨节点关系
    fill = "fill"              # 补缺失环节
    drop = "drop"              # 剔除无依据节点


class Suggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=lambda: str(uuid4()))
    kind: SuggestionKind
    node_id: str | None = None
    message: str
    evidence: str | None = None
    # 规则建议没有经过概率校准，None 表示尚未评估，而不是伪造可信度。
    confidence: float | None = Field(default=None, ge=0, le=1)


class SuggestResponse(BaseModel):
    suggestions: list[Suggestion] = Field(default_factory=list)
    diagnostics: list[Diagnostic] = Field(default_factory=list)
