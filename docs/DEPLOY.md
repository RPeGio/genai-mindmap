# 部署与依赖

## 1. Python 依赖

```bash
cd backend
pip install -r requirements.txt
```

十项全部是请求路径上的硬依赖，缺任何一项都会让对应功能不可用（`tests/test_dependencies.py` 会守住这一点）。语音转写的两项（`aliyun-python-sdk-core`、`websockets`）只在调用语音接口时导入，缺了不影响文档解析，但语音功能会报错并提示重装依赖。

## 2. 系统依赖：LibreOffice

**只有 `.doc` 和 `.ppt` 需要它**，其余格式没有它也能正常工作。

```bash
# Debian / Ubuntu
sudo apt install -y libreoffice-writer libreoffice-impress
# Windows
winget install TheDocumentFoundation.LibreOffice
```

占用约 **1.5 GB 磁盘**。装好后 `soffice` 通常在 PATH 里；不在的话用环境变量指定：

```bash
MINDMAP_SOFFICE_CMD=/usr/bin/libreoffice
MINDMAP_SOFFICE_CMD="C:\Program Files\LibreOffice\program\soffice.exe"   # Windows
```

没装时的行为：上传 `.doc` / `.ppt` 返回 422，错误文案里带上面那两条安装命令，**不会静默降级成空内容**。

## 3. 环境变量

全部经 `os.getenv` 读取，`.env` 文件**不会**自动加载，必须在启动前设入进程环境。样例见 `backend/.env.example`。

| 变量 | 默认 | 说明 |
|---|---|---|
| `MINDMAP_LLM_MODE` | `demo` | `demo` 不联网走本地占位；`model` 才调模型。**PDF 视觉提取只在 `model` 下工作** |
| `MINDMAP_LLM_BASE_URL` | — | 模型服务地址，须以 `http://` 或 `https://` 开头 |
| `MINDMAP_LLM_API_KEY` | — | 密钥，只从环境变量读 |
| `MINDMAP_LLM_MODEL` | — | 模型名，当前用 `deepseek-chat` |
| `MINDMAP_DB_PATH` | `backend/data/mindmaps.db` | SQLite 文件路径 |
| `MINDMAP_SOFFICE_CMD` | 自动探测 | 可选，LibreOffice 可执行文件路径 |
| `ALIYUN_AK_ID` | — | 语音转写的 AccessKey ID |
| `ALIYUN_AK_SECRET` | — | 语音转写的 AccessKey Secret |
| `NLS_APPKEY` | — | 智能语音交互项目的 Appkey |
| `MINDMAP_ASR_INTERNAL` | 空 | 部署在阿里云北京/上海/深圳时设为 `1`，语音走内网网关 |
| `MINDMAP_NLI_ENABLED` | 空（关） | 引用核验的第三层（语义蕴含打分）。需同时配 `MINDMAP_NLI_MODEL_DIR` |
| `MINDMAP_NLI_MODEL_DIR` | — | 含 `model.onnx` / `tokenizer.json` / `config.json` 的目录 |
| `MINDMAP_NLI_MIN_FREE_MB` | `700` | 可用内存低于此值就不加载模型（仅 Linux 可检测） |
| `MINDMAP_ASR_CONCURRENCY` | `2` | 账号的一句话识别并发额度，调大不会变快只会报错 |

## 4. 各格式的依赖与耗时

| 格式 | 依赖 | 耗时量级 | 说明 |
|---|---|---|---|
| `.pdf` | `pymupdf` + 模型 | 约 5–10 秒/页 | 逐页渲染后视觉识别；最多 20 页 |
| `.docx` `.pptx` `.xlsx` `.html` `.md` | `docling` | 1–2 秒 | 读结构化格式，不加载版面模型 |
| `.doc` `.ppt` | `docling` + LibreOffice + 模型 | 约 10–60 秒 | 先转 PDF 再视觉识别；**耗时按转换后的页数算**，重排版后页数会增加 |
| `.xls` | `xlrd` | 0.1 秒 | 直接读单元格 |
| `.wav` | `websockets` + 阿里云 | 约 0.14 秒/秒音频 | 一句话识别；≤60 秒单次，更长自动切片成多段 |
| `.png` `.jpg` `.webp` `.gif` `.bmp` | `MINDMAP_LLM_*` | 约 5–8 秒/张 | 与 PDF 逐页共用多模态调用；`demo` 模式或未配密钥时返回 422 |

`demo` 模式下 PDF 会退回到文本层抽取（约 0.3 秒），扫描件则返回 422——因为视觉提取被模式检查拦下了。

## 5. 资源要求

| 项目 | 用量 |
|---|---|
| Python 依赖磁盘 | 约 700 MB（主要是 `docling` 及其传递依赖） |
| LibreOffice 磁盘 | 约 1.5 GB（仅旧格式需要） |
| 常驻内存 | 约 350 MB（`docling` 在首次解析后常驻） |
| 单次 PDF 解析峰值内存 | 约 70 MB（渲染与模型调用都在外部） |

2C2G 的机器上：不装 LibreOffice 可以跑，装了之后要留意 `.doc/.ppt` 转换时 `soffice` 进程会额外占 350–380 MB。

