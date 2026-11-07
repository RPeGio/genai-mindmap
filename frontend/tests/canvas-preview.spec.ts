import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { MindMapContent } from '../src/api/types';
import App from '../src/App.vue';
import MindNode from '../src/canvas/MindNode.vue';
import {
  PREVIEW_ID_PREFIX,
  previewNodeId,
  useMindMapStore,
  type MindNodeData,
} from '../src/stores/mindmap';

/**
 * 画布的渲染源。
 *
 * 用户的抱怨是「节点一个个到达，但画布中间始终是空的」：预览只长在右侧面板里。
 * 这组用例守的是画布的取数规则——三级优先级 content → candidate → agentPreview，
 * 以及「看得见」与「能采纳」这两件事始终分得开。
 */

/** 手动推进的 SSE 响应：生成中的状态只在流没结束时存在。 */
function controllableReply() {
  const encoder = new TextEncoder();
  let push: (frame: unknown) => void = () => {};
  let finish: () => void = () => {};
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      push = frame => { controller.enqueue(encoder.encode(`data: ${JSON.stringify(frame)}\n\n`)); };
      finish = () => {
        controller.enqueue(encoder.encode('data: [DONE]\n\n'));
        controller.close();
      };
    },
  });
  const response = { ok: true, status: 200, body, json: async () => ({}) } as unknown as Response;
  return { response, push, finish };
}

function grow(path: number[] | null, label: string, expandable: boolean | null = null) {
  return { type: 'node_grow', seq: 1, path, label, expandable };
}

/** 一份"已应用"的内容：id 是真 id，和预览的派生 id 不会撞。 */
function content(label: string): MindMapContent {
  return {
    title: '示例',
    root: { id: 'root', label, children: [{ id: 'a', label: '甲', children: [] }] },
  };
}

function startStream(goal = '做一张导图') {
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
  vi.unstubAllEnvs();
});

describe('预览节点的 id', () => {
  it('由路径派生，同一棵树永远得到同一套 id', () => {
    expect(previewNodeId([])).toBe('preview-root');
    expect(previewNodeId([0])).toBe('preview-0');
    expect(previewNodeId([0, 2, 1])).toBe('preview-0-2-1');
    // 同一个路径算两次必须一致：不一致的话布局缓存和 Vue 的 key 都会跟着抖
    expect(previewNodeId([0, 2, 1])).toBe(previewNodeId([0, 2, 1]));
  });

  it('不同路径不会撞车，且都带前缀', () => {
    const paths = [[], [0], [0, 0], [0, 1], [0, 1, 0], [1], [1, 0], [10]];
    const ids = paths.map(path => previewNodeId(path));
    expect(new Set(ids).size).toBe(paths.length);
    expect(ids.every(id => id.startsWith(PREVIEW_ID_PREFIX))).toBe(true);
    // 10 与 [1,0] 是两条不同的路径，不能被拼成同一个字符串
    expect(previewNodeId([10])).not.toBe(previewNodeId([1, 0]));
  });
});

describe('渲染源：content 为空时画布画预览', () => {
  it('预览边到边长，画布上的节点跟着多起来', async () => {
    const { stream, store, running } = startStream();
    expect(store.nodes).toHaveLength(0);

    stream.push(grow(null, '机器学习'));
    await vi.waitFor(() => expect(store.nodes).toHaveLength(1));
    expect(store.nodes[0].id).toBe(previewNodeId([]));
    expect(store.nodes[0].data?.label).toBe('机器学习');

    stream.push(grow([], '监督学习'));
    stream.push(grow([0], '线性回归'));
    await vi.waitFor(() => expect(store.nodes).toHaveLength(3));

    expect(store.nodes.map(node => node.id)).toEqual([
      previewNodeId([]),
      previewNodeId([0]),
      previewNodeId([0, 0]),
    ]);
    // 连线也得跟着长，否则画布上是一堆孤立的框
    expect(store.edges).toHaveLength(2);
    expect(store.layout?.positions.size).toBe(3);

    stream.push({ type: 'done', seq: 9 });
    stream.finish();
    await running;
  });

  it('增量到达时已有节点的 id 不变', async () => {
    const { stream, store, running } = startStream();
    stream.push(grow(null, '根'));
    stream.push(grow([], '一'));
    await vi.waitFor(() => expect(store.nodes).toHaveLength(2));
    const before = store.nodes.map(node => node.id);

    // 每来一帧整棵树都会被重新派生一次；旧节点的 id 必须原样保留，
    // 否则 Vue Flow 会认为这是新节点，元素重建、视图跟着跳。
    stream.push(grow([], '二'));
    await vi.waitFor(() => expect(store.nodes).toHaveLength(3));
    expect(store.nodes.slice(0, before.length).map(node => node.id)).toEqual(before);

    stream.push({ type: 'done', seq: 9 });
    stream.finish();
    await running;
  });

  it('同一棵树重放一次，得到完全相同的 id', async () => {
    const frames = [grow(null, '根'), grow([], '甲'), grow([0], '甲的孩'), grow([], '乙')];

    const first = startStream('第一轮');
    for (const frame of frames) first.stream.push(frame);
    await vi.waitFor(() => expect(first.store.nodes).toHaveLength(4));
    const firstIds = first.store.nodes.map(node => node.id);
    first.store.cancel();
    first.stream.finish();
    await first.running;

    const second = startStream('第二轮');
    for (const frame of frames) second.stream.push(frame);
    await vi.waitFor(() => expect(second.store.nodes).toHaveLength(4));
    expect(second.store.nodes.map(node => node.id)).toEqual(firstIds);

    second.stream.push({ type: 'done', seq: 9 });
    second.stream.finish();
    await second.running;
  });
});

