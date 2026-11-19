"""画像方向**按导图隔离**：跨图污染为 0、成本为 0 往返、老数据只读不删。

这一层要钉的是四件事，每一件都对应一个能静默退化回去的地方：

  1. **池键**：`stored(map_id=…)` 是必填的（漏传是 `TypeError`，不是静默的全局），
     且它**不返回**隔离之前那批 `map_id` 为 NULL 的全局方向；
  2. **样本只来自这张图**：两张不同主题的材料算出两批互不相干的方向；
  3. **加分不跨图**：另一张图的命中拿到的画假加分是 **0**（不是"少一点"）；
  4. **零 embedding 往返**：方向只读 `chunk_vectors` 里现成的块向量，
     一次 `embed_texts` 都不发——这正是"默认关"那条成本理由消失的依据。

全部离线：向量手工塞进 `chunk_vectors`（维度缩到 2 维，主题就是两根轴），
`no_real_http` 那条 autouse 闸顺带证明"真的一发请求都没发"。
"""
from __future__ import annotations

import math
from contextlib import closing
from dataclasses import dataclass

import pytest

from app.contracts.source import Source
from app.knowledge import vector_cache
from app.mindmaps import repository
from app.prefs import profile

DIM = 2


def _unit(x: float, y: float) -> list[float]:
    norm = math.sqrt(x * x + y * y)
    return [x / norm, y / norm]


#: 两个主题各一根轴。真实 embedding 里它们之间的余弦约 0（正交），
#: 与实测的"跨主题 0.5 基线"不是一回事——测试要的是**可判定的极端**，
#: 而 0.5 那条基线由 `scripts/measure_profile_isolation.py` 的真跑负责。
AXIS_A = _unit(1.0, 0.0)
AXIS_B = _unit(0.0, 1.0)


def _material(tag: str, units: int = 90) -> str:
    """一段足够长、且**每句都不同**的材料（同一句重复会撞 `text_hash`）。"""
    return "".join(
        f"{tag}主题的第{index}个要点讲的是编号{index}这件事，它与其他要点不重复。"
        for index in range(units))


def _sources(prefix: str, count: int = 3) -> list[Source]:
    return [Source(id=f"{prefix}{index}", filename=f"{prefix}{index}.txt", kind="text",
                   text=_material(f"{prefix}{index}"))
            for index in range(count)]


def _seed(sources: list[Source], vector: list[float]) -> None:
    """把这些材料的**子块**向量塞进 `chunk_vectors`——假装它们被检索过。

    走 `_chunk_tree` 与 `vector_cache.text_hash`，与 `profile.chunk_samples`
    读的是同一套坐标；这也是"零往返"能成立的全部前提。
    """
    from app.knowledge.service import _chunk_tree

    model = "text-embedding-v3"
    items: list[tuple[str, list[float]]] = []
    for source in sources:
        for node in _chunk_tree(source.text):
            if node.level != 1:
                continue
            quote = source.text[node.start:node.end]
            items.append((vector_cache.text_hash(quote), vector))
    vector_cache.put_vectors(items, model)


@pytest.fixture(autouse=True)
def two_dim(monkeypatch):
    """维度缩到 2：`stored` 的维度校验与 `cluster` 都要用同一个数。"""
    monkeypatch.setattr(profile.embedding, "dimension", lambda: DIM)
    yield


@pytest.fixture
def maps(monkeypatch):
    """两张图：A 讲机器学习（轴 A），B 讲碳中和（轴 B）。"""
    a = _sources("ml")
    b = _sources("cn")
    _seed(a, AXIS_A)
    _seed(b, AXIS_B)
    return a, b


# ---------------------------------------------------------------- 池键


def test_pool_key_prefers_map_id_then_run_id():
    """有 `map_id` 用它；没有用 `run:<run_id>`；两边都没有才是全局池。

    "新图还没保存"这个状态会永远存在（每次 create 都是它），所以 `run:` 不是
    过渡形态，是一条常驻分支。
    """
    assert profile.pool_key(map_id="map-1", run_id="run-1") == "map-1"
    assert profile.pool_key(run_id="run-1") == "run:run-1"
    assert profile.pool_key() == ""
    assert profile.pool_key(map_id="", run_id="run-1") == "run:run-1"


