"""流式生成：先把骨架推给前端，再补细节，避免十几秒的白屏。

设计参考：

- 两段式。第一段只要「标题 + 一级分支」，`max_tokens` 压到 400，通常 2–4 秒回；
  第二段把骨架原样回传给模型，让它「为每个分支补子节点」，结构因此保持一致。
- 事件格式跟 `docs/ARCHITECTURE_ADDENDUM.md` 记的约定一致，但事件类型放在 JSON 的
  `type` 字段里，不用 SSE 的 `event:` 行 —— 因为响应头发出后就不能再改，
  异常也只能走 data 传（这一点与研枢项目的处理方式相同）。
- 等待期间发心跳：模型调用是同步阻塞的，不主动吐帧就什么都发不出去，
  前端会以为连接死了。心跳里带已等待毫秒数，界面可以直接显示。
- 第二段失败不销毁第一段成果：退化成骨架当结果，并在 `done` 里标注 `degraded`。

事件一览（每行 `data: <json>`）：

    {"type":"stage","stage":"skeleton","progress":0.1,"detail":"正在生成骨架"}
    {"type":"skeleton","content":{...}}
    {"type":"heartbeat","waitedMs":1500}
    {"type":"stage","stage":"detail","progress":0.4,"detail":"正在展开分支"}
    {"type":"done","mode":"model","content":{...},"latencyMs":18234,"degraded":false}
    {"type":"error","code":"...","message":"..."}
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from app.contracts.mindmap import MindMapContent, Node
from app.generation.service import (
    DETAIL_PROMPT,
    GenerationError,
    _demo_content,
    chat,
)

# 骨架段提示词：只要一级分支，形状固定，便于前端立刻画出主结构。
SKELETON_PROMPT = (
    "把用户输入归纳成一张思维导图的骨架，只返回一个 JSON 对象，不要代码围栏。"
    "格式为 {\"title\":\"标题\",\"root\":{\"id\":\"root\",\"label\":\"中心主题\","
    "\"children\":[{\"id\":\"b1\",\"label\":\"一级分支\",\"children\":[]}]}}。"
    "要求：一级分支 3 到 7 个，每个分支的 children 必须是空数组，"
    "id 用 b1、b2 这样的短标识，label 不超过 20 字，不要捏造输入中没有的事实。"
)

# 展开段提示词：把骨架原样回传，只让模型补子节点，结构不会跑偏。
EXPAND_PROMPT = (
    "下面是一张思维导图的骨架 JSON。请为每个一级分支补充 2 到 5 个子节点，"
    "只返回补全后的完整 JSON 对象，不要代码围栏。"
    "硬性要求：title 与所有一级分支的 label、id 原样保留，一个都不能改；"
    "新增子节点的 id 全局唯一；总节点数不超过 300；层级不超过 8；"
    "不要捏造输入中没有的具体事实。\n\n骨架：\n"
)

_HEARTBEAT_SECONDS = 1.5      # 超过这个间隔没新事件就补一条心跳
_POLL_SECONDS = 0.4           # 轮询队列的粒度
# 骨架段只要 3–7 个分支，但 deepseek-flash 是推理模型：reasoning token 波动很大，
# 实测同一提示词下 102 到 1075 都有，留 400 会被思考吃光、content 返回空串。
# 2000 是实测能稳定拿到合法骨架的值（最差一次仍剩 900 余量）。
_SKELETON_MAX_TOKENS = 2000


def _sse(payload: dict[str, Any]) -> str:
    """拼一帧 SSE。JSON 里不允许出现裸换行，ensure_ascii=False 让中文保持可读。"""
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def _start(fn) -> queue.Queue:
    """把阻塞调用丢进守护线程，主生成器靠轮询队列保持可吐帧。"""
    box: queue.Queue = queue.Queue()
    def runner() -> None:
        try:
            box.put(("ok", fn()))
        except BaseException as exc:  # noqa: BLE001 - 结果统一交给主线程判断
            box.put(("err", exc))
    threading.Thread(target=runner, daemon=True).start()
    return box


def build_skeleton(text: str) -> MindMapContent:
    """第一段：只要骨架。校验不过就报错，让调用方决定降级策略。"""
    result = chat(SKELETON_PROMPT, text, max_tokens=_SKELETON_MAX_TOKENS)
    try:
        content = MindMapContent.model_validate(result)
    except ValidationError as exc:
        raise GenerationError("骨架不是合法的导图结构") from exc
    content.source_usage = []
    return content


def expand_skeleton(skeleton: MindMapContent, text: str) -> MindMapContent:
    """第二段：把骨架回传给模型补细节。"""
    skeleton_json = json.dumps(
        {"title": skeleton.title, "root": skeleton.root.model_dump()},
        ensure_ascii=False,
    )
    result = chat(EXPAND_PROMPT + skeleton_json, text)
    try:
        content = MindMapContent.model_validate(result)
    except ValidationError as exc:
        raise GenerationError("展开结果不是合法的导图结构") from exc
    content.source_usage = []
    return content


def _skeleton_is_usable(content: MindMapContent) -> bool:
    """骨架至少要有一个分支，否则前端画出来是孤零零一个根节点。"""
    return len(content.root.children) > 0


def _stream_events(text: str) -> Iterator[str]:
    """流式生成的主体。异常交给外层 generate_stream 统一收口。"""
    started = time.monotonic()

    def elapsed_ms() -> int:
        return int((time.monotonic() - started) * 1000)

    mode = os.getenv("MINDMAP_LLM_MODE", "demo").strip().lower()

    if mode == "demo":
        yield _sse({"type": "stage", "stage": "demo", "progress": 0.5,
                    "detail": "演示模式，不调用模型"})
        content = _demo_content(text)
        yield _sse({"type": "skeleton", "content": content.model_dump()})
        yield _sse({"type": "done", "mode": "demo", "content": content.model_dump(),
                    "latencyMs": elapsed_ms(), "degraded": False})
        return

    if mode != "model":
        yield _sse({"type": "error", "code": "bad_mode",
                    "message": "MINDMAP_LLM_MODE 只能是 demo 或 model"})
        return

    # ---- 第一段：骨架 ----
    yield _sse({"type": "stage", "stage": "skeleton", "progress": 0.1,
                "detail": "正在生成骨架"})
    box = _start(lambda: build_skeleton(text))
    skeleton: MindMapContent | None = None
    last_beat = time.monotonic()
    while skeleton is None:
        try:
            kind, payload = box.get(timeout=_POLL_SECONDS)
        except queue.Empty:
            now = time.monotonic()
            if now - last_beat >= _HEARTBEAT_SECONDS:
                yield _sse({"type": "heartbeat", "waitedMs": elapsed_ms()})
                last_beat = now
            continue
        if kind == "err":
            message = str(payload) if isinstance(payload, GenerationError) else "生成失败，请重试"
            yield _sse({"type": "error", "code": "generation_failed", "message": message})
            return
        skeleton = payload  # type: ignore[assignment]

    if not _skeleton_is_usable(skeleton):
        yield _sse({"type": "error", "code": "empty_skeleton",
                    "message": "模型没有给出任何一级分支，请换一种说法重试"})
        return

    yield _sse({"type": "skeleton", "content": skeleton.model_dump()})

    # ---- 第二段：展开 ----
    yield _sse({"type": "stage", "stage": "detail", "progress": 0.4,
                "detail": "正在展开分支"})
    box = _start(lambda: expand_skeleton(skeleton, text))
    detailed: MindMapContent | None = None
    last_beat = time.monotonic()
    while detailed is None:
        try:
            kind, payload = box.get(timeout=_POLL_SECONDS)
        except queue.Empty:
            now = time.monotonic()
            if now - last_beat >= _HEARTBEAT_SECONDS:
                yield _sse({"type": "heartbeat", "waitedMs": elapsed_ms()})
                last_beat = now
            continue
        if kind == "err":
            # 骨架已经在屏幕上，退回它比整张图消失好。
            yield _sse({"type": "done", "mode": "model",
                        "content": skeleton.model_dump(),
                        "latencyMs": elapsed_ms(), "degraded": True,
                        "reason": str(payload) if isinstance(payload, GenerationError)
                                  else "展开阶段失败"})
            return
        detailed = payload  # type: ignore[assignment]

    yield _sse({"type": "stage", "stage": "validate", "progress": 0.9,
                "detail": "校验结构"})
    yield _sse({"type": "done", "mode": "model", "content": detailed.model_dump(),
                "latencyMs": elapsed_ms(), "degraded": False})


def count_nodes(content: MindMapContent) -> int:
    """统计节点数，流式端点回给前端的 `nodeCount` 用它。"""
    total = 0
    stack: list[Node] = [content.root]
    while stack:
        node = stack.pop()
        total += 1
        stack.extend(node.children)
    return total


# 终止帧沿用 OpenAI 的 `[DONE]` 约定：前端据此判定「流正常走完」，
# 而不是靠 TCP 连接关闭来推断 —— 连接被中间层掐断和主动结束，长得一模一样。
_DONE_FRAME = "data: [DONE]\n\n"


def generate_stream(text: str) -> Iterator[str]:
    """流式生成的唯一出口。

    不管走 demo、走模型、还是中途抛错，帧格式完全一致：
    若干 `data: {json}`，最后一条必然是 `data: [DONE]`。
    统一在一处收口是为了让前端只写一套解析器。
    """
    try:
        yield from _stream_events(text)
    except GenerationError as exc:
        yield _sse({"type": "error", "code": "generation_failed", "message": str(exc)})
    except Exception:  # noqa: BLE001 - 流已开始，只能把失败推进流里
        yield _sse({"type": "error", "code": "internal_error",
                    "message": "生成过程出错，请重试"})
    yield _DONE_FRAME


# 供路由层直接导入，避免在 router 里再多一次 import 摆动。
__all__ = [
    "DETAIL_PROMPT",
    "SKELETON_PROMPT",
    "EXPAND_PROMPT",
    "build_skeleton",
    "expand_skeleton",
    "generate_stream",
    "count_nodes",
]
