"""`Conn`：一个穿 `sqlite3` 外衣的 psycopg 连接。

调用点写的是 190 处 `conn.execute("... WHERE id = ?", (x,))`、79 处 `closing(...)`、
40 处 `with closing(conn), conn:`。这些写法在 SQLite 上是对的，在 psycopg 上各有
一处会炸的差异。`Conn` 把四处差异收进这一个类，让那 190 处一行不改。

## 一、`?` 占位符

psycopg 只认 `%s`。逐文件手改 263 处 `?` 是最容易出错的一条路：错一处就是运行时
参数错位，而参数错位通常不报错——它只是把 A 的值写进 B 的列。

`execute` 里按 params 的类型决定翻译方式（**这三条规则是有先后的，别合并**）：

- `params is None`：SQL 原样传下去。psycopg 在 params 为 `None` 时**根本不解析
  占位符**，所以 SQL 里的 `%`（比如 `to_char(..., 'YYYY%')`）是安全的。
- `params` 是映射（`dict`）：SQL 原样传下去，由调用方直接写 `%(name)s`。这种写法
  只出现在被改写成单条语句的那几处（`runs.claim` / `quota.consume`）——那里 `%`
  是占位符语法本身，转义会把它们拆坏。
- 其余（元组 / 列表）：**先**把 `%` 转义成 `%%`，**再**把 `?` 换成 `%s`。顺序不能反：
  反了就会把刚生成的 `%s` 再转义成 `%%s`。先转义 `%` 也顺带救活了那些还没改完的
  `strftime('%Y-%m-%dT%H:%M:%fZ','now')` 常量——它们的 `%` 不会被当成占位符。

## 二、每条语句包一层 SAVEPOINT（这是最不显然的一处）

SQLite 里"这条语句失败、下一条继续用同一条连接"是正常用法。现有代码有十几处依赖
它，最典型的是 `prefs/profile.py` 的三张表串行取样本：任一张失败只 warning，接着
读下一张。

PG 不是这样：**一条语句失败后整个事务进入 aborted 状态**，后续任何语句都抛
`InFailedSqlTransaction: current transaction is aborted, commands ignored until end of
transaction block`。实测"try/except 捕获后继续 execute"必炸。

所以每条语句外面套 `SAVEPOINT _shim_sp` / `RELEASE`，失败时 `ROLLBACK TO` 再原样
抛出。代价实测：每条语句 +0.12–0.17ms，一个 6 语句的连接周期从 4.20ms 变 5.20ms
（+24%）。这笔钱必须付——不付，那十几处"吞掉异常继续用"会从"降级"变成"整条链路
崩掉"。

## 三、`with conn:` 的语义是反的

- SQLite：`with conn:` 退出时**提交或回滚事务，但不关连接**。
- psycopg：`with conn:` 退出时**关闭连接**（它等价于 `closing`）。

现有代码有 40 处 `with closing(connect()) as conn, conn:`——内层那个 `with conn:`
在 psycopg 上一退出就把连接关了，后面的语句全是 `the connection is closed`。

`Conn.__exit__` 实现的是 **SQLite 的语义**：成功 `commit()`、异常 `rollback()`，
**绝不 close**。连接的生命周期只由 `close()` 管。

## 四、`close()` 是"归还"不是"销毁"

`psycopg_pool` 的 `pool.connection()` 返回的是上下文管理器不是连接；要拿连接必须
`getconn()` / `putconn()`。直接 `conn.close()` 会把池里那条连接销毁——实测之后池
30 秒超时 `couldn't get a connection after 30.00 sec`。

`Conn.close()` 先回滚未提交事务再 `putconn`。这样现有 8 处 `conn.close()`（限流与
配额的 `finally`）与 79 处 `closing(...)` 全部零改动。

## 五、事务控制语句按首词拦截

旧代码用 SQL 字符串控制事务（`BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK`），共 17 处
真正执行。psycopg 是隐式事务：`BEGIN` 不需要发，`COMMIT`/`ROLLBACK` 用连接方法。

`BEGIN IMMEDIATE` 在 PG 上**没有对应语句**（它是 SQLite 的"立刻拿写锁"）。这里直接
忽略：M2 已经把 `claim()` / `quota.consume()` 改写成单条原子语句，不再需要提前拿锁。
保留忽略而不是抛错，是为了让"还没改到的模块"在迁移中途也能跑起来——但**真正的
互斥判据已经不在 BEGIN 上了，别指望它**。
"""
from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any

import psycopg

logger = logging.getLogger(__name__)

#: 保存点名。每条语句用同一个名字是安全的：每次 execute 在返回前都已经 RELEASE，
#: 不存在嵌套（PG 允许同名 SAVEPOINT，内层的会遮蔽外层）。
_SAVEPOINT = "_shim_sp"

