# LightMem 调研：agent 记忆该怎么实现，我们该不该做

调研时间：2026-11（论文 v4 / 仓库 commit `09b57f7`）
结论先行：**三层架构不搬；只搬"在线只追加"这一条；落地物是 `run_steps` 一张表。**

本文区分三种依据，正文用标记注明：
- 〔论文〕arXiv:2510.18866v4 正文原话
- 〔代码〕本地克隆 `D:\tmp\LightMem` 实际源码
- 〔推断〕我的判断，附理由
- 〔本仓〕`D:\genai-mindmap` 实际代码

---

## 0. 资料可及性（先说清楚读了什么、没读到什么）

| 源 | 状态 |
|---|---|
| 论文 HTML 全文 | 读到。`arxiv.org/@/html/2510.18866`，v4，正文 85 KB / 1905 行 |
| 论文 PDF | 未抓（HTML 已含表格与附录，无需重抓） |
| GitHub `zjunlp/LightMem` | 读到。`--depth 1` 克隆，全量源码 |
| 中文社区 | 部分读到。见 §3，两篇 InfoQ/QCon 抓不到正文 |

代理说明：任务给的 `127.0.0.1:7890` 不通，实际监听端口是 **7897**（`verge-mihomo`，Verge 新版默认混合端口）。以下所有抓取走 7897。

---

## 1. LightMem 的实现要点

### 1.1 三个 Light 对应的代码模块

| 论文 | 代码 | 位置 |
|---|---|---|
| Light1 预压缩 | `LlmLingua2Compressor` / `EntropyCompressor` | `factory/pre_compressor/{llmlingua_2,entropy_compress}.py` |
| Light1 主题切分 | `LlmLingua2Segmenter` + `SenMemBufferManager` | `factory/topic_segmenter/llmlingua_2.py`、`factory/memory_buffer/sensory_memory.py` |
| Light2 短时记忆 | `ShortMemBufferManager` + `manager.meta_text_extract` | `factory/memory_buffer/short_term_memory.py` |
| Light3 长时 + 睡眠更新 | `offline_update` / `construct_update_queue_all_entries` / `offline_update_all_entries` | `memory/lightmem.py:399-634` |
| 主流程 | `LightMemory.add_memory` | `memory/lightmem.py:204-394` |

关键量（代码默认值，非论文）：
- 感官记忆缓冲 `SenMemBufferManager(max_tokens=512)`〔代码〕，与论文"buffer 512"一致〔论文 §5.1〕
- 短时记忆缓冲 `ShortMemBufferManager(max_tokens=2000)`〔代码〕；实验扫描 256/512/768/1024〔论文 Table 2/3〕
- 切分只对 `role=="user"` 计 token，assistant 消息无损带过、不占预算〔代码 `_recount_tokens`〕

### 1.2 数据存在哪：向量库 payload，不是 SQLite

**这是本次调研最需要纠正的常见误解。**

- 主存储：Qdrant（默认）/ FAISS / BM25，见 `RetrieverConfig`〔论文 README 表格〕
- 条目字段落在 Qdrant payload〔代码 `memory/lightmem.py:411-428`〕，共 14 个：
  `time_stamp, float_time_stamp, weekday, topic_id, topic_summary, category,
  subcategory, memory_class, memory, original_memory, compressed_memory,
  speaker_id, speaker_name, consolidated`（`bam_tags` 可选）
- `index_strategy ∈ {context, hybrid}` 时另存一份 `memory_entries.json`〔代码 `lightmem.py:404-406`〕
- **`history_db_path` 是死配置**：只在 `configs/base.py:73` 声明，全仓库零引用；README 却写 "persist conversation history and lightweight state. Useful to restore state across restarts"〔代码 + README〕。承诺的 SQLite 持久化**没有实现**
- 两个 buffer 都是进程内 list，重启即失〔代码〕

### 1.3 LLMLingua-2：功能上可选，包依赖上是硬依赖

