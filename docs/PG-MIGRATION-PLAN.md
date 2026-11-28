# SQLite → PostgreSQL 迁移：智能体分工与改动界限

> 状态：**待用户确认**。确认后才开子智能体动工，本文件是那一轮的唯一作业依据。
> 分支 `feat/postgres-migration`，base `97bbfeb`。容量评估与实测数据见对话记录（2C2G 扛得住：
> PG 常驻 45MB / 池预热 67MB / 8 线程写压测后 168MB）。

## 0. 一句话方案

**不逐个改 190 个 `conn.execute` 调用点**，而是新增一层穿 sqlite3 外衣的 psycopg 连接包装
（`app/db/`，下称 shim）。shim 吃掉占位符翻译、事务语义、行工厂、连接归还四件事，把改动
集中在 **21 个连接入口 + 十几个语义点 + 测试基座 + 部署/文档**。

已经写原型验证过（不是纸面设计）：263 处 `?` 零改动、40 处 `with closing(conn), conn:` 零改动、
79 处 `closing(...)` 零改动、157 处 `row["k"]` 零改动、8 处 `conn.close()` 零改动、
17 处 `BEGIN IMMEDIATE`/`COMMIT`/`ROLLBACK` 零改动、`SAVEPOINT` 全链零改动。

## 1. 已定的技术决策（及其理由）

| # | 决策 | 理由 | 与早期结论的关系 |
|---|---|---|---|
| D1 | 用 shim 包 psycopg，不逐处改 SQL | 手改 189 处 = 189 个引入 bug 的机会，且 `?`→`%s` 靠人眼在 15 个文件里做不可靠 | 新决策 |
| D2 | 连接池 `psycopg_pool`，**不是**可选项 | 现有"每次操作新开连接 + 连接内重放 13 张表 DDL"在 PG 上是 2.98ms/次；一次生成仅 settings 读就要 5-27 次 | 与第一轮一致（硬性前置） |
| D3 | **不引入 alembic / SQLAlchemy** | 两者需要 SQLAlchemy（本项目零 ORM），只为 DDL 版本管理引入 ~30MB 依赖不值得。DDL 全部幂等（`CREATE TABLE IF NOT EXISTS` + `ADD COLUMN IF NOT EXISTS`），一个 `schema_migrations` 表 + 有序步骤列表足够（~150 行） | **推翻第一轮"alembic 是前置条件"**：那条的前提是"DDL 不能每次连接重放"，用一个启动期执行的幂等 schema 初始化就能满足，不需要 alembic |
| D4 | 时间戳保持 `TEXT`，格式用服务器端 `nowstamp()` 生成 | 实测 `to_char(statement_timestamp() at time zone 'utc', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"')` 与 SQLite `strftime('%Y-%m-%dT%H:%M:%fZ','now')` **逐字节同格式**；且与 `timestamptz` 版插入开销相同（4.147 vs 4.171ms）。保住所有"按字典序比时间"的现有逻辑。**时钟取 `statement_timestamp()` 而不是 `now()`**：后者是事务时间戳、一个事务内恒定，而 SQLite 的 `now` 是每条语句重新求值（实测同事务隔 0.4 秒写两行，`now()` 版时间戳逐字节相等）；也不能用 `clock_timestamp()`，它在同一条语句的多行之间就会变，而 SQLite 的 `now` 在一条语句内是常量 | 评审后修正 |
| D5 | 用 STABLE SQL 函数 `nowstamp(offset_seconds DEFAULT 0)` 替代 `_NOW` 常量 | psycopg 只在 `params is not None` 时解析 `%`，而 `to_char` 格式串里全是 `%`。做成函数后 **SQL 文本里不含任何 `%`**，彻底绕开 `%%` 逃逸陷阱。实测函数被内联，无额外开销（3.879 vs 3.938ms） | 新决策 |
| D6 | `edit_events.id` / `library_chunks.id` 用 `bigserial`，**不用 UUID** | `contracts/edits.py:132 id: int` → `openapi.json` → `schema.d.ts` → `types.ts` → `client.ts` 整链不动。用 UUID 要重导契约，多一轮风险 | 新决策（沿用早前判断） |
| D7 | 向量继续存 `bytea`（原 BLOB），**不上 pgvector** | `vector_cache.py` 是纯 Python 计算，只把表当 KV 存取；引入 pgvector 是另一件事 | 沿用现状 |
| D8 | 时间/注解都不做"顺手重构" | 迁移期的 diff 只允许"为 PG 而改"，其余一律不动（`git diff` 可审） | 约束 |
| D9 | 测试隔离：同一库 + 每测试 `DELETE FROM` 全表（+ 需要时重置序列） | **方案定稿时**的实测（13 张表、未含 commit）：`DELETE FROM` 0.140ms、`TRUNCATE` 181ms、`DROP+CREATE` 208ms、建库 53ms → `TRUNCATE` 一档否决。`synchronous_commit=off` 对 TRUNCATE 无效（180 vs 182ms），说明开销在 TRUNCATE 自身。**落地后在终态 17 张表上重测（含 commit，中位数）**：`DELETE`+重置序列 **17.4ms**、`TRUNCATE` **332ms**、`CREATE SCHEMA`+建表 14.0ms、`CREATE DATABASE ... TEMPLATE` 127ms —— 结论不变（`DELETE` 仍是最便宜的可维护档），但绝对值是定稿时的 124 倍：2775 条用例 × 17.4ms ≈ **48 秒，占全量 150 秒的三分之一**。`CREATE SCHEMA` 只快 3ms 却要把 `search_path` 塞进 DSN，21 处连接入口共用一个池，谁都可能写个直连绕过隔离，所以不选 | 新决策（**绝对值在落地后修正**） |
| D10 | SQLite 支持**一次性导入后删除**，不保留双轨 | `docs/PLAN.md:90` 已定"不再保留 SQLite 双轨"；且活跃库只有 20 行真实数据，"双写观察一周"（`ARCHITECTURE_ADDENDUM.md:62-67`）不成立 | 沿用 PLAN，**推翻 ADDENDUM** |

