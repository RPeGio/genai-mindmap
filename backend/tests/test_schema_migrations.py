"""`app/db/schema.py` 的那几件事：幂等建表、补列、清库、时间戳形状。

**这个文件原来是另一副样子**：它锁的是"SQLite 老库就地迁移"——`_ensure_columns`
补列的竞态、`model_settings` / `user_prefs` 从单行表搬成多 owner 表、半迁移状态的
续搬、以及"搬完必须 commit 否则跨连接看不见"。换到 PostgreSQL 之后那些前提**全部
消失**：

- 就地迁移那段代码被删了。`init_schema()` 建出来的就是终态表，老库数据由一次性
  导入脚本搬成终态形状（见 `docs/PG-MIGRATION-PLAN.md` D10）。没有"半迁移状态"
  可言——导入是一次性的、失败就整体重来。
- `PRAGMA table_info` + `ALTER` + 吞 `duplicate column name` 那一整套竞态处理也不需要
  了：PG 的 `ADD COLUMN IF NOT EXISTS` 把竞态收进了语句本身。
- "跨连接读得到 == commit 过"这条判据仍然成立，而且在新形状下更值得钉——因为
  `Conn.__exit__` 会 commit、`close()` 会 rollback，两者的顺序一错就会静默丢写入。

所以这里的用例换成了新契约：**幂等性**（跑两遍与跑一遍等价）、**补列**（缺的补上、
已有的不动）、**时间戳格式**（与旧 SQLite 那条 strftime 逐字节同形，因为全仓的
时间比较都靠字典序）、**清库**（17 张表 + 自增序列，每测试复用它）。
"""
from __future__ import annotations

import re
import unittest

from app.db import schema
from app.db.conn import Conn
from app.db.errors import DatabaseError
from app.db.pool import get_pool
from app.mindmaps import repository

#: `nowstamp()` 该产出的形状：定宽 UTC、三位毫秒、以 `Z` 收尾。`` 与 SQLite 的
#: `strftime('%Y-%m-%dT%H:%M:%fZ', 'now')` 逐字节同形——这是全仓时间比较按字典序
#: 做的前提，形状一变，"谁更晚"的比较就会在边界上错。
_STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def _conn() -> Conn:
    """新开一条连接。用完必须 `close()`（它会 rollback 未提交事务再归还池）。"""
    return Conn(get_pool().getconn(), get_pool())


def _columns(table: str) -> set[str]:
    """某张表在**当前库**里的列名集合。"""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = ?",
            (table,)).fetchall()
    finally:
        conn.close()
    return {str(row["column_name"]) for row in rows}


def _tables() -> set[str]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema()").fetchall()
    finally:
        conn.close()
    return {str(row["table_name"]) for row in rows}


class InitSchemaTest(unittest.TestCase):
    def test_init_schema_is_idempotent(self):
        """再跑一遍不抛、不丢数据。启动期每条连接都会走到它附近，重复是常态。"""
        conn = _conn()
        try:
            conn.execute("INSERT INTO mindmaps (id, version, content, updated_at) "
                         "VALUES ('keep-me', 1, '{}', nowstamp())")
            conn.commit()
        finally:
            conn.close()

        schema.init_schema()  # 第二遍

        conn = _conn()
        try:
            row = conn.execute(
                "SELECT id FROM mindmaps WHERE id = 'keep-me'").fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, "幂等建表把已有的行冲掉了")
        self.assertEqual(row["id"], "keep-me")

    def test_every_declared_table_exists(self):
        conn = _conn()
        try:
            conn.execute("SELECT 1")
        finally:
            conn.close()
        present = _tables()
        # `TABLES_IN_CLEAR_ORDER` 是"清库要清哪些"的清单，它就是全表清单。
        missing = set(schema.TABLES_IN_CLEAR_ORDER) - present
        self.assertEqual(missing, set(), f"这些表没建出来：{sorted(missing)}")

    def test_the_schema_version_is_recorded(self):
        conn = _conn()
        try:
            row = conn.execute(
                "SELECT value FROM schema_migrations WHERE key = 'version'").fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, "schema_migrations 里没有版本行")
        self.assertEqual(str(row["value"]), schema.SCHEMA_VERSION)