- `pyproject.toml` 的主 `dependencies` 含 `llmlingua==0.2.2`、`torch==2.8.0`、`transformers==4.57.0`、`qdrant-client==1.15.1`〔代码〕→ **装包必拉 torch**
- 功能上 `pre_compress` / `topic_segment` 默认 `False`〔代码 `configs/base.py`〕→ 不开就不加载模型
- 模型 `microsoft/llmlingua-2-bert-base-multilingual-cased-meetingbank`：**177,341,186 参数，全 F32**〔实测 HF API `/api/models`〕→ 权重约 **677 MB**
- 论文称 "follows a lightweight BERT architecture and requires less than 2GB of GPU memory"〔论文 App. A.1〕

**CPU 速度〔推断〕**：177M 参数 BERT-base 量级，512 token 单次 forward 在 2 核上量级是数百毫秒。对 2C2G 的真正障碍不是单次延迟，而是常驻成本：677 MB 权重 + PyTorch 2.8 CPU 运行时（数百 MB）→ 内存基本占满。未做本机实测（不为此下载 677 MB 权重），故标推断。

### 1.4 "sleep-time" 是显式调用，不是定时任务

```python
lightmem.construct_update_queue_all_entries()        # 建更新队列
lightmem.offline_update_all_entries(score_threshold=0.8)
```
- 两步都在 `memory/lightmem.py`，**没有 cron / apscheduler**（全仓 grep 无命中）
- 有独立脚本 `experiments/longmemeval/offline_update.py` 和 MCP tool 各手动触发一次〔代码〕
- `configs/base.py:95` 把 offline 描述成 `"scheduled updates"`，与实现不符〔代码〕

离线阶段**实际**做两件事（不是"去重 / 合并 / 修复"三件）：
1. `construct_update_queue_all_entries(top_k=20, keep_top_n=10)`：每条 entry 用**自己的向量**搜 top-20 邻居，过滤 `float_time_stamp <= ts`（只允许更晚的更新更早的），取 top-10 写入 `update_queue` 字段。`ThreadPoolExecutor(max_workers=8)`
2. `offline_update_all_entries(score_threshold=0.9)`：反向收集"谁的 `update_queue` 里包含我且 score ≥ 阈值"，调一次 LLM `UPDATE_PROMPT`，返回 `action ∈ {update, delete}`。`max_workers=5`

### 1.5 记忆条目 schema（`MemoryEntry`，18 字段）

```python
id, time_stamp, float_time_stamp, weekday, category, subcategory,
memory_class, memory, original_memory, compressed_memory,
topic_id, topic_summary, speaker_id, speaker_name,
hit_time, update_queue, consolidated, bam_tags
```
〔代码 `memory/utils.py:class MemoryEntry`〕

时间轴只有一条（`time_stamp` / `float_time_stamp`）。对照 Zep/Graphiti 的四时间戳（见 §3），这是简化。

### 1.6 代码级缺陷（我发现的，论文没写）

按严重度：

1. **`online_update()` 函数体只有 `return None`**〔代码 `lightmem.py:396-397`〕。README 承认 `'online'` 是 "no-op placeholder"。所以论文 §3.3 的 "soft update at test time" 在代码里的实现方式是：默认 `update='offline'` → `offline_update()` 直接 embedding + insert。**soft update == 只插入**。
2. **合并后向量不重算**：`offline_update_all_entries` 里 `action=="update"` 时只改 payload 的 `memory`，`vector = entry.get("vector")` 仍是旧向量〔代码 `lightmem.py:618-623`〕→ 合并后的文本与它的向量语义失配，检索会漂。
3. `history_db_path` 死配置（见 1.2）。
4. `candidate_sources` 收集是 **O(n²) 全表扫描**（对每条 entry 遍历全部 entry 的 `update_queue`）〔代码 `lightmem.py:578-584`〕。
5. `LlmLingua2Segmenter.buffer_len` 读了配置但 attention 计算里从未使用〔代码〕。
6. **论文说边界是 A ∩ S（交集），代码不是**：`cut_with_segmenter` 先取相似度细边界，再要求它与某个注意力粗边界相差 ≤3 turn 才采纳；**若一条都对不上，直接退化为纯相似度边界**〔代码 `sensory_memory.py:cut_with_segmenter`〕。§3.1 的公式应理解为"带容差的仲裁 + 失败退化"。

