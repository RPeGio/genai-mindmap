# 单机容器化部署（Docker / docker compose）

这一页只讲**一台机器、三个容器、一个 PostgreSQL 实例**的形态：backend（FastAPI + docling）+ frontend（nginx 托静态产物 + `/api` 反代）+ postgres（`postgres:16`，库落在 `mindmap-db` 卷上）。

> **这一页在迁移到 PostgreSQL 之后改过一次口径。** 旧形态是"两个容器 + 一个 SQLite 文件"，数据是 `/data/mindmaps.db` 这个文件；现在数据是 postgres 容器里的一个库，backend 只拿一条连接串连过去。区别不只是少了个文件：**备份不再能拷文件、卷的边界变了（`/data` 不再装数据库）、多 worker 也不再是写者冲突**。下文凡是提到这三点的地方都是重写过的，保留的少量"换库之前是什么样"的说明是为了解释"为什么现在不一样"。

> **先说结论性的一句话**：这套东西**没有在真机上构建或运行过**（开发机上不存在 `docker` 命令），所有"能跑"的结论都是静态核验推出来的。第 9 节逐条列了哪些必须到真机上才能确认。请把这一页当成"一份写得很仔细的部署脚本 + 一份诚实的未验证清单"，而不是"已验证的部署说明"。

---

## 1. 与现有 systemd 部署的关系

仓库里已经有一套**不用容器**的部署（`deploy/`：`install.sh`、`genai-mindmap.service`、`nginx.conf`、`setup-python.sh`、`backup.sh`、CD 入口），目标机是 2C2G 的单台 ECS，细节见 `docs/DEPLOY.md`。**两套用的是同一个 PostgreSQL 大家族**（systemd 那套装在宿主机上，容器版跑在 `postgres` 容器里），这一点是换库之后才成立的——以前两边各自持有一个 SQLite 文件。

**两套部署互斥，不要同时跑。** 三个硬冲突：

| 冲突点 | 后果 |
| --- | --- |
| 端口 80 | systemd 那套的 nginx 与 frontend 容器都监听 80，后启动的直接失败 |
| 端口 8000 | systemd 那套的 uvicorn 监听 127.0.0.1:8000；容器版不发布 8000，但若按调试说明临时发布了，就会撞上 |
| 数据库 | 两边各有自己的一套 PG 与 DSN，同时跑就是**两份互不同步的数据**——用户在这边存的图，在那边看不见；更糟的情况是两边配到了**同一个** PG 库，那是两个写入方共用一个库 |

第三条在 PG 下**换了形状**，不再只是"两个文件不同步"：容器版的库是 compose 里的 `postgres` 服务 + `mindmap-db` 卷，systemd 版是宿主机上的 PG 实例。两者可能① 各自独立（两份数据互不同步）、② 共用一个库（两个应用进程同时写、迁移与备份也互相干扰）、③ 各起一个 postgres 容器（多占一份内存与一个端口）。第 ② 种最隐蔽：它**不报错**，看起来一切正常，直到你在一边改的图从另一边看不见、或者一次 `pg_restore` 把另一边的数据覆盖掉。

真正的风险仍然是这一条：端口冲突会当场报错、你立刻知道；数据库错配则**不报错**，等你切回去才发现"我的导图呢"。

迁移只有一条路：**停一套 → 导数据 → 起另一套**。两个方向的过程见第 6 节。切换期间必须让用户知道服务会中断——这不是蓝绿，是换轮胎。

另外明确一句：**容器化不接管现有生产 CD。** `.github/workflows/` 与 `/usr/local/sbin/genai-cd-apply` 一行都没动，CI 不会构建镜像、也不会部署容器。要不要加一个 build 步骤见第 12 节。

---

## 2. 产出物清单

| 文件 | 作用 |
| --- | --- |
| `docker-compose.yml` | 三个 service（backend / frontend / postgres）、三个 named volume、健康检查、依赖顺序 |
| `docker/backend.Dockerfile` | `python:3.12-slim` + `requirements.txt` + CPU 版 torch |
| `docker/frontend.Dockerfile` | `node:24-slim` 构建 → `nginx:1.27-alpine` 托管 |
| `docker/nginx.conf` | 站点配置：静态托管 + `/api` 反代 + SSE 三条 |
| `docker/.env.example` | 配置模板（**要拷到仓库根**成 `.env`，见第 3 节） |
| `.dockerignore` | 构建上下文排除规则（`.venv` 1.4GB、`node_modules`、`.git`、`data`） |
| `docker/verify-static.py` | 静态核验脚本（第 10 节），只读，需要 PyYAML |

两套部署可以共存于同一分支，靠"你选哪套去跑"区分。**但迁到 PostgreSQL 之后，`deploy/` 不再"全部未改动"了**：`deploy/backup.sh` 从 `sqlite3 .backup` 换成了 `pg_dump -Fc`（第 5 节），`deploy/install.sh` 装的东西里 sqlite 换成了 PostgreSQL。这跟容器版是同一件事的两面——库换成了服务端形态，两套部署的运维动作必须一起换，否则会出现"systemd 那套备份还在拷一个已经不存在的文件"。

---

## 3. 构建与启动

### 3.1 前置

- Docker Engine 20.10+ / Docker Desktop，带 compose v2（`docker compose`，不是老的 `docker-compose`）
- 磁盘：镜像约 1.3–1.7GB（见第 7 节）+ LibreOffice 可选层 1.5GB + postgres 镜像约 150MB
- 内存：后端常驻约 350MB，解析 PDF 时峰值再 +70MB 量级（`docs/DEPLOY.md` 第 5 节）；**再加上 postgres 的一笔**：空闲 45MB / 连接池预热 67MB / 8 线程写压测后 168MB。这笔是从"后端 350MB"之外另算的，换库之前它嵌在 API 进程里（说不清、但确实占着），现在是独立进程（说得清、且空闲也要 45MB）。超预算的处置见第 11 节第 3 条。

### 3.2 配好 `.env`（**必须在仓库根**）

```bash
cp docker/.env.example .env
# 编辑 .env：填 MINDMAP_LLM_* 三项 + 设一个 MINDMAP_PG_PASSWORD
```

为什么模板在 `docker/` 下、成品却在仓库根：`docker-compose.yml` 在仓库根，compose 的 `env_file: - .env` 与 `${VAR}` 插值都按**compose 文件所在目录**解析。放别处等于配置不生效。

#### 3.2.1 升级用户注意：`MINDMAP_PG_PASSWORD` 是新增的**必填项**

放在这里而不是第 11 节，是因为这是升级时**第一个**会撞上的东西：老的 `.env` 里没有这个键，直接 `docker compose up` 会在解析 compose 文件时就失败，错误长这样——

```
error while interpolating services.backend.environment.MINDMAP_PG_DSN:
  required variable MINDMAP_PG_PASSWORD is missing a value: 请在 .env 里设置 MINDMAP_PG_PASSWORD
```

三个要点：

1. **为什么会失败。** compose 里 backend 的 DSN 与 postgres 的 `POSTGRES_PASSWORD` 都写成 `${MINDMAP_PG_PASSWORD:?...}`。`:?` 的语义是"变量没设就让 compose 直接报错退出"，这是**刻意**的：宁可 up 不起来，也不要一个用 `postgres/postgres` 这种公开口令跑起来的库。这与 `env_file` 缺文件报错、与 `deploy/genai-mindmap.service` 的 `EnvironmentFile=` 不带 `-` 前缀是同一个用意——**不许静默带空配置跑**。
2. **怎么填。** 只用十六进制字符（`openssl rand -hex 24`）。别用带 `@ : / # $` 或空格的密码：这个值会被原样拼进 backend 的 `MINDMAP_PG_DSN`（一个 URL），那些字符会把 URL 拆坏，表现为**认证失败而口令看起来没错**。
3. **改它不会改库里的口令。** `POSTGRES_PASSWORD` 只在 initdb 那一次生效。`mindmap-db` 卷已经初始化过之后，改 `.env` 里的值只会让 backend 拿新口令去连一个还认旧口令的库——症状同样是认证失败。真要换口令得进容器 `psql -c "ALTER USER mindmap PASSWORD '...'"`，再改 `.env`。要彻底重来就 `docker compose down -v`（**连数据一起删**，见第 11 节第 9 条）。

#### 3.2.2 三件必须知道的事

