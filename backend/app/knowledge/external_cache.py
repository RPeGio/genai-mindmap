"""外部检索结果的 SQLite 缓存：同一个 query 24 小时内只打一次网。

## 为什么按归一化 query 缓存，而不是复用入库那条去重

`library.ingest` 已经有 `content_hash` 去重（**内容级**）：两次搜回来的正文一样时，
第二份不会入库。但**搜索动作本身每次都执行**——同一主题连续生成两次，就搜两次网、
各花 4–5 秒。内容级去重省的是库里的行，省不掉那两次 HTTP。

所以这张表的键是**归一化后的 query**，值是一整批命中的原始字段。命中时
`search_external` 直接返回上次的命中、跳过全部 HTTP；转 `Source`、入库、检索、引用
校验照常走——入库那一步会因 `content_hash` 判重变成 no-op，这是**对的**：缓存省的是
"再搜一次"，不是"再入库一次"。

## 归一化：只折叠写法，不折叠意思

四条，够用且都可复现：

  - `NFKC`：全角→半角（`（）`→`()`、`Ａ`→`A`）、兼容字符（`①`→`1`）。中文主题撞
    全角标点是常态，不折叠的话 `碳中和（技术）` 与 `碳中和(技术)` 是两条缓存行；
  - 折叠全部空白（含 U+3000 全角空格）为单个半角空格，并去首尾；
  - 小写化。用 `lower()` 而不是 `casefold()`：与 `knowledge.service._token_list` 的
    小写化同口径——两处对"什么算同一个词"必须只有一个答案。
  - 不做任何同义词 / 去限定语 / 去标点的折叠。

最后一条是这张表能力的边界，不是没做完：`碳中和` 与 `碳达峰` 是两条独立的行，
`碳中和` 与 `碳中和，用鱼骨图` 也是。把"意思相近"折叠起来需要一个词表，而词表判错
的代价是**用户换了个主题却拿到上一题的资料**——一份看起来有出处的假依据，比多重搜
一次网糟得多。真实用例里那个代价是可量化的：`runs` 表 16 次运行里有 5 次围绕
`机器学习`（`机器学习` / `机器学习，用鱼骨图` / `机器学习，树状图` /
`机器学习的主要方向`），归一化只认得出其中**字面相同的那 2 次**。

## 存哪

与导图、块向量共库（`mindmaps.repository.db_path()`），理由与 `vector_cache` 同一份：
少一个文件、一套备份、一套"用例要记得改路径"的环境变量。风险也是同一个：SQLite 的
库级写锁在毫秒级争用下由 `timeout=10` 吸收，真撞上时最坏结果是这轮缓存没写进去
（下次重新搜），不会破坏导图记录。

## 上限与清理

写入前先删过期行（一条 `DELETE`，`searched_at` 上有索引），再判 `MAX_ROWS`。到线就
只读不写——这张表是纯缓存，丢掉它只会多一次网，不会丢数据。

**缓存不可用不是错误**：`_open()` 的任何失败都返回 None，读写各自降级成"没有缓存"。
库里这张表被删了、磁盘满了、`MINDMAP_DB_PATH` 指向一个只读目录——三种都只该让这次
生成多花一次网，不该让生成失败。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import unicodedata
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any

from app.contracts.source import ISO_SECONDS_FORMAT
from app.db.conn import Connection
from app.db.errors import DatabaseError
from app.mindmaps import repository

logger = logging.getLogger(__name__)

CACHE_TABLE = "external_cache"

# 存活时间：24 小时。这个数是两件事的折中，两边都有实测锚点：
#
#   - **够长**：覆盖"同一轮对话里反复改主题"这个真实场景。`runs` 表里
#     `rust学习路线` 的两次生成相隔 48 秒、`刑法胆大如斗…` 相隔 11 秒、`你好` 相隔
#     5 秒——用户改一个字再生成是这个功能最常见的用法，而这类间隔全在分钟量级；
#   - **够短**：维基条目几天内变化不大，24 小时里"同一条目被重写"几乎不会发生；
#     真发生了，代价是这一天里用户看到的是昨天那个版本的正文，而正文本身仍然
#     逐字可校验（引用校验拿的是缓存里的正文，两边一致——不会出现"引用对不上原文"）。
#
# 一天以上的间隔就重新搜：那多半是另一次会话、另一个话题了，命中率本来就低，
# 而"隔了一天还拿旧正文"开始变得可疑。
TTL_SECONDS = 24 * 3600

# 行数上限。一条 payload 是 3 条命中的完整正文（每条最多 `TEXT_MAX_CHARS`=8000 字，
# 中文 UTF-8 约 24 KB），3 条约 72 KB；5000 行约 360 MB，已经是该被看见的体积。
# 到线停写而不是淘汰：淘汰要选一个"删谁"的规则，而这张表里没有比 `searched_at`
# 更好的信号，过期清理已经把真正该删的删完了。
MAX_ROWS = 5000

# 开关。默认开；`0/off/no/...` 时读也读不到、写也不写。
#
# **测量脚本必须设成 0**（`scripts/measure_external*.py` 已经这么做）：那两个脚本的
# 产出是"这次检索花了多少秒、返回几条"，命中缓存会让耗时变成 0、候选池变成上一次的
# 快照——那不是测量，是复述。
CACHE_ENV = "MINDMAP_EXTERNAL_CACHE"
_FALSEY = frozenset({"0", "off", "no", "false", "disable", "disabled", "none"})

#: 建表建索引语句。**生产路径不再执行它们**（表由 `db.schema.init_schema()` 建），
#: 保留是因为测试与一次性脚本会直接拿它们兜"空库上也能跑"。热路径上别调：PG 上每条
#: `IF NOT EXISTS` 都是真实往返，实测 2.458ms。
_CREATE = f"""CREATE TABLE IF NOT EXISTS {CACHE_TABLE} (
    query_hash   TEXT PRIMARY KEY,
    query        TEXT NOT NULL,
    payload      TEXT NOT NULL,
    limit_value  INTEGER NOT NULL,
    hit_count    INTEGER NOT NULL DEFAULT 0,
    searched_at  TEXT NOT NULL
)"""

# 过期清理与 TTL 判定共用的一条索引。`searched_at` 是唯一会被比较的列。
_INDEX = (f"CREATE INDEX IF NOT EXISTS {CACHE_TABLE}_searched_at "
          f"ON {CACHE_TABLE} (searched_at)")


def cache_enabled() -> bool:
    """缓存开关，**默认开**。显式写 `0 / off / no` 才算关。"""
    return str(os.getenv(CACHE_ENV, "1")).strip().lower() not in _FALSEY


def normalize_query(raw: str) -> str:
    """归一化：NFKC → 折叠空白 → 小写。规则与理由见模块 docstring。

    **只作用于键，不作用于发给源的那条 query**：`search_external` 拿原始文本去检索
    （大小写、全角标点都可能影响源站的匹配），归一化只发生在"这两次是同一个 query
    吗"这个判断上。两者混在一起会让缓存悄悄改写检索行为——那是两类完全不同的改动。
    """
    value = unicodedata.normalize("NFKC", raw or "")
    return " ".join(value.split()).lower()


def query_hash(raw: str) -> str:
    """缓存键：归一化 query 的 sha256 前 32 位（128 bit）。

    与 `vector_cache.text_hash` 同一个取法：这个规模下碰撞概率可以忽略，而键短一半
    让主键索引更省。**不含 limit / 语种 / 源清单**——那三样不是 query 的身份，而是
    同一次 query 的参数（limit 另存一列判，见 `get`）。
    """
    return hashlib.sha256(normalize_query(raw).encode("utf-8")).hexdigest()[:32]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _open() -> Connection | None:
    """开连接。任何失败都返回 None：缓存不可用不是错误。

    **这里不再建表建索引**：`external_cache` 由 `db.schema.init_schema()` 在建库时
    建好。SQLite 时代重放 DDL 是空转；PG 上每条都是真实往返（实测 2.458ms），而这
    是检索热路径——每查一次缓存都要走一次。
    """
    try:
        return repository.connect()
    except (DatabaseError, OSError) as exc:
        logger.warning("外部检索缓存不可用：%s", type(exc).__name__)
        return None


def _parse_stamp(value: str) -> datetime | None:
    """`searched_at` → 带时区的 UTC 时间；解不出返回 None（当成过期）。"""
    try:
        return datetime.strptime(value, ISO_SECONDS_FORMAT).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def get(raw_query: str, *, limit: int) -> tuple[list[dict[str, Any]], int] | None:
    """取一条缓存：返回 `(命中记录列表, 命中过几次)`，没有/过期/不可用返回 None。

    三种"当没有"的情形，每一种都有具体理由：

      - **过期**（`searched_at` 早于 `TTL_SECONDS`）：见那个常量；
      - **缓存那次要的条数比这次少**（`limit_value < limit`）：缓存里存的是那次检索
        的**最终产物**（已过相关性门、已交替取样、已截到 limit）。拿 1 条去回答
        "要 3 条"的要求是错答案，不是近似答案——前 1 条确实是前 1 条，但缺的两条
        会让下游以为"这个主题只有一条资料"；
      - **`payload` 解不出数组 / 元素不是对象**：这张表可能被人手动改过、可能是旧版本
        写下的形状。缓存是**外部数据**，读它和读源的响应一样要挑形状。

    `hit_count` 由 `get` 自己 `UPDATE` 递增：调用方拿到的是**这次之前**的次数，日志
    里写"第 N 次命中"正好从 1 起。
    """
    if not cache_enabled():
        return None
    key = query_hash(raw_query)
    conn = _open()
    if conn is None:
        return None
    deadline = (_now() - timedelta(seconds=TTL_SECONDS)).strftime(ISO_SECONDS_FORMAT)
    try:
        with closing(conn), conn:
            row = conn.execute(
                f"SELECT payload, limit_value, hit_count, searched_at FROM {CACHE_TABLE} "
                f"WHERE query_hash = ?", (key,),
            ).fetchone()
            if row is None:
                return None
            stamp = _parse_stamp(row["searched_at"])
            if stamp is None or stamp.strftime(ISO_SECONDS_FORMAT) < deadline:
                return None
            if int(row["limit_value"] or 0) < int(limit):
                return None
            try:
                records = json.loads(row["payload"])
            except (TypeError, ValueError):
                return None
            if not isinstance(records, list):
                return None
            kept = [item for item in records if isinstance(item, dict)]
            if len(kept) != len(records):
                return None
            conn.execute(
                f"UPDATE {CACHE_TABLE} SET hit_count = hit_count + 1 WHERE query_hash = ?",
                (key,),
            )
            return kept, int(row["hit_count"] or 0)
    except (DatabaseError, OSError) as exc:
        logger.warning("外部检索缓存读取失败：%s", type(exc).__name__)
        return None


def put(raw_query: str, *, limit: int, records: list[dict[str, Any]]) -> bool:
    """写一条缓存，返回是否真的落库。

    `records` **可以是空列表**：那是"这个 query 搜过，确实 0 条命中"，是个有效结论
    （见 `external._worth_caching` 的判据）。失败不进这里——失败与"0 条"在返回值上
    都长成空列表，但前者不该被记成结论。

    `ON CONFLICT DO UPDATE` 而不是 `INSERT OR IGNORE`：同一个 query 隔一天重搜之后，
    这一行必须换成新结果（旧结果的 `searched_at` 已经过期，留着它等于永远返回旧正文）。
    `hit_count` 同时清零——它记的是"这次搜索结果被复用了多少次"。
    """
    if not cache_enabled():
        return False
    key = query_hash(raw_query)
    stamp = _now().strftime(ISO_SECONDS_FORMAT)
    body = json.dumps(records, ensure_ascii=False, separators=(",", ":"))
    conn = _open()
    if conn is None:
        return False
    deadline = (_now() - timedelta(seconds=TTL_SECONDS)).strftime(ISO_SECONDS_FORMAT)
    try:
        with closing(conn), conn:
            # 过期行顺手清掉：这张表没有别的 GC，而"过期"正是唯一确定的删除判据。
            # 放在写入前而不是定时任务里，是因为写入是这条链上唯一的低频操作。
            conn.execute(f"DELETE FROM {CACHE_TABLE} WHERE searched_at < ?", (deadline,))
            total = conn.execute(f"SELECT COUNT(*) AS total FROM {CACHE_TABLE}").fetchone()["total"]
            if total >= MAX_ROWS:
                logger.info("外部检索缓存已达上限 %d 行，本次只读不写", MAX_ROWS)
                return False
            conn.execute(
                f"INSERT INTO {CACHE_TABLE} "
                f"(query_hash, query, payload, limit_value, hit_count, searched_at) "
                f"VALUES (?, ?, ?, ?, 0, ?) "
                f"ON CONFLICT(query_hash) DO UPDATE SET "
                f"query = excluded.query, payload = excluded.payload, "
                f"limit_value = excluded.limit_value, hit_count = 0, "
                f"searched_at = excluded.searched_at",
                (key, normalize_query(raw_query)[:200], body, int(limit), stamp),
            )
            return True
    except (DatabaseError, OSError) as exc:
        logger.warning("外部检索缓存写入失败：%s", type(exc).__name__)
        return False


def clear() -> int:
    """清空整张表，返回删掉几行。**只给用例与人工排障用**。

    生产链路里没有任何一处该调它：命中过期由 `get` 判、过期行由 `put` 顺手清。
    用例要用它是因为"同一份夹具、同一个主题"在多个断言里出现，而缓存会把第二次
    调用变成第一次的复述——那种失败看起来像"缓存坏了"，实际上是"这个用例没隔离"。
    """
    conn = _open()
    if conn is None:
        return 0
    try:
        with closing(conn), conn:
            cursor = conn.execute(f"DELETE FROM {CACHE_TABLE}")
            return int(cursor.rowcount or 0)
    except (DatabaseError, OSError) as exc:
        logger.warning("外部检索缓存清空失败：%s", type(exc).__name__)
        return 0
