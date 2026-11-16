import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import AgentPanel from '../src/components/AgentPanel.vue';
import type { MindMapContent } from '../src/api/types';
import { useMindMapStore } from '../src/stores/mindmap';

/**
 * 检索期间的进度信号（`stage` 帧）上屏。
 *
 * 这条链的另一半在后端（`tests/test_agent_stage.py`）：外部检索的 4–6 秒里后端推
 * 两帧，前端必须把它们显示出来——原先这段时间界面上什么都不动，看起来像卡死。
 *
 * 两处都要看得见：状态栏（生成按钮下面那行 `stageText`）与面板（"模型协作"里
 * 检索那一行）。只做一处的话，用户盯哪儿都可能看不见。
 */

function controllableReply() {
  const encoder = new TextEncoder();
  let push: (frame: unknown) => void = () => {};
  let finish: () => void = () => {};
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      push = (frame) => { controller.enqueue(encoder.encode(`data: ${JSON.stringify(frame)}\n\n`)); };
      finish = () => {
        controller.enqueue(encoder.encode('data: [DONE]\n\n'));
        controller.close();
      };
    },
  });
  const response = { ok: true, status: 200, body, json: async () => ({}) } as unknown as Response;
  return { response, push, finish };
}

function stage(detail: string, count?: number) {
  // `count` 缺席＝开始帧，`0` 是有效值："还没结束"与"一条都没找到"必须分得开
  return count === undefined
    ? { type: 'stage', seq: 1, stage: 'external_search', detail }
    : { type: 'stage', seq: 1, stage: 'external_search', detail, count };
}

function content(label: string): MindMapContent {
  return {
    title: '示例',
    root: { id: 'root', label, children: [{ id: 'a', label: '甲', children: [] }] },
  };
}

function startStream(goal = '碳排放交易') {
  const stream = controllableReply();
  vi.stubGlobal('fetch', vi.fn(async () => stream.response));
  const store = useMindMapStore();
  const running = store.runGeneration(goal, 'create');
  return { stream, store, running };
}

beforeEach(() => {
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('检索期间的进度信号', () => {
  it('开始帧上状态栏，检索结束后变成已找到的条数', async () => {
    const { stream, store, running } = startStream();

    stream.push(stage('正在检索外部资料…'));
    await vi.waitFor(() => expect(store.stageText).toBe('正在检索外部资料…'));

    // 结束帧替换同一行的文案，并带上条数
    stream.push(stage('已找到 3 条外部资料', 3));
    await vi.waitFor(() => expect(store.stageText).toBe('已找到 3 条外部资料'));

    const frames = store.agentEvents.filter(event => event.type === 'stage');
    expect(frames.map(frame => (frame.type === 'stage' ? frame.count : undefined)))
      .toEqual([undefined, 3]);

    stream.push({ type: 'done', seq: 9, content: content('最终候选') });
    stream.finish();
    await running;
  });

  it('面板里也有同一句话（用户盯的是面板）', async () => {
    const { stream, store, running } = startStream();

    stream.push(stage('正在检索外部资料…'));
    await vi.waitFor(() => expect(store.stageText).toBe('正在检索外部资料…'));

    const wrapper = mount(AgentPanel);
    await vi.waitFor(() => expect(wrapper.text()).toContain('正在检索外部资料…'));

    stream.push(stage('已找到 1 条外部资料', 1));
    // 只留最后一句：面板里不该同时出现"正在检索"和"已找到"
    await vi.waitFor(() => expect(wrapper.text()).toContain('已找到 1 条外部资料'));
    expect(wrapper.text()).not.toContain('正在检索外部资料…');

    stream.push({ type: 'done', seq: 9, content: content('最终候选') });
    stream.finish();
    await running;
  });

  it('一条都没找到也是结论，不能显示成"还在检索"', async () => {
    const { stream, store, running } = startStream();

    stream.push(stage('没有搜到外部资料，按主题生成', 0));
    await vi.waitFor(() => expect(store.stageText).toBe('没有搜到外部资料，按主题生成'));

    const wrapper = mount(AgentPanel);
    await vi.waitFor(() => expect(wrapper.text()).toContain('没有搜到外部资料'));

    stream.push({ type: 'done', seq: 9, content: content('最终候选') });
    stream.finish();
    await running;
  });

  it('没有 detail 的 stage 帧退回显示 stage 标识', async () => {
    const { stream, store, running } = startStream();

    stream.push({ type: 'stage', seq: 1, stage: '准备' });
    await vi.waitFor(() => expect(store.stageText).toBe('准备'));

    stream.push({ type: 'done', seq: 9, content: content('最终候选') });
    stream.finish();
    await running;
  });

  it('检索帧不影响工具行的进度：两者各占一行', async () => {
    const { stream, store, running } = startStream();

    stream.push({ type: 'tool_start', seq: 1, call_id: 'c1', name: 'mindmap_generate' });
    stream.push(stage('正在检索外部资料…'));
    await vi.waitFor(() => expect(store.stageText).toBe('正在检索外部资料…'));

    const wrapper = mount(AgentPanel);
    await vi.waitFor(() => expect(wrapper.text()).toContain('正在定框架'));
    // 工具行仍然在跑（tool_end 还没到），检索那句话不能顶掉它
    expect(wrapper.text()).toContain('正在检索外部资料…');

    stream.push({ type: 'done', seq: 9, content: content('最终候选') });
    stream.finish();
    await running;
  });
});
