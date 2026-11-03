# W1 开工清单（11/3 – 11/8）

> 前置：`docs/PLAN.md` 与 `docs/CONTRACT_V2.md` 已 review 通过。
> 每周五验收门：浏览器输入一句话 → 画布逐节点长出导图 → 刷新后能打开，且线上 `IP:8080` 可访问。

## D0 已完成（2026-11-02）

- `dev` 已在远端；`docs/project-plan` 已合入 `dev`（PR #2）；CI 配置已合入（PR #1）；CI 第 1–4 次 run 全绿。
- **fork 仓库的 Actions 必须手动启用一次**：到 Actions 页面点 "I understand my workflows, go ahead and enable them"，否则后续提交不会触发（`PUT /actions/permissions` 与空提交都推不动）。
- 旧 React 骨架已打 tag `v0.1.0-react-scaffold`（指向 `c4ff5e1`）并推送；Vue 重建的 R1 在 `feat/vue-rebuild`（PR #3，待合入 `dev`）。
- 推送代理脚本 `scripts/git-net.ps1` **不入库**（个人环境专用，已从索引删除，本机走 `.git/info/exclude`）；`git config --local http.proxy` 固定代理，`git net` 别名带"端口不通就直连"的回退。
- 新机器仍需执行的只有：`git switch dev && git pull --ff-only`，然后从 `dev` 切 `feat/*` 或 `fix/*`。原 D0 的推送命令已执行完毕，不再保留。

之后所有开发分支都从 `dev` 切出（`feat/*`、`fix/*`），PR 目标一律 `dev`；`dev` 每周日合 `main` 并打 tag。

工作区里 `frontend/package-lock.json` 有既存的 esbuild 版本漂移（未提交），W1 D4 重建前端时会整份重写，**不要**单独提交它。

**服务器（阿里云控制台）**

- 安全组入方向：`22`（限制本机 IP）、`8080`（0.0.0.0/0）。
- 系统是 **Alibaba Cloud Linux 3.2104**，不是 Ubuntu，所有 `apt` 命令作废：

```bash
sudo dnf install -y dnf-utils device-mapper-persistent-data lvm2 git
sudo dnf config-manager --add-repo https://mirrors.aliyun.com/docker-ce/linux/centos/docker-ce.repo
sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker $USER   # 重新登录生效

# swap 2G（2C2G 必须）
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
free -h
```

## D1（11/3）后端地基：配置、HTTP、错误、日志、测试

| 任务 | 交付 |
|---|---|
| 依赖升级 | `requirements.txt`：`fastapi` `pydantic` `pydantic-settings` `httpx` `sqlalchemy>=2` `alembic` `psycopg[binary]` `sse-starlette` `pyjwt` `passlib[bcrypt]` `cryptography` `pytest` `pytest-cov` `pytest-asyncio` |
| `app/core/config.py` | `Settings(BaseSettings)` 收口全部 env（见 PLAN 第 10 节），删除散落的 `os.getenv` |
| `app/core/errors.py` | `AppError(code, message, status)` + 全局 exception handler → `{code,message,detail,request_id}` |
| `app/core/logging.py` | JSON 日志 + `request_id` 中间件（`X-Request-ID` 透传/生成） |
| `app/core/db.py` | SQLAlchemy `engine`/`SessionLocal`/`get_db` 依赖 |
| `generation/service.py` | `urllib` → `httpx.AsyncClient`（超时 60s，`raise_for_status`，异常映射到 `AppError`） |
| 测试框架 | `pytest.ini`；现有 19 个 `unittest` 用例改为 pytest（`TestClient`），保持全绿 |

**DoD**：`pytest -q` 全绿；`/api/health` 返回 `{status, version, commit}`；故意触发 500 时响应体含 `request_id`。
**讲解稿要点**：为什么用 `pydantic-settings` 而不是散落 env；为什么错误体要带 `request_id`。

