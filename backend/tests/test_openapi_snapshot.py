"""`backend/openapi.json` 与代码一致。

前端类型从这份快照生成，快照一旦过期，前端的类型就跟不上后端 —— 而"类型跟不上"
的表现往往不是报错，是字段被静默丢掉（`source_refs` 和 `note` 都这样丢过一次）。
所以这条用例把"改完契约忘了重新导出"变成一个红灯，而不是留给下次线上排查。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
SNAPSHOT = BACKEND_DIR / "openapi.json"
SCRIPT = BACKEND_DIR / "scripts" / "export_openapi.py"


def _load_export_script():
    spec = importlib.util.spec_from_file_location("export_openapi", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_snapshot_is_up_to_date():
    module = _load_export_script()
    expected = module.render()
    actual = SNAPSHOT.read_text(encoding="utf-8")

    assert actual == expected, (
        "openapi.json 与 app 不一致。重新生成：\n"
        "    cd backend && ./.venv/Scripts/python.exe scripts/export_openapi.py\n"
        "然后在 frontend 目录跑 npm run gen:api 更新类型。"
    )


def test_snapshot_contains_v2_contract():
    document = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    schemas = document["components"]["schemas"]

    node = schemas["Node-Output"]["properties"]
    content = schemas["MindMapContent-Output"]["properties"]

    assert {"id", "label", "children", "note", "source_refs"} <= set(node)
    assert {"title", "root", "schema_version", "layout", "theme", "relations"} <= set(content)
    assert "vertical" in schemas["LayoutKind"]["enum"]
    assert document["paths"], "快照里没有任何接口，导出多半出了问题"


def test_snapshot_covers_all_three_diagram_types():
    """三种图型的顶层结构都在快照里，且保存入口接受它们的联合。

    守这一条与"快照过期"是两件事：这三份 schema 只在**被接口引用**时才进快照
    （`app.openapi()` 不导出孤立模型）。某个契约类哪天不再被引用，它就会静默从快照
    与 `schema.d.ts` 里消失——没有报错，只是前端再也写不出那个类型的代码。把
    "联合少了一个成员"变成红灯，是这条断言的全部价值。

    `anyOf` 的**顺序**也要钉：那是 `DiagramContent` 的 `union_mode="left_to_right"`
    的判别顺序（旧图全是树，排第一位才能一次命中）。
    """
    document = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    schemas = document["components"]["schemas"]

    for name in ("MindMapContent-Output", "FishboneContent-Output", "FlowContent-Output"):
        assert name in schemas, f"{name} 不在快照里：它是不是不再被任何接口引用？"

    content = schemas["SaveRequest"]["properties"]["content"]
    refs = [item["$ref"].rsplit("/", 1)[-1] for item in content["anyOf"]]
    assert refs == [
        "MindMapContent-Input", "FishboneContent-Input", "FlowContent-Input",
    ], "保存入口必须按 树 → 鱼骨 → 流程 的顺序接受三种图型"

    # 三套的必填字段就是判别依据本身：树有 root，鱼骨有 problem，流程有 stages。
    assert schemas["MindMapContent-Output"]["required"] == ["title", "root"]
    assert "problem" in schemas["FishboneContent-Output"]["required"]
    assert "stages" in schemas["FlowContent-Output"]["required"]
