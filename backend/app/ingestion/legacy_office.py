"""Office 97-2003 旧格式（.doc / .ppt / .xls）的解析入口。

.doc / .ppt 是 OLE2 复合文档，正文分散在各类记录里，纯 Python 抽不全——
实测 .ppt 只能捞出字体名和母版占位符，连正文段落都拿不到。所以交给
LibreOffice 渲染成 PDF，再走已有的视觉提取：版式、表格、公式一起保住。

.xls 例外：它是行列结构，`xlrd` 直接读单元格又快又准（实测 5ms 读完
40 行 4 列），没必要绕一圈渲染再识别。

三者都需要额外依赖：
    LibreOffice（约 1.5GB 磁盘，仅 .doc / .ppt 需要）
    xlrd（约 100KB，仅 .xls 需要）

LibreOffice 路径按以下顺序找：MINDMAP_SOFFICE_CMD → PATH 里的 soffice/libreoffice
→ Windows 与 macOS 的默认安装位置。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

# 单次转换超时。一个几十页的文档正常在 3~10 秒内转完；挂住的多半是
# LibreOffice 弹了看不见的模态框，超过这个时间就没必要再等。
CONVERT_TIMEOUT_SECONDS = 180

# LibreOffice 对用户配置目录加排他锁：同一 profile 上并发转换会互相打架。
# 给每次调用开独立 profile（下面 _isolated_profile），再加一把进程内的锁兜底。
_convert_lock = threading.Lock()

_WINDOWS_DEFAULT = r"C:\Program Files\LibreOffice\program\soffice.exe"
_MAC_DEFAULT = "/Applications/LibreOffice.app/Contents/MacOS/soffice"


class LegacyFormatUnavailable(Exception):
    """缺少依赖或转换失败，由上层翻译成对用户说的话。"""


def soffice_cmd() -> str | None:
    """找到 soffice 可执行文件；找不到返回 None。"""
    configured = os.getenv("MINDMAP_SOFFICE_CMD", "").strip()
    if configured and Path(configured).exists():
        return configured
    for name in ("soffice", "libreoffice"):
        hit = shutil.which(name)
        if hit:
            return hit
    for candidate in (_WINDOWS_DEFAULT, _MAC_DEFAULT):
        if Path(candidate).exists():
            return candidate
    return None


def available() -> bool:
    return soffice_cmd() is not None


def install_hint() -> str:
    return (
        "解析 .doc / .ppt 需要 LibreOffice 做格式转换。安装方式：\n"
        "  Linux:   sudo apt install -y libreoffice-writer libreoffice-impress\n"
        "  Windows: winget install TheDocumentFoundation.LibreOffice\n"
        "  或用 MINDMAP_SOFFICE_CMD 指定 soffice 的完整路径"
    )


def convert_to_pdf(raw: bytes, suffix: str) -> bytes:
    """把 .doc / .ppt 转成 PDF，返回字节。

    走临时目录：soffice 只接受文件路径，不能从 stdin 读。目录在退出时自动清理，
    不留下用户上传过的原件。
    """
    cmd = soffice_cmd()
    if cmd is None:
        raise LegacyFormatUnavailable(install_hint())

    with tempfile.TemporaryDirectory(prefix="mindmap-legacy-") as workdir:
        work = Path(workdir)
        source = work / f"input{suffix}"
        source.write_bytes(raw)

        # 独立 profile：避免与机器上其它 LibreOffice 实例抢锁，
        # 也避免并发请求互相踩。每次转换都用一个全新的空目录。
        profile = work / "profile"
        args = [
            cmd,
            "--headless",
            "--norestore",
            "--nolockcheck",
            "--nodefault",
            f"-env:UserInstallation={profile.as_uri()}",
            "--convert-to", "pdf",
            "--outdir", str(work),
            str(source),
        ]

        with _convert_lock:
            try:
                completed = subprocess.run(
                    args, capture_output=True, timeout=CONVERT_TIMEOUT_SECONDS,
                    cwd=str(work),
                )
            except subprocess.TimeoutExpired as exc:
                raise LegacyFormatUnavailable(
                    f"格式转换超时（超过 {CONVERT_TIMEOUT_SECONDS} 秒），"
                    "这份文档可能过大或已损坏") from exc
            except OSError as exc:
                raise LegacyFormatUnavailable(
                    f"无法启动 LibreOffice：{exc}") from exc

        output = work / "input.pdf"
        if not output.exists():
            detail = (completed.stderr or b"").decode("utf-8", "ignore").strip()
            raise LegacyFormatUnavailable(
                "格式转换失败，LibreOffice 没有产出 PDF"
                + (f"：{detail[:200]}" if detail else ""))

        pdf = output.read_bytes()
        if not pdf.startswith(b"%PDF"):
            raise LegacyFormatUnavailable("格式转换产出的文件不是有效 PDF")
        return pdf


# ---- .xls ----

def _cell_text(book, cell) -> str:
    """把 xlrd 单元格转成给人看的字符串。

    xlrd 返回的是原始值：日期是 Excel 序列号（46096.0 而不是 2026-03-15），
    整数也带小数点（12 变成 12.0）。不处理的话表格读出来没法看。
    """
    import xlrd

    if cell.ctype == xlrd.XL_CELL_DATE:
        try:
            return xlrd.xldate_as_datetime(cell.value, book.datemode).strftime("%Y-%m-%d")
        except Exception:  # noqa: BLE001 - 个别越界日期转不出来，退回原值
            return str(cell.value)
    if cell.ctype == xlrd.XL_CELL_NUMBER:
        number = float(cell.value)
        return str(int(number)) if number.is_integer() else repr(number)
    if cell.ctype == xlrd.XL_CELL_BOOLEAN:
        return "TRUE" if cell.value else "FALSE"
    if cell.ctype == xlrd.XL_CELL_ERROR:
        return "#ERROR"
    if cell.ctype == xlrd.XL_CELL_EMPTY:
        return ""
    return str(cell.value).strip()


def read_xls(raw: bytes) -> str:
    """把 .xls 读成 Markdown 表格，每个工作表一个二级标题。"""
    try:
        import xlrd
    except ImportError as exc:  # pragma: no cover - 只在缺依赖的部署上触发
        raise LegacyFormatUnavailable(
            "解析 .xls 需要 xlrd，请执行 pip install xlrd") from exc

    try:
        book = xlrd.open_workbook(file_contents=raw)
    except Exception as exc:  # noqa: BLE001 - xlrd 的异常类型不稳定
        raise LegacyFormatUnavailable(f"读取 .xls 失败：{exc}") from exc

    blocks: list[str] = []
    for sheet in book.sheets():
        rows: list[str] = []
        for r in range(sheet.nrows):
            cells = [_cell_text(book, sheet.cell(r, c)) for c in range(sheet.ncols)]
            # 整行空白的直接丢掉：Excel 常把用过的范围撑到很远，全空行是噪音
            if not any(cell for cell in cells):
                continue
            rows.append("| " + " | ".join(cells) + " |")
        if not rows:
            continue
        header_rule = "|" + "---|" * sheet.ncols
        blocks.append(f"## {sheet.name}\n\n" + "\n".join(
            [rows[0], header_rule] + rows[1:]))

    if not blocks:
        raise LegacyFormatUnavailable("这份 .xls 里没有可读的单元格内容")
    return "\n\n".join(blocks)
