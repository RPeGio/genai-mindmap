"""外部知识源：把百科与论文的检索结果转成 `Source`。

## 外部源清单（2026-11 实测，逐个的可抓性报告见 `_baike` / `_openalex` 的 docstring）

接了五个：中文维基（`wikipedia`，web）、快懂百科（`baike`，web）、arXiv（`arxiv`，
paper）、OpenAlex（`openalex`，paper）、Crossref（`crossref`，paper）。

**没接的那些，理由都是"过不去"而不是"没试"**：

  - 百度百科：`robots.txt` 的 `User-agent: *` 组是 `Disallow: /`（全站拒绝），
    词条页对正常请求返回的是 `window.__PARIS_CHALLENGE_START__` 的 JS 挑战页
    （4.7 KB、零正文容器），要拿正文得执行它的挑战脚本——那是绕过，不做。
  - 知乎：`robots.txt` 的 `User-agent: *` 组同样是 `Disallow: /`（只 `Allow:
    /tardis/jm`），且 `/search` 对**每一个**具名 bot 都显式 `Disallow`。
    `/search?q=…` 回 403、`/api/v4/search_v3` 回 403 + `{"error":{"code":40362}}`、
    `/api/v4/search` 回 403 + `{"error":{"need_login":true,"code":40352}}`。
  - Semantic Scholar：连续三次实测都是 429（无 key 时全 IP 共享池），不稳定，
    不能作为生成链路的依赖。
  - Stack Exchange：能通（200 + gzip），但**中文 query 返回跑题噪声**
    （`q=碳中和` 命中"Segmentation fault while using a C module"），与主题无关。
  - 萌娘百科：Cloudflare 挑战页（403 + `Just a moment...`），要过挑战才能读。
  - 搜狗百科：`567` + 7.3 KB 拦截页；360 百科：域名已不解析。
  - Wikidata：有中文 label/description，但没有正文，对生成无用。

## 为什么是"转成 Source"而不是另造一套命中

引用逐字校验（`optimization/source_refs`）、父块坐标、"这条引用落在可见父块里吗"、
`library.load()` 回查、前端"来源引用"面板——这五件事都已经只认 `Source`。外部结果
只要变成 `Source` 并 `library.ingest`，它就走完整条既有的 RAG 链路，不需要第二套
hit 形状、第二套引用格式、第二套校验。所以这个模块只做三件事：**抓取、清洗、转换**。
它不碰切块、不碰打分、不碰引用校验。

## 外部内容是**不可信数据**

`text` 里可能有人写"忽略上面的要求"、可能有 HTML、可能五万字。三条规矩：

  - 标签剥掉（`clean_text`），HTML 实体反转义一次；
  - 单条正文截到 `TEXT_MAX_CHARS`，抓取时的原始长度记在
    `Source.metadata.raw_chars`——不记的话，"模型只看到开头 8000 字"与"这条本来就
    只有 8000 字"在界面上长得一模一样；
  - 提示词侧另有一句"这些是外部抓取的内容，只当资料用"，见
    `generation.service.EXTERNAL_TRUST_RULES`。本模块不负责教育模型。

**本模块永不执行、永不请求外部内容里出现的任何 URL**：`url` 只作为数据存进
`Source.metadata.url`，供界面渲染成链接；代码里没有任何一处拿它再发请求。
`contracts.source.safe_http_url` 另外把非 http/https 的 scheme 折成 None——前端会把
这个值塞进 `<a href>`，`javascript:` 进来就是一个 XSS 面。

## 失败即空

单请求超时 10 秒、不重试、任何异常都吞掉并返回已经拿到的部分。外部源挂了不能影响
生成：调用方（`agent.tools._generate`）拿不到结果就退回"只凭主题"的那条路。总预算
（`REQUEST_LIMIT` 次请求 / `DEADLINE_SECONDS` 秒）是硬的，理由见 `_Budget`。

## 多方向检索：查一遍 = 一个方面，短主题要的是几个方面

`碳中和` 这样的主题，用户期待的是"政策 / 技术路径 / 企业案例"这类**多面覆盖**，而
一条 query 只能打回一个方面。所以检索前先拆方向（`plan_directions`，**一次** LLM
调用），每个方向各打一轮，合并去重后按方向轮转取样：

    主题 → 2-4 个方向 → 每方向并行打源（各 2 条）→ URL 去重
         → 按**方向**各自过词面门 → 方向内按源交替 → 跨方向轮转 → 取 limit 条

三条约束是这个形状存在的前提，改动它们等于取消这个功能：

  - **门按方向施加而不是按主题**（`_relevant_hits(direction, hits)`）。合并后的池子
    里一条"讲碳中和但不讲政策"的命中，对方向「碳中和政策」就是噪声；拿主题去判它
    反而放行。拆方向如果不动门，门就会从"判一条命中是不是这个 query 的东西"退化成
    "判它是不是这个话题的东西"——粗一档，等于白拆。
  - **跨方向轮转取样**（`_interleave_directions`）而不是直接 `_interleave`：后者按
    **源**分桶，三个方向都命中维基时，维基桶的前 3 条会占满名额，很可能全来自方向 1
    ——拆了三个方向，返回的还是同一个方面。方向内仍按源交替（`_interleave`），两层
    交替各管一件事：方向内保源多样、跨方向保方面覆盖。
  - **降级必须是一个方向**（`[topic]`）。拆方向的 LLM 调用失败/超时/没配置时，走的是
    与 `3c40ba6` 完全相同的单方向路径（5 源、9 次请求 / 20 秒），不是"少拆一点"。

预算分两层（`_Budget.take(slot)`）：全局 `MULTI_REQUEST_LIMIT`=15 次 / 20 秒；每个
方向另有 `DIRECTION_REQUEST_LIMIT`=5 次的硬上限，3 方向 × 5 = 15 与全局正好对齐。
每方向的请求数由"维基 1 次条目检索 + 1 次抓正文 + 三个论文源各 1 次"构成，所以
**方向路径跳过快懂百科**：它只做精确词条命中（`/search` 被 robots 禁），而方向是
「碳中和政策」这类组合短语，在它那里必然是 404——那一次请求是纯浪费，还会挤掉一个
真能出结果的源。

## 代理

外网直连在本机不通（实测：`zh.wikipedia.org` 连不上、`export.arxiv.org` 的 https
读超时），所以代理是这条链路的前置条件，不是可选优化。默认值就是 Clash Verge 的
`127.0.0.1:7897`；`MINDMAP_EXTERNAL_PROXY=none` 是显式直连（给"机器上本来就有全局
透明代理"那种环境留的出口）。

**国内源也走同一个代理**，这是实测之后的选择而不是偷懒：`baike.com`（字节的
`volc-dcdn`）走代理 1.1 秒、直连 7.5 秒，反而代理更快。反过来 `baike.baidu.com`
走代理直接被掐（TLS 握手 `UNEXPECTED_EOF` / 502），但那个站本来就不接（见上）。
一个 `_client()` 覆盖全部五个源，比给每个源配一份代理策略少一处会配错的地方。

`trust_env=False` 是必须的：本机 `ProxyEnable=0`（系统代理开关是关的），让 httpx
去读环境变量会拿到空值然后直连超时——那是最难解释的一种"外部源全挂"。
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Sequence
from urllib.parse import quote
from xml.etree import ElementTree

import httpx

from app.contracts.source import ISO_SECONDS_FORMAT
# 模型出口与 JSON 解析**借用同一份实现**（`query_plan` 也是这么用的）：`parse_model_json`
# 已经吃过"代码围栏 / 前后多写字 / 括号收支错位"这三类真实故障，在这里另写一套剥围栏
# 逻辑等于把那些故障的两套判定带到两个模块里。
from app.generation.service import chat_async, parse_model_json
# 私有名跨模块引用：分词这件事已经有实现（`_token_list`），门必须与检索用同一套
# "什么算同一个词"，另写一份就是两处真相。模块级导入而不是函数内懒导入——两者
# 之间没有循环（`service` 不引用 `external`），懒导入只会把导入期错误推后。
from app.knowledge.service import _token_list

logger = logging.getLogger(__name__)

# 代理。默认值对着本机的 Clash Verge（`mixed-port: 7897`）。
EXTERNAL_PROXY_ENV = "MINDMAP_EXTERNAL_PROXY"
DEFAULT_PROXY = "http://127.0.0.1:7897"

# 单请求超时。10 秒是"比一条普通 HTTP 往返长得多、又不至于让用户以为卡死"的量级：
# 实测维基 1.1–1.4s、arXiv 0.7–4.0s，而代理节点抖动时会出现 25s 以上的读超时——
# 那种时刻的重试没有意义（同一个节点、同一个目标），所以要的是快速放弃。
REQUEST_TIMEOUT = 10.0

# 整次外部检索的总预算。两个数字一起构成"最坏情况下用户多等多久"：
#   - `DEADLINE_SECONDS`：整次调用的墙钟上限，20 秒后任何新请求都不再发；
#   - `REQUEST_LIMIT`：请求次数上限。9 次正好是五个源的上界之和：
#       维基 1（精确标题）+ 1（条目检索）+ 3（逐条抓 `limit` 条）
#       arXiv 1 / OpenAlex 1 / Crossref 1 / 快懂百科 1
#     = 5 + 1 + 1 + 1 + 1 = 9。没有次数上限时，源返回一个"下一条标题"的畸形响应
#     就能把链路拖进无界循环；给多了则是"为几条候选把预算烧光"。
DEADLINE_SECONDS = 20.0
REQUEST_LIMIT = 9

# 多方向检索的请求预算。15 = 3 个方向 × 5 次（每方向的次数构成见 `DIRECTION_SOURCES`
# 与 `_wikipedia` 的 `exact_first` / `fetch_limit`）。**墙钟不放宽**：`DEADLINE_SECONDS`
# 仍然是 20 秒，三个方向的源请求并行跑，所以"多了两轮检索"不会把等待变成三倍。
MULTI_REQUEST_LIMIT = 15
# 每方向的请求上限。它与 `MAX_DIRECTIONS` 相乘必须等于 `MULTI_REQUEST_LIMIT`：
# 3 × 5 = 15。改其中一个而不改另一个，预算就会在"某个方向整轮被饿死"和"全局还有
# 余量却没人能用"之间二选一——这两种都在实测里表现为"偶尔少一个方面的结果"。
DIRECTION_REQUEST_LIMIT = 5
# 默认最多拆几个方向。提示词要 2-4 个（给模型一点选择余地），代码取前
# `max_directions` 个：多一个方向就多一条"名额被摊薄"的路径，而最终只要 3 条。
MAX_DIRECTIONS = 3
# 每方向最多留几条候选。**不是每方向 3 条**：3 方向 × 3 = 9 条候选去争最终 3 个名额，
# 等于把名额浪费在"同方向的三条"上。2 是"一个方面能贡献两个视角"与"别挤掉别的方面"
# 之间的取值。
DIRECTION_HIT_LIMIT = 2
# 方向路径里维基只抓 1 条正文。理由同上：第 2 条正文的边际价值低于"别的源还有一次
# 请求额度"，而维基的逐条抓取是这条链上唯一会随 `limit` 线性涨请求数的地方。
DIRECTION_WIKI_FETCH = 1
# 方向路径的源清单：**跳过 baike**（精确词条命中，组合短语必然 404），见模块 docstring。
DIRECTION_SOURCES: tuple[str, ...] = ("wikipedia", "openalex", "crossref", "arxiv")

# 拆方向的 LLM 调用：单次、8 秒、不重试。与 `query_plan.TIMEOUT_SECONDS` 同一个量级，
# 但单独一个名字——两条路径的预算将来可能不同，共用一个常量会把它们绑死。
DIRECTION_TIMEOUT_SECONDS = 8.0
# 2-4 个方向、每个十几个字 + JSON 结构，中文约 80 token。给 300 是留足余量：给大了
# 只会让慢模型更慢，而预算只有 8 秒。
DIRECTION_MAX_OUTPUT_TOKENS = 300
# 方向的长度区间。提示词里要的是 6-20 字（写得具体一点才检索得出新东西），但**代码闸
# 的下限放到 4**：实测合法方向的字数是「碳中和政策」5、「碳交易市场」5、「Rust 内存模型」
# 9——6 字下限会把它们整批打掉，而那正是最自然的形状（主题原词 + 两个字的限定语）。
#
# **单位是"折算字数"而不是字符数**：英文主题的方向必然是英文（模型对 `transformer
# architecture` 给的是 `transformer architecture 原理`，27 个字符），按字符数上限 20
# 会把英文主题整批判死——实测三个英文主题全部降级成单方向就是这个原因。折算是
# 1 个中文汉字 = 1，1 个英文词 = 2（一个英文词的检索价值大致相当于两个汉字）。
DIRECTION_MIN_CHARS = 4
DIRECTION_MAX_CHARS = 20
# 纯英文方向的独立闸：2-8 个词、总长度 ≤ 60 字符。`Byzantine fault tolerance threat
# models` 是 5 个词 39 字符，合法；而一段整句会在词数上暴露。
DIRECTION_MIN_WORDS = 2
DIRECTION_MAX_WORDS = 8
DIRECTION_MAX_ASCII_CHARS = 60
# 方向里的"英文词"：字母开头，允许 `C++` / `C#` / `node.js` 这类带符号的写法。
_DIRECTION_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#._-]*")
# 模型一次最多给几个方向。提示词写 2-4，这里是它的代码执行处：多给的部分直接截掉，
# 而不是"多跑几个"。截到 4（提示词的上界）后再由 `max_directions` 收一次。
DIRECTION_MAX_ITEMS = 4

# 拆方向的开关。`MINDMAP_EXTERNAL_DIRECTIONS` **默认关**——量过了，是负收益：
# 同一时段、同一出口、同一批源的两份池子（`scripts/measure_external_relevance.py
# --collect-single` / `--collect-directions`，20 主题）上，三条全相关 13/20 → 6/20、
# 成功率 95% → 50%、耗时中位 4.51 → 6.65 秒，噪声 8 → 5 只是因为返回从 43 条掉到
# 21 条。三处原因都不是"再调调"能消掉的：
#
#   - **限定语闸把有料的主题砍空**：`幻觉` 的方向池里有 4 条相关候选，过门后 0 条；
#     `内卷` 2 条相关 → 0 条。方向以主题原词开头时，`_phrase_terms(direction)` 退化成
#     主题的判定词块，必须靠限定语补第二刀，而限定语在这批源站的摘要里经常不出现。
#   - **单条命中本来就够**：20 个主题里有 10 个，单方向路径回来那条维基正文的前 300 字
#     就已经含 2-3 个方向限定语（`注意力机制` 三条全中、`最终一致性` 三条全中）。
#     拆方向是在把一份已经多面的材料切成单面的几份。
#   - **每方向预算太薄**：15 次 / 3 方向 = 每源一次机会，任何一个源 429 就丢掉该方向的
#     一整个源；单方向路径有 9 次预算、维基能抓 3 条，容错高得多。实测方向池里"至少
#     1 条相关候选"的主题只有 12/20，而单方向池是 16/20——池子本身就差一档。
#
# 保留实现而不是删掉，是因为它把"为什么不行"变成了可复现的东西：打开开关、跑
# `--collect-directions` / `--directions`，上面每个数字都能重算。默认关保证
# `3c40ba6` 的行为逐字节不变（`_search_single` 就是它）。
DIRECTIONS_ENV = "MINDMAP_EXTERNAL_DIRECTIONS"
_TRUTHY = {"1", "true", "yes", "on", "y", "t"}

# 单条正文的入库上限。8000 字大约是中文维基一个条目正文的两倍、arXiv 摘要的十几倍：
# 再长的部分进不了 prompt（父块 450–700 字，一次检索最多给 5 条），留着只是占库。
TEXT_MAX_CHARS = 8000

# 一次检索最多拿几条（跨源合计）。默认 3 与 `_generate` 的 `GENERATE_RETRIEVAL_TOP_K=5`
# 不冲突：外部来源本身是"一份材料"，进库之后由检索筛，条数多了反而把清单撑满。
DEFAULT_LIMIT = 3

# 查询词的长度上限。用户可能整段贴进来；维基的 `titles=` 是一次精确查找、arXiv 的
# `all:` 是一条检索式，两者在 50 字以上都只会返回噪声或空。
QUERY_MAX_CHARS = 50

WIKIPEDIA_API = "https://zh.wikipedia.org/w/api.php"
ARXIV_API = "http://export.arxiv.org/api/query"
OPENALEX_API = "https://api.openalex.org/works"
CROSSREF_API = "https://api.crossref.org/works"
# 快懂百科（原互动百科）。**路径是 `/wiki/<词条名>`，不是站内搜索**：它的
# `robots.txt` 明确 `Disallow: /search`，而 `/wiki/*` 在 `User-agent: *` 组下是
# `Allow: /`。所以这个源只能做精确词条命中，拼不出候选（详见 `_baike`）。
BAIKE_WIKI_URL = "https://www.baike.com/wiki/"

# 每个源向服务端要几条候选。给 5 而不是 `limit`：`_interleave` 从 5 个桶里轮取，
# 只给 3 条的话，中文源一旦排在后面就一条都进不了结果（见 `_interleave` 的
# `prefer_language`）——而"5 个源、3 个名额"本来就是一道数学题，候选多一点才有
# 得挑。
SOURCE_CANDIDATES = 5

# 维基要求可识别的 User-Agent 并带联系方式，否则 403：
# 实测纯 `Mozilla/5.0 ...` 得到 "Please respect our robot policy"，
# 带产品名 + URL + 邮箱的规范 UA 才是 200。这不是能省的礼貌，是硬门槛。
#
# 域名与邮箱用 RFC 2606 的保留域名（`example.invalid`），因为它只是一个占位：
# 编一个真实域名等于冒充别人的联系地址。**部署时换成自己的**——维基的机器人
# 政策要的正是"出事了能找到人"。
USER_AGENT = (
    "genai-mindmap/0.1 (external knowledge retrieval; "
    "+https://example.invalid; research@example.invalid)"
)

# Crossref 的示好邮箱。多方向检索把请求密度抬到单方向的 3 倍（3 个方向各打一轮），
# 实测会撞上匿名限流：同一出口 IP 一分钟内十几次请求后开始回 429。Crossref 明确支持
# "polite pool"——把联系方式放进 UA（已经有了）**并且**放进 `mailto` 参数，限流额度
# 按这个身份算。所以这既是礼貌，也是密度上去之后能不能用的分界。
#
# **OpenAlex 不传这个参数**：实测它的 `meta.notice` 原文写着 "The mailto parameter
# doesn't raise this limit"——它只认 API key，`mailto` 一个字都不影响预算。给它加一个
# 无效参数会让下一个读代码的人以为"限流已经处理过了"，那是比没有更坏的状态。
# 它撞限流时的行为是 429 → 该源降级为空，与"源挂了"同一条路（见 `_openalex`）。
POLITE_MAILTO = "research@example.invalid"

# 响应体上限。2 MB 与 `generation.service._MAX_RESPONSE_BYTES` 同口径：外部源不是
# 我们可以信任的一端，一个畸形响应不该把内存吃满。
MAX_RESPONSE_BYTES = 2_000_000

# 标签清洗。先整段删 `<script>` / `<style>`（它们的内容不是文字），再删剩下的标签。
_BLOCK_TAG_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]{0,200}>")
_HSPACE_RE = re.compile(r"[ \t\u00a0\u3000]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")
# 维基 explaintext 保留 `== 小节 ==`。转成 Markdown 标题不是装饰：切块器
# （`knowledge.service._HEADING_RE`）与材料目录（`heading_outline`）都只认
# `#{1,6} ` 这一种标题行，留着 `==` 的话这些小节在检索侧只是正文里的怪符号。
_WIKI_HEADING_RE = re.compile(r"^(={2,6})\s*(.+?)\s*\1\s*$", re.MULTILINE)

# CJK 统一表意文字。只用来判"这条正文是不是中文"，不判语种细分。
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

# 快懂百科的词条页。两个正则各对应一处**服务端渲染**出来的结构：
#   - 词条名：`<h1 class="container-xxx doc-title-container">碳中和</h1>`
#     （`container-xxx` 带构建哈希，`doc-title-container` 是语义名，钉语义名）；
#   - 正文段落：`<div class="baike-render-paragraph">…</div>`。
#
# **不能对整页 `clean_text`**：实测"碳中和"页 307 KB 里，参考资料 JSON 的
# `"Content"` 字段藏着一整段 1427 字的今日头条转载原文，还有导航、推荐位、
# 轮播文案。整页清洗会把它们当成词条正文——引用会指向一段用户没看过的转载。
_BAIKE_TITLE_RE = re.compile(
    r'<h1[^>]*class="[^"]*doc-title-container[^"]*"[^>]*>(.*?)</h1>', re.DOTALL)
_BAIKE_PARA_RE = re.compile(r'<div class="baike-render-paragraph">(.*?)</div>', re.DOTALL)

# 段落内的标签上限 800 而不是 `_TAG_RE` 的 200：快懂百科的内链 `<a>` 带五个 class
# 加 id 加三个 data 属性，实测约 230 字符——超过 200 就匹配不上，`class=…` 会原样
# 留在正文里。这是"复用 `clean_text`"在这里会静默出错的那一类。
_BAIKE_TAG_RE = re.compile(r"<[^>]{0,800}>")

_ATOM = "{http://www.w3.org/2005/Atom}"


@dataclass(frozen=True)
class ExternalHit:
    """一条外部命中。**只是抓取的产物**，不是检索契约的一部分。

    字段与"转成 `Source` 需要什么"一一对应：`title` + `source_name` 拼文件名，
    `text` 进正文（**已按 `TEXT_MAX_CHARS` 截断**），`raw_chars` 是截断前的长度
    （进 `metadata.raw_chars`，让"模型只看到开头"这件事可见），`url` 进
    `metadata.url`，`fetched_at` / `language` 是抓取过程的描述。

    刻意不含 `score`：外部源不给我们可信的相关性分数，编一个只会让下游误以为
    它和 `EvidenceHit` 是同一种东西。

    `direction` 是这条命中来自哪个检索方向（`""` = 没拆方向，即单方向检索的既有
    行为）。它**只用于说明与报告**：不是评分、不参与排序，`_interleave` 也只把它当
    分组用（见 `_interleave_directions`）。带默认值是为了"没拆方向"那条路径的构造
    点一个都不用改——降级必须与 `3c40ba6` 逐字节一致。
    """

    title: str
    text: str
    url: str
    source_name: str
    fetched_at: str
    language: str
    # 截断**之前**的长度。`len(text)` 在长条目上恒等于 `TEXT_MAX_CHARS`，
    # 靠它分不清"这份资料本来就这么长"与"被砍掉了九成"。
    raw_chars: int = 0
    # 这条命中来自哪个方向。空串 = 单方向检索（既有行为）。
    direction: str = ""


@dataclass(frozen=True)
class _Slot:
    """一个方向在这次检索里的请求槽位：`key` 是它的名字，`limit` 是它的请求上限。

    单独造一个类型而不是给 `take()` 传两个参数，是为了让"同一方向的四次 take 用同
    一个槽位"这件事在调用处只是一个变量传递：`_wikipedia(query, budget, 2, slot=slot)`
    里三次请求各自 `budget.take(slot)`，漏传一次就会多花掉别的方向的额度，而这种
    泄漏在线程池里表现为"某个方向偶尔少一条"，最难查。

    `key` 为空时**不做**单槽位计数（单方向路径），语义与 `take()` 不带参数相同。
    """

    key: str = ""
    limit: int | None = None


class _Budget:
    """这次外部检索的共享预算：请求次数 + 墙钟截止时间。

    五个源在线程里并行跑，所以计数必须加锁——"两个线程各自读到 8、各自发第 9 次"
    是典型的一读一写竞态，而它只在预算刚好用尽时出现，最难复现。

    多方向检索时这是**一份**全局预算（`MULTI_REQUEST_LIMIT`），另有一个按槽位
    （`_Slot`）计的维度：每个方向 5 次。两个维度在同一把锁里判、同一把锁里加，
    所以"两个方向同时抢最后一次全局额度"不会双开。分两次加锁（先判槽位再判全局）
    会留下一个窗口：两边都通过槽位判定、各自 +1，全局就超了 1 次。

    `take()` 同时给出本次请求可用的超时：`min(REQUEST_TIMEOUT, 剩余预算)`。不这么夹
    的话，第 9 次请求仍会拿到满额 10 秒，实际总耗时就能超出 20 秒的预算。
    """

    def __init__(self, limit: int, seconds: float) -> None:
        self._limit = max(int(limit), 0)
        self._deadline = time.monotonic() + max(float(seconds), 0.0)
        self._used = 0
        # 槽位名 → 已用次数。键是方向的序号（`str(index)`），不值钱。
        self._by_slot: dict[str, int] = {}
        self._lock = threading.Lock()

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    def slot_used(self, key: str) -> int:
        with self._lock:
            return self._by_slot.get(key, 0)

    def take(self, slot: _Slot | None = None) -> float | None:
        """拿一次请求额度，返回本次允许的超时；没额度或已过截止时间返回 None。"""
        with self._lock:
            if self._used >= self._limit:
                return None
            key = slot.key if slot is not None else ""
            if key and slot is not None and slot.limit is not None:
                if self._by_slot.get(key, 0) >= slot.limit:
                    return None
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                return None
            self._used += 1
            if key:
                self._by_slot[key] = self._by_slot.get(key, 0) + 1
            return min(REQUEST_TIMEOUT, remaining)


def _directions_enabled() -> bool:
    """拆方向开关，**默认关**。显式设成 1 / true / yes / on 才开。

    默认关的依据写在 `DIRECTIONS_ENV` 上面那段（三条全相关 13/20 → 6/20）。这里再
    强调一句：它不是"功能没做完"，实现是完整的、有 29 条用例；它是"做完了、量过了、
    现在的形态不划算"。要翻默认值，先让 `--directions` 的那张表达标。
    """
    return str(os.getenv(DIRECTIONS_ENV, "")).strip().lower() in _TRUTHY


def _proxy() -> str | None:
    """本次检索用的代理地址；None 表示直连。

    只认显式配置：`MINDMAP_EXTERNAL_PROXY` 优先，其次 `HTTPS_PROXY` / `HTTP_PROXY`，
    都没有就落到 `DEFAULT_PROXY`。`none` / `off` / `direct` 是显式直连——没有这个出口，
    在"机器上本来就有透明代理"的环境里就没法关掉它。
    """
    raw = ""
    for name in (EXTERNAL_PROXY_ENV, "HTTPS_PROXY", "https_proxy",
                 "HTTP_PROXY", "http_proxy"):
        raw = os.getenv(name, "").strip()
        if raw:
            break
    if not raw:
        return DEFAULT_PROXY
    if raw.lower() in ("none", "off", "direct", "0"):
        return None
    return raw


def _client() -> httpx.Client:
    """建一个本次检索用的客户端。

    单独成函数是为了可测：用例 patch 掉它就能拿到完整的假响应，而不用碰
    `httpx.HTTPTransport`（`conftest.py` 的 `no_real_http` 闸会把真请求变成断言失败）。
    """
    return httpx.Client(
        proxy=_proxy(),
        timeout=REQUEST_TIMEOUT,
        trust_env=False,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json, application/atom+xml, */*"},
    )


def clean_text(text: str) -> str:
    """剥标签、反转义、压空白。**只做形态清洗，不做内容判断**。

    顺序是关键：先 `html.unescape` 再删标签。反过来（先删后反转义）会把
    `&lt;script&gt;alert(1)&lt;/script&gt;` 留成一段看得见的 `<script>` 文本——不危险，
    但模型会把它当成"这段代码该被执行"的上下文，而我们要的是它读资料。

    压的只有行内空白，换行保留：父块切分按空行与句末标点走，把换行吃掉会把整篇
    压成一行，切块就只剩句末这一个分隔符了。
    """
    value = html.unescape(text or "")
    value = _BLOCK_TAG_RE.sub(" ", value)
    value = _TAG_RE.sub(" ", value)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = _HSPACE_RE.sub(" ", value)
    value = _BLANK_LINES_RE.sub("\n\n", value)
    return "\n".join(line.strip() for line in value.split("\n")).strip()


def _wiki_markdown(text: str) -> str:
    """维基的 `== 小节 ==` → Markdown `## 小节`。理由见 `_WIKI_HEADING_RE`。

    层级一一对应：维基用两侧等号的数量表示级别（`==` 一级章节、`===` 二级），
    Markdown 用 `#` 的数量，所以 `"=" * n` 直接映射成 `"#" * n`（n 从 2 起）。
    少映射一级会让所有小节挤成同一个 `#` 标题，切块的标题吸附就白做了。
    """
    return _WIKI_HEADING_RE.sub(
        lambda match: "#" * len(match.group(1)) + " " + match.group(2), text)


def truncate_text(text: str) -> str:
    """按 `TEXT_MAX_CHARS` 截断，**结果长度不超上限**（省略号算在里面）。

    省略号不是装饰：正文里出现"…"比出现一个看不出来的硬切口更诚实——用户会知道
    这里少了一截。截断位置先 `rstrip`，免得留半个空格在句尾。
    """
    if len(text) <= TEXT_MAX_CHARS:
        return text
    return text[:max(TEXT_MAX_CHARS - 1, 0)].rstrip() + "…"


def _now() -> str:
    return datetime.now(timezone.utc).strftime(ISO_SECONDS_FORMAT)


def _guess_language(text: str) -> str:
    """按字符集判"zh"还是"en"。**故意不信源自己申报的 `language` 字段。**

    实测 OpenAlex 把《"碳中和"如何"致中和"》标成 `ja`、把《"触电"也要"碳中和"》
    标成 `it`——它的语言检测器在这类短标题上是噪声。字符集不会撒谎：有汉字就是
    `zh`。这个字段唯一的下游用途是 `_interleave` 的 `prefer_language`（中文 query
    时让中文条目先占名额），所以判错一个 `ja`/`it` 的代价就是排序错一位。
    """
    return "zh" if _CJK_RE.search(text or "") else "en"


def _request(
    client: httpx.Client, url: str, *, params: dict[str, Any] | None, timeout: float,
) -> httpx.Response:
    """发一次请求并做两道外部数据的基本校验：状态码、响应体大小。

    非法 JSON **不在这里**判：`Response.json()` 的异常由调用方的 `except Exception`
    收掉，两处各判一次会让"哪一层负责降级"变得含糊。
    返回的 `Response` 已经把正文读进内存，后续解析不会再有网络。
    """
    response = client.get(url, params=params, timeout=timeout)
    if response.status_code >= 400:
        raise ValueError(f"HTTP {response.status_code}")
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise ValueError(f"响应体 {len(response.content)} 字节，超过上限")
    return response


# ---------------------------------------------------------------- 维基百科


def _wiki_pages(payload: Any) -> list[dict[str, Any]]:
    """从 `action=query` 的响应里取出**有正文**的页面。

    同时认 `formatversion=2`（`pages` 是列表）与 1（`pages` 是以 pageid 为键的对象）：
    我们自己请求的是 2，但这条路也能被样本/旧响应喂进来，形状判断放在这里比放在
    解析处更省事。
    """
    if not isinstance(payload, dict):
        return []
    query = payload.get("query")
    if not isinstance(query, dict):
        return []
    pages = query.get("pages")
    if isinstance(pages, dict):
        pages = list(pages.values())
    if not isinstance(pages, list):
        return []
    return [
        page for page in pages
        if isinstance(page, dict) and (page.get("extract") or "").strip()
    ]


def _wiki_search_titles(
    client: httpx.Client, query: str, budget: _Budget, limit: int,
    slot: _Slot | None = None,
) -> list[str]:
    """条目名检索：主题不是精确标题时（"碳中和 政策"），先找候选条目。

    维基的 `titles=` 是**精确**查找，不命中就只回 `missing`。没有这一步，
    "碳中和技术路径"这类输入会一条都拿不到——而它恰恰是最常见的输入形态。
    """
    timeout = budget.take(slot)
    if timeout is None:
        return []
    response = _request(client, WIKIPEDIA_API, params={
        "action": "query", "list": "search", "srsearch": query,
        "srlimit": str(limit), "format": "json", "formatversion": "2",
    }, timeout=timeout)
    payload = response.json()
    search = (payload.get("query") or {}).get("search") if isinstance(payload, dict) else None
    if not isinstance(search, list):
        return []
    return [
        str(item["title"]) for item in search
        if isinstance(item, dict) and isinstance(item.get("title"), str)
        and item["title"].strip()
    ]


def _wikipedia(
    query: str, budget: _Budget, limit: int, *, slot: _Slot | None = None,
    exact_first: bool = True, fetch_limit: int | None = None,
) -> list[ExternalHit]:
    """中文维基：先试精确标题，不命中再走条目检索，然后逐条抓正文。

    请求次数的上界是 1（精确）+ 1（检索）+ limit（逐条）。每一次都过 `budget`：
    预算用尽时立刻返回已经拿到的部分，而不是"再试最后一条"。

    逐条抓取里的任何一次失败都**保留已拿到的条目**：第 2 条超时时把第 1 条一起丢掉，
    等于用一次抖动废掉整个源——而这两件事没有因果关系。

    两个只给多方向路径用的开关（默认值与单方向路径的既有行为一致）：

      - `exact_first=False`：跳过精确标题那一次。方向是「碳中和政策」这类组合短语，
        `titles=` 几乎必然 `missing`，那一次请求是纯浪费——而每个方向只有 5 次额度。
        跳过不等于放弃精确命中：条目检索（`list=search`）对"碳中和"这种真词条名
        同样会把它排在第一，只是多花一次抓取请求。
      - `fetch_limit`：逐条抓取的上限，独立于 `limit`（返回条数）。方向路径传 1，
        因为"多抓一条正文"的边际价值低于"留一次额度给别的源"。
    """
    hits: list[ExternalHit] = []
    fetch_max = limit if fetch_limit is None else max(min(limit, fetch_limit), 0)
    try:
        with _client() as client:
            pages: list[dict[str, Any]] = []
            if exact_first:
                timeout = budget.take(slot)
                if timeout is None:
                    return []
                payload = _request(client, WIKIPEDIA_API, params={
                    "action": "query", "prop": "extracts|info", "inprop": "url",
                    "explaintext": "1", "redirects": "1", "titles": query,
                    "format": "json", "formatversion": "2",
                }, timeout=timeout).json()
                pages = _wiki_pages(payload)

            if not pages and fetch_max > 0:
                # `[:fetch_max]` 是必须的：`srlimit` 由**服务端**执行，而外部源不是可信的
                # 一端——它回 8 条候选时，逐条抓取就会变成 8 次请求。即使 `budget`
                # 最终会兜住次数，那也已经是"为了几条候选把预算烧光"了。
                for title in _wiki_search_titles(
                        client, query, budget, fetch_max, slot)[:fetch_max]:
                    if len(pages) >= fetch_max:
                        break
                    timeout = budget.take(slot)
                    if timeout is None:
                        break
                    try:
                        payload = _request(client, WIKIPEDIA_API, params={
                            "action": "query", "prop": "extracts|info", "inprop": "url",
                            "explaintext": "1", "redirects": "1", "titles": title,
                            "format": "json", "formatversion": "2",
                        }, timeout=timeout).json()
                    except Exception as exc:  # noqa: BLE001 — 单条失败不废整源
                        logger.warning("外部检索：维基条目 %r 抓取失败（%s）：%s",
                                       title, type(exc).__name__, str(exc)[:120])
                        continue
                    pages.extend(_wiki_pages(payload))

            for page in pages[:fetch_max]:
                hit = _wikipedia_hit(page)
                if hit is not None:
                    hits.append(hit)
    except Exception as exc:  # noqa: BLE001 — 外部源失败只降级，理由见模块 docstring
        logger.warning("外部检索：中文维基失败（%s）：%s",
                       type(exc).__name__, str(exc)[:160])
    return hits


def _wikipedia_hit(page: dict[str, Any]) -> ExternalHit | None:
    title = str(page.get("title") or "").strip()
    body = clean_text(_wiki_markdown(str(page.get("extract") or "")))
    if not title or not body:
        return None
    url = str(page.get("fullurl") or "").strip()
    if not url:
        # `inprop=url` 在样本/旧响应里可能缺；按维基的固定形状自己拼一个，
        # 这仍然只是**数据**，不会被本模块请求。
        url = "https://zh.wikipedia.org/wiki/" + quote(title.replace(" ", "_"))
    return ExternalHit(
        title=title, text=truncate_text(body), url=url,
        source_name="wikipedia", fetched_at=_now(), language="zh",
        raw_chars=len(body),
    )


# ---------------------------------------------------------------- arXiv


def _arxiv_terms(query: str) -> str:
    """把主题变成 arXiv 的检索式。

    用 `all:"…"` 而不是 `all:词1 词2`：后者是分词后的布尔组合，实测按
    `all:carbon neutrality policy` 命中过"碳纳米材料毒性"——完全跑题的条目会
    污染整棵树，而一条跑题的引用比没有引用更糟（它看起来有出处）。
    短语检索返回空是更好的失败。
    """
    return 'all:"' + re.sub(r'"', " ", query).strip() + '"'


def _arxiv(
    query: str, budget: _Budget, limit: int, *, slot: _Slot | None = None,
) -> list[ExternalHit]:
    """arXiv Atom 接口。用 `xml.etree` 解析，不引新依赖。

    中文主题在这里通常返回 0 条（arXiv 的语料基本是英文），这不是 bug、也不该被
    翻译成英文再查：凭空翻译一个术语就是凭空造一条检索式，而用户输入的语言是
    他的意思的一部分。0 条时维基那条源仍然有结果。
    """
    timeout = budget.take(slot)
    if timeout is None:
        return []
    try:
        with _client() as client:
            response = _request(client, ARXIV_API, params={
                "search_query": _arxiv_terms(query), "max_results": str(limit),
            }, timeout=timeout)
        return _parse_arxiv(response.text, limit)
    except Exception as exc:  # noqa: BLE001 — 外部源失败只降级，理由见模块 docstring
        logger.warning("外部检索：arXiv 失败（%s）：%s",
                       type(exc).__name__, str(exc)[:160])
        return []


def _parse_arxiv(payload: str, limit: int) -> list[ExternalHit]:
    """Atom → `ExternalHit`。**解析失败抛异常**，由 `search_external` 的兜底收掉。

    单独成函数是为了让用例能拿固定 XML 直接喂进来（不联网、不 patch 客户端）。
    """
    root = ElementTree.fromstring(payload)
    hits: list[ExternalHit] = []
    for entry in root.findall(f"{_ATOM}entry"):
        if len(hits) >= limit:
            break
        title = clean_text((entry.findtext(f"{_ATOM}title") or ""))
        summary = clean_text((entry.findtext(f"{_ATOM}summary") or ""))
        if not title or not summary:
            continue
        raw_url = (entry.findtext(f"{_ATOM}id") or "").strip()
        # arXiv 的 `<id>` 是 `http://arxiv.org/abs/2310.01234v1`。换成 https 只是
        # 让浏览器别吃一次重定向，值本身不再被本模块请求。
        url = re.sub(r"^http://", "https://", raw_url)
        hits.append(ExternalHit(
            title=title, text=truncate_text(summary), url=url,
            source_name="arxiv", fetched_at=_now(), language="en",
            raw_chars=len(summary),
        ))
    return hits


# ---------------------------------------------------------------- OpenAlex


# `abstract_inverted_index` 里的位置索引上限。外部源给什么数字都得先当敌意输入：
# 一个 `[10**9]` 能让下面的 `sorted` 与字典分配吃掉几百 MB，而这只是一个字段。
MAX_ABSTRACT_POSITION = 100_000


def _openalex_abstract(inverted: Any) -> str:
    """OpenAlex 的 `abstract_inverted_index` → 一段文本。

    它的形状是 `{单词: [出现位置, …]}`——存的是倒排索引而不是字符串（省空间）。
    重建就是按位置把词排回去。

    中文实测可用：OpenAlex 对中文摘要的分词位置是连续的，重建后是
    `目前全球气温已达过去12万年来最高值…` 这样的正常句子，不带多余空格。
    """
    if not isinstance(inverted, dict):
        return ""
    positions: dict[int, str] = {}
    for word, indexes in inverted.items():
        if not isinstance(word, str) or not isinstance(indexes, list):
            continue
        for index in indexes:
            if isinstance(index, int) and 0 <= index < MAX_ABSTRACT_POSITION:
                positions[index] = word
    if not positions:
        return ""
    return " ".join(positions[index] for index in sorted(positions))


def _parse_openalex(payload: Any, limit: int) -> list[ExternalHit]:
    """OpenAlex 的 `results[]` → `ExternalHit`。**没有摘要的条目直接丢弃**。

    只有标题的条目对生成毫无用处：它会作为"一条有出处的资料"进入清单，让模型以为
    有据可查，而它实际上只有一个名字。宁可少一条。

    单独成函数是为了让用例能拿固定 JSON 直接喂进来（不联网、不 patch 客户端）。
    """
    if not isinstance(payload, dict):
        return []
    results = payload.get("results")
    if not isinstance(results, list):
        return []
    hits: list[ExternalHit] = []
    for work in results:
        if len(hits) >= limit:
            break
        if not isinstance(work, dict):
            continue
        title = clean_text(str(work.get("title") or "")).strip()
        body = clean_text(_openalex_abstract(work.get("abstract_inverted_index")))
        if not title or not body:
            continue
        # 原文地址按"能点回真正落地页"降序取：`primary_location` 是出版社/库的
        # 落地页，`doi` 能换出落地页，`id`（openalex.org/works/W…）是最后的兜底。
        location = work.get("primary_location")
        url = ""
        if isinstance(location, dict):
            url = str(location.get("landing_page_url") or "").strip()
        if not url:
            url = str(work.get("doi") or "").strip()
        if not url:
            url = str(work.get("id") or "").strip()
        hits.append(ExternalHit(
            title=title, text=truncate_text(body), url=url,
            source_name="openalex", fetched_at=_now(),
            language=_guess_language(title), raw_chars=len(body),
        ))
    return hits


def _openalex(
    query: str, budget: _Budget, limit: int, *, slot: _Slot | None = None,
) -> list[ExternalHit]:
    """OpenAlex 检索。免费、无需 key，**对中文 query 直接有效**。

    实测 `search=碳中和` 返回 70562 条命中，取前 20 条里 20 条是中文标题、
    20 条有摘要（中位 213 字）；加 `filter=language:zh` 则有 37138 条。

    所以这里**不做"中文→英文"翻译**：它能直接吃中文，翻译反而会把一条中文命中
    换成三条不那么相关的英文命中。

    无 key 时有一个按出口 IP 共享的日预算（响应里的 `meta.notice` 会说明），
    超了就是 429。429 走 `_request` 的 `status_code >= 400` 那条路，降级为空——
    这与"源挂了"在行为上一致，日志里会写明是 OpenAlex。
    """
    timeout = budget.take(slot)
    if timeout is None:
        return []
    try:
        with _client() as client:
            payload = _request(client, OPENALEX_API, params={
                "search": query, "per-page": str(SOURCE_CANDIDATES),
            }, timeout=timeout).json()
        return _parse_openalex(payload, limit)
    except Exception as exc:  # noqa: BLE001 — 外部源失败只降级，理由见模块 docstring
        logger.warning("外部检索：OpenAlex 失败（%s）：%s",
                       type(exc).__name__, str(exc)[:160])
        return []


# ---------------------------------------------------------------- Crossref


def _parse_crossref(payload: Any, limit: int) -> list[ExternalHit]:
    """Crossref 的 `message.items[]` → `ExternalHit`。同样丢弃无摘要的条目。

    Crossref 的 `abstract` 是 JATS XML（`<jats:p>…</jats:p>`），`clean_text` 的
    "先反转义再剥标签"正好把它变成正文——顺序反过来会把 `&lt;jats:p&gt;` 留成
    一段看得见的标签文本。

    单独成函数是为了让用例能拿固定 JSON 直接喂进来。
    """
    if not isinstance(payload, dict):
        return []
    message = payload.get("message")
    if not isinstance(message, dict):
        return []
    items = message.get("items")
    if not isinstance(items, list):
        return []
    hits: list[ExternalHit] = []
    for item in items:
        if len(hits) >= limit:
            break
        if not isinstance(item, dict):
            continue
        # `title` 有时是列表、有时是字符串（旧条目），两种都要认。
        raw_title = item.get("title")
        if isinstance(raw_title, list):
            raw_title = raw_title[0] if raw_title else ""
        title = clean_text(str(raw_title or "")).strip()
        body = clean_text(str(item.get("abstract") or ""))
        if not title or not body:
            continue
        url = str(item.get("URL") or "").strip()
        if not url:
            doi = str(item.get("DOI") or "").strip()
            url = f"https://doi.org/{doi}" if doi else ""
        hits.append(ExternalHit(
            title=title, text=truncate_text(body), url=url,
            source_name="crossref", fetched_at=_now(),
            language=_guess_language(title), raw_chars=len(body),
        ))
    return hits


def _crossref(
    query: str, budget: _Budget, limit: int, *, slot: _Slot | None = None,
) -> list[ExternalHit]:
    """Crossref 检索。免费、无需 key，对中文 query 有效（中文标题 19/20）。

    但**摘要覆盖率只有约一半**（实测 20 条里 9–12 条带 abstract）——剩下的
    只有标题，会被 `_parse_crossref` 丢掉。所以这个源在生产上比 OpenAlex 稀疏，
    两个都接是为了"一个没摘要时另一个还有"。

    `USER_AGENT` 里带了邮箱，正好满足 Crossref 的 polite pool 要求（它明确
    建议把联系方式放在 UA 或 `mailto` 参数里，别把匿名流量当默认）。
    """
    timeout = budget.take(slot)
    if timeout is None:
        return []
    try:
        with _client() as client:
            payload = _request(client, CROSSREF_API, params={
                "query": query, "rows": str(SOURCE_CANDIDATES),
                "mailto": POLITE_MAILTO,
            }, timeout=timeout).json()
        return _parse_crossref(payload, limit)
    except Exception as exc:  # noqa: BLE001 — 外部源失败只降级，理由见模块 docstring
        logger.warning("外部检索：Crossref 失败（%s）：%s",
                       type(exc).__name__, str(exc)[:160])
        return []


# ---------------------------------------------------------------- 快懂百科


def _baike_paragraph(block: str) -> str:
    """一个 `baike-render-paragraph` 块 → 一行纯文本。

    段落内的文字被服务端拆成一串 `<span>`（每个词、每条内链各一个），直接拼 span
    内容会丢词间空格（`Carbon` + ` Neutrality` 被各自的 `strip` 一挤就成了
    `CarbonNeutrality`）。所以**先剥标签再统一压空白**——标签之间的空格本来就是
    正文的一部分，压一次就够。

    内链 `<a>` 的文字留在这里是对的：它是句子的一部分（"途径是减少碳排放和进行
    碳交易"），只是不该把 `href` 带进正文。
    """
    value = html.unescape(_BLOCK_TAG_RE.sub(" ", block))
    # 标签替换成**空串**而不是空格：段落块里只有行内 `span`/`a`，词间空格由 span
    # 的内容自带（实测第三片是 `" Neutrality）…"`，前导空格就在里面）。换成空格
    # 会得到 `碳中和（ Carbon Neutrality）` 这种中文括号后多一个空格的样子。
    value = _BAIKE_TAG_RE.sub("", value)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    return _HSPACE_RE.sub(" ", value).strip()


def _parse_baike(page: str, limit: int, url: str = "") -> list[ExternalHit]:
    """快懂百科词条页 → 最多一条 `ExternalHit`。**解析失败返回空，不抛**。

    一次请求就是一页一个词条，所以这里天然最多 1 条；`limit` 只用于"调用方一条
    都不要"的短路。

    正文按 `baike-render-paragraph` 段落拼接、段落之间留空行：切块器按空行与句末
    标点分块，段间没有空行整篇就会挤成一块。

    站内搜索**不做**：`robots.txt` 的 `User-agent: *` 组明确 `Disallow: /search`，
    而 `/wiki/*` 是 `Allow: /`。所以这个源只做精确词条命中——"碳中和技术路径"
    这种输入在这里是 404，由维基的条目检索兜住。

    `url` 由调用方给**重定向落地后**的地址；留空时下游 `safe_http_url` 折成
    None、界面不渲染链接——好过拼一个可能不存在的 `/wiki/<title>`。

    单独成函数是为了让用例能拿固定的 HTML 文本直接喂进来。
    """
    if limit <= 0:
        return []
    title_match = _BAIKE_TITLE_RE.search(page)
    title = clean_text(title_match.group(1)) if title_match else ""
    paragraphs = [
        text for text in (_baike_paragraph(block) for block in _BAIKE_PARA_RE.findall(page))
        if text
    ]
    body = "\n\n".join(paragraphs)
    if not title or not body:
        return []
    return [ExternalHit(
        title=title, text=truncate_text(body), url=url,
        source_name="baike", fetched_at=_now(), language="zh",
        raw_chars=len(body),
    )]


def _baike(
    query: str, budget: _Budget, limit: int, *, slot: _Slot | None = None,
) -> list[ExternalHit]:
    """快懂百科（`baike.com`，原互动百科）：**唯一实测 robots 允许的中文百科**。

    为什么是它而不是百度百科/知乎：那两个站的 `robots.txt` 在 `User-agent: *`
    组下都是 `Disallow: /`（全站拒绝未列名爬虫），百度百科的词条页还会返回
    `window.__PARIS_CHALLENGE_START__` 的 JS 挑战页而不是正文。快懂百科是
    `Allow: /`（只禁 `/search`、`/user`、`/edit_community` 等），词条页服务端
    直接渲染正文，不需要任何绕过。这条差别是这个源存在的唯一理由。

    正文实测偏短（"碳中和" 725 字、"量子计算" 881 字、"光合作用" 256 字），
    比中文维基（"碳中和" 2704 字）短得多。它的价值是**第二个中文百科视角**，
    不是替代维基。
    """
    timeout = budget.take(slot)
    if timeout is None:
        return []
    try:
        with _client() as client:
            response = _request(
                client, BAIKE_WIKI_URL + quote(query), params=None, timeout=timeout)
        # 落地 URL 用**跟随重定向之后**的地址：`/wiki/碳中和` 会 302 到
        # `/wikiid/7194…`，存后者才是一个稳定可点的链接（前者依赖词条名，
        # 改名的词条会 404）。
        return _parse_baike(response.text, limit, url=str(response.url))
    except Exception as exc:  # noqa: BLE001 — 外部源失败只降级，理由见模块 docstring
        logger.warning("外部检索：快懂百科失败（%s）：%s",
                       type(exc).__name__, str(exc)[:160])
        return []


# ---------------------------------------------------------------- 聚合


def _interleave(
    hits: Sequence[ExternalHit], limit: int, *, prefer_language: str | None = None,
) -> list[ExternalHit]:
    """按 `source_name` 交替取。

    不交替的后果是一边倒：维基一次能给 3 条、arXiv 常给 0–3 条，直接拼接会让维基
    占满全部名额，用户拿到的"资料"其实是同一个百科条目的三个邻接条目。交替之后，
    只要两个源都有结果，就至少各占一条。

    `prefer_language` 解决的是交替之后仍然存在的一个坑：**5 个源抢 3 个名额**时，
    排在后面的源一条都进不了结果。中文 query 下如果不偏好中文，"碳中和"可能拿到
    三条英文论文摘要（OpenAlex/Crossref 的英文命中比中文多得多），而用户输入的是
    中文。所以中文 query 时把中文桶提到前面——桶内顺序不动，源之间也不打乱，
    只是"先给谁名额"。

    只有 `prefer_language is not None` 才排序。英文 query 传 None，走原来的顺序：
    一个"猜出来的偏好"不该改写英文检索的既有行为。
    """
    buckets: dict[str, list[ExternalHit]] = {}
    for hit in hits:
        buckets.setdefault(hit.source_name, []).append(hit)
    queues = [queue[:] for queue in buckets.values()]
    if prefer_language is not None:
        # `sorted` 是稳定排序：同语言内保持源的收集顺序，不会打乱既有交替。
        queues.sort(key=lambda queue: 0 if queue[0].language == prefer_language else 1)
    merged: list[ExternalHit] = []
    while len(merged) < limit:
        taken = False
        for queue in queues:
            if not queue or len(merged) >= limit:
                continue
            merged.append(queue.pop(0))
            taken = True
        if not taken:
            break
    return merged


def _query_language(query: str) -> str | None:
    """query 是不是中文。**只回 "zh" 或 None，从不回 "en"。**

    回 "en" 意味着英文 query 时把中文源压到后面，而中文源在英文主题上经常也有
    结果（中文维基有"伦敦"、Crossref 有英文标题的中文期刊论文）。那是一种没有
    证据支撑的偏好。中文 query 相反：用户用中文问，就该先看到中文材料。
    """
    return "zh" if _CJK_RE.search(query or "") else None


def _run_source(name: str, fetch: Any) -> list[ExternalHit]:
    """跑一个源，**永不抛**。

    每个源自己已经吞过异常；这里再兜一层是因为"某个源写错一个字段名"不该让
    整天生成挂掉——而 `AttributeError` 恰恰不会被源内部的 `except Exception` 之外
    的任何地方拦住。
    """
    try:
        return list(fetch())
    except Exception as exc:  # noqa: BLE001 — 外部源失败只降级，理由见模块 docstring
        logger.warning("外部检索：%s 失败（%s）：%s",
                       name, type(exc).__name__, str(exc)[:160])
        return []


# 单方向检索的源清单与顺序。**顺序不能改**：`_interleave` 按源的收集顺序建桶，
# 而 `prefer_language` 用的是稳定排序——换了顺序，同一批命中会以另一种次序占名额。
DEFAULT_SOURCES: tuple[str, ...] = ("wikipedia", "arxiv", "openalex", "crossref", "baike")


def _search_one(
    query: str, limit: int, *, budget: _Budget, slot: _Slot | None = None,
    sources: Sequence[str] = DEFAULT_SOURCES, exact_first: bool = True,
    fetch_limit: int | None = None,
) -> list[ExternalHit]:
    """打**一轮**源，返回全部候选：**不筛、不去重、不截断**。

    这是两条路共用的那一轮（单方向 5 源、多方向的每个方向 4 源）。出口的三件事
    留给调用方，因为它们的位置不同：

      - 单方向：先聚合 → 按 query 过门 → `_interleave` 取前 `limit`；
      - 多方向：先按**方向**过门 → 跨方向合并去重 → 方向内按源交替 → 跨方向轮转。

    并行在这里而不是在调用方：串行时最坏耗时是五个 10 秒超时相加，而它们都只是
    "多等一会儿"，没有任何顺序依赖。多方向时每个方向各有一个线程池，共享同一份
    `_Budget`（全局 + 槽位两个维度，见 `_Budget.take`）。
    """
    registry: dict[str, Any] = {
        "wikipedia": lambda: _wikipedia(
            query, budget, limit, slot=slot, exact_first=exact_first,
            fetch_limit=fetch_limit),
        "arxiv": lambda: _arxiv(query, budget, limit, slot=slot),
        "openalex": lambda: _openalex(query, budget, limit, slot=slot),
        "crossref": lambda: _crossref(query, budget, limit, slot=slot),
        "baike": lambda: _baike(query, budget, limit, slot=slot),
    }
    if slot is None and exact_first and fetch_limit is None:
        # 单方向路径：按**三个位置参数**调用，与 `3c40ba6` 的形态完全一样。
        # 这不是为了好看——既有用例（`tests/test_external_relevance.py` 的
        # `_bare_sources`）把五个源替换成 `lambda q, budget, limit: ...`，多传一个
        # kwarg 就会 TypeError。而"降级与基线逐字节一致"本来也包括**调用形态**。
        registry = {
            "wikipedia": lambda: _wikipedia(query, budget, limit),
            "arxiv": lambda: _arxiv(query, budget, limit),
            "openalex": lambda: _openalex(query, budget, limit),
            "crossref": lambda: _crossref(query, budget, limit),
            "baike": lambda: _baike(query, budget, limit),
        }
    planned = [(name, registry[name]) for name in sources if name in registry]
    collected: list[ExternalHit] = []
    if not planned:
        return collected
    with ThreadPoolExecutor(max_workers=len(planned)) as pool:
        futures = [pool.submit(_run_source, name, fetch) for name, fetch in planned]
        for (name, _), future in zip(planned, futures):
            hits = future.result()
            collected.extend(hits)
            logger.debug("外部检索：%s 返回 %d 条（累计请求 %d 次）",
                         name, len(hits), budget.used)
    return collected


# ---------------------------------------------------------------- 相关性门

# 判定用的三个数：看多长的正文、英文词多短算噪声、中文要求几个字。判据本身见
# `_relevant_hits`，这几个数各自的来历见紧跟着的注释与 `_phrase_terms`。

# 只看**标题 + 正文开头**。正文可以长到 `TEXT_MAX_CHARS`（8000）字，而判"这条讲的
# 是不是 query 那件事"只需要开头：跑题命中在开篇就暴露了（实测 `rust 学习路线` 拿到
# 「陳淑樺」，正文第一段就是歌手生平）。300 不是新数字——它是 `LEAF_MAX_CHARS` 与
# `SourceRef.quote.max_length` 的同款口径，也就是"一条引用装得下的字数"：门放行的
# 深度与引用能取证的深度重合，更靠后的正文本来就长不出一句可校验的引用。
RELEVANCE_CHARS = 300

# 参与判定的英文词最短长度。1–2 字符的词在中文主题的正文里是纯噪声（`ai`、`os`、
# `ui`、`no`），命中它们说明不了任何事；3 是全大写缩写（TCP / GPT / OCR）的下界。
MIN_ASCII_TERM = 3

# 中文判定词块的长度：query 的中文段去掉开头虚词后，要求**头这么多个字**原样出现。
# 3 的来历见 `_phrase_terms` 的 docstring（2 字等于 bigram 口径，更长会打死
# "概念 + 限定语"这一类查询）。
MIN_PHRASE_CHARS = 3

# 只认"以字母开头"的英文词。`2024` / `3` 这种纯数字在任何正文里都有，拿它当相关
# 证据等于没判。小写是因为 `_token_list` 已经把英文词全部小写化。
_ASCII_TERM_RE = re.compile(r"^[a-z][a-z0-9]*$")

# query 的中文词块 = 连续的 CJK 段。这是**切段**不是分词：真正的分词（中文 bigram /
# 英文整词）只有 `_token_list` 一套，这里只负责把"中英混排"里的中文部分整段取出来，
# 好让"移动语义"不被 `C++` 的空格切成两截。与 `_CJK_RE` 同一个字符区间。
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")

# 句首虚词：出现在查询词块开头时没有判别力、要先跳过的字。
#
# 用途只有一处（`_phrase_terms`）：`为什么中文大模型…` 的前三个字全是虚词，不跳过
# 的话判据会去匹配"为什么"，一篇讲足球的文章只要用了这三个字就能混进来。
#
# 为什么这张表**不含** `所`/`最`/`一`/`因`/`更`/`太`/`很`：它们同样是实词的开头
# （`所有权`、`最终一致性`、`一致性`、`因果`、`更新`、`太阳`、`很少`）。混在一起
# 会把 `所有权` 剥成 `有权`、把 `一致性` 剥成 `致性`——判据要求的那 3 个字随之变弱，
# 噪声正好从这些缝里进来。
#
# 跳过句首虚词对召回没有代价：命中若含整个词块，必然也含跳完前缀后的那一段
# （子串关系），所以这一步只会放宽"命中只给出核心词条"的情形。
_LEADING_FILLER_CHARS = frozenset(
    "的了着过是在和与及或而就也都还又再把被从对向为以于其之这那个们"
    "我你他她它么呢吗吧啊呀嘛哦什怎"
)


def _word_terms(text: str) -> frozenset[str]:
    """一段文字里的英文词（小写、≥`MIN_ASCII_TERM`、以字母开头）。

    分词**直接用 `knowledge.service._token_list`**（BM25 那条路用的同一份实现），
    不在这里另写一套：门与检索对"什么算同一个词"必须只有一个答案，两套实现迟早
    分叉，而分叉的表现是"检索认为命中、门认为无关"这种谁也说不清的自相矛盾。
    """
    words: set[str] = set()
    for token in _token_list(text):
        if (token.isascii() and len(token) >= MIN_ASCII_TERM
                and _ASCII_TERM_RE.match(token)):
            words.add(token)
    return frozenset(words)


def _phrase_terms(text: str) -> frozenset[str]:
    """query 的判定词块：每个中文连续段**去掉开头虚词后的头 `MIN_PHRASE_CHARS` 个字**。

    为什么不是"整块出现"（第一版）：`碳中和技术路径` 的命中是维基的 `碳中和` 词条，
    整块判据会把它判成无关——而"概念 + 限定语"是最常见的查询形状（`碳中和 技术路径`、
    `Rust 所有权`、`深度学习 路线`）。既有用例
    （`test_wikipedia_falls_back_to_search_when_title_misses`）钉的正是这件事：
    搜不到精确标题时退回条目检索，`碳中和` 必须还能用。

    为什么是**前缀**而不是任意 2 字窗口（任务最初给的 bigram 口径）：中文查询的
    修饰语在前、中心词在后，命中往往只给出核心词条。任意窗口等于把尾部那个最泛的
    中心词也当成证据——`一致性` 会命中"无线电一致性测试"、`学习路线` 会命中
    "深度学习路线"的教学模式论文。取最左的内容字，留下的恰好是限定语。

    为什么是 3 个字：2 字重合等于 bigram 口径（实测噪声一条不少、还误杀简繁）；
    再长就把"概念 + 限定语"整类打死。3 是"一个中文实词"的下界，也是缩写
    （`碳中和`/`注意力`）的长度。

    开头虚词要先跳过去：`为什么中文大模型…` 的前 3 个字全是虚词，不跳的话判据会
    去匹配"为什么"，一篇讲足球的文章只要用了这三个字就能混进来。跳完之后要求
    `中文大` 出现——那才是这个查询的内容起点。
    """
    phrases: set[str] = set()
    for run in _CJK_RUN_RE.findall(text):
        start = 0
        while start < len(run) and run[start] in _LEADING_FILLER_CHARS:
            start += 1
        core = run[start:]
        if len(core) < 2:
            # 整段都是虚词（`的了`、`为什么`）：没有可判的内容，不参与比对。
            continue
        phrases.add(core[:MIN_PHRASE_CHARS])
    return frozenset(phrases)


def _hit_blob(hit: ExternalHit) -> str:
    """判定用的命中文本：标题 + 正文前 `RELEVANCE_CHARS` 字（理由见那个常量）。"""
    return f"{hit.title}\n{hit.text[:RELEVANCE_CHARS]}"


def _qualifier_terms(text: str) -> tuple[frozenset[str], frozenset[str]]:
    """限定语的判定词面：**全部中文 2-gram** + 全部英文整词。

    与 `_phrase_terms` 的"只取头 3 字前缀"不同：这里要判的是"命中里有没有提到这个
    限定语"，限定语本身只有 2-4 个字（`政策`、`技术路径`、`企业案例`），取前缀会
    把「技术路径」压成「技术路」——一个几乎不可能原样出现的串。滑 2-gram 之后
    「路径」「案例」这些真实会被写进标题的词才留得住。

    代价是 2-gram 本来就宽（`路径` 在无关文章里也常见）。这是词面判据的天花板，
    与 `_relevant_hits` 是同一类取舍：宁可放过几条同形噪声，也不能把方向整轮判成空的。
    """
    phrases: set[str] = set()
    for run in _CJK_RUN_RE.findall(text):
        for index in range(len(run) - 1):
            gram = run[index:index + 2]
            if not all(char in _LEADING_FILLER_CHARS for char in gram):
                phrases.add(gram)
    return frozenset(phrases), _word_terms(text)


def _direction_qualifier(topic: str, direction: str) -> str:
    """方向里**比主题多出来的那部分**：`碳中和政策` → `政策`；`Rust 内存模型` → `内存模型`。

    这一步是"按方向施门"能成立的前提。方向按提示词的要求以主题原词开头，而
    `_phrase_terms` 只取头 3 字——`_phrase_terms("碳中和政策")` 恰好等于
    `_phrase_terms("碳中和")`。也就是说，直接拿方向去调 `_relevant_hits`，判据会
    **退化成主题门**：一条"讲碳中和但完全不讲政策"的命中照样通过。拆方向的意义
    就在这里丢掉。

    切除用的是主题的判定词块/英文词在方向里最长的那一次匹配（不是"去掉方向前
    若干字"）：模型偶尔会把主题写在后面（`政策视角下的碳中和`），按位置切就会
    切出一段没有意义的尾巴。
    """
    if not direction:
        return ""
    anchors = [phrase for phrase in _phrase_terms(topic) if phrase in direction]
    lowered = direction.lower()
    anchors += [word for word in _word_terms(topic) if word in lowered]
    if not anchors:
        # 方向不含主题原词（比如主题「碳中和」、方向「碳交易市场」）。这时方向的
        # 全文就是限定语——它得自己站得住。
        return direction
    anchor = max(anchors, key=len)
    position = lowered.find(anchor.lower())
    if position < 0:
        return direction
    rest = direction[:position] + " " + direction[position + len(anchor):]
    return rest.strip(" \t\u3000，,、·:：-|/（）()")


def _direction_gate(
    topic: str, direction: str, hits: Sequence[ExternalHit],
) -> list[ExternalHit]:
    """**按方向**过词面门：先过既有那道门，再要求命中提到这个方向的限定语。

    两道顺序不能换：第一道（`_relevant_hits(direction, ...)`）保证命中讲的是这个
    主题，第二道（限定语）保证它讲的是**这个方面**。只有第二道时，一条只含"政策"
    二字的无关文章也能进；只有第一道时，方向门退化成主题门（理由见
    `_direction_qualifier`）。

    第二道**不退回**：被限定语挡掉的命中不留作后备。理由与 `_relevant_hits` 的
    "被滤掉的槽位由候选池补位"不同——那里的候选池是同一次查询的候选，这里的后备
    是"另一个方向"的东西，挪过来只会让报告里"政策那个方向拿回了什么"变成谎话。
    代价是某个方向可能一条都不剩（实测里维基对「碳中和政策」就是这样），那由
    跨方向轮转从别的方向补名额，不是故障。
    """
    passed = _relevant_hits(direction, hits)
    qualifier = _direction_qualifier(topic, direction)
    if not qualifier:
        return passed
    phrases, words = _qualifier_terms(qualifier)
    if not phrases and not words:
        return passed
    kept: list[ExternalHit] = []
    dropped: list[str] = []
    for hit in passed:
        blob = _hit_blob(hit)
        if any(gram in blob for gram in phrases) or words & _word_terms(blob):
            kept.append(hit)
        else:
            dropped.append(f"{hit.source_name}/{hit.title[:40]}")
    if dropped:
        # 只记数量，不记方向文本：方向是主题的改写，泄漏程度与主题同级；而这条日志
        # 每个方向都会触发一次，把文本抄进去等于在 info 级别重复一遍主题。
        # "哪个方向挡了几条"由 `_search_many` 的汇总行回答（那是一行一条）。
        logger.info("外部检索：限定语闸挡掉 %d 条（方向与主题的差异部分共 %d 字）",
                    len(dropped), len(qualifier))
    return kept


def _relevant_hits(query: str, hits: Sequence[ExternalHit]) -> list[ExternalHit]:
    """按**词面**相关性过滤命中。判据见下方"判据"一节。

    ## 为什么需要这道门

    "搜到"不等于"搜准"：外部源按自己的相似度排序，而它们对中文主题的理解偏差
    很大。实测 `rust 学习路线` 拿到「陳淑樺」维基页与一篇卢托斯拉夫斯基音乐论文；
    `Rust 所有权` 拿到"农业"（rust 是锈病）；`C++ 移动语义` 三条里两条是语言学；
    `zzqx 不存在的词` 也照样返回 3 条 crossref 垃圾。这些命中随后**会变成引用**
    ——逐字可校验、能点回原文、界面上写着"依据来自外部检索"，而它们与主题毫无
    关系。整条链最怕的正是这种失败形态：不是没有依据，是一份看起来正规的假依据。

    ## 判据

    命中必须在 `标题 + 正文前 300 字` 里**原样包含** query 的某个中文词块
    （连续 CJK 段，≥2 字，非纯虚词），或包含 query 里的某个英文词
    （≥3 字符，大小写不敏感，按整词比）。两条任一成立即保留。

    ## 放行：判据说不出话时，沉默

    两类命中不过判据也**放行**：

      - **query 切不出任何可比词面**（纯符号、纯数字、`C++` 只剩一个被
        `MIN_ASCII_TERM` 裁掉的 `c`）：判据在这里没有表决权，把"判不了"当成
        "都不相关"会让这些主题一条资料都拿不到。
      - **跨语言**：query 与命中在书写系统上没有交集（中文 query 配全英文命中，
        或英文 query 配纯中文正文）。判据在那里无从比较，放行是为了不误杀
        （实测救回 `consensus algorithm` 的中文正文 `Raft`）；代价见"天花板"。

    ## 天花板（方法本身的上限，不是"没调好"）

    词面重合只能证明"两边出现了同一串字符"，不能证明"讲的是同一件事"。三类它
    永远分不开，实测各有实例（数据在 `scripts/measure_external_relevance.py`）：

      - **同形异义**：`Rust`（语言）/ `rust`（锈病）、`Cursor`（编辑器）/ `cursor`
        （光标）、`consensus`（共识算法）/ `Consensus`（RANSAC 的全名）、
        `学习路线`（学习规划）/ `深度学习路线`（教学模式）。判据看见字符就放行。
      - **简繁差异**：`用戶畫像` 不含 `用户画像`，一条真相关的维基条目会被滤掉
        （实测 1 条）。要修得引入简繁映射表，那是一个新的数据依赖，代价大于收益。
      - **跨语言**：中文 query 与全英文命中之间没有可比的字符。`注意力机制` 的相关
        命中 `Attention Is All You Need` 靠放行才留得住；同一条放行也让
        `Rust 所有权` 有可能收下纯英文的小麦锈病论文（实测未触发：那批命中带中文
        摘要，不走放行支路；arXiv 全英文语料会走）。

    ## 实测（`scripts/measure_external_relevance.py`，20 主题同一份候选池）

    | | 无门 | 前缀 3 字 + 放行（本实现） |
    |---|---|---|
    | 返回条数 | 60 | 49 |
    | 噪声条目（人工判定） | 22 | 10 |
    | 三条全相关的主题 | 10/20 | 12/20 |
    | 成功率（≥1 条） | 100% | 95% |
    | 误杀（判为相关却被滤） | 0 | 1 |

    同一份池子上另外两条口径的成绩，是"为什么不是它们"的依据：

    | | 返回条数 | 噪声 | 三条全相关 | 成功率 | 误杀 |
    |---|---|---|---|---|---|
    | bigram（任务最初给的口径） | 59 | 22 | 9/20 | 100% | 2 |
    | 整块词块（第一版） | 47 | 9 | 11/20 | 85% | 2 |

    bigram 口径**噪声一条没少、三条全相关还掉了一个**：它挡下的只是"完全没共享
    词面"的少数命中，而源站自己的排序已经很少返回那种东西；它唯一的"功劳"是误杀
    ——`用戶畫像`（繁体）不含 `用户画像`（简体）。整块口径多砍了一条噪声，却把
    `碳中和技术路径 → 碳中和` 这类"概念 + 限定语"查询整类打死（成功率掉到 85%）。

    残余的 10 条噪声里 **9 条是靠词面命中的**（`幻觉` 的戏曲与决定论论文、`晶体学`
    论文——正文写着"最终一致性因子为R=0.030"、`Rust` 的小麦锈病、`cursor` 的光标
    论文、`学习路线` 的教学模式），另外 1 条是跨语言放行进来的。这道门一条都挡不住
    ——那是词面判据的天花板，不是阈值问题。

    要跨过这条线得用语义判据：`knowledge/rerank.score_documents(query, documents)`
    已经能给出真正的相关性分数（`qwen3-rerank`）。**量过了，没接**——接上之后
    （同一份候选池、同一批 20 个主题、分数真打 `qwen3-rerank`）的读数是：

    | | 返回条数 | 噪声 | 三条全相关 | 成功率 | 重复标题 |
    |---|---|---|---|---|
    | 词面门（本实现） | 49 | 10 | 12/20 | 95% | 3 |
    | 词面门 + rerank | 49 | 9 | 12/20 | 95% | 5 |

    噪声少 1 条、三条全相关一条没动，代价是每次外部检索多 0.74 秒中位（合计中位
    4.20→5.08 秒）、重复标题 3→5（重排取代 `_interleave` 之后，同一标题来自两个源
    时两条都会进结果）。按"全相关 16/20 以上、噪声 5 条以下"的判据，这叫"只动了一
    两条"。

    根因有三条，都不是调阈值能解决的：

      - **重排在最脏的地方没机会开口**：门槛是"过门候选 > `limit`"，而最脏的三个
        主题过门候选恰好是 3 / 1 / 0 条，一次都不触发；
      - **16/20 是候选池的物理上限**：池内一条相关候选都没有的主题有 4 个
        （`加密货币的量子套利`、`为什么中文大模型…`、`rust 学习路线`、`zzqx`），
        任何排序或阈值都救不回来，得换源或扩源；
      - **绝对分数跨查询不可比**：晶体学论文 0.7108 比 `TCP 三次握手` 的真依据
        （0.4738）、`Rust 所有权` 的真依据（0.5635）都高，切它必然切掉别的主题的
        真依据；同一主题内用**相对**阈值（≥0.90×top1）才分得开，而那一格是
        15/20、4 条噪声、返回条数从 49 腰斩到 27。

    真要走语义判据，它是**替换**这道门而不是叠加：实测里"不要词面门、只按分数阈值
    筛"与"门 + 重排 + 阈值"在每一个阈值上的读数完全相同。数字与逐条读数在
    `scripts/measure_external_relevance.py`（`--rerank` / `--sweep`）。
    """
    phrases = _phrase_terms(query)
    words = _word_terms(query)
    if not phrases and not words:
        return list(hits)
    # 跨语言放行的判据：query 与命中的**书写系统**有没有交集。中文 query 配一条全
    # 英文命中时，两边没有任何可比的字符——那时判据说不出话，让它表决等于用"拼写
    # 不同"判"内容无关"。实测放行这一支救回了 `consensus algorithm` 那条中文正文的
    # `Raft`（不放行时它被误杀，名额转手给一条 RANSAC 噪声）。
    #
    # 代价要说清：全英文命中在中文 query 下一律放行，`Rust 所有权` 若拿到一篇纯英文
    # 的小麦锈病论文也会进来。实测这批 OpenAlex 命中都带中文摘要（不走这一支），
    # 所以没有触发；arXiv 是全英文语料，会走这一支。这个洞是"词面判据跨语言无定义"
    # 的直接后果，不是实现取舍。
    query_writes_cjk = bool(_CJK_RUN_RE.search(query))
    kept: list[ExternalHit] = []
    dropped: list[str] = []
    for hit in hits:
        blob = _hit_blob(hit)
        if any(phrase in blob for phrase in phrases) or words & _word_terms(blob):
            kept.append(hit)
            continue
        if bool(_CJK_RUN_RE.search(blob)) != query_writes_cjk:
            kept.append(hit)
            continue
        dropped.append(f"{hit.source_name}/{hit.title[:60]}")
    if dropped:
        # info 而不是 debug：这道门**扔掉的是要变成引用的东西**，扔了哪几条必须
        # 在日志里查得到，否则"这次怎么一条依据都没有"永远无从复盘。
        logger.info("外部检索：%r 过滤掉 %d 条词面无关命中（%s）",
                    (query or "")[:50], len(dropped), "；".join(dropped[:5]))
    return kept


def search_external(
    query: str, *, limit: int = DEFAULT_LIMIT, directions: Sequence[str] | None = None,
) -> list[ExternalHit]:
    """去外部源搜一次，返回最多 `limit` 条命中。**失败返回空列表，永不抛。**

    这是整条外部检索的入口，两条路：

      - **单方向**（默认；`directions` 只有一条，或开关关着，或拆方向失败）：五个源
        并行打同一个 query，9 次请求 / 20 秒，出口一道按 query 的词面门 —— 与
        `3c40ba6` 逐字节相同。
      - **多方向**（`MINDMAP_EXTERNAL_DIRECTIONS=1` 且拆出 2-4 个方向）：每个方向各打
        一轮 `DIRECTION_SOURCES`，15 次请求 / 20 秒、每方向 5 次，门按**各自的方向**施加，
        合并去重后跨方向轮转。**默认关**，理由与实测见 `DIRECTIONS_ENV`。

    五个源并行跑：串行时最坏耗时是五个 10 秒超时相加，而它们都只是"多等一会儿"，
    没有任何顺序依赖。并行之后墙钟上界由 `DEADLINE_SECONDS` 那一份预算兜住。

    源的分工（都是实测通过的，可抓性报告见模块 docstring 的"外部源清单"）：

      - `wikipedia` / `baike`：中文百科，给"这个词条是什么"；
      - `arxiv` / `openalex` / `crossref`：论文，给"这个方向研究到哪了"。
        其中 arXiv 对中文 query 基本 0 条（语料是英文），OpenAlex 与 Crossref
        能吃中文 query 并返回中文论文——所以中文检索不靠翻译。

    用时与条数各记一行 info：这是用户点"生成"之后**多出来的一次外网等待**，
    混在总耗时里就永远看不出"慢在哪"（与 `_retrieve_topic` 的同一条规矩）。

    `directions` 是给调用方（测量脚本、用例）**注入**方向的出口：不传就自己拆
    （`plan_directions`），传了就不调模型——拆方向的效果可以脱离模型单独量。
    """
    text = (query or "").strip()[:QUERY_MAX_CHARS]
    if not text or limit <= 0:
        return []

    started = time.perf_counter()
    if directions is not None:
        planned = list(directions)
    elif _directions_enabled():
        planned = _plan_directions_sync(text)
    else:
        # 开关关着：零 LLM 调用、零额外分支，与 `3c40ba6` 完全一致。
        planned = [text]
    usable: list[str] = []
    for item in planned:
        direction = (item or "").strip()
        if direction and direction not in usable:
            usable.append(direction)
    usable = usable[:MAX_DIRECTIONS]

    if len(usable) <= 1:
        # 降级路径：**一个方向**。没有拆方向、拆出来只有一条、或拆失败——三者都走
        # 这里，且与 `3c40ba6` 的单方向实现逐字节相同（同一份预算、同一批源、同一
        # 道门、同一行日志）。"拆方向失败"不能顺带改变别的东西。
        return _search_single(usable[0] if usable else text, limit, started)
    return _search_many(text, usable, limit, started)


def _plan_directions_sync(topic: str) -> list[str]:
    """同步地拆方向。失败/超时/没配置时 `plan_directions` 自己返回 `[topic]`。

    `search_external` 是同步函数（调用方 `agent.tools._external_sources` 用
    `asyncio.to_thread` 把它丢进线程），所以这里要自己造事件循环。

    已经在事件循环里的情况（有人直接在协程里调 `search_external`）**另起一个线程**：
    `asyncio.run` 在运行中的 loop 里会抛 `RuntimeError`，而把异常当成"拆方向失败"
    吞掉是错的——那会让一个调用方式的错误静默退化成"永远只有一个方向"。
    """
    coro = plan_directions(topic)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return list(asyncio.run(coro))
    with ThreadPoolExecutor(max_workers=1) as pool:
        return list(pool.submit(asyncio.run, coro).result())


def _search_single(query: str, limit: int, started: float) -> list[ExternalHit]:
    """单方向检索：**这就是 `3c40ba6` 的 `search_external` 主体**，一行都没改。

    单独成函数是为了让"降级"有唯一的落点：`search_external` 里任何一条拆方向失败
    的支路都汇到这里，而不是各自复制一份预算与门。
    """
    budget = _Budget(REQUEST_LIMIT, DEADLINE_SECONDS)
    collected = _search_one(query, limit, budget=budget)

    # 门在**聚合之后、截断之前**：先从五个源拿全部候选，再按词面相关性筛，最后才
    # 交替取前 `limit` 条。顺序不能换——先截断再过滤时，一个被滤掉的噪声槽位是
    # 空的（3 条变 2 条），而候选池里本来还有真相关的条目可以补位。
    relevant = _relevant_hits(query, collected)
    hits = _interleave(relevant, limit, prefer_language=_query_language(query))
    logger.info(
        "外部检索：%r 命中 %d 条（%s），候选 %d 条过门 %d 条，请求 %d 次，耗时 %.2fs",
        query, len(hits), ", ".join(f"{hit.source_name}/{hit.title}" for hit in hits[:3]),
        len(collected), len(relevant), budget.used, time.perf_counter() - started,
    )
    return hits


def _search_many(
    topic: str, directions: Sequence[str], limit: int, started: float,
) -> list[ExternalHit]:
    """多方向检索：方向之间并行，每个方向各自过门，合并去重后跨方向轮转。

    五件事的先后顺序都不能换，理由见模块 docstring 的"多方向检索"一节：

      1. 每方向一轮，共享 `MULTI_REQUEST_LIMIT` 全局预算 + `DIRECTION_REQUEST_LIMIT`
         单方向预算；
      2. **按方向**过词面门（`_direction_gate`：主题锚 + 该方向的限定语，不是按主题）；
      3. 合并后按 URL 去重（不同方向命中同一条维基/论文是常态）；
      4. 方向内按源交替（`_interleave`），每方向留 `DIRECTION_HIT_LIMIT` 条；
      5. 跨方向轮转（`_interleave_directions`）取最终 `limit` 条。

    门放在"合并去重"之前还是之后？这里放在**之前**：一条命中属于哪个方向，只有
    那个方向的候选池知道；合并之后它已经变成"某条来自方向 2 的命中"，再用方向 2
    去判它就得把方向标签跟着候选走一遍——那是同一个判断的第二种写法。
    """
    budget = _Budget(MULTI_REQUEST_LIMIT, DEADLINE_SECONDS)
    slots = [_Slot(str(index), DIRECTION_REQUEST_LIMIT) for index in range(len(directions))]

    def run(index: int) -> list[ExternalHit]:
        direction = directions[index]
        candidates = _search_one(
            direction, DIRECTION_HIT_LIMIT, budget=budget, slot=slots[index],
            sources=DIRECTION_SOURCES, exact_first=False,
            fetch_limit=DIRECTION_WIKI_FETCH,
        )
        kept = _direction_gate(topic, direction, candidates)
        logger.debug("外部检索：方向 #%d 候选 %d 条过门 %d 条（该方向已用 %d 次请求）",
                     index, len(candidates), len(kept), budget.slot_used(slots[index].key))
        return [replace(hit, direction=direction) for hit in kept]

    with ThreadPoolExecutor(max_workers=max(len(directions), 1)) as pool:
        per_direction = list(pool.map(run, range(len(directions))))

    merged, duplicated = _dedupe_by_url(per_direction)
    groups: list[list[ExternalHit]] = []
    for group in merged:
        groups.append(_interleave(group, DIRECTION_HIT_LIMIT,
                                  prefer_language=_query_language(topic)))
    hits = _interleave_directions(groups, limit)
    logger.info(
        "外部检索：方向 %d 个（%s）命中 %d 条（%s），候选 %d 条去重掉 %d 条，请求 %d 次，耗时 %.2fs",
        len(directions), "、".join(directions), len(hits),
        ", ".join(f"{hit.direction}/{hit.source_name}/{hit.title[:24]}" for hit in hits[:3]),
        sum(len(group) for group in per_direction), duplicated, budget.used,
        time.perf_counter() - started,
    )
    return hits


def _hit_key(hit: ExternalHit) -> str:
    """去重用的身份：**URL 优先**，没有 URL 时退回 `(source_name, title)`。

    URL 要先去 fragment、去尾部斜杠：同一篇论文在两个方向上可能一个带 `#abstract`、
    一个不带；那是同一个落地页。没有 URL 的命中（快懂百科在拿不到落地地址时、以及
    各种样本）用源名 + 标题兜底——标题在两个方向上都一样时，那本来就是同一条。
    """
    url = (hit.url or "").strip().split("#", 1)[0].rstrip("/")
    if url:
        return url
    return f"{hit.source_name}\x00{hit.title}"


def _dedupe_by_url(
    groups: Sequence[Sequence[ExternalHit]],
) -> tuple[list[list[ExternalHit]], int]:
    """跨方向按 URL 去重：**保留首次出现的方向**，返回 `(各组, 去掉几条)`。

    保留首见而不是"合并方向标签"：一条命中只该有一个方向，否则报告里"政策那个方向
    拿回了什么"就说不清（一条命中同时挂在两个方向下，两条线都算它的功劳）。代价是
    "同一篇论文同时讲政策与技术"时第二个方向少一条——可接受，因为它本来就只该
    贡献一次。
    """
    seen: set[str] = set()
    kept: list[list[ExternalHit]] = []
    dropped = 0
    for group in groups:
        survivors: list[ExternalHit] = []
        for hit in group:
            key = _hit_key(hit)
            if key in seen:
                dropped += 1
                continue
            seen.add(key)
            survivors.append(hit)
        kept.append(survivors)
    return kept, dropped


def _interleave_directions(
    groups: Sequence[Sequence[ExternalHit]], limit: int,
) -> list[ExternalHit]:
    """跨方向轮流取：第 1 轮每个方向各取一条，第 2 轮再各取一条，直到 `limit`。

    为什么不是直接用 `_interleave`（按源分桶）：三个方向都命中维基时，维基桶里的
    前 3 条会占满名额，而它们很可能**全来自同一个方向**——拆了三个方向，返回的还是
    同一个方面。方向内那层 `_interleave` 保的是源多样，这一层保的是方面覆盖，两层
    各管一件事，缺一层都会退化成"某一维占满名额"。

    `groups` 里的一组的顺序已经由 `_interleave` 排好（源交替），所以这里只负责
    "先给谁名额"。空组自然跳过；全部取尽时提前结束。
    """
    merged: list[ExternalHit] = []
    depth = 0
    while len(merged) < limit:
        progressed = False
        for group in groups:
            if len(merged) >= limit:
                break
            if depth < len(group):
                merged.append(group[depth])
                progressed = True
        if not progressed:
            break
        depth += 1
    return merged


# ---------------------------------------------------------------- 拆方向


# 提示词。写法借 `query_plan.BRANCH_SYSTEM_PROMPT`：三条硬约束都用**正反例**
# 写成"可对照的形状"，不用形容词；例子只用「碳中和」这类通用词并明确说"不要抄
# 例子"，因为 2.3 那次编出的超导/离子阱正是"照着别处的词往下写"的产物。
#
# 与那个模块的差别是本质的：那边服务"在材料里找片段"（corrected/intents/hyde/
# subqueries + 四道防发散闸），这边服务"一个主题从几个方面查"。语义不同，所以
# 不复用 `plan_query`——复用会让两边互相绑死在对方的失败模式上。
DIRECTION_SYSTEM_PROMPT = (
    "你在为一次外部资料检索做规划：把一个主题拆成 2-4 个**检索方向**，每个方向各搜一轮。"
    "你只输出一个 JSON 对象，不要写解释、不要加代码围栏、不要在 JSON 前后写字。\n"
    "\n"
    "形状：{\"directions\": [\"方向1\", \"方向2\", \"方向3\"]}\n"
    "\n"
    "每个方向的写法要求：\n"
    "  - 6-20 个字，是可检索的短语，不是问句、不是整段话；\n"
    "  - 必须**原样包含主题的写法**（主题是「碳中和」，方向就写成「碳中和政策」）；\n"
    "  - 方向之间是主题的不同方面（政策 / 技术路径 / 企业案例 / 争议 / 产业），互不重复。\n"
    "\n"
    "三条硬规则，逐条守：\n"
    "\n"
    "1. 方向必须是主题的子方面，不能换领域。\n"
    "   ❌ 主题是「碳中和」，方向写成「超导量子比特」「离子阱」——那是别的领域。\n"
    "   ✅ 主题是「碳中和」，方向写成「碳中和政策」「碳中和技术路径」「碳中和企业案例」。\n"
    "\n"
    "2. 主题的写法逐字照抄，不要换同义词、不要翻译、不要改写。\n"
    "   ❌ 主题「碳中和」→ 方向「碳抵消」「温室气体净零排放」（换了说法，检索会换一批源）。\n"
    "   ✅ 主题「碳中和」→ 方向「碳中和政策」（原词在前，只追加限定语）。\n"
    "\n"
    "3. 不要编造主题里没有的机构、标准、文号、篇名、年份。\n"
    "   ❌ 「碳中和 GB/T 36132-2018 合规要求」——那个文号是你编的。\n"
    "   ✅ 「碳中和 标准与核算方法」。\n"
    "\n"
    "上面的例子只用来说明形状，不要把例子里的词写进你的结果。"
)


def _directions_message(topic: str) -> str:
    """这一次的用户消息：只有主题。**不塞材料**——这条路上没有材料可塞。

    没有材料时"锚定"全靠主题本身，所以提示词把"原样包含主题写法"写成硬规则，代码
    侧再用 `_direction_anchored` 逐条核对（`query_plan` 那条"没有材料就返回空"的
    代码闸在这里的等价物）。
    """
    return f"主题：{topic}\n\n只返回那个 JSON 对象。\n"


def _directions_payload(raw: Any) -> dict[str, Any] | None:
    """模型出口 → dict。`chat_async` 正常返回 dict；字符串形态再解析一次。

    与 `query_plan._payload` 同一条规矩（用同一个解析器收尾），但**不复用它的函数**：
    那是 query_plan 的私有实现，跨模块直接调私有名会让两个模块的改动互相牵制。
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = parse_model_json(raw)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _raw_directions(payload: dict[str, Any]) -> list[str]:
    """取出候选方向：认 `directions` / `queries` 两种键，外加一个裸数组形状。

    收多种键不是宽容，是"模型把数组换个名字"这件事的代价太低：为它丢掉一整次调用
    的产出，等于让整条路径的成功率下降一截。
    """
    for key in ("directions", "queries", "aspects"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, str)]
    return []


