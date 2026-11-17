"""知识库的只读视图：列表、单份正文、被几张导图引用。

**为什么单独一个模块，而不是塞进 `library.py`**：`library.py` 的每个函数都是
"库自己的事"（写入、淘汰、复用判定），读写都不碰导图；而"被几张导图引用"必须
读 `mindmaps` 表。两件事的依赖方向相反，混在一起会让 `library` 反向依赖导图表，
而它现在是导图仓储的下游（`repository.connect()` 会 import 它来建表）。

**这里不控制检索开关**。`MINDMAP_LIBRARY_SEARCH` 决定的是"生成时要不要从库里
捞材料参与"，不是"库能不能看"。入口是纯展示的，把展示也挂到那个开关上，关掉
检索时列表会莫名变空。

**统计口径是"被几张已保存导图引用"**：一张图的 `source_usage` 与它所有节点的
`source_refs` 都算。前者是"这次生成用了这份材料"，后者是"某个节点指着这段原文"
——只算后者会漏掉"用了但没逐字引用"的材料，而那句"被 N 张导图引用"要回答的
正是"这份材料我到底用没用过"。
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.mindmap import MindMapContent
from app.contracts.source import SourceKind
from app.knowledge import library as knowledge_library
from app.mindmaps import repository

logger = logging.getLogger(__name__)

# 列表默认返回多少条。与 `library.list_sources` 的缺省一致：50 条 × 摘要字段
# 在几十 KB 量级，2C2G 上翻一页的延迟感觉不到。
DEFAULT_LIMIT = 50

# 单份正文预览的截断长度（按码点）。列表页点开只是"确认一下是不是这份"，
# 不提供全文阅读——真要看全文，用户手里有原文件。
PREVIEW_CHARS = 4000


class LibraryEntry(BaseModel):
    """列表里的一份材料。**不含正文**，与 `library.list_sources` 同口径。"""

    model_config = ConfigDict(extra="forbid")
    source_id: str
    filename: str | None = None
    kind: SourceKind
    chars: int = Field(ge=0)
    chunks: int = Field(ge=0)
    created_at: str
    last_used_at: str
    # 被几张**已保存导图**引用（口径见 `_cited_counts`）。0 表示生成时没用上。
    cited_by: int = Field(ge=0)


class LibraryList(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[LibraryEntry] = Field(default_factory=list)
    # 库里的总数，不是 len(items)：界面要能写"共 137 份，显示最近 50 份"。
    total: int = Field(ge=0)


class LibrarySource(BaseModel):
    """单份材料的正文预览。`truncated` 为真时必须让用户看到"只是前 4000 字"。"""

    model_config = ConfigDict(extra="forbid")
    source_id: str
    filename: str | None = None
    kind: SourceKind
    text: str
    truncated: bool = False
    total_chars: int = Field(ge=0)



class SourceNotFound(LookupError):
    """库里没有这个 id。路由层翻成 404，与"导图不存在"同一套措辞。"""


class LibraryUnavailable(RuntimeError):
    """库本身读不出来（表被删、文件被锁），**不是**"没有这份材料"。

    两者必须分开：404 告诉用户"这份材料没了，重试也没用"，503 才是"稍后再看"。
    混成 404 会让人以为材料被删了，而这恰恰是排查时最误导的一条反馈。
    """


def _cited_counts() -> dict[str, int]:
    """每个持久 source_id 被几张**已保存导图**引用。

    一次扫全表而不是给每份来源各查一次：来源数最多几百，导图数也是几百，
    逐份查是 N×M 次查询，扫一次是 M 次 JSON 解析。

    这条路是**旁路统计**，不能反过来把入口打挂：单张读不出来的导图跳过并记
    warning（由 `repository.list_maps(tolerant=True)` 逐行兜住），导图表整个读不
    出来时返回空计数、列表照常展示（`cited_by` 退化成 0）。真正"坏在哪儿"由列表
    页/单图接口去暴露，不是这里。
    """
    counts: dict[str, int] = {}
    try:
        maps = repository.list_maps(tolerant=True)
    except (sqlite3.Error, OSError) as exc:
        logger.warning("读不到导图表（%s），引用计数按 0 处理：%s",
                       type(exc).__name__, str(exc)[:160])
        return counts

    for record in maps:
        content: MindMapContent = record.content
        used = {usage.source_id for usage in content.source_usage}
        stack = [content.root]
        while stack:
            node = stack.pop()
            for ref in node.source_refs or ():
                used.add(ref.source_id)
            stack.extend(node.children)
        # 同一张图里同一份来源出现多次只算一张：口径是"被几张导图引用"，
        # 不是"被引用几次"。
        for source_id in used:
            counts[source_id] = counts.get(source_id, 0) + 1
    return counts


def _summary(row: dict[str, Any], cited_by: dict[str, int]) -> dict[str, Any]:
    """一行库记录 → 列表项。字段名与 `library.list_sources` 保持一致，只加两项。"""
    source_id = str(row["id"])
    return {
        "source_id": source_id,
        "filename": row["filename"],
        "kind": row["kind"],
        "chars": int(row["chars"]),
        "chunks": int(row["chunks"]),
        "created_at": row["created_at"],
        "last_used_at": row["last_used_at"],
        "cited_by": cited_by.get(source_id, 0),
    }


def list_entries(*, limit: int = DEFAULT_LIMIT) -> LibraryList:
    """库里的材料列表，最近使用的在前。

    `total` 是**库里的总数**，不是本次返回的条数——列表页要能写"共 137 份，
    显示最近 50 份"，只给 `len(items)` 的话用户会以为库里只有 50 份。

    库读不出来（表被删、文件被锁）时返回空列表 + `total=0` 并记 warning，
    不抛：入口打不开比入口空着更糟，而这条路上"库坏了"的正确表现就是一条空列表。
    导图表读不出来、或其中个别行坏掉时，只影响 `cited_by`（按 0 计），不影响列表。
    """
    size = max(int(limit), 1)
    try:
        rows = knowledge_library.list_sources(limit=size)
        total = knowledge_library.count()
        # 引用计数是旁路展示：它自己会兜住坏行/坏表，这里顺带放在 try 内——
        # 即便将来它抛了 sqlite 错误，也不该连带把列表变成 500。
        # 空库不必白扫一遍导图表。
        cited_by = _cited_counts() if rows else {}
    except (sqlite3.Error, OSError) as exc:
        logger.warning("读不到知识库（%s），列表按空处理：%s",
                       type(exc).__name__, str(exc)[:160])
        return LibraryList(items=[], total=0)
    return LibraryList(
        items=[LibraryEntry.model_validate(_summary(row, cited_by)) for row in rows],
        total=total,
    )


def read_entry(source_id: str) -> LibrarySource:
    """单份来源的正文预览。不存在抛 `SourceNotFound`，库读不出来抛 `LibraryUnavailable`。

    `truncated` / `total_chars` 两个字段是给界面的：只截了前 4000 字就必须说清，
    否则用户会拿预览当全文，回头在导图里引用一句"材料里其实没有"的话。
    """
    try:
        source = knowledge_library.load(source_id)
    except (sqlite3.Error, OSError) as exc:
        logger.warning("读不到知识库（%s），预览按不可用处理：%s",
                       type(exc).__name__, str(exc)[:160])
        raise LibraryUnavailable(source_id) from exc
    if source is None:
        raise SourceNotFound(source_id)

    text = source.text or ""
    # 不带 created_at / last_used_at：界面是从列表点进来的，那两个字段已经在
    # 手上；这里再查一次库只为它们，等于每次点开都多一次 SELECT。
    return LibrarySource(
        source_id=source.id,
        filename=source.filename,
        kind=source.kind,
        text=text[:PREVIEW_CHARS],
        truncated=len(text) > PREVIEW_CHARS,
        total_chars=len(text),
    )


__all__ = [
    "DEFAULT_LIMIT",
    "PREVIEW_CHARS",
    "LibraryEntry",
    "LibraryList",
    "LibrarySource",
    "LibraryUnavailable",
    "SourceNotFound",
    "list_entries",
    "read_entry",
]