class EnsureColumnsTest(unittest.TestCase):
    """补列：缺的补上、已有的不动、跑两遍等价。

    原来是拿一个 `_StubConnection` 模拟 PRAGMA + ALTER 测"并发补列撞名要不要吞"。
    新实现是 `ADD COLUMN IF NOT EXISTS` 一句话，没有"撞名"这个分支可测——要测的是
    **幂等**（第二遍不抛），以及**已有的列值不被清掉**。
    """

    _TABLE = "probe_columns"

    def setUp(self):
        conn = _conn()
        try:
            conn.execute(f"DROP TABLE IF EXISTS {self._TABLE}")
            conn.execute(f"CREATE TABLE {self._TABLE} (id TEXT PRIMARY KEY)")
            conn.commit()
        finally:
            conn.close()
        self.addCleanup(self._drop)

    def _drop(self) -> None:
        conn = _conn()
        try:
            conn.execute(f"DROP TABLE IF EXISTS {self._TABLE}")
            conn.commit()
        finally:
            conn.close()

    def test_a_missing_column_is_added(self):
        conn = _conn()
        try:
            schema.ensure_columns(conn, self._TABLE, {"model": "TEXT"})
            conn.commit()
        finally:
            conn.close()
        self.assertIn("model", _columns(self._TABLE))

    def test_running_twice_is_harmless(self):
        """第二遍不能再抛。原来这里靠吞 `duplicate column name` 才不炸。"""
        conn = _conn()
        try:
            schema.ensure_columns(conn, self._TABLE, {"model": "TEXT"})
            schema.ensure_columns(conn, self._TABLE, {"model": "TEXT"})
            conn.commit()
        finally:
            conn.close()
        self.assertIn("model", _columns(self._TABLE))

    def test_an_existing_column_is_not_touched(self):
        conn = _conn()
        try:
            conn.execute(f"INSERT INTO {self._TABLE} (id) VALUES ('r1')")
            schema.ensure_columns(conn, self._TABLE, {"id": "TEXT"})
            conn.commit()
            row = conn.execute(
                f"SELECT id FROM {self._TABLE} WHERE id = 'r1'").fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, "补列把已有的行冲掉了")

    def test_drop_columns_is_also_idempotent(self):
        conn = _conn()
        try:
            schema.ensure_columns(conn, self._TABLE, {"model": "TEXT"})
            schema.drop_columns(conn, self._TABLE, ("model",))
            schema.drop_columns(conn, self._TABLE, ("model",))
            conn.commit()
        finally:
            conn.close()
        self.assertNotIn("model", _columns(self._TABLE))


