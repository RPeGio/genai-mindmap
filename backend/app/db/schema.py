"""幂等建表：进程启动时跑一次，测试建库时跑一次。

## 为什么建表不在 `connect()` 里了

旧的 `repository.connect()` 每次开连接都重放一遍建表与补列（`CREATE TABLE IF NOT
EXISTS` ×13、`PRAGMA table_info` ×6、`ALTER` 若干）。那在 SQLite 上是"读时补齐"的
合理写法：文件句柄几十微秒，十条 DDL 是本地元数据操作。

在 PG 上这十条 DDL 要走网络、要拿锁、要写 WAL——实测 **2.458ms**。而旧代码有 21 处
连接入口、190 处 `execute`，一次生成光读模型配置就要 5–27 次连接。把建表从热路径
上拿下来，是这次迁移最大的单笔收益之一。

**顺带消失的东西**（不要留，留着就是死代码）：
- `_ensure_columns` 那一整套"PRAGMA 查列 → ALTER 补列 → 吞 duplicate column 竞态"：
  PG 有 `ADD COLUMN IF NOT EXISTS`，而且这里建的就是终态表，没有"线上老表缺列"。
- `_drop_columns`：同理。
- 三处 legacy 就地迁移（`user_prefs` 单主键 → 复合主键、`model_settings` 单行 →
  一 owner 一行、`style_profile` 同前）：一次性导入脚本（M6）负责把老库搬进来，
  搬的时候就搬进终态表，运行期不再有"半成品状态要续上"。

## 为什么是一个函数而不是 8 个 `ensure_schema`

原来 8 个模块各有 `ensure_schema(conn)`，全部由 `repository.connect()` 顺次调用。
现在生产路径上一个都不调了（建表只在 `init_schema()`），分散在 8 个模块里只让"到底
建了哪些表"更难读。表都在这儿列全。

**各模块的 `ensure_schema` 不是薄壳**：它们是各自完整的建表 DDL（`auth.store` 5 条、
`knowledge.library` 3 条，其余各 1 条上下），一行 `init_schema()` 都没有。留着它们
只是因为 `tests/test_auth.py` 直接 import 过一个，删掉会连带改测试口径。**新代码不要
照着抄**：加表/加列请直接改本文件的 `DDL`，那才是唯一在跑的那份。

## `end` 是保留字

`library_chunks.end` 与 `UNION`/`CASE ... END` 的 `END` 撞车，PG 直接语法错
（实测 `CREATE TABLE t (start INTEGER, end INTEGER)` → `syntax error at or near "end"`）。
SQLite 宽容，PG 不。所以那一列在 DDL 与所有 SQL 里都写成 `"end"`——**列名不变**，
契约与 Python 侧（`row["end"]`、`StoredChunk.end`）零改动。
"""
from __future__ import annotations

import logging

from app.db.conn import Conn
from app.db import pool as pool_module

logger = logging.getLogger(__name__)

#: 当前 schema 版本。写进 `schema_migrations` 是为了让"这个库是哪一版建出来的"能直接查，
#: 而不是靠挨表数有没有新列。**不做迁移判断**：这次迁移之后的结构就是唯一的结构，
#: 以后再加列时用 `ADD COLUMN IF NOT EXISTS`，加表时用 `CREATE TABLE IF NOT EXISTS`。
SCHEMA_VERSION = "2"

