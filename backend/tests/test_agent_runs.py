"""`runs` 表与 run 身份的测试。

这一层验的是此前不存在的东西：**同一次运行的身份**与**运行结束（含被取消）时的
草稿**。SSE 帧格式一个字节都没改，所以这里不重复验帧（那份在 test_agent_stream）。

取消那条用例直接驱动 `router._frames`：uvicorn 在客户端断开时做的事就是对自己
手里的响应生成器调一次 `aclose()`，这条路与真机一致，而且能精确停在"候选已经
产出、用户刚好关页面"的那一刻——换成 TestClient 打请求，取消时机就不可控了。
"""
from __future__ import annotations

import asyncio
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

import app.agent.router as agent_router
from app.agent import runs
from app.agent.contracts import (
    AgentRunRequest,
    Completion,
    DoneEvent,
    StageEvent,
    ToolCall,
    ToolResult,
    ToolSpec,
)
from app.agent.router import _request_summary, router
from app.agent.service import run as service_run
from app.agent.stream import DONE_FRAME, encode_event
from app.agent.tools import ToolRegistry
from app.contracts.mindmap import MindMapContent
from app.contracts.source import Source
from app.core.errors import register_error_handlers

app = FastAPI()
register_error_handlers(app)
app.include_router(router, prefix="/api")
client = TestClient(app)

URL = "/api/agent/runs/stream"

# 三条一级分支：`mindmap_generate` 的默认校验要求 ≥3，少一条整棵树会被判 invalid。
TREE = {
    "title": "示例",
    "root": {"id": "root", "label": "主题", "children": [
        {"id": "a", "label": "甲", "children": []},
        {"id": "b", "label": "乙", "children": []},
        {"id": "c", "label": "丙", "children": []},
    ]},
}
CANDIDATE = MindMapContent.model_validate(TREE)


def _frames(text: str) -> list[dict]:
    out: list[dict] = []
    for block in text.split("\n\n"):
        line = block.strip()
        if not line.startswith("data: "):
            continue
        payload = line[6:].strip()
        if payload == "[DONE]":
            continue
        out.append(json.loads(payload))
    return out


async def _scripted(*events):
    for event in events:
        yield event


def _completion(calls: list[ToolCall] | None = None, finish: str = "stop") -> Completion:
    calls = calls or []
    message: dict[str, Any] = {"role": "assistant", "content": "好的"}
    if calls:
        message["tool_calls"] = [
            {"id": call.call_id, "type": "function",
             "function": {"name": call.name,
                          "arguments": json.dumps(call.arguments, ensure_ascii=False)}}
            for call in calls
        ]
    return Completion(message=message, tool_calls=calls, finish_reason=finish,
                      usage={"total_tokens": 1})


class ScriptedProvider:
    """按脚本返回；脚本项可以是 Completion，可以是异常，也可以是"永不返回"。"""

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.calls = 0

    async def complete_tools(self, messages, tools, *, budget):
        self.calls += 1
        if not self._script:
            raise AssertionError("脚本已用完，循环比预期多调了一次模型")
        item = self._script.pop(0)
        if item is _HANG:
            await asyncio.Event().wait()
        if isinstance(item, BaseException):
            raise item
        return item


_HANG = object()


class _EchoArgs(BaseModel):
    text: str = Field(min_length=1)


def _registry(handler) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ToolSpec(name="echo", description="测试工具", args_model=_EchoArgs, fn=handler))
    return registry


# ---------------------------------------------------------------- 仓储层


def test_begin_creates_a_running_row():
    claim = runs.begin("r1", {"goal": "做一张导图", "intent": "create"})

    assert claim.claimed is True
    assert claim.record.status == runs.STATUS_RUNNING
    row = runs.get("r1")
    assert row is not None
    assert row.request == {"goal": "做一张导图", "intent": "create"}
    assert row.candidate is None
    # 时间戳只在服务端生成，客户端不能指定：它是"谁更晚"的唯一依据。
    assert row.created_at == row.updated_at
    assert row.created_at.endswith("Z")


def test_finish_writes_candidate_and_status():
    runs.begin("r1", {})
    assert runs.finish("r1", runs.STATUS_DONE, candidate=CANDIDATE, mode="model") is True

    row = runs.get("r1")
    assert row.status == runs.STATUS_DONE
    assert row.candidate is not None
    assert row.candidate.root.label == "主题"
    assert row.mode == "model"


