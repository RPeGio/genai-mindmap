"""外部检索缓存（`knowledge/external_cache.py`）：同一 query 24 小时内只打一次网。

七组断言对着这张表的七条要求：

  1. **归一化**：全角/半角、大小写、多余空白都是同一个 query；`碳达峰` 不是。
  2. **命中即零 HTTP**：第二次同一个 query 一个请求都不发，`cached=True`。
  3. **命中不丢字段**：title/text/url/source_name/language/raw_chars/direction 全等
     ——少一个字段，重建出来的命中就是另一种东西。
  4. **TTL 24 小时**：拨回 25 小时 → 重新搜。
  5. **失败不缓存，"0 条"缓存**：前者会让"网络恢复了但一天没结果"，后者是有效结论。
  6. **测量出口绕开缓存**：`directions is not None` 与 `MINDMAP_EXTERNAL_CACHE=0`
     两种情况下既不读也不写——它们要的是真实耗时，不是复述上一轮。
  7. **缓存坏了只是少一次加速**：payload 被改坏、表被删掉都不影响生成。

全部离线：`httpx.MockTransport` 在传输层之前返回，既不联网也不触发
`conftest.no_real_http`。数据库按 `conftest.clean_env` 落到各用例自己的 `tmp_path`，
所以用例之间天然隔离，不需要额外清表。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.contracts.source import ISO_SECONDS_FORMAT
from app.knowledge import external, external_cache
from app.mindmaps import repository

# 一条能过词面门的维基响应：`_relevant_hits("碳中和", …)` 要求命中里出现「碳中和」。
# `fullurl` 带 URL 编码（实测就是这样），`extract` 里有 `== ==` 小节——命中重建之后
# 这些细节必须还在。
WIKI_JSON: dict[str, Any] = {
    "batchcomplete": True,
    "query": {"pages": [{
        "pageid": 749663,
        "ns": 0,
        "title": "碳中和",
        "extract": "碳中和（英語：carbon neutrality）指抵消自身排放。\n\n== 原理 ==\n碳補償與低碳技術。",
        "fullurl": "https://zh.wikipedia.org/wiki/%E7%A2%B3%E4%B8%AD%E5%92%8C",
    }]},
}

# "搜过，确实 0 条"的形状：五个源全部 200、全部合法、全部空。
EMPTY_ROUTES: dict[str, Any] = {
    "wikipedia": {"batchcomplete": True, "query": {"pages": [], "search": []}},
    "arxiv": "<?xml version='1.0' encoding='UTF-8'?><feed xmlns='http://www.w3.org/2005/Atom'/>",
    "openalex": {"results": []},
    "crossref": {"message": {"items": []}},
    "baike": "<html><body>没有词条</body></html>",
}

_SOURCE_HOSTS = (
    ("arxiv.org", "arxiv"),
    ("openalex.org", "openalex"),
    ("crossref.org", "crossref"),
    ("baike.com", "baike"),
    ("wikipedia.org", "wikipedia"),
)


def _route_of(request: httpx.Request) -> str:
    host = request.url.host or ""
    for needle, name in _SOURCE_HOSTS:
        if needle in host:
            return name
    return "unknown"


def _client_factory(handler):
    """`external._client` → 走 `MockTransport` 的工厂（URL / 参数 / 状态码全真跑）。"""
    def factory() -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler), timeout=10.0)
    return factory


def _router(routes: dict[str, Any], calls: list[httpx.Request] | None = None):
    """按 host 分派；没配置的源一律 500（让不关心的源自然降级）。

    `routes` 的值：dict（JSON）、str（原始文本）、`Exception`（抛出去）。
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        target = _route_of(request)
        spec = routes.get(target)
        if spec is None:
            return httpx.Response(500, text=f"{target} 未配置")
        if isinstance(spec, Exception):
            raise spec
        if isinstance(spec, str):
            return httpx.Response(200, text=spec)
        return httpx.Response(200, json=spec)
    return handler


def _use(monkeypatch, routes: dict[str, Any],
         calls: list[httpx.Request] | None = None) -> list[httpx.Request]:
    """装好假客户端，返回请求记录（不传 `calls` 时自己造一个）。"""
    recorded = calls if calls is not None else []
    monkeypatch.setattr(external, "_client", _client_factory(_router(routes, recorded)))
    return recorded


def _age_cache(hours: float) -> None:
    """把表里所有行的 `searched_at` 拨回若干小时（TTL 用例的唯一手段）。"""
    stamp = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
        ISO_SECONDS_FORMAT)
    with repository.connect() as conn:
        conn.execute(f"UPDATE {external_cache.CACHE_TABLE} SET searched_at = ?", (stamp,))


