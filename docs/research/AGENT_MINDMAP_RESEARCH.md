# Agent 做思维导图：外部调研汇总

调研时间：2026-11。star 数与最后 push 时间均为 `api.github.com` 实测值，不是记忆或估算。
各仓库的 README 原文快照在 `docs/research/readmes/`（18 份）。

本文用途：为「把本项目从一次性生成改造为 agent 化」提供外部参考。文末的「对本项目的映射」是调研者（非本仓库作者）给出的判断，供计划制定者参考，不是既定方案。

---

## 一、结论摘要

1. 「agent 循环 + 思维导图」这个交汇点上**没有万星项目**。真正带 agent 循环的最高 star 是 304，而单纯「文本转导图」的内核 markmap 有 13217。这条路还没有被走通，没有现成答案可抄。
2. 生态里存在**两条互斥的技术路线**：生成后编辑（用 op 改树）与生成前探索（多步推理再落树）。两者解决的是不同问题。
3. MCP 层的共识是：**导图是 agent 的输出物，不是 agent 本身**。xmind-mcp 系全是「操作 .xmind 文件的工具」。
4. 唯一把「agent 化」拆得足够清楚、可以直接照结构的是 `xianjianlf2/MindGeniusAI`（285 star，见第四节）。

## 二、带 agent 循环的项目

判断标准：有「规划 → 执行 → 反馈 → 再规划」的多步，或调用工具，或多角色协作。只做一次 LLM 补全的不算。

| 仓库 | star | 最后 push | 它怎么用 agent |
|---|---|---|---|
| helixnow/deep-student | 304 | 2026-11-02 | 学习工作台，agent 直接调 mind map 等 app 工具；goal mode 跨轮、并行 sub-agents、MCP、高危操作审批 |
| xianjianlf2/MindGeniusAI | 285 | 2026-06 | 4 个工具 + `stopWhen: stepCountIs(8)`，plan→retrieve→draw→edit 每次工具调用可见 |
| vcmf/dim0 | 197 | 2026-11-04 | 无限画布上的 board-aware agent，读取选中节点后多步写回可编辑节点 |
| cheshireyang/mindmap-mcts-skill | 110 | 2026-06-24 | 给 Codex/Claude Code 装「可见思维树 + 轻量 MCTS」：propose branches → 跑探针 → 证据打分 → 回传 → UCB 选下一支 |
| xiehuan123/dsh-deepread | 67 | 2026-09-11 | 11 步费曼循环：目录 → 逐章证据 → 章节导图 → 闭书复述 → 自测查缺 → 对照修正 → 合并导图 |
| jmanhype/MindMapper | 34 | 2026-03-25 | 多个 LLM agent 各持 thought tree，按 tick 互相交流演化认知图 |

## 三、挂着 agent 名头、实为单次生成的项目

这些可以读它们的 prompt，但不要当作 agent 架构参考。

| 仓库 | star | 实际情况 |
|---|---|---|
| kongkongyo/Ai-Markmap | 443 | 纯前端单文件，一次 prompt 采样 1–5 个版本，无反思循环 |
| Fu-Jie/openwebui-extensions | 333 | 插件集里的 Smart Mind Map，单次生成；tool 版可被 AI 主动调用 |
| XimilalaXiang/DeLive | 260 | 系统音频转录 → 摘要/导图，固定流水线；另开 MCP server 供外部 agent 调用 |
| mindolph/Mindolph | 201 | 桌面 PKM，单次 LLM API 调用 |
| format81/TI-Mindmap-GPT | 112 | 威胁情报文章 → 导图，单次生成 |
| SimpleAI2026/SimpleMind | 84 | 原 linkxzhou/SimpleMind。有价值的是 thinking templates（第一性原理/费曼/贝叶斯/批判性）+ 每节点自带 `nextSystemPrompt` 做逐层展开；工程质量不足以直接参考 |

