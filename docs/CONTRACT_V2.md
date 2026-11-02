# MindMapContent v2 契约

> 状态：**冻结候选**（W1 定稿后冻结）· 归属：A/B 共管 · 任何字段改动需双人 review + 迁移脚本
> v1 定义见 `backend/app/contracts/mindmap.py`（`id` / `label` / `children`）。
>
> **落地状态（2026-11-02）**：后端仍是 v1，`MindMapContent` 只有 `title` / `root` / `source_usage`；`frontend/src/api/types.ts` 是手写的 v1 镜像，**不是** v2 生成物。D2 把 v2 写进 Pydantic 后，前端类型改由 `openapi-typescript` 从 `/openapi.json` 生成，禁止再手写第二份。

## 1 v1 → v2 变更摘要

| 变更 | 原因 |
|---|---|
| 新增 `schema_version: 2` | 显式版本号，迁移与兼容判断不再靠字段探测 |
| `Node` 新增 `note` / `collapsed` / `style` / `source_refs` / `pos` | 画布编辑、样式、知识引用、手动布局都需要落到数据里 |
| 新增 `layout` | 布局模式必须随导图保存，否则重开后视图错乱 |
| 新增 `theme` | 主题是导图属性，不是本地偏好 |
| 新增 `relations[]` | 跨节点因果/引用/冲突关系，v1 无法表达（只能用重复节点冒充，已明确禁止） |
| `source_usage` 保留 | 整图来源摘要，语义不变 |

**不变**：节点唯一标识仍是 `id`，节点文字仍是 `label`（不是 `topic`/`text`），接口前缀仍是 `/api`。

## 2 数据结构

```python
# backend/app/contracts/mindmap.py（v2）
class Position(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: float = Field(ge=-1e6, le=1e6)
    y: float = Field(ge=-1e6, le=1e6)


class NodeStyle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")  # 节点主色
    icon: str | None = Field(default=None, max_length=40)                  # 语义图标名，非任意 emoji
    bold: bool = False
    italic: bool = False
    shape: Literal["rounded", "rect", "pill"] = "rounded"


class SourceRef(BaseModel):
    """节点级来源引用；只允许指向本次引入的 Source。"""
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(min_length=1, max_length=100)
    page: int | None = Field(default=None, ge=1)
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, ge=0)
    quote: str | None = Field(default=None, max_length=300)


class Node(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=200)
    children: list[Node] = Field(default_factory=list, max_length=50)
    note: str | None = Field(default=None, max_length=2000)
    collapsed: bool = False
    style: NodeStyle | None = None
    source_refs: list[SourceRef] = Field(default_factory=list, max_length=20)
    pos: Position | None = None   # 仅 layout == "manual" 时有意义


class RelationType(str, Enum):
    cause = "cause"        # A 导致 B
    ref = "ref"            # A 引用/参见 B
    conflict = "conflict"  # A 与 B 冲突
    depends = "depends"    # A 依赖 B


class Relation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=100)
    source: str = Field(min_length=1, max_length=100)   # 节点 id
    target: str = Field(min_length=1, max_length=100)   # 节点 id
    type: RelationType
    label: str | None = Field(default=None, max_length=100)
    evidence: str | None = Field(default=None, max_length=300)  # 原文片段
    provenance: Literal["user", "ai", "import"] = "user"


class Theme(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preset: Literal["default", "slate", "forest", "sunset", "mono"] = "default"
    accent: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")  # 覆盖主色
    font: Literal["system", "serif", "mono"] = "system"
    direction: Literal["right", "left", "both"] = "right"                     # 双向仅导图布局


class LayoutKind(str, Enum):
    tree = "tree"        # 经典思维导图（左右/单向）
    logic = "logic"      # 逻辑结构图（缩进树）
    timeline = "timeline"
    fishbone = "fishbone"  # L3，不进 v1.0.0 的 UI
    manual = "manual"      # 用户拖拽后进入，pos 生效


class MindMapContent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[2] = 2
    title: str = Field(min_length=1, max_length=200)
    root: Node
    layout: LayoutKind = LayoutKind.tree
    theme: Theme = Field(default_factory=Theme)
    relations: list[Relation] = Field(default_factory=list, max_length=200)
    source_usage: list[SourceUsage] = Field(default_factory=list, max_length=50)
```

