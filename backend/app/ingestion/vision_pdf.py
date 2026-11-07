"""PDF 视觉提取：把每页渲染成图，交给多模态模型直接转 Markdown。

为什么不用 pymupdf 抽文本层：文本层只有字符，没有表格、没有公式结构、没有字号
层级。扫描件更是一个字符都抽不到。视觉模型读整页能同时拿到文字、表格行列、
公式与标题层级——实测同一份 Attention 论文的图片版，pymupdf 抽出 0 字符，
视觉模型能还原标题层级、Table 1 的四行数据（含 LaTeX 公式）以及正文。

代价是慢和花钱：单页 5~7 秒、每份几分钱，且必须联网。所以：
  - 页数封顶（MAX_VISION_PAGES），超出的页丢弃并在 Source.truncated 里说明
  - 渲染串行（pymupdf 的 Document 不是线程安全的），只把 API 调用并发
  - 任何一页失败都退回到该页的文本层（如果这份 PDF 有的话），不整份报废

**并发模型（逐页仍然是并发的，别改回串行）**：一页 5~7 秒里绝大部分是在等
模型回包，串行识别 20 页要一分半以上；渲染 20 页实测只要 0.5 秒，是纯 CPU
且 pymupdf 不线程安全，所以顺序是「先串行渲染、再并发调模型」。并发度见
CONCURRENCY。

**结果必须按页码就位**：下游节点靠页码引用来源，错位不会报错，只会让引用
静默指到错误的页。所以并发写法上只能「按索引回填」，不能「按完成顺序追加」
——`pool.map` 本身保序，但下面的代码只依赖返回值里带回的 index，不依赖迭代
顺序，两重保险。
"""
from __future__ import annotations

import base64
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

# 最多渲染多少页。渲染 + 逐页调模型的开销是线性的，不封顶的话一份 541 页的
# 专著能把请求挂十几分钟，前端早超时了。
MAX_VISION_PAGES = 20

# 渲染分辨率。150 对正文、表格、公式足够，200 以上体积和耗时涨得快而识别率提升有限。
RENDER_DPI = 150

# 并发调用数。视觉调用是纯网络往返，4 路足以把往返延迟藏掉（20 页从串行的
# 两分多钟压到 5 轮 ≈ 半分钟），这也是本模块唯一值得并发的地方。
#
# 保持 3。这个值是之前实测定的：再高容易触发 429，而 429 的代价是这一页
# 记进 failed_pages —— 扫描件没有文本层可以补回，用户看到的是一份静默缺页的
# 正文，比慢几十秒难发现得多。要提速先解决前端一次只解析一份文件的排队，
# 那条路径的收益比这里加一路大得多。
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

# 图片用的提示。和 PDF 单页的差别在于图片没有"页"的概念，可能是草图、
# 截图、白板照或照片，需要模型自己判断形态再决定怎么组织内容。
IMAGE_PROMPT = (
    "把这张图片转成 Markdown。按内容形态选择处理方式：\n"
    "1. 思维导图或手绘草图：按层级还原成缩进列表，保留父子关系\n"
    "2. 幻灯片或界面截图：提取标题、正文、列表，表格用 Markdown 表格\n"
    "3. 白板或照片：提取所有可辨认的文字，按空间位置组织\n"
    "4. 图表：保留坐标轴、图例与数据标签，数据写成 Markdown 表格\n"
    "只输出内容，不要解释，不要代码围栏。看不清的地方留空，不要猜。"
)

# 图片魔数 → MIME。模型要的是正确的 data URL 前缀，写错会让服务端解码失败，
# 而错误信息只有一句"识别失败"，很难查回根因。
_MIME_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
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