### 1.1 顺带被这一决策简化掉的东西（**可以删，不要留**）

现有**三处**"就地升级老 SQLite 文件"的代码，存在理由全都只是这一个：

| 位置 | 行 | 干的事 |
|---|---|---|
| `repository._migrate_legacy_prefs` | :90-134（调用于 :276） | `user_prefs` 加 `owner` 时"改名→建新表→搬行→删老表" |
| `settings.store._migrate_legacy` + `_copy_legacy_rows` | :278-332 / :224-276（调用于 :353） | `model_settings` 同上，且按老表**实际列集**动态拼 `SELECT` |
| `prefs.style.ensure_schema` 的 legacy 分支 | `style.py:246-285`，配 `_LEGACY_TABLE`/`_LEGACY_CREATE`（:125/:127） | `style_profile` 同上；`_LEGACY_CREATE` 现在**已无人引用**（死代码） |

改成一次性导入之后：它们的判据（"有 `X` 列、没有 `owner` 列"）在新库里永远不成立；
最贵的部分（按老表实际列集搬行）**搬进 M6 的导入脚本**；app 侧三处整段删除，
连带 `_LEGACY_TABLE` / `_LEGACY_MARKER_COLUMN` / `_PREF_LEGACY_TABLE` / `_LEGACY_CREATE` 常量。
→ `backend/tests/test_schema_migrations.py`（265 行 / 13 条用例）的**前提消失**，
改为一条"`init_schema()` 幂等"的用例。

**但另外两个 `_adopt_legacy` 要留**：`prefs/service.py:334`（`UPDATE user_prefs SET owner = ? WHERE owner = ''`）
与 `prefs/style.py:300`（同形，改 `style_profile`）。它们不是 schema 迁移，是"老库无主行归部署方"的一次性
业务认领，SQL 也不含 SQLite 专有语法。判据 `owner = ''` 在新库里自然不成立 → **零成本空转，保留**（D8：不做顺带删除）。

这是本次迁移里唯一一处"删代码比改代码多"的地方，必须显式执行，否则会留下一堆永不触发的分支。

### 1.2 四个需要用户拍板的点（前三个是与文档冲突，第四个是新发现的硬约束）

1. **`ARCHITECTURE_ADDENDUM.md:62-67`「双写观察一周」不可行**（D10）：单文件库 vs 服务端，
   且活跃库只有 20 行。建议改为「**停服 → 跑导入脚本 → 校验行数/内容 → 切换 → 旧库文件原样保留
   在 `/var/lib/genai-mindmap/mindmaps.db.pre-pg` 作回滚点**」。`docs/` 由 M7 按这个口径改。
2. **`docs/PLAN.md:90`「单测用事务回滚隔离」做不到**（D9）：现有 `conftest.py:156` 是
   "每测试一个全新库文件"，而 PG 上每测试开事务回滚会与 `BEGIN IMMEDIATE` 语义、
   与 `ensure_schema` 的 DDL 冲突。改为「**同一库 + 每测试 DELETE 全表**」。
