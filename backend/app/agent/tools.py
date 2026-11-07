"""工具注册表与五个业务工具。

注册表只负责分派与参数校验，业务逻辑在各个 `_xxx` 函数里。

五个工具的划分照「用户意图 + 作用域」而不是 CRUD 动词：
- `mindmap_generate` 只在没有导图时可用，一次调用产出一整棵候选树
- `mindmap_generate_deep` 同样只在没有导图时可用，但拆成「骨架 → 并发展开 →
  验收 → 局部重试」四段，复杂主题用它（理由见该函数的 docstring）
- `node_expand` 只能在已选中的节点下加子节点，不能删、不能移
- `rag_query` 只读，且只能查本次请求带来的来源
- `mindmap_edit` 才允许改/删/移，且必须按节点 id

这样切是为了把「改错地方」的风险压到协议层：`node_expand` 的入参里没有
`parent_id` 的自由度（只能是你当前选中的那个），模型想乱挂也挂不了。
`build_registry(intent)` 再按意图只放出该用的那几个——看不到的工具，
模型不会反复试着调。
"""
from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agent.contracts import RunContext, ToolCall, ToolResult, ToolSpec
from app.agent.ops import (
    TEMP_ID_PREFIX,
    apply_ops,
    find_node,
    new_node_id,
    normalize_ops,
)
from app.contracts.diagnostic import Diagnostic, DiagnosticLevel
from app.contracts.mindmap import MindMapContent, Node
from app.contracts.ops import OpBatch, OpError
from app.contracts.source import Source
from app.generation.service import (
    DETAIL_PROMPT,
    MIN_BRANCHES,
    SOURCED_DETAIL_PROMPT,
    SOURCED_THINKING_GUARD,
    GenerationError,
    chat_async,
    thinking_block,
    validate_generated_content,
)
from app.knowledge.service import search_sources

# 与 backend/app/contracts/mindmap.py 的 Node.children max_length 同口径
MAX_CHILDREN = 50

# 分片生成的并发上限。外部模型服务通常有并发上限：一次把 8 个请求全打出去很
# 容易被限流，而限流的表现是超时或 429——比"慢"更难排查，因为失败的分支看上去
# 和"模型就是没给内容"一模一样。3 是保守值：足够让 5-8 条分支重叠跑，又不至于
# 让服务端把它当成突发流量。
DEEP_CONCURRENCY = 3

# 单个来源喂给模型的字符上限。超出的部分截断并显式注明——不注明的话，模型
# 会以为自己看到的是全文，然后为"结尾那段"写引用，而那些引用在原文里
# 根本匹配不上。注意校验用的 sources 不截断：quote 要在完整原文里逐字核对。
MAX_SOURCE_CHARS = 12000
SOURCE_TRUNCATED_HINT = "（原文过长，已截断）"


class ToolRegistry:
    """按名字分派的工具表。

    只注册不执行任何未登记的名字：模型不能凭空调用一个没实现的工具，
    这一点由 `execute` 的第一行保证，不依赖模型自觉。
    """

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"工具 {spec.name} 重复注册")
        self._specs[spec.name] = spec

    def __contains__(self, name: str) -> bool:
        return name in self._specs

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def names(self) -> list[str]:
        return sorted(self._specs)

    def specs(self) -> list[dict[str, Any]]:
        return [spec.schema() for spec in self._specs.values()]

    async def execute(self, call: ToolCall, ctx: RunContext) -> ToolResult:
        spec = self._specs.get(call.name)
        if spec is None:
            # 返回而不是抛：模型偶尔会编一个很像的工具名（把 mindmap_generate
            # 写成 generate_mindmap）。写成一条可读错误回给模型，下一轮它通常
            # 能自己改对；抛异常则整个 run 报废。
            return ToolResult(
                ok=False,
                content=f"没有名为 {call.name} 的工具，可用的有：{', '.join(self.names()) or '（无）'}",
                data={"code": "unknown_tool", "name": call.name},
            )

        if call.parse_error:
            return ToolResult(
                ok=False,
                content=f"参数解析失败：{call.parse_error}",
                data={"code": "invalid_tool_args", "errors": [{"path": "", "reason": call.parse_error}]},
            )

        try:
            args = spec.args_model.model_validate(call.arguments)
        except ValidationError as exc:
            errors = [
                {"path": ".".join(str(part) for part in item["loc"]), "reason": item["msg"]}
                for item in exc.errors()[:5]
            ]
            detail = "；".join(f"{item['path']}: {item['reason']}" for item in errors)
            return ToolResult(
                ok=False,
                content=f"参数不合法：{detail}",
                data={"code": "invalid_tool_args", "errors": errors},
            )

        # 同步与异步工具都要支持：纯计算的工具（按 op 改树）没有 await 点，
        # 强制写成 async 只是给作者加噪音；调模型的工具必然是 async。
        result = spec.fn(args, ctx)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, ToolResult):
            raise TypeError(f"工具 {call.name} 必须返回 ToolResult，收到 {type(result).__name__}")
        return result


# ---------------------------------------------------------------- 参数模型


class GenerateArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    topic: str = Field(min_length=1, max_length=1000, description="中心主题，用用户的语言")


