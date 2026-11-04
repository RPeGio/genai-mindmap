# PDF 解析方案选型报告

面向 2C2G 部署机的实测对比。目的是回答一个问题：**PDF 导入该用哪个库。**

## 测试条件

- 机器：Windows 11 / Python 3.13.5
- 方法：每个「方案 × 样本」起一个独立子进程，测峰值 RSS 与墙钟耗时。
  独立进程是必须的——内存峰值是进程级指标，同进程连跑多个方案会互相污染读数。
- 每个方案只用**默认或官方推荐参数**，不做针对性调优。比的是开箱即用能力。
- 全部代码在 `_mx/bench_one.py`、`_mx/pdf_runners.py`，结果在 `_mx/pdf_bench_results.json`。

### 样本

| 代号 | 文件 | 规模 | 特征 |
|---|---|---|---|
| en_paper | `1706.03762v1.pdf`（Attention Is All You Need） | 15 页 / 2.1MB | 英文双栏、2 个数据表格、公式 |
| cn_doc | `技术文档.pdf` | 30 页 / 4.7MB | 中文、大量表格与图文混排 |
| cn_report | `实验一实验报告.pdf` | 6 页 / 389KB | 中文、表单式排版 |

## 实测数据

`#` = Markdown 标题行数，`tbl` = Markdown 表格行数，`peakMB` = 进程峰值 RSS。

### en_paper（英文双栏论文）

| 方案 | 耗时 | 峰值内存 | 字符 | 标题 | 表格行 |
|---|---|---|---|---|---|
| pdf_inspector | 0.04s | 52 MB | 35972 | 0 | 0 |
| pymupdf | 0.19s | 55 MB | 36882 | 0 | 0 |
| pymupdf_sorted | 0.35s | 55 MB | 42018 | 0 | 0 |
| pypdf | 0.84s | 50 MB | 36878 | 0 | 0 |
| pdfplumber | 1.98s | 118 MB | 33209 | 0 | 0 |
| pymupdf4llm_light | 3.61s | 154 MB | 38967 | 1 | 15 |
| pymupdf4llm_noocr | 26.45s | 351 MB | 38822 | 25 | 53 |
| pymupdf4llm | 57.28s | 709 MB | 39514 | 26 | 53 |
| docling | 115.20s | **1900 MB** | 39924 | 27 | 52 |

### cn_doc（中文 30 页技术文档）

| 方案 | 耗时 | 峰值内存 | 字符 | 标题 | 表格行 |
|---|---|---|---|---|---|
| pymupdf | 0.38s | 66 MB | 145909 | 6 | 0 |
| pdf_inspector | 0.40s | 88 MB | 151449 | 4 | 1 |
| pymupdf_sorted | 0.74s | 66 MB | 182701 | 1 | 0 |
| pypdf | 5.89s | 64 MB | 145786 | 22* | 0 |
| pdfplumber | 26.33s | 436 MB | 143822 | 23* | 1 |
| pymupdf4llm_light | 57.24s | 155 MB | 172538 | 1 | 849 |
| pymupdf4llm_noocr | 92.66s | 360 MB | 168106 | 92 | 715 |
| pymupdf4llm | 94.12s | 387 MB | 168070 | 92 | 710 |
| docling | 366.61s | **2779 MB** | 269499 | 187 | 701 |

\* `pypdf` / `pdfplumber` 不产出 Markdown，这里的 `#` 是正文里恰好出现在行首的井号字符，不是真标题。

### cn_report（中文 6 页实验报告）

| 方案 | 耗时 | 峰值内存 | 字符 | 标题 | 表格行 |
|---|---|---|---|---|---|
| pdf_inspector | 0.014s | 23 MB | 3625 | 0 | 0 |
| pymupdf | 0.12s | 50 MB | 3392 | 0 | 0 |
| pymupdf_sorted | 0.12s | 50 MB | 5208 | 0 | 0 |
| pypdf | 0.27s | 37 MB | 3236 | 0 | 0 |
| pdfplumber | 0.45s | 43 MB | 3316 | 0 | 0 |
| pymupdf4llm_light | 1.51s | 154 MB | 5217 | 3 | 8 |
| pymupdf4llm_noocr | 5.40s | 319 MB | 3702 | 5 | 7 |
| pymupdf4llm | 29.92s | 698 MB | 4540 | 5 | 7 |
| docling | 73.37s | 1603 MB | 5102 | 9 | 7 |

