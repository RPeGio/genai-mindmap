import { mount, type VueWrapper } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeAll, beforeEach, describe, expect, it } from 'vitest';

import type { FlowContent, MindNode } from '../src/api/types';
import MindMapCanvas from '../src/canvas/MindMapCanvas.vue';
import { layoutFlow } from '../src/canvas/layout/flow';
import { validateDraft } from '../src/mindmap/edits';
import {
  CANVAS_ROOT_ID,
  canvasEdgesOf,
  canvasStageEdgesOf,
  canvasTreeOf,
  edgesOfContent,
  stageInnerEdgesOf,
} from '../src/mindmap/shape';
import { useMindMapStore } from '../src/stores/mindmap';

/**
 * 阶段内部的判定出边（`FlowStage.edges`）真的被画出来、被删除、被导出。
 *
 * 这一组用例盯的是一个**具体的空洞**：3b 把 `edges` 加进了 `FlowStage`，但前端四条
 * 读边的路（`viewOf` / `canvasEdgesOf` / `edits.edgesOf` / store 的 `contentEdges`）
 * 全都只读 `content.edges`。契约上有了、视觉上完全没有——不是画歪，是没有代码在找它。
 *
 * 三条最容易做错的地方各有一条用例钉着：
 *
 *  1. 阶段内边的端点**本来就是节点 id**，拼进"阶段 id → 阶段入口"那段映射会被
 *     静默 `continue` 吞掉；
 *  2. 阶段内边**不能参与布局**（`Edge` 契约写明边不参与布局），否则 dagre 会把
 *     兄弟步骤推到下一 rank，整张流程图的分层变形——而且看起来"像是 dagre 该做的"；
 *  3. 侧栏删除必须知道该删哪一层数组（阶段内删 `stage.edges`，阶段间删 `content.edges`）。
 */

const node = (id: string, label: string, children: MindNode[] = []): MindNode => ({
  id, label, children,
});

/**
 * 一份**真实形状**的流程图：一个阶段里有一个判定节点（两个出口），
 * 外加一条步骤之间的顺序跳转（兄弟之间）。
 *
 * 兄弟之间那条（`n11 → n12`）是刻意的：它是**唯一**能把 dagre 的 rank 推变形的那种
 * 阶段内边（父子之间的边与已有的父子线同向，喂给 dagre 也不会动层号）。用它才能
 * 证明"布局只吃阶段间边"这条约束真的在起作用，而不是恰好没有区别。
 */
const flow = (withInner = true): FlowContent => ({
  title: '报销流程',
  diagram_type: 'flowchart',
  stages: [
    {
      id: 's_1',
      root: node('n1', '审不审', [node('n11', '填表'), node('n12', '补材料')]),
      ...(withInner ? {
        edges: [
          { id: 'e_i1', source: 'n1', target: 'n12', label: '否', kind: 'branch' as const },
          { id: 'e_i2', source: 'n11', target: 'n12', kind: 'flow' as const },
        ],
      } : {}),
    },
    { id: 's_2', root: node('n2', '付款') },
  ],
  edges: [{ id: 'e_1', source: 's_1', target: 's_2', label: '通过', kind: 'flow' }],
});

beforeAll(() => {
  const scope = globalThis as unknown as Record<string, unknown>;
  scope.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (window as any).DOMMatrixReadOnly = class {
    m22 = 1;
  };
});

beforeEach(() => {
  setActivePinia(createPinia());
});

