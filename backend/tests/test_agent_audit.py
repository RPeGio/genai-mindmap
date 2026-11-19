"""`audit_tree` 的行为测试。

它存在的理由只有一个：**"漏了哪几支"必须由代码数，不能由模型自己想**。

分片生成那条路上，循环边界是代码保证的（工具内部 gather 全部支、自己判 sparse
并报 `partial`）；`expand` 意图下没有这层保证——模型逐个 `node_expand` 展开，
而它看到的大纲只列前 200 个节点。所以这里的用例都盯着同一件事：数出来的东西
是不是**树的事实**，以及它有没有在任何一条路径上偷偷改动树。

三个列表的口径（改一处就要改这里）：
  - `missing`：一级分支里没有子节点的；
  - `thin`：一级分支里只有一个子节点的;
  - `empty`：**非**一级分支且没有子节点的（一级分支归 `missing`，不重复报）。
"""
from __future__ import annotations

import asyncio

from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
from app.agent.tools import AUDIT_LIST_LIMIT, build_registry

TREE = {
    "title": "示例",
    "root": {
        "id": "root", "label": "根", "children": [
            {"id": "b1", "label": "甲", "children": [
                {"id": "b1a", "label": "甲一", "children": []},
            ]},
            {"id": "b2", "label": "乙", "children": []},
            {"id": "b3", "label": "丙", "children": []},
            {"id": "b4", "label": "丁", "children": [
                {"id": "b4a", "label": "丁一", "children": [
                    {"id": "b4a1", "label": "丁一一", "children": []},
                ]},
                {"id": "b4b", "label": "丁二", "children": []},
            ]},
        ],
    },
}


def _ctx(tree: dict | None = None, *, expandable: str | None = None) -> RunContext:
    from app.contracts.mindmap import MindMapContent

    candidate = None
    if tree is not None:
        payload = tree
        if expandable is not None:
            payload = _stamp(tree, expandable)
        candidate = MindMapContent.model_validate(payload)
    request = AgentRunRequest(
        goal="把这张图补完", intent="expand",
        budget=RunBudget(max_model_calls=5),
    )
    return RunContext(request=request, candidate=candidate,
                      sources_by_id={})


def _stamp(tree: dict, value: str) -> dict:
    """把整棵树的 `expandable` 统一写成某个值（`pending` 那条用例要用）。"""

    def walk(node: dict) -> dict:
        return {**node, "expandable": value,
                "children": [walk(child) for child in node.get("children", [])]}

    return {"title": tree["title"], "root": walk(tree["root"])}


async def _execute(registry, ctx):
    return await registry.execute(
        ToolCall(call_id="c-audit", name="audit_tree", arguments={}), ctx)


def _audit(ctx: RunContext):
    registry = build_registry("expand", has_library=False)
    return asyncio.run(_execute(registry, ctx))


def test_no_candidate_is_a_readable_failure():
    result = _audit(_ctx(None))
    assert result.ok is False
    assert result.data["code"] == "no_candidate"
    assert "还没有导图" in result.content


def test_counts_missing_thin_and_empty_from_the_real_tree():
    result = _audit(_ctx(TREE))
    assert result.ok is True
    data = result.data
    assert data["branches"] == 4
    assert data["expanded"] == 2          # 甲 与 丁 有子节点
    assert data["nodes"] == 9
    assert data["depth"] == 4             # root → 丁 → 丁一 → 丁一一

    assert [(item["id"], item["label"]) for item in data["missing"]] == [
        ("b2", "乙"), ("b3", "丙")]
    assert data["missing_count"] == 2

    assert [(item["id"], item["children"]) for item in data["thin"]] == [("b1", 1)]
    assert data["thin_count"] == 1

    # 一级分支没有子节点归 missing，不重复出现在 empty 里。
    # 深度：root=1 / 一级分支=2 / 它们的子节点=3 起。
    assert {(item["id"], item["depth"]) for item in data["empty"]} == {
        ("b1a", 3), ("b4a1", 4), ("b4b", 3)}
    assert data["empty_count"] == 3


