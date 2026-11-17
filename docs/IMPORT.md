# 导入：把别人的导图变成我们的 `MindMapContent`

代码在 `backend/app/importing/`，界面入口是顶栏的「导入」按钮
（`frontend/src/components/ImportMindmap.vue`），接口是
`POST /api/mindmaps/import`（multipart，只解析不落库）。

这条功能的判据只有一句：**能导出什么就能导入什么**。否则用户"导出改一改再导入"
就会静默丢东西，而那种 bug 最难解释——图看起来是对的，只是少了一部分。

## 格式矩阵

| 格式 | 导出 | 导入 | 往返之后还剩什么 | 丢了什么 |
| --- | --- | --- | --- | --- |
| JSON（`MindMapContent` 序列化） | ✅ `/api/export/json` | ✅ | 结构、`label`/`note`/`detail`、颜色、图标、折叠、图型、布局/坐标、主题、附加边、跨节点关系 | `source_refs` / `source_usage`（**故意**清空） |
| Markdown 缩进大纲 | ✅ | ✅ | 结构、标题、附加边（label 与 kind） | `note` 与 `label` 的区分（合成一句塞进 `label`）、颜色/图标/图型 |
| OPML 2.0 | ✅ | ✅ | 同上 | 同上 |
| PlantUML mindmap | ✅ | ✅ | 结构、图型（文件头注释）、附加边 | 同上，另加：正文里的 `*` 已被导出成 `·`，不可逆 |
| XMind（`.xmind`，新版 `content.json`） | ✗ | ✅ | 结构、`title`、备注、`structureClass`→布局、`svg:fill`→颜色、marker→图标 | 无导出出口（XMind 的写入不在本仓库范围） |
| XMind 8（`content.xml`，含单独拖进来的 xml） | ✗ | ✅ | 同上（旧版样式表 `styles.xml` 不读） | 同上 |
| FreeMind（`.mm`） | ✗ | ✅ | 结构、`TEXT`、`COLOR`、`<icon>`、`<richcontent>`、`FOLDED` | 无导出出口；`POSITION`/`<attribute>`/`LINK` 丢掉 |

`tests/test_import.py` 里每种格式都有"导出 → 导入 → 与原树等价"的用例（闭环是
这一层的核心），`tests/test_export.py::ByteIdenticalTest` 把三种文本格式的输出
逐字节钉死——新增 JSON 出口不允许改动它们。

## 格式判定：`detect_format(filename, head_bytes)`

```
1. zip 魔数（PK\x03\x04 / 05\x06 / 07\x08 / 06\x06）  → xmind（候选）
2. 强内容特征：<opml / <map>+<node / <xmap-content / @startmindmap / { 或 [  → 内容赢
3. 扩展名（.json/.md/.opml/.xmind/.mm/.puml…）          → 用它
4. markdown 的嗅探（# 标题 / - 项）                      → 只在没有扩展名时兜底
5. 都不成立                                              → unsupported_format
```

两处刻意的顺序：

- **zip 魔数压过 `.json` 扩展名**（题目里的判据）。zip 里到底有没有 XMind 的
  `content.json`/`content.xml` 只有打开才知道，所以 `xmind.parse` 会做二次确认；
  确认失败报 `unsupported_format`（"这是 zip，但里面没有 XMind 的内容表"），
  而不是 `parse_failed`——那不是文件坏了，是类型不对。
- **markdown 排最后**。`* 根` / `- 项` 在聊天记录、笔记、CSV 里都常见；让它推翻
  扩展名的后果是：一个缺 `@startmindmap` 的 `.puml` 被当成 markdown 读成一份
  **层级丢失**的大纲（`** 子` 在 markdown 里是根的同级项），用户拿到一张结构错乱
  却"导入成功"的图。

## 五件边界处理（`normalize.py` 的唯一实现）

