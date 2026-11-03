from fastapi import APIRouter, File, HTTPException, UploadFile, status

from app.contracts.source import (
    CreateTextSourceRequest,
    CreateTextSourceResponse,
    SourceKind,
)
from app.ingestion.service import (
    DOCUMENT_SUFFIXES,
    NotSupportedSource,
    parse_bytes,
    parse_text,
)

router = APIRouter(prefix="/ingestion", tags=["多模态输入"])

# 上传上限。Docling 要把整份文档读进内存做版面分析，定太小会白等，
# 定太大在 2C2G 机器上会把内存吃满；10MB 大约够 300 页纯文字 PDF。
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


@router.post("/text", response_model=CreateTextSourceResponse)
def create_text_source(body: CreateTextSourceRequest):
    """文本 / Markdown 直通。"""
    try:
        source = parse_text(body.text, body.kind)
    except NotSupportedSource as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return CreateTextSourceResponse(source=source)


@router.post("/files", response_model=CreateTextSourceResponse)
async def create_file_source(file: UploadFile = File(...)):
    """上传文档（PDF / DOCX / PPTX / XLSX / HTML 等），解析成文本来源。

    返回的 `source` 可直接传给 `POST /api/pipeline/generate` 生成导图。
    解析失败（含扫描件无文本层）返回 422 并说明原因，不返回空内容。
    """
    filename = file.filename or ""
    suffix = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    if suffix not in DOCUMENT_SUFFIXES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"暂不支持的文件类型：{suffix or '未知'}；"
                   f"目前支持 {', '.join(sorted(DOCUMENT_SUFFIXES))}")

    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"文件超过 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB 上限")
    if not raw:
        raise HTTPException(status_code=422, detail="文件内容为空")

    try:
        source = parse_bytes(SourceKind.document, filename, raw)
    except NotSupportedSource as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return CreateTextSourceResponse(source=source)


@router.get("/formats")
def list_supported_formats():
    """列出当前支持的文件类型，前端可据此限制文件选择框。"""
    return {"document": sorted(DOCUMENT_SUFFIXES),
            "maxUploadBytes": MAX_UPLOAD_BYTES}
