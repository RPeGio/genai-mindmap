from fastapi import APIRouter, File, HTTPException, UploadFile, status

from starlette.concurrency import run_in_threadpool

from app.contracts.source import (
    CreateTextSourceRequest,
    CreateTextSourceResponse,
    SourceKind,
)
from app.ingestion.service import (
    AUDIO_SUFFIXES,
    DOCUMENT_SUFFIXES,
    IMAGE_SUFFIXES,
    NotSupportedSource,
    parse_bytes,
    parse_text,
)

router = APIRouter(prefix="/ingestion", tags=["多模态输入"])

# 上传上限。PDF 用 pymupdf 只读文本层，开销很小；DOCX/PPTX 要整份读进内存，
# 定太大在 2C2G 机器上会把内存吃满。10MB 大约够 300 页纯文字 PDF。
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

# 音频单独给上限：60 秒 16k 单声道 WAV 约 1.83MB，20MB 约合 11 分钟。
# 更长的音频会被切成更多 60 秒片段依次识别，耗时线性上升，没有硬上限必要。
MAX_AUDIO_BYTES = 20 * 1024 * 1024

# 图片按 base64 传给模型会膨胀约 1/3，10MB 原图约 13.6MB 请求体，
# 离模型 32MiB 的单图上限还有余量。
MAX_IMAGE_BYTES = 10 * 1024 * 1024


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
    """上传文档或音频，解析成文本来源。

    返回的 `source` 可直接传给 `POST /api/pipeline/generate` 生成导图。
    解析失败（含扫描件无文本层、音频无人声）返回 422 并说明原因，
    不返回空内容。
    """
    filename = file.filename or ""
    suffix = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""

    if suffix in AUDIO_SUFFIXES:
        kind = SourceKind.audio
        limit = MAX_AUDIO_BYTES
    elif suffix in IMAGE_SUFFIXES:
        kind = SourceKind.image
        limit = MAX_IMAGE_BYTES
    elif suffix in DOCUMENT_SUFFIXES:
        kind = SourceKind.document
        limit = MAX_UPLOAD_BYTES
    else:
        hint = ""
        if suffix in {".doc", ".ppt", ".xls"}:
            hint = (f"；这是旧版二进制格式，解析它需要经 LibreOffice 转换，"
                    f"内存与耗时都是新格式的数倍，请另存为 {suffix}x 后再上传")
        elif suffix in {".mp3", ".m4a", ".aac", ".amr", ".ogg"}:
            hint = "；语音目前只接受 WAV，请在浏览器里录制或先转成 16kHz 单声道 WAV"
        elif suffix == ".svg":
            hint = "；SVG 是矢量描述，模型拿到的是 XML 而不是画面，请导出成 PNG 后再上传"
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"暂不支持的文件类型：{suffix or '未知'}{hint}；"
                   f"目前支持 "
                   f"{', '.join(sorted(DOCUMENT_SUFFIXES | AUDIO_SUFFIXES | IMAGE_SUFFIXES))}")

    raw = await file.read(limit + 1)
    if len(raw) > limit:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"文件超过 {limit // (1024 * 1024)}MB 上限")
    if not raw:
        raise HTTPException(status_code=422, detail="文件内容为空")

    try:
        # 解析是同步阻塞的，直接在 async 函数里调用会把事件循环占住，
        # 同进程的其他请求全部排队。甩给线程池，循环继续接客。
        # 语音转写内部会开自己的事件循环跑 WebSocket，跑在线程池里正合适。
        source = await run_in_threadpool(parse_bytes, kind, filename, raw)
    except NotSupportedSource as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return CreateTextSourceResponse(source=source)


@router.get("/formats")
def list_supported_formats():
    """列出当前支持的文件类型与上传上限，前端可据此限制文件选择框。"""
    return {"document": sorted(DOCUMENT_SUFFIXES),
            "audio": sorted(AUDIO_SUFFIXES),
            "image": sorted(IMAGE_SUFFIXES),
            "maxUploadBytes": MAX_UPLOAD_BYTES,
            "maxAudioBytes": MAX_AUDIO_BYTES,
            "maxImageBytes": MAX_IMAGE_BYTES}
