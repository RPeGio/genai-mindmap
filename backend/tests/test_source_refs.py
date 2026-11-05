"""节点引用的三层校验与 API 闭环；所有模型调用均为 mock。"""
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from app.contracts.mindmap import MindMapContent, Node
from app.contracts.source import Source, SourceRef, SourceUsage
from app.generation.service import generate
from app.main import app
from app.mindmaps import repository
from app.optimization.service import check
from app.optimization.source_refs import verify_source_refs


@pytest.fixture(autouse=True)
def _isolate_nli(monkeypatch):
    """这些用例断言的是前两层的行为，必须把可选的第三层关掉。

    NLI 依赖外部模型文件，是否启用取决于运行环境。不隔离的话，同一份代码
    在有模型和无模型的机器上跑出两套诊断，测试就成了环境的探测器而不是
    代码的守门人。第三层自己的用例在 tests/test_nli.py。
    """
    monkeypatch.setenv("MINDMAP_NLI_ENABLED", "0")
    from app.optimization import nli

    nli.reset()
    yield
    nli.reset()


@pytest.fixture
def sources():
    return [
        Source(id="s1", kind="text", text="  机器学习🤖用于识别图像。\n", status="parsed"),
        Source(id="s2", kind="markdown", filename="会议.md", text="会议决定下周发布。", status="parsed"),
    ]


@pytest.fixture
def tree(sources):
    return {
        "title": "材料摘要",
        "root": {"id": "r", "label": "材料摘要", "source_refs": [
            {"source_id": "s1", "quote": "机器学习"},
            {"source_id": "s2", "quote": "下周发布"},
        ], "children": [
            {"id": "a", "label": "图像识别", "source_refs": [
                {"source_id": "s1", "quote": "识别图像"},
            ]},
            {"id": "b", "label": "发布安排", "source_refs": [
                {"source_id": "s2", "quote": "下周发布"},
            ]},
        ]},
        "source_usage": [
            SourceUsage(source_id=s.id, kind=s.kind, filename=s.filename,
                        char_count=len(s.text.strip())).model_dump(mode="json") for s in sources
        ],
    }


@pytest.fixture
def client(monkeypatch):
    # 使用唯一的临时数据库文件，既不触碰开发库，也不依赖临时目录的 ACL。
    data = Path(__file__).resolve().parents[1] / "data"
    data.mkdir(exist_ok=True)
    db_file = data / f"source-refs-{uuid4().hex}.db"
    monkeypatch.setenv("MINDMAP_DB_PATH", str(db_file))
    monkeypatch.setenv("MINDMAP_LLM_MODE", "model")
    try:
        with TestClient(app) as api:
            yield api
    finally:
        db_file.unlink(missing_ok=True)


@pytest.mark.parametrize("fields", [
    {"source_id": ""}, {"source_id": " \n"}, {"source_id": "x" * 101},
    {"quote": "x" * 301}, {"unknown": 1},
    {"page": 0}, {"page": -1}, {"page": True}, {"page": 1.5}, {"page": "1"},
    {"start": -1, "end": 1}, {"start": 0}, {"end": 1},
    {"start": 1, "end": 1}, {"start": 2, "end": 1},
    {"start": False, "end": 1}, {"start": 0, "end": 1.5},
])
def test_reference_field_constraints(fields):
    with pytest.raises(ValidationError):
        SourceRef.model_validate({"source_id": "s1", **fields})


def test_blank_quotes_and_reference_limits():
    assert SourceRef(source_id="s1", quote=" \n").quote is None
    assert SourceRef(source_id="s1", quote=" 引文\n").quote == " 引文\n"
    ref = SourceRef(source_id="s1", page=1, start=0, end=1, quote="文")
    assert len(Node(id="n", label="节点", source_refs=[ref] * 20).source_refs) == 20
    with pytest.raises(ValidationError):
        Node(id="n", label="节点", source_refs=[ref] * 21)


