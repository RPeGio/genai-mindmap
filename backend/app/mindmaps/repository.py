"""仓储入口：一条连接，以及建表。

**这个模块原来叫"本地 SQLite 仓储"，现在是 PostgreSQL**。改动落在三处，其余对外
签名一个字没变：

1. `connect()` 从"打开一个文件 + 重放十条建表语句"变成"从池里取一条"。
2. `db_path()` 从返回 `Path` 变成返回连接串（保留旧名字，因为它被 11 处引用，
   其中两处是**语义依赖**：游客 cookie 的签名密钥与设置缓存的键，见下）。
3. 建表搬到 `app/db/schema.py`，由 `app/main.py` 的 lifespan 在启动时跑一次。

## 被删掉的东西，以及为什么不要留

`_ensure_columns` / `_drop_columns` / `_columns_of` / `_table_exists` /
`_migrate_legacy_prefs` 全部删掉。它们存在的唯一原因是 SQLite 的两个限制：
`CREATE TABLE IF NOT EXISTS` 不给已存在的表补列、以及改不了主键。PG 有
`ADD COLUMN IF NOT EXISTS`，而"老库缺列"这个前提在新库里根本不存在——建出来的
就是终态表。老数据的形态问题由一次性导入脚本处理（M6），不在运行期处理。

**`db_path()` 这个名字留着但返回值变了**，这是刻意的取舍：它有两个读者不看内容、
只把它当"这个库的身份"，一个是 `auth/store._guest_secret()`（cookie 签名密钥 =
`sha256(前缀 + str(db_path()))`），另一个是 `settings/store` 的读缓存键。**前者换值
等于所有现存会话与游客身份失效一次**——这是换库必然的结果（认不出旧的人了），不是
bug，但必须写进发布说明。后者只是缓存失效，无害。
"""
from __future__ import annotations

import json
import logging
from contextlib import closing
from typing import Any
from uuid import uuid4

from app.contracts.mindmap import DiagramContent, MapRecord, migrate_content
from app.db import pool as pool_module
from app.db.conn import Conn

logger = logging.getLogger(__name__)

#: 连接串的环境变量名。**与 `app/db/pool.DSN_ENV` 同一个**，在这里再导出一次是为了
#: 让"这个模块负责回答'库在哪'"这件事保持不变——历史上 48 个文件在字面上引用
#: `MINDMAP_DB_PATH`，现在那个变量已废除（应用不再读它，老库导入由
#: `scripts/migrate_sqlite_to_pg.py --source` 显式指定）。
DSN_ENV = pool_module.DSN_ENV


def dsn() -> str:
    """这个进程连的是哪个库。

    测试把它指到 `*_test` 库上（见 `tests/conftest.py`）；线上由 systemd 的
    `EnvironmentFile` 给。
    """
    return pool_module.dsn()


def db_path() -> str:
    """**历史名字**，现在返回连接串而不是文件路径。

    见模块 docstring 最后一段：两个不关心内容、只关心"库的身份"的读者还在用它，
    改名要连带改他们的语义，所以先留一个别名。新代码用 `dsn()`。
    """
    return dsn()


def connect(*, timeout: float = 10) -> Conn:
    """从池里取一条连接。用 `closing(connect())` 关，或者 `with conn:` 提交/回滚。

    `timeout` 仍然有意义，但含义变了：原来它是"等 SQLite 写锁的上限"，现在是
    "等池里空出一条连接的上限"。写成形参的理由没变——生成中途落检查点的写库跑在
    线程池里，它等锁的每一秒都是这一轮生成在等，而检查点本身是"存不上就算了"的
    东西，不值得陪一把慢锁耗满默认的十秒（`agent/runs.py` 的
    `CHECKPOINT_LOCK_TIMEOUT = 2.0` 就是那个更短的值）。

    **注意这里不再建表**：建表在 `app/db/schema.init_schema()`，由 lifespan 跑一次。
    这条改动把"每次操作白付十条 DDL"（PG 上实测 2.458ms）从热路径上拿掉了。
    """
    pool = pool_module.get_pool()
    return Conn(pool.getconn(timeout=timeout), pool)


def _record(row: dict[str, Any]) -> MapRecord:
    # 库里可能还躺着没有 schema_version 的 v1 记录；补默认值这一步放在读取处，
    # 而不是写一个一次性迁移脚本——数据量小、只有一个写入方，读时补齐足够，
    # 而且永远不会有"忘了跑脚本"的窗口。
    return MapRecord(id=row["id"], version=row["version"],
                     content=migrate_content(json.loads(row["content"])),
                     updated_at=row["updated_at"])


