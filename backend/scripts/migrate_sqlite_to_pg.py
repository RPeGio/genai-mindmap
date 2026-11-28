"""把迁移前的 SQLite 单文件库一次性搬进 PostgreSQL。

## 为什么是"一次性导入"，不是双写

`docs/PG-MIGRATION-PLAN.md` 的 D10：这条迁移**不保留双轨**。理由不是省事，而是
双写要求"两份存储在任何时刻都正确"——一边是单文件库、一边是服务端库，两套失败
模式、两套备份、两套恢复演练；而切换时活跃库里只有二十行真实数据，双写的收益是
零，风险却是双份。所以旧库只被读一次，读完就退休。

## 为什么必须停服导入

脚本自己不做"边跑边被写"的防护：它先读老库、再建目标、再逐表搬、最后比行数。
如果这时还有进程在往老库写，最后那一步比对会看到"老库多了几行"——那不是脚本
的 bug，是它不该在这种状态下被跑。所以流程是**停服 → 跑本脚本 → 校验 → 切连接串
→ 旧库原样留在磁盘上作回滚点**（回滚就是把 `MINDMAP_PG_DSN` 撤掉、把老文件放回
原处重启服务，因为老文件从头到尾没被写过）。

## 为什么它读 SQLite，而全仓别处都不许

这是**整个仓库里唯一合法 `import sqlite3` 的地方**。别处的 SQLite 痕迹都已在
M0–M5 清掉（`grep -rn "import sqlite3" backend/app` 应为 0 命中）；这里之所以
例外，是因为它的职责就是读那份"迁移前的快照"，而快照的格式就是 SQLite 文件。
M6 之后这个脚本只剩"历史留档"的价值（部署时也可能需要重放一次），但它必须留着：
删了它，万一切换后才发现目标库少东西，就没有一个可复现的导入路径了。

## 为什么列集要探测，不能写死

老库是**按时代演进的**：`main` 时代没有 `owner` 列、PR#27 之前没有
`api_key_host` 列。写死 `SELECT id, version, content, updated_at, owner` 会在
`SELECT` 阶段抛 `no such column`，而那正是迁移脚本最不该出现的失败方式（它看起来
像"库坏了"）。所以逐表用 `PRAGMA table_info` 读老库实际列，与目标表实际列取交集
再搬——老库多出来的历史列自然不搬，目标表新增的列由 PG 的列默认值兜住。

这套逻辑原来在 `app/settings/store.py::_copy_legacy_rows`（base `97bbfeb`），
M0 把它连同"运行期就地迁移"一起删了；搬到这里是因为它现在只该发生一次。

## 幂等

`INSERT ... ON CONFLICT DO NOTHING`（不带冲突目标：主键、`UNIQUE(content_hash)`、
`UNIQUE(source_id, start, "end", level)`、`UNIQUE(owner, text_hash)` 全都按任意冲突
忽略）。所以跑到一半中断、修好之后重跑，已经搬过的行不会重复插入也不会报错——
这是"导入脚本"必须有的性质，因为它是在生产切换窗口里跑的，不能有"只许成功一次"
的假设。

## 时间戳不做转换

老库与目标库的时间戳都是 `TEXT`（`2026-11-01T00:00:00.000Z` 这种），而且全仓有
依赖**字典序**比较时间的逻辑（`formed_at > ?`、`searched_at < ?`、一日窗口）。
转成 `timestamptz` 会把"按字符串比"变成"按类型比"，等于在导入时顺手改语义。
D4 已经定了保持 TEXT，服务器端同格式由 `nowstamp()` 生成，所以这里原样搬。

用法（在 `backend/` 下）：

    python scripts/migrate_sqlite_to_pg.py --dry-run
    python scripts/migrate_sqlite_to_pg.py --dsn postgresql:///mindmaps?host=/var/run/postgresql
    python scripts/migrate_sqlite_to_pg.py --source /path/to/mindmaps.db --skip-schema

退出码：0 = 全部一致；1 = 有表搬失败或有比对不一致；2 = 参数/环境问题。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:  # 让 `python scripts/xxx.py` 不必先设 PYTHONPATH
    sys.path.insert(0, str(BACKEND))

#: 缺省老库：`backend/data/mindmaps.db`（主 checkout 里那份；worktree 里没有）。
DEFAULT_SOURCE = BACKEND / "data" / "mindmaps.db"

#: 缺省连接串。**故意与 `app/db/pool.py` 的 `DEFAULT_DSN` 同值而不是 import 它**：
#: `--dry-run` 的契约是"不连目标库"，而 import `app.db.pool` 会连带 import psycopg
#: 与建池代码；这里多写一行字符串，换的是"只读一遍老库"这件事能真的成立。
DEFAULT_DSN = "postgresql:///mindmaps?host=/var/run/postgresql"

#: 一批多少行。500 行一条语句在 2C2G 上是"一屏内存 + 一次往返"的折中；表都不大
#: （活跃库总共二十行），这个数字只是防止将来出现大表时把内存吃穿。
BATCH = 500

#: 比对时抽查 `content` 的表。只有导图正文是"错一个字节就等于用户丢东西"的形态，
#: 其余表都是短字段，行数对得上基本就够（键集也比对，见 `_verify`）。
CONTENT_CHECK_TABLE = "mindmaps"

#: SQLite 自己的记账表，不是业务表。
_SQLITE_INTERNAL = ("sqlite_sequence", "sqlite_stat1", "sqlite_stat4")

#: 目标表 NOT NULL、没有列默认值、而老表里**根本不存在**的列，导入时填什么。
#:
#: 只有一条，但它是真实的：`main` 时代的 `model_settings` 是"单行 + `CHECK (id = 1)`"，
#: 加 BYOK 之后主键换成了 `owner`（`TEXT PRIMARY KEY`，没有 DEFAULT——主键本来就不该
#: 有默认值）。老那一行代表"部署方那份配置"，迁移后的去处是 `owner = ''`
#: （`settings.store.INSTANCE_OWNER` / `auth.owner.NO_OWNER` 同一个值）。
#:
#: **为什么不做成"NOT NULL 就填空串"的通则**：那样 `mindmaps.id` / `users.username`
#: 这类列缺值时会被填成空串，插进去一串空主键——不报错、但数据已经不是原样了。
#: 宁可写死这一条并让它显眼：新增这种列时，人会看见这张表然后决定填什么。
_NOT_NULL_FILLS: dict[tuple[str, str], Any] = {
    ("model_settings", "owner"): "",
}


class MigrationError(RuntimeError):
    """迁移不能继续了。消息里必须写清"下一步该做什么"。"""


# ---------------------------------------------------------------- 参数


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="把迁移前的 SQLite 库一次性导入 PostgreSQL（幂等，可重跑）")
    parser.add_argument(
        "--source", type=Path, default=DEFAULT_SOURCE,
        help=f"迁移前的 SQLite 库（缺省 {DEFAULT_SOURCE}）")
    parser.add_argument(
        "--dsn", default="",
        help="目标连接串；缺省依次取 MINDMAP_PG_DSN、"
             f"{DEFAULT_DSN}")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="只读老库、报每张表会搬多少行；不建表、不写库")
    parser.add_argument(
        "--skip-schema", action="store_true",
        help="假定目标库的表已经建好（不调 init_schema）")
    return parser.parse_args(argv)


def _resolve_dsn(explicit: str) -> str:
    """显式 --dsn > 环境变量 > 本机默认。与 `app/db/pool.dsn()` 同一套优先级。"""
    if explicit.strip():
        return explicit.strip()
    env = (os.getenv("MINDMAP_PG_DSN", "") or "").strip()
    return env or DEFAULT_DSN


# ---------------------------------------------------------------- 老库读取
#
# 这一节是"读快照"的唯一入口。它只依赖 sqlite3，不 import 应用，所以 --dry-run
# 可以在任何一台机器上跑（哪怕那台机器上没装 psycopg）。


def _quote(name: str) -> str:
    """SQL 标识符加引号。`library_chunks."end"` 是保留字，不加引号 PG 直接语法错。"""
    return '"' + name.replace('"', '""') + '"'


def _legacy_connect(source: Path) -> sqlite3.Connection:
    if not source.exists():
        raise MigrationError(
            f"老库不存在：{source}\n"
            "  它是迁移前的快照，通常在主 checkout 的 backend/data/mindmaps.db。"
            "worktree 里没有这份文件，用 --source 指过去。")
    if not source.is_file():
        raise MigrationError(f"--source 不是文件：{source}")
    # `mode=ro`：这个脚本对老库**只读**。用 URI 打开是刻意的——旧库要原样留着当
    # 回滚点，任何一次误写都会让"回滚"变成"回滚到一个改过的文件"。
    conn = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _legacy_tables(conn: sqlite3.Connection) -> list[str]:
    """老库里有哪些业务表（按名称排序，顺序由调用方按外键关系重排）。"""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall()
    return [row["name"] for row in rows if row["name"] not in _SQLITE_INTERNAL]


def _legacy_columns(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    """老表的列。`PRAGMA` 不支持参数占位符，只能拼表名——表名来自 `sqlite_master`，
    不是用户输入，且这里已按标识符转义。"""
    return conn.execute(f"PRAGMA table_info({_quote(table)})").fetchall()


def _legacy_count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {_quote(table)}").fetchone()[0])


def _legacy_pk(conn: sqlite3.Connection, table: str) -> list[str]:
    """主键列（按 `pk` 序号排，复合主键的顺序有意义）。"""
    rows = [row for row in _legacy_columns(conn, table) if row["pk"]]
    return [row["name"] for row in sorted(rows, key=lambda row: row["pk"])]


# ---------------------------------------------------------------- 目标库


def _pg_connect(dsn: str):
    """取一条目标库连接。

    **先设环境变量再取池**：`app.db.pool.dsn()` 每次读 `MINDMAP_PG_DSN`，而
    `get_pool()` 在连接串变化时会重建池。这样脚本不必把 dsn 透传进应用层，
    也就不会出现"脚本连 A 库、应用模块连 B 库"的分叉。
    """
    from app.db import pool as pool_module
    from app.db.conn import Conn

    os.environ[pool_module.DSN_ENV] = dsn
    pool = pool_module.get_pool()
    return Conn(pool.getconn(), pool)


def _target_columns(conn, table: str) -> list[dict[str, Any]]:
    """目标表的列（含是否可空、列默认值）。行是 `dict_row`，见 `app/db/pool.py`。"""
    rows = conn.execute(
        "SELECT column_name, is_nullable, column_default "
        "FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = ? "
        "ORDER BY ordinal_position", (table,)).fetchall()
    return [
        {
            "name": row["column_name"],
            "nullable": row["is_nullable"] == "YES",
            "default": row["column_default"],
        }
        for row in rows
    ]


def _target_tables(conn) -> set[str]:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'").fetchall()
    return {row["table_name"] for row in rows}


def _migration_order(legacy_tables: list[str]) -> list[str]:
    """按"父表在前"排。直接借 `schema.TABLES_IN_CLEAR_ORDER`（它是**反序**：子表
    在前），反过来就是可插入顺序——`library_chunks.source_id` 有外键，顺序错了会
    撞约束。两处表清单不再各写一份，将来加表只改 `schema.py`。"""
    from app.db.schema import TABLES_IN_CLEAR_ORDER

    known = [table for table in reversed(TABLES_IN_CLEAR_ORDER) if table in legacy_tables]
    rest = [table for table in legacy_tables if table not in set(TABLES_IN_CLEAR_ORDER)]
    return known + sorted(rest)


def _table_defaults(conn, carried: list[str], target: list[dict[str, Any]],
                    legacy_nullable: dict[str, bool]) -> dict[str, Any]:
    """目标表 NOT NULL、而老表允许 NULL 的那几列，缺值时补什么。

    这就是旧 `_copy_legacy_rows` 里那段 `COALESCE` 的理由：`INSERT ... ON CONFLICT
    DO NOTHING` 会把违反 NOT NULL 的整行**静默丢掉**（不是报错），用户看到的是
    "升级完我的配置/密钥不见了"。所以先在 `SELECT` 侧把 NULL 换成目标列的默认值。

    默认值不写死，直接向 PG 问：`information_schema.columns.column_default` 是个
    SQL 表达式（如 `''::text`），`SELECT` 一次就知道该补什么。**跳过 `nextval(`
    的那几个**：那是 bigserial 的 id，求值会白耗一个序列号，而 id 为 NULL 的行
    本来就不该出现在合法快照里——让它如实报错，比悄悄换个 id 好。
    """
    wanted: dict[str, str] = {}
    for column in target:
        name = column["name"]
        if name not in carried or column["nullable"]:
            continue
        if legacy_nullable.get(name, True):
            expr = column["default"] or ""
            if expr and "nextval(" not in expr:
                wanted[name] = expr
    if not wanted:
        return {}
    select = ", ".join(f"{expr} AS {_quote(name)}" for name, expr in wanted.items())
    row = conn.execute(f"SELECT {select}").fetchone()
    return {name: row[name] for name in wanted}


# ---------------------------------------------------------------- 搬运


def _insert_rows(conn, table: str, columns: list[str], rows: list[tuple[Any, ...]]) -> None:
    """分批 `INSERT ... ON CONFLICT DO NOTHING`。

    分批是为了"将来出现大表时内存有上界"，不是为了现在的二十行——活跃库的表都很小，
    一次 500 行只是把上限写出来。

    **不带冲突目标**（`ON CONFLICT DO NOTHING` 而不是 `(id) DO NOTHING`）：要忽略的
    冲突不止主键，还有 `library_sources.content_hash`、`memory_items(owner, text_hash)`、
    `library_chunks(source_id, start, "end", level)` 这些唯一约束。不带目标时 PG 对
    任意唯一冲突都忽略，正好是"重跑不报错"要的语义。

    返回值不用：`executemany` 的 `rowcount` 含被 `DO NOTHING` 忽略的行，当"插入数"
    会虚高。真实数量由 `_verify` 的（目标总数 vs 老库总数）回答——那才是证据。
    """
    names = ", ".join(_quote(name) for name in columns)
    placeholders = ", ".join("?" for _ in columns)
    sql = (f"INSERT INTO {_quote(table)} ({names}) VALUES ({placeholders}) "
           "ON CONFLICT DO NOTHING")
    for start in range(0, len(rows), BATCH):
        conn.executemany(sql, rows[start:start + BATCH])


def _migrate_table(conn, legacy: sqlite3.Connection,
                   table: str) -> dict[str, Any]:
    """搬一张表。返回这一行的报表（老库行数、搬了几列、补了哪些默认值）。

    列集按"目标表也有的列"取交集：老库多出来的历史列（早被删掉的）不搬，目标表
    新增的列（`owner` / `api_key_host` 这类）不在交集里，于是走 PG 的列默认值。
    **列默认值兜不住的那几个**（NOT NULL 又没有 DEFAULT，见 `_NOT_NULL_FILLS`）
    显式给个常量，否则整行会被 `ON CONFLICT DO NOTHING` 静默吞掉。
    """
    legacy_cols = _legacy_columns(legacy, table)
    legacy_names = [column["name"] for column in legacy_cols]
    legacy_nullable = {column["name"]: not column["notnull"] for column in legacy_cols}

    columns = _target_columns(conn, table)
    if not columns:
        raise MigrationError(
            f"目标库里没有表 {table}。--skip-schema 时请确认 schema 已建好；"
            "否则去掉这个开关，让脚本调 init_schema()。")
    target_names = [column["name"] for column in columns]
    carried = [name for name in target_names if name in set(legacy_names)]
    if not carried:
        raise MigrationError(
            f"{table}：老表与目标表没有一列同名，无法判断怎么搬（老表列：{legacy_names}）")

    defaults = _table_defaults(conn, carried, columns, legacy_nullable)

    # 老表里没有、目标表又非要不可的列（NOT NULL 且无列默认值）。只有 `_NOT_NULL_FILLS`
    # 里登记过的能搬；其余直接中止——静默填空串会把主键填成 "" 之类的东西。
    missing = [column["name"] for column in columns
               if not column["nullable"] and not column.get("default")
               and column["name"] not in set(legacy_names)]
    pinned: dict[str, Any] = {}
    unresolved = []
    for name in missing:
        if (table, name) in _NOT_NULL_FILLS:
            pinned[name] = _NOT_NULL_FILLS[(table, name)]
        else:
            unresolved.append(name)
    if unresolved:
        raise MigrationError(
            f"{table}：目标表这几列是 NOT NULL、没有列默认值、老表里也不存在：{unresolved}。"
            "要么给目标列加 DEFAULT（`app/db/schema.py`），要么在 `_NOT_NULL_FILLS` 里"
            "登记这份快照该填的值。")

    select = ", ".join(_quote(name) for name in carried)
    raw_rows = legacy.execute(f"SELECT {select} FROM {_quote(table)}").fetchall()
    rows: list[tuple[Any, ...]] = []
    filled = 0
    for raw in raw_rows:
        values: list[Any] = []
        for name in carried:
            value = raw[name]
            if value is None and name in defaults:
                # 见 `_table_defaults` 的 docstring：NOT NULL 的 NULL 会让整行被
                # `DO NOTHING` 静默吞掉，所以就地补上目标列的默认值。
                value = defaults[name]
                filled += 1
            values.append(value)
        values.extend(pinned[name] for name in pinned)
        rows.append(tuple(values))

    if rows:
        _insert_rows(conn, table, [*carried, *pinned], rows)
    # `vendored` 列 = 目标有、老库没有的列：报表里列出来，方便解释"为什么搬过来的行
    # 比目标表列数少"。
    vendored = [name for name in target_names if name not in set(legacy_names)]
    return {
        "table": table,
        "legacy": _legacy_count(legacy, table),
        "columns": len(carried) + len(pinned),
        "vendored": vendored,
        "filled": filled,
        "pinned": sorted(pinned),
    }


def _fix_sequences(conn) -> list[tuple[str, str, int]]:
    """校正 bigserial 的序列。返回 [(表, 序列, 下一次会用到的值)]。

    搬行时 `edit_events.id` / `library_chunks.id` 是**显式带 id 插入**的（老库的
    id 要保留，否则 `edit_events` 里 `map_id` 之外的引用关系会断），而 `bigserial`
    的序列不会因为显式插入自动前进。不校正的后果：切换后第一条编辑事件拿到 id=1，
    撞上搬过来的 id=1 → `UniqueViolation`。

    表名与序列名都不在这里写死：`schema.SEQUENCES` 说"哪几张表有序列"，具体序列名
    由 `pg_get_serial_sequence()` 向 PG 要（它返回带 schema 的全名，也顺手验证了
    这个序列真的挂在那张表的 id 上）。
    """
    from app.db.schema import SEQUENCES

    suffix = "_id_seq"
    fixed: list[tuple[str, str, int]] = []
    for sequence in SEQUENCES:
        if not sequence.endswith(suffix):
            raise MigrationError(
                f"{sequence} 不符合 '<表>_id_seq' 的命名，无法从序列名反推表名。"
                "改名的同时要改这里。")
        table = sequence[: -len(suffix)]
        row = conn.execute(
            "SELECT pg_get_serial_sequence(?, 'id') AS seq", (table,)).fetchone()
        qualified = row["seq"] if row else None
        if not qualified:
            raise MigrationError(
                f"{table}.id 上没有找到 serial 序列（{sequence}）。"
                "schema 可能与 `app/db/schema.py` 不一致。")
        top = conn.execute(f"SELECT COALESCE(MAX(id), 0) AS top FROM {_quote(table)}").fetchone()
        next_value = int(top["top"]) + 1
        # `false` = 这个值还没被用过，下一次 nextval() 就返回它。
        conn.execute("SELECT setval(?, ?, false)", (qualified, next_value))
        fixed.append((table, qualified, next_value))
    conn.commit()
    return fixed


# ---------------------------------------------------------------- 校验


def _legacy_staging_tables(legacy: sqlite3.Connection,
                           tables: list[str]) -> list[tuple[str, int]]:
    """快照里"两段式迁移搬到一半"的中间表：`<表>_legacy_v1`，返回 [(表, 行数)]。

    `settings.store` / `prefs.style` / `mindmaps.repository` 的老做法是"老表改名成
    `_legacy_v1` → 建新表 → 搬行 → 删老表"，每一步单独提交。如果进程在"改名完、
    搬行前"被杀掉，快照里就会留下一张 `model_settings_legacy_v1` 之类**还装着旧数据**
    的表，而正式表是空的。

    本脚本不搬这些表（它们是运行时迁移的中间态，不是 schema 的一部分），但**必须
    报出来**：静默跳过等于把用户上次没搬完的那份配置丢了，而"丢了"和"本来就没有"
    长得一样——那正是这次迁移最不该有的失败方式。所以调用方拿到非空结果时让退出码
    非 0，由人决定先跑一次旧版本把迁移补完，还是手工合并。
    """
    out: list[tuple[str, int]] = []
    for table in tables:
        if table.endswith("_legacy_v1"):
            rows = _legacy_count(legacy, table)
            if rows:
                out.append((table, rows))
    return out


def _verify(conn, legacy: sqlite3.Connection, tables: list[str]) -> list[str]:
    """逐表比对：行数、主键集合、以及导图 `content` 的逐字节相等。

    三类不一致分开报，因为它们的排查路径不同：
      - **行数不等** → 目标库里有迁移之外的行，或某张表被跳过；
      - **主键缺失** → 那一行没搬进来（多半是被 `DO NOTHING` 当成冲突忽略了，
        而老库里那行的唯一键与目标已有行相同——这种情况必须人看一眼）；
      - **content 不等** → 编码/截断问题，最严重。
    """
    problems: list[str] = []
    for table in tables:
        expected = _legacy_count(legacy, table)
        actual = int(conn.execute(f"SELECT COUNT(*) AS n FROM {_quote(table)}").fetchone()["n"])
        if expected != actual:
            problems.append(
                f"{table}：老库 {expected} 行，目标 {actual} 行"
                f"（差 {actual - expected:+d}；目标库若不是空的，先确认这多出来的行是哪来的）")

        pk = _shared_pk(conn, legacy, table)
        if pk:
            select = ", ".join(_quote(name) for name in pk)
            legacy_keys = {tuple(row[name] for name in pk)
                           for row in legacy.execute(
                               f"SELECT {select} FROM {_quote(table)}").fetchall()}
            target_keys = {tuple(row[name] for name in pk)
                           for row in conn.execute(
                               f"SELECT {select} FROM {_quote(table)}").fetchall()}
            missing = legacy_keys - target_keys
            if missing:
                sample = sorted(missing, key=repr)[:3]
                problems.append(
                    f"{table}：目标库缺少 {len(missing)} 个主键（例如 {sample}）——"
                    "多半是那行的唯一键与目标已有行冲突，被 DO NOTHING 忽略了，需要人工确认")

        if table == CONTENT_CHECK_TABLE:
            problems.extend(_verify_content(conn, legacy))
    return problems


def _shared_pk(conn, legacy: sqlite3.Connection, table: str) -> list[str]:
    """拿**两张表都有**的主键列来比行。

    直接用老库主键是不行的：`model_settings` 的老主键是 `id`（单行表时代），新表
    主键是 `owner`——老库主键在新表里不存在，`SELECT "id"` 会直接 `UndefinedColumn`。
    取交集之后剩两种情况，都能用：
      - 主键没变（`mindmaps.id` / `users.id` / `library_sources.id`）→ 按老主键比，
        这正是"那一行到底进没进来"最直接的证据；
      - 主键变了（只有 `model_settings`）→ 交集为空 → 不比键集，只比行数。这一张表
        在活跃库里有 1–2 行，行数相等加上文里的逐列 NOT NULL 检查已经够。
    """
    target_names = {column["name"] for column in _target_columns(conn, table)}
    pk = [name for name in _legacy_pk(legacy, table) if name in target_names]
    return pk if len(pk) == len(_legacy_pk(legacy, table)) else []


def _verify_content(conn, legacy: sqlite3.Connection) -> list[str]:
    """导图正文**逐字节**比对（不只比长度）。

    `content` 是 JSON 文本，一个字节的差异就等于用户的图变了。逐行比全部（不是抽
    一行）：这张表在活跃库里只有个位数行，全比不花什么钱，而"抽样通过"从来不是
    数据迁移该有的证据。
    """
    problems: list[str] = []
    legacy_rows = legacy.execute(
        f"SELECT id, {_quote('content')} FROM {_quote(CONTENT_CHECK_TABLE)} "
        "ORDER BY id").fetchall()
    for row in legacy_rows:
        target = conn.execute(
            f"SELECT {_quote('content')} FROM {_quote(CONTENT_CHECK_TABLE)} WHERE id = ?",
            (row["id"],)).fetchone()
        if target is None:
            problems.append(f"{CONTENT_CHECK_TABLE}：目标库缺少 id={row['id']}")
            continue
        left = (row["content"] or "").encode("utf-8")
        right = (target["content"] or "").encode("utf-8")
        if left != right:
            problems.append(
                f"{CONTENT_CHECK_TABLE}：id={row['id']} 的 content 不一致"
                f"（老库 {len(left)} B / 目标 {len(right)} B）")
    return problems


# ---------------------------------------------------------------- 报表


def _print_counts(title: str, rows: list[dict[str, Any]], *, dry: bool) -> None:
    """打印逐表报表。`dry=True` 时没有"目标"列（那一次导压根没连目标库）。"""
    print()
    print(title)
    print("-" * 78)
    header = f"{'表':<20}{'老库':>6}{'搬入列':>8}"
    if not dry:
        header += f"{'目标':>7}"
    print(header + "  备注")
    print("-" * 78)
    total_legacy = 0
    for row in rows:
        total_legacy += row["legacy"]
        note = []
        if row.get("filled"):
            note.append(f"补默认 {row['filled']} 处")
        if row.get("pinned"):
            note.append("老表缺列按 `_NOT_NULL_FILLS` 填 " + ",".join(row["pinned"]))
        if row.get("vendored"):
            note.append("目标新增列 " + ",".join(row["vendored"]))
        line = f"{row['table']:<20}{row['legacy']:>6}{row['columns']:>8}"
        if not dry:
            line += f"{row.get('target', ''):>7}"
        print(line + "  " + "；".join(note))
    print("-" * 78)
    print(f"{'合计':<20}{total_legacy:>6}{'':>8}" + ("" if dry else f"{'':>7}"))


def _run_dry(legacy: sqlite3.Connection, legacy_tables: list[str],
             order: list[str], source: Path) -> int:
    """只读老库。不建表、不连目标库、不写任何东西。"""
    rows = []
    for table in order:
        rows.append({
            "table": table,
            "legacy": _legacy_count(legacy, table),
            "columns": len(_legacy_columns(legacy, table)),
        })
    print(f"老库：{source}")
    _print_counts("--dry-run：各表会搬多少行（没有连目标库，也没建表）", rows, dry=True)
    skipped = [table for table in legacy_tables if table not in set(order)]
    if skipped:
        print(f"\n跳过 {len(skipped)} 张不在目标 schema 里的表（历史遗留，不搬）：{skipped}")
    print(f"\n合计 {sum(row['legacy'] for row in rows)} 行来自 {len(rows)} 张表。")
    print("去掉 --dry-run 就会真导入；导入是幂等的，重跑只补缺的行。")
    return 0


# ---------------------------------------------------------------- 主流程


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    dsn = _resolve_dsn(args.dsn)
    source = Path(args.source).resolve()

    if os.getenv("MINDMAP_DB_PATH"):
        print("注意：MINDMAP_DB_PATH 已经废除（应用不再读它）。老库请用 --source 指定："
              f"{os.environ['MINDMAP_DB_PATH']} 这个值不会影响本脚本。\n")

    problems: list[str] = []
    with closing(_legacy_connect(source)) as legacy:
        legacy_tables = _legacy_tables(legacy)
        if not legacy_tables:
            raise MigrationError(f"{source} 里一张业务表都没有，不像迁移前的快照。")
        order = _migration_order(legacy_tables)
        if args.dry_run:
            return _run_dry(legacy, legacy_tables, order, source)

        print(f"老库：{source}")
        print(f"目标：{_mask_dsn(dsn)}")
        from app.db import pool as pool_module

        conn = _pg_connect(dsn)
        try:
            if args.skip_schema:
                print("schema：跳过了 init_schema（--skip-schema）")
            else:
                from app.db.schema import init_schema
                init_schema(conn)
                print("schema：已就绪（init_schema 幂等）")

            target_tables = _target_tables(conn)
            if not target_tables:
                raise MigrationError(
                    "目标库里一张表都没有。先跑一次应用（lifespan 会建表），"
                    "或者去掉 --skip-schema。")

            report: list[dict[str, Any]] = []
            for table in order:
                if table not in target_tables:
                    print(f"跳过 {table}：目标 schema 里没有这张表")
                    continue
                row = _migrate_table(conn, legacy, table)
                row["target"] = int(conn.execute(
                    f"SELECT COUNT(*) AS n FROM {_quote(table)}").fetchone()["n"])
                report.append(row)
                print(f"  {table:<20} 老库 {row['legacy']:>4} 行 → 目标 {row['target']:>4} 行"
                      f"（{row['columns']} 列）")
            conn.commit()

            unknown = [table for table in legacy_tables
                       if table not in set(order) or table not in target_tables]
            if unknown:
                print(f"\n跳过 {len(unknown)} 张目标 schema 里没有的表：{unknown}")

            fixed = _fix_sequences(conn)
            print("\n序列校正：")
            for table, sequence, next_value in fixed:
                print(f"  {table:<20} {sequence:<28} 下一次 nextval() = {next_value}")

            problems = _verify(conn, legacy, [row["table"] for row in report])
            _print_counts("导入结果（老库 vs 目标）", report, dry=False)
            print(f"content 逐字节比对：{CONTENT_CHECK_TABLE} 全部 "
                  f"{_legacy_count(legacy, CONTENT_CHECK_TABLE)} 行")
        finally:
            conn.close()
            # 池是进程级的（`app/db/pool.py`），不关的话 psycopg_pool 的后台线程
            # 会在解释器退出时被强行终止，stderr 上留下一串 "couldn't stop thread"。
            # 那几行不是错误，但会让人以为迁移出了问题——而迁移脚本的输出是要被
            # 贴进工单当证据的，不该混进无关噪音。
            pool_module.close_pool()

    if problems:
        print("\n不一致：")
        for problem in problems:
            print(f"  · {problem}")
        print("\n已搬的行都已提交。修掉上面几条后直接重跑本脚本即可续上——它是幂等的。")
        return 1
    print("\n全部一致：行数、主键集合与导图正文逐字节相等。")
    return 0


def _mask_dsn(dsn: str) -> str:
    """打印用：抹掉密码。迁移日志会被贴进工单/聊天，连接串里的密码不该跟着走。"""
    if "://" not in dsn:
        return dsn
    scheme, rest = dsn.split("://", 1)
    if "@" not in rest:
        return dsn
    credentials, host = rest.rsplit("@", 1)
    if ":" in credentials:
        user = credentials.split(":", 1)[0]
        credentials = f"{user}:***"
    return f"{scheme}://{credentials}@{host}"


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MigrationError as error:
        print(f"\n迁移中止：{error}", file=sys.stderr)
        raise SystemExit(2) from error
