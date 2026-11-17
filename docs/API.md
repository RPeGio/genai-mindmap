# API 与数据契约

前后端以 `/api` 为前缀。FastAPI 启动后的 `/docs` 展示当前路由；本页说明每条路由是否真正实现。当前无身份认证，仅限本机开发。

**错误体已统一**：所有错误响应都是 `{code, message, detail, request_id}`（实现见 `backend/app/core/errors.py`）。`message` 是中文可读文案，`detail` 保留原始细节（校验错误为数组），`request_id` 优先沿用请求头 `X-Request-ID`、没有则生成，并同时通过响应头回传。前端 `src/api/client.ts` 的 `ApiError` 读 `message`，缺失时回退到 `detail`。

**前端消费情况**（截至 2026-11-16）：`POST /api/agent/runs/stream`（`runAgentStream`，生成与编辑的**唯一**入口）、`POST /api/ingestion/files`（`uploadDocument`）、`POST /api/export/{puml,markdown,opml}`、四个 `/api/mindmaps*` 端点、`GET /api/knowledge/library` 与 `GET /api/knowledge/library/{source_id}`（`listLibrary` / `readLibrarySource`，顶栏「知识库」抽屉）。画布与编辑全部在 `src/canvas/`、`src/mindmap/{edits,ops}.ts` 里完成，只把整棵树发给后端。节点引用（`node.source_refs`）已能展示：角标显示条数，详情面板列来源与位置，抽屉里看原文并高亮命中句，实现见 `src/mindmap/refs.ts`。

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

前端从 2026-11-10 起按 v2 发送（`frontend/src/api/types.ts` 已补字段），保存 / 打开 / 导出都带 `note` 与 `layout` / `theme`；`frontend/src/api/client.ts` 里原有的两个裁剪函数 `toContractV1()` / `toExportV1()` 已删除。三种导出取节点正文（`note`），没有正文时回落标题。

`source_usage` 是可选的**整张导图来源摘要**：agent 的 `mindmap_generate_deep` 在合并候选树时按实际输入重建（`backend/app/agent/tools.py::_generate_deep`），保存导图时一并保存；旧数据省略时默认为空列表。它只记录来源 ID、类型、文件名和使用的字符数，不保存原文，也没有节点到来源片段的对应关系，不能作为节点级引用或事实验证。没有来源的生成该列表为空；后续完整追溯还需要来源存储、分段定位与节点关联。

## 已实现