| 事项 | 做法 | 为什么 |
| --- | --- | --- |
| id | 全部用 `agent.ops.new_node_id` 重新生成，建 `旧 id → 新 id` 映射重建边/关系 | 外部 id 可能是 `1`、UUID 或我们自己的 `n_xxx`，与库里的碰撞后果是"引用了另一个节点"这种完全静默的错 |
| 节点数 > 1000 | **BFS（层序）保留前 1000 + warn** | DFS 会把第一个分支挖到底：1500 节点的图剩下一根细辫子，另外五个一级分支整条消失，而那才是用户最先要看的。BFS 保证每层每个分支都留下，被裁掉的是各支最深处（"没读到这里"，不是"这里没有了"） |
| 深度 > 8 | 截断子树 + warn | 契约硬边界，超了 `validate_tree` 会整份 422 |
| 单节点 > 50 子 | 截到 50 + warn | `Node.children` 的 `max_length` 是硬边界（`MAX_NODE_CHILDREN`） |
| `label` | 只套契约上限 **200**，不套生成侧的 16 | 用户手写的长标题被截到 16 字不可接受；16 是"模型生成时画布放得下"的口径，不是导入的 |
| 视觉字段 | 精确表映射（`colors.py`），映射不到就丢 | 猜错的颜色/图标用户改不回来，也说不清出处。hex 认得出就留在 `NodeStyle.color`（不渲染、保存不丢） |
| `source_refs` / `source_usage` | **一律清空 + 一条 warn** | 外部引用指向的是**别人那份原文**的字符偏移，`source_id` 在这边不存在。带进来直接让 `validate_tree` 抛"引用的来源不在 source_usage 中"，整份 422，而错误指向一个用户改不掉的地方 |

超限**永远不整份拒绝**：用户导一张 XMind 进来，因为多了几个节点就什么都不给，
比少几个节点糟得多。

## 脏数据：堵住了什么、还剩什么

已经堵住的（都有用例）：

- **NUL / ESC 等控制字符**（`unicodedata.category == "Cc"` 折成空格）——否则
  label 里会带 `\x1b[31m` 这种终端控制序列。
- **压缩炸弹**：XMind 单个成员解压后上限 20MB（先看中央目录的 `file_size`，
  读完再按实际长度复核）。
- **实体膨胀**：带 `<!DOCTYPE` / `<!ENTITY` 的 XML 直接拒（标准库会展开内部实体，
  1KB 能撑到 GB 级）。
- **解析期节点上限**：5000（`RAW_NODE_HARD_CAP`），四个文本解析器在**收集阶段**
  就截，避免"先把几十万个列表项装进内存再裁"。
- **深度递归**：所有树遍历用显式栈；`json.loads` 的 `RecursionError` 被 catch
  成 400。
- **循环引用**：树结构不可能成环（逐层嵌套，json 的重复对象各自独立）；
  `edges`/`relations` 成环是**合法**的（回退线），契约允许。
- **文件大小**：10MB 上传上限。
- **dangling / 自环 / 重复边**：进契约前丢掉 + warn（dangling 会让前端白屏）。

还剩下的（会崩的地方说清楚）：

- **`note` / `detail` 超 2000 字**被截断（有 warn）。不截的话整份 422，所以这是
  两害相权。
- **双向文本控制符**（U+202E 等，Unicode `Cf` 类）不清洗。它们不可见但不破坏
  渲染——最坏情况是某个标题在画布上"从右往左"显示。清掉它们的风险是误伤合法的
  RTL 文本（阿拉伯语 / 希伯来语），所以留给人眼判断。
- **零宽字符**（U+200B / U+FEFF）保留。它们不破坏布局，但会让"看起来一样的两个
  标题"在搜索时不相等。
- **同一 label 的重名节点**：markdown/opml/puml 的边端点按文字匹配，重名时连到
  **第一个**（BFS 顺序）。这是格式能力所限（文本格式里节点没有 id），不是 bug，
  但导入一张同名的图时那几条边可能连错位置。
- **`options` / `theme` 之外的布局语义**：`logic` / `timeline` / `fishbone` 会被
  保存下来，但前端只渲染 tree / vertical，所以"逻辑结构图"导入后看起来是一张
  普通思维导图。这是既有的渲染口径，不是导入引入的。
- **XMind 的 `detached`（游离主题）**：丢掉 + 提示。它们不属于任何父节点，而模型
  里每个节点都必须挂在树上；塞到根下会假装它们是根的分支。
