---
name: change-map-schema
description: 改导图 JSON 结构时使用——给节点或整图加字段、改字段类型、加节点属性（expandable / style / pos / relations 之类）。当任务涉及 contracts/mindmap.py、ops.py、mindmap/ops.ts、生成的导图缺字段、或旧导图打开报 invalid_tree 时，先读这个。
---

# 改导图 JSON 结构

导图结构有两份实现（Python 与 TypeScript）和三个消费者（保存、agent 生成、画布渲染）。改一个字段要过**八**关，其中第 4 关是唯一能自动发现"两端形状不一致"的地方。

## 1. 契约：`backend/app/contracts/mindmap.py`

`Node`（第 153 行起）与 `MindMapContent`（第 207 行起）。两个类都有 `model_config = ConfigDict(extra="forbid")`——**任何一端多写或漏写字段都直接拒收**，不做静默忽略。

写法照 `expandable` 这一条（`contracts/mindmap.py:182`）：

```python
    # 三态的判定表见模块顶部 NODE_EXPANDABLE 的注释。
    # 缺省取 `unknown` 而不是 `yes`：库里那些 v1/v2 记录从来没被问过这件事，
    # 把没问过的记成"还能展开"会让界面替旧图许下一个它兑现不了的承诺。
    expandable: Literal["yes", "no", "unknown"] = "unknown"
```

三条硬要求：

- **新字段必须可选且有默认值**（见第 7 步）。
- 有长度上限就用 `Field(max_length=...)`，**且上限引用契约常量**（`MAX_NODE_LABEL_CHARS` / `MAX_NODE_DETAIL_CHARS`），不要重新敲数字。
- 归一化用 `@field_validator`，照 `blank_text_as_none`（`:196`）——纯空白折成 `None`，别留空串。

## 2. op 契约：`backend/app/contracts/ops.py`

新字段若**能通过 op 写入**，两个类都要改：

- `AddOp`（`:48`）加字段，照 `detail` 那条（`:65`）——注释里写清"缺省 None 与空值同义"。
- `UpdateChanges`（`:68`）加字段 **`X | None = None`**，并扩展 `at_least_one`（`:84`）的判断。

`UpdateChanges` 的语义是 `None` = 不动、`[]`/空串 = 显式清空。**这个区别丢不得**：模型漏写一个字段就等于把内容全删了。

## 3. 执行器：`backend/app/agent/ops.py`

`_apply_add`（`:207`）与 `_apply_update`（`:233`）各加一处。照 `detail` 的写法：

```python
    if op.changes.detail is not None:      # 用 `is not None`，不要用真值判断
        detail = op.changes.detail.strip()
        node.detail = detail or None
```

两个坑都写在现成注释里，照抄它们的理由：

- **`is not None` 而非真值判断**（`:240`）：写成 `if op.changes.source_refs:`，空列表会被当成"没给"，模型就永远清不掉一条错误引用。
- **`add` 里复制可变对象**（`:226`）：`source_refs=list(op.source_refs)`，不要让树和 op 共享同一个 list。

`remove` / `move` 不用动——它们只搬节点，不碰字段。

## 4. 两端一致：三份东西必须同时改

这一关是整个流程的锚点：

1. `backend/app/agent/ops.py`（后端执行器）
2. `frontend/src/mindmap/ops.ts`（前端同构执行器，`applyOps` 在 `:175`，`normalizeOps` 在 `:334`）
3. **`tests/fixtures/mindmap_ops.json`** ← 仓库根的 `tests/`，不是 `backend/tests/`

第 3 份被两端同时读取：

- `backend/tests/test_ops_contract_sync.py:22`：`parents[2] / "tests" / "fixtures" / "mindmap_ops.json"`
- `frontend/tests/ops-contract.spec.ts`：`import sharedFixture from '../../tests/fixtures/mindmap_ops.json'`

在 `cases` 里加一条用例，形状照现有成员：

```json
{ "name": "update_detail_keeps_note",
  "ops": [{ "op": "update", "id": "n2", "changes": { "detail": "新说明" } }],
  "expect_tree": [["n1", "根"], ["n2", "子"]],
  "expect_details": [null, "新说明"],
  "expect_notes": [null, "用户自己写的备注"] }
```

`test_fixture_covers_both_outcomes`（`:95`）要求 fixture 同时有成功用例、失败用例、`expect_base_unchanged`。别只加成功的那条。

**新字段的 fixture 必须覆盖"改 A 不碰 B"**——`expect_details` / `expect_notes` 这个双序列就是为此存在的。

## 5. 生成侧：让模型能产出这个字段

两处：

1. `backend/app/generation/service.py` —— 提示词里的字段清单与要求。照 `DETAIL_PROMPT`（`:246`）：