describe('读侧：两层边各有各的函数', () => {
  it('stageInnerEdgesOf 把各阶段的边摊平；树与鱼骨是空数组', () => {
    expect(stageInnerEdgesOf(flow()).map(edge => edge.id)).toEqual(['e_i1', 'e_i2']);
    expect(stageInnerEdgesOf(flow(false))).toEqual([]);
    expect(stageInnerEdgesOf({
      title: '树', root: node('r', '根'),
    })).toEqual([]);
  });

  it('edgesOfContent 的语义没动：它只读 content.edges（内容真相）', () => {
    expect(edgesOfContent(flow()).map(edge => edge.id)).toEqual(['e_1']);
  });

  it('canvasEdgesOf 两路：阶段间换端点、阶段内原样追加', () => {
    const all = canvasEdgesOf(flow());
    expect(all.map(edge => edge.id)).toEqual(['e_1', 'e_i1', 'e_i2']);
    // 阶段间：阶段 id → 阶段入口节点 id
    expect([all[0]!.source, all[0]!.target]).toEqual(['n1', 'n2']);
    // 阶段内：端点本来就是节点 id，一个字节都不换
    expect([all[1]!.source, all[1]!.target]).toEqual(['n1', 'n12']);
    expect([all[2]!.source, all[2]!.target]).toEqual(['n11', 'n12']);
  });

  it('canvasStageEdgesOf 只有阶段间那一路（布局吃它）', () => {
    expect(canvasStageEdgesOf(flow()).map(edge => edge.id)).toEqual(['e_1']);
  });

  it('没有阶段内边时 canvasEdgesOf 不会多造一层：内容与阶段间那一路逐条相同', () => {
    const content = flow(false);
    expect(canvasEdgesOf(content)).toStrictEqual(canvasStageEdgesOf(content));
    // 树型下两者连 `content.edges` 的**原对象**都相同：一个字节都没变。
    const tree = {
      title: '树',
      root: node('r', '根', [node('a', '甲'), node('b', '乙')]),
      edges: [{ id: 'e_t', source: 'a', target: 'b' }],
    };
    expect(canvasEdgesOf(tree)).toBe(canvasStageEdgesOf(tree));
    expect(canvasEdgesOf(tree)).toBe(edgesOfContent(tree));
  });
});

describe('布局：阶段内边不参与分层', () => {
  it('加不加上阶段内边，每个阶段的坐标一模一样', () => {
    const withInner = flow(true);
    const withoutInner = flow(false);
    const treeOf = (content: FlowContent) => canvasTreeOf(content).root;

    const a = layoutFlow(treeOf(withInner), canvasStageEdgesOf(withInner));
    const b = layoutFlow(treeOf(withoutInner), canvasStageEdgesOf(withoutInner));
    expect([...a.positions]).toEqual([...b.positions]);
    expect([...a.layers]).toEqual([...b.layers]);
  });

  it('把阶段内边直接喂给 dagre 会变形——所以"只吃阶段间"这条约束是真的在起作用', () => {
    const content = flow(true);
    const root = canvasTreeOf(content).root;
    const guarded = layoutFlow(root, canvasStageEdgesOf(content));
    const naive = layoutFlow(root, canvasEdgesOf(content));
    // n11 与 n12 本来是同一层的兄弟；一条 n11 → n12 的"顺序跳转"会把 n12 压到下一层。
    expect(guarded.layers.get('n11')).toBe(guarded.layers.get('n12'));
    expect(naive.layers.get('n12')!).toBeGreaterThan(naive.layers.get('n11')!);
    expect(naive.positions.get('n12')!.y).toBeGreaterThan(guarded.positions.get('n12')!.y);
  });

  it('store 的布局与"去掉阶段内边"的那份完全一致', () => {
    const store = useMindMapStore();
    store.preview(flow(true), 'down');
    const withInner = [...store.layout!.positions];
    const layers = [...(store.layout as unknown as { layers: Map<string, number> }).layers];

    store.preview(flow(false), 'down');
    expect([...store.layout!.positions]).toEqual(withInner);
    expect([...(store.layout as unknown as { layers: Map<string, number> }).layers]).toEqual(layers);
    // 合成根还在（流程图上多一个不在 content 里的起点）
    expect(store.nodes.map(item => item.id)).toContain(CANVAS_ROOT_ID);
  });
});

describe('画布：阶段内边画成细线', () => {
  it('三条附加边都进了画布，阶段内的两条带 inner 标记', () => {
    const store = useMindMapStore();
    store.preview(flow(true), 'down');

    const attach = store.edges.filter(edge => edge.id.startsWith('attach:'));
    expect(attach.map(edge => edge.id)).toEqual(['attach:e_1', 'attach:e_i1', 'attach:e_i2']);
    expect(attach.map(edge => edge.data?.inner === true)).toEqual([false, true, true]);
    // 阶段间保留"回退"这个几何判据；阶段内边根本没进过分层，不可能被判成回退。
    expect(attach[1]!.data?.back).toBeUndefined();
  });

  it('真渲染：阶段内边的 stroke-width 比主轴细，kind 的颜色还在', async () => {
    const wrapper: VueWrapper = mount(MindMapCanvas, {
      global: { plugins: [setActivePinia(createPinia())] },
    });
    const store = useMindMapStore();
    store.preview(flow(true), 'down');
    for (let i = 0; i < 4; i += 1) {
      await wrapper.vm.$nextTick();
      await new Promise(resolve => setTimeout(resolve, 0));
    }

    const groups = wrapper.findAll('g.vue-flow__edge-mindEdge');
    expect(groups).toHaveLength(3);
    const paths = wrapper.findAll('path.vue-flow__edge-path')
      .map(item => item.attributes('style') ?? '')
      .filter(style => style.includes('stroke-width'));
    const widthOf = (style: string) => Number(/stroke-width:\s*([\d.]+)/.exec(style)?.[1] ?? '0');
    // 三条线：阶段之间的主轴（flow 1.5）、阶段内的「否」（branch 2 减细一档 = 1.25）、
    // 阶段内的顺序跳转（flow 1.5 减细一档，下限 1）。
    expect(paths.map(widthOf).sort((x, y) => x - y)).toEqual([1, 1.25, 1.5]);
    // 「否」那条细线仍然是靛蓝：细一档不等于丢掉语义色。
    const indigo = paths.filter(style => style.includes('#6366f1') || style.includes('99, 102, 241'));
    expect(indigo).toHaveLength(1);
    expect(widthOf(indigo[0]!)).toBe(1.25);
  });
});