class NowstampTest(unittest.TestCase):
    """时间戳形状。全仓的时间比较都按字符串字典序，格式是硬契约。"""

    def _stamp(self, offset: float | None = None) -> str:
        conn = _conn()
        try:
            if offset is None:
                row = conn.execute("SELECT nowstamp() AS stamp").fetchone()
            else:
                row = conn.execute(
                    "SELECT nowstamp(?) AS stamp", (offset,)).fetchone()
        finally:
            conn.close()
        return str(row["stamp"])

    def test_shape_matches_the_old_sqlite_format(self):
        self.assertRegex(self._stamp(), _STAMP_RE)

    def test_an_offset_moves_the_stamp_backwards_in_lexicographic_order(self):
        """`nowstamp(-86400)` 是一天前，**而且字典序也确实是更小**。

        这条比"格式对"更本质：`sweep_abandoned` / `library.prune` / `edits` 的时间窗
        全靠 `updated_at < nowstamp(-N)` 这种比较，格式对了但顺序反了照样错。
        """
        now = self._stamp()
        before = self._stamp(-86400)
        self.assertRegex(before, _STAMP_RE)
        self.assertLess(before, now)

    def test_two_calls_in_the_same_statement_share_one_timestamp(self):
        """同一条语句里的两个 `nowstamp()` 相等——和 SQLite 一样。

        理由不是"事务开始时刻"（那是 `now()`，见下一条用例），而是
        `statement_timestamp()` 在**一条语句内**是常量：SQLite 的
        `strftime(...,'now')` 也是这样。成对写 `created_at` / `updated_at` 时两者
        必须一致，否则"什么时候建的"与"什么时候改的"会差几毫秒，而排序键用的正是后者。
        """
        conn = _conn()
        try:
            row = conn.execute(
                "SELECT nowstamp() AS a, nowstamp() AS b").fetchone()
        finally:
            conn.close()
        self.assertEqual(str(row["a"]), str(row["b"]))

    def test_nowstamp_is_a_statement_clock_not_a_transaction_clock(self):
        """同一事务里的两条**语句**必须拿到不同的时间戳。

        这条钉的是 `_NOWSTAMP_SQL` 里那个函数的选择。用 `now()`（=
        `transaction_timestamp()`）时，一个事务内所有写入的时间戳**逐字节相等**，
        而 SQLite 的 `strftime(...,'now')` 是每条语句重新求值——`ingest_many` 一个
        事务里入库多份材料，靠"按新旧淘汰"的 `prune` 就此退化成按 id 排。

        用 `clock_timestamp()` 也不对：它在**同一条语句的多行之间**就会变，而
        SQLite 的 `now` 在一条语句内是常量（见上一条）。`statement_timestamp()`
        是唯一两头都对的那个。
        """
        conn = _conn()
        try:
            first = str(conn.execute("SELECT nowstamp() AS s").fetchone()["s"])
            conn.execute("SELECT pg_sleep(0.02)")
            second = str(conn.execute("SELECT nowstamp() AS s").fetchone()["s"])
        finally:
            conn.close()
        self.assertNotEqual(
            first, second,
            "nowstamp() 退化成事务时间戳了（见 app/db/schema.py 的 _NOWSTAMP_SQL）")

    def test_every_row_of_one_insert_shares_the_statement_stamp(self):
        """一条 `INSERT ... SELECT` 写多行时，行与行之间共享同一个时间戳。

        `_ingest_one` 的 `executemany` 与 `sweep_abandoned` 的批量 `UPDATE` 都依赖
        这一点：同一批操作在语义上是"一次"，不该因为行数多少而产生几十个不同时刻。
        用 `clock_timestamp()` 会破坏它（行行不同）。
        """
        conn = _conn()
        try:
            conn.execute("DROP TABLE IF EXISTS _stamp_rows")
            conn.execute("CREATE TABLE _stamp_rows (n int, at text)")
            conn.execute(
                "INSERT INTO _stamp_rows SELECT g, nowstamp() "
                "FROM generate_series(1, 50) g")
            row = conn.execute(
                "SELECT count(DISTINCT at) AS kinds FROM _stamp_rows").fetchone()
            conn.execute("DROP TABLE _stamp_rows")
        finally:
            conn.close()
        self.assertEqual(int(row["kinds"]), 1)


class ClearAllTest(unittest.TestCase):
    """`clear_all()` 是每个测试函数的前置动作（`conftest.fresh_db`）。

    它错了不会报错，只会让用例之间互相污染——症状是"单跑绿、全跑红"，那种失败
    最难查。所以这里钉两件事：真的清空了，以及自增序列也回到 1。
    """

    def test_it_empties_every_table_in_the_clear_list(self):
        conn = _conn()
        try:
            conn.execute("INSERT INTO mindmaps (id, version, content, updated_at) "
                         "VALUES ('to-be-cleared', 1, '{}', nowstamp())")
            conn.commit()
        finally:
            conn.close()

        schema.clear_all()

        conn = _conn()
        try:
            for table in schema.TABLES_IN_CLEAR_ORDER:
                row = conn.execute(
                    f"SELECT COUNT(*) AS total FROM {table}").fetchone()
                self.assertEqual(int(row["total"]), 0, f"{table} 没被清空")
        finally:
            conn.close()

    def test_it_restarts_the_sequences(self):
        """`edit_events.id` 是 `bigserial`，而用例会断言"第一条 id 是 1"。

        不重置序列的话，第二个用例拿到的 id 接着上一个涨——`test_edit_events.py`
        里 `assert rows[1]["id"] == 2` 那条会变成跨用例随机红。
        """
        conn = _conn()
        try:
            # `actor` / `kind` 是 NOT NULL 且没有默认值：插值必须给全，否则撞
            # `NotNullViolation`。**这条不是测试写法问题**——`edit_events` 的
            # 写入方 `repository.record_edit_event()` 一直在给这几列。
            conn.execute(
                "INSERT INTO edit_events (map_id, actor, kind, at, recorded_at) "
                "VALUES ('m', 'user', 'move', nowstamp(), nowstamp())")
            conn.commit()
        finally:
            conn.close()

        schema.clear_all()

        conn = _conn()
        try:
            row = conn.execute(
                "INSERT INTO edit_events (map_id, actor, kind, at, recorded_at) "
                "VALUES ('m2', 'user', 'move', nowstamp(), nowstamp()) "
                "RETURNING id").fetchone()
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(int(row["id"]), 1, "自增序列没有回到 1")