## D2（11/3–11/4）契约 v2 落地

| 任务 | 交付 |
|---|---|
| 契约实现 | 按 `docs/CONTRACT_V2.md` 改写 `contracts/mindmap.py`（含 6 条校验规则） |
| 迁移脚本 | `backend/scripts/migrate_v1_v2.py` + 单测（v1 样本 → v2 样本） |
| Alembic | `alembic init`、`mindmaps` 表（`jsonb`）、`users` 表、`llm_calls` 表首版迁移 |
| 仓储改造 | `mindmaps/repository.py` 从 `sqlite3` 换成 SQLAlchemy，**对外函数签名不变**，乐观锁语义保留（`UPDATE ... WHERE version = ?`） |
| 类型生成 | 前端 `npm run gen:api` → `src/api/schema.d.ts` 入库；CI 校验 `git diff --exit-code` |

**DoD**：v1 旧库（若本地有 `backend/data/mindmaps.db`）迁移后全部可读；非法数据（重复 id / 关系指向不存在节点）返回 422 且错误信息指明节点 id。
**讲解稿要点**：为什么要 `schema_version` 而不是探测字段；为什么 `pos` 只在 manual 下允许。

## D3（11/4–11/5）单次流式生成 + SSE

| 任务 | 交付 |
|---|---|
| skeleton 调用 | `outline`：只出 `title` + ≤7 个一级分支；`expand`：逐分支补子树 |
| 任务表 | `POST /api/generation-tasks` → `{task_id}`；`generation_tasks` 表（内存态先跑通，W3 落库） |
| SSE | `GET /api/generation-tasks/{id}/events`：`stage` / `node` / `diagnostic` / `done` / `error`，带 `X-Accel-Buffering: no` |
| 降级 | 单分支失败 → `node` 占位 + `error` 事件，其余分支继续；整体失败才 `error` |
| 埋点 | 每次调用写 `llm_calls(model, prompt_tokens, completion_tokens, latency_ms, task_id)` |
| 结构化输出 | 先试 `response_format=json_schema`，失败回退提示词；两种情况都过 `model_validate`，仅对 `ValidationError` 重试一次并把字段路径回灌 |

**DoD**：`curl -N` 能看到逐条 `node` 事件；首节点延迟 < 3s（DeepSeek，短输入）；断网时前端收到 `error` 事件而不是白屏。
**讲解稿要点**：为什么用流式而不是等整棵树；SSE 与同步接口并存的分工。（注：曾评估过「两段式：先骨架再展开」，因要多一次模型调用且代码要维护两套，最终采用单次增量解析。）

## D4（11/5–11/6）Vue 前端重建 + 画布

> 状态（2026-11-03 更新）：**R1、R2 已完成**。
> R1：Vue 3 + TS + Vite + Tailwind 骨架、`src/api/client.ts`（`generateMindMapStream` 流式封装与 SSE 解析）、`src/api/types.ts`。
> R2：`@vue-flow/core` 画布（`src/canvas/MindMapCanvas.vue`、`src/canvas/MindNode.vue`）、自研紧凑树布局（`src/canvas/layout/tidy.ts`，支持向右 / 向左 / 向下 / 双向）、Pinia store（`src/stores/mindmap.ts`）、三栏界面（输入 / 画布 / 节点详情）、截断文字的悬停卡片与详情面板。
> 22 个 vitest 用例；`npm test` 与 `npm run build` 均通过。
> **R2 是只读画布**：不能拖拽节点、不能改文字、不能增删、没有撤销与保存——这些是 R3 的范围。
> 依赖已装 `pinia`、`@vue-flow/core`、`@vue-flow/background`；未装 `@vue-flow/controls`、minimap、dagre。

```powershell
cd frontend
git rm -r src tests   # 旧 React 源码与测试（先在 main 打 tag v0.1.0-react-scaffold）
npm i vue pinia
npm i -D @vitejs/plugin-vue vue-tsc @vue/test-utils vitest jsdom openapi-typescript tailwindcss
npm i @vue-flow/core @vue-flow/background @vue-flow/controls
```

