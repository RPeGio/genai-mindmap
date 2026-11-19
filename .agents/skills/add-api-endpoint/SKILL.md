---
name: add-api-endpoint
description: 新增或修改一个 FastAPI 后端接口时使用——包括加路由、加/改请求响应模型、补错误路径用例、重导 openapi 快照、生成前端类型、补 client 调用与类型镜像。当任务涉及 router.py、给前端接一个新的后端能力、或改了契约后前端类型报错/字段丢失时，先读这个。
---

# 新增一个后端接口

一个接口在**六个**地方有形，漏任何一个都能让测试保持绿色：后端能跑、前端类型对不上，或者前端调了一个不存在的字段、字段被静默丢掉。

顺序照做，每一步都能单独验证。

## 1. 路由：`backend/app/<domain>/router.py`

现成的两份样例，按需要挑一份照着写：

- **带 404 / 409 与自定义 201** → `backend/app/mindmaps/router.py`
- **带本地请求模型与 validator** → `backend/app/optimization/router.py`

形状（取自 `backend/app/mindmaps/router.py`）：

```python
router = APIRouter(prefix="/mindmaps", tags=["导图管理"])

@router.post("", response_model=MapRecord, status_code=status.HTTP_201_CREATED)
def create_map(body: SaveRequest):
    return repository.create_map(body.content)

@router.get("/{map_id}", response_model=MapRecord)
def get_map(map_id: str):
    result = repository.get_map(map_id)
    if result is None:
        raise HTTPException(status_code=404, detail="导图不存在")
    return result
```

约定：`prefix` + `tags`（中文）必有；`response_model` 必写（不写的话 OpenAPI 里没有形状，第 5、6 步就没东西可生成）；`detail` 用中文。

**新建了 domain 目录才需要动 `backend/app/main.py`**——已有的六个 router 都已在里面注册：

```python
app.include_router(mindmaps_router, prefix="/api")   # 三行 import 在文件顶部
```

## 2. 请求与响应模型

两个家，选一个：

- **跨模块共用 → `backend/app/contracts/`**（`mindmap.py` / `source.py` / `ops.py` / `diagnostic.py`）。导图、来源、诊断这些形状被前后端同时使用，只能放在契约层。
- **只被这一个 router 用 → 就地写在 `router.py` 里**，见 `optimization/router.py` 的 `CheckRequest` / `CheckResponse`。

约束写法（照 `backend/app/contracts/mindmap.py`）：

```python
class Node(BaseModel):
    model_config = ConfigDict(extra="forbid")   # 多余字段一律拒收，不做静默忽略
    label: str = Field(min_length=1, max_length=MAX_NODE_LABEL_CHARS)
```

**上限必须引用契约常量，不要重新敲数字。** `MAX_NODE_LABEL_CHARS` / `MAX_NODE_DETAIL_CHARS` / `MAX_TREE_NODES` / `MAX_RELATIONS` 都在 `contracts/mindmap.py` 顶部；`LEAF_MAX_CHARS` 与 `SourceRef.quote` 的 300 是同一份字数契约（见 `change-map-schema`）。

校验用 `@field_validator` / `@model_validator(mode="after")`，样例见 `optimization/router.py` 的 `unique_sources` 与 `contracts/ops.py` 的 `at_least_one`。

## 3. 用例：`backend/tests/`

用 `fastapi.testclient.TestClient`，fixture 照抄 `backend/tests/test_source_refs.py:78`：

```python
@pytest.fixture
def client(monkeypatch):
    data = Path(__file__).resolve().parents[1] / "data"
    data.mkdir(exist_ok=True)
    db_file = data / f"source-refs-{uuid4().hex}.db"
    monkeypatch.setenv("MINDMAP_DB_PATH", str(db_file))
    try:
        with TestClient(app) as api:
            yield api
    finally:
        db_file.unlink(missing_ok=True)
```

**三条错误路径各写一条，不要只测成功**：

