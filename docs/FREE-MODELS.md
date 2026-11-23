# 免费的模型档：为什么是动态的，以及实测到的东西

> 起于 m16558：免费额度不能写死模型名。Command Code 的 coding plan 里标 `:free`
> 的那些模型在活动期间不收费，但**名单不是常驻的**——平台会新上架免费模型，也会把
> 已有的下架，下架之后再用那个名字只会 400。所以选谁是运行时现问的。

代码只有两个文件：`backend/app/generation/free_models.py`（发现与排序）与
`backend/app/settings/store.py` 里那段免费档替换（决定"这一次用不用免费档"）。
开关是 `MINDMAP_FREE_TIER`，**默认关**。

## 1. 为什么默认关

打开它等于改变"这一次生成花谁的钱"。已有部署把密钥放在环境变量里、跑的是付费模型，
升级之后如果默认改走免费档，行为会静默变成**又慢又受每日次数限制**——而界面上只会
出现一个不认识的模型名。所以：

- 关着的时候，请求体与加这个功能之前**逐字节相同**（没有 `reasoning_effort` 这个键，
  `max_tokens` 也不会被抬高）；
- 打开之后，只影响**借用部署方配置的人**（自己没填密钥的那些）。

## 2. API 事实（2026-11-23 实测）

| 事实 | 值 |
| --- | --- |
| 模型列表 | `GET https://api.commandcode.ai/provider/v1/models` |
| 要不要鉴权 | **不需要也返回 200**（无 token 与带 token 的 105 个 id 完全一致）；但 `/chat/completions` 不带 key 是 401 `UNAUTHORIZED` |
| 一次请求耗时 | **22.1 秒 / 21.7 秒**（两次实测） |
| 返回形状 | `{"object":"list","data":[{id, object, created, owned_by, name, context_length, supported_endpoints}]}` |
| 免费标识 | id **以 `:free` 结尾**（用 `endswith`，不用 `in`——`x:free-trial` 可能是计费档） |
| 实测免费模型 | `recursive/flint-v1.0:free`（262K）、`stealth/glyph-cluster:free`（256K），文档标 `FREE`、各 1000 请求/天 |
| 名字必须精确 | 不带 `owner/` 前缀的 `flint-v1.0:free` 直接 400 `unsupported_model` |
| `created` 字段 | **每次请求都变**（服务端按请求时间生成），所以不能用列表内容做哈希判断有没有更新 |
| 流里的 `model` | 只有最后一帧才带 `:free` 后缀（前面是 `stealth/glyph-cluster`）——不能用它判断是否免费 |
| tool calling | 两个免费模型都支持，`finish_reason: tool_calls` |
| 吞吐 | 约 24.6 tok/s（含 reasoning） |

`supported_endpoints` 里必须带 `/chat/completions` 才算可用：105 个模型里有 12 个只
支持 `/messages`（Anthropic 格式）。如果哪天某个免费模型只挂在那条路上，选它出来会
让每次调用都 400，而报错文案（"模型服务返回 HTTP 400"）完全看不出该换模型。

## 3. 三条实测约束 → 三个设计决定

### 约束一：列表要 20 秒以上

**决定：后台线程 + 1 小时 TTL 缓存，列表永远不在请求路径上。**

`free_models()` **永不阻塞**：

- 缓存新鲜 → 直接返回；
- 缓存过期 → 起一个后台 daemon 线程刷新，**这一秒仍然返回旧列表**；
- 一次都没拉到过 → 返回空列表，同时起线程。

第一次调用返回空是**有意**的：为了第一次请求能立刻出结果，宁可这一次报"免费档还没
准备好"（用户点一下重试就好，后台已经拉好了），也不要让每个第一次访问的人都盯着
"正在连接"等 22 秒。

TTL 取 1 小时：上下架不是分钟级事件，而每次刷新要占上游 20 秒。比这更短只增加上游
压力，更长则让新上架的免费模型一整天用不上。

**一个只在失败路径上出现的坑**：刷新失败时必须把 `_REFRESHING` 这个"正在刷新"标记
放掉。只放成功那一侧的话，一次失败会让这个端点**一小时内再也不会重试**
（`_start_refresh` 看到它在集合里就直接返回），表现是"免费档时好时坏、重启之后又
好了"——最像玄学的那种。`test_a_failed_refresh_keeps_the_old_list_and_warns` 钉着它。

### 约束二：`:free` 是唯一标识，且名单会变

**决定：一个模型名都不硬编码，每次刷新都现问。**

排序是"**上下文大的优先，同大按 id 升序**"。上下文优先是因为免费档的常见用途是
"让没有密钥的人也能看到一张完整的图"，上下文越大越不容易在中途撞上窗口上限（那是
一次跑到一半才失败的生成）。id 升序是**确定性**：没有它，同一天里前一次用 flint、
后一次用 glyph，用户会觉得功能时好时坏。

### 约束三：上游偶发 429

**决定：给出候选序列，一次运行内逐个试。**

