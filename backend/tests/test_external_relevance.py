"""外部检索的词面相关性门（`external._relevant_hits`）。

这道门的失败形态和别处相反：它**扔的是要变成引用的东西**，所以每条用例都在钉
"扔对了谁"，而不是"没抛异常"。七组：

  1. 真相关留下、明显无关滤掉（报告里那三条反例）；
  2. 全被滤掉时返回空（不存在的词必须一条都没有）；
  3. 大小写：`Rust` 与 `rust` 是同一个词；
  4. 中文判定词块：取词块去虚词后的头 3 个字，"概念 + 限定语"仍能命中核心词条；
  5. 纯 ASCII token：整词比、≥3 字符、以字母开头；
  6. 两类放行：query 切不出词面、跨语言；
  7. 位置：门在聚合之后、截断之前。

最后两条用例钉的是**已知的天花板**（同形异义、简繁差异）：它们现在是红的反面
——用例断言"门在这里判错"，改门时若行为变了，这条会红，提醒把结论一起改掉。

判据的数字依据见 `scripts/measure_external_relevance.py` 的 docstring。
"""
from __future__ import annotations

import pytest

from app.knowledge import external


def _hit(
    title: str,
    text: str = "",
    *,
    source_name: str = "wikipedia",
    language: str = "zh",
    url: str = "https://example.invalid/x",
) -> external.ExternalHit:
    return external.ExternalHit(
        title=title, text=text, url=url, source_name=source_name,
        fetched_at="2026-01-01T00:00:00Z", language=language,
        raw_chars=len(text),
    )


def _titles(query: str, hits: list[external.ExternalHit]) -> list[str]:
    return [hit.title for hit in external._relevant_hits(query, hits)]


# ---------------------------------------------------------------- 1 判对了谁


def test_relevant_survives_and_junk_is_dropped():
    """报告里的第一条反例：`rust 学习路线` 拿到「陳淑樺」与一篇音乐论文。

    两条噪声都**不与 query 共享任何词块**（`rust` 作为 ASCII 词也没出现在它们的
    标题/正文开头），所以必须被扔掉；同时不能再多扔。
    """
    hits = [
        _hit("陳淑樺", "陳淑樺（1958年5月14日—），台灣殿堂級女歌手。1977年畢業於中國文化學院。"),
        _hit("“意识之线”回退：鲁托斯拉夫斯基《威尼斯游戏》中的声音现象学结构",
             "中文 现象学还原强调主体持有与客体世界保持距离的“目光”。"),
        _hit("Rust 学习路线：从入门到所有权",
             "Rust 学习路线通常从 Cargo、所有权与借用检查开始。"),
    ]

    assert _titles("rust 学习路线", hits) == ["Rust 学习路线：从入门到所有权"]


def test_homonym_passes_when_the_word_itself_appears():
    """**天花板**：`Rust 所有权` 那条"农业"只差正文里有没有 `rust` 这个词。

    两条命中是同一件事（小麦锈病），差别只在正文里写没写英文 `rust`：写了就通过，
    没写就挡下。判据只认字符，不认"这里的 rust 是锈病"。
    """
    hits = [
        _hit("农业", "农业（agriculture）……小麦的锈病（rust）会造成严重减产。"),
        _hit("农业（第二版）", "农业包括作物种植、畜牧、渔业养殖，主要产品是食物与纤维。"),
        _hit("Rust (编程语言)", "Rust 的所有权系统在编译期检查借用。"),
    ]

    assert _titles("Rust 所有权", hits) == ["农业", "Rust (编程语言)"]


def test_all_filtered_returns_empty():
    """`zzqx 不存在的词`：一个不存在的词不该有任何"依据"。返回空是正确结果。"""
    hits = [
        _hit("初中英语词汇教学存在的问题及思考", "一直以来，词汇教学得到越来越多专家和学者的重视。"),
        _hit("研究中作者不端行为令人烦恼但长期存在的问题", "本文研究了作者身份不当行为。"),
    ]

    assert external._relevant_hits("zzqx 不存在的词", hits) == []


# ---------------------------------------------------------------- 2 大小写


def test_ascii_token_is_case_insensitive():
    """`Rust` 与 `rust` 是同一个词：`_token_list` 已经把英文词全部小写化。"""
    hits = [_hit("RUST ownership explained", "Ownership is the core idea of Rust.")]

    assert _titles("rust", hits) == ["RUST ownership explained"]
    assert _titles("RUST", hits) == ["RUST ownership explained"]


