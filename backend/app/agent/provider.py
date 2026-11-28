"""模型适配层：把一轮「消息 + 工具定义」变成 `Completion`。

单独成文件是为了可替换。循环、预算、幂等、去重这些逻辑都在 `service.py`，
它们不该因为换模型供应商而改一行——所以这里用 Protocol 定接口，真实实现只有
一个 `OpenAICompatibleProvider`，测试里换成按脚本回放的假实现。

类名不带供应商名是刻意的：它只依赖 OpenAI 兼容的三件事——`tools` 能真的被调用、
`stream: true` 能出增量、`max_tokens` 有上限可学（见 `docs/DEPLOY.md` 的
「换模型服务商」）。DeepSeek、commandcode 这些只是同一份代码的两组环境变量。

配置口径与 `generation/service.py::chat_async` 完全一致（同一组环境变量、
同一个错误类型、同样的 2MB 响应上限）。刻意复用它的 `_config_full()` 而不是
自己再读一遍环境变量：两处各写一份，迟早会出现「生成接口能连、agent 连不上」
这种排查半天的分歧。

**免费档的候选序列也在这里**：`resolve()` 把模型名换成 `:free` 那个之后，
这一层要负责"上游偶发 429 就换下一个免费模型再试一次"。放在这个文件而不是
`service.py` 的循环里，因为那里一次只认一个模型名——它拿到的是已经解析好的
`Completion`，不知道自己被换过。
"""
from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.agent.contracts import Completion, RunBudget, ToolCall
from app.generation import free_models as _free_models
from app.generation.service import (
    CONTEXT_TOKEN_LIMIT,
    FREE_MAX_OUTPUT_TOKENS,
    FREE_MIN_OUTPUT_TOKENS,
    UNAVAILABLE_STATUSES,
    GenerationError,
    # 完整配置（含免费档那两样：`reasoning_effort` 与"这次走的是免费模型"）。
    # 三元组版本给其余调用方，这里要的是完整的那份——理由见 `_config_full()`。
    _config_full,
    # 「这个模型暂时不可用」的异常类型同源，见下面 `_UNAVAILABLE_STATUSES` 的注释。
    _ModelUnavailable,
    # 免费平台的每日额度用完（OpenCode 实测 429 + 正文 `FreeUsageLimitError`）。
    # 与 `_ModelUnavailable` 的分工：前者不换候选（账号级的桶，换模型没用）、
    # 后者才换。两条出口都必须先查额度再查可用性。
    FreeQuotaExhausted,
    # 按"响应正文里有没有 FreeUsageLimitError"判定每日额度用完，见它的 docstring。
    _quota_exhausted_error,
    # 模型名的判定与「深度思考」那三档优先级只有一份实现，在 generation 那边：
    # 工具内部的 `chat_async` 也要用同一套名字（两条出口同一个开关）。这里只是
    # 复用，不重新定义——两处各写一份的结果已经出现过一次：勾上深度思考只换了
    # 外层工具循环的模型，骨架/结构/说明仍是普通模型。
    _model_for,
    # SSE 帧的字段名（`reasoning` / `reasoning_content` 两个都认）与工具调用分片
    # 的拼装规则都只有一份实现，在 generation 那边：这条出口与 `chat_async`
    # 必须按同一套规则读同一个响应。这里只负责"要不要走流式"与结果组装。
    _stream_chunk,
    # 「现在有没有人在看思考增量」。有消费者才走流式：没有消费者时这条出口
    # 必须与加流式之前**逐字节一致**（请求体相同、同样只收整包），否则一次
    # 与预览无关的改动会顺手改掉所有直接调 provider 的行为。
    current_thinking_sink,
    context_overflow_error,
    estimate_input_tokens,
    max_tokens_cap,
)

# 单次响应上限。与 generation 那边同口径：超过这个体积说明模型跑飞了，
# 读进内存只会把进程拖垮。
_MAX_RESPONSE_BYTES = 2_000_000
# 与 generation.service._TIMEOUT_SECONDS 同口径。60 秒是照短输出拍的：一轮
# 带 8 支展开的调用实测就要一分多钟，慢速档更久，超时即整次 run 报废。
_TIMEOUT_SECONDS = 600.0

