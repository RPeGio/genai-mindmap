"""工具注册表与四个业务工具。

注册表只负责分派与参数校验，业务逻辑在各个 `_xxx` 函数里。

四个工具的划分照「用户意图 + 作用域」而不是 CRUD 动词：
- `mindmap_generate` 只在没有导图时可用，产出一整棵候选树
- `node_expand` 只能在已选中的节点下加子节点，不能删、不能移
- `rag_query` 只读，且只能查本次请求带来的来源
- `mindmap_edit` 才允许改/删/移，且必须按节点 id

这样切是为了把「改错地方」的风险压到协议层：`node_expand` 的入参里没有
`parent_id` 的自由度（只能是你当前选中的那个），模型想乱挂也挂不了。
"""
from __future__ import annotations

import inspect
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agent.contracts import RunContext, ToolCall, ToolResult, ToolSpec
from app.agent.ops import apply_ops, find_node, normalize_ops
from app.contracts.mindmap import MindMapContent
from app.contracts.ops import OpBatch, OpError
from app.generation.service import (
    DETAIL_PROMPT,
    SOURCED_DETAIL_PROMPT,
    GenerationError,
    chat_async,
    validate_generated_content,
)
from app.knowledge.service import search_sources
from app.pipeline.service import _understand

# 与 backend/app/contracts/mindmap.py 的 Node.children max_length 同口径
MAX_CHILDREN = 50


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


EXPAND_PROMPT = (
    "你在给思维导图的一个节点补充子要点。只返回一个 JSON 对象，不要代码围栏。"
    '格式为 {"points":["要点一","要点二"]}。'
    "每个要点必须是一句能独立成立的话，不超过 30 字；不要编号，不要解释，"
    "不要重复已有子节点，不要编造具体数据、人名或文献。"
)


# ---------------------------------------------------------------- 工具实现


async def _generate(args: GenerateArgs, ctx: RunContext) -> ToolResult:
    if ctx.request.intent != "create":
        return _fail("wrong_intent", "只有新建导图时才能调用这个工具；修改现有导图请用 node_expand 或 mindmap_edit。")
    if ctx.candidate is not None and ctx.candidate.root.children:
        return _fail("map_exists", "已经有一张导图了，请用 node_expand 或 mindmap_edit 修改它。")

    sources = _pick_sources(ctx, [])
    try:
        if sources:
            raw = await chat_async(SOURCED_DETAIL_PROMPT, _understand(sources))
        else:
            raw = await chat_async(DETAIL_PROMPT, args.topic)
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


async def _expand(args: ExpandArgs, ctx: RunContext) -> ToolResult:
    content = _require_candidate(ctx)
    if isinstance(content, ToolResult):
        return content

    node = find_node(content.root, args.node_id)
    if node is None:
        return _fail("node_not_found", f"节点 {args.node_id} 不存在。", path=f"node_id={args.node_id}")
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
        raw = await chat_async(EXPAND_PROMPT, ask)
    except GenerationError as exc:
        return _fail("generation_failed", f"展开失败：{exc}")

    points = raw.get("points") if isinstance(raw, dict) else None
    if not isinstance(points, list) or not points:
        return _fail("invalid_points", "模型没有返回可用的子要点，请重试。")

    drafts = []
    for offset, point in enumerate(points[: args.count]):
        label = str(point).strip()
        if not label:
            continue
        drafts.append({
            "op": "add",
            "id": f"$e{offset}",
            "parent_id": node.id,
            "index": len(node.children) + offset,
            "label": label[:200],
        })
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

    drafts: list[dict[str, Any]] = []
    for item in args.ops:
        draft: dict[str, Any] = {"op": item.op, "id": item.id}
        if item.parent_id is not None:
            draft["parent_id"] = item.parent_id
        if item.new_parent_id is not None:
            draft["new_parent_id"] = item.new_parent_id
        if item.label is not None:
            draft["label"] = item.label
        if item.changes is not None:
            draft["changes"] = {"label": item.changes.label}

        if item.op == "add":
            # 草案允许省略 index，补成追加到末尾：模型经常只给 parent_id 和 label，
            # 这不该被判成参数错误。
            parent_id = item.parent_id
            if parent_id is None:
                return _fail("invalid_ops", "add 必须给出 parent_id。", path=f"ops.{item.id}.parent_id")
            parent = None if parent_id.startswith("$") else find_node(content.root, parent_id)
            draft["index"] = item.index if item.index is not None else (len(parent.children) if parent else 0)
            draft.setdefault("source_refs", [])
        if item.op == "move" and item.index is None:
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


def build_registry() -> ToolRegistry:
    """本次可用的工具集。

    四个工具都注册，但各自用 intent / 候选状态做前置校验——注册表不按意图裁剪，
    因为模型看到"工具存在但你不能用"并收到一句解释，比看不到这个工具却不知道
    为什么更好调试。
    """
    registry = ToolRegistry()
    registry.register(ToolSpec(
        name="mindmap_generate",
        description="为主题生成一张完整思维导图。只有当前还没有导图时可用。",
        args_model=GenerateArgs,
        fn=_generate,
    ))
    registry.register(ToolSpec(
        name="node_expand",
        description="把当前大纲里的某个节点展开成若干子要点，只新增子节点，不改动其他节点。",
        args_model=ExpandArgs,
        fn=_expand,
    ))
    registry.register(ToolSpec(
        name="rag_query",
        description="在本次请求附带的资料里检索相关原文片段。回答关于资料的问题之前先调用它。",
        args_model=RagArgs,
        fn=_rag,
    ))
    registry.register(ToolSpec(
        name="mindmap_edit",
        description="按节点 id 精确修改已有导图：增删改节点或移动子树。不要用它整张重建。",
        args_model=EditArgs,
        fn=_edit,
    ))
    return registry
