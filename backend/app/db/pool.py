"""连接池。

**为什么必须用池，而不是"每次操作开一条连接"**：旧代码是 SQLite，连接是本地文件
句柄，开一条几十微秒，所以 190 处 `conn.execute` 全都写成"用完就关"。换成 PG 之后
`connect()` 是 TCP 握手 + 认证，实测 **1.387ms**；再叠加旧代码在连接里重放的十条
建表 DDL（实测 **2.458ms**），单次操作 **2.979ms**。一次生成光读模型配置就要 5–27
次连接（见 `settings/store.py` 那段统计），于是白付 16–89ms。

换成池之后同一操作 **0.15–0.23ms**。这不是优化项：不换池，这笔开销会乘进每一条
生成路径，而它们原本都是亚毫秒的本地操作。

**池的大小是 8，不是 20**：目标机器 2C2G（见 `AGENTS.md`），PG 每个后端进程实测
15–23MB 常驻，8 条已经是 120–180MB。而我们的并发上限是 uvicorn 单 worker + 少量
线程池任务，超过 8 条只会在内存上互相挤。

**`open=False` 起步**：池在第一次 `getconn()` 时才真连。这样"导入模块"不会因为
PG 还没起就失败，schema 初始化也能自己决定什么时候连（见 `main.py` 的 lifespan）。
"""
from __future__ import annotations

import logging
import os
import threading

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)

#: 连接串。**从 `MINDMAP_DB_PATH`（一个文件路径）改成了 DSN**：这条改动会
#: 让 `auth.store._guest_secret()` 派生的 cookie 签名密钥变化，也就是**所有现存
#: 登录会话与游客身份在切换后失效一次**。这是预期行为——它本来就是"库换了，
#: 认不出旧的人了"，而不是 bug。发布说明里要写。
DSN_ENV = "MINDMAP_PG_DSN"

#: 本机默认。部署时由 systemd 的 `EnvironmentFile` 覆盖（见 `deploy/genai-mindmap.service`）。
DEFAULT_DSN = "postgresql:///mindmaps?host=/var/run/postgresql"

#: 池大小。见模块 docstring：不是越大越好。
POOL_MAX = 8
POOL_MIN = 1

#: 取连接的最长等待。与旧 `repository.connect(timeout=10)` 的默认值一致——
#: 调用方（`runs.py` 的检查点写）会显式传更短的值，那里等不起。
POOL_TIMEOUT = 10.0

_pool: ConnectionPool | None = None
_pool_dsn: str | None = None
_lock = threading.Lock()


def dsn() -> str:
    """当前连接串。测试由 `conftest.py` 把它指到一个 `*_test` 库上。"""
    return (os.getenv(DSN_ENV, "") or "").strip() or DEFAULT_DSN


def get_pool() -> ConnectionPool:
    """取（必要时创建）进程级连接池。

    连接串变了就重建：测试里换库、以及"同一个进程先后连两个库"的调试场景都靠它。
    重建时先关旧池，否则旧连接会一直挂在 PG 上直到进程退出。
    """
    global _pool, _pool_dsn
    wanted = dsn()
    with _lock:
        if _pool is not None and _pool_dsn == wanted:
            return _pool
        if _pool is not None:
            _pool.close()
            _pool = None
        _pool = ConnectionPool(
            wanted,
            min_size=POOL_MIN,
            max_size=POOL_MAX,
            timeout=POOL_TIMEOUT,
            open=False,
            # `dict_row` 让 `row["col"]` 继续能用：全仓有 157 处按列名取值、只有 18 处
            # 按位置取值。选 dict 是让多数不改、少数改，而不是反过来。
            #
            # 代价实测：取 500 行 dict 0.348ms vs tuple 0.222ms，差 0.13ms。值得。
            kwargs={"row_factory": dict_row},
            name="mindmap",
        )
        _pool.open()
        _pool_dsn = wanted
        logger.info("连接池已就绪：%d–%d 条", POOL_MIN, POOL_MAX)
        return _pool


def close_pool() -> None:
    """关池。进程退出时调用（`main.py` 的 lifespan 收尾、测试收尾）。幂等。"""
    global _pool, _pool_dsn
    with _lock:
        if _pool is not None:
            _pool.close()
        _pool = None
        _pool_dsn = None