class GenerateDeepArgs(BaseModel):
    """分片生成的入参。

    `branches` 与 `points_per_branch` 是**期望值**而不是硬约束：骨架给多给少一条
    都继续，展开不足只补那一支。把它们写成严格契约，等于把模型的一次抖动
    升级成整次失败。
    """

    model_config = ConfigDict(extra="forbid")
    topic: str = Field(min_length=1, max_length=1000, description="中心主题，用用户的语言")
    branches: int = Field(default=5, ge=3, le=8, description="一级分支数量")
    points_per_branch: int = Field(default=3, ge=2, le=5, description="每条一级分支下至少要有几个要点")


class ExpandArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str = Field(min_length=1, max_length=100, description="要展开的节点 id，取自当前大纲")
    count: int = Field(default=3, ge=2, le=4, description="要补充几个子要点")
    instruction: str | None = Field(default=None, max_length=300, description="可选的补充要求")


class RagArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=5, ge=1, le=5)


class EditChange(BaseModel):
    """第一版只允许改标题文字。

    引用（`source_refs`）刻意不开放：模型给节点加一条引用就必须逐字匹配原文，
    而这一步在生成路径上已经由 `validate_generated_content` 做过了；编辑路径
    再开一个入口，等于多一条能凭空造引用的路。
    """

    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=200)