#: 服务器端时间戳函数。**这是整个迁移里最关键的一个替换**。
#:
#: 旧代码有 10 个模块写着 `_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"` 并把它
#: f-string 拼进 SQL。PG 里对应的写法是 `to_char(statement_timestamp() at time zone
#: 'utc', ...)`，而那个格式串里全是 `%`——psycopg 在**带参数**的语句上会把 `%` 当占位符解析，
#: 于是 `%Y` 会让整条语句抛 `ProgrammingError: only '%s', '%b', '%t' are allowed
#: as placeholders, got '%'`。要绕开可以写 `%%Y`，但那就意味着每个 `_NOW` 用在
#: 带参数的语句里时要手工转义，而用在无参数语句里时又不能转义——同一个常量两种写法。
#:
#: 包成 SQL 函数之后，SQL 文本里只剩 `nowstamp()` 八个字母，一个 `%` 都没有。
#: 实测：`nowstamp()` → `2026-11-28T04:30:45.430Z`，与 SQLite 的
#: `%Y-%m-%dT%H:%M:%fZ` 输出**逐字节同形**（正则 `\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\\.\\d{3}Z`
#: 完全匹配），所以 `TEXT` 时间戳列、以及依赖字典序的字符串比较（`memory/summarize.py`
#: 的 `formed_at > ?`、`external_cache.py` 的 `searched_at < ?`、`edits.py` 的
#: 一日窗口）全部原样成立。
#:
#: **为什么是 `statement_timestamp()` 而不是 `now()`**：`now()` 就是
#: `transaction_timestamp()`，它在**一个事务内恒定**，而 SQLite 的 `strftime(...,'now')`
#: 是**每条语句**重新求值。用 `now()` 的后果实测过：同一事务里隔 0.4 秒写两行，
#: 时间戳**逐字节相等**。影响最大的是 `library.ingest_many`——一个事务里循环入库多份
#: 材料，每份的 `created_at`/`last_used_at` 全同，`prune` 的"按新旧淘汰"就此退化成按 id。
#:
#: 也不用 `clock_timestamp()`：它**在同一语句的多行之间**也会变（实测同一条
#: `INSERT ... SELECT FROM generate_series(1,3)` 得到 3 个不同的值），而 SQLite 的
#: `now` 在一条语句内是常量。`statement_timestamp()` 恰好两头都对：跨语句不等、
#: 单语句内多行同值（实测 500 行只有 1 个不同值）。回归用例见
#: `tests/test_schema_migrations.py::test_nowstamp_is_a_statement_clock_not_a_transaction_clock`。
#:
#: **性能没有代价**：`INSERT ... VALUES (nowstamp())` p50 3.879ms vs 内联
#: `to_char(...)` p50 3.938ms —— `STABLE` 的 SQL 函数会被内联，等于白拿。
#:
#: `offset_seconds` 是为了替掉 SQLite 的 `strftime(..., 'now', '-N seconds')`
#: （5 处：`runs.sweep_abandoned`、`auth.store` 的会话过期、`library.prune`）和
#: `date('now','-1 day')`（`edits.py` 一处）。它们都是"相对现在偏移一段时间"，
#: 用 `make_interval` 而不是拼 `'-N seconds'` 字符串：拼字符串要过类型转换，
#: 而 `interval` 的字符串解析规则比参数绑定宽松得多。
_NOWSTAMP_SQL = """
CREATE OR REPLACE FUNCTION nowstamp(offset_seconds double precision DEFAULT 0)
RETURNS text
LANGUAGE sql
STABLE
AS $$
    SELECT to_char(
        (statement_timestamp() AT TIME ZONE 'utc') + make_interval(secs => offset_seconds),
        'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'
    )
$$
"""