| 方法和路径 | 请求 | 成功响应 | 失败情况 |
|---|---|---|---|
| `GET /api/health` | 无 | `{"status":"ok","version":"..."}` | — |
| `POST /api/ingestion/text` | `{"text":"内容","kind":"text 或 markdown"}` | `{"source": 来源}` | 空文本或其他来源类型 422 |
| `POST /api/optimization/check` | `{"content":导图}` | 结构诊断列表 | 结构无效 422 |
| `POST /api/optimization/suggest` | `{"content":导图}` | 诊断与候选建议 | 结构无效 422；建议需用户判断 |
| `POST /api/export/puml` | `{"content":导图}` | PlantUML 文本；根节点来自 `root.label` | 结构无效 422 |
| `POST /api/export/markdown` | `{"content":导图}` | Markdown 大纲：标题 + 缩进列表 | 结构无效 422 |
| `POST /api/export/opml` | `{"content":导图}` | OPML 2.0，可导入 XMind | 结构无效 422 |
| `POST /api/ingestion/files` | multipart，字段名 `file` | `{"source": 来源}`，`source.text` 是解析出的正文 | 扩展名不在白名单 422；文档/图片超 10MB 或音频超 20MB 413；扫描件无文本层、音频无人声、图片视觉不可用、缺语音凭证 422 |
| `GET /api/ingestion/formats` | 无 | `{"document":[…],"audio":[".wav"],"image":[".png",…],"maxUploadBytes":…,"maxAudioBytes":…,"maxImageBytes":…}` | — |
| `GET /api/mindmaps` | 无 | 导图记录数组 | — |
| `POST /api/mindmaps` | `{"content":导图}` | 201，导图记录 | 结构无效 422 |
| `GET /api/mindmaps/{map_id}` | 无 | 导图记录 | 不存在 404 |
| `PUT /api/mindmaps/{map_id}` | `{"version":1,"content":导图}` | 更新后的记录，版本加 1 | 不存在 404；版本过期 409；结构无效 422 |
| `POST /api/agent/runs/stream` | `{"run_id":"…","goal":"…","intent":"create / expand / edit","thinking_model":"default / first-principles / feynman / systematic / critical","snapshot":导图或省略,"selected_ids":[…],"sources":[…],"prefs_applied":["diagram_type.default=fishbone"],"budget":{…}}` | SSE v2：每帧 `data: <json>`，信封 `{seq, type, ...}`（`backend/app/agent/contracts.py::_EventBase` 只有 `seq`），`type` ∈ `stage` / `sources` / `tool_start` / `tool_end` / `ops` / `snapshot` / `node_grow` / `heartbeat` / `done` / `error`；响应体最前面还有一行 **SSE 注释帧** `: 正在把材料收进知识库`（入库在首帧之前，先用它把首字节送出去；不是数据帧、不占 `seq`）；`done` 带 `mode`（`demo` / `model`，序列化用 `exclude_none=True`，所以 demo / model 帧必带，缺字段只说明对面"没说"），产物是**候选**；另外**只在没产出导图时** `done` 还带 `text` / `reasoning`——模型收尾那一轮的原话与思考内容（各截断到 4000 字），用于排障"它为什么只写字、不调工具"；模型思考期间还有 `heartbeat` 帧（`{seq, type:"heartbeat", waited_ms}`，每 10 秒一帧，只保活、不带业务进度，详见协议文档）；`sources` 帧**只在材料真的进了知识库时出现**（见下） | 目标为空 422；来源合计超出模型上下文 422；同一个 `run_id` 正在跑 409（`run_in_progress`）；预算用尽或模型失败以 `error` 事件下发 |
| `GET /api/knowledge/library` | 可选 `limit`（默认 50，范围 1–200；越界 422） | `{"items":[…],"total":N}`；每条含 `source_id` / `filename` / `kind` / `chars` / `chunks` / `created_at` / `last_used_at` / `cited_by`（被几张**已保存导图**引用，`source_usage` 与节点 `source_refs` 都算，同图去重；该计数带 30 秒缓存，缓存键是导图表指纹，**存完/改完导图再列库会立刻反映**） | — （库读不出来时返回空列表；导图表里有读不出的行时该行跳过、`cited_by` 按 0 计，都不返回 500） |
| `GET /api/knowledge/library/{source_id}` | 无 | `{"source_id","filename","kind","text","truncated","total_chars"}`，正文最多前 4000 字 | 库里没有这份 404（`code=not_found`）；库本身读不出来 503（`code=library_unavailable`，稍后重试） |
| `GET /api/agent/runs` | 查询参数 `status`（`done` / `cancelled` / `failed` / `running`，可省略）、`unapplied`（默认 `true`）、`limit`（1–100，默认 20） | 运行台账数组（`run_id` / `status` / `intent` / `goal` / 时间戳），**不含候选正文**。`status` 省略时取 `done` + `cancelled` 中"有草稿且既没应用也没丢弃"的 | — |
| `GET /api/agent/runs/{run_id}` | 无 | 同上再加 `candidate`（整棵树）、`failure`、`mode` | 不存在 404 |
| `POST /api/agent/runs/{run_id}/applied`<br>`POST /api/agent/runs/{run_id}/dismissed` | 无 | 204；幂等（只打第一个时间戳） | 不存在 404 |
| `GET /api/prefs` | 无 | 全部偏好：`key` / `value` / `scope` / `source` / 两个时间戳，以及 `last_applied_at`（来自 `runs.request.prefs_applied`，为空 = 从未生效） | — |
| `PUT /api/prefs/{pref_key}` | `{"value":"…","source":"user_explicit / suggestion_accepted","source_run_id":null}` | `{"pref":…,"changed":true/false}`；同值重复提交 `changed=false` 且不刷新 `updated_at` | 键非法 / 枚举外 / 备注超 8 条或 500 字 400（`pref_key_invalid` / `pref_value_invalid` / `pref_limit_exceeded`） |
| `DELETE /api/prefs/{pref_key}` | 无 | 204，硬删（没有软删） | 不存在 404（与"删了但还在"必须分得开） |
| `GET /api/prefs/suggestions` | 无 | 候选建议数组（`key` / `value` / `label` / `count` / `message`）。**只读不落库**：最近 3 次有效选择都是同一个值、且与当前偏好不同才提 | — |

