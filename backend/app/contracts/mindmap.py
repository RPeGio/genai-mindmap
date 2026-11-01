from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.contracts.source import SourceUsage


class Node(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=200)
    children: list[Node] = Field(default_factory=list, max_length=50)


class MindMapContent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    root: Node
    source_usage: list[SourceUsage] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def validate_tree(self) -> MindMapContent:
        seen: set[str] = set()
        stack = [(self.root, 1)]
        while stack:
            node, depth = stack.pop()
            if node.id in seen:
                raise ValueError("节点 ID 必须唯一")
            seen.add(node.id)
            if len(seen) > 300 or depth > 8:
                raise ValueError("导图最多 300 个节点，深度不超过 8 层")
            stack.extend((child, depth + 1) for child in node.children)
        return self


class GenerateRequest(BaseModel):
    text: str = Field(min_length=1, max_length=10000)
    @model_validator(mode="after")
    def not_blank(self) -> GenerateRequest:
        self.text = self.text.strip()
        if not self.text:
            raise ValueError("请输入非空文本")
        return self


class GenerateResponse(BaseModel):
    mode: Literal["demo", "model"]
    content: MindMapContent


class SaveRequest(BaseModel):
    content: MindMapContent


class UpdateRequest(SaveRequest):
    version: int = Field(ge=1)


class MapRecord(BaseModel):
    id: str
    version: int
    content: MindMapContent
    updated_at: str