## 6. 语音输入（F07）

走阿里云**一句话识别**，不在部署机上跑本地模型。

**为什么不用本地模型**：实测 `sherpa-onnx` 的 paraformer-zh-small（78 MB 磁盘）在 2C2G 上常驻 143 MB、短音频 RTF 0.013，但吃不下长输入——33.8 秒整段 CER 86.4%，60 秒直接返回空串并抛 onnxruntime Reshape 错误。它只能安全处理 10 秒以内的片段。云端在同一批音频上 60 秒整段正常出字、CER 0%，代价是每次约 0.0035 元且依赖网络。

### 开通与凭证

1. 控制台搜索「智能语音交互」→ 开通（新用户 3 个月试用）
2. 「全部项目」→ 创建项目 → 项目功能配置选「中文普通话 / 16kHz / 通用模型」
3. 项目列表里抄下 **Appkey**，填进 `NLS_APPKEY`
4. 建议建一个只授 `AliyunNLSFullAccess` 的 **RAM 子账号**，用它的 AccessKey

三个变量缺任何一个，语音上传都会返回 422 并明确说明缺哪个。

### 内网网关

部署在阿里云北京 / 上海 / 深圳时设 `MINDMAP_ASR_INTERNAL=1`，语音改走
`ws://nls-gateway-<region>-internal.aliyuncs.com:80`：

- 不产生公网流量费
- 绕开小带宽瓶颈——60 秒音频 1.83 MB，1 Mbps 公网要传 15 秒，内网 0.15 秒

要求 VPC 网络，经典网络访问不到内网网关。其他地域没有内网地址，不要开这个开关。

### 前端录音必须走 HTTPS

浏览器只在**安全上下文**（HTTPS 或 localhost）下暴露 `navigator.mediaDevices`。用 `http://服务器IP:8080` 打开时这个对象是 `undefined`，录音按钮会报「当前页面无法访问麦克风：浏览器要求 HTTPS 或 localhost」。

**文档上传和 WAV 文件上传不受影响**，只有浏览器内直接录音需要 HTTPS。要么配证书，要么让用户改用「上传音频文件」入口。

### 成本

一句话识别 3.50 元/千次（单次 ≤60 秒），1000 次 3.5 元。双声道且两路内容不同时会识别两路，成本翻倍；两路完全相同时后端只识别一路，不翻倍。

### 隐私

录音会上传到阿里云做转写。教育场景若涉及学生发言，需要在上传前取得同意，并确认数据留存策略。

## 7. 引用核验（可选，默认关闭）

节点引用做三层校验，前两层是纯代码、零成本：

1. **字段与来源关联**：`source_id` 必须出现在本次 `sources` 里，`page` 不能超出总页数，也不能指向解析失败的页。
2. **引文逐字匹配**：模型必须给出 `quote`（声称的原文片段），程序在来源原文里做子串查找。对不上就把整条引用丢掉。

第三层是**可选的语义蕴含打分**，需要额外部署一个 NLI 模型：

```bash
export MINDMAP_NLI_ENABLED=1
export MINDMAP_NLI_MODEL_DIR=/opt/models/nli   # 含 model.onnx + tokenizer.json + config.json
```

它补的是前两层管不住的一种情况：引文逐字对得上，但节点论断与原文语义相反（原文说「只在小数据集上验证」，节点写成「已在大规模数据上验证」）。

**这一层只产出诊断，绝不改变引用的有无。** 理由是它是概率模型，自己也会错；让概率模型推翻可复核的机械判据，等于把「诚实性来自判据的机械性」这条原则反过来用。判定矛盾时引用保留、同时给一条 warn 诊断，由人判断。

**默认关闭**，因为模型加载约需 100 MB 峰值（25.7 MB 权重的 int8 量化版 + onnxruntime 运行时），2C2G 上要和 Docling、语音模块抢内存。关闭时连 `onnxruntime` 都不会 import。

模型选择上有实测约束：本机验证用的是 `typeform/mobilebert-uncased-mnli`（ONNX int8，25.7 MB），英文判断准确（entailment 0.98 / contradiction 0.81），但**中文的 contradiction 识别偏弱**——「完全不能识别图像」对上「识别图像」被判成 neutral 0.55 而非 contradiction。中文场景建议换中文 NLI 模型（如 Erlangshen-Roberta-NLI 系列）并自行导出 ONNX。

## 8. 启动

```bash
cd backend
export MINDMAP_LLM_MODE=model
export MINDMAP_LLM_BASE_URL=https://api.deepseek.com/v1
export MINDMAP_LLM_MODEL=deepseek-chat
export MINDMAP_LLM_API_KEY=<your-key>
# 语音（可选，不配则语音上传返回 422，其余功能正常）
export ALIYUN_AK_ID=<your-ak-id>
export ALIYUN_AK_SECRET=<your-ak-secret>
export NLS_APPKEY=<your-appkey>
export MINDMAP_ASR_INTERNAL=1          # 阿里云同地域 ECS 才开
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

检查：

```bash
curl -s http://127.0.0.1:8000/api/health
curl -s http://127.0.0.1:8000/api/ingestion/formats
```