## 四、MindGeniusAI 的结构拆解（最值得照抄的部分）

技术栈：React 18 + AntV X6（前端）、Hono + Vercel AI SDK v5（后端）、zod 校验、in-process RAG。
关键文件：`apps/server/src/agent/tools.ts`、`apps/server/src/prompts/index.ts`。

### 4.1 四个工具的划分

```ts
// 出整图
mindmap_generate: { topic, extraContext?, thorough? } → markdown
// 按节点展开
node_expand: { content } → markdown list
// 按 id 精确改已有图
mindmap_edit: { ops: [
  { op: 'add',    parentId, label },
  { op: 'update', id, label },
  { op: 'remove', id },
] }
// 检索附件
rag_query: { question } → 相关段落
```

循环由 `streamText` + `stopWhen: stepCountIs(8)` 控制，最多 8 步工具调用。

### 4.2 schema 强校验代替 prompt 祈求

```ts
const mindMapStructure = z.object({
  root: z.string(),
  branches: z.array(z.object({
    title: z.string(),
    points: z.array(z.string()).max(8),
  })).min(2).max(8),
})
```

用 `generateObject` 强制结构化输出；**失败才回退**到「自由生成 markdown + 剥代码围栏」。分支数下限定在 2，上限定在 8，由 schema 而非 prompt 保证。

### 4.3 双温度两段式

默认走单次结构化（快）：`mindMapDirectPrompt`，temperature 0.7。
`thorough=true` 才走两段：

```
brainstormPrompt(topic)      temperature 0.9   // 只管发散，不管格式
      ↓
structurePrompt(topic, 发散结果)  temperature 0.3   // 收敛进 schema
```

### 4.4 单次 prompt 原文（`mindMapDirectPrompt`，中文分支）

```
围绕主题「${topic}」构建一张思维导图。
先在心里尽量发散（覆盖多种角度、子主题、关键要点），再组织成：
- 一个简洁的中心主题
- 5~7 个主分支，每个分支一个简洁标题
- 每个分支下 3~6 个简洁要点
去重、择优、措辞精炼。
```

`structurePrompt`（第二段收敛）中文原文：

```
把下面这段关于「${topic}」的发散内容，提炼组织成一张思维导图：
- 一个简洁的中心主题（root）
- 3~7 个主分支，每个分支一个简洁标题
- 每个分支下 3~6 个简洁的要点
去重、择优、措辞精炼。发散内容如下：
```

### 4.5 op 模型与双向同步

- 用户的画布编辑和 agent 的编辑共用同一套 `add/update/remove` op。
- 用户的手动操作被缓冲合并后，作为 `recentEdits` 随下一次请求发给 agent，让它知道「你改了什么」，而不只是当前快照。
- `mindmap_edit` 在后端只回执，实际 patch 由前端应用（因为前端持有画布状态）。

### 4.6 它的 README 自述对比表（我们正好落在被批评的那一栏）

```
| Real multi-step agent | ✅ visible tool calls (plan → retrieve → draw → edit) | ❌ one-shot prompt → static tree |
| Edits the existing map | ✅ surgical, by node id                          | ❌ regenerates the whole thing  |
```

## 五、方法论层：可复用的硬结论

### 5.1 数量约束必须写成「区间 + 分层」

不同来源的一致性很高，没有一篇建议「要全面」这种表述：

- 腾讯云（播客知识导图）：拆成 6–7 个板块，每个板块 3–6 个要点；板块之间要有逻辑递进。
- boardmix：4 至 6 个一级分支，每个一级分支包含 2 至 4 个二级节点。
- 掘金（PRD 拆解）：3–4 层；每个节点子项不超过 6 个。
- CSDN（DeepSeek 生成导图）：5–7 个主分支，每支 3–5 个子分支。
- 最简版（AI导航）：最多 5 个一级分支，每个分支下面最多 4 个二级要点，要点用短句，不要解释。

