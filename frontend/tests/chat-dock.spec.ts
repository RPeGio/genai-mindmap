import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import App from '../src/App.vue';
import type { MindMapContent } from '../src/api/types';
import { useChatStore } from '../src/stores/chat';
import { useMindMapStore } from '../src/stores/mindmap';
import { useSourcesStore } from '../src/stores/sources';
import { textSource } from '../src/sources/input';
import { isPrefsRequest, prefsReply } from './helpers/prefs-stub';

/**
 * 底部对话框的状态机、语音态、材料缩略图与悬浮球。
 *
 * 这一版的核心交互就是这几条：
 *   空闲 → 完整对话框；生成中 → 一条细进度；生成完 → 淡出（只剩画布 + 悬浮球）；
 *   选中节点 → 工具条；点「改」→ 对话框回来并预填。
 *
 * jsdom 量不出像素，但**谁渲染、谁消失、谁带什么文字**全都钉得住；像素那部分在
 * `.visioncheck/verify_chat_dock.py` 的真浏览器里量。
 */
vi.mock('../src/canvas/MindMapCanvas.vue', () => ({
  default: { name: 'MindMapCanvasStub', template: '<div data-test="canvas" />' },
}));

const TREE: MindMapContent = {
  title: '机器学习',
  root: { id: 'r', label: '机器学习', children: [{ id: 'a', label: '监督学习', children: [] }] },
};

function streamReply(frames: unknown[]): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const frame of frames) {
        controller.enqueue(encoder.encode(`data: ${JSON.stringify(frame)}\n\n`));
      }
      controller.enqueue(encoder.encode('data: [DONE]\n\n'));
      controller.close();
    },
  });
  return { ok: true, status: 200, body, json: async () => ({}) } as unknown as Response;
}

function stubFetch(stream: unknown[] = []) {
  const mock = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
    const url = String(input);
    if (isPrefsRequest(url)) return prefsReply(url);
    if (url.endsWith('/health')) return { ok: true, status: 200, json: async () => ({ version: 'test' }) } as unknown as Response;
    return streamReply(stream);
  });
  vi.stubGlobal('fetch', mock);
  return mock;
}

const wrappers: VueWrapper[] = [];

function mountApp(): VueWrapper {
  const pinia = createPinia();
  setActivePinia(pinia);
  const wrapper = mount(App, { global: { plugins: [pinia] } });
  wrappers.push(wrapper);
  return wrapper;
}

const DOCK_CARD = '[data-test="intent-box"]';
const SUBMIT = `${DOCK_CARD} button`;

/** 整块对话框当前是不是淡出的（`data-faded="true"` 与 `opacity-0` 同步）。 */
function dockFaded(wrapper: VueWrapper): boolean {
  return wrapper.get('[data-test="chat-dock"]').attributes('data-faded') === 'true';
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
});

afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount());
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('底部对话框的状态机', () => {
  it('空闲：完整对话框（输入框、按钮行、缩略图不在）', async () => {
    stubFetch();
    const wrapper = mountApp();
    await flushPromises();

    expect(wrapper.find('textarea#intent').exists()).toBe(true);
    expect(wrapper.find('[data-test="deep-thinking"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="attach"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="voice-toggle"]').exists()).toBe(true);
    // 没有材料时不出缩略图区：空一条横条只是占地方
    expect(wrapper.find('[data-test="thumbs"]').exists()).toBe(false);
    expect(dockFaded(wrapper)).toBe(false);
  });

  it('生成中：整块收成一条细进度，输入框与参数都不渲染', async () => {
    stubFetch();
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.busy = true;
    store.stageText = '正在定框架';
    await wrapper.vm.$nextTick();

    const row = wrapper.get('[data-test="run-status"]');
    expect(row.text()).toContain('正在生成');
    expect(row.text()).toContain('正在定框架');
    expect(row.find('[data-test="run-status-cancel"]').exists()).toBe(true);
    // 「收缩」不是"盖一层"：输入框、按钮行、参数全都不在了
    expect(wrapper.find('textarea#intent').exists()).toBe(false);
    expect(wrapper.find(DOCK_CARD).exists()).toBe(false);
  });

  it('生成完：2 秒后淡出，只剩画布 + 悬浮球', async () => {
    stubFetch([{ type: 'done', seq: 3, content: TREE, mode: 'model' }]);
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(TREE, 'right');
    await store.runGeneration('机器学习', 'edit');
    await flushPromises();
    await wrapper.vm.$nextTick();

    // 结果先落一条状态行：用户要看得见"已落到画布"
    expect(wrapper.get('[data-test="run-status"]').text()).toContain('已落到画布');
    expect(dockFaded(wrapper)).toBe(false);

    vi.advanceTimersByTime(2100);
    await wrapper.vm.$nextTick();
    expect(dockFaded(wrapper)).toBe(true);
  });

  it('失败时对话框留住（他要改一句重来，不该先翻历史）', async () => {
    stubFetch();
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(TREE, 'right');
    store.agentError = '模型服务暂时不可用';
    await wrapper.vm.$nextTick();

    vi.advanceTimersByTime(5000);
    await wrapper.vm.$nextTick();

    expect(dockFaded(wrapper)).toBe(false);
    expect(wrapper.get('[data-test="run-status"]').text()).toContain('生成失败');
    expect(wrapper.find('textarea#intent').exists()).toBe(true);
  });

  it('输入框里还有没发出去的字时，成功也不淡出', async () => {
    stubFetch([{ type: 'done', seq: 2, content: TREE, mode: 'model' }]);
    const wrapper = mountApp();
    const store = useMindMapStore();
    await wrapper.get('textarea#intent').setValue('后半句还没写完');
    store.preview(TREE, 'right');
    store.generatedSummary = { nodes: 2, changes: null };
    await wrapper.vm.$nextTick();

    vi.advanceTimersByTime(5000);
    await wrapper.vm.$nextTick();
    expect(dockFaded(wrapper)).toBe(false);
  });

  it('画布还空着就永远留着：那是唯一的入口', async () => {
    stubFetch();
    const wrapper = mountApp();
    vi.advanceTimersByTime(5000);
    await wrapper.vm.$nextTick();
    expect(dockFaded(wrapper)).toBe(false);
  });
});

