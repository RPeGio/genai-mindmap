# 改动日志：先让"用户最常改什么"变成事实

这一版**只记录、不总结**。

用户的完整设想是"记录修改 → 总结 → 喂给训练模型 → 个性化"。后面几步（DPO 微调）
需要几百条偏好对，而一天几次的生成攒够要几个月。但第 1 步值得马上做，因为很可能的
结论是：改动集中在一两个具体问题上（"80% 的改动是把长标题改短"），那种用规则一天
就能修，不需要训练。

所以这一版的唯一目标：**能回答"用户最常改什么"**。

不做的事，逐条都有理由：不做 LLM 总结（实测同一主题两分钟内出现过两个相反要求，
自动总结会写出互相矛盾的条目）；不做训练数据导出（等有几百条再说）；不做实时上报
（攒到保存）；不做 `sendBeacon` 兜底——"不保存就关页面 → 事件丢失"是可接受的
（那次编辑本来也没保存），用半截方案假装能挽回只会让丢失变得更难查。

---

## 一、diff 怎么算才算有意义

实现在 `frontend/src/mindmap/edit-log.ts`（纯函数）与 `frontend/src/stores/mindmap.ts`
（`pushHistory` / `collectEdits`）。分三份比：节点字段、树结构、边。

三条判断，都直接影响统计怎么读：

**一个字段一条事件。** 改一个节点的 `label` 同时改 `color` 会产生 2 条，不是 1 条。
合并成一条的话，`by_field` 就必须把一条事件同时算进两个桶，`sum(by_field)` 与
`by_kind.update + by_kind.style` 对不上——统计一旦不能自校验，读的人得先怀疑数字再
怀疑结论。代价是"一次操作产生几条事件"只留在 `at` 里：同一次提交内所有事件共享
同一个时刻字符串，`GROUP BY at` 能把它还原成一次操作。

**id 变了就是 remove + add，不做相似度匹配。** 节点 id 由 `newId()`（`n_` + 随机）
或后端分配，"删掉再加一个同名节点"必然是新 id。按 label 或位置去猜"这其实是同一次
修改"会把统计变成一件需要解释算法的事，而它现在只需要读 SQL。

**顺序变化只在"这一层成员没变"时才算 move。** 单纯比 sibling index 会炸：一次
"在 50 个兄弟中间插一个"会让后面每个兄弟的 index 都 +1，于是 1 次真实操作变成 50 条
move。所以 index 变化只有在**该父节点的子 id 集合完全没变**时才上报——那才是重排
（agent 的 `move` op 带 index 就会走到这里）。

## 二、`EditEvent` 的形状

```ts
type EditEvent = {
  actor: 'user' | 'agent'
  kind: 'add' | 'remove' | 'update' | 'move' | 'edge' | 'style'
  nodeId?: string          // edge 事件填源节点；title 事件没有节点
  field?: EditField        // 只在 update / style 上有值
  before?: string          // 摘要，不是内容
  after?: string
  depth?: number           // 节点层数，根是 1
  runId?: string           // 这次改动动的是哪个 run 的产物
  at: string               // 同一次提交共享同一个值
}
```

`field` 只在 `update` / `style` 上有值，这条规则换来两条恒等式：
`sum(by_field) == by_kind.update + by_kind.style`、`sum(by_actor) == total`。

摘要规则：

| 字段 | 存什么 | 为什么 |
|---|---|---|
| `label` / `title` | 全文（≤200 字） | 统计要算长度，截了就没法算 |
| `detail` / `note` | `"1800字"` | 2000 字正文进日志会把表撑成第二个 `mindmaps.content`，而"从 1800 字改成 700 字"本身就是情报 |
| `color` / `icon` | 原值或 `"无"` | 它是键不是内容 |
| `move` | 父标题或 `"第2位"` | id 对统计没用，标题读得懂 |
| `edge` | `"A → B（是）"` | 边只有 id，人对不上 |
| `depth` | 层数，`remove` 填删除前所在层 | `removed_depth` 直接 `GROUP BY` 它 |

`depth` 是这一版最容易被忽略、又最值钱的一个字段：没有它，"删掉的集中在第几层"
就只能靠事后猜。

## 三、提交时机：并进保存请求，不另开端点

`POST` / `PUT /api/mindmaps` 的请求体里带 `events`（可省略）。

决定性的理由：**第一次保存之前根本没有 `map_id`**。一个
`POST /api/mindmaps/{id}/edits` 要么发不出去，要么必须先 create 再补发——于是多出
"内容存了、事件没存"这种半截状态，而统计从此只看见部分改动，且没有任何地方能察觉。

并进保存之后：两者同一次请求、同一次成败；409 冲突时一起拒，重试一次就都补上，不会
出现"事件已写、内容被拒"的统计漂移。