---

## 2. 四个问题的回答

### 2.1 Light1 压缩：对我们有硬冲突，不能搬

〔本仓〕引用不变量是逐字相等：`_reference_problem` 拿 `quote != text[start:end]` 比对（`backend/app/agent/tools.py:892` 注释、`:4592` 实现），且 `library_chunks` **没有 text 列**——只有 `source_id/start/end/level/parent`，块文本是从 `library_sources.text` 现场切的（`backend/app/knowledge/library.py:CREATE_CHUNKS`）。

三处硬冲突：

1. **坐标失效**。压缩改写 `source.text` 后，历史 `start:end` 全部指向错误位置，`quote == text[start:end]` 必然 `SOURCE_QUOTE_MISMATCH`，引用被 `remove_invalid=True` 静默剔光。
2. **检索与引用脱钩**。若只压缩索引侧、保留原文，则 BM25 的词表与 `quote` 的词表不在同一空间，命中后引用对不上。
3. **压缩粒度与我们相反**。LLMLingua-2 是 token 级二分类丢弃，丢的恰是虚词与重复限定词；中文里丢这些句子读不通，而用户要在导图里读引用。

LightMem 自己也没解决：`LlmLingua2Compressor.compress` **就地覆盖 `mes['content']`**，源码留着未完成的 TODO——

> `# TODO: Consider adding an extra field in the message, compressed_content, and put the compressed content in this field while keeping content unchanged.`

〔代码 `factory/pre_compressor/llmlingua_2.py`〕作者知道该保留原文，没做。`MemoryEntry.original_memory` 是在压缩**之后**才填的，救不回来。

**结论**：我们的"冗余"不是对话寒暄，是长文档里的重复论述——而那正是 BM25/RRF 的信号来源，压掉是负收益。

### 2.2 Light2 阈值触发：没有对应物

Light2 的机制是"攒够 token 才花一次 LLM 调用"〔论文 §3.2〕。它的前提是**增量到达**——一条条消息流进来。

我们没有增量：一次请求就是一次批处理。最接近的对应物是 `_generate_deep` 的层内并发展开〔本仓 `tools.py:2130`〕，但那是**预算驱动**而非阈值驱动：`INITIAL_MAX_NODES=80` / `INITIAL_MAX_DEPTH=3` → 满额 25 次内容调用；"额度不足就停手，已经展开的部分照常交出，标 `partial`"。

我们的粒度已经比它细（一支一次调用），没有"攒"的空间。

唯一可借的是一条**负面发现**：论文 §5.5 说 STM 阈值对效率单调、对准确率**非单调**，"the ideal setting ... requires careful tuning"。对我们的推论〔推断〕：加预算不等于加质量，`INITIAL_MAX_NODES` 上调前要先有离线评估，别凭感觉加。

### 2.3 Light3 soft update + 离线重建：思想可搬，机器不可搬

场景对照：

| LightMem | 我们 |
|---|---|
| 无界增长的对话历史 | 一次 run，有明确终点 |
| 条目会矛盾/过期，需要合并 | 任务是单调推进的状态机 |
| 语义相似度找邻居 | 需要"哪支做完了"这种**精确**查询 |
| 合并靠 LLM 判 update/delete | 进度是事实，不需要 LLM 判断 |

核心判断〔推断〕：**我们的记忆不是内容记忆，是状态记忆。** LightMem 整套机器（向量找邻居 → LLM 判 update/delete）是为"语义矛盾"设计的，我们没有语义矛盾。用向量库存任务进度，是用牛刀做算术——而且是每次续跑都要先算 embedding。

**可搬的只有一条**：**只追加、不覆盖**。这直接对应 checkpoint 设计——不要 `UPDATE` 一行状态，要 `INSERT` 一条事件。这条在工业界有独立印证：某中文教材明确写"**2026 年 Mem0 改成了只 ADD**"（见 §3.3），与 LightMem 的 soft update 殊途同归。