def _direction_length_ok(text: str) -> bool:
    """方向的长度闸：中英混排按"折算字数"算，纯英文按词数算。

    折算规则与理由见 `DIRECTION_MAX_CHARS` 上面那段：一个英文词算两个字。纯英文
    方向再另设一道"2-8 个词、≤60 字符"的闸——`Byzantine fault tolerance threat
    models` 五个词是合法的，而一段整句会在词数上先暴露。
    """
    cjk = sum(len(run) for run in _CJK_RUN_RE.findall(text))
    words = _DIRECTION_WORD_RE.findall(text)
    if cjk and words:
        size = cjk + 2 * len(words)
        return DIRECTION_MIN_CHARS <= size <= DIRECTION_MAX_CHARS
    if cjk:
        return DIRECTION_MIN_CHARS <= cjk <= DIRECTION_MAX_CHARS
    if not words:
        return False
    return (DIRECTION_MIN_WORDS <= len(words) <= DIRECTION_MAX_WORDS
            and len(text) <= DIRECTION_MAX_ASCII_CHARS)


def _direction_anchored(topic: str, direction: str) -> str | None:
    """方向能不能被主题**词面锚住**；能就返回清洗后的方向，不能返回 None。

    这是"不许编造领域"的代码执行处。四条判据，任一成立即通过：

      1. 方向包含主题的判定词块（`_phrase_terms(topic)` 的头 3 字）——「碳中和政策」
         含「碳中和」；
      2. 方向包含主题里的英文整词（≥3 字符）——「Rust 内存模型」含 `rust`；
      3. 反向包含：主题里含**方向**的判定词块——主题「为什么中文大模型在长文本任务
         上表现不稳定」，方向「中文大模型 长文本」的方向词块「中文大」出现在主题里。

    为什么用"原词出现在方向里"而不是"与主题共享 bigram"：bigram 判据在这件事上
    两头不讨好。它放不过「碳交易」这种**不含**主题原词但确实相关的子方面（`碳中`/
    `中和` 与 `碳交`/`交易` 没有交集），也挡不住「中超联赛」这类只是撞了一个高频
    汉字的编造（共享「中」）。既然两种口径在"合法的窄化方向"上都失手，就选那条
    **可执行、可解释、且与提示词一致**的：提示词要求方向原样包含主题写法，代码按
    同一句话核对。被拒的方向丢掉，不拼回去——拼成「碳中和 超导量子比特」比丢掉更糟。

    长度闸（`DIRECTION_MIN_CHARS`-`DIRECTION_MAX_CHARS`）与"等于主题"的闸也在这里：
    方向的字数区间是任务给的 6-20 字，而等于主题的那条是**白跑一路**（同一个 query
    打两遍），它会让"3 个方向"实际只有 2 个方面。
    """
    text = " ".join((direction or "").split())
    if not _direction_length_ok(text):
        return None
    if text == topic:
        return None
    if any(phrase in text for phrase in _phrase_terms(topic)):
        return text
    if _word_terms(topic) & _word_terms(text):
        return text
    if any(phrase in topic for phrase in _phrase_terms(text)):
        return text
    return None