# 实测可用的输出上限：DeepSeek 接受 max_tokens=65536（HTTP 200）。RunBudget
# 允许配到 131072，超过实测值的部分本来也只会拿到 400，所以这里夹一层；
# 要再放开得先重新实测服务端接受度。
#
# 它是**默认值**，不是唯一值：真正发出去的那个数由
# `generation.service.max_tokens_cap()` 给出（`MINDMAP_LLM_MAX_TOKENS` 配置值、
# 与服务端 400 里学到的上限取小）。写死一个数的后果是"换一个模型就整条链 400"，
# 而 qwen 系只认 32768 这件事不该要求改代码。
_MAX_TOKENS_CAP = 65536

# 「深度思考」用的模型名判定（`_model_for`）与 `DEEP_MODEL_ENV` /
# `DEEP_MODEL_FALLBACK` 两个常量都在 `generation/service.py`：那是模型出口的家，
# 工具内部的 `chat_async` 也走同一份判定。要读那两个常量请从
# `app.generation.service` 导入——`provider` 这边**不转发**，否则又会出现第二个
# 可以 import 得到的位置。

logger = logging.getLogger(__name__)

# 「这个模型此刻不可用」的状态码与异常类型都只有一份实现，在
# `generation/service.py`：两条出口（这一层与工具内部的 `chat_async`）对"该换
# 下一个候选"必须用同一个判据，否则会出现"agent 那轮换了、写内容那次没换"
# ——正是 AGENTS.md 那条"只改了一条路径"的老问题。
_UNAVAILABLE_STATUSES = UNAVAILABLE_STATUSES


def _estimate_request_tokens(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]],
) -> int:
    """估算一次请求的输入 token：消息正文 + 函数调用参数 + 工具定义。

    消息历史随工具循环一路增长，它是 agent 这条路径上唯一会自己长大的输入，
    所以边界必须在这里守一次——入口处的 `AgentRunRequest` 只挡住了材料，
    挡不住"模型反复调工具把上下文喂爆"。工具定义每轮都在请求里，也要算。
    """
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += estimate_input_tokens(content)
        elif isinstance(content, list):  # 多模态内容：只算文本分片，图片另计
            total += sum(estimate_input_tokens(str(part.get("text") or ""))
                         for part in content if isinstance(part, dict))
        for call in message.get("tool_calls") or []:
            arguments = (call.get("function") or {}).get("arguments") or ""
            total += estimate_input_tokens(str(arguments))
    if tools:
        total += estimate_input_tokens(json.dumps(tools, ensure_ascii=False))
    return total