def _rows() -> int:
    """表里有几行。**表还不存在时算 0**。

    表由 `db.schema.init_schema()` 建好（`_open()` 不再懒建），所以正常路径上它一定
    在；这个 try 只是给"有人直接拿空库跑这个模块"留的余量，与"0 行"同义。
    """
    try:
        with repository.connect() as conn:
            return int(conn.execute(
                f"SELECT COUNT(*) AS total FROM {external_cache.CACHE_TABLE}").fetchone()["total"])
    except Exception:  # noqa: BLE001 — 没有表 = 没有缓存，与"0 行"同义
        return 0


# ---------------------------------------------------------------- 1 归一化


def test_normalize_folds_width_case_and_spacing():
    """四条归一化各管一件事，合起来是"同一个 query 的另一种写法"。"""
    assert external_cache.normalize_query("  碳中和  ") == "碳中和"
    assert external_cache.normalize_query("碳中和\u3000技术") == "碳中和 技术"
    assert external_cache.normalize_query("Carbon   Neutrality") == "carbon neutrality"
    assert external_cache.normalize_query("CarbonNeutrality") == "carbonneutrality"
    # 全角括号 / 全角字母 → 半角（NFKC）
    assert external_cache.normalize_query("碳中和（Carbon）") == "碳中和(carbon)"
    assert external_cache.normalize_query("ＡＢＣ") == "abc"


def test_normalize_does_not_fold_meaning():
    """只折叠写法，不折叠意思：主题换了一个词就是另一个 query。

    这一条是这张表能力的**边界**，不是缺陷：折叠近义词需要一张词表，而词表判错的
    代价是"用户换了个主题却拿到上一题的资料"——一份看起来有出处的假依据。
    """
    assert external_cache.normalize_query("碳中和") != external_cache.normalize_query("碳达峰")
    assert (external_cache.normalize_query("机器学习")
            != external_cache.normalize_query("机器学习，用鱼骨图"))
    # 标点是有意义的一部分：它可能是模型/用户给的限定语
    assert external_cache.normalize_query("rust 学习路线") != external_cache.normalize_query(
        "rust学习路线")


def test_query_hash_ignores_case_and_width():
    """键只看归一化结果：两种写法同一个 hash，两种意思两个 hash。"""
    assert external_cache.query_hash("碳中和") == external_cache.query_hash("  碳中和 ")
    assert external_cache.query_hash("Rust 所有权") == external_cache.query_hash("rust 所有权")
    assert external_cache.query_hash("碳中和") != external_cache.query_hash("碳达峰")
    # 键是短的十六进制串（128 bit），不是整段文本
    assert len(external_cache.query_hash("碳中和")) == 32


# ---------------------------------------------------------------- 2 命中即零 HTTP


def test_second_identical_query_never_touches_the_network(monkeypatch):
    """同一主题搜两次：第二次一个 HTTP 请求都不发，且 `cached=True`。

    这是这个功能存在的全部理由——用户改一个字再生成是常态（真库里
    `rust学习路线` 的两次生成相隔 48 秒），而每次 4–5 秒的外网等待里没有一次
    是本轮的新信息。
    """
    calls = _use(monkeypatch, {"wikipedia": WIKI_JSON})

    first = external.search_external("碳中和")
    assert first and first.cached is False
    requests_after_first = len(calls)
    assert requests_after_first >= 1

    second = external.search_external("碳中和")

    assert second.cached is True
    assert len(calls) == requests_after_first, "命中缓存之后不该再发任何请求"
    assert [(hit.title, hit.url) for hit in second] == [
        (hit.title, hit.url) for hit in first]


def test_cached_hits_keep_every_field(monkeypatch):
    """命中重建的每一条字段都与第一次相同（含 `raw_chars` 与 `direction`）。

    少一个字段就是"另一种东西"：`raw_chars` 决定界面上"这条被砍掉九成"的显示，
    `direction` 决定多方向报告里"这条来自哪个方面"。
    """
    _use(monkeypatch, {"wikipedia": WIKI_JSON})
    fields = ("title", "text", "url", "source_name", "fetched_at", "language",
              "raw_chars", "direction")

    first = external.search_external("碳中和")
    second = external.search_external("碳中和")

    assert [[getattr(hit, name) for name in fields] for hit in first] == [
        [getattr(hit, name) for name in fields] for hit in second]
    # 正文里的维基小节（转成 Markdown 的那种）也原样回来了
    assert "## 原理" in second[0].text