def list_maps(*, tolerant: bool = False) -> list[MapRecord]:
    """按更新时间倒序加载全部导图。

    `tolerant=True` 只给**旁路统计**用（如知识库的"被几张导图引用"）：单行坏数据
    （坏 JSON、v1 之后又演进的字段）跳过并记 warning，不让一行脏数据把整个知识库
    入口打成 500。列表页/导出必须用默认的 `False`——坏数据要暴露给排查的人，
    而不是被静默跳过。
    """
    with closing(connect()) as conn, conn:
        rows = conn.execute("SELECT * FROM mindmaps ORDER BY updated_at DESC, id").fetchall()
    records: list[MapRecord] = []
    for row in rows:
        try:
            records.append(_record(row))
        except Exception:  # noqa: BLE001 - tolerant 模式下"任何读不出来的行"都算坏行
            if not tolerant:
                raise
            logger.warning("跳过一张读不出来的导图：id=%s", row["id"], exc_info=True)
    return records


def maps_fingerprint() -> tuple[str, int, str | None]:
    """导图表当前状态的廉价指纹：库位置 + 条数 + 最后更新时间。

    "被几张导图引用"每次开抽屉都要算，而算它得把每张图的正文全量解析一遍。这个
    查询只走聚合，不读正文——任何新建或保存都会改动指纹（`update_map` 一定刷新
    `updated_at`），所以拿它当缓存键比"纯按时间过期"更准。

    库身份也进指纹：测试与本地调试会把 `MINDMAP_PG_DSN` 指到另一个库，换了库当然
    不能沿用上一个库的计数（否则两个库恰好"条数相同、最后一毫秒相同"时会互相串）。
    """
    with closing(connect()) as conn, conn:
        row = conn.execute(
            "SELECT COUNT(*) AS total, MAX(updated_at) AS latest FROM mindmaps").fetchone()
    return dsn(), int(row["total"]), row["latest"]


def get_map(map_id: str) -> MapRecord | None:
    with closing(connect()) as conn, conn:
        row = conn.execute("SELECT * FROM mindmaps WHERE id = ?", (map_id,)).fetchone()
    return _record(row) if row else None


def create_map(content: DiagramContent, *, owner: str | None = None) -> MapRecord:
    """建一张图并记下它归谁。

    `owner=None` 时按当前身份算（`store.owner_of`）：登录用户是 `user:<id>`，游客是
    `guest:<cookie>`。**游客的图跟着那台浏览器的 cookie 走**——清掉 cookie 或换浏览器
    就找不回来了。这是"游客"这个词本身的含义，不是缺陷；登录之后要保住它得重新保存一次。

    直接传 `owner=""` 只在测试与迁移脚本里出现，表示"无主"。
    """
    map_id = str(uuid4())
    if owner is None:
        from app.settings.store import owner_of  # 函数内 import：避开 auth↔settings 的环

        from app.auth.identity import current_identity

        owner = owner_of(current_identity())
    with closing(connect()) as conn, conn:
        conn.execute(
            "INSERT INTO mindmaps (id, version, content, updated_at, owner) "
            "VALUES (?, 1, ?, nowstamp(), ?)",
            (map_id, content.model_dump_json(), owner))
    return get_map(map_id)  # type: ignore[return-value]


def update_map(
    map_id: str,
    version: int,
    content: DiagramContent,
    *,
    owner: str | None = None,
    allow_unowned: bool = False,
) -> str:
    """保存。返回值多了一个 `"forbidden"`：这张图不是你的。

    **判据顺序是刻意的**：先看存在、再看归属、最后才比版本。反过来会出现"版本不符"
    与"不是你的"两种拒绝混在一起，而后者不该向非创建者透露这张图现在是什么版本——
    那本身就是一条信息。

    `allow_unowned` 只给部署方：`owner = ''` 是迁移前的老行（那时还没有账号这件事），
    把无主的历史内容交给这台机器上第一个注册的人，是唯一能自洽的归属答案。
    """
    if owner is None:
        from app.auth.identity import current_identity
        from app.settings.store import owner_of

        owner = owner_of(current_identity())
    with closing(connect()) as conn, conn:
        row = conn.execute("SELECT owner FROM mindmaps WHERE id = ?", (map_id,)).fetchone()
        if row is None:
            return "missing"
        holder = row["owner"] or ""
        if holder != owner and not (allow_unowned and holder == ""):
            return "forbidden"
        result = conn.execute("""UPDATE mindmaps SET content = ?, version = version + 1,
            updated_at = nowstamp()
            WHERE id = ? AND version = ?""", (content.model_dump_json(), map_id, version))
        return "updated" if result.rowcount else "conflict"
