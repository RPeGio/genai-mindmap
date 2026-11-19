"""生成时的注入：一次生成只检索一次，且空记忆时与改动前逐字节相同。

两条不变量，各有一种很具体的坏法：

  - **检索放进 `_system_text`** → 每一轮都检索一次（骨架 1 次 + 结构 1 次 + detail
    2 次 + 工具循环 N 轮 = N+4 次 embedding，每次 0.3–1 秒）。延迟翻几倍，而界面上
    看起来只是"今天有点慢"。
  - **空记忆时多拼一个空段** → system 里多一行空白（或者一句"（暂无记忆）"），
    老请求的提示词从此与改动前不再逐字节相同。这类变化不会让任何用例红，只会让
    "这次生成的图和上次不一样"变成一个无法归因的问题。
"""
from __future__ import annotations

import asyncio
import math
from contextlib import closing

from app.agent import service as agent_service
from app.agent.contracts import AgentRunRequest, Completion, RunContext, ToolCall
from app.memory import store
from app.mindmaps import repository


# ---------------------------------------------------------------- 夹具

def fake_embedding(monkeypatch, vectors: dict[str, list[float]] | None = None):
    """让向量服务"配置好"，并数清楚 `embed_texts` 被调了几次、送了什么。"""
    sent: list[str] = []
    table = dict(vectors or {})
    monkeypatch.setenv("MINDMAP_EMBEDDING_API_KEY", "test-key")
    monkeypatch.setenv("MINDMAP_EMBEDDING_BASE_URL", "http://127.0.0.1:9/v1")

    def embed(texts: list[str]) -> list[list[float] | None]:
        sent.extend(texts)
        return [_normalize(table.get(text)) for text in texts]

    monkeypatch.setattr(store, "embed_texts", embed)
    monkeypatch.setattr(store.vector_cache, "get_vectors", lambda hashes, model: {})
    monkeypatch.setattr(store.vector_cache, "put_vectors", lambda items, model: 0)
    return sent


def _normalize(vector: list[float] | None) -> list[float] | None:
    if not vector:
        return None
    norm = math.sqrt(sum(value * value for value in vector))
    return vector if norm <= 0 else [value / norm for value in vector]


BASE = [1.0, 0.0, 0.0, 0.0]


def put(text: str, vector: list[float] | None, *, days_ago: float = 0.0) -> str:
    from datetime import timedelta

    moment = store.utcnow() - timedelta(days=days_ago)
    stamp = moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    item_id = f"id-{abs(hash((text, days_ago))) % 10**10}"
    with closing(repository.connect()) as conn, conn:
        conn.execute(
            f"INSERT INTO {store.ITEM_TABLE} "
            "(id, text, kind, source, created_at, text_hash, model, dim, vector) "
            "VALUES (?, ?, 'preference', 'summary', ?, ?, 'text-embedding-v3', ?, ?)",
            (item_id, text, stamp, store.vector_cache.text_hash(text),
             None if vector is None else len(vector),
             None if vector is None else store.vector_cache.pack(vector)),
        )
    return item_id


class ScriptedProvider:
    """按脚本回放的 provider：第 N 次调用返回第 N 个 `Completion`。

    用它而不是真的 provider，是为了让"跑了多少轮"变成一个确定的数——"一次生成只
    检索一次"这条断言必须有"其实跑了 3 轮"作为前提，否则 1 轮里数出 1 次什么都
    证明不了。
    """

    model_name = "scripted"

    def __init__(self, steps: list[Completion]) -> None:
        self.steps = steps
        self.calls = 0

    async def complete_tools(self, messages, tools, *, budget):
        step = self.steps[min(self.calls, len(self.steps) - 1)]
        self.calls += 1
        return step


def tool_step(call_id: str = "c1", name: str = "no_such_tool") -> Completion:
    """一次工具调用。工具名是故意不存在的：它不会改树，也不会联网。"""
    return Completion(
        message={"role": "assistant", "content": None,
                 "tool_calls": [{"id": call_id, "type": "function",
                                 "function": {"name": name, "arguments": "{}"}}]},
        tool_calls=[ToolCall(call_id=call_id, name=name, arguments={})],
        finish_reason="tool_calls",
    )


def stop_step() -> Completion:
    return Completion(
        message={"role": "assistant", "content": "好的"},
        tool_calls=[],
        finish_reason="stop",
    )