def _build_directions(payload: dict[str, Any], topic: str, max_directions: int) -> list[str]:
    """从 payload 重建方向清单：逐条过锚定闸、去重保序、截到 `max_directions`。

    全部被拒时返回**空列表**（不是 `[topic]`）——那是调用方的事：`plan_directions`
    的契约是"拆不出来就 `[topic]`"，而空列表在这个函数里只表示"模型给的东西一条
    都不能用"。两者分开，才能让日志区分"模型没答"与"模型答的东西全被拒"。
    """
    kept: list[str] = []
    for item in _raw_directions(payload)[:DIRECTION_MAX_ITEMS]:
        direction = _direction_anchored(topic, item)
        if direction is not None and direction not in kept:
            kept.append(direction)
    return kept[:max(max_directions, 1)]


def _log_directions(outcome: str, started: float, *, topics: int,
                    directions: Sequence[str]) -> None:
    """一行日志：**只有形状，没有内容**。

    主题本身不记：它是用户的输入，可能是"我的合同违约金怎么算"这种不该出现在日志里
    的东西；方向数、可用方向数、耗时足够回答"拆没拆、拆出几条、降没降级"。方向文本
    也不记——它是主题的改写，泄漏程度与主题同级。
    """
    logger.info(
        "外部检索拆方向 outcome=%s 耗时=%.0fms 条数=%d 可用=%d 长度=%d",
        outcome, (time.perf_counter() - started) * 1000, topics, len(directions),
        sum(len(item) for item in directions),
    )


