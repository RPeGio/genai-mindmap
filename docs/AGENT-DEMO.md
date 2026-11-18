# 开启 agent 与现场演示指南

> 面向"要在别人面前演示一遍"的场景：怎么启动、会看到什么、坏了怎么查。
> 所有开关与默认值都对照代码核实过，不是凭印象。

## 先说一件容易踩空的事

**agent 有 demo 模式，但要显式打开。**

`MINDMAP_LLM_MODE=demo` 时，agent 的 `run()` 在进入模型循环**之前**就返回：一帧
`stage`（"演示模式，不调用模型"）、一帧 `snapshot`、一帧 `done`（`mode="demo"`），
一次模型调用都不发（`agent/service.py::_loop` 开头那段）。占位内容与普通生成共用
同一份 `generation.service._demo_content`。

三件事要一起记住：

- **按图型产出**：`_demo_content(goal, diagram_type)` 会按这次请求的图型给占位形状
  ——树是一棵两层导图，鱼骨是"problem + 三根骨"，流程是"三个阶段 + 两条阶段间
  跳转"。界面上选了流程图却出来一棵导图，那是让人以为功能坏了。
- **缺省值不是 demo**：不设这个变量时仍然真跑模型。旧路径那套"没配置就走 demo"
  照搬到 agent 上，等于把默认路径静默降级成占位图。
- **demo 不代表工具循环能用**：这条路不发任何工具调用，所以它演示的是"渲染一张图"，
  不是"模型怎么一步步改图"。要演示编辑/展开仍然是真实调用。

所以：**演示 agent 的交互** = 配真实 key；**演示三种图型长什么样**（没有 key 的
机器）= `MINDMAP_LLM_MODE=demo` + 在界面上切图型下拉。

## 两个开关

agent 能不能用，由两个独立的环境变量决定，**两个都要开**：

| 开关 | 在哪一侧 | 默认 | 开的值 |
|---|---|---|---|
| `MINDMAP_AGENT_ENABLED` | 后端 | **开**（`"1"`，只有设 `0` 才关） | 不用设；关了要设回 `1` |
| `VITE_MINDMAP_AGENT` | 前端 | **关** | 必须设 `1` |

前端默认是关的，所以"面板根本不出来"最常见的原因就是漏了 `VITE_MINDMAP_AGENT=1`。

两个变量都在**进程启动时**读取：

- 后端改了要重启 `uvicorn`；
- 前端是 Vite 在启动时注入 `import.meta.env`，改了要**重启 `npm run dev`**，光刷新页面不够。

后端被关掉时，`POST /api/agent/runs/stream` 返回 `503`，`detail.code` 是 `agent_disabled`。

## 最短路径

两个终端，分别在 `backend/` 和 `frontend/` 下。

**终端一（后端）**

```powershell
cd D:\genai-mindmap\backend
$env:MINDMAP_LLM_MODE='model'
$env:MINDMAP_LLM_BASE_URL='https://api.commandcode.ai/provider/v1'
$env:MINDMAP_LLM_API_KEY='你的key'
$env:MINDMAP_LLM_MODEL='z-ai/glm-5.3-flash'
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

这三个变量是**任意 OpenAI 兼容端点**的通用三件套，换服务商只改它们，代码不用动
（端点要满足 `tools` 真被调用、`stream: true` 能出增量、`max_tokens` 有上限可学，
细则见 `docs/DEPLOY.md` 的「换模型服务商」）。

看到 `Application startup complete` 与 `Uvicorn running on http://127.0.0.1:8000` 即可。

**终端二（前端）**

```powershell
cd D:\genai-mindmap\frontend
$env:VITE_MINDMAP_AGENT='1'
npm run dev
```

看到 `Local: http://localhost:5173/` 后打开它。

**确认两边都通**（可选，但演示前建议做一次）：

```powershell
Invoke-WebRequest http://127.0.0.1:8000/docs -UseBasicParsing | Select-Object StatusCode   # 期望 200
Invoke-WebRequest http://localhost:5173/ -UseBasicParsing | Select-Object StatusCode       # 期望 200
```

前端通过 Vite 代理把 `/api` 转发到 `127.0.0.1:8000`，所以后端必须是 8000 端口，否则前端能开、生成会失败。

## 演示时会看到什么

