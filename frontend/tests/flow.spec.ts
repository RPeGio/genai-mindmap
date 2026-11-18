import { describe, expect, it } from 'vitest';

import type { MindEdge, MindNode } from '../src/api/types';
import { layoutFlow } from '../src/canvas/layout/flow';
import { NODE_SIZE } from '../src/canvas/layout/tidy';

/**
 * 流程图分层单测。
 *
 * 形状取自生成侧的真实产物（`backend/app/agent/tools.py::FLOW_PLAN_PROMPT`）：
 * 一级分支是**阶段**、按执行先后排列，`edges` 只连 root 与一级分支。所以
 * "退回上一步"在数据里是**兄弟之间**的边，而不是指回祖先——这一点决定了分层
 * 该怎么算，也正是下面几条用例要钉住的东西。
 */

const stage = (id: string, label: string): MindNode => ({ id, label, children: [] });

/** 三个阶段 + 条件分支 + 驳回（与后端提示词里的示例同构）。 */
function flowTree(): { root: MindNode; edges: MindEdge[] } {
  return {
    root: {
      id: 'root',
      label: '报销',
      children: [stage('a', '提交申请'), stage('b', '形式审查'), stage('c', '付款')],
    },
    edges: [
      { id: 'e0', source: 'root', target: 'a', kind: 'flow' },
      { id: 'e1', source: 'b', target: 'c', label: '是', kind: 'branch' },
      { id: 'e2', source: 'b', target: 'a', label: '驳回', kind: 'loop' },
    ],
  };
}

function layersOf(result: ReturnType<typeof layoutFlow>) {
  return Object.fromEntries(result.layers);
}

describe('阶段分层', () => {
  it('根在第 0 层，阶段在它下面；层号从 0 起且连续', () => {
    const { root, edges } = flowTree();
    const result = layoutFlow(root, edges);
    const layers = layersOf(result);
    expect(layers.root).toBe(0);
    expect(layers.a).toBe(1);
    expect(layers.b).toBe(1);

    const used = [...result.layers.values()].sort((x, y) => x - y);
    expect(used[0]).toBe(0);
    for (let i = 1; i < used.length; i += 1) expect(used[i]! - used[i - 1]!).toBeLessThanOrEqual(1);
  });

  it('条件分支把目标阶段压到下一层（这是"用边分层"与"树换方向"的差别）', () => {
    const { root, edges } = flowTree();
    const result = layoutFlow(root, edges);
    const layers = layersOf(result);
    // b → c（是）要求 c 至少比 b 低一层；树布局里 c 会和 a、b 挤在同一列。
    expect(layers.c).toBe((layers.b ?? 0) + 1);
    expect(result.positions.get('c')!.y).toBeGreaterThan(result.positions.get('b')!.y);
  });

  it('驳回边不参与分层：它不会把"提交申请"拖到"形式审查"下面', () => {
    const { root, edges } = flowTree();
    const withLoop = layoutFlow(root, edges);
    const withoutLoop = layoutFlow(root, edges.filter(edge => edge.id !== 'e2'));
    // 去掉驳回边之后，每个节点的层号与位置都不变——说明它确实没被拿去分层。
    expect(layersOf(withLoop)).toEqual(layersOf(withoutLoop));
    for (const id of ['root', 'a', 'b', 'c']) {
      expect(withLoop.positions.get(id)).toEqual(withoutLoop.positions.get(id));
    }
  });

  it('每个父节点的层号都小于子节点（不会被贪心拆环反向）', () => {
    const { root, edges } = flowTree();
    const result = layoutFlow(root, edges);
    const walk = (node: MindNode) => {
      for (const child of node.children) {
        expect(result.layers.get(node.id)!).toBeLessThan(result.layers.get(child.id)!);
        expect(result.positions.get(node.id)!.y).toBeLessThan(result.positions.get(child.id)!.y);
        walk(child);
      }
    };
    walk(root);
  });

  it('二点互指（b⇄c）也不会反向父子边', () => {
    const root: MindNode = {
      id: 'root', label: '根',
      children: [
        { id: 'b', label: '乙', children: [stage('c', '丙')] },
      ],
    };
    // c → b 是回退（目标前序在前），b ⇄ c 之间就成环了；分层仍必须保持父子序。
    const result = layoutFlow(root, [{ id: 'back', source: 'c', target: 'b', kind: 'loop' }]);
    expect(result.layers.get('root')!).toBeLessThan(result.layers.get('b')!);
    expect(result.layers.get('b')!).toBeLessThan(result.layers.get('c')!);
    expect(result.backEdges.has('back')).toBe(true);
  });
});