#: `BEGIN` / `COMMIT` / `ROLLBACK` 的首词判据。`ROLLBACK TO SAVEPOINT x` 不算
#: 事务控制（它要真的发给 PG），所以单独判。
_BEGIN_RE = re.compile(r"^\s*BEGIN\b", re.IGNORECASE)
_COMMIT_RE = re.compile(r"^\s*(COMMIT|END)\b", re.IGNORECASE)
_ROLLBACK_RE = re.compile(r"^\s*ROLLBACK\b(?!\s+TO\b)", re.IGNORECASE)
_PRAGMA_RE = re.compile(r"^\s*PRAGMA\b", re.IGNORECASE)
#: 调用方**自己**管保存点（`knowledge/library.py` 的 `SAVEPOINT ingest_one`）时要原样
#: 发给服务器。被 `_shim_sp` 包住会出事：`RELEASE SAVEPOINT _shim_sp` 会连带销毁
#: 在它之后建立的 `ingest_one`（PG 语义：释放一个保存点同时释放其后建立的所有保存点），
#: 于是后面那句 `ROLLBACK TO ingest_one` 报 "no such savepoint"——而这条路径正是
#: "同一批材料里一份失败、只回滚它自己"的实现，坏掉会变成整批静默丢数据。
_SAVEPOINT_CTL_RE = re.compile(r"^\s*(SAVEPOINT|RELEASE|ROLLBACK\s+TO)\b", re.IGNORECASE)


def _translate(sql: str, params: Any) -> str:
    """占位符翻译。规则与顺序见模块 docstring 第一节。"""
    if params is None:
        return sql
    if isinstance(params, Mapping):
        # 命名参数由调用方直接写 `%(name)s`；这里的 `%` 是语法，不能转义。
        return sql
    if "?" not in sql and "%" not in sql:
        return sql
    return sql.replace("%", "%%").replace("?", "%s")