def test_stored_requires_an_explicit_pool():
    """`map_id` 没有默认值：漏传是 `TypeError`，不是静默的"读全部池"。

    这是那个 bug 的形状本身——一张图被另一张图的方向加权，而没有任何地方看得出来。
    加一个 `= None` 的默认值，代码里就再也不会有人注意到这件事。
    """
    with pytest.raises(TypeError):
        profile.stored()  # type: ignore[call-arg]


def test_legacy_rows_are_kept_but_never_returned_for_a_pool():
    """隔离之前的全局方向（`map_id` 为 NULL）：留着、给脚本看得见、图读不到。"""
    with closing(repository.connect()) as conn, conn:
        conn.execute(
            "INSERT INTO profile_vectors (id, vector, label, weight, samples,"
            " created_at, updated_at, map_id) VALUES ('d0', ?, '老全局方向', 1.0, 3,"
            " '2026-11-19T00:00:00.000Z', '2026-11-19T00:00:00.000Z', NULL)",
            (vector_cache.pack(AXIS_A),))
        conn.execute(
            "INSERT INTO profile_vectors (id, vector, label, weight, samples,"
            " created_at, updated_at, map_id) VALUES ('run:x#d0', ?, '这张图的方向',"
            " 1.0, 3, '2026-11-19T00:00:00.000Z', '2026-11-19T00:00:00.000Z', 'run:x')",
            (vector_cache.pack(AXIS_B),))

    pool = profile.stored(map_id="run:x")
    assert [row.label for row in pool] == ["这张图的方向"]
    # 全量视图还在：脚本要能回答"老的那批方向还在不在"
    assert {row.label for row in profile.stored(map_id=None)} == {"老全局方向", "这张图的方向"}
    # 显式要"全局池"是第三种取值，与 None（全量）不同
    assert [row.label for row in profile.stored(map_id="")] == ["老全局方向"]


def test_rebuild_clears_only_its_own_pool(maps):
    """给一张图重建方向不能把别的池抹掉——那是隔离最不该有的副作用。"""
    _a, live = maps
    with closing(repository.connect()) as conn, conn:
        conn.execute(
            "INSERT INTO profile_vectors (id, vector, label, weight, samples,"
            " created_at, updated_at, map_id) VALUES ('run:other#d0', ?, '别的图', 1.0, 3,"
            " '2026-11-19T00:00:00.000Z', '2026-11-19T00:00:00.000Z', 'run:other')",
            (vector_cache.pack(AXIS_A),))
    profile.rebuild("run:mine", sources=live)
    assert [row.label for row in profile.stored(map_id="run:other")] == ["别的图"]
    assert profile.stored(map_id="run:mine")


# ---------------------------------------------------------------- 方向只来自这张图


def test_directions_come_only_from_this_maps_materials(maps):
    """图 B 的方向与图 B 的材料同轴，与图 A 的材料正交。

    全同向量只聚出**一个**簇（`cluster` 丢空簇，见 `test_cluster_drops_empty_groups`）
    ——这本身就是"这张图的材料只有一个方向"的正确表达，不是退化。
    """
    _a, b = maps
    directions = profile.build_directions_from_sources(b)
    assert directions, "图 B 有 3 份材料、几十个子块，不该一个方向都没有"
    best = max(directions, key=lambda item: item.weight)
    assert best.vector[1] > 0.99        # 落在轴 B（碳中和）上
    assert abs(best.vector[0]) < 0.01   # 与轴 A（机器学习）正交
    assert best.samples >= profile.MIN_SAMPLES


def test_another_maps_hits_get_exactly_zero_bonus(maps):
    """**跨图污染 = 0**。不是"少一点"，是"一分都没加"。

    这条是整个改动的验收判据。隔离之前，图 A 的命中会撞上"图 A 那个方向"
    （cos 0.9+）而拿到满额加分；隔离之后图 B 的方向池里根本没有那根向量，
    跨图命中与它的余弦掉到跨主题基线（实测 0.49–0.52），过不了 `MIN_COSINE`。
    """
    a, b = maps
    directions_b = profile.build_directions_from_sources(b)

    @dataclass
    class Hit:
        score: float
        quote: str
        source_id: str
        start: int = 0

    # 命中 1：图 A 的材料（应拿 0 分）；命中 2：图 B 的材料（应拿满分档的加分）
    hits = [
        Hit(0.02, "机器学习主题的第1个要点讲的是编号1这件事", "ml0"),
        Hit(0.02, "碳中和主题的第1个要点讲的是编号1这件事", "cn0"),
    ]

    # `_cosines` 会真的去 embed 命中的 quote；这里把它的输出直接钉死，
    # 因为要测的是"门 + 加分"，不是 embedding 本身。
    def fake_cosines(items, vectors):
        return {0: (0.50, 0), 1: (0.96, 0)}      # 跨图 0.50（基线）、本图 0.96

    import app.prefs.profile as module

    original = module._cosines
    module._cosines = fake_cosines
    try:
        result = profile.boost(hits, directions=directions_b)
    finally:
        module._cosines = original

    assert result.bonuses.get(0) is None          # 跨图：一分没有
    assert result.bonuses.get(1, 0) > 0           # 本图：拿到了加分
    assert result.cosines[0] == 0.50