### 2.4 topic segmentation 比我们的结构切块好吗：分情况，结论是不换

两边信号性质不同：
- 我们：`_chunk_tree` → `_chunk_spans`（父块 450-700）+ `_leaf_spans`（子块 200），边界取自 `_unsafe_boundaries`（结构标题 / 空行 / 句末）〔本仓 `knowledge/service.py`〕→ **语法/版式**信号
- LightMem：attention 局部峰（语义转折）+ 相邻相似度（语义跳变）→ **语义**信号，代价是 177M BERT + GPU + 512 token 上限〔论文 App. C.1〕

**它更好的场景**：
- 无版式文本：ASR 转录、会议记录。我们确实有 `ingestion/asr.py`，转录出来没有标题，`_unsafe_boundaries` 只剩句末，这时语义切分有价值——这是**唯一**值得试的场景
- 段内主题漂移：一个 700 字父块里讲了两件事

**我们更好的场景**：
- 有标题层级的材料：`heading_outline` 直接拿材料自己的目录，比任何语义推断都准（骨架那条链已经在用）
- 引用要落在可见父块内：结构边界保证"这段完整属于某标题下"；语义切分可能切在句子中间，破坏"落在可见父块内"的约束

**关于 6.3% / 5.4%**〔论文 §5.4〕：这是**对话**数据集上的消融（session 当 ground truth），证明"加了这个模块比不加好"，**不等于**"比结构切块好"。不能直接外推到文档切块。

---

## 3. 中文社区在工程上怎么落地

### 3.1 资料可及性（如实交代）

| 目标 | 结果 |
|---|---|
| CSDN / 知乎 | **没直接命中**高质量工程文。搜索结果被官方科普与二手复述占据 |
| InfoQ《从上下文到经验资产：Agent 记忆系统的工程化路径与 MemOS 实践》 | **抓不到正文**（SSL 握手失败 / 0 bytes） |
| QCon《Agent 的记忆不在对话里》 | **抓不到正文**（同上）。标题正是我们要的主题，可惜 |
| hello-agent-system 第 18 课（中文 agent 记忆教程） | 读到，质量高，含"坑—后果—正确做法"三列表 |
| awesome-agent-memory / Letta 笔记 | 读到 |
| Graphiti bi-temporal 解读 | 读到 |
| mem0 源码 | **读到一手代码**（就在 LightMem 仓库 `memory_toolkits/.../baselines/mem0/`） |

### 3.2 三个开源实现的数据模型

**mem0**（一手源码 `mem0/memory/main.py`）
- payload 核心：`data`（记忆文本）、`hash`（去重）、`created_at`、`updated_at`、`id`，外加 promoted keys（`user_id` / `agent_id` / `run_id`）
- `hash` 字段与我们 `library_sources.content_hash`（sha256）是同一思路
- 写入流程：抽候选事实 → 检索 top-s 相似已有记忆 → LLM 函数调用选 ADD/UPDATE/DELETE/NOOP

**Zep / Graphiti**
- **双时间轴、四个时间戳**：T 轴（事件时间）`t_valid, t_invalid`；T′ 轴（事务时间）`t'_created, t'_expired`；另有 episode 的 `t_ref`
- 矛盾用 invalidation（版本化）处理，不覆盖
- 比 LightMem 的单时间轴严格；对我们的启示：**"这条记录什么时候写下的"（T′）与"它描述的事什么时候成立"（T）是两回事**，任务进度两者都需要

**Letta（MemGPT）**
- Block-based：persona / human / archival，分类管理
- memory 操作走 tool calling，不做框架隐式管理
- 笔记里一条建议正中我们靶心：`MemoryRecord` 保留 `kind` 字段（`profile | session | fact | procedure | **trace**`）

### 3.3 非对话类 agent 用记忆：没有可信实战报告

