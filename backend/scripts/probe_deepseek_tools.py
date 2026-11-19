"""探针：DeepSeek 的 `/v1/tools/search` 与 `/v1/tools/fetch` 的真实响应形状。

**只是一次性排障脚本**，不进任何生产路径。它存在的原因：这两个接口的官方文档
没有给出逐字段的响应样例，而接一条新源必须先把字段名钉死——靠猜字段名写出来的
解析器会在上线后安静地永远返回 0 条。

用法：

    python -m scripts.probe_deepseek_tools search "机器学习的主要方向 lang:zh" 3
    python -m scripts.probe_deepseek_tools fetch https://example.com/

key 从 `MINDMAP_DEEPSEEK_TOOLS_API_KEY`（或 `DEEPSEEK_API_KEY`）读，**不写死**。
原始响应存到 `scripts/probe_deepseek_tools.<kind>.json`，正文也一起存——别的脚本
（`measure_external_deepseek.py`）要拿同一份样本复算字数与截断，第二次打网会得到
另一批结果，那就不是同一份样本了。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx

BASE = os.getenv("MINDMAP_DEEPSEEK_TOOLS_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
TIMEOUT = float(os.getenv("MINDMAP_DEEPSEEK_TOOLS_TIMEOUT", "20"))
HERE = Path(__file__).resolve().parent


def api_key() -> str:
    for name in ("MINDMAP_DEEPSEEK_TOOLS_API_KEY", "DEEPSEEK_API_KEY"):
        value = os.getenv(name, "").strip()
        if value:
            return value
    raise SystemExit("没有 key：设置 MINDMAP_DEEPSEEK_TOOLS_API_KEY 或 DEEPSEEK_API_KEY")


def post(path: str, body: dict) -> tuple[int, float, bytes]:
    started = time.perf_counter()
    with httpx.Client(timeout=TIMEOUT, trust_env=False) as client:
        response = client.post(
            BASE + path, json=body,
            headers={"Authorization": f"Bearer {api_key()}",
                     "Content-Type": "application/json"},
        )
    return response.status_code, time.perf_counter() - started, response.content


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    kind = argv[1]
    if kind == "search":
        query = argv[2] if len(argv) > 2 else "机器学习的主要方向 lang:zh"
        count = int(argv[3]) if len(argv) > 3 else 3
        body = {"query": query, "count": count, "need_content": True}
        path = "/tools/search"
    elif kind == "fetch":
        url = argv[2]
        body = {"url": url}
        path = "/tools/fetch"
    else:
        print(__doc__)
        return 2

    status, seconds, raw = post(path, body)
    print(f"status={status} 耗时={seconds:.2f}s 字节={len(raw)}")
    target = HERE / f"probe_deepseek_tools.{kind}.json"
    target.write_bytes(raw)
    print(f"原始响应 → {target}")
    try:
        payload = json.loads(raw)
    except ValueError:
        print(raw[:800].decode("utf-8", "replace"))
        return 1
    # 只打印**结构**：字段名、类型、长度。正文前 80 字单独看，免得刷屏。
    def walk(node, prefix=""):
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{prefix}.{key}" if prefix else key)
        elif isinstance(node, list):
            print(f"{prefix}[] {len(node)} 项")
            if node:
                walk(node[0], f"{prefix}[0]")
        else:
            shown = node if not isinstance(node, str) else (
                f"{node[:80]!r}…({len(node)} 字)" if len(node) > 80 else repr(node))
            print(f"{prefix} = {type(node).__name__} {shown}")
    walk(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