class EditOp(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["add", "update", "remove", "move"]
    id: str = Field(min_length=1, max_length=100)
    parent_id: str | None = Field(default=None, max_length=100)
    new_parent_id: str | None = Field(default=None, max_length=100)
    index: int | None = Field(default=None, ge=0)
    label: str | None = Field(default=None, max_length=200)
    changes: EditChange | None = None


class EditArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ops: list[EditOp] = Field(min_length=1, max_length=100)


# ---------------------------------------------------------------- 辅助


def _count_nodes(root) -> int:
    return 1 + sum(_count_nodes(child) for child in root.children)


def _pick_sources(ctx: RunContext, source_ids: list[str]) -> list:
    """只允许缩小到本次请求已带的来源。

    模型不能凭空引用一个不在 `ctx.sources_by_id` 里的 id，否则它给出的引用
    在界面上点不开——那不是"引用不准"，是引用不存在。
    """
    if not source_ids:
        return list(ctx.request.sources)
    picked = [ctx.sources_by_id[sid] for sid in source_ids if sid in ctx.sources_by_id]
    return picked


def _fail(code: str, message: str, **extra: Any) -> ToolResult:
    return ToolResult(ok=False, content=message, data={"code": code, **extra})


def _require_candidate(ctx: RunContext) -> MindMapContent | ToolResult:
    if ctx.candidate is None:
        return _fail("no_candidate", "当前还没有导图，请先调用 mindmap_generate 生成。")
    return ctx.candidate


def _node_ids(root) -> set[str]:
    seen: set[str] = set()
    stack = [root]
    while stack:
        node = stack.pop()
        seen.add(node.id)
        stack.extend(node.children)
    return seen


def _skeleton_titles(raw: Any) -> list[str]:
    """从骨架响应里取出分支标题，剔除空项，超长截到 50 字。

    只接受字符串：`{"branches":[{"label":"x"}]}` 说明模型没照格式写，硬转字符串
    会把一整个 dict 的 repr 当成节点标题挂到树上——那种"成功"比失败更难发现。
    """
    if not isinstance(raw, dict):
        return []
    items = raw.get("branches")
    if not isinstance(items, list):
        return []
    titles: list[str] = []
    for item in items:
        if not isinstance(item, str):
            continue
        title = item.strip()
        if not title:
            continue
        titles.append(title[:50])
    return titles


def _point_labels(raw: Any, limit: int) -> list[str]:
    """从展开响应里取出要点标题，最多 `limit` 条。

    截到 `limit` 而不是全收：多出来的要点会让单支子节点数失控，而这张图的上限是
    按 `points_per_branch` 做的预算。
    """
    if not isinstance(raw, dict) or limit <= 0:
        return []
    items = raw.get("points")
    if not isinstance(items, list):
        return []
    labels: list[str] = []
    for item in items:
        if not isinstance(item, str):
            continue
        label = item.strip()
        if not label:
            continue
        labels.append(label[:200])
        if len(labels) >= limit:
            break
    return labels


def _audit_branches(
    content: MindMapContent, minimum: int,
) -> tuple[list[tuple[int, str, str, int]], list[str]]:
    """确定性验收：只用代码看合并后的树，一次模型调用都不发。

    返回 `(要点不足的分支 (下标, id, 标题, 实际点数), 要点超过上限的分支标题)`。
    两者性质不同：不足是模型质量的正常波动，接受现状并记诊断；超过
    `MAX_CHILDREN` 是结构错误——那样的树连契约都存不进去，必须判失败。

    带下标是因为同一支可能既"点数不足"又"根本没展开成"：调用方要靠下标把
    重复的告警剔掉，否则一个因额度被跳过的分支会同时得到两条诊断。
    """
    sparse: list[tuple[int, str, str, int]] = []
    overfull: list[str] = []
    for index, branch in enumerate(content.root.children):
        count = len(branch.children)
        if count < minimum:
            sparse.append((index, branch.id, branch.label, count))
        if count > MAX_CHILDREN:
            overfull.append(branch.label)
    return sparse, overfull


def _source_prompt_input(sources: list[Source]) -> str:
    """把来源拼成 `SOURCED_DETAIL_PROMPT` 约定的 sources 数组。

    必须逐项带 `source_id`：提示词要求模型按 id 写 `source_refs`，而
    `_understand()` 返回的是纯文本合并，里面一个 id 都没有——模型看不见 id
    就永远写不出合法引用，引用功能等于从来没通。那个函数服务于 pipeline 的
    限长与 demo 路径，语义不同，不能在这里复用。

    用 `ensure_ascii=False`：中文原样传输，模型看到的 token 更少，
    也免得它把 `\\u4e2d` 这种转义当成正文抄进 quote。
    """
    items = [
        {
            "source_id": source.id,
            "text": _clip_source_text(source.text or ""),
        }
        for source in sources
    ]
    return "sources 数组（JSON）：\n" + json.dumps(items, ensure_ascii=False)


def _clip_source_text(text: str) -> str:
    if len(text) <= MAX_SOURCE_CHARS:
        return text
    return text[:MAX_SOURCE_CHARS] + SOURCE_TRUNCATED_HINT


def _selection_scope_error(
    content: MindMapContent, args: EditArgs, ctx: RunContext,
) -> ToolResult | None:
    """把可改范围收窄到用户选中节点的子树。

    注册表已经按意图裁掉了 expand 下的 `mindmap_edit`，但那是**一层**防线：
    注册表被误配、或被调用方直接传进一个全量 registry 时，这一层就没了。
    所以执行层自己再判一次——改错节点是不可逆的（用户手写的分支会被删）。

    两处口径说明：
      - `add` 比的是 `parent_id` 而不是 `id`。新节点的 id 是本批临时名
        （`$tmp1`），它根本不在树里，"落在谁的子树内"对它没有意义；
        真正决定"改到哪去"的是父节点。
      - 同批新建的父节点（`$` 开头）放行：它自己会被"它挂靠在谁下面"
        那条检查约束住，放行不会漏出范围。
    """
    if not ctx.request.selected_ids:
        return None

    scope: set[str] = set()
    for selected in ctx.request.selected_ids:
        node = find_node(content.root, selected)
        if node is not None:
            scope |= _node_ids(node)
    allowed = "、".join(ctx.request.selected_ids)

    for index, item in enumerate(args.ops):
        if item.op == "add":
            if item.parent_id is None:
                # 缺 parent_id 由后面的 invalid_ops 给更准确的错误，这里不抢答
                continue
            target, field = item.parent_id, "parent_id"
            if target.startswith(TEMP_ID_PREFIX):
                continue
        else:
            target, field = item.id, "id"
        if target not in scope:
            return _fail(
                "out_of_selection",
                f"只能改当前选中节点及其子树（{allowed}），第 {index} 条操作指向 {target}。",
                path=f"ops.{index}.{field}",
            )
        if item.op == "move" and item.new_parent_id not in scope:
            return _fail(
                "out_of_selection",
                f"移动的目标父节点 {item.new_parent_id} 不在选中范围（{allowed}）内。",
                path=f"ops.{index}.new_parent_id",
            )
    return None


EXPAND_PROMPT = (
    "你在给思维导图的一个节点补充子要点。只返回一个 JSON 对象，不要代码围栏。"
    '格式为 {"points":["要点一","要点二"]}。'
    "每个要点必须是一句能独立成立的话，不超过 30 字；不要编号，不要解释，"
    "不要重复已有子节点，不要编造具体数据、人名或文献。"
)

# 分片生成第一步：只定骨架，不写内容。一次调用同时要求模型「定框架 + 每支展开」，
# 它会把注意力放在先写出来的那几支上，后面的分支越写越敷衍——这是质量不稳的
# 直接来源。骨架单独一次调用，模型只需回答「分成哪几块」。
PLAN_PROMPT = (
    "你在为一张思维导图只定一级骨架，不写任何子要点。"
    "只返回一个 JSON 对象，不要代码围栏，格式为 "
    '{"branches":["分支一","分支二"]}。'
    "每条分支是一个能独立成立的短标题，不超过 20 字；彼此覆盖的范围不重叠，"
    "按逻辑顺序排列；不要编号，不要解释，不要编造具体数据、人名或文献。"
)

# 分片生成第二步：按骨架逐支展开。与 `EXPAND_PROMPT` 的差别只有上下文——
# 那个服务于"用户选中某个节点再展开"，语义保持不动；这个要额外告诉模型它写的是
# 哪一支、以及别越到同级别的分支上去（并发展开时它看不到其他分支写了什么）。
BRANCH_EXPAND_PROMPT = (
    "你在给思维导图的一条一级分支补充子要点。只返回一个 JSON 对象，不要代码围栏，"
    '格式为 {"points":["要点一","要点二"]}。'
    "每个要点必须是一句能独立成立的话，不超过 30 字；不要编号，不要解释，"
    "只写这条分支范围内的内容，不要重复同级其他分支，不要编造具体数据、人名或文献。"
)


# ---------------------------------------------------------------- 工具实现


async def _generate(args: GenerateArgs, ctx: RunContext) -> ToolResult:
    if ctx.request.intent != "create":
        return _fail("wrong_intent", "只有新建导图时才能调用这个工具；修改现有导图请用 node_expand 或 mindmap_edit。")
    if ctx.candidate is not None and ctx.candidate.root.children:
        return _fail("map_exists", "已经有一张导图了，请用 node_expand 或 mindmap_edit 修改它。")

    # 只喂真的有正文的来源：`status` 是给界面看的，模型能用的是 text。
    # 不过滤的话，一个还没解析成功的空来源会让 validate_generated_content
    # 重建 source_usage 时撞上 char_count >= 1，把整次生成判成失败——
    # 用户只是多勾了一个空文件，却得到"模型返回的导图不合法"。
    sources = [source for source in _pick_sources(ctx, []) if (source.text or "").strip()]
    # 额度在调用**之前**占：请求一旦发出就是一次真实计费，哪怕它随后抛错。
    # 只在成功后记账会让预算在工具执行期间被超支使用（预算 1 实测跑出 4 次）。
    if not ctx.reserve_model_call():
        return _fail("budget_exhausted", "本次运行的模型调用额度已用完，无法生成导图。")
    ctx.extra_model_calls += 1
    try:
        # 思维模型只改「怎么拆」，不改「拆什么」：拼在基础提示词之后、来源输入
        # 之前（来源在 user 消息里）。有来源时要多带一句限定，否则「必须举反例」
        # 这类要求会被当成展开许可，模型开始补写材料里没有的内容。
        thinking = thinking_block(ctx.request.thinking_model)
        if sources:
            # 有来源时必须给结构化输入：模型要靠 source_id 写引用。
            raw = await chat_async(
                SOURCED_DETAIL_PROMPT + thinking + SOURCED_THINKING_GUARD,
                _source_prompt_input(sources),
            )
        else:
            raw = await chat_async(DETAIL_PROMPT + thinking, args.topic)
    except GenerationError as exc:
        return _fail("generation_failed", f"生成失败：{exc}")

    try:
        content, diagnostics = validate_generated_content(raw, sources=sources or None)
    except Exception as exc:  # noqa: BLE001 — 校验失败的原因就是要回给模型的文本
        return _fail("invalid_map", f"模型返回的导图不合法：{exc}")

    ctx.candidate = content
    ctx.revision += 1
    count = _count_nodes(content.root)
    return ToolResult(
        ok=True,
        content=f"已生成导图「{content.title}」，共 {count} 个节点。",
        data={
            "root_id": content.root.id,
            "node_count": count,
            "revision": ctx.revision,
            "diagnostics": [item.model_dump() for item in diagnostics],
        },
    )


async def _generate_deep(args: GenerateDeepArgs, ctx: RunContext) -> ToolResult:
    """分片生成：先出骨架，再并发展开每支，最后确定性验收并只补不合格的那几支。

    为什么要拆：整图生成的契约允许 300 节点 × 200 字标签，最坏约 4.4 万 token，
    而单次调用的输出上限只有 16384——大图只能靠模型自己收敛，收敛不住就是截断。
    分片后每次调用只装自己那一支，单次输出量就与整图上限脱钩了。
    第二个收益是质量：一次调用同时要求「定框架 + 每支展开」，模型会把注意力
    花在先写出来的分支上；把骨架单列成一次调用，它只需要回答「分成哪几块」。

    四段都有各自的失败语义，不要合并：
      1. 骨架拿不到 → 整次失败（连框架都没有，没有可交付的东西）
      2. 某支展开抛错 → 那一支留空，其余照常（一次 429 不该废掉整张图）
      3. 验收发现不足 → 只补那一支，骨架不重来（这是分片结构的全部价值）
      4. 额度不足 → 立刻停手，把已展开的部分交出去并显式标 `partial`
    """
    if ctx.request.intent != "create":
        return _fail("wrong_intent", "只有新建导图时才能调用这个工具；修改现有导图请用 node_expand 或 mindmap_edit。")
    if ctx.candidate is not None and ctx.candidate.root.children:
        return _fail("map_exists", "已经有一张导图了，请用 node_expand 或 mindmap_edit 修改它。")

    need = min(args.points_per_branch, MAX_CHILDREN)
    diagnostics: list[Diagnostic] = []
    # 思维模型两条路都要拼：骨架决定怎么分块，展开决定每块怎么拆。
    thinking = thinking_block(ctx.request.thinking_model)

    # 第一步：出骨架。额度在调用**之前**占——请求一旦发出就是一次真实计费。
    if not ctx.reserve_model_call():
        return _fail("budget_exhausted", "本次运行的模型调用额度已用完，连骨架都出不了，无法分片生成。")
    ctx.extra_model_calls += 1
    try:
        raw_plan = await chat_async(PLAN_PROMPT + thinking, args.topic)
    except GenerationError as exc:
        return _fail("generation_failed", f"骨架生成失败：{exc}")

    titles = _skeleton_titles(raw_plan)
    if len(titles) < MIN_BRANCHES:
        return _fail(
            "too_few_branches",
            f"模型只给了 {len(titles)} 条一级分支，少于 {MIN_BRANCHES} 条，无法成图。",
            branches=len(titles),
        )
    titles = titles[:MAX_CHILDREN]
    # 数量只做宽容校验（允许 ±1）。为了一两条差异整张重来，成本是又一轮全额调用，
    # 而多一条少一条对这张图并不致命；即使超出宽容范围也用实际返回的继续，
    # 只记一条诊断说明"没按要求的条数走"。
    if not args.branches - 1 <= len(titles) <= args.branches + 1:
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.warn, code="SKELETON_SIZE_OFF",
            message=f"请求 {args.branches} 条一级分支，模型给了 {len(titles)} 条，已按实际条数继续。",
        ))

    # 分支节点的 id 在骨架阶段就定好：展开是并发跑的，合并时要按 id 把要点挂回去，
    # 而树这时还不存在——只能先分配 id，合并时一次性落进树里。
    taken: set[str] = set()
    root_id = new_node_id(taken)
    taken.add(root_id)
    skeleton: list[tuple[str, str]] = []
    for title in titles:
        branch_id = new_node_id(taken)
        taken.add(branch_id)
        skeleton.append((title, branch_id))

    # 第二步：并发展开。限流必须包住整个"占额度 + 发请求"，否则额度会被
    # 排队中的任务一次性预占，实际请求却还没发出去。
    semaphore = asyncio.Semaphore(DEEP_CONCURRENCY)

    async def expand_branch(title: str) -> list[str] | None:
        """展开一条分支。额度不够返回 None——那是"没展开"，与"展开失败"是两件事。"""
        async with semaphore:
            if not ctx.reserve_model_call():
                return None
            ctx.extra_model_calls += 1
            ask = (
                f"整体主题：{args.topic}\n"
                f"本条一级分支：{title}\n"
                f"请给出 {need} 个子要点。"
            )
            raw = await chat_async(BRANCH_EXPAND_PROMPT + thinking, ask)
            return _point_labels(raw, need)

    results = await asyncio.gather(
        *(expand_branch(title) for title, _ in skeleton),
        return_exceptions=True,
    )

    # 异常不许扩散到其他分支：某支 429 / 超时，只该让那一支空着。
    labels_by_branch: list[list[str]] = []
    skipped: list[int] = []
    failed: list[tuple[int, str]] = []
    for index, (_, item) in enumerate(zip(skeleton, results)):
        if item is None:
            skipped.append(index)
            labels_by_branch.append([])
        elif isinstance(item, BaseException):
            # 有些异常（如 TimeoutError）没有 message，只用类名也比空串可读
            reason = f"{type(item).__name__}: {item}" if str(item) else type(item).__name__
            failed.append((index, reason[:200]))
            labels_by_branch.append([])
        else:
            labels_by_branch.append(list(item))

    skipped_set = set(skipped)
    failed_set = {index for index, _ in failed}

    # 第五步：局部重试。只补「要点数不足」的那几支，骨架不重来——一次质量波动
    # 不该把已经展开好的分支一起作废。抛异常的分支不在此列：它已经花掉一次调用，
    # 盲目再打一次更可能只是把 429 重放一遍。
    retried: list[int] = []
    for index, (title, _) in enumerate(skeleton):
        if index in skipped_set or index in failed_set:
            continue
        missing = need - len(labels_by_branch[index])
        if missing <= 0:
            continue
        if not ctx.reserve_model_call():
            # 额度用完就停手：继续调只会全部失败，不如把已展开的部分交出去
            break
        ctx.extra_model_calls += 1
        retried.append(index)
        ask = (
            f"整体主题：{args.topic}\n"
            f"本条一级分支：{title}\n"
            f"还差 {missing} 个子要点，请补齐，不要重复已经给出的内容。"
        )
        try:
            raw = await chat_async(BRANCH_EXPAND_PROMPT + thinking, ask)
        except GenerationError:
            continue  # 补调失败只是"仍然不足"，不升级成整次失败
        labels_by_branch[index].extend(_point_labels(raw, missing))

    # 所有分支都没留下内容、且没有任何一支是因为额度被跳过 → 没有可交付的树。
    # 反过来，如果有分支是"额度不够没展开"，骨架本身仍然有效，照常交出。
    if not any(labels_by_branch) and not skipped_set:
        detail = "；".join(message for _, message in failed) or "模型没有返回可用的子要点"
        return _fail(
            "all_branches_failed",
            f"{len(skeleton)} 个分支全都展开失败：{detail}",
            failed_branches=[skeleton[index][0] for index in sorted(failed_set)],
        )

    # 第三步：合并。骨架先成树（分支 id 已在骨架里），再把所有要点作为一批 add
    # 应用上去——整批原子，半截树不会进 ctx.candidate。
    label = args.topic.strip()[:200] or "思维导图"
    content = MindMapContent(
        title=label,
        root=Node(
            id=root_id,
            label=label,
            children=[Node(id=branch_id, label=title, children=[]) for title, branch_id in skeleton],
        ),
    )

    drafts: list[dict[str, Any]] = []
    for index, (_, branch_id) in enumerate(skeleton):
        for point_index, point in enumerate(labels_by_branch[index]):
            drafts.append({
                "op": "add",
                "id": f"$d{index}-{point_index}",
                "parent_id": branch_id,
                "index": point_index,
                "label": point,
            })

    if drafts:
        ops, _ = normalize_ops(drafts, _node_ids(content.root))
        batch = OpBatch(batch_id=_batch_id(ctx), base_revision=ctx.revision, ops=ops)
        try:
            content = apply_ops(content, batch)
        except OpError as exc:
            return _fail(exc.code, f"{exc.path}：{exc.message}", path=exc.path)

    # 第四步：确定性验收，全部用代码，不调模型。整树重跑一次契约校验——
    # `apply_ops` 内部也做了，但"骨架没有任何要点"时不会经过它（没有 op 可应用），
    # 只靠那边会把一棵只剩根和空分支的树放过去。
    try:
        content = MindMapContent.model_validate(content.model_dump())
    except ValidationError as exc:
        detail = "；".join(error["msg"] for error in exc.errors()[:3])
        return _fail("invalid_tree", f"合并后的导图不合法：{detail}")

    sparse, overfull = _audit_branches(content, need)
    if overfull:
        return _fail(
            "too_many_points",
            f"分支「{'、'.join(overfull)}」的要点数超过上限 {MAX_CHILDREN}，这张图不能落库。",
        )

    retried_set = set(retried)
    for index, branch_id, title, count in sparse:
        # 因额度没展开、或因异常留空的分支已经由下面两条诊断说清楚了；
        # 这里再报一条「点数不足」等于对同一个原因告警两次。
        if index in skipped_set or index in failed_set:
            continue
        tail = ("已补调一次模型，仍不足，按现状接受" if index in retried_set
                else "模型调用额度不足，未能补调")
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.warn, code="SPARSE_BRANCH", node_id=branch_id,
            message=f"分支「{title}」只有 {count} 个要点，少于要求的 {need} 个；{tail}",
        ))
    for index, message in failed:
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.warn, code="BRANCH_FAILED", node_id=skeleton[index][1],
            message=f"分支「{skeleton[index][0]}」展开失败，已留空：{message}",
        ))
    for index in skipped:
        diagnostics.append(Diagnostic(
            level=DiagnosticLevel.warn, code="BRANCH_SKIPPED_BUDGET", node_id=skeleton[index][1],
            message=f"因模型调用额度不足，分支「{skeleton[index][0]}」没有展开，已留空",
        ))

    expanded = len(skeleton) - len(skipped_set) - len(failed_set)
    partial = bool(skipped_set) or bool(failed_set)

    ctx.candidate = content
    ctx.revision += 1
    count = _count_nodes(content.root)

    content_parts = [f"已生成导图「{content.title}」，共 {count} 个节点。"]
    if skipped_set:
        content_parts.append(
            f"因模型调用额度不足，只展开了 {expanded}/{len(skeleton)} 个分支，"
            "其余分支只保留了骨架。"
        )
    if failed_set:
        names = "、".join(skeleton[index][0] for index in sorted(failed_set))
        content_parts.append(f"{len(failed_set)} 个分支展开出错，已留空：{names}。")
    if retried:
        content_parts.append(f"其中 {len(retried)} 个分支要点不足，已各补调一次模型。")

    return ToolResult(
        ok=True,
        content="".join(content_parts),
        data={
            "root_id": content.root.id,
            "node_count": count,
            "revision": ctx.revision,
            "planned_branches": len(skeleton),
            "expanded_branches": expanded,
            "partial": partial,
            "branches": [
                {"id": branch_id, "label": title, "points": len(labels_by_branch[index])}
                for index, (title, branch_id) in enumerate(skeleton)
            ],
            "diagnostics": [item.model_dump() for item in diagnostics],
        },
    )