1. **缺 `.env` 会直接启动失败**，不是静默带空配置跑。这是刻意的，与 `deploy/genai-mindmap.service` 里 `EnvironmentFile=` 不带 `-` 前缀同一个用意。换库之后失败点更靠前了一层：以前是"库路径回落进镜像的只读层，表现成一保存就 500"；现在 `MINDMAP_PG_PASSWORD` 由 `${...:?}` 必填插值把关，**compose 在起容器之前就拒绝解析**。
2. **别在 `.env` 里直接写 `MINDMAP_PG_DSN`。** compose 的 `environment` 段已经从口令和 postgres 服务名拼好了这条串，`environment` 覆盖 `env_file`，所以 `.env` 里那条写了也不生效（模板里是注释掉的）。更要紧的是：`DEFAULT_DSN` 是 `postgresql:///mindmaps?host=/var/run/postgresql`（Unix socket），**容器里没有那个 socket**——真让 DSN 落回缺省值，表现是连接失败而不是"用了默认库"。
3. **后端没有 dotenv**。`backend/app/**` 里不存在 `load_dotenv`，它只读进程环境变量。`env_file` 不是"让程序去读文件"，而是 compose 替你把这些键注入进程环境。
4. 仓库根的 `.env` / `.env.*` 已被 `.gitignore` 忽略，密钥不会进版本库。

**必填四项**（`MINDMAP_LLM_MODE` / `_BASE_URL` / `_API_KEY` / `_MODEL`）：代码的缺省值不是 demo，不配时 agent 因缺密钥直接报错，不会降级成占位图。加上第 3.2.1 节的 `MINDMAP_PG_PASSWORD`，容器版一共**五项必填**。

### 3.3 构建与启动

```bash
docker compose build            # 构建两个镜像（postgres 用官方镜像，不构建）
docker compose up -d            # 启动
docker compose ps               # 看健康状态（backend / frontend / postgres 三个都该是 healthy）
docker compose logs -f backend  # 跟日志
```

打开：

- `http://<主机>/` —— 前端
- `http://<主机>/api/health` —— 后端健康检查，返回 `{"status":"ok","version":"..."}`

`version` 来自 `MINDMAP_VERSION` 环境变量（`backend/app/main.py` 的 `_app_version()`，缺省 `dev`）。想让它有意义就在 `.env` 里加 `MINDMAP_VERSION=<提交或标签>`。

前端端口不是 80 就在 `.env` 里改 `WEB_PORT=8080`。

### 3.4 构建参数（两个）

```bash
# 国内机器换 pip 镜像（torch 的 CPU 轮子固定在官方源，不走这个）
PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple docker compose build backend

# 装 LibreOffice，支持 .doc / .ppt（约 1.5GB，会顶穿 2.5GB 体积预算）
WITH_OFFICE=1 docker compose build backend
```

两个都能写进 `.env`，不用每次带环境变量：

```bash
docker compose build --build-arg WITH_OFFICE=1 backend   # 等价写法
```

`WITH_OFFICE=0`（默认）时 `.doc` / `.ppt` 上传收到一条"需要 LibreOffice"的 422，**其余格式全部不受影响**（`.pdf` / `.docx` / `.pptx` / `.xlsx` / `.xls` / `.html` / `.md` / 图片 / 音频）。要不要为两种旧格式付 1.5GB，见 `docs/LEGACY-FORMAT-COST.md`。

### 3.5 不想每次都重建

```bash
docker compose up -d --build    # build + up 一步
docker compose down             # 停并删容器（三个卷保留，库还在）
docker compose down -v          # 连卷一起删（`mindmap-db` 里的库没了，慎用）
```

---

## 4. 数据在哪

**唯一的状态是 `postgres` 容器里的 `mindmaps` 库**，数据目录落在 named volume `mindmap-db` 上（挂到 `/var/lib/postgresql/data`）。backend 容器里**没有任何数据文件**——它只有一条连接串（`MINDMAP_PG_DSN`，指向 compose 内网的 `postgres:5432`），所有读写都过网络到那个库。

这个库装的不只是"导图"：导图、运行记录、知识库、块向量缓存、外部检索缓存、账号与设置全在**同一个库的几张表**里。所以还是一个库，不存在"要不要再挂一个上传目录"的问题。

> **没有上传目录。** 上传走 `UploadFile` 读进内存（`backend/app/ingestion/router.py:80`，上限 10MB / 音频 20MB / 图片 10MB），不落盘。唯一落盘的是 `.doc` / `.ppt` 转换时的临时目录（`backend/app/ingestion/legacy_office.py:80` 的 `tempfile.TemporaryDirectory`），退出即清，用的是容器 `/tmp`。别在文档里给自己编一个"uploads 卷"。

`mindmap-data` 卷**仍然要用**，但它现在装的是 docling 的 `HOME` 缓存（`/data/.cache/docling`）、上传中转与临时文件，**不再装数据库**。换库之前它是"唯一的状态文件"所在的卷，也是备份的对象；现在备份走 `pg_dump`，这个卷丢了只是缓存重下。

另一个卷 `mindmap-models` 挂到 `/models`，装的是 docling 的版面/表格模型（首次解析 PDF/DOCX 时从 HuggingFace 下载，几百 MB）。**丢了只是下次要重新下**，不是数据损失。

| 卷 | 容器内路径 | 内容 | 丢了会怎样 |
| --- | --- | --- | --- |
| `genai-mindmap_mindmap-db` | `postgres` 的 `/var/lib/postgresql/data` | PG 的数据目录（导图/知识库/账号/设置全在里面） | **全部数据丢失**（除非有 `pg_dump` 备份）——这是最要命的那个 |
| `genai-mindmap_mindmap-data` | `backend` 的 `/data` | docling `HOME` 缓存、临时文件 | 下次解析重新下载模型与缓存，**数据不丢** |
| `genai-mindmap_mindmap-models` | `backend` 的 `/models` | docling/HF 模型缓存 | 下次解析重新下载 |

卷名前缀来自 compose 的 `name: genai-mindmap`（写死，不与目录名绑定）。

**为什么要三个卷而不是一个**：性质三种，处置方式也三种。`mindmap-db` 是**不可重建**的（必须 `pg_dump`，见第 5 节）；`mindmap-models` 是可重建的（几百 MB，重下即可）；`mindmap-data` 介于两者之间（缓存可重建，但丢了会让下一次解析变慢）。混在一个卷里，你就没法说"备份这一个、扔掉那两个"——而 `pg_dump` 也不认识"卷"这个东西，它只认识库。

**`HOME=/data` 仍然是必须的**，不是随手写的。docling 的缓存目录缺省是 `Path.home()/".cache"/"docling"`（`site-packages/docling/datamodel/settings.py:72`），而 tectonic 引擎**只认 HOME**（`docling/backend/latex/engines/tectonic.py:85`）。`HOME` 不可写时，首次解析 docx/pptx 下载模型会失败，而报错长得像"模型有问题"。容器里 `/home/app` 是可写的，但把 `HOME` 指到 `/data` 是为了让**.cache 也落在卷上**，否则容器一重建就重下。

### 4.1 看卷在宿主机哪

```bash
docker volume inspect genai-mindmap_mindmap-db --format '{{ .Mountpoint }}'
```

一般 `/var/lib/docker/volumes/genai-mindmap_mindmap-db/_data`。**别直接去改那个目录里的文件**——PG 的数据目录不是"一堆可以直接编辑的文件"，尤其别在容器跑着的时候往里拷东西。要在库层面操作就用 `docker compose exec postgres psql`，要迁移就 `pg_dump` / `pg_restore`。

> 一个具体的坑：`mindmap-db` 只能用**命名卷**，不能改成宿主机目录的 bind mount。PG 对数据目录的权限很挑，宿主机上一个属主不对（或空目录属主是 root）的目录会让它拒绝启动，日志里是 `data directory has wrong ownership` 或 `could not change permissions`。命名卷由 docker 管属主，这是刻意选的。

---

## 5. 备份与恢复

**备份是 `pg_dump -Fc`，不是拷卷、不是拷文件。** 这一节整节重写过：换库之前 `deploy/backup.sh` 用的是 `sqlite3 "$DB" ".backup '$OUT'"`（不是 `cp`），理由是服务在跑时直接复制文件可能拿到"页写了一半"的副本。这个理由在 PG 下**换了一个更硬的形式**：`mindmap-db` 卷里是 PG 的数据目录，它由 postmaster + 一堆后台进程（checkpointer / walwriter / autovacuum）共同维护，服务在跑时 `cp` 那个目录拿到的是**一份撕裂的集群**——各进程分别写了一部分页、WAL 与数据文件对不上，恢复时根本起不来。

`pg_dump` 在服务端开一个一致性快照，导出的是一份"某一时刻"的库。容器版没有 systemd 那套的 cron，所以要自己调。

### 5.1 备份（可在线，不中断服务）

```bash
docker compose exec -T postgres \
  pg_dump -U mindmap -d mindmaps -Fc \
  > "backups/mindmaps-$(date +%Y%m%d-%H%M%S).dump"
```

