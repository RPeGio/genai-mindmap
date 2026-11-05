"""生成流水线（F01 / F02 / F03 / F15 / F16）。

把"素材 → 导图"分成四个阶段，当前只提供同步整体调用：

    understand  →  outline  →  validate  →  verify
    合并多来源    调用模型     契约校验      引文与结构诊断

当前实现：
    understand  合并已解析文本；来源不可用或输入过长时明确报错
    outline     复用 generation.service.generate（demo / model 两种模式）
    validate    字段与来源关联校验，失败抛 GenerationError
    verify      原文引用核验与基础结构诊断；无依据只提示，不删除节点

demo 模式返回占位导图和结构诊断，不代表 AI 已经可用。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.contracts.diagnostic import Diagnostic
from app.contracts.mindmap import MindMapContent
from app.contracts.source import Source
from app.generation.service import GenerationError, generate
from app.optimization.service import check

# 单次生成允许的输入上限。定 20000 是因为文档解析（Docling）出来的正文
# 动辄上万字，8000 会把正常的一份讲义直接挡在门外；20000 字符约合
# 一万出头 token，deepseek-chat 的上下文完全吃得下。
MAX_INPUT_CHARS = 20000


@dataclass
class StageTrace:
    """每个阶段的执行记录，用于前端展示进度与排查。"""
    stage: str
    ok: bool
    detail: str = ""


@dataclass
class PipelineResult:
    content: MindMapContent
    mode: str
    diagnostics: list[Diagnostic] = field(default_factory=list)
    stages: list[StageTrace] = field(default_factory=list)


class PipelineError(Exception):
    pass


class PipelineInputError(PipelineError):
    """来源无法处理或超过当前输入限制。"""


class PipelineGenerationError(PipelineError):
    """模型调用或模型结果校验失败。"""


def _understand(sources: list[Source]) -> str:
    """合并多个来源。

    合并文本用于限长和 demo；模型输入另带来源 ID，原始空白保持不变。
    """
    if not sources:
        raise PipelineInputError("请先提供至少一个来源")
    if len({s.id for s in sources}) != len(sources):
        raise PipelineInputError("来源 ID 不能重复")
    for index, source in enumerate(sources, start=1):
        if source.status != "parsed" or not (source.text or "").strip():
            raise PipelineInputError(f"第 {index} 个来源尚未解析成功或内容为空")
    merged = "\n\n".join(s.text or "" for s in sources)
    if len(merged) > MAX_INPUT_CHARS:
        raise PipelineInputError(f"合并后内容为 {len(merged)} 字，超过当前 {MAX_INPUT_CHARS} 字限制；请缩短或分批处理")
    return merged


def run(sources: list[Source]) -> PipelineResult:
    stages: list[StageTrace] = []

    text = _understand(sources)
    stages.append(StageTrace("understand", True, f"{len(sources)} 个来源，{len(text)} 字"))

    try:
        response = generate(text, sources=sources)
    except GenerationError as exc:
        stages.append(StageTrace("outline", False, str(exc)))
        raise PipelineGenerationError(str(exc)) from exc
    stages.append(StageTrace("outline", True, f"mode={response.mode}"))

    # generate() 内部已经做过 MindMapContent 校验，这里只记录结果
    stages.append(StageTrace("validate", True, "引用字段与来源关联校验通过"))

    # generate 已用真实来源重建摘要，并完成引文核验；这里合并结构诊断。
    content = response.content
    diagnostics = response.diagnostics + check(content)

    # 第三层（可选，默认关）：对已通过前两层的引用做语义蕴含打分。
    # 未启用时 annotate_nli 立即返回空列表，不 import onnxruntime。
    from app.optimization.source_refs import annotate_nli

    diagnostics.extend(annotate_nli(content, sources))
    stages.append(StageTrace("verify", True, f"{len(diagnostics)} 条诊断"))

    return PipelineResult(content=content, mode=response.mode,
                          diagnostics=diagnostics, stages=stages)
