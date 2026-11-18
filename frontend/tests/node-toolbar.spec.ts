import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import App from '../src/App.vue';
import type { MindMapContent } from '../src/api/types';
import { textSource } from '../src/sources/input';
import { useMindMapStore } from '../src/stores/mindmap';
import { useSourcesStore } from '../src/stores/sources';
import { isPrefsRequest, prefsReply } from './helpers/prefs-stub';

/**
 * 选中节点旁的浮出工具条：`[改][展开][删][色]`。
 *
 * 它是"可以点击节点要求 AI 修改"那条需求的落点。定位靠**读画布的 DOM**（这里用一个
 * 带 `data-id` 的替身节点），所以这组用例同时钉住两件事：工具条只在选中时出现、
 * 点「改」会把对话框叫回来并把"关于「节点名」…"填进输入框。
 */
vi.mock('../src/canvas/MindMapCanvas.vue', () => ({
  default: { name: 'MindMapCanvasStub', template: '<div data-test="canvas" />' },
}));

const TREE: MindMapContent = {
  title: '报销流程',
  root: {
    id: 'r',
    label: '报销流程',
    children: [{ id: 'a', label: '提交申请', children: [{ id: 'b', label: '材料齐全吗', children: [] }] }],
  },
};

function stubFetch() {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (isPrefsRequest(url)) return prefsReply(url);
    if (url.endsWith('/health')) return { ok: true, status: 200, json: async () => ({ version: 'test' }) } as unknown as Response;
    return { ok: true, status: 200, json: async () => ({}) } as unknown as Response;
  }));
}

const wrappers: VueWrapper[] = [];

function mountApp(): VueWrapper {
  const pinia = createPinia();
  setActivePinia(pinia);
  const wrapper = mount(App, { global: { plugins: [pinia] } });
  wrappers.push(wrapper);
  return wrapper;
}

/**
 * 画布替身节点。
 *
 * 真画布是 Vue Flow，节点 wrapper 上带 `data-id`；工具条就靠这个属性找位置。
 * jsdom 里没有 Vue Flow，所以这里手工放一个同形状的元素——测的是"定位与浮出"，
 * 不是 Vue Flow 自己的渲染。
 */
function placeNode(id: string): void {
  const node = document.createElement('div');
  node.className = 'vue-flow__node';
  node.setAttribute('data-id', id);
  node.getBoundingClientRect = () => ({
    x: 100, y: 200, top: 200, left: 100, right: 220, bottom: 240, width: 120, height: 40,
    toJSON: () => ({}),
  }) as DOMRect;
  document.body.appendChild(node);
}

afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount());
  document.querySelectorAll('.vue-flow__node').forEach(node => node.remove());
  vi.unstubAllGlobals();
});

beforeEach(() => {
  // 工具条用 requestAnimationFrame 定位
  vi.stubGlobal('requestAnimationFrame', (fn: FrameRequestCallback) => {
    fn(0);
    return 1;
  });
  vi.stubGlobal('cancelAnimationFrame', () => {});
});