def test_legacy_tree_has_independent_empty_reference_lists():
    content = MindMapContent(title="旧导图", root={"id": "r", "label": "根", "children": [
        {"id": "c", "label": "子"},
    ]})
    assert content.source_usage == []
    assert content.root.source_refs == content.root.children[0].source_refs == []
    assert content.root.source_refs is not content.root.children[0].source_refs


def test_referential_integrity_applies_to_nested_nodes(tree):
    tree["root"]["children"][0]["children"] = [
        {"id": "deep", "label": "深层", "source_refs": [{"source_id": "invented"}]},
    ]
    with pytest.raises(ValidationError, match="不在 source_usage 中"):
        MindMapContent.model_validate(tree)


def test_duplicate_source_summaries_are_rejected(tree):
    tree["source_usage"].append(tree["source_usage"][0])
    with pytest.raises(ValidationError, match="来源 ID 不能重复"):
        MindMapContent.model_validate(tree)


def test_quotes_and_unicode_offsets_match_the_original_text(tree, sources):
    quote = "🤖用于识别图像"
    start = sources[0].text.index(quote)
    tree["root"]["children"][0]["source_refs"] = [
        {"source_id": "s1", "start": start, "end": start + len(quote), "quote": quote},
        {"source_id": "s1", "start": 0, "end": len(sources[0].text)},
    ]
    content = MindMapContent.model_validate(tree)
    before = content.model_dump()
    assert verify_source_refs(content, sources) == []
    assert content.model_dump() == before


@pytest.mark.parametrize("ref,code", [
    ({"source_id": "s1", "quote": "下周发布"}, "SOURCE_QUOTE_MISMATCH"),
    ({"source_id": "s1", "quote": "根本没有这句话"}, "SOURCE_QUOTE_MISMATCH"),
    ({"source_id": "s1", "start": 0, "end": 2, "quote": "机器学习"}, "EMPTY_SOURCE_EXCERPT"),
    ({"source_id": "s1", "start": 2, "end": 6, "quote": "识别图像"}, "SOURCE_QUOTE_MISMATCH"),
    ({"source_id": "s1", "start": 0, "end": 999}, "SOURCE_RANGE_OUT_OF_BOUNDS"),
    ({"source_id": "s1"}, "UNVERIFIED_SOURCE_REF"),
])
def test_bad_evidence_is_reported_without_editing_the_map(tree, sources, ref, code):
    tree["root"]["children"][0]["source_refs"] = [ref]
    content = MindMapContent.model_validate(tree)
    before = content.model_dump()
    diagnostics = verify_source_refs(content, sources)
    assert [(d.code, d.node_id) for d in diagnostics] == [(code, "a"), ("UNSOURCED_NODE", "a")]
    assert content.model_dump() == before


def test_invalid_reference_does_not_discard_valid_sibling_reference(tree, sources):
    tree["root"]["children"][0]["source_refs"].append({"source_id": "s2", "quote": "识别图像"})
    content = MindMapContent.model_validate(tree)
    diagnostics = verify_source_refs(content, sources, remove_invalid=True)
    assert [(d.code, d.node_id) for d in diagnostics] == [("SOURCE_QUOTE_MISMATCH", "a")]
    assert [r.source_id for r in content.root.children[0].source_refs] == ["s1"]


@pytest.mark.parametrize("status,text", [("pending", "原文"), ("failed", "原文"), ("parsed", None)])
def test_unavailable_source_is_not_treated_as_verified(tree, sources, status, text):
    sources[0].status = status
    sources[0].text = text
    diagnostics = verify_source_refs(MindMapContent.model_validate(tree), sources)
    assert any(d.code == "SOURCE_TEXT_UNAVAILABLE" and d.node_id == "a" for d in diagnostics)
    assert any(d.code == "UNSOURCED_NODE" and d.node_id == "a" for d in diagnostics)


