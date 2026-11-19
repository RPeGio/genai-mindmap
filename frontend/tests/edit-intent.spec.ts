import { mount, type VueWrapper } from '@vue/test-utils';
import { createPinia, setActivePinia, type Pinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import App from '../src/App.vue';
import AgentPanel from '../src/components/AgentPanel.vue';
import type { MindMapContent } from '../src/api/types';
import { useMindMapStore } from '../src/stores/mindmap';
import { isPrefsRequest, prefsReply } from './helpers/prefs-stub';
import { isOptimizationRequest, optimizationReply } from './helpers/optimization-stub';

// jsdom 里没有 ResizeObserver，真正的 Vue Flow 画布跑不起来；这一组用例只关心
// 「改这张图」入口与候选 diff，画布换成占位元素。
vi.mock('../src/canvas/MindMapCanvas.vue', () => ({
  default: { name: 'MindMapCanvasStub', template: '<div data-test="canvas" />' },
}));

/**
 * 画布右上角那个框。合并之后它同时承担 `create` 与 `edit`，所以按 id 取而不是
 * 按 aria-label 取——label 会随状态变（「主题或要求」/「怎么改」），id 不会。
 */
const EDIT_INPUT = 'textarea#intent';
const SUBMIT = '[data-test="intent-box"] button';

/** 把若干事件包成 SSE 帧，模拟后端 /api/agent/runs/stream。 */
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

function stubFetch(reply: () => Response) {
  const calls: Array<{ url: string; body: unknown }> = [];
  const mock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    // 偏好是挂载时的背景请求（生成面板带预填）。它不属于这里被测的"这个入口发出
    // 什么"，进 calls 只会让 `calls[0]` 指向一个 GET。
    if (isPrefsRequest(url)) return prefsReply(url);
    // 生成落地之后前端自动跑一次只读结构检查（`ChatDock` 那条 watch）。它是背景
    // 请求，与 prefs / health 同一条理由：进 calls 会让"只发了一次生成请求"变成两条。
    if (isOptimizationRequest(url)) return optimizationReply();
    if (url.endsWith('/health')) {
      // 挂载时探一次版本号。它是背景请求，和"点这个入口发出了什么"无关。
      return new Response(JSON.stringify({ status: 'ok', version: 'test' }), {
        status: 200, headers: { 'content-type': 'application/json' },
      });
    }
    calls.push({ url, body: init?.body ? JSON.parse(String(init.body)) : null });
    return reply();
  });
  vi.stubGlobal('fetch', mock);
  return calls;
}

const BASE: MindMapContent = {
  title: '示例',
  root: {
    id: 'root', label: '根', children: [
      { id: 'a', label: '甲', children: [] },
      { id: 'b', label: '乙', children: [] },
      { id: 'c', label: '丙', children: [] },
    ],
  },
};

/** 模型改完的树：甲改了标题与颜色，丙被删掉，另外新增一个节点。 */
const EDITED: MindMapContent = {
  title: '示例',
  root: {
    id: 'root', label: '根', children: [
      { id: 'a', label: '甲（拆细）', color: 'purple', children: [] },
      { id: 'b', label: '乙', children: [] },
      { id: 'd', label: '新增的要点', children: [] },
    ],
  },
};

function mountApp(): VueWrapper {
  return mount(App, { global: { plugins: [createPinia()] } });
}