def test_hit_count_tracks_reuse(monkeypatch):
    """`hit_count` 记的是"这条缓存被复用了多少次"，不是"搜过几次"。"""
    _use(monkeypatch, {"wikipedia": WIKI_JSON})

    external.search_external("碳中和")
    with repository.connect() as conn:
        before = conn.execute(
            f"SELECT hit_count FROM {external_cache.CACHE_TABLE}").fetchone()["hit_count"]
    assert before == 0

    external.search_external("碳中和")
    external.search_external("碳中和")
    with repository.connect() as conn:
        after = conn.execute(
            f"SELECT hit_count FROM {external_cache.CACHE_TABLE}").fetchone()["hit_count"]
    assert after == 2


# ---------------------------------------------------------------- 3 TTL


def test_ttl_expiry_forces_a_real_search(monkeypatch):
    """25 小时前的缓存不算命中：一天以上的间隔多半是另一次会话、另一个话题了。"""
    calls = _use(monkeypatch, {"wikipedia": WIKI_JSON})
    external.search_external("碳中和")
    requests_after_first = len(calls)

    _age_cache(25)
    again = external.search_external("碳中和")

    assert again.cached is False
    assert len(calls) > requests_after_first, "过期之后必须重新打网"


def test_fresh_cache_within_ttl_is_used(monkeypatch):
    """23 小时前的缓存照样命中——TTL 是 24 小时，不是"几分钟"。"""
    calls = _use(monkeypatch, {"wikipedia": WIKI_JSON})
    external.search_external("碳中和")
    requests_after_first = len(calls)

    _age_cache(23)
    again = external.search_external("碳中和")

    assert again.cached is True
    assert len(calls) == requests_after_first


def test_ttl_constant_is_one_day():
    """24 小时这个数本身也是契约：改它要改 docstring 里那两段理由。"""
    assert external_cache.TTL_SECONDS == 24 * 3600


# ---------------------------------------------------------------- 4 零条 vs 失败


def test_zero_hits_are_cached(monkeypatch):
    """"搜过，确实 0 条"是**有效结论**：重搜一遍仍然是 0 条，不该再等一次网。"""
    calls = _use(monkeypatch, EMPTY_ROUTES)

    first = external.search_external("碳中和")
    assert first == [] and first.cached is False
    requests_after_first = len(calls)

    second = external.search_external("碳中和")

    assert second == []
    assert second.cached is True
    assert len(calls) == requests_after_first, "0 条命中也要跳过 HTTP"


def test_total_failure_is_not_cached(monkeypatch):
    """五个源全挂（代理没开）**不进缓存**：网络恢复了就该有一次真搜的机会。

    这是"失败不缓存"那一条的全部意义。缓存住它意味着"代理打开了，但接下来一天
    都是空结果"——而用户重试的正是那一刻。
    """
    _use(monkeypatch, {name: httpx.ConnectError("proxy refused")
                       for name in ("wikipedia", "arxiv", "openalex", "crossref", "baike")})

    assert external.search_external("碳中和") == []
    assert _rows() == 0, "全源失败的空结果不该被记成结论"

    # 网络恢复：同一个 query 必须重新搜，而不是命中上面那次失败
    calls = _use(monkeypatch, {"wikipedia": WIKI_JSON})
    recovered = external.search_external("碳中和")

    assert recovered and recovered.cached is False
    assert calls, "恢复后要真的发请求"
    assert recovered[0].title == "碳中和"


def test_partial_failure_with_hits_is_still_cached(monkeypatch):
    """部分源挂了但拿到了东西 → 照样缓存。

    取舍是明确的：宁可 TTL 内少一条（那个源这轮没给），也不要让用户等两次一样长的
    等待。真要每次都齐，会退化成"永远不缓存"。
    """
    _use(monkeypatch, {"wikipedia": WIKI_JSON,
                       "arxiv": httpx.ConnectError("boom")})

    first = external.search_external("碳中和")
    assert first and [hit.source_name for hit in first] == ["wikipedia"]

    second = external.search_external("碳中和")

    assert second.cached is True
    assert [hit.source_name for hit in second] == ["wikipedia"]


def test_requests_are_needed_before_caching_an_empty_result(monkeypatch):
    """一次请求都没发出去的空结果不算"查过"，不写缓存。

    这条是 `_worth_caching` 里 `requests > 0` 那个条件的执行处：`DEADLINE_SECONDS`
    被压到 0 时预算立刻用尽，五个源一次都没打出去——那不是"网上没有"。
    """
    monkeypatch.setattr(external, "DEADLINE_SECONDS", 0.0)
    _use(monkeypatch, EMPTY_ROUTES)

    assert external.search_external("碳中和") == []
    assert _rows() == 0


# ---------------------------------------------------------------- 5 测量出口


