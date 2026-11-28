"""免费档：动态发现、不阻塞、失败留痕、每模型档位。

这一层钉六件事，每件都对应一种"看起来能用、实际不能用"的形状：

1. **选谁**（`pick_free`）：两个目录形状各按它自己的字段判——Command Code 是
   `:free` 后缀 + `supported_endpoints` 里有 `/chat/completions`；OpenCode（实测
   `https://opencode.ai/inference/openai/v1/models` 带 key 返回 200）是
   `type=="chat"` + `cost.text` 两个 price 都是 0。字段缺失按"不支持"处理：
   宁可少一个候选，也不要运行时才发现调不通。
2. **档位跟着模型走**（`FreeModel.effort`）：OpenCode 目录的
   `capabilities.reasoning` 说的是"这一个模型"能不能关思考（`none` 在 effort
   列表里、或 `toggle` 为真）——没给档位信息的模型**不发那个键**。
3. **不阻塞**：拉一次列表要 20 秒以上（Command Code 本机实测 22.1s / 21.7s），
   所以第一次调用返回空、同时起后台线程；过期的旧列表继续用。
4. **失败留痕**：拉不到列表时保留旧列表并记 warning；拉到但一个免费模型都没有
   也要记 warning。两者都不许静默。
5. **额度与候选**：OpenCode 免费档用完是**账号级每日桶**（429 + 正文
   `FreeUsageLimitError` + `retry-after` 到明天）——换候选没用，必须单独一档
   异常；上游偶发 429（Command Code "Upstream model provider is temporarily
   unavailable"）才是"换一个免费模型往往就通"。
6. **接线**：`max_tokens` 在免费档同时有**下限**（1400 时正文会被 reasoning
   吃光）与**上限**（免费模型按额度大小决定思考多久，不夹上限时整条链 549.9 秒、
   夹住 359.6 秒）——两个方向都不看"有没有 effort"，看的是"是不是免费档"。

时间在测试里不睡：缓存条目直接改时间戳来"让时间过去"。
"""
from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace

import httpx
import pytest

from app.agent import provider as provider_module
from app.generation import free_models
from app.generation import service as generation_service
from app.generation.service import (
    FreeQuotaExhausted,
    _ModelUnavailable,
    _quota_exhausted_error,
    chat_async,
)
from app.settings import store

BASE = "https://api.commandcode.ai/provider/v1"
KEY = "cmd-key-1234"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """每个用例从"开关关、缓存空"开始，退出时也清掉缓存。

    缓存是模块级的：不清的话前一个用例塞进去的假列表会被后一个用例当成真的，
    而那种失败会表现得像"重试有时通过"。
    """
    monkeypatch.delenv(free_models.ENABLED_ENV, raising=False)
    free_models.reset()
    yield
    free_models.reset()


def _model(model_id: str, *, context: int | None = None,
           endpoints: list[str] | None = None) -> dict:
    """Command Code 形状的最小条目。"""
    payload = {"id": model_id, "object": "model"}
    if context is not None:
        payload["context_length"] = context
    if endpoints is not None:
        payload["supported_endpoints"] = endpoints
    return payload


def _opencode(model_id: str, *, context: int | None = None, effort=None,
              toggle: bool = False, input_price: float = 0.0,
              output_price: float = 0.0, rtype: str = "chat",
              recommended: str | None = "chat-completions") -> dict:
    """OpenCode 形状的最小条目（按实测目录的骨架，见 docs/FREE-MODELS.md）。"""
    payload: dict = {
        "id": model_id, "type": rtype,
        "cost": {"text": {"input": {"price": input_price},
                          "output": {"price": output_price}}},
    }
    if recommended is not None:
        payload["recommended_api"] = recommended
    if context is not None:
        payload["limit"] = {"context": context}
    reasoning: dict = {}
    if effort is not None:
        reasoning["effort"] = effort
    if toggle:
        reasoning["toggle"] = True
    if reasoning:
        payload["capabilities"] = {"reasoning": reasoning}
    return payload


def _put_cache(ids: list[str], *, age: float = 0.0) -> str:
    """直接往缓存里写一条（Command Code 形状：档位按 FALLBACK_EFFORT 兜），返回键。"""
    key = BASE.rstrip("/")
    records = [free_models.FreeModel(model_id, free_models.FALLBACK_EFFORT, 0)
               for model_id in ids]
    with free_models._LOCK:
        free_models._CACHE[key] = (time.monotonic() - age, records)
    return key


def _put_records(records: list[free_models.FreeModel], *, age: float = 0.0) -> str:
    """OpenCode 形状的缓存直写：档位跟着每条记录走。"""
    key = BASE.rstrip("/")
    with free_models._LOCK:
        free_models._CACHE[key] = (time.monotonic() - age, records)
    return key


# ---------------------------------------------------------------- 1. 选谁