beforeEach(() => {
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('画布右上角的「改这张图」入口', () => {
  it('没有导图时它先当生成入口用，并说明"写个主题"', async () => {
    const calls = stubFetch(() => agentReply([{ type: 'done', seq: 2 }]));
    const wrapper = mountApp();
    const input = wrapper.find(EDIT_INPUT);
    expect(input.exists()).toBe(true);
    // 合并之后这个框不再对"没图"禁用：没图时它自己就是生成入口
    expect((input.element as HTMLTextAreaElement).disabled).toBe(false);
    expect(wrapper.get('[data-test="intent-box"]').text())
      .toContain('写一个主题，或者点「+」添加材料');
    // 空着点不动，也就不该发请求
    await wrapper.get(SUBMIT).trigger('click');
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(calls).toHaveLength(0);
  });

  it('有导图时打字 + 点按钮，发出 intent=edit 且带上当前树', async () => {
    const calls = stubFetch(() => agentReply([
      { type: 'done', seq: 2, content: EDITED },
    ]));
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(structuredClone(BASE), 'right');
    await wrapper.vm.$nextTick();

    await wrapper.find(EDIT_INPUT).setValue('把第三支拆细一点');
    const button = wrapper.get(SUBMIT);
    expect(button.text()).toBe('改这张图');
    await button.trigger('click');
    await new Promise(resolve => setTimeout(resolve, 0));

    expect(calls).toHaveLength(1);
    expect(calls[0].url).toContain('/agent/runs/stream');
    const body = calls[0].body as Record<string, unknown>;
    expect(body.intent).toBe('edit');
    expect(body.goal).toBe('把第三支拆细一点');
    // edit 必须带 snapshot：后端要在客户端这棵树上改，而它可能有一堆没保存的编辑
    expect((body.snapshot as MindMapContent).root.id).toBe('root');
  });

  it('回车也能提交', async () => {
    const calls = stubFetch(() => agentReply([{ type: 'done', seq: 2, content: EDITED }]));
    const wrapper = mountApp();
    useMindMapStore().preview(structuredClone(BASE), 'right');
    await wrapper.vm.$nextTick();

    const input = wrapper.find(EDIT_INPUT);
    await input.setValue('把甲改名');
    await input.trigger('keydown', { key: 'Enter' });
    await new Promise(resolve => setTimeout(resolve, 0));

    expect(calls).toHaveLength(1);
    expect((calls[0].body as Record<string, unknown>).intent).toBe('edit');
  });

  it('空输入不发请求', async () => {
    const calls = stubFetch(() => agentReply([{ type: 'done', seq: 1 }]));
    const wrapper = mountApp();
    useMindMapStore().preview(structuredClone(BASE), 'right');
    await wrapper.vm.$nextTick();

    const input = wrapper.find(EDIT_INPUT);
    await input.setValue('   ');
    await input.trigger('keydown', { key: 'Enter' });
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(calls).toHaveLength(0);
  });
});

describe('侧栏改样式', () => {
  it('下拉框里选颜色、输入框里写图标都写回 content，并可撤销', async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const store = useMindMapStore();
    store.preview(structuredClone(BASE), 'right');
    store.select('a');
    const wrapper = mount(App, { global: { plugins: [pinia] } });
    await wrapper.vm.$nextTick();

    const select = wrapper.find('select[aria-label="节点颜色"]');
    expect(select.exists()).toBe(true);
    // 选项是调色板的 8 个键 + 一个"默认"
    expect(select.findAll('option')).toHaveLength(9);
    await select.setValue('purple');
    expect(store.content!.root.children[0].color).toBe('purple');

    const icon = wrapper.find('input[aria-label="节点图标"]');
    await icon.setValue('🎯');
    await icon.trigger('change');
    expect(store.content!.root.children[0].icon).toBe('🎯');

    // 选「默认」等于清除颜色（不是"不动"）
    await select.setValue('');
    expect(store.content!.root.children[0].color).toBeNull();

    // 两次改动都可撤销：第一次撤销回紫色，第二次回到"没有颜色"
    store.undo();
    expect(store.content!.root.children[0].color).toBe('purple');
  });
});

describe('生成落地后能看到改动', () => {
  /**
   * 跑一次 edit 意图，拿到那一轮的树（`done` 帧直接给修改后的整棵树）。
   *
   * 返回**这一个** pinia：面板必须挂在同一个 store 实例上，否则它看到的是
   * 另一个空的 store（没有改动清单，diff 永远不出现），而用例会以"元素找不到"
   * 的形式失败——那种失败看起来像组件写错了。
   */
  async function runEdit(): Promise<{ store: ReturnType<typeof useMindMapStore>; pinia: Pinia }> {
    const pinia = createPinia();
    setActivePinia(pinia);
    stubFetch(() => agentReply([{ type: 'done', seq: 2, content: structuredClone(EDITED) }]));
    const store = useMindMapStore();
    store.preview(structuredClone(BASE), 'right');
    await store.runGeneration('把第三支拆细一点', 'edit');
    return { store, pinia };
  }

  it('面板列出「这次改动」，逐条写清改了什么', async () => {
    const { pinia } = await runEdit();
    const wrapper = mount(AgentPanel, { global: { plugins: [pinia] } });
    await wrapper.vm.$nextTick();

    const section = wrapper.find('[aria-label="这次改动"]');
    expect(section.exists()).toBe(true);
    expect(section.text()).toContain('修改');
    expect(section.text()).toContain('甲（拆细）');
    expect(section.text()).toContain('新增');
    expect(section.text()).toContain('删除');
    // 颜色的变化要能看出来（否则用户只知道"改了"，不知道改成什么色）
    expect(section.text()).toContain('purple');
  });

  it('点一条改动会把画布选中到那个节点', async () => {
    const { store, pinia } = await runEdit();
    const wrapper = mount(AgentPanel, { global: { plugins: [pinia] } });
    store.select(null);
    await wrapper.vm.$nextTick();

    const entry = wrapper.findAll('[aria-label="这次改动"] button')
      .find(item => item.text().includes('甲（拆细）'));
    expect(entry).toBeTruthy();
    await entry!.trigger('click');
    expect(store.selectedId).toBe('a');
  });

  it('create 意图没有可比基线：不显示改动清单', async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    stubFetch(() => agentReply([{ type: 'done', seq: 2, content: structuredClone(EDITED) }]));
    const store = useMindMapStore();
    await store.runGeneration('做一张新图', 'create');
    const wrapper = mount(AgentPanel, { global: { plugins: [pinia] } });
    await wrapper.vm.$nextTick();
    expect(wrapper.find('[aria-label="这次改动"]').exists()).toBe(false);
  });

  it('改动作数为 0 时说"一模一样"，而不是空区块', async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    stubFetch(() => agentReply([{ type: 'done', seq: 2, content: structuredClone(BASE) }]));
    const store = useMindMapStore();
    store.preview(structuredClone(BASE), 'right');
    await store.runGeneration('随便改改', 'edit');
    const wrapper = mount(AgentPanel, { global: { plugins: [pinia] } });
    await wrapper.vm.$nextTick();

    const section = wrapper.find('[aria-label="这次改动"]');
    expect(section.exists()).toBe(true);
    expect(section.text()).toContain('一模一样');
  });
});

