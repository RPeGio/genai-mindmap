"""脚本专用的临时 PostgreSQL database 生命周期（只给 `backend/scripts/` 用）。

**为什么这个文件放在 `scripts/` 而不是 `app/`**：它唯一的本事是"建一个库、用完把
整库删掉"。生产代码不该有这种能力——那是能让线上库消失的权限，而应用层需要的只是
"连上去、读写"。所以它是脚本的私有件（下划线开头，不会被当成公开 API import）。

**为什么不能继续用"临时文件"那一套**：SQLite 时代每个脚本 `mkdtemp()` 再指一个
`*.db` 路径就完事，删文件不影响别人。PG 里"一个库"是集群级对象，建与删都**不能
跑在目标库里，也不能在事务里**（`CREATE DATABASE` 在事务块中直接报
`CREATE DATABASE cannot run inside a transaction block`），所以要连到另一个库
（这里是 `postgres`）用 autocommit 连接做。成套的四步是：建库 → 把
`MINDMAP_PG_DSN` 指过去 → **建表** → 收尾先关池再 `DROP ... WITH (FORCE)`。
少任何一步的后果都很具体：少建表是运行时炸，少关池是删不掉（"being accessed by
other users"），少删库是在 PG 上攒一堆没人认领的库。

**建表为什么必须在这里做**：`repository.connect()` 以前每次连接都重放十条
`CREATE TABLE IF NOT EXISTS`，换成 PG 之后不再重放——建表只在 `app/main.py` 的
lifespan 与 `tests/conftest.py` 里跑。脚本没有 lifespan，所以必须显式
`init_schema()`，否则第一次 `library.ingest` 会
`relation "library_sources" does not exist`。放在这里而不是让每个脚本各调一次：
十个脚本里漏一个就是运行时炸，而且只在"那条脚本恰好第一次写库"时才暴露。

**收尾顺序不能反**：`close_pool()` 必须在 `DROP DATABASE` 之前。`WITH (FORCE)`
（PG 13+）会强行踢掉目标库上所有连接；如果被踢的是我们自己池里的连接，池还以为
它们活着，下一次 `getconn()` 拿到的是断开的连接。反过来"先关池再删库"没有这个
问题：`psycopg_pool` 的 `close()` 是同步等所有连接归还并关闭之后才返回的。

用法（两种，看脚本有没有 `main`）：

    # 有 main、生命周期包得住的
    with temp_database("mm_shapes") as dsn:
        ...

    # 模块级就要建好（后续 import 的应用模块直接用）、没有统一出口的
    db = TempDatabase.create("mm_prefs")
    atexit.register(db.close)
"""
from __future__ import annotations

import contextlib
import os
import re
import sys
import uuid
from typing import Iterator

#: 建库/删库时连的那个库。`CREATE DATABASE`/`DROP DATABASE` 不能跑在目标库里，
#: 所以把业务 DSN 的 dbname 换成这个再连过去。`postgres` 是 PG 初始化时必建的。
ADMIN_DBNAME = "postgres"


def _target_dsn() -> str:
    """业务库所在的集群（新库建在同一个集群里）。

    从应用层读而不是从参数读：脚本已经把 `MINDMAP_PG_DSN` 当作唯一入口，
    这里再读一次环境变量就会和 `pool.dsn()` 的默认值逻辑分叉。
    """
    from app.db import pool as pool_module

    return pool_module.dsn()


def _with_dbname(dsn: str, dbname: str) -> str:
    """换掉 DSN 里的库名，其余（host/端口/用户/密码/参数）原样保留。

    用 psycopg 自己的 conninfo 解析而不是手拼字符串：DSN 既可能是 URI
    （`postgresql://u@h:port/db`），也可能是默认值那种带查询参数的形态
    （`postgresql:///mindmaps?host=/var/run/postgresql`），手拼会把后者的
    `?host=` 粘到库名后面，连到一个不存在的库。
    """
    from psycopg.conninfo import make_conninfo

    return make_conninfo(dsn, dbname=dbname)


@contextlib.contextmanager
def _admin_connection(dsn: str) -> Iterator[object]:
    import psycopg

    # autocommit=True 是硬要求：`CREATE DATABASE` 不能在事务块里跑，而 psycopg
    # 的默认连接会隐式开事务。
    with psycopg.connect(_with_dbname(dsn, ADMIN_DBNAME), autocommit=True) as conn:
        yield conn


def _unique_name(prefix: str) -> str:
    """`前缀_8位hex`。后缀是为了并发跑两份脚本时不撞名，前缀只是给人看的。

    截到 20 字符：PG 标识符上限 63 字节，留足余量，且失败时报错信息里能看清是
    哪个脚本建的。
    """
    safe = re.sub(r"[^a-z0-9_]", "_", prefix.lower()).strip("_") or "mm_tmp"
    return f"{safe[:20]}_{uuid.uuid4().hex[:8]}"


def _create_database(base_dsn: str, name: str) -> None:
    from psycopg import sql

    with _admin_connection(base_dsn) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))


def _database_exists(base_dsn: str, name: str) -> bool:
    with _admin_connection(base_dsn) as conn:
        row = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
    return row is not None