describe('渲染源：content 优先', () => {
  it('已有内容时预览不顶掉它', async () => {
    const store = useMindMapStore();
    store.preview(content('旧图'), 'right');
    const applied = store.nodes.map(node => node.id);
    expect(applied).toEqual(['root', 'a']);

    const { stream, running } = startStream();
    stream.push(grow(null, '新生成'));
    stream.push(grow([], '新分支'));
    await vi.waitFor(() => expect(store.agentPreview?.children).toHaveLength(1));

    // 用户已经打开了一张图：让没定稿的预览顶上去，看起来就是"我的图被改了"
    expect(store.nodes.map(node => node.id)).toEqual(applied);
    expect(store.nodes.every(node => !node.id.startsWith(PREVIEW_ID_PREFIX))).toBe(true);
    expect(store.nodes.every(node => node.selectable === true)).toBe(true);

    stream.push({ type: 'done', seq: 9 });
    stream.finish();
    await running;
  });

  it('已有内容时候选也不顶掉它', async () => {
    const store = useMindMapStore();
    store.preview(content('旧图'), 'right');

    const { stream, running } = startStream();
    stream.push({ type: 'snapshot', seq: 3, content: content('候选根') });
    await vi.waitFor(() => expect(store.candidate).not.toBeNull());

    expect(store.nodes.map(node => node.id)).toEqual(['root', 'a']);
    expect(store.nodes[0].data?.label).toBe('旧图');

    stream.push({ type: 'done', seq: 9 });
    stream.finish();
    await running;
  });
});

describe('渲染源：候选接住预览清空的那一刻', () => {
  it('snapshot 到达时画布不空一帧，直接换成候选', async () => {
    const { stream, store, running } = startStream();
    stream.push(grow(null, '预览根'));
    stream.push(grow([], '预览分支'));
    await vi.waitFor(() => expect(store.nodes).toHaveLength(2));
    expect(store.nodes[0].id.startsWith(PREVIEW_ID_PREFIX)).toBe(true);

    stream.push({ type: 'snapshot', seq: 8, content: content('快照根') });
    await vi.waitFor(() => expect(store.candidate).not.toBeNull());

    // 预览这一刻被整棵作废了，但画布不能跟着掉回空态：这正是用户抱怨的那一帧
    expect(store.agentPreview).toBeNull();
    expect(store.nodes.length).toBeGreaterThan(0);
    expect(store.nodes.map(node => node.id)).toEqual(['root', 'a']);
    expect(store.nodes[0].data?.label).toBe('快照根');
    // 候选只是"看得见"：节点仍然是不可选、不可编辑的草稿
    expect(store.nodes.every(node => node.selectable === false)).toBe(true);
    expect(store.nodes.every(node => node.data?.draft === 'candidate')).toBe(true);

    stream.push({ type: 'done', seq: 9 });
    stream.finish();
    await running;
  });

  it('候选能被应用，应用之后画布回到正常节点', async () => {
    const { stream, store, running } = startStream();
    stream.push(grow(null, '预览根'));
    await vi.waitFor(() => expect(store.nodes).toHaveLength(1));

    stream.push({ type: 'snapshot', seq: 8, content: content('快照根') });
    await vi.waitFor(() => expect(store.candidate).not.toBeNull());

    // 渲染源变了，采纳语义一步没松：候选仍然要用户点「应用」才进 content
    expect(store.content).toBeNull();
    expect(store.acceptCandidate()).toBe('applied');

    expect(store.content?.root.label).toBe('快照根');
    expect(store.candidate).toBeNull();
    expect(store.nodes.map(node => node.id)).toEqual(['root', 'a']);
    expect(store.nodes.every(node => node.selectable === true)).toBe(true);
    expect(store.nodes.every(node => node.data?.draft === undefined)).toBe(true);

    stream.push({ type: 'done', seq: 9 });
    stream.finish();
    await running;
  });
});