三个选择都要说清：

- **`-Fc`（custom 格式），不是默认的纯 SQL。** 它自带压缩（所以**不要再 gzip**），而且能用 `pg_restore -t 表名` 只捞一张表、用 `pg_restore -l` 先看里面有什么。纯 SQL 那套（`-Fp`）体积大、且恢复只能整库重放。
- **`exec -T`（关掉 TTY）。** 不加它，`docker compose exec` 会分配一个伪终端，把 dump 的二进制流搅坏（`pg_dump: could not write to output file: Bad file descriptor`，或者得到一个无法 `pg_restore` 的文件）。
- **`-U mindmap -d mindmaps` 写全。** compose 里 `POSTGRES_USER`/`POSTGRES_DB` 是 `mindmap`/`mindmaps`；进容器默认身份是 `postgres` 超级用户，靠它也能导，但备份脚本写成显式用户名，将来换成受限账号时不用改结构。

**dump 直接写到宿主机，不要落在容器里。** 上面用 shell 重定向接住 stdout，所以产物在宿主机的 `backups/`。落在容器里的话，`docker compose down` 一删就没了——这不是"备份"。

`:ro` 那类"备份进程不该能改库"的保护在这里不需要：`pg_dump` 本身是只读的（它只 `BEGIN` 一个只读快照事务），不需要挂任何卷。

### 5.2 恢复（必须停服）

```bash
docker compose stop backend
docker compose exec -T postgres \
  pg_restore -U mindmap -d mindmaps --clean --if-exists \
  < "backups/mindmaps-20250101-120000.dump"
docker compose start backend
```

四条注意，每条都对应一种真实的坏结果：

- **服务必须先停。** backend 的连接池持有连接，`pg_restore --clean` 要 `DROP` 表，会撞上表锁——报 `database "mindmaps" is being accessed by other users`，或更糟：`--clean` 成功了一部分、恢复中止，库变成**一半新一半旧**。
- **`--clean` 会删表**，所以恢复之前先按 5.1 现备一份。恢复错了还有回退点。
- **`--if-exists` 不是可选项。** 没有它，`--clean` 在遇到"要删的表本来就不存在"（例如恢复到全新空库）时会报错并中止。
- 恢复后打一次 `/api/health` 确认起来了，再打一次 `/api/mindmaps` 看列表是不是预期的那份。

**如果库已经坏到 `pg_restore` 也进不去**（比如 `postgres` 容器起不来），那是另一条路：`down` 之后删掉 `mindmap-db` 卷、重新 `up` 让镜像 initdb 一个空库，再 `pg_restore`。代价是期间没有任何数据，所以这一步只有在前一条走不通时才用。

### 5.3 定时备份

容器版的定时任务由**宿主机**的 cron / systemd timer 调 5.1 那条命令，不要放进容器（容器重启就没了）。一个最小写法：

```bash
# /etc/cron.d/genai-mindmap-backup —— 每天 03:20
20 3 * * * root cd /opt/genai-mindmap && docker compose exec -T postgres \
  pg_dump -U mindmap -d mindmaps -Fc > "backups/mindmaps-$(date +\%Y\%m\%d).dump"
```

`cd` 那一截是必须的：`docker compose` 靠当前目录找 `docker-compose.yml`，cron 的工作目录不是仓库根。cron 里 `%` 要转义成 `\%`，否则被当成换行。

保留策略自己定（`find backups -name 'mindmaps-*.dump' -mtime +14 -delete` 这类）。**跟 systemd 那套的 `MINDMAP_BACKUP_KEEP_DAYS=14` 保持一致**，别一边留 14 天一边留 90 天。

### 5.4 只想要一份逻辑导出（给人看）

```bash
docker compose exec -T postgres pg_dump -U mindmap -d mindmaps --data-only --table=mindmaps \
  > mindmaps-only.sql
```

这条不是备份，是"把导图表捞出来看一眼"。`--data-only` 不带 DDL，`--table=mindmaps` 只那一张。要恢复到别的库就得自己先建表——所以**别拿它当备份**。

---

## 6. 在 systemd 部署与容器部署之间迁移

数据搬家用 `pg_dump` / `pg_restore`，**不再搬文件**；模型缓存不必搬（会重新下载，也可以拷过去省一次下载）。换库之前这一步是"搬 `mindmaps.db` 一个文件"，现在两边各有自己的 PG 实例（或共用一个），搬的是逻辑备份。

### 6.1 systemd → 容器

```bash
# 1. 停服务（这一步之后到第 5 步之间，用户看到的是 502）
sudo systemctl stop genai-mindmap

# 2. 从 systemd 那套的 PG 导出一份（服务已停，安全）
sudo -u postgres pg_dump -Fc mindmaps > /tmp/mindmaps.dump

# 3. 起容器（会 initdb 一个空库）
cp docker/.env.example .env && $EDITOR .env      # 填 key + 设 MINDMAP_PG_PASSWORD
docker compose up -d --build
docker compose ps                                 # 等 postgres 变 healthy

# 4. 把数据灌进去（先停 backend，别让连接池抢表锁）
docker compose stop backend
docker compose exec -T postgres pg_restore -U mindmap -d mindmaps --clean --if-exists \
  < /tmp/mindmaps.dump
docker compose start backend

# 5. 验证
curl -s http://127.0.0.1/api/health
curl -s http://127.0.0.1/api/mindmaps | head -c 300
```

**第 3 步的一个陷阱**：`postgres` 容器第一次启动时才 initdb，用 `.env` 里的 `MINDMAP_PG_PASSWORD` 建 `mindmap` 用户。所以第 4 步的 `pg_restore` 用的 `-U mindmap` 依赖第 3 步那个口令——**顺序不能反**，也不能先灌数据再改口令（改口令不会改库，见第 3.2.1 节第 3 条）。

### 6.2 容器 → systemd

反向同理：用 5.1 导出一份 dump，在 systemd 那台机器上 `sudo -u postgres pg_restore -d mindmaps --clean --if-exists < dump`，再 `systemctl start genai-mindmap`。注意 systemd 那套的 PG 用户/库名可能与容器版不同（默认都是 `mindmaps` 库），`pg_restore` 的目标库要按那边的 `MINDMAP_PG_DSN` 来。

**`--clean` 在这条路上更危险**：它删的是目标库里现有的表。容器 → systemd 的时候，如果那台机器上已经有一套在跑，先把它的库备份一份再恢复。

### 6.3 想两边都留着做对比

**不要。** 见第 1 节。要对比就在不同机器上跑，别在同一台机器上跑两份指向"同一个应用、不同数据"的东西——用户分不清哪个是真数据。

**尤其别让两边连同一个 PG 库。** 那不是"对比"，是两个应用进程同时写同一批表：一次 `pg_restore --clean` 会把另一边的数据连表一起删掉，而两边都觉得自己是唯一的主人。

---

## 7. 镜像分层与体积估算

### 7.1 后端镜像（`docker/backend.Dockerfile`）

| 层 | 内容 | 估算 |
| --- | --- | --- |
| 1 | `python:3.12-slim` 基础 | ~150MB |
| 2 | `apt-get install libgomp1` | ~2MB |
| 3 | `pip install` torch/torchvision（CPU 索引） | ~450–550MB |
| 4 | `pip install -r requirements.txt`（其余依赖） | ~700–900MB |
| 5 | `backend/app`（只有 .py） | ~2.3MB |
| 6 | LibreOffice（`WITH_OFFICE=1` 才有） | ~1.5GB |

**合计：约 1.3–1.7GB（不装 LibreOffice），2.8–3.2GB（装）。** 2.5GB 预算只有不装 LibreOffice 时才守得住。

这个表算的是**后端镜像**。迁库之后整机还要多两笔：`postgres:16` 官方镜像约 **150MB**（不构建，直接拉），以及库本身在 `mindmap-db` 卷里占的空间（活跃库只有 20 行真实数据，量级是 MB；见 `LIBRARY-MEASUREMENTS.md` 里"体积要按 `pg_total_relation_size` 量"的口径）。`requirements.txt` 新增的 `psycopg[binary]` + `psycopg-pool` 在本机实测约 **9MB**（`psycopg` 1.7MB + `psycopg_pool` 0.3MB + `psycopg_binary` 0.8MB + `psycopg_binary.libs` 6.2MB），对体积估算没有影响——`[binary]` 带的是预编译 libpq，省掉的是**目标机上装 libpq-dev 与 pg_config** 那一步，不是体积。

估算依据与不确定性（重要）：

