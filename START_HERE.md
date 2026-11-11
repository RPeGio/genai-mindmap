# 项目开发骨架使用说明

本目录是在原申报书和 README 基础上搭建的开发起点。**已实现的是最小文本演示闭环，不是项目书全部功能的完成品**。完整功能、责任人、验收方式和实现状态见 [功能清单与分工](docs/FEATURES_AND_OWNERS.md)，系统设计见 [系统架构](docs/ARCHITECTURE.md)，里程碑与工程门见 [项目执行计划](docs/PLAN.md)，目标契约见 [CONTRACT_V2](docs/CONTRACT_V2.md)（**尚未落地，后端仍是 v1**），需要双方确认的跨人决策见 [交接文档](docs/HANDOFF.md)。

## 当前可运行范围

- 前端 Vue 3 + TypeScript：文本 / 文档 / 图片 / 录音多来源输入 → 流式或流水线生成 → `@vue-flow/core` 画布（平移、缩放、点选节点看全文，支持向右 / 向左 / 向下 / 双向四种布局）→ 双击改文字 / Tab 加子节点 / Enter 加兄弟 / Delete 删子树 / 撤销重做 / Ctrl+S 保存（`version` 乐观锁，409 时三选弹层）→ 打开已保存导图 → PUML / Markdown / OPML 三种导出 → Agent 面板（候选需点接受才落到画布，旧 React 骨架已打 tag `v0.1.0-react-scaffold` 存档）。
- 后端 FastAPI：生成接口（同步与 SSE 流式）、结构校验、SQLite 保存和读取、版本冲突检测、多来源流水线（文本 / 文档 / 图片 / 语音）、文档上传（PDF 逐页视觉识别、Docling 结构化格式、LibreOffice 转旧格式、`xlrd` 读 `.xls`、百炼 Qwen-Audio-ASR 转写 WAV）、节点级来源引用与三层校验、PUML / Markdown / OPML 导出、Agent 工具循环（`/api/agent/runs/stream`，产物是候选）、统一错误体、基础结构提示。
- 默认 `demo` 生成固定结构，用于联调，**不会调用 AI**。可配置 Chat Completions 兼容模型服务，实际能力取决于服务商和模型。模型调用尚未在真实密钥环境验收。
- 尚未实现：知识库检索与持久知识库（`/api/knowledge/search` 是桩）、知识图谱、协作、翻译、模型评估、多布局与主题风格（v2 契约未落地）、容器化（Docker/compose；线上的 systemd + nginx 方案见 `docs/DEPLOY.md` 第 7 节）。图片与语音处理已在 2026-11-05 落地，别再按"尚未实现"处理。
- 多来源流水线记录整图来源摘要与节点级引用（`source_refs`，机械核验 + 可选 NLI），都能随导图保存；**界面已能展示引用**：节点右上角显示引用条数，选中后在右侧详情面板列出来源与位置，点一条在抽屉里看原文并高亮命中句。原文不持久化，所以跨会话重新打开只剩生成时记下的摘录。合并后文本超过 20000 字会报错，不会悄悄丢弃内容。

**当前只有本地单用户开发能力，没有登录、授权、访问隔离或生产安全加固，不要直接部署到公网或用于敏感数据。**

## 启动（Windows PowerShell）

需要 Python 3.12+ 与 Node.js 20.19+ 或 22.12+（CI 用 3.12 / 22）。在项目根目录分别打开两个 PowerShell：

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

```powershell
cd frontend
npm install
npm run dev
```

打开终端打印的前端地址（默认 http://localhost:5173）。接口文档为 http://127.0.0.1:8000/docs。数据默认保存在 `backend/data/mindmaps.db`，已由 `.gitignore` 排除。

后端测试：在 `backend` 目录执行 `pytest -q`（`pytest.ini` 已设 `pythonpath = .`，也可用 `python -m pytest`）；前端测试与构建检查：在 `frontend` 目录分别执行 `npm test` 和 `npm run build`。CI 配置见 `.github/workflows/ci.yml`，两个 job 分别对应这两组命令。

