from fastapi import APIRouter, HTTPException, Response

from app.contracts.mindmap import SaveRequest
from app.export.service import to_puml

router = APIRouter(prefix="/export", tags=["导出"])


@router.post("/puml", response_class=Response)
def export_puml(body: SaveRequest):
    """导出 PlantUML 文本。已实现。

    前端拿到后可直接下载，或贴到语雀 / Typora / Confluence 等支持 puml 的工具。
    """
    text = to_puml(body.content)
    return Response(content=text, media_type="text/plain; charset=utf-8")


@router.post("/markdown")
def export_markdown(body: SaveRequest):
    """导出 Markdown 大纲。待实现（纯映射，工作量小）。"""
    raise HTTPException(status_code=501, detail="Markdown 导出待实现")


@router.post("/opml")
def export_opml(body: SaveRequest):
    """导出 OPML，便于导入 XMind 等工具。待实现。"""
    raise HTTPException(status_code=501, detail="OPML 导出待实现")