def test_only_the_free_suffix_qualifies():
    picked = free_models.pick_free([
        _model("recursive/flint-v1.0:free", context=262000,
               endpoints=["/chat/completions"]),
        _model("deepseek/deepseek-v4-flash", context=128000,
               endpoints=["/chat/completions"]),
    ])
    assert picked == [free_models.FreeModel(
        "recursive/flint-v1.0:free", free_models.FALLBACK_EFFORT, 262000)]


def test_a_name_that_merely_contains_free_is_not_free():
    """用 `endswith` 而不是 `in`：`x:free-trial` 可能是要计费的档位。"""
    picked = free_models.pick_free([
        _model("vendor/model:free-trial", endpoints=["/chat/completions"]),
    ])
    assert picked == []


def test_a_free_model_without_the_chat_endpoint_is_skipped():
    picked = free_models.pick_free([
        _model("vendor/only-messages:free", endpoints=["/messages"]),
        _model("vendor/talks:free", endpoints=["/chat/completions", "/responses"]),
    ])
    assert picked == [free_models.FreeModel("vendor/talks:free", free_models.FALLBACK_EFFORT, 0)]


def test_a_missing_endpoint_list_counts_as_unsupported():
    """字段缺失时宁可少一个候选，也不要运行时才发现调不通。"""
    assert free_models.pick_free([_model("vendor/x:free")]) == []


def test_the_biggest_context_comes_first_then_id_order():
    """上下文大的优先；同大按 id 升序——没有第二条，同一天里会换着用不同模型。"""
    picked = free_models.pick_free([
        _model("b/model:free", context=1000, endpoints=["/chat/completions"]),
        _model("a/model:free", context=1000, endpoints=["/chat/completions"]),
        _model("c/model:free", context=9000, endpoints=["/chat/completions"]),
    ])
    assert [record.id for record in picked] == ["c/model:free", "a/model:free", "b/model:free"]


def test_a_model_without_a_context_length_sorts_last():
    picked = free_models.pick_free([
        _model("no-size/model:free", endpoints=["/chat/completions"]),
        _model("sized/model:free", context=4096, endpoints=["/chat/completions"]),
    ])
    assert [record.id for record in picked] == ["sized/model:free", "no-size/model:free"]


def test_garbage_entries_are_ignored_not_fatal():
    picked = free_models.pick_free([
        None,
        "recursive/flint-v1.0:free",
        {"id": 7},
        _model("good/model:free", endpoints=["/chat/completions"]),
    ])
    assert picked == [free_models.FreeModel("good/model:free", free_models.FALLBACK_EFFORT, 0)]


# --------------------------------------------------- 2. OpenCode 的形状与档位


def test_an_opencode_model_with_zero_cost_is_free():
    """OpenCode 判免费不靠后缀（`big-pickle` 连 `-free` 都没有），靠 cost=0。"""
    picked = free_models.pick_free([
        _opencode("big-pickle", context=200000, input_price=0.0, output_price=0.0),
        _opencode("paid/one", context=1000000, input_price=0.05, output_price=0.4),
    ])
    # 没给档位信息 → effort=None（不发键）。
    assert picked == [free_models.FreeModel("big-pickle", None, 200000)]


def test_an_opencode_model_that_advertises_none_sends_none():
    """effort 列表里直接有 `none`（实测 nemotron-super / hy4 就是这么写的）。"""
    picked = free_models.pick_free([
        _opencode("nemotron-3.5-super-vl-free", context=1048576,
                  effort=["none", "minimal", "low", "medium", "high", "xhigh"]),
    ])
    assert picked == [free_models.FreeModel(
        "nemotron-3.5-super-vl-free", "none", 1048576,
        ("none", "minimal", "low", "medium", "high", "xhigh"))]


def test_toggle_counts_as_none():
    """`toggle: true` = 目录答应"可以加一个关思考的档"（实测 deepseek-v4.1-flash-free）。"""
    picked = free_models.pick_free([
        _opencode("deepseek-v4.1-flash-free", context=200000,
                  effort=["low", "high", "max"], toggle=True),
    ])
    assert picked == [free_models.FreeModel(
        "deepseek-v4.1-flash-free", "none", 200000, ("none", "low", "high", "max"))]


def test_effort_without_none_takes_the_cheapest_rank():
    """没有 `none` 就取列表里最省的一档——免费模型把 token 烧在思考上是实测过的
    失败形状，"最省"正是冲着它去的。"""
    picked = free_models.pick_free([
        _opencode("beam-free", context=1048576,
                  effort=["low", "medium", "high", "xhigh", "max"]),
        _opencode("hy4-flash-free", context=1048576, effort=["none", "high"]),
    ])
    assert [record.effort for record in picked] == ["low", "none"]


def test_an_opencode_model_without_effort_info_sends_nothing():
    """实测目录上 big-pickle / iceman / lightning / mimo 没有 capabilities.reasoning：
    它们的请求里不该出现 `reasoning_effort`，发一个它没声明的档位才是真的 400。"""
    picked = free_models.pick_free([
        _opencode("iceman-free", context=262144),
    ])
    assert picked == [free_models.FreeModel("iceman-free", None, 262144)]


