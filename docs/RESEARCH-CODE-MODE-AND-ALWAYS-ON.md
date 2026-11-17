# 技术调研：Code Mode 与 always-on agent 对我们意味着什么

调研对象：

1. <https://code.claude.com/docs/en/code-mode>（Claude Code 文档，实验特性，抓的是 `.md` 原文）
2. <https://claude.com/ja/resources/articles/building-always-on-agents>（Claude 资源区 best-practice 文章，作者 William Hu，2026-11-13）

标注约定：**「文档」**＝原文写了；**「推断」**＝我按文档+我们代码推出来的，原文没有。

---

## 一、两篇各自讲了什么

**Code Mode（文档）**：Claude Code 的实验特性（v2.1.323+，默认关，任何版本都可能改或删），把「模型发 tool call、读结果、再决定」换成「模型写一段 JavaScript，Claude Code 在会话里执行」。工具在代码里是函数（`` $`cmd` `` 跑 shell、`edit(path, old, new)` 改文件，`.text()` / `.nothrow()` 取输出），一段代码可以并行、可以把上一个结果喂给下一个，**只把最后一行的返回值带回模型**。会话里看到的是 tool call（比如 diff），代码本身折在 Ctrl+O 的 **Code** 行里。适用场景原文列了三种：跨多文件的同一处改动、需要先过滤再读的检索、可以同时跑的独立命令。

**always-on agent（文章）**：这不是产品功能，是一篇「自建」路线的架构文章，开头就否认字面意思——always-on agent 不是一直开着，而是**大部分时间在睡，周期性醒来做一段活**，每段活叫一个 run（收一个事件、做完、把发生的存起来），底层就是普通 agent loop，差别在外围架构。外围由五个原语组成：event inbox（handler 写行、worker 取行）、timers（follow-up / schedule）、memory（用户指令 / 用户笔记 / 过往消息 / 任务进度）、permissions（凭据代理 / auto mode / deferred tool call）、outbox（dispatcher 按需发送）。前置条件原文自己写了：需要新模型的判断力（知道什么时候该来问），以及更便宜的智能（某个长程编码基准上 Opus 5.5 每次解决比 Opus 5 便宜 78%，常规负载 40%）。

---

## 二、Code Mode 到底是什么（据文档）

### 2.1 怎么工作

一段代码 = 一次工具执行 = 模型的一次往返。代码里能直接调工具（文档示例用了 `` $`rg -l ...` `` 和 `edit()`），能 `await`，能并发，最后一行是返回值。文档那句关键定义：*"One piece of code can run commands in parallel, use one result in the next call, and return the part of the output that Claude needs to read."* —— 收益的第三项是**只回传你筛过的那部分输出**，不是"少调几次工具"。会话视图里保留的是 tool call（diff 等），代码要在 transcript viewer 里展开——也就是说**代码不是给人读的默认视图**。

### 2.2 三道控制各管什么（这三层不是一回事，文档分得很清）

- **`deny` / `ask` 规则 + `PreToolUse` hooks：管内层每一次 tool call。** 原文：*"Each tool call that Claude's code makes goes through your `deny` and `ask` permission rules and your `PreToolUse` hooks."* 你不需要为 `Bash` 这类工具改 matcher——既有规则照旧生效。所以 Code Mode **没有减少权限判定次数**，它减少的是模型的往返次数。
- **auto mode 的 classifier：管"一段代码"，不管"一次调用"。** 它在代码跑之前审这段代码；原文明确它的盲区：*"It doesn't see values computed at run time, such as a path taken from search output, so keep rules or hooks for calls you must control."* 想审代码本体，得写一条匹配 `REPL`（跑代码的那个工具）的 `PreToolUse` hook，读 `tool_input.code`。其它 permission mode 下代码不审、逐次调用提示。
- **Bash 规则的天花板（原文自带警告）**：规则匹配的是命令文本，所以被 deny 的命令可以换个写法绕过；要"与命令文本无关"的边界，得用 sandbox。

### 2.3 明确的限制（文档逐条，全部是硬边界）

