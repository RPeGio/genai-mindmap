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

from app.contracts.edits import (
    EditEventIn,
    EditEventRow,
    EditHealth,
    EditSummary,
    LabelShrunk,
)
from app.mindmaps import repository

logger = logging.getLogger(__name__)

#: 时间格式与 `mindmaps` / `runs` / `user_prefs` / `library_*` 完全一致。
#: 三处格式不同会有人"谁更准"上错一次——这是仓库里写过的教训（`agent/runs.py:_NOW`）。
_NOW = "nowstamp()"

#: `recent()` 的默认条数与上限。上限存在的理由与别处一样：这是给人读的一屏，
#: 不是导出通道（要导出该另开一条，而不是把 `limit` 放到一万）。
RECENT_DEFAULT = 50
RECENT_LIMIT = 500


def record(map_id: str, events: Sequence[EditEventIn], *, owner: str = "") -> int:
    """把一批事件写进 `edit_events`，返回写入条数。

    单个事务、一条 `executemany`：一批事件是**一次保存**的产物，半批落库会让
    `by_kind` 出现"改了 20 处但只记了 7 条"这种无法解释的缺口。

    **失败不抛。** 调用方是保存接口，那时内容已经落库；为了统计写失败而让用户看到
    "保存失败"、进而重试撞 409，方向完全是错的。这里只记 warning——服务端日志里
    有 map_id 与条数，够定位。

    `owner` 是**谁做的这次改动**，与 `map_id`（哪张图）是两件事：图是共享的，
    但"他把标题改短了"这条观察只能进他自己的记忆总结。`owner` 由调用方显式给，
    不能在这里读 `current_identity()`——那条约定来自 `app/auth/owner.py`（后台线程
    读不到 ContextVar，而这条路径以后可能从线程里调）。
    """
    if not events:
        return 0
    rows = [
        (map_id, event.run_id, owner, event.actor, event.kind, event.node_id, event.field,
         event.before, event.after, event.depth, event.subtree_size, event.change_count,
         event.node_count, event.at)
        for event in events
    ]
    try:
        with closing(repository.connect()) as conn, conn:
            conn.executemany(
                f"""INSERT INTO edit_events
                        (map_id, run_id, owner, actor, kind, node_id, field, before, after,
                         depth, subtree_size, change_count, node_count, at, recorded_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, {_NOW}), {_NOW})""",
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
    sql = (f"SELECT {column} AS label, COUNT(*) AS total FROM edit_events WHERE map_id = ? "
           f"GROUP BY {column} ORDER BY COUNT(*) DESC, {column}")
    return {row["label"]: row["total"] for row in conn.execute(sql, (map_id,))
            if row["label"] is not None}


def summary(map_id: str) -> EditSummary:
    """这张图的改动统计。**这是这一版真正的交付物。**

    `label_shrunk` 与 `removed_depth` / `removed_subtree` 是特意要的三个数，因为它们
    直接对应生成侧该改什么：

      - 标题普遍被缩短（**只算 user**）→ 生成侧 16 字上限可能还是太长，或者提示词
        让它写得太长；
      - 零散删掉的集中在第 3 层 → 初次深度太深；
      - 删掉的是整支还是零散 → "整支被删"更像"这一支根本不该生成"，与"挖太深"是
        两件事，混在一起读会得出错的处方。

    **分母是 `kind='generate'` 的 `node_count` 之和**（候选层删掉之后的口径，见
    `contracts/edits.py`）。三个数一起读才成立：

      - `generated_nodes` = SUM(node_count)：模型一共交出多少个节点；
      - `changed_events`：这些图上发生了多少条节点改动（含用户手工改的）；
      - `untouched_runs`：关掉"改动事件"这一轴之后，有多少次生成一处都没被碰。

    只看前两个会得出"每个节点被改 0.8 次"这种看不出方向的数；加上第三个才知道
    "是普遍改了 0.8 次"还是"9 次一字未改、1 次改了 8 处"——这两种情况的处方完全
    相反。

    它们只说"发生了什么"，不说"为什么"——局限见 `docs/EDIT-LOG.md`。
    """
    with closing(repository.connect()) as conn:
        # 行工厂是 `dict_row`，原来的 `fetchone()[0]` / `[1]` 一律改按列名取
        # （见 `app/db/conn.py` 的 `Row`）。这里给聚合列起名字，是因为"第二列是
        # MAX(recorded_at)"这种位置约定在改名之后会静默错位。
        totals = conn.execute(
            "SELECT COUNT(*) AS total, MAX(recorded_at) AS last_event_at "
            "FROM edit_events WHERE map_id = ?",
            (map_id,)).fetchone()
        total, last_event_at = totals["total"], totals["last_event_at"]
        by_actor = _count_by(conn, "actor", map_id)
        by_kind = _count_by(conn, "kind", map_id)
        by_field = _count_by(conn, "field", map_id)

        # 分母与生成次数。`COALESCE(SUM(...), 0)`：只有 generate 行而它们全都没有
        # `node_count`（加列之前的老行）时，`SUM` 是 NULL 而不是 0，直接进 JSON
        # 会变成一个 `null` 分母。
        generated = conn.execute(
            """SELECT COUNT(*) AS total, COALESCE(SUM(node_count), 0) AS nodes
               FROM edit_events
               WHERE map_id = ? AND kind = 'generate'""",
            (map_id,),
        ).fetchone()

        # 分子。`edge` 不进：分母数的是节点，把"加了一条关系"算成节点改动，比例就
        # 再也读不出"每个节点被改了几次"。
        changed_events = conn.execute(
            """SELECT COUNT(*) AS total FROM edit_events
               WHERE map_id = ? AND kind IN ('update', 'remove', 'add', 'move', 'style')""",
            (map_id,),
        ).fetchone()["total"]

        # 零改动次数。**与分子的 kind 集合逐字相同**（`edge` 在两边都不算）：只要
        # 一边多认一种，就会出现"既不算改动事件、也不算一字未改"的黑洞，而那正是
        # `changed_events == 0 ⇒ untouched_runs == generated_runs` 这条恒等式破掉的样子。
        # `g.run_id IS NOT NULL`：老数据里 generate 行可能没有 run_id
        # （NULL = NULL 在 SQL 里不成立，会被 `NOT EXISTS` 判成"没有改动事件"），
        # 那条数不是"一字未改"，是"当时没记"。
        untouched = conn.execute(
            """SELECT COUNT(*) AS total FROM edit_events AS g
               WHERE g.map_id = ? AND g.kind = 'generate' AND g.run_id IS NOT NULL
                 AND NOT EXISTS (
                   SELECT 1 FROM edit_events AS e
                    WHERE e.map_id = g.map_id AND e.run_id = g.run_id
                      AND e.kind IN ('update', 'remove', 'add', 'move', 'style')
                 )""",
            (map_id,),
        ).fetchone()["total"]

        # `length()` 对 TEXT 在 UTF-8 下按**字符**算，与前端 `charLength`（码点）
        # 同一口径。两处过滤各管一件事：
        #  - `field='label'`：`title`（导图标题）的改动是另一件事，混进来会让"节点
        #    标题被改短"这个信号被导图标题稀释；
        #  - `actor='user'`：`agent` 那批是"生成自己的改动记在模型名下"，性质完全
        #    不同。实测 B 图只有 3 条 user 的缩短，不过滤会读成 4 条。
        shrunk = conn.execute(
            """SELECT COUNT(*) AS total, AVG(length(before)) AS before_len,
                      AVG(length(after)) AS after_len
               FROM edit_events
               WHERE map_id = ? AND field = 'label' AND actor = 'user'
                 AND before IS NOT NULL AND after IS NOT NULL
                 AND length(after) < length(before)""",
            (map_id,),
        ).fetchone()

        # **只统计零散删除。** 一条 remove 现在代表一整棵被删的子树，根所在的层不等于
        # "这一层的内容被嫌弃"；`subtree_size IS NULL` 的老数据同理（当时没记，无法
        # 判断它是零散还是整支），两者都不进这个分布——否则"每层都在被删"那次污染还在。
        depths = conn.execute(
            """SELECT depth AS depth, COUNT(*) AS total FROM edit_events
               WHERE map_id = ? AND kind = 'remove' AND depth IS NOT NULL
                 AND subtree_size = 1
               GROUP BY depth ORDER BY depth""",
            (map_id,),
        ).fetchall()

        sizes = conn.execute(
            """SELECT subtree_size AS subtree_size, COUNT(*) AS total FROM edit_events
               WHERE map_id = ? AND kind = 'remove'
               GROUP BY subtree_size""",
            (map_id,),
        ).fetchall()

    return EditSummary(
        total=total,
        last_event_at=last_event_at,
        by_actor=by_actor,
        by_kind=by_kind,
        by_field=by_field,
        label_shrunk=LabelShrunk(
            count=shrunk["total"] or 0,
            avg_before=round(shrunk["before_len"], 1) if shrunk["before_len"] is not None else None,
            avg_after=round(shrunk["after_len"], 1) if shrunk["after_len"] is not None else None,
        ),
        removed_depth={row["depth"]: row["total"] for row in depths},
        removed_subtree=_subtree_histogram(sizes),
        generated_runs=generated["total"] or 0,
        generated_nodes=generated["nodes"] or 0,
        changed_events=changed_events,
        untouched_runs=untouched,
    )


def _subtree_histogram(rows: Sequence[dict]) -> dict[str, int]:
    """`[{"subtree_size": n, "total": m}]` → `{"size_1": 5, "size_12": 1, "size_unknown": 2}`。

    排序固定（规模升序、`size_unknown` 最后）：同样的数据永远同样的输出，两次调用
    之间不会因为数据库的扫描顺序而抖动——抖动的统计没法对比"这周和上周"。

    入参是 `dict_row` 的行（原先是 `sqlite3.Row`，当时按 `row[0]` / `row[1]` 取）。
    改成按列名取之后，这里**必须**给聚合列起名（SQL 里是 `AS subtree_size` /
    `AS total`），否则 `row["subtree_size"]` 会 KeyError。
    """
    ordered = sorted(
        rows,
        key=lambda row: (1, 0) if row["subtree_size"] is None else (0, row["subtree_size"]))
    return {
        "size_unknown" if row["subtree_size"] is None else f"size_{row['subtree_size']}":
            row["total"]
        for row in ordered
    }


def health() -> EditHealth:
    """跨图的链路健康检查：昨日编辑过的图数 vs 昨日保存的图数。

    判据只有一句：**"保存了但没有任何事件"在正常情况下不该发生**。每次生成完成都记
    一条 `generate`（连一字未改也记），所以 `saved > 0 且 edited == 0` 只能是链路断了。

    两个方向不对称，这很重要：链路真断时这个判据**一定**报警（事件收不到 → edited
    恒为 0）；反过来它只会漏报，不会因为"数据少"而误报成健康。唯一的误报窗口是跨
    零点那一次保存——`updated_at` 写在事件之前几毫秒，两者可能落进相邻两天，于是
    "昨天存了一张、事件记在今天"。代价是一天至多一次假警报，比不上为它引入一套时间
    窗口膨胀逻辑。

    `saved_maps` 是**下界**：`mindmaps` 只留最后一次保存时刻，昨天存过、今天又存过的
    图不在昨天的窗里。下界只会让报警更难触发（漏报），不会凭空报警。
    """
    with closing(repository.connect()) as conn:
        # 原来这里是 `date('now','-1 day') || 'T00:00:00.000Z'`（SQLite 的日期函数）。
        # PG 上的等价写法：取 UTC 今天的日期、减一天，`to_char` 成与仓库里其他时间戳
        # 同形的定宽文本（`recorded_at` / `updated_at` 都是这种文本，比较靠字典序）。
        # 用 `AT TIME ZONE 'utc'` 而不是裸 `now()::date`：后者的"日"取决于会话时区，
        # 服务器时区一变，这个判据的窗口就跟着漂。
        window = conn.execute(
            "SELECT to_char(((now() AT TIME ZONE 'utc')::date - 1), 'YYYY-MM-DD') "
            "         || 'T00:00:00.000Z' AS window_start, "
            "       to_char((now() AT TIME ZONE 'utc')::date, 'YYYY-MM-DD') "
            "         || 'T00:00:00.000Z' AS window_end").fetchone()
        window_start, window_end = window["window_start"], window["window_end"]
        edited = conn.execute(
            """SELECT COUNT(DISTINCT map_id) AS total FROM edit_events
               WHERE recorded_at >= ? AND recorded_at < ?""",
            (window_start, window_end)).fetchone()["total"]
        saved = conn.execute(
            'SELECT COUNT(*) AS total FROM mindmaps WHERE updated_at >= ? AND updated_at < ?',
            (window_start, window_end)).fetchone()["total"]

    suspect = saved > 0 and edited == 0
    if suspect:
        detail = (f"昨日保存了 {saved} 张图，却没有收到任何编辑事件——日志链路可能断了："
                  "前端在发、后端在丢，或事件根本没进请求体。")
    elif saved == 0:
        detail = (f"昨日没有保存（{edited} 张图有编辑事件）——没有保存时零事件是正常的，"
                  "判据不适用。")
    else:
        detail = f"昨日保存 {saved} 张图，其中 {edited} 张有编辑事件，链路正常。"

    return EditHealth(
        window_start=window_start,
        window_end=window_end,
        edited_maps=edited,
        saved_maps=saved,
        chain_suspect=suspect,
        detail=detail,
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
            before=row["before"], after=row["after"], depth=row["depth"],
            subtree_size=row["subtree_size"], change_count=row["change_count"],
            node_count=row["node_count"],
            at=row["at"], recorded_at=row["recorded_at"],
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
    "health",
    "recent",
    "record",
    "summary",
]