async def _expand(args: ExpandArgs, ctx: RunContext) -> ToolResult:
    content = _require_candidate(ctx)
    if isinstance(content, ToolResult):
        return content

    node = find_node(content.root, args.node_id)
    if node is None:
        return _fail("node_not_found", f"节点 {args.node_id} 不存在。", path=f"node_id={args.node_id}")
    # 展开只能作用于用户选中的那个节点。参数里虽然有 node_id，但它是模型填的——
    # 不校验的话，用户选中 A 而模型去改 B 也会“成功”，而他根本没同意改 B。
    if ctx.request.selected_ids and args.node_id not in ctx.request.selected_ids:
        allowed = "、".join(ctx.request.selected_ids)
        return _fail("not_selected", f"只能展开当前选中的节点（{allowed}），不能改 {args.node_id}。")
    if len(node.children) + args.count > MAX_CHILDREN:
        return _fail(
            "no_room",
            f"节点「{node.label}」已有 {len(node.children)} 个子节点，上限 {MAX_CHILDREN}，放不下 {args.count} 个。",
        )

    existing_titles = "、".join(child.label for child in node.children) or "（空）"
    ask = f"父节点：{node.label}\n已有子节点：{existing_titles}\n请补充 {args.count} 个新的子要点。"
    if args.instruction:
        ask += f"\n补充要求：{args.instruction}"

    try:
        if not ctx.reserve_model_call():
            return _fail("budget_exhausted", "本次运行的模型调用额度已用完，无法展开。")
        ctx.extra_model_calls += 1
        raw = await chat_async(EXPAND_PROMPT, ask)
    except GenerationError as exc:
        return _fail("generation_failed", f"展开失败：{exc}")

    points = raw.get("points") if isinstance(raw, dict) else None
    if not isinstance(points, list) or not points:
        return _fail("invalid_points", "模型没有返回可用的子要点，请重试。")

    drafts = []
    for point in points:
        label = str(point).strip()
        if not label:
            continue
        # 索引用**已接受的数量**而不是原始下标：模型返回 [" ", "有效"] 时，
        # 第一条被跳过，用原始下标会往空父节点的 index 1 插，整批 index_out_of_range。
        drafts.append({
            "op": "add",
            "id": f"$e{len(drafts)}",
            "parent_id": node.id,
            "index": len(node.children) + len(drafts),
            "label": label[:200],
        })
        if len(drafts) >= args.count:
            break
    if not drafts:
        return _fail("invalid_points", "模型返回的子要点都是空的，请重试。")

    ops, id_map = normalize_ops(drafts, _node_ids(content.root))
    batch = OpBatch(batch_id=_batch_id(ctx), base_revision=ctx.revision, ops=ops)
    try:
        updated = apply_ops(content, batch)
    except OpError as exc:
        return _fail(exc.code, exc.message, path=exc.path)

    ctx.candidate = updated
    ctx.batches.append(batch)
    ctx.revision += 1
    return ToolResult(
        ok=True,
        content=f"已在「{node.label}」下新增 {len(ops)} 个子节点。",
        data={
            "batch": batch,
            "batches": [batch],
            "id_map": id_map,
            "revision": ctx.revision,
            "labels": [op.label for op in ops],
        },
    )


