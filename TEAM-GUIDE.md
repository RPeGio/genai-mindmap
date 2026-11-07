# 给组员的阅读说明

这个包里是**能跑起来的开发骨架**，不是完成品。功能按模块留了位，接口和数据结构已经定死，剩下的活是往对应模块里填实现。

打开顺序建议：先看本文把项目跑起来，再读 `docs/FLOW.md` 搞清楚数据怎么流动，最后按自己负责的部分读 `docs/` 下对应的文档。

---

## 1 五分钟跑起来

需要 Python 3.10+ 和 Node.js 20.19+ / 22.12+。开两个 PowerShell 窗口，都从解压后的根目录出发。

**后端**（第一个窗口）：

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

看到 `Uvicorn running on http://127.0.0.1:8000` 就好了。接口文档在 http://127.0.0.1:8000/docs 。

如果 PowerShell 不让执行脚本，用后一种方式绕开激活步骤：

```powershell
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

**前端**（第二个窗口）：

```powershell
cd frontend
npm install
npm run dev
```

打开终端打印的地址（默认 http://localhost:5173）。前端通过 Vite 代理把 `/api` 转到 `127.0.0.1:8000`，后端没起会报“无法连接后端”。

**默认是演示模式**，不调用任何 AI，生成出来的是固定占位结构。要接真实模型，在启动后端的那个窗口先设环境变量：

```powershell
$env:MINDMAP_LLM_MODE = 'model'
$env:MINDMAP_LLM_BASE_URL = 'https://你的服务商/v1'
$env:MINDMAP_LLM_MODEL = '模型名'
$env:MINDMAP_LLM_API_KEY = '你的密钥'
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

模型服务必须兼容 `/chat/completions`，且能按提示词返回 JSON。切回演示模式：`$env:MINDMAP_LLM_MODE = 'demo'`。

数据默认存在 `backend/data/mindmaps.db`（SQLite，已排除在版本控制外）。

---

## 2 怎么验证没改坏

```powershell
# 后端
cd backend
pytest -q            # pytest.ini 已设 pythonpath = .

# 前端
cd frontend
npm test
npm run build
```

当前基线（2026-11-06）：后端 309 个测试（pytest）、前端 239 个 vitest 用例（16 个文件）。CI（`.github/workflows/ci.yml`）跑的是同一组命令，PR 上两个 job 必须双绿。

本机跑后端测试时如果出现 `LegacyOfficeTest::test_xls_cell_formatting` 失败、报 `No module named 'xlrd'`，是依赖没装齐（`.xls` 直读要用它），`pip install xlrd` 即可，不是代码问题。

开发服务器左上角有一排「布局示例」按钮（单节点 / 向右 / 向左 / 向下 / 双向）：点一下就能不经过后端装载一份示例导图，用来肉眼检查布局与文本截断。数据在 `frontend/src/dev/samples.ts`，只在 `npm run dev` 下按需加载，生产构建里不进包。

`unittest` 默认可能将进度输出到 stderr；这本身不代表失败。请同时检查最后的 `OK` 和 `$LASTEXITCODE`，测试成功时退出码应为 0。

---

## 3 目录结构

