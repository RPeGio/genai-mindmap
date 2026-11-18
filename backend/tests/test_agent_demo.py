"""agent 的演示模式：没有模型密钥的机器上也要能出一张图。

`MINDMAP_LLM_MODE=demo` 时 `run()` 不进模型循环，直接发 stage → snapshot → done
三帧，占位内容复用 `generation.service._demo_content`。

三件事必须锁住：
  - 一帧模型都不发（假 provider 的调用次数就是证据，它一次都不该被调到）；
  - 占位树就是旧路径那一份，不是在这里另写一份；
  - 只有显式 `demo` 才走这条路，缺省值仍然是"真跑模型"。
"""
from __future__ import annotations

import asyncio
import os
import unittest
from typing import Any
from unittest.mock import patch

from app.agent.contracts import AgentRunRequest, Completion, RunBudget
from app.agent.service import run
from app.agent.tools import build_registry
from app.generation.service import _demo_content

TOPIC = "设计一款智能家居产品"
DEMO_CHILD_LABEL = "请编辑此演示节点"


class _ScriptedProvider:
    """按脚本返回预设结果的假 provider。

    这里只用到它的 `calls` 计数：demo 路径不该走到模型，计数停在 0 就是证据。
    """

    def __init__(self, script: list[Any] | None = None) -> None:
        self._script = list(script or [])
        self.calls = 0

    async def complete_tools(self, messages, tools, *, budget):
        self.calls += 1
        if not self._script:
            raise AssertionError("脚本已用完，循环比预期多调了一次模型")
        return self._script.pop(0)


def _run(coro):
    return asyncio.run(coro)


def _drain(request: AgentRunRequest, provider: _ScriptedProvider) -> list[Any]:
    async def collect():
        return [event async for event in
                run(request, provider=provider, tools=build_registry(request.intent))]

    return _run(collect())


class DemoModeTest(unittest.TestCase):
    def _events(self, *, goal: str = TOPIC) -> tuple[list[Any], _ScriptedProvider]:
        provider = _ScriptedProvider()
        request = AgentRunRequest(
            goal=goal, intent="create", budget=RunBudget(max_model_calls=1),
        )
        with patch.dict(os.environ, {"MINDMAP_LLM_MODE": "demo"}):
            return _drain(request, provider), provider

    def test_demo_emits_stage_snapshot_done(self):
        """三帧、顺序固定、seq 与其他帧共用同一个从 1 起的计数器。"""
        events, _ = self._events()

        self.assertEqual([event.type for event in events], ["stage", "snapshot", "done"])
        self.assertEqual([event.seq for event in events], [1, 2, 3])
        self.assertEqual(events[0].stage, "demo")
        self.assertEqual(events[0].detail, "演示模式，不调用模型")
        # usage 显式给空表：这一路一次模型调用都没有，不该出现任何用量数字。
        self.assertEqual(events[-1].usage, {})

    def test_content_is_the_shared_placeholder_tree(self):
        """占位树是旧路径那一份（根标题截 60 字 + 一个可编辑子节点），不是另写一份。"""
        goal = "目" * 80
        events, _ = self._events(goal=goal)
        snapshot, done = events[1], events[2]

        expected = _demo_content(goal)
        self.assertEqual(snapshot.content.root.label, goal[:60])
        self.assertEqual(done.content.title, expected.title)
        self.assertEqual([child.label for child in done.content.root.children],
                         [DEMO_CHILD_LABEL])
        # snapshot 与 done 是同一棵树：前端两次都拿到内容，不会一帧空一帧满。
        self.assertEqual(snapshot.content, done.content)

    def test_demo_never_calls_the_model(self):
        """假 provider 一次都没被调到：demo 的意义就是不依赖模型服务。"""
        _, provider = self._events()

        self.assertEqual(provider.calls, 0)

    def test_done_frame_declares_demo_mode(self):
        """done 帧必须说明这棵树是占位树。

        前端落地时会照这个字段标来源；没有它就只能一律写"模型生成"，
        一张没经过模型的图会被冒充满分答案。缺省 None 表示"没说"，永远不该
        被理解成 model。
        """
        events, _ = self._events()

        self.assertEqual(events[-1].type, "done")
        self.assertEqual(events[-1].mode, "demo")