describe('预览与候选都不能编辑', () => {
  it('预览节点不可选中，选中请求被忽略', async () => {
    const { stream, store, running } = startStream();
    stream.push(grow(null, '根'));
    await vi.waitFor(() => expect(store.nodes).toHaveLength(1));

    const id = store.nodes[0].id;
    expect(store.nodes[0].selectable).toBe(false);

    store.select(id);
    expect(store.selectedId).toBeNull();
    expect(store.nodes[0].selected).toBe(false);
    // 详情面板据此显示"还没选中"，而不是对着一个不存在的节点说话
    expect(store.selectedNode).toBeNull();
    expect(store.breadcrumb).toEqual([]);

    stream.push({ type: 'done', seq: 9 });
    stream.finish();
    await running;
  });

  it('对预览 id 调编辑入口不会改到任何数据', async () => {
    const { stream, store, running } = startStream();
    stream.push(grow(null, '根'));
    stream.push(grow([], '分支'));
    await vi.waitFor(() => expect(store.nodes).toHaveLength(2));
    const id = store.nodes[1].id;

    store.addChildTo(id);
    store.addSiblingTo(id);
    store.deleteNode(id);
    store.beginEdit(id);
    store.commitLabel(id, '改掉它');

    // content 一直是空的，编辑入口全部早退：预览没有被"编辑"变成真的树
    expect(store.content).toBeNull();
    expect(store.agentPreview?.children[0].label).toBe('分支');
    expect(store.nodeCount).toBe(2);
    expect(store.canUndo).toBe(false);
    expect(store.selectedId).toBeNull();
    expect(store.editingId).toBeNull();

    stream.push({ type: 'done', seq: 9 });
    stream.finish();
    await running;
  });

  it('只有预览时 acceptCandidate 仍然返回 invalid', async () => {
    const { stream, store, running } = startStream();
    stream.push(grow(null, '预览根'));
    stream.push(grow([], '预览分支'));
    await vi.waitFor(() => expect(store.nodes).toHaveLength(2));

    expect(store.candidate).toBeNull();
    expect(store.acceptCandidate()).toBe('invalid');
    // 画布上已经看得见这棵树，但它一个字都没进 content
    expect(store.content).toBeNull();
    expect(store.nodeCount).toBe(2);

    stream.push({ type: 'done', seq: 9 });
    stream.finish();
    await running;
  });
});

describe('预览清空后画布回到空态', () => {
  it('error 帧清掉预览，画布不留下半截树', async () => {
    const { stream, store, running } = startStream();
    stream.push(grow(null, '根'));
    stream.push(grow([], '分支'));
    await vi.waitFor(() => expect(store.nodes).toHaveLength(2));

    stream.push({ type: 'error', seq: 8, code: 'generation_failed', message: '模型服务不可用' });
    await vi.waitFor(() => expect(store.agentPreview).toBeNull());

    expect(store.nodes).toEqual([]);
    expect(store.edges).toEqual([]);
    expect(store.nodeCount).toBe(0);
    expect(store.layout).toBeNull();

    stream.finish();
    await running;
  });

  it('取消之后同样回到空态，不留预览残影', async () => {
    const { stream, store, running } = startStream();
    stream.push(grow(null, '根'));
    await vi.waitFor(() => expect(store.nodes).toHaveLength(1));

    store.cancel();
    expect(store.nodes).toEqual([]);
    expect(store.nodeCount).toBe(0);

    stream.finish();
    await running;
  });
});

