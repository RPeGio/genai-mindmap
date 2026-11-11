"""知识检索（F12 / F13 / F14）。

位置：agent 循环里由 `rag_query` 工具按需调用（`app/agent/tools.py::_rag()`），
用检索结果给模型补上下文，不进导图契约。
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

from app.contracts.source import Source


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


# ---------------------------------------------------------------- 本次来源内检索


class EvidenceHit(BaseModel):
    """检索到的一段原文。

    `start` / `end` 是 **Unicode 码点**偏移，与 `SourceRef` 同口径。用 UTF-16 下标
    会在中文和 emoji 上错位，前端拿它 slice 出来的就是另一段文字。
    """

    model_config = ConfigDict(extra="forbid")
    source_id: str
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    quote: str
    score: float


CHUNK_CHARS = 300


def _bigrams(text: str) -> set[str]:
    """字符二元组。

    中文没有空格可分词，按单字切又太碎（重合率虚高，几乎什么都能命中），
    二元组是这两者之间最省事的折中：不引入分词依赖，区分度也够用。
    """
    chars = list(text)
    if len(chars) < 2:
        return set(chars)
    return {chars[index] + chars[index + 1] for index in range(len(chars) - 1)}


def _chunks(text: str, size: int = CHUNK_CHARS):
    """按码点定长切块，产出 (start, end, 文本)。"""
    chars = list(text)
    for start in range(0, len(chars), size):
        end = min(start + size, len(chars))
        yield start, end, "".join(chars[start:end])


def search_sources(sources: list[Source], query: str, top_k: int = 5) -> list[EvidenceHit]:
    """在**本次请求带来的来源**里找相关片段。

    只查参数给的这些，不碰任何持久化索引：agent 一次运行能看到的证据范围必须
    与它实际拿到的来源严格一致，否则它引用的东西在界面上根本点不到。

    这是词面检索而不是语义检索，`score` 只表示字面相关程度。命中不证明结论
    正确，上层不能拿它当事实校验——这也是它与 `search()` 那个检索桩的区别：
    那个将来接向量库，这个永远只做"在给定文本里定位"。
    """
    question = _bigrams(query.strip())
    if not question:
        return []

    hits: list[EvidenceHit] = []
    for source in sources:
        # 不能先 strip 再切块：偏移会整体前移，调用方拿它去切 Source.text
        # 得到的不是 quote 那段文字。空判断用 strip，切块用原文。
        text = source.text or ""
        if not text.strip():
            continue
        for start, end, chunk in _chunks(text):
            overlap = question & _bigrams(chunk)
            if not overlap:
                continue
            hits.append(EvidenceHit(
                source_id=source.id,
                start=start,
                end=end,
                quote=chunk,
                score=round(len(overlap) / len(question), 4),
            ))

    # 同分时短的块优先：整块命中率相同的情况下，短块的信息密度更高
    hits.sort(key=lambda hit: (-hit.score, hit.end - hit.start))
    return hits[:top_k]
