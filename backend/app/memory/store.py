"""记忆条目：一张表、一套与 `chunk_vectors` 逐字节相同的向量存法。

**这张表和 `user_prefs` 的分工**（一混就会重演"我明明写了它却没用"）：

  - `user_prefs`：**用户显式写的**。A/B 类是参数（图型、拆解方式、联网开关），
    必须精确，正确位置是请求层；C 类 `note.*` 是自由文本，条数是个位数，全塞进
    prompt 就够——它的 docstring 里"不做检索"那段论证在 8 条这个规模上是对的。
  - `memory_items`：**系统从历史会话里推断的**。写入源是总结管线（`summarize`），
    条数会从个位数长到几百上千。到了那个规模，全塞进 prompt 不可能，检索才成立。

所以这里**不接管** `note.*`，也不读它。手写的是显式意图、自动提炼的是推断，
两者在提示词里各占一段（显式的在前），这是刻意的：用户写下"标题别超过 8 个字"
之后，它必须永远生效，不能被一次"检索到大概相关的三条"挤掉。

**为什么复用 `text-embedding-v3` 与 `vector_cache` 的存取**：

  - 向量维度（1024 float32）与打包方式与 `chunk_vectors` 逐字节相同——同一份
    `pack` / `unpack`，不复制第二份。两处各写一份的结果已经出现过一次：换了
    字节序、一处的旧向量全读不出来，而报错只有一个沉默的 `None`。
  - 条目自己的向量**不进** `chunk_vectors`：那张表是纯缓存（丢了只会变慢），
    而记忆的向量是这条记忆的一部分（丢了这条记忆就再也检索不到）。混进去的后果
    是某天清缓存时把记忆的语义路一起清掉。
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, get_args
from uuid import uuid4

from app.knowledge import vector_cache
from app.knowledge.embedding import (
    EmbeddingError,
    EmbeddingNotConfigured,
    configured as embedding_configured,
    embed_texts,
    model_name as embedding_model,
)
from app.mindmaps import repository

logger = logging.getLogger(__name__)

ITEM_TABLE = "memory_items"
SESSION_TABLE = "memory_sessions"

# ---------------------------------------------------------------- 值域与上限

#: 一条记忆是什么。三档的区别只有一个：它约束的是**表达方式**、**事实**，还是
#: **做事习惯**。分档不是为了给检索用（检索只看向量），是为了让人看列表时能扫，
#: 以及给将来的"按档设阈值"留一个不需要改表结构的入口。
#:
#:   - `preference` 表达方式上的取舍：标题去掉人名、节点不超过 8 个字
#:   - `fact`       关于这个人的稳定事实：他做电类课程、习惯中文术语
#:   - `pattern`    做事习惯：先要鱼骨图再补细节、喜欢先给结论
MemoryKind = Literal["preference", "fact", "pattern"]
KINDS: tuple[str, ...] = get_args(MemoryKind)

#: 这条记忆是怎么来的。`summary` = 会话结束时的模型提炼；`user_explicit` = 用户
#: 手工加的。与 `user_prefs.source` 同一套措辞（`user_explicit`），但**不共用
#: 那张表**：这里是"可检索的推断/声明"，那里是"永远全塞的显式参数"。
MemorySource = Literal["summary", "user_explicit"]
SOURCES: tuple[str, ...] = get_args(MemorySource)

SOURCE_SUMMARY = "summary"
SOURCE_USER_EXPLICIT = "user_explicit"

#: 一条记忆的字数上限。与总结 prompt 里那条硬约束一致（"一条一句，不超过 100 字"），
#: 入库时再兜一次：prompt 是约束，不是保证。
#:
#: 超了**截断**而不是拒绝。一句被截断的偏好仍然多半是有用的（"标题去掉人名与并列
#: 项，节点不超过 8 个字"截到 100 字还是这句话），而拒绝的代价是这条事实永久丢失
#: ——同一个会话只会被总结一次，丢了不会再有第二次机会。
MAX_ITEM_CHARS = 100

#: 时间格式与 `mindmaps` / `runs` / `user_prefs` / `edit_events` / `library_*` 完全
#: 一致。五处格式不同就会在"谁更晚"上错一天——这是仓库里写过的教训（`runs.py:_NOW`）。
_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"

_CREATE_ITEMS = f"""CREATE TABLE IF NOT EXISTS {ITEM_TABLE} (
    id            TEXT PRIMARY KEY,
    text          TEXT NOT NULL,
    kind          TEXT NOT NULL,
    source        TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    formed_at     TEXT,
    last_used_at  TEXT,
    source_run_id TEXT,
    session_id    TEXT,
    text_hash     TEXT NOT NULL,
    model         TEXT,
    dim           INTEGER,
    vector        BLOB
)"""

_CREATE_SESSIONS = f"""CREATE TABLE IF NOT EXISTS {SESSION_TABLE} (
    session_id    TEXT PRIMARY KEY,
    status        TEXT NOT NULL,
    item_count    INTEGER NOT NULL DEFAULT 0,
    detail        TEXT,
    summarized_at TEXT NOT NULL
)"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """建两张表。由 `repository.connect()` 在同一个连接上调用，幂等。

    **`text_hash` 上是一条 UNIQUE 索引**，不是普通索引：去重是这个设计的硬要求
    （同一条偏好在两次会话里被总结出来，不该变成两条），而"先查再插"在并发下会
    漏——总结管线与手工添加可能同时写。UNIQUE 让数据库来裁决。

    之所以不是 `(text_hash, kind)` 联合唯一：同一条文本只该存在一次，与它的档位
    无关。允许两档并存等于允许"同一句话既是偏好又是事实"，检索时两条一起进
    top_k，白占两个名额。

    `memory_sessions` 记"哪些会话已经处理过"。它的行在**所有条目写完之后**才写，
    所以进程中途被杀 → 没这一行 → 下次重新总结 → `text_hash` 去重挡住重复条目。
    "重试"这件事因此不需要额外的状态机。
    """
    conn.execute(_CREATE_ITEMS)
    conn.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{ITEM_TABLE}_hash "
                 f"ON {ITEM_TABLE}(text_hash)")
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{ITEM_TABLE}_created "
                 f"ON {ITEM_TABLE}(created_at)")
    conn.execute(_CREATE_SESSIONS)


# ---------------------------------------------------------------- 时间

def utcnow() -> datetime:
    """当前的 UTC 时刻。

    名字带 `utc` 而不是叫 `now`：`age_days` / `recall` 都有一个叫 `now` 的关键字
    参数（那是"以哪个时刻为基准"，是用例钉住分数的地方）。模块级函数同名的话，
    函数体里那句 `now or now()` 会先解析到参数，于是"没传 now"变成
    `None()`——一个只在缺省路径上炸的 `TypeError`。
    """
    return datetime.now(timezone.utc)


def parse_time(stamp: str | None) -> datetime | None:
    """把库里的 ISO 时间读成 aware datetime；读不出来返回 None。

    三种形状都要认，因为库里的时间来自三个地方：SQLite 的
    `strftime('%Y-%m-%dT%H:%M:%fZ')`（带毫秒）、模型给的大概日期（`YYYY-MM-DD`）、
    以及可能有也可能没有毫秒的历史行。统一按 UTC 读：写入端全是 UTC，这里如果
    按本地时区解释，衰减会整体偏几个小时——不致命，但会让"30 天"变成"29.8 天"，
    而这种偏差只会在对比两份部署时暴露。
    """
    if not stamp:
        return None
    text = stamp.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# ---------------------------------------------------------------- 向量

def _vector_for(text: str) -> tuple[list[float] | None, str | None]:
    """条目文本 → 单位向量；未配置或失败时返回 (None, None)。

    失败只记 warning、不抛：一条记忆没有向量，后果是它检索不到（`recall` 会跳过
    没有向量的行），而不是整次总结失败。反过来做的话，一次 embedding 抖动会让
    "这次会话总结出 4 条"变成"一条都没写进去"。
    """
    if not embedding_configured():
        logger.warning("记忆条目没有向量：向量服务未配置，这条只能靠人工查看")
        return None, None
    try:
        vectors = embed_texts([text])
    except EmbeddingNotConfigured:
        logger.warning("记忆条目没有向量：向量服务未配置，这条只能靠人工查看")
        return None, None
    except EmbeddingError as exc:
        logger.warning("记忆条目没有向量：%s", f"{type(exc).__name__}：{exc}"[:160])
        return None, None
    vector = vectors[0] if vectors else None
    return (vector, embedding_model()) if vector else (None, None)


# ---------------------------------------------------------------- 写

@dataclass(frozen=True)
class Item:
    """`memory_items` 里的一行。`vector_dim` 为 None 表示这条没有向量。"""

    id: str
    text: str
    kind: str
    source: str
    created_at: str
    formed_at: str | None = None
    last_used_at: str | None = None
    source_run_id: str | None = None
    session_id: str | None = None
    vector_dim: int | None = None
    model: str | None = None

    @property
    def has_vector(self) -> bool:
        return self.vector_dim is not None


@dataclass(frozen=True)
class Added:
    """`add()` 的结果：写进去的那条记忆，以及这次是新建还是命中去重。"""

    item: Item
    created: bool


def _row(row: sqlite3.Row) -> Item:
    return Item(
        id=row["id"],
        text=row["text"],
        kind=row["kind"],
        source=row["source"],
        created_at=row["created_at"],
        formed_at=row["formed_at"],
        last_used_at=row["last_used_at"],
        source_run_id=row["source_run_id"],
        session_id=row["session_id"],
        vector_dim=row["dim"],
        model=row["model"],
    )


def _connect() -> sqlite3.Connection:
    # 表在 `repository.connect()` 里与 `mindmaps` / `runs` 同一时刻建好，所以这里
    # 没有任何"发现表不存在"的分支——与 `prefs.service._connect` 同一条理由。
    return repository.connect()


def _check_text(text: str) -> str:
    """规范化并夹住长度。空文本抛 `ValueError`（那是程序错误，不是数据问题）。"""
    cleaned = " ".join((text or "").split())
    if not cleaned:
        raise ValueError("记忆条目不能是空文本")
    if len(cleaned) > MAX_ITEM_CHARS:
        logger.warning("记忆条目超过 %d 字上限，已截断（原文 %d 字）",
                       MAX_ITEM_CHARS, len(cleaned))
        cleaned = cleaned[:MAX_ITEM_CHARS]
    return cleaned


def add(
    text: str,
    kind: str = "preference",
    *,
    source: str = SOURCE_SUMMARY,
    source_run_id: str | None = None,
    session_id: str | None = None,
    formed_at: str | None = None,
    embed: bool = True,
) -> Added:
    """写一条记忆；文本已存在时返回已有那条（`created=False`），不重复写。

    **`created_at` 是总结时刻，不是"这条事发生的时刻"**。两者分开存：
    `created_at` 是审计事实（"这条是什么时候被写进来的"），`formed_at` 是模型对
    "这条大约是什么时候形成的"的推断，可空。衰减基准取 `COALESCE(formed_at,
    created_at)`——见 `recall()`。

    为什么不把 `formed_at` 直接写进 `created_at`：那会让"这条记忆是哪次会话总结
    出来的"变成一个需要反推的问题，而 `session_id` / `source_run_id` 正是为回答
    它存在的。审计事实被模型推断覆盖，是一种不可逆的信息丢失。

    去重命中且向量齐全时**一次 embedding 都不再付**：同一条偏好会在好几次会话里
    被反复总结出来，那是常态。
    """
    cleaned = _check_text(text)
    if kind not in KINDS:
        raise ValueError(f"未知的记忆档位：{kind!r}")
    if source not in SOURCES:
        raise ValueError(f"未知的记忆来源：{source!r}")
    digest = vector_cache.text_hash(cleaned)

    with closing(_connect()) as conn, conn:
        existing = conn.execute(
            f"SELECT * FROM {ITEM_TABLE} WHERE text_hash = ?", (digest,)).fetchone()
        if existing is not None and existing["vector"] is not None:
            return Added(item=_row(existing), created=False)

        vector, model = _vector_for(cleaned) if embed else (None, None)
        if existing is not None:
            # 命中但原来没有向量（那次 embedding 失败了）：这次补上——否则这条记忆
            # 会永久检索不到，而它在列表里看起来完全正常。
            if vector is not None:
                conn.execute(
                    f"UPDATE {ITEM_TABLE} SET vector = ?, dim = ?, model = ? WHERE id = ?",
                    (vector_cache.pack(vector), len(vector), model, existing["id"]),
                )
                existing = conn.execute(
                    f"SELECT * FROM {ITEM_TABLE} WHERE id = ?", (existing["id"],)).fetchone()
            return Added(item=_row(existing), created=False)

        item_id = uuid4().hex
        conn.execute(
            f"INSERT INTO {ITEM_TABLE} (id, text, kind, source, created_at, formed_at, "
            f"source_run_id, session_id, text_hash, model, dim, vector) "
            f"VALUES (?, ?, ?, ?, {_NOW}, ?, ?, ?, ?, ?, ?, ?)",
            (item_id, cleaned, kind, source, formed_at, source_run_id, session_id,
             digest, model, None if vector is None else len(vector),
             None if vector is None else vector_cache.pack(vector)),
        )
        row = conn.execute(f"SELECT * FROM {ITEM_TABLE} WHERE id = ?", (item_id,)).fetchone()
    return Added(item=_row(row), created=True)


def delete(item_id: str) -> bool:
    """硬删一条。返回是否真的删掉了——404 与"删了但没删掉"必须分得开。"""
    with closing(_connect()) as conn, conn:
        result = conn.execute(f"DELETE FROM {ITEM_TABLE} WHERE id = ?", (item_id,))
    return result.rowcount == 1


def count() -> int:
    with closing(_connect()) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {ITEM_TABLE}").fetchone()[0])


def items(limit: int = 200) -> list[Item]:
    """最近的若干条，新的在前。供 HTTP 列表与总结时的"已知记忆"用。"""
    with closing(_connect()) as conn:
        rows = conn.execute(
            f"SELECT * FROM {ITEM_TABLE} ORDER BY created_at DESC, id LIMIT ?",
            (max(1, limit),)).fetchall()
    return [_row(row) for row in rows]


__all__ = [
    "ITEM_TABLE",
    "KINDS",
    "MAX_ITEM_CHARS",
    "SESSION_TABLE",
    "SOURCES",
    "SOURCE_SUMMARY",
    "SOURCE_USER_EXPLICIT",
    "Added",
    "Item",
    "add",
    "count",
    "delete",
    "ensure_schema",
    "items",
    "parse_time",
    "utcnow",
]
