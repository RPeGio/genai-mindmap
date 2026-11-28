"""块向量的缓存（PostgreSQL `chunk_vectors` 表）：混合检索里省掉重复的 embedding 调用。

**为什么按文本哈希缓存，而不是按 `source_id`**：`Source.id` 是
`uuid4()` 默认值，同一份材料每次请求都会拿到新 id——按它缓存的命中率恒为 0，
表只会无限膨胀。真正稳定的是块文本本身：同一份材料重传、同一次会话里
`rag_query` 被多次调用、相邻块因 100 码点重叠而共享大段原文，都能命中。

**知识库落地之后这条结论不变，但理由要补一句**：`knowledge/library.py` 给了来源
一个跨请求稳定的 id，所以按 `source_id` 缓存**已经能命中了**。仍然按文本哈希，是
因为它还能跨来源共享同一段原文（库来源与本次上传的同一份材料、两块因重叠共享
100 码点的原文），而按来源缓存做不到；而且 `prune` 删掉一份来源时，按来源缓存
会连带丢掉那些仍被别处引用的向量。代价是这张表**没有** `source_id` 列，所以
"某个来源的向量"永远查不出来——那是刻意的，它是缓存，不是索引。

**为什么和导图共用同一个库**（`mindmaps.repository.dsn()`）：省掉一套备份、
一套"测试要记得改连接串"的环境变量。风险是事务隔离——写入是单条自动提交的短写，
不与 `update_map` 的乐观锁共用事务；PG 的行锁只锁那一行，真出现写冲突时最坏结果
是这轮缓存没写进去（下次重新 embed），不会破坏导图记录。

**向量列是 `bytea`，不是 pgvector**：仓库里向量的相似度由纯 Python 算
（`app/knowledge/service.py` 的混合检索），没有 ANN 索引需求；上 pgvector 只是
多一个扩展依赖、多一份 HNSW 内存（2C2G 上不划算），换不来本项目的查询方式。
打包格式（小端 float32）与 `memory_items` 逐字节同构，见 `pack()` 的 docstring。

**上限保护**：行数到 `MAX_ROWS` 就不再写入，只读已有。向量表是纯缓存，丢掉
它只会变慢，不会丢数据，所以宁可停写也不要让它吃掉磁盘。
"""
from __future__ import annotations

import hashlib
import logging
import struct
from contextlib import closing

from app.db.conn import Connection
from app.db.errors import DatabaseError
from app.mindmaps import repository

logger = logging.getLogger(__name__)

VECTOR_TABLE = "chunk_vectors"

# 缓存行数上限。1024 维 float32 = 4 KB/行，20 万行约 800 MB——已经是个不该
# 被静默跨过的体积，所以到线就停写。
MAX_ROWS = 200_000

#: 一次 IN 查询按 400 分批。SQLite 那里是"单条语句最多 999 个变量"的上限；PG 的
#: 上限是 65535，但保持 400 不动：批越小，单条语句的往返与内存越可控，而这条路的
#: 瓶颈从来不是批大小。
_QUERY_BATCH = 400

#: 建表语句。**生产路径不再执行它**（表由 `db.schema.init_schema()` 建），保留是因为
#: 测试与一次性脚本会直接拿它兜"空库上也能跑"。热路径上别调：PG 上一条
#: `CREATE TABLE IF NOT EXISTS` 是真实往返，实测 2.458ms。
_CREATE = f"""CREATE TABLE IF NOT EXISTS {VECTOR_TABLE} (
    text_hash TEXT NOT NULL,
    model     TEXT NOT NULL,
    dim       INTEGER NOT NULL,
    vector    bytea NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (text_hash, model)
)"""