- resume 只能在终端、且必须仍开着 Code Mode；关了会**拒绝** resume。
- 等待式执行：代码跑完才回话，**10 分钟封顶**（含你在权限提示上花的时间）；要后台跑最多 2 小时，且会话必须以 auto 或 `bypassPermissions` 起。
- hook 出问题会**拒掉调用**：`the hook failed`（hook 起不来/被杀/退出码 126、127、>128/HTTP 错/输出非法）、`the hook did not finish`（超时，且一旦本会话跑过代码，对**所有**工具调用都适用），除非显式 `"onFailure": "continue"`。
- 不能用 Remote Control / Claude in Chrome / computer use；不能把会话搬到 Desktop 或转成 background session；`/fork` 不能开后台副本。
- 环境门槛（对我们最要命）：只支持交互式终端 `claude` —— **`-p`、Agent SDK、VS Code 面板、Desktop、云会话都不行**；不能走网关 / Bedrock / Claude Platform on AWS / Google Cloud Agent Platform / Microsoft Foundry；模型只认 Opus 5.5、Sonnet 5.5、Haiku 5.5、Fable 5.2；要原生安装；Windows 要有 Git for Windows；开着 API key helper、agent teams、OpenTelemetry、`DISABLE_TELEMETRY` 都起不来。

### 2.4 「什么情况下退回普通 tool calling」

**文档没有这个机制。** 回退粒度是**整个 session**：不支持就不进入 Code Mode（会话顶部一行原因，`/status` 也能看，共 12 条原因表），而不是跑到一半降级；反过来，没开 Code Mode 起的会话以后也只能以普通 tool calling 续。**推断**：这意味着 Code Mode 在文档里是"会话的一种形态"，不是一个可以逐轮切换的执行策略——对我们这种想"贵的时候才用"的用法，它没有提供开关。

### 2.5 对模型能力的要求

**文档只给了白名单**（Opus 5.5 / Sonnet 5.5 / Haiku 5.5 / Fable 5.2），没给任何能力说明或基准。**推断**：门槛卡在这一档不是随意的——把"多步工具编排"从自然语言决策变成写程序，比"从工具列表里挑一个"高一档；反过来，能力不够的模型写出的 JS 会以"语法对、逻辑错"的方式失败，比 tool calling 的失败更难诊断（我们要自己造诊断）。

---

## 三、Code Mode 能不能解决我们的 `max_tool_calls=8`

### 3.1 先把事实对齐（有一处与你给的硬约束不同）

`RunBudget` 现在的默认值是 **模型 100 次 / 工具 8 次 / 600 秒**，不是 80：

- `contracts.py:109-121` `_default_max_model_calls()` 返回 `初次生成 33 次 × 3 → 取整 100`；`contracts.py:164-166` 的默认值就是它，旁边注释写明了"这个数比旧的 80 高，但高的理由不是猜着不够"。
- `max_tool_calls: int = Field(default=8, ge=1, le=50)`（`contracts.py:166`）没变。

更关键的是：**咬过你的那两次，都已经被结构性关掉了，不是靠调大数字。**

- 「连查 8 次 `rag_query`、一次生成都没调」→ 带材料的 `create` 现在**不注册** `rag_query`（`_ALLOWED_BY_INTENT["create"]` 只有两个生成工具，`tools.py:4930-4934`；补 `rag_query` 那一步只对 `expand` / `edit` 生效，`tools.py:5077-5078`）。**一次 run 就是 1 次工具调用。**

  但这条没完全关死：`tools.py:5079-5080` 还有一条——「请求不带来源、但知识库非空」时**任何意图**都补 `rag_query`。也就是说"库里有 200 份材料、用户只输了一句主题"这条路上，模型仍然能连查 8 次。`max_tool_calls=8` 正是这条路的闸。
- 「80 次模型调用在第 6/7 层烧光、下面整片留空」→ 初次生成锁到 4 层（`tools.py:200-226`，`INITIAL_MAX_DEPTH = 3` 即含根 4 层），真实调用 33 次，默认 100 有 3 倍余量。

所以现在的活约束不是工具次数，是 `max_model_calls`；而它数的**主要是工具内部的调用**：`contracts.py:127-130` 写明"循环里每一轮 provider 调用、以及工具内部的每一次 `chat_async` 都算一次，共用同一个账本"。

### 3.2 收益面：它压的是外层往返，而我们的钱花在里面

一次完整初次生成 33 次模型调用里，**31 次在工具内部**（骨架 1 + 展开 6 个分支 + 展开 18 个二级 = 25，加每支最多一次补点 6），**外层只有 2 次**（发起 / 收尾）。Code Mode 只能动那 2 次，而且这 2 次本来就已经是最小值。