### 5.2 覆盖面用「双向白名单」

boardmix 原文：「必须覆盖【要点】，不要加入【范围外内容】。节点使用简短中文短语；对不确定的事实标记"待核验"」。
掘金用的是把必须包含的字段清单直接写进 prompt（目标与范围、角色权限、核心流程……），清单即覆盖度 schema。

### 5.3 禁废话最有效的是 ❌/✅ 对照例

腾讯云那篇明说这是让 AI 理解「什么叫提炼」最有效的方式：

```
❌「主播提到了主要矛盾的重要性，并举例说明」
✅「先论主要矛盾，才能提纲挈领地解出整个命局」
```

### 5.4 想被机器校验就必须用 JSON 树，不能用 Markdown 层级

- Markdown 标题层级：便宜、可人审、可直接粘进 XMind。
- JSON 树：可程序校验、可带 `kind` 与 `refs`。

jieyuepei/dsh-session-mindmap 的 DESIGN.md 用后者，节点形如：

```json
{"title": "...", "root": {"id": "n1", "label": "核心主题", "kind": "topic",
 "detail": "一句话概述", "refs": {"seq": [12, 48]}, "children": []}}
```

`kind ∈ topic|conclusion|decision|todo|question|file` 决定配色，`refs.seq` 支撑「这个节点来自原文哪一段」。

### 5.5 真正的多轮只有两种，且都必须把上一轮产物显式放回 prompt

- 按分支扩展（人工带上下文，CSDN）：出 5–7 主分支骨架 → 「请扩展 XX 分支，添加 3 个二级子节点」→ 「基于前次结构做优化：合并 A 与 C、给 B 新增实施路径、删除冗余的 D」。
- 按段 Map-Reduce 归并（程序带上下文，dsh-session-mindmap）：超预算时按轮切 ≤8 段，各出局部脑图，再把**局部脑图而不是原文**归并成总图，成本上界 = 段数 + 1。

dsh-session-mindmap 的 D12 还有一条实测结论：**模型对同一话题的粒度不稳定（实测同一会话两次生成 40 → 20 个节点）**，必须把上一次生成的节点 label 连同「同一话题沿用相同措辞」写进 prompt，否则增量对比全是噪音。

### 5.6 校验要落在确定性代码上

- dsh-session-mindmap：JSON 解析失败时带校验错误回喂、**只重试 1 次**，且按失败类型换话术（截断就要求更小产出）；校验项为节点数 ≤80、深度 ≤4、去重、字符裁剪；仍失败则**不出图**，原话是「一个"长得像脑图的目录树"比明确报错更误导人」。
- markmap-skill：用脚本 `validate_and_render.py --validate-only` 断言「每个非叶节点 5–7 个分支」「每条分支必须带类比」，失败就要求先改大纲，不许绕过。
- 让模型自己反思分支数并自动重试的中文教程正文：**未找到**。

### 5.7 「一次性生成整棵树会失败」的成因

最硬的一条证据来自 dsh-session-mindmap 的 DESIGN.md：

> prompt 允许 60 个节点（label 40 字 + detail 120 字 ≈ 4,300 token），而 maxOutputTokens 是 4000 → JSON 被硬截断，重试发同一指令继续撞墙。

它给出的规则：产出规模必须装进输出预算，用最坏情况（节点数 × 满额字段）估 token 并写守卫测试；解析器要能救回被截断的 JSON；错误信息必须带 finish 原因。

版面侧的独立结论（AI导航）：「层级超过三层，图必然失控」「一级分支超过 6 个，画面就会变成一棵枝杈乱伸的树」。给的控制线是一级 3–6、二级每支 2–4、三级少量。

## 六、对本项目的映射（调研者判断，非既定方案）

### 6.1 已确认的缺陷（有代码依据）