3. **第一轮说的"alembic 是硬性前置"降级为不引入**（D3）。
4. **改文档会撞评估语料的指纹闸门**（新发现，已实测）：`backend/scripts/eval_cases_manual.py`
   的 28 条手写 case 把 `docs/PLAN.md`（7 条）、`docs/ARCHITECTURE.md`（6 条）、
   `docs/AGENT_PLAN.md`（7 条）、`docs/RAG-DECISIONS.md`（8 条）的指定章节正文算了指纹，
   登记在 `MANUAL_SOURCE_HASHES`；改了标注章节的正文，`test_manual_source_hashes_match_the_real_materials`
   当场红（装载即 `SystemExit`，不静默跳过）。而 M7 需要改的行**几乎全在标注章节里**：

   | 要改的行 | 所在章节 | 是否在标注区 |
   |---|---|---|
   | `ARCHITECTURE.md:22/23` | 目标与当前边界 | **是** |
   | `ARCHITECTURE.md:35` | 模块边界 | **是** |
   | `ARCHITECTURE.md:64` | 技术选择及来源 | **是** |
   | `ARCHITECTURE.md:72` | 安全与部署边界 | **是**（且 `:74` 是 focus 锚点 `**这条与录音功能冲突**`） |
   | `PLAN.md:15/16` | 0 一页摘要 | **是** |
   | `PLAN.md:90/101` | 3 技术架构定稿 | **是** |
   | `PLAN.md:227` | 11 决策记录（Q26–Q36） | **是** |
   | `PLAN.md:201` | 10 配置项 | 否，安全 |

   即 11 行里 10 行要动指纹。`ARCHITECTURE.md` 更是**整份正文都在标注区内**（5 章 3-76 行全覆盖）。
   现状已验证：`test_retrieval_eval_manual.py` + `test_retrieval_eval_crossdoc.py` 当前
   **38 passed**，指纹重算与登记值完全一致。

   **但 case 的定位方式决定了实际风险很小**（已逐条核对）：28 条 case 只用两种锚，
   ① `sections` = 章节标题**前缀**（要求恰好命中 1 个标题），② `focus` = 一句**逐字原文**（可空）。
   涉及这两份文件的 13 条里，**11 条 `focus` 为空**，只有 2 条有锚句：
   `#06 ARCHITECTURE.md` 的 `**这条与录音功能冲突**`（:74，安全与部署边界）、
   `#14 PLAN.md` 的 `**指标口径**`（:127，5 验收门与指标口径）。
   而 M7 要改的 11 行**没有一行是这两个锚句所在行**，也**不改任何章节标题**。
   → 只要守住"不改标题、不删这两句"，改正文只会让 4 条指纹变化，**13 条 case 的语义不会失效**。

   两条路，需要选一条：
   - **A（推荐）**：M7 照改，改完**重算并覆写 4 条指纹**即可，外加一条硬约束：不改章节标题、
     不删 `ARCHITECTURE.md:74` 与 `PLAN.md:127` 这两句锚句。13 条 case 的 `sections`/`focus`
     **不需要重标**（前提成立时它们仍精确命中）。代价：一次指纹重算 + lead 独立复算。
   - **B（更省）**：M7 **只改标注区以外的行**（当前只有 `PLAN.md:201`），
     其余"SQLite 单文件"的表述留在标注章节里不动，另开一份 `docs/PG-CHANGES.md` 记录差异，
     等评估语料下次整体重标时一并更新。代价：文档里会短期留着与实现不符的说法。

   我倾向 **A**：成本只是"重算 4 条指纹 + 守住两条锚句"，换来文档与实现一致。
   若选 A，指纹重算是 M7 的**独立验收项**（不许并进别的提交），lead 在最终验收时独立复算一遍。

## 2. 改动面总账（供估时与划界）

| 项 | 数量 | 位置 |
|---|---|---|
| 存储模块 | 15 个 / 7,317 行 | `app/mindmaps` `app/agent` `app/auth` `app/generation` `app/knowledge` `app/memory` `app/prefs` `app/settings` |
| `conn.execute` 调用点 | 190 | 同上 |
| SQL `?` 占位符 | 263 | 同上（shim 吃掉，**零改动**） |
| 连接入口 | 21 处 `repository.connect(...)` 调用（10 处是各模块的 `_connect()` 包装，11 处散在 edits/summarize/external_cache/vector_cache） | 改名到 shim |
| `closing(...)` | 79 处 / 75 行 | **零改动** |
| 双重 `with ... , conn:` | 40 | **零改动** |
| `conn.close()` | 8 | **零改动**（shim 的 close = putconn） |
| 事务控制 SQL | **17**（`BEGIN IMMEDIATE` 7 处真执行 + 5 处只是注释；SQL 形式的 `COMMIT` 5 / `ROLLBACK` 5）+ `PRAGMA foreign_keys = ON` 1 | **零改动**（shim 按首词分派）；`PRAGMA foreign_keys` 那 1 行删掉（PG 外键默认强制） |
| `SAVEPOINT` | 4（library.py:240/265/283/289，其中 3 处在代码里、1 处在注释） | **零改动** |
| `row["k"]` | 157 | **零改动**（`dict_row`） |
| 位置索引 `row[N]` | 供参考 18 处，实际需改 **8 处** | edits.py:84/199/214/216、settings/store.py:205/221、external_cache.py、vector_cache.py |
| `_NOW` 常量 | 10 个模块 | 换 `nowstamp()` |
| 带偏移的 `strftime(...,'now',?)` | 5 | 换 `nowstamp(?)` |
| `sqlite3.X` 引用 | 共 96 处（Connection 39 / Error 39 / Row 11 / OperationalError 3 / IntegrityError 2 …） | 各客主自改 |
| `PRAGMA table_info` | **6 处 / 4 文件**（repository:61/80/139、settings/store:205/221、prefs/style:261） | 换 `information_schema.columns` |
| `sqlite_master` | **3 处**（repository:144、settings/store:210、prefs/style:284） | 换 `to_regclass` |
| legacy 就地迁移 | **3 处**（见 §1.1），连带 4 个常量 | **整段删除**，逻辑搬进 M6 导入脚本 |
| `CREATE TABLE IF NOT EXISTS` | 38（含索引） | 收进 schema 初始化，启动执行一次 |
| 测试文件 | 111 个 / 2,773 条 collect | 32 个直接碰 sqlite3 |
| 脚本 | 12 个 `backend/scripts/*.py` 触库 | 大多改一处入口即可 |
| 部署 | `deploy/` 8 个 / `docker/` 6 个 | backup/install/service/verify-static |
| 文档 | `docs/` 12 份 + 根 4 份 | M7 统一改；其中 4 份文档的标注章节有指纹闸门（§1.2 第 4 条） |
| 评估语料指纹 | `MANUAL_SOURCE_HASHES` 4 条 / `eval_cases_manual.py` 28 条 case（13 条涉这两份文档，其中仅 2 条有 focus 锚句） | 若选 A：重算覆写 4 条指纹 + 不改标题/不删锚句 |

