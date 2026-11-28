"""文本折行：把控制字符折成空格。

**为什么单独一个模块（而不是放在 `knowledge/library.py` 里）**：调用方之一是
`scripts/migrate_sqlite_to_pg.py`，那个脚本的 `--dry-run` 契约是"不连目标库、不加载
应用依赖"——它的临时运行环境里只有 `psycopg`，连 `pydantic` 都没有。折字符这件事
只依赖标准库的 `unicodedata`，单独放一层，脚本就能只 import 它而不拖进整条
`app.contracts` → `pydantic` 的依赖链（实测撞过：`ModuleNotFoundError: No module
named 'pydantic'`）。

本模块**不许 import 本仓任何其它模块，也不许 import 第三方库**：它是那条"零依赖"
约束本身。
"""

from __future__ import annotations

import unicodedata

# 要保留的控制字符：制表、换行、回车、换页。正文靠它们分段，折成空格会把整篇压成
# 一行（切块算法按段落切）。
KEPT_CONTROLS = frozenset("\t\n\r\f")


def fold_control_chars(text: str) -> str:
    """把控制字符（Unicode `Cc` 类）折成空格，但**保留换行与制表**。

    **为什么必须折**：PostgreSQL 的 `text` 不允许 NUL（`0x00`），一条含 NUL 的正文
    会让整批 `INSERT` 抛
    `psycopg.DataError: PostgreSQL text fields cannot contain NUL (0x00) bytes`；
    SQLite 的 TEXT 没这条限制，所以这类正文能一路存进老库、直到迁 PG 时才炸。其余
    控制字符虽然 PG 收得下，但落进导出的文档会变成看不见的方块或 ANSI 控制序列。

    **为什么单留 `\\t\\n\\r` 与分页符**：知识库正文是带结构的材料，切块算法（
    `knowledge.service._chunk_tree`）按段落与标题切，把这些折成空格会把整篇压成一
    行——NUL 的问题修好了，切块与检索全坏了。要折的只是"不该出现的那些"。

    **折成空格而不是删掉**：`abc\\x00def` 是两个词，不是 `abcdef`。口径与
    `app.importing.normalize.one_line` 同源（那边更激进，连换行一起折——它服务的是
    单行字段，不是正文），跨层 import 会把解析层拖进检索层，所以这里独立实现。
    """
    if not text:
        return text
    return "".join(
        " " if unicodedata.category(ch) == "Cc" and ch not in KEPT_CONTROLS else ch
        for ch in text
    )
