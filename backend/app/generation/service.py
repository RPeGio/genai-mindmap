"""本地演示和 OpenAI Chat Completions 兼容服务接入。"""
from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4
from pydantic import ValidationError
from app.contracts.mindmap import GenerateResponse, MindMapContent, Node


class GenerationError(Exception):
    pass


def generate(text: str) -> GenerateResponse:
    mode = os.getenv("MINDMAP_LLM_MODE", "demo").strip().lower()
    if mode == "demo":
        # 显式的占位内容，不伪装为模型生成。
        label = text[:60]
        content = MindMapContent(title=label, root=Node(id=str(uuid4()), label=label,
            children=[Node(id=str(uuid4()), label="请编辑此演示节点")]))
        return GenerateResponse(mode="demo", content=content)
    if mode != "model":
        raise GenerationError("MINDMAP_LLM_MODE 只能是 demo 或 model")
    base = os.getenv("MINDMAP_LLM_BASE_URL", "").strip().rstrip("/")
    key = os.getenv("MINDMAP_LLM_API_KEY", "").strip()
    model = os.getenv("MINDMAP_LLM_MODEL", "").strip()
    if not base.startswith(("https://", "http://")) or not key or not model:
        raise GenerationError("请在后端环境变量中配置模型地址、密钥和模型名称")
    prompt = ("将用户输入整理成思维导图。只返回一个 JSON 对象，不要代码围栏。"
              "格式为 {\"title\":\"标题\",\"root\":{\"id\":\"唯一id\","
              "\"label\":\"节点文字\",\"children\":[]}}。每个节点均含 id、label、children，"
              "ID 全局唯一，层级不超过 8，节点不超过 300。不要捏造输入中没有的具体事实。")
    payload = json.dumps({"model": model, "messages": [
        {"role": "system", "content": prompt}, {"role": "user", "content": text}
    ], "temperature": 0.2}, ensure_ascii=False).encode("utf-8")
    request = Request(base + "/chat/completions", data=payload, method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=60) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise GenerationError("模型响应过大")
        body = json.loads(raw)
        result = json.loads(body["choices"][0]["message"]["content"])
        content = MindMapContent.model_validate(result)
        # 单文本接口无可验证的来源记录，不接受模型虚构的来源元数据。
        content.source_usage = []
    except HTTPError as exc:
        raise GenerationError(f"模型服务返回 HTTP {exc.code}，请检查服务配置") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise GenerationError("无法连接模型服务或请求超时") from exc
    except (ValueError, KeyError, IndexError, TypeError, ValidationError) as exc:
        raise GenerationError("模型返回内容不是符合约定的导图 JSON，请重试") from exc
    return GenerateResponse(mode="model", content=content)
