"""把后端的 OpenAPI 文档导出成仓库里的快照 `backend/openapi.json`。

为什么要有这个快照：前端的契约类型（`frontend/src/api/schema.d.ts`）由它生成，
生成结果要提交、要在 CI 里比对。若每次都靠"起一个服务再抓 `/openapi.json`"，
CI 就多一个可能挂的环节，而且快照本身没法 review——契约改了什么，看 diff 最清楚。

用法（在 `backend` 目录下）：

    ./.venv/Scripts/python.exe scripts/export_openapi.py

生成结果必须与 `app.openapi()` 一致，`tests/test_openapi_snapshot.py` 会核对；
改完契约忘了更新快照时，那条用例会失败并提示重跑本脚本。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from app.main import app  # noqa: E402

SNAPSHOT = BACKEND_DIR / "openapi.json"


def render() -> str:
    """序列化成稳定的字符串：字段顺序由 pydantic 决定，不做 sort_keys。

    `sort_keys` 会把 `paths` 的接口顺序也打乱，那样每次加接口都会产生
    一段与真实改动无关的 diff，review 时反而更难看出契约变了什么。
    """
    document = app.openapi()
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def main() -> int:
    SNAPSHOT.write_text(render(), encoding="utf-8", newline="\n")
    print(f"wrote {SNAPSHOT.relative_to(BACKEND_DIR.parent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