describe('画布 DOM', () => {
  /** jsdom 没有 ResizeObserver，真实 Vue Flow 挂载需要一个空壳。 */
  class ResizeObserverStub {
    observe() {}
    unobserve() {}
    disconnect() {}
  }

  /**
   * jsdom 也没有 DOMMatrixReadOnly，而 Vue Flow 量节点尺寸时要从视口变换里读缩放。
   * 补一个只提供 m22 的桩：jsdom 里元素尺寸恒为 0，量不出变化，后续分支不会走到。
   */
  class DOMMatrixReadOnlyStub {
    m22 = 1;
    constructor(_transform?: string) {}
  }

  it('生成过程中画布上真的长出节点，而不是只有右侧面板在动', async () => {
    vi.stubGlobal('ResizeObserver', ResizeObserverStub);
    vi.stubGlobal('DOMMatrixReadOnly', DOMMatrixReadOnlyStub);

    const pinia = createPinia();
    setActivePinia(pinia);
    const wrapper = mount(App, { global: { plugins: [pinia] }, attachTo: document.body });
    const store = useMindMapStore();

    expect(store.nodes).toHaveLength(0);
    expect(wrapper.text()).toContain('还没有导图');

    const stream = controllableReply();
    vi.stubGlobal('fetch', vi.fn(async () => stream.response));
    const running = store.runGeneration('做一张导图', 'create');

    stream.push(grow(null, '机器学习'));
    stream.push(grow([], '监督学习'));

    await vi.waitFor(() => expect(wrapper.findAll('.vue-flow__node')).toHaveLength(2));
    // 有节点之后占位文案必须让位，否则它会盖在刚长出来的树上
    expect(wrapper.text()).not.toContain('还没有导图');

    // 快照到达：预览作废、候选接手，画布上的节点数不能掉到 0
    stream.push({ type: 'snapshot', seq: 8, content: content('快照根') });
    await vi.waitFor(() => expect(store.candidate).not.toBeNull());
    await vi.waitFor(() => expect(wrapper.findAll('.vue-flow__node')).toHaveLength(2));
    expect(wrapper.text()).not.toContain('还没有导图');

    stream.push({ type: 'done', seq: 9 });
    stream.finish();
    await running;
    wrapper.unmount();
  });
});

describe('节点样式', () => {
  function nodeData(over: Partial<MindNodeData> = {}): MindNodeData {
    return { label: '节点', body: '节点', fullLabel: '节点', depth: 1, out: [], in: [], ...over };
  }

  function box(data: MindNodeData, selected = false) {
    const wrapper = mount(MindNode, { props: { id: 'x', data, selected } });
    const hit = wrapper.find('div.rounded-md.border');
    if (!hit.exists()) throw new Error('没找到节点框');
    return { wrapper, box: hit };
  }

  it('已应用的节点样式不受新增字段影响', () => {
    const child = box(nodeData()).box;
    expect(child.classes()).toContain('border-slate-300');
    expect(child.classes()).toContain('text-slate-700');
    expect(child.classes()).not.toContain('border-dashed');
    expect(child.classes().some(name => name.startsWith('opacity-'))).toBe(false);

    const root = box(nodeData({ depth: 0 })).box;
    expect(root.classes()).toContain('bg-slate-900');

    const selected = box(nodeData(), true).box;
    expect(selected.classes()).toContain('border-slate-900');
    expect(selected.classes()).toContain('shadow-md');
  });

  it('预览节点是虚线的草稿，明确不展开的再弱一档', () => {
    const draft = box(nodeData({ draft: 'preview' })).box;
    expect(draft.classes()).toContain('border-dashed');
    expect(draft.classes()).toContain('opacity-60');

    const leaf = box(nodeData({ draft: 'preview', previewLeaf: true })).box;
    expect(leaf.classes()).toContain('border-dashed');
    expect(leaf.classes()).toContain('opacity-40');

    // 根节点也不走深色：预览的根还没被采纳，长得像正式根只会让人以为图已经生成了
    const root = box(nodeData({ draft: 'preview', depth: 0 })).box;
    expect(root.classes()).not.toContain('bg-slate-900');
    expect(root.classes()).toContain('border-dashed');
  });

  it('候选是实线草稿：比预览实，比已应用的图淡', () => {
    const pending = box(nodeData({ draft: 'candidate', previewLeaf: true })).box;
    expect(pending.classes()).toContain('border-slate-400');
    expect(pending.classes()).toContain('opacity-80');
    expect(pending.classes()).not.toContain('border-dashed');
  });
});