def test_repeated_begin_is_idempotent_and_keeps_the_draft():
    """同一 run_id 重复 begin：不报错，也不覆盖已经产出的候选。

    这条是"两个标签页同时投递"的底线：第二个请求可以拿不到运行权，但它不能把
    第一个已经跑出来的草稿抹掉——那等于用户为了捡结果而点一下，结果就没了。
    """
    runs.begin("r1", {"goal": "第一次"})
    runs.finish("r1", runs.STATUS_DONE, candidate=CANDIDATE)

    again = runs.begin("r1", {"goal": "第二次"})

    assert again.claimed is False
    row = runs.get("r1")
    assert row.status == runs.STATUS_DONE
    assert row.candidate is not None and row.candidate.root.label == "主题"
    # 身份行记的是"这一次运行是什么"，不是"最后一次请求说了什么"。
    assert row.request == {"goal": "第一次"}


def test_done_is_terminal_so_a_late_finish_cannot_overwrite_it():
    """`done` 之后冻结：一条迟到的取消不许把已经交付的候选改写成 cancelled。"""
    runs.begin("r1", {})
    runs.finish("r1", runs.STATUS_DONE, candidate=CANDIDATE)

    assert runs.finish("r1", runs.STATUS_CANCELLED) is False
    row = runs.get("r1")
    assert row.status == runs.STATUS_DONE
    assert row.candidate is not None


def test_finish_without_candidate_never_clears_the_previous_draft():
    """收尾没拿到候选（被取消的那次树还没生成完）不该把上一份草稿写成 NULL。"""
    runs.begin("r1", {})
    runs.finish("r1", runs.STATUS_CANCELLED, candidate=CANDIDATE)

    runs.finish("r1", runs.STATUS_CANCELLED)

    assert runs.get("r1").candidate is not None


def test_cancelled_and_failed_runs_can_be_reopened_but_done_cannot():
    for terminal in (runs.STATUS_CANCELLED, runs.STATUS_FAILED):
        runs.begin("r-" + terminal, {})
        runs.finish("r-" + terminal, terminal)

        reopened = runs.begin("r-" + terminal, {})

        assert reopened.claimed is True, terminal
        assert runs.get("r-" + terminal).status == runs.STATUS_RUNNING

    runs.begin("r-done", {})
    runs.finish("r-done", runs.STATUS_DONE)
    assert runs.begin("r-done", {}).claimed is False


def test_finish_rejects_non_terminal_status():
    runs.begin("r1", {})
    with unittest.TestCase().assertRaises(ValueError):
        runs.finish("r1", runs.STATUS_RUNNING)


def test_pending_only_returns_done_runs_that_were_never_applied():
    """`pending()` 的字面含义：**done** 且没应用过。"""
    runs.begin("done-unapplied", {})
    runs.finish("done-unapplied", runs.STATUS_DONE, candidate=CANDIDATE)

    runs.begin("done-applied", {})
    runs.finish("done-applied", runs.STATUS_DONE, candidate=CANDIDATE)
    runs.mark_applied("done-applied")

    runs.begin("done-no-candidate", {})
    runs.finish("done-no-candidate", runs.STATUS_DONE)

    runs.begin("cancelled-with-draft", {})
    runs.finish("cancelled-with-draft", runs.STATUS_CANCELLED, candidate=CANDIDATE)

    runs.begin("still-running", {})

    assert [row.run_id for row in runs.pending()] == ["done-unapplied"]
    # 前端展示时把"被取消但有草稿"一起算：那 40 秒正是这么捡回来的。
    assert {row.run_id for row in runs.pending(
        statuses=(runs.STATUS_DONE, runs.STATUS_CANCELLED))} == {
            "done-unapplied", "cancelled-with-draft"}


def test_dismissed_runs_stop_showing_up():
    runs.begin("r1", {})
    runs.finish("r1", runs.STATUS_DONE, candidate=CANDIDATE)

    assert runs.mark_dismissed("r1") is True

    assert runs.pending() == []
    # 与"应用过"分开记：丢弃过的候选仍能被查到，只是不再提醒。
    row = runs.get("r1")
    assert row.dismissed_at is not None
    assert row.applied_at is None


def test_mark_applied_is_idempotent_and_keeps_the_first_stamp():
    runs.begin("r1", {})
    runs.finish("r1", runs.STATUS_DONE, candidate=CANDIDATE)

    assert runs.mark_applied("r1") is True
    first = runs.get("r1").applied_at
    assert runs.mark_applied("r1") is True

    assert runs.get("r1").applied_at == first