/**
 * 「选中节点 + 一句话」。
 *
 * 这一组守的是本次改动的**主路径**：用户在画布上圈几个节点，然后说一句
 * 「这几个合并成一个」。选区必须真的进请求（否则模型只能从整棵树里猜），
 * 而且必须看得见（否则用户不知道自己圈的东西算不算数）。
 */
describe('「改这张图」认选区', () => {
  /** 选中三个节点：主选区 a，再追加 b 与 c。 */
  function selectThree(store: ReturnType<typeof useMindMapStore>) {
    store.preview(structuredClone(BASE), 'right');
    store.select('a');
    store.toggleSelect('b');
    store.toggleSelect('c');
  }

  it('选中三个节点：请求体里的 selected_ids 正好是这三条', async () => {
    const calls = stubFetch(() => agentReply([{ type: 'done', seq: 2, content: structuredClone(BASE) }]));
    const wrapper = mountApp();
    const store = useMindMapStore();
    selectThree(store);
    await wrapper.vm.$nextTick();

    await wrapper.find(EDIT_INPUT).setValue('这三个合并成一个');
    await wrapper.get(SUBMIT).trigger('click');
    await new Promise(resolve => setTimeout(resolve, 0));

    const body = calls[0].body as Record<string, unknown>;
    expect(body.intent).toBe('edit');
    // 顺序也有意义：第一个是主选区（详情面板对着的那个），后两个是追加的
    expect(body.selected_ids).toEqual(['a', 'b', 'c']);
  });

  it('徽标显示「已选 3 个节点」，placeholder 与按钮跟着变', async () => {
    const wrapper = mountApp();
    const store = useMindMapStore();
    await wrapper.vm.$nextTick();
    // 没选中时不显示徽标，placeholder 是那句通用的说法
    expect(wrapper.find('[data-test="edit-selection"]').exists()).toBe(false);
    expect((wrapper.find(EDIT_INPUT).element as HTMLTextAreaElement).placeholder)
      .toBe('发消息或写一个主题…');

    selectThree(store);
    await wrapper.vm.$nextTick();

    expect(wrapper.find('[data-test="edit-selection"]').text()).toBe('已选 3 个节点');
    expect((wrapper.find(EDIT_INPUT).element as HTMLTextAreaElement).placeholder)
      .toBe('改选中的这 3 个节点…');
    expect(wrapper.get(SUBMIT).text()).toBe('改这 3 个节点');
    // 提示语要说明"选区是提示不是限制"，否则用户会以为自己把模型锁住了
    expect(wrapper.text()).toContain('优先改这 3 个节点及其子分支');
  });

  it('没选中时提示语是"先在画布上选中节点，或直接描述要改哪里"', async () => {
    const wrapper = mountApp();
    useMindMapStore().preview(structuredClone(BASE), 'right');
    await wrapper.vm.$nextTick();

    expect(wrapper.text()).toContain('先在画布上选中节点，或直接描述要改哪里');
  });

  it('不回归：选了三个节点时，expand 仍然只发主选区那一个 id', async () => {
    const calls = stubFetch(() => agentReply([{ type: 'done', seq: 2, content: structuredClone(BASE) }]));
    const store = useMindMapStore();
    selectThree(store);
    await store.runGeneration('展开节点「甲」，补充 3 个子节点。', 'expand');

    const body = calls[0].body as Record<string, unknown>;
    expect(body.intent).toBe('expand');
    // 后端开流前要求 expand 的 selected_ids 长度恰好为 1
    expect(body.selected_ids).toEqual(['a']);
  });

  it('再点一次同一个节点就取消选中；主选区被取消时提一个追加选区上来', () => {
    const store = useMindMapStore();
    selectThree(store);
    expect(store.selectedIds).toEqual(['a', 'b', 'c']);

    store.toggleSelect('c');
    expect(store.selectedIds).toEqual(['a', 'b']);

    // 取消主选区：b 顶上来当主选区，详情面板不会停在"没选中但有选区"
    store.toggleSelect('a');
    expect(store.selectedIds).toEqual(['b']);
    expect(store.selectedId).toBe('b');

    // 普通单击换人：追加选区一起清掉
    store.select('c');
    expect(store.selectedIds).toEqual(['c']);
  });

  it('显式传空选区就真的不发选区（"看得见的才发"这条规矩的兜底）', async () => {
    const calls = stubFetch(() => agentReply([{ type: 'done', seq: 2, content: structuredClone(BASE) }]));
    const store = useMindMapStore();
    selectThree(store);
    // 画布右上角那个框按徽标上的数把选区带上去；反过来，任何一个不显示选区的
    // 入口都必须显式传 `[]`，否则它会静默带着用户在画布上圈的东西去"重新生成"。
    await store.runGeneration('按材料重做一张', 'edit', undefined, 'default', 'auto', { selection: [] });

    const body = calls[0].body as Record<string, unknown>;
    expect(body.intent).toBe('edit');
    expect(body.selected_ids).toEqual([]);
  });
});

