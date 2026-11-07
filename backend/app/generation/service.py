"""本地演示和 OpenAI Chat Completions 兼容服务接入。"""
from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from typing import Any
from uuid import uuid4

import httpx
from pydantic import ValidationError

from app.contracts.diagnostic import Diagnostic, DiagnosticLevel
from app.contracts.mindmap import GenerateResponse, MindMapContent, Node
from app.contracts.source import Source, SourceUsage
from app.optimization.source_refs import verify_source_refs

# 单次模型调用的超时（秒）与响应体上限（字节）：超时避免请求无限挂起，
# 上限避免异常响应把内存读满。
_TIMEOUT_SECONDS = 60.0
_MAX_RESPONSE_BYTES = 2_000_000

# 输出 token 上限。模型服务默认 8192，而实测一棵 240 节点的树要 7578 tok、
# 输出文本约 1.3 万字符；DETAIL_PROMPT 允许 300 节点，必然超出上限被截断，
# 而截断的 JSON 解析不出来，整次请求报废（实测 20000 字符的输入就撞过一次，
# finish_reason=length、输出正好 8192）。服务端接受更大的值，取 16384 留余量。
MAX_OUTPUT_TOKENS = 16384

# 一次性生成完整导图的提示词（非流式接口、流式接口的第二段都用它）。
#
# 输入有两种形态，必须分开说：早先只写了「整理」和「不要捏造」，模型对
# 一句话的主题（如「机器学习」）就严格照做——那四个字里确实没有可整理的
# 分支，于是只回一个光杆根节点，被 MIN_BRANCHES 拦下。而这个产品本来的
# 用法就是「说一个主题，给一张图」，所以要把「围绕主题展开框架」和
# 「忠实整理材料」区分开：前者允许补全结构，后者不许凭空加内容，两者
# 都不许编造具体事实。
DETAIL_PROMPT = (
    "把用户输入变成一张思维导图。只返回一个 JSON 对象，不要代码围栏。"
    "格式为 {\"title\":\"标题\",\"root\":{\"id\":\"唯一id\","
    "\"label\":\"节点文字\",\"children\":[]}}。每个节点均含 id、label、children，"
    "ID 全局唯一，层级不超过 8，节点不超过 300。\n"
    "输入分两种，按情况处理：\n"
    "1) 完整材料（会议纪要、文章、笔记）：忠实整理它的结构，不要添加材料里"
    "没有的内容。\n"
    "2) 简短的主题或目标（如「机器学习」「帮我规划毕业论文」）：围绕它展开成 "
    "3-6 个一级分支，每个分支再给 2-4 个要点。这是框架展开，不算捏造；"
    "但不要编造具体的数据、人名、文献或引用。\n"
    "两种情况下根节点都必须有子节点，只有根节点的结果不合格。"
)

SOURCED_DETAIL_PROMPT = DETAIL_PROMPT + (
    "注意：这次输入的是带了来源的材料，按上面第 1 种形态处理，"
    "不要因为主题简短就自行展开——所有内容都必须能在 sources 里找到依据。"
    "用户输入为 sources 数组，每项含 source_id 和 text，text 是待整理的原文。"
    "每个有原文依据的节点增加 source_refs 数组，格式为"
    '[{"source_id":"输入中已有的来源ID","quote":"逐字摘录的原文片段"}]。'
    "quote 不超过 300 字，必须出自对应来源的 text，不得改写、拼接或跨来源引用。"
    "没有直接依据时 source_refs 留空，不要编造引用。"
    "不要输出 source_usage；不要猜测 page、start、end。"
)


# 一级分支下限：只要有一条分支就已经是「层次结构」，光杆根节点才算失败。
# 同步接口与流式接口两处共用这一个值，改一处就全改 ——
# 否则同一个模型结果会被一个入口判成功、另一个判失败。
MIN_BRANCHES = 1


class GenerationError(Exception):
    pass


# 模型偶尔无视「不要代码围栏」的指示，把 JSON 包进 ```json … ``` 里。
# 裸 json.loads 会因此直接抛错，用户侧表现为随机失败 —— 实测 18 次调用里
# 撞到 1 次。解析前先剥干净，比要求模型「务必听话」可靠。
_FENCE_OPEN = re.compile(r"^\s*```(?:json|JSON)?\s*")
_FENCE_CLOSE = re.compile(r"\s*```\s*$")