def test_missing_original_text_and_omitted_sources_are_distinguished(tree):
    content = MindMapContent.model_validate(tree)
    assert check(content) == []
    diagnostics = check(content, [])
    assert {d.code for d in diagnostics} == {"SOURCE_TEXT_UNAVAILABLE", "UNSOURCED_NODE"}
    assert {d.node_id for d in diagnostics if d.code == "UNSOURCED_NODE"} == {"r", "a", "b"}


@pytest.mark.parametrize("page,code", [(1, "SOURCE_PAGE_UNVERIFIED"), (2, "INVALID_SOURCE_PAGE"),
                                       (4, "INVALID_SOURCE_PAGE")])
def test_page_claims_are_not_confused_with_quote_matching(tree, sources, page, code):
    sources[0] = Source(id="s1", kind="document", text=sources[0].text,
                        status="parsed", span={"page": 3}, failed_pages=[2])
    tree["root"]["children"][0]["source_refs"][0]["page"] = page
    diagnostics = verify_source_refs(MindMapContent.model_validate(tree), sources)
    assert any(d.code == code and d.node_id == "a" for d in diagnostics)


def test_pipeline_prompts_with_source_boundaries_and_rebuilds_summary(client, tree, sources):
    tree["source_usage"] = {"fabricated": "even invalid metadata must not be trusted"}
    tree["root"]["children"][0]["source_refs"].extend([
        {"source_id": "invented", "quote": "伪造来源"},
        {"source_id": "s2", "quote": "识别图像"},
    ])
    original = deepcopy(tree)
    with patch("app.generation.service.chat", return_value=tree) as chat:
        response = client.post("/api/pipeline/generate", json={
            "sources": [s.model_dump(mode="json") for s in sources],
        })
    assert response.status_code == 200, response.text
    body = response.json()
    content = MindMapContent.model_validate(body["content"])
    assert [s.source_id for s in content.source_usage] == ["s1", "s2"]
    assert content.source_usage[1].filename == "会议.md"
    assert content.source_usage[0].char_count == len(sources[0].text.strip())
    assert content.root.children[0].source_refs == [SourceRef(source_id="s1", quote="识别图像")]
    assert {(d["code"], d["node_id"]) for d in body["diagnostics"]} == {
        ("UNKNOWN_SOURCE_REF", "a"), ("SOURCE_QUOTE_MISMATCH", "a"),
    }
    assert [stage["stage"] for stage in body["stages"]] == ["understand", "outline", "validate", "verify"]
    system, user = chat.call_args.args
    assert "source_refs" in system and "quote" in system
    assert json.loads(user) == {"sources": [{"source_id": s.id, "text": s.text} for s in sources]}
    assert tree == original


def test_pipeline_clears_unfounded_citation_but_keeps_node(client, tree, sources):
    tree["root"]["children"][0]["source_refs"][0]["quote"] = "凭空添加的内容"
    with patch("app.generation.service.chat", return_value=tree):
        response = client.post("/api/pipeline/generate", json={
            "sources": [s.model_dump(mode="json") for s in sources],
        })
    body = response.json()
    assert response.status_code == 200, response.text
    assert body["content"]["root"]["children"][0]["id"] == "a"
    assert body["content"]["root"]["children"][0]["source_refs"] == []
    assert {(d["code"], d["node_id"]) for d in body["diagnostics"]} == {
        ("SOURCE_QUOTE_MISMATCH", "a"), ("UNSOURCED_NODE", "a"),
    }


def test_pipeline_invalid_reference_shape_is_an_upstream_error(client, tree, sources):
    tree["root"]["source_refs"][0]["start"] = -1
    with patch("app.generation.service.chat", return_value=tree):
        response = client.post("/api/pipeline/generate", json={
            "sources": [s.model_dump(mode="json") for s in sources],
        })
    assert response.status_code == 502
    assert response.json()["request_id"]


