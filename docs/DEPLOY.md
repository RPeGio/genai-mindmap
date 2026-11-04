# 部署与依赖

## 1. Python 依赖

```bash
cd backend
pip install -r requirements.txt
```

八项全部是请求路径上的硬依赖，缺任何一项都会让对应功能不可用（`tests/test_dependencies.py` 会守住这一点）。

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

## 4. 各格式的依赖与耗时

| 格式 | 依赖 | 耗时量级 | 说明 |
|---|---|---|---|
| `.pdf` | `pymupdf` + 模型 | 约 5–10 秒/页 | 逐页渲染后视觉识别；最多 20 页 |
| `.docx` `.pptx` `.xlsx` `.html` `.md` | `docling` | 1–2 秒 | 读结构化格式，不加载版面模型 |
| `.doc` `.ppt` | `docling` + LibreOffice + 模型 | 约 10–60 秒 | 先转 PDF 再视觉识别；**耗时按转换后的页数算**，重排版后页数会增加 |
| `.xls` | `xlrd` | 0.1 秒 | 直接读单元格 |

`demo` 模式下 PDF 会退回到文本层抽取（约 0.3 秒），扫描件则返回 422——因为视觉提取被模式检查拦下了。

## 5. 资源要求

| 项目 | 用量 |
|---|---|
| Python 依赖磁盘 | 约 700 MB（主要是 `docling` 及其传递依赖） |
| LibreOffice 磁盘 | 约 1.5 GB（仅旧格式需要） |
| 常驻内存 | 约 350 MB（`docling` 在首次解析后常驻） |
| 单次 PDF 解析峰值内存 | 约 70 MB（渲染与模型调用都在外部） |

2C2G 的机器上：不装 LibreOffice 可以跑，装了之后要留意 `.doc/.ppt` 转换时 `soffice` 进程会额外占 350–380 MB。

## 6. 启动

```bash
cd backend
export MINDMAP_LLM_MODE=model
export MINDMAP_LLM_BASE_URL=https://api.deepseek.com/v1
export MINDMAP_LLM_MODEL=deepseek-chat
export MINDMAP_LLM_API_KEY=<your-key>
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

检查：

```bash
curl -s http://127.0.0.1:8000/api/health
curl -s http://127.0.0.1:8000/api/ingestion/formats
```