- 本机 `backend/.venv/Lib/site-packages` 实测 **1403.9MB**，其中 `torch` **513.7MB**、`cv2` 112MB、`scipy` 108.5MB、`transformers` 103.4MB、`pandas` 67.9MB、`sympy` 65.4MB、`pymupdf` 52.9MB。但那是 **Windows + Python 3.13** 的展开体积，与容器里的 **Linux + Python 3.12** 不是同一批 wheel，只能当量级参考。
- **`requirements.txt` 里已经没有 `sqlite3` 这一项了**（它是标准库，本来也不占 pip 体积），新增的 psycopg 两个包实测约 9MB，见上。所以这张表在迁库前后几乎是同一组数字——**换库没有让镜像变大**，变的是"多一个容器、多一个卷、多一种备份方式"。
- `docs/DEPLOY.md` 第 5 节记的是"Python 依赖约 700MB"，与本机实测的 1.4GB 差一倍。两者可能都对：平台/版本不同、是否计入 torch 不同。**我没能力判定哪个数字更接近容器里的真实值**，所以上面写的是区间。
- **torch 的 CPU 轮子下载体积没量到**：对 `download.pytorch.org` 上的 `.whl` 发 `HEAD` 返回 403，发 `Range: bytes=0-0` 只回 1 字节、不给总长。所以只能从"本机装好的 CPU 版 torch 展开 513.7MB"反推。
- 关键结论不受影响：**torch 必须从 CPU 索引装**。PyPI 上的 linux `torch` 轮子会拖 `nvidia-cublas` / `nvidia-cudnn` / `nvidia-nccl` 等十来个包，解压后 2–3GB，镜像直接顶到 5GB 以上。docling 的 `standard` extra 硬性要求 `torch>=2.2.2`（`docling_slim-*.dist-info/METADATA`），躲不掉，只能换 CPU 轮子。

### 7.2 为什么分两步 pip

`docker/backend.Dockerfile` 里是：

```
pip install --index-url <torch cpu> torch torchvision
pip install --index-url <PIP_INDEX_URL> -r requirements.txt
```

顺序不能换。第一步把 torch 装成"已满足"，第二步解析 `requirements.txt` 时 pip 就不会再去 PyPI 抓带 CUDA 的那份。

并且紧跟一条构建期断言，万一解析被带偏就在**构建时**失败，而不是推出 5GB 镜像之后才发现：

```dockerfile
RUN python -c "import torch,sys; sys.exit('CUDA wheel leaked in: ' + torch.__version__) if torch.version.cuda is not None else print('torch', torch.__version__, 'cpu-only')"
```

（本机 venv 里的 `torch 2.15.0+cpu`、`torch.version.cuda is None`，说明 CPU 轮子是这条路子，但**没有**在 Linux/py3.12 上验证过。）

### 7.3 前端镜像（`docker/frontend.Dockerfile`）

| 层 | 内容 | 估算 |
| --- | --- | --- |
| 1 | `node:24-slim`（仅构建段，不进最终镜像） | — |
| 2 | `npm ci`（仅构建段） | — |
| 3 | `nginx:1.27-alpine` | ~20MB |
| 4 | `dist`（静态产物） | ~0.3–1MB |
| 5 | `docker/nginx.conf` | <10KB |

**最终镜像约 21–22MB。** 多阶段构建的意义就在这里：`node_modules`（本机实测 129MB）与 `node:24-slim` 全部留在构建段，不进产物。

### 7.4 构建上下文

`.dockerignore` 把三块最大的排掉：`backend/.venv`（**实测 1415MB**）、`frontend/node_modules`（**实测 129MB**）、`.git`。不排的话每次 `docker build` 都要把 1.5GB 打包发给 dockerd——表现是"以为卡死了"。

---

## 8. SSE（流式生成）是怎么保住的

这是整套配置里最容易在"看起来能跑"的表象下丢东西的一块。三条链路各有一道保险，**少任何一层都会退回"攒够一批才发"**。

### 8.1 三道保险

1. **应用层**：`backend/app/agent/router.py:51-58` 的 `_SSE_HEADERS` 带 `X-Accel-Buffering: no`。代码注释明确记着"漏掉这条的那条路会在反代后面攒够一批才发"。
2. **nginx 层**：`docker/nginx.conf` 的 `location /api/` 里，从 `deploy/nginx.conf` 第 53-55 行原样搬来三条：

   ```nginx
   proxy_buffering off;
   proxy_cache off;
   chunked_transfer_encoding on;
   ```

3. **超时层**：这里要防的不是"生成要跑多久"，而是"两次读之间隔多久"。`proxy_read_timeout` 的语义是**两次成功读之间的最大间隔**，而 SSE 链路上每 `HEARTBEAT_INTERVAL_SECONDS`（10 秒，`backend/app/agent/service.py`）必有一帧：真进度帧之外，模型调用的长静默由 `_await_quietly` 每 10 秒发一次 `heartbeat`。所以 900 秒是帧间隔的 90 倍余量——**它不随生成预算（`RunBudget.max_seconds`，现 1800 秒）变化**：

   ```nginx
   proxy_connect_timeout 10s;
   proxy_send_timeout    900s;
   proxy_read_timeout    900s;
   ```

   写成 60s 之类的"看起来合理"的值仍然危险——不是因为预算 1800 秒，而是因为**任何一次超过 60 秒没有帧的窗口**都会被掐。正常情况下帧不会断：工具执行期间由 `_run_tool` 的 `asyncio.wait_for(updates.get(), timeout=10)` 兜发心跳，模型调用期间由 `_await_quietly` 每 10 秒发一次，入库那段有开头的 SSE 注释帧。所以读超时设小了，实际含义是"我认定 60 秒没帧就等于卡死"。这是 `deploy/nginx.conf` 里已经踩过一次的坑，别重新踩。反过来，把读超时跟着预算一路调大也没有意义：帧真的断了就说明后端卡死了，让它 504 比让浏览器一直转圈更好。

### 8.2 还有两条容易被当成"无关"的

```nginx
proxy_http_version 1.1;
proxy_set_header Connection "";
```

不上 1.1、不把 `Connection` 置空，nginx 与上游之间就是每个请求一条新连接。长生成期间会攒一堆 `TIME_WAIT`。

### 8.3 gzip 里**不能**有 `text/event-stream`

`docker/nginx.conf` 的 `gzip_types` 是：

```nginx
gzip_types text/css application/javascript application/json image/svg+xml;
```

**刻意不写 `text/event-stream`，也刻意不写 `*`。** 把事件流交给 zlib，事件的边界会被压缩缓冲吃掉，前端 `ReadableStream` 那边看到的是一段一段的、延迟不可预测的数据。这点在 `docker/nginx.conf` 里有注释，删之前读一遍。

### 8.4 前端怎么消费

`frontend/src/api/client.ts:228` 的 `postSse()` 用 `fetch` + `ReadableStream` 自己切帧（不用 `EventSource`，因为要 POST 长文本）。它依赖的是"每帧以空行分隔、`data:` 行取内容"——这正是 nginx 不能重新分帧、不能缓冲的原因。

### 8.5 怎么确认真的在流

真机上用这条（浏览器 F12 也行，看 Network 里那条 `text/event-stream` 是逐步增长还是"突然全出现"）：

```bash
curl -N -s -X POST http://127.0.0.1/api/agent/runs/stream \
  -H 'Content-Type: application/json' \
  -d '{"topic":"测试"}' | head -c 500
```

`-N` 关掉 curl 自己的缓冲。如果 30 秒内一个字节都没出来，而日志显示后端已经在跑，就是某一道保险没生效。

---

## 9. 这些**没在真机上验证过**

**开发机上没有 `docker` 命令**（`docker --version` 与 `docker compose version` 都报"术语不被识别"），所以下面每一条都只做到了静态核验，没有一条跑起来过。

### 9.1 从来没执行过的（构建期）

