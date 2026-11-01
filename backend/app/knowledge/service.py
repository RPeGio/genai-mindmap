"""知识检索（F12 / F13 / F14）。

位置：pipeline 的 understand 之后、outline 之前，可选用检索结果做上下文增强。
状态：**未实现**。当前返回空结果，接口存在只为固定契约与前端联调顺序。

实现路线（按成本从低到高）：
  1. PostgreSQL 全文检索（tsvector）—— 不需要新组件
  2. pgvector + 嵌入模型 —— 需要 embedding 调用与向量列
  3. Milvus / Pinecone —— 仅在明确要求时再考虑

注意：向量检索解决的是"找到相关片段"，不是"判断内容正确"。
F13 的验收不能被等同于事实验证。
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class KnowledgeHit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str | None = None
    snippet: str = ""
    score: float = 0.0


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=5, ge=1, le=20)


class SearchResponse(BaseModel):
    hits: list[KnowledgeHit] = Field(default_factory=list)
    implemented: bool = False


def search(query: str, top_k: int = 5) -> list[KnowledgeHit]:
    """检索知识库。

    TODO(F12/F13)：
      - 建索引：文档 → 分块 → 嵌入 → 存向量列
      - 检索：query → 嵌入 → 相似度排序 → 返回片段 + 来源定位
    现在返回空列表，调用方能正常跑完流程，但不会得到任何增强。
    """
    _ = (query, top_k)
    return []
