"""记忆的 HTTP 面：列、加、删、按主题检索。

这一层的价值是**让正确性可以被当场观察**。自动总结在空库上会合法地产出 0 条，
于是在攒出第一条记忆之前，"功能正常"与"功能根本没接上"在外部表现完全一样。
`/search` 把那三件事（有没有被检索到、score 多少、衰减有没有生效）变成一次请求
就能看到的数——下面这几条用例就是那个观察面的回归测试。
"""
from __future__ import annotations

import math
from contextlib import closing
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.errors import register_error_handlers
from app.memory import store
from app.memory.router import router as memory_router
from app.mindmaps import repository

api = FastAPI()
register_error_handlers(api)
api.include_router(memory_router, prefix="/api")
client = TestClient(api)


# ---------------------------------------------------------------- 夹具

def fake_embedding(monkeypatch, vectors: dict[str, list[float]] | None = None):
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


# ---------------------------------------------------------------- 列与加

def test_list_is_empty_on_a_fresh_database():
    """空库 → 空数组（而不是 404）。这条同时是"死表探测器"的基线。"""
    response = client.get("/api/memory")
    assert response.status_code == 200
    assert response.json() == []


def test_add_then_list(monkeypatch):
    fake_embedding(monkeypatch, {"标题倾向去掉人名": BASE})
    created = client.post("/api/memory", json={"text": "标题倾向去掉人名"})
    assert created.status_code == 201
    body = created.json()
    assert body["created"] is True
    assert body["item"]["source"] == store.SOURCE_USER_EXPLICIT
    assert body["item"]["has_vector"] is True

    listed = client.get("/api/memory").json()
    assert [row["text"] for row in listed] == ["标题倾向去掉人名"]


def test_add_is_idempotent_by_text(monkeypatch):
    """同一段文本再写一次返回 201 + `created=false` + 已有那条。

    不返回 409：再写一条一模一样的，语义上就是幂等的 upsert，不是一次冲突。
    """
    fake_embedding(monkeypatch, {"偏好鱼骨图": BASE})
    first = client.post("/api/memory", json={"text": "偏好鱼骨图"}).json()
    second = client.post("/api/memory", json={"text": "偏好鱼骨图"}).json()
    assert second["created"] is False
    assert second["item"]["id"] == first["item"]["id"]
    assert len(client.get("/api/memory").json()) == 1


def test_add_rejects_an_unknown_kind(monkeypatch):
    fake_embedding(monkeypatch)
    response = client.post("/api/memory", json={"text": "一句话", "kind": "opinion"})
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_kind"


def test_add_accepts_a_known_kind(monkeypatch):
    fake_embedding(monkeypatch)
    response = client.post("/api/memory", json={"text": "一句话", "kind": "pattern"})
    assert response.status_code == 201
    assert response.json()["item"]["kind"] == "pattern"


def test_add_without_embedding_still_stores(monkeypatch):
    """向量服务未配置时条目照样写进去，`has_vector=false` 如实暴露。

    "这条以后检索不到"比"这句话丢了"轻得多；而把它藏起来才是真正危险的。
    """
    monkeypatch.delenv("MINDMAP_EMBEDDING_API_KEY", raising=False)
    body = client.post("/api/memory", json={"text": "没有向量的一条"}).json()
    assert body["item"]["has_vector"] is False


def test_delete_reports_404_for_a_missing_item():
    response = client.delete("/api/memory/nope")
    assert response.status_code == 404
    assert response.json()["code"] == "memory_not_found"


def test_delete_then_list(monkeypatch):
    fake_embedding(monkeypatch)
    item = client.post("/api/memory", json={"text": "要删掉的"}).json()["item"]
    assert client.delete(f"/api/memory/{item['id']}").status_code == 204
    assert client.get("/api/memory").json() == []


# ---------------------------------------------------------------- 检索

def test_search_returns_the_four_numbers(monkeypatch):
    """`cosine` / `decay` / `score` / `age_days` 全给出来。

    只返回 `score` 的接口让人无法区分"因为语义不符"与"因为太旧"——两种失败要改的
    常量不是同一个。
    """
    fake_embedding(monkeypatch, {"标题": BASE})
    put("标题倾向去掉人名", BASE)
    body = client.get("/api/memory/search", params={"q": "标题"}).json()
    assert body["hits"], "一条也没命中说明检索没接上"
    hit = body["hits"][0]
    assert hit["cosine"] == pytest.approx(1.0, abs=1e-6)
    assert hit["decay"] == pytest.approx(1.0, abs=1e-6)
    assert hit["score"] == pytest.approx(hit["cosine"] * hit["decay"], abs=1e-9)
    assert hit["age_days"] == pytest.approx(0, abs=1)
    assert body["scanned"] == 1
    assert body["top_k"] == store.TOP_K
    assert body["min_cosine"] == store.MIN_COSINE


def test_search_shows_decay_working(monkeypatch):
    """新旧混排时，`decay` 与 `score` 真的随年龄下降，而**旧的那条仍在结果里**。

    这就是"衰减只影响排序、不删除"那条要求的外部观察面：如果哪天有人把门槛改成
    判 `score`，这条 300 天前的记忆会直接消失，而这个用例会红。
    """
    fake_embedding(monkeypatch, {"偏好": BASE})
    put("新的偏好", BASE, days_ago=0)
    put("很旧的偏好", BASE, days_ago=300)

    body = client.get("/api/memory/search", params={"q": "偏好"}).json()
    assert [hit["text"] for hit in body["hits"]] == ["新的偏好", "很旧的偏好"]
    old = body["hits"][1]
    assert old["decay"] < 0.01
    assert old["score"] < 0.01
    assert old["age_days"] == pytest.approx(300, abs=1)


def test_search_does_not_mark_last_used(monkeypatch):
    """一次人工查询不该冒充"真的被注入过"——那是两件事。"""
    fake_embedding(monkeypatch, {"偏好": BASE})
    item_id = put("被查询的", BASE)
    client.get("/api/memory/search", params={"q": "偏好"})
    with closing(repository.connect()) as conn:
        row = conn.execute(
            f"SELECT last_used_at FROM {store.ITEM_TABLE} WHERE id = ?", (item_id,)
        ).fetchone()
    assert row["last_used_at"] is None


def test_search_reports_scanned_zero_on_an_empty_table(monkeypatch):
    """"表是空的"与"全都不相关"必须分得开：它们要采取的动作完全不同。"""
    fake_embedding(monkeypatch, {"偏好": BASE})
    body = client.get("/api/memory/search", params={"q": "偏好"}).json()
    assert body["hits"] == []
    assert body["scanned"] == 0


def test_search_requires_a_query():
    assert client.get("/api/memory/search").status_code == 422


def test_search_respects_top_k(monkeypatch):
    fake_embedding(monkeypatch, {"偏好": BASE})
    for index in range(5):
        put(f"偏好 {index}", BASE)
    body = client.get("/api/memory/search", params={"q": "偏好", "top_k": 2}).json()
    assert len(body["hits"]) == 2
    assert body["top_k"] == 2