## 3. 智能体分工与改动界限

**硬规则（每个客主都必须遵守）**

1. 只改自己名下的文件。**不属于自己的文件只读不写**；需要别人改 → 报告给 lead，不自己动手。
2. 每个客主负责把自己文件里的 `import sqlite3` / `sqlite3.Error` / `sqlite3.Row` 注解一并换掉
   （这是同一文件的内部一致性，不是额外任务）。禁止为了"顺手"去改别人的文件。
3. **契约层冻结**：`backend/openapi.json`、`frontend/src/api/**`、`app/contracts/**` 一律不许写。
   若发现"必须改契约才能迁"→ **立刻停手报告**（这是设计失败信号，不是任务）。
4. 不做与 PG 无关的重构（D8）。`git diff` 里出现纯格式/纯改名改动 = 打回。
5. 每完成一个模块，自己跑对应测试文件并在报告里给出命令 + 结果；不许只跑全量然后说"绿了"。
6. 任何"我发现 XX 也能顺手改"→ 写进报告，不自己做。

### M0 · 地基（**阻塞 M1-M5，必须先完成并验收**）

| 项 | 内容 |
|---|---|
| 新建 | `backend/app/db/__init__.py`、`backend/app/db/pool.py`、`backend/app/db/conn.py`（shim）、`backend/app/db/schema.py`、`backend/app/db/errors.py` |
| 改（自有） | `backend/app/mindmaps/repository.py`、`backend/app/main.py`、`backend/requirements.txt`、`backend/tests/conftest.py` |
| 改（**唯一允许的跨文件例外**） | `backend/app/settings/store.py` 只删 `_migrate_legacy`（:278-332）+ `_copy_legacy_rows`（:224-276）+ `_LEGACY_TABLE`/`_LEGACY_MARKER_COLUMN` 常量 + :353 的调用；`backend/app/prefs/style.py` 只删 `ensure_schema` 的 legacy 分支（:246-285 里 `if not columns or ...` 之后那半段）+ `_LEGACY_TABLE`/`_LEGACY_CREATE` 常量。**除此之外这两文件一行都不许动**，其余改动归 M4 / M5 |
| 删（自有） | `repository._migrate_legacy_prefs`、`_ensure_columns`（换 `ADD COLUMN IF NOT EXISTS`）、`_drop_columns`、`_columns_of` / `_table_exists`（换 `information_schema` / `to_regclass`）；`backend/tests/test_schema_migrations.py` 改为幂等性用例 |

**为什么 legacy 删除必须归 M0 而不是 M4/M5**：这三处的存在条件是"老 SQLite 文件就地升级"，
而 `init_schema()` 一次性建终态表之后这个条件永不成立。若留给 M4/M5 并行做，M0 建表时没有
人删它们 → 两个客主会同时改 `settings/store.py` 的表结构假设。**M0 先删干净并跑通，M4/M5 从
删干净的基线开始**。搬行逻辑（`_copy_legacy_rows` 的按实际列集动态 `SELECT`）由 M0 抄进
`docs/PG-MIGRATION-PLAN.md` 的 M6 交接说明或直接以注释形式留在导入脚本骨架里。

**shim 契约（M1-M5 全部依赖，签名不许改）**

```python
# app/db/conn.py
class Conn:
    execute(sql, params=None) -> Cursor      # ?→%s；每条语句包 SAVEPOINT，失败 ROLLBACK TO 并原样抛出
    executemany(sql, rows) -> Cursor         # 走 conn.cursor().executemany
    cursor() -> Cursor
    commit() / rollback()                    # 透传
    __enter__() -> self                      # 不回滚不关闭
    __exit__() -> 提交或回滚，然后**绝不 close**（关键：psycopg 的 `with conn:` 会关连接）
    close()                                  # rollback 后 putconn 归还池，不销毁
```

- 事务控制分派：SQL 首词 `BEGIN*` → no-op（PG 隐式事务）；`COMMIT` → `_raw.commit()`；
  `ROLLBACK`（不含 `TO`）→ `_raw.rollback()`。
- `row_factory = psycopg.rows.dict_row` 在 `ConnectionPool(configure=...)` 里统一设。
- **必须在 `execute` 里包 SAVEPOINT**：psycopg 语句出错会让整个事务进 aborted 状态
  （`InFailedSqlTransaction`），而现有代码 10+ 处依赖"吞掉异常继续用同一连接"。
  实测代价：每语句 +0.12-0.17ms，6 语句周期 +1.0ms（+24%）—— 可接受。
  实测确认：包了 SAVEPOINT 之后 `SELECT 1/0` 失败，同一连接上的下一条语句照常返回
  （事务**没有**进 aborted 态），从池里再取一条也正常。

**池配置**：`min_size=1, max_size=8, timeout=10, max_idle=300, max_lifetime=3600, open=False`，
在 lifespan 里 `pool.open()`。`configure` 设 `row_factory`，`reset` 用默认（回滚）。
`CHECKPOINT_LOCK_TIMEOUT=2.0` 那条要能透过 `connect(timeout=...)` 传到 `getconn(timeout=)`。