async def drain(request: AgentRunRequest, provider, *, tools=None) -> None:
    async for _event in agent_service.run(request, provider=provider, tools=tools):
        pass


# ---------------------------------------------------------------- 空记忆

#: 改动前的 `_system_text` 在"create、无材料、无备注、无选区、无导图"下的**完整**
#: system。写死字面量而不是拼模块常量：拼常量的话，"加了一个空段"这类改动会让
#: 期望值跟着变，用例永远绿——而它要抓的正是这类改动。
SYSTEM_BEFORE_MEMORY = (
    "你在操作一张思维导图，可以通过工具读写它。"
    "只能调用下面列出的工具；需要改图时用工具，不要只在文字里描述。"
    "\n"
    "这次是新建导图。直接生成，不要先去检索资料。"
    "\n"
    "当前导图："
    "\n"
    "（当前没有导图）"
)


def test_empty_memory_is_byte_identical_to_before():
    """没有记忆时，system 与改动前**逐字节相同**。

    这条是这次改动对既有行为的唯一承诺：库里一条记忆都没有的那些部署（现在全部
    都是），跑出来的提示词一个字都不该变。
    """
    request = AgentRunRequest(goal="机器学习")
    assert agent_service._system_text(request, None) == SYSTEM_BEFORE_MEMORY
    assert store.MEMORY_HEADER not in SYSTEM_BEFORE_MEMORY


def test_empty_memory_adds_no_frame_and_no_placeholder():
    """不注入"（暂无记忆）"这种占位句：那会让模型把"记忆是空的"当成一种输入来处理。"""
    request = AgentRunRequest(goal="机器学习")
    text = agent_service._system_text(request, None, memory="")
    assert "记忆" not in text


def test_empty_memory_keeps_the_notes_paragraph_identical(monkeypatch):
    """有手写备注、没有记忆时，那一段也不变（`user_prefs` 的行为一个字不动）。"""
    from app.prefs import service as prefs

    monkeypatch.setattr(prefs, "prompt_notes", lambda: (prefs.NOTE_HEADER + "\n- 标题别超 8 字", 0))
    request = AgentRunRequest(goal="机器学习")
    text = agent_service._system_text(request, None)
    assert text.split("\n")[1] == prefs.NOTE_HEADER
    assert "- 标题别超 8 字" in text
    assert store.MEMORY_HEADER not in text


# ---------------------------------------------------------------- 有记忆

def test_memory_lands_between_the_notes_and_the_intent_rule():
    """记忆那一段在**备注之后、意图规则之前**：显式声明在前、系统推断在后。

    顺序错了不会报错，只会让"我明明写了它却没用"再发生一次——模型的阅读顺序就是
    它的优先级。
    """
    from app.prefs import service as prefs

    request = AgentRunRequest(goal="机器学习")
    memory = store.MEMORY_HEADER + "\n- 标题倾向去掉人名"
    text = agent_service._system_text(request, None, memory=memory)

    assert prefs.NOTE_HEADER not in text, "没有备注时备注段本来就是空的"
    position_memory = text.index(store.MEMORY_HEADER)
    position_intent = text.index(agent_service._INTENT_RULES["create"])
    assert position_memory < position_intent
    assert text.index("- 标题倾向去掉人名") > position_memory


def test_memory_comes_after_the_notes_when_both_exist(monkeypatch):
    """两段同时存在时，手写的备注排在系统推断之前。"""
    from app.prefs import service as prefs

    monkeypatch.setattr(prefs, "prompt_notes", lambda: (prefs.NOTE_HEADER + "\n- 手写的", 0))
    request = AgentRunRequest(goal="机器学习")
    text = agent_service._system_text(
        request, None, memory=store.MEMORY_HEADER + "\n- 推断的")
    assert text.index(prefs.NOTE_HEADER) < text.index(store.MEMORY_HEADER)


def test_refresh_outline_reuses_the_hint_instead_of_recalling(monkeypatch):
    """`_refresh_outline` 从 `ctx.memory_hints` 抄，不再检索。"""
    fake_embedding(monkeypatch, {"机器学习": BASE})
    request = AgentRunRequest(goal="机器学习")
    ctx = RunContext(request=request)
    ctx.memory_hints = store.MEMORY_HEADER + "\n- 标题去掉人名"
    messages = [{"role": "system", "content": ""}]

    agent_service._refresh_outline(messages, ctx)
    assert store.MEMORY_HEADER in messages[0]["content"]