def _balance_brackets(text: str) -> str:
    """修正括号收支不平衡：多出来的闭合符丢掉，缺失的补上。

    两个方向都实测出现过：
      - 多一个 `}`：输入 100000 字符时 3 次里 2 次，模型收尾多写一个花括号，
        `finish_reason` 仍然是 `stop`（它认为自己写完了），整份 JSON 报废。
      - 少若干闭合符：输出撞上 max_tokens 被硬切。
    只动括号，不碰字符串内部 —— 节点文字里的 `{` `}` 必须原样保留。
    """
    out: list[str] = []
    stack: list[str] = []
    in_string = False
    escaped = False

    for ch in text:
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
        elif ch in "{[":
            stack.append(ch)
            out.append(ch)
        elif ch in "}]":
            expected = "{" if ch == "}" else "["
            if stack and stack[-1] == expected:
                stack.pop()
                out.append(ch)
            # 没有配对的开启符：这个闭合符是多余的，直接丢弃
        else:
            out.append(ch)

    if in_string:
        out.append('"')
    for opener in reversed(stack):
        out.append("}" if opener == "{" else "]")
    return "".join(out)


def parse_model_json(content: str) -> Any:
    """解析模型返回的 JSON，容忍代码围栏、多余文字与括号收支错位。"""
    text = (content or "").strip()
    if not text:
        raise ValueError("模型返回了空内容")
    text = _FENCE_CLOSE.sub("", _FENCE_OPEN.sub("", text))
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 括号收支错位：这是实测最常见的一种失败，先修它再试
    balanced = _balance_brackets(text)
    if balanced != text:
        try:
            return json.loads(balanced)
        except json.JSONDecodeError:
            pass

    # 还有解释文字包着时，截取最外层的对象或数组
    for left, right in (("{", "}"), ("[", "]")):
        start, end = text.find(left), text.rfind(right)
        if start < 0 or end <= start:
            continue
        candidate = _balance_brackets(text[start:end + 1])
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise ValueError("模型返回内容里没有可解析的 JSON")


def _choice_content(body: Any) -> str:
    """从响应体里取出模型 content，并拦住被长度上限截断的输出。

    `finish_reason == "length"` 表示输出在 max_tokens 处被硬切。不拦的话，
    `parse_model_json` 里的 `_balance_brackets` 会把半截 JSON 的括号补齐，
    让它"合法"——一棵缺了半边的树就这样被当成成功结果返回，比直接失败
    难排查得多。`stop` 与字段缺失时照常解析：老版本的 mock、部分 OpenAI
    兼容端点都不返回这个字段，那不是"被截断"，只是"没说原因"。
    """
    choice = body["choices"][0]
    if isinstance(choice, dict) and choice.get("finish_reason") == "length":
        raise GenerationError("模型输出撞上长度上限被截断，结果不完整，请重试")
    return choice["message"]["content"]


def _config() -> tuple[str, str, str]:
    """读模型配置。缺失或格式不对时抛 GenerationError，让上层给出可读文案。"""
    base = os.getenv("MINDMAP_LLM_BASE_URL", "").strip().rstrip("/")
    key = os.getenv("MINDMAP_LLM_API_KEY", "").strip()
    model = os.getenv("MINDMAP_LLM_MODEL", "").strip()
    if not base.startswith(("https://", "http://")) or not key or not model:
        raise GenerationError("请在后端环境变量中配置模型地址、密钥和模型名称")
    return base, key, model


def chat(system: str, user: str, *, max_tokens: int | None = None) -> dict[str, Any]:
    """调一次 Chat Completions，返回模型 content 里那段 JSON 解析后的对象。

    只负责传输与 JSON 解析；结构是否符合契约由调用方用 MindMapContent 校验。
    """
    base, key, model = _config()
    request_body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
        "max_tokens": max_tokens or MAX_OUTPUT_TOKENS,
    }
    payload = json.dumps(request_body, ensure_ascii=False).encode("utf-8")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    try:
        # 调用方路由函数是同步 def，这里只用同步客户端；按流读取，
        # 一旦越过字节上限立即放弃响应，不必把超限内容读进内存。
        with httpx.Client(timeout=_TIMEOUT_SECONDS, follow_redirects=True) as client:
            with client.stream("POST", base + "/chat/completions",
                               content=payload, headers=headers) as response:
                response.raise_for_status()
                buffer = bytearray()
                for chunk in response.iter_bytes():
                    buffer.extend(chunk)
                    if len(buffer) > _MAX_RESPONSE_BYTES:
                        raise GenerationError("模型响应过大")
        body = json.loads(buffer)
        # finish_reason 必须先看：被截断的 content 交给 parse_model_json 会被
        # 补齐成"合法"的半棵树，那是最危险的一种成功。
        return parse_model_json(_choice_content(body))
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        raise GenerationError(f"模型服务返回 HTTP {code}，请检查服务配置") from exc
    except (httpx.RequestError, httpx.InvalidURL, OSError) as exc:
        raise GenerationError("无法连接模型服务或请求超时") from exc
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise GenerationError("模型返回内容不是符合约定的导图 JSON，请重试") from exc


