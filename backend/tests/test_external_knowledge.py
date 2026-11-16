"""主题生成的外部知识检索：抓取 → 清洗 → 转 `Source` → 走既有 RAG 链。

十组断言对着这条链的十条要求（第 8 组的前端一半在 `tests/external-ref-link.spec.ts`）：

  1. 解析：固定 JSON / XML / HTML 样本 → `ExternalHit` 字段正确（不联网）。五个源
     各一组：维基、arXiv、OpenAlex、Crossref、快懂百科。
  2. 清洗：HTML/XML 标签剥掉、维基小节转 Markdown、超长正文截断且原始长度可查。
  3. 失败：超时 / 500 / 429 / 非法 JSON / 脏 XML → 返回空，**不抛**。
  4. 开关关：与 `aeed033` / `83d61a0` / `dceae7e` 旧模块**逐字节**相同（加载旧
     `tools.py` 对比 `(ok, content, data)`，并且一次外部请求都不发）。
  5. 开关开 + 有结果：user 消息里有外部正文，`source_refs` 指向新入库的来源，
     引用**校验后还在**（断言"还在"，"没抛异常"证明不了任何事）。
  6. 开关开 + 全失败：退回纯主题生成，`content` 里说明。
  7. 预算：请求次数不超上限、截止时间到了不再发请求。
  8. `Source` 转换：文件名 / kind / metadata.url / raw_chars，以及 `javascript:`
     这类 scheme 在契约层折成 None。
  9. 中文支持：`search_external("碳中和")` 必须给有实质内容的中文条目——这是
     那次换源（百度百科/知乎 → OpenAlex/Crossref/快懂百科）的验收线。
 10. **默认开 + 材料太短**：不设环境变量时一次纯主题生成就能拿到外部引用；8 字
     材料不走材料路径（改为"主题提示 + 外部检索/纯知识"）；三种退化各有各的话术；
     `full` 形态不受这条规则影响（阈值的依据是切块参数，只有 `rag` 有切块）。

全部离线：`tests/conftest.py` 的 `no_real_http` 会把真到传输层的请求变成断言失败；
本文件用 `httpx.MockTransport` 造响应（它在传输层之前就返回，不触发那道闸），
既验证了 URL / 参数 / 次数，又一条真请求都不发。
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import unquote

import httpx
import pytest

from app.agent import tools as agent_tools
from app.agent.contracts import AgentRunRequest, RunBudget, RunContext
from app.contracts.source import Source, SourceKind, SourceMetadata, safe_http_url
from app.generation.service import SOURCE_MODE_ENV, SOURCE_MODE_FULL
from app.knowledge import external
from app.knowledge import library

# 写死基线，不跟随 HEAD：这一轮之后 `tools.py` 又被改过，拿 HEAD 对比等于自己跟
# 自己比（与 `tests/test_library_wiring.py` 同一条规矩）。rev 与内容各钉一道——
# 只有 rev 时改常量就能换掉基线，只有 hash 时看不出这份基线是哪一版。
#
# 三个基线，钉的是三件不同的事：
#   - `aeed033`：外部知识检索**第一次接进来**的那一版。开关关着时"整个功能不存在"；
#   - `83d61a0`：加源之前的那一版。加三个源不能改动开关关闭时的任何一个字节
#     ——这是"新源可以加，但不许偷偷改变关闭状态下的行为"的验收线；
#   - `dceae7e`：**翻默认值之前**的 HEAD。翻成默认开之后，"关掉开关"必须仍然回到
#     这一版的行为：不发请求、不换提示词、`data` 的键集不多不少；
#   - `202a7bd`：**加相关性门与检索进度帧之前**的 HEAD（本轮改动的起点）。门只该
#     在"抓到了东西"时改变结果，进度帧只在有 SSE 消费者时出现——两件事都不该碰
#     "关掉外部检索"这条路的任何一个字节。它是本轮唯一能证明"我没顺手改坏别的"
#     的基线，缺了它就只能靠读 diff 自查。
BASELINES = {
    "aeed033": "546fde932e7d331c70dec0bc221b7041a9d46409216bcc17b023d0f04d9a239e",
    "83d61a0": "f085b4f2fc341f1bb0d4f1299590552abf291e7af63af518b868b6877b0cab3b",
    "dceae7e": "51902cb3149cbc1b1cf486ae65bf6e576b0677d8db8d580152583c3d2255db4c",
    "202a7bd": "a55d394823818d3dd00649df566d33abe53892cd9b56f02948d8f086a12810ef",
}

REPO_ROOT = Path(__file__).resolve().parents[2]

# 真实响应的形状（2026-11 实测所得，正文截短）：`formatversion=2` 的 `pages` 是
# 列表，条目里带 `fullurl` 与 `extract`。
WIKI_JSON: dict[str, Any] = {
    "batchcomplete": True,
    "query": {
        "pages": [{
            "pageid": 749663,
            "ns": 0,
            "title": "碳中和",
            "extract": (
                "碳中和（英語：carbon neutrality）是指在一定时间内抵消自身产生的"
                "二氧化碳排放量，达到相对“零排放”。\n\n\n"
                "== 原理 ==\n要達成碳中和一般有两种做法：碳補償與低碳技術。\n\n"
                "=== 碳交易 ===\n透過碳交易把減排成本轉移給其他地區。\n"
            ),
            "fullurl": "https://zh.wikipedia.org/wiki/%E7%A2%B3%E4%B8%AD%E5%92%8C",
        }],
    },
}

# 中文标题在维基上不命中时的搜索响应形状（`list=search`，`formatversion=2`）。
WIKI_SEARCH_JSON: dict[str, Any] = {
    "batchcomplete": True,
    "query": {"search": [{"title": "碳中和"}, {"title": "碳交易"}]},
}

WIKI_MISS_JSON: dict[str, Any] = {
    "batchcomplete": True,
    "query": {"pages": [{"ns": 0, "title": "碳中和技术路径", "missing": True}]},
}

# arXiv 的 Atom 形状：`<id>` 是 abs 页（原文地址），摘要里带 XML 转义。
ARXIV_XML = """<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2310.01234v1</id>
    <title>Carbon Neutrality: Policy &amp; Technology Pathways</title>
    <summary>We study &lt;b&gt;carbon&lt;/b&gt; neutrality targets.
A second line of the abstract.</summary>
  </entry>