- InfoQ/QCon 两篇主题对，正文抓不到，不能引用其结论
- 一条**间接**证据：LightMem 自带 `tutorial-notebooks/LightMem_Example_code.ipynb`（code assistant）与 `travel`，notebook 日志显示 `Received 15 memory entries` ——说明它把**工具轨迹**当记忆喂进去过。但 notebook 里没有效果对比数据，只能算存在性证据，不算效果证据〔代码 + 推断〕

### 3.4 常见的坑（有一手中文整理）

最有用的三条〔教程 §6〕：
1. **把 UPDATE 实现成直接覆盖** → 改错了查不到、改不回来 → 旧值写 `history`，DELETE 走软删除
2. **把工具输出/网页/邮件自动写进记忆** → 记忆投毒的主入口 → 只记明确表达过的内容，做 `source_type` 检查
3. **检索时刷新近期性却不加限制** → 富者愈富，常被检索的永远"新鲜"，挤掉新信息

还有一条成本纪律直接判死一个方案〔教程 §5.5〕：**写入异步化，每条消息 1~2 次模型调用，不能放在响应路径上**。我们是 20-70 秒同步请求，往里面再插记忆写入就是自己给自己加延迟。

### 3.5 一条必须记下的反证

同一份教材给出的数字〔二手引述 Mem0 论文〕：LOCOMO 上 **full-context 准确率 72.9%，Mem0 只有 66.9%**；记忆系统换来的是 p95 延迟降 91%（17.1s → 1.4s）、token 省 90%+。原文结论："**记忆系统首先是成本和延迟的优化，不是准确率的免费午餐。**"

**这与 LightMem 论文自己的表完全吻合**——见 §4.1。

---

## 4. 我们该做什么、不该做什么

### 4.1 先纠正三处与项目现状不符的前提

**（a）`runs` 表已经远超"只记状态"。**
实际 10 列：`run_id, status, request, candidate, failure, mode, created_at, updated_at, applied_at, dismissed_at`〔本仓 `mindmaps/repository.py:36-47`〕，配 `begin()` 幂等认领（`BEGIN IMMEDIATE` 事务内判重）、`finish()` 的 `COALESCE` 草稿保护 + `done` 后冻结、`pending()` 的"有草稿且未处理"查询、`mark_applied` / `mark_dismissed`〔`agent/runs.py`〕。

所以"上次生成的导图去哪了"**已经能回答**。缺的不是这个。

**（b）单支失败已经被容忍。**
`_generate_deep` 的 docstring 写死四段失败语义："某支展开抛错 → 那一支留空，其余照常（一次 429 不该废掉整张图）"，并有 `partial` 标记的五个来源〔`tools.py:2170-2175`、`:3327-3330`〕。所以"第 4 支断了"若是**单支失败**，现状不丢东西。

**（c）真正缺的是 `_generate_deep` 内部的分支级 checkpoint。**
依据：该函数是**一次工具调用内的完整分片生成**——骨架 → 层内 `asyncio.gather` 并发展开（`tools.py:2789`、`:2997`）→ 验收补支 → 合并校验后才 return。整棵树在 gather **全部完成后**才构造。`RunContext` 逐个字段看过（`request, candidate, revision, batches, sources_by_id, executed, dedupe, model_calls_used, extra_model_calls, emit, evidence`），**没有任何分支级进度字段**。

于是：一次 25 次内容调用的生成，在第 18 次被超时/取消时，前 17 次**全部丢失**，`runs.finish()` 写进去的 `ctx.candidate` 还是上一次的完整树（或 None）。

这才是"上次那张图在第 4 支断了，从哪继续"的真实语义——**不是整次 run 的记忆，是 run 内部的分支记忆。**

### 4.2 用 LightMem 那套三层架构值得吗：不值得

四道否决：
1. **内存**：677 MB 权重 + torch 2.8 运行时，2C2G 直接吃满（§1.3）
2. **场景**：它的复杂度是为**无界历史**付的；我们的 run 有界
3. **收益口径**：它的 token 降幅只统计 memory-bank construction，**不含检索阶段**——论文自己说明"for fair comparison, K, N and number of retrieved entries are same among all methods"〔论文 §5.1〕。我们的成本恰恰在检索 + 生成
4. **准确率**：见 4.3

