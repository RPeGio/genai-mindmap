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
from dataclasses import dataclass, field

from app.contracts.source import Source
from app.knowledge import library as knowledge_library

logger = logging.getLogger(__name__)

# 默认开：入库是知识库入口能显示出东西的唯一来源，默认关等于这个入口永远空白。
# 需要一个"完全不写库"的部署时，在环境里置 0。
REMEMBER_ENV = "MINDMAP_LIBRARY_REMEMBER"


def remember_enabled() -> bool:
    return os.getenv(REMEMBER_ENV, "1").strip() != "0"


@dataclass(frozen=True)
class Remembered:
    """入库的结果：可以继续往下用的材料，以及需要通知前端的 id 变更。"""

    sources: list[Source]
    # 旧 id → 持久 id。为空表示没有任何 id 变化（包括整个开关关掉的情形）。
    mapping: dict[str, str] = field(default_factory=dict)


def remember_sources(sources: list[Source]) -> Remembered:
    """逐份入库，返回换了 id 的材料清单。

    **单份失败不影响整次生成**：入库是记账，不是生成的前置条件。素材正文已经
    在请求里了，这一轮照样能生成导图，只是这批材料下次不能从库里复用。
    失败时保留原 uuid——那意味着这张图的引用重新打开后指不到原文，但至少
    本次会话内的引用（前端按会话内的 source 解析）仍然有效。
    """
    if not sources or not remember_enabled():
        return Remembered(sources=list(sources))

    mapping: dict[str, str] = {}
    kept: list[Source] = []
    for source in sources:
        try:
            persistent_id = knowledge_library.ingest(source)
        except (ValueError, sqlite3.Error, OSError) as exc:
            # ValueError 是空正文（不该进库），其余是库本身的问题。
            logger.warning(
                "材料没能进知识库（%s: %s），本次仍按原样使用：%s",
                type(exc).__name__, str(exc)[:160], source.filename or source.id,
            )
            kept.append(source)
            continue
        if persistent_id == source.id:
            kept.append(source)
            continue
        mapping[source.id] = persistent_id
        kept.append(source.model_copy(update={"id": persistent_id}))
    return Remembered(sources=kept, mapping=mapping)


__all__ = [
    "REMEMBER_ENV",
    "Remembered",
    "remember_enabled",
    "remember_sources",
]
