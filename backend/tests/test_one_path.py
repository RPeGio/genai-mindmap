"""一条生成路的契约：`create` 只有分片，形态开关已经不存在。

这次删除把 `MINDMAP_SOURCE_MODE=full`（正文整份进 prompt）与整树一次成型的
`mindmap_generate` 一起去掉了（决策与代价见 `docs/ONE-PATH.md`）。这个文件钉三件事：

  1. **注册表只有分片**：`create` 无论有没有来源、库里有没有材料，生成工具都只有
     `mindmap_generate_deep`。这是"删干净"里最容易退回去的一处——把整树加回来
     只需要在 `_ALLOWED_BY_INTENT` 里多写一个名字。
  2. **形态开关真的没了**：`generation.service` 里那个形态判定函数与它的常量都不在，
     `app/` 下没有任何一处读 `MINDMAP_SOURCE_MODE`。留着一条只认一个值的分支，
     下一个人就会以为它还能切回去。
  3. **原属整树路的三个入口行为还在**（它们随 `_generate` 一起搬进了分片）：
     无材料 → 外部检索、带材料 → 按支检索、材料过短 → 退化成主题 + 外部补全。
     这三条是端到端的：从 `build_registry` 进、到 `ctx.candidate` 与 `content` 出。

为什么这些用例不写在 `test_deep_slicing.py`：那个文件钉的是**检索覆盖度**的机制
（每支只看自己的 hits、并发、降级），这个文件钉的是**入口语义**（材料怎么来、
没有材料时发生什么）。混在一起时，一次失败读不出是哪一层的契约坏了。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
from app.agent.tools import (
    EXTERNAL_KNOWLEDGE_ENV,
    EXTERNAL_SEARCH_STAGE,
    PLAN_PROMPT,
    build_registry,
    external_knowledge,
)
from app.contracts.source import Source, SourceKind
from app.generation import service as generation_service
from app.knowledge import external as external_knowledge_module
from app.knowledge import library

APP_ROOT = Path(__file__).resolve().parents[1] / "app"

TOPIC = "碳中和"
BRANCHES = ("政策", "技术路径", "企业案例")

# 外部命中的正文：三节分别对上三条一级分支的标题，BM25 才可能按支召回。
# 标题行是刻意的——真实抓回来的维基正文就长这样，而切块器按标题行切。
EXTERNAL_TEXT = (
    "# 碳中和\n\n"
    "## 政策\n\n"
    "碳中和政策要求重点行业制定碳达峰路线图，二零二五年前完成一次摸底，"
    "并把结果写进年度报告。\n\n"
    "## 技术路径\n\n"
    "技术路径包括可再生能源替代、碳捕集与封存、电气化改造三类，"
    "三类的单位成本差别很大。\n\n"
    "## 企业案例\n\n"
    "企业案例显示钢铁与水泥行业先行试点，改造成本由专项债覆盖，"
    "试点期三年。\n"
)

# 材料（用户上传的那种）：章节标题 + 正文。**必须超过 `SHORT_MATERIAL_CHARS=300`**，
# 否则这条用例测的就变成退化路径了（`test_short_material_*` 才是那条）。
MATERIAL_TEXT = (
    "# 预算管理办法\n\n"
    "## 预算审批\n\n"
    "预算由财务经理核准后执行，超过五十万元报总经理审批。"
    "每季度末做一次预算执行分析，偏差超过百分之十的需要书面说明原因。"
    "年度预算在十一月启动编制，各部门按上一年的执行率申报，财务汇总后报董事会。\n\n"
    "## 报销时限\n\n"
    "费用报销的时限是三十个自然日，逾期不再受理；跨年度的单据必须附说明。"
    "报销单需要部门负责人签字，财务只做形式审核，实质合规由内审抽查。"
    "差旅费按标准报销，超标部分需要总经理特批，特批记录留存三年备查。\n\n"
    "## 采购合规\n\n"
    "采购合同必须经过法务审核，金额超过五十万元另行招标。"
    "供应商准入每两年复核一次，出现过重大违约的记录在案，三年内不得再次准入。\n\n"
    "## 附则\n\n"
    "本办法由财务部负责解释，自发布之日起执行；此前发布的相关规定同时废止。"
    "各部门可以在本办法框架内制定实施细则，报财务部备案后生效。\n"
)

# 8 字材料：真实案例（`rust 学习路线`）。低于 `SHORT_MATERIAL_CHARS=300`，整份
# 都塞得进一条引用，所以它只能当主题提示。
SHORT_TEXT = "rust 学习路线"


def _request(sources: list[Source] | None = None,
             max_model_calls: int = 20) -> AgentRunRequest:
    return AgentRunRequest(
        goal=TOPIC, intent="create", thinking_model="default",
        sources=sources or [],
        budget=RunBudget(max_model_calls=max_model_calls, max_tool_calls=8,
                         max_seconds=60.0, max_output_tokens=8192, max_repairs=1),
    )


def _ctx(sources: list[Source] | None = None) -> RunContext:
    payload = sources or []
    return RunContext(
        request=_request(payload),
        sources_by_id={source.id: source for source in payload},
    )


def _material(text: str = MATERIAL_TEXT, *, source_id: str = "s-budget") -> Source:
    return Source(id=source_id, kind=SourceKind.document, status="parsed",
                  filename="预算管理办法.docx", text=text)


def _hit(source: Source) -> external_knowledge_module.ExternalHit:
    return external_knowledge_module.ExternalHit(
        title=TOPIC, text=source.text, url="https://zh.wikipedia.org/wiki/碳中和",
        source_name="wikipedia", fetched_at="2026-01-01T00:00:00Z", language="zh",
    )


def _branch_of(user: str) -> str:
    return user.split("本条一级分支：", 1)[1].split("\n", 1)[0]


def _evidence_of(user: str) -> list[dict]:
    """取出拼在 user 末尾的 evidence 数组；没有就是空。"""
    if "evidence 数组）：\n" not in user:
        return []
    return json.loads(user.split("evidence 数组）：\n", 1)[1])


def _ref_from(hit: dict) -> dict:
    """照抄一条命中——用 `evidence_*` 那一组（真正命中的 200 字，带绝对值偏移）。

    没有 `evidence_*`（单级检索）时只抄 `quote`、且**不超过 300 字**并省掉 start/end：
    这是提示词明确要求模型的抄法（超长只截一段连续的），也是 `SourceRef.quote` 的
    长度上限。整段 370 字的父块原样抄过去会被 `_ref_from` 当成不合格丢掉，看起来
    像"引用被校验剔了"，其实模型没按规矩抄。
    """
    ref = {"source_id": hit["source_id"]}
    if hit.get("evidence_quote"):
        ref |= {"start": hit["evidence_start"], "end": hit["evidence_end"],
                "quote": hit["evidence_quote"]}
    else:
        ref |= {"quote": hit["quote"][:300]}
    return ref


def _run(ctx: RunContext, chat, *, arguments: dict | None = None,
         has_sources: bool | None = None):
    args = {"topic": TOPIC, "branches": 3, "points_per_branch": 2, "max_depth": 2}
    args |= arguments or {}
    registry = build_registry(
        "create",
        has_sources=bool(ctx.request.sources) if has_sources is None else has_sources,
        has_library=False,
    )
    with patch("app.agent.tools.chat_async", new=chat):
        return asyncio.run(registry.execute(
            ToolCall(call_id="c-deep", name="mindmap_generate_deep", arguments=args),
            ctx,
        ))


def _all_refs(root) -> list:
    """整棵树上的引用（引用挂在**要点**节点上，不在它的一级分支上）。"""
    stack, refs = [root], []
    while stack:
        node = stack.pop()
        refs.extend(node.source_refs)
        stack.extend(node.children)
    return refs


def _codes(result) -> list[str]:
    return [item["code"] for item in result.data["diagnostics"]]


def _citable_chat(seen: list[tuple[str, str]] | None = None,
                  branches: tuple[str, ...] = BRANCHES):
    """模型替身：骨架给 `branches`、每支两个要点，引用照抄本支 evidence 的第一条。

    引用必须**照抄**而不是自己编：收尾的逐字校验会剔掉编造的引用，而这条用例要
    验的正是"外部来源的引用活到了候选树上"。
    """
    async def chat(system: str, user: str, *, max_tokens=None, on_delta=None) -> dict:
        if seen is not None:
            seen.append((system, user))
        if system.startswith(PLAN_PROMPT):
            return {"branches": list(branches)}
        payload = _evidence_of(user)
        refs = [_ref_from(payload[0])] if payload else []
        return {"children": [
            {"label": f"{_branch_of(user)}·一", "expandable": False, "source_refs": refs},
            {"label": f"{_branch_of(user)}·二", "expandable": False},
        ]}
    return chat


# ---------------------------------------------------------------- 1. 注册表只有分片


@pytest.mark.parametrize("has_sources", [False, True])
@pytest.mark.parametrize("has_library", [False, True])
def test_create_intent_offers_only_the_sliced_generator(has_sources, has_library):
    """`create` 的生成工具**只有一个**，与来源/知识库无关。

    早先这里是"按形态二选一"：`full` 下给整树、`rag` 下给分片。形态删掉之后，
    任何一组入参都该得到同一个答案——留一条形态分支就会让"带材料的请求走哪条路"
    重新变成一个需要读代码才能回答的问题。
    """
    registry = build_registry("create", has_sources=has_sources,
                              has_library=has_library)
    names = registry.names()
    # 生成工具只有一个；除了它，`create` 只可能多一个 `rag_query`
    # （请求不带材料但库里有东西时，检索是模型唯一能碰到原文的入口）。
    assert [name for name in names if "generate" in name] == ["mindmap_generate_deep"]
    assert set(names) - {"mindmap_generate_deep"} <= {"rag_query"}


def test_the_sliced_generator_is_the_only_create_tool_that_generates():
    """更硬的一条：`create` 的可选工具集里除了检索，只有它。"""
    names = build_registry("create", has_sources=True, has_library=True).names()
    assert set(names) <= {"mindmap_generate_deep", "rag_query"}, names


# ---------------------------------------------------------------- 2. 开关真的没了


def test_source_mode_switch_is_gone():
    """形态判定函数与它的常量都不在了，`app/` 里也没有人再读那个环境变量。

    这条不是"洁癖"：只认一个值的开关最坏的地方是**看起来还能切**。下一个人照着
    `os.getenv("MINDMAP_SOURCE_MODE")` 去找分支，会先花半天确认它是死代码。

    注释里提到这个名字是允许的（`docs/ONE-PATH.md` 要解释删了什么，代码里也要
    留一句"这条路为什么没了"）。被判红的是**读它**：`getenv` / `environ` 出现在
    同一行。

    待检名字里有几个是**拼出来**的，不是打错：验收用的那条 `rg` 会扫全仓的形态
    常量字面量，把守卫自己的断言抄成字面量就会让它误报。
    """
    gone = (
        "source" + "_mode",
        "SOURCE_MODE" + "_ENV",
        "SOURCE_MODE" + "_FULL",
        "SOURCE_MODE" + "_RAG",
        "DEFAULT_SOURCE" + "_MODE",
        "_WARNED_SOURCE" + "_MODES",
    )
    for name in gone:
        assert not hasattr(generation_service, name), name
    offenders = [
        f"{path.relative_to(APP_ROOT)}:{number}"
        for path in APP_ROOT.rglob("*.py")
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "MINDMAP_SOURCE_MODE" in line
        and ("getenv" in line or "environ" in line or "putenv" in line)
    ]
    assert offenders == []


def test_sourced_prompt_and_the_whole_tree_prompt_are_gone():
    """整树那四份提示词不再导出：`create` 走的只有分片那三份。

    名字同样拼起来写，理由见上一条。
    """
    gone = (
        "SOURCED_DETAIL" + "_PROMPT",
        "SOURCED_THINKING" + "_GUARD",
        "RAG_SOURCE" + "_PROMPT",
        "RAG_THINKING" + "_GUARD",
    )
    for name in gone:
        assert not hasattr(generation_service, name), name


# ---------------------------------------------------------------- 3. 三条端到端


def test_no_material_goes_to_external_and_keeps_the_citation(monkeypatch):
    """无材料 → 外部检索 → 入库 → 按支检索 → 引用逐字校验后仍在。

    这条用例的价值在"仍在"：`verify_source_refs(remove_invalid=True)` 会把指向
    未知来源的引用**静默删掉**，所以"没抛异常"什么都证明不了。外部来源只有先
    `library.ingest` 拿到持久 id、再并进 `ctx.library_sources`，引用才可能活下来。
    """
    monkeypatch.setenv(EXTERNAL_KNOWLEDGE_ENV, "1")
    hit = _hit(Source(kind=SourceKind.web, status="parsed", text=EXTERNAL_TEXT))
    monkeypatch.setattr(
        external_knowledge, "search_external", lambda *args, **kwargs: [hit])

    frames: list[tuple[str, str, int | None]] = []

    async def emit(frame) -> None:
        # `_emit_stage` 交给消费者的是 `exclude_none=True` 的 dict（见 `_stage_frame`），
        # 不是事件对象——这里按 dict 读，与前端收到的那一份同形。
        if frame.get("type") == "stage":
            frames.append((frame["stage"], frame["detail"], frame.get("count")))

    ctx = _ctx()
    ctx.emit = emit
    seen: list[tuple[str, str]] = []
    result = _run(ctx, _citable_chat(seen))

    assert result.ok, result.content
    # 进度帧：一帧开始（没有 count）、一帧结束（一定带 count，包括 0）。
    stages = [item for item in frames if item[0] == EXTERNAL_SEARCH_STAGE]
    assert len(stages) == 2, frames
    assert stages[0][2] is None
    assert stages[1][2] == 1

    # 外部正文进的是**检索命中**，不是 prompt 全文：system 里从来没有整份正文
    # （"正文整份进 prompt"那条路已经不存在了）。
    assert "二零二五年前完成一次摸底" not in "".join(system for system, _ in seen)
    # 不可信数据的声明必须在 system 里：它约束的是模型怎么对待这些内容。
    system = next(system for system, user in seen if "本条一级分支：" in user)
    assert "外部抓取的内容" in system

    refs = _all_refs(ctx.candidate.root)
    assert refs, "外部来源的引用被校验剔掉了"
    stored = library.load(refs[0].source_id)
    assert stored is not None and stored.kind is SourceKind.web
    assert stored.metadata.url == hit.url
    assert result.data["external"]["sources"] == 1
    assert result.data["external"]["failed"] is None
    assert "GENERATION_FROM_EXTERNAL" in _codes(result)
    assert "外部检索" in result.content


def test_material_takes_the_sliced_path_and_never_asks_the_outside(monkeypatch):
    """带材料 → 分片 + 按支检索；外部检索一次都不发。

    这条不是优化而是语义：用户上传了材料，"这次该依据什么"已经有答案，再去网上
    抓一份混进来，会让引用指向他自己没给过的来源。
    """
    monkeypatch.setenv(EXTERNAL_KNOWLEDGE_ENV, "1")
    forbidden: list[str] = []
    monkeypatch.setattr(
        external_knowledge, "search_external",
        lambda *args, **kwargs: forbidden.append("called") or [])

    seen: list[tuple[str, str]] = []
    ctx = _ctx([_material()])
    # 分支标题必须对着材料自己的章节：这一条验的是"材料能被按支检索到"，
    # 用一组与材料无关的标题（比如`碳中和`那三条）只会得到三条空命中。
    material_branches = ("预算审批", "报销时限", "采购合规")
    result = _run(ctx, _citable_chat(seen, material_branches))

    assert result.ok, result.content
    assert forbidden == [], "请求自带材料时不该发外部请求"
    assert "external" not in result.data
    # 骨架那一次看到了材料自带的章节（标题行，不是正文）
    plan_system, plan_user = seen[0]
    assert plan_system.startswith(PLAN_PROMPT)
    assert "预算审批" in plan_user
    # 每支各检索一次：展开消息里有且只有自己的 evidence
    expand_users = [user for _, user in seen if "本条一级分支：" in user]
    assert len(expand_users) == len(material_branches)
    assert all("evidence 数组）：" in user for user in expand_users)
    # 检索到的东西确实进了候选树的引用里（逐字校验之后仍在）
    refs = _all_refs(ctx.candidate.root)
    assert refs, (result.data["source_refs"], result.content)
    assert all(ref.source_id == "s-budget" for ref in refs)
    assert result.data["retrieval"]["mode"] == "branch"
    assert result.data["retrieval"]["branches_with_hits"] >= 1


def test_short_material_degrades_to_a_topic_hint(monkeypatch):
    """材料过短 → 当成"没有材料"，文字只当主题提示，且必须被说出来。

    不这么做的代价实测过：8 字材料走检索只回 1 条 9 字命中，模型据此生成 72 个
    节点、其中 7 个带引用，而界面上看不出另外 65 个是它自己补的。
    """
    monkeypatch.setenv(EXTERNAL_KNOWLEDGE_ENV, "0")
    seen: list[tuple[str, str]] = []
    result = _run(_ctx([_material(SHORT_TEXT, source_id="s-short")]),
                  _citable_chat(seen))

    assert result.ok, result.content
    assert "MATERIAL_TOO_SHORT" in _codes(result)
    assert result.data["material"]["degraded"] is True
    assert result.data["material"]["sources"] == 1
    assert result.data["material"]["chars"] == len(SHORT_TEXT)
    # 那 8 个字没有被丢掉：它们进了骨架的 user 消息，只是不再产生证据。
    plan_user = seen[0][1]
    assert SHORT_TEXT in plan_user
    assert "不足以作为可检索的材料" in plan_user
    # 没有材料时 system 里必须说清"别写 source_refs"——否则模型凭习惯写出的引用
    # 会让整棵树因为 `source_usage` 为空被判 `tree_invalid`。
    assert "不要写 source_refs" in seen[0][0]
    assert "材料只有" in result.content
    assert "外部检索未开启" in result.content
    assert result.data["retrieval"]["mode"] == "off"


def test_short_material_is_topped_up_by_external_sources(monkeypatch):
    """材料过短 + 外部有结果：短材料当查询词，依据由外部补上，且两件事都说清。"""
    monkeypatch.setenv(EXTERNAL_KNOWLEDGE_ENV, "1")
    hit = _hit(Source(kind=SourceKind.web, status="parsed", text=EXTERNAL_TEXT))
    asked: list[str] = []

    def search(query, *, limit):
        asked.append(query)
        return [hit]

    monkeypatch.setattr(external_knowledge, "search_external", search)

    ctx = _ctx([_material(SHORT_TEXT, source_id="s-short")])
    result = _run(ctx, _citable_chat())

    assert result.ok, result.content
    # 短材料的文字被并进了外部检索的查询词（换个用法，不是丢掉）
    assert asked and SHORT_TEXT in asked[0]
    assert "MATERIAL_TOO_SHORT" in _codes(result)
    assert "GENERATION_FROM_EXTERNAL" in _codes(result)
    assert "并用外部检索到的 1 条资料" in result.content
    refs = _all_refs(ctx.candidate.root)
    assert refs, "外部补全之后应当留下引用"


def test_external_empty_says_it_found_nothing(monkeypatch):
    """外部检索空手而归：仍然出图，但必须在 `content` 里说清"没有搜到外部资料"。"""
    monkeypatch.setenv(EXTERNAL_KNOWLEDGE_ENV, "1")
    monkeypatch.setattr(external_knowledge, "search_external", lambda *a, **k: [])

    result = _run(_ctx(), _citable_chat())

    assert result.ok, result.content
    assert "GENERATION_WITHOUT_EXTERNAL" in _codes(result)
    assert "没有搜到外部资料" in result.content
    assert result.data["external"]["sources"] == 0
    assert result.data["retrieval"]["mode"] == "off"


def test_external_failure_is_reported_as_a_failure_not_as_absence(monkeypatch):
    """检索出错与"源里没有"是两件事：把前者说成后者就是谎报。"""
    monkeypatch.setenv(EXTERNAL_KNOWLEDGE_ENV, "1")

    def boom(*args, **kwargs):
        raise RuntimeError("ConnectError: 代理没开")

    monkeypatch.setattr(external_knowledge, "search_external", boom)

    result = _run(_ctx(), _citable_chat())

    assert result.ok, result.content
    assert "外部检索出错" in result.content
    assert "没有搜到外部资料" not in result.content
    assert result.data["external"]["failed"]


def test_external_switch_off_keeps_the_data_keyset(monkeypatch):
    """开关关着时 `data` 里不该多出 `external` / `material` 键：键集本身就是契约。"""
    monkeypatch.setenv(EXTERNAL_KNOWLEDGE_ENV, "0")
    forbidden: list[str] = []
    monkeypatch.setattr(
        external_knowledge, "search_external",
        lambda *args, **kwargs: forbidden.append("called") or [])

    result = _run(_ctx(), _citable_chat())

    assert result.ok, result.content
    assert forbidden == []
    assert "external" not in result.data
    assert "material" not in result.data