def test_eight_simultaneous_begin_calls_start_exactly_one_run():
    """并发认领靠 SQLite 的写锁，不靠进程内字典——后者在多 worker 下每个进程一份。"""
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda _: runs.begin("race", {}), range(8)))

    assert sum(claim.claimed for claim in claims) == 1
    assert all(claim.record.run_id == "race" for claim in claims)


def test_request_summary_never_carries_source_text():
    """`runs` 是身份台账，不是材料仓库：正文一个字都不进来。"""
    secret = "绝密原文" * 500
    body = AgentRunRequest(goal="整理材料", sources=[Source(kind="text", text=secret)])

    summary = _request_summary(body)

    assert summary["source_ids"] == [body.sources[0].id]
    assert secret not in json.dumps(summary, ensure_ascii=False)


# ---------------------------------------------------------------- 路由层


def test_old_client_without_run_id_gets_byte_identical_frames():
    """不带 run_id 的老客户端：响应体与加这张表之前**逐字节**相同。

    服务端会自己补一个 run_id 落库，但那是副作用，不许出现在帧里——帧格式一变，
    旧前端就会开始丢字段。
    """
    events = [
        StageEvent(seq=1, stage="准备"),
        DoneEvent(seq=2, content=CANDIDATE, mode="model"),
    ]
    with patch("app.agent.router.run", return_value=_scripted(*events)):
        response = client.post(URL, json={"goal": "做一张导图", "intent": "create"})

    assert response.status_code == 200
    assert response.text == "".join(encode_event(event) for event in events) + DONE_FRAME
    # 副作用确实发生了：这一行有结论，并且 run_id 是服务端生成的合法形状。
    rows = runs.pending(statuses=(runs.STATUS_DONE,), unresolved_only=False)
    assert len(rows) == 1
    assert len(rows[0].run_id) == 32 and rows[0].run_id.isalnum()


def test_records_are_written_for_each_terminal_status():
    """正常收尾 / 上游失败 / 中途被取消，三种结局都落一行。"""
    with patch("app.agent.router.run", return_value=_scripted(
            DoneEvent(seq=1, content=CANDIDATE, mode="model"))):
        client.post(URL, json={"goal": "g", "intent": "create", "run_id": "ok"})

    async def failing():
        from app.generation.service import GenerationError
        raise GenerationError("模型服务不可用")
        yield  # pragma: no cover

    with patch("app.agent.router.run", return_value=failing()):
        client.post(URL, json={"goal": "g", "intent": "create", "run_id": "bad"})

    ok, bad = runs.get("ok"), runs.get("bad")
    assert ok.status == runs.STATUS_DONE and ok.candidate is not None
    assert bad.status == runs.STATUS_FAILED
    assert bad.failure == {"code": "generation_failed", "message": "模型服务不可用"}
    assert bad.candidate is None


def test_same_run_id_while_running_is_409():
    runs.begin("dup", {"goal": "g"})
    started: list[int] = []

    def should_not_start(_request, **_kwargs):
        started.append(1)
        return _scripted()

    with patch("app.agent.router.run", new=should_not_start):
        response = client.post(URL, json={"goal": "g", "intent": "create", "run_id": "dup"})

    assert response.status_code == 409
    assert response.json()["code"] == "run_in_progress"
    assert response.json()["message"]
    assert started == []


def test_same_run_id_after_done_replays_the_candidate_without_calling_the_model():
    """同一个 run_id 第二次请求：把已有候选原样重放，**一次模型调用都不发**。"""
    runs.begin("done-1", {"goal": "g", "intent": "create"})
    runs.finish("done-1", runs.STATUS_DONE, candidate=CANDIDATE, mode="model")

    chats: list[str] = []

    async def fake_chat(system, user, **kwargs):
        chats.append(user)
        return TREE

    def should_not_start(_request, **_kwargs):
        raise AssertionError("已经有结论的 run 不该重新跑")

    with patch("app.agent.tools.chat_async", new=fake_chat), \
            patch("app.agent.router.run", new=should_not_start):
        response = client.post(URL, json={"goal": "g", "intent": "create", "run_id": "done-1"})

    frames = _frames(response.text)
    assert [frame["type"] for frame in frames] == ["snapshot", "done"]
    assert frames[-1]["content"]["title"] == "示例"
    # 来历也照原样回填：占位树被重放成 model 就是界面替它撒谎。
    assert frames[-1]["mode"] == "model"
    assert response.text.rstrip().endswith("data: [DONE]")
    assert chats == []