async def _rag(args: RagArgs, ctx: RunContext) -> ToolResult:
    sources = _pick_sources(ctx, [])
    if not sources:
        return _fail("no_sources", "这次请求没有附带任何资料，无法检索。")

    hits = search_sources(sources, args.question, args.top_k)
    if not hits:
        return ToolResult(
            ok=True,
            content="没有在本次资料里找到相关片段。",
            data={"hits": [], "method": "lexical"},
        )

    # 回给模型的是结构化 JSON：它要能拿到 source_id 与偏移，才能在引用里指准。
    return ToolResult(
        ok=True,
        content=json.dumps([hit.model_dump() for hit in hits], ensure_ascii=False),
        data={"hits": [hit.model_dump() for hit in hits], "method": "lexical"},
    )


async def _edit(args: EditArgs, ctx: RunContext) -> ToolResult:
    content = _require_candidate(ctx)
    if isinstance(content, ToolResult):
        return content

    # 纵深防御：注册表已经按意图裁剪过，这里再按"用户选中了哪些节点"收窄一次。
    # 注册表配错时这条仍然拦得住，被测过（expand 意图下删除选中节点之外的分支）。
    scope_error = _selection_scope_error(content, args, ctx)
    if scope_error is not None:
        return scope_error

    drafts: list[dict[str, Any]] = []
    # 同一批里对同一个父节点连续 add 时，缺省 index 必须按“前面几条已经加进去之后”
    # 的长度算。直接用执行前的长度会让「先 first 后 second」变成「second、first」。
    pending: dict[str, int] = {}
    for item in args.ops:
        draft: dict[str, Any] = {"op": item.op, "id": item.id}
        if item.parent_id is not None:
            draft["parent_id"] = item.parent_id
        if item.new_parent_id is not None:
            draft["new_parent_id"] = item.new_parent_id
        # update 的 label 只能进 changes：顶层再放一份会被 UpdateOp 的
        # extra="forbid" 拒掉，那个错误信息对模型毫无指导意义。
        if item.label is not None and item.op != "update":
            draft["label"] = item.label
        if item.changes is not None:
            draft["changes"] = {"label": item.changes.label}
        elif item.op == "update" and item.label is not None:
            # 实测模型会把 label 直接写在 op 顶层而不是 changes 里。它被拒之后
            # 下一轮能自己改对（错误反馈是有效的），但那要多花一次模型调用和
            # 几秒等待。两种写法都能表达同一件事，就在入口吸收掉。
            draft["changes"] = {"label": item.label}

        # 显式 index 对所有 op 都必须保留。之前只有 add 分支复制了它，
        # move 给了合法 index 反而在契约层报 move.index: Field required。
        if item.index is not None:
            draft["index"] = item.index

        if item.op == "add":
            # 草案允许省略 index，补成追加到末尾：模型经常只给 parent_id 和 label，
            # 这不该被判成参数错误。
            parent_id = item.parent_id
            if parent_id is None:
                return _fail("invalid_ops", "add 必须给出 parent_id。", path=f"ops.{item.id}.parent_id")
            if "index" not in draft:
                parent = None if parent_id.startswith("$") else find_node(content.root, parent_id)
                base = len(parent.children) if parent else 0
                draft["index"] = base + pending.get(parent_id, 0)
                pending[parent_id] = pending.get(parent_id, 0) + 1
            draft.setdefault("source_refs", [])
        if item.op == "move" and "index" not in draft:
            draft["index"] = 0

        drafts.append(draft)

    try:
        ops, id_map = normalize_ops(drafts, _node_ids(content.root))
    except ValidationError as exc:
        detail = "；".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()[:5]
        )
        return _fail("invalid_ops", f"操作列表不合法：{detail}")

    batch = OpBatch(batch_id=_batch_id(ctx), base_revision=ctx.revision, ops=ops)
    try:
        updated = apply_ops(content, batch)
    except OpError as exc:
        # 把执行器的错误原样回给模型：它知道是第几条、哪个字段错了才能自己改对
        return _fail(exc.code, f"{exc.path}：{exc.message}", path=exc.path)

    changed = {op.id for op in ops} | {
        op.new_parent_id for op in ops if op.op == "move" and op.new_parent_id
    }
    ctx.candidate = updated
    ctx.batches.append(batch)
    ctx.revision += 1
    return ToolResult(
        ok=True,
        content=f"已应用 {len(ops)} 条修改，导图现在有 {_count_nodes(updated.root)} 个节点。",
        data={
            "batch": batch,
            "batches": [batch],
            "id_map": id_map,
            "revision": ctx.revision,
            "changed_ids": sorted(changed),
        },
    )