**流式接口只有一套帧格式**，就是 `/api/agent/runs/stream` 的这套；旧的 `/api/generations/stream`（`stage` / `skeleton` / `node` / `done` / `error` + 末尾 `data: [DONE]`）已连路由一起删除。`backend/app/generation/stream_partial.py` 现在只剩增量解析器（`parse_partial` / `collect_new_nodes`），供 agent 的 `node_grow` 预览调用，不再有帧拼装。

**`run_id` 让一次运行有身份（新增）。** 以前请求里没有 run id，服务端没有能力识别"同一次运行又投递了一遍"，于是换标签页、刷新重发、客户端重试都会新开一次真跑——两倍的钱、两份候选，两个标签页各点一次「应用」就是两张图（`create_map` 每次 `uuid4()`）。现在：

- 带同一个 `run_id` 且该行 `status=running` → 409 `run_in_progress`（同一个 run 并发跑两次会互相打架）。
- 带同一个 `run_id` 且该行 `done` → 直接把已有候选按原样的帧重放（`snapshot` → `done`，`mode` 照原值），**不再调模型**。
- 带同一个 `run_id` 且该行 `cancelled` / `failed` → 允许重跑，复用同一个 id（那是用户主动重试，不是重复投递）。重跑期间不清空上一轮的草稿。
- 不带 `run_id`（老客户端）→ 服务端补一个，**响应体逐字节不变**，只是额外落一行台账。

`done` 之后这行冻结：一条迟到的取消不许把已经交付的候选改写成 `cancelled`。SSE 因此从"run 本身"降级成"这一行的一次观察窗口"，帧格式一个字节都没改。

**运行结束（含被取消）时草稿落库。** 这是"关掉页面那 40 秒白花"的解：abort 会让 uvicorn 关掉生成器、`aclosing` 触发 `task.cancel()`，工具里的模型请求被取消且已经花掉的调用不回滚——原先 `ctx.candidate` 只活内存，用户回来什么都看不到。现在 `_frames` 的 `finally` 里写一次（正常 → `done`，异常 → `failed` + `failure`，什么都没发出去就断了 → `cancelled`，**只要候选非空就写**）。前端进页面时问一次 `GET /api/agent/runs`，有残留就在侧栏提示一句（不是弹窗），点「查看」把它载入现有的候选确认流程，点「丢弃」记一笔 `dismissed_at`。
信封只有 `{seq, type, ...}`：`seq` 由服务端补齐，没有 `v` / `runId` / revision 字段（轮次与候选版本在各事件自己的 payload 里）。产物是 `ops` 操作批次或整树 `snapshot` 而不是"增量节点上屏"；`node_grow` 只作深层生成的预览帧、收到 `snapshot` 即作废，前端把它挂在候选副本上，用户点接受才落到画布。`MINDMAP_LLM_MODE=demo` 也作用于 agent：`run()` 入口直接发 `stage` / `snapshot` / `done` 三帧（`done.mode=demo`），不调用模型，因此演示模式下没有 `tool_start` / `ops` / `node_grow` 帧。协议见 `docs/AGENT_STREAM_PROTOCOL.md`，设计背景见 `docs/AGENT_PLAN.md`。

记录形如 `{"id":"...","version":1,"content":{...},"updated_at":"...Z"}`。生成接口不会自动保存。`demo` 为演示模式，不是 AI；`model` 表示请求已成功经模型服务处理，不代表内容必然正确。`/api/agent/tasks` 仍返回固定阶段列表，那是桩，不反映真实进度。

**文档解析按格式走不同链路**（详见 [部署文档](DEPLOY.md)）。`.pdf` 逐页渲染后交模型视觉识别（demo 模式回落文本层，扫描件则 422），处理到第几页由上下文预算算出（`plan_page_count`，扫描页按 379 字/页折算），另有 300 页的转换护栏；`.docx`/`.pptx`/`.xlsx`/`.html`/`.md` 走 Docling 读结构化格式，不加载版面模型、离线可用；`.doc`/`.ppt` 要先经 LibreOffice 转 PDF 再视觉识别，没装 LibreOffice 时 422 并附安装命令；`.xls` 用 `xlrd` 直读单元格；`.png`/`.jpg`/`.webp`/`.gif`/`.bmp` 复用多模态调用；`.wav` 走百炼 Qwen-Audio-ASR（≤300 秒，更长自动切片）。解析失败或拿不到正文时返回 422，不返回空内容。

