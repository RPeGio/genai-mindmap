"""导入总入口：判定格式 → 解析 → 归一化。

顺序不可换，理由都在两处：`detect` 先把"这是不是我们能读的东西"回答掉（否则一个
`.docx` 会进 markdown 解析器，报出来的错是"没找到列表项"）；`normalize` 必须在
所有解析器之后（限额与 id 重生成只有一份实现，见 `normalize` 的 docstring）。

**只解析，不落库。** 落库仍走用户点保存：导入失败时不应该在库里留下半张图，而
"导入成功但用户不想要"也应该能直接丢弃——它在这一层表现为 `dirty=true` 的未保存
状态，与生成出来的图走同一条保存路径。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from pydantic import ValidationError

from app.contracts.mindmap import MindMapContent
from app.importing import freemind, json_import, markdown, opml, puml, xmind
from app.importing.detect import SNIFF_BYTES, detect_format
from app.importing.errors import ImportFailure, ParseFailure, UnsupportedFormat
from app.importing.model import ImportWarning
from app.importing.normalize import normalize

logger = logging.getLogger(__name__)

# 格式名 → 解析函数。新增一种格式只改这一张表：`detect` 给名，这里给实现。
PARSERS = {
    "json": json_import.parse,
    "markdown": markdown.parse,
    "opml": opml.parse,
    "xmind": xmind.parse,
    "freemind": freemind.parse,
    "puml": puml.parse,
}

# 格式名 → 界面上说的名字。错误信息里用中文，`source_format` 里用键。
FORMAT_LABELS = {
    "json": "JSON（本仓库导出）",
    "markdown": "Markdown 大纲",
    "opml": "OPML",
    "xmind": "XMind",
    "freemind": "FreeMind",
    "puml": "PlantUML",
}

# upload 上限。思维导图的文本/XML/zip 都远小于它；定 10MB 是为了挡住"误传了一个
# 视频"这类情况，而不是限制真实文件。
MAX_IMPORT_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class ImportResult:
    content: MindMapContent
    warnings: list[ImportWarning]
    source_format: str


def parse_import(filename: str, raw: bytes) -> ImportResult:
    """`(文件名, 原始字节)` → 导图 + 告警。失败抛 `ImportFailure` 的两个子类。"""
    if not raw:
        raise ParseFailure("文件内容为空")

    source_format = detect_format(filename, raw[:SNIFF_BYTES])
    if source_format is None:
        supported = "、".join(FORMAT_LABELS.values())
        raise UnsupportedFormat(
            f"认不出这个文件的格式（文件名：{filename or '未命名'}）。"
            f"支持 {supported}；扩展名不对时可以按内容识别，但至少要有可辨认的头部")

    parser = PARSERS[source_format]
    try:
        parsed = parser(raw)
    except ImportFailure:
        raise
    except RecursionError as exc:
        # 几千层嵌套的 XML/JSON 会把递归打穿。转成"文件的问题"，而不是 500。
        raise ParseFailure("文件嵌套太深，解析时栈溢出；请检查它是否被工具反复嵌套过") from exc
    except ValidationError as exc:
        # 走到这里说明 normalize 产出的东西不合契约——是我们的 bug，不是文件的。
        # 记日志并回 400：用户至少拿到一句可读的话，而日志里有现场。
        logger.exception("导入归一化产出不合契约")
        raise ParseFailure(f"导入结果没能通过契约校验：{exc.error_count()} 处不合法") from exc

    try:
        content, warnings = normalize(parsed, source_format=source_format)
    except ValidationError as exc:
        logger.exception("导入归一化产出不合契约")
        raise ParseFailure(f"导入结果没能通过契约校验：{exc.error_count()} 处不合法") from exc

    return ImportResult(content=content, warnings=warnings, source_format=source_format)