class DemoContentShapesTest(unittest.TestCase):
    """占位内容按**图型**产出：没有密钥的机器上也要能演示鱼骨与流程。

    恒为树时，界面上选了"流程图"出来一棵导图——那不是演示，是让人以为功能坏了。
    所以这里钉两件事：形状对得上请求的 `diagram_type`，且占位内容自己是**合法**的
    契约实例（它会走和真产物同一条落库/渲染路径，形状不合法的话演示模式自己就 422）。
    """

    def test_demo_follows_the_requested_diagram_type(self):
        from app.contracts.mindmap import FishboneContent, FlowContent, MindMapContent

        self.assertIsInstance(_demo_content(TOPIC), MindMapContent)
        bone = _demo_content(TOPIC, "fishbone")
        flow = _demo_content(TOPIC, "flowchart")

        self.assertIsInstance(bone, FishboneContent)
        self.assertTrue(bone.problem)
        self.assertEqual(len(bone.bones), 3)
        self.assertIsInstance(flow, FlowContent)
        self.assertEqual(len(flow.stages), 3)
        # 三个阶段、两条阶段间跳转：占位也要看得出"这是一条流程"，否则画布上
        # 只有三个并排的盒子，主轴是断的。
        self.assertEqual(len(flow.edges), 2)

    def test_demo_content_passes_the_contract_of_its_own_shape(self):
        from pydantic import TypeAdapter

        from app.contracts.mindmap import DiagramContent

        adapter: TypeAdapter = TypeAdapter(DiagramContent)
        for kind in ("tree", "fishbone", "flowchart"):
            content = _demo_content(TOPIC, kind)
            again = adapter.validate_python(content.model_dump())
            self.assertIs(type(again), type(content))

    def test_demo_run_emits_the_shape_asked_for_in_the_request(self):
        """端到端：请求里选了流程图，demo 出来的就是流程（不是一棵树）。"""
        from app.contracts.mindmap import FlowContent

        provider = _ScriptedProvider()
        request = AgentRunRequest(
            goal=TOPIC, intent="create", diagram_type="flowchart",
            budget=RunBudget(max_model_calls=1),
        )
        with patch.dict(os.environ, {"MINDMAP_LLM_MODE": "demo"}):
            events = _drain(request, provider)

        self.assertIsInstance(events[1].content, FlowContent)
        self.assertEqual(len(events[1].content.stages), 3)
        self.assertEqual(provider.calls, 0)


class NotDemoModeTest(unittest.TestCase):
    def test_unset_mode_still_runs_the_model_loop(self):
        """缺省值不是 demo：没配这个变量时必须照常进模型循环。

        旧路径的「没配置就走 demo」是给普通生成用的；照搬到 agent 上等于把默认
        路径静默降级成占位图，而这正是要成为默认的那条路。
        """
        provider = _ScriptedProvider([
            Completion(message={"role": "assistant", "content": "好了"}, finish_reason="stop"),
            # 零工具调用的收尾会先被提醒一次（见 service.MAX_NUDGES）；第二段
            # 仍然只写字，循环就认了。这条用例只关心"没走 demo、真的调了模型"。
            Completion(message={"role": "assistant", "content": "还是文字"}, finish_reason="stop"),
        ])
        request = AgentRunRequest(goal=TOPIC, intent="create")

        with patch.dict(os.environ, {"MINDMAP_LLM_MODE": ""}):
            events = _drain(request, provider)

        self.assertEqual(provider.calls, 2)
        self.assertEqual(events[-1].type, "done")
        self.assertIsNone(events[-1].content)
        self.assertEqual(events[-1].mode, "model")


if __name__ == "__main__":
    unittest.main()