class OpenAICompatibleProvider:
    """走 OpenAI 兼容的 `/chat/completions`，带 tools 字段。

    `deep_thinking` 是**实例**属性而不是 `complete_tools` 的参数：接口
    （`contracts.ToolProvider`）保持原样，测试里那些按脚本回放的假 provider 一行都不用改，
    真正要变的只有"这一次往上游发哪个 model 名"这一件事。一次运行一个 provider 实例
    （`service._loop` 里按请求构造），所以它天然是"这一次"的语义。
    """

    def __init__(self, *, deep_thinking: bool = False) -> None:
        self.deep_thinking = deep_thinking
        # 上一次真正发出去的模型名。`_loop` 收尾时把它挂进 `done` 帧，
        # 界面上"这次到底用了哪个模型"就有据可查——否则勾了「深度思考」而配置里
        # 没给对名字时，用户只能看到一张图，看不出它走的哪条路。
        self.model_name: str | None = None
        # 服务端回报的模型名（响应体的 `model`）。与 `model_name` 分开两个字段：
        # 一个是"我要的"，一个是"它给的"，实测这两个不一样（请求 `deepseek-chat`、
        # 服务 `deepseek-flash`）。把它们合成一个，界面上就再也看不出路由发生。
        self.served_model: str | None = None
        # 这一次运行里已经被上游拒过的免费模型。放在实例上而不是模块级：
        # "哪个模型坏了"是这一次运行的事实，模块级会让并发请求互相污染
        # （A 撞上 429，B 就永远拿不到那个模型）。
        self._tried: set[str] = set()

    async def complete_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        budget: RunBudget,
    ) -> Completion:
        resolved = _config_full()
        base, key = resolved.base_url, resolved.api_key
        # 判"这次走的是不收费的模型"用 `resolved.free_tier`（`model_source ==
        # SOURCE_FREE`）。**别用 `reasoning_effort is not None`**：免费档的思考档位
        # 现在是每模型各自的（OpenCode 目录 `capabilities.reasoning`），目录里没给
        # 档位的免费模型它就是 None——那是合法值，不是"没走免费档"。
        free_tier = resolved.free_tier
        if free_tier:
            # 免费档把模型名钉死在免费那个上，**不走 `_model_for`**：那一档可能
            # 换成深度模型（显式配置或 `DEEP_MODEL_FALLBACK`），而深度模型不在免费
            # 名单里——照它发出去就是用部署方的钱生成，与"免费额度"这个承诺相反。
            # 首发那个的档位已在 `Resolved.reasoning_effort`（`resolve()` 从同一条
            # 记录取的）；候选序列里的后续模型**各自的档位**从记录现取。
            plan: list[tuple[str, str | None]] = [
                (resolved.model, resolved.reasoning_effort)]
            # 同一次运行内的候选序列：上游偶发 429（实测同一时刻连发三次也可能
            # 全 200），换一个免费模型往往就通了。已经在这轮里失败过的不再重试。
            plan += [(record.id, record.effort) for record in _free_models.candidates(
                base, tried=self._tried | {resolved.model})]
        else:
            plan = [(_model_for(base, resolved.model, self.deep_thinking), None)]

        # 输入侧的物理边界。放在发请求之前、且只算一次：它只与消息和工具定义有关，
        # 与用哪个模型无关，换模型重试不必重算。
        used = _estimate_request_tokens(messages, tools)
        if used > CONTEXT_TOKEN_LIMIT:
            raise context_overflow_error(
                chars=sum(len(str(message.get("content") or "")) for message in messages),
                tokens=used, limit=CONTEXT_TOKEN_LIMIT, label="本轮对话上下文")

        failure: Exception | None = None
        for model, effort in plan:
            try:
                completion = await self._attempt(
                    base=base, key=key, model=model, messages=messages, tools=tools,
                    budget=budget, reasoning_effort=effort, free_tier=free_tier,
                )
            except _ModelUnavailable as exc:
                # 只有"这个模型此刻不可用"才轮到下一个候选。连不上、超时、响应
                # 不合法这些换模型也一样，继续试只是把同一个失败重复几遍。
                self._tried.add(model)
                logger.warning("模型 %s 暂时不可用，换下一个候选：%s", model, exc)
                failure = exc
                continue
            # 响应里的 `model` 记在实例上，与 `model_name` 并存：`_loop` 收尾时都要。
            self.served_model = completion.served_model
            return completion
        # 候选全试完还是不行：抛最后一个真实原因，不要包装成"没有候选"
        # （用户看到的文案该说清是 429 还是连不上）。
        if failure is not None:
            raise GenerationError(str(failure)) from failure
        raise GenerationError("这个模型地址上没有可用的免费模型")

    async def _attempt(
        self,
        *,
        base: str,
        key: str,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        budget: RunBudget,
        reasoning_effort: str | None,
        free_tier: bool,
    ) -> Completion:
        """发一次请求。只有"上游明确说这个模型此刻不可用"才抛 `_ModelUnavailable`。

        两种发法，**由"有没有人在看思考增量"决定**：

        - 没人看（单测、直接调 provider 的脚本）：收整包，与加流式之前逐字节一致。
        - 有人看（`_run_tool` 之外的工具选择轮）：`stream: true` 逐帧读，把
          `delta.reasoning` 转进思考通道，同时自己拼 `delta.tool_calls` 分片。

        为什么不一律走流式：这条路的消费者要的是**完整**的 `tool_calls`，流式拼装
        多一处状态机与一处兜底（服务端不认 `stream` 会直接回整包 JSON），那些代价
        只有在能拿到思考增量时才值得付。
        """
        self.model_name = model
        requested = budget.max_output_tokens
        if free_tier:
            # 免费档的实测代价：`max_tokens=1400` 时两个免费模型都会把 1400 全花在
            # thinking 上，正文一个字没写就 `finish_reason=length`。所以给它一个
            # 下限；**也要给上限**，理由见 `FREE_MAX_OUTPUT_TOKENS`——只抬下限时
            # 调用方要的 65536 会原样发出去，免费模型按额度大小决定思考多久，
            # 一轮就能吃掉几百秒。`max_tokens_cap()` 仍是最终上限。
            #
            # 夹不夹看的是 free_tier，**不是有没有 effort**：目录里没给档位的免费
            # 模型（effort=None，请求里没有那个键）同样会把 token 烧在思考上——
            # 夹住的是"给思考的预算"，键只是"告诉它按哪档想"。
            requested = max(requested, FREE_MIN_OUTPUT_TOKENS)
            requested = min(requested, FREE_MAX_OUTPUT_TOKENS)
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": min(requested, max_tokens_cap()),
        }
        if reasoning_effort:
            # 只在这个模型有自己的档位时加这个键（档位跟着模型走，见
            # `free_models.FreeModel.effort`）；自配端点收不到一个它可能不认识的
            # 参数。
            body["reasoning_effort"] = reasoning_effort
        # 没有工具时不要挂空的 tools 数组：部分兼容端点会因此返回 400。
        if tools:
            body["tools"] = tools

        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        sink = current_thinking_sink()
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS, follow_redirects=True) as client:
                if sink is None:
                    buffer = await _read_whole(client, base, body, headers)
                    return _parse_completion(buffer)
                streamed = await _read_stream(client, base, body, headers, sink)
        except httpx.HTTPStatusError as exc:
            raise GenerationError(f"模型服务返回 HTTP {exc.response.status_code}") from exc
        except (httpx.RequestError, httpx.InvalidURL, OSError) as exc:
            raise GenerationError("无法连接模型服务或请求超时") from exc
        if streamed is None:
            # 服务端不认 `stream`（直接回了整包 JSON）或流里一帧可用的都没有：
            # 退回整包读，这一轮的结果不能因为"想显示思考"而失败。
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS,
                                         follow_redirects=True) as client:
                buffer = await _read_whole(client, base, body, headers)
            return _parse_completion(buffer)
        return streamed