def test_demo_does_not_invent_references(monkeypatch, sources):
    monkeypatch.setenv("MINDMAP_LLM_MODE", "demo")
    with patch("app.generation.service.chat") as chat:
        result = generate("材料摘要", sources=sources)
    chat.assert_not_called()
    assert result.content.root.source_refs == result.content.root.children[0].source_refs == []
    assert [d.code for d in result.diagnostics] == ["UNSOURCED_NODE", "UNSOURCED_NODE"]


def test_pipeline_limit_counts_original_whitespace(client):
    with patch("app.generation.service.chat") as chat:
        response = client.post("/api/pipeline/generate", json={"sources": [
            {"id": "s1", "kind": "text", "status": "parsed", "text": " " * 20000 + "字"},
        ]})
    assert response.status_code == 422
    chat.assert_not_called()


@pytest.mark.parametrize("path", ["/api/optimization/check", "/api/optimization/suggest"])
def test_diagnostic_endpoints_accept_sources_and_remain_read_only(client, tree, sources, path):
    tree["root"]["children"][0]["source_refs"][0]["quote"] = "不在原文中"
    saved = client.post("/api/mindmaps", json={"content": tree})
    assert saved.status_code == 201
    payload = {"content": tree, "sources": [s.model_dump(mode="json") for s in sources]}
    response = client.post(path, json=payload)
    assert response.status_code == 200
    assert {d["code"] for d in response.json()["diagnostics"]} == {"SOURCE_QUOTE_MISMATCH", "UNSOURCED_NODE"}
    if path.endswith("suggest"):
        assert response.json()["suggestions"] == []
    assert client.get("/api/mindmaps/" + saved.json()["id"]).json() == saved.json()
    assert client.post(path, json={"content": tree}).json()["diagnostics"] == []
    payload["sources"].append(payload["sources"][0])
    assert client.post(path, json=payload).status_code == 422


def test_reference_save_load_update_list_and_conflict(client, tree, sources):
    quote = "机器学习"
    start = sources[0].text.index(quote)
    tree["root"]["source_refs"][0].update(start=start, end=start + len(quote), page=1)
    created = client.post("/api/mindmaps", json={"content": tree})
    assert created.status_code == 201
    record = created.json()
    path = "/api/mindmaps/" + record["id"]
    assert client.get(path).json() == record
    assert client.get("/api/mindmaps").json() == [record]
    content = record["content"]
    content["root"]["label"] = "修改标题保留引用"
    updated = client.put(path, json={"version": 1, "content": content})
    assert updated.status_code == 200
    assert updated.json()["version"] == 2
    assert updated.json()["content"] == content
    assert client.put(path, json={"version": 1, "content": content}).status_code == 409
    assert client.get(path).json() == updated.json()


def test_bad_save_and_update_do_not_write_dangling_references(client, tree):
    created = client.post("/api/mindmaps", json={"content": tree}).json()
    path = "/api/mindmaps/" + created["id"]
    tree["root"]["children"][1]["source_refs"][0]["source_id"] = "unknown"
    for response in (
        client.post("/api/mindmaps", json={"content": tree}),
        client.put(path, json={"version": 1, "content": tree}),
    ):
        assert response.status_code == 422
        assert response.json()["code"] == "validation_error"
    assert client.get(path).json() == created
    assert client.get("/api/mindmaps").json() == [created]


def test_existing_database_records_load_without_migration(client):
    legacy = {"title": "旧导图", "root": {"id": "r", "label": "根", "children": [
        {"id": "c", "label": "子", "children": []},
    ]}}
    conn = repository.connect()
    try:
        with conn:
            conn.execute("INSERT INTO mindmaps VALUES (?, ?, ?, ?)",
                         ("legacy", 1, json.dumps(legacy), "2026-01-01T00:00:00Z"))
    finally:
        conn.close()
    response = client.get("/api/mindmaps/legacy")
    assert response.status_code == 200
    content = response.json()["content"]
    assert content["root"]["source_refs"] == content["root"]["children"][0]["source_refs"] == []
    assert content["source_usage"] == []
    assert client.put("/api/mindmaps/legacy", json={"version": 1, "content": content}).status_code == 200