async def chat_async(system: str, user: str, *, max_tokens: int | None = None) -> dict[str, Any]:
    """`chat()` 的异步版本，供流式接口使用。

    流式接口必须能真正取消：客户端断开时 Starlette 会 cancel 生成器，
    异步 httpx 会随之关闭连接、停止计费；同步 httpx 跑在线程里，
    取消只能丢掉结果，请求本身仍会跑完并产生费用。
    """
    import httpx

    base, key, model = _config()
    request_body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
        "max_tokens": max_tokens or MAX_OUTPUT_TOKENS,
    }
    payload = json.dumps(request_body, ensure_ascii=False).encode("utf-8")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS, follow_redirects=True) as client:
            async with client.stream("POST", base + "/chat/completions",
                                     content=payload, headers=headers) as response:
                response.raise_for_status()
                buffer = bytearray()
                async for chunk in response.aiter_bytes():
                    buffer.extend(chunk)
                    if len(buffer) > _MAX_RESPONSE_BYTES:
                        raise GenerationError("模型响应过大")
        body = json.loads(buffer)
        # agent 的工具（生成 / 展开）走这条路径，没有 provider 那层的
        # finish_reason 检查，所以截断必须在这里拦住。
        return parse_model_json(_choice_content(body))
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        raise GenerationError(f"模型服务返回 HTTP {code}，请检查服务配置") from exc
    except (httpx.RequestError, httpx.InvalidURL, OSError) as exc:
        raise GenerationError("无法连接模型服务或请求超时") from exc
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise GenerationError("模型返回内容不是符合约定的导图 JSON，请重试") from exc


def _demo_content(text: str) -> MindMapContent:
    """显式的占位内容，不伪装为模型生成。"""
    label = text[:60]
    return MindMapContent(title=label, root=Node(id=str(uuid4()), label=label,
        children=[Node(id=str(uuid4()), label="请编辑此演示节点")]))


def validate_generated_content(
    result: Any, *, sources: list[Source] | None = None,
) -> tuple[MindMapContent, list[Diagnostic]]:
    """以真实输入重建来源摘要，剔除模型伪造的关联，再执行契约与原文校验。

    无 Source 的同步和 SSE 入口共用此出口：在契约校验之前清空所有节点的
    来源声明，避免伪造摘要与引用互相背书，也不让错误的引用拖垮有效的树。
    """
    raw = deepcopy(result)
    diagnostics: list[Diagnostic] = []
    allowed_ids = {source.id for source in sources or []}
    if isinstance(raw, dict):
        raw["source_usage"] = [
            SourceUsage(source_id=source.id, kind=source.kind, filename=source.filename,
                        char_count=len((source.text or "").strip())).model_dump()
            for source in sources or []
        ]
        stack = [raw.get("root")]
        while stack:
            node = stack.pop()
            if not isinstance(node, dict):
                continue
            if sources is None:
                node["source_refs"] = []
            elif isinstance(node.get("source_refs"), list):
                kept = []
                for ref in node["source_refs"]:
                    if (isinstance(ref, dict) and isinstance(ref.get("source_id"), str)
                            and ref["source_id"] not in allowed_ids):
                        diagnostics.append(Diagnostic(
                            level=DiagnosticLevel.warn, code="UNKNOWN_SOURCE_REF",
                            node_id=node.get("id") if isinstance(node.get("id"), str) else None,
                            message=f"已移除引用：来源 {ref['source_id']} 不在本次输入中",
                        ))
                    else:
                        kept.append(ref)
                node["source_refs"] = kept
            children = node.get("children")
            if isinstance(children, list):
                stack.extend(reversed(children))
    try:
        content = MindMapContent.model_validate(raw)
    except ValidationError as exc:
        raise GenerationError("模型返回内容不是符合约定的导图 JSON，请重试") from exc
    if len(content.root.children) < MIN_BRANCHES:
        raise GenerationError("模型只返回了一个根节点，没有任何分支，请换一种说法重试")
    if sources is not None:
        diagnostics.extend(verify_source_refs(content, sources, remove_invalid=True))
    return content, diagnostics


def generate(text: str, *, sources: list[Source] | None = None) -> GenerateResponse:
    mode = os.getenv("MINDMAP_LLM_MODE", "demo").strip().lower()
    if mode == "demo":
        result = _demo_content(text.strip()).model_dump()
    elif mode == "model":
        if sources is None:
            result = chat(DETAIL_PROMPT, text)
        else:
            # 保留原始空白，引用区间才能与 Source.text 对齐；JSON 保持来源边界。
            prompt_input = json.dumps({"sources": [
                {"source_id": source.id, "text": source.text} for source in sources
            ]}, ensure_ascii=False)
            result = chat(SOURCED_DETAIL_PROMPT, prompt_input)
    else:
        raise GenerationError("MINDMAP_LLM_MODE 只能是 demo 或 model")
    content, diagnostics = validate_generated_content(result, sources=sources)
    return GenerateResponse(mode=mode, content=content, diagnostics=diagnostics)