工具次数同理：账本上 `tool_calls += 1` 是"模型发来一个 tool_call"（`service.py:579`），Code Mode 语义下会变成"跑一段代码"。但正常路径离 8 很远：带材料的 create 是 1 个（两个生成工具二选一），`expand` / `edit` 带来源时是 2 个（`node_expand`+`rag_query` / `mindmap_edit`+`rag_query`）。**唯一能逼近 8 的是 3.1 第二条那个缺口**（库非空、请求不带来源的 create），而那正是需要闸的地方。

**结论：解决不了任何现存问题。** Code Mode 解决的是"模型需要连着调几十次工具、且需要先筛输出再读"的场景（跨多文件改、原始检索结果太长）。我们三天的实测做的恰恰是**相反且更彻底**的动作：不是"让模型写代码去过滤工具输出"，而是"工具内部做完检索、只把结构化结果给模型"（`tools.py:2662`、`4169`、`test_deep_slicing.py:7` 都在钉这条规矩）。同一个问题，我们选了"不给模型看原始输出"，它选了"让模型自己写代码筛"。前者不需要多一个运行时。

### 3.3 代价（四条落到我们身上，不是泛泛而谈）

- **安全边界：前提本身要纠正。** 我们这 5 个工具没有一个写磁盘、跑测试、调外部 API：`rag_query` 只读检索，`mindmap_generate` / `mindmap_generate_deep` / `node_expand` 只写 `ctx.candidate`，`mindmap_edit` 只改内存里的 draft op（失败时候选原样不动，`test_agent_tools.py:1500-1509`）。真正的风险不是"代码能删文件"，而是"代码能重复触发生成"——而这个仍然被 `max_model_calls` 拦住，因为工具内部的 `chat_async` 与循环共用 `RunContext` 账本。**推断**：所以安全不是阻碍，阻碍在下面三条。
- **可观测性：这是最贵的一条。** 现在是 `tool_start` / `tool_end` 逐次配对、`node_grow` 增量上屏（`service.py:574-598`、`_run_tool` 的 `emit` 通道 `service.py:356-401`）；实测首帧 2.5 秒出现、`tool_end` 在 25.7 秒，等于提前 23 秒开始上屏（`docs/GENERATION-PIPELINE-NOTES.md:90`）。改成"一次 REPL 执行"之后，这些帧要么由 JS 执行器转发内层事件（新造一条通道 + 扩 `docs/AGENT_STREAM_PROTOCOL.md` 的帧契约），要么前端退化成一个 25 秒不动的卡片。文档的解法是"会话里显示 tool call、代码折叠"，那是有编辑器 UI 的地方才成立的答案——我们要自己造一遍。
- **错误处理：文档没写，全要新定契约。** 我们的工具是事务性的（失败不动候选），所以"回滚一步"不是难点；难点在语义：内层某个调用被 deny 规则拒了，JS 收到的是异常还是返回值？`Promise.all` 里三个调用失败两个、最后一行返回什么？`PreToolUse` 超时按文档是**整次调用被拒**，这在我们 600 秒预算里怎么记？这些每一条都是新契约，不是"复用现成的"。
- **2C2G：多一层 JS 运行时。** Node 子进程常驻 40–90MB RSS + 冷启动；QuickJS 之类嵌入式省内存但要自己补超时、内存上限、以及没有 Node 生态（`` $`cmd` `` 这种 shell 模板就没了）。而换来的账面收益是"上限 8 变成不设上限，但实际只用 1 到 2"。

### 3.4 判断：不该做

**8 不是容量，是闸。** 它和 `max_repairs` 同性质（`contracts.py:136-139`："按轮重置等于给模型无限次重试，每次重试都照样计费"），存在的目的是拦住跑飞的循环——而它上次咬人，正是因为模型真的在跑飞（连查 8 次）。

**推断**：Code Mode 会把这道闸从"每次调用"移到"每次执行"里面。于是只剩两种选择——要么你重新数内层调用（那就是没省，还多了个运行时），要么你不数（那就没有闸，一个 `for` 循环能把 `mindmap_generate_deep` 打十次，每次 31 次模型调用）。**它不是在同样的边界下把 8 变成 N，它是把计量单位挪走。**