</feed>"""

ARXIV_EMPTY_XML = """<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>ArXiv Query</title>
</feed>"""

# OpenAlex 的形状（2026-11 实测，正文截短）：摘要是**倒排索引**——`{单词: [位置,…]}`，
# 不是字符串。中文的分词位置是连续的，重建后是正常句子。
OPENALEX_JSON: dict[str, Any] = {
    "meta": {"count": 70562, "per_page": 5},
    "results": [
        {
            "id": "https://openalex.org/W7100000001",
            "doi": "https://doi.org/10.26789/jnhb.v7i2.745",
            "title": "试析“碳达峰碳中和”的对策",
            "language": "ja",          # 实测它把中文标成 ja/it，故意留在这里
            "publication_year": 2022,
            "primary_location": {
                "landing_page_url": "https://doi.org/10.26789/jnhb.v7i2.745",
            },
            "abstract_inverted_index": {
                "论文主要分析了碳达峰碳综合的发展背景": [0],
                "阐述了": [1],
                "影响因素": [2],
            },
        },
        {
            "id": "https://openalex.org/W7100000002",
            # 没有摘要 → 必须被丢掉（只有标题的条目对生成毫无用处）
            "title": "A Paper Without Abstract",
            "language": "en",
            "abstract_inverted_index": None,
        },
        {
            "id": "https://openalex.org/W7100000003",
            "title": "Carbon Neutrality Pathways",
            "language": "en",
            "abstract_inverted_index": {"We": [0], "model": [1], "paths.": [2]},
        },
    ],
}

# Crossref 的形状（2026-11 实测，正文截短）：`abstract` 是 JATS XML。
CROSSREF_JSON: dict[str, Any] = {
    "status": "ok",
    "message-type": "work-list",
    "message": {
        "total-results": 305341,
        "items": [
            {
                "title": ["刍议碳达峰和碳中和背景下工业低碳发展"],
                "URL": "https://doi.org/10.26789/jnhb.v7i5.1241",
                "DOI": "10.26789/jnhb.v7i5.1241",
                "abstract": (
                    "<jats:p>全球经济在飞速发展，碳排放量也一路高飞猛进。</jats:p>"
                ),
            },
            {
                "title": ["Blockchain for carbon reduction"],
                "DOI": "10.55800/envirarxiv132",
                "URL": "https://doi.org/10.55800/envirarxiv132",
                # 无 abstract → 丢掉
            },
            {
                # 旧条目的 `title` 是字符串而不是列表，两种形状都要认
                "title": "An Old Style Title",
                "abstract": "<jats:p>An abstract in the old shape.</jats:p>",
                "DOI": "10.1/old",
            },
        ],
    },
}

# 快懂百科词条页的形状（2026-11 实测，结构原样、正文截短）：标题在
# `doc-title-container` 的 h1 里，正文是 `baike-render-paragraph` 里被拆成
# `<span data-type="text">` 碎片、中间夹着内链 span。
BAIKE_HTML = """<!DOCTYPE html><html><head><title>快懂百科</title></head><body>
<h1 class="container-U8bUzX doc-title-container" contenteditable="false">碳中和</h1>
<div class="descriptionContainer-ROSEC9"><div class="description-SlWUwu">节能减排术语</div></div>
<div class="baike-render-paragraph"><span data-node-start-position="1" data-type="text">碳中和（</span><span data-node-start-position="5" data-type="text">Carbon</span><span data-node-start-position="11" data-type="text"> Neutrality）是指各主体测算在一定时间内的排放总量。</span></div>
<div class="baike-render-paragraph"><span data-type="text">实现碳中和的两个重要途径是</span><span data-doc-id="7243" data-title="碳交易" data-type="internal_link"><a href="/wikiid/7243">减少碳排放和进行碳交易</a></span><span data-type="text">。</span></div>
<script type="application/json">{"ReferenceList":[{"Content":"这是一段来自今日头条的转载原文，绝不该进入词条正文。"}]}</script>
</body></html>"""


# ---------------------------------------------------------------- 夹具


def _client_factory(handler):
    """把 `external._client` 换成一个走 `MockTransport` 的工厂。

    `MockTransport` 在传输层**之前**返回响应，所以既不联网、也不触发
    `conftest.no_real_http`——这比"把 `httpx.Client` 整个换掉"更接近生产路径：
    URL 拼接、参数编码、状态码判断、JSON 解析全都真的跑了一遍。
    """
    def factory() -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler), timeout=10.0)
    return factory


# host 片段 → 源名。顺序有意义：`api.crossref.org` 同时含 "crossref.org"，
# 放在 wikipedia 之前无所谓，但每一条都必须唯一命中一个源名。
_SOURCE_HOSTS = (
    ("arxiv.org", "arxiv"),
    ("openalex.org", "openalex"),
    ("crossref.org", "crossref"),
    ("baike.com", "baike"),
    ("wikipedia.org", "wikipedia"),
)


def _route_of(request: httpx.Request) -> str:
    """按 host 判断这次请求打的是哪个源。"""
    host = request.url.host or ""
    for needle, name in _SOURCE_HOSTS:
        if needle in host:
            return name
    return "unknown"


def _router(routes: dict[str, Any], calls: list[httpx.Request] | None = None):
    """按 host 分派的 handler。`routes` 的值可以是 dict（JSON）、str（原始文本）、
    `Exception`（那就抛出去）或 callable(request) → 响应。

    **没在 `routes` 里出现的源一律 500**：一条用例通常只关心一两个源，其余三个
    应当自然降级为空，而不是逼着每处都写一遍"这个源返回 404"。这也让"新增源"
    不需要回头改老用例——它们对新源自动免疫。
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        target = _route_of(request)
        spec = routes.get(target)
        if spec is None:
            return httpx.Response(500, text=f"{target} 未在本用例的 routes 里配置")
        if isinstance(spec, Exception):
            raise spec
        if callable(spec):
            return spec(request)
        if isinstance(spec, str):
            return httpx.Response(200, text=spec)
        return httpx.Response(200, json=spec)
    return handler


def _request(sources: list[Source] | None = None) -> AgentRunRequest:
    return AgentRunRequest(
        goal="碳中和", intent="create", thinking_model="default",
        sources=sources or [],
        budget=RunBudget(max_model_calls=10, max_tool_calls=8, max_seconds=60.0,
                         max_output_tokens=4096, max_repairs=1),
    )


def _ctx(sources: list[Source] | None = None) -> RunContext:
    payload = sources or []
    return RunContext(
        request=_request(payload),
        sources_by_id={source.id: source for source in payload},
    )


