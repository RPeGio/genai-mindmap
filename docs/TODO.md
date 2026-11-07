# 待办与文件占用（RPeGio 侧）

**这个文件解决什么问题**：两个人同时改同一个文件，冲突只能靠手工解。
2026-11-07 合并上游 19 个提交时，唯一冲突就落在 `frontend/src/stores/mindmap.ts`——双方各加字段、语义都没错，纯粹撞在一起。
所以先约定：**我要动的文件在这里列出来，你近期要动同一批文件时先说一声**，反过来我也照做。

- 最后更新：2026-11-08（对应 `feat/ref-index` 分支起点 `989c2e8`）
- 当前基线：前端 343 个 vitest（20 个文件）、后端 487 个 pytest、CI 双 job 全绿

## 1. 我这一轮要做的（按提交顺序）

| # | 事项 | 主要文件 | 是否动后端 |
|---|---|---|---|
| 1 | 撤销栈改按字节封顶（10MB），节点上限提到 1000 后仍可控 | `frontend/src/stores/mindmap.ts`、`frontend/tests/history.spec.ts` | 否 |
| 2 | 来源反向索引：算出"每个来源被哪些节点引用"（纯函数） | `frontend/src/mindmap/refs.ts`、`frontend/tests/refs.spec.ts` | 否 |
| 3 | store 接反向索引：`refIndex`、`locate(id)`（选中并让画布居中，用 `focusTick` 计数器而非监听 `selectedId`） | `frontend/src/stores/mindmap.ts`、`frontend/src/canvas/MindMapCanvas.vue` | 否 |
| 4 | 左侧来源行显示"被 N 个节点引用"，点开是内联节点列表；未被引用的来源标 amber | `frontend/src/components/SourceInputs.vue` | 否 |
| 5 | 右侧"生成时用到的来源"摘要补计数 | `frontend/src/App.vue` | 否 |
| 6 | 文档同步（功能清单、接线表、已知坑） | `docs/FEATURES_AND_OWNERS.md`、`docs/FLOW.md`、`TEAM-GUIDE.md`、`docs/PLAN.md` | 否 |

第 1 项已提交：`1175696 feat(web): 撤销栈加 10MB 字节上限`。

## 2. 请你尽量别同时改的文件

按冲突风险从高到低：

1. `frontend/src/stores/mindmap.ts`——本轮改动最集中（`refIndex`/`locate`/`focusTick`/历史栈）。你最近一次 `node_grow` 预览也在这个文件加字段，**如果这周还要动它，提前告诉我，我等你合完再动**。
2. `frontend/src/App.vue`——我只改"生成时用到的来源"那一段摘要。
3. `frontend/src/canvas/MindMapCanvas.vue`——只加一个 `focusTick` 的居中效果，不碰节点生长与预览逻辑。
4. `frontend/src/components/SourceInputs.vue`——加计数与内联列表，不改上传/录音的交互。

反过来说，**这些地方我不会碰**，你随时可以改：`backend/` 全部、`frontend/src/agent/`、`frontend/src/stores/sources.ts` 的上传与泳道逻辑、`frontend/src/canvas/MindNode.vue` 的节点内编辑与预览态。

## 3. 关于你最近这批提交，我这边怎么接

- **节点上限从 300 提到 1000**：我的撤销栈原来按"50 步 × 整树快照"算，1000 节点时单份快照就能到几十 KB，50 步最坏几十 MB——所以第 1 项改成按字节封顶，同时保留至少一步。
- **画布上直接生长节点（预览树）**：`store.nodeCount` 现在是"画布可见节点数"，含候选/预览树。所以反向索引一律只从 `store.content.root` 算，不数画布上的节点，免得把候选节点算进引用统计。
- **`store.select()` 只接受 content 里真实存在的 id**：这让"点引用跳到节点"天然安全，预览节点点不进去，不用额外判断。
- **`MindNodeData.refs` 改成了可选**：你新加的预览测试对象不带它，编译期因此报过 `TS18048`；现在渲染端用 `(data.refs ?? 0)`，你造测试数据时不必补这个字段。
- **导出会折正文**：`frontend/src/api/client.ts` 的 `toExportV1()` 把 `note` 折进 `label` 再发（`导出前`），保存仍走 `toContractV1()` 裁掉 `note`。这两种口径都在前端，后端不需要区分。

## 4. 需要你确认的两件事（详见 `docs/HANDOFF.md`）

- **H1**：节点正文复用契约 v2 已有的 `Node.note`，`label` 保持短标题、不收紧 200 字上限；`note` **与 v2 同批上线**（v1 是 `extra="forbid"`，提前发会 422）。前端已按"有 `note` 用 `note`、没有回退 `label`"实现，并在保存/导出时裁掉或折叠它。
- **H2**：保存以整棵树为单位（`POST` 建图、`PUT` 带 `version`），不做字段级合并；前端复刻一份同样的上限校验（只为省一次 422，不替代后端校验），文案与后端一致。

这两条都等你回一句"同意"或"要改"，我在 `docs/HANDOFF.md` 里记 PR 号或提交号结案。

## 5. 之后排队的（不急，先占个位）

- 结构诊断面板（F15）：把 `POST /api/optimization/check` 的结果画到画布上（现在后端能诊断，前端没接）。
- 语音句级时间戳、页码级跳转（F08 剩下的部分）。
- 容器化部署骨架（F22）：Dockerfile / compose / nginx 配置，仓库里目前还没有。
- 契约 v2 落地：`layout` / `theme` / `relations` 三个字段，等画布与协作稳定后再定稿。