| # | 未验证项 | 需要什么才能验证 | 可能怎么坏 |
| --- | --- | --- | --- |
| 1 | `docker compose build` 整体成功 | Linux 上装 Docker 20.10+ 的机器 | 某个 apt 包名在 slim 里不存在；`libgomp1` 之外的隐式系统库缺失 |
| 2 | torch CPU 轮子能装到 py3.12 / linux | 同上，且能访问 `download.pytorch.org` | 只确认了目录结构，没确认 cp312 wheel 的实际依赖解析 |
| 3 | 构建期断言 `torch.version.cuda is None` 通过 | 同上 | 若 pip 仍从 PyPI 解析，断言会失败（这是它的设计目的），要改的是 pip 参数不是断言 |
| 4 | `libgomp1` 足够让 `import torch` 成功 | 同上 | 可能还缺 `libstdc++` 之外的别的（如 `libc6` 的版本要求） |
| 5 | `pip install -r requirements.txt` 在 py3.12 全绿 | 同上 | 本机是 py3.13，某些包在 3.12 上的 wheel 覆盖没核过 |
| 6 | `npm ci` 在 node:24-slim 上成功 | 同上 | lockfile 里有 linux 原生包（rollup/oxide/lightningcss），核过存在但没装过 |
| 7 | `npm run build`（`vue-tsc --noEmit && vite build`）在容器里通过 | 同上 | 与 CI 同一命令，但容器内 node 版本/字体等环境不同 |
| 8 | 镜像体积真的在 1.3–1.7GB | 构建完 `docker images` | 第 7 节的估算偏差可能达 ±30%，见那里的说明 |
| 9 | `WITH_OFFICE=1` 时 LibreOffice 层能装 | 同上 | Debian slim 里 `libreoffice-writer` / `libreoffice-impress` 的包名与依赖没核过 |
| 10 | `.dockerignore` 在真 Docker 下的语义与 `fnmatch` 模拟一致 | 构建完看上下文大小 | 脚本用 `fnmatch` 近似，`**` 的递归匹配在 Docker 实现里细节不同 |

### 9.2 从来没执行过的（运行期）

| # | 未验证项 | 需要什么才能验证 | 可能怎么坏 |
| --- | --- | --- | --- |
| 11 | 三个容器能起来并通过 healthcheck（`postgres` 的 `pg_isready` 先绿，backend 才起） | `docker compose up -d` | postgres 首次 initdb 若因口令含特殊字符失败，backend 会一直等到 `start_period` 结束再被判 unhealthy |
| 12 | `backend:8000` 的 DNS 在 compose 内网能解析 | 同上 | 依赖 `resolver 127.0.0.11`（Docker 内嵌 DNS），只在 user-defined network 存在 |
| 13 | nginx 启动时**不**因为 backend 未就绪而退出 | 同上 | 用了 `set $backend` + `resolver` 就是为了这个，但没实测过"backend 先挂掉再重启"的时序 |
| 14 | nginx 在 backend 换 IP 后能自动跟上 | 重建 backend 容器 | `valid=10s` 的重新解析行为只在文档上确认过 |
| 15 | `/api/health` 过反代返回 200 | `curl http://<host>/api/health` | — |
| 16 | 前端页面能打开、能调通 `/api` | 浏览器打开 | 同源假设：`client.ts:45` 缺省 `BASE='/api'`，没设 `VITE_API_BASE` |
| 17 | **SSE 真的在流**（第 8 节） | 第 8.5 节的 `curl -N` | 这是最需要真机确认的一条，也是最容易"看起来正常其实不流"的一条 |
| 18 | 数据写在 `mindmap-db` 卷上、容器重建后数据还在 | 保存一张导图后 `docker compose restart postgres`（或 `down` 再 `up`，**不带 `-v`**），再看列表 | 这里**必须重启 postgres 才说明问题**：只重启 backend 只验证了"重连"，数据可能一直在某个容器内层文件里。`down -v` 会把卷删掉，那是"数据没了"而不是"没持久化"，两种现象要分清 |
| 19 | docling 首次解析 PDF 能下模型到 `/models` | 传一份 PDF | `HF_ENDPOINT=https://hf-mirror.com` 的可达性、卷权限 |
| 20 | `HOME=/data` 下的 `.cache/docling` 可写 | 同上 | `useradd --uid 10001` 的属主与卷属主是否匹配（"空 named volume 复制镜像目录属主"这个行为本身没实测，但写权限已由第 9.5 节的构建期探针兜住，属主对不上会在构建时就失败而不是运行时） |
| 21 | 健康检查脚本（`python -c` 里的 urllib）在容器里能跑 | `docker compose ps` 显示 healthy | 单行 `python -c` 的引号转义没在真 shell 里跑过 |
| 22 | `wget` 在 `nginx:1.27-alpine` 里存在（busybox 提供） | 同上 | alpine 的 busybox 通常带 wget，没实测 |
| 23 | `stop_grace_period: 90s` 下 `docker compose down` 会在 90 秒后 SIGKILL，**正在跑的生成会被打断** | 生成中途 `docker compose down` | 与 systemd 的 `TimeoutStopSec=90` 同量级，且是刻意的：发版不能等在跑的生成跑完（预算现 1800 秒）。打断后靠检查点续做——用户在「上次的结果」里点「补齐说明」。这一行早先写的是"能优雅收尾"，那是错的 |
| 24 | `pg_dump -Fc` 能在线跑出一份、`pg_restore --clean --if-exists` 能把它灌回去（第 5 节） | 起容器后照 5.1 / 5.2 各跑一次 | 这是整页**最该先验**的一条：备份流程只在文档里对，从来没执行过。离线库里没有大对象，风险主要是权限（`mindmap` 用户对表的属主关系）与 `--clean` 的删除顺序 |
| 25 | `postgres` 容器首次启动用 `.env` 里的口令 initdb 成功 | `docker compose up -d postgres` 后 `docker compose logs postgres` | 口令含 `@ : / # $` 之类字符时 `MINDMAP_PG_DSN` 的插值或 initdb 会出问题——这正是 `.env.example` 要求"只用十六进制"的原因，但没在真机上试过 |


### 9.3 明确"不知道"的

- **镜像体积的确切数字**：第 7 节是区间估算，不是测量值。
- **`HF_ENDPOINT=https://hf-mirror.com` 的端到端可用性**：`docs/DEPLOY.md` 第 11 节已经标注"这条没做过端到端验证"，容器版沿用同一个不确定性。
- **docling 在容器里首次初始化要多久**：代码注释说"十几秒"，但那是作者机器，容器里可能更久（`start_period: 40s` 是猜的）。
- **compose 的 `env_file` 与 `environment` 谁覆盖谁**：文档上 `environment` 优先，核验脚本也按这个假设写断言（`docker/.env.example` 里那三个容器路径是注释掉的，就是为了防误改），但没在真机上对比过 `docker compose config` 的实际输出。

### 9.4 已有的自动化核验（能替代一部分，替代不了上面这些）

`docker/verify-static.py`，第 10 节说明。

### 9.5 卷属主：从"未验证"改成"已用构建期断言兜住"

这是原本 9.2 里第 21 条，也是**第一次真跑最可能炸**的地方：非 root（`app` / uid 10001）写不进 `/data`，用户第一次保存导图就看到 `Permission denied`，很容易以为应用本身坏了。

现在不是靠"文档上说应该没问题"，而是在**构建期**就把它证掉。`docker/backend.Dockerfile` 里加了两道，顺序有意义：

```dockerfile
# 第一道（root 身份）：比数字属主。不能用 test -w —— 这一步还是 root，
# root 对任何路径的 -w 都为真，那是恒过的假断言。
RUN test "$(stat -c '%u:%g' /data)" = "10001:10001" \
 && test "$(stat -c '%u:%g' /models)" = "10001:10001" \
 && test "$(stat -c '%u:%g' /home/app)" = "10001:10001" \
 && echo "ownership ok: uid/gid 10001 for /data /models /home/app"

USER app

# 第二道（最终运行身份）：真的建文件再删掉，这才是"能不能写"。
RUN for d in /data /models; do \
      f="$d/.write-probe"; \
      if : > "$f"; then \
        rm -f "$f"; \
        echo "writable as uid $(stat -c '%u' "$d"): $d"; \
      else \
        echo "NOT WRITABLE as non-root user: $d" >&2; \
        exit 1; \
      fi; \
    done
```

为什么第二道不能省：第一道只证明**属主位**对，证明不了以 app 身份真能写（还有 ACL、目录位、挂载方式这些变量）。反过来第一道也不能省——它能在 `USER app` 之前就报错，定位信息更直接。两道都在**构建期**失败，日志里直接写着哪条路径不可写，而不是等用户第一次保存导图时给一个 `Permission denied`。

这也解释了为什么 `chown` 必须放在 `USER app` **之前**：放到之后等于没做（那时已经不是 root，chown 会失败）。核验脚本对这一点的断言见第 10.1 节第 13 类。

**仍然没被兜住的**：named volume 首次创建时"把镜像里该目录的属主复制进卷"这个 Docker 行为本身，只在官方文档上确认过、没在本机实测。如果它不成立（比如用了 `volume` 的 `driver_opts` 或预置了非空卷），容器里仍会是 `Permission denied`——但那时构建期两道断言都是绿的，说明问题出在卷而不是镜像。这道探针现在只覆盖 `mindmap-data`（`/data`）与 `mindmap-models`（`/models`）两个卷；**`mindmap-db` 不适用**，它是 postgres 容器的，属主由 PG 镜像自己管（`postgres` 用户 999），跟这里的 10001 无关——手工造过 `mindmap-data` 卷内容的话，记得 `chown 10001:10001`。