def test_ascii_token_matches_whole_words_only():
    """整词比：`rust` 不该命中 `trust` / `rusty`（`_token_list` 按词切，不做子串比）。"""
    hits = [_hit("Trust in institutions", "A survey on trust and credibility.")]

    assert external._relevant_hits("rust", hits) == []


# ---------------------------------------------------------------- 3 中文词块


def test_chinese_phrase_keeps_core_entry_for_qualified_query():
    """`碳中和技术路径` 的命中是维基 `碳中和` 词条——这是"概念 + 限定语"的常态。

    整块匹配会把它判成无关（第一版就是这么错的，既有用例
    `test_wikipedia_falls_back_to_search_when_title_misses` 钉着它），所以判据取
    词块去虚词后的头 3 个字：`碳中和`。
    """
    hits = [_hit("碳中和", "碳中和（英語：carbon neutrality）是指国家、企业在一定时间内……")]

    assert _titles("碳中和技术路径", hits) == ["碳中和"]
    assert _titles("碳中和", hits) == ["碳中和"]


def test_leading_function_words_are_skipped():
    """`为什么中文大模型…` 的前 3 个字全是虚词，跳过去之后要求 `中文大`。

    不跳的话判据会去匹配"为什么"，一篇讲足球的文章只要用了这三个字就能混进来。
    """
    hits = [
        _hit("为什么足球走向世界是中国政治文明的标志？", "中国发展最深厚的根源存在于广大民众之中。"),
        _hit("中文大模型的长文本能力评测", "本文评测了中文大模型在长文本任务上的表现。"),
    ]

    assert _titles("为什么中文大模型在长文本任务上表现不稳定", hits) == [
        "中文大模型的长文本能力评测"]


def test_chinese_phrase_must_be_contiguous():
    """不相邻的字不算：`语义` 单独出现不能给"移动语义"的查询背书。"""
    hits = [_hit("语义记忆", "語意記憶（Semantic memory）是一种记忆的型态。")]

    assert external._relevant_hits("C++ 移动语义", hits) == []


# ---------------------------------------------------------------- 4 ASCII token


def test_short_ascii_tokens_are_ignored():
    """1–2 字符的词（`ai`/`c`）不参与判定：它们在中文正文里到处都是。"""
    assert external._phrase_terms("C++") == frozenset()
    assert external._word_terms("C++") == frozenset()   # `_ASCII_WORD_RE` 只切出一个 `c`
    assert external._word_terms("AI 应用") == frozenset()   # `ai` 短于 3 字符


def test_english_words_are_matched_as_tokens():
    """≥3 字符的英文词整词比，标题与正文开头都算。"""
    assert external._word_terms("transformer architecture") == frozenset(
        {"transformer", "architecture"})
    hits = [_hit("On Layer Normalization in the Transformer Architecture",
                 "The Transformer is widely used in natural language processing tasks.")]
    assert _titles("transformer", hits) == [
        "On Layer Normalization in the Transformer Architecture"]


# ---------------------------------------------------------------- 5 两类放行


def test_undecidable_query_lets_everything_through():
    """切不出可比词面时放行：判据说不出话，沉默才对。

    `C++` 只剩一个被 `MIN_ASCII_TERM` 裁掉的 `c`，全滤会让这个主题一条资料都没有。
    """
    hits = [_hit("随便什么", "与查询毫无关系的正文。")]

    assert _titles("C++", hits) == ["随便什么"]
    assert _titles("---", hits) == ["随便什么"]
    assert _titles("2024", hits) == ["随便什么"]


def test_cross_lingual_hit_is_kept():
    """英文 query 配中文正文（`Raft`）：两边没有可比的字符，判据没有表决权。

    不放行时这条会被误杀，名额转手给一条 RANSAC 噪声（实测）。
    """
    hits = [_hit("Raft", "Raft 是一种用于替代 Paxos 的共识算法，目标是提供更清晰的逻辑分工。")]

    assert _titles("consensus algorithm", hits) == ["Raft"]


def test_cross_lingual_does_not_save_same_script_junk():
    """放行只对"书写系统相反"成立：中文 query 配中文正文时判据照样表决。"""
    hits = [_hit("呼吸作用", "呼吸作用是生物体内有机物氧化分解并释放能量的过程。")]

    assert external._relevant_hits("光合作用", hits) == []


