import { mount, type VueWrapper } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import App from '../src/App.vue';
import type { MindMapContent, Pref } from '../src/api/types';
import { textSource } from '../src/sources/input';
import { useMindMapStore } from '../src/stores/mindmap';
import { EXTERNAL_SEARCH_PREF_KEY } from '../src/stores/prefs';
import { useSourcesStore } from '../src/stores/sources';
import { diagramPref, isPrefsRequest, prefsReply } from './helpers/prefs-stub';

/**
 * 「联网检索」开关：界面上真的关得掉的那个东西。
 *
 * 它与此前所有生成面板里的控件都不同——图型和拆解方式只是给模型的参数，关不掉任何
 * 一步；这个开关关掉时**一次外网往返都不发**（后端 `tools.external_knowledge_off_reason`
 * 是唯一的判定点）。所以这一组要钉住三件事：
 *
 *   1. 勾一下就是**写偏好**（不是发一个请求字段）：下次生成、下次开页面都跟着变；
 *   2. 关掉之后这次生成**记进 `prefs_applied`**，面板上的"上次生效"才会亮起来
 *      （那一条是这张表是不是死表的探测器）；
 *   3. 带够长的材料时它**置灰并写明原因**——那时后端根本不走外部检索，勾来勾去
 *      不会有任何变化，而灰着不解释比摆着不解释更糟。
 */

vi.mock('../src/canvas/MindMapCanvas.vue', () => ({
  default: { name: 'MindMapCanvasStub', template: '<div data-test="canvas" />' },
}));

const INTENT = 'textarea#intent';
const SUBMIT = '[data-test="intent-box"] button';
const TOGGLE = '[data-test="external-search"]';
const HINT = '[data-test="external-search-hint"]';
const SUBMIT_HINT = '[data-test="intent-hint"]';
const PANEL_TOGGLE = `[data-test="pref-toggle-${EXTERNAL_SEARCH_PREF_KEY}"]`;

const content = (label = '根'): MindMapContent => ({
  title: '示例',
  root: { id: 'root', label, children: [{ id: 'a', label: '甲', children: [] }] },
});

function agentReply(frames: unknown[]): Response {
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

const doneFrame = (payload: MindMapContent) => ({ type: 'done', seq: 4, content: payload });

interface Recorded {
  url: string;
  method: string;
  body: unknown;
}

/** 偏好接口 + 生成接口的桩。与 `prefs.spec.ts` 里那个同源，只少建议那一路。 */
function stubBackend(prefs: Pref[] = []) {
  const items = [...prefs];
  const writes: Recorded[] = [];
  const runs: Array<{ url: string; body: Record<string, unknown> }> = [];

  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? 'GET';

    if (url.includes('/agent/runs/stream')) {
      runs.push({ url, body: JSON.parse(String(init?.body)) as Record<string, unknown> });
      return agentReply([doneFrame(content())]);
    }
    if (url.includes('/prefs/suggestions')) return prefsReply(url, [], []);
    if (isPrefsRequest(url)) {
      if (method === 'PUT') {
        const body = JSON.parse(String(init?.body)) as { value: string; source: string };
        writes.push({ url, method, body });
        const pref: Pref = {
          ...diagramPref(body.value),
          key: decodeURIComponent(url.split('/prefs/')[1]),
          source: body.source as Pref['source'],
        };
        const index = items.findIndex(item => item.key === pref.key);
        if (index >= 0) items[index] = pref;
        else items.push(pref);
        return {
          ok: true, status: 200, json: async () => ({ pref, changed: true }),
        } as unknown as Response;
      }
      return prefsReply(url, items);
    }
    return agentReply([]);
  }));
  return { writes, runs, items };
}

/** 一条联网检索偏好。`false` = 用户在界面上关掉过。 */
function externalPref(value: string, extra: Partial<Pref> = {}): Pref {
  return diagramPref(value, { key: EXTERNAL_SEARCH_PREF_KEY, ...extra });
}

function mountApp(): VueWrapper {
  const pinia = createPinia();
  setActivePinia(pinia);
  return mount(App, { global: { plugins: [pinia] } });
}

/** 往材料区塞一份"已就绪"的来源。字数决定后端会不会真的用它当语料。 */
function addMaterial(text: string): void {
  const source = {
    ...textSource(text), id: 'doc-1', kind: 'document' as const, filename: '材料.pdf',
  };
  useSourcesStore().items = [{
    key: 's1', id: 's1', label: '材料.pdf', kind: 'document', state: 'ready',
    source, error: '', seconds: 0,
  }];
}

