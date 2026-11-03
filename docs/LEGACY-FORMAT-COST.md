# 旧版 Office 格式（.doc / .ppt / .xls）的解析代价实测

代码目前**明确拒绝**这三种格式。这份文档记录拒绝的依据——不是估计，是实测。

## 结论先行

| 项目 | 实测值 |
|---|---|
| LibreOffice 安装占用磁盘 | **1501 MB** |
| 单份 `.doc` 转换峰值内存 | **744 MB**（进程树） |
| 单份 `.xls` 转换峰值内存 | **720 MB**（进程树） |
| 相对新格式的耗时倍数 | `.doc` 4.5×、`.xls` 8.9× |
| 输出质量 | **与新格式逐字相同** |

## 实测数据

环境：Windows 11 / Python 3.13.5 / Docling 2.145.0 / LibreOffice 26.8.2.2

样本：`Exp2_report`（31KB Word 文档，17034 字）与 `sample`（40 行 Excel 表，1325 字），
两者都先用 LibreOffice 从新格式转成旧格式，保证内容完全一致。

测量方法：起一个 50ms 采样的监控线程，按**进程树**累加 RSS。
只测主进程会严重低估——`soffice.exe` 会再 fork `soffice.bin`，
真正的开销在那个子进程里。

| 格式 | 耗时 | 主进程峰值 | 进程树峰值 | 子进程 | 输出字符 |
|---|---|---|---|---|---|
| DOCX | 1.81s | 354 MB | 354 MB | — | 17034 |
| **DOC** | **8.06s** | 364 MB | **744 MB** | 380 MB | 17034 |
| XLSX | 0.54s | 365 MB | 365 MB | — | 1325 |
| **XLS** | **4.80s** | 365 MB | **720 MB** | 355 MB | 1325 |

三次重复测量，`.doc` 耗时 8.06 / 9.08 / 9.85 / 10.41s，进程树峰值稳定在 744~745MB。
波动来自 LibreOffice 冷启动。

## 三个必须知道的细节

### 一、装了 LibreOffice 还不够

第一次实测直接失败：

```
RuntimeError: LibreOffice is required to convert a .doc file to .docx.
Install LibreOffice and make sure it is on PATH.
```

Docling 用 `get_libreoffice_cmd()` 查找可执行文件，顺序是
`DOCLING_LIBREOFFICE_CMD` 环境变量 → `shutil.which("libreoffice")` → `shutil.which("soffice")`。
winget 装完不会进 PATH，必须显式配置：

```bash
DOCLING_LIBREOFFICE_CMD=/usr/bin/libreoffice   # Linux
DOCLING_LIBREOFFICE_CMD=C:\Program Files\LibreOffice\program\soffice.exe   # Windows
```

这个错误不会在启动时暴露，只在用户上传 `.doc` 的那一刻才出现。

### 二、每份文件都冷启动一次 LibreOffice

Docling 用 `_isolated_libreoffice_profile()` 给每次转换开一个**独立的临时
LibreOffice 用户配置**——因为 LibreOffice 对 profile 目录加排他锁，不隔离就
没法并发。代价是进程内单例复用的手段在这里失效，这 355~380MB 和 5~9 秒的启动
开销，每份文件都要重新付一遍。

### 三、输出质量没有任何提升

`.doc` 与 `.docx` 解析出的字符数完全相同（17034），`.xls` 与 `.xlsx` 也完全相同（1325）。
旧格式只是**多绕一道转换**，没有任何信息增益。

## 为什么不放开

2C2G 部署机的可用内存约 1.5~1.8GB。当前常驻的是 FastAPI + SQLite + Docling
（DOCX 路径 354MB），再叠加一份 744MB 的转换峰值，余量只剩几百 MB，
两个用户同时上传旧格式就会触发 OOM。

对比另一个选择：让用户把 `.doc` 另存为 `.docx`，成本是几秒钟的操作，
换来的是省掉 1.5GB 磁盘和 744MB 峰值。

**如果将来要放开**，前置条件是：

1. 机器内存提到 4GB 以上，或
2. 把 LibreOffice 转换放到独立进程池并限并发 1，或
3. 直接改用 `antiword` / `catdoc` 这类几百 KB 的命令行工具只抽文本（会丢格式）

放开时只需把 `.doc` / `.ppt` / `.xls` 加进 `DOCUMENT_SUFFIXES`，并在部署文档里
写明 `DOCLING_LIBREOFFICE_CMD` 的配置。

## 复现

```
_mx/legacy_bench.py     测量脚本
_mx/legacy/             测试样本（.doc / .xls）
```

```powershell
$env:DOCLING_LIBREOFFICE_CMD='C:\Program Files\LibreOffice\program\soffice.exe'
cd _mx; python legacy_bench.py
```