def test_replay_keeps_demo_mode():
    runs.begin("demo-1", {"goal": "g"})
    runs.finish("demo-1", runs.STATUS_DONE, candidate=CANDIDATE, mode="demo")

    def should_not_start(*_args, **_kwargs):
        raise AssertionError("不该重新跑")

    with patch("app.agent.router.run", new=should_not_start):
        response = client.post(URL, json={"goal": "g", "intent": "create", "run_id": "demo-1"})

    assert _frames(response.text)[-1]["mode"] == "demo"


def test_cancelled_run_is_reopened_instead_of_being_replayed():
    """取消过的 run 允许用同一个 id 重跑：那是用户主动重试，不是重复投递。"""
    runs.begin("retry-1", {"goal": "g"})
    runs.finish("retry-1", runs.STATUS_CANCELLED, candidate=CANDIDATE)

    with patch("app.agent.router.run", return_value=_scripted(
            DoneEvent(seq=1, content=CANDIDATE, mode="model"))) as replayed:
        response = client.post(URL, json={"goal": "g", "intent": "create", "run_id": "retry-1"})

    assert replayed.called
    assert [frame["type"] for frame in _frames(response.text)] == ["done"]
    assert runs.get("retry-1").status == runs.STATUS_DONE


def test_run_id_is_validated_and_extra_fields_still_forbidden():
    """新字段进契约，`extra="forbid"` 那条护栏不能跟着松掉。"""
    assert client.post(URL, json={
        "goal": "g", "intent": "create", "run_id": "a/b"}).status_code == 422
    assert client.post(URL, json={
        "goal": "g", "intent": "create", "runid": "abc"}).status_code == 422


# ---------------------------------------------------------------- 取消路径


def _routed_with(provider, registry):
    """把 `_frames` 调用的 `run` 换成"真循环 + 假 provider"。"""

    def routed(request, *, on_close=None, first_seq=None):
        return service_run(request, provider=provider, tools=registry, on_close=on_close)

    return routed


def _open_run(run_id: str, request: AgentRunRequest) -> None:
    """真机上 `stream_run` 开流之前都会先 `begin()`，这里直接驱动 `_frames` 要补上。"""
    runs.begin(run_id, {"goal": request.goal, "intent": request.intent})


def _cancel_after_first_snapshot(run_id: str, request: AgentRunRequest) -> list[str]:
    """驱动 `_frames`，收到 snapshot 帧就关掉生成器（= 客户端断开 / 用户取消）。"""

    async def scenario() -> list[str]:
        chunks: list[str] = []
        agen = agent_router._frames(request, run_id)
        async for chunk in agen:
            chunks.append(chunk)
            if '"type": "snapshot"' in chunk:
                break
        # uvicorn 在客户端断开时做的就是这个
        await agen.aclose()
        return chunks

    _open_run(run_id, request)
    return asyncio.run(scenario())


def test_cancel_mid_run_still_writes_the_draft():
    """**这就是"关掉页面那 40 秒别白花"**：树已经生成完，用户关了页面。"""
    provider = ScriptedProvider([
        _completion([ToolCall(call_id="c1", name="echo", arguments={"text": "生成"})],
                    finish="tool_calls"),
        _HANG,
    ])

    def generate(_args, ctx):
        ctx.candidate = CANDIDATE.model_copy(deep=True)
        return ToolResult(ok=True, content="已生成")

    async def fake_chat(system, user, **kwargs):
        return TREE

    request = AgentRunRequest(goal="做一张导图", intent="create")
    with patch("app.agent.router.run", new=_routed_with(provider, _registry(generate))):
        _cancel_after_first_snapshot("cancel-1", request)

    row = runs.get("cancel-1")
    assert row.status == runs.STATUS_CANCELLED
    assert row.candidate is not None
    assert row.candidate.root.label == "主题"
    assert row.failure is None


