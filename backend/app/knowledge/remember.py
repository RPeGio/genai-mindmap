"""生成时把材料收进知识库，并把临时 id 换成持久 id。

**为什么是"生成时入库"，而不是"上传时入库"**：上传那一刻还没有"用户要不要用它"
的信息，把每次上传都写进库，库会很快被试用过的废文件填满，而 `prune` 的保留依据
是 `last_used_at`——那些文件从来没被用过，会一直躺在最前面。生成时入库的口径是
"这张图用到了它"，与库存在的理由一致。

**这套逻辑要解决的是 id 不一致**。解析接口给每份材料发的是本次请求内有效的
uuid，而库里的记录有它自己的持久 id（正文 sha256 只用来判重、不充当 id）。
两套 id 并存时，导图里记的 `source_refs.source_id` 在重新打开后就谁也找不到
——引用计数、点开看原文、会话内反向索引全部失效。所以入库之后必须把 id 换掉，
并把"旧 id → 新 id"告诉前端。

**开关与检索开关是两件事**。`MINDMAP_LIBRARY_SEARCH` 管"生成时要不要从库里捞
材料参与"；`MINDMAP_LIBRARY_REMEMBER` 管"这次用到的材料要不要存进库"。线上把
检索关着（记忆模块还没上）也需要入库，否则知识库入口永远是空的。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field

from app.contracts.source import Source
from app.knowledge import library as knowledge_library

logger = logging.getLogger(__name__)

# 默认开：入库是知识库入口能显示出东西的唯一来源，默认关等于这个入口永远空白。
# 需要一个"完全不写库"的部署时，在环境里置 0。
REMEMBER_ENV = "MINDMAP_LIBRARY_REMEMBER"

# 入库整段的总预算。入库发生在第一帧之前，SQLite 被并发写锁挡住时一次 connect
# 最久等 10 秒——50 份串起来就是几分钟静默，而客户端这时连"连接已建立"都看不到。
# 到点就放弃剩下的材料、按临时 id 继续生成：记上账是旁路，不能压过生成本身。
REMEMBER_BUDGET_SECONDS = 5.0

# 自动淘汰（`library.prune()`：90 天 / 200 份）的两个闸门。入库是唯一的写入
# 来源，把淘汰挂在它后面就不需要额外的定时任务；但"每次生成都淘汰一遍"没必要，
# 所以：条数要明显超过保留上限（比 200 留 20% 余量，避免刚好 201 份就开始删），
# 且进程内每小时最多检查一次。真正的删除放到后台线程——prune 要扫全表，
# 不该出现在首帧之前的关键路径上。
PRUNE_TRIGGER_COUNT = 240
PRUNE_INTERVAL_SECONDS = 3600.0
_prune_checked_at = 0.0
_prune_lock = threading.Lock()


def remember_enabled() -> bool:
    return os.getenv(REMEMBER_ENV, "1").strip() != "0"


@dataclass(frozen=True)
class Remembered:
    """入库的结果：可以继续往下用的材料，以及需要通知前端的 id 变更。"""

    sources: list[Source]
    # 旧 id → 持久 id。为空表示没有任何 id 变化（包括整个开关关掉的情形）。
    mapping: dict[str, str] = field(default_factory=dict)


def _maybe_prune() -> None:
    """到点就把淘汰交给后台线程；首帧之前的关键路径上只多一次 `COUNT`。"""
    global _prune_checked_at

    now = time.monotonic()
    with _prune_lock:
        if now - _prune_checked_at < PRUNE_INTERVAL_SECONDS:
            return
        # 先记账再判断：读条数失败时也不该每分钟重试一次，一小时后再说。
        _prune_checked_at = now
        try:
            total = knowledge_library.count()
        except (sqlite3.Error, OSError) as exc:
            logger.warning("读不到知识库条数（%s），本次跳过淘汰：%s",
                           type(exc).__name__, str(exc)[:160])
            return
        if total <= PRUNE_TRIGGER_COUNT:
            return
        logger.info("知识库 %d 份，超过阈值 %d，后台淘汰一次", total, PRUNE_TRIGGER_COUNT)
        threading.Thread(target=_prune_once, name="library-prune", daemon=True).start()


def _prune_once() -> None:
    """执行一次淘汰；这是守护线程里跑的东西，异常只记日志、不许往外抛。"""
    try:
        removed = knowledge_library.prune()
    except (sqlite3.Error, OSError) as exc:
        logger.warning("知识库自动淘汰失败（%s）：%s", type(exc).__name__, str(exc)[:160])
    except Exception:  # noqa: BLE001 - 守护线程里没人接异常，兜住并留栈
        logger.exception("知识库自动淘汰出现未预期异常")
    else:
        if removed:
            logger.info("知识库自动淘汰 %d 份来源（保留 90 天 / 200 份）", removed)


def remember_sources(sources: list[Source]) -> Remembered:
    """一次连接、一个事务地把材料收进库，返回换了 id 的材料清单。

    **单份失败不影响整次生成**：入库是记账，不是生成的前置条件。素材正文已经
    在请求里了，这一轮照样能生成导图，只是这批材料下次不能从库里复用。
    失败时保留原 uuid——那意味着这张图的引用重新打开后指不到原文，但至少
    本次会话内的引用（前端按会话内的 source 解析）仍然有效。

    **批量化是因为入库发生在首帧之前**：几十份材料各开一次连接、各撞一次写锁
    超时，就是几十次 10 秒的静默，而这时 SSE 还一帧都没发。现在整批共用一个
    连接一个事务，并且有总预算（`REMEMBER_BUDGET_SECONDS`），超预算的剩余材料
    按临时 id 继续——这批记上账比记得全重要。

    因此这里兜到底：已知异常（空正文、库故障）按 warning 记，其余任何意外按
    exception 记完整栈——这条支路不该有能力中断生成，异常一旦穿出去就会被
    `agent/router.py` 兜成 `agent_failed`，整轮生成不跑。

    **淘汰挂在入库后面**：`prune` 的保留策略（90 天 / 200 份）写死在库里，但
    没有定时任务去调它；库只增不减，早晚把磁盘和列表接口一起拖慢。这里按
    `PRUNE_TRIGGER_COUNT` / `PRUNE_INTERVAL_SECONDS` 触发一次，删除动作在后台
    线程里跑——记账可以慢一点，首帧不能。
    """
    if not sources or not remember_enabled():
        return Remembered(sources=list(sources))

    deadline = time.monotonic() + REMEMBER_BUDGET_SECONDS
    try:
        persistent_ids = knowledge_library.ingest_many(sources, deadline=deadline)
    except Exception as exc:  # noqa: BLE001 - 入库是旁路，必须兜住一切
        # 批量入口已逐份兜底，走到这里说明连连接都建不起来：记完整栈，全按原样用。
        logger.exception(
            "材料没能进知识库（%s: %s），这一轮按原样继续",
            type(exc).__name__, str(exc)[:160])
        return Remembered(sources=list(sources))

    mapping: dict[str, str] = {}
    kept: list[Source] = []
    for source, persistent_id in zip(sources, persistent_ids):
        if not persistent_id or persistent_id == source.id:
            kept.append(source)
            continue
        mapping[source.id] = persistent_id
        kept.append(source.model_copy(update={"id": persistent_id}))
    _maybe_prune()
    return Remembered(sources=kept, mapping=mapping)


__all__ = [
    "PRUNE_INTERVAL_SECONDS",
    "PRUNE_TRIGGER_COUNT",
    "REMEMBER_BUDGET_SECONDS",
    "REMEMBER_ENV",
    "Remembered",
    "remember_enabled",
    "remember_sources",
]