```
backend/
  app/
    main.py            应用装配，注册 8 组路由（19 个端点）
    contracts/         数据契约（改这里要通知另一个人）
      mindmap.py       MindMapContent / Node / 请求响应模型
      source.py        Source / SourceUsage / SourceRef（节点引用）
      diagnostic.py    Diagnostic / Suggestion
      ops.py           导图操作（add / update / remove / move）
    ingestion/         多模态输入 → Source（文本 / 文档 / 图片 / WAV）
    pipeline/          四段流水线编排
    generation/        模型调用（demo / model，同步 + SSE 流式）
    optimization/      结构诊断与建议（三条结构规则 + 引用核验）
    knowledge/         知识检索（桩）
    agent/             AI 代理（工具循环 / op 执行器 / SSE v2）
    export/            导出 puml / markdown / opml
    mindmaps/          保存 / 读取 / 版本冲突
  tests/               309 个测试
frontend/
  src/
    App.vue            工作台（三栏：输入 / 画布 / 节点详情，含保存、打开、冲突弹层、导出、来源面板）
    main.ts            入口（挂 Pinia 与 Vue Flow 样式）
    styles.css         Tailwind 入口
    api/client.ts      fetch 封装 + ApiError（生成 3 个、导图 CRUD 4 个、上传 1 个、导出 1 个、agent 1 个）
    api/types.ts       与后端契约 v1 对应的 TS 类型（`note` 为 v2 预留）
    canvas/MindMapCanvas.vue  Vue Flow 容器：平移 / 缩放 / 点选 / 键盘建树
    canvas/MindNode.vue       自定义节点：截断 + 悬停看全文 + 内联编辑
    canvas/layout/tidy.ts     自研紧凑树布局（向右 / 向左 / 向下 / 双向）
    mindmap/edits.ts   纯函数：改名 / 加子节点 / 加兄弟 / 删子树 / 提交前校验
    mindmap/ops.ts     纯函数：applyOps（与后端 agent/ops.py 共读同一份用例）
    sources/input.ts   来源收集常量与校验（格式白名单、字数与大小上限）
    audio/             浏览器录音（16kHz WAV）与 WAV 头处理
    components/        AgentPanel / RecordButton / SourceInputs
    stores/mindmap.ts  Pinia：内容、布局、选中、编辑、撤销重做、保存状态、agent 候选
    stores/sources.ts  Pinia：来源列表
    dev/samples.ts     开发期布局示例（仅 `npm run dev` 动态加载）
  tests/               239 个 vitest 用例（纯函数 / 布局 / store / 编辑 / 保存 / 界面 / agent）
docs/
  FLOW.md              端到端流程 ← 先读这个
  API.md               每条路由实现到什么程度
  ARCHITECTURE.md      模块边界、技术选型及来源
  ARCHITECTURE_ADDENDUM.md  存储迁移、SSE 协议、流水线细节
  PLAN.md              里程碑、验收门、风险表、决策记录（Q1–Q36）
  CONTRACT_V2.md       目标契约（尚未落地，后端仍是 v1）
  HANDOFF.md           需要对方知悉或确认的跨人决策（待确认 / 已确认）
  W1_DAY1.md           W1 逐日开工清单
  FEATURES_AND_OWNERS.md    26 项功能追踪与 A/B 分工
START_HERE.md          原始的骨架说明
```

---

## 4 现在到底有什么

**能用的**：文本生成导图（demo 或真实模型，`/api/generations` 同步、`/stream` 流式）、多来源混合输入（文本 / 文档 / 图片 / WAV / 浏览器录音，合并后一次生成）、结构诊断（单子节点 / 深度上限 / 同级重复标签）与候选建议、节点级来源引用（三层校验）、三种导出（PUML / Markdown / OPML）、保存与版本冲突检测、agent 运行（工具循环 + 候选确认，`POST /api/agent/runs/stream`）。

**是桩的**：知识检索（返回空 + `implemented:false`）、`POST /api/agent/tasks`（返回固定四阶段，不产生导图；真正的 agent 走 `/agent/runs/stream`）。

**没有 501 的导出**：Markdown 与 OPML 在 2026-11-05 已实现，三种格式都从浏览器直接下载。

这条规矩写在代码注释里，也写在 `docs/FLOW.md` 第 4 节：**不允许返回空对象假装成功**。桩接口必须带 `implemented:false`，让前端能据此禁用入口。

### 各格式解析的前提

| 格式 | 走哪条路 | 前提 |
|---|---|---|
| `.pdf` | 逐页渲染后交模型视觉识别 | 要 `MINDMAP_LLM_MODE=model` 与密钥；≤20 页；demo 模式回落文本层、扫描件 422 |
| `.docx` `.pptx` `.xlsx` `.html` `.md` | Docling 读结构化格式 | 不需要模型，离线可用 |
| `.doc` `.ppt` | LibreOffice 转 PDF 再视觉识别 | 要装 LibreOffice（约 1.5 GB），没装时 422 并附安装命令 |
| `.xls` | `xlrd` 直读单元格 | 要 `xlrd` 依赖 |
| `.png` `.jpg` 等 | 复用多模态调用 | 同 PDF，demo 模式或未配密钥 422 |
| `.wav` | 百炼 Qwen-Audio-ASR | 要 `DASHSCOPE_API_KEY`；≤300 秒，更长自动切片 |