**生成链路不检查 `finish_reason`。** `backend/app/generation/service.py` 的两个出口都是裸的 `parse_model_json(body["choices"][0]["message"]["content"])`，而同一代码库里 `vision.py` 与 ingestion 都读了 `finish_reason == "length"` 并判失败（`tests/test_vision.py:97`、`tests/test_ingestion.py:352` 有对应断言）。最核心的入口反而是唯一没防截断的。

**输出预算与契约上限失配。** 契约允许 300 节点、每节点 label 200 字（`backend/app/contracts/mindmap.py`），按每汉字约 0.6 token 估算约 4.4 万 token，是 `MAX_OUTPUT_TOKENS = 16384` 的 2.7 倍。实测锚点：240 节点 7578 token（约 31.6 token/节点，反推 label 平均 11 字），说明正常输入够用、密材料会撞。撞了之后 `_balance_brackets()` 会把硬切的 JSON 补成合法的半棵树——**静默成功**。

**`MIN_BRANCHES = 1` 过松。** 所有调研来源给的下限都是 3，主流是 4–6。

**不能按节点增量编辑。** 前端 `generateSources()` 靠 `contentAtStart` 引用比对 + 弹窗确认来回避「重新生成冲掉用户编辑」，这是绕而不是解。

### 6.2 可借鉴的四点

1. 把 `MIN_BRANCHES` 提高，并在 prompt 里补「每支要点数」的下限（现有 `DETAIL_PROMPT` 只有「3-6 个一级分支」，没写每支几个要点）。
2. `DETAIL_PROMPT` 增加 ❌/✅ 对照例，专治 `source_refs` 的 `quote` 退化成概括。
3. 引入 `finish_reason` 检查，截断走 `GenerationError` 而不是静默半棵树。
4. 粒度漂移：我们观察到引用输出在 28 节点 0 引用 与 15/16 引用之间波动，与 D12 是同类问题，解是措辞锚定。

### 6.3 两条路线与我们的位置

- 路线 A「生成后编辑」：MindGeniusAI、dim0。解决「改不动」。
- 路线 B「生成前探索」：mindmap-mcts-skill、dsh-deepread。解决「想得浅」。
- 本项目现状：一次性 prompt 出整棵树，两条都不属于。

## 七、参考材料索引

- `docs/research/readmes/MindGeniusAI.md` — 四工具架构、双向同步、README 对比表
- `docs/research/readmes/dim0.md` — board-aware agent 的多步写回
- `docs/research/readmes/mindmap-mcts-skill.md` — MCTS 选支
- `docs/research/readmes/deep-student.md` — goal mode / 并行 sub-agents
- `docs/research/readmes/dsh-deepread.md` — 11 步费曼循环
- `docs/research/readmes/Ai-Markmap.md` — 忠实整理型 prompt
- `docs/research/readmes/markmap-mcp-server.md`、`mindmap-mcp-server.md` — MCP 层的接口形态
- 其余见目录。

外部原文（供追溯）：
- 腾讯云开发者社区《1小时播客3分钟看完…（附完整prompt）》 https://cloud.tencent.cn/developer/article/2765281
- 掘金《把 PRD/需求描述自动整理成思维导图》 https://juejin.cn/post/7586504691845627913
- AI导航《AI做思维导图总是一团乱怎么办？先给结构再让它画》 https://xiaoan.cc/article/ai-zuo-si-wei-dao-tu-zong-shi-yi-tuan-luan-zen-me-ban-xian-g-agk
- boardmix《AI 生成思维导图怎么做？提示词、步骤与结果校验》 https://boardmix.cn/article/ai-generate-mind-map/
- CSDN《DeepSeek生成思维导图》 https://blog.csdn.net/qq_37935324/article/details/145622689
- dsh-session-mindmap DESIGN.md https://raw.githubusercontent.com/jianyuepei/dsh-session-mindmap/main/DESIGN.md
- markmap-skill SKILL.md https://raw.githubusercontent.com/aiwriter-windfeng/markmap-skill/main/SKILL.md