describe('节点工具条', () => {
  it('没选中节点时不出现在页面上', async () => {
    stubFetch();
    const wrapper = mountApp();
    useMindMapStore().preview(TREE, 'right');
    await flushPromises();
    expect(wrapper.find('[data-test="node-toolbar"]').exists()).toBe(false);
  });

  it('选中节点后浮出，四个动作都在', async () => {
    stubFetch();
    placeNode('a');
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(TREE, 'right');
    store.select('a');
    await flushPromises();

    const bar = wrapper.get('[data-test="node-toolbar"]');
    expect(bar.text()).toContain('改');
    expect(bar.text()).toContain('展开');
    expect(bar.text()).toContain('删');
    expect(bar.text()).toContain('色');
  });

  it('点「改」：对话框回来，输入框预填「关于『提交申请』…」', async () => {
    stubFetch();
    placeNode('a');
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(TREE, 'right');
    store.select('a');
    await flushPromises();

    // 先把对话框淡出（模拟"生成完只剩画布"）
    await wrapper.get('[data-test="new-run"]').trigger('click');
    await flushPromises();
    const chatDock = wrapper.get('[data-test="chat-dock"]');
    expect(chatDock.attributes('data-faded')).toBe('false');

    store.preview(TREE, 'right');
    store.select('a');
    await flushPromises();
    await wrapper.get('[data-test="node-action-edit"]').trigger('click');
    await flushPromises();

    const input = wrapper.get('textarea#intent').element as HTMLTextAreaElement;
    expect(input.value).toBe('关于「提交申请」…');
  });

  it('点「删」真的删掉这个节点（中心主题不允许删）', async () => {
    stubFetch();
    placeNode('a');
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(TREE, 'right');
    store.select('a');
    await flushPromises();

    await wrapper.get('[data-test="node-action-delete"]').trigger('click');
    await flushPromises();
    // 删一支会连它下面的子节点一起删（'a' 底下还挂着 'b'）
    expect(store.content?.root.children).toHaveLength(0);
    expect(store.nodeCount).toBe(1);
  });

  it('「色」展开调色板，选一个就把语义色写进节点', async () => {
    stubFetch();
    placeNode('a');
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(TREE, 'right');
    store.select('a');
    await flushPromises();

    await wrapper.get('[data-test="node-action-color"]').trigger('click');
    await wrapper.vm.$nextTick();
    const palette = wrapper.get('[data-test="node-palette"]');
    await palette.get('[aria-label="红（风险）"]').trigger('click');
    await flushPromises();

    expect(store.selectedNode?.node.color).toBe('red');
  });

  it('中心主题那条「删」是灰的（删掉就不是这张图了）', async () => {
    stubFetch();
    placeNode('r');
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(TREE, 'right');
    store.select('r');
    await flushPromises();

    expect(wrapper.get('[data-test="node-action-delete"]').attributes('disabled')).toBeDefined();
  });

  it('点「展开」把当前材料一起发出去（旧图 + 新材料那条路）', async () => {
    /** 只关心开流那一条的请求体：其它请求（prefs / health）照旧回空。 */
    const streams: Array<Record<string, unknown>> = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (isPrefsRequest(url)) return prefsReply(url);
      if (url.endsWith('/health')) {
        return { ok: true, status: 200, json: async () => ({ version: 'test' }) } as unknown as Response;
      }
      if (url.endsWith('/agent/runs/stream')) {
        streams.push(JSON.parse(String(init?.body ?? '{}')));
        const encoder = new TextEncoder();
        const body = new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(encoder.encode(
              `data: ${JSON.stringify({ type: 'done', seq: 1, content: TREE, mode: 'model' })}\n\n`));
            controller.enqueue(encoder.encode('data: [DONE]\n\n'));
            controller.close();
          },
        });
        return { ok: true, status: 200, body, json: async () => ({}) } as unknown as Response;
      }
      return { ok: true, status: 200, json: async () => ({}) } as unknown as Response;
    }));

    placeNode('a');
    const wrapper = mountApp();
    const store = useMindMapStore();
    const sources = useSourcesStore();
    // 旧图：画布上已经有一棵树（`source_refs` 里没有这份新材料）
    store.preview(TREE, 'right');
    // 新上传的材料：只活在当前会话里，后端回查不到，只能由请求带过去
    sources.items = [{
      key: 's1', id: 'doc-new', label: '新材料.pdf', kind: 'document', state: 'ready',
      source: { ...textSource('新材料正文'), id: 'doc-new', kind: 'document', filename: '新材料.pdf' },
      error: '', seconds: 0,
    }];
    store.select('a');
    await flushPromises();

    await wrapper.get('[data-test="node-action-expand"]').trigger('click');
    await flushPromises();

    expect(streams).toHaveLength(1);
    expect(streams[0].intent).toBe('expand');
    const sent = streams[0].sources as Array<{ id: string }>;
    expect(sent.map(source => source.id)).toEqual(['doc-new']);
  });
});
