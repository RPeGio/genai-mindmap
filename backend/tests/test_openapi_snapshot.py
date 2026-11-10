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
