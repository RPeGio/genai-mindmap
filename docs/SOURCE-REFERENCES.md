# 节点来源引用

本次只扩展后端 v1 契约的 `Node.source_refs`，不启用完整 v2 契约。
旧请求和数据库记录可省略此字段，读取时默认为 `[]`；现有 SQLite JSON
存储会自动保存新增字段，无需表结构或数据迁移。

## 引用格式

```json
{
  "id": "n1",
  "label": "发布安排",
  "children": [],
  "source_refs": [
    {"source_id": "meeting", "quote": "下周发布", "start": 4, "end": 8}
  ]
}
```

这里 `Source.text` 为 `会议决定下周发布。`。字符区间使用原始 `Source.text`
上的 Python Unicode 字符索引，从零开始，左闭右开；不是 UTF-8 字节偏移，
也不是 JavaScript UTF-16 代码单元偏移。原文开头的空白、换行都计入索引。

每个节点最多 20 条引用。`source_id` 必填、非空白、最长 100 字；可选
`quote` 最长 300 字，空白引文归为 `null`；可选 `page` 为从 1 开始的整数。
`start` / `end` 要么都省略，要么同时提供非负整数，且 `end > start`。

## 三层校验

| 层次 | 执行位置 | 规则与行为 |
| --- | --- | --- |
| 1：字段 | `SourceRef` / `Node` | 类型、长度、页码、字符区间和数量限制；拒绝未知字段。请求不合法返回 422，模型结构不合法返回 502。 |
| 2：关联 | `MindMapContent` | 每条引用的 ID 必须存在于 `source_usage`；摘要 ID 不得重复。所有接收导图的接口共用此约束。 |
| 3：原文 | `verify_source_refs` | 引文必须逐字出现在指定来源中；带区间时检查边界和非空白内容，若同时有引文则必须与该区间完全一致。 |

第三层不做模糊匹配或跨来源拼接，也不证明节点文字被引文语义支持。
只有来源 ID / 页码、没有引文或字符区间的引用标记为 `UNVERIFIED_SOURCE_REF`。
没有可核验引用的节点标记为 `UNSOURCED_NODE`，不自动删除节点或建议删除。

## 可选的第四层：NLI 语义打分

前三层都是机械判据，它们管不住一种情况：引文逐字对得上，但节点论断与原文
**语义相反**（原文「只在小数据集上验证」，节点写成「已在大规模数据上验证」）。

`app/optimization/nli.py` 用一个小型 ONNX cross-encoder 对「节点标签 ↔ 引文」
再判一次语义蕴含，**只产出诊断，不改变任何引用**：

| 判定 | 诊断 |
| --- | --- |
| contradiction | `SOURCE_REF_CONTRADICTED`（warn），引用保留 |
| entailment / neutral | `SOURCE_REF_NLI`（info），附置信度 |

设计约束：概率模型不得推翻确定性判据。冲突时引用留着、同时给出警告，
由人判断。没有 `quote` 也没有字符区间的引用直接跳过——拿整篇原文去比一个
节点标签，NLI 只会给 neutral。

**默认关闭**（`MINDMAP_NLI_ENABLED` 未设即关）。关闭时连 `onnxruntime`
都不会 import，`annotate_nli` 立刻返回空列表。启用需要
`MINDMAP_NLI_MODEL_DIR` 指向含 `model.onnx` / `tokenizer.json` / `config.json`
的目录。加载前有内存闸门（默认 700 MB，Linux 可检测）。

实测参考：

| 模型 | 文件 | 内存增量 | 加载 | 单次推理 | 中文准确率 |
|---|---|---|---|---|---|
| `typeform/mobilebert-uncased-mnli` int8 | 25.7 MB | +83.5 MB | 9.9 s | 11–23 ms | 0/4 |
| `IDEA-CCNL/Erlangshen-Roberta-110M-NLI` int8 | 98.3 MB | +140.3 MB | 1.4 s | 15–44 ms | 6/6 |

**英文模型对中文完全失效**，不是「偏弱」：它对「完全不能识别图像」vs「本系统可以识别图像」给 `entailment 0.52`，对「数据库采用主从架构」这种无关内容给 `entailment 0.65`——四条中文用例全判成蕴含，说明它只在数词汇重叠，没有识别否定结构。中文场景必须用中文模型。

`id2label` 顺序各模型不同（mobilebert 是 `0=ENTAILMENT / 2=CONTRADICTION`，Erlangshen 是 `0=CONTRADICTION / 2=ENTAILMENT`），代码从 `config.json` 读，写死会整体错位。

## 各接口的行为

`SourceSpan.page` 是文档总页数，没有逐页文本映射。超出总页数或指向解析失败
页面的引用标记为 `INVALID_SOURCE_PAGE`；其余页码仍标记为
`SOURCE_PAGE_UNVERIFIED`，即使引文已在全文匹配。模型提示词要求只返回来源 ID
与逐字引文，不猜测页码或字符偏移。

## 生成、诊断与保存

- `POST /api/pipeline/generate` 请求仍为 `{"sources": [...]}`。后端将来源 ID
  和完整原文以 JSON 边界传给模型；根据实际输入重建 `source_usage`，不信任
  模型提供的摘要。未知来源与未通过原文核验的引用会被剔除，原因通过
  `diagnostics` 返回，并带上 `node_id`。节点本身保留，合法引用保留。
- `POST /api/generations` 和 `/api/generations/stream` 只有单段文本，没有
  可追溯的来源记录；在契约校验前清空模型返回的摘要及所有节点引用。
  SSE 增量节点也不携带未经核验的引用。demo 不编造引用。
- `POST /api/optimization/check` 和 `/api/optimization/suggest` 可额外传
  `sources` 执行第三层；省略时只做原有结构诊断，传 `[]` 会明确报告缺失原文。
  两个接口都不改写导图或数据库，重复来源 ID 返回 422。
- `POST/PUT /api/mindmaps` 校验前两层并持久化引用，版本冲突仍返回 409。
  保存接口没有原文，不能执行第三层；重新核验时须把原文传给诊断接口。
  本功能不增加原文存储，引用匹配针对本次请求提供的原文，不代表外部事实核验。

诊断请求示例：

```json
{
  "content": {
    "title": "会议摘要",
    "root": {
      "id": "n1", "label": "发布安排", "children": [],
      "source_refs": [{"source_id": "meeting", "quote": "下周发布"}]
    },
    "source_usage": [{"source_id": "meeting", "kind": "text", "char_count": 9}]
  },
  "sources": [{
    "id": "meeting", "kind": "text", "status": "parsed", "text": "会议决定下周发布。"
  }]
}
```

针对引用的测试：在 `backend` 目录执行
`python -m pytest -p no:cacheprovider tests/test_source_refs.py tests/test_generation.py tests/test_stream.py`。
