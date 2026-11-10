# API 与数据契约

前后端以 `/api` 为前缀。FastAPI 启动后的 `/docs` 展示当前路由；本页说明每条路由是否真正实现。当前无身份认证，仅限本机开发。

**错误体已统一**：所有错误响应都是 `{code, message, detail, request_id}`（实现见 `backend/app/core/errors.py`）。`message` 是中文可读文案，`detail` 保留原始细节（校验错误为数组），`request_id` 优先沿用请求头 `X-Request-ID`、没有则生成，并同时通过响应头回传。前端 `src/api/client.ts` 的 `ApiError` 读 `message`，缺失时回退到 `detail`。

**前端消费情况**（截至 2026-11-07）：`POST /api/generations`、`POST /api/generations/stream`、`POST /api/pipeline/generate`（`generateFromSources`）、`POST /api/ingestion/files`（`uploadDocument`）、`POST /api/agent/runs/stream`（`runAgentStream`）、`POST /api/export/{puml,markdown,opml}`、四个 `/api/mindmaps*` 端点。画布与编辑全部在 `src/canvas/`、`src/mindmap/{edits,ops}.ts` 里完成，只把整棵树发给后端。节点引用（`node.source_refs`）已能展示：角标显示条数，详情面板列来源与位置，抽屉里看原文并高亮命中句，实现见 `src/mindmap/refs.ts`。

**尚未封装**：`POST /api/ingestion/text`、`GET /api/ingestion/formats`、`POST /api/optimization/check`、`POST /api/optimization/suggest`、`POST /api/knowledge/search`、`POST /api/agent/tasks`。其中前两个是小事，中间两个是"结构诊断还没进界面"，后两个仍是桩。

## 导图格式 v2（当前；v1 请求体仍然接受）

```json
{
  "schema_version": 2,
  "title": "人工智能",
  "layout": "tree",
  "theme": {"preset": "default", "font": "system", "direction": "right"},
  "root": {
    "id": "root",
    "label": "人工智能",
    "note": "可选的正文，画布上只显示短标题",
    "children": [
      {"id": "node-1", "label": "机器学习", "children": []}
    ]
  },
  "source_usage": [
    {"source_id": "source-1", "kind": "text", "filename": null, "char_count": 42}
  ]
}
```

每个节点需要非空 `id`、非空 `label`；省略 `children` 时默认为空。节点还可以带可选的 `source_refs`（**属于 v1，不是 v2 预告**）：

```json
"source_refs": [{"source_id": "source-1", "quote": "原文里的逐字片段", "start": 12, "end": 25, "page": 3}]
```

`quote` 必须能在本次请求提供的原文里逐字匹配，`source_id` 必须出现在 `source_usage` 里；不合规的引用在生成/保存时被剔除，原因通过 `diagnostics`（带 `node_id`）返回，节点本身保留。规则见 [节点来源引用](SOURCE-REFERENCES.md)。

整棵树 ID 唯一，最多 1000 节点、8 层、每个节点最多 50 个子节点，每节点最多 20 条引用。标题和节点文字长度不超过 200。

**v2 已落地**（2026-11-10，`backend/app/contracts/mindmap.py`）：在 v1 之上新增 `schema_version`、`layout`（`tree` / `vertical` / `logic` / `timeline` / `fishbone` / `manual`）、`theme`（预设、强调色、字体、`direction`）、`relations[]`，以及节点的 `note` / `collapsed` / `style` / `pos`。字段定义与校验规则见 [CONTRACT_V2](CONTRACT_V2.md)。

兼容策略是"**读时补默认值**"：没有 `schema_version` 的 v1 请求体与库里的 v1 记录都直接可用（缺的字段填默认值），保存后写成 v2；不需要跑迁移脚本，也不需要停机。反过来不成立——把 v2 字段发给旧版后端仍会 422（`extra="forbid"`），所以升级顺序必须是后端先上、前端后上。