def _batch_id(ctx: RunContext) -> str:
    return f"b{ctx.revision + 1}-{len(ctx.batches)}"


# ---------------------------------------------------------------- 注册


# 意图 -> 允许的工具。这张表就是"模型这条路能走多远"的唯一声明。
_ALLOWED_BY_INTENT: dict[str, tuple[str, ...]] = {
    "create": ("mindmap_generate", "mindmap_generate_deep"),
    "expand": ("node_expand",),
    "edit": ("mindmap_edit",),
}

# 会读来源的意图。`create` 不在里面：整张从零生成时不给检索入口，
# 否则模型会把"先查一下"当成规定动作（实测 10 次里 10 次都先查一遍）。
_SOURCE_AWARE_INTENTS = ("expand", "edit")

# 注册顺序固定，便于日志与测试对照；用工厂而不是共享实例，避免一个 registry
# 上的改动影响到另一个（ToolSpec 目前不可变，但这个保证不该靠约定维持）。
_TOOL_ORDER = (
    "mindmap_generate",
    "mindmap_generate_deep",
    "node_expand",
    "rag_query",
    "mindmap_edit",
)

_TOOL_SPECS: dict[str, Callable[[], ToolSpec]] = {
    "mindmap_generate": lambda: ToolSpec(
        name="mindmap_generate",
        description=(
            "一次调用生成一张完整思维导图，适合分支少、内容浅的简单主题。"
            "只有当前还没有导图时可用；复杂主题（分支多、每支都要展开几个要点）"
            "改用 mindmap_generate_deep，它更慢但更不容易被单次输出上限截断。"
        ),
        args_model=GenerateArgs,
        fn=_generate,
    ),
    "mindmap_generate_deep": lambda: ToolSpec(
        name="mindmap_generate_deep",
        description=(
            "分片生成一张完整思维导图：先定一级骨架，再并发展开每条分支，"
            "最后按代码验收并只补不合格的那几支。适合复杂主题——它每次只让模型"
            "输出一支，不受单次输出上限挤压，因此比 mindmap_generate 更稳。"
            "简单主题用 mindmap_generate（一次调用就够，更快）。"
            "只有当前还没有导图时可用。"
        ),
        args_model=GenerateDeepArgs,
        fn=_generate_deep,
    ),
    "node_expand": lambda: ToolSpec(
        name="node_expand",
        description="把当前大纲里的某个节点展开成若干子要点，只新增子节点，不改动其他节点。",
        args_model=ExpandArgs,
        fn=_expand,
    ),
    "rag_query": lambda: ToolSpec(
        name="rag_query",
        description="在本次请求附带的资料里检索相关原文片段。回答关于资料的问题之前先调用它。",
        args_model=RagArgs,
        fn=_rag,
        readonly=True,
    ),
    "mindmap_edit": lambda: ToolSpec(
        name="mindmap_edit",
        description="按节点 id 精确修改已有导图：增删改节点或移动子树。不要用它整张重建。",
        args_model=EditArgs,
        fn=_edit,
    ),
}