真要放宽，直接改 `max_tool_calls` 这个数（现在 `le=50`）比换执行模型便宜一百倍——但我们没有理由改，因为没有任何一条真实路径接近它。

---

## 四、五个原语，逐条判断

### 4.1 Event inbox：worker 不要，那一行要

**现状（代码依据）**：`POST /api/agent/runs/stream` 直接把生成器接到 `StreamingResponse`（`router.py:94-112`）；`AgentRunRequest` 没有 run id / 幂等键字段，而且 `extra="forbid"`（`contracts.py:415-434`）—— 服务端**没有能力**识别"这是同一次点击的第二次投递"。前端只有一个 `AbortController`（`mindmap.ts:704`、`1478-1481`），新 run 会 abort 旧的，**但这只在同一个标签页里成立**。

**所以重复提交会怎样**：换标签页、刷新后重发、代理/客户端重试 → 两个独立 run → 两倍钱 → 两个候选。接受时走 `create_map`，每次 `uuid4()` 新 id（`repository.py:62-67`），所以两个标签页各点一次应用 = 两张图。

**我们已有的等价物只有两层，都不在请求层**：run 内 `ctx.dedupe`（call_id 幂等 + 参数指纹去重，`service.py:404-441`，注释写着实测模型会并行发两个 `node_expand(n1)`）；保存层 `update_map` 的 version 乐观锁（`repository.py:70-78`，过版返回 `conflict`）。文章说 inbox 要按来源 ID 去重、要有 status —— 我们这两层恰好证明了"我们懂这个道理，只是把它用在了错误的层"。

**判断**：handler/worker/批量合并/单活跃 worker 全不要。事件源只有用户一个人，一次点击一个事件，没有并发抢同一件事的问题，也不需要 ack 快（浏览器不看 200）。要的只是**一行 run 记录 + 唯一键**。

### 4.2 用户关页面会怎样：这是真问题，但不是常驻的理由

abort → uvicorn 关生成器 → `aclosing` 触发 `task.cancel()`（`service.py:589-598`、`393-401`），工具里的模型请求被取消；已经花掉的调用不回滚，`ctx.candidate` 只活在内存里，**不落库**（`docs/AGENT_PLAN.md:79`：「结果放 ctx 并发 snapshot；不落库」）。所以"钱花了、图没了"是当前设计的直接后果。

**这不构成"要上常驻"的理由。** 20–70 秒的活，用户中途取消是他的权利；真正的损失是**取消之后产物被一起扔掉**。文章那套（timers/scheduler/worker）解决的是"用户不在场时也要继续干活"，而我们的场景是"用户在等、但他走了"——这两件事的解法差一个数量级：前者要常驻进程，后者只要在 run 结束时把 `candidate` 落一次草稿。

### 4.3 Outbox：我们没有这个决策点

SSE 本身就是即时投递：一次请求、一条流、一个屏幕前的用户。文章里 outbox 的前提是"run 在后台跑完，用户不在，所以要决定说不说、什么时候说"——我们没有那一刻。唯一可借的是文章那句"宁可从安静开始、慢慢赢得用户的注意力"，映射到我们身上是**呈现噪音**：`node_grow` 25 秒 106 帧（`GENERATION-PIPELINE-NOTES.md:90`）。那是"怎么呈现"，不是 outbox。

**判断：不学。**

### 4.4 Memory：差别重要，而且方向相反

文章的 memory 是 **agent 自己写的、跨 run 读的、关于用户与任务的状态**（用户指令、用户笔记、过往消息、任务进度，且原则是"只存查不回来的，其余存指针"）。我们的知识库是 **用户给的、被检索的、关于世界的内容**（`library.py:98-121` 的 `sources` / `chunks` 两张表 + 图谱表）。前者缺失导致的具体行为差只有一句能说清：**没有任何一处能回答"上次那张图在第 4 支断了，从哪继续"。**

但补齐它需要的不是"memory store"，而是 run 身份 + 会话 transcript + 可恢复的中断点 —— 文章这条路一直走到 deferred tool call 才闭环（run 结束、session 存盘、用户答复作为新事件恢复）。我们是"点一下、等 40 秒、拿一个候选"，那套东西的每一件都比我们的问题重。

**判断**：概念上学（任务进度确实没有），机制上不建 memory store。因为在我们这个尺度上，**进度就是候选树本身**——把中间产物落盘一次，比写"进度文件 + 索引 + dreaming"便宜两个数量级。