### 4.3 必须看清的一组数字（重要）

论文 Table 2 / Table 3，GPT-4o-mini 骨干，ACC%：

| 数据集 | FullText | NaiveRAG | Mem0 | A-MEM | LightMem |
|---|---|---|---|---|---|
| LongMemEval-S | 56.80 | 61.00 | 53.61 | 62.60 | **68.64** |
| LoCoMo | **73.83** | 63.64 | 61.69 | 64.16 | 71.95–72.99 |

〔论文 Table 2/3〕

- LongMemEval-S 上 LightMem **全面最优**，含超过 FullText
- **LoCoMo 上 LightMem 71.95–72.99 仍低于 FullText 73.83**；论文所说 "improves ACC by 6.10%–18.12%"，是相对 **Mem0/A-MEM/MemoryOS 这些记忆基线**，不是相对 FullText〔论文 §5.2〕

这条与 §3.5 的中文反证互相独立地指向同一结论：**记忆系统的价值主张是"用极小成本逼近全量上下文"，不是"超过全量上下文"。** 拿它当"准确率工具"引入，会失望。

### 4.4 如果只做一件事

**加一张 `run_steps` 表，只追加，在 `_generate_deep` 每个分支落定时写一行。**

```sql
CREATE TABLE IF NOT EXISTS run_steps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    step        TEXT NOT NULL,   -- 'skeleton' | 'branch' | 'supplement' | 'verify'
    branch_path TEXT,            -- '0/3' = root.children[0].children[3]；skeleton 为 NULL
    status      TEXT NOT NULL,   -- 'started' | 'ok' | 'failed' | 'skipped'
    payload     TEXT,            -- 该支展开出的子树 JSON（status='ok' 时有值）
    error       TEXT,            -- status='failed' 时的 code/message
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_run_steps_run ON run_steps(run_id, id);
```

时间格式必须沿用 `strftime('%Y-%m-%dT%H:%M:%fZ','now')`——三处表格式不同会在"谁更晚"上错一天，这是仓库里已经写过的教训〔`runs.py:48-50`、`library.py` 注释〕。

设计取舍都有依据：
- **只追加不 UPDATE**：这是 LightMem soft update 唯一可搬的部分，也是 Mem0 2026 年的收敛方向（§3.3）。崩溃时最后一行 `started` 就是断点，不需要另维护"当前进度"字段，且天然可审计
- **`branch_path` 用父节点索引链，不用 UUID**：`tools.py` 里 `path` 已是这个语义（"`path` 是父节点的子索引链"），前端与续跑逻辑不必再学一套坐标系
- **`payload` 存子树而非 op 批次**：`ctx.batches` 是增量、依赖顺序重放；子树幂等，可直接按 `branch_path` 拼回
- **不复用 `runs.candidate`**：它是整树、`COALESCE` 语义、`done` 后冻结；step 是过程流水，两种生命周期混在一列会互相污染

配套两个读接口（不新增写入路径）：
1. `resume_plan(run_id)`：取最后一条 `started` 且无对应终态的 step → "从哪支续"
2. `partial_tree(run_id)`：把 `status='ok'` 的 payload 按 `branch_path` 拼回一棵"已确定"子树，供 `_generate_deep` 跳过已完成分支

**续跑不是重新调模型，是跳过已 ok 的分支**——省下的就是已花掉的那部分调用。

### 4.5 一页判断（按成本/收益排序）

| # | 做什么 | 成本 | 收益 |
|---|---|---|---|
| 1 | `run_steps` 表 + `_generate_deep` 分支落定写一行 | ~120 行（1 表 + 3 写点 + 2 读接口） | "25 次调用中途取消 = 全丢" → "最多丢 1 支" |
| 2 | 续跑时跳过已 `ok` 分支 | ~60 行（`_generate_deep` 入口读 `partial_tree`） | 直接省 token 与 20–70 秒 |
| 3 | 界面显示断点（"上次在第 4 支中断，可续"） | ~40 行（复用 `pending()`） | 用户不用自己看 |
| 4 | `run_steps` TTL 清理，沿用 `prune` 的 90 天 / 200 条口径 | ~20 行 | 2C2G 磁盘不涨 |