beforeEach(() => {
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('开关本身', () => {
  it('没材料时默认开着，并说明它值多少时间', async () => {
    stubBackend();
    const wrapper = mountApp();

    const toggle = wrapper.get(TOGGLE);
    expect((toggle.element as HTMLInputElement).checked).toBe(true);
    expect(toggle.attributes('disabled')).toBeUndefined();
    expect(wrapper.get(HINT).text()).toContain('约 4 秒');
  });

  it('勾一下就是写偏好（不是发一次性字段），落库之后界面跟着变', async () => {
    const backend = stubBackend();
    const wrapper = mountApp();

    await wrapper.get(TOGGLE).setValue(false);
    await vi.waitFor(() => expect(backend.writes).toHaveLength(1));

    expect(backend.writes[0].url).toContain(EXTERNAL_SEARCH_PREF_KEY);
    expect(backend.writes[0].body).toEqual({ value: 'false', source: 'user_explicit' });
    // 写完之后那个勾要自己变成"关"，提示也换成"不会有引用"
    await vi.waitFor(() =>
      expect((wrapper.get(TOGGLE).element as HTMLInputElement).checked).toBe(false));
    expect(wrapper.get(HINT).text()).toContain('已关闭');
    expect(wrapper.get(HINT).text()).toContain('不会有引用');
  });

  it('库里写着 false 时勾是空的，且这一次生成把它记进 prefs_applied', async () => {
    const backend = stubBackend([externalPref('false')]);
    const wrapper = mountApp();

    await vi.waitFor(() =>
      expect((wrapper.get(TOGGLE).element as HTMLInputElement).checked).toBe(false));

    await wrapper.get(INTENT).setValue('碳中和');
    await wrapper.get(SUBMIT).trigger('click');
    await vi.waitFor(() => expect(backend.runs).toHaveLength(1));

    // 这一列是面板"上次生效"的唯一数据源：不记的话，用户刚关过的开关永远显示
    // "尚未生效"——那是死表的形状，而它其实是活的。
    expect(backend.runs[0].body.prefs_applied).toEqual([`${EXTERNAL_SEARCH_PREF_KEY}=false`]);
  });

  it('没设过这条偏好时什么都不记：默认开，没有条目可报', async () => {
    const backend = stubBackend();
    const wrapper = mountApp();
    await vi.waitFor(() => expect(wrapper.find(TOGGLE).exists()).toBe(true));

    await wrapper.get(INTENT).setValue('碳中和');
    await wrapper.get(SUBMIT).trigger('click');
    await vi.waitFor(() => expect(backend.runs).toHaveLength(1));

    expect('prefs_applied' in backend.runs[0].body).toBe(false);
  });
});

describe('有材料时它不生效', () => {
  it('材料够长：置灰，并写明"这次带材料，不走联网检索"', async () => {
    stubBackend();
    const wrapper = mountApp();
    addMaterial('字'.repeat(320));
    await wrapper.vm.$nextTick();

    expect(wrapper.get(TOGGLE).attributes('disabled')).toBeDefined();
    expect(wrapper.get(HINT).text()).toBe('这次带材料，不走联网检索');
  });

  it('材料太短时仍然可用：那种材料会被降级成主题提示，后端照样联网补依据', async () => {
    stubBackend();
    const wrapper = mountApp();
    addMaterial('rust 学习路线');
    await wrapper.vm.$nextTick();

    expect(wrapper.get(TOGGLE).attributes('disabled')).toBeUndefined();
    expect(wrapper.get(HINT).text()).toContain('约 4 秒');
  });

  it('带够长的材料时也不记进 prefs_applied：那条路根本没走外部检索', async () => {
    const backend = stubBackend([externalPref('true')]);
    const wrapper = mountApp();
    addMaterial('字'.repeat(320));
    await wrapper.vm.$nextTick();

    await wrapper.get(INTENT).setValue('整理这份材料');
    await wrapper.get(SUBMIT).trigger('click');
    await vi.waitFor(() => expect(backend.runs).toHaveLength(1));

    expect('prefs_applied' in backend.runs[0].body).toBe(false);
  });

  it('有图（edit）时置灰不生效：那条路不生成，也没有外部检索', async () => {
    stubBackend();
    const wrapper = mountApp();
    useMindMapStore().preview(content('原图'), 'right');
    await wrapper.vm.$nextTick();

    // 「智能搜索」这一格留在按钮行里（截图里那一行是三件东西，抽掉一格会让布局跳），
    // 但 edit 这条路根本不生成、也不发检索请求，所以它必须是灰的。
    const toggle = wrapper.get(TOGGLE);
    expect(toggle.attributes('disabled')).toBeDefined();
    expect(wrapper.get(HINT).text()).toContain('不走联网检索');
  });
});

/**
 * 「会不会联网」这件事有两个渲染点：勾选框旁那行（`external-search-hint`），与提交
 * 行下面那行（`intent-hint`）。它们曾经各写一份文案：开关关掉之后，前者说"不会有
 * 引用"、后者还在说"会先去外部搜资料"，两行同时挂在一个面板上互相打脸，而后端只认
 * 开关。这一组钉住"判据只有一份、两处说的是同一件事"。
 */
describe('两处提示共用同一份判据', () => {
  it('开关关着：两处一字不差，都不再说"会去外部搜"', async () => {
    stubBackend([externalPref('false')]);
    const wrapper = mountApp();
    await vi.waitFor(() =>
      expect((wrapper.get(TOGGLE).element as HTMLInputElement).checked).toBe(false));

    await wrapper.get(INTENT).setValue('碳中和');

    const beside = wrapper.get(HINT).text();
    const under = wrapper.get(SUBMIT_HINT).text();
    expect(beside).toContain('已关闭');
    expect(under).toBe(beside);
    expect(under).not.toContain('外部搜');
  });

  it('材料太短 + 开关开着：两处都说会先补一轮资料', async () => {
    stubBackend();
    const wrapper = mountApp();
    addMaterial('rust 学习路线');
    await wrapper.vm.$nextTick();

    await wrapper.get(INTENT).setValue('整理成学习路线');

    expect(wrapper.get(HINT).text()).toContain('约 4 秒');
    expect(wrapper.get(SUBMIT_HINT).text()).toContain('约 4 秒');
  });

  it('材料够长：两处都说不走联网检索', async () => {
    stubBackend();
    const wrapper = mountApp();
    addMaterial('字'.repeat(320));
    await wrapper.vm.$nextTick();

    await wrapper.get(INTENT).setValue('整理这份材料');

    expect(wrapper.get(HINT).text()).toBe('这次带材料，不走联网检索');
    expect(wrapper.get(SUBMIT_HINT).text()).toContain('这次带材料，不走联网检索');
    expect(wrapper.get(SUBMIT_HINT).text()).not.toContain('外部搜');
  });
});

describe('偏好面板里也能改', () => {  it('布尔型偏好画成勾选框，勾一下发 PUT，标签跟着变', async () => {
    const backend = stubBackend([externalPref('true')]);
    const wrapper = mountApp();
    await vi.waitFor(() => expect(wrapper.find(PANEL_TOGGLE).exists()).toBe(true));

    const panel = wrapper.get('[data-test="prefs-panel"]');
    expect(panel.text()).toContain('联网检索');
    expect(panel.text()).toContain('已开启');

    await wrapper.get(PANEL_TOGGLE).setValue(false);
    await vi.waitFor(() => expect(backend.writes).toHaveLength(1));

    expect(backend.writes[0].body).toEqual({ value: 'false', source: 'user_explicit' });
    await vi.waitFor(() =>
      expect(wrapper.get('[data-test="prefs-panel"]').text()).toContain('已关闭'));
  });

  it('布尔型没有「改」按钮：值域只有两个，勾选框就是全部编辑面', async () => {
    stubBackend([externalPref('false')]);
    const wrapper = mountApp();
    await vi.waitFor(() => expect(wrapper.find(PANEL_TOGGLE).exists()).toBe(true));

    expect(wrapper.find(`[data-test="pref-edit-btn-${EXTERNAL_SEARCH_PREF_KEY}"]`).exists())
      .toBe(false);
    // 删除仍然在：删了就回到默认（开）
    expect(wrapper.find(`[data-test="pref-del-${EXTERNAL_SEARCH_PREF_KEY}"]`).exists())
      .toBe(true);
  });
});
