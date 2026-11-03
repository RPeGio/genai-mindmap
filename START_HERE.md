# 项目开发骨架使用说明

本目录是在原申报书和 README 基础上搭建的开发起点。**已实现的是最小文本演示闭环，不是项目书全部功能的完成品**。完整功能、责任人、验收方式和实现状态见 [功能清单与分工](docs/FEATURES_AND_OWNERS.md)，系统设计见 [系统架构](docs/ARCHITECTURE.md)，里程碑与工程门见 [项目执行计划](docs/PLAN.md)，目标契约见 [CONTRACT_V2](docs/CONTRACT_V2.md)（**尚未落地，后端仍是 v1**）。

## 当前可运行范围

- 前端 Vue 3 + TypeScript：输入文字、按层级预览节点、生成候选导图；画布、编辑、保存及撤销/重做在 W1 R2–R3 接入（旧 React 骨架已打 tag `v0.1.0-react-scaffold` 存档）。
- 后端 FastAPI：生成接口、结构校验、SQLite 保存和读取、版本冲突检测、文本多来源流水线、基础结构提示及 PUML 文本导出。
- 默认 `demo` 生成固定结构，用于联调，**不会调用 AI**。可配置 Chat Completions 兼容模型服务，实际能力取决于服务商和模型。模型调用尚未在真实密钥环境验收。
- 可视化画布、文件/图片/语音处理、知识库、知识图谱、协作、翻译、模型评估等均尚未实现，详见功能清单。
- 多来源流水线记录整图来源摘要并可随导图保存；原文持久化和节点级来源引用尚未实现。合并超过 8000 字会报错，不会悄悄丢弃内容。

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

## 团队从这里开始

1. RPeGio 与陈嘉涛先共同阅读 [系统架构](docs/ARCHITECTURE.md)、[API 契约](docs/API.md) 和 [CONTRACT_V2](docs/CONTRACT_V2.md)，固定导图 JSON 格式；契约改动必须经 RPeGio 与陈嘉涛双人 review 并附迁移脚本。
2. 分工按 [执行计划第 11 节](docs/PLAN.md)：RPeGio 负责契约 / Vue 画布 / 布局主题导出 / 部署与 CI，陈嘉涛负责后端地基 / 生成链路 / 多模态解析 / RAG / 诊断。任务与依赖仍以 [功能清单与分工](docs/FEATURES_AND_OWNERS.md) 的 F 编号为准。
3. RPeGio 与陈嘉涛先在本人电脑跑通生成（R1 已有）与保存/重新打开（R3 完成后），再按功能清单实现剩余能力。
4. 分支：`main` ← `dev` ← `feat/*`·`fix/*`，**PR 目标一律 `dev`**，提交信息用中文 Conventional Commits，PR 模板三问必填；`dev` 每周日合 `main` 并打 tag。CI 必须绿才能合并。