详细依赖、体积与耗时见 `docs/DEPLOY.md`；`.env` 不会自动加载，必须在启动前把变量设进进程环境。

---

## 5 接口一览（17 个路径 / 19 个端点）

```
基础
GET  /api/health

生成
POST /api/generations              文本 → 导图（一次性）
POST /api/generations/stream       文本 → 导图（SSE 流式，页面默认走这条）

输入（多来源）
POST /api/ingestion/text           文本 → Source
POST /api/ingestion/files          上传文档 / 图片 / WAV → Source（multipart，≤10MB，音频 ≤20MB）
GET  /api/ingestion/formats        支持的扩展名与上传上限
POST /api/pipeline/generate        Source[] → 导图 + 阶段 + 诊断

优化
POST /api/optimization/check       导图 → 诊断（可带 sources 做引用核验）
POST /api/optimization/suggest     导图 → 候选建议 + 诊断

导出
POST /api/export/puml              导图 → PlantUML 文本
POST /api/export/markdown          导图 → Markdown 文本
POST /api/export/opml              导图 → OPML 文本

保存
GET  /api/mindmaps                 列表
POST /api/mindmaps                 新建（version 从 1 起）
GET  /api/mindmaps/{id}            读取
PUT  /api/mindmaps/{id}            更新（带版本号，冲突返回 409）

代理与检索
POST /api/agent/runs/stream        目标 + 来源 → SSE v2 事件与候选（可用）
POST /api/agent/tasks              目标 → 阶段骨架（桩，implemented:false）
POST /api/knowledge/search         检索（桩，返回空）
```

---

## 6 RPeGio 与陈嘉涛的分工

按 `docs/FEATURES_AND_OWNERS.md` 的 F 编号划分，具体见 `docs/PLAN.md` 第 11 节 Q31：

- **A（RPeGio）**：契约与前端（Vue 画布、布局、主题、导出、协作客户端）、部署基建、CI。
- **B（陈嘉涛）**：后端地基、生成链路、多模态解析、RAG 与诊断、AI 代理、评估集。

W1 两条线并行：B 做 D1–D3，A 做 D2 契约 + D4 画布 + D5 部署，W2 起交叉 review。

交界只有一个：`backend/app/contracts/`。改这里的任何字段，必须两个人同时知道，同步 `docs/CONTRACT_V2.md`、`docs/API.md` 和前端 `src/api/types.ts`（v2 落地后改为 `npm run gen:api` 生成）。

---

## 7 动手前必须知道的四件事

**一、`MindMapContent` 是唯一数据结构。** 节点字段是 `id` / `label` / `children` / `source_refs`（后者可选，节点级引用），不是 `topic`、不是 `text`。上限 300 节点、8 层、每节点 50 个子节点、标题与节点文字各 200 字，超了直接 422。`note` 仍属 v2 预留，后端 v1 契约是 `extra="forbid"`，发过去就 422。

**二、保存是乐观锁。** `PUT /api/mindmaps/{id}` 必须带上读到的 `version`，服务端版本号变了就返回 409，前端要提示用户重新打开，不能强行覆盖。

**三、生成接口不自动保存。** `POST /api/generations` 和 `/api/pipeline/generate` 只返回内容，用户点了保存才落库。

**四、AI 产出是候选。** 结构诊断和建议都不会自动改用户的导图，必须用户确认。手工编辑不能被生成结果覆盖。

---

## 8 已知的坑