**lifespan**：启动 `init_schema()`（它内部经 `get_pool()` 顺带把池打开），shutdown
`close_pool()`。`init_schema()` 顺序执行幂等 DDL（含 `CREATE OR REPLACE FUNCTION
nowstamp(...)`），并写 `schema_migrations` 行（键 `version`，一个键值表而不是一整张迁移
历史——本项目没有版本递进，只有"建到终态"，记一版是为了让"这个库是哪天哪一版建的"能
直接查）。**不再**在每次连接时重放 DDL。

**`db_path()` → `dsn()`**：环境变量 `MINDMAP_PG_DSN` 一条串搞定，缺省
`postgresql:///mindmaps?host=/var/run/postgresql`（**走 Unix socket，同机部署的默认形态**）。
不拆成 `MINDMAP_PG_HOST/PORT/DB/USER/PASSWORD` 五个变量：拆开就要处理"配了三个漏了两个"
的半个配置，而 psycopg 的 DSN 本来就能表达全部组合，多一层拼装只多一层出错的地方。
**两处派生读点必须同步改**：`auth/store.py`（cookie 签名密钥）、
`settings/store.py`（缓存键）—— 前者会导致**所有现有会话失效**，写进发布说明。

**conftest.py**：删 `monkeypatch.setenv("MINDMAP_DB_PATH", ...)`，改为读 `MINDMAP_TEST_DSN`
（缺省 `postgresql://postgres@127.0.0.1:55433/mindmap_test`）；autouse fixture 每测试
`DELETE FROM` 全表 + `ALTER SEQUENCE ... RESTART WITH 1`；缺 DSN 或连不上 → **明确报错，
不许 skip**（AGENTS.md：skip 把"环境配错"伪装成"这条不适用"）。

验收：`backend/tests/test_auth.py::test_the_schema_comes_up_with_the_repository` 改写后绿；
全量 collect 仍 2,773；`grep -rn "sqlite3" backend/app` 只余注释。

### M1 · 导图编辑 + 资料库 + 缓存

| | |
|---|---|
| 改 | `app/mindmaps/edits.py`、`app/knowledge/library.py`、`app/knowledge/vector_cache.py`、`app/knowledge/external_cache.py`、`app/knowledge/shelf.py`、`app/knowledge/remember.py`、`app/knowledge/graph_search.py` |
| 测试 | `test_edit_events.py`、`test_library.py`、`test_library_wiring.py`、`test_external_cache.py`、`test_knowledge_shelf.py`、`test_knowledge_remember.py` |
| 语义点 | ① `edits.py:237-239` `date('now','-1 day')` → `nowstamp(-86400)` 派生日期串；② `edits.py:84/199/214/216` 位置索引改 dict；③ `library.py:112` `AUTOINCREMENT`→`bigserial`（DDL 在 M0 的 schema 里）；④ `library.py:147` 删 `PRAGMA foreign_keys`（PG 默认强制）；⑤ `library.py:262` `sqlite3.IntegrityError`→`psycopg.errors.UniqueViolation`；⑥ `library.py:278` `conn.executemany` 走 shim；⑦ `library.py:477` 带偏移 strftime→`nowstamp(?)`；⑧ `vector_cache.py:148` / `external_cache.py:180/190/227/232` 同上 |
| 不许碰 | `app/knowledge/service.py`（BM25 纯 Python）、`app/knowledge/graph_store.py`（Neo4j）、`app/ingestion/**` |

### M2 · 生成运行态（并发核心，最难）

| | |
|---|---|
| 改 | `app/agent/runs.py` |
| 测试 | `test_agent_runs.py`、`test_agent_stream.py` |
| 语义点 | ① `claim()`（:325-355）压成单条 `INSERT ... ON CONFLICT (run_id) DO UPDATE ... WHERE runs.status IN ('failed','cancelled') AND runs.owner = EXCLUDED.owner RETURNING *, (xmax = 0) AS inserted`（**已实测四种分支语义全对**）；② `sweep_abandoned()`（:501-515）→ `updated_at < nowstamp(-N)` + `run_id = ANY(%s)`；③ `_connect()`（:205-217）删 `isolation_level = None`；④ `finish()`（:398-423）保留 `COALESCE` 形状，只换 `_NOW`；⑤ 5 处 `BEGIN IMMEDIATE`/`COMMIT`/`ROLLBACK` 走 shim（零改动，但要验证）；⑥ `runs.py:241` 等 `sqlite3.Row` 注解换 `dict_row` 类型 |
| 风险 | `_ACTIVE: set[str]` 是进程内登记（runs.py:88-105），`RUN_ABANDONED_SECONDS` 单 worker 假设仍然成立（uvicorn 仍 1 worker）→ **不趁机改成 PG 级租约**，留待后续 |
| 不许碰 | `app/agent/tools.py`、`app/agent/service.py` |

### M3 · 身份 + 限流 + 配额

