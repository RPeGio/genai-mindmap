"""文本解码与 XML 解析的共同入口。

三件事，每一件都对应一种真实文件：

  1. **编码**。用户从 Windows 上的老工具导出的 `.mm` / `.opml` 常是 GB18030，
     而 XMind 的 `content.xml` 偶尔带 BOM。UTF-8 → GB18030 这个顺序能覆盖两者，
     并且 UTF-16 只在**有 BOM** 时才尝试——否则任何偶数长度的 GB18030 字节流都
     会被"成功"解成乱码，报出来的错误就变成"没找到根节点"而不是"编码不对"。

  2. **实体膨胀**。标准库 `xml.etree` 会展开内部实体，一份 1KB 的
     `<!ENTITY a "aaaa…">` 递归十层能撑到 GB 级内存。我们自己的导出从不写
     `DOCTYPE`/`ENTITY`，所以直接拒绝带这两个标记的文件——**零依赖**的堵法，
     比引入 defusedxml 更符合"2C2G 单机不加重依赖"的部署口径。

  3. **错误定位**。`ET.ParseError` 带 `(line, column)`，把它拼进 message：
     "第 12 行第 5 列"是可操作的，"XML 格式错误"不是。
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from app.importing.errors import ParseFailure

# 只在"看起来像声明"的地方判，避免把正文里的 `&lt;!ENTITY` 也拒掉——那已经
# 被转义过，不构成实体声明。
_DOCTYPE = re.compile(r"<!\s*DOCTYPE", re.IGNORECASE)
_ENTITY = re.compile(r"<!\s*ENTITY", re.IGNORECASE)


def decode_text(raw: bytes) -> str:
    """字节 → 文本。UTF-16 只在有 BOM 时尝试（理由见模块 docstring）。"""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError:
            pass
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ParseFailure("文件编码认不出来：试过 UTF-8 / UTF-16（带 BOM）/ GB18030")


def parse_xml(text: str, what: str) -> ET.Element:
    """解析 XML 文本，失败时给出 `what` 与行列号。"""
    if _DOCTYPE.search(text) or _ENTITY.search(text):
        raise ParseFailure(
            f"{what} 里含有 DOCTYPE / ENTITY 声明：这类文件可以靠实体递归撑爆内存，"
            "已拒绝解析。请用导出工具重新导出一份干净的 XML")
    try:
        return ET.fromstring(text)
    except ET.ParseError as exc:
        line, column = getattr(exc, "position", (0, 0))
        raise ParseFailure(
            f"{what}的 XML 解析失败（第 {line} 行第 {column + 1} 列）：{exc}") from exc


def local_name(tag: object) -> str:
    """去掉命名空间前缀：`{urn:xmind:...}topic` → `topic`。

    不按固定 namespace 匹配的原因：XMind 8 的 `content.xml` 里 namespace 是
    `urn:xmind:xmap:xmlns:content:2.0`，而第三方工具重新打包时可能改掉它；
    FreeMind 干脆没有 namespace。按本地名匹配一次覆盖三种，代价是理论上可能
    撞上同名的外来元素——那在"只读 title / children / notes"这几个位置上是
    可接受的。
    """
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def children_named(element: ET.Element, name: str) -> list[ET.Element]:
    """直接子元素里本地名等于 `name` 的那些。"""
    return [child for child in element if local_name(child.tag) == name]


def first_child(element: ET.Element, name: str) -> ET.Element | None:
    for child in element:
        if local_name(child.tag) == name:
            return child
    return None


def text_of(element: ET.Element | None) -> str:
    """元素里的全部文字（含子元素文字，去首尾空白）。"""
    if element is None:
        return ""
    return "".join(element.itertext()).strip()