async def plan_directions(topic: str, *, max_directions: int = MAX_DIRECTIONS) -> list[str]:
    """把主题拆成 2-4 个检索方向；拆不出来返回 `[topic]`。**一次调用、8 秒、不重试。**

    返回值的三种形态，调用方只需要认第一种与最后一种：

      - `[topic]`：没配置模型、调用失败、超时、JSON 解不出、方向全被拒。**与"没有
        这个功能"逐字节一致**——调用方（`search_external`）据此走单方向路径。
      - 2-4 条方向：每条都是主题的子方面，且词面被主题锚住（`_direction_anchored`）。
      - `[]`：主题为空。**没有方向可拆**，调用方拿到空列表时按"没搜到"处理。

    为什么一次调用产出全部方向：3 个方向各调一次 = 3 次往返，而每次 8 秒预算，
    2C2G 上不可能；一次调用产出 2-4 条的成本与产出 1 条几乎相同。

    为什么不复用 `query_plan.plan_query`：那个函数服务"在材料里找片段"，产出是
    corrected/intents/hyde/subqueries，还带四道防发散闸与"没有材料就不许扩"的代码闸。
    这里没有材料、也不需要改写用户的问题——只需要"这个主题有哪几个方面"。语义不同，
    复用会让两边绑死在对方的失败模式上（`query_plan` 的教训正是"不看材料就编领域"，
    而这里**必然**没有材料，那条闸一开就是永久关闭）。
    """
    text = (topic or "").strip()[:QUERY_MAX_CHARS]
    if not text:
        return []
    started = time.perf_counter()
    outcome = "ok"
    raw: Any = None
    try:
        # 只发一次，绝不重试：失败就是这次不拆方向，重试只会把 8 秒预算打穿。
        raw = await asyncio.wait_for(
            chat_async(DIRECTION_SYSTEM_PROMPT, _directions_message(text),
                       max_tokens=DIRECTION_MAX_OUTPUT_TOKENS),
            timeout=DIRECTION_TIMEOUT_SECONDS,
        )
    except (TimeoutError, asyncio.TimeoutError):
        outcome = "timeout"
    # 与 `plan_query` 同样不拦 `CancelledError`：那是我方被取消（客户端断开），
    # 不是模型坏了，吞掉它会让上层以为任务正常跑完了。
    except Exception:  # noqa: BLE001 — 没配置/网络/解析都可能，全部降级
        outcome = "error"
    if outcome != "ok":
        _log_directions(outcome, started, topics=1, directions=[text])
        return [text]

    payload = _directions_payload(raw)
    if payload is None:
        _log_directions("bad_json", started, topics=1, directions=[text])
        return [text]
    directions = _build_directions(payload, text, max_directions)
    if not directions:
        # 全被锚定闸拒掉：这次拆方向没有可用的东西，与"模型挂了"同一个结果。
        # 不能返回模型给的原话——那些正是判据说"不是这个主题"的东西。
        _log_directions("rejected", started, topics=1, directions=[text])
        return [text]
    _log_directions("ok", started, topics=1, directions=directions)
    return directions