def test_an_opencode_entry_with_another_recommended_api_is_skipped():
    """`recommended_api` 存在时必须认：只挂 `/messages` 的条目调 `/chat/completions`
    就是 400。字段**不存在**时放行（实测目录上大量条目不带这个字段）。"""
    assert free_models.pick_free([
        _opencode("a/free", recommended="messages"),
        _opencode("b/free", recommended=None),
        _opencode("c/free"),
    ]) == [
        free_models.FreeModel("b/free", None, 0),
        free_models.FreeModel("c/free", None, 0),
    ]


def test_a_non_chat_opencode_entry_is_skipped():
    assert free_models.pick_free([_opencode("img/one", rtype="image")]) == []


def test_missing_price_fields_count_as_unsupported():
    assert free_models.pick_free([
        {"id": "x", "type": "chat"},                      # 连 cost 都没有
        {"id": "y", "type": "chat", "cost": {}},          # cost 里没有 text
        {"id": "z", "type": "chat",
         "cost": {"text": {"input": {"price": 0}}}},      # 只有 input
    ]) == []


def test_opencode_ordering_uses_limit_context():
    picked = free_models.pick_free([
        _opencode("small/one", context=262144, effort=["none"]),
        _opencode("big/one", context=1048576, effort=["none"]),
    ])
    assert [record.id for record in picked] == ["big/one", "small/one"]


def test_effort_options_carry_the_full_catalog_list():
    """`effort_options` 是目录声明的**全部**档位（面板下拉的选项），不是只有
    "最省"那一档说了算——用户挑哪档就该发哪档。顺序保持目录给的。"""
    picked = free_models.pick_free([
        _opencode("beam-free", context=1048576,
                  effort=["low", "medium", "high", "xhigh", "max"]),
    ])
    assert picked[0].effort_options == ("low", "medium", "high", "xhigh", "max")


def test_toggle_prepends_none_to_the_options():
    picked = free_models.pick_free([
        _opencode("deepseek-v4.1-flash-free", context=200000,
                  effort=["low", "high", "max"], toggle=True),
    ])
    assert picked[0].effort_options == ("none", "low", "high", "max")


def test_commandcode_records_have_no_options_and_the_directory_is_the_truth():
    """Command Code 目录不带 capabilities：档位未知，选项就是空的——
    面板据此不显示下拉，而不是编一排可能 400 的档位。"""
    picked = free_models.pick_free([
        _model("recursive/flint-v1.0:free", context=262000,
               endpoints=["/chat/completions"]),
    ])
    assert picked[0].effort_options == ()


def test_options_for_matches_by_id_and_answers_empty_when_unknown(monkeypatch):
    monkeypatch.setenv(free_models.ENABLED_ENV, "1")
    key = _put_records([
        free_models.FreeModel("nemotron-3.5-super-vl-free", "none", 1048576,
                              ("none", "minimal", "low", "medium", "high", "xhigh")),
        free_models.FreeModel("big-pickle", None, 200000),
    ])
    assert free_models.options_for(key, "nemotron-3.5-super-vl-free") == \
        ("none", "minimal", "low", "medium", "high", "xhigh")
    assert free_models.options_for(key, "big-pickle") == ()
    assert free_models.options_for(key, "no/such-model") == ()


def test_options_for_answers_empty_when_the_switch_is_off(monkeypatch):
    """开关关着时 `options_for` 也是空——面板不会为一段没人用的配置去拉目录。"""
    key = _put_records([
        free_models.FreeModel("nemotron-3.5-super-vl-free", "none", 1048576,
                              ("none", "minimal", "low")),
    ])
    assert free_models.options_for(key, "nemotron-3.5-super-vl-free") == ()


# ---------------------------------------------------------------- 3. 不阻塞


def test_the_switch_defaults_to_off(monkeypatch):
    """默认关：打开它等于改变"这一次生成花谁的钱"。"""
    assert free_models.enabled() is False
    _put_cache(["recursive/flint-v1.0:free"])
    assert free_models.free_models(BASE) == []
    monkeypatch.setenv(free_models.ENABLED_ENV, "1")
    assert free_models.enabled() is True
    assert free_models.free_models(BASE) == [
        free_models.FreeModel("recursive/flint-v1.0:free", free_models.FALLBACK_EFFORT, 0)]


def test_the_first_call_returns_empty_and_refreshes_in_the_background(monkeypatch):
    monkeypatch.setenv(free_models.ENABLED_ENV, "1")
    started: list[str] = []

    def fake_start(base: str) -> None:
        started.append(base)

    monkeypatch.setattr(free_models, "_start_refresh", fake_start)
    assert free_models.free_models(BASE) == []
    assert started == [BASE.rstrip("/")]


def test_a_fresh_cache_is_used_without_a_refresh(monkeypatch):
    monkeypatch.setenv(free_models.ENABLED_ENV, "1")
    _put_cache(["recursive/flint-v1.0:free"], age=60)

    def boom(base: str) -> None:
        raise AssertionError("缓存还新鲜，不该去刷新")

    monkeypatch.setattr(free_models, "_start_refresh", boom)
    assert [record.id for record in free_models.free_models(BASE)] == [
        "recursive/flint-v1.0:free"]