describe('材料缩略图', () => {
  it('一份材料一个缩略块，✕ 就地移除', async () => {
    stubFetch();
    const wrapper = mountApp();
    const sources = useSourcesStore();
    sources.addText('一段材料正文');
    await wrapper.vm.$nextTick();

    expect(wrapper.get('[data-test="thumbs"]').text()).toContain('文本 1');
    expect(wrapper.get('[data-test="thumbs"]').text()).toContain('字');

    await wrapper.get('[aria-label="移除 文本 1"]').trigger('click');
    expect(sources.items).toHaveLength(0);
    await wrapper.vm.$nextTick();
    expect(wrapper.find('[data-test="thumbs"]').exists()).toBe(false);
  });

  it('解析失败的那份在缩略块里说明原因，并能重试', async () => {
    stubFetch();
    const wrapper = mountApp();
    const sources = useSourcesStore();
    sources.items = [{
      key: 'k1', id: 'k1', label: '坏文件.pdf', kind: 'document', state: 'failed',
      source: null, error: '这个来源没有解析出可用文字', seconds: 0,
    }];
    await wrapper.vm.$nextTick();

    const thumbs = wrapper.get('[data-test="thumbs"]').text();
    expect(thumbs).toContain('坏文件.pdf');
    expect(thumbs).toContain('没有解析出可用文字');
    expect(wrapper.get('[data-test="thumbs"]').find('button:not([aria-label])').exists()).toBe(true);
  });

  it('「+」打开材料面板：粘贴、上传、录音都还在', async () => {
    stubFetch();
    const wrapper = mountApp();
    expect(wrapper.get('[data-test="material-panel"]').attributes('style') ?? '').toContain('display: none');

    await wrapper.get('[data-test="attach"]').trigger('click');
    await wrapper.vm.$nextTick();

    const panel = wrapper.get('[data-test="material-panel"]');
    expect(panel.attributes('style') ?? '').not.toContain('display: none');
    expect(panel.text()).toContain('粘贴材料正文');
    expect(panel.text()).toContain('添加文件（可多选）');
    expect(panel.text()).toContain('录音添加来源');
  });
});

