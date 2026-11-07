from fastapi import APIRouter

from app.knowledge.service import SearchRequest, SearchResponse, search

router = APIRouter(prefix="/knowledge", tags=["知识增强"])


@router.post("/search", response_model=SearchResponse)
def search_knowledge(body: SearchRequest):
    """知识库检索（F12/F13）。

    当前实现返回空结果且 `implemented=false`，前端应据此禁用相关入口，
    不要把它当成"已经接好了知识库"。
    """
    hits = search(body.query, body.top_k)
    # 是否实现与是否命中是两件事；接入真实检索时须显式改为 True。
    return SearchResponse(hits=hits, implemented=False)