async def _read_whole(
    client: Any, base: str, body: dict[str, Any], headers: dict[str, str],
) -> bytearray:
    """收满整个响应体。**请求体一个字都不改**——没有思考消费者时必须与旧行为一致。"""
    async with client.stream(
        "POST", base + "/chat/completions",
        content=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
    ) as response:
        if response.status_code in _UNAVAILABLE_STATUSES:
            # **先把错误体读进来，再判"是不是免费额度用完"**：流式响应在读之前
            # `.text` 会抛 `ResponseNotRead`（`_quota_exhausted_error` 会把它吞成
            # "没有正文"，判据就永远落空）。OpenCode 免费档用完回的就是 429，
            # 先按"不可用"换候选会把同一个账号级的桶再撞十来次。
            await response.aread()
            exhausted = _quota_exhausted_error(response)
            if exhausted is not None:
                raise exhausted
            raise _ModelUnavailable(f"模型服务返回 HTTP {response.status_code}")
        response.raise_for_status()
        buffer = bytearray()
        async for chunk in response.aiter_bytes():
            buffer.extend(chunk)
            if len(buffer) > _MAX_RESPONSE_BYTES:
                raise GenerationError("模型响应过大")
    return buffer


async def _read_stream(
    client: Any,
    base: str,
    body: dict[str, Any],
    headers: dict[str, str],
    sink: Callable[[str], Awaitable[None]],
) -> Completion | None:
    """`stream: true` 读一遍，边读边把思考推给 `sink`，同时拼 `delta.tool_calls`。

    返回 None 表示**这次流式尝试不算数**，调用方退回整包读：服务端不认 `stream`
    直接回了整包 JSON、流里没见到任何帧、流被切断（`[DONE]` 与 finish_reason 都
    没见到）、拼出来的工具调用缺 id 或缺函数名。这些都是"预览机制"该
    自己认栽的范围——工具选择轮不能因为想显示思考就失败。

    **故意抛出的只有一种**：`finish_reason == "length"`。半截 `arguments` 拿去执行
    比直接失败危险得多（见 `_parse_completion` 里那段注释），这条纪律两条读法
    必须一致——流式路径也**不**去 salvage 半个工具调用。另外 `_ModelUnavailable`
    与"响应过大"照旧向上抛，它们与流式无关。
    """
    body = dict(body, stream=True)
    text_parts: list[str] = []
    pending: dict[int, dict[str, Any]] = {}
    finish_reason: str | None = None
    served_model: str | None = None
    usage: dict[str, Any] = {}
    seen_any = False
    ended = False
    async with client.stream(
        "POST", base + "/chat/completions",
        content=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
    ) as response:
        if response.status_code in _UNAVAILABLE_STATUSES:
            # **先把错误体读进来，再判"是不是免费额度用完"**（`_read_whole` 同一
            # 处置，理由见那里——流式响应在读之前 `.text` 会抛 `ResponseNotRead`）。
            await response.aread()
            exhausted = _quota_exhausted_error(response)
            if exhausted is not None:
                raise exhausted
            raise _ModelUnavailable(f"模型服务返回 HTTP {response.status_code}")
        response.raise_for_status()
        size = 0
        async for line in response.aiter_lines():
            # 有的兼容端点写 `data:{...}` 不带空格，两种都要认。
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                ended = True
                break
            chunk = _stream_chunk(data)
            if chunk is None:
                continue
            seen_any = True
            size += len(data)
            if size > _MAX_RESPONSE_BYTES:
                raise GenerationError("模型响应过大")
            if chunk["model"]:
                served_model = chunk["model"]
            if chunk["usage"]:
                usage = dict(chunk["usage"])
            if chunk["thought"]:
                # 与 `chat_async` 同一条纪律：回调自己坏了就关掉它，
                # 不能让"显示思考"连累这次生成。
                try:
                    await sink(chunk["thought"])
                except Exception:  # noqa: BLE001 — 纯展示，出错就不显示
                    sink = _no_sink
            if chunk["text"]:
                text_parts.append(chunk["text"])
            for piece in chunk["tool_calls"]:
                _absorb_tool_delta(pending, piece)
            if chunk["reason"] is not None:
                finish_reason = chunk["reason"]
                ended = True
    if not seen_any or not ended:
        return None
    if finish_reason == "length":
        raise GenerationError("模型输出被长度上限截断，本轮不执行任何工具调用")
    message: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts)}
    calls, raw_calls = _assembled_tool_calls(pending)
    if calls is None:
        return None
    if raw_calls:
        message["tool_calls"] = raw_calls
    return Completion(
        message=message,
        tool_calls=calls,
        finish_reason=finish_reason,
        usage=usage,
        served_model=served_model,
    )


