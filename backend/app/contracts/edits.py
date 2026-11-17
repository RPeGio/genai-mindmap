"""编辑日志的契约：一条改动长什么样，统计结果长什么样。

放在 `contracts/` 而不是 `mindmaps/edits.py`，是为了避开一个环：`SaveRequest`
（`contracts/mindmap.py`）要把事件放进请求体，而 `mindmaps/repository.py` 又要
import `contracts.mindmap`。事件模型留在仓储层的话，`contracts → mindmaps.edits →
mindmaps.repository → contracts` 就成环了，只能靠函数内 import 打补丁——那是症状，
不是解法。

四个刻意的取舍（数字怎么读全靠它们）：

  - **`before` / `after` 是摘要，不是内容。** `label` 存全文（≤200 字，统计要算
    长度），`detail` / `note` 只存 `"1800字"`。一份 2000 字的正文进日志表，它就
    变成了第二个 `mindmaps.content`；而"从 1800 字改成 700 字"本身就是情报。
  - **`field` 只在 `update` / `style` 上出现。** 这条规则让统计能自校验：
    `sum(by_field) == by_kind["update"] + by_kind["style"]`。新增与删除改的其实是
    父节点的 `children`，但把它们也标成 `children` 会让那条恒等式失效，而
    `by_kind` 已经把"加了多少、删了多少"说清楚了。
  - **`depth` 是节点层数（根=1）**，`remove` 事件填删除前所在层。`removed_depth`
    直接对它 `GROUP BY`："删掉的集中在第 3 层"就是"初次生成挖得太深"的证据。
  - **时间有两个。** `at` 是前端说的事件时刻（可能漂移：用户改系统时间、页面开了
    很久），`recorded_at` 是入库时刻（永远可信）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: 单次保存最多接受多少条事件。**与前端 `EDIT_EVENT_LIMIT` 同一个数**：前端先分批，
#: 用户就不用为"一次删掉整棵树"收一条 422；这里仍然校验，因为契约是最终裁判。
#: 超出部分**不是丢弃**：前端把它们留在队列里等下一次保存。
MAX_EVENTS_PER_SAVE = 500

#: 单条摘要的长度上限。`label` / `title` ≤200，`"第2位"` / `"A → B（是）"` 更短，
#: 400 是给"标题全文 + 少量装饰"留的余量。超了直接拒：那是形状错了，不是内容长。
MAX_SUMMARY_CHARS = 400

#: 与前端 `mindmap/edit-log.ts` 的三个联合类型逐字对应。写成 `Literal` 而不是 `str`：
#: 它们会进 OpenAPI，前端镜像那边是同一份字面量联合，两端对齐靠契约。
EditActor = Literal["user", "agent"]
EditKind = Literal["add", "remove", "update", "move", "edge", "style"]
EditField = Literal["label", "title", "detail", "note", "color", "icon"]


class EditEventIn(BaseModel):
    """一条改动（`POST/PUT /mindmaps` 请求体里的一项）。

    `extra="forbid"`：字段名漂移（比如前端写成 `nodeId`）必须当场 422。静默丢掉
    一条事件比拒收整次保存更糟——统计会少一块，而没人知道少的是哪一块。
    """

    model_config = ConfigDict(extra="forbid")

    actor: EditActor
    kind: EditKind
    #: 节点 id。`edge` 事件填边的**源**节点，`title` 事件没有节点。
    node_id: str | None = Field(default=None, max_length=100)
    field: EditField | None = None
    before: str | None = Field(default=None, max_length=MAX_SUMMARY_CHARS)
    after: str | None = Field(default=None, max_length=MAX_SUMMARY_CHARS)
    #: 层数，根是 1。上限给到 64 而不是 `MAX_TREE_DEPTH`：日志是历史记录，一张按
    #: 旧契约存的深树被删掉时，拒收会让整次保存失败，而它已经是事实。
    depth: int | None = Field(default=None, ge=1, le=64)
    #: 这次改动动的是哪个 run 的产物。只有"应用候选"那一步对得上，手工编辑留空。
    run_id: str | None = Field(default=None, max_length=100)
    #: 事件发生的时刻（前端 `new Date().toISOString()`）。**可为空**：拿不到合法
    #: ISO 时退回入库时刻，而不是拒收——少一个时间戳比少一条改动可接受得多。
    at: str | None = None

    @field_validator("at")
    @classmethod
    def _valid_iso_or_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            return None
        try:
            datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        return text


class EditEventRow(EditEventIn):
    """落库后的一行：载荷 + 自增 id + 服务端写入时刻。"""

    id: int
    map_id: str
    at: str
    recorded_at: str


class LabelShrunk(BaseModel):
    """标题被改短的统计。

    `avg_before` / `avg_after` 是**字数**（码点），只有 `count` 为 0 时才是 `None`。
    两个平均值的差（18.3 → 9.1）就是"缩短了多少"，比只看 count 有信息量得多：
    从 18 缩到 9 是"太长了"，从 18 缩到 17 只是"改了个字"。
    """

    count: int = 0
    avg_before: float | None = None
    avg_after: float | None = None


class EditSummary(BaseModel):
    """`GET /mindmaps/{id}/edits/summary` 的响应。

    两条恒等式让数字可以先自校验再读结论：

      - `sum(by_actor) == total`；
      - `sum(by_field) == by_kind["update"] + by_kind["style"]`。

    对不上就说明有写入路径绕过了契约，而不是"统计有点怪"。
    """

    total: int
    by_actor: dict[str, int]
    by_kind: dict[str, int]
    by_field: dict[str, int]
    label_shrunk: LabelShrunk
    #: 删除的节点按层分布（根=1）。键在 JSON 里是字符串，前端声明为 `Record<string, number>`。
    removed_depth: dict[int, int]


__all__ = [
    "EditActor",
    "EditEventIn",
    "EditEventRow",
    "EditField",
    "EditKind",
    "EditSummary",
    "LabelShrunk",
    "MAX_EVENTS_PER_SAVE",
    "MAX_SUMMARY_CHARS",
]