def test_a_stale_cache_still_answers_while_it_refreshes(monkeypatch):
    """过期时返回旧列表**同时**起线程：这一次请求不能等 22 秒。"""
    monkeypatch.setenv(free_models.ENABLED_ENV, "1")
    _put_cache(["recursive/flint-v1.0:free"], age=free_models.TTL_SECONDS + 5)
    started: list[str] = []
    monkeypatch.setattr(free_models, "_start_refresh", started.append)

    assert [record.id for record in free_models.free_models(BASE)] == [
        "recursive/flint-v1.0:free"]
    assert started == [BASE.rstrip("/")]


def test_the_returned_list_is_a_copy(monkeypatch):
    """调用方改了返回值不能污染缓存（`candidates` 会做过滤，容易顺手改）。"""
    monkeypatch.setenv(free_models.ENABLED_ENV, "1")
    _put_cache(["a/model:free"])
    got = free_models.free_models(BASE)
    got.append(free_models.FreeModel("bogus", None, 0))
    assert [record.id for record in free_models.free_models(BASE)] == ["a/model:free"]


# ---------------------------------------------------------------- 4. 失败留痕


def test_a_failed_refresh_keeps_the_old_list_and_warns(monkeypatch, caplog):
    monkeypatch.setenv(free_models.ENABLED_ENV, "1")
    _put_cache(["recursive/flint-v1.0:free"], age=free_models.TTL_SECONDS + 5)

    def boom(base: str) -> list[free_models.FreeModel]:
        raise httpx.ConnectError("连不上")

    monkeypatch.setattr(free_models, "_fetch", boom)
    with caplog.at_level(logging.WARNING):
        free_models._refresh(BASE)

    assert [record.id for record in free_models.free_models(BASE)] == [
        "recursive/flint-v1.0:free"]
    assert any("刷新模型列表失败" in record.getMessage() for record in caplog.records)
    # 失败之后 `_REFRESHING` 必须放掉，否则这一小时里再也不会重试。
    assert BASE.rstrip("/") not in free_models._REFRESHING


def test_a_failed_first_refresh_says_there_is_no_free_tier(caplog):
    def boom(base: str) -> list[free_models.FreeModel]:
        raise httpx.ConnectError("连不上")

    from app.generation import free_models as module

    original = module._fetch
    module._fetch = boom
    try:
        with caplog.at_level(logging.WARNING):
            module._refresh(BASE)
    finally:
        module._fetch = original

    assert any("拉不到模型列表" in record.getMessage() for record in caplog.records)


def test_an_empty_free_list_is_also_worth_a_warning(monkeypatch, caplog):
    """免费活动结束后列表会是空的，日志里必须留下"当前 0 个免费模型"。"""
    monkeypatch.setattr(free_models, "_fetch", lambda base: [])
    with caplog.at_level(logging.WARNING):
        free_models._refresh(BASE)
    assert free_models.free_models(BASE) == []
    assert any("没有任何" in record.getMessage() for record in caplog.records)


def test_a_successful_refresh_replaces_the_list(monkeypatch):
    monkeypatch.setenv(free_models.ENABLED_ENV, "1")
    _put_cache(["old/model:free"], age=free_models.TTL_SECONDS + 5)
    monkeypatch.setattr(
        free_models, "_fetch", lambda base: [free_models.FreeModel("new/model:free", None, 0)])
    free_models._refresh(BASE)
    assert [record.id for record in free_models.free_models(BASE)] == ["new/model:free"]


# ---------------------------------------------------------------- 5. 候选序列


def test_candidates_skip_what_this_run_already_tried(monkeypatch):
    monkeypatch.setenv(free_models.ENABLED_ENV, "1")
    _put_cache(["a/model:free", "b/model:free", "c/model:free"])
    assert [record.id for record in free_models.candidates(BASE, tried={"a/model:free"})] == [
        "b/model:free", "c/model:free"]
    assert free_models.current(BASE).id == "a/model:free"


# ---------------------------------------------------------------- 6. 接线


@pytest.fixture
def llm_env(monkeypatch):
    monkeypatch.setenv("MINDMAP_LLM_BASE_URL", BASE)
    monkeypatch.setenv("MINDMAP_LLM_API_KEY", KEY)
    monkeypatch.setenv("MINDMAP_LLM_MODEL", "vendor/paid-model")


def test_resolve_switches_to_the_free_model_when_the_switch_is_on(
        monkeypatch, llm_env):
    monkeypatch.setenv(free_models.ENABLED_ENV, "1")
    _put_cache(["recursive/flint-v1.0:free"])
    resolved = store.resolve()
    assert resolved.model == "recursive/flint-v1.0:free"
    assert resolved.model_source == store.SOURCE_FREE
    # Command Code 的目录不带档位信息，按实测出的下限兜。
    assert resolved.reasoning_effort == free_models.FALLBACK_EFFORT
    assert resolved.free_tier is True