async def _no_sink(_text: str) -> None:
    """思考通道坏掉之后的空实现：保留"每帧都在调 sink"的形状，一个字节都不发。"""
    return None


def _absorb_tool_delta(pending: dict[int, dict[str, Any]], piece: dict[str, Any]) -> None:
    """把一帧 `delta.tool_calls` 分片并进 `pending[index]`。

    流式下工具调用是**按片**发出来的：第一片带 `id` 与 `function.name`、后续片
    只带 `function.arguments` 的一段字符串，`index` 是唯一稳定的键（有的端点连
    首片的 `index` 都省，按"当前有几条"兜底）。拼装规则只有这一份实现，别在
    调用方再写一遍——两份拼法不一致时坏的不是形状，是参数内容。
    """
    index = piece.get("index")
    if not isinstance(index, int):
        index = len(pending)
    slot = pending.setdefault(index, {"id": "", "name": "", "arguments": ""})
    if isinstance(piece.get("id"), str) and piece["id"]:
        slot["id"] = piece["id"]
    function = piece.get("function")
    if not isinstance(function, dict):
        return
    if isinstance(function.get("name"), str) and function["name"]:
        slot["name"] = function["name"]
    arguments = function.get("arguments")
    if isinstance(arguments, str):
        slot["arguments"] += arguments


