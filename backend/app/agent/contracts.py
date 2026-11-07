"""agent 循环的契约：请求、预算、模型交互、事件流。

这里只放**跨模块边界的数据结构**，不放业务逻辑。工具的真实实现（生成、展开、
检索、改树）在 `tools.py`，模型适配在 `provider.py`，循环在 `service.py`。

事件用 discriminated union 而不是一个大而全的模型：帧的种类会随需求增长，
用一个带十几个可选字段的类，调用方永远分不清"这个字段为 None 是因为不该有，
还是因为忘了填"。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any, Callable, Literal, Protocol, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.contracts.mindmap import MindMapContent
from app.contracts.ops import OpBatch
from app.contracts.source import Source

# 与 pipeline/service.py::MAX_INPUT_CHARS 同口径。前端不再把材料截成 1000 字，
# 改走 sources 整体传，这条是唯一的边界。
MAX_SOURCE_CHARS = 20000


# ---------------------------------------------------------------- 预算


class RunBudget(BaseModel):
    """一次 run 的资源上限。

    `max_model_calls` 数的是**真实调用次数**，不是外层轮数：循环里每一轮 provider
    调用、以及工具内部的每一次 `chat_async` 调用都算一次，共用 `RunContext` 上
    同一个账本。取 5 是实测值：探针显示一次响应能给出 2–3 个工具调用，
    「查 → 建 → 改 → 修」四段用 5 轮足够；给 8 轮只是把跑飞的成本藏进去。

    `max_repairs` 约束的是**工具执行失败次数**（`ToolResult.ok == False` 的次数），
    与模型调用次数无关：一次成功的模型响应里发三个工具、其中两个失败，只消耗
    这里两次。全轮共享而不是按轮重置——按轮重置等于给模型无限次重试，
    每次重试都照样计费。
    """

    model_config = ConfigDict(extra="forbid")
    max_model_calls: int = Field(default=5, ge=1, le=20)
    max_tool_calls: int = Field(default=8, ge=1, le=50)
    max_seconds: float = Field(default=120.0, gt=0, le=600)
    # 这是**单次**模型调用的输出上限，不是整轮累计——它直接作为 provider 请求里
    # 的 `max_tokens` 发出去，每次调用都按这个值重新计。整轮累计的上界是
    # 「这个值 × 实际调用次数」，而调用次数由 `max_model_calls` 约束，因此
    # 不需要再加一个"累计输出"字段：两个上界相乘已经封顶。
    max_output_tokens: int = Field(default=32768, ge=1024, le=131072)
    # 容忍几次工具执行失败。模型第一次把 label 写错位置是常态，它看到错误后
    # 下一轮会改对；超过这个次数说明它卡住了，继续烧调用没有意义。
    # 计的是累计失败数，不是"连续失败数"：成功的工具调用不会把计数清零。
    max_repairs: int = Field(default=1, ge=0, le=5)


# ---------------------------------------------------------------- 模型交互


class ToolCall(BaseModel):
    """模型发起的一次工具调用。

    `raw_arguments` 与 `parse_error` 是为了区分两种失败：模型给了合法的 JSON 但
    字段不对（交给 ToolSpec 的参数模型报错），还是模型把 JSON 写坏了（这里就
    记下来）。两者都要能变成一条可回给模型的具体错误，而不是整个 run 崩掉。
    """

    call_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=100)
    arguments: dict[str, Any] = Field(default_factory=dict)
    raw_arguments: str = ""
    parse_error: str | None = None


class Completion(BaseModel):
    """一轮模型调用的结果。

    `message` 原样保存，因为它要连同 `tool_calls` 一起回填进下一轮的消息历史——
    少了它，模型看到的"自己说过的话"就是不完整的，`tool_call_id` 也对不上。
    """

    message: dict[str, Any]
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)


class ToolProvider(Protocol):
    """模型适配层接口。

    抽这一层不是为了好看：循环里的逻辑（结果配对、幂等、去重、预算、终止判断）
    全是最容易出错也最该被测的部分，但它们跟"谁来产生工具调用"无关。有了接口，
    测试可以用一个按脚本返回预设结果的实现驱动循环，不依赖网络、不耗额度、
    不会随机失败。
    """

    async def complete_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        budget: RunBudget,
    ) -> Completion: ...


class ToolResult(BaseModel):
    """工具执行结果。

    `content` 回给模型（自然语言或 JSON 串），`data` 只用于事件流与后续逻辑，
    不塞进消息历史——工具的内部数据（比如完整的新树）进上下文会迅速吃满窗口。
    """

    ok: bool
    content: str
    data: dict[str, Any] = Field(default_factory=dict)


@dataclass
class ToolSpec:
    """一个可被模型调用的工具。

    用 dataclass 而不是 BaseModel：要装 `args_model` 这个类和 `fn` 这个函数，
    pydantic 存它们需要 arbitrary_types_allowed，徒增噪音。

    参数校验用 pydantic 模型而不是 JSON Schema 库：`jsonschema` 在本机是 docling
    的传递依赖，并不在 requirements.txt 里，靠它等于给 CI 埋一个"本地能过、
    CI 崩"的雷。
    """

    name: str
    description: str
    args_model: type[BaseModel]
    fn: Callable[[Any, "RunContext"], Any]
    # 只读工具才允许按参数去重。写操作（改树）即使参数完全相同，第二次执行时
    # 树的状态已经变了，返回上次的结果等于谎报成功。
    readonly: bool = False

    def schema(self) -> dict[str, Any]:
        """转成 OpenAI tools 格式。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.args_model.model_json_schema(),
            },
        }