def build_registry(intent: str = "create", *, has_sources: bool = False) -> ToolRegistry:
    """本次可用的工具集，按意图裁剪。

    早先是「四个全注册 + 各自内部校验意图」。那样做的问题是模型看得到工具、
    却每次调用都被拒：实测 expand 意图下它坚持调 `mindmap_edit` 删节点，
    失败三次才转向，白白烧掉调用额度。看不到这个工具，它就只能走对的那条路。

    裁剪规则：
      - `create` 给 `mindmap_generate` 与 `mindmap_generate_deep` 两个，由模型
        按主题复杂度自选（描述里写清了两者的差别）
      - `expand` 只给 `node_expand`，请求带来源时再加 `rag_query`
      - `edit` 给 `mindmap_edit`，请求带来源时再加 `rag_query`

    两个生成工具必须同时在场：只留一个，等于替用户决定了"这张图多大"。而
    分片那条路的成本是 N+1 次调用，简单主题走它纯属浪费额度——这个判断
    只有拿到主题的模型能做。

    `rag_query` 只在真有来源时才注册：没来源的检索必然返回「没有找到」，
    注册它只会诱导模型拿它当"思考"用。未知意图按 `create` 兜底——宁可少给
    工具，也不要把破坏性工具塞进一个没预期到的新意图里。
    """
    allowed = set(_ALLOWED_BY_INTENT.get(intent, _ALLOWED_BY_INTENT["create"]))
    if has_sources and intent in _SOURCE_AWARE_INTENTS:
        allowed.add("rag_query")

    registry = ToolRegistry()
    for name in _TOOL_ORDER:
        if name in allowed:
            registry.register(_TOOL_SPECS[name]())
    return registry