单次最多 500 条。超出的部分**不是丢弃**，而是留在前端队列里等下一次保存（分批），
只把条数上报成一条服务端 warning——不在表里插一条"我截断了"的假事件，因为 `total`
必须是真实改动数。

事件写失败**不让保存失败**（`edits.record` 内部吞掉并记 warning）。为了让统计写进去
而让用户看到"保存失败"、进而重试撞 409，方向完全错了。

## 四、刻意不记的三件事

| 不记 | 理由 |
|---|---|
| 撤销 / 重做 | 一次被撤销的编辑不是用户的最终意图。记了它，`label_shrunk` 会同时收到"18→9"和"9→18"，平均值被拉回中性、次数却翻倍——比不记更糟。"用户反复撤销"该是另一个指标，这一版不做。 |
| `create` 接受候选 | 整棵树都是"新增"，那不是 diff，是把整棵树重念一遍。与 `diffCandidate` 对 `create` 返回 `null` 同一条判断。 |
| 换朝向 / 换布局 | 它写在 `content` 里，但回答不了"生成侧该改什么"，记下来只是噪音。 |

## 五、局限：不要让日志看起来能回答所有问题

一次真实跑批（20 节点的旧图，用户改 12 条标题、上 3 个色、写 2 条备注、挪 1 个节点、
加 4 个、删 10 个，再让 agent 应用一轮候选）得到：

```
total 38   by_actor {user:32, agent:6}
by_kind {update:20, remove:10, add:4, style:3, move:1}
by_field {label:16, color:3, detail:2, note:2}
label_shrunk {count:13, avg_before:14.4, avg_after:6.0}
removed_depth {3:2, 4:7, 5:1}
```

看起来能直接推导出两条处方：**标题太长了**（13 条缩短，14.4 → 6.0 字）、**生成挖得
太深**（删掉的 10 个里 7 个在第 4 层）。这两条推论**都可能是错的**，而且错法各不相同：

- **缩短 ≠ 太长。** 用户也可能只是在压缩视觉密度，或者这张图本来就要放进一页 PPT。
  工具有个"画布 24 字截断"，用户把 14 字改成 6 字也可能是在给节点框留白。
  要区分这两种，需要**同一个节点上没有其它改动**的样本，以及"缩短后图的总节点数
  变没变"——这一版都没记。
- **删在第 4 层 ≠ 初次太深。** 也可能是因为第 4 层的内容质量最差（模型在深层只会写
  "进一步细化"这种废话），那该改的是提示词而不是深度参数。日志只有 `depth`，没有
  "被删的那句话写了什么"——按这一版的取舍，`label` 存了全文，所以**这一条其实能查**：
  `GET /edits` 里 `kind='remove'` 的 `before` 就是那些标题。这是设计里少有的"事后
  还能补查"的地方。
- **`by_actor` 分不清"改"和"接受"。** `agent` 的 6 条是用户点了「应用」之后一次性
  落地的，它不代表用户认同每一处——用户可能只想改 1 条，接受了 6 条。真正表示
  "用户不满"的信号只有 `user` 那 32 条。
- **样本量。** 38 条来自一张图。一张图的改动分布与"这个人怎么用这个工具"是两件事，
  而这张表只按 `map_id` 存。要回答后者，得跨图聚合（`GROUP BY map_id` 再平均），
  这一版没有这个接口。

一句话：**这张表能告诉你"改动集中在哪"，不能告诉你"为什么"。** 前者已经够砍掉一
大半的猜测（不用再开会讨论"用户到底嫌哪里不好"），后者仍然得靠看具体的那几十条
`before` / `after`——所以 `GET /edits` 明细那条路径不是附属品，它是把分布翻译回
人话的唯一入口。

## 六、代码位置

| 位置 | 内容 |
|---|---|
| `frontend/src/mindmap/edit-log.ts` | 类型 + `diffEdits` 纯函数（三条判断都在这里） |
| `frontend/src/stores/mindmap.ts` | `pendingEdits` / `pushHistory(actor)` / `collectEdits`；`save()` 里分批提交、成功才清 |
| `frontend/src/api/client.ts` | `saveBody`（空数组不发字段）、`getEditSummary`、`listEditEvents` |
| `backend/app/contracts/edits.py` | `EditEventIn` / `EditEventRow` / `EditSummary` |
| `backend/app/mindmaps/edits.py` | `record` / `summary` / `recent` / `clear` |
| `backend/app/mindmaps/repository.py` | `edit_events` 表与索引 |
| `backend/tests/test_edit_events.py` | 23 条：落库、统计、两个关键指标、五个边界 |
| `frontend/tests/edit-log.spec.ts` | 23 条：diff 正确性、actor 区分、只存长度、500 上限、清空时机、撤销不记 |