改了 `backend/app/contracts/` 里的任何字段后，要按这个顺序更新前端类型（漏了哪一步，`pytest`、`vue-tsc` 或 CI 会各报一处）：

```powershell
cd backend
.\.venv\Scripts\python.exe scripts\export_openapi.py   # 更新 backend/openapi.json
cd ..\frontend
npm run gen:api                                        # 重新生成 src/api/schema.d.ts
```

不使用虚拟环境激活也可以用 `.\.venv\Scripts\python -m pip ...` 和 `.\.venv\Scripts\python -m uvicorn ...`。如果 PowerShell 限制脚本执行，可使用后一种方式。

### 配置真实模型（可选）

在**启动后端的同一个 PowerShell 窗口**设置以下变量并启动服务；接口必须兼容 `/chat/completions`，而且模型能够按提示输出 JSON：

```powershell
$env:MINDMAP_LLM_MODE = 'model'
$env:MINDMAP_LLM_BASE_URL = 'https://你的服务商地址/v1'
$env:MINDMAP_LLM_MODEL = '你的模型名称'
$env:MINDMAP_LLM_API_KEY = '你的密钥'
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

请以供应商实际文档为准，不同供应商的 URL、参数、区域和模型能力可能不同。不要把密钥提交进仓库；`backend/.env.example` 仅为变量清单，程序不会自动读取 `.env`。切回演示模式：`$env:MINDMAP_LLM_MODE = 'demo'`。调用真实模型会产生外部请求和可能的费用，避免上传未经授权的资料。

### 另外几个可选变量

| 变量 | 用途 |
|---|---|
| `DASHSCOPE_API_KEY` | 语音转写（百炼 Qwen-Audio-ASR），没配时 `.wav` 上传返回 422 |
| `MINDMAP_SOFFICE_CMD` | 指到 LibreOffice 的 `soffice` 路径，处理 `.doc`/`.ppt`/`.xls` 之外的老格式；不配则从 `PATH` 里找 |
| `MINDMAP_AGENT_ENABLED` | 设为 `0` 关闭 `/api/agent/runs/stream` |
| `MINDMAP_DB_PATH` | SQLite 文件位置，默认 `backend/data/mindmaps.db` |
| `MINDMAP_NLI_ENABLED`、`MINDMAP_NLI_MODEL_DIR`、`MINDMAP_NLI_MIN_FREE_MB` | 可选的引用语义校验（ONNX 模型），不配时只做机械核验 |

PDF 视觉识别复用上面的 `MINDMAP_LLM_*`，并要求 `MINDMAP_LLM_MODE=model`；`demo` 模式下 PDF 只回落到文本层，扫描件会返回 422。

## 团队从这里开始

1. RPeGio 与陈嘉涛先共同阅读 [系统架构](docs/ARCHITECTURE.md)、[API 契约](docs/API.md) 和 [CONTRACT_V2](docs/CONTRACT_V2.md)，固定导图 JSON 格式；契约改动必须经 RPeGio 与陈嘉涛双人 review 并附迁移脚本。
2. 分工按 [执行计划第 11 节](docs/PLAN.md)：RPeGio 负责契约 / Vue 画布 / 布局主题导出 / 部署与 CI，陈嘉涛负责后端地基 / 生成链路 / 多模态解析 / RAG / 诊断。任务与依赖仍以 [功能清单与分工](docs/FEATURES_AND_OWNERS.md) 的 F 编号为准。
3. RPeGio 与陈嘉涛先在本人电脑跑通生成（R1）→ 画布（R2）→ 编辑与保存（R3）整条链路，再按功能清单实现剩余能力。
4. 分支：`main` ← `dev` ← `feat/*`·`fix/*`，**PR 目标一律 `dev`**，提交信息用中文 Conventional Commits，PR 模板三问必填；**`main` 是生产分支**（ECS 上跑的就是它），`dev` 是集成分支，里程碑时 `dev` → `main` 合入并在 `main` 上打 tag（`v0.4.0-deploy` 起）。CI 必须绿才能合并。