**明确不做**（每条都给了否决理由，不是口味）：

| 不做 | 否决依据 |
|---|---|
| LLMLingua-2 压缩 | 与 `quote == text[start:end]` 直接冲突（§2.1）；677 MB 权重 + torch 在 2C2G 上不可行 |
| 向量库存记忆 | 进度是主键查询不是语义检索（§2.3）；已用 SQLite，加 Qdrant 是净增运维 |
| sleep-time 离线批处理 | 无常驻进程、无夜间窗口；2C2G 上离线任务只会抢用户等待时的 CPU |
| 语义切分替换结构切块 | 有标题材料上 `heading_outline` 更准；还会破坏"引用落在可见父块内"（§2.4） |
| 用户偏好记忆 | 用户要的是"这次生成做到哪了"。偏好是另一个产品需求，混进来会让 `run_steps` 语义变脏 |
| 抄 `history_db_path` 这种"声明了没实现"的配置 | LightMem 自己踩过，README 承诺与代码不符（§1.2） |

---

## 5. 对十点摘要的核对

| # | 摘要 | 判定 |
|---|---|---|
| 1 | 窗口限制 / lost in the middle / token 成本 / 在线更新延迟 | ✅ 论文 §1 Challenges 三条对应 |
| 2 | 不堆结构，拆成过滤 / 短期组织 / 长期维护 | ✅ |
| 3 | ICLR 2026，浙大/NUS/南大，zjunlp/LightMem | ✅（NUS 为 Shumin Deng；南大部分为 State Key Lab + Ningyu Zhang 双挂） |
| 4 | Light1 用 LLMLingua-2 压缩 + 注意力边界 + 相邻相似度切分 | ⚠️ 基本对，但"结合"的方式**不是交集**：代码是相似度为主 + attention ±3 turn 校验带，对不上则退化为纯相似度（§1.6-6） |
| 5 | Light2 按主题组织，buffer 到阈值才总结 | ✅（默认阈值 2000，实验扫 256–1024） |
| 6 | Light3 在线 soft update，离线并行去重/合并/修复 | ❌ **三处出入**：`online_update()` 是空实现；离线只有 update/delete 两个动作，没有独立"去重/修复"；**不是定时任务**，是两次显式调用（config 写 "scheduled" 与实现不符） |
| 7 | 超过强基线，同时降 token / API / 运行时间 | ⚠️ 需限定：LongMemEval 上全面最优；**LoCoMo 上低于 FullText**（71.95–72.99 vs 73.83），只超过记忆类基线（§4.3） |
| 8 | 7.7% / 29.3%；38× / 20.9×；106× / 117× | ✅ 数字对。但 106×/117× 是"仅在线测试时"；且"总 token"口径不含检索阶段（§4.2-3） |
| 9 | 去掉 topic segmentation，GPT −6.3% / Qwen −5.4% | ✅ 论文 §5.4 |
| 10 | 优化构建与维护成本；部署仍需处理检索质量、隐私、错误记忆、压缩失败、调参 | ⚠️ **论文没有独立的 Limitations 章节**。仅有 §5.5 "requires careful tuning" 有直接依据；隐私 / 错误记忆 / 压缩失败是外推，不是论文原话 |

---

## 6. 一句结论

**最大启发**：把"更新"拆成"在线只追加、离线再整理"。Mem0 在 2026 年也退到了这一步。对我们就是 checkpoint 只 `INSERT` 不 `UPDATE`。

**最大陷阱**：它的收益指标（38× / 106× token 降）是在**对话**上量的、只算 memory-bank construction、**不含检索**，而且 LoCoMo 准确率仍低于 FullText。把这套指标搬过来，会得出"我们该做一套记忆系统"的错误结论——我们的钱不是花在对话历史上，是花在检索 + 生成本身上。我们要的是**断点续跑**，不是**记忆基础设施**。