def test_cancelled_edit_run_keeps_the_tree_that_only_the_context_knows():
    """`edit` 的改动只走 op 批次，**没有任何一帧带着整棵树**。

    所以草稿只能来自 `ctx.candidate`：客户端手里那堆增量拼不回整树。这条用例
    专门盯这一点——把 `on_close` 拿掉，它必须红。
    """
    snapshot = MindMapContent.model_validate(TREE)
    provider = ScriptedProvider([
        _completion([ToolCall(call_id="c1", name="echo", arguments={"text": "改"})],
                    finish="tool_calls"),
    ])

    def edit(_args, ctx):
        assert ctx.candidate is not None
        grown = ctx.candidate.model_copy(deep=True)
        grown.root.children.append(type(grown.root)(id="new", label="新加的一支", children=[]))
        ctx.candidate = grown
        return ToolResult(ok=True, content="已改")

    request = AgentRunRequest(goal="改一下", intent="edit", snapshot=snapshot)

    async def scenario() -> None:
        agen = agent_router._frames(request, "edit-cancel")
        async for chunk in agen:
            if '"type": "tool_end"' in chunk:
                break
        await agen.aclose()

    _open_run("edit-cancel", request)
    with patch("app.agent.router.run", new=_routed_with(provider, _registry(edit))):
        asyncio.run(scenario())

    row = runs.get("edit-cancel")
    assert row.status == runs.STATUS_CANCELLED
    assert row.candidate is not None
    assert [child.id for child in row.candidate.root.children] == ["a", "b", "c", "new"]


def test_cancelled_error_also_lands_as_cancelled():
    """任务被 `cancel()`（不是 aclose）时同样要落库——两条路都是"用户走了"。"""
    provider = ScriptedProvider([_HANG])
    request = AgentRunRequest(goal="做一张导图", intent="create")

    async def scenario() -> None:
        agen = agent_router._frames(request, "task-cancel")
        task = asyncio.ensure_future(agen.__anext__())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await agen.aclose()

    _open_run("task-cancel", request)
    with patch("app.agent.router.run", new=_routed_with(provider, _registry(
            lambda _args, ctx: ToolResult(ok=True, content="ok")))):
        asyncio.run(scenario())

    row = runs.get("task-cancel")
    assert row.status == runs.STATUS_CANCELLED


# ---------------------------------------------------------------- 读路径


def test_list_endpoint_hides_the_candidate_body_and_the_applied_ones():
    runs.begin("done-1", {"goal": "上次那张图", "intent": "create"})
    runs.finish("done-1", runs.STATUS_DONE, candidate=CANDIDATE)
    runs.begin("done-2", {"goal": "已经应用过的", "intent": "create"})
    runs.finish("done-2", runs.STATUS_DONE, candidate=CANDIDATE)
    runs.mark_applied("done-2")

    response = client.get("/api/agent/runs", params={"status": "done", "unapplied": "1"})

    assert response.status_code == 200
    body = response.json()
    assert [item["run_id"] for item in body] == ["done-1"]
    assert body[0]["goal"] == "上次那张图"
    # 列表不带候选正文：那是一整棵树，20 条一起下发只为显示一句提示。
    assert "candidate" not in body[0]


def test_list_endpoint_includes_cancelled_drafts_by_default():
    """默认把"被取消但有草稿"也算上——否则那 40 秒只是从内存白花变成入库白花。"""
    runs.begin("done-1", {"goal": "完成了", "intent": "create"})
    runs.finish("done-1", runs.STATUS_DONE, candidate=CANDIDATE)
    runs.begin("cancelled-1", {"goal": "被中断了", "intent": "create"})
    runs.finish("cancelled-1", runs.STATUS_CANCELLED, candidate=CANDIDATE)

    body = client.get("/api/agent/runs").json()

    assert {item["status"] for item in body} == {"done", "cancelled"}


def test_detail_endpoint_returns_the_tree_and_the_failure_reason():
    runs.begin("done-1", {"goal": "上次那张图", "intent": "expand"})
    runs.finish("done-1", runs.STATUS_DONE, candidate=CANDIDATE, mode="model")

    body = client.get("/api/agent/runs/done-1").json()

    assert body["candidate"]["root"]["label"] == "主题"
    assert body["intent"] == "expand"
    assert body["mode"] == "model"
    assert client.get("/api/agent/runs/nope").status_code == 404


def test_applied_and_dismissed_endpoints_are_idempotent_and_404_for_unknown():
    runs.begin("r1", {})
    runs.finish("r1", runs.STATUS_DONE, candidate=CANDIDATE)

    assert client.post("/api/agent/runs/r1/applied").status_code == 204
    assert client.post("/api/agent/runs/r1/applied").status_code == 204
    assert runs.get("r1").applied_at is not None
    assert client.get("/api/agent/runs").json() == []

    runs.begin("r2", {})
    runs.finish("r2", runs.STATUS_DONE, candidate=CANDIDATE)
    assert client.post("/api/agent/runs/r2/dismissed").status_code == 204
    assert runs.get("r2").dismissed_at is not None

    assert client.post("/api/agent/runs/nope/applied").status_code == 404
    assert client.post("/api/agent/runs/nope/dismissed").status_code == 404


if __name__ == "__main__":
    unittest.main()
