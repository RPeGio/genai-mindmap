"""知识库只读入口（`knowledge/shelf.py` + `/api/knowledge/library`）的行为约束。

四组，各自钉一类错误：

1. **列表的口径**。`total` 必须是库里的总数而不是本次返回的条数——写成
   `len(items)` 时列表页会显示"共 50 份"，而库里可能有 137 份，用户据此判断
   "我的材料丢了"。
2. **被引用次数按导图去重**。同一张图里既出现在 `source_usage`、又在多个节点的
   `source_refs` 里，只能算一张。逐次累加会算出"被 5 张导图引用"，而实际上只有
   一张图用过它。
3. **预览要能被识破**。正文超过截断长度时 `truncated` 必须为真，
   `total_chars` 要报全文长度；否则用户会拿前 4000 字当全文。
4. **库坏了不等于入口挂了**。库读不出来时列表返回空 + `total=0`，不抛 500；
   导图表里有一行坏 JSON 时该行跳过、`cited_by` 按 0 计，列表照常返回——
   这条路上"库坏了"的正确表现是一条空列表，而不是整页打不开。预览这条路上
   库故障也不能冒充 404：材料不存在与库读不出来是两件事（503 `library_unavailable`
   vs 404 `not_found`），混在一起会让用户以为材料被删了。

全部离线：`conftest.py` 已清空凭证、开关置 0、数据库落在本次用例的 `tmp_path`。
"""
from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.contracts.mindmap import MindMapContent
from app.contracts.source import Source, SourceKind
from app.knowledge import library, shelf
from app.main import app
from app.mindmaps import repository


def _source(text: str, *, filename: str | None = None) -> Source:
    return Source(kind=SourceKind.text, status="parsed", text=text, filename=filename)


def _material(chapter: int = 1, *, filler: int = 30) -> str:
    """一份长度超过预览上限、能切出多个父块的材料。"""
    body = "\n\n".join(
        f"### 第{chapter}章 第{index}节\n\n本节讨论预算审批的第 {index} 条规则。" for index in range(filler)
    )
    # 重复到远超 PREVIEW_CHARS（4000 字），专门用来验 truncated。
    return f"# 第{chapter}章 预算审批制度\n\n{body}\n\n" + ("补充说明：制度以财务部解释为准。\n\n" * 300)


def _ingest(text: str, *, filename: str | None = None) -> str:
    return library.ingest(_source(text, filename=filename))


def _save_map(*, usage: list[str], refs: list[str] = (), title: str = "用到了这些材料") -> str:
    """存一张引用了给定持久 source_id 的导图。"""
    content = MindMapContent.model_validate(
        {
            "title": title,
            "root": {
                "id": "root",
                "label": "根",
                "children": [
                    {
                        "id": f"child-{index}",
                        "label": f"分支 {index}",
                        "source_refs": [{"source_id": source_id} for source_id in refs],
                    }
                    for index in range(1)
                ],
            },
            "source_usage": [
                {"source_id": source_id, "kind": "text", "char_count": 10}
                for source_id in usage
            ],
        }
    )
    return repository.create_map(content).id


def _insert_raw_map(map_id: str, content: str) -> None:
    """绕过契约直接往导图表塞一行原始 `content`：模拟坏 JSON / 旧版本残留。"""
    with repository.connect() as conn:
        conn.execute(
            "INSERT INTO mindmaps VALUES (?, 1, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
            (map_id, content),
        )


@pytest.fixture
def client():
    with TestClient(app) as api:
        yield api


# ---------------------------------------------------------------- 1 列表口径


def test_empty_library_lists_nothing(client):
    body = client.get("/api/knowledge/library").json()
    assert body == {"items": [], "total": 0}


def test_total_is_the_library_size_not_the_page_size(client):
    ids = [_ingest(_material(chapter)) for chapter in (1, 2, 3)]

    body = client.get("/api/knowledge/library", params={"limit": 2}).json()

    assert len(body["items"]) == 2
    # 库里 3 份、这次只返回 2 份：total 必须是 3，不是 2。
    assert body["total"] == 3
    assert {item["source_id"] for item in body["items"]} <= set(ids)


def test_entry_fields_match_the_stored_material(client):
    source_id = _ingest(_material(1), filename="预算制度.md")

    item = client.get("/api/knowledge/library").json()["items"][0]

    assert item["source_id"] == source_id
    assert item["filename"] == "预算制度.md"
    assert item["kind"] == SourceKind.text.value
    assert item["chars"] > 0
    assert item["chunks"] > 0
    # 没被任何导图引用过：0，而不是缺字段。
    assert item["cited_by"] == 0


# ---------------------------------------------------------------- 2 被引用次数


def test_cited_by_counts_saved_maps(client):
    cited = _ingest(_material(1))
    _save_map(usage=[cited], title="第一张")
    _save_map(usage=[cited], title="第二张")
    _ingest(_material(2))

    items = {item["source_id"]: item for item in client.get("/api/knowledge/library").json()["items"]}

    assert items[cited]["cited_by"] == 2