class Conn:
    """psycopg 连接的 SQLite 语义适配层。

    不要直接实例化：用 `app.db.pool.get_pool().getconn()` 或
    `app.mindmaps.repository.connect()`。
    """

    __slots__ = ("_raw", "_pool", "_closed")

    def __init__(self, raw: psycopg.Connection, pool: Any) -> None:
        self._raw = raw
        self._pool = pool
        self._closed = False

    # ------------------------------------------------------------ 上下文

    def __enter__(self) -> "Conn":
        """进入不做事。**提交/回滚在 `__exit__`**，这是 SQLite 的语义。"""
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        """成功提交、异常回滚，**绝不关闭连接**（见模块 docstring 第三节）。"""
        try:
            if exc_type is None:
                self._raw.commit()
            else:
                self._raw.rollback()
        except psycopg.Error:
            # 连接已经断了的时候，提交/回滚都会抛。这时不要盖掉原始异常——
            # 调用方要看到的是它自己那条错，不是"连接没了"。
            if exc_type is None:
                raise
            logger.warning("事务收尾失败，连接可能已断", exc_info=True)
        return False

    def close(self) -> None:
        """回滚未提交事务并**归还池**。幂等。"""
        if self._closed:
            return
        self._closed = True
        try:
            if not self._raw.closed:
                self._raw.rollback()
        except psycopg.Error:
            # 已经断了的连接，rollback 也会抛；归还时池会自己丢弃它。
            logger.debug("close 时回滚失败，交给池处理", exc_info=True)
        try:
            self._pool.putconn(self._raw)
        except Exception:  # noqa: BLE001 - 归还失败不该影响调用方收尾
            logger.warning("归还连接失败", exc_info=True)

    def __del__(self) -> None:
        """兜底归还：`with repository.connect() as conn:` 这种写法不会调 `close()`。

        在 SQLite 上那种写法是能跑的——`sqlite3.Connection` 被垃圾回收时自己关掉
        文件描述符，最坏是多开一条连接。池不是这样：**不还就少一条**，漏满
        `max_size` 条之后所有请求都 `couldn't get a connection`。

        所以这里补回 SQLite 的"回收即释放"语义。它是兜底而不是正路：显式的
        `closing(...)` / `close()` 仍然是首选，走到 `__del__` 的调用点会多一次
        未被察觉的占用周期。解释器退出阶段池可能已经关了，所以整段吞异常。
        """
        try:
            self.close()
        except Exception:  # noqa: BLE001 - 解释器退出阶段的兜底，不能抛
            pass

    # ------------------------------------------------------------ 语句

    def execute(self, sql: str, params: Any = None):
        """执行一条语句，返回游标。

        事务控制语句与 `PRAGMA` 在这里被截住，不发往服务器（见 docstring 第五节）。
        """
        if _PRAGMA_RE.match(sql):
            # `PRAGMA foreign_keys = ON` 是 SQLite 的连接级开关；PG 的外键**默认强制**，
            # 没有对应物。另外几处 `PRAGMA table_info` 是查列，M0 之后已改走
            # information_schema，走不到这里。
            if "table_info" in sql.lower():
                raise RuntimeError(
                    "PRAGMA table_info 在 PG 上不存在，改查 information_schema.columns")
            return _NoOpCursor()
        if _BEGIN_RE.match(sql):
            # 隐式事务：psycopg 在下一条真语句上自己开事务，不需要发 BEGIN。
            # `BEGIN IMMEDIATE` 的"提前拿写锁"在 PG 上没有对应语句（见 docstring）。
            return _NoOpCursor()
        if _COMMIT_RE.match(sql):
            self._raw.commit()
            return _NoOpCursor()
        if _ROLLBACK_RE.match(sql):
            self._raw.rollback()
            return _NoOpCursor()
        if _SAVEPOINT_CTL_RE.match(sql):
            # 调用方自己管保存点：**直接发**，不套 `_shim_sp`（理由见 `_SAVEPOINT_CTL_RE`
            # 的注释）。代价是这条语句失败后事务会进 aborted 态——但那本来就是调用方
            # 的显式事务块，它自己会 `ROLLBACK TO` 处理。
            return self._raw.execute(_translate(sql, params), params)

        text = _translate(sql, params)
        self._savepoint_begin()
        try:
            cursor = self._raw.execute(text, params)
        except psycopg.Error:
            self._savepoint_rollback()
            raise
        self._savepoint_release()
        return cursor

    def executemany(self, sql: str, rows: Sequence[Any]):
        """批量执行。`psycopg.Connection` 没有这个方法（只有 `Cursor` 有）。"""
        text = _translate(sql, (None,))
        self._savepoint_begin()
        try:
            with self._raw.cursor() as cursor:
                cursor.executemany(text, rows)
                rowcount = cursor.rowcount
        except psycopg.Error:
            self._savepoint_rollback()
            raise
        self._savepoint_release()
        return _RowcountCursor(rowcount)

    def cursor(self):
        """裸游标。只有"批量"那条路需要（`executemany` 在 `Cursor` 上）。"""
        return self._raw.cursor()

    # ------------------------------------------------------------ 事务

    def commit(self) -> None:
        self._raw.commit()

    def rollback(self) -> None:
        self._raw.rollback()

    # ------------------------------------------------------------ 兼容属性
    #
    # 这两个属性在 SQLite 上是"配置连接"，在 PG 上没有意义。做成可写属性而不是
    # 让赋值抛 `AttributeError`：`repository.py` / `runs.py` 里那两行赋值还在，
    # 等它们被删掉之前，先让代码跑得起来。**`row_factory` 的读值是假的**——真正的
    # 行工厂由 `pool.py` 的 `kwargs={"row_factory": dict_row}` 统一设。

    @property
    def row_factory(self):
        return dict

    @row_factory.setter
    def row_factory(self, value) -> None:  # noqa: ARG002 - 故意忽略
        return None

    @property
    def isolation_level(self):
        return None

    @isolation_level.setter
    def isolation_level(self, value) -> None:  # noqa: ARG002 - 故意忽略
        return None

    # ------------------------------------------------------------ SAVEPOINT

    def _savepoint_begin(self) -> None:
        self._raw.execute(f"SAVEPOINT {_SAVEPOINT}")

    def _savepoint_release(self) -> None:
        self._raw.execute(f"RELEASE SAVEPOINT {_SAVEPOINT}")

    def _savepoint_rollback(self) -> None:
        """回到保存点并清掉它。**清理失败只记日志**：这里正在处理一个真实错误，
        不能用一个"清理也失败了"把它盖成另一个错。"""
        try:
            self._raw.execute(f"ROLLBACK TO SAVEPOINT {_SAVEPOINT}")
            self._raw.execute(f"RELEASE SAVEPOINT {_SAVEPOINT}")
        except psycopg.Error:
            logger.debug("回滚保存点失败，连接可能已断", exc_info=True)


class _NoOpCursor:
    """事务控制语句的返回值占位。

    旧代码会拿它做 `result.rowcount`（`quota` 那条 `INSERT ... ON CONFLICT` 的
    超额判据）或直接丢掉。给个 `rowcount = 0` 让"没改任何行"这个默认含义成立。
    """

    __slots__ = ()

    rowcount = 0

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def fetchmany(self, size: int = 0):  # noqa: ARG002
        return []

    def close(self) -> None:
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


class _RowcountCursor(_NoOpCursor):
    """只有 `rowcount` 有意义的伪游标（`executemany` 的返回值）。"""

    __slots__ = ("rowcount",)

    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


#: 类型别名，给那些原来写 `sqlite3.Connection` 的注解用。
Connection = Conn

#: 一行查询结果的类型。原来是 `sqlite3.Row`（支持 `row["col"]` 与 `row[0]`），现在池里
#: 配的是 `psycopg.rows.dict_row`，所以是普通 dict：`row["col"]` 不变，`row[0]` 消失。
#:
#: 用 `dict[str, Any]` 而不是 `psycopg.rows.DictRow`：后者是 `TypedDict` 风格的协议，
#: 在 `list[Row]` 这类注解里可读性更差，而这些注解的读者是维护者而不是类型检查器。
Row = dict[str, Any]