---

## 10. 静态核验做了什么

```bash
backend/.venv/Scripts/python.exe docker/verify-static.py .    # Windows
.venv/bin/python docker/verify-static.py .                    # Linux
```

只读脚本，**119 项断言，当前全部 PASS**（需要 PyYAML；`backend/.venv` 里有）。它检查的是"文件之间是否自洽"，不检查"能不能跑"。

迁移到 PostgreSQL 之前是 96 项。多出来的 23 项几乎都集中在"后端与库的契约"上：compose 里多了一个 `postgres` service、多了一条 `MINDMAP_PG_DSN`、`.env.example` 多了一个必填口令、CI 多了一个 postgres service。**这些恰好是重构最容易漏的地方**——漏了不会报错，只会在真机上表现为"容器反复重启，日志里一条看着像代码 bug 的连接错误"。

### 10.1 覆盖的十六类

1. **compose 结构与服务拓扑**：YAML 可解析；service 恰为 `{backend, frontend, postgres}`；`name: genai-mindmap`；顶层卷恰为 `{mindmap-data, mindmap-models, mindmap-db}`；`backend.depends_on.postgres.condition == service_healthy`（库没起来就不许起后端）
2. **路径存在性**：service 的 `build.context` 与 `build.dockerfile` 解析成绝对路径后真实存在
3. **卷与路径一致性**：`mindmap-data→/data`、`mindmap-models→/models`、**`mindmap-db→/var/lib/postgresql/data`**（PG 的数据目录路径，写错就是"数据在容器内层，一删就没"）；`MINDMAP_PG_DSN` 里必须出现 `@postgres:`（连 `localhost` 是最常见也最隐蔽的错：容器里的 `127.0.0.1` 是容器自己）；`HF_HOME`/`DOCLING_CACHE_DIR` 以 `/models` 开头；`HOME` 以 `/data` 开头；`MINDMAP_EXTERNAL_PROXY` 存在
4. **端口与依赖**：backend **无** `ports`、只有 `expose: ["8000"]`；frontend `ports == ["${WEB_PORT:-80}:80"]`；`depends_on.backend.condition == service_healthy`；backend 有 healthcheck 且命中 `/api/health`；`env_file == [".env"]`
5. **变量闭环**：compose 里（剥掉注释行后）每个 `${VAR}` 在 `docker/.env.example` 都有赋值行；compose 的每个 build arg 在 `backend.Dockerfile` 有 `ARG` 声明
6. **nginx upstream 名 == compose service 名**：从 nginx 里抽出所有 `http://host:port`，主机名集合必须恰为 `{"backend"}`
7. **SSE 八条逐条正则**：`proxy_buffering off` / `proxy_cache off` / `chunked_transfer_encoding on` / `proxy_http_version 1.1` / `proxy_set_header Connection ""` / `proxy_connect_timeout 10s` / `proxy_send_timeout 900s` / `proxy_read_timeout 900s`
8. **超时与体积语义**：`proxy_read_timeout`(900) > 6 × `HEARTBEAT_INTERVAL_SECONDS`(10)（**按帧间隔计，不按生成预算计**；预算与读超时早已不同量级：预算 1800 秒，读超时 900 秒，这不是缺陷）；`client_max_body_size` ≥ 21m（音频上限 20MB）；`gzip_types` 不含 `event-stream` 也不含 `*`
9. **nginx 语法级自检**：花括号净深度 0；每条指令以 `;`/`{`/`}` 结尾；有 `resolver 127.0.0.11`；`set $backend` 在 server 块内；剥注释后无 `__DOMAIN__`/`__WEB_ROOT__`/`__CERT_DIR__`/`__LISTEN6__` 残留
10. **Dockerfile 关键事实**：后端 `python:3.12-slim`、`--workers 1`、`USER app`、HEALTHCHECK 打 `/api/health`；前端构建段 `node:24`、有 `AS build`、运行段 `FROM nginx:`、有 HEALTHCHECK
11. **COPY 源路径**：从两个 Dockerfile 抽出所有非 `--from=` 的 COPY 源（10 个），逐个 `exists()`；再用 `fnmatch` 模拟 `.dockerignore`，断言这 10 个**都没被排除**，同时断言 `.git`/`backend/.venv`/`frontend/node_modules`/`frontend/dist`/`.env`/`backend/data` 六个**都**会命中排除规则（这一条是**规则匹配**，不是存在性检查——`frontend/dist` 与 `backend/data` 在当前工作树里本来就不存在，它们在构建机与生产机上才会出现，规则必须能挡住它们）
12. **依赖清单与脚本名**：`requirements.txt` 包名可解析（`docling, fastapi, httpx, pydantic, pymupdf, python-multipart, uvicorn[standard], xlrd, neo4j, psycopg[binary], psycopg-pool`）；含 `docling` 与 `uvicorn[standard]`（`psycopg` 两个包只断言"包名能解析"，没断言"必须有"——它们是运行期硬依赖，缺了测试会红，不需要静态脚本再兜一遍）；`package.json` 的 `build` 恰为 `vue-tsc --noEmit && vite build`；`package-lock.json` 的 `lockfileVersion == 3`（`npm ci` 的前提）；`tsconfig.json` 的 `include` 含 `"tests"`
13. **卷属主与非 root 可写（第 9.5 节那条）**：`mkdir -p /data /models` 存在；`chown -R app:app /data /models` 存在；**`chown` 的行号在 `USER app` 之前**（放到之后等于没做）；`chown` 与 `mkdir` 在同一条 `RUN` 内（相差 ≤3 行，防止中间插层把属主盖掉）；root 侧断言用的是 `stat` 比数字属主而不是 `test -w`（后者对 root 恒真，是假断言）；`USER app` **之后**还有一条真探针；探针覆盖 `/data` 与 `/models` 两个落点；探针失败会 `exit 1`
14. **`postgres` 服务六条（迁库新增）**：`image == postgres:16`（版本要与服务端一致——`pg_dump` 跟随大版本，客户端比服务端新会直接拒绝）；`POSTGRES_PASSWORD` 与 backend DSN 里的口令**同源**（同一个 `${MINDMAP_PG_PASSWORD}`，不是两份各写一遍——那是最容易漂移的一种配置）；`mindmap-db` 挂到 `/var/lib/postgresql/data`（PGDATA）；**不发布 5432**（`ports` 为空，只在 compose 内网可达）；healthcheck 用 `pg_isready`（端口通不等于能查询：initdb 还没跑完、或库在恢复时端口是通的）；`depends_on` 用 `condition: service_healthy`
15. **`.env.example` 模板四条约（迁库新增）**：`MINDMAP_DB_PATH` **没有赋值行**（已废除；留着会让人以为还能用文件库）；有 `MINDMAP_PG_PASSWORD` 且**值为空**（模板要提交进 git，不能带真口令）；`MINDMAP_PG_DSN` 在模板里是**注释掉的**（连外部库是例外路径，一条 enabled 的 DSN 会盖掉 compose 里指向 `postgres` 服务名的正确配置）
16. **CI 也连真库三条（迁库新增）**：`ci.yml` 的 backend job 有 `postgres:16` service；pytest 步骤显式带 `MINDMAP_TEST_DSN`（缺这一行 conftest 会落到本机默认端口 55433，CI 上就是整套后端测试全红）；checkout 带 `fetch-depth: 0`（`git show` 读旧版本的回归测试靠它，见 `AGENTS.md`）

这一类的断言做过**变异测试**：在临时副本里分别把 `chown` 删掉、把 `chown` 挪到 `USER app` 之后、把 `stat` 断言退化成 `test -w`、把探针改成只测 `/tmp`、把探针里的 `exit 1` 去掉——5 种改法全部被脚本抓到（exit 1），还原后回到 119/0。没做变异测试的断言不能算"在检查"，只能算"恰好没报错"。

**新加的那三类没有像第 13 类那样做完整的变异测试**，如实写在这里：加进来时验证过两处"故意改坏会被抓到"——把 DSN 的 `@postgres:` 改成 `@localhost:`（`FAIL 1`，报错直指那条 DSN 的值）、把 `.env.example` 里 `MINDMAP_PG_PASSWORD=` 改成注释行（`FAIL 3`，分别报"每个 `${VAR}` 在模板里都有赋值行""有赋值行""留空"）。但 `postgres` 服务剩下的四条与 CI 那三条还没逐条改坏验证过。按本页自己的标准，这只能算"在检查一部分"——补齐做法同上：临时副本里逐条改坏、确认 exit 1、还原后回到 119/0。

### 10.2 核验过程中真的抓到过两个问题