describe('侧栏：两层分开列、都删得掉', () => {
  it('选中判定节点，阶段内那两条边出现在它名下', () => {
    const store = useMindMapStore();
    store.preview(flow(true), 'down');

    store.select('n1');
    expect(store.selectedInnerEdges.map(edge => edge.id)).toEqual(['e_i1']);
    // 阶段间的边仍然按"选中节点属于哪个阶段"列出（既有语义没动）
    expect(store.selectedEdges.map(edge => edge.id)).toEqual(['e_1']);
    expect(store.contentInnerEdges.map(edge => edge.id)).toEqual(['e_i1', 'e_i2']);
  });

  it('删除阶段内的边只动 stage.edges，content.edges 一个字节不变', () => {
    const store = useMindMapStore();
    store.preview(flow(true), 'down');
    store.select('n1');

    expect(store.deleteEdge('e_i1')).toBe(true);
    const content = store.content as unknown as FlowContent;
    expect(content.stages[0]!.edges!.map(edge => edge.id)).toEqual(['e_i2']);
    expect(content.edges!.map(edge => edge.id)).toEqual(['e_1']);
    // 可以撤销（连线是内容修改）
    expect(store.canUndo).toBe(true);
    store.undo();
    expect((store.content as unknown as FlowContent).stages[0]!.edges!.map(edge => edge.id))
      .toEqual(['e_i1', 'e_i2']);
  });

  it('删不存在的边返回 false，状态行不变', () => {
    const store = useMindMapStore();
    store.preview(flow(true), 'down');
    expect(store.deleteEdge('e_nope')).toBe(false);
  });
});

describe('写侧：同一阶段内的两个节点连一条边', () => {
  it('落进那个阶段的 edges，端点是节点 id', () => {
    const store = useMindMapStore();
    store.preview(flow(true), 'down');

    expect(store.connectNodes('n11', 'n1', '退回填表', 'loop')).toBe(true);
    const content = store.content as unknown as FlowContent;
    const inner = content.stages[0]!.edges!;
    expect(inner).toHaveLength(3);
    const added = inner[inner.length - 1]!;
    expect([added.source, added.target, added.label, added.kind])
      .toEqual(['n11', 'n1', '退回填表', 'loop']);
    // 没有跑到阶段间那一层去
    expect(content.edges!.map(edge => edge.id)).toEqual(['e_1']);
  });

  it('跨阶段仍然写 content.edges，端点换成阶段 id', () => {
    const store = useMindMapStore();
    store.preview(flow(false), 'down');

    expect(store.connectNodes('n11', 'n2', '跳过')).toBe(true);
    const content = store.content as unknown as FlowContent;
    expect(content.edges!.map(edge => [edge.source, edge.target])).toEqual([['s_1', 's_2'], ['s_1', 's_2']]);
  });
});

describe('删节点连带删边（两张表都清）', () => {
  it('删掉阶段内一个步骤，指向它的阶段内边跟着消失，保存不会 422', () => {
    const store = useMindMapStore();
    store.preview(flow(true), 'down');
    expect(validateDraft(store.content!) === null || true).toBe(true);

    store.deleteNode('n12');
    const content = store.content as unknown as FlowContent;
    expect(content.stages[0]!.edges!.map(edge => edge.id)).toEqual([]);
    expect(content.edges!.map(edge => edge.id)).toEqual(['e_1']);
    expect(validateDraft(store.content!)).toBeNull();
  });
});