def test_citation_in_both_usage_and_refs_counts_as_one_map(client):
    """同一张图里 `source_usage` 与节点 `source_refs` 都指向它，只算一张图。"""
    source_id = _ingest(_material(1))
    _save_map(usage=[source_id], refs=[source_id])

    item = client.get("/api/knowledge/library").json()["items"][0]

    assert item["cited_by"] == 1

# ---------------------------------------------------------------- 3 预览与截断


def test_preview_is_truncated_and_says_so(client):
    source_id = _ingest(_material(1))

    body = client.get(f"/api/knowledge/library/{source_id}").json()

    assert body["source_id"] == source_id
    assert body["truncated"] is True
    assert len(body["text"]) == shelf.PREVIEW_CHARS
    assert body["total_chars"] > shelf.PREVIEW_CHARS


def test_short_material_is_not_marked_truncated(client):
    source_id = _ingest("短材料：只有一句话。")

    body = client.get(f"/api/knowledge/library/{source_id}").json()

    assert body["truncated"] is False
    assert body["text"] == "短材料：只有一句话。"
    assert body["total_chars"] == len("短材料：只有一句话。")


def test_unknown_source_is_404(client):
    response = client.get("/api/knowledge/library/没这份")

    assert response.status_code == 404
    body = response.json()
    # 统一错误体：`code` / `message` 在顶层，前端读的是这两项。
    assert body["code"] == "not_found"
    assert body["message"] == "这份来源不在知识库里"


def test_listing_does_not_touch_last_used_at(client):
    """看列表不等于用到材料：`last_used_at` 是 `prune` 的保留依据，不能因浏览而改。"""
    source_id = _ingest(_material(1))
    before = library.list_sources()[0]["last_used_at"]
    _stamp(source_id, "2020-01-01T00:00:00.000Z")

    client.get("/api/knowledge/library")

    assert library.list_sources()[0]["last_used_at"] == "2020-01-01T00:00:00.000Z"
    assert before != "2020-01-01T00:00:00.000Z"


def _stamp(source_id: str, stamp: str) -> None:
    with repository.connect() as conn:
        conn.execute("UPDATE library_sources SET last_used_at = ? WHERE id = ?", (stamp, source_id))


# ---------------------------------------------------------------- 4 库坏了


def test_listing_survives_a_broken_library(client, monkeypatch):
    """库读不出来时列表空着返回，不把入口整个打成 500。"""
    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("no such table: library_sources")

    monkeypatch.setattr(library, "list_sources", boom)

    response = client.get("/api/knowledge/library")

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0}


def test_broken_library_on_preview_is_503_not_404(client, monkeypatch):
    """库本身读不出来 ≠ 这份材料不存在：前者 503，后者 404。

    原来的实现把 sqlite 故障翻成 `SourceNotFound`，前端于是说"这份来源不在知识
    库里"——用户会以为材料被删了，怎么重试都没用。真正的 404 由
    `test_unknown_source_is_404` 覆盖。
    """
    def boom(source_id):
        raise sqlite3.OperationalError("no such table: library_sources")

    monkeypatch.setattr(library, "load", boom)

    response = client.get("/api/knowledge/library/任意id")

    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "library_unavailable"
    assert body["message"] == "知识库暂时读不出来，请稍后重试"


def test_listing_survives_a_corrupt_map_row(client):
    """导图表里有一行坏 JSON：该行跳过，健康的那张照常计数，入口仍是 200。

    这是 `_cited_counts` docstring 里的承诺：坏数据只该让"它自己那张图"消失，
    不能把整个知识库列表打成 500。
    """
    cited = _ingest(_material(1))
    _save_map(usage=[cited], title="健康的一张")
    _insert_raw_map("broken-json", "{这不是 JSON")

    response = client.get("/api/knowledge/library")

    assert response.status_code == 200
    items = {item["source_id"]: item for item in response.json()["items"]}
    assert items[cited]["cited_by"] == 1


def test_corrupt_map_row_still_surfaces_in_strict_mode(client):
    """`tolerant` 只给旁路统计用：列表页/单图读取走的严格模式仍然要抛。"""
    _save_map(usage=[])
    _insert_raw_map("broken-json", "{这不是 JSON")

    with pytest.raises(json.JSONDecodeError):
        repository.list_maps()
    # 同一批数据在容错模式下只剩健康的那张。
    assert len(repository.list_maps(tolerant=True)) == 1


def test_listing_survives_a_broken_mindmap_table(client, monkeypatch):
    """导图表整个读不出来时，列表照常返回，只是 `cited_by` 一律 0。"""
    _ingest(_material(1))

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("no such table: mindmaps")

    monkeypatch.setattr(repository, "list_maps", boom)

    response = client.get("/api/knowledge/library")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["cited_by"] == 0