| | |
|---|---|
| 改 | `app/auth/store.py`、`app/auth/throttle.py`、`app/generation/quota.py` |
| 测试 | `test_auth.py`、`test_guest_signature.py`、`test_throttle.py`、`test_quota.py`、`test_byok.py` |
| 语义点 | ① `auth/store.py:89/121/381/401` `COLLATE NOCASE` → `CREATE UNIQUE INDEX ... ON users (lower(username))` + `WHERE lower(username) = lower(%s)`（已实测：大小写不同也抛 `UniqueViolation`，查询正确命中）；② `auth/store.py:190` cookie 密钥改读 `dsn()`（**会全站会话失效**）；③ `quota.py:156-179` 压成单条 `INSERT ... ON CONFLICT (bucket,day) DO UPDATE SET used = quota.used + 1 ... WHERE quota.used < %(ceiling)s RETURNING used`（已实测 ceiling=2 时返回 `[True,True,False,False]`）；④ `quota.py`/`throttle.py` 8 处 `conn.close()` **零改动**（shim close=putconn），但要确认没被改成 `del`；⑤ 带偏移 strftime 3 处 → `nowstamp(?)` |
| 不许碰 | `app/auth/identity.py`、`app/auth/gate.py` |

### M4 · 设置 + 记忆

| | |
|---|---|
| 改 | `app/settings/store.py`、`app/memory/store.py`、`app/memory/summarize.py` |
| 测试 | `test_settings_panel.py`、`test_prefs_wiring.py`、`test_memory_store.py`、`test_memory_summary.py`、`test_memory_inject.py`、`test_memory_recall.py`、`test_memory_router.py` |
| 语义点 | ① `settings/store.py:199-221` `_columns_of`/`_has_table`/`_not_null_columns` → `information_schema.columns` / `to_regclass`（`row[1]`/`row[3]` 位置索引一并改）；② **`_migrate_legacy`（:278-332）与 `_copy_legacy_rows`（:224-276）由 M0 删**（见 §1.1 与 M0 的接缝说明），M4 只在自己的验收里确认删干净、`_LEGACY_TABLE`/`_LEGACY_MARKER_COLUMN` 常量已移走；③ `settings/store.py:466` 缓存键改 `dsn()`；④ `_NOW` 的 3 个（settings:139、memory/store:143、summarize:106）；⑤ `row.keys()` 依赖 1 处（settings:568，runs 那 4 处归 M2）；⑥ `memory/store.py:618` / `summarize.py:364/453/508` 是 Python 侧格式化，**只需确认与 SQL 侧同格式，不改** |
| 不许碰 | `app/settings/router.py`、`app/prefs/**`（那是 M5）、`app/memory/*.py` 以外的 |

**注意 M4 与 M5 的接缝**：`settings/store.py` 里的 `owner_of()` 被 `prefs.*` 大量调用，
**M4 只改存储层，不改 `owner_of` 的签名/语义**；`prefs/*` 需要的改动全在 M5 自己文件里。

### M5 · 偏好（最大的三个文件）

| | |
|---|---|
| 改 | `app/prefs/profile.py`（1,198 行）、`app/prefs/style.py`（872）、`app/prefs/service.py`（665） |
| 测试 | `test_prefs.py`、`test_prefs_profile.py`、`test_prefs_profile_isolation.py`、`test_prefs_style.py`、`test_isolation.py`、`test_private_isolation.py` |
| 语义点 | ① 3 个 `_NOW`（profile:221、service:104、style:111）；② `prefs/style.py:261` `PRAGMA table_info` + `:284` `sqlite_master` → `information_schema` / `to_regclass`；③ **`style.py:246-285` 的 legacy 分支（改名→搬行→删老表）与 `_LEGACY_TABLE`/`_LEGACY_CREATE` 常量由 M0 删**（见 §1.1），`_adopt_legacy()`（:300）**保留**；④ `profile.py:469/492/505` 的"三张表串行取样本、任一张失败只 warning"**是 shim 的 SAVEPOINT 语义的验证靶子**——必须实测"第一张失败后第二三张仍能查"；⑤ `prefs/service.py:453` / `style.py:627` 的 `ON CONFLICT ... excluded.` **PG 原生支持，不改**；⑥ `style.py:273` 的 `INSERT OR IGNORE ... SELECT`（在 legacy 分支里，随 M0 一起消失——**若 M0 决定保留则改 `ON CONFLICT DO NOTHING`**） |
| 不许碰 | `app/prefs/router.py`、`app/settings/**` |

### M6 · 数据导入 + 部署 + CI

| | |
|---|---|
| 新建 | `backend/scripts/migrate_sqlite_to_pg.py`（一次性导入；含老表列集探测与 `_copy_legacy_rows` 的搬迁逻辑） |
| 改 | `deploy/backup.sh`（`sqlite3 .backup` → `pg_dump -Fc`）、`deploy/install.sh`（装 postgres 客户端、写 `MINDMAP_PG_DSN`、cron）、`deploy/genai-mindmap.service`（`ReadWritePaths` 与 1-worker 的理由注释重写）、`deploy/setup-python.sh`、`.github/workflows/ci.yml`（加 `services: postgres:16` + `MINDMAP_TEST_DSN`）、`docker-compose.yml` + `docker/backend.Dockerfile` + `docker/.env.example`、`docker/verify-static.py`（:87-88、:366-367 两条静态断言前提消失） |
| 脚本 | `backend/scripts/measure_profile_ab.py:69`（唯一脚本级 `sqlite3.connect`）、`measure_library.py:72-73`（`_db_bytes()` 用 `st_size` → `pg_total_relation_size`）、其余 10 个只改入口 |
| 阻塞 | 依赖 M0（新 schema）与 M1-M5（rep 层已换）→ **最后开工** |
| 不许碰 | `backend/app/**`（一行都不许改）、`docs/**`（那是 M7） |