def test_boost_refuses_to_guess_its_directions():
    """`directions` 必填：漏传是 `TypeError`，不是"读全表"。

    它曾经默认 `stored()`——那正是"图 B 被图 A 的方向加权"的入口。
    """
    with pytest.raises(TypeError):
        profile.boost([object()])  # type: ignore[call-arg]


# ---------------------------------------------------------------- 零往返


def test_directions_never_call_embedding(monkeypatch, maps):
    """**零 embedding 往返**：方向只读现成的块向量，一次都不发。

    这条成立了，"默认关"那条成本理由（"每次生成多 N 次往返"）才真正消失。
    `no_real_http` 那条 autouse 闸只是第二道保险：这里直接把调用点换成断言。
    """
    def boom(*_args, **_kwargs):
        raise AssertionError("画像方向不该调 embed_texts——它只读 chunk_vectors")

    monkeypatch.setattr(profile.embedding, "embed_texts", boom)
    _a, b = maps
    directions = profile.build_directions_from_sources(b)
    assert directions


def test_no_materials_means_no_directions_and_a_readable_reason():
    """新图没有材料：没有样本 → 没有方向 → 不加权，并且给一句人话。

    这条**不特判**，它就是"样本不足"那条正常路径；`reason` 是唯一能让用户看出
    "为什么个性化没生效"的东西。
    """
    assert profile.build_directions_from_sources([]) == []

    @dataclass
    class Hit:
        score: float
        quote: str = "一段原文"
        source_id: str = "s1"
        start: int = 0

    result = profile.boost([Hit(0.02)], directions=[])
    assert result.reason
    assert result.bonuses == {}


def test_chunk_samples_share_quota_across_materials(maps):
    """份数轮流取：第一份材料块再多，也不能把另两份的配额挤光。"""
    _a, live = maps
    heavy = _sources("heavy", count=1)[0]
    heavy.text = _material("heavy", units=400)
    _seed([heavy], AXIS_B)
    samples = profile.chunk_samples([heavy, live[0]])
    assert samples
    assert sum(1 for sample in samples if sample.tag.startswith("heavy")) <= len(samples)
    # 两份都在样本里（不是只有第一份）
    assert any(sample.tag.startswith("heavy") for sample in samples)
    assert any(sample.tag.startswith("cn0") for sample in samples)


def test_short_material_still_contributes_one_sample(maps):
    """很短的材料也贡献样本（`_chunk_tree` 给它一个子块，退路是父块）。

    意义不在"短材料更好"，而在**取样本这一步从不因为短就崩**：`_sub_spans` 的
    兜底分支就是为了零宽父块那种边角情形——退回父块最多是这几条命中不了缓存。
    """
    short = Source(id="tiny", filename="tiny.txt", kind="text", text="很短的一句话。")
    _seed([short], AXIS_A)
    samples = profile.chunk_samples([short])
    assert len(samples) == 1
    assert samples[0].tag.startswith("tiny.txt#")


def test_round_robin_is_fair_by_construction():
    """`_round_robin` 的直接用例：三份 2/1/5 条样本、上限 4 → 各取 2/1/1。"""
    groups = [
        [profile.ChunkSample([1.0], "a0"), profile.ChunkSample([1.0], "a1")],
        [profile.ChunkSample([1.0], "b0")],
        [profile.ChunkSample([1.0], f"c{index}") for index in range(5)],
    ]
    got = [sample.tag for sample in profile._round_robin(groups, 4)]
    assert got == ["a0", "b0", "c0", "a1"]