写下来是因为"核验脚本第一次跑就全绿"通常说明它没在检查什么：

1. **真问题**：`HF_ENDPOINT` 只出现在 compose 里，模板 `docker/.env.example` 缺赋值行 → 用户按模板配了 `.env`，`${HF_ENDPOINT:-...}` 仍走缺省，但模板里看不到这个键，改起来要找文档。已在模板补一节"模型下载源"。
2. **脚本自己的误报**：正则扫到了注释里的 `${VAR}` 和注释里提到的 `__DOMAIN__`。两处断言改成先剥注释行/剥注释后判断。修的是脚本，不是配置。

### 10.3 三处"为了少赌一层解析"而改的写法

本机没有 docker 验证不了 Dockerfile 解析器的边缘行为，所以：

- **`CMD` 写成单行 JSON 数组**（不用 `\` 续行）。exec 形式要靠解析器先吃掉续行再交 JSON 解析。
- **构建期断言写成单行 `python -c`**（不用 `\` 续行 + 多语句）。
- **`ENV` 的注释放在 `ENV` 之前**，不夹在续行中间。续行里的注释行是否能被正确剥离，各版本解析器的行为不一定一致。

---

## 11. 已知限制

1. **不内置 TLS。** `docker/nginx.conf` 只 `listen 80`。要 HTTPS 就在宿主机再架一层 nginx/caddy 反代到这个容器的 80，或者上 `deploy/nginx-tls.conf` 那套（但那套是给 systemd 部署的）。参考 `deploy/nginx-tls.conf` 的 443 配法，注意里面同样有三条 SSE 配置与 900s 超时，别在搬的时候漏掉。
2. **nginx 那层没有任何 Basic Auth，鉴权全在应用层。** 账号体系上线后，两份 nginx 配置（`deploy/nginx.conf`、`docker/nginx.conf`）都不再有 `auth_basic`；拦截匿名请求的是 `backend/app/auth/gate.py`，发身份与游客 cookie 的是 `IdentityMiddleware`。全站一个口令那层已经**弃用**——它不只是重复，还与"游客每天 3 次免费"直接冲突：口令过不去就拿不到游客 cookie，新访客连试用的机会都没有。

   > **⚠️ 但容器版仍然要求"有身份"，而身份是应用自己发的不代表外面有一道锁。** 唯一被应用放行的匿名路径是 `/api/health` 和 `/api/auth/*`（注册、登录、领游客 cookie）。**直接把它暴露到公网，任何人扫到 `IP:WEB_PORT` 都能立刻领一个游客身份、用掉那 3 次免费额度，并注册账号**——如果你不打算接受陌生人注册，就在外层（宿主机 nginx/caddy、安全组、SSO）限住来源。这是"门禁"与"谁能进来"两件事的分工：应用负责前者，部署方负责后者。
   >
   > **`/api/settings/model` 那个面为什么现在安全了。** 它曾经完全没有鉴权：`PUT` 能把模型地址改指向任意主机、`POST /api/settings/model/test` 会带着**已存的那把密钥**去请求那个地址（`backend/app/settings/probe.py`）。两步就能把密钥明文送到攻击者手上，不需要知道它是什么。现在有三道，且都在代码层（不依赖部署方记得配什么）：门禁要求有身份；`probe._refuse()` 拒本机/内网/保留地址/云元数据（要连本机模型得显式设 `MINDMAP_SETTINGS_ALLOW_PRIVATE=1`）；`store.resolve()` 把密钥与它保存时的**主机名**绑定，换个地址就不再交出密钥（`api_key_source: "unbound"`，界面提示重贴一次）。

3. **`--workers 1` 写死，但理由换过了一次。** 换 PostgreSQL 之前，理由是**写者冲突**：状态全在一个 SQLite 文件里（`backend/app/mindmaps/repository.py::connect()`），多 worker 就是多个写者，会偶发 `database is locked` 的 500。迁到 PG 之后这条**不成立了**——多 worker 只是多个普通客户端，写冲突由服务端行锁处理，`database is locked` 不会再出现。但 `--workers 1` 仍然保留，理由变成**内存**：2C2G 上多一个进程就要多一份完整的 Python 运行时（docling 首次解析后常驻的那几百 MB 会跟着复制一份），而且**每个 worker 有自己的连接池**——`max_size=8` 乘上 worker 数，PG 侧的常驻与峰值内存一起翻倍（空闲单连接 1–2 MB，PG 实测空闲 45 MB / 池预热 67 MB / 8 线程写压测 168 MB）。所以这是"这台机器扛不住第二个进程"，不是"库不许多写"。**不要为了"多用几个核"去改这个值**；真要横向扩，先扩内存、再把池的 `max_size` 从 8 降到 4，见 `PG-MIGRATION-PLAN.md` 的风险表。
4. **后端不发布 8000。** 只有前端容器在宿主机上开 `WEB_PORT`。要本机调后端就临时加 `ports: ["127.0.0.1:8000:8000"]`，调完删掉——线上形态是"后端只听内网"。
5. **`MINDMAP_EXTERNAL_PROXY` 必须显式设。** 代码缺省值是 `http://127.0.0.1:7897`（作者本机的 Clash，`backend/app/knowledge/external.py:196`）。**容器里的 `127.0.0.1` 是容器自己**，写它等于"哪都连不上"，而且失败是 5–6 秒后静默返回空。compose 已默认成 `none`（直连）。要走宿主机代理：

   - Docker Desktop（macOS/Windows）：`http://host.docker.internal:7897` 直接可用
   - Linux：`host.docker.internal` 默认不解析，要给 backend 加

     ```yaml
     extra_hosts:
       - "host.docker.internal:host-gateway"
     ```

     而且代理得监听 `0.0.0.0` 而不是 `127.0.0.1`，否则容器照样连不上。

6. **Neo4j 也不在容器里。** 图存储是可选的独立服务（`MINDMAP_GRAPH_*`），compose 没带。要连宿主机的就写 `host.docker.internal:7687`，别写 `127.0.0.1:7687`。
7. **`MINDMAP_NLI_ENABLED=0` 默认关。** 引用语义复核要 ONNX 模型，模型文件得自己放进卷（`MINDMAP_NLI_MODEL_DIR`）。容器里没预置，开了但目录是空的会失败。
8. **卷不会自动备份。** 第 5 节给的是命令，不是定时任务。宿主机的 cron/timer 要自己配。注意那条命令备份的是**库**（`pg_dump`），不是 `mindmap-db` 卷——卷里是 PG 数据目录，拷它没有意义，见 5 节开头。
9. **`docker compose down -v` 会连卷一起删。** 手滑一次数据全没——现在三个卷里 `mindmap-db` 装的是整个库（导图、知识库、账号、设置全在里面），删掉就是全部归零，`mindmap-data` / `mindmap-models` 只是缓存。备份没做起来之前，别在生产机上敲这条。
10. **镜像里几乎没有调试工具。** 基础镜像是 `python:3.12-slim`（Debian 系），没有装 `curl`、没有 `git`（`requirements.txt` 也不提供这些东西）。**但"想查库"这件事现在比以前简单了**：库不在这个镜像里，而是在 `postgres` 容器里，那边有完整的 `psql` 与 `pg_dump`/`pg_restore`。要查数据：

    ```bash
    docker compose exec postgres psql -U mindmap -d mindmaps -c '\dt'
    ```

    这比换库之前"用 `docker compose exec backend python -c "import sqlite3..."` 拼一行 Python" 好得多——那时能用的只有 `sqlite3` 标准库，因为镜像里连 `sqlite3` 命令行都没有。**别再按旧心智去 backend 容器里找库文件**：那里没有库，也没有 `psql`。基础镜像里还有哪些 shell 与工具（`bash` / `sh` / `wget` 之类），**我没有在真机上看过**，见第 9.2 节。这是刻意的：镜像里每多一个工具，攻击面和体积都涨一点。
11. **不接管 CD。** `.github/workflows/` 没有构建镜像的步骤，`/usr/local/sbin/genai-cd-apply` 也不会去动容器。见第 12 节。

---

## 12. 已拍板的取舍，与落地前必须做的一件事

下面四条都**已经由用户拍板**，这里是记录结论与理由，不需要再讨论。

1. **CI 里不加 `docker build`。** 保持 `.github/workflows/ci.yml` 现状不动。理由：本机没有 Docker，在没人见过它成功构建之前就塞进 CI，只会得到"天天红、而且红不指向真实回归"——CI 的红灯必须意味着"代码坏了"，不能意味着"我们的验证环境不完整"。等第 12.1 节那条做完再考虑加。
2. **LibreOffice 默认不装**（`WITH_OFFICE=0`）。装了能支持 `.doc`/`.ppt`，代价是镜像从约 1.5GB 变成约 3GB，顶穿 2.5GB 预算。不装时这两种格式返回 422，提示里说明需要 LibreOffice。要开就在 `.env` 里设 `WITH_OFFICE=1` 重新构建。成本分析见 `docs/LEGACY-FORMAT-COST.md`。
3. **不用 nginx 的 Basic Auth**，鉴权由应用层账号体系承担（见第 11 节第 2 条）。这**不代表容器版"开箱即可安全上公网"**：门禁只拦匿名，游客身份谁都能领、注册是开放的，能不能进来要由部署方在外层限。
4. **不引入 registry**，只用本地 tag（`genai-mindmap-backend:local` / `genai-mindmap-frontend:local`）。单机部署不需要分发；要做多机再说。

### 12.1 落地前必须做：找一台有 Docker 的机器跑通一次

**这一条是硬的，不能跳。** 本仓库里这一套文件（`docker-compose.yml` + `docker/` 下 5 个 + `.dockerignore`，加上本页）**从头到尾没有在任何一台机器上构建或运行过**（原因见第 9 节）。在有人真的跑通之前，它们的地位是"一份经过静态核验的草案"，不是"能用的部署方案"。

按这个顺序做，每一步失败都能定位到具体一层：

```bash
# 1. 构建（先只建后端，它最可能炸）
docker compose build backend

# 2. 起库 + 后端，确认 postgres 先 healthy、/api/health 再通
#    后端不发布端口（第 11 节第 4 条），所以从容器内部打，不绕宿主机
docker compose up -d postgres
docker compose ps postgres          # 等 STATUS 里出现 healthy
docker compose up -d backend
docker compose exec backend python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/api/health').read())"

# 3. 确认两个卷真的可写（第 9.5 节那两道构建期断言之外的最后一道）
#    用 python 而不是 sh/touch：这个镜像里到底有没有 sh 我没验证过，
#    但 python 一定在。注意这里只有 backend 的两个卷：mindmap-db 归 postgres
#    容器，属主由 PG 镜像管，不要拿这个探针去测它
docker compose exec backend python -c "import pathlib
for d in ('/data','/models'):
    p = pathlib.Path(d)/'probe.txt'; p.write_text('x'); p.unlink(); print('RW ok', d)"

# 3b. 确认后端真的连上了库（不是连到了别的地方）
#     查的是 PG 自己的 pg_stat_activity，不是后端日志
docker compose exec postgres psql -U mindmap -d mindmaps -c "select count(*) from schema_migrations;"

# 4. 起前端，浏览器打开，保存一张导图，**重启 postgres** 确认数据还在
#    只重启 backend 只验证了"重连"，验不出"数据写没写进卷"
docker compose up -d frontend
docker compose restart postgres
docker compose ps                   # 等 postgres 再 healthy
docker compose exec postgres psql -U mindmap -d mindmaps -c "select count(*) from mindmaps;"

# 4b. 备份 + 恢复各跑一次（第 5 节，整页最该先验的一条）
docker compose exec -T postgres pg_dump -U mindmap -d mindmaps -Fc > /tmp/smoke.dump
docker compose stop backend
docker compose exec -T postgres pg_restore -U mindmap -d mindmaps --clean --if-exists < /tmp/smoke.dump
docker compose start backend

# 5. 确认 SSE 真的在流（第 8.5 节）
curl -N -s -X POST http://127.0.0.1/api/agent/runs/stream -H 'Content-Type: application/json' -d '{"topic":"测试"}' | head -c 500

# 6. 传一份 PDF，确认 docling 模型下到 /models
docker compose exec backend python -c "import pathlib
for d in ('/models','/data/.cache'): print(d, pathlib.Path(d).exists() and sum(1 for _ in pathlib.Path(d).rglob('*')))"
```

第 4 步为什么强调"重启 postgres"：换库之前"重启 backend 后数据还在"是个有效证据（SQLite 文件在卷上，进程重启不影响文件）。现在数据在**另一个进程**里，重启 backend 只说明它重连成功了。要证"持久化"，要么重启 postgres，要么 `down` 再 `up`（**不带 `-v`**，带了是删卷）。

全部通过之后，再回头决定要不要把 `docker compose build`（不 push）加进 CI——那时候加，红灯才有意义。

**如果第 1 步就失败**：把构建日志末尾贴出来，最可能的三处是 `libgomp1` 之外缺系统库、torch CPU 索引的 URL 拼错、`npm ci` 的 linux 原生包没命中。第 9.1 节的表就是按"可能怎么坏"排的，对照着看。

---

## 13. 排障速查

| 现象 | 先看这里 |
| --- | --- |
| `compose up` 报 `.env` 不存在 | 第 3.2 节，模板要拷到**仓库根** |
| `compose up` 报 `required variable MINDMAP_PG_PASSWORD is missing a value` | 第 3.2.1 节，`.env` 里缺这个必填项（升级用户最容易撞上） |
| backend 一直 `unhealthy` | `docker compose logs backend`；`docker compose exec backend python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/api/health').read())"`。**先看 `docker compose ps` 里 postgres 是不是 healthy**——backend 的 healthcheck 要过得了就意味着它连上了库 |
| backend 反复重启，日志里连接错误 | 三种成因：① `MINDMAP_PG_DSN` 指到了 `localhost` 而不是 `postgres`（容器里的 `127.0.0.1` 是容器自己，见第 3.2.2 节）② `.env` 里的 `MINDMAP_PG_PASSWORD` 与库里的实际口令不一致（改过 `.env` 但没改库，见 3.2.1 第 3 条）③ postgres 起不来（`docker compose logs postgres`，多半是口令含特殊字符或数据目录属主不对） |
| `password authentication failed for user "mindmap"` | 改过 `.env` 的 `MINDMAP_PG_PASSWORD` 但没改库里的口令。这个变量只在**首次 initdb** 生效，改它不会改库（3.2.1 第 3 条）：要么 `ALTER USER`，要么 `down -v` 重来（数据会没） |
| 前端打开是 502 | backend 没起来或 healthcheck 没过；nginx 用了变量 + `resolver`，backend 挂着时它不退出、只返 502——这是设计 |
| 首字迟迟不出来（SSE） | 第 8 节，尤其 8.5 的 `curl -N` |
| 长材料 504 | `proxy_read_timeout` 被改小了（必须远大于 10 秒帧间隔，见 8.1 第 3 条）；若用的是默认值，看后端日志是否还在打印——在打印说明是"真的 900 秒没发过一帧"，那已经是卡死 |
| 保存导图 500 | 三种成因：① 库连不上（同上面"反复重启"那行，先看 `docker compose logs backend` 末尾是不是 psycopg 的异常）② `/data` 卷属主不对——只影响上传中转与 docling 缓存，导图本身不再写 `/data`（第 9.5 节）③ 迁移时旧库没导入（`select count(*) from mindmaps` 是 0，见 6.1） |
| `database is locked` | **这条现在不该出现了。** 那是 SQLite 单写者的症状；迁到 PG 后写冲突由服务端行锁处理。如果再看到它，说明有人在跑旧的 SQLite 代码（没同步镜像）或环境里还留着旧容器 |
| `could not extend file ... No space left on device` | 磁盘满。PG 报这个比 SQLite 的 `database or disk is full` 更具体——它说是哪个文件扩不动。先清 `backups/` 里的旧 dump，再看 `mindmap-db` 卷大小（第 5 节） |
| `database "mindmaps" is being accessed by other users` | 有人在服务跑着的时候 `pg_restore --clean`。恢复必须先停 backend，见 5.2 第一条 |
| 首次解析 PDF 很慢/失败 | 模型在下载（`/models`）；看 `HF_ENDPOINT` 与出网；日志里会有 HuggingFace 的下载记录 |
| 外部检索总是返回空 | `MINDMAP_EXTERNAL_PROXY` 指向了容器自己的 127.0.0.1（第 11 节第 5 条） |
| `.doc`/`.ppt` 报 422 | 没装 LibreOffice，见第 12 节第 2 条 |
| 构建卡在 "transferring context" | `.dockerignore` 没生效，1.5GB 的 `.venv` 被发了（第 7.4 节） |

---

## 附：和 `docs/DEPLOY.md` 的分工

- `docs/DEPLOY.md`：systemd + nginx + CD 那套（**生产在用**），含资源测算、格式依赖表、出网受限、自动化部署
- 本页：容器那套（**新增、未验证**），含构建、compose 结构、SSE 三道保险、静态核验、未验证清单

两边共用的背景事实（格式依赖、资源占用、模型缓存）都写在 `DEPLOY.md`，本页只引用不复述，避免两份文档各自漂移。