def _drop_database(base_dsn: str, name: str) -> None:
    """删库。失败只报警不抛出：它总在 `finally` 里跑，抛出去会盖掉真正的异常
    （"为什么我的测量脚本崩了"变成"为什么删库失败"）。报警留库名，人可以手动清。
    """
    from psycopg import sql

    try:
        with _admin_connection(base_dsn) as conn:
            # `IF EXISTS`：库可能在两次调用之间被别处删了。
            # `WITH (FORCE)`：踢掉还挂在目标库上的连接（PG 13+）。正常情况下我们
            # 已经先 `close_pool()`，这里是兜底——脚本被 patch/mock 绕过收尾时，
            # 残留连接会让普通 DROP 直接失败。
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)")
                         .format(sql.Identifier(name)))
    except Exception as exc:  # noqa: BLE001 —— 见 docstring：不能盖掉上游异常
        print(f"[_pg_tmp] 删库 {name} 失败（{exc}）；请手动执行 "
              f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)', file=sys.stderr)


def activate(dsn: str) -> None:
    """让应用层连到 `dsn` 指向的库，并把表建好。

    先 `close_pool()` 再改环境变量：`get_pool()` 自己也会在 DSN 变化时重建池，
    但显式关一次能把失败点分开——"连不上新库"与"建表失败"是两种完全不同的错，
    混在一起会让人去翻 schema。
    """
    from app.db import pool as pool_module
    from app.db.schema import init_schema

    pool_module.close_pool()
    os.environ[pool_module.DSN_ENV] = dsn
    # 幂等：对已经建好表的库重复调用只会多几条 `CREATE TABLE IF NOT EXISTS`。
    init_schema()


class TempDatabase:
    """一个已建好表、已接管 `MINDMAP_PG_DSN` 的临时库。

    优先用 `with temp_database(...)`；只在"模块级就要建好"的脚本里直接
    `create()` + `atexit.register(close)`。
    """

    def __init__(self, base_dsn: str, name: str, dsn: str, previous: str | None) -> None:
        self.base_dsn = base_dsn
        self.name = name
        self.dsn = dsn
        #: 进门前 `MINDMAP_PG_DSN` 的值。收尾要还原：脚本可能被同进程的其它代码
        #: import 进来跑（`measure_generation_diagnostics` 就是这样用 base 的），
        #: 留下一个指向已删库的连接串会让后来者莫名连到空气里。
        self._previous = previous
        self._closed = False

    @classmethod
    def create(cls, prefix: str = "mm_tmp") -> "TempDatabase":
        from app.db import pool as pool_module

        base = _target_dsn()
        previous = os.environ.get(pool_module.DSN_ENV)
        name = _unique_name(prefix)
        _create_database(base, name)
        dsn = _with_dbname(base, name)
        db = cls(base, name, dsn, previous)
        try:
            activate(dsn)
        except BaseException:
            # 建库成功但建表/连池失败：这里没有对象可 yield，必须立刻清掉，
            # 否则异常路径直接漏库。
            db.close()
            raise
        return db

    def __enter__(self) -> "TempDatabase":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """关池 → 删库 → 还原环境变量。幂等。"""
        if self._closed:
            return
        self._closed = True
        from app.db import pool as pool_module

        # 顺序见模块 docstring：先关池（同步等连接关闭），再删库。
        pool_module.close_pool()
        _drop_database(self.base_dsn, self.name)
        if self._previous is None:
            os.environ.pop(pool_module.DSN_ENV, None)
        else:
            os.environ[pool_module.DSN_ENV] = self._previous


@contextlib.contextmanager
def temp_database(prefix: str = "mm_tmp") -> Iterator[str]:
    """建一个一次性 database，装好表与应用环境变量；退出时关池并删库。

    yield 的是 DSN 而不是库名：脚本里唯一还需要它的地方是"打印给人看"，
    而 DSN 比库名更能说明"这次连的是哪儿"。
    """
    db = TempDatabase.create(prefix)
    try:
        yield db.dsn
    finally:
        # 放在 finally 而不是 __exit__ 之后：脚本 `return 2` 之类的正常提前退出、
        # 以及 Ctrl-C/异常，都要走这里。漏一次就在 PG 上留一个库。
        db.close()


def persistent_database(spec: str) -> tuple[str, bool]:
    """把一个"持久库"标识变成已建表的 DSN，**不删**。

    `spec` 收两种写法（历史上的 `MINDMAP_EVAL_DB_PATH` 是个文件路径，现在没有
    路径这个概念了，所以这里同时认库名与连接串）：

      - 含 `://`（或 keyword 形态的 `host=`）：当成完整连接串直接用，库由调用方
        自己保证存在——脚本不该替人跑到别的集群里建库；
      - 否则当成**库名**：同一个集群里没有就建，已存在就复用。

    返回 `(dsn, created)`。`created` 给调用方打印用：复用别人的库时至少要让读报告
    的人知道"these numbers 可能混着上一次的残留"。
    """
    spec = (spec or "").strip()
    if not spec:
        raise ValueError("持久库标识不能为空：给库名或完整 DSN")
    if "://" in spec or "=" in spec:
        activate(spec)
        return spec, False
    base = _target_dsn()
    created = not _database_exists(base, spec)
    if created:
        _create_database(base, spec)
    dsn = _with_dbname(base, spec)
    activate(dsn)
    return dsn, created