`source_usage` 是可选的**整张导图来源摘要**：多来源流水线根据实际输入填写，保存导图时一并保存；旧数据省略时默认为空列表。它只记录来源 ID、类型、文件名和使用的字符数，不保存原文，也没有节点到来源片段的对应关系，不能作为节点级引用或事实验证。直接文本生成时该列表为空；后续完整追溯还需要来源存储、分段定位与节点关联。

## 已实现

| 方法和路径 | 请求 | 成功响应 | 失败情况 |
|---|---|---|---|
| `GET /api/health` | 无 | `{"status":"ok","version":"..."}` | — |
| `POST /api/generations` | `{"text":"输入内容"}` | `{"mode":"demo 或 model","content":导图}` | 输入无效 422；模型配置或生成失败 502。 |
| `POST /api/generations/stream` | `{"text":"输入内容"}` | SSE：`stage` / `node` / `done` / `error`，末尾 `data: [DONE]` | 流开始前校验失败走普通 JSON 422；开始后错误以 `{"type":"error"}` 事件下发 |
| `POST /api/ingestion/text` | `{"text":"内容","kind":"text 或 markdown"}` | `{"source": 来源}` | 空文本或其他来源类型 422 |
| `POST /api/pipeline/generate` | `{"sources":[来源]}` | `mode`、`content`、`diagnostics`、`stages` | 来源无效或重复 422；材料估算后超出模型上下文上限也 422（不再按固定字数卡）；模型失败 502 |
| `POST /api/optimization/check` | `{"content":导图}` | 结构诊断列表 | 结构无效 422 |
| `POST /api/optimization/suggest` | `{"content":导图}` | 诊断与候选建议 | 结构无效 422；建议需用户判断 |
| `POST /api/export/puml` | `{"content":导图}` | PlantUML 文本；根节点来自 `root.label` | 结构无效 422 |
| `POST /api/export/markdown` | `{"content":导图}` | Markdown 大纲：标题 + 缩进列表 | 结构无效 422 |
| `POST /api/export/opml` | `{"content":导图}` | OPML 2.0，可导入 XMind | 结构无效 422 |
| `POST /api/ingestion/files` | multipart，字段名 `file` | `{"source": 来源}`，`source.text` 是解析出的正文 | 扩展名不在白名单 422；文档/图片超 10MB 或音频超 20MB 413；扫描件无文本层、音频无人声、图片视觉不可用、缺语音凭证 422 |
| `GET /api/ingestion/formats` | 无 | `{"document":[…],"audio":[".wav"],"image":[".png",…],"maxUploadBytes":…,"maxAudioBytes":…,"maxImageBytes":…}` | — |
| `GET /api/mindmaps` | 无 | 导图记录数组 | — |
| `POST /api/mindmaps` | `{"content":导图}` | 201，导图记录 | 结构无效 422 |
| `GET /api/mindmaps/{id}` | 无 | 导图记录 | 不存在 404 |
| `PUT /api/mindmaps/{id}` | `{"version":1,"content":导图}` | 更新后的记录，版本加 1 | 不存在 404；版本过期 409；结构无效 422 |
| `POST /api/agent/runs/stream` | `{"goal":"…","intent":"create / expand / edit","snapshot":导图或省略,"selected_ids":[…],"sources":[…],"budget":{…}}` | SSE v2：`{v:2,runId,seq,type}`，`type` ∈ `stage` / `heartbeat` / `tool_start` / `ops` / `tool_end` / `snapshot` / `done` / `error`，产物是**候选** | 目标为空 422；预算用尽或模型失败以 `error` 事件下发 |

**流式接口有两套帧格式。** 生成链路（`/api/generations/stream`）每帧 `data: <json>`，`type` 区分 `stage` / `node` / `heartbeat` / `done` / `error` / `skeleton`；`node` 事件的节点只有 `id`/`label`（不含 children），前端按 id 插入或覆盖；收到 `done` 后前端应用完整 `content` 对齐；`data: [DONE]` 表示流正常结束，不是 JSON。事件定义与生成逻辑见 `backend/app/generation/stream_partial.py`。
Agent 链路（`/api/agent/runs/stream`）用 v2 信封 `{v:2,runId,seq,baseClientRevision,candidateRevision,type}`，产物是 `ops` 操作批次而不是增量节点；前端把它挂在候选副本上，用户点接受才落到画布。协议见 `docs/AGENT_PLAN.md`。