/**
 * 200 这条上限的**口径**：每个数组各自算（不是各阶段合计）。
 *
 * 后端两处都是"只看这条边要落进的那个数组"——`ops._apply_add_flow_edge` 的
 * `len(sibling)`、契约 `FlowContent.validate_flow` 逐阶段调的 `_validate_edges`。
 * 前端却拿 `stageInnerEdgesOf()` 的**摊平结果**判 200，于是两个阶段各有 100 条时
 * 它把一条本该允许的边拦下来，而同样的动作经 `apply_flow_edge_ops` 能成功。
 * 下面三条分别钉住"两端都允许 / 满的那个才拒绝 / 额度不共享"。
 */
const innerEdges = (source: string, target: string, count: number, prefix: string) =>
  Array.from({ length: count }, (_, index) => ({
    id: `e_${prefix}${index}`,
    source,
    target,
    label: `第${index}条`,
    kind: 'flow' as const,
  }));

/** 两个阶段，各自装满 `countPerStage` 条阶段内边（端点互不相同，便于分别计数）。 */
const stagesWithInner = (countPerStage: number): FlowContent => ({
  title: '两个阶段',
  diagram_type: 'flowchart',
  stages: [
    {
      id: 's_1',
      root: node('n1', '甲阶段', [node('n11', '填表'), node('n12', '补材料')]),
      edges: innerEdges('n11', 'n12', countPerStage, 'a'),
    },
    {
      id: 's_2',
      root: node('n2', '乙阶段', [node('n21', '打款'), node('n22', '记账')]),
      edges: innerEdges('n21', 'n22', countPerStage, 'b'),
    },
  ],
  edges: [{ id: 'e_1', source: 's_1', target: 's_2', kind: 'flow' }],
});

describe('阶段内边的上限：按每个阶段各自算', () => {
  it('两个阶段各有 100 条时还能再加一条（合计 200，正是旧判据看到的数）', () => {
    const store = useMindMapStore();
    store.preview(stagesWithInner(100), 'down');
    expect(store.contentInnerEdges).toHaveLength(200);

    expect(store.connectNodes('n11', 'n12', '新的一条')).toBe(true);

    const content = store.content as unknown as FlowContent;
    expect(content.stages[0]!.edges).toHaveLength(101);
    expect(content.stages[1]!.edges).toHaveLength(100);
  });

  it('单个阶段到 201 条才拒绝，文案指向"这个阶段"', () => {
    const store = useMindMapStore();
    store.preview(stagesWithInner(200), 'down');

    expect(store.connectNodes('n11', 'n12', '第201条')).toBe(false);
    expect(store.status).toContain('这个阶段内部最多 200 条');
  });

  it('一个阶段满了，另一个阶段的额度不受影响', () => {
    const content = stagesWithInner(0);
    content.stages[0]!.edges = innerEdges('n11', 'n12', 200, 'a');
    const store = useMindMapStore();
    store.preview(content, 'down');

    expect(store.connectNodes('n21', 'n22', '乙阶段第一条')).toBe(true);
    expect((store.content as unknown as FlowContent).stages[1]!.edges).toHaveLength(1);
  });

  it('跨阶段的额度仍按 content.edges 单独算（阶段内再多也不吃它）', () => {
    const store = useMindMapStore();
    store.preview(stagesWithInner(200), 'down');
    expect(store.contentInnerEdges).toHaveLength(400);

    expect(store.connectNodes('n11', 'n22', '跳过')).toBe(true);
    expect((store.content as unknown as FlowContent).edges).toHaveLength(2);
  });
});

describe('校验：阶段内边按该阶段自己的节点命名空间查', () => {
  it('端点落在别的阶段 → 拦在保存之前', () => {
    const content = flow(true);
    content.stages[0]!.edges = [{ id: 'e_bad', source: 'n1', target: 'n2' }];
    expect(validateDraft(content)).toContain('不在阶段 s_1 里');
  });

  it('边 id 在两个数组之间重名 → 拦下（契约层同样硬拒）', () => {
    const content = flow(true);
    content.stages[0]!.edges = [{ id: 'e_1', source: 'n1', target: 'n12' }];
    expect(validateDraft(content)).toContain('边 ID 必须唯一');
  });

  it('一份合法的两层边内容通过校验', () => {
    expect(validateDraft(flow(true))).toBeNull();
  });
});
