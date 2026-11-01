# 项目开发骨架使用说明

本目录是在原申报书和 README 基础上搭建的开发起点。**已实现的是最小文本演示闭环，不是项目书全部功能的完成品**。完整功能、责任人、验收方式和实现状态见 [功能清单与分工](docs/FEATURES_AND_OWNERS.md)，系统设计见 [系统架构](docs/ARCHITECTURE.md)。

## 当前可运行范围

- 前端 React + TypeScript：输入文字、树状列表显示、增删改节点、保存和打开；切换导图、重新生成或离开页面前会提醒未保存的内容。
- 后端 FastAPI：生成接口、结构校验、SQLite 保存和读取、版本冲突检测、文本多来源流水线、基础结构提示及 PUML 文本导出。
- 默认 `demo` 生成固定结构，用于联调，**不会调用 AI**。可配置 Chat Completions 兼容模型服务，实际能力取决于服务商和模型。模型调用尚未在真实密钥环境验收。
- 可视化画布、文件/图片/语音处理、知识库、知识图谱、协作、翻译、模型评估等均尚未实现，详见功能清单。
- 多来源流水线记录整图来源摘要并可随导图保存；原文持久化和节点级来源引用尚未实现。合并超过 8000 字会报错，不会悄悄丢弃内容。

**当前只有本地单用户开发能力，没有登录、授权、访问隔离或生产安全加固，不要直接部署到公网或用于敏感数据。**

## 启动（Windows PowerShell）

需要 Python 3.10+ 与 Node.js 20.19+ 或 22.12+。在项目根目录分别打开两个 PowerShell：

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

后端测试：在 `backend` 目录执行 `python -m unittest discover -s tests -v`；前端测试和构建检查：在 `frontend` 目录分别执行 `npm test` 和 `npm run build`。

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

1. 两人先共同阅读 [系统架构](docs/ARCHITECTURE.md) 和 [API 契约](docs/API.md)，固定导图 JSON 格式。
2. 成员 A 负责导图编辑与协作，成员 B 负责 AI 和知识处理；具体任务与依赖见 [功能清单与分工](docs/FEATURES_AND_OWNERS.md)。
3. 先分别在各自电脑跑通生成、编辑、保存、重新打开；然后按功能清单实现剩余能力。
4. 修改共享 JSON 或接口前先沟通并同步文档。开发分支通过 PR 合并，避免直接覆盖彼此工作。