#: 表清单与建表顺序（外键在前）。`conftest.py` 的每测试清库按**反序** DELETE。
DDL: tuple[str, ...] = (
    # ---------------------------------------------------------------- 导图
    """CREATE TABLE IF NOT EXISTS mindmaps (
        id         TEXT PRIMARY KEY,
        version    INTEGER NOT NULL,
        content    TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        owner      TEXT NOT NULL DEFAULT ''
    )""",
    """CREATE TABLE IF NOT EXISTS runs (
        run_id        TEXT PRIMARY KEY,
        status        TEXT NOT NULL,
        request       TEXT,
        candidate     TEXT,
        failure       TEXT,
        mode          TEXT,
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL,
        applied_at    TEXT,
        model         TEXT,
        reasoning     TEXT,
        partial       INTEGER,
        partial_reason TEXT,
        served_model  TEXT,
        checkpointed_at TEXT,
        owner         TEXT NOT NULL DEFAULT ''
    )""",
    "CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status, updated_at)",
    """CREATE TABLE IF NOT EXISTS user_prefs (
        owner         TEXT NOT NULL DEFAULT '',
        pref_key      TEXT NOT NULL,
        pref_value    TEXT NOT NULL,
        scope         TEXT NOT NULL DEFAULT 'global',
        source        TEXT NOT NULL,
        source_run_id TEXT,
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL,
        PRIMARY KEY (owner, pref_key)
    )""",
    # `id` 用 bigserial：契约 `contracts/edits.py` 里 `EditEventRow.id` 是 `int`，
    # 换成 UUID 就要动 openapi.json → schema.d.ts → types.ts → client.ts 一整条链，
    # 而 CI 有 openapi 快照 diff 校验（`.github/workflows/ci.yml`）。bigserial 让
    # 契约零改动。代价是"id 连续"这件事只在单库内成立——`test_edit_events.py` 里
    # `assert rows[1]["id"] == 2` 因此依赖每测试重置序列，见 `conftest.py`。
    """CREATE TABLE IF NOT EXISTS edit_events (
        id           bigserial PRIMARY KEY,
        map_id       TEXT NOT NULL,
        run_id       TEXT,
        owner        TEXT NOT NULL DEFAULT '',
        actor        TEXT NOT NULL,
        kind         TEXT NOT NULL,
        node_id      TEXT,
        field        TEXT,
        before       TEXT,
        after        TEXT,
        depth        INTEGER,
        subtree_size INTEGER,
        change_count INTEGER,
        node_count   INTEGER,
        at           TEXT NOT NULL,
        recorded_at  TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_edit_events_map ON edit_events(map_id, at)",
    # ---------------------------------------------------------------- 身份
    # `username` 的大小写无关判据：SQLite 用 `COLLATE NOCASE` 建列，PG 没有这个
    # collation 修饰符。改成"列存原样 + 表达式唯一索引"，查询侧写
    # `WHERE lower(username) = lower(%s)`。实测大小写不同也抛唯一冲突、查询正确命中。
    #
    # **为什么不用 `citext`**：那是额外扩展，而 2C2G 部署里多装一个扩展就是多一处
    # 需要装的依赖。`lower()` 索引足够，而且不依赖初始化时用的 locale。
    """CREATE TABLE IF NOT EXISTS users (
        id            TEXT PRIMARY KEY,
        username      TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username_lower ON users(lower(username))",
    """CREATE TABLE IF NOT EXISTS sessions (
        token_hash   TEXT PRIMARY KEY,
        user_id      TEXT NOT NULL,
        created_at   TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        expires_at   TEXT NOT NULL,
        revoked_at   TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at)",
    # ---------------------------------------------------------------- 限流与配额
    # `first_at` / `last_at` 是 epoch 秒（`REAL`），不是 ISO 文本：这张表只做减法。
    """CREATE TABLE IF NOT EXISTS login_attempts (
        bucket   TEXT PRIMARY KEY,
        failures INTEGER NOT NULL,
        first_at double precision NOT NULL,
        last_at  double precision NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS free_quota (
        bucket  TEXT NOT NULL,
        day     TEXT NOT NULL,
        used    INTEGER NOT NULL,
        last_at double precision NOT NULL,
        PRIMARY KEY (bucket, day)
    )""",
    # ---------------------------------------------------------------- 模型配置
    """CREATE TABLE IF NOT EXISTS model_settings (
        owner        TEXT PRIMARY KEY,
        base_url     TEXT NOT NULL DEFAULT '',
        api_key      TEXT NOT NULL DEFAULT '',
        model        TEXT NOT NULL DEFAULT '',
        deep_model   TEXT NOT NULL DEFAULT '',
        updated_at   TEXT,
        api_key_host TEXT NOT NULL DEFAULT ''
    )""",
    # ---------------------------------------------------------------- 知识库
    """CREATE TABLE IF NOT EXISTS library_sources (
        id           TEXT PRIMARY KEY,
        content_hash TEXT NOT NULL,
        filename     TEXT,
        kind         TEXT NOT NULL,
        text         TEXT NOT NULL,
        metadata     TEXT NOT NULL,
        created_at   TEXT NOT NULL,
        last_used_at TEXT NOT NULL,
        UNIQUE(content_hash)
    )""",
    # `"end"` 加引号：见模块 docstring 最后一节。
    """CREATE TABLE IF NOT EXISTS library_chunks (
        id        bigserial PRIMARY KEY,
        source_id TEXT NOT NULL REFERENCES library_sources(id) ON DELETE CASCADE,
        start     INTEGER NOT NULL,
        "end"     INTEGER NOT NULL,
        level     INTEGER NOT NULL,
        parent    INTEGER,
        UNIQUE(source_id, start, "end", level)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_library_chunks_source ON library_chunks(source_id)",
    # 向量仍是 bytea + 纯 Python 算（`vector_cache.py` 的 pack/unpack）。
    # **不上 pgvector**：`float32` 的 1024 维就是 4KB，两万行 80MB，全表读进 Python
    # 内部算余弦本来就在亚毫秒量级（`vector_cache.py` 实测）。上扩展要多装一个
    # 服务端插件、要把 pack/unpack 改成 SQL 表达式、还要在 2C2G 上多一份索引内存。
    """CREATE TABLE IF NOT EXISTS chunk_vectors (
        text_hash  TEXT NOT NULL,
        model      TEXT NOT NULL,
        dim        INTEGER NOT NULL,
        vector     bytea NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (text_hash, model)
    )""",
    """CREATE TABLE IF NOT EXISTS external_cache (
        query_hash  TEXT PRIMARY KEY,
        query       TEXT NOT NULL,
        payload     TEXT NOT NULL,
        limit_value INTEGER NOT NULL,
        hit_count   INTEGER NOT NULL DEFAULT 0,
        searched_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS external_cache_searched_at ON external_cache(searched_at)",
    # ---------------------------------------------------------------- 记忆
    """CREATE TABLE IF NOT EXISTS memory_items (
        id            TEXT PRIMARY KEY,
        owner         TEXT NOT NULL DEFAULT '',
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
        vector        bytea
    )""",
    # 唯一键是 `(owner, text_hash)`：同一条文本只该存在一次（与档位无关），
    # 而加 owner 之后必须跟着变，否则两个人说同一句话时第二个人写不进去。
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_items_owner_hash "
    "ON memory_items(owner, text_hash)",
    "CREATE INDEX IF NOT EXISTS idx_memory_items_created ON memory_items(created_at)",
    """CREATE TABLE IF NOT EXISTS memory_sessions (
        session_id    TEXT PRIMARY KEY,
        status        TEXT NOT NULL,
        item_count    INTEGER NOT NULL DEFAULT 0,
        detail        TEXT,
        summarized_at TEXT NOT NULL
    )""",
    # ---------------------------------------------------------------- 偏好与画像
    """CREATE TABLE IF NOT EXISTS style_profile (
        owner      TEXT NOT NULL DEFAULT '',
        key        TEXT NOT NULL,
        value      TEXT NOT NULL,
        samples    INTEGER NOT NULL,
        confidence double precision NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (owner, key)
    )""",
    """CREATE TABLE IF NOT EXISTS profile_vectors (
        id         TEXT PRIMARY KEY,
        vector     bytea NOT NULL,
        label      TEXT NOT NULL,
        weight     double precision NOT NULL,
        samples    INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        map_id     TEXT
    )""",
    # ---------------------------------------------------------------- 元信息
    """CREATE TABLE IF NOT EXISTS schema_migrations (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""",
)

#: 按**反序**清空用的表名（子表在前、父表在后，照顾 `library_chunks` 的外键）。
#: 顺序错了会撞外键——不是"慢一点"，是直接抛错。
TABLES_IN_CLEAR_ORDER: tuple[str, ...] = (
    "library_chunks",
    "library_sources",
    "chunk_vectors",
    "external_cache",
    "memory_items",
    "memory_sessions",
    "style_profile",
    "profile_vectors",
    "edit_events",
    "runs",
    "mindmaps",
    "user_prefs",
    "model_settings",
    "free_quota",
    "login_attempts",
    "sessions",
    "users",
)

#: 带序列的表（每测试重置自增用）。`ALTER SEQUENCE ... RESTART` 单独走，因为它
#: 不属于 DELETE。
SEQUENCES: tuple[str, ...] = ("edit_events_id_seq", "library_chunks_id_seq")


def init_schema(conn: Conn | None = None) -> None:
    """建全部表、索引与 `nowstamp()`。幂等，可重复执行。

    自己取连接时会在一个事务里跑完（任何一步失败整批回滚，不留半成品库）；传入
    连接时用它的事务，由调用方决定何时提交。
    """
    owns = conn is None
    if owns:
        conn = _connect()
    try:
        conn.execute(_NOWSTAMP_SQL)
        for statement in DDL:
            conn.execute(statement)
        # 用 "INSERT ... ON CONFLICT" 而不是 "INSERT OR REPLACE"：后者在 PG 里没有
        # 这个写法，而且我们只想"第一次写入时记版本"，不想每次启动都覆盖。
        conn.execute(
            "INSERT INTO schema_migrations (key, value) VALUES ('version', ?) "
            "ON CONFLICT (key) DO NOTHING", (SCHEMA_VERSION,))
        conn.commit()
        logger.info("schema 就绪（v%s）", SCHEMA_VERSION)
    finally:
        if owns:
            conn.close()


def clear_all(conn: Conn | None = None) -> None:
    """清空全部业务数据并重置自增。**只给测试用**。

    ## 为什么是 `DELETE FROM` 而不是 `TRUNCATE`

    在当前这 17 张表上实测（全部含 commit，中位数）：

    | 做法 | p50 |
    |---|---|
    | `DELETE FROM` 17 张 + 2 条 `ALTER SEQUENCE`（经 shim，= 实际走的路径） | **17.4ms** |
    | 同上，但用 raw cursor 绕开 shim 的每语句 SAVEPOINT | 8.7ms |
    | `TRUNCATE ... RESTART IDENTITY CASCADE` | **332ms** |
    | `CREATE SCHEMA` + 建 17 表 + `DROP SCHEMA CASCADE` | 14.0ms |
    | `CREATE DATABASE ... TEMPLATE` + `DROP DATABASE` | 127ms |

    `TRUNCATE` 慢 19 倍，而且**不是被 WAL 刷盘拖慢的**：`synchronous_commit=off`
    实测 on≈180ms / off≈182ms 没有区别，开销在它自己的文件截断路径上。
    `DELETE` 是空表上的元组扫描，代价与"表里有没有行"几乎无关（清场的目的就是不
    留行，所以"空表"本来就是要打的那一档）。

    `CREATE SCHEMA` 那档只慢/快几毫秒，看着可以，但它要求**每条连接都带对
    `search_path`**（DSN 的 `options=-c search_path=...`），把"每条用例一个 schema"
    耦合进连接串，而代码里 21 处连接入口都是走同一个池、同一个 DSN。为了省 3ms
    把隔离机制塞进 DSN，下次谁写个直连就绕过隔离——这正是"坏了和本来就没有长得
    一样"。所以选 `DELETE`。

    ## 序列必须重置

    `test_edit_events.py` 里有 `assert rows[1]["id"] == 2`，跨用例残留会让它红。
    两条 `ALTER SEQUENCE ... RESTART` 实测约 4.7ms，是一笔固定开销（1 条与 2 条
    几乎没差别，都在 4-4.7ms），不吃表数。

    ## 17.4ms 意味着什么

    2775 条用例 × 17.4ms ≈ **48 秒**，占全量 150 秒的三分之一。这是这笔设计最大
    的一处代价，也是它仍然比另外几档便宜的原因（`TRUNCATE` 那档会是 15 分钟）。

    自己取连接时在一个事务里跑完；传入连接时用它的事务（`conftest.fresh_db` 走后者，
    省掉每测试一次往返）。
    """
    owns = conn is None
    if owns:
        conn = _connect()
    try:
        for table in TABLES_IN_CLEAR_ORDER:
            conn.execute(f"DELETE FROM {table}")
        for sequence in SEQUENCES:
            conn.execute(f"ALTER SEQUENCE {sequence} RESTART WITH 1")
        conn.commit()
    finally:
        if owns:
            conn.close()


def _connect() -> Conn:
    """取一条池连接。函数内 import 避免与 `repository` 成环。"""
    return Conn(pool_module.get_pool().getconn(), pool_module.get_pool())


# ---------------------------------------------------------------- 列增删
#
# 这两个函数是旧的 `repository._ensure_columns` / `_drop_columns` 的替代。旧版本要做
# "PRAGMA 查列 → 决定要不要 ALTER → 吞掉 `duplicate column name` 竞态"这一整套，
# 因为 SQLite 没有 `IF NOT EXISTS`。PG 有，所以这里是三行。
#
# **生产路径上没有人调它们**：表由 `init_schema()` 一次建到终态，加列时改上面的
# `DDL` 就行。还在的几个调用点（`settings/store.py`、`memory/store.py`、
# `prefs/profile.py` 的 `ensure_schema`）本身也不在生产路径上——它们只被
# `tests/test_auth.py` 直接 import 过，删掉要连带改测试口径。
#
# 留着**不是为了兼容老库**（老库那条路由一次性导入脚本接管），而是为了让并行改动
# 不撞车；但请不要把 `ensure_columns(...)` 当成"加列的常规做法"，它不是。


def ensure_columns(conn: Conn, table: str, columns: dict[str, str]) -> None:
    """给表补列。幂等（`ADD COLUMN IF NOT EXISTS`，PG 9.6+）。

    与旧实现的差别只有一条，但它很重要：**这里不需要吞任何异常**。旧版本必须 catch
    `duplicate column name`，因为"两个连接同时查列、同时 ALTER"有一个必输；PG 的
    `IF NOT EXISTS` 把那个竞态收进了语句本身。少一处 `except` 就少一处"真正的错误
    被当成竞态吞掉"的机会。
    """
    for name, declaration in columns.items():
        conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {declaration}")


def drop_columns(conn: Conn, table: str, columns: tuple[str, ...]) -> None:
    """删列。幂等（`DROP COLUMN IF EXISTS`）。"""
    for name in columns:
        conn.execute(f'ALTER TABLE {table} DROP COLUMN IF EXISTS "{name}"')

