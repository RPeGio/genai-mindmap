"""导出（F20 / F10）。

已实现：MindMapContent → PlantUML `@startmindmap` 文本。
待实现：Markdown 大纲、OPML（都是纯映射，工作量很小，可随时补）。

关于 PUML 的定位（与 docs/ARCHITECTURE.md 第 5 条一致）：
    puml 只作**导出格式**，不作为渲染主链路。
    原因：plantuml-encoder 只做压缩编码，出图要靠 PlantUML 服务端（JVM）；
          viz.js 是 Graphviz 的 WASM，只认 DOT，解析不了 PlantUML 语法。
    若要在页面内预览，需要额外部署 plantuml-server，并且预览只能是图片。
"""
from __future__ import annotations

from app.contracts.mindmap import MindMapContent, Node


def _depth_marks(depth: int) -> str:
    """PlantUML mindmap 用星号数量表示层级。"""
    return "*" * max(1, depth)


def to_puml(content: MindMapContent) -> str:
    """把导图转成 PlantUML mindmap 语法。

    节点文字里的换行与星号会破坏语法，统一替换成空格。
    """
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


def _sanitize(text: str) -> str:
    return text.replace("\r", " ").replace("\n", " ").replace("*", "·").strip()
