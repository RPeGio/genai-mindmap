"""工具注册表与执行入口。

这一版**不实现任何真实业务工具**——生成导图、展开节点、检索资料、精确改树
在下一批填充。这里只提供注册、把工具定义转成模型能读的 schema、以及带参数
校验的执行。循环（`service.run`）只认这个注册表，所以下一批加工具不需要改循环。

参数校验用 pydantic：每个工具自带一个参数模型，注册时把模型的 JSON Schema 交给
模型看，执行时用同一个模型校验模型给的参数。**schema 与校验同源**，不会出现
"文档说必填、代码不检查"的错位。
"""
from __future__ import annotations

import inspect
from typing import Any

from pydantic import ValidationError

from app.agent.contracts import RunContext, ToolCall, ToolResult, ToolSpec


class ToolRegistry:
    """按名字分派的工具表。

    只注册不执行任何未登记的名字：模型不能凭空调用一个没实现的工具，
    这一点由 `execute` 的第一行保证，不依赖模型自觉。
    """

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"工具 {spec.name} 重复注册")
        self._specs[spec.name] = spec

    def __contains__(self, name: str) -> bool:
        return name in self._specs

    def names(self) -> list[str]:
        return sorted(self._specs)

    def specs(self) -> list[dict[str, Any]]:
        return [spec.schema() for spec in self._specs.values()]

    async def execute(self, call: ToolCall, ctx: RunContext) -> ToolResult:
        spec = self._specs.get(call.name)
        if spec is None:
            # 返回而不是抛：模型偶尔会编一个很像的工具名（比如 mindmap_generate
            # 写成 generate_mindmap）。把它写成一条可读的错误回给模型，下一轮
            # 它通常能自己改对；抛异常则整个 run 报废。
            return ToolResult(
                ok=False,
                content=f"没有名为 {call.name} 的工具，可用的有：{', '.join(self.names()) or '（无）'}",
                data={"code": "unknown_tool", "name": call.name},
            )

        if call.parse_error:
            return ToolResult(
                ok=False,
                content=f"参数解析失败：{call.parse_error}",
                data={"code": "invalid_tool_args", "errors": [{"path": "", "reason": call.parse_error}]},
            )

        try:
            args = spec.args_model.model_validate(call.arguments)
        except ValidationError as exc:
            errors = [
                {"path": ".".join(str(part) for part in item["loc"]), "reason": item["msg"]}
                for item in exc.errors()[:5]
            ]
            detail = "；".join(f"{item['path']}: {item['reason']}" for item in errors)
            return ToolResult(
                ok=False,
                content=f"参数不合法：{detail}",
                data={"code": "invalid_tool_args", "errors": errors},
            )

        # 同步与异步工具都要支持：纯计算的工具（比如按 op 改树）没有 await 点，
        # 强制写成 async 只是给作者加噪音；而调模型的工具必然是 async。
        # 统一在这里吸收差异，工具作者不必关心。
        result = spec.fn(args, ctx)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, ToolResult):
            # 工具作者写错返回值要立刻暴露，不能让它变成一串 None 传下去
            raise TypeError(f"工具 {call.name} 必须返回 ToolResult，收到 {type(result).__name__}")
        return result


def build_registry() -> ToolRegistry:
    """构造本次可用的工具集。

    当前是空表：业务工具在下一批实现后往这里注册。空表也能跑通循环——模型
    只会收到"没有可用工具"，然后以纯文本结束，这本身就是一条合法路径。
    """
    return ToolRegistry()