# ---------------------------------------------------------------- 6 位置


def _bare_sources(monkeypatch, payload: dict[str, list[external.ExternalHit]]) -> None:
    """把五个源换成固定返回值，绕开 HTTP 与预算。"""
    for name in ("wikipedia", "arxiv", "openalex", "crossref", "baike"):
        monkeypatch.setattr(
            external, f"_{name}",
            lambda q, budget, limit, _n=name: list(payload.get(_n, [])))


def test_filter_happens_before_truncation(monkeypatch):
    """门在聚合之后、截断之前：噪声占满名额时，真相关的候选要能补位。

    先截断再过滤（错误顺序）时，`limit=1` 只会留下那条噪声，然后被滤掉 → 返回空。
    正确顺序返回那条相关的命中。
    """
    _bare_sources(monkeypatch, {
        "wikipedia": [_hit("陳淑樺", "台灣殿堂級女歌手。")],
        "openalex": [_hit("Rust 所有权与借用检查", "所有权是 Rust 的核心概念。",
                          source_name="openalex", language="zh")],
    })

    hits = external.search_external("Rust 所有权", limit=1)

    assert [hit.title for hit in hits] == ["Rust 所有权与借用检查"]


def test_all_sources_filtered_returns_empty_list(monkeypatch):
    """全被滤掉时 `search_external` 返回空列表（不是 None、不是抛错）。"""
    _bare_sources(monkeypatch, {"wikipedia": [_hit("陳淑樺", "台灣殿堂級女歌手。")]})

    assert external.search_external("Rust 所有权") == []


# ---------------------------------------------------------------- 7 已知天花板


def test_traditional_chinese_is_a_known_false_negative():
    """**天花板**：简繁差异会被误杀。

    `人物誌 (用戶體驗)` 就是 `用户画像`（正文里写着"又稱用戶畫像"），但繁体字串
    不包含简体查询，判据判它无关。修它要引入简繁映射表——那是一个新的数据依赖，
    代价大于收益，所以这里据实钉住这个错误：哪天有人真去做了映射，这条会红。
    """
    hits = [_hit("人物誌 (用戶體驗)",
                 "人物誌（英語：Persona）又稱用戶畫像，是一種描繪目標用戶的方法。")]

    assert external._relevant_hits("用户画像", hits) == []


def test_homonym_is_a_known_false_positive():
    """**天花板**：同形异义挡不住，而且同形得越完整越挡不住。

    实测里最刺眼的一条：`最终一致性` 的三条命中中两条是**晶体学**论文，正文写着
    "最终一致性因子为R=0.030"——那是单晶结构精修的 R 因子，与分布式一致性毫无
    关系。整句原样命中，词面判据在这里没有任何可说的话。
    """
    hits = [_hit("一种新颖钒,钨异金属八核簇合物的合成与结构",
                 "对５４９９个可观测反射点最终一致性因子为Ｒ＝０．０３０。")]

    assert _titles("最终一致性", hits) == [
        "一种新颖钒,钨异金属八核簇合物的合成与结构"]


def test_two_character_query_is_judged_by_the_word_alone():
    """**天花板**：两字查询的判定词块就是这两个字，语义无法区分。

    `幻觉` 命中的戏曲论文（"幻觉化"是戏剧理论术语）与精神病学词条共享同一串字符，
    判据只能都留下。
    """
    hits = [_hit("戏曲“幻觉”浅议", "随着对戏剧观、戏曲特征研究的不断深入，“幻觉化”成为重要论题。")]

    assert _titles("幻觉", hits) == ["戏曲“幻觉”浅议"]


def test_gate_is_documented_in_the_measurement_script():
    """判据的实测数字必须留在脚本 docstring 里：改门不改表，表就成了假账。"""
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "scripts" / "measure_external_relevance.py"
    text = script.read_text(encoding="utf-8")

    assert "前缀 3 字" in text and "| 无门 | 60 | 22 | 10/20 | 100% | 0 |" in text


@pytest.mark.parametrize("query", ["碳中和", "rust", "注意力机制"])
def test_gate_never_raises_on_weird_text(query: str):
    """外部正文是不可信数据：空、超长、只有符号都不该让门抛出去。"""
    for hit in (
        _hit("", ""),
        _hit("x", "…" * 5000),
        _hit("的的的", "的了着过是在"),
        _hit("Ω≈ç√", "🙂🙂🙂"),
    ):
        external._relevant_hits(query, [hit])
