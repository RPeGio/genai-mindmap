# DeepSeek 工具 API 五件套：是什么，以及我们哪些已经有了

> 2026-11-13 DeepSeek 发布「全家桶应用 API」：把 App 里的联网搜索、文件解析、语音等能力
> 开放成接口。本文按官方文档逐项核对，并对照本仓库现有实现，指出哪些是替换、哪些是补齐、
> 哪些不该接。核实日期 2026-11-18，以官方文档为准。

## 一张表看完

| 能力 | 接口 | 计费 | 限速 | 我们有没有 |
|---|---|---|---|---|
| **Search** | `POST /v1/tools/search` | 5 元 / 1000 次成功请求 | 100 QPS | 有（五源关键词检索），但语义不如它 |
| **Fetch** | `POST /v1/tools/fetch` | 5 元 / 1000 次成功请求 | 25 QPS | 有（自己抓，`external.py`） |
| **OCR** | `POST /v1/tools/ocr` | 1.5 元 / 1000 页 | 并发 15 | 有，但依赖重（Docling + pymupdf + LibreOffice） |
| **ASR** | `POST /v1/tools/asr` | 0.78 元/小时（现 0.39） | 并发 50 | 有（百炼 `qwen-audio-3.1-asr-flash`） |
| **TTS** | `POST /v1/tools/tts` + WebSocket | 3 元 / 万字符（现 1.5） | 并发 50 | **没有** |

全部走 `https://api.deepseek.com`，Bearer 鉴权，与模型 API 同一把 key。

---

## 一、Search —— 网页搜索

```http
POST /v1/tools/search

{ "query": "DeepSeek V4 模型 发布", "count": 5, "need_content": true }
```

| 字段 | 说明 |
|---|---|
| `query` | 1–128 字符，支持 `site:` / `-site:` / `-词` / `"精确短语"` / `-"...":` / `lang:zh,en` / `after:2025` / `before:2026-06` |
| `count` | 1–50，默认 10 |
| `ori_question` | 触发搜索的原始问题，帮助理解意图 |
| `localization` | 优先返回本地结果的 IP |
| `need_content` | **为 true 时 `content` 返回网页正文** |

**响应**（每条结果）：

```
rerank_score  0–3，≥1.5 为优质
title / url
snippet       100–200 字符，给界面看
summary       600–800 字符，给模型读
content       网页正文，need_content=false 时恒为空
site_name / site_icon
date          UTC RFC 3339，未知为 null
authority     0–4，≥3 为极权威，-1 未知
```

**这一项推翻了一个误判**：早先看 Messages API 的内联工具时，搜索结果里有一个
`encrypted_content` 字段，据此判断"拿不到全文、只能当摘要源"。**那是两套东西**——
内联工具确实加密，但**独立 Search 接口的 `content` 是明文正文**（`need_content: true`）。
所以它可以当完整的检索源用，不是只能当摘要。

**不乱序、有 `authority` 与 `rerank_score`** —— 这两个分数是我们现在的五源检索给不出的。

---

## 二、Fetch —— 抓单个网页

```http
POST /v1/tools/fetch

{ "url": "https://example.com/article" }
```

响应只有两个字段：

```
title     页面标题，缺失时为空串
content   抓取后的 Markdown 正文
```

**它把 HTML 转成 Markdown**——这正是我们 `external.py` 里手写抓取逻辑在做的事。
差别是它处理了各种站点的边界（我们那套只对维基/OpenAlex/Crossref 这类结构化源稳）。

---

## 三、OCR —— 文档与图片识别

```http
POST /v1/tools/ocr        multipart/form-data
  file      必填，需带文件名
  purpose   text_recognition | rich_layout_recognition
```

**支持**：PDF、Word（DOC/DOCX）、PowerPoint（PPT/PPTX）、PNG/JPEG/WebP/GIF/BMP/TIFF。
格式按**文件实际内容**判断，不看后缀。

| `purpose` | 行为 |
|---|---|
| `text_recognition` | 不含图片的 Word/PPT 直接提取文字，其他逐页识别 |
| `rich_layout_recognition` | 所有文件都逐页识别（保留版面与坐标） |

**限制**：100 MiB、1000 页、上传 120 秒内完成、总时长 180 秒。

**响应**：

```
pages[].index         从 0 开始
pages[].text          该页模型原始输出，不做清理
pages[].finish_reason  stop | length（length 表示这页不完整）
```

逐页识别会保留 `<｜ref｜>标题<｜/ref｜><｜box｜>[[52, 40, 948, 96]]<｜/box｜>` 这类标记，
坐标归一化到 0–1000（原点在页面左上角）。

**这一项对我们最有价值**，理由在下面"对齐"一节。

---

## 四、ASR —— 语音转文字

```http
POST /v1/tools/asr        multipart/form-data
  file      16 位 PCM WAV 或 Ogg Opus
```

- **语种**：22 种，**没有语种参数**，按音频自动识别
- **声道**：单/双，**双声道只识别左声道**
- **限制**：8 MiB、最长 120 秒、上传 10 分钟内完成
- **不支持流式**
- **响应**：`{ request_id, text, duration_ms }`

**120 秒上限是个硬约束**——超过要自己切片（我们现有的百炼那条路是 300 秒，更宽）。