`source` 是 `/api/ingestion/text` 返回的对象，也可按 `/docs` 中的 Source 结构提交；每次 agent 运行最多 50 个来源，必须均为非空且已解析，ID 不可重复。当前超长会报错，不会静默截断或丢弃来源。

**材料什么时候进知识库，以及 id 为什么会变**：上传只解析、不入库；**生成时**才把这次用到的材料收进知识库（`backend/app/knowledge/remember.py`）。入库后持久 id 与解析时给的临时 id 不同，所以这批 id 的映射会作为 `sources` 帧在事件流最前面下发一趟，前端收到后把本地条目就地改名（`sources.remap`）。之后保存的导图、节点引用、抽屉里取原文用的都是持久 id，重开导图也能对上。整批入库共用一个连接一个事务，并带总预算（`REMEMBER_BUDGET_SECONDS`，5 秒）：超预算或单份失败的材料保留临时 id 继续参与生成——入库是旁路，不能拖住第一帧。开关是 `MINDMAP_LIBRARY_REMEMBER`（只有 `"0"` 表示关），与 `MINDMAP_LIBRARY_SEARCH`（生成时要不要从库里捞材料）是两件事，可以分别关。

**入口只读且不受检索开关影响**：`GET /api/knowledge/library*` 只看库、不改库（也不刷 `last_used_at`——"看了一眼列表"不等于"用到了这份材料"，那个字段是 `prune` 保留与否的依据）。线上把 `MINDMAP_LIBRARY_SEARCH` 置 0 时，这两个接口照常可用。

## 已注册但尚未实现

| 路由 | 当前行为 |
|---|---|
| `POST /api/knowledge/search` | 返回空列表与 `implemented: false`，没有接入检索。 |
| `POST /api/agent/tasks` | 返回固定阶段与 `implemented: false`，不会生成导图；真正能跑的是 `/api/agent/runs/stream`。 |

## 功能接口规划（均未实现）

| 模块 | 候选接口/契约 | 目的 |
|---|---|---|
| 多模态文件 | 已落地 `POST /api/ingestion/files`（文档 + 图片 + WAV）；来源查询接口待确定 | 上传或解析 PDF、Word、PPT、图片、WAV 音频已可用；音频已有逐句时间戳，但节点引用还不能指回"第几秒"，页码级定位也缺。 |
| 生成任务 | `POST /api/generation-tasks`、`GET /api/generation-tasks/{id}`、`GET .../{id}/events` | 多来源输入、持久任务状态与进度；输出仍转换为统一导图结构。当前只有 agent 这一条 SSE 流，没有持久任务。 |
| 优化 | 已有 `/api/optimization/suggest`，持久化和应用接口待确定 | 未来补齐矛盾、缺失、重组、跨节点关系候选及依据，不直接覆盖。 |
| 知识库 | 已落地只读入口 `GET /api/knowledge/library` 与 `.../library/{id}`；写入侧只有"生成时入库" | 材料列表、被引用次数、正文预览可用；检索（`POST /api/knowledge/search`）仍是桩，检索真正参与生成要等记忆模块。`POST /api/knowledge-bases` 系列尚未实现。 |
| AI 代理 | `POST /api/agent/runs/stream`、`GET /api/agent/runs` 系列已实现；`POST /api/agent/tasks` 仍是桩 | 工具循环、候选确认与"运行结束后落草稿"已可用（`runs` 表，SQLite）；任务化长跑、`runs` 的跨进程/跨机存储（现为本地 SQLite，多 worker 共享同一文件）与多角色仍待设计。 |
| 协作 | 房间授权接口 + Yjs WebSocket | 房间鉴权、实时同步、在线状态、恢复；最终协议由协作负责人完成设计。 |
| 更多导出 | `/api/export/puml`、`/markdown`、`/opml` 均已实现；图像导出待开发 | PNG/SVG 渲染要额外依赖，优先用浏览器端截图或交给 PlantUML 服务。 |

未来接口的请求、响应、权限、错误和任务生命周期需在实现前由 RPeGio 与陈嘉涛共同评审，不能将候选路径视为已实现承诺。