记录形如 `{"id":"...","version":1,"content":{...},"updated_at":"...Z"}`。生成接口不会自动保存。`demo` 为演示模式，不是 AI；`model` 表示请求已成功经模型服务处理，不代表内容必然正确。流水线目前同步返回，`stages` 是完成后的阶段记录，不是实时进度。

**文档解析按格式走不同链路**（详见 [部署文档](DEPLOY.md)）。`.pdf` 逐页渲染后交模型视觉识别（demo 模式回落文本层，扫描件则 422），处理到第几页由上下文预算算出（`plan_page_count`，扫描页按 379 字/页折算），另有 300 页的转换护栏；`.docx`/`.pptx`/`.xlsx`/`.html`/`.md` 走 Docling 读结构化格式，不加载版面模型、离线可用；`.doc`/`.ppt` 要先经 LibreOffice 转 PDF 再视觉识别，没装 LibreOffice 时 422 并附安装命令；`.xls` 用 `xlrd` 直读单元格；`.png`/`.jpg`/`.webp`/`.gif`/`.bmp` 复用多模态调用；`.wav` 走百炼 Qwen-Audio-ASR（≤300 秒，更长自动切片）。解析失败或拿不到正文时返回 422，不返回空内容。

`source` 是 `/api/ingestion/text` 返回的对象，也可按 `/docs` 中的 Source 结构提交；每次流水线最多 50 个来源，必须均为非空且已解析，ID 不可重复。当前超长会报错，不会静默截断或丢弃来源。

## 已注册但尚未实现

| 路由 | 当前行为 |
|---|---|
| `POST /api/knowledge/search` | 返回空列表与 `implemented: false`，没有接入检索。 |
| `POST /api/agent/tasks` | 返回固定阶段与 `implemented: false`，不会生成导图；真正能跑的是 `/api/agent/runs/stream`。 |

## 功能接口规划（均未实现）

| 模块 | 候选接口/契约 | 目的 |
|---|---|---|
| 多模态文件 | 已落地 `POST /api/ingestion/files`（文档 + 图片 + WAV）；来源查询接口待确定 | 上传或解析 PDF、Word、PPT、图片、WAV 音频已可用；音频已有逐句时间戳，但节点引用还不能指回"第几秒"，页码级定位也缺。 |
| 生成任务 | `POST /api/generation-tasks`、`GET /api/generation-tasks/{id}`、`GET .../{id}/events` | 多来源输入、持久任务状态与进度；输出仍转换为统一导图结构。当前是同步请求 + SSE 秒级流，没有持久任务。 |
| 优化 | 已有 `/api/optimization/suggest`，持久化和应用接口待确定 | 未来补齐矛盾、缺失、重组、跨节点关系候选及依据，不直接覆盖。 |
| 知识库 | `POST /api/knowledge-bases`、`POST .../{id}/sources`、`POST .../{id}/search` | 导入来源、切分、向量化、检索与引用。 |
| AI 代理 | `POST /api/agent/runs/stream` 已实现；`POST /api/agent/tasks` 仍是桩 | 工具循环与候选确认已可用；任务化长跑、过程记录落库与多角色仍待设计。 |
| 协作 | 房间授权接口 + Yjs WebSocket | 房间鉴权、实时同步、在线状态、恢复；最终协议由协作负责人完成设计。 |
| 更多导出 | `/api/export/puml`、`/markdown`、`/opml` 均已实现；图像导出待开发 | PNG/SVG 渲染要额外依赖，优先用浏览器端截图或交给 PlantUML 服务。 |

未来接口的请求、响应、权限、错误和任务生命周期需在实现前由 RPeGio 与陈嘉涛共同评审，不能将候选路径视为已实现承诺。