def test_resolve_carries_each_models_own_effort(monkeypatch, llm_env):
    """OpenCode 形状：`resolve()` 填的是**首选那个模型自己的**档位。"""
    monkeypatch.setenv(free_models.ENABLED_ENV, "1")
    _put_records([free_models.FreeModel("nemotron-super-free", "none", 1048576),
                  free_models.FreeModel("beam-free", "low", 1048576)])
    resolved = store.resolve()
    assert resolved.model == "nemotron-super-free"
    assert resolved.reasoning_effort == "none"
    assert resolved.free_tier is True


def test_resolve_leaves_the_model_alone_without_the_switch(llm_env):
    _put_cache(["recursive/flint-v1.0:free"])
    resolved = store.resolve()
    assert resolved.model == "vendor/paid-model"
    assert resolved.reasoning_effort is None
    assert resolved.free_tier is False


def test_resolve_does_not_touch_a_users_own_key(monkeypatch, llm_env):
    """用户自己填了密钥就按他的意思走——免费档与他的钱无关。"""
    monkeypatch.setenv(free_models.ENABLED_ENV, "1")
    _put_cache(["recursive/flint-v1.0:free"])
    from conftest import guest_client

    # 门禁上线后 `/api/settings/*` 要身份；这条用例关心的是"自己填的密钥优先"，
    # 与"谁有资格访问设置面板"无关，所以用游客身份（它有自己那一行配置）。
    client = guest_client()
    response = client.put("/api/settings/model", json={
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "sk-mine-0000",
        "model": "deepseek-chat",
    })
    assert response.status_code == 200, response.text
    assert response.json()["model"] == "deepseek-chat"
    assert response.json()["model_source"] == store.SOURCE_SAVED


def _capture_chat(monkeypatch) -> list[dict]:
    sent: list[dict] = []

    async def fake_once(base: str, request_body: dict, headers: dict) -> dict:
        sent.append(request_body)
        return {"title": "t", "root": {"id": "r", "label": "t", "children": []}}

    monkeypatch.setattr(generation_service, "_chat_once", fake_once)
    return sent


def test_chat_async_sends_the_reasoning_effort_and_a_floor_on_max_tokens(
        monkeypatch, llm_env):
    """1400 会让免费模型把额度全花在思考上——所以免费档抬到 4000。"""
    monkeypatch.setenv(free_models.ENABLED_ENV, "1")
    _put_cache(["recursive/flint-v1.0:free"])
    sent = _capture_chat(monkeypatch)

    asyncio.run(chat_async("sys", "user", max_tokens=1400))

    assert sent[0]["model"] == "recursive/flint-v1.0:free"
    assert sent[0]["reasoning_effort"] == free_models.FALLBACK_EFFORT
    assert sent[0]["max_tokens"] >= generation_service.FREE_MIN_OUTPUT_TOKENS


def test_chat_async_omits_the_effort_key_for_a_model_without_one(
        monkeypatch, llm_env):
    """档位是 None 的免费模型**不发 `reasoning_effort` 键**，但 `max_tokens`
    两个方向的夹子都要在——夹的是"给思考的预算"，键只是"告诉它按哪档想"。"""
    monkeypatch.setenv(free_models.ENABLED_ENV, "1")
    _put_records([free_models.FreeModel("big-pickle", None, 200000)])
    sent = _capture_chat(monkeypatch)

    asyncio.run(chat_async("sys", "user", max_tokens=1400))
    asyncio.run(chat_async("sys", "user", max_tokens=65536))

    assert "reasoning_effort" not in sent[0]
    assert sent[0]["max_tokens"] == generation_service.FREE_MIN_OUTPUT_TOKENS
    assert sent[1]["max_tokens"] == generation_service.FREE_MAX_OUTPUT_TOKENS


def test_chat_async_puts_a_ceiling_on_max_tokens_for_the_free_tier(
        monkeypatch, llm_env):
    """只抬下限是错的：调用方要 65536 时 `max(65536, 4000)` 还是 65536。

    免费模型**按额度大小决定思考多久**（同一份提示词：cap=4000 → 19.9 秒、
    cap=65536 → 87.7 秒，正文都是 1900 字左右），所以上限必须真的夹住，
    而不是只保证"不太小"。整条链实测 549.9 秒 → 359.6 秒就是这一条。
    """
    monkeypatch.setenv(free_models.ENABLED_ENV, "1")
    _put_cache(["recursive/flint-v1.0:free"])
    sent = _capture_chat(monkeypatch)

    asyncio.run(chat_async("sys", "user", max_tokens=65536))

    assert sent[0]["max_tokens"] == generation_service.FREE_MAX_OUTPUT_TOKENS
    assert sent[0]["max_tokens"] <= generation_service.FREE_MAX_OUTPUT_TOKENS
    # 上限必须留得住一次结构调用的正文：实测 1723–2712 字 + 最多 2052 的 reasoning。
    assert generation_service.FREE_MAX_OUTPUT_TOKENS > generation_service.FREE_MIN_OUTPUT_TOKENS