打开页面后，右侧会多出一个 agent 面板（顶部状态标签与工具调用列表）。三个值得当场指出来的点：

**一、节点直接在画布上长出来。** 在画布底部那条对话框里写个主题（或先传一份材料、只点生成），点「生成导图」之后 2~3 秒，中心主题和一级分支就出现，之后子节点陆续往下挂。不是等十几秒黑屏、然后整棵树同时出现。

- 预览中的节点是**虚线 + 半透明**；生成完成那一刻整棵树进画布，节点变成实线（此时已是未保存的作品）。
- 顺序是：预览（虚线，边生成边长）→ `done` 一到整树落画布（未保存）→ 想退回去用「回到生成之前」或撤销。
- **预览节点不能选中、不能编辑**。原因很实在：它们的 id 后端不认识，选中之后会被带进 expand 请求的 `selected_ids`。

**二、只有明确"到底了"的节点才带标记。** 预览里带小圆点的节点，是模型自己声明「我已经是最小可执行单元」的。没有圆点表示它没说，或者它说还能继续拆。

**三、深度和规模是内容决定的，不是写死的。** 同一份论文实测能到 5 层、100 多个节点；纯主题展开会更深更宽。层级上限 8 是可读性上限，不是目标。

## 现场容易出问题的四个地方

**面板不出现。** 前端漏了 `VITE_MINDMAP_AGENT=1`，或者设了但没重启 `npm run dev`。

**点了生成，面板立刻报错。** 先看面板上那一行红字下面的 `code：message`——现在失败原因会直接显示出来。常见的是：

- `generation_failed` + 「请在后端环境变量中配置模型地址、密钥和模型名称」→ 后端三个变量没配全（地址、key、模型名缺一不可）。
- `budget_exhausted` + 「模型调用次数已用完」→ 树已经建好了，只是没额度再问下一轮。**这不算失败**：会以已建好的树收尾，节点照常可用。
- HTTP `503` + `agent_disabled` → 后端 `MINDMAP_AGENT_ENABLED=0`。

**生成很久没有新节点。** 真机上单次生成 25~65 秒都出现过，中间大段没有新节点是模型在憋输出，不是卡死。判断依据是面板顶部的工具行还在「运行中」，以及后端日志里没有错误记录。

**画布上不生，只有右侧面板在长。** 说明你打开了一张旧图再点生成。`content` 非空时预览不会顶掉它——这是有意的，不然生成中的中间态会盖住你已经保存的图。**要演示画布上的生长，从空白画布开始。**

## 演示前的检查清单

1. 后端三个 `MINDMAP_LLM_*` 变量齐全，且 `MINDMAP_LLM_MODE=model`。
2. 前端 `VITE_MINDMAP_AGENT=1`，且改完之后**重启过** `npm run dev`。
3. 后端在 8000，前端在 5173，`/docs` 与首页都返回 200。
4. 画布是空的（没有打开过旧图）。
5. 准备好的材料能在界面上正常解析完，字数不超上限（合并后 300000 字以内）。
6. 先自己跑一遍完整流程，记下这次的耗时——现场用同一份材料，心里有数。

## 关闭与回滚

- 临时关掉 agent、退回原有生成路径：后端设 `MINDMAP_AGENT_ENABLED=0` 并重启。前端不用动（面板还在，但请求会被 503 挡掉）。
- 彻底不显示面板：前端去掉 `VITE_MINDMAP_AGENT` 并重启 `npm run dev`。
- 两者都不需要回滚代码。

## 没有网络或不想花钱时怎么办

**先试 demo 模式**（`MINDMAP_LLM_MODE=demo`）：它给出三种图型各自的占位图，一次
模型调用都不发，也不需要任何 key。适合"看看鱼骨/流程长什么样、画布怎么排"。

它不能替代的部分是**工具循环**：demo 只有三帧（stage → snapshot → done），没有
工具调用、没有流式生长。要讲"模型一步步改图"仍然需要真实调用。可选的替代：

1. **提前录屏**。最稳，也不受现场网络影响。
2. **演示已保存的图**。导图可以存到 SQLite 再从列表打开（不需要生成），讲结构、讲编辑、讲布局都成立，只是没有"流式生长"这一段。
3. **接受真实调用**。DeepSeek 按量计费，演示一次的量级很小；但要提前跑一遍确认额度够。