def _call_vision(image_bytes: bytes, mime: str, prompt: str, label: str) -> str:
    """把一张图交给多模态模型，返回纯文本。失败抛 VisionUnavailable。

    PDF 逐页和单张图片走同一条路径：都是 OpenAI 兼容的 `image_url` +
    base64 data URL。模型换成不支持图片的那个时，两处会一起坏掉——这是
    有意的，免得只在一条路径上发现问题。
    """
    import httpx

    base, key, model = _config()
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {
                    "url": f"data:{mime};base64," +
                           base64.b64encode(image_bytes).decode()}},
            ],
        }],
        "temperature": 0.1,
        "max_tokens": _PAGE_MAX_TOKENS,
    }
    with httpx.Client(timeout=180.0) as client:
        try:
            resp = client.post(base + "/chat/completions", json=payload,
                               headers={"Authorization": f"Bearer {key}"})
        except httpx.HTTPError as exc:
            # 连接失败、超时、DNS 错误都在这里。不包一层的话异常会直接冒到
            # 路由层变成 500，用户看到的是"服务器内部错误"，而不是"图片识别
            # 没成功"。PDF 路径有单页兜底看不出来，图片路径没有兜底。
            raise VisionUnavailable(
                f"{label}请求失败：{type(exc).__name__}") from exc
        if resp.status_code != 200:
            raise VisionUnavailable(
                f"{label}识别失败：HTTP {resp.status_code}")
        body = resp.json()
    choice = body["choices"][0]
    # finish_reason=length 表示输出撞上了 max_tokens 被硬切。半个结果如果当成
    # 成功收下，内容就永久缺了一截，而整体还报"解析完整"。
    if (choice.get("finish_reason") or "").lower() == "length":
        raise VisionUnavailable(f"{label}输出被截断（达到 token 上限）")
    content = choice["message"].get("content") or ""
    text = _strip_fence(content)
    if not text:
        raise VisionUnavailable(f"{label}没有返回内容")
    return text


def _page_to_markdown(image_png: bytes, page_no: int) -> str:
    """单页转 Markdown；失败抛异常，由调用方决定怎么兜。"""
    return _call_vision(image_png, "image/png", PAGE_PROMPT, f"第 {page_no} 页")


def _guess_mime(raw: bytes) -> str:
    for magic, mime in _MIME_MAGIC:
        if raw.startswith(magic):
            return mime
    # WebP 的头部是 RIFF....WEBP，魔数不在开头，单独判
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    raise VisionUnavailable(
        "无法识别的图片格式（支持 PNG / JPEG / GIF / WebP / BMP）")


def describe_image(raw: bytes) -> str:
    """单张图片 → Markdown。失败抛 VisionUnavailable，不返回空内容。

    走的是和 PDF 逐页完全相同的多模态调用，所以模型不支持图片输入时，
    这两条路径会一起坏——有意为之，免得只在一条上发现问题。
    """
    # 配置预检放在最前：demo 模式、缺密钥必须立刻暴露，不能被后面的
    # 单页兜底逻辑吞掉，留下"上传成功但内容是空的"这种更难查的状态。
    _config()
    if not raw:
        raise VisionUnavailable("图片内容为空")
    return _call_vision(raw, _guess_mime(raw), IMAGE_PROMPT, "这张图片")


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
        """识别第 index 页。异常不外抛——一页失败只影响这一页。"""
        if not images[index]:
            return index, "", False
        try:
            return index, _page_to_markdown(images[index], index + 1), True
        except Exception as exc:  # noqa: BLE001 - 单页失败不该拖垮整份文档
            # 只记页码和异常类型：正文可能含整页原文，不适合进日志。
            # 少了这一行，429 / 超时 / 输出截断在线上完全无从区分。
            logger.warning("第 %d 页视觉识别失败：%s", index + 1, type(exc).__name__)
            return index, "", False

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        for index, text, ok in pool.map(work, range(len(images))):
            # 按返回的 index 回填，不按到达顺序追加：顺序错位会让下游节点
            # 引用指到错误的页，而且不会报错。
            if ok:
                pages[index] = text
            else:
                failed.append(index + 1)

    # 页码升序对外：调用方拿它和文本层逐页对齐，乱序会被误读。
    return pages, page_count, limit, sorted(failed)
