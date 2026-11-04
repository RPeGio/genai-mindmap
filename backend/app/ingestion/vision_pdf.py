"""PDF 视觉提取：把每页渲染成图，交给多模态模型直接转 Markdown。

为什么不用 pymupdf 抽文本层：文本层只有字符，没有表格、没有公式结构、没有字号
层级。扫描件更是一个字符都抽不到。视觉模型读整页能同时拿到文字、表格行列、
公式与标题层级——实测同一份 Attention 论文的图片版，pymupdf 抽出 0 字符，
视觉模型能还原标题层级、Table 1 的四行数据（含 LaTeX 公式）以及正文。

代价是慢和花钱：单页 5~7 秒、每份几分钱，且必须联网。所以：
  - 页数封顶（MAX_VISION_PAGES），超出的页丢弃并在 Source.truncated 里说明
  - 渲染串行（pymupdf 的 Document 不是线程安全的），只把 API 调用并发
  - 任何一页失败都退回到该页的文本层（如果这份 PDF 有的话），不整份报废
"""
from __future__ import annotations

import base64
import json
import os
from concurrent.futures import ThreadPoolExecutor

# 最多渲染多少页。渲染 + 逐页调模型的开销是线性的，不封顶的话一份 541 页的
# 专著能把请求挂十几分钟，前端早超时了。
MAX_VISION_PAGES = 20

# 渲染分辨率。150 对正文、表格、公式足够，200 以上体积和耗时涨得快而识别率提升有限。
RENDER_DPI = 150

# 并发调用数。模型服务对并发有速率限制，3 路是实测下比较稳的一档；
# 再高容易触发 429，反而拖慢整体。
CONCURRENCY = 3

# 单页输出上限。一页正文 + 表格通常 600~900 token，留足余量。
_PAGE_MAX_TOKENS = 4000

PAGE_PROMPT = (
    "把这一页 PDF 转成 Markdown。要求：\n"
    "1. 标题用 # 表示层级，按字号与位置判断级别（正文一级、小节二级、子小节三级）\n"
    "2. 表格必须用 Markdown 表格，保留表头分隔行与行列对齐\n"
    "3. 公式用 LaTeX，行内 $...$，独立公式 $$...$$\n"
    "4. 图片位置写 [图: 图注内容]，没有图注就跳过\n"
    "5. 页眉页脚、页码丢弃\n"
    "只输出这一页的 Markdown，不要解释，不要代码围栏。"
)


class VisionUnavailable(Exception):
    """视觉提取整体不可用（未配置模型、网络不通、全部页失败）。"""


def _config() -> tuple[str, str, str]:
    base = os.getenv("MINDMAP_LLM_BASE_URL", "").strip().rstrip("/")
    key = os.getenv("MINDMAP_LLM_API_KEY", "").strip()
    model = os.getenv("MINDMAP_LLM_MODEL", "").strip()
    # 演示模式必须不联网：其它接口在 demo 下走本地占位，视觉提取若照样调 API，
    # 会出现"演示模式却在烧 token"，与整个项目的模式约定冲突。
    mode = os.getenv("MINDMAP_LLM_MODE", "demo").strip().lower()
    if mode != "model":
        raise VisionUnavailable(
            f"当前是 {mode} 模式，不做视觉提取（需要 MINDMAP_LLM_MODE=model）")
    if not base or not key or not model:
        raise VisionUnavailable("未配置模型地址、密钥或模型名，无法做视觉提取")
    return base, key, model


def _strip_fence(text: str) -> str:
    """模型偶尔无视「不要代码围栏」，把整页包进 ```markdown 里。"""
    s = (text or "").strip()
    if s.startswith("```"):
        first_newline = s.find("\n")
        if first_newline > 0:
            s = s[first_newline + 1:]
    if s.endswith("```"):
        s = s[: -3]
    return s.strip()


def _page_to_markdown(image_png: bytes, page_no: int) -> str:
    """单页转 Markdown；失败抛异常，由调用方决定怎么兜。"""
    import httpx

    base, key, model = _config()
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": PAGE_PROMPT},
                {"type": "image_url", "image_url": {
                    "url": "data:image/png;base64," +
                           base64.b64encode(image_png).decode()}},
            ],
        }],
        "temperature": 0.1,
        "max_tokens": _PAGE_MAX_TOKENS,
    }
    with httpx.Client(timeout=180.0) as client:
        resp = client.post(base + "/chat/completions", json=payload,
                           headers={"Authorization": f"Bearer {key}"})
        if resp.status_code != 200:
            raise VisionUnavailable(
                f"第 {page_no} 页识别失败：HTTP {resp.status_code}")
        body = resp.json()
    choice = body["choices"][0]
    # finish_reason=length 表示输出撞上了 max_tokens 被硬切。半页内容如果当成
    # 成功收下，这一页的正文就永久缺了一截，而整份结果还报"解析完整"。
    # 当成该页失败处理，上层会用同一页的文本层补。
    if (choice.get("finish_reason") or "").lower() == "length":
        raise VisionUnavailable(
            f"第 {page_no} 页输出被截断（达到单页 token 上限）")
    content = choice["message"].get("content") or ""
    text = _strip_fence(content)
    if not text:
        raise VisionUnavailable(f"第 {page_no} 页没有返回内容")
    return text


def extract_pages(raw: bytes) -> tuple[list[str], int, int, list[int]]:
    """把 PDF 逐页转成 Markdown。

    返回 (逐页 markdown 列表, 总页数, 实际处理页数, 失败的页码列表)。
    失败的页在列表里对应空串，调用方可以用文本层补。
    """
    # 配置预检放在渲染之前：demo 模式、密钥缺失这类问题必须立刻暴露，
    # 否则会被下面「单页失败不拖垮整份」的兜底吞掉，变成渲染 20 页、
    # 每页都失败、最后静默回落——用户看不到真实原因，还白烧一遍 CPU。
    _config()

    import pymupdf

    doc = pymupdf.open(stream=raw, filetype="pdf")
    try:
        page_count = doc.page_count
        limit = min(page_count, MAX_VISION_PAGES)
        # pymupdf 的 Document 不是线程安全的：先把图渲染出来，再并发调 API。
        images: list[bytes] = []
        for index in range(limit):
            try:
                images.append(doc[index].get_pixmap(dpi=RENDER_DPI).tobytes("png"))
            except Exception:  # noqa: BLE001 - 单页渲染失败留空位，后面按失败处理
                images.append(b"")
    finally:
        doc.close()

    if not images:
        raise VisionUnavailable("这份 PDF 没有可渲染的页面")

    pages = [""] * len(images)
    failed: list[int] = []

    def work(index: int) -> tuple[int, str, bool]:
        if not images[index]:
            return index, "", False
        try:
            return index, _page_to_markdown(images[index], index + 1), True
        except Exception:  # noqa: BLE001 - 单页失败不该拖垮整份文档
            return index, "", False

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        for index, text, ok in pool.map(work, range(len(images))):
            if ok:
                pages[index] = text
            else:
                failed.append(index + 1)

    return pages, page_count, limit, failed