describe('语音模式', () => {
  it('点 🎤 换成「按住说话」，⌨ 能切回键盘', async () => {
    stubFetch();
    const wrapper = mountApp();

    await wrapper.get('[data-test="voice-toggle"]').trigger('click');
    await wrapper.vm.$nextTick();
    expect(wrapper.find('[data-test="press-to-talk"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="press-to-talk"]').text()).toContain('按住说话');
    expect(wrapper.find('textarea#intent').exists()).toBe(false);
    expect(wrapper.get('[data-test="voice-toggle"]').text()).toBe('⌨');

    await wrapper.get('[data-test="voice-toggle"]').trigger('click');
    await wrapper.vm.$nextTick();
    expect(wrapper.find('textarea#intent').exists()).toBe(true);
    expect(wrapper.get('[data-test="voice-toggle"]').text()).toBe('🎤');
  });

  it('按住说话复用录音能力：松手之后那份 WAV 变成一份材料', async () => {
    const recorded = new File([new Uint8Array([1, 2, 3])], 'voice.wav', { type: 'audio/wav' });
    const stop = vi.fn(async () => recorded);
    const cancel = vi.fn();
    const startRecording = vi.fn(async () => ({
      elapsedSeconds: () => 1,
      stop,
      cancel,
    }));
    vi.doMock('../src/audio/recorder', async () => {
      const actual = await vi.importActual<typeof import('../src/audio/recorder')>('../src/audio/recorder');
      return {
        ...actual,
        startRecording,
        RecordingError: actual.RecordingError,
      };
    });
    vi.resetModules();
    const { default: VoicePress } = await import('../src/components/VoicePress.vue');
    const { mount: mountPlain } = await import('@vue/test-utils');

    const wrapper = mountPlain(VoicePress);
    await wrapper.get('[data-test="press-to-talk"]').trigger('pointerdown');
    await flushPromises();
    expect(startRecording).toHaveBeenCalled();
    expect(wrapper.get('[data-test="press-to-talk"]').text()).toContain('松开');

    window.dispatchEvent(new Event('pointerup'));
    await flushPromises();
    expect(stop).toHaveBeenCalled();
    expect(wrapper.emitted('recorded')?.[0]).toEqual([recorded]);
    wrapper.unmount();
    vi.doUnmock('../src/audio/recorder');
  });
});

describe('悬浮球与消息时间线', () => {
  it('新消息在面板关着时累加角标，点开后清零', async () => {
    stubFetch();
    const wrapper = mountApp();
    const chat = useChatStore();

    chat.said('第一句');
    chat.replied({ text: '已落到画布', nodes: 6, changes: 2 });
    await wrapper.vm.$nextTick();

    expect(wrapper.get('[data-test="message-badge"]').text()).toBe('2');

    await wrapper.get('[data-test="message-fab"]').trigger('click');
    await wrapper.vm.$nextTick();
    expect(wrapper.find('[data-test="message-badge"]').exists()).toBe(false);

    const items = wrapper.findAll('[data-test="message-item"]');
    expect(items).toHaveLength(2);
    expect(items[0].text()).toContain('第一句');
    expect(items[1].text()).toContain('已落到画布');
  });

  it('一次生成完就有一条 AI 消息，带模型名与改动条数', async () => {
    stubFetch([{ type: 'done', seq: 2, content: TREE, mode: 'model', model: 'deepseek-reasoner' }]);
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(TREE, 'right');
    await store.runGeneration('再加一支', 'edit');
    await flushPromises();
    await wrapper.vm.$nextTick();

    const chat = useChatStore();
    const last = chat.messages[chat.messages.length - 1];
    expect(last.role).toBe('ai');
    expect(last.model).toBe('deepseek-reasoner');
    expect(last.text).toContain('已落到画布');
    expect(wrapper.find('[data-test="message-badge"]').exists()).toBe(true);
  });

  it('失败也进时间线，写成一条错误（而不是静默消失）', async () => {
    stubFetch();
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(TREE, 'right');
    // 走一遍真实的"开始 → 失败"：`agentRunning` 的下降沿是唯一的触发点
    store.agentRunning = true;
    store.busy = true;
    await wrapper.vm.$nextTick();
    store.agentError = '模型服务暂时不可用';
    store.agentRunning = false;
    store.busy = false;
    await wrapper.vm.$nextTick();

    const chat = useChatStore();
    expect(chat.messages.some(item => item.kind === 'error' && item.text.includes('模型服务暂时不可用'))).toBe(true);
  });

  it('「继续说一句」把对话框叫回来（淡出之后的回程入口）', async () => {
    stubFetch();
    const wrapper = mountApp();
    const chat = useChatStore();
    chat.said('第一句');
    await wrapper.vm.$nextTick();

    await wrapper.get('[data-test="message-fab"]').trigger('click');
    await wrapper.vm.$nextTick();
    await wrapper.get('[data-test="message-compose"]').trigger('click');
    await flushPromises();

    expect(chat.open).toBe(false);
    expect(wrapper.get('[data-test="chat-dock"]').attributes('data-faded')).toBe('false');
    expect(wrapper.find('textarea#intent').exists()).toBe(true);
  });

  it('「改这句」把原句填回输入框（改一个词重发，不用重打）', async () => {
    stubFetch();
    const wrapper = mountApp();
    const chat = useChatStore();
    chat.said('把报销流程画清楚');
    await wrapper.vm.$nextTick();

    await wrapper.get('[data-test="message-fab"]').trigger('click');
    await wrapper.vm.$nextTick();
    await wrapper.get('[data-test="message-reuse"]').trigger('click');
    await flushPromises();

    const input = wrapper.get('textarea#intent').element as HTMLTextAreaElement;
    expect(input.value).toBe('把报销流程画清楚');
    expect(wrapper.get('[data-test="chat-dock"]').attributes('data-faded')).toBe('false');
  });

  it('发出去的那句话立刻进时间线（对话框随后会收起并清空）', async () => {
    stubFetch([{ type: 'done', seq: 2, content: TREE, mode: 'model' }]);
    const wrapper = mountApp();
    await wrapper.get('textarea#intent').setValue('把报销流程画清楚');
    await wrapper.get(SUBMIT).trigger('click');
    await flushPromises();

    const chat = useChatStore();
    expect(chat.messages[0].role).toBe('user');
    expect(chat.messages[0].text).toBe('把报销流程画清楚');
  });
});

