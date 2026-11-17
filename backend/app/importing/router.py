"""`POST /api/mindmaps/import`（multipart/form-data, `file`）。

响应形状（题目给定的契约）：

    200 { content: MindMapContent, warnings: [{code, message}], source_format: "xmind" }
    400 { code: "unsupported_format" | "parse_failed", message }

**为什么挂在 `/mindmaps/import` 而不是 `/import`**：它导入的产物就是一张导图，
与 `/mindmaps` 那组接口是同一件事的两端（一个写库，一个只解析）。放在同一个前缀
下，前端的"保存/打开/导入"三条路读起来是一组。

**为什么只解析不落库**：落库仍走用户点保存。导入失败不污染已有数据；导入成功但
用户改主意也能直接关掉——它在界面上的形态就是"有未保存修改的新导图"。

错误走 `core.errors.AppError`：整个仓库的错误出口统一成
`{code, message, detail, request_id}`，这里只需要给 `code` 与中文 `message`
（`detail` 留 None）。前端 `toApiError` 读 `message` 显示，读 `code` 分流。
"""
from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

from app.contracts.mindmap import MindMapContent
from app.core.errors import AppError
from app.importing.errors import ImportFailure
from app.importing.service import MAX_IMPORT_BYTES, parse_import

router = APIRouter(prefix="/mindmaps/import", tags=["导入"])


class ImportWarningPayload(BaseModel):
    """一条导入告警。只有 `code` 与 `message`（定位信息写进 message 正文）。"""

    code: str
    message: str


class ImportResponse(BaseModel):
    content: MindMapContent
    warnings: list[ImportWarningPayload]
    source_format: str


@router.post("", response_model=ImportResponse)
async def import_mindmap(file: UploadFile = File(...)) -> ImportResponse:
    filename = file.filename or ""
    raw = await file.read(MAX_IMPORT_BYTES + 1)
    if len(raw) > MAX_IMPORT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"文件超过 {MAX_IMPORT_BYTES // (1024 * 1024)}MB 上限")
    try:
        result = parse_import(filename, raw)
    except ImportFailure as exc:
        # 两个码都由异常自己带着（`UnsupportedFormat.code` / `ParseFailure.code`），
        # 这里不按类型再判一次：加一种失败类型时只改那个类。
        raise AppError(code=exc.code, message=str(exc), status=400) from exc
    return ImportResponse(
        content=result.content,
        warnings=[ImportWarningPayload(**item.as_dict()) for item in result.warnings],
        source_format=result.source_format,
    )