| 任务 | 交付 |
|---|---|
| 工程 | `vite.config.ts` 换 Vue 插件；`tsconfig` 支持 `.vue`；Tailwind 接入 |
| 骨架 | `App.vue` + `stores/mindmap.ts`（Pinia）+ `api/client.ts`（fetch 封装，统一错误 toast） |
| 画布 | `canvas/MindMapCanvas.vue`：`@vue-flow/core` + 自定义节点；`layout/tidyTree.ts` 自研布局（导图/逻辑/时间轴三套） |
| 交互 | 增删改、拖拽、折叠、撤销重做（命令栈）、搜索定位 |
| 测试 | Vue Test Utils：生成→渲染、编辑→保存、撤销恢复 三个用例 |

**DoD**：画布能拖能存能撤销；无 React 依赖残留；`npm run build` 通过。
**讲解稿要点**：tidy tree 的坐标怎么算；undo 栈存的是命令还是快照，为什么。

## D5（11/6–11/7）CI + Compose + 上线

| 任务 | 交付 |
|---|---|
| CI | `.github/workflows/ci.yml`：后端 `pytest --cov`、前端 `vitest` + `build` + `gen:api` 一致性 |
| Compose | `deploy/docker-compose.yml`：`nginx` / `api` / `web`（构建产物）/ `db` / `ys`；`asr` 用 `profiles: [asr]` |
| 配置 | `.env.example`（不含真实密钥）；`deploy/README.md` 部署与回滚步骤 |
| 上线 | `scp` 或 `git pull` + `docker compose up -d --build`；`curl http://IP:8080/api/health` |
| 备份 | `deploy/backup.sh`：`pg_dump` + `tar uploads/` + 保留 7 天；`crond` 每日 03:00 |

**DoD**：从零 `docker compose up` 能在服务器起服务；CI 绿灯；备份脚本手动跑通一次并能在本地恢复。
**讲解稿要点**：五个容器各自职责；为什么 ASR 用 profile 而不是常驻。

## D6（11/7–11/8）文档同步与联调

> 状态（2026-11-02）：**首轮同步已完成**（PR：`docs/sync-vue`）——README / START_HERE / TEAM-GUIDE / ARCHITECTURE / API / FLOW / FEATURES 已按 Vue、v1 现状、部署形态改完；联调仍待 W1 末。

- `README.md`：技术路线改 Vue / FastAPI / PG，删掉未落地的 Zustand/axios/Zod/`viz.js` 承诺（或标注"未采用"）。
- `docs/ARCHITECTURE.md`：React Flow → Vue Flow；补 v2 契约与 PG。
- `docs/API.md`、`docs/FLOW.md`：新增任务/SSE/认证接口。
- `START_HERE.md` / `TEAM-GUIDE.md`：目录结构与启动命令更新（含 Docker 路线）。
- RPeGio 与陈嘉涛联调：真实模型 + 画布 + 保存，跑 PLAN 第 4 节 W1 验收门。

**DoD**：新人按 `START_HERE.md` 从零能跑起来（RPeGio 与陈嘉涛各自按文档走一遍，不许口头补充）。

## 周检查表（周五 20:00）

- [ ] CI 绿；后端覆盖率 ≥50%（W1 目标，W3 到 60%）
- [ ] 线上 `IP:8080` 可访问，`/api/health` 返回 commit
- [ ] 流式 SSE 首节点 < 2s、整棵树 < 4s，截图存证
- [ ] 画布可编辑、可保存、可撤销
- [x] 旧 React 前端已打 tag；四份文档已按 Vue 现状同步（2026-11-02）
- [ ] RPeGio 与陈嘉涛各交一篇「讲解稿」，互讲 30 分钟，勾掉 20 题中已覆盖的
