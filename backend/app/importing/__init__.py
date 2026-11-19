"""导入已有思维导图（`.json` / Markdown / OPML / XMind / FreeMind / PlantUML）。

与 `app/export/` 是一对：导出把 `MindMapContent` 变成文本，导入把文本变回来。
"能导出什么就能导入什么"这条原则的落点就在这里——凡是 `export.service` 能产出的
形状，本包都必须读得回来，否则用户"导出改一改再导入"就会静默丢东西。

分层（按职责，不按格式）：

    detect.py     扩展名 + 头部嗅探 → 格式名（zip 的二次确认留在 xmind.py）
    model.py      中间表示 `RawNode` / `RawMap` + 解析期节点预算
    text.py       编码解码、XML 解析（含实体膨胀防线）、namespace 处理
    colors.py     视觉字段的**精确**逆向映射表（映射不到就丢，不猜）
    json_import.py  markdown.py  opml.py  xmind.py  freemind.py  puml.py
    normalize.py  五件边界处理的唯一实现（id / 限额 / label / 视觉 / 引用清空）
    service.py    判定 → 解析 → 归一化；只解析不落库
    router.py     POST /api/mindmaps/import
"""
from __future__ import annotations

from app.importing.detect import detect_format
from app.importing.errors import ImportFailure, ParseFailure, UnsupportedFormat
from app.importing.model import ImportWarning
from app.importing.service import ImportResult, parse_import

__all__ = [
    "ImportFailure",
    "ImportResult",
    "ImportWarning",
    "ParseFailure",
    "UnsupportedFormat",
    "detect_format",
    "parse_import",
]
