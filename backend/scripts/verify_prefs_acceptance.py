"""验收标准实跑：改一个值 → 生成一次 → `prefs_applied` 非空 → 产物确实变了。

一次性脚本，不是测试（同一件事的断言在 `tests/test_prefs.py` 的三条
`test_acceptance_*` 里）。它存在的理由是把中间值打出来，让"走通了"不用靠推测。

运行：`cd backend ; python scripts/verify_prefs_acceptance.py`
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

# 与 tests/conftest.py 同一套隔离：无凭证、开关全关、库落临时目录。脚本能被反复跑，
# 也就不会碰到仓库里的 backend/data/mindmaps.db。
os.environ["MINDMAP_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "mindmaps.db")
for name in ("DASHSCOPE_API_KEY", "MINDMAP_LLM_API_KEY", "MINDMAP_LLM_BASE_URL",
             "MINDMAP_LLM_MODEL", "MINDMAP_EMBEDDING_API_KEY", "MINDMAP_NEO4J_URI",
             "MINDMAP_NEO4J_PASSWORD"):
    os.environ.pop(name, None)
for name in ("MINDMAP_EXTERNAL_KNOWLEDGE", "MINDMAP_EXTERNAL_DIRECTIONS", "MINDMAP_HYBRID_SEARCH",
             "MINDMAP_RERANK", "MINDMAP_LIBRARY_SEARCH", "MINDMAP_GRAPH_EXTRACTION"):
    os.environ[name] = "0"

from fastapi import FastAPI  # noqa: E402  （必须在 env 设好之后才 import app）
from fastapi.testclient import TestClient  # noqa: E402

from app.agent import runs  # noqa: E402
from app.agent.contracts import Completion, ToolCall  # noqa: E402
from app.agent.router import router as agent_router  # noqa: E402
from app.agent.tools import FISHBONE_PLAN_PROMPT, PLAN_PROMPT  # noqa: E402
from app.core.errors import register_error_handlers  # noqa: E402
from app.prefs import service  # noqa: E402
from app.prefs.router import router as prefs_router  # noqa: E402

api = FastAPI()
register_error_handlers(api)
api.include_router(prefs_router, prefix="/api")
api.include_router(agent_router, prefix="/api")
client = TestClient(api)


def _frames(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for block in text.split("\n\n"):
        line = block.strip()
        if not line.startswith("data: "):
            continue
        payload = line[6:].strip()
        if payload == "[DONE]":
            continue
        out.append(json.loads(payload))
    return out


async def _chat(system: str, user: str, *, max_tokens: int | None = None,
                on_delta: Any = None) -> dict[str, Any]:
    """骨架响应按图型分派：两边的提示词不一样，返回的分支也不一样。"""
    if system.startswith(FISHBONE_PLAN_PROMPT):
        return {"branches": ["人", "机", "料", "法", "环", "测"]}
    if system.startswith(PLAN_PROMPT):
        return {"branches": ["甲", "乙", "丙"]}
    return {"points": ["要点一", "要点二"]}


class OneShot:
    def __init__(self) -> None:
        self.rounds = 0

    async def complete_tools(self, messages, tools, *, budget):
        self.rounds += 1
        if self.rounds > 1:
            return Completion(message={"role": "assistant", "content": "好了"}, finish_reason="stop")
        call = ToolCall(call_id="c-gen", name="mindmap_generate_deep",
                        arguments={"topic": "机器学习", "branches": 6, "points_per_branch": 2})
        return Completion(
            message={"role": "assistant", "content": "生成", "tool_calls": [
                {"id": call.call_id, "type": "function", "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False)}}]},
            tool_calls=[call], finish_reason="tool_calls", usage={"total_tokens": 1})


def generate(run_id: str, body: dict[str, Any]) -> dict[str, Any]:
    with (
        patch("app.agent.service.default_provider", return_value=OneShot()),
        patch("app.agent.tools.chat_async", new=AsyncMock(side_effect=_chat)),
    ):
        response = client.post("/api/agent/runs/stream", json={"run_id": run_id, **body})
    assert response.status_code == 200, response.text
    done = [frame for frame in _frames(response.text) if frame["type"] == "done"]
    return done[-1]["content"]


print("=" * 70)
print("步骤 1  用户在偏好面板里把默认图型改成鱼骨图（同一个 HTTP 接口）")
print("=" * 70)
created = client.put("/api/prefs/diagram_type.default", json={"value": "fishbone"}).json()
print("PUT /api/prefs/diagram_type.default ->", json.dumps(
    {"changed": created["changed"], "pref": created["pref"]}, ensure_ascii=False, indent=2))

print()
print("=" * 70)
print("步骤 2  前端启动拉偏好、预填下拉；这一次请求带的是界面上显示的值")
print("=" * 70)
listed = client.get("/api/prefs").json()
prefill = listed[0]["value"]
print("GET /api/prefs ->", json.dumps(
    [{"key": item["key"], "value": item["value"], "last_applied_at": item["last_applied_at"]}
     for item in listed], ensure_ascii=False))
print("界面上下拉显示：", prefill)

print()
print("=" * 70)
print("步骤 3  生成一次（模型 mock，只看这条链路）")
print("=" * 70)
product = generate("run-acceptance", {
    "goal": "机器学习", "intent": "create",
    "diagram_type": prefill,
    "prefs_applied": [f"diagram_type.default={prefill}"],
})
print("产物 diagram_type =", product["diagram_type"])
print("产物一级分支     =", [child["label"] for child in product["root"]["children"]])

print()
print("=" * 70)
print("步骤 4  runs.request 里留下了什么")
print("=" * 70)
stored = runs.get("run-acceptance")
print("runs.request =", json.dumps(stored.request, ensure_ascii=False, indent=2))

print()
print("=" * 70)
print("步骤 5  面板上这条偏好因此显示什么")
print("=" * 70)
row = {item.key: item for item in service.list_all()}["diagram_type.default"]
print("last_applied_at =", row.last_applied_at, "→ 面板显示：上次生效：", row.last_applied_at[:10])

print()
print("=" * 70)
print("对照组  同一条链路、没有偏好时")
print("=" * 70)
baseline = generate("run-baseline", {"goal": "机器学习", "intent": "create"})
print("产物 diagram_type =", baseline["diagram_type"])
print("产物一级分支     =", [child["label"] for child in baseline["root"]["children"]])
print("runs.request.diagram_type =", json.dumps(
    runs.get("run-baseline").request["diagram_type"]))
print("runs.request.prefs_applied =",
      json.dumps(runs.get("run-baseline").request["prefs_applied"]))

print()
changed = baseline["diagram_type"] != product["diagram_type"]
print("=" * 70)
print(f"结论：产物 {product['diagram_type']} vs {baseline['diagram_type']} → 变了 = {changed}")
print(f"      prefs_applied 非空 = {bool(stored.request['prefs_applied'])}")
print("=" * 70)
