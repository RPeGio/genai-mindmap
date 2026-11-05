"""导出（F20 / F10）。

三种格式，都是 MindMapContent → 文本的纯映射，不调模型也不读盘：
    PUML      PlantUML `@startmindmap`，能贴进语雀 / Typora / Confluence
    Markdown  缩进大纲，最通用的中间格式
    OPML      能直接导入 XMind 等导图工具

关于 PUML 的定位（与 docs/ARCHITECTURE.md 第 5 条一致）：
    puml 只作**导出格式**，不作为渲染主链路。
    原因：plantuml-encoder 只做压缩编码，出图要靠 PlantUML 服务端（JVM）；
          viz.js 是 Graphviz 的 WASM，只认 DOT，解析不了 PlantUML 语法。
    若要在页面内预览，需要额外部署 plantuml-server，并且预览只能是图片。
"""
from __future__ import annotations

from xml.sax.saxutils import escape, quoteattr

from app.contracts.mindmap import MindMapContent, Node


def _depth_marks(depth: int) -> str:
    """PlantUML mindmap 用星号数量表示层级。"""
    return "*" * max(1, depth)


def _one_line(text: str) -> str:
    """压成单行。导出格式里换行会破坏缩进与 XML 结构。"""
    return " ".join(text.split())


def _sanitize(text: str) -> str:
    """PUML 专用：星号在 mindmap 语法里是层级标记，出现在正文会被解析成新节点。"""
    return _one_line(text).replace("*", "·")


def _escape_markdown(text: str) -> str:
    """转义会破坏 Markdown 结构的字符。

    尖括号不转义会被渲染器当成 HTML 标签——`List<T>` 里的 `<T>` 直接消失，
    节点文字就残了。节点总在 `- ` 之后，不占行首，所以 `#` 和 `>` 不用管。
    """
    return _one_line(text).replace("<", "&lt;").replace(">", "&gt;")


def to_puml(content: MindMapContent) -> str:
    """把导图转成 PlantUML mindmap 语法。"""
    lines: list[str] = ["@startmindmap"]
    lines.append(f"* {_sanitize(content.root.label)}")

    def walk(node: Node, depth: int) -> None:
        for child in node.children:
            lines.append(f"{_depth_marks(depth)} {_sanitize(child.label)}")
            walk(child, depth + 1)

    # 根节点已输出，子节点从第 2 层开始
    walk(content.root, 2)
    lines.append("@endmindmap")
    return "\n".join(lines) + "\n"


def to_markdown(content: MindMapContent) -> str:
    """导出 Markdown 大纲：标题 + 缩进列表。

    根节点也写成列表项而不是跳过——否则 `title` 和 `root.label` 不一致时
    （生成器允许不同），根节点的文字会在导出结果里消失。
    """
    lines: list[str] = [f"# {_escape_markdown(content.title)}", ""]
    lines.append(f"- {_escape_markdown(content.root.label)}")

    def walk(node: Node, depth: int) -> None:
        for child in node.children:
            lines.append(f"{'  ' * depth}- {_escape_markdown(child.label)}")
            walk(child, depth + 1)

    walk(content.root, 1)
    return "\n".join(lines) + "\n"


def to_opml(content: MindMapContent) -> str:
    """导出 OPML 2.0，XMind / MindManager / 幕布都能直接导入。

    节点文字用 quoteattr 转义：模型生成的标签里出现引号或尖括号时，手写
    字符串拼接会产出非法 XML，导入端只会报"文件格式错误"，很难追到根因。
    """

    def outline(node: Node, depth: int) -> list[str]:
        pad = "  " * depth
        parts = [f"{pad}<outline text={quoteattr(_one_line(node.label))}>"]
        for child in node.children:
            parts.extend(outline(child, depth + 1))
        parts.append(f"{pad}</outline>")
        return parts

    lines: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<opml version="2.0">',
        "  <head>",
        f"    <title>{escape(_one_line(content.title))}</title>",
        "  </head>",
        "  <body>",
    ]
    lines.extend(outline(content.root, 2))
    lines.extend(("  </body>", "</opml>"))
    return "\n".join(lines) + "\n"