def test_the_ceiling_never_exceeds_the_server_cap(monkeypatch, llm_env):
    """服务端上限优先：`MINDMAP_LLM_MAX_TOKENS` 比免费档上限还小时，听服务端的。"""
    monkeypatch.setenv(free_models.ENABLED_ENV, "1")
    monkeypatch.setenv(generation_service.MAX_TOKENS_ENV, "4000")
    generation_service.reset_learned_max_tokens()
    _put_cache(["recursive/flint-v1.0:free"])
    sent = _capture_chat(monkeypatch)

    asyncio.run(chat_async("sys", "user", max_tokens=65536))

    assert sent[0]["max_tokens"] == 4000


def test_chat_async_keeps_the_body_byte_identical_without_the_free_tier(
        monkeypatch, llm_env):
    """关着的时候请求体里不该多出 `reasoning_effort`，`max_tokens` 也不许被抬。"""
    _put_cache(["recursive/flint-v1.0:free"])
    sent = _capture_chat(monkeypatch)

    asyncio.run(chat_async("sys", "user", max_tokens=1400))
    asyncio.run(chat_async("sys", "user", max_tokens=65536))

    assert "reasoning_effort" not in sent[0]
    assert sent[0]["max_tokens"] == 1400
    assert sent[0]["model"] == "vendor/paid-model"
    # 也不能被压：16000 那道夹子只对免费档生效（上下两个方向都要钉，
    # 否则"顺手给所有档位都加个上限"这种改法能悄悄过掉）
    assert sent[1]["max_tokens"] == 65536


def test_the_free_tier_ceiling_reaches_the_budget_arithmetic(monkeypatch, llm_env):
    """上限不只在"发请求那一刻"生效：按上限反推容量的地方也要跟着降。

    `_max_tokens()` / `_nodes_per_call()` 自己不发请求，却用这个数算"这次装得下
    几个节点"。少这一道时免费档会按 65536 规划批次，而实际只发 16000——规划量是
    真实容量的四倍多，照它切出来的批必然被 `finish_reason=length` 截断。
    """
    from app.agent.contracts import AgentRunRequest, RunBudget, RunContext
    from app.agent.tools import _max_tokens, _nodes_per_call

    def context() -> RunContext:
        return RunContext(request=AgentRunRequest(
            goal="做一张导图", intent="create",
            budget=RunBudget(max_model_calls=10, max_output_tokens=65536),
        ))

    paid = _nodes_per_call(context())
    assert _max_tokens(context()) == generation_service.max_tokens_cap()

    monkeypatch.setenv(free_models.ENABLED_ENV, "1")
    _put_cache(["recursive/flint-v1.0:free"])

    assert _max_tokens(context()) == generation_service.FREE_MAX_OUTPUT_TOKENS
    assert _nodes_per_call(context()) < paid


def test_chat_async_fails_over_to_the_next_free_model(monkeypatch, llm_env):
    """上游偶发 429：换一个免费模型往往就通了，整次生成不该因此失败。"""
    monkeypatch.setenv(free_models.ENABLED_ENV, "1")
    _put_cache(["a/model:free", "b/model:free"])
    seen: list[str] = []

    async def fake_once(base: str, request_body: dict, headers: dict) -> dict:
        seen.append(request_body["model"])
        if len(seen) == 1:
            raise _ModelUnavailable("模型服务返回 HTTP 429")
        return {"title": "t", "root": {"id": "r", "label": "t", "children": []}}

    monkeypatch.setattr(generation_service, "_chat_once", fake_once)
    asyncio.run(chat_async("sys", "user"))
    assert seen == ["a/model:free", "b/model:free"]


def test_chat_async_reports_the_last_reason_when_every_candidate_fails(
        monkeypatch, llm_env):
    monkeypatch.setenv(free_models.ENABLED_ENV, "1")
    _put_cache(["a/model:free", "b/model:free"])

    async def always_429(base: str, request_body: dict, headers: dict) -> dict:
        raise _ModelUnavailable("模型服务返回 HTTP 503")

    monkeypatch.setattr(generation_service, "_chat_once", always_429)
    with pytest.raises(generation_service.GenerationError) as caught:
        asyncio.run(chat_async("sys", "user"))
    # 文案要说清这一轮发生了什么（503/429），而不是"请检查服务配置"。
    assert "503" in str(caught.value)


def test_chat_async_does_not_fail_over_when_the_daily_bucket_is_empty(
        monkeypatch, llm_env):
    """OpenCode 免费档用完（429 + `FreeUsageLimitError`）是**账号级**的：
    换十个候选也是同一个桶，再发就是白等。所以它必须跳过候选循环直接抛，
    且只发一次请求。"""
    monkeypatch.setenv(free_models.ENABLED_ENV, "1")
    _put_cache(["a/model:free", "b/model:free"])
    seen: list[str] = []

    async def fake_once(base: str, request_body: dict, headers: dict) -> dict:
        seen.append(request_body["model"])
        raise FreeQuotaExhausted("今日免费体验额度已用完，明天再来，或配置自己的模型继续。", 68612)

    monkeypatch.setattr(generation_service, "_chat_once", fake_once)
    with pytest.raises(FreeQuotaExhausted) as caught:
        asyncio.run(chat_async("sys", "user"))
    assert caught.value.retry_after == 68612
    assert seen == ["a/model:free"]