| 状态 | 怎么造 | 断言 |
| --- | --- | --- |
| 422 | 请求体缺字段 / 超上限 / `extra` 字段 | `resp.json()["code"] == "validation_error"` |
| 404 | 路径参数指向不存在的记录 | `status_code == 404`，`code` 为 `not_found` 或 `http_404` |
| 409 | 版本冲突（`version` 落后） | `code == "version_conflict"` |

统一错误形状在 `backend/app/core/errors.py`：`{"code", "message", "detail", "request_id"}`。断言用 `code` 而不是 `message`——文案会改。

现成对照：`backend/tests/test_source_refs.py:290`（成功 + 409）、`:310`（422 且确认没写脏数据）、`backend/tests/test_errors.py`（三个出口同一套结构）。

## 4. 重导 OpenAPI 快照

在 `backend/` 下：

```bash
python scripts/export_openapi.py   # 写 backend/openapi.json
```

漏了会被两条用例挡：`backend/tests/test_openapi_snapshot.py::test_snapshot_is_up_to_date`（本地红灯）与 CI 的 `git diff --exit-code backend/openapi.json`。

这一步必须紧跟第 2 步，**第 5、6 步都以它为前提**。

## 5. 重生成前端契约类型

在 `frontend/` 下：

```bash
npm run gen:api    # openapi-typescript 读 backend/openapi.json → src/api/schema.d.ts
```

`schema.d.ts` 是生成物，**不要手改**。CI 会重跑一次再 `git diff --exit-code`。

## 6. 前端调用函数 + 类型镜像

三处，一处都不能少：

1. `frontend/src/api/client.ts` — 加调用函数。照 `listMindMaps` / `getMindMap` / `uploadDocument` 的写法，统一走文件内的 `request<T>(path, init)`，错误交给 `toApiError` 折成 `ApiError`。
2. `frontend/src/api/types.ts` — 手写的镜像类型（`MindNode` / `MindMapContent` / `Source` …）。runtimes 与契约同名同形。
3. `frontend/src/api/contract-check.ts` — 在 `checks` 里加/补一行：

```ts
nodeKeys: true satisfies SameKeys<MindNode, Schemas['Node-Output']>,
nodeShape: true satisfies SameShape<MindNode, Schemas['Node-Output']>,
```

**第 3 处是"漏镜像"的唯一自动防线**：`SameKeys` 在 `vue-tsc` 期间比对键集合，`SameShape` 比对字段类型。历史上 `source_refs` 与 `note` 都因为"镜像没声明"在保存时被整批丢掉，而那时测试全绿——因为后端和生成物各自都对，只有镜像少了一行。

别忘了 `npm run type-check` 收尾。

## 7. 界面入口（需要时）

- 顶层面板 → `frontend/src/App.vue`（`AgentPanel` / `SourceInputs` 在这里挂）
- 可复用部件 → `frontend/src/components/`
- 状态走 `frontend/src/stores/` 的 Pinia store，不要在组件里直接 `fetch`

## 自检清单

- [ ] `router.py` 有 `prefix` / `tags` / `response_model`，`detail` 是中文
- [ ] 请求/响应模型放在对的家（契约层 or 就地），上限值引用契约常量而非硬编码
- [ ] `python -m pytest -o addopts="" -q` 通过，且新用例覆盖 422 / 404 / 409 里适用的
- [ ] 422 断言的是 `code` 字段，不是 `message` 文案
- [ ] 跑过 `python scripts/export_openapi.py`，`git diff backend/openapi.json` 里有**预期的**变化（没有变化说明路由没被 `app` 收进去）
- [ ] 跑过 `npm run gen:api`，`git diff frontend/src/api/schema.d.ts` 有对应变化
- [ ] `client.ts` 有调用函数、`types.ts` 有镜像、`contract-check.ts` 的 `checks` 有 `SameKeys` / `SameShape` 两行
- [ ] `npm run type-check` 与 `npm test` 都过
- [ ] 没有手改 `schema.d.ts`，没有跑 `npm run build`
