"""三层接线的三条硬性质。

这三条不是"多跑一条断言"，每一条都对应一个能静默失常的地方：

  1. 画像拼在 **user 末尾**（不是 system、不是 user 开头）；
  2. 直到序号为空串时，user 那条消息与没有这个功能时**逐字节相同**；
  3. 画像加权只改顺序，开关关着时 `_rag` 的 `data` 键集一个字都不多。
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass


from app.agent import service, tools
from app.agent.contracts import AgentRunRequest
from app.prefs import profile, style


def _request() -> AgentRunRequest:
    return AgentRunRequest(goal="整理机器学习的主要方向", intent="create")


# ---------------------------------------------------------------- 第一层 → user


def test_style_text_is_empty_without_profile():
    """临时库里没有 `style_profile` 行时，画像段是空串（而不是"暂无画像"）。"""
    assert service._style_text() == ""


def test_style_text_respects_switch(monkeypatch):
    monkeypatch.setenv(style.PROMPT_SWITCH_ENV, "0")
    monkeypatch.setattr(style, "prompt_block", lambda *a, **k: "形状")
    assert service._style_text() == ""
    monkeypatch.setenv(style.PROMPT_SWITCH_ENV, "1")
    assert service._style_text() == "形状"


def test_style_text_swallows_errors(monkeypatch):
    """画像读不出来只降级，不该让一次生成失败。"""
    def boom(*_args, **_kwargs):
        raise RuntimeError("库锁住了")

    monkeypatch.setattr(style, "prompt_block", boom)
    assert service._style_text() == ""


def test_initial_messages_put_style_at_the_end_of_user():
    """段落落在 user **末尾**：开头是用户这次的原话，它权重最高。"""
    block = style.STYLE_HEADER + "\n  一级分支 4–6 条\n" + style.SHAPE_RULE
    messages = service._initial_messages(_request(), "", block)
    user = messages[1]["content"]
    assert user.startswith("整理机器学习的主要方向")
    assert user.endswith(style.SHAPE_RULE)
    # system 一个字都不该被污染
    assert style.STYLE_HEADER not in messages[0]["content"]


def test_initial_messages_are_byte_identical_without_style():
    """没有画像时与加这个功能之前**逐字节相同**（既有 2162 条用例的前提）。"""
    messages = service._initial_messages(_request(), "", "")
    assert messages[1] == {"role": "user", "content": "整理机器学习的主要方向"}


# ---------------------------------------------------------------- 第二层 → 排序


@dataclass
class _Hit:
    score: float
    quote: str = "一段原文"
    source_id: str = "s1"
    start: int = 0
    method: str = "lexical"


def test_profile_rank_is_a_no_op_when_switch_is_off(monkeypatch):
    monkeypatch.delenv(profile.SWITCH_ENV, raising=False)
    hits = [_Hit(0.08), _Hit(0.02)]
    ranked, lifted, note = asyncio.run(tools._profile_rank(hits, []))
    assert ranked == hits and lifted == [] and note == ""


def test_profile_rank_returns_original_order_on_failure(monkeypatch):
    """开关开着、加权炸了：按原顺序交付，不抛。"""
    monkeypatch.setenv(profile.SWITCH_ENV, "1")

    def boom(*_args, **_kwargs):
        raise RuntimeError("向量服务挂了")

    monkeypatch.setattr(profile, "boost", boom)
    hits = [_Hit(0.08), _Hit(0.02)]
    ranked, lifted, _note = asyncio.run(tools._profile_rank(hits, []))
    assert ranked == hits and lifted == []


def test_profile_rank_reports_lifted_keys(monkeypatch):
    """抬上来的命中报 `source_id:start`，**不带原文**（引文是材料正文，不进日志）。"""
    monkeypatch.setenv(profile.SWITCH_ENV, "1")
    hits = [_Hit(0.08, source_id="s1", start=0), _Hit(0.075, source_id="s2", start=120)]

    def fake_boost(items, **_kwargs):
        return profile.Boost(hits=[items[1], items[0]], lifted=[items[1]],
                             cosines={0: 0.1, 1: 0.9})

    monkeypatch.setattr(profile, "boost", fake_boost)
    ranked, lifted, _note = asyncio.run(tools._profile_rank(hits, []))
    assert ranked == [hits[1], hits[0]]
    assert lifted == ["s2:120"]


def test_hit_key_never_contains_the_quote():
    assert tools._hit_key(_Hit(0.1, quote="机密原文", source_id="src", start=42)) == "src:42"


def test_profile_db_has_no_row_until_refresh():
    """两张新表在 `repository.connect()` 里建好，但**不预填**——第一层要人工触发
    一次 `refresh()`（脚本或面板），否则每次生成都会静默地"有一张空画像表"。"""
    from contextlib import closing

    from app.mindmaps import repository

    with closing(repository.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) AS total FROM style_profile").fetchone()["total"] == 0
        assert conn.execute("SELECT COUNT(*) AS total FROM profile_vectors").fetchone()["total"] == 0


def test_style_profile_is_not_written_into_runs_request():
    """画像不进 `runs.request`：请求体是"用户要什么"，画像是"系统猜什么"。

    这条钉住的是"不重演 `note.*` 与 `user_prefs` 那个老问题"——一旦画像混进请求体，
    `prefs_applied` 那一列的语义（"用户设的这条真的被用了吗"）就被稀释了。
    """
    block = style.STYLE_HEADER + "\n" + style.SHAPE_RULE
    messages = service._initial_messages(_request(), "", block)
    assert json.dumps(messages, ensure_ascii=False).count(style.STYLE_HEADER) == 1
    payload = json.dumps(_request().model_dump(), ensure_ascii=False)
    assert style.STYLE_HEADER not in payload
    assert style.SHAPE_RULE not in payload
