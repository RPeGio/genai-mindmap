"""生成流水线（F01 / F02 / F03 / F15 / F16）。

把"素材 → 导图"分成四个阶段，当前只提供同步整体调用：

    understand  →  outline  →  validate  →  verify
    合并多来源    调用模型     契约校验      结构诊断

当前实现：
    understand  合并已解析文本；来源不可用或输入过长时明确报错
    outline     复用 generation.service.generate（demo / model 两种模式）
    validate    由 MindMapContent 自身校验，失败抛 GenerationError
    verify      调用 optimization.service.check 执行基础结构诊断

demo 模式返回占位导图和结构诊断，不代表 AI 已经可用。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.contracts.diagnostic import Diagnostic
from app.contracts.mindmap import MindMapContent
from app.contracts.source import Source, SourceUsage
from app.generation.service import GenerationError, generate
from app.optimization.service import check

# 当前同步链路的输入限制；超限直接返回错误，不静默丢弃内容。
MAX_INPUT_CHARS = 8000


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

    只拼接正文；来源摘要会附在导图上，不声称节点级溯源已实现。
    """
    if not sources:
        raise PipelineInputError("请先提供至少一个来源")
    if len({s.id for s in sources}) != len(sources):
        raise PipelineInputError("来源 ID 不能重复")
    for index, source in enumerate(sources, start=1):
        if source.status != "parsed" or not (source.text or "").strip():
            raise PipelineInputError(f"第 {index} 个来源尚未解析成功或内容为空")
    merged = "\n\n".join((s.text or "").strip() for s in sources)
    if len(merged) > MAX_INPUT_CHARS:
        raise PipelineInputError(f"合并后内容为 {len(merged)} 字，超过当前 {MAX_INPUT_CHARS} 字限制；请缩短或分批处理")
    return merged


def run(sources: list[Source]) -> PipelineResult:
    stages: list[StageTrace] = []

    text = _understand(sources)
    stages.append(StageTrace("understand", True, f"{len(sources)} 个来源，{len(text)} 字"))

    try:
        response = generate(text)
    except GenerationError as exc:
        stages.append(StageTrace("outline", False, str(exc)))
        raise PipelineGenerationError(str(exc)) from exc
    stages.append(StageTrace("outline", True, f"mode={response.mode}"))

    # generate() 内部已经做过 MindMapContent 校验，这里只记录结果
    stages.append(StageTrace("validate", True, "契约校验通过"))

    # 服务端根据实际参与的来源填写摘要，不采信模型自行生成的来源信息。
    source_usage = [SourceUsage(source_id=s.id, kind=s.kind, filename=s.filename,
                                char_count=len((s.text or "").strip())) for s in sources]
    content = response.content.model_copy(update={"source_usage": source_usage})
    diagnostics = check(content)
    stages.append(StageTrace("verify", True, f"{len(diagnostics)} 条诊断"))

    return PipelineResult(content=content, mode=response.mode,
                          diagnostics=diagnostics, stages=stages)
