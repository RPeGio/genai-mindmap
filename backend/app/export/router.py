from fastapi import APIRouter, Response

from app.contracts.mindmap import SaveRequest
from app.export.service import to_json, to_markdown, to_opml, to_puml

router = APIRouter(prefix="/export", tags=["导出"])


@router.post("/puml", response_class=Response)
def export_puml(body: SaveRequest):
    """导出 PlantUML 文本。

    前端拿到后可直接下载，或贴到语雀 / Typora / Confluence 等支持 puml 的工具。
    """
    text = to_puml(body.content)
    return Response(content=text, media_type="text/plain; charset=utf-8")


@router.post("/markdown", response_class=Response)
def export_markdown(body: SaveRequest):
    """导出 Markdown 大纲。"""
    text = to_markdown(body.content)
    return Response(content=text, media_type="text/markdown; charset=utf-8")


@router.post("/opml", response_class=Response)
def export_opml(body: SaveRequest):
    """导出 OPML，便于导入 XMind 等工具。"""
    text = to_opml(body.content)
    return Response(content=text, media_type="text/x-opml; charset=utf-8")


@router.post("/json", response_class=Response)
def export_json(body: SaveRequest):
    """导出 JSON（`MindMapContent` 的序列化）。

    这是唯一**无损**的格式，也是「导出 → 手改 → 导入」的保底：另外三种都只有
    一个文字槽位，往返一次 `note` / `detail` / 颜色 / 图型会退化（见
    `app/export/service.py::to_json` 的 docstring）。
    """
    text = to_json(body.content)
    return Response(content=text, media_type="application/json; charset=utf-8")
