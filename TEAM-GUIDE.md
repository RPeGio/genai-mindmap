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

当前基线：后端 50 个测试（pytest）、前端 5 个 vitest 用例，全部通过。CI（`.github/workflows/ci.yml`）跑的是同一组命令，PR 上两个 job 必须双绿。

`unittest` 默认可能将进度输出到 stderr；这本身不代表失败。请同时检查最后的 `OK` 和 `$LASTEXITCODE`，测试成功时退出码应为 0。

---

## 3 目录结构

```
backend/
  app/
    main.py            应用装配，注册 8 组路由
    contracts/         数据契约（改这里要通知另一个人）
      mindmap.py       MindMapContent / Node / 请求响应模型
      source.py        Source / SourceUsage
      diagnostic.py    Diagnostic / Suggestion
    ingestion/         多模态输入 → Source        （只有文本可用）
    pipeline/          四段流水线编排             （可用）
    generation/        模型调用（demo / model）  （可用）
    optimization/      结构诊断与建议             （三条规则）
    knowledge/         知识检索                   （桩）
    agent/             AI 代理                    （桩）
    export/            导出 puml                  （puml 可用）
    mindmaps/          保存 / 读取 / 版本冲突     （可用）
  tests/               50 个测试
frontend/
  src/
    App.vue            工作台（输入 + 层级预览；画布 R2 接入）
    main.ts            入口
    styles.css         Tailwind 入口
    api/client.ts      fetch 封装 + ApiError（目前只接了 /api/generations）
    api/types.ts       与后端契约 v1 对应的 TS 类型
  tests/app.spec.ts    5 个 vitest 用例
docs/
  FLOW.md              端到端流程 ← 先读这个
  API.md               每条路由实现到什么程度
  ARCHITECTURE.md      模块边界、技术选型及来源
  ARCHITECTURE_ADDENDUM.md  存储迁移、SSE 协议、流水线细节
  PLAN.md              里程碑、验收门、风险表、决策记录（Q1–Q34）
  CONTRACT_V2.md       目标契约（尚未落地，后端仍是 v1）
  W1_DAY1.md           W1 逐日开工清单
  FEATURES_AND_OWNERS.md    26 项功能追踪与 A/B 分工
START_HERE.md          原始的骨架说明
```

---

## 4 现在到底有什么

**能用的**：文本生成导图（demo 或真实模型，同步与流式两条接口）、文档导入（PDF/Word/PPT/Excel/HTML 经 Docling 解析）、多来源流水线、结构诊断（单子节点 / 深度上限 / 同级重复标签）、候选建议、导出 PlantUML、保存与版本冲突检测。

**是桩的**：知识检索（返回空 + `implemented:false`）、AI 代理（返回固定四阶段，不产生导图）。

**返回 501 的**：图片与语音输入（`/api/ingestion/files` 目前只处理文档，图片和语音走 `parse_bytes` 时仍抛待实现）、Markdown 导出、OPML 导出。

这条规矩写在代码注释里，也写在 `docs/FLOW.md` 第 4 节：**不允许返回空对象假装成功**。桩接口必须带 `implemented:false`，让前端能据此禁用入口。

---

## 5 接口一览（16 个路径 / 18 个端点）

```
GET  /api/health
POST /api/generations              文本 → 导图（一次性）
POST /api/generations/stream       文本 → 导图（SSE 流式，页面默认走这条）
POST /api/ingestion/text           文本 → Source
POST /api/ingestion/files          上传文档 → Source（multipart）
GET  /api/ingestion/formats        支持的扩展名与上传上限
POST /api/pipeline/generate        Source[] → 导图 + 阶段 + 诊断
POST /api/optimization/check       导图 → 诊断
POST /api/optimization/suggest     导图 → 候选建议
POST /api/knowledge/search         检索（桩）
POST /api/agent/tasks              目标 → 阶段骨架（桩）
POST /api/export/puml              导图 → PlantUML 文本
POST /api/export/markdown          501
POST /api/export/opml              501
GET  /api/mindmaps                 列表
POST /api/mindmaps                 新建
GET  /api/mindmaps/{id}            读取
PUT  /api/mindmaps/{id}            更新（带版本号，冲突返回 409）
```

---

## 6 两人分工

按 `docs/FEATURES_AND_OWNERS.md` 的 F 编号划分，具体见 `docs/PLAN.md` 第 11 节 Q31：

- **A（我）**：契约与前端（Vue 画布、布局、主题、导出、协作客户端）、部署基建、CI。
- **B（队友）**：后端地基、生成链路、多模态解析、RAG 与诊断、AI 代理、评估集。

W1 两条线并行：B 做 D1–D3，A 做 D2 契约 + D4 画布 + D5 部署，W2 起交叉 review。

交界只有一个：`backend/app/contracts/`。改这里的任何字段，必须两个人同时知道，同步 `docs/CONTRACT_V2.md`、`docs/API.md` 和前端 `src/api/types.ts`（v2 落地后改为 `npm run gen:api` 生成）。

---

## 7 动手前必须知道的四件事

**一、`MindMapContent` 是唯一数据结构。** 节点字段是 `id` / `label` / `children`，不是 `topic`、不是 `text`。上限 300 节点、8 层、每节点 50 个子节点，超了直接 422。

**二、保存是乐观锁。** `PUT /api/mindmaps/{id}` 必须带上读到的 `version`，服务端版本号变了就返回 409，前端要提示用户重新打开，不能强行覆盖。

**三、生成接口不自动保存。** `POST /api/generations` 和 `/api/pipeline/generate` 只返回内容，用户点了保存才落库。

**四、AI 产出是候选。** 结构诊断和建议都不会自动改用户的导图，必须用户确认。手工编辑不能被生成结果覆盖。

---

## 8 已知的坑

- **两个上限不一致**：`/api/ingestion/text` 允许 10000 字，但 `/api/pipeline/generate` 合并后限 8000 字。9000 字的文本能存成 Source，却进不了流水线。要统一的话改 `MAX_INPUT_CHARS` 或 contract 里的 `max_length`。
- **导出用根节点文字，不是标题**：`export/puml` 取的是 `root.label`。前端“导图标题”和根节点文字是两个独立的输入框，改得不一致时，导出的中心主题会跟标题对不上。
- **`stages` 不是实时进度**：它是同步响应里的完成记录。要看实时进度得等 SSE 或任务化改造。
- **前端只接了生成接口**：`src/api/client.ts` 目前只有 `generateMindMap`（`POST /api/generations`），list / get / create / update / pipeline / optimization / export 都还没封装，R2–R3 补齐。
- **当前没有登录和权限**：所有接口裸奔，只能在本机跑，不要往公网放。

---

## 9 下一步优先级

如果只有一个人有余力，按这个顺序：

1. 让真实模型跑通一条完整链路（目前只在 demo 模式验证过，真实密钥环境没验收）。
2. 层级预览换成 `@vue-flow/core` 画布（R2，这是"看起来像产品"的关键）。
3. PDF / Word / PPT 解析（第二个可演示入口）。
4. 其余按 `FEATURES_AND_OWNERS.md` 的 F 编号推进，范围取舍以 `docs/PLAN.md` 第 2 节为准。

别急着上 Celery、Redis、Milvus 这些东西。当前是单进程同步架构，两人团队把时间花在功能上比花在运维上划算得多。
