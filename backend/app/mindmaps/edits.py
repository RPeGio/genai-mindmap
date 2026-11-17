"""编辑日志的读写：一张表、两条读路径、一个写入口。

这一版**只记录、不总结**：后面那几步（LLM 总结 → DPO 微调）要几百条偏好对，
一天几次的生成攒够要几个月；但"改动集中在一两个具体问题上"（比如"80% 的改动
是把长标题改短"）跑一次统计就能看出来，而那种问题用规则一天就能修。

所以这个模块只服务一句话：**用户最常改什么**。形状（`EditEventIn` /
`EditSummary`）在 `app/contracts/edits.py`，那里写了四个取舍的来由。
"""
from __future__ import annotations

import logging
from contextlib import closing
from typing import Sequence

from app.contracts.edits import EditEventIn, EditEventRow, EditSummary, LabelShrunk
from app.mindmaps import repository

logger = logging.getLogger(__name__)

#: 时间格式与 `mindmaps` / `runs` / `user_prefs` / `library_*` 完全一致。
#: 三处格式不同会有人"谁更准"上错一次——这是仓库里写过的教训（`agent/runs.py:_NOW`）。
_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"

#: `recent()` 的默认条数与上限。上限存在的理由与别处一样：这是给人读的一屏，
#: 不是导出通道（要导出该另开一条，而不是把 `limit` 放到一万）。
RECENT_DEFAULT = 50
RECENT_LIMIT = 500


def record(map_id: str, events: Sequence[EditEventIn]) -> int:
    """把一批事件写进 `edit_events`，返回写入条数。

    单个事务、一条 `executemany`：一批事件是**一次保存**的产物，半批落库会让
    `by_kind` 出现"改了 20 处但只记了 7 条"这种无法解释的缺口。

    **失败不抛。** 调用方是保存接口，那时内容已经落库；为了统计写失败而让用户看到
    "保存失败"、进而重试撞 409，方向完全是错的。这里只记 warning——服务端日志里
    有 map_id 与条数，够定位。
    """
    if not events:
        return 0
    rows = [
        (map_id, event.run_id, event.actor, event.kind, event.node_id, event.field,
         event.before, event.after, event.depth, event.at)
        for event in events
    ]
    try:
        with closing(repository.connect()) as conn, conn:
            conn.executemany(
                f"""INSERT INTO edit_events
                        (map_id, run_id, actor, kind, node_id, field, before, after, depth, at,
                         recorded_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, {_NOW}), {_NOW})""",
                rows,
            )
    except Exception:  # noqa: BLE001 — 统计写失败不该让用户的保存失败
        logger.warning("编辑事件写入失败：map_id=%s 条数=%d", map_id, len(rows), exc_info=True)
        return 0
    return len(rows)


def _count_by(conn, column: str, map_id: str) -> dict:
    """按一列分组计数。

    排序固定（先次数降序、再键升序）：同样的数据永远同样的输出，两次调用之间不会
    因为 SQLite 的扫描顺序而抖动——抖动的统计没法对比"这周和上周"。
    空值（比如 `field` 在 add/remove 上是 NULL）不进结果：那不是一"种"字段。
    """
    sql = (f"SELECT {column}, COUNT(*) FROM edit_events WHERE map_id = ? "
           f"GROUP BY {column} ORDER BY COUNT(*) DESC, {column}")
    return {row[0]: row[1] for row in conn.execute(sql, (map_id,)) if row[0] is not None}


def summary(map_id: str) -> EditSummary:
    """这张图的改动统计。**这是这一版真正的交付物。**

    `label_shrunk` 与 `removed_depth` 是特意要的两个数，因为它们直接对应生成侧
    该改什么：

      - 标题普遍被缩短 → 生成侧 16 字上限可能还是太长，或者提示词让它写得太长；
      - 删掉的集中在第 3 层 → 初次深度太深。

    但它们只说"发生了什么"，不说"为什么"——局限见这一版的交付说明。
    """
    with closing(repository.connect()) as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM edit_events WHERE map_id = ?", (map_id,)).fetchone()[0]
        by_actor = _count_by(conn, "actor", map_id)
        by_kind = _count_by(conn, "kind", map_id)
        by_field = _count_by(conn, "field", map_id)

        # `length()` 对 TEXT 在 UTF-8 下按**字符**算，与前端 `charLength`（码点）
        # 同一口径。只算 field='label'：`title`（导图标题）的改动是另一件事，混进来
        # 会让"节点标题被改短"这个信号被导图标题稀释。
        shrunk = conn.execute(
            """SELECT COUNT(*), AVG(length(before)), AVG(length(after))
               FROM edit_events
               WHERE map_id = ? AND field = 'label'
                 AND before IS NOT NULL AND after IS NOT NULL
                 AND length(after) < length(before)""",
            (map_id,),
        ).fetchone()

        depths = conn.execute(
            """SELECT depth, COUNT(*) FROM edit_events
               WHERE map_id = ? AND kind = 'remove' AND depth IS NOT NULL
               GROUP BY depth ORDER BY depth""",
            (map_id,),
        ).fetchall()

    return EditSummary(
        total=total,
        by_actor=by_actor,
        by_kind=by_kind,
        by_field=by_field,
        label_shrunk=LabelShrunk(
            count=shrunk[0] or 0,
            avg_before=round(shrunk[1], 1) if shrunk[1] is not None else None,
            avg_after=round(shrunk[2], 1) if shrunk[2] is not None else None,
        ),
        removed_depth={row[0]: row[1] for row in depths},
    )


def recent(map_id: str, limit: int = RECENT_DEFAULT) -> list[EditEventRow]:
    """最近的改动明细，新的在前。

    统计给的是分布；明细回答"那 42 条具体是什么"。没有它，一个反常的平均值只能连
    数据库才查得下去——而这张表存在的意义就是让人不连数据库也能看见。

    `at DESC, id ASC` 而不是 `id DESC`：同一批事件的 `at` **完全相同**（一次操作
    共享一个时刻，见前端 `collectEdits`），按 id 降序会把批内顺序整个翻过来，
    读起来像"先删了标题再改的标题"。跨批次仍然新的在前，因为那时 `at` 不同。
    """
    size = max(1, min(limit, RECENT_LIMIT))
    with closing(repository.connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM edit_events WHERE map_id = ? ORDER BY at DESC, id ASC LIMIT ?",
            (map_id, size),
        ).fetchall()
    return [
        EditEventRow(
            id=row["id"], map_id=row["map_id"], run_id=row["run_id"], actor=row["actor"],
            kind=row["kind"], node_id=row["node_id"], field=row["field"],
            before=row["before"], after=row["after"], depth=row["depth"], at=row["at"],
            recorded_at=row["recorded_at"],
        )
        for row in rows
    ]


def clear(map_id: str | None = None) -> int:
    """清空（只在用例与本地调试用）。传 `map_id` 只清那一张。"""
    with closing(repository.connect()) as conn, conn:
        if map_id is None:
            return conn.execute("DELETE FROM edit_events").rowcount
        return conn.execute("DELETE FROM edit_events WHERE map_id = ?", (map_id,)).rowcount


__all__ = [
    "RECENT_DEFAULT",
    "RECENT_LIMIT",
    "clear",
    "recent",
    "record",
    "summary",
]
