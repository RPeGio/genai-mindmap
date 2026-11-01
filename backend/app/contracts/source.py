"""统一来源模型（F05–F08）。

无论输入是文本、文档、图片还是语音，解析后都收敛成 Source，
生成流水线只认识 Source，不关心原始模态。
"""
from __future__ import annotations

from enum import Enum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class SourceKind(str, Enum):
    text = "text"
    markdown = "markdown"
    document = "document"   # PDF / DOCX / PPTX
    image = "image"         # 手绘草图、截图
    audio = "audio"         # 录音、视频音轨


class SourceSpan(BaseModel):
    """来源定位，用于节点溯源。"""
    model_config = ConfigDict(extra="forbid")
    page: int | None = None
    start: int | None = None
    end: int | None = None


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=100)
    kind: SourceKind
    filename: str | None = None
    text: str | None = None          # 抽取后的纯文本；未解析成功时为 None
    span: SourceSpan | None = None
    status: Literal["pending", "parsed", "failed"] = "pending"
    error: str | None = None


class SourceUsage(BaseModel):
    """整张导图使用过的来源摘要；不代表具体节点已建立证据链接。"""
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(min_length=1, max_length=100)
    kind: SourceKind
    filename: str | None = None
    char_count: int = Field(ge=1)


class CreateTextSourceRequest(BaseModel):
    """最小可用入口：直接贴文本。"""
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=10000)
    kind: Literal[SourceKind.text, SourceKind.markdown] = SourceKind.text


class CreateTextSourceResponse(BaseModel):
    source: Source