```python
    "\"label\":\"节点文字\",\"detail\":\"节点说明\",\"children\":[]}}。"
    "每个节点均含 id、label、detail、children，"
```

要求要**可执行**（"2–3 句，可以带数字与条件"），不要写"写详细一点"。

2. `backend/app/agent/tools.py` —— 批量清洗，防止模型写坏一个值就毁掉整棵树：

- `_slot_from`（`:955`）— 逐项归一化，`_detail_text`（`:1002`）是它的字段专属帮手
- `_strict_label`（`:3828`）/ `_strict_refs`（`:3852`）— 严格路径，写坏就整条拒收

原则：**清洗在生成侧做完，不要让契约兜底**。契约 `Field` 只会整棵拒收，用户拿到的是 `invalid_tree`（`tools.py:3304`），而不是"某一句被截短了"。

## 6. 前端类型：三处

```bash
cd frontend && npm run type-check    # 改了类型就先跑这个
```

1. `frontend/src/api/types.ts` —— 手写镜像，`MindNode`（`:10`）/ `MindMapContent`（`:139`）
2. `frontend/src/api/schema.d.ts` —— 生成物，**不要手改**，走 `add-api-endpoint` 的第 4、5 步
3. `frontend/src/api/contract-check.ts` —— `checks` 里的 `nodeKeys` / `nodeShape`

`contract-check.ts` 的 `SameKeys` 在 `vue-tsc` 期间比对键集合。算法改了但镜像没跟，这里就红——这是历史上 `source_refs`、`note` 被整批丢掉之后加的防线。

## 7. 旧数据兼容（最容易漏）

`Node` 与 `MindMapContent` 都是 `extra="forbid"`，库里存着 v1/v2 记录，`layout=manual` 的旧图带 `pos`、旧图没有 `expandable`。加一个**必填**字段 = 打开旧导图直接失败。

- 新字段一律 `X | None = None` 或有语义的缺省值（`expandable` 取 `unknown` 而不是 `yes`）
- 改字段类型要能读旧值（`test_contract_v2.py` 有一批 `*_gains_defaults` / `*_v1_row_is_readable` 用例在钉这件事）
- 加完补一条"旧 payload 能读出来"的用例，照 `backend/tests/test_contract_v2.py:56` 的 `test_v1_payload_gets_v2_defaults`

## 8. 若字段参与引用校验

`backend/app/optimization/source_refs.py`：`verify_source_refs`（`:302`）遍历节点字段做逐字核对，`_reference_problem`（`:251`）决定引用是否算有效。

### 长度上限必须与引用契约对齐

`LEAF_MAX_CHARS = 300`（`knowledge/service.py:584`）与 `SourceRef.quote` 的 `max_length=300`（`contracts/source.py:281`）是**同一份字数契约**。

它们曾经差 20 字：子块能到 320，而 `quote` 只收 300。后果不是报错，是**静默**的——命中块里那句话填不进引用字段，`evidence_*` 给不出来，引用校验退化成格式检查，而且没有任何测试会红。

所以：**任何新的字数上限，先问它是不是引用链的一段**。是的话，两端一起改，并在 `source_refs.py` 或 `contracts/source.py` 的注释里写清它和谁对齐。

## 自检清单

- [ ] `contracts/mindmap.py` 的 `Node` / `MindMapContent` 都改了，新字段有默认值
- [ ] 能通过 op 写入的话，`contracts/ops.py` 的 `AddOp` / `UpdateChanges` / `at_least_one` 都改了
- [ ] `agent/ops.py` 的 `_apply_add` / `_apply_update` 都改了，用 `is not None` 判空
- [ ] `frontend/src/mindmap/ops.ts` 形状与后端一致
- [ ] `tests/fixtures/mindmap_ops.json` 加了用例，含"改 A 不碰 B"和失败用例
- [ ] `tests/test_ops_contract_sync.py` 与 `frontend/tests/ops-contract.spec.ts` 都绿
- [ ] `generation/service.py` 的提示词要求模型输出该字段（要求可执行，不是"写详细点"）
- [ ] `agent/tools.py` 的 `_slot_from` / `_detail_text`（或对应 `_strict_*`）能清洗它
- [ ] `types.ts` 镜像改了，`contract-check.ts` 的 `SameKeys` / `SameShape` 两行补了
- [ ] 跑过 `npm run gen:api` 与 `npm run type-check`
- [ ] 有一条"旧 payload 仍能读"的用例
- [ ] 字段有字数上限的话，确认它与 `SourceRef.quote` 的 300 对齐（或说明为什么不需要）
- [ ] `python -m pytest -o addopts="" -q` 全绿