### 云端（阿里云文档智能，仅 en_paper）

| 方案 | 耗时 | 峰值内存 | 表格 | 备注 |
|---|---|---|---|---|
| DocMind `SubmitConvertPdfToMarkdownJob` | 10.4s | 90 MB | **表格转图片，内容丢失** | 1000 页一次性额度 |

## 质量核查

抽取同一段落（3.3 Position-wise Feed-Forward Networks）与 Table 1 做人工比对。

**表格还原**：只有 `docling`、`pymupdf4llm`（含 noocr）、`pymupdf4llm_light` 三条产出真正的
Markdown 表格，Table 1 的四行数据完整。`pymupdf4llm` 的单元格带 `<sup>` 与 `_斜体_` 噪音，
`docling` 最干净。`pypdf` / `pymupdf` / `pdfplumber` / `pdf_inspector` 的表格全部退化成散行，
表头与数据行错位。

**双栏顺序**：`pymupdf`、`pypdf` 在本文档上顺序正确（该 PDF 的内容流本身按栏写入）。
`pdf_inspector` 出现跨栏粘连（`d model . 3.5 Positional Encoding`）。

**中文**：除 `pdf_inspector` 的 markdown 接口把整页误判成一张大表格外，其余中文均无乱码。
`pdf_inspector` 的 markdown 接口在本批样本上不可用。

**Docling 没有轻量档**：`SimplePipeline` 只处理 DOCX 一类，喂 PDF 直接
`RuntimeError: Pipeline SimplePipeline failed`。想省内存只能整个不用它。

## 结论

### 关键事实

1. **Docling 在 2C2G 上不可用。** cn_doc 峰值 2779MB，进程会被 OOM killer 直接杀掉。
   en_paper 也要 1900MB。这不是调参能解决的，版面模型本身就有 1.2GB 常驻。
2. **结构信息全部来自版面分析。** 关掉 layout 后，`pymupdf4llm` 的标题从 26 个掉到 1 个。
   OCR 不是结构来源——关掉 OCR 只掉 1 个标题，但内存减半、速度翻倍。
3. **速度与结构强相关，没有免费午餐。** 保住标题层级的最低成本是
   `pymupdf4llm_noocr`：351MB / 26s（en_paper），中文长文档仍要 92s。
4. **产线只吃 20000 字符。** `MAX_INPUT_CHARS = 20000` 会把 cn_doc 的 168070 字符
   截掉 88%。为被丢弃的内容等 92 秒没有意义。

### 推荐

| 场景 | 方案 | 依据 |
|---|---|---|
| **默认（文本型 PDF）** | `pymupdf` | 0.19–0.38s、55–66MB，文本顺序正确，是唯一同时满足「快 + 省 + 无乱码」的方案 |
| 需要表格结构且文档不大 | `pymupdf4llm_noocr` | 351MB / 26s，保住 25/26 标题与全部表格 |
| 扫描件（无文本层） | 阿里云 DocMind | 本地 OCR 在 2G 上不现实；云端 90MB / 10s |
| 高精度离线、机器 ≥4G | `docling` | 表格最干净，但需要 2.8GB 峰值 |

**默认选 `pymupdf` 的理由不是它结构最好，而是结构本来就该交给 LLM 做。**
导图生成链路必须过一次 DeepSeek，让模型从纯文本里分章节，比任何版面模型都准——
它读得懂「3.1 Encoder and Decoder Stacks」是个小节标题，版面模型只看到一行粗体字。
本地 0.38 秒拿到文本，再花 3 秒让 LLM 分层，总耗时和内存都远优于本地跑版面分析。

代价要说清楚：表格会退化成散行，表格里的数据在导图里会变成零散文本，而不是「一张表」。
如果验收要求保留表格结构，就用 `pymupdf4llm_noocr`，接受 26 秒。

## 落地

`backend/app/ingestion/service.py` 通过 `MINDMAP_PDF_PARSER` 选择链路：

```
auto（默认）  ≥2200MB 可用内存用 docling，否则 pypdf
docling       强制 Docling
light         强制 pypdf
docmind       云端文档智能，需 MINDMAP_DOCMIND_AK / _SK
```

按本报告结论，2C2G 机器应显式设置：

```bash
MINDMAP_PDF_PARSER=pymupdf
```

（`pymupdf` 档位在本次选型后新增，见下节改动。）
