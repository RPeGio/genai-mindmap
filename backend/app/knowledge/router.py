"""知识增强路由。

`/search` 是早期留下的检索桩（`implemented=false`）；`/library` 两个 GET 是
知识库的**只读入口**：列出库里有哪些材料、每份被几张已保存导图引用、点开看
正文预览。

两条职责不同，别把它们的功能互相当前提：`/library` 不受
`MINDMAP_LIBRARY_SEARCH` 影响——那个开关管的是"生成时要不要从库里捞材料参与"，
关掉它只应该让生成变回不查库，不该让用户看不见自己库里的东西。
"""
from fastapi import APIRouter

from app.core.errors import AppError
from app.knowledge import shelf
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


@router.get("/library", response_model=shelf.LibraryList)
def list_library(limit: int = shelf.DEFAULT_LIMIT):
    """库里的材料列表，最近使用的在前。`total` 是库里的总数，不是返回条数。

    只读：不改任何状态，也不刷 `last_used_at`（"看了一下列表"不等于"用到了
    这份材料"——那个字段是 `prune` 保留与否的依据）。
    """
    return shelf.list_entries(limit=limit)


@router.get("/library/{source_id}", response_model=shelf.LibrarySource)
def read_library_source(source_id: str):
    """单份材料的正文预览（前 4000 字，`truncated` 说明是否截断）。"""
    # 这里用 AppError 而不是 HTTPException：后者会被全局处理器兜成"请求处理失败
    # （HTTP 404）"，前端读顶层 message，用户看到的就是那句没信息量的话。
    #
    # 404 与 503 必须分开：前者是"这份材料没了，重试也没用"，后者是"库暂时读不
    # 出来，稍后再看"。把库故障说成 404，用户会以为材料被删了。
    try:
        return shelf.read_entry(source_id)
    except shelf.SourceNotFound as error:
        raise AppError("not_found", "这份来源不在知识库里", status=404) from error
    except shelf.LibraryUnavailable as error:
        raise AppError(
            "library_unavailable", "知识库暂时读不出来，请稍后重试", status=503,
        ) from error