@dataclass
class RunContext:
    """一次 run 的可变状态。

    跨轮存活，所以不含任何与模型对话相关的东西——那些在 `service.run()` 的
    局部变量里，生命周期更短也更清楚。
    """

    request: "AgentRunRequest"
    candidate: MindMapContent | None = None
    revision: int = 0
    batches: list[OpBatch] = field(default_factory=list)
    sources_by_id: dict[str, Source] = field(default_factory=dict)
    # call_id -> (参数指纹, 结果)。存指纹是为了识别"同一个 id 换了参数"这种协议违规，
    # 那必须报错而不是返回旧结果。
    executed: dict[str, tuple[str, "ToolResult"]] = field(default_factory=dict)
    # (工具名, 规范化参数) -> 结果。只对只读工具生效。
    dedupe: dict[str, "ToolResult"] = field(default_factory=dict)
    # 模型调用的**权威账本**：外层每一轮 provider 调用与工具内部每一次 chat_async
    # 调用共用这一个计数。额度必须在请求发出**之前**通过 `reserve_model_call()`
    # 占掉——事后记账等于先花钱后记账，工具执行期间预算就会被超支使用。
    model_calls_used: int = 0
    # 工具内部直接调模型的次数，由工具自己上报。它是对账兜底与观测值，
    # **不**参与判停：判停看 `model_calls_used`，只有它同时覆盖外层与工具内部。
    extra_model_calls: int = 0

    def reserve_model_call(self) -> bool:
        """占掉一次模型调用额度，拿不到返回 False。

        循环与工具都走这里，所以这段判断只该有一份实现：两处各写一份判断、
        各记一份数，迟早出现「判的时候以为还有额度、记的时候又多记一次」的
        分叉——预算为 1 的 run 实测发出过 4 次真实请求，就是这么来的。
        """
        if self.model_calls_used >= self.request.budget.max_model_calls:
            return False
        self.model_calls_used += 1
        return True


# ---------------------------------------------------------------- 请求


class AgentRunRequest(BaseModel):
    """一次 agent 运行。

    `snapshot` 是前端当前的树。服务端**不**从数据库重读——用户可能有一堆没保存
    的编辑，重读数据库等于把它们丢掉。
    """

    model_config = ConfigDict(extra="forbid")
    goal: str = Field(min_length=1, max_length=1000)
    intent: Literal["create", "expand", "edit"] = "create"
    # 同一句主题换一种拆解方法，结构就不同（表在 `generation.service.THINKING_MODELS`）。
    # 用 Literal 而不是自由字符串：拼错的值会被 `model_config` 挡在开流之前，
    # 否则它会静默退化成 default，用户在下拉里选的东西等于没生效。
    thinking_model: Literal[
        "default", "first-principles", "feynman", "systematic", "critical"
    ] = "default"
    snapshot: MindMapContent | None = None
    selected_ids: list[str] = Field(default_factory=list, max_length=50)
    sources: list[Source] = Field(default_factory=list, max_length=50)
    budget: RunBudget = Field(default_factory=RunBudget)

    @model_validator(mode="after")
    def sources_within_budget(self) -> "AgentRunRequest":
        """来源正文合计要有个上限。

        前端不再把材料截断成 1000 字（那会丢绝大部分内容），改走 `sources`
        整体传过来；但整份传过来就没有任何边界了——`Source.text` 本身没有
        长度限制。这里用与 pipeline 同一口径的 20000 字兜住，超了在开流前
        就拒绝，而不是等模型那边炸掉。
        """
        total = sum(len(source.text or "") for source in self.sources)
        if total > MAX_SOURCE_CHARS:
            raise ValueError(f"来源正文合计 {total} 字，超过 {MAX_SOURCE_CHARS} 字上限，请精简后重试")
        return self


# ---------------------------------------------------------------- 事件流


class _EventBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seq: int = Field(ge=1)


class StageEvent(_EventBase):
    type: Literal["stage"] = "stage"
    stage: str
    detail: str = ""


class ToolStartEvent(_EventBase):
    type: Literal["tool_start"] = "tool_start"
    call_id: str
    name: str
    # 只给摘要：完整参数里可能带整份来源正文，不该出现在事件流里
    args_summary: dict[str, Any] = Field(default_factory=dict)


class ToolEndEvent(_EventBase):
    type: Literal["tool_end"] = "tool_end"
    call_id: str
    name: str
    ok: bool
    result_summary: dict[str, Any] = Field(default_factory=dict)
    latency_ms: int = 0


class OpsEvent(_EventBase):
    type: Literal["ops"] = "ops"
    call_id: str
    batch: OpBatch


class SnapshotEvent(_EventBase):
    type: Literal["snapshot"] = "snapshot"
    content: MindMapContent


class DoneEvent(_EventBase):
    type: Literal["done"] = "done"
    content: MindMapContent | None = None
    usage: dict[str, Any] = Field(default_factory=dict)


class ErrorEvent(_EventBase):
    type: Literal["error"] = "error"
    code: str
    message: str
    # 预算耗尽时说明是哪一个，界面据此给可操作的提示
    limit: str | None = None


AgentEvent = Annotated[
    Union[StageEvent, ToolStartEvent, ToolEndEvent, OpsEvent, SnapshotEvent, DoneEvent, ErrorEvent],
    Field(discriminator="type"),
]