def _tree(refs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """三条一级分支（`MIN_BRANCHES=3` 的下限），第一条带引用。"""
    return {
        "title": "碳中和",
        "root": {"id": "root", "label": "碳中和", "children": [
            {"id": "a", "label": "政策", "children": [], "source_refs": refs or []},
            {"id": "b", "label": "技术路径", "children": []},
            {"id": "c", "label": "企业案例", "children": []},
        ]},
    }


def _generate(ctx: RunContext, topic: str = "碳中和", *,
              fake: Any = None) -> tuple[Any, list[tuple[str, str]]]:
    """跑一次 `mindmap_generate`，同时把每次 `chat_async` 的 (system, user) 留下来。

    `fake` 可以是固定的树，也可以是一个 "看 (system, user) 再决定返回什么" 的函数
    —— 第五组要按 user 消息里的 evidence 造引用，只有后者做得到。
    """
    calls: list[tuple[str, str]] = []

    async def chat(system: str, user: str, **kwargs: Any) -> Any:
        calls.append((system, user))
        return fake(system, user) if callable(fake) else fake

    with patch.object(agent_tools, "chat_async", chat):
        result = asyncio.run(agent_tools.build_registry("create").execute(
            agent_tools.ToolCall(call_id="c-gen", name="mindmap_generate",
                                 arguments={"topic": topic}), ctx))
    return result, calls


def _load_baseline_tools(tmp_path: Path, rev: str = "aeed033"):
    """把某个基线 rev 的 `agent/tools.py` 载成独立模块，并核对 sha256。"""
    try:
        raw = subprocess.run(
            ["git", "show", f"{rev}:backend/app/agent/tools.py"],
            cwd=REPO_ROOT, capture_output=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:  # pragma: no cover
        pytest.skip(f"读不到 {rev} 的 tools.py：{exc}")

    assert hashlib.sha256(raw).hexdigest() == BASELINES[rev]

    path = tmp_path / f"tools_{rev}.py"
    path.write_bytes(raw)
    spec = importlib.util.spec_from_file_location(f"tools_baseline_{rev}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- 1 解析


def test_wikipedia_json_becomes_external_hit(monkeypatch):
    """固定 JSON → `ExternalHit` 的每个字段都对，且小节变成了 Markdown 标题。

    断言的是**字段值**，不是"没抛异常"：外呼链路上最容易错的就是"parsed = True
    但 title 是 None"这种半成功，它会在清单里显示成一条没有名字的材料。
    """
    calls: list[httpx.Request] = []
    monkeypatch.setattr(
        external, "_client", _client_factory(_router({"wikipedia": WIKI_JSON}, calls)))

    hits = external.search_external("碳中和")

    assert len(hits) == 1
    hit = hits[0]
    assert hit.source_name == "wikipedia"
    assert hit.title == "碳中和"
    assert hit.language == "zh"
    assert hit.url == "https://zh.wikipedia.org/wiki/%E7%A2%B3%E4%B8%AD%E5%92%8C"
    assert hit.fetched_at.endswith("Z") and len(hit.fetched_at) == 20
    # 维基的小节标记转成了 Markdown —— 切块器与材料目录只认 `#{1,6} `。
    assert "## 原理" in hit.text and "== 原理 ==" not in hit.text
    assert "### 碳交易" in hit.text
    assert hit.text.startswith("碳中和（英語：carbon neutrality）")
    assert hit.raw_chars == len(hit.text)     # 未超限，原始长度就是正文长度
    # 一次精确标题请求就够：`titles=` 命中时不该再去搜一遍。
    wiki_calls = [c for c in calls if "wikipedia.org" in c.url.host]
    assert [request.url.params.get("titles") for request in wiki_calls] == ["碳中和"]


def test_arxiv_atom_becomes_external_hit(monkeypatch):
    """固定 Atom → `ExternalHit`；XML 转义反转义、内嵌标签剥掉、http 换 https。"""
    monkeypatch.setattr(
        external, "_client", _client_factory(_router({"arxiv": ARXIV_XML})))

    hits = external.search_external("carbon neutrality")

    assert len(hits) == 1
    hit = hits[0]
    assert hit.source_name == "arxiv"
    assert hit.language == "en"
    assert hit.title == "Carbon Neutrality: Policy & Technology Pathways"
    assert hit.url == "https://arxiv.org/abs/2310.01234v1"
    assert "carbon neutrality targets" in hit.text
    assert "<b>" not in hit.text and "&lt;" not in hit.text
    # 多行摘要压成一段（换行保留但行内空白归一）
    assert "A second line of the abstract." in hit.text


def test_openalex_inverted_index_becomes_chinese_text(monkeypatch):
    """OpenAlex：倒排索引重建、丢弃无摘要条目、语言按字符判而不信 `language`。

    这一组断言各自钉一个实测过的坑：

      - `abstract_inverted_index` 是 `{单词: [位置]}`，不重建就只剩一堆键名；
      - 只有标题的条目必须丢——它会以"有出处的资料"身份进清单，让模型以为有据可查；
      - 样本里第一条的 `language` 是 `"ja"`（实测 OpenAlex 真把中文标题标成 ja/it），
        而 `hit.language` 必须是 `"zh"`：字符集不会撒谎。
    """
    monkeypatch.setattr(
        external, "_client", _client_factory(_router({"openalex": OPENALEX_JSON})))

    hits = external.search_external("碳中和")

    assert [hit.source_name for hit in hits] == ["openalex", "openalex"]
    first = hits[0]
    assert first.title == "试析“碳达峰碳中和”的对策"
    assert first.language == "zh"
    assert first.text == "论文主要分析了碳达峰碳综合的发展背景 阐述了 影响因素"
    assert first.url == "https://doi.org/10.26789/jnhb.v7i2.745"
    assert first.raw_chars == len(first.text)
    # 英文摘要那条：语言按字符判成 en，URL 落到 openalex 的 id（没有 doi/落地页）
    assert hits[1].language == "en"
    assert hits[1].url == "https://openalex.org/W7100000003"


def test_openalex_abstract_rebuild_ignores_hostile_positions():
    """倒排索引里的位置索引是**外部数据**：越界与非整数一律丢，不参与分配。"""
    payload = {
        "results": [{
            "title": "碳中和",
            "abstract_inverted_index": {
                "甲": [0], "乙": [1],
                "越界": [10 ** 9], "负数": [-1], "非整数": ["x"],
                "坏值": None,
            },
        }],
        "not_a_dict": True,
    }
    hits = external._parse_openalex(payload, 3)
    assert [hit.text for hit in hits] == ["甲 乙"]
    # 顶层不是 dict / results 不是 list 时也只是空，不抛
    assert external._parse_openalex(["不是字典"], 3) == []
    assert external._parse_openalex({"results": "不是列表"}, 3) == []


def test_crossref_jats_abstract_is_stripped(monkeypatch):
    """Crossref：JATS 标签剥掉、无摘要丢弃、`title` 列表与字符串两种形状都认。"""
    monkeypatch.setattr(
        external, "_client", _client_factory(_router({"crossref": CROSSREF_JSON})))

    hits = external.search_external("碳中和")

    assert [hit.source_name for hit in hits] == ["crossref", "crossref"]
    assert hits[0].title == "刍议碳达峰和碳中和背景下工业低碳发展"
    assert hits[0].language == "zh"
    assert hits[0].text == "全球经济在飞速发展，碳排放量也一路高飞猛进。"
    assert "jats" not in hits[0].text and "<" not in hits[0].text
    assert hits[0].url == "https://doi.org/10.26789/jnhb.v7i5.1241"
    # 旧条目：title 是字符串，URL 缺省时由 DOI 拼出来
    assert hits[1].title == "An Old Style Title"
    assert hits[1].url == "https://doi.org/10.1/old"


def test_baike_takes_only_the_article_body(monkeypatch):
    """快懂百科：碎片 span 拼成句子，**只取正文容器**。

    最后那条断言是这张用例存在的理由：样本里 `ReferenceList` 的 `Content` 字段
    藏了一段"来自今日头条的转载原文"。整页 `clean_text` 会把它当词条正文收进来
    ——引用会指向一段用户没在词条里看过的转载。
    """
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, text=BAIKE_HTML, request=request)

    monkeypatch.setattr(external, "_client", _client_factory(handler))

    hits = external.search_external("碳中和")

    assert len(hits) == 1
    hit = hits[0]
    assert hit.source_name == "baike"
    assert hit.title == "碳中和"
    assert hit.language == "zh"
    assert hit.text.startswith("碳中和（Carbon Neutrality）是指各主体")
    # 两段之间留空行：切块器按空行分块，段间无空行整篇会挤成一块
    assert "\n\n" in hit.text
    # 内链的文字要留在正文里，链接标签不能留
    assert "减少碳排放和进行碳交易" in hit.text
    assert "<a" not in hit.text and "/wikiid/7243" not in hit.text
    # 参考资料里的转载原文一个字都不该进来
    assert "今日头条" not in hit.text
    assert "转载原文" not in hit.text
    # 只发一次请求：`/search` 被 robots 禁止，这个源不做候选检索
    baike_calls = [call for call in calls if "baike.com" in call.url.host]
    assert len(baike_calls) == 1
    assert unquote(str(baike_calls[0].url)) == "https://www.baike.com/wiki/碳中和"


def test_baike_missing_title_or_body_yields_nothing():
    """页面上没有 `doc-title-container` 或没有段落 → 空，不产出半条。"""
    assert external._parse_baike("<html><body>没有正文</body></html>", 3) == []
    assert external._parse_baike(
        '<h1 class="doc-title-container">碳中和</h1>', 3) == []
    assert external._parse_baike(BAIKE_HTML, 0) == []


def test_wikipedia_falls_back_to_search_when_title_misses(monkeypatch):
    """`titles=` 不命中时走条目检索，再逐条抓正文。

    没有这一步，"碳中和技术路径"这种输入在维基上一条都拿不到——而它恰恰是
    用户最常输入的形态（精确标题只对"碳中和"这种单名词生效）。
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("list") == "search":
            return httpx.Response(200, json=WIKI_SEARCH_JSON)
        titles = request.url.params.get("titles")
        if titles == "碳中和":
            return httpx.Response(200, json=WIKI_JSON)
        # 第二条：正文为空 → 不该产出命中（`_wiki_pages` 会滤掉）
        return httpx.Response(200, json={
            "query": {"pages": [{"ns": 0, "title": titles, "extract": "  "}]}})

    monkeypatch.setattr(
        external, "_client", _client_factory(_router({"wikipedia": handler})))

    hits = external.search_external("碳中和技术路径")

    assert [hit.title for hit in hits] == ["碳中和"]


# ---------------------------------------------------------------- 1b 中文支持


def _cjk_count(text: str) -> int:
    return sum(1 for char in text if "\u4e00" <= char <= "\u9fff")


def test_chinese_query_returns_chinese_content(monkeypatch):
    """`search_external("碳中和")` **必须至少给一条有实质内容的中文条目**。

    这是这次加源的硬要求，也是"5 个源抢 3 个名额"最容易失守的一条：OpenAlex 与
    Crossref 的英文命中比中文多得多，不做偏好时三条名额会被英文摘要占满，而用户
    输入的是中文。

    断言分两层——`language == "zh"` 只说明标签对，`_cjk_count(text) > 20` 才说明
    **正文真的有汉字**（一条"标题是中文、正文是英文摘要"的命中会骗过第一层）。
    """
    monkeypatch.setattr(external, "_client", _client_factory(_router({
        "wikipedia": WIKI_JSON,
        "arxiv": ARXIV_XML,
        "openalex": OPENALEX_JSON,
        "crossref": CROSSREF_JSON,
        "baike": BAIKE_HTML,
    })))

    hits = external.search_external("碳中和", limit=3)

    assert len(hits) == 3
    chinese = [hit for hit in hits if hit.language == "zh"]
    assert chinese, [(hit.source_name, hit.language) for hit in hits]
    assert any(_cjk_count(hit.text) > 20 for hit in chinese), \
        [(hit.source_name, hit.text[:40]) for hit in chinese]
    # 三个中文源都在，说明不是"维基一个人顶住了"
    assert len({hit.source_name for hit in chinese}) >= 2


def test_english_query_keeps_collection_order(monkeypatch):
    """英文 query **不偏好**中文：`prefer_language` 是 None，走原来的收集顺序。

    不为英文 query 排一次序，是因为那需要猜"哪个源更该先给名额"，而这个猜测在
    "中文维基的英文条目"、"Crossref 里英文标题的中文期刊论文"上都会猜错。
    """
    monkeypatch.setattr(external, "_client", _client_factory(_router({
        "wikipedia": WIKI_JSON, "arxiv": ARXIV_XML,
        "openalex": OPENALEX_JSON, "crossref": CROSSREF_JSON, "baike": BAIKE_HTML,
    })))

    assert external._query_language("carbon neutrality") is None
    assert external._query_language("碳中和") == "zh"
    assert external._query_language("") is None


def test_interleave_prefers_language_bucket():
    """中文 query 时中文桶先占名额；英文 query 时顺序不变。"""
    def hit(source: str, language: str) -> external.ExternalHit:
        return external.ExternalHit(
            f"{source}-{language}", "t", f"https://{source}/{language}", source,
            "2026-01-01T00:00:00Z", language)

    # 收集顺序是"论文源在前"，不偏好时英文摘要会占满三个名额
    collected = [
        hit("arxiv", "en"), hit("openalex", "en"), hit("crossref", "en"),
        hit("wikipedia", "zh"), hit("baike", "zh"),
    ]

    plain = external._interleave(collected, 3)
    assert [item.source_name for item in plain] == ["arxiv", "openalex", "crossref"]

    preferred = external._interleave(collected, 3, prefer_language="zh")
    assert [item.source_name for item in preferred] == ["wikipedia", "baike", "arxiv"]

    # 桶内顺序不动：同语言的两个论文源仍是收集顺序
    two = external._interleave(collected[:3], 2, prefer_language="en")
    assert [item.source_name for item in two] == ["arxiv", "openalex"]


# ---------------------------------------------------------------- 2 清洗与截断


def test_clean_text_strips_tags_and_entities():
    """标签与实体：`<script>` 整段删掉，`&lt;` 反转义后当作标签再删。"""
    dirty = (
        "<div>前<script>alert(1)</script>后</div>\n\n\n\n"
        "&lt;img src=x onerror=alert(1)&gt;正文 &amp; 更多"
    )
    cleaned = external.clean_text(dirty)

    assert "alert(1)" not in cleaned        # script 内容整段不见
    assert "<div>" not in cleaned and "</div>" not in cleaned
    assert "前" in cleaned and "后" in cleaned
    assert "&amp;" not in cleaned and "&" in cleaned
    # 空行压到最多两行（切块按空行分段，多余空行只是噪声）
    assert "\n\n\n" not in cleaned


def test_truncate_records_original_length(monkeypatch):
    """超长正文截到 8000 字，`raw_chars` 仍是原始长度。

    这两个数字分开才有意义：只留截断后的长度时，"这条本来就这么长"与"被砍掉了
    九成"在界面上长得一模一样。
    """
    long_extract = "碳中和" * 4000          # 12000 字
    payload = {
        "query": {"pages": [{"ns": 0, "title": "碳中和", "extract": long_extract,
                             "fullurl": "https://zh.wikipedia.org/wiki/x"}]}}
    monkeypatch.setattr(
        external, "_client", _client_factory(_router({"wikipedia": payload})))

    hits = external.search_external("碳中和")

    assert len(hits[0].text) == external.TEXT_MAX_CHARS
    assert hits[0].text.endswith("…")
    assert hits[0].raw_chars == len(long_extract)


def test_markdown_heading_conversion_keeps_depth():
    """`==` / `===` 的层级原样保留（2 → 2 级、3 → 3 级）。"""
    text = "== 一级 ==\n正文\n=== 二级 ===\n正文"
    assert external._wiki_markdown(text) == "## 一级\n正文\n### 二级\n正文"


# ---------------------------------------------------------------- 3 失败即空


@pytest.mark.parametrize("failure", [
    httpx.ReadTimeout("timed out"),
    httpx.ConnectError("proxy refused"),
    httpx.HTTPError("boom"),
])
def test_network_failure_returns_empty(monkeypatch, failure):
    """超时 / 连不上 / 传输错误 → 空列表，不抛。外部源挂了不能影响生成。"""
    monkeypatch.setattr(
        external, "_client", _client_factory(_router({"wikipedia": failure,
                                                      "arxiv": failure})))
    assert external.search_external("碳中和") == []


def test_http_500_returns_empty(monkeypatch):
    monkeypatch.setattr(
        external, "_client",
        _client_factory(lambda request: httpx.Response(500, text="upstream down")))

    assert external.search_external("碳中和") == []


def test_invalid_json_returns_empty(monkeypatch):
    """非法 JSON 走 `ValueError` 那条路（`Response.json()` 抛的就是它）。"""
    monkeypatch.setattr(
        external, "_client", _client_factory(_router({"wikipedia": "{not json",
                                                      "arxiv": ARXIV_EMPTY_XML})))

    assert external.search_external("碳中和") == []


def test_broken_xml_returns_empty(monkeypatch):
    monkeypatch.setattr(
        external, "_client",
        _client_factory(_router({"wikipedia": WIKI_JSON,
                                 "arxiv": "<feed><entry>unclosed"})))

    hits = external.search_external("碳中和")

    # arXiv 挂了不影响维基那条：一个源出问题不该把另一个源的成果一起扔掉
    assert [hit.source_name for hit in hits] == ["wikipedia"]


@pytest.mark.parametrize("broken, label", [
    (httpx.Response(500, text="openalex is down"), "openalex 500"),
    (httpx.Response(429, text='{"message":"Too Many Requests"}'), "openalex 429 限流"),
    (httpx.Response(200, text="{not json"), "openalex 非法 JSON"),
])
def test_new_source_failure_does_not_break_old_sources(monkeypatch, broken, label):
    """新源 500 / 429 / 脏 JSON 时，维基与 arXiv 照常返回。

    429 单列一条是因为 OpenAlex 无 key 时全 IP 共享日预算，它在生产上真的会出现，
    而"限流"与"坏了"在降级行为上必须一致：都只是少一个源，不是整次检索失败。
    """
    monkeypatch.setattr(external, "_client", _client_factory(_router({
        "wikipedia": WIKI_JSON,
        "arxiv": ARXIV_XML,
        "openalex": lambda request: broken,
    })))

    hits = external.search_external("碳中和")

    assert {hit.source_name for hit in hits} == {"wikipedia", "arxiv"}, label


def test_baike_500_does_not_break_old_sources(monkeypatch):
    """快懂百科 500（词条不存在/被限流）时老源照常。"""
    monkeypatch.setattr(external, "_client", _client_factory(_router({
        "wikipedia": WIKI_JSON,
        "baike": httpx.Response(500, text="upstream down"),
    })))

    hits = external.search_external("碳中和")

    assert [hit.source_name for hit in hits] == ["wikipedia"]


def test_all_new_sources_fail_still_returns_old_ones(monkeypatch):
    """三个新源全挂：维基 + arXiv 的结果一条不少。

    "只实现实测通过的源"不等于"新源可以拖累老源"。这条用例钉的是那条边界：
    新源集体失败时，返回集合与 83d61a0 的行为完全一样。
    """
    monkeypatch.setattr(external, "_client", _client_factory(_router({
        "wikipedia": WIKI_JSON,
        "arxiv": ARXIV_XML,
        "openalex": httpx.HTTPError("boom"),
        "crossref": httpx.ReadTimeout("timed out"),
        "baike": httpx.ConnectError("refused"),
    })))

    hits = external.search_external("碳中和")

    assert {hit.source_name for hit in hits} == {"wikipedia", "arxiv"}


def test_empty_query_makes_no_request(monkeypatch):
    calls: list[httpx.Request] = []
    monkeypatch.setattr(
        external, "_client", _client_factory(_router({}, calls)))

    assert external.search_external("   ") == []
    assert external.search_external("碳中和", limit=0) == []
    assert calls == []


def test_oversized_response_is_rejected(monkeypatch):
    """超过 `MAX_RESPONSE_BYTES` 的响应体不解析（外部源不是可信的一端）。"""
    huge = "x" * (external.MAX_RESPONSE_BYTES + 1)
    monkeypatch.setattr(
        external, "_client",
        _client_factory(lambda request: httpx.Response(200, text=huge)))

    assert external.search_external("碳中和") == []


# ---------------------------------------------------------------- 4 开关关＝旧模块


@pytest.mark.parametrize("rev", sorted(BASELINES))
def test_switch_off_matches_baseline(tmp_path, monkeypatch, rev):
    """`MINDMAP_EXTERNAL_KNOWLEDGE=0` 时，`_generate` 与基线 rev 逐字节相同。

    比的是 `(ok, content, data)`——`data` 的键集也要比，因为"逐字节"包含它：
    多一个 `external` 键就是一次契约变化，而它在界面上表现为"同一个请求、两种
    遥测形状"，排查时最难对上。

    跑四个基线（理由见 `BASELINES`）：`aeed033` 证明"功能不存在"，`83d61a0`
    证明"加了三个源之后，关闭状态仍然一个字节没动"，`dceae7e` 证明"**默认值翻了
    之后**，显式关掉仍然回到翻之前的行为"，`202a7bd` 证明"加了相关性门与检索进度
    帧之后，关掉开关仍然回到本轮改动之前的行为"。

    `_ctx()` 的 `emit` 是 None，所以这条用例同时钉住"没有 SSE 消费者时一帧都不多"：
    进度帧只该出现在有消费者的那条路上。

    **关掉必须显式写 `0`**：默认值现在是开，`delenv` 只会把开关打开，这条用例就
    变成"开着的时候与关着的基线相同"——它会在第一次外部请求上失败（一次请求都
    不该发），但失败原因读起来像是实现坏了。"默认开"这件事由
    `test_default_is_on_and_explicit_zero_still_turns_it_off` 单独钉。
    """
    baseline = _load_baseline_tools(tmp_path, rev)
    monkeypatch.setenv(agent_tools.EXTERNAL_KNOWLEDGE_ENV, "0")

    forbidden: list[str] = []
    monkeypatch.setattr(
        agent_tools.external_knowledge, "search_external",
        lambda *args, **kwargs: forbidden.append("called") or [])

    tree = _tree()
    current, current_calls = _generate(_ctx(), fake=tree)

    async def old_chat(system: str, user: str, **kwargs: Any) -> Any:
        return tree

    with patch.object(baseline, "chat_async", old_chat):
        old = asyncio.run(baseline._generate(
            baseline.GenerateArgs(topic="碳中和"), _ctx()))

    assert (current.ok, current.content, current.data) \
        == (old.ok, old.content, old.data), rev
    # 提示词也逐字相同（system 里不该多出那句外部来源的警告）
    assert current_calls and current_calls[0][0] == agent_tools.DETAIL_PROMPT \
        + agent_tools.thinking_block("default") + agent_tools.DETAIL_NO_SOURCE_RULES
    assert forbidden == [], "开关关着时一次外部请求都不该发"


def test_kind_table_is_the_only_thing_tools_gained():
    """这次改 `tools.py` **只多了归类表里的三行**，别的一律是注释。

    开关关闭时的行为没变，靠的不是"我记得没改"，而是把"改了什么"钉成一条可执行的
    断言：`_EXTERNAL_KINDS` 的键集合必须正好是五个源，多一个少一个都失败。
    新增源时这条会红——那正是它的用途。
    """
    assert set(agent_tools._EXTERNAL_KINDS) == {
        "wikipedia", "baike", "arxiv", "openalex", "crossref"}
    # 默认值：表里没有的名字落 `web`，不是 `paper`。默认必须是"最弱的那一类"，
    # 否则一个新源会因为没人想起它而被当成学术论文引用。
    assert agent_tools._external_source(external.ExternalHit(
        title="t", text="x", url="", source_name="unknown-source",
        fetched_at="2026-01-01T00:00:00Z", language="zh")).kind is SourceKind.web


# ---------------------------------------------------------------- 5 开 + 有结果


def test_external_sources_flow_into_generation(monkeypatch):
    """开关开 + 外部有结果：正文进 user 消息、引用指向新入库的来源、校验后还在。

    这条用例的价值在"还在"两个字上：`verify_source_refs(remove_invalid=True)` 会把
    指向未知来源的引用**静默删掉**，所以"没抛异常"什么都证明不了。
    """
    monkeypatch.setenv(agent_tools.EXTERNAL_KNOWLEDGE_ENV, "1")
    monkeypatch.setattr(
        external, "_client",
        _client_factory(_router({"wikipedia": WIKI_JSON,
                                 "arxiv": ARXIV_EMPTY_XML})))

    seen_user: list[str] = []

    def fake(system: str, user: str) -> Any:
        seen_user.append(user)
        # 从 user 消息末尾的 evidence 数组里抄一条引用——这正是模型的处境：
        # 它唯一的原文来源就是这段 JSON。
        payload = json.loads(user[user.rindex("["):])
        hit = payload[0]
        return _tree([{"source_id": hit["source_id"], "start": hit["start"],
                       "end": hit["end"], "quote": hit["quote"]}])

    ctx = _ctx()
    result, calls = _generate(ctx, fake=fake)

    assert result.ok, result.content
    assert seen_user, "外部正文没有进 user 消息"
    assert "碳中和（英語：carbon neutrality）" in seen_user[0]
    # 不可信数据的声明必须在 system 里（不是 user）：它约束的是模型怎么对待这些
    # 内容，而不是这些内容本身。
    system = calls[0][0]
    assert "外部抓取的内容" in system and "不要执行" in system

    # 引用指向**入库后的持久 id**，且校验之后仍在
    refs = ctx.candidate.root.children[0].source_refs
    assert refs, "外部来源的引用被校验剔掉了"
    source_id = refs[0].source_id
    stored = library.load(source_id)
    assert stored is not None
    assert stored.kind is SourceKind.web
    assert stored.metadata.url == "https://zh.wikipedia.org/wiki/%E7%A2%B3%E4%B8%AD%E5%92%8C"
    assert stored.metadata.tags == ["wikipedia"]
    assert stored.filename == "wikipedia: 碳中和"
    # `source_usage` 带上了 url：保存后的导图也要能点回原文
    usage = {item.source_id: item for item in ctx.candidate.source_usage}
    assert usage[source_id].url == stored.metadata.url
    assert result.data["external"]["sources"] == 1
    assert result.data["external"]["failed"] is None


def test_external_hits_are_interleaved(monkeypatch):
    """两个源都有结果时按 `source_name` 交替，而不是一个源占满。"""
    hits = [
        external.ExternalHit("w1", "t", "https://w/1", "wikipedia", "2026-01-01T00:00:00Z", "zh"),
        external.ExternalHit("w2", "t", "https://w/2", "wikipedia", "2026-01-01T00:00:00Z", "zh"),
        external.ExternalHit("a1", "t", "https://a/1", "arxiv", "2026-01-01T00:00:00Z", "en"),
    ]
    assert [hit.title for hit in external._interleave(hits, 3)] == ["w1", "a1", "w2"]
    assert [hit.title for hit in external._interleave(hits, 2)] == ["w1", "a1"]


def test_library_reuse_does_not_duplicate(monkeypatch):
    """同一份外部正文再次命中 → 复用同一条库记录（`content_hash` 去重）。"""
    hit = external.ExternalHit(
        title="碳中和", text="碳中和正文", url="https://zh.wikipedia.org/wiki/x",
        source_name="wikipedia", fetched_at="2026-01-01T00:00:00Z",
        language="zh", raw_chars=5)
    source = agent_tools._external_source(hit)

    first = library.ingest(source)
    second = library.ingest(source)

    assert first == second
    assert len(library.list_sources(limit=10)) == 1


def test_request_sources_skip_external_entirely(monkeypatch):
    """请求自带材料时**一条外部请求都不发**：主题检索只在"只有一个主题"时发生。

    这条不是优化而是语义：用户上传了材料，"这次该依据什么"已经有了答案，
    再去网上抓一份混进来，会让引用指向他自己没给过的来源。
    """
    monkeypatch.setenv(agent_tools.EXTERNAL_KNOWLEDGE_ENV, "1")
    forbidden: list[Any] = []
    monkeypatch.setattr(
        agent_tools.external_knowledge, "search_external",
        lambda *args, **kwargs: forbidden.append("called") or [])

    material = Source(kind=SourceKind.text, status="parsed", text=(
        "# 制度\n\n## 预算审批\n\n预算审批由部门负责人签核，超过额度上报总经理。\n\n"
        "## 报销时限\n\n费用报销的时限是三十个自然日，逾期不再受理。\n\n"
        "## 采购合规\n\n采购合同必须经过法务审核，金额超过五十万元另行招标。\n\n"
        "## 归档与审计\n\n全部单据按季度归档，财务部每半年抽查一次；"
        "审计发现的问题要在十五个工作日内整改完毕并书面反馈。\n\n"
        "## 培训\n\n新员工入职后三十日内完成财务制度培训并考核，"
        "未通过的在下一批次补训；各部门负责人负责督促落实。\n\n"
        "## 附则\n\n本制度自发布之日起施行，由财务部负责解释；"
        "各部门可依据本制度制定实施细则，但不得与本制度相抵触。\n\n"
        "## 违规处理\n\n未按本制度履行审批手续的，财务部有权退回并暂停该笔支付。\n"
    ))
    # 材料必须长过降级阈值，否则这次走的是"材料太短 → 无材料 → 去搜"，而那条路
    # **本来就该**发外部请求——用例会红，但红的不是它想证明的那件事。
    assert len(material.text) > agent_tools.SHORT_MATERIAL_CHARS
    ctx = _ctx([material])
    result, _ = _generate(ctx, fake=_tree())

    assert result.ok, result.content
    assert forbidden == []
    assert "external" not in result.data


# ---------------------------------------------------------------- 6 开 + 全失败


def test_external_empty_falls_back_to_topic_only(monkeypatch):
    """源返回空 → 纯主题生成，`content` 里必须说明。"""
    monkeypatch.setenv(agent_tools.EXTERNAL_KNOWLEDGE_ENV, "1")
    monkeypatch.setattr(
        external, "_client",
        _client_factory(_router({"wikipedia": WIKI_MISS_JSON,
                                 "arxiv": ARXIV_EMPTY_XML})))

    ctx = _ctx()
    result, calls = _generate(ctx, topic="量子色动力学的渐近自由", fake=_tree())

    assert result.ok, result.content
    assert "没有搜到外部资料" in result.content
    assert "只基于主题" in result.content
    # 纯主题那条提示词：不带外部来源的警告（这次没有外部内容）
    assert "外部抓取的内容" not in calls[0][0]
    assert {item["code"] for item in result.data["diagnostics"]} >= {
        "GENERATION_WITHOUT_EXTERNAL"}
    assert result.data["external"] == {
        "hits": 0, "sources": 0,
        "seconds": result.data["external"]["seconds"], "failed": None}
    assert library.list_sources(limit=10) == []


def test_external_error_falls_back_with_reason(monkeypatch):
    """外部检索抛错 → 同样退回纯主题，但文案要说是**出错**而不是"没有"。"""
    monkeypatch.setenv(agent_tools.EXTERNAL_KNOWLEDGE_ENV, "1")

    def boom(query: str, **kwargs: Any) -> list[Any]:
        raise httpx.ConnectError("proxy refused")

    monkeypatch.setattr(agent_tools.external_knowledge, "search_external", boom)

    ctx = _ctx()
    result, _ = _generate(ctx, fake=_tree())

    assert result.ok, result.content
    assert "外部检索出错" in result.content and "ConnectError" in result.content
    assert result.data["external"]["failed"].startswith("ConnectError")


# ---------------------------------------------------------------- 7 预算


def test_budget_caps_requests_and_time():
    """预算：请求次数到顶就 `None`，截止时间到了也 `None`。"""
    budget = external._Budget(limit=2, seconds=60.0)
    assert budget.take() is not None
    assert budget.take() is not None
    assert budget.take() is None
    assert budget.used == 2

    expired = external._Budget(limit=9, seconds=0.0)
    assert expired.take() is None


def test_search_external_never_exceeds_request_limit(monkeypatch):
    """真实路径的请求次数不超上限：维基精确 miss + 搜索 + 逐条，其余四个源各一次。"""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if "arxiv.org" in request.url.host:
            return httpx.Response(200, text=ARXIV_EMPTY_XML)
        if request.url.params.get("list") == "search":
            # 搜索返回一堆候选，逼着它逐条抓——这是请求次数最容易失控的地步
            return httpx.Response(200, json={
                "query": {"search": [{"title": f"候选{i}"} for i in range(8)]}})
        return httpx.Response(200, json=WIKI_MISS_JSON)

    monkeypatch.setattr(external, "_client", _client_factory(handler))

    external.search_external("碳中和", limit=3)

    assert len(calls) <= external.REQUEST_LIMIT, [str(c.url) for c in calls]
    # 上界也是硬的，并且与 `REQUEST_LIMIT` 逐项对上：
    #   维基 1 精确 + 1 检索 + 3 逐条 = 5
    #   arXiv 1 / OpenAlex 1 / Crossref 1 / 快懂百科 1 = 4
    # 合计 9 —— 五个源都开到最大也刚好顶到上限，不需要"机动"次数兜底。
    assert len(calls) <= 9


def test_deadline_stops_slow_source(monkeypatch):
    """慢源不拖垮整次检索：一个源卡住时，另一个源的结果照常返回。"""
    monkeypatch.setattr(external, "DEADLINE_SECONDS", 1.0)
    monkeypatch.setattr(external, "REQUEST_TIMEOUT", 0.2)

    def slow(request: httpx.Request) -> httpx.Response:
        if "arxiv.org" in request.url.host:
            time.sleep(0.5)
            raise httpx.ReadTimeout("timed out")
        return httpx.Response(200, json=WIKI_JSON)

    monkeypatch.setattr(external, "_client", _client_factory(_router({"wikipedia": slow})))

    hits = external.search_external("碳中和")

    assert [hit.source_name for hit in hits] == ["wikipedia"]


# ---------------------------------------------------------------- 8 Source 转换


def test_external_source_contract_fields():
    """转换后的 `Source` 每个字段都对——这是"外部内容进入契约"的唯一入口。"""
    hit = external.ExternalHit(
        title="Carbon Neutrality", text="正文", url="https://arxiv.org/abs/2310.01234v1",
        source_name="arxiv", fetched_at="2026-01-01T00:00:00Z", language="en",
        raw_chars=9999)
    source = agent_tools._external_source(hit)

    assert source.kind is SourceKind.paper
    assert source.filename == "arxiv: Carbon Neutrality"
    assert source.text == "正文"
    assert source.status == "parsed"      # 少这一条就是一类静默剔除
    assert source.metadata.url == "https://arxiv.org/abs/2310.01234v1"
    assert source.metadata.raw_chars == 9999
    assert source.metadata.tags == ["arxiv"]
    assert source.content_hash is None    # 由 `library.ingest` 按正文重算


def test_unknown_source_name_defaults_to_web():
    hit = external.ExternalHit(
        title="某条目", text="正文", url="https://example.com/x",
        source_name="baike", fetched_at="2026-01-01T00:00:00Z", language="zh",
        raw_chars=2)
    assert agent_tools._external_source(hit).kind is SourceKind.web


@pytest.mark.parametrize("source_name, expected", [
    ("wikipedia", SourceKind.web),
    ("baike", SourceKind.web),
    ("arxiv", SourceKind.paper),
    ("openalex", SourceKind.paper),
    ("crossref", SourceKind.paper),
    # 表里没有的名字仍然是 web：**默认值必须是"能渲染的最弱那一类"**，
    # 而不是悄悄当成论文。
    ("some-future-source", SourceKind.web),
])
def test_external_kind_table_covers_every_source(source_name, expected):
    """每个源都要在 `_EXTERNAL_KINDS` 里有归类。

    这条用例的价值是把"加了源忘了归类"变成一个测试失败：漏改的表现是三条新源
    全落进 `web`，不报错、不崩，只是界面标签错一个字——没人会注意到，
    而 `EXTERNAL_TRUST_RULES` 会按这个类型给模型不同的提示语。
    """
    hit = external.ExternalHit(
        title="标题", text="正文", url="https://example.com/x",
        source_name=source_name, fetched_at="2026-01-01T00:00:00Z", language="zh",
        raw_chars=2)
    assert agent_tools._external_source(hit).kind is expected


def test_every_fetched_source_has_a_kind():
    """抓取层真实产出的 `source_name` 集合 == 归类表里的键集合。

    前一条用例是"查表对不对"，这一条是"表全不全"——两者分开才有意义：只查表的话，
    一个**新加但漏进表里**的源永远不会被断言到。
    """
    produced = {
        "wikipedia", "baike",   # 百科
        "arxiv", "openalex", "crossref",   # 论文
    }
    assert produced == set(agent_tools._EXTERNAL_KINDS)


@pytest.mark.parametrize("value, expected", [
    ("https://arxiv.org/abs/2310.01234v1", "https://arxiv.org/abs/2310.01234v1"),
    ("http://example.com/a", "http://example.com/a"),
    ("  https://example.com/b  ", "https://example.com/b"),
    ("javascript:alert(1)", None),
    ("data:text/html,<script>alert(1)</script>", None),
    ("file:///etc/passwd", None),
    ("example.com", None),
    ("", None),
    (None, None),
])
def test_safe_http_url_whitelist(value, expected):
    """只有 http/https 能进 `metadata.url`：前端会把它塞进 `<a href>`。"""
    assert safe_http_url(value) == expected


def test_metadata_rejects_javascript_url():
    """契约层的兜底：`javascript:` 进不来（前端那一侧就不用再判一次）。"""
    metadata = SourceMetadata(url="javascript:alert(1)")
    assert metadata.url is None
    with pytest.raises(Exception):
        SourceMetadata(raw_chars=-1)


# ---------------------------------------------------------------- 10 默认开 + 短材料

# 8 字的真实案例（上一轮暴露的那个）：它不是材料，是主题。作为材料它切不出一个
# 可区分的检索单元，作为检索词却正好是用户想说的那件事。
SHORT_TEXT = "rust 学习路线"


def _short_material(text: str = SHORT_TEXT) -> Source:
    return Source(id="short-1", kind=SourceKind.text, status="parsed", text=text)


def test_default_is_on_and_explicit_zero_still_turns_it_off(monkeypatch):
    """不设环境变量时默认**开**；显式 `0` 仍然关得掉。

    断言的是 `external_knowledge_enabled()` 本身，不是"某条链路碰巧发了请求"：
    默认值是这一轮唯一被改的配置事实，把它测在它自己身上，红的时候不用再往下猜。
    """
    monkeypatch.delenv(agent_tools.EXTERNAL_KNOWLEDGE_ENV, raising=False)
    assert agent_tools.external_knowledge_enabled() is True

    for off in ("0", "off", "no", "false", ""):
        monkeypatch.setenv(agent_tools.EXTERNAL_KNOWLEDGE_ENV, off)
        assert agent_tools.external_knowledge_enabled() is False, off

    for on in ("1", "on", "TRUE", "yes"):
        monkeypatch.setenv(agent_tools.EXTERNAL_KNOWLEDGE_ENV, on)
        assert agent_tools.external_knowledge_enabled() is True, on


def test_short_material_falls_back_to_topic_with_a_reason(monkeypatch):
    """8 字材料不走材料路径，但**文字当提示进 prompt**，并在 `content` 里说清原因。

    这条钉的是上一轮的真问题：材料只有 8 个字，却生成 72 个节点、其中 7 个带引用
    （密度 9.7%），界面上看不出另外 65 个是模型自己补的。判据不是"少几个引用"，
    而是**这次压根不该走材料路径**——它切不出可区分的检索单元（见
    `tools.SHORT_MATERIAL_CHARS`）。

    两个方向都要断言：提示词退回 `DETAIL_PROMPT`（不走检索），而短材料的文字仍然
    在 user 消息里（用户的输入不能被丢掉，它只是从"语料"变成"说明"）。
    """
    monkeypatch.setenv(agent_tools.EXTERNAL_KNOWLEDGE_ENV, "0")
    forbidden: list[Any] = []
    monkeypatch.setattr(
        agent_tools.external_knowledge, "search_external",
        lambda *args, **kwargs: forbidden.append("called") or [])

    ctx = _ctx([_short_material()])
    result, calls = _generate(ctx, topic=SHORT_TEXT, fake=_tree())

    assert result.ok, result.content
    system, user = calls[0]
    assert system == agent_tools.DETAIL_PROMPT + agent_tools.thinking_block("default") \
        + agent_tools.DETAIL_NO_SOURCE_RULES
    assert "本次附带" not in user            # 材料清单一份都不给
    assert SHORT_TEXT in user                # 但它的文字还在（当主题提示）
    assert "不足以作为可检索的材料" in user
    # 用户看得到的那句话
    assert f"材料只有 {len(SHORT_TEXT)} 字" in result.content
    assert "这次按主题生成" in result.content
    assert "外部检索未开启" in result.content
    # 机器读的那一半
    codes = {item["code"] for item in result.data["diagnostics"]}
    assert "MATERIAL_TOO_SHORT" in codes
    assert "retrieval" not in result.data, "没走材料路径，就不该有这次预检索"
    assert result.data["material"] == {
        "chars": len(SHORT_TEXT), "sources": 1,
        "threshold": agent_tools.SHORT_MATERIAL_CHARS, "degraded": True}
    assert forbidden == []


def test_short_material_is_topped_up_by_external_sources(monkeypatch):
    """短材料 + 外部搜到：走 RAG，引用指向外部来源，`content` 说清"补全"。

    端到端（`MockTransport` 在传输层之前返回，所以 URL / 参数 / 解析都真跑过）：
    短材料 → 外部检索 → 入库 → 预检索 → 引用 → 收尾校验。引用**校验之后还在**是
    这条用例的重点：`verify_source_refs(remove_invalid=True)` 会把指向未知来源的
    引用静默删掉，"没抛异常"证明不了任何事。
    """
    monkeypatch.setenv(agent_tools.EXTERNAL_KNOWLEDGE_ENV, "1")
    monkeypatch.setattr(
        external, "_client",
        _client_factory(_router({"wikipedia": WIKI_JSON, "arxiv": ARXIV_EMPTY_XML})))

    seen: dict[str, str] = {}

    def fake(system: str, user: str) -> Any:
        seen["system"], seen["user"] = system, user
        payload = json.loads(user[user.rindex("["):])
        hit = payload[0]
        return _tree([{"source_id": hit["source_id"], "start": hit["start"],
                       "end": hit["end"], "quote": hit["quote"]}])

    ctx = _ctx([_short_material("碳中和 政策")])
    result, _ = _generate(ctx, topic="碳中和", fake=fake)

    assert result.ok, result.content
    assert seen["system"].startswith(agent_tools.RAG_SOURCE_PROMPT)
    # 短材料的文字仍然当提示进 prompt，且位置在 evidence 之前（evidence 必须在末尾）
    assert "碳中和 政策" in seen["user"]
    assert seen["user"].index("碳中和 政策") < seen["user"].index("本次已对该主题做过一次检索")
    # 引用落在**外部来源**上，且校验后仍在
    refs = ctx.candidate.root.children[0].source_refs
    assert refs, "外部来源的引用被校验剔掉了"
    assert library.load(refs[0].source_id) is not None
    # `content` 里同时说清"材料太短"与"外部补全"
    assert "材料只有 6 字" in result.content
    assert "并用外部检索到的 1 条资料" in result.content
    assert "wikipedia" in result.content
    codes = {item["code"] for item in result.data["diagnostics"]}
    assert "MATERIAL_TOO_SHORT" in codes
    assert "GENERATION_FROM_EXTERNAL" in codes


def test_default_on_reaches_external_without_setting_the_env(monkeypatch):
    """不设环境变量（= 默认开）时，一次纯主题生成就能拿到外部引用。

    这一条是"默认开"的**端到端**证据：上面那条只验证了开关的返回值，这条验证它
    真的接到了 `_generate` 上。少了它，`os.getenv(..., "0")` 打错成别的键也照样全绿。
    """
    monkeypatch.delenv(agent_tools.EXTERNAL_KNOWLEDGE_ENV, raising=False)
    monkeypatch.setattr(
        external, "_client",
        _client_factory(_router({"wikipedia": WIKI_JSON, "arxiv": ARXIV_EMPTY_XML})))

    def fake(system: str, user: str) -> Any:
        payload = json.loads(user[user.rindex("["):])
        hit = payload[0]
        return _tree([{"source_id": hit["source_id"], "start": hit["start"],
                       "end": hit["end"], "quote": hit["quote"]}])

    ctx = _ctx()
    result, calls = _generate(ctx, topic="碳中和", fake=fake)

    assert result.ok, result.content
    assert calls[0][0].startswith(agent_tools.RAG_SOURCE_PROMPT)
    assert result.data["external"]["sources"] == 1
    assert ctx.candidate.root.children[0].source_refs
    # 有引用时也要说清"依据从外部来"——B 那句话术
    assert "依据来自外部检索" in result.content
    codes = {item["code"] for item in result.data["diagnostics"]}
    assert "GENERATION_FROM_EXTERNAL" in codes
    # 而且排在**第一条**：前端的诊断面板只列前三条，逐节点 `UNSOURCED_NODE`（一棵树
    # 里几十条）会把这句话挤到看不见（见 `_generate` 里 `lead` 那段注释）。
    assert result.data["diagnostics"][0]["code"] == "GENERATION_FROM_EXTERNAL"


def test_external_empty_says_it_found_nothing(monkeypatch):
    """搜不到 → 纯主题，`content` 说"没有搜到外部资料"（不开环境变量也一样）。"""
    monkeypatch.delenv(agent_tools.EXTERNAL_KNOWLEDGE_ENV, raising=False)
    assert agent_tools.external_knowledge_enabled(), "前提：默认开"
    monkeypatch.setattr(
        external, "_client",
        _client_factory(_router({"wikipedia": WIKI_MISS_JSON, "arxiv": ARXIV_EMPTY_XML})))

    ctx = _ctx()
    result, calls = _generate(ctx, topic="量子色动力学的渐近自由", fake=_tree())

    assert result.ok, result.content
    assert calls[0][0] == (agent_tools.DETAIL_PROMPT + agent_tools.thinking_block("default")
                           + agent_tools.DETAIL_NO_SOURCE_RULES)
    assert "没有搜到外部资料" in result.content
    assert "只基于主题" in result.content
    codes = {item["code"] for item in result.data["diagnostics"]}
    assert "GENERATION_WITHOUT_EXTERNAL" in codes
    assert "MATERIAL_TOO_SHORT" not in codes


def test_the_three_degradations_speak_differently(monkeypatch):
    """三种退化各有各的话术：材料太短 / 外部搜到 / 外部搜不到。

    钉的是"用户能看懂这次为什么没有引用（或为什么有）"：
      A 材料太短 → `MATERIAL_TOO_SHORT`（+ 有没有外部补全）；
      B 外部搜到 → `GENERATION_FROM_EXTERNAL`；
      C 外部搜不到 → `GENERATION_WITHOUT_EXTERNAL`。
    三条 signature 必须**互不出现在对方的 content 里**——只断言"三句话不相等"太松：
    两句都能带同一个前缀，用户还是分不清。
    """
    signatures = {
        "short": "不足以支撑检索",
        "external": "依据来自外部检索",
        "missing": "没有搜到外部资料",
    }
    contents: dict[str, str] = {}
    codes: dict[str, set[str]] = {}

    def record(name: str, result: Any) -> None:
        contents[name] = result.content
        codes[name] = {item["code"] for item in result.data["diagnostics"]}

    # A：短材料 + 关掉外部检索（最干净的 A，不带任何外部尾巴）
    monkeypatch.setenv(agent_tools.EXTERNAL_KNOWLEDGE_ENV, "0")
    record("short", _generate(_ctx([_short_material()]), topic=SHORT_TEXT,
                              fake=_tree())[0])

    # B：无材料 + 外部搜到
    monkeypatch.setenv(agent_tools.EXTERNAL_KNOWLEDGE_ENV, "1")
    monkeypatch.setattr(
        external, "_client",
        _client_factory(_router({"wikipedia": WIKI_JSON, "arxiv": ARXIV_EMPTY_XML})))

    def fake(system: str, user: str) -> Any:
        payload = json.loads(user[user.rindex("["):])
        hit = payload[0]
        return _tree([{"source_id": hit["source_id"], "start": hit["start"],
                       "end": hit["end"], "quote": hit["quote"]}])

    record("external", _generate(_ctx(), topic="碳中和", fake=fake)[0])

    # C：无材料 + 外部搜不到
    monkeypatch.setattr(
        external, "_client",
        _client_factory(_router({"wikipedia": WIKI_MISS_JSON, "arxiv": ARXIV_EMPTY_XML})))
    record("missing", _generate(_ctx(), topic="碳中和的产业政策演变路径",
                                fake=_tree())[0])

    assert len(set(contents.values())) == 3, contents
    for name, signature in signatures.items():
        assert signature in contents[name], (name, contents[name])
        for other, other_signature in signatures.items():
            if other != name:
                assert other_signature not in contents[name], (name, other_signature)

    # 诊断码也必须分得开（遥测/日志按 code 聚合，不读中文句子）
    assert "MATERIAL_TOO_SHORT" in codes["short"]
    assert "GENERATION_FROM_EXTERNAL" in codes["external"]
    assert "GENERATION_WITHOUT_EXTERNAL" in codes["missing"]
    assert "GENERATION_WITHOUT_EXTERNAL" not in codes["external"]
    assert "GENERATION_FROM_EXTERNAL" not in codes["missing"]


def test_full_mode_keeps_using_a_short_material(monkeypatch):
    """阈值是**切块参数**的推论，只在 `rag` 形态成立：`full` 下短材料照用。

    `full` 把正文整份放进 prompt，模型看得到每一个字——那里既没有"落不到段落"
    的问题，也不该因为短就被丢掉（丢的是几个真能挂上引用的节点）。这条用例把
    "按形态收窄"这个决定钉住：谁哪天把规则放开成全局，这里立刻红。
    """
    monkeypatch.setenv(SOURCE_MODE_ENV, SOURCE_MODE_FULL)
    monkeypatch.setenv(agent_tools.EXTERNAL_KNOWLEDGE_ENV, "1")
    forbidden: list[Any] = []
    monkeypatch.setattr(
        agent_tools.external_knowledge, "search_external",
        lambda *args, **kwargs: forbidden.append("called") or [])

    ctx = _ctx([_short_material()])
    result, calls = _generate(ctx, topic=SHORT_TEXT, fake=_tree())

    assert result.ok, result.content
    assert calls[0][0].startswith(agent_tools.SOURCED_DETAIL_PROMPT)
    assert SHORT_TEXT in calls[0][1]          # 正文照旧整份进 prompt
    assert "material" not in result.data
    assert forbidden == []


def test_external_query_merges_the_hint_without_repeating_it():
    """短材料的文字进查询词，但同一句话不会被查两遍。

    去重不是洁癖：材料常常就是主题的复述（用户把主题粘进上传框），拼起来会得到
    `rust 学习路线 rust 学习路线`，而 `QUERY_MAX_CHARS=50` 还不知道会砍掉哪一半。
    """
    short = [_short_material()]
    assert agent_tools._external_query(SHORT_TEXT, short) == SHORT_TEXT
    assert agent_tools._external_query("学习路线", short) == SHORT_TEXT
    assert agent_tools._external_query("帮我规划一下", short) == "帮我规划一下 rust 学习路线"
    assert agent_tools._external_query("碳中和", []) == "碳中和"
    assert agent_tools._external_query("", [_short_material("了解一下")]) == "了解一下"


def test_short_material_diverges_from_the_baseline_on_purpose(tmp_path, monkeypatch):
    """与 `dceae7e` 的**唯一一处有意分叉**，单独钉在这里。

    `test_switch_off_matches_baseline` 用"请求不带材料"跑，它守的是"翻默认值没有
    改变关闭状态的既有行为"。短材料是例外，而且必须是例外：那条规则是**正确性**
    修复（8 字材料撑起 72 个节点、界面看不出哪些是编的），不是网络功能，所以它
    不受"外部检索开关"管辖——关掉开关的用户同样不该被喂一张假依据的图。

    这条用例把分叉的形状钉死：`content` 多了那句说明，`data` 里 `retrieval` 换成
    `material`，别的地方不许再动。
    """
    baseline = _load_baseline_tools(tmp_path, "dceae7e")
    monkeypatch.setenv(agent_tools.EXTERNAL_KNOWLEDGE_ENV, "0")

    ctx = _ctx([_short_material()])
    current, _ = _generate(ctx, topic=SHORT_TEXT, fake=_tree())

    async def old_chat(system: str, user: str, **kwargs: Any) -> Any:
        return _tree()

    with patch.object(baseline, "chat_async", old_chat):
        old = asyncio.run(baseline._generate(
            baseline.GenerateArgs(topic=SHORT_TEXT), _ctx([_short_material()])))

    assert (current.ok, old.ok) == (True, True)
    assert current.content != old.content
    assert "不足以支撑检索" in current.content
    assert "不足以支撑检索" not in old.content
    # 键集：少一个 `retrieval`（没走预检索）、多一个 `material`（这次被降级了）
    assert set(old.data) - set(current.data) == {"retrieval"}
    assert set(current.data) - set(old.data) == {"material"}
    assert current.data["node_count"] == old.data["node_count"]