- **两个文本入口的上限不同**：`/api/ingestion/text` 与 `/api/generations` 都是单段 10000 字，`/api/pipeline/generate` 合并后限 20000 字（`backend/app/pipeline/service.py:29`）。前端来源面板按 20000 字提示，所以 15000 字走 pipeline 没问题、走 `/api/generations` 会被 422 挡下。
- **引用的原文不落库**：`source_refs` 随导图一起保存（`source_id` + `quote` + 可选的页码与字符区间），但 `Source.text` 只活在本次会话的来源列表里（`frontend/src/stores/sources.ts`）。刷新后重新打开导图，节点角标、来源名与 `quote` 还在，抽屉里却没有前后文——界面会显示"原文没有随导图保存"。另外后端**只在生成/流水线时校验引用**，保存时不复检，所以手工改过节点文字之后，旧引用可能已经对不上原文。
- **导出用根节点文字，不是标题**：`export/puml` 取的是 `root.label`。前端“导图标题”和根节点文字是两个独立的输入框，改得不一致时，导出的中心主题会跟标题对不上。
- **导出会丢掉节点正文（`note`）**：三种导出都收 v1 的 `MindMapContent`，`Node` 是 `extra="forbid"`，所以 `frontend/src/api/client.ts::exportMindMap()` 跟保存一样先调 `toContractV1()` 把 `note` 裁掉再发。结果是导出的文字只有 `label`（画布上的短标题），详情面板里的多行正文进不了文件。等 v2 上线要改成按正文导出。
- **`stages` 不是实时进度**：它是同步响应里的完成记录。要看实时进度得用 SSE（`/api/generations/stream`、`/api/agent/runs/stream`）。
- **前端还没接的接口**：`POST /api/ingestion/text`、`GET /api/ingestion/formats`、`POST /api/optimization/check` 与 `/suggest`。上传格式白名单目前写死在 `frontend/src/sources/input.ts` 的 `FILE_ACCEPT`，与后端 `formats` 接口有漂移风险；诊断接口已有返回值但界面不渲染（`frontend/src/api/types.ts` 里 `diagnostics` 标注为"目前只用于排障"）。
- **编辑不记协作方**：保存以整棵树为单位（`version` 乐观锁），没有字段级合并；两个人同时编辑同一张图，后保存的会拿到 409，只能选择覆盖（重新加载）或另存为新导图。
- **撤销栈只在内存**：刷新页面后撤销历史清空，最多 50 步；保存不写历史。
- **旧格式要系统依赖**：`.doc` / `.ppt` 依赖 LibreOffice（约 1.5 GB），没装时 422 而不是降级；`.xls` 依赖 `xlrd`。部署机装不装，直接决定这几个格式能不能用。
- **当前没有登录和权限**：所有接口裸奔，只能在本机跑，不要往公网放。

---

## 9 下一步优先级

如果只有一个人有余力，按这个顺序：

1. **诊断面板**（RPeGio，F15）：接 `POST /api/optimization/check` 与 `/suggest`，把 `diagnostics` 渲染出来（单子节点、深度上限、重复标签、引用核验失败原因），建议仍是候选、需用户确认。
2. **契约 v2 定稿**（RPeGio 与陈嘉涛，见 `docs/HANDOFF.md` 的 H1/H2）：`note` 与 v2 同批上线；layout/theme/relations 再排期。
3. **真实密钥环境整体验收**（RPeGio 与陈嘉涛）：真实模型链路只在 2026-11-04 的单份英文论文样本上量过（`docs/LLM-OUTLINE-ACCURACY.md`），中文样本、语音、图片都还没有系统评测。
4. **部署骨架**（RPeGio，F22）：`Dockerfile` + `deploy/docker-compose.yml` + nginx，目标单台 2C2G、`IP:8080` 可访问；没有登录和权限之前只能内网演示。

已完成：**把引用搬到界面上**（F08，2026-11-07，`fc8f82f`）——节点右上角显示引用条数，选中后在右侧详情面板列出来源名与位置（`第 19–37 字`，有页码时一并给出），点一条在抽屉里看原文并把命中句标黄；原文不落库，跨会话只剩生成时记下的 `quote`。

其余按 `FEATURES_AND_OWNERS.md` 的 F 编号推进，范围取舍以 `docs/PLAN.md` 第 2 节为准。

别急着上 Celery、Redis、Milvus 这些东西。当前是单进程同步架构，RPeGio 与陈嘉涛的时间花在功能上比花在运维上划算得多。