---

## 五、TTS —— 文字转语音

**两种方式**：

| 方式 | 端点 | 输入 | 输出 |
|---|---|---|---|
| 整段 | `POST /v1/tools/tts` | JSON 里的完整文本 | 响应体流式返回音频 |
| 流式 | `wss://api.deepseek.com/v1/tools/tts` | 分段文本 | 音频帧 |

- **音色**：`stella`（海星，女声，默认）、`tide`（暗潮，男声）——**只有两个**
- **语种**：10 种，自动识别
- **格式**：`wav`（默认）或 `pcm`；WebSocket 还能 `opus`（不带容器，不能直接存 Ogg）
- **限制**：单次最多 10,000 计费字符、30 分钟音频、请求体 64 KiB
- **不支持调节语速**，也不支持中断后续传
- **会做内容审核**，不通过返回 `400 Content Exists Risk`

**汉字算 2 个计费字符**，英文数字空格算 1 个。

---

## 对齐：我们哪些已经有了

| 能力 | 我们现在的实现 | 建议 |
|---|---|---|
| Search | `knowledge/external.py` 五源（维基 / OpenAlex / Crossref / arXiv / 快懂百科） | **当预检索接进来**，见下 |
| Fetch | 同上，自己抓后转纯文本 | 可替换，但收益小 |
| OCR | `ingestion/`：PDF 逐页视觉识别、Docling、LibreOffice、`xlrd` | **值得替换**，见下 |
| ASR | 百炼 `qwen-audio-3.1-asr-flash`，300 秒 | 不动（限长更宽，已跑通） |
| TTS | **无** | 想加朗读才需要 |

### 三条具体判断

**① Search 适合当"预检索"，不适合当"检索"**

我们现在的检索是**关键词匹配**，它有一个已知缺陷写在 `docs/EXTERNAL-RELEVANCE.md` 里：
同形异义会带来噪声（`Rust 所有权` 搜到小麦锈病论文）。

DeepSeek 的搜索引擎带语义理解。**把它的 `url` 列表拿来，再用我们的抓取取全文**，
就走通了"语义找站 + 全文切块"两件事：

```
deepseek Search（语义理解，给 url + summary）
  → 我们的 Fetch（拿全文）
  → 现有 RAG 链（切块 / 向量 / 逐字校验引用）
```

**收益**：同形异义噪声下降；**代价**：多一次外部调用与 5 元/千次。

**不建议**直接把它的 `content` 当 `Source` —— 那样每份材料的 chunk 只有 1–2 个，
现有"按分支检索"（覆盖率 30.5% vs 5.1%）的前提就没了。

**② OCR 对 2C2G 这台机器是净收益**

我们现在的文档解析链是**三套依赖叠在一起**：Docling（带 torch）、pymupdf、LibreOffice。
`docs/FEATURES_AND_OWNERS.md` 的 F22 记过：

> 选 systemd 而非容器化的理由：2C2G 上构建 docling+torch 镜像容易 OOM。

**一个 HTTP 调用换掉这三套**，对 2C2G 是实打实的减负。而且它给
`<｜box｜>` 归一化坐标——我们`span.page` 现在只记总页数，做不了页内定位（F05 的已知缺口）。

**代价**：1.5 元/1000 页，且文档内容要发给第三方。

**③ TTS 是唯一真缺的**

我们有 ASR（说 → 字）但没有 TTS（字 → 说）。要加"朗读导图"才需要它。
但**只有两个音色**，且不支持调语速——对"朗读"这个用途够，对"配音"不够。

---

## 一个实测：`deepseek-chat` 会被服务端换成 `deepseek-flash`

请求体明明写的是 `"model": "deepseek-chat"`，带上 `deepseek_web_search` 工具后，
响应里的 `model` 字段是 **`deepseek-flash`**。

```
请求  {"model":"deepseek-chat", "tools":[{"type":"deepseek_web_search",...}]}
响应  {"model":"deepseek-flash", "server_tool_calls":[{...}], "choices":[...]}
```

搜索结果里还有一条官方更新日志（2026-07-31）：

> The two legacy API model names, **deepseek-chat and deepseek-reasoner**,
> will be discontinued in three months (2026-07-24).

**那个日期已经过了。** 所以很可能 `deepseek-chat` 现在只是 `deepseek-flash` 的别名。

**这一条尚未核实**：不带工具、纯文本请求时返回的 `model` 是什么，还没测。
如果也是 `deepseek-flash`，那本仓库这一周所有实测数字
（detail 142 字、跨支重复 0、33–39 秒）标的模型名都该改。

---

## 参考

- [全家桶应用 API 公告](https://api-docs.deepseek.com/zh-cn/news/news261113/)
- [网页搜索与抓取](https://api-docs.deepseek.com/zh-cn/guides/search)
- [OCR](https://api-docs.deepseek.com/zh-cn/guides/ocr)
- [语音识别](https://api-docs.deepseek.com/zh-cn/guides/asr)
- [语音合成](https://api-docs.deepseek.com/zh-cn/guides/tts)
- [构建 Chatbot（总览）](https://api-docs.deepseek.com/zh-cn/guides/build_a_chatbot)