def test_injected_directions_bypass_the_cache(monkeypatch):
    """`directions is not None`（测量脚本与用例的出口）不读也不写。

    它们要的是真实耗时与真实候选池：命中缓存会让"这次检索花了几秒"变成 0，让
    `--collect-single` 与 `--collect-directions` 拿到同一份池子——那两组对照就没意义了。
    """
    calls = _use(monkeypatch, {"wikipedia": WIKI_JSON})

    external.search_external("碳中和", directions=["碳中和"])
    requests_after_first = len(calls)
    assert _rows() == 0, "注入方向的那一次不该写缓存"

    external.search_external("碳中和", directions=["碳中和"])

    assert len(calls) > requests_after_first, "注入方向时不该读缓存"
    assert _rows() == 0


def test_env_switch_turns_the_cache_off(monkeypatch):
    """`MINDMAP_EXTERNAL_CACHE=0` 时一个读写都不发生（测量脚本用的就是这个）。"""
    monkeypatch.setenv(external_cache.CACHE_ENV, "0")
    assert external_cache.cache_enabled() is False
    calls = _use(monkeypatch, {"wikipedia": WIKI_JSON})

    external.search_external("碳中和")
    requests_after_first = len(calls)
    again = external.search_external("碳中和")

    assert again.cached is False
    assert len(calls) > requests_after_first
    assert _rows() == 0


def test_larger_limit_is_not_a_cache_hit(monkeypatch):
    """缓存那次要 1 条、这次要 3 条 → 不算命中。

    缓存里存的是那次检索的**最终产物**（已过门、已交替取样、已截到 limit）。拿
    1 条去回答"要 3 条"是错答案，不是近似答案——缺的两条会让下游以为"这个主题
    只有一条资料"。
    """
    calls = _use(monkeypatch, {"wikipedia": WIKI_JSON})
    one = external.search_external("碳中和", limit=1)
    assert len(one) == 1
    requests_after_first = len(calls)

    three = external.search_external("碳中和", limit=3)

    assert three.cached is False
    assert len(calls) > requests_after_first


# ---------------------------------------------------------------- 6 坏缓存只是少一次加速


def test_corrupt_payload_falls_back_to_a_real_search(monkeypatch):
    """payload 被改坏（旧版本写过 / 手动改过）→ 重新搜，不抛。"""
    _use(monkeypatch, {"wikipedia": WIKI_JSON})
    external.search_external("碳中和")
    with repository.connect() as conn:
        conn.execute(
            f"UPDATE {external_cache.CACHE_TABLE} SET payload = 'not json'")

    calls = _use(monkeypatch, {"wikipedia": WIKI_JSON})
    again = external.search_external("碳中和")

    assert again.cached is False
    assert again, "坏缓存不能把结果一起扔掉"
    assert calls


def test_expired_rows_are_pruned_on_write(monkeypatch):
    """写入时顺手清过期行：这张表没有别的 GC，而过期是唯一确定的删除判据。"""
    _use(monkeypatch, {"wikipedia": WIKI_JSON})
    external.search_external("碳中和")
    _age_cache(30)
    assert _rows() == 1

    # 换一个 query 触发一次新写入。用"全源 200 且 0 条"那组响应：它同样会写缓存
    # （`_worth_caching` 认"确实 0 条"），但不依赖夹具里那个主题词能不能过门。
    _use(monkeypatch, EMPTY_ROUTES)
    external.search_external("另一件事")

    # 那条过期的没了、新的那一条在（写之前先删过期行）
    assert _rows() == 1
    with repository.connect() as conn:
        query = conn.execute(
            f"SELECT query FROM {external_cache.CACHE_TABLE}").fetchone()["query"]
    assert query == "另一件事"


def test_cache_table_row_is_readable_for_diagnostics(monkeypatch):
    """表里那几列要能直接回答"这个主题搜过几次、什么时候"——它是排障入口。"""
    _use(monkeypatch, {"wikipedia": WIKI_JSON})
    external.search_external("碳中和")
    external.search_external("碳中和")

    with repository.connect() as conn:
        row = conn.execute(
            f"SELECT query, limit_value, hit_count, searched_at FROM "
            f"{external_cache.CACHE_TABLE}").fetchone()

    assert row["query"] == "碳中和"
    assert row["limit_value"] == external.DEFAULT_LIMIT
    assert row["hit_count"] == 1
    # 秒级 ISO，与库里其余时间戳同一口径（`ISO_SECONDS_FORMAT`）
    assert datetime.strptime(row["searched_at"], ISO_SECONDS_FORMAT)


def test_clear_removes_every_row(monkeypatch):
    """`clear()` 只给用例与排障用；它的存在让"缓存未隔离"能一眼排掉。"""
    _use(monkeypatch, {"wikipedia": WIKI_JSON})
    external.search_external("碳中和")
    assert _rows() == 1

    assert external_cache.clear() == 1
    assert _rows() == 0