describe('回退边的判定', () => {
  it('目标阶段在源之前 → 回退', () => {
    const { root, edges } = flowTree();
    const result = layoutFlow(root, edges);
    expect([...result.backEdges]).toEqual(['e2']);
  });

  it('前序在前的边一律算回退（含"指回祖先"）', () => {
    const root: MindNode = {
      id: 'root', label: '根',
      children: [{ id: 'a', label: '甲', children: [stage('a1', '甲一')] }, stage('b', '乙')],
    };
    const result = layoutFlow(root, [
      { id: 'up', source: 'a1', target: 'root', kind: 'loop' },
      { id: 'skip', source: 'b', target: 'a1', kind: 'loop' },
      { id: 'forward', source: 'a', target: 'b', kind: 'flow' },
    ]);
    expect(result.backEdges.has('up')).toBe(true);
    expect(result.backEdges.has('skip')).toBe(true);
    expect(result.backEdges.has('forward')).toBe(false);
  });

  it('落在同一层的边也算回退（画的时候要绕开主干）', () => {
    // 两个互不相干的阶段之间横着连一条：前序在前的当源，于是它是"正向"的；
    // 反向那条（b → a）才是回退。这里断言的是"层号相同也进 backEdges"这条并集规则。
    const root: MindNode = {
      id: 'r', label: '根',
      children: [stage('a', '甲'), stage('b', '乙'), stage('c', '丙')],
    };
    const result = layoutFlow(root, [{ id: 'x', source: 'b', target: 'a' }]);
    expect(result.backEdges.has('x')).toBe(true);
  });

  it('悬空边与自环不进图、也不算回退', () => {
    const { root, edges } = flowTree();
    const result = layoutFlow(root, [
      ...edges,
      { id: 'ghost', source: 'a', target: 'nope' },
      { id: 'self', source: 'a', target: 'a' },
    ]);
    expect(result.positions.size).toBe(4);
    expect(result.backEdges.has('ghost')).toBe(false);
    expect(result.backEdges.has('self')).toBe(false);
  });
});

describe('坐标与包围盒', () => {
  it('根落在原点（与树布局同一个约定），坐标是节点中心', () => {
    const { root, edges } = flowTree();
    const result = layoutFlow(root, edges);
    expect(result.positions.get('root')).toEqual({ x: 0, y: 0 });
  });

  it('包围盒包住所有节点框，且大于单个节点', () => {
    const { root, edges } = flowTree();
    const result = layoutFlow(root, edges);
    expect(result.bounds.width).toBeGreaterThan(NODE_SIZE.width);
    expect(result.bounds.height).toBeGreaterThan(NODE_SIZE.height);
    for (const point of result.positions.values()) {
      expect(point.x - NODE_SIZE.width / 2).toBeGreaterThanOrEqual(result.bounds.minX - 1e-9);
      expect(point.x + NODE_SIZE.width / 2).toBeLessThanOrEqual(result.bounds.maxX + 1e-9);
      expect(point.y - NODE_SIZE.height / 2).toBeGreaterThanOrEqual(result.bounds.minY - 1e-9);
      expect(point.y + NODE_SIZE.height / 2).toBeLessThanOrEqual(result.bounds.maxY + 1e-9);
    }
  });

  it('同层节点不横向重叠', () => {
    const { root, edges } = flowTree();
    const result = layoutFlow(root, edges);
    const byLayer = new Map<number, Array<{ id: string; x: number }>>();
    for (const [id, layer] of result.layers) {
      const list = byLayer.get(layer) ?? [];
      list.push({ id, x: result.positions.get(id)!.x });
      byLayer.set(layer, list);
    }
    for (const [layer, nodes] of byLayer) {
      nodes.sort((left, right) => left.x - right.x);
      for (let i = 1; i < nodes.length; i += 1) {
        const gap = nodes[i]!.x - nodes[i - 1]!.x;
        expect(gap, `第 ${layer} 层 ${nodes[i - 1]!.id}/${nodes[i]!.id}`)
          .toBeGreaterThanOrEqual(NODE_SIZE.width);
      }
    }
  });

  it('同一份输入两次结果完全一致（没有随机源）', () => {
    const { root, edges } = flowTree();
    const first = layoutFlow(root, edges);
    const second = layoutFlow(root, edges);
    expect([...first.positions]).toEqual([...second.positions]);
    expect([...first.backEdges]).toEqual([...second.backEdges]);
  });

  it('单个节点也能排：坐标在原点、包围盒等于节点尺寸', () => {
    const result = layoutFlow({ id: 'r', label: '唯一', children: [] }, []);
    expect(result.positions.get('r')).toEqual({ x: 0, y: 0 });
    expect(result.bounds.width).toBe(NODE_SIZE.width);
    expect(result.bounds.height).toBe(NODE_SIZE.height);
    expect(result.direction).toBe('down');
  });
});

describe('判定节点', () => {
  it('两个出口的节点，两条子链各占一层（布局上已经是分叉）', () => {
    const root: MindNode = {
      id: 'r', label: '根',
      children: [
        {
          id: 'decide', label: '审不审',
          children: [stage('yes', '通过'), stage('no', '退回')],
        },
      ],
    };
    const result = layoutFlow(root, [
      { id: 'y', source: 'decide', target: 'yes', label: '是', kind: 'branch' },
      { id: 'n', source: 'decide', target: 'no', label: '否', kind: 'branch' },
    ]);
    expect(result.layers.get('yes')).toBe(result.layers.get('no'));
    expect(result.positions.get('yes')!.y).toBe(result.positions.get('no')!.y);
    expect(result.positions.get('yes')!.x).not.toBe(result.positions.get('no')!.x);
    // "是/否"这两条边与父子边同向，不算回退
    expect(result.backEdges.size).toBe(0);
  });
});