### 4.5 Permissions / 候选确认：算，而且比 deferred 更简单

算一层，理由不是"我们也有 UI"，而是**我们的工具没有不可撤销的副作用**（见 3.3 第一条）。真正需要批准的只有一个动作：**落库**（用户点应用 → `create_map` / `update_map`），它已经在界面上。

和 deferred tool call 的差别值得写清：deferred 是**运行中**停下来问、run 结束、session 存成半句、用户答复作为新事件恢复；我们是**运行后**整体确认、run 已经结束、结果在内存。我们的更简单，也更脆——脆的地方正是 4.2 那一条（内存里的结果会被连接带走）。

**判断**：不学 deferred。但"为什么我们不需要 permissions 层"这条要写进文档，否则下一个人会照着文章再加一层。

---

## 五、如果只学一件事

**把一次 run 变成数据库里的一行：`id`（幂等键）+ `status` + 产物。**

理由是这两处痛点是同一个缺口的两个症状：run 只活在一个 HTTP 连接的寿命里。重复点击生两张图，是因为 run 没有身份；取消即丢弃，是因为 run 没有超出连接的存储。

而且这一行是文章全部五件东西的挂载点：inbox 就是这行本身，timers 是这行上的到期时间，memory 是这行的进展，outbox 是这行的出口，permissions（deferred）是这行在中途挂起。**学到这一行，其余四件在需要时长出来；跳过这一行去学任何一件，都是给一个没有身份的对象加状态。**

具体到一个可以落地的改动：前端每次点击生成一个 `run_id`，`AgentRunRequest` 加一个字段（现在 `extra="forbid"` 会挡掉任何多余字段，所以这是显式改动），`runs` 表 `UNIQUE(run_id)`，`status ∈ running/done/cancelled/failed`；SSE 保持不变，但它从"run 本身"降级成"这行的观察窗口"；run 结束（含被取消）时把 `candidate` 写进这行当草稿。改动量是一张表 + 一个字段 + 一次落盘。

**不学另外四件的理由：**

- **Timers**：我们没有任何"该主动醒来"的语义。唯一的候选用途是"失败自动重试"，但那是**替用户再花一次钱**，而不是节省他的时间；代价是 2C2G 上多一个常驻轮询单元。
- **Event inbox 的 handler/worker**：事件源只有用户，一次点击一个事件，不需要批量合并、不需要抢锁、不需要"崩溃后重放"。我们需要的那部分（id、status）已经在"学的那一件"里。
- **Memory**：知识库已经占住了"跨请求可查"这个位置，但它存世界不存任务；任务进度在我们的尺度上就是候选树，落一次盘即可。
- **Permissions（deferred）**：候选确认在更外层做了同一件事，而我们的工具没有不可撤销副作用——deferred 要解决的"运行中挂起、存盘、新事件恢复"，在我们这里没有对应的风险。
- **Outbox**：SSE 已经是即时投递，我们没有"后台算完再决定要不要说"的时刻。

---

## 六、一句话判断

**价值**：这两篇把"一个 run 是什么"讲清楚了 —— 它不是一次 HTTP 请求，是一段有身份、有状态、有产物、可以醒很多次的工作单元。我们最贵的 20–70 秒恰好被当成了一次请求，所以连接一断就什么都没了。

**最大的陷阱**：把"我们缺的东西"读成"我们要建的架构"。这两篇描述的都是"agent 长时间不在场"的世界（常驻、多事件源、无人值守、按小时计的后台代码、五个模型档位的白名单）；我们是"用户点一下、等 40 秒、盯着屏幕"。照着它们补 inbox + timers + memory + outbox，会在 2C2G 上多出四个常驻/半常驻部件，换来的能力是"用户不在时也能干活"——而我们的用户不在时不需要干活，他需要的是"我关掉页面那 40 秒别白花"。

**第二个陷阱（Code Mode 侧）**：把 `max_tool_calls=8` 当成容量问题去解。它是一道防跑飞的闸（与 `max_repairs` 同性质），而它咬人的那两次已经被"create 不给 `rag_query`"和"初次锁 4 层"结构性关掉了。用 Code Mode 去换更大的闸，等于把闸挪进 JS：要么你重新数内层调用（没省），要么你不数（没闸）。
