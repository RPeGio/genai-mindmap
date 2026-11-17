import { flushPromises, mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import App from '../src/App.vue';
import type { MindMapContent, MindNode, OpBatch } from '../src/api/types';
import MindEdge from '../src/canvas/MindEdge.vue';
import { LIMITS } from '../src/mindmap/edits';
import { applyOps, normalizeOps, OpError } from '../src/mindmap/ops';
import { diffEdges, useMindMapStore } from '../src/stores/mindmap';

// jsdom 里没有 ResizeObserver，真正的 Vue Flow 画布跑不起来；界面用例只关心侧栏，
// 画布换成占位元素（与 `tests/app.spec.ts` 同一个做法）。
vi.mock('../src/canvas/MindMapCanvas.vue', () => ({
  default: { name: 'MindMapCanvasStub', template: '<div data-test="canvas" />' },
}));

const node = (id: string, label: string, children: MindNode[] = []): MindNode => ({
  id, label, children,
});

/** 三条阶段的流程：`b`（形式审查）有两个出口，`b → a` 是回退。 */
const flow = (): MindMapContent => ({
  title: '报销流程',
  root: node('root', '报销', [
    node('a', '提交申请'),
    node('b', '形式审查'),
    node('c', '付款'),
  ]),
  edges: [
    { id: 'e1', source: 'b', target: 'c', label: '是', kind: 'branch' },
    { id: 'e2', source: 'b', target: 'a', label: '驳回', kind: 'loop' },
  ],
});

const plain = (): MindMapContent => ({
  title: '思维导图',
  root: node('root', '根', [node('a', '甲'), node('b', '乙')]),
});

beforeEach(() => {
  setActivePinia(createPinia());
});

// ---------------------------------------------------------------------------
// 自定义边：三种 kind 的画法 + 标签
// ---------------------------------------------------------------------------

/**
 * `EdgeLabelRenderer` 内部要 `useVueFlow` 的上下文（它把标签 teleport 到画布外
 * 的一层 HTML）。单挂一个边组件时没有那个上下文，所以替换成一个直通 div：
 * 这里要验的是"标签写了什么、颜色怎么选"，而不是 teleport 的目标。
 *
 * `BaseEdge` 也一起换掉：它在真实画布里负责把 path 画进 SVG，这里只需要一个
 * 能读出 `style` / `marker-end` 的 `<path>`。
 */
const stubs = {
  EdgeLabelRenderer: { template: '<div class="edge-label"><slot /></div>' },
  BaseEdge: {
    props: ['path', 'markerEnd', 'style'],
    template: '<path class="vue-flow__edge-path" :d="path" :marker-end="markerEnd" :style="style" />',
  },
};

function mountEdge(data: Record<string, unknown>, id = 'attach:e1') {
  return mount(MindEdge, {
    props: {
      id,
      source: 'b',
      target: 'c',
      sourceX: 0,
      sourceY: 0,
      targetX: 120,
      targetY: 40,
      sourcePosition: 'right',
      targetPosition: 'left',
      data,
    } as never,
    global: { stubs },
  });
}

describe('自定义边渲染', () => {
  it('branch 用靛蓝实线，label 画在线上', () => {
    const wrapper = mountEdge({ label: '是', kind: 'branch' });
    const path = wrapper.find('path.vue-flow__edge-path');
    expect(path.exists()).toBe(true);
    expect(path.attributes('style')).toContain('rgb(99, 102, 241)');
    expect(path.attributes('style')).not.toContain('stroke-dasharray');
    expect(wrapper.find('.edge-label').text()).toBe('是');
  });

  it('loop 用虚线 + 箭头（回退的通用视觉语言）', () => {
    const wrapper = mountEdge({ label: '驳回', kind: 'loop' });
    const path = wrapper.find('path.vue-flow__edge-path');
    // 虚线是"往回走"的唯一信号，缺了它回退线与主干线看起来一样。
    expect(path.attributes('style')).toContain('stroke-dasharray');
    expect(path.attributes('style')).toContain('6 4');
    expect(path.attributes('marker-end')).toContain('url(#edge-arrow-');
    expect(wrapper.find('.edge-label').text()).toBe('驳回');
  });

  it('没有 kind 按 flow 画：灰色细线，不抢主干的注意力', () => {
    const wrapper = mountEdge({ label: '跳过两步' });
    const path = wrapper.find('path.vue-flow__edge-path');
    expect(path.attributes('style')).toContain('rgb(148, 163, 184)');
    expect(path.attributes('style')).not.toContain('stroke-dasharray');
  });

  it('没有 label 时不画标签块', () => {
    const wrapper = mountEdge({ kind: 'flow' });
    expect(wrapper.find('.edge-label').exists()).toBe(false);
  });

  it('每条边的箭头 marker id 唯一（否则颜色会互相覆盖）', () => {
    const first = mountEdge({ kind: 'branch' }, 'attach:e1');
    const second = mountEdge({ kind: 'loop' }, 'attach:e2');
    const idOf = (wrapper: ReturnType<typeof mountEdge>) =>
      wrapper.find('marker').attributes('id');
    expect(idOf(first)).not.toBe(idOf(second));
  });
});

// ---------------------------------------------------------------------------
// store：加边 / 删边 / 删节点连带删边
// ---------------------------------------------------------------------------

describe('附加关系的编辑', () => {
  it('连一条边：写进 content、记一步撤销、不重排节点', () => {
    const store = useMindMapStore();
    store.preview(flow(), 'right');
    const positionsBefore = store.nodes.map(item => `${item.id}:${item.position.x},${item.position.y}`);

    expect(store.connectNodes('a', 'c', '通过', 'flow')).toBe(true);
    expect(store.content?.edges).toHaveLength(3);
    expect(store.canUndo).toBe(true);
    expect(store.dirty).toBe(true);

    // **边不参与布局**：加一条线之后每个节点的坐标必须一模一样。
    // 这条是"树 + 边"能用的前提——否则用户每连一条线，刚记住的位置就没了。
    const positionsAfter = store.nodes.map(item => `${item.id}:${item.position.x},${item.position.y}`);
    expect(positionsAfter).toEqual(positionsBefore);
  });

  it('自环与重复边被拒绝，且不写脏内容', () => {
    const store = useMindMapStore();
    store.preview(flow(), 'right');

    expect(store.connectNodes('a', 'a')).toBe(false);
    expect(store.status).toContain('同一个节点');
    expect(store.connectNodes('b', 'c', '是')).toBe(false);  // e1 已经存在
    expect(store.content?.edges).toHaveLength(2);
    expect(store.canUndo).toBe(false);
  });

  it('同一对节点、不同 label 的两条边都能存在', () => {
    const store = useMindMapStore();
    store.preview(flow(), 'right');

    expect(store.connectNodes('b', 'c', '否', 'branch')).toBe(true);
    expect(store.contentEdges.map(item => item.label)).toEqual(['是', '驳回', '否']);
  });

  it('删除一条边只动 edges，不动树', () => {
    const store = useMindMapStore();
    store.preview(flow(), 'right');
    const rootBefore = JSON.stringify(store.content?.root);

    expect(store.deleteEdge('e2')).toBe(true);
    expect(store.contentEdges.map(item => item.id)).toEqual(['e1']);
    expect(JSON.stringify(store.content?.root)).toBe(rootBefore);
  });

  it('删节点会连带删掉与它相关的边', () => {
    const store = useMindMapStore();
    store.preview(flow(), 'right');

    store.deleteNode('b');
    // e1（b→c）与 e2（b→a）都以 b 为端点，必须一起消失；否则下一次保存 422。
    expect(store.content?.edges).toEqual([]);
  });

  it('额度到了就拒绝加边，而不是让它保存时才失败', () => {
    const store = useMindMapStore();
    const many = Array.from({ length: LIMITS.edges }, (_, index) => ({
      id: `e${index}`, source: 'a', target: 'c', label: `${index}`, kind: 'flow' as const,
    }));
    store.preview({ ...flow(), edges: many }, 'right');

    expect(store.contentEdges).toHaveLength(LIMITS.edges);
    expect(store.connectNodes('a', 'c', '再来一条')).toBe(false);
    expect(store.status).toContain('最多');
  });
});

// ---------------------------------------------------------------------------
// 画布：边的句柄要跟着附加边补齐
// ---------------------------------------------------------------------------

describe('画布上的边', () => {
  it('附加边画成 mindEdge，父子线仍然是 smoothstep', () => {
    const store = useMindMapStore();
    store.preview(flow(), 'right');

    const attach = store.edges.filter(item => item.type === 'mindEdge');
    const tree = store.edges.filter(item => item.type === 'smoothstep');
    expect(attach.map(item => (item.data as { label?: string }).label)).toEqual(['是', '驳回']);
    expect(attach.every(item => item.id.startsWith('attach:'))).toBe(true);
    expect(tree.length).toBeGreaterThan(0);
  });

  it('两节点之间同时有父子线与回退线时 id 不撞车', () => {
    const store = useMindMapStore();
    // root → a 是父子线；再加一条 a → root 的回退线（同两个端点）。
    store.preview({
      ...flow(),
      edges: [{ id: 'back', source: 'a', target: 'root', label: '退回', kind: 'loop' }],
    }, 'right');

    const ids = store.edges.map(item => item.id);
    expect(new Set(ids).size).toBe(ids.length);
    expect(ids).toContain('attach:back');
  });

  it('叶子节点也有出口句柄（否则回退线接不上）', () => {
    const store = useMindMapStore();
    store.preview({
      ...flow(),
      edges: [{ id: 'back', source: 'c', target: 'a', label: '返工', kind: 'loop' }],
    }, 'right');

    const leaf = store.nodes.find(item => item.id === 'c')!;
    const data = leaf.data as { out: string[] };
    expect(data.out.length).toBeGreaterThan(0);
  });
});

// ---------------------------------------------------------------------------
// 候选 diff：边也算进去
// ---------------------------------------------------------------------------

describe('候选 diff 算上边', () => {
  it('新增与删除的边分别上报', () => {
    // 直接把候选塞进 store 太重（要走整条 run 流程），这里用 `diffEdges` 验算：
    // `store.candidateDiff` 调的就是同一个函数，两边不会分叉。
    const next = {
      ...flow(),
      edges: [
        { id: 'e1', source: 'b', target: 'c', label: '是', kind: 'branch' as const },
        { id: 'e9', source: 'a', target: 'b', label: '通过', kind: 'flow' as const },
      ],
    };
    const changes = diffEdges(flow().edges!, next.edges, (id) => id);

    expect(changes.map(item => item.kind).sort()).toEqual(['edge_added', 'edge_removed']);
    expect(changes.find(item => item.kind === 'edge_added')?.label).toBe('a → b（通过）');
    expect(changes.find(item => item.kind === 'edge_removed')?.label).toBe('b → a（驳回）');
  });

  it('同一条边改标签也算一次改动', () => {
    const changes = diffEdges(
      [{ id: 'e1', source: 'b', target: 'c', label: '是', kind: 'branch' }],
      [{ id: 'e1', source: 'b', target: 'c', label: '否', kind: 'branch' }],
      (id) => id,
    );
    expect(changes).toHaveLength(1);
    expect(changes[0].kind).toBe('edge_added');
    expect(changes[0].detail).toContain('否');
  });

  it('边没变就一条都不报', () => {
    expect(diffEdges(flow().edges!, flow().edges!, (id) => id)).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// op 执行器：add_edge / remove_edge 与级联
// ---------------------------------------------------------------------------

function batch(ops: OpBatch['ops']): OpBatch {
  return { batch_id: 'b1', base_revision: 0, ops };
}

describe('op 执行器里的边', () => {
  it('add_edge 落库并返回被触及的节点', () => {
    const { content, changed_ids } = applyOps(plain(), batch([
      { op: 'add_edge', id: 'e1', source: 'a', target: 'b', label: '是', kind: 'branch' },
    ]));
    expect(content.edges).toEqual([
      { id: 'e1', source: 'a', target: 'b', label: '是', kind: 'branch' },
    ]);
    expect(changed_ids.sort()).toEqual(['a', 'b']);
  });

  it('失败时整批不生效（原内容对象不变）', () => {
    const before = plain();
    expect(() => applyOps(before, batch([
      { op: 'add_edge', id: 'e1', source: 'a', target: 'ghost' },
    ]))).toThrow(OpError);
    expect(before.edges).toBeUndefined();
  });

  it('自环在执行器里报 self_loop', () => {
    try {
      applyOps(plain(), batch([{ op: 'add_edge', id: 'e1', source: 'a', target: 'a' }]));
      throw new Error('应该抛错');
    } catch (error) {
      expect((error as OpError).code).toBe('self_loop');
    }
  });

  it('remove_edge 不动树，只删那条线', () => {
    const { content } = applyOps(flow(), batch([{ op: 'remove_edge', id: 'e1' }]));
    expect(content.edges?.map(item => item.id)).toEqual(['e2']);
    expect(content.root).toEqual(flow().root);
  });

  it('remove_edge 指向不存在的边时整批失败', () => {
    expect(() => applyOps(flow(), batch([{ op: 'remove_edge', id: 'nope' }])))
      .toThrow(OpError);
  });

  it('remove 节点连带删掉命中整棵子树的边', () => {
    const tree = node('root', '根', [
      node('a', '甲', [node('a1', '甲一')]),
      node('b', '乙'),
    ]);
    const content: MindMapContent = {
      title: 't', root: tree,
      edges: [
        { id: 'e1', source: 'a1', target: 'b' },   // 端点在子孙上
        { id: 'e2', source: 'b', target: 'a' },    // 端点在祖先上
        { id: 'e3', source: 'root', target: 'b' }, // 不命中
      ],
    };
    const result = applyOps(content, batch([{ op: 'remove', id: 'a' }]));
    expect(result.content.edges?.map(item => item.id)).toEqual(['e3']);
  });

  it('add_edge 的临时 id 会换成真实边 id，端点的临时名也跟着换', () => {
    // 真实路径是 `normalizeOps` → `applyOps`（与后端 `normalize_ops` → `apply_ops`
    // 逐条对应）。直接给 `applyOps` 喂 `$` 开头的 id 是错的用法，这里按真实顺序来。
    const drafts: OpBatch['ops'] = [
      { op: 'add', id: '$n1', parent_id: 'root', index: 0, label: '新节点' },
      { op: 'add_edge', id: '$e1', source: '$n1', target: 'b', label: '下一步' },
    ];
    const { ops } = normalizeOps(drafts, new Set(['root', 'a', 'b']));
    const { content, changed_ids } = applyOps(plain(), batch(ops));

    const created = content.root.children[0];
    // 前端规范化出来的节点 id 是完整的 uuid（没有前缀，见 `ops.ts::fullRandomId`），
    // 而边的 id 走 `e_` 前缀（与后端 `new_edge_id` 同口径）。
    expect(created.id.startsWith('$')).toBe(false);
    expect(created.label).toBe('新节点');
    expect(content.edges).toHaveLength(1);
    expect(content.edges![0].source).toBe(created.id);
    expect(content.edges![0].id.startsWith('e_')).toBe(true);
    expect(changed_ids).toContain(created.id);
  });
});

// ---------------------------------------------------------------------------
// 侧栏入口（App.vue）：手工补边与删边
// ---------------------------------------------------------------------------

describe('侧栏的附加关系入口', () => {
  it('选中节点后能列出相关关系、连一条新的、再删掉它', async () => {
    // App 挂载时会去拉导图列表与运行台账；这里只需要界面，不需要那些请求。
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true, status: 200, json: async () => [],
      body: { getReader: () => ({ read: async () => ({ done: true, value: undefined }) }) },
    })));

    const pinia = createPinia();
    setActivePinia(pinia);
    const wrapper = mount(App, { global: { plugins: [pinia] } });
    const store = useMindMapStore();
    store.preview(flow(), 'right');
    store.select('b');
    await flushPromises();

    const section = wrapper.find('[aria-label="附加关系"]');
    expect(section.exists()).toBe(true);
    // `b` 的两条边都列出来了。
    expect(section.text()).toContain('形式审查 → 付款');
    expect(section.text()).toContain('形式审查 → 提交申请');

    // 手工连一条：终点下拉 + 标签输入 + 按钮。
    await wrapper.find('select[aria-label="关系终点"]').setValue('a');
    await wrapper.find('input[aria-label="关系说明"]').setValue('退回');
    const connect = wrapper.findAll('button').find(item => item.text() === '连一条关系');
    expect(connect).toBeTruthy();
    await connect!.trigger('click');
    expect(store.content?.edges).toHaveLength(3);
    const added = store.contentEdges[store.contentEdges.length - 1];
    expect(added?.label).toBe('退回');

    // 删掉刚加的那条：列表里最后一个「删除」按钮。
    await flushPromises();
    const deletes = wrapper.find('[aria-label="附加关系"]').findAll('button')
      .filter(item => item.text() === '删除');
    await deletes[deletes.length - 1]!.trigger('click');
    expect(store.content?.edges).toHaveLength(2);

    vi.unstubAllGlobals();
  });
});


describe('校验', () => {
  it('dangling 边通过 op 通道进不来', () => {
    expect(() => applyOps(plain(), batch([
      { op: 'add_edge', id: 'e1', source: 'a', target: 'ghost' },
    ]))).toThrow(/不存在/);
  });

  it('契约层字段都不受影响：expandable / detail / color 仍能一起用', () => {
    const content: MindMapContent = {
      title: '带字段的流程',
      root: node('root', '根', [
        { id: 'a', label: '提交申请', children: [], expandable: 'yes', detail: '说明' },
        { id: 'b', label: '形式审查', children: [], color: 'amber' },
      ]),
      edges: [{ id: 'e1', source: 'a', target: 'b', label: '是', kind: 'branch' }],
    };
    const { content: next } = applyOps(content, batch([
      { op: 'add_edge', id: 'e9', source: 'b', target: 'a', label: '驳回', kind: 'loop' },
    ]));
    expect(next.root.children[0].expandable).toBe('yes');
    expect(next.root.children[0].detail).toBe('说明');
    expect(next.root.children[1].color).toBe('amber');
    expect(next.edges).toHaveLength(2);
  });
});