def test_content_tells_the_model_what_to_do_next():
    partial = _audit(_ctx(TREE))
    assert "先补这些" in partial.content
    assert "乙" in partial.content and "b2" in partial.content

    only_skeleton = _audit(_ctx({
        "title": "示例",
        "root": {"id": "root", "label": "根", "children": [
            {"id": "a", "label": "甲", "children": []},
            {"id": "b", "label": "乙", "children": []},
            {"id": "c", "label": "丙", "children": []},
        ]},
    }))
    assert only_skeleton.data["missing_count"] == 3
    assert only_skeleton.data["thin_count"] == 0
    assert only_skeleton.data["empty_count"] == 0

    full = _audit(_ctx({
        "title": "示例",
        "root": {"id": "root", "label": "根", "children": [
            {"id": "a", "label": "甲", "children": [
                {"id": "a1", "label": "甲一", "children": []},
                {"id": "a2", "label": "甲二", "children": []},
            ]},
        ]},
    }))
    assert full.data["missing_count"] == 0
    assert "可以收尾" in full.content


def test_pending_counts_only_nodes_that_can_still_be_opened():
    stamped = _audit(_ctx(TREE, expandable="no"))
    # 根不计数：它永远是 unknown/yes，算进来会让"全都到底了"显示成 1 处待展开。
    assert stamped.data["pending"] == 0

    unknown = _audit(_ctx(TREE))
    assert unknown.data["pending"] == 8   # 除根以外的 8 个节点


def test_audit_never_touches_the_tree():
    ctx = _ctx(TREE)
    before = ctx.candidate.model_dump()
    revision = ctx.revision

    first = asyncio.run(_execute(build_registry("expand", has_library=False), ctx))
    second = asyncio.run(_execute(build_registry("expand", has_library=False), ctx))

    assert first.data == second.data
    assert ctx.revision == revision
    assert ctx.candidate.model_dump() == before


def test_lists_are_capped_but_the_counts_are_not():
    branches = [{"id": f"b{index}", "label": f"分支{index}", "children": []}
                for index in range(AUDIT_LIST_LIMIT + 5)]
    result = _audit(_ctx({"title": "示例",
                          "root": {"id": "root", "label": "根", "children": branches}}))
    assert result.data["missing_count"] == AUDIT_LIST_LIMIT + 5
    assert len(result.data["missing"]) == AUDIT_LIST_LIMIT
    # 被截断时 content 也要说清"列不全"，否则模型会以为只差列出来的那 20 支。
    assert f"只列前 {AUDIT_LIST_LIMIT} 条" in result.content


def test_registered_for_expand_only_and_marked_readonly():
    assert "audit_tree" in build_registry("expand", has_library=False).names()
    assert "audit_tree" in build_registry("expand", has_sources=True,
                                          has_library=False).names()
    # create 只有一条生成路（`tests/test_one_path.py` 钉着），审计不是第二条路，
    # 但也不该挤进去；edit 是按 id 的精确操作，整图体检对它没有意义。
    assert "audit_tree" not in build_registry("create", has_library=False).names()
    assert "audit_tree" not in build_registry("edit", has_library=False).names()

    assert build_registry("expand", has_library=False).get("audit_tree").readonly is True
    # 只读工具按参数去重：同一轮里调两次不该算两遍。
    assert build_registry("expand", has_library=False).get("audit_tree") is not None


def test_unknown_arguments_are_rejected():
    """它没有参数——模型硬塞一个进来要报错，而不是被静默忽略。

    静默忽略的后果是模型以为自己传的 `branches` 生效了，于是对着自己那份骨架
    读审计结果，而那正是这个工具要消灭的东西。
    """
    ctx = _ctx(TREE)
    result = asyncio.run(build_registry("expand", has_library=False).execute(
        ToolCall(call_id="c-x", name="audit_tree", arguments={"branches": 8}), ctx))
    assert result.ok is False
    assert result.data["code"] == "invalid_tool_args"


# ---------------------------------------------------------------- 端到端


def _completion(calls: list[ToolCall]):
    import json

    from app.agent.contracts import Completion

    message = {"role": "assistant", "content": ""}
    if calls:
        message["tool_calls"] = [
            {"id": call.call_id, "type": "function",
             "function": {"name": call.name,
                          "arguments": json.dumps(call.arguments, ensure_ascii=False)}}
            for call in calls
        ]
    return Completion(message=message, tool_calls=calls,
                      finish_reason="tool_calls" if calls else "stop",
                      usage={})


