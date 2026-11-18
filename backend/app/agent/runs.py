"""一次 agent 运行的**身份行**（`runs` 表）。

这张表存在的理由只有一个：run 此前只活在一个 HTTP 连接的寿命里。由此长出两个
症状，而它们其实是同一个缺口：

  1. **重复提交生成两张图**。请求里没有 run id，服务端就没有能力识别"这是同一次
     运行又投递了一遍"——换标签页、刷新重发、客户端重试都会新开一次真跑，花两倍
     钱产出两份结果，两个标签页各存一次就是两张图。原有的两道防线都在错的那一层：
     run 内的 `ctx.dedupe` 只活在一次运行里，保存层的版本乐观锁只认得"同一张已
     保存的图"。
  2. **关掉页面那 40 秒白花**。连接一断，生成器被 `aclosing` 关掉、工具里的模型
     请求被取消，已经付过钱的调用不回滚，而结果只活在内存里。用户回来什么都看不到。

所以：**SSE 从"run 本身"降级成"这一行的一次观察窗口"**（帧格式一个字节都没动）。
连接断了不代表运行没了，重发请求也不等于第二次运行。

两件事都在这张表上做，而不是各加一套机制：

- 身份：`begin()` 是唯一的入口，它在**一条 SQL 事务里**决定"这次是真跑、还是重复
  投递"。判在进程内的字典里不行——uvicorn 多 worker 时两个进程各有一份字典，
  "只有一个真跑"立刻变成"每个进程各跑一次"。
- 结果：`finish()` 在运行结束时（**含被取消**）把产出写进来。写在 `finally` 里，
  所以取消、异常、正常收尾三条路都只写一次。这一份**不是待确认的草稿**：生成结果
  已经直接进了画布（见 `docs/` 与前端 `stores/mindmap.ts`），存下来只为两件事——
  关掉页面还能捡回来，以及将来做生成历史。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from typing import Any, Sequence

from app.contracts.mindmap import MindMapContent
from app.mindmaps import repository

STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_CANCELLED = "cancelled"
STATUS_FAILED = "failed"

#: 不会再变的三种终态。`done` 之后**冻结**（见 `finish`），另两种可以重开。
TERMINAL_STATUSES = (STATUS_DONE, STATUS_CANCELLED, STATUS_FAILED)

#: 允许用同一个 run_id 重开的终态：用户主动重试。
RESTARTABLE_STATUSES = (STATUS_CANCELLED, STATUS_FAILED)

#: 时间格式与 `mindmaps` / `library_*` 完全一致（SQLite 的 `%f` 是毫秒）。
#: 三处格式不同就会在"谁更晚"的比较上错一天。
_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"


@dataclass(frozen=True)
class RunRecord:
    """`runs` 里的一行。`candidate` 已按契约解析过，不是原始 JSON 字符串。

    `candidate` 的语义变了：它不再是"等你点应用的草稿"，而是**这次生成产出的那棵树
    的快照**。生成结果已经直接进了画布，这份存下来是为了两件事——关掉页面再回来
    还能把上次的结果捡回来（`pending`），以及将来做生成历史。它不再有任何"落地
    与否"的含义，那个由前端画布的 `dirty` 与 `applied_at` 回答。
    """

    run_id: str
    status: str
    request: dict[str, Any]
    candidate: MindMapContent | None
    failure: dict[str, Any] | None
    created_at: str
    updated_at: str
    #: 这次生成的结果**被保存进 `mindmaps` 了**。空值 = 生成过、但还没存下来。
    #:
    #: 它曾经是"用户点了应用"，候选层删掉之后那一天没有用户动作可记；剩下的、
    #: 也是唯一有区分度的判据是"后来有没有存"。
    applied_at: str | None = None
    #: 候选是怎么来的（`demo` / `model`）。重放时原样回填 `done` 帧的 `mode`，
    #: 否则一棵占位树会被贴上"模型生成"的标签——那正是 `DoneEvent.mode` 要防的。
    mode: str | None = None


@dataclass(frozen=True)
class Claim:
    """`begin()` 的结果：这一次调用有没有真的拿到运行权。

    `claimed=False` 时 `record` 是**库里已有的那一行**，调用方据此决定是回 409
    还是把它的结果重放出来。判断与写入同一条事务，所以不存在"查的时候还在跑、
    写的时候已经结束"的空窗。
    """

    record: RunRecord
    claimed: bool


def _connect() -> sqlite3.Connection:
    """连接 + 关掉 Python 的隐式事务。

    `isolation_level=None` 是必需的：`begin()` 要显式开 `BEGIN IMMEDIATE` 才能让
    "读到的状态"和"写下的状态"在同一把写锁里。留在默认的隐式事务模式下，
    `BEGIN` 会撞上 Python 自己刚开的那条，抛 `cannot start a transaction within
    a transaction`——而那条错误只在并发时才出现，平时一点痕迹都没有。
    """
    conn = repository.connect()
    conn.isolation_level = None
    return conn


def _dump_candidate(candidate: MindMapContent | None) -> str | None:
    return None if candidate is None else candidate.model_dump_json()


def _loads(text: str | None) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        # 库里被人手改坏的 JSON 不该让整条读路径崩掉：一次运行的历史值不值得
        # 为它 500 一整个页面。认不出来就当没有。
        return None


def _record(row: sqlite3.Row) -> RunRecord:
    raw_candidate = _loads(row["candidate"])
    candidate: MindMapContent | None = None
    if isinstance(raw_candidate, dict):
        try:
            candidate = MindMapContent.model_validate(raw_candidate)
        except ValueError:
            candidate = None
    request = _loads(row["request"])
    failure = _loads(row["failure"])
    return RunRecord(
        run_id=row["run_id"],
        status=row["status"],
        request=request if isinstance(request, dict) else {},
        candidate=candidate,
        failure=failure if isinstance(failure, dict) else None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        applied_at=row["applied_at"],
        mode=row["mode"],
    )


def begin(run_id: str, request: dict[str, Any]) -> Claim:
    """认领一次运行：新 id 建行，旧 id 按它的状态分三种去向。

    - 不存在 → 建一行 `running`，`claimed=True`（这次真跑）。
    - `running` → 原样返回，`claimed=False`。调用方回 409：同一个 run 并发跑两次
      会互相打架（两次都往同一个候选上写，谁也说不清最终那棵树是哪个的）。
    - `done` → 原样返回，`claimed=False`。调用方把已有候选重放一遍，**不再调模型**。
    - `cancelled` / `failed` → 用户主动重试，把这行重新认领成 `running`。

    **任何分支都不碰 `candidate`**：同一 id 重复 `begin`（两个标签页同时点）不能把
    上一轮已经产出的草稿抹掉；重跑期间用户回来查，仍能看见那份草稿，直到新的
    终态写进新的一份。
    """
    payload = None if not request else json.dumps(request, ensure_ascii=False)
    with closing(_connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            created = conn.execute(
                "INSERT OR IGNORE INTO runs (run_id, status, request, created_at, updated_at) "
                f"VALUES (?, ?, ?, {_NOW}, {_NOW})",
                (run_id, STATUS_RUNNING, payload),
            )
            claimed = created.rowcount == 1
            if not claimed:
                reopened = conn.execute(
                    f"UPDATE runs SET status = ?, failure = NULL, updated_at = {_NOW} "
                    "WHERE run_id = ? AND status IN (?, ?)",
                    (STATUS_RUNNING, run_id, *RESTARTABLE_STATUSES),
                )
                claimed = reopened.rowcount == 1
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:  # pragma: no cover — 插入或重开的成功路径上必有一行
        raise RuntimeError(f"runs 里没有 run_id={run_id} 的行，插入与重开都没生效")
    return Claim(record=_record(row), claimed=claimed)


def finish(
    run_id: str,
    status: str,
    *,
    candidate: MindMapContent | None = None,
    failure: dict[str, Any] | None = None,
    mode: str | None = None,
) -> bool:
    """写终态。返回这一行有没有真的被改动。

    两条不对称的规矩，都是为了"已经付过钱的产出不许被一条迟到的收尾抹掉"：

    - `candidate` 用 `COALESCE`：传 None 表示"这次没有新草稿"，**不清空**库里
      已有的那一份。被取消的运行常常是"树已经生成完、用户关了页面"，落库那一步
      绝不能因为某个分支没拿到 candidate 就把草稿写成 NULL。
    - `status = done` 之后冻结：`WHERE status <> 'done'`。不做这个限制，一次超时
      取消就可能把"已经交付给用户的候选"改写成 `cancelled`，前端下次加载时就不
      再提示它了。

    `failure` 反过来是直接赋值：它是"这一次的终态为什么是这样"，属于最新事实，
    上一次的失败原因留在上面只会让人读错。
    """
    if status not in TERMINAL_STATUSES:
        raise ValueError(f"finish 只接受终态，收到 {status!r}")
    with closing(_connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            changed = conn.execute(
                "UPDATE runs SET status = ?, candidate = COALESCE(?, candidate), "
                "failure = ?, mode = COALESCE(?, mode), updated_at = " + _NOW +
                " WHERE run_id = ? AND status <> ?",
                (status, _dump_candidate(candidate),
                 None if failure is None else json.dumps(failure, ensure_ascii=False),
                 mode, run_id, STATUS_DONE),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return changed.rowcount == 1


def get(run_id: str) -> RunRecord | None:
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    return _record(row) if row else None


def pending(
    *,
    limit: int = 20,
    statuses: Sequence[str] = (STATUS_DONE,),
    unresolved_only: bool = True,
) -> list[RunRecord]:
    """有结果、而且后来没被保存过的运行，最新的在前。

    默认只要 `done`。前端展示时传 `(done, cancelled)`：被取消但已经产出结果的运行
    同样值得捡起来（那 40 秒就是这么省回来的），只是措辞不同（"上次被中断了"而不是
    "上次完成了"）。

    判据是 `applied_at`，不是"结果有没有写进 `mindmaps` 表"：那份内容可能被 PUT 到
    一张已有导图上（推不出新记录），也可能用户存成了另一张——用 `mindmaps` 反推
    只会得到一堆猜。

    `dismissed_at` 那半个条件已经删掉：没有"丢弃候选"这个动作了，而"用户没保存就
    走了"与"用户不要这份结果"在删掉候选层之后是同一件事——他现在可以撤销、可以
    直接改，也可以什么都不做。

    `unresolved_only=False` 时不去管"保存过没有"，只按状态列：那是查台账的用法
    （"上次那次失败到底怎么失败的"），与"提醒用户"是两件事，所以做成一个开关
    而不是两条 SQL。
    """
    if not statuses:
        return []
    placeholders = ", ".join("?" for _ in statuses)
    unresolved = ("AND candidate IS NOT NULL AND applied_at IS NULL"
                  ) if unresolved_only else ""
    with closing(_connect()) as conn:
        rows = conn.execute(
            f"""SELECT * FROM runs
                 WHERE status IN ({placeholders}) {unresolved}
                 ORDER BY updated_at DESC, run_id
                 LIMIT ?""",
            (*statuses, max(1, limit)),
        ).fetchall()
    return [_record(row) for row in rows]


def mark_applied(run_id: str) -> bool:
    """这次生成的结果**被保存进库了**。幂等：已经有时间戳就不再改。

    前端在保存成功之后调用一次。删掉候选层之后它是"生成过但没保存"的唯一反向证据：
    没有它，每次生成都会在下次进页面时重新问一遍"要不要捡回上次的结果"。
    """
    return _mark(run_id, "applied_at")


def _mark(run_id: str, column: str) -> bool:
    # 只有一个固定列名会走到这里，不是拼接外部输入。
    assert column == "applied_at"
    with closing(_connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            changed = conn.execute(
                f"UPDATE runs SET {column} = COALESCE({column}, {_NOW}) WHERE run_id = ?",
                (run_id,),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return changed.rowcount == 1


__all__ = [
    "Claim",
    "RESTARTABLE_STATUSES",
    "RunRecord",
    "STATUS_CANCELLED",
    "STATUS_DONE",
    "STATUS_FAILED",
    "STATUS_RUNNING",
    "TERMINAL_STATUSES",
    "begin",
    "finish",
    "get",
    "mark_applied",
    "pending",
]