**导入脚本的验收口径**：对真实库 `backend/data/mindmaps.db` 跑一遍，逐表比对
行数（mindmaps 4 / runs 4 / user_prefs 1 / edit_events 6 / model_settings 2 / sessions 1 /
users 1，其余 0 行），并抽 1 张导图比对 `content` 逐字节相等。
脚本必须**幂等**（重跑不重复插入）且支持 `--dry-run`。

### M7 · 文档（可与任何阶段并行）

| | |
|---|---|
| 改 | `docs/ARCHITECTURE.md`（:22/23/35/64/72）、`docs/ARCHITECTURE_ADDENDUM.md`（:14/18/62-67，双写段按 D10 重写）、`docs/PLAN.md`（:15/16/90/101/201/227，:101 内存预算表按实测改，:90 验收口径按 D9 改，:227 Q35 决策标注"已执行"）、`docs/PROJECT-STATUS.md`、`docs/HANDOFF.md`、`docs/DEPLOY.md`（:104/107/109/197/369/419-422 + 恢复步骤）、`docs/DOCKER.md`（:516 单写者理由重写）、`docs/GRAPH-TWO-PASS.md:183`、`docs/LIBRARY-MEASUREMENTS.md`、`README.md:123`、`NEXT-PLAN.md`、`TEAM-GUIDE.md:54`、`START_HERE.md:69`、`AGENTS.md`（"2C2G 单机 + SQLite 单文件"与"技术栈-检索：SQLite"两条要改） |
| 不许碰 | `backend/**`、`deploy/**` |
| 约束 | **这四份文档的标注章节正文受指纹保护**：`docs/PLAN.md`（7 条 case）、`docs/ARCHITECTURE.md`（6 条）、`docs/AGENT_PLAN.md`（7 条）、`docs/RAG-DECISIONS.md`（8 条）。改到标注章节正文，`MANUAL_SOURCE_HASHES` 当场失配、`test_manual_source_hashes_match_the_real_materials` 红。**M7 要改的 11 行里有 10 行落在标注区内**（详见 §1.2 第 4 条），必须先取得用户对 A/B 方案的选择 |
| 若选 A | 改完文档后：① 重算指纹 `cd backend; python -c "import sys; sys.path[:0]=['scripts','.']; import evaluate_retrieval as ev; print(ev.annotated_hashes(ev.load_materials(list(ev.DEFAULT_MATERIALS), [])))"`，② 覆写 `backend/scripts/eval_cases_manual.py` 的 `MANUAL_SOURCE_HASHES`（4 条），③ **硬约束：不改任何章节标题、不删 `ARCHITECTURE.md:74` 的 `**这条与录音功能冲突**` 与 `PLAN.md:127` 的 `**指标口径**`**（这两句是 `#06`/`#14` 两条 case 的 focus 锚句），④ 跑 `tests\test_retrieval_eval_manual.py tests\test_retrieval_eval_crossdoc.py` 应回到 38 passed。**这四步是独立验收项，不许并入别的提交** |
| 若选 B | 标注章节内的表述**一行不动**，差异写进新建的 `docs/PG-CHANGES.md`，列入后续待办；M7 只改标注区外的行（当前仅 `PLAN.md:201`）与未受保护的文档 |

### 3.1 并行排程

```
第 1 轮  M0（单独，串行）                      ← 地基，其他人不动手
第 2 轮  M1 ┃ M2 ┃ M3 ┃ M4 ┃ M5 ┃ M7          ← 6 个并行，文件集完全不相交
第 3 轮  M6（导入脚本 + 部署 + CI）            ← 等 M1-M5 落地
第 4 轮  lead 统一验收（见第 5 节）
```

M7 也可以放到第 3 轮，因为它只碰 `docs/`；放第 2 轮是为了让文档改动与实现并行。

## 4. 明确不在范围内（谁都不许碰）

| 不碰 | 原因 |
|---|---|
| `app/knowledge/graph_store.py`、`graph_search.py` 的 Neo4j 逻辑、`test_graph_neo4j*.py` | 它本来就是 Neo4j，不是 SQLite；`graph_search.py` 只需改异常类 |
| `app/knowledge/service.py`（2,762 行）、`app/agent/tools.py`（6,700+ 行） | 纯 Python 检索/工具层，无 SQL |
| `frontend/**` | 契约零改动（D6）。只在验收时跑 `npm run gen:api` + `npm test` + `npm run type-check` 确认没漂 |
| `app/contracts/**`、`backend/openapi.json` | 冻结（规则 3） |
| 向量检索/BM25 算法 | 迁移不做算法改动 |
| `MINDMAP_SOURCE_MODE` 之类的历史开关 | 已删除，别复活 |

## 5. 统一验收标准（lead 在最后逐条跑）

1. `grep -rn "import sqlite3" backend/app` → **0 命中**；`grep -rn "MINDMAP_DB_PATH" backend/app` → **0 命中**。
2. `grep -rn "sqlite_master\|PRAGMA " backend/app` → 0 命中。
3. 全量 pytest：**2,773 collected**，失败集合 ⊆ {`test_openapi_snapshot.py::test_snapshot_is_up_to_date`}
   （本机 pydantic/fastapi 版本差异导致的**已知红**，不是迁移引入的，不要去"修"）。