实测同一时刻同一 key 连发三次也可能全 200，也会撞上
`{"error":{"message":"Upstream model provider is temporarily unavailable...", "type":"rate_limit_error"}}`。
所以 `candidates(base, tried)` 返回"本次还能试的"，调用方自己记着试过谁——**这个状态
不能放模块级**：那是每请求的东西，放模块级会让并发请求互相污染（A 试过 flint，B 就
永远拿不到 flint）。

"该换下一个"的判据只有一份：`generation/service.py` 的
`UNAVAILABLE_STATUSES = {429, 502, 503, 504}` 与 `_ModelUnavailable`。**400 不在里面**
——它几乎总是"这个请求本身不对"（模型名不支持、参数越界），换模型重发只会把同一个
400 再来一次、还多花一次额度。

两条模型出口都接了这个候选序列，因为它们都得接：

- `backend/app/agent/provider.py::complete_tools`（外层工具循环，每轮一次）；
- `backend/app/generation/service.py::chat_async`（工具内部：骨架 / 结构 / 说明）。

只接一条会得到"agent 那轮换了、写内容那次没换"——AGENTS.md 那条"改一个能力的入口
要检查所有会走到它的路径"讲的就是这个。

## 4. `reasoning_effort`：不给就会白等

两个免费模型**不支持 `off`**（400：`does not support reasoning_effort "off"`），但支持
`low`。不传这个参数时它们的思考会吃掉整个输出预算：

| 配置 | 结果 |
| --- | --- |
| 不传 `reasoning_effort`，`max_tokens=1400` | **56.9 秒**、`completion_tokens=1400`、其中 `reasoning_tokens=1311`、**正文空串**、`finish_reason=length` |
| `reasoning_effort="low"`，带 tools | 8.9 秒、`comp=385 reasoning=316`、`finish=tool_calls` |
| 不传 `reasoning_effort`，`max_tokens=4000` | 正常，`comp=67 reasoning=14 finish=stop` |

所以走免费档时两件事一起做：发 `reasoning_effort: "low"`，并且给 `max_tokens` 一个
**下限** `FREE_MIN_OUTPUT_TOKENS = 4000`（调用方要得更少也抬到 4000）。抬完之后仍要
过 `max_tokens_cap()`——那是服务端真实上限，不能为了凑够 4000 发一个一定会 400 的值。

注意这也是"为什么免费档慢"的一半原因：24.6 tok/s 的吞吐加上每个节点都要写说明，
一张四十节点的图要几分钟。部署方想快就用自己那行填密钥，那样根本不进这一档。

## 5. 免费模型也要密钥

实测不带 key 请求 `/chat/completions` 是 401 `UNAUTHORIZED`。所以这一档的语义是
**"用部署方那把钥匙、走不收费的模型"**，而不是"不用密钥"。直接后果：

- `store.resolve()` 只在 `api_key_source` 落在 `instance` / `env` 上时才换模型名；
- `api_key_source: "saved"`（用户自己填了密钥）**不换**——那是他的钱，与免费档无关；
- `api_key_source` 是 `unset` / `unbound` 时也**不换**——连端点或密钥都没有，此刻该
  报的是"去配模型"，不是"换一个免费模型试试"。

## 6. 降级要留下痕迹

- 拉不到列表 → 保留旧列表 + `WARNING`（有旧列表时说"继续用 N 分钟前的列表"，
  没有时说"暂按没有免费档处理"）；
- 拉到了但**一个免费模型都没有** → 也 `WARNING`。免费活动结束后界面上"用免费档
  生成"会一直失败，日志里没有"当前 0 个免费模型"这句话会让排查从"查日志"变成"猜"；
- 候选全试完 → 抛**最后一个真实原因**（"模型服务返回 HTTP 503"），不要包装成
  "没有候选"：用户看到的文案该说清这一轮到底发生了什么。

## 7. 怎么打开、怎么验证

```bash
# backend/.env 或 /etc/genai-mindmap/env
MINDMAP_FREE_TIER=1
```

打开之后第一次生成会报"免费档还没准备好"（后台在拉列表），隔一分钟再点一次就好。
日志里能看到的判断依据：

```
免费模型列表已刷新：recursive/flint-v1.0:free, stealth/glyph-cluster:free
模型 xxx 暂时不可用，换下一个候选：模型服务返回 HTTP 429
```

面板上"模型"那一栏的来源标签会显示**免费模型**（`model_source: "free"`），而不是
`本站共享配置`——模型名是运行时选的，不是部署方在面板里填的那个。

## 8. 这一批没做的事

- **3 次免费额度还没接**：这一批只做了"怎么选免费模型"，没有任何次数限制。任何
  匿名访问者现在都能用部署方那把 key 走免费档（受上游每天 1000 次的账户级上限约束）。
  额度（同一机器/IP 的游客与账号共享 3 次池）是下一批。
- **不做主动预热**：进程启动时不去拉列表（那会让启动多 22 秒，且大多数部署不开这个
  开关）。
- **不支持每用户不同的免费名单**：名单是"部署方那台端点"的事实，不是每人的。