/**
 * 候选 diff 上的「范围内 / 外」标记。
 *
 * 改图带上了选区之后，用户要知道的第一件事不是"改了几处"，而是"它有没有跑到
 * 我圈的范围外面"。所以每条改动都要能定位。
 */
describe('候选 diff 的范围标记', () => {
  /** 甲的标题改了（范围内），乙的标题也改了（范围外）。 */
  const SCOPED: MindMapContent = {
    title: '示例',
    root: {
      id: 'root', label: '根', children: [
        { id: 'a', label: '甲（改）', children: [] },
        { id: 'b', label: '乙（改）', children: [] },
        { id: 'c', label: '丙', children: [] },
      ],
    },
  };

  async function runScopedEdit() {
    const pinia = createPinia();
    setActivePinia(pinia);
    stubFetch(() => agentReply([{ type: 'done', seq: 2, content: structuredClone(SCOPED) }]));
    const store = useMindMapStore();
    store.preview(structuredClone(BASE), 'right');
    store.select('a');
    await store.runGeneration('把甲和乙都改一下', 'edit');
    return { store, pinia };
  }

  it('范围内 / 范围外各标一条，并在清单下面给出小结', async () => {
    const { pinia } = await runScopedEdit();
    const wrapper = mount(AgentPanel, { global: { plugins: [pinia] } });
    await wrapper.vm.$nextTick();

    const marks = wrapper.findAll('[data-test="diff-scope-mark"]');
    expect(marks).toHaveLength(2);
    // 甲在选中的子树里，乙不在
    expect(marks.map(mark => mark.text()).sort()).toEqual(['范围内', '范围外']);

    const rows = wrapper.findAll('[aria-label="这次改动"] li');
    const rowOf = (label: string) => rows.find(row => row.text().includes(label))!.text();
    expect(rowOf('甲（改）')).toContain('范围内');
    expect(rowOf('乙（改）')).toContain('范围外');

    expect(wrapper.find('[data-test="diff-scope-summary"]').text())
      .toContain('1 处改到了范围外');
  });

  it('没带选区时不显示任何范围标记', async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    stubFetch(() => agentReply([{ type: 'done', seq: 2, content: structuredClone(SCOPED) }]));
    const store = useMindMapStore();
    store.preview(structuredClone(BASE), 'right');
    // 不选中任何节点直接说"改这张图"
    await store.runGeneration('把甲和乙都改一下', 'edit');

    const wrapper = mount(AgentPanel, { global: { plugins: [pinia] } });
    await wrapper.vm.$nextTick();
    expect(wrapper.findAll('[data-test="diff-scope-mark"]')).toHaveLength(0);
    expect(wrapper.find('[data-test="diff-scope-summary"]').exists()).toBe(false);
  });

  it('在选中节点下面新增的节点也算范围内（新 id 在基线树里查不到）', async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const grown: MindMapContent = {
      title: '示例',
      root: {
        id: 'root', label: '根', children: [
          { id: 'a', label: '甲', children: [{ id: 'a1', label: '甲一（新）', children: [] }] },
          { id: 'b', label: '乙', children: [] },
          { id: 'c', label: '丙', children: [] },
        ],
      },
    };
    stubFetch(() => agentReply([{ type: 'done', seq: 2, content: structuredClone(grown) }]));
    const store = useMindMapStore();
    store.preview(structuredClone(BASE), 'right');
    store.select('a');
    await store.runGeneration('给甲补一个子节点', 'edit');

    const changes = store.generatedChanges!;
    const added = changes.find(item => item.kind === 'added')!;
    expect(added.id).toBe('a1');
    expect(added.inSelection).toBe(true);
  });
});