4. 全量耗时 **≤ 600 秒**（基线 472.53s；预期 +8.7s 来自测试重置，+~5s 来自 savepoint）。
5. `python scripts/export_openapi.py && git diff --exit-code backend/openapi.json` → 无 diff。
6. 真实库导入脚本跑通，逐表行数与 `content` 逐字节比对通过。
7. `deploy/backup.sh` 在 PG 上跑通并**实际恢复一次**；`docker/verify-static.py` 的静态断言改完后仍通过。
8. 启一次真实服务：`/api/health` 200、登录/游客 cookie 签发、生成一条导图、编辑保存、资料上传检索。
9. `git diff --stat` 逐文件过一遍，确认**没有与 PG 无关的改动**（D8）。
10. `AGENTS.md` 与新文档里没有残留"SQLite 单文件"的说法。
11. 若 §1.2 第 4 条选 A：`tests\test_retrieval_eval_manual.py tests\test_retrieval_eval_crossdoc.py`
    **38 passed**，且 `MANUAL_SOURCE_HASHES` 4 条已按新正文重算覆写；
    `ARCHITECTURE.md:74` 与 `PLAN.md:127` 两句 focus 锚句仍在（lead 独立重算指纹，不采信客主自报）。
12. `grep -rn "MINDMAP_DB_PATH" backend/tests` → 0 命中（统一夹具已替代"每测试一个临时库文件"的旧形态）；
    `grep -rn "MINDMAP_TEST_DSN" backend/tests/conftest.py` → 有命中。
13. `test_schema_migrations.py` 已改为 `init_schema()` 幂等用例（跑两次结果一致、表/列/函数 `nowstamp` 齐全），
    不再是"老库就地迁移"那 13 条。

## 6. 风险与预置应对

| 风险 | 触发信号 | 应对 |
|---|---|---|
| SAVEPOINT 包每条语句拖慢写路径 | 全量耗时 > 600s | 先查是不是测试重置的锅；仍超则只在"已知会失败并吞异常的调用点"（profile.py:469/492/505、library.py:284/290 等 ~10 处）手动包 SAVEPOINT，其余裸执行 |
| `InFailedSqlTransaction` 漏网 | 某测试报 `current transaction is aborted` | 该调用点补 SAVEPOINT；**不许**用"整体重连"糊过去 |
| 序列未重置导致 `edit_events.id` 断言红 | `test_edit_events.py:99 assert rows[1]["id"] == 2` | conftest 的重置序列覆盖全部 bigserial 表 |
| 契约被迫变更 | `export_openapi.py` 出现 diff | **停手**，回到 D6 用 `bigserial` |
| 本机无 PG 导致整套测试跑不了 | 开发/CI 报连不上 | CI 加 `services: postgres:16`；本地文档写清一条 `pg_ctl` 启动命令；**不许 skip** |
| 会话全失效被认为"迁移出 bug" | 上线后所有人重新登录 | 写进发布说明（`db_path()`→`dsn()` 改了 cookie 密钥输入） |
| 2C2G 上池 + savepoint 叠加超预算 | 生产 RSS 峰值 > 250MB | 池 `max_size` 从 8 降到 4；实测池 idle 单连接 ~1-2MB，有空间 |
| 改文档撞评估语料指纹 | `test_manual_source_hashes_match_the_real_materials` 报 `SystemExit`/指纹失配 | 按 §1.2 第 4 条 A/B 执行；**不许**把测试改成 `skip`（AGENTS.md 明令：`skip` 把"环境配错"伪装成"不适用"）。选 A 时若报错，先查是不是手滑改了章节标题或删了锚句——指纹失配只是症状，case 定位失效才是真问题 |

## 7. 我这一轮已经做完、下一轮不必重做的事

- 环境：WSL PG 16.16 已装好在 `~/pgenv`、`~/pgdata`，端口 55433，trust 认证；
  Windows 侧 `D:\AnaConda\python.exe` 已装 psycopg 3.3.9 / psycopg_pool 3.3.3 / neo4j 6.4.1 /
  pymupdf / xlrd / docling-core。**全量 collect 成功 2,773 条。**
- 基线：**21 failed / 2,712 passed / 40 skipped / 472.53s**，其中 20 条失败已确认全是缺包（已补装转绿），
  唯一真实红是 `test_openapi_snapshot`（本机依赖版本差）。
- shim 原型、`claim()` 单语句、`quota.consume()` 单语句、`nowstamp()` 函数、
  `lower(username)` 唯一索引、SAVEPOINT 开销、测试隔离耗时 —— **全部已实测**，见第 1 节。
- 临时文件已清理，worktree `git status` 干净。

跑测试的标准命令（每次都要带 `PYTHONUTF8`，否则 `iniconfig` 读中文 pytest.ini 报 GBK 错）：

```powershell
cd backend; $env:PYTHONPATH="."; $env:PYTHONUTF8="1"; $env:MINDMAP_TEST_DSN="postgresql://postgres@127.0.0.1:55433/mindmap_test"
& D:\AnaConda\python.exe -m pytest -o addopts="" -q -p no:randomly
```