def text_hash(text: str) -> str:
    """块文本的稳定键：sha256 前 32 位十六进制。

    取前 32 位（128 bit）而不是全长是为了让键短一半；在这个规模下碰撞概率
    可以忽略，而键短了索引和 IN 查询都更省。
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def pack(vector: list[float]) -> bytes:
    """float32 小端打包。小端是显式指定的，不随机器字节序变。

    公开而不是私有：**"同一存法"必须只有一个实现**。`memory_items` 的向量列与
    `chunk_vectors.vector` 逐字节同构，两处各写一份打包函数的结果已经出现过一次：
    一处改了字节序，另一处的旧向量全部读成 `None`，而失败只有一个沉默的 `None`。
    """
    return struct.pack(f"<{len(vector)}f", *vector)


def unpack(blob: bytes, dim: int) -> list[float] | None:
    """解包；长度对不上（维度改过、写坏了）就当没有，绝不返回半截向量。"""
    if dim <= 0 or len(blob) != dim * 4:
        return None
    return list(struct.unpack(f"<{dim}f", blob))


# 旧名保留：本模块内部的调用点与可能存在的既有引用都还用这两个下划线名字。
# 它们只是别名，不是第二份实现。
_pack = pack
_unpack = unpack


def _open() -> Connection | None:
    """开连接。任何失败都返回 None：缓存不可用不是错误。

    **这里不再建表**：`chunk_vectors` 由 `db.schema.init_schema()` 在建库时建好。
    SQLite 时代重放 `CREATE TABLE IF NOT EXISTS` 是空转；PG 上每条都是真实往返
    （实测 2.458ms），而这是检索热路径——一次生成要读上百次向量。
    """
    try:
        return repository.connect()
    except (DatabaseError, OSError) as exc:
        logger.warning("向量缓存不可用：%s", type(exc).__name__)
        return None


def get_vectors(hashes: list[str], model: str) -> dict[str, list[float]]:
    """按文本哈希批量取向量；没命中的不在返回值里。"""
    if not hashes:
        return {}
    unique = list(dict.fromkeys(hashes))
    conn = _open()
    if conn is None:
        return {}
    found: dict[str, list[float]] = {}
    with closing(conn), conn:
        for start in range(0, len(unique), _QUERY_BATCH):
            window = unique[start:start + _QUERY_BATCH]
            marks = ",".join("?" * len(window))
            rows = conn.execute(
                f"SELECT text_hash, dim, vector FROM {VECTOR_TABLE} "
                f"WHERE model = ? AND text_hash IN ({marks})",
                (model, *window),
            ).fetchall()
            for row in rows:
                vector = _unpack(row["vector"], row["dim"])
                if vector:
                    found[row["text_hash"]] = vector
    return found


def put_vectors(items: list[tuple[str, list[float]]], model: str) -> int:
    """写入向量；同 (文本, 模型) 已有记录时忽略。返回真正落库的条数。

    `ON CONFLICT DO NOTHING` 而不是 REPLACE：同一段文本同一模型算出来的向量本来
    就应该一样，重复写只会白加一次磁盘写和一次 WAL 抖动。
    """
    if not items:
        return 0
    conn = _open()
    if conn is None:
        return 0
    written = 0
    with closing(conn), conn:
        # 上限检查放在写入前，一次检索只走一次 COUNT。
        total = conn.execute(f"SELECT COUNT(*) AS total FROM {VECTOR_TABLE}").fetchone()["total"]
        if total >= MAX_ROWS:
            logger.info("向量缓存已达上限 %d 行，本次只读不写（%d 条未落库）",
                        MAX_ROWS, len(items))
            return 0
        for hash_value, vector in items:
            if not vector:
                continue
            cursor = conn.execute(
                f"INSERT INTO {VECTOR_TABLE} "
                f"(text_hash, model, dim, vector, created_at) "
                f"VALUES (?, ?, ?, ?, nowstamp()) "
                f"ON CONFLICT (text_hash, model) DO NOTHING",
                (hash_value, model, len(vector), _pack(vector)),
            )
            written += cursor.rowcount
    return written