def _assembled_tool_calls(
    pending: dict[int, dict[str, Any]],
) -> tuple[list[ToolCall] | None, list[dict[str, Any]]]:
    """拼好的工具调用，以及它们要回填进 `message` 的那份原始形状。

    缺 id 或名字时返回 `None`（调用方退回整包读）：一个没有名字的工具调用没法
    执行，也没法回给模型。
    """
    calls: list[ToolCall] = []
    raw_calls: list[dict[str, Any]] = []
    for index in sorted(pending):
        slot = pending[index]
        if not slot["id"] or not slot["name"]:
            return None, []
        decoded, parse_error = _decode_arguments(slot["arguments"])
        calls.append(ToolCall(
            call_id=slot["id"],
            name=slot["name"],
            arguments=decoded,
            raw_arguments=slot["arguments"],
            parse_error=parse_error,
        ))
        raw_calls.append({
            "id": slot["id"],
            "type": "function",
            "function": {"name": slot["name"], "arguments": slot["arguments"]},
        })
    return calls, raw_calls


def _decode_arguments(raw_args: str) -> tuple[dict[str, Any], str | None]:
    """`arguments` 字符串 → `(解析出来的对象, 解析错误)`。

    解析错误**不抛**：让执行层把它变成一条可回给模型的具体错误，模型看到
    "参数不是合法 JSON"通常会自己重写一次。两条读法共用这一份判定。
    """
    if not raw_args:
        return {}, None
    try:
        decoded = json.loads(raw_args)
    except json.JSONDecodeError as exc:
        return {}, f"函数参数不是合法 JSON：{exc.msg}"
    if isinstance(decoded, dict):
        return decoded, None
    return {}, "函数参数必须是一个 JSON 对象"


def _parse_completion(buffer: bytearray) -> Completion:
    """把原始响应体解析成 `Completion`。

    单独出来便于测试直接喂字节，不必起一个 HTTP 服务。
    """
    try:
        payload = json.loads(buffer)
        choice = payload["choices"][0]
        message = choice["message"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise GenerationError("模型返回的不是合法的 Chat Completions 响应") from exc

    finish_reason = choice.get("finish_reason")

    # 输出被硬切时，tool_calls 里的 arguments 可能只写了一半。半截参数拿去执行
    # 比直接失败危险得多（比如 add 的 label 被截断，树里就多一个残缺节点），
    # 所以这里直接判失败，而不是"能解析多少算多少"。
    #
    # **与 `chat_async` 的 salvage 有意不同**：那条路要的是一棵可回收的树
    # （`salvage_model_json` 能救回已经闭合的节点，交给用户半棵树比整轮报废好）；
    # 这条路要的是一个**工具调用**，参数缺一半执行了就是脏数据，没有"回收一半"
    # 这种选项。两处的判断因此不能统一——统一成 salvage 会把半截参数放进树里。
    if finish_reason == "length":
        raise GenerationError("模型输出被长度上限截断，本轮不执行任何工具调用")

    calls: list[ToolCall] = []
    for raw in message.get("tool_calls") or []:
        function = raw.get("function") or {}
        raw_args = function.get("arguments") or ""
        # 与流式那条读法共用同一份判定：两处各写一遍时，一处改了另一处没改，
        # 坏的不是形状而是"同一份参数在两种读法下解析结果不同"。
        arguments, parse_error = _decode_arguments(raw_args)
        calls.append(ToolCall(
            call_id=str(raw.get("id") or ""),
            name=str(function.get("name") or ""),
            arguments=arguments,
            raw_arguments=raw_args,
            parse_error=parse_error,
        ))

    return Completion(
        message=message,
        tool_calls=calls,
        finish_reason=finish_reason,
        usage=payload.get("usage") or {},
        # 响应体里的顶层 `model`。不填默认值：字段缺失就是"对面没说"，界面上写
        # "未知"。这里读的是 `payload` 而不是 `message`——`model` 在顶层，与 `choices`
        # 平级；写成 `message.get("model")` 会永远拿到 None，而那个 bug 看起来像
        # "这个服务端不返回模型名"。
        served_model=payload.get("model") if isinstance(payload.get("model"), str) else None,
    )


def default_provider(*, deep_thinking: bool = False) -> OpenAICompatibleProvider:
    """这一次运行要用的 provider。`deep_thinking` 缺省 `False`，与加它之前同一个调用。"""
    return OpenAICompatibleProvider(deep_thinking=deep_thinking)