def test_a_self_configured_endpoint_keeps_the_old_error_sentence(
        monkeypatch, llm_env):
    """关着免费档时错误文案必须与加这个功能之前逐字一致。"""
    _put_cache(["a/model:free"])

    async def always_429(base: str, request_body: dict, headers: dict) -> dict:
        raise _ModelUnavailable("模型服务返回 HTTP 429")

    monkeypatch.setattr(generation_service, "_chat_once", always_429)
    with pytest.raises(generation_service.GenerationError) as caught:
        asyncio.run(chat_async("sys", "user"))
    assert str(caught.value) == "模型服务返回 HTTP 429，请检查服务配置"


def test_a_gateway_timeout_also_counts_as_unavailable(monkeypatch, llm_env):
    """524 是 Cloudflare 的"源站超时"，对我们与 502/504 是同一件事。

    本地实测免费模型一次响应能跑到 180 秒以上，网关先撑不住——`recursive/
    flint-v1.0:free` 在结构那一步直接回 524。它不在这个集合里的话，免费档
    不会换候选，整轮就以一条"请检查服务配置"的假结论结束，而配置是对的。
    """
    assert 524 in generation_service.UNAVAILABLE_STATUSES

    monkeypatch.setenv(free_models.ENABLED_ENV, "1")
    _put_cache(["a/model:free", "b/model:free"])
    seen: list[str] = []

    async def fake_once(base: str, request_body: dict, headers: dict) -> dict:
        seen.append(request_body["model"])
        if len(seen) == 1:
            # 走真实的 `_chat_once` 映射路径：524 必须变成 `_ModelUnavailable`。
            raise generation_service._ModelUnavailable("模型服务返回 HTTP 524")
        return {"title": "t", "root": {"id": "r", "label": "t", "children": []}}

    monkeypatch.setattr(generation_service, "_chat_once", fake_once)
    asyncio.run(chat_async("sys", "user"))
    assert seen == ["a/model:free", "b/model:free"]


# --------------------------------------------- 7. 免费额度的判定（正文，不是状态码）


def _fake_response(text: str = "", headers: dict | None = None) -> SimpleNamespace:
    """`_quota_exhausted_error` 只看 `text` 与 `headers`，给个最小替身。"""
    return SimpleNamespace(text=text, headers=headers or {})


OPENCODE_429_BODY = (
    '{"type":"error","error":{"type":"FreeUsageLimitError",'
    '"message":"Free usage limit reached. Free models work in OpenCode '
    '(https://opencode.ai); subscribe to OpenCode Go to keep going: '
    'https://opencode.ai/go"}}')


def test_the_daily_bucket_is_recognised_by_the_body_not_the_status():
    error = _quota_exhausted_error(_fake_response(
        OPENCODE_429_BODY, {"retry-after": "68612"}))
    assert isinstance(error, FreeQuotaExhausted)
    assert error.retry_after == 68612
    assert "今日免费体验额度已用完" in str(error)


def test_a_plain_429_is_not_the_daily_bucket():
    """Command Code 的偶发 429（"Upstream model provider is temporarily
    unavailable"）没有那个类型名——它要的是换候选，不是"明天再来"。"""
    assert _quota_exhausted_error(_fake_response(
        '{"error":{"message":"Upstream model provider is temporarily '
        'unavailable"}}')) is None


def test_a_non_numeric_retry_after_is_ignored_not_fatal():
    error = _quota_exhausted_error(_fake_response(
        OPENCODE_429_BODY, {"retry-after": "tomorrow"}))
    assert isinstance(error, FreeQuotaExhausted)
    assert error.retry_after is None


def test_a_bodyless_response_never_counts_as_exhausted():
    """拿不到正文（流没读、响应被 mock 掉）按"不是额度问题"处理——
    宁可多换一次候选，也不能把普通 429 判成"今天没得用了"。"""
    assert _quota_exhausted_error(_fake_response()) is None


# ---------------------------------------------------------------- 8. provider 那条出口


class _FakeStream:
    def __init__(self, status_code: int, body: bytes = b"",
                 headers: dict | None = None) -> None:
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        # 与真 httpx 的差别：真流式响应在读之前 `.text` 会抛 ResponseNotRead，
        # 这里直接给个值——`_read_whole`/`_read_stream` 都会在判额度前 `aread()`，
        # 而那条路径在本类上没有副作用。
        self.text = body.decode("utf-8", "replace") if body else ""

    async def aread(self) -> bytes:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://example.com")
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=request,
                response=httpx.Response(self.status_code, request=request),
            )

    async def aiter_bytes(self):
        yield self._body


