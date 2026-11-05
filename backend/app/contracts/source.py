"""统一来源模型（F05–F08）。

无论输入是文本、文档、图片还是语音，解析后都收敛成 Source，
生成流水线只认识 Source，不关心原始模态。
"""
from __future__ import annotations

from enum import Enum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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
    # 实际得到内容的页数。与 span.page（整份文档共几页）不是一回事：
    # 51 页的 PDF 只处理前 50 页时 span.page = 51、parsed_pages = 50；
    # 中间有页识别失败时它还会更小。只有分页格式（PDF）才有意义，其余留 None。
    parsed_pages: int | None = None
    # 是否因 MAX_PAGES 被截断：后半截根本没处理过。
    # 必须显式告诉调用方——否则用户拿到的是一份缺了尾巴的正文，
    # 界面却显示"解析成功"，导图会静默漏掉后面的章节。
    truncated: bool = False
    # 处理过的页里，彻底没拿到内容的页码（1 起）。
    # 与 truncated 语义不同：那个是"没处理到"，这个是"处理了但中间有洞"。
    # 两者对用户的含义不一样，合并成一个布尔值会给出自相矛盾的提示
    # （"共 3 页，仅处理前 3 页"这种）。
    failed_pages: list[int] = Field(default_factory=list)
    # 语音来源的音频时长（秒）。文档用 parsed_pages 表示"处理了多少"，
    # 语音用时长更贴切：转写结果没有页的概念，用户关心的是"这段录音多长"。
    # 其余模态留 None。
    duration_seconds: float | None = None


class SourceUsage(BaseModel):
    """整张导图使用过的来源摘要；不代表具体节点已建立证据链接。"""
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(min_length=1, max_length=100)
    kind: SourceKind
    filename: str | None = None
    char_count: int = Field(ge=1)


class SourceRef(BaseModel):
    """节点引用。字符区间以原始 Source.text 为准，使用从零开始的 [start, end)。

    第一层只校验字段；来源关联和原文匹配分别由导图契约与服务层校验。
    """
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(min_length=1, max_length=100)
    page: int | None = Field(default=None, ge=1, strict=True)
    start: int | None = Field(default=None, ge=0, strict=True)
    end: int | None = Field(default=None, ge=0, strict=True)
    quote: str | None = Field(default=None, max_length=300)

    @field_validator("source_id")
    @classmethod
    def source_id_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("引用的来源 ID 不能为空白")
        return value

    @field_validator("quote")
    @classmethod
    def blank_quote_as_none(cls, value: str | None) -> str | None:
        return value if value and value.strip() else None

    @model_validator(mode="after")
    def validate_range(self) -> SourceRef:
        if (self.start is None) != (self.end is None):
            raise ValueError("引用的 start 和 end 必须同时提供")
        if self.start is not None and self.end is not None and self.end <= self.start:
            raise ValueError("引用的 end 必须大于 start")
        return self


class CreateTextSourceRequest(BaseModel):
    """最小可用入口：直接贴文本。"""
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=10000)
    kind: Literal[SourceKind.text, SourceKind.markdown] = SourceKind.text


class CreateTextSourceResponse(BaseModel):
    source: Source