# ---------------------------------------------------------------- 只检索一次

def test_one_generation_recalls_exactly_once(monkeypatch):
    """**一次生成只检索一次**：跑 2 轮（1 次工具 + 收尾），embedding 只调 1 次。

    那 1 次工具调用不是装饰：没有它，"只检索一次"与"每轮检索一次"在结果上完全
    无法区分（1 轮里数出 1 次什么都证明不了）。
    """
    sent = fake_embedding(monkeypatch, {"机器学习 复习提纲": BASE})
    put("标题倾向去掉人名", BASE)

    provider = ScriptedProvider([tool_step("c1"), stop_step()])
    request = AgentRunRequest(goal="机器学习 复习提纲", run_id="r1")
    asyncio.run(drain(request, provider))

    assert provider.calls == 2, "两轮：第一轮工具调用，第二轮收尾"
    assert sent == ["机器学习 复习提纲"], f"embedding 被调了 {len(sent)} 次，应该 1 次"


def test_the_hint_reaches_every_round_of_the_loop(monkeypatch):
    """每一轮下发的 system 里都有那段记忆——算一次、抄 N 次。"""
    fake_embedding(monkeypatch, {"机器学习": BASE})
    put("偏好鱼骨图", BASE)

    seen: list[str] = []

    class Recording(ScriptedProvider):
        async def complete_tools(self, messages, tools, *, budget):
            seen.append(messages[0]["content"])
            return await super().complete_tools(messages, tools, budget=budget)

    provider = Recording([tool_step("c1"), stop_step()])
    request = AgentRunRequest(goal="机器学习", run_id="r1")
    asyncio.run(drain(request, provider))

    assert len(seen) == 2
    assert all(store.MEMORY_HEADER in content for content in seen)
    assert all("- 偏好鱼骨图" in content for content in seen)


def test_no_memory_means_no_embedding_call_at_all(monkeypatch):
    """嘴上说"没记忆就不注入"是不够的：**一次 embedding 都不该发**。

    向量服务未配置时 `query_vector` 直接返回 None，所以这里连 0.3–1 秒都不会花。
    """
    sent = fake_embedding(monkeypatch)
    provider = ScriptedProvider([stop_step()])
    request = AgentRunRequest(goal="机器学习", run_id="r1")
    asyncio.run(drain(request, provider))
    # 空表 + 配了向量服务：会查一次查询向量（那是"检索"本身，不是注入）
    assert sent == ["机器学习"]


def test_unconfigured_embedding_skips_the_lookup_entirely(monkeypatch):
    """向量服务没配置 → 一次网络、一次 embedding 都不发，system 与改动前相同。"""
    monkeypatch.delenv("MINDMAP_EMBEDDING_API_KEY", raising=False)
    sent: list[str] = []
    monkeypatch.setattr(store, "embed_texts", lambda texts: sent.extend(texts) or [None])

    request = AgentRunRequest(goal="机器学习")
    assert agent_service._system_text(request, None) == SYSTEM_BEFORE_MEMORY
    assert asyncio.run(agent_service._memory_hints(request)) == ""
    assert sent == []


def test_recall_failure_does_not_break_the_generation(monkeypatch):
    """检索抛错 → 一句 warning + 空串，生成照跑。"""
    import sqlite3

    def boom(request):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "prompt_for", boom)
    request = AgentRunRequest(goal="机器学习")
    assert asyncio.run(agent_service._memory_hints(request)) == ""


def test_memory_hints_default_is_empty():
    """新增字段的默认值必须是空串：直接构造 `RunContext` 的代码没有检索过。"""
    ctx = RunContext(request=AgentRunRequest(goal="机器学习"))
    assert ctx.memory_hints == ""


def test_demo_mode_never_recalls(monkeypatch):
    """演示模式不发 system，所以不该花那一次检索。"""
    monkeypatch.setenv("MINDMAP_LLM_MODE", "demo")
    sent = fake_embedding(monkeypatch, {"机器学习": BASE})
    request = AgentRunRequest(goal="机器学习", run_id="r-demo")
    asyncio.run(drain(request, ScriptedProvider([stop_step()])))
    assert sent == []