class CommitSemanticsTest(unittest.TestCase):
    """`Conn` 的两条收尾路径：`with` 提交、`close()` 回滚。

    这是 shim 最容易错的地方，而错了的表现是"写进去了"和"没写进去"在进程内长得
    一样——只有**换一条连接**再读才分得开。
    """

    def test_a_with_block_commits(self):
        with _conn() as conn:
            conn.execute("INSERT INTO mindmaps (id, version, content, updated_at) "
                         "VALUES ('committed', 1, '{}', nowstamp())")
        # 出了 with，连接已归还池；读的是另一个会话。
        conn = _conn()
        try:
            row = conn.execute(
                "SELECT id FROM mindmaps WHERE id = 'committed'").fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, "with 块没有提交")

    def test_close_without_commit_rolls_back(self):
        """不提交就关 = 回滚。这是 SQLite 语义，也是仓库里 `closing(conn)` 全部
        依赖它的原因。psycopg 原生的 `close()` 也回滚，但 `with conn:` 会**关连接**，
        所以 shim 必须自己实现这一层。
        """
        conn = _conn()
        conn.execute("INSERT INTO mindmaps (id, version, content, updated_at) "
                     "VALUES ('rolled-back', 1, '{}', nowstamp())")
        conn.close()  # 没有 commit

        conn = _conn()
        try:
            row = conn.execute(
                "SELECT id FROM mindmaps WHERE id = 'rolled-back'").fetchone()
        finally:
            conn.close()
        self.assertIsNone(row, "没提交的写入居然落库了")

    def test_a_failed_statement_does_not_poison_the_transaction(self):
        """**每条语句包 SAVEPOINT 的唯一理由。**

        现有代码十几处依赖"吞掉一条语句的错误、接着用同一条连接"（典型是
        `prefs/profile.py` 串行取三张表的样本，任一张失败只 warning 继续下一张）。
        psycopg 的原生行为是"语句一错整个事务进 aborted 态，后续全部拒绝执行"，
        那会把"少一份样本"放大成"整个画像层失效"。
        """
        with _conn() as conn:
            with self.assertRaises(DatabaseError):
                conn.execute("SELECT * FROM table_that_does_not_exist")
            # 同一条连接、同一个事务里继续跑——不该抛 InFailedSqlTransaction。
            row = conn.execute("SELECT 1 AS one").fetchone()
        self.assertEqual(int(row["one"]), 1)


class LegacyAdoptionTest(unittest.TestCase):
    """`_adopt_legacy` 是**保留**下来的那部分：它不是 schema 迁移，是业务认领。

    三处运行期就地迁移（settings / prefs / style）被删掉了，但"老库无主行归部署方"
    这条业务规则还在。`owner = ''` 的行在新库里不会自然出现（导入脚本会写真实
    owner），所以它在新库上是零成本的空转——**空转也要验证它不炸**。
    """

    def test_style_adopt_legacy_is_a_no_op_on_an_empty_table(self):
        from app.prefs import style

        style._adopt_legacy()  # 不该抛

        conn = _conn()
        try:
            row = conn.execute(
                f"SELECT COUNT(*) AS total FROM {style.STYLE_TABLE}").fetchone()
        finally:
            conn.close()
        self.assertEqual(int(row["total"]), 0)

    def test_prefs_adopt_legacy_is_a_no_op_on_an_empty_table(self):
        from app.prefs import service

        service._adopt_legacy()  # 不该抛


class DbPathAliasTest(unittest.TestCase):
    """`repository.db_path()` 还在，但返回的是 DSN 而不是文件路径。

    它被两处当**稳定的实例标识**用：`auth/store.py` 的 cookie 签名密钥、
    `settings/store.py` 的缓存键。返回 DSN 是有意的——换库等于换实例，旧会话全部
    失效是预期行为（导入脚本不动这些），而不是 bug。
    """

    def test_it_returns_the_dsn_not_a_file_path(self):
        text = repository.db_path()
        self.assertTrue(text.startswith("postgresql://"), text)

    def test_it_agrees_with_the_pool(self):
        from app.db.pool import dsn

        self.assertEqual(repository.db_path(), dsn())


if __name__ == "__main__":
    unittest.main()