describe('thinking 全链路：请求体与折叠展示', () => {
  it('勾「深度思考」→ 请求体带 deep_thinking:true，回复里的 reasoning 折进消息流', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (isPrefsRequest(url)) return prefsReply(url);
      if (url.endsWith('/health')) return { ok: true, status: 200, json: async () => ({ version: 'test' }) } as unknown as Response;
      const body = JSON.parse(String(init?.body ?? '{}'));
      expect(body.deep_thinking).toBe(true);
      return streamReply([{
        type: 'done', seq: 2, content: TREE, mode: 'model',
        model: 'deepseek-reasoner', reasoning: '先看材料，再定六个分支。'.repeat(3),
      }]);
    });
    vi.stubGlobal('fetch', fetchMock);

    const wrapper = mountApp();
    await wrapper.get('[data-test="deep-thinking"]').trigger('click');
    expect(wrapper.get('[data-test="deep-thinking"]').attributes('aria-pressed')).toBe('true');

    await wrapper.get('textarea#intent').setValue('机器学习');
    await wrapper.get(SUBMIT).trigger('click');
    await flushPromises();
    await wrapper.vm.$nextTick();

    const chat = useChatStore();
    const last = chat.messages[chat.messages.length - 1];
    expect(last.reasoning).toContain('先看材料');
    // 消息面板里的 `<details>` 折叠着全文（不默认展开，不截断）
    await wrapper.get('[data-test="message-fab"]').trigger('click');
    await wrapper.vm.$nextTick();
    const panel = wrapper.get('[data-test="message-panel"]');
    expect(panel.text()).toContain('模型当时的思考');
    expect(panel.text()).toContain('先看材料');
  });

  it('不勾时不发这个字段（与加开关之前逐字节相同）', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (isPrefsRequest(url)) return prefsReply(url);
      if (url.endsWith('/health')) return { ok: true, status: 200, json: async () => ({ version: 'test' }) } as unknown as Response;
      const body = JSON.parse(String(init?.body ?? '{}'));
      expect('deep_thinking' in body).toBe(false);
      return streamReply([{ type: 'done', seq: 2, content: TREE, mode: 'model' }]);
    });
    vi.stubGlobal('fetch', fetchMock);

    const wrapper = mountApp();
    await wrapper.get('textarea#intent').setValue('机器学习');
    await wrapper.get(SUBMIT).trigger('click');
    await flushPromises();
    expect(fetchMock).toHaveBeenCalled();
  });
});

describe('材料参与生成', () => {
  it('缩略图里的材料照旧进请求体（材料没有因为换位置而丢）', async () => {
    const fetchMock = stubFetch([{ type: 'done', seq: 2, content: TREE, mode: 'model' }]);
    const wrapper = mountApp();
    const sources = useSourcesStore();
    sources.items = [{
      key: 's1', id: 'doc-1', label: '材料.pdf', kind: 'document', state: 'ready',
      source: { ...textSource('材料正文'), id: 'doc-1', kind: 'document', filename: '材料.pdf' },
      error: '', seconds: 0,
    }];
    await wrapper.vm.$nextTick();

    await wrapper.get('textarea#intent').setValue('按材料生成');
    await wrapper.get(SUBMIT).trigger('click');
    await flushPromises();

    const body = JSON.parse(String(fetchMock.mock.calls[fetchMock.mock.calls.length - 1]?.[1]?.body ?? '{}'));
    expect(body.sources).toHaveLength(1);
  });
});
