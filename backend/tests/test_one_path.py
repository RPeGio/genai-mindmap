"""一条生成路的契约：`create` 只有**整树一次生成**，形态开关已经不存在。

`MINDMAP_SOURCE_MODE=full`（正文整份进 prompt）与它的提示词早就删了
（决策与代价见 `docs/ONE-PATH.md`）。这个文件钉四件事：

  1. **注册表只有一次生成**：`create` 无论有没有来源、库里有没有材料，生成工具都
     只有 `mindmap_generate`。分片（`mindmap_generate_deep`）的代码还在，但**不在
     `create` 的名字表里**——这是"它没被注册回主路径"里最容易静默退回去的一处。
  2. **形态开关真的没了**：`generation.service` 里那个形态判定函数与它的常量都不在，
     `app/` 下没有任何一处读 `MINDMAP_SOURCE_MODE`。
  3. **三个材料入口行为还在**：无材料 → 外部检索、带材料 → 按支检索（每支一次，
     不是一次）、材料过短 → 退化成主题 + 外部补全。三条都是端到端的：从
     `build_registry` 进、到 `ctx.candidate` 与 `content` 出。
  4. **没有退化成"一次主题检索"**：这条路保留了分片按支检索的证据形状——命中
     按分支分组进 prompt、每支各发一次检索。退回去的表现是 `queries` 只有一条，
     而那正是当年整树路径覆盖 5.1% 的原因。

为什么这些用例不写在 `test_deep_slicing.py`：那个文件钉的是**检索覆盖度**的机制
（每支只看自己的 hits、并发、降级），这个文件钉的是**入口语义**（材料怎么来、
没有材料时发生什么、一次调用怎么写完整棵树）。混在一起时，一次失败读不出是哪
一层的契约坏了。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from app.agent import tools as tools_module
from app.agent.contracts import AgentRunRequest, RunBudget, RunContext, ToolCall
from app.agent.tools import (
    DETAIL_ONLY_PROMPT,
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
from app.prefs import service as prefs

APP_ROOT = Path(__file__).resolve().parents[1] / "app"

TOPIC = "碳中和"
BRANCHES = ("政策", "技术路径", "企业案例")

# 第三段（只写说明）的返回正文。长度按提示词的目标区间（80–150 字）写：这一段在
# 用例里要验的是"说明按 id 对号落回树上"，写一句短语会把断言变成在测兜底。
_DETAIL_TEXT = (
    "这里是一段写到八十个字以上的具体说明：它说清这个要点是什么、怎么做、"
    "在什么条件下成立，并给出可以核对的数字或判断标准，而不是把标题换一种说法。"
)

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


def _evidence_groups(user: str) -> dict[str, list[dict]]:
    """取出 user 里按分支分组的 evidence 数组：`标题 -> 命中列表`。

    分组表头是**一行**、数组是**下一行**（`json.dumps` 不换行）。按行读而不是按
    空行切：父块 `quote` 里可能本来就有连续换行，按空行切会把数组切坏。
    """
    groups: dict[str, list[dict]] = {}
    lines = user.splitlines()
    for index, line in enumerate(lines):
        if "的检索结果（evidence 数组）：" not in line:
            continue
        title = line.split("「", 1)[1].split("」", 1)[0]
        groups[title] = json.loads(lines[index + 1])
    return groups


def _ref_from(hit: dict) -> dict:
    """照抄一条命中——用 `evidence_*` 那一组（真正命中的 200 字，带绝对值偏移）。

    没有 `evidence_*`（单级检索）时只抄 `quote`、且**不超过 300 字**并省掉 start/end：
    这是提示词明确要求模型的抄法（超长只截一段连续的），也是 `SourceRef.quote` 的
    长度上限。整段 370 字的父块原样抄过去会被逐字校验判不合格丢掉，看起来像
    "引用被校验剔了"，其实模型没按规矩抄。
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
    args = {"topic": TOPIC, "branches": 3}
    args |= arguments or {}
    registry = build_registry(
        "create",
        has_sources=bool(ctx.request.sources) if has_sources is None else has_sources,
        has_library=False,
    )
    with patch("app.agent.tools.chat_async", new=chat):
        return asyncio.run(registry.execute(
            ToolCall(call_id="c-gen", name="mindmap_generate", arguments=args),
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


def _tree_chat(seen: list[tuple[str, str]] | None = None,
               branches: tuple[str, ...] = BRANCHES):
    """模型替身：骨架给 `branches`，结构那一次写出整棵树（每支两个要点），
    第三段（只写说明）按 id 回一份说明。

    结构与说明是**两次**调用（见 `tools._fill_details`）：结构那次只写 id / label /
    引用，说明由第三次调用按节点 id 逐个写回。用例据此断言调用次数。
    引用照抄**本支**分组的第一个命中：收尾的逐字校验会剔掉编造的引用，而这条
    用例要验的正是"证据的引用活到了候选树上"。
    """
    async def chat(system: str, user: str, *, max_tokens=None, on_delta=None) -> dict:
        if seen is not None:
            seen.append((system, user))
        if system.startswith(PLAN_PROMPT):
            return {"branches": list(branches)}
        if system.startswith(DETAIL_ONLY_PROMPT):
            # 只回 id → 说明；结构、引用一个都不碰（真实模型这一轮也只该回这个形状）
            ids = [line.split("id=", 1)[1].split("｜", 1)[0]
                   for line in user.splitlines() if "id=" in line]
            return {node_id: _DETAIL_TEXT for node_id in ids}
        groups = _evidence_groups(user)
        children = []
        for index, title in enumerate(branches):
            payload = groups.get(title, [])
            refs = [_ref_from(payload[0])] if payload else []
            children.append({
                "id": f"b{index}", "label": title, "children": [
                    {"id": f"b{index}-1", "label": f"{title}·一", "detail": "一句说明。",
                     "source_refs": refs},
                    {"id": f"b{index}-2", "label": f"{title}·二", "detail": "另一句说明。"},
                ],
            })
        return {"title": TOPIC, "root": {
            "id": "root", "label": TOPIC, "detail": "中心主题", "children": children,
        }}
    return chat


def _generate_users(seen: list[tuple[str, str]]) -> list[str]:
    """结构那一次的 user 消息（这条路只该有一条）。

    **不含第三段**（只写说明）：那一次的 system 是 `DETAIL_ONLY_PROMPT`，它拿到的
    是 id / label / 层级的清单，不是"这次要写整棵树"的请求。混在一起数，会把
    "结构只写一次"这条断言变成"两份 user 里有一份像"，而两者要验的是不同的事。
    """
    return [user for system, user in seen
            if not system.startswith(PLAN_PROMPT)
            and not system.startswith(DETAIL_ONLY_PROMPT)]


# ---------------------------------------------------------------- 1. 注册表只有一次生成


@pytest.mark.parametrize("has_sources", [False, True])
@pytest.mark.parametrize("has_library", [False, True])
def test_create_intent_offers_only_the_single_call_generator(has_sources, has_library):
    """`create` 的生成工具**只有一个**，与来源/知识库无关。

    早先这里是"按形态二选一"（`full` 给整树、`rag` 给分片），后来变成"只有分片"。
    形态删掉之后，任何一组入参都该得到同一个答案——留一条形态分支就会让"带材料的
    请求走哪条路"重新变成一个需要读代码才能回答的问题。
    """
    registry = build_registry("create", has_sources=has_sources,
                              has_library=has_library)
    names = registry.names()
    # 生成工具只有一个；除了它，`create` 只可能多一个 `rag_query`
    # （请求不带材料但库里有东西时，检索是模型唯一能碰到原文的入口）。
    assert [name for name in names if "generate" in name] == ["mindmap_generate"]
    assert set(names) - {"mindmap_generate"} <= {"rag_query"}


def test_the_single_call_generator_is_the_only_create_tool_that_generates():
    """更硬的一条：`create` 的可选工具集里除了检索，只有它。"""
    names = build_registry("create", has_sources=True, has_library=True).names()
    assert set(names) <= {"mindmap_generate", "rag_query"}, names


def test_the_sliced_generator_survives_but_is_not_registered_under_create():
    """分片那条路**代码还在**，只是不挂在 `create` 上。

    它服务的是"一次响应写不下 200+ 节点"的大图（见 `_generate_deep_spec` 的
    docstring）。把它从代码里删掉与把它注册回 `create` 是两种不同的错：后者会让
    真实用法（100% 无材料）重新变成 1+N 次调用与三十秒。
    """
    assert "mindmap_generate_deep" not in build_registry("create").names()
    # 它仍然可以被显式调到（`create_deep` 是给这条路留的入口，不是产品意图）：
    deep = build_registry("create_deep", has_library=False)
    assert deep.names() == ["mindmap_generate_deep"]


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
    """`full` 形态那四份提示词不再导出：`rag` 那个形态的正文入口永远不存在了。

    名字同样拼起来写，理由见上一条。注意这条与"整树一次生成回来了"不冲突：
    回来的是**一次生成这件事**，不是"正文整份塞进 prompt"那个形态——现在的整树
    生成照样只吃检索命中（`_retrieve_branches`），一个字都不读全文。
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
        # `_emit_stage` 交给消费者的是 `exclude_none=True` 的 dict，不是事件对象
        # ——这里按 dict 读，与前端收到的那一份同形。
        if frame.get("type") == "stage":
            frames.append((frame["stage"], frame["detail"], frame.get("count")))

    ctx = _ctx()
    ctx.emit = emit
    seen: list[tuple[str, str]] = []
    result = _run(ctx, _tree_chat(seen))

    assert result.ok, result.content
    # 进度帧：一帧开始（没有 count）、一帧结束（一定带 count，包括 0）。
    stages = [item for item in frames if item[0] == EXTERNAL_SEARCH_STAGE]
    assert len(stages) == 2, frames
    assert stages[0][2] is None
    assert stages[1][2] == 1

    # 外部正文进的是**检索命中**，不是 prompt 全文：system 里从来没有整份正文。
    assert "二零二五年前完成一次摸底" not in "".join(system for system, _ in seen)
    # 不可信数据的声明必须在 system 里：它约束的是模型怎么对待这些内容。
    assert "外部抓取的内容" in seen[-1][0]

    refs = _all_refs(ctx.candidate.root)
    assert refs, "外部来源的引用被校验剔掉了"
    stored = library.load(refs[0].source_id)
    assert stored is not None and stored.kind is SourceKind.web
    assert stored.metadata.url == hit.url
    assert result.data["external"]["sources"] == 1
    assert result.data["external"]["failed"] is None
    assert "GENERATION_FROM_EXTERNAL" in _codes(result)
    assert "外部检索" in result.content


def test_material_takes_the_sliced_retrieval_and_never_asks_the_outside(monkeypatch):
    """带材料 → 骨架读目录 + **每支各检索一次**；外部检索一次都不发。

    这条不是优化而是语义：用户上传了材料，"这次该依据什么"已经有答案，再去网上
    抓一份混进来，会让引用指向他自己没给过的来源。

    "每支各检索一次"是这条路从分片那里继承来的**唯一**关键能力：退回一次主题
    检索（top_k=5）时覆盖率对同一份 14,120 字材料只有 5.1%，而按支是 30.5%。
    所以这里直接数检索调用次数，不只数证据条数。
    """
    monkeypatch.setenv(EXTERNAL_KNOWLEDGE_ENV, "1")
    forbidden: list[str] = []
    monkeypatch.setattr(
        external_knowledge, "search_external",
        lambda *args, **kwargs: forbidden.append("called") or [])

    calls: list[str] = []
    real_search_branch = tools_module._search_branch

    async def counting(sources, title, intents=()):
        calls.append(title)
        return await real_search_branch(sources, title, intents)

    monkeypatch.setattr(tools_module, "_search_branch", counting)

    seen: list[tuple[str, str]] = []
    ctx = _ctx([_material()])
    # 分支标题必须对着材料自己的章节：这一条验的是"材料能被按支检索到"，
    # 用一组与材料无关的标题（比如`碳中和`那三条）只会得到三条空命中。
    material_branches = ("预算审批", "报销时限", "采购合规")
    result = _run(ctx, _tree_chat(seen, material_branches))

    assert result.ok, result.content
    assert forbidden == [], "请求自带材料时不该发外部请求"
    assert "external" not in result.data
    # 检索次数 = 分支数，不是 1
    assert calls == list(material_branches), calls
    # 骨架那一次看到了材料自带的章节（标题行，不是正文）
    plan_system, plan_user = seen[0]
    assert plan_system.startswith(PLAN_PROMPT)
    assert "预算审批" in plan_user
    # 生成只发一次，且它的 user 里有**三组**证据（按支分组，带归属）
    generate_users = _generate_users(seen)
    assert len(generate_users) == 1, generate_users
    groups = _evidence_groups(generate_users[0])
    assert set(groups) == set(material_branches), groups
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
                  _tree_chat(seen))

    assert result.ok, result.content
    assert "MATERIAL_TOO_SHORT" in _codes(result)
    assert result.data["material"]["degraded"] is True
    assert result.data["material"]["sources"] == 1
    assert result.data["material"]["chars"] == len(SHORT_TEXT)
    # 那 8 个字没有被丢掉：它们进了骨架与生成两步的 user 消息，只是不再产生证据。
    plan_user = seen[0][1]
    assert SHORT_TEXT in plan_user
    assert "不足以作为可检索的材料" in plan_user
    assert SHORT_TEXT in _generate_users(seen)[0]
    # 没有材料时 system 里必须说清"别写 source_refs"——否则模型凭习惯写出的引用
    # 会让整棵树因为 `source_usage` 为空被判 `tree_invalid`。
    assert "不要写 source_refs" in seen[0][0]
    assert "不要写 source_refs" in seen[-1][0]
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
    result = _run(ctx, _tree_chat())

    assert result.ok, result.content
    # 短材料的文字被并进了外部检索的查询词（换个用法，不是丢掉）
    assert asked and SHORT_TEXT in asked[0]
    assert "MATERIAL_TOO_SHORT" in _codes(result)
    assert "GENERATION_FROM_EXTERNAL" in _codes(result)
    assert "并用外部检索到的 1 条资料" in result.content
    refs = _all_refs(ctx.candidate.root)
    assert refs, "外部补全之后应当留下引用"


def test_external_empty_says_it_found_nothing(monkeypatch):
    """外部检索空手而归：仍然出图，但必须在 `content` 里说清"没有找到相关资料"。"""
    monkeypatch.setenv(EXTERNAL_KNOWLEDGE_ENV, "1")
    monkeypatch.setattr(external_knowledge, "search_external", lambda *a, **k: [])

    result = _run(_ctx(), _tree_chat())

    assert result.ok, result.content
    assert "GENERATION_WITHOUT_EXTERNAL" in _codes(result)
    assert "联网检索没有找到相关资料" in result.content
    assert result.data["external"]["sources"] == 0
    assert result.data["retrieval"]["mode"] == "off"


def test_external_failure_is_reported_as_a_failure_not_as_absence(monkeypatch):
    """检索出错与"源里没有"是两件事：把前者说成后者就是谎报。"""
    monkeypatch.setenv(EXTERNAL_KNOWLEDGE_ENV, "1")

    def boom(*args, **kwargs):
        raise RuntimeError("ConnectError: 代理没开")

    monkeypatch.setattr(external_knowledge, "search_external", boom)

    result = _run(_ctx(), _tree_chat())

    assert result.ok, result.content
    assert "外部检索出错" in result.content
    assert "联网检索没有找到相关资料" not in result.content
    assert result.data["external"]["failed"]


def test_external_switch_off_keeps_the_data_keyset(monkeypatch):
    """开关关着时 `data` 里不该多出 `external` / `material` 键：键集本身就是契约。"""
    monkeypatch.setenv(EXTERNAL_KNOWLEDGE_ENV, "0")
    forbidden: list[str] = []
    monkeypatch.setattr(
        external_knowledge, "search_external",
        lambda *args, **kwargs: forbidden.append("called") or [])

    result = _run(_ctx(), _tree_chat())

    assert result.ok, result.content
    assert forbidden == []
    assert "external" not in result.data
    assert "material" not in result.data


def test_user_switch_off_skips_the_search_and_says_so(monkeypatch):
    """用户在界面上关掉联网检索：一次外部请求都不发，而 `content` 必须说出来。

    这一条钉的是**可感知的差异**。关掉之后用户拿到的是一棵没有引用的树——与"搜了
    但一条都没搜到"长得一模一样。如果 `content` 不点明，他只会得出后一个结论
    （那句旧话术还在，见 `test_external_empty_says_it_found_nothing`），然后去换一个
    更常见的主题词重试；而正确的动作是去把开关打开。
    """
    monkeypatch.setenv(EXTERNAL_KNOWLEDGE_ENV, "1")     # 部署方允许
    prefs.set(prefs.EXTERNAL_SEARCH_KEY, "false")       # 用户在界面上关掉
    forbidden: list[str] = []
    monkeypatch.setattr(
        external_knowledge, "search_external",
        lambda *args, **kwargs: forbidden.append("called") or [])

    result = _run(_ctx(), _tree_chat())

    assert result.ok, result.content
    assert forbidden == [], "开关关着时不该发外部请求"
    # 诊断码分开：那条旧码的语义是"试过了没拿到"（可以重试），这条是"根本没试"
    assert "GENERATION_EXTERNAL_DISABLED" in _codes(result)
    assert "GENERATION_WITHOUT_EXTERNAL" not in _codes(result)
    assert "你关掉了联网检索" in result.content
    # 两件事都不能出现："没搜到"的结论、以及关着时无效的那条建议
    assert "没有搜到外部资料" not in result.content
    assert "换一个更常见的主题词" not in result.content
    # 有效动作写出来了（就在那句里，不是另起一句）
    assert "要出处就打开联网检索再生成一次" in result.content
    # 同一句话不能出现两遍：常量本身就是完整句，拼接处再补一遍就成同义反复
    assert result.content.count("你关掉了联网检索") == 1
    # 键集不变：不多出 `external`（与闸门关着时逐字节相同的那条约定）
    assert "external" not in result.data
    assert result.data["retrieval"]["mode"] == "off"


def test_deployment_switch_off_reports_a_different_code_than_a_failed_search(monkeypatch):
    """部署方关掉（环境变量）与"搜了没搜到"必须是两个码。

    这一支与上面那条测的不是同一件事：那条是**用户**在界面上关的，诊断文案要给出
    他能做的动作（去把开关打开）；这一条是**部署方**关的，用户没有任何动作可做，
    所以它连诊断都不发——行为与加这个开关之前逐字节相同。这里钉的正是那个区别：
    部署方关掉时 `GENERATION_WITHOUT_EXTERNAL` 不出现，而"没搜到"那套话术也不出现。
    """
    monkeypatch.setenv(EXTERNAL_KNOWLEDGE_ENV, "0")
    monkeypatch.setattr(
        external_knowledge, "search_external",
        lambda *args, **kwargs: pytest.fail("开关关着时不该发外部请求"))

    result = _run(_ctx(), _tree_chat())

    assert result.ok, result.content
    # 部署方关掉：不报回退码（那是"试过了没拿到"），也不报用户开关那个码
    assert "GENERATION_WITHOUT_EXTERNAL" not in _codes(result)
    assert "GENERATION_EXTERNAL_DISABLED" not in _codes(result)
    assert "联网检索没有找到相关资料" not in result.content
    assert "external" not in result.data
    assert result.data["retrieval"]["mode"] == "off"


def test_turning_it_back_on_restores_the_search(monkeypatch):
    """对照组：同一条链路，偏好是 `true` 时外部检索照跑，引用真的落回树上。

    "关掉有差别"这句话只有在打开时确实不同时才成立；少了这一条，上面那条断言只能
    证明"某一次生成没有引用"，证不出是因为开关。
    """
    monkeypatch.setenv(EXTERNAL_KNOWLEDGE_ENV, "1")
    prefs.set(prefs.EXTERNAL_SEARCH_KEY, "true")
    hit = _hit(Source(kind=SourceKind.web, status="parsed", text=EXTERNAL_TEXT))
    asked: list[str] = []
    monkeypatch.setattr(
        external_knowledge, "search_external",
        lambda query, *, limit: asked.append(query) or [hit])

    ctx = _ctx()
    result = _run(ctx, _tree_chat())

    assert result.ok, result.content
    assert asked == [TOPIC]
    assert "GENERATION_EXTERNAL_DISABLED" not in _codes(result)
    assert result.data["external"]["sources"] == 1
    assert _all_refs(ctx.candidate.root), "开着时引用应当活到候选树上"


def test_external_switch_off_does_not_fire_when_material_is_present(monkeypatch):
    """带材料那一路不说"你关掉了联网检索"：用户根本没要求联网。"""
    monkeypatch.setenv(EXTERNAL_KNOWLEDGE_ENV, "0")

    result = _run(_ctx([_material()]), _tree_chat(seen=None,
                                                  branches=("预算审批", "报销时限", "采购合规")))

    assert result.ok, result.content
    assert "GENERATION_EXTERNAL_DISABLED" not in _codes(result)
    assert "你关掉了联网检索" not in result.content


def test_external_cache_hit_is_reported_in_data(monkeypatch):
    """`data.external.cached` 必须能分开"缓存命中"与"真搜了一次网"。

    少了它，`seconds` 从 4.5 秒掉到 0.02 秒会被读成"外部检索不稳定"，而真相是
    "这个主题 24 小时内搜过"。
    """
    monkeypatch.setenv(EXTERNAL_KNOWLEDGE_ENV, "1")
    hit = _hit(Source(kind=SourceKind.web, status="parsed", text=EXTERNAL_TEXT))
    monkeypatch.setattr(
        external_knowledge, "search_external",
        lambda *args, **kwargs: external_knowledge_module.ExternalHits([hit], cached=True))

    result = _run(_ctx(), _tree_chat())

    assert result.ok, result.content
    assert result.data["external"]["cached"] is True
    # 缓存命中照样走完整条链：入库、检索、引用都在（缓存跳过的只有 HTTP）
    assert result.data["external"]["sources"] == 1
    assert "GENERATION_FROM_EXTERNAL" in _codes(result)


def test_external_cache_miss_reports_false(monkeypatch):
    """普通 `list`（没有 `cached` 属性）读作"没命中缓存"：十几条用例都是这么 patch 的。"""
    monkeypatch.setenv(EXTERNAL_KNOWLEDGE_ENV, "1")
    hit = _hit(Source(kind=SourceKind.web, status="parsed", text=EXTERNAL_TEXT))
    monkeypatch.setattr(external_knowledge, "search_external", lambda *a, **k: [hit])

    result = _run(_ctx(), _tree_chat())

    assert result.ok, result.content
    assert result.data["external"]["cached"] is False


def test_no_source_rules_forbid_fake_precision():
    """无引用时没有任何东西能拦住编造，所以提示词要正面要求"别写看起来精确的东西"。

    有引用时逐字校验至少能拦住"引文是编的"（模型挂上出处的那句话必须能在原文里
    逐字找到）。无引用时那条防线不存在：一个"2015 年《自然》上那篇论文指出……"的
    节点与一句正确的结论在界面上长得一模一样，而前者随时可能是三个真实碎片被拼成
    一件没发生过的事。
    """
    rules = generation_service.DETAIL_NO_SOURCE_RULES

    assert "没有任何可核对的原文" in rules
    for forbidden in ("年份", "人名", "论文标题", "数字统计"):
        assert forbidden in rules, forbidden
    assert "不确定的就略过" in rules
    # 引用那条老约束不能被新句子挤掉：没有材料时写 `source_refs` 会让整棵树作废。
    assert "不要写 source_refs" in rules