class _FakeClient:
    def __init__(self, statuses: list[int], bodies: list[bytes],
                 headers: list[dict] | None = None) -> None:
        self.statuses = statuses
        self.bodies = bodies
        self.headers = headers or [{}] * len(statuses)
        self.bodies_sent: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url, *, content, headers):
        import json

        self.bodies_sent.append(json.loads(content))
        index = min(len(self.bodies_sent) - 1, len(self.statuses) - 1)
        stream = _FakeStream(self.statuses[index], self.bodies[index],
                             self.headers[index])

        class _Ctx:
            async def __aenter__(self_inner):
                return stream

            async def __aexit__(self_inner, *exc):
                return False

        return _Ctx()


def _completion_body(model: str) -> bytes:
    import json

    return json.dumps({
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": "{}"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }).encode()


def test_the_provider_also_switches_to_the_next_free_model(
        monkeypatch, llm_env):
    """两条出口同一个策略：agent 那轮撞上 429 也要换，而不是整轮失败。"""
    monkeypatch.setenv(free_models.ENABLED_ENV, "1")
    _put_cache(["a/model:free", "b/model:free"])
    client = _FakeClient([429, 200], [b"", _completion_body("b/model:free")])
    monkeypatch.setattr(
        provider_module.httpx, "AsyncClient", lambda **kwargs: client)

    provider = provider_module.OpenAICompatibleProvider()
    from app.agent.contracts import RunBudget

    completion = asyncio.run(provider.complete_tools(
        [{"role": "user", "content": "hi"}], [], budget=RunBudget()))

    assert completion.served_model == "b/model:free"
    assert [body["model"] for body in client.bodies_sent] == [
        "a/model:free", "b/model:free"]
    # 免费档的请求体必须带 reasoning_effort，且 max_tokens 有下限与上限。
    assert client.bodies_sent[0]["reasoning_effort"] == free_models.FALLBACK_EFFORT
    assert client.bodies_sent[0]["max_tokens"] >= generation_service.FREE_MIN_OUTPUT_TOKENS
    # 上限也要落在 provider 这条出口上：`RunBudget` 默认要 65536，不夹的话
    # agent 那一轮会按额度大小思考几百秒（见 `FREE_MAX_OUTPUT_TOKENS`）。
    assert client.bodies_sent[0]["max_tokens"] <= generation_service.FREE_MAX_OUTPUT_TOKENS
    # 已经在这个 provider 实例里失败过的模型不再重试。
    assert provider._tried == {"a/model:free"}


def test_the_provider_sends_each_candidates_own_effort(monkeypatch, llm_env):
    """档位跟着模型走：首选 `none`、候选 `low`、没档位的不发键。"""
    monkeypatch.setenv(free_models.ENABLED_ENV, "1")
    _put_records([free_models.FreeModel("m-none", "none", 100),
                  free_models.FreeModel("m-low", "low", 100),
                  free_models.FreeModel("m-bare", None, 100)])
    # 503 在 UNAVAILABLE_STATUSES 里；500 不是——撞上 500 说明请求本身不对，
    # 换模型重发只会再来一次（那是另一档错误，见 `_attempt` 的收尾）。
    client = _FakeClient(
        [503, 503, 200],
        [b"", b"", _completion_body("m-bare")],
    )
    monkeypatch.setattr(
        provider_module.httpx, "AsyncClient", lambda **kwargs: client)

    provider = provider_module.OpenAICompatibleProvider()
    from app.agent.contracts import RunBudget

    completion = asyncio.run(provider.complete_tools(
        [{"role": "user", "content": "hi"}], [], budget=RunBudget()))

    assert completion.served_model == "m-bare"
    assert [body["model"] for body in client.bodies_sent] == [
        "m-none", "m-low", "m-bare"]
    assert client.bodies_sent[0]["reasoning_effort"] == "none"
    assert client.bodies_sent[1]["reasoning_effort"] == "low"
    assert "reasoning_effort" not in client.bodies_sent[2]


def test_the_provider_does_not_fail_over_when_the_daily_bucket_is_empty(
        monkeypatch, llm_env):
    """账号级的桶：429 + `FreeUsageLimitError` 不换候选，第一发就抛。"""
    monkeypatch.setenv(free_models.ENABLED_ENV, "1")
    _put_cache(["a/model:free", "b/model:free"])
    client = _FakeClient(
        [429], [OPENCODE_429_BODY.encode()], [{"retry-after": "68612"}])
    monkeypatch.setattr(
        provider_module.httpx, "AsyncClient", lambda **kwargs: client)

    provider = provider_module.OpenAICompatibleProvider()
    from app.agent.contracts import RunBudget

    with pytest.raises(FreeQuotaExhausted) as caught:
        asyncio.run(provider.complete_tools(
            [{"role": "user", "content": "hi"}], [], budget=RunBudget()))
    assert caught.value.retry_after == 68612
    assert len(client.bodies_sent) == 1
    assert provider._tried == set()
