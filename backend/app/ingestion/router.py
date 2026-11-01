from fastapi import APIRouter, HTTPException, status

from app.contracts.source import (
    CreateTextSourceRequest,
    CreateTextSourceResponse,
    Source,
    SourceKind,
)
from app.ingestion.service import NotSupportedSource, parse_text

router = APIRouter(prefix="/ingestion", tags=["多模态输入"])


@router.post("/text", response_model=CreateTextSourceResponse)
def create_text_source(body: CreateTextSourceRequest):
    """文本 / Markdown 直通。已实现。"""
    try:
        source = parse_text(body.text, body.kind)
    except NotSupportedSource as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return CreateTextSourceResponse(source=source)


@router.post("/files", response_model=CreateTextSourceResponse)
def create_file_source(kind: SourceKind, filename: str | None = None):
    """文件入口（F05–F07）。

    尚不接收文件内容，所有类型都返回 501。文本可使用 /text。
    真正实现时改成 `UploadFile` 接收 multipart，并加大小与类型白名单。
    """
    _ = (kind, filename)
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED,
                        detail="文件上传尚未实现；文本或 Markdown 请使用 /api/ingestion/text")