## 3 校验规则（`model_validator(mode="after")`，任一不满足 → 422）

1. 节点 `id` 全局唯一；总数 ≤ **300**；深度 ≤ **8**；每节点子节点 ≤ **50**（沿用 v1）。
2. `label` 去首尾空白后非空；`label` 内的换行折叠为空格（前端同样处理）。
3. `relations`：`source`/`target` 必须存在于树中；`source != target`；同一 `(source, target, type)` 不得重复；总数 ≤ 200。
4. `layout == "manual"` 时，**至少一个**节点有 `pos`；否则 `pos` 必须是 `None`（避免自动布局被历史坐标污染）。
5. `source_refs.source_id` 必须出现在 `source_usage` 中；生成接口返回时若模型编造来源，**服务端直接清空** `source_refs`（沿用 v1 对 `source_usage` 的处理策略）。
6. `note` / `quote` / `evidence` 允许为空，但不允许纯空白；纯空白视为 `None`。

## 4 迁移规则（v1 记录 → v2）

写入时统一走 `migrate_v1_to_v2(content: dict) -> dict`：

```python
DEFAULTS = {"schema_version": 2, "layout": "tree", "theme": {"preset": "default"},
            "relations": []}

def migrate(content: dict) -> dict:
    if content.get("schema_version") == 2:
        return content
    return {**content, **DEFAULTS}  # Node 侧缺省字段由 pydantic 默认值补齐
```

- 迁移脚本 `backend/scripts/migrate_v1_v2.py`：读 `mindmaps` 全表 → 逐行迁移 → 写回并 `version + 1` → 输出统计（迁移数 / 跳过数 / 失败 id）。**先备份 `pg_dump` 再跑**。
- 迁移期间服务只读（维护窗口 5 分钟即可，数据量小）。
- 回滚：保留迁移前 dump，失败则 `pg_restore` 并回退服务镜像 tag。

## 5 前端类型生成（不再手写 `types.ts`）

```powershell
# backend 启动后
npx openapi-typescript http://127.0.0.1:8000/openapi.json -o src/api/schema.d.ts
```

- 生成物 `src/api/schema.d.ts` **提交入库**，CI 里跑 `npm run gen:api && git diff --exit-code` 保证与后端同步。
- 手写的 `frontend/src/api/types.ts`（v1 镜像）删除；业务层只从 `schema.d.ts` 取 `components["schemas"]["MindMapContent"]`。
- 前端额外需要、后端没有的类型（如画布内部节点）放在 `src/canvas/types.ts`，**不得**与契约同名。

## 6 完整示例

```json
{
  "schema_version": 2,
  "title": "机器学习学习路线",
  "layout": "tree",
  "theme": { "preset": "forest", "accent": "#2f855a", "font": "system", "direction": "right" },
  "root": {
    "id": "root",
    "label": "机器学习",
    "children": [
      {
        "id": "n1",
        "label": "数学基础",
        "collapsed": false,
        "source_refs": [{ "source_id": "src-1", "page": 3, "quote": "线性代数与概率论是前置" }],
        "children": [
          { "id": "n1-1", "label": "线性代数", "children": [] },
          { "id": "n1-2", "label": "概率统计", "children": [] }
        ]
      },
      { "id": "n2", "label": "监督学习", "children": [] }
    ]
  },
  "relations": [
    { "id": "r1", "source": "n1", "target": "n2", "type": "depends",
      "label": "先修", "provenance": "user" }
  ],
  "source_usage": [{ "source_id": "src-1", "kind": "document", "filename": "大纲.pdf", "char_count": 4200 }]
}
```

## 7 演进纪律

- **只加不删**：新增字段必须给默认值，旧数据无需迁移即可读。
- **删/改字段 = breaking**：需双人 review + 迁移脚本 + `schema_version` 升位 + `docs/API.md` 与生成类型同步。
- `extra="forbid"` **保留**：宁可 422 也不让前后端字段悄悄漂移。
- 语义结构（树 + relations）与视图（layout/theme/pos）分离——布局算法不得往 `label` 里塞标记。
