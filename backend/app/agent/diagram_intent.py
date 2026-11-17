"""从 `goal` 里读出图型，并把那个词从 `goal` 里剥掉。

**为什么需要这一层**：`AgentRunRequest.diagram_type` 只覆盖了"用户在下拉里选"。
实证那一条（库里 06:02 的 run）是用户**打字说的**「机器学习，用鱼骨图」：请求层
`diagram_type` 是 `None`，而模型看不见界面——`GenerateDeepArgs.diagram_type` 有
默认值可走，它就走默认值 `tree`，还把这几个字当成主题的修饰语，产出的根节点叫
「机器学习（鱼骨图分析）」。

认不出来是一件事；认出来之后**那个词仍然留在 `goal` 里**是另一件事：`goal` 原样
进提示词，根标签就还是被污染的那一句。所以这里做两件事：识别 + 剥离。

**边界（诚实地写在代码里，不在文档里）**：这是"从一句话里猜意图"，不是语义理解。
它按词面判，判据写在下面的正则表里，每一条判错的方向都能从表上读出来。猜不出来
时**什么都不做**（`source="model"`），而不是猜一个——一张按默认值长出来的思维导图
是可以重来的，一张被猜歪的流程图不是。`docs/` 里没有比这一段更权威的说法。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

# 三个取值与中文名只在契约里定义一次：这里再抄一遍，某天加第四种图型时会出现
# "请求层收下了、契约层 422"这种指错地方的分叉。
from app.contracts.mindmap import DIAGRAM_LABELS, DIAGRAM_TYPES  # noqa: F401  （re-export）

if TYPE_CHECKING:  # 只为了类型注解：本模块不 import 契约，契约反过来要引用它的常量
    from app.agent.contracts import AgentRunRequest

# 图型的来源。三态是这台机器的全部输出：
#
#   - `request`：用户在下拉里选的（请求层已经带了 `diagram_type`）；
#   - `inferred`：从 `goal` 的文字里读出来的（本模块）；
#   - `model`：两边都没有，最终由工具的 `diagram_type` 参数（也就是模型）决定。
#
# 为什么要分这么细：用户问"我要的鱼骨图怎么成了思维导图"时，答案不在这三种里就
# 一定在"根本没有分辨率"那一种里。只记一个 final 值，三件事会被压成同一句话。
SOURCE_REQUEST = "request"
SOURCE_INFERRED = "inferred"
SOURCE_MODEL = "model"


@dataclass(frozen=True)
class Inferred:
    """一次识别 + 剥离的结果。

    `goal` 是**剥完之后**的主题；`stripped` 是真的从原文里移走的那一段
    （`"用鱼骨图"`），没移走时为 `None`。
    """

    diagram_type: str
    goal: str
    stripped: str | None


@dataclass(frozen=True)
class DiagramIntent:
    """"这次图型是怎么定的"。

    `source` 为 `None` 表示**还没解析过**（`of()` 对没经过 `apply()` 的请求的返回）。
    它与 `SOURCE_MODEL` 不是一回事：前者是"这一层没跑"，后者是"跑了，两边都没说"。
    """

    diagram_type: str | None
    source: str | None
    stripped: str | None


# ---------------------------------------------------------------- 识别

# 图型词表。**顺序只在同一位置上决定胜负**（见 `_first`），不是全局优先级：
# 用户先说的那个词更接近他的真实意图。「不要流程图，画成树状图」里两个词都在，
# 谁在前面谁赢——而"否定"这件事本模块不处理（判错的代价见文件头）。
_FISHBONE = re.compile(r"鱼骨(?:图)?")
_TREE = re.compile(r"思维导图|脑图|树状图")

# 「流程图」整词：三个字连在一起，几乎不可能是别的意思。
_FLOW_WORD = re.compile(r"流程图")

# 祈使动词。顺序里长词在前，避免 `画成` 被 `画个` 之类的短支先吃掉。
_PLAN_VERBS = r"画一个|画一张|画成|画个|做成|生成|整理成|输出成|用|按"

# 「流程」单独出现时**只有被祈使动词支配**才算要流程图。这一条是全部误判风险的
# 来源，判据只有一句：`流程` 是不是这句话的**宾语**。
#
#   ❌「优化退货流程」  —— 没有祈使动词，"退货流程"是主题本身；
#   ❌「退货流程的痛点」—— 同上（"痛点"才是主题的中心词）；
#   ❌「流程优化」      —— 同上；
#   ✅「画成流程」      —— 被 `画成` 支配，是宾语；
#   ✅「用流程说明退货」—— 被 `用` 支配。
#
# `(?!图)` 是去重用的：`用流程图` 同时命中这两条，让 `_FLOW_WORD` 赢下位置即可。
_FLOW_IMPERATIVE = re.compile(
    rf"(?:{_PLAN_VERBS})\s*(?:一?[张个幅])?\s*流程(?!图)")

# 同一位置上先登记的赢，所以 `flowchart` 的两条挨在一起、`_FLOW_WORD` 在前。
_CANDIDATES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("fishbone", _FISHBONE),
    ("tree", _TREE),
    ("flowchart", _FLOW_WORD),
    ("flowchart", _FLOW_IMPERATIVE),
)

# 剥的时候顺手吞掉的"悬挂边"：分隔符、空白、括号、引号、破折号。两个方向共用
# 一个集合——前吞 `（` 后吞 `）`，都该吞。
_EDGE_CHARS = " \t\r\n\u3000，,、；;：:。.！!？?—–-~～·（）()【】[]《》〈〉<>“”‘’\"'"

# 图型词**紧挨着的祈使动词**一起算进"被剥掉的那一段"：只剥 `鱼骨图` 会留下一个
# 悬空的 `用`，`goal` 变成 `"机器学习 用"`。
#
# 两支：
#   1. 「把/将/对 + 至多 8 个字 + 动词」：`把退货做成一张流程图` 里 `流程图` 是被
#      `做成一张` 支配的，整段状语一起走才干净；
#   2. 光杆动词（可选地前面带一个客气词）：`请用鱼骨图分析…`。
_LEAD_VERB = re.compile(
    rf"(?:(?:请|请帮我|帮我|帮忙|麻烦)\s*)?"
    rf"(?:(?:把|将|对)[^，,。.；;：:！!？?\s]{{0,8}}?(?:{_PLAN_VERBS})"
    rf"|(?:{_PLAN_VERBS}))"
    rf"\s*(?:一?[张个幅])?\s*$"
)

# 「把退货**做成**流程」这一句里，祈使动词已经被 `_FLOW_IMPERATIVE` 吃进了匹配
# 本身，`_LEAD_VERB` 在剩下的 `"把退货"` 上找不到动词。只吞动词（这里是"做成"）
# 会把 goal 剥成 `"把退货"`——一个只剩半个状语的"主题"，模型只能围着它瞎猜。
# 所以这一支专门收拾「把/将/对 + 宾语」：它和那个动词是同一个状语的另一半。
_LEAD_OBJECT = re.compile(
    r"(?:(?:请|请帮我|帮我|帮忙|麻烦)\s*)?(?:把|将|对)[^，,。.；;：:！!？?\s]{0,8}$"
)

# 剥完之后只剩这些字，等于什么都没剩（`"帮我用鱼骨图"` → `"帮我"`）。那就不算剥过：
# 一个由客气话构成的"主题"比原句更糟，模型会把它当成要画的东西。
_FILLER = re.compile(r"[请帮我帮忙麻烦你一下的\s，,。.、；;：:！!？?]*")

# 剥完**以虚词开头**，说明这一刀切坏了：`"鱼骨图的使用方法"` 的主题就是鱼骨图这个
# 工具本身，剥掉那三个字只剩 `"的使用方法"`；`"流程图与思维导图的区别"` 同理只剩
# `"与思维导图的区别"`。这时一律回退成"不剥"——图型照样认（那是确定的），`goal`
# 一个字不动（那是保命的）。
_BROKEN_HEAD = re.compile(r"^[的地得是和与及把被在或者个将对]")


def _first(goal: str) -> tuple[str, re.Match[str]] | None:
    """最早出现的图型词；同一位置上按 `_CANDIDATES` 的顺序。"""
    best: tuple[str, re.Match[str]] | None = None
    for diagram_type, pattern in _CANDIDATES:
        match = pattern.search(goal)
        if match is None:
            continue
        if best is None or match.start() < best[1].start():
            best = (diagram_type, match)
    return best


def _span(goal: str, match: re.Match[str]) -> tuple[int, int, str]:
    """算要剥掉的区间，以及"被剥掉的那一段"（不含首尾分隔符，含祈使动词）。"""
    start = match.start()
    verb = _LEAD_VERB.search(goal[:start])
    if verb is not None:
        start = verb.start()
    else:
        # 动词在匹配内部时（`做成流程` / `用流程` 这两支），前面可能还挂着一个
        # 「把 + 宾语」状语，见 `_LEAD_OBJECT`。
        obj = _LEAD_OBJECT.search(goal[:start])
        if obj is not None:
            start = obj.start()
    end = match.end()
    core = goal[start:end].strip()
    while start > 0 and goal[start - 1] in _EDGE_CHARS:
        start -= 1
    while end < len(goal) and goal[end] in _EDGE_CHARS:
        end += 1
    return start, end, core


def _tidy(text: str) -> str:
    return text.strip(_EDGE_CHARS)


def infer(goal: str) -> Inferred | None:
    """从 `goal` 里读出图型；读不出来返回 `None`（不改 `goal`）。

    **剥完为空就保留原文**（`"用鱼骨图"` 单独一句）：那时候"鱼骨图"这三个字
    是这句话里唯一的实词，把它拿走等于给模型一个空主题。宁可让那个词当主题（模型
    会把它理解成话题，产出一张围绕"鱼骨图"这个概念的图），也不能让 `goal` 变成
    空串——契约的 `min_length=1` 会在开流前直接 422，用户拿到的是一次报错。
    """
    hit = _first(goal)
    if hit is None:
        return None
    diagram_type, match = hit
    start, end, core = _span(goal, match)
    # 两段之间补一个空格再 tidy：直接拼会把 `机器学习` 与 `分析` 粘成 `机器学习分析`。
    remainder = _tidy(goal[:start] + " " + goal[end:])
    if not remainder or _FILLER.fullmatch(remainder) or _BROKEN_HEAD.match(remainder):
        return Inferred(diagram_type, goal, None)
    return Inferred(diagram_type, remainder, core)


# ---------------------------------------------------------------- 接线


def of(request: "AgentRunRequest") -> DiagramIntent:
    """只读：这次请求的图型是怎么定的（没解析过时 `source is None`）。"""
    source = request._diagram_source
    if source is None:
        return DiagramIntent(request.diagram_type, None, None)
    return DiagramIntent(request.diagram_type, source, request._diagram_stripped)


def apply(request: "AgentRunRequest") -> DiagramIntent:
    """解析并把结果**写回请求**（幂等）：`goal` 剥干净、`diagram_type` 定下来。

    位置在 `app/agent/router.py::stream_run` 的 `_validate(body)` 之后——那里是
    "请求已经合法、还没有任何一个字节发出去"的唯一时刻。放在 `_generate_deep`
    里不行：`tests/test_agent_diagram_type.py` 用 `2ed92ac` 的旧模块钉着"请求不带
    图型时产出逐字节一致"，而在工具层推断会让那条回归在自己的 goal 上翻车
    （它构造的 goal 恰好就是「画个流程图」）。请求层的解析就该发生在请求层。

    **只有 `create` 会推断**：`expand` / `edit` 的 `goal` 是"改哪儿"，剥掉里面的
    图型词会连指令一起改（「把这一支改成流程图」剥完只剩「把这一支改成」）。
    """
    recorded = of(request)
    if recorded.source is not None:
        return recorded

    if request.intent != "create":
        intent = DiagramIntent(
            request.diagram_type,
            SOURCE_REQUEST if request.diagram_type else SOURCE_MODEL,
            None,
        )
    elif request.diagram_type is not None:
        # 请求已经指定：**不覆盖**，也不剥 `goal`——用户选了下拉、又在句子里重复
        # 一遍图型词时，那个词是他对自己主题的描述（"用流程图说明报销"），
        # 不是需要清理的噪声。
        intent = DiagramIntent(request.diagram_type, SOURCE_REQUEST, None)
    else:
        hit = infer(request.goal)
        if hit is None:
            intent = DiagramIntent(None, SOURCE_MODEL, None)
        else:
            intent = DiagramIntent(hit.diagram_type, SOURCE_INFERRED, hit.stripped)
            request.diagram_type = hit.diagram_type
            if hit.goal != request.goal:
                request.goal = hit.goal

    request._diagram_source = intent.source
    request._diagram_stripped = intent.stripped
    return intent