def test_the_expand_loop_reaches_the_audit_and_leaves_the_tree_alone():
    """端到端：`expand` 意图下模型能拿到审计计数，且树一个字节都没变。

    这条用例守的是"注册 + 只读"两件事在**真的主循环**里成立，而不只是在
    `registry.execute` 这一层成立：`readonly` 还会让同参数的重复调用命中缓存
    （`service._execute_once`），所以它不能有任何副作用。
    """
    from app.agent.service import run
    from app.contracts.mindmap import MindMapContent

    snapshot = MindMapContent.model_validate(TREE)
    request = AgentRunRequest(
        goal="把这张图补完", intent="expand", snapshot=snapshot,
        budget=RunBudget(max_tool_calls=5),
    )

    class _Provider:
        def __init__(self):
            self.script = [
                _completion([ToolCall(call_id="c1", name="audit_tree", arguments={})]),
                _completion([]),
                # 第三轮：体检说还缺 2 支，收尾提醒生效后再收一次。
                _completion([]),
            ]

        async def complete_tools(self, messages, tools, *, budget):
            return self.script.pop(0)

    async def drain():
        return [event async for event in run(request, provider=_Provider())]

    events = asyncio.run(drain())
    tool_ends = [event for event in events if event.type == "tool_end"]
    assert len(tool_ends) == 1
    assert tool_ends[0].ok is True
    assert tool_ends[0].result_summary["missing_count"] == 2
    assert tool_ends[0].result_summary["branches"] == 4
    # 只读：没有 op 批次，也没有新的整树快照（树从请求里来，不是这次生成的）。
    assert not [event for event in events if event.type == "ops"]
    assert not [event for event in events if event.type == "snapshot"]
    assert events[-1].type == "done"
    assert events[-1].content.model_dump() == snapshot.model_dump()


# ---------------------------------------------------------------- 收尾提醒


class _LoopProvider:
    """按顺序回放脚本，并记下主循环问了几次。"""

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.rounds = 0

    async def complete_tools(self, messages, tools, *, budget):
        self.rounds += 1
        return self.script.pop(0)


def _loop_events(script: list, *, tree: dict = TREE, calls: int = 5):
    from app.agent.service import run
    from app.contracts.mindmap import MindMapContent

    request = AgentRunRequest(
        goal="把这张图补完", intent="expand",
        snapshot=MindMapContent.model_validate(tree),
        budget=RunBudget(max_tool_calls=calls),
    )

    async def drain():
        return [event async for event in run(request, provider=_LoopProvider(script))]

    return asyncio.run(drain())


def test_pending_audit_earns_exactly_one_reminder_before_finishing():
    """模型**自己**查出还缺 2 支却要收尾 → 代码提醒一次；它还停，就放行。

    这是"循环边界由代码保证"唯一的落点：代码不判断"该不该补"（那是模型的判断），
    它只把模型已经知道的事实再说一遍，并给它一次改的机会。
    """
    audit = _completion([ToolCall(call_id="c1", name="audit_tree", arguments={})])
    events = _loop_events([audit, _completion([]), _completion([])])

    stages = [event.stage for event in events if event.type == "stage"]
    assert stages.count("audit_pending") == 1
    reminder = [event.detail for event in events
                if event.type == "stage" and event.stage == "audit_pending"][0]
    assert "还有 2 条一级分支" in reminder
    # 提醒之后模型还是收尾：只提醒一次，run 正常 done（不是 error）。
    assert events[-1].type == "done"


def test_a_clean_audit_never_triggers_the_reminder():
    clean = {"title": "示例", "root": {"id": "root", "label": "根", "children": [
        {"id": "a", "label": "甲", "children": [
            {"id": "a1", "label": "甲一", "children": []},
        ]},
    ]}}
    events = _loop_events(
        [_completion([ToolCall(call_id="c1", name="audit_tree", arguments={})]),
         _completion([])],
        tree=clean,
    )
    assert not [event for event in events
                if event.type == "stage" and event.stage == "audit_pending"]


def test_no_audit_means_no_reminder():
    """没体检过就收尾：一个字都不加。

    `expand` 意图的用户可能只是要展开一个节点，那时"还有 3 支没展开"是正常状态。
    没有这一条，`audit_tree` 就从"帮模型查漏"变成了"强迫它把整张图写完"。
    """
    events = _loop_events([_completion([])])
    assert not [event for event in events
                if event.type == "stage" and event.stage == "audit_pending"]
    assert events[-1].type == "done"
