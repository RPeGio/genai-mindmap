import { describe, expect, it } from 'vitest';

import type { MindNode } from '../src/api/types';
import { handleSide, layoutTidy, NODE_SIZE } from '../src/canvas/layout/tidy';

/** 造一棵两层树：根 + 三个叶子。 */
function tree(): MindNode {
  return {
    id: 'root',
    label: '根',
    children: [
      { id: 'a', label: '甲', children: [] },
      { id: 'b', label: '乙', children: [] },
      { id: 'c', label: '丙', children: [] },
    ],
  };
}

function point(result: ReturnType<typeof layoutTidy>, id: string) {
  const found = result.positions.get(id);
  if (!found) throw new Error(`缺少节点 ${id}`);
  return found;
}

describe('layoutTidy', () => {
  it('单个节点落在原点，包围盒等于节点尺寸', () => {
    const result = layoutTidy({ id: 'r', label: '根', children: [] });
    expect(result.positions.size).toBe(1);
    expect(point(result, 'r')).toEqual({ x: 0, y: 0 });
    expect(result.bounds.width).toBe(NODE_SIZE.width);
    expect(result.bounds.height).toBe(NODE_SIZE.height);
  });

  it('向右布局：层级沿 x 增长，兄弟沿 y 排开且互不重叠', () => {
    const result = layoutTidy(tree(), { direction: 'right' });
    const root = point(result, 'root');
    const kids = ['a', 'b', 'c'].map(id => point(result, id));

    expect(root.x).toBeLessThan(kids[0].x);
    // 同一层的 x 必须完全一致，否则看出层级是歪的
    expect(new Set(kids.map(k => k.x)).size).toBe(1);
    // 兄弟间距 = 节点高 + 兄弟间隙，保证节点框不叠在一起
    expect(kids[1].y - kids[0].y).toBe(NODE_SIZE.height + 20);
    // 父节点纵向居中于子节点区间
    expect(root.y).toBe((kids[0].y + kids[2].y) / 2);
  });

  it('向左布局是向右的镜像', () => {
    const right = layoutTidy(tree(), { direction: 'right' });
    const left = layoutTidy(tree(), { direction: 'left' });

    for (const id of ['root', 'a', 'b', 'c']) {
      expect(point(left, id).x).toBe(-point(right, id).x);
      expect(point(left, id).y).toBe(point(right, id).y);
    }
  });

  it('向下布局：层级沿 y 增长，兄弟沿 x 排开', () => {
    const result = layoutTidy(tree(), { direction: 'down' });
    const root = point(result, 'root');
    const kids = ['a', 'b', 'c'].map(id => point(result, id));

    expect(root.y).toBeLessThan(kids[0].y);
    expect(new Set(kids.map(k => k.y)).size).toBe(1);
    // 兄弟间距 = 节点宽 + 间隙，垂直布局时横向不重叠
    expect(kids[1].x - kids[0].x).toBe(NODE_SIZE.width + 24);
    expect(root.x).toBe((kids[0].x + kids[2].x) / 2);
  });

  it('双向布局把子节点对半分列两侧', () => {
    const result = layoutTidy(tree(), { direction: 'both' });
    const root = point(result, 'root');
    const xs = ['a', 'b', 'c'].map(id => point(result, id).x);

    expect(root.x).toBe(0);
    // 三个子节点：两个在右、一个在左（ceil 向上取整）
    expect(xs.filter(x => x > 0)).toHaveLength(2);
    expect(xs.filter(x => x < 0)).toHaveLength(1);
  });

  it('只有一层的双向布局退化为向右，避免左右不对称', () => {
    const result = layoutTidy(tree(), { direction: 'both' });
    const single = layoutTidy(
      { id: 'root', label: '根', children: [{ id: 'a', label: '甲', children: [] }] },
      { direction: 'both' },
    );
    expect(point(result, 'root').x).toBe(0);
    expect(point(single, 'a').x).toBeGreaterThan(0);
  });

  it('句柄方向跟随布局方向', () => {
    expect(handleSide('a', layoutTidy(tree(), { direction: 'right' }))).toBe('left');
    expect(handleSide('a', layoutTidy(tree(), { direction: 'left' }))).toBe('right');
    expect(handleSide('a', layoutTidy(tree(), { direction: 'down' }))).toBe('top');
    // 双向布局里左侧的节点句柄朝右
    const both = layoutTidy(tree(), { direction: 'both' });
    const leftNode = ['a', 'b', 'c'].find(id => point(both, id).x < 0) as string;
    expect(handleSide(leftNode, both)).toBe('right');
  });

  it('深层子树不会与相邻兄弟的子树重叠', () => {
    const root: MindNode = {
      id: 'root',
      label: '根',
      children: [
        {
          id: 'a',
          label: '甲',
          children: [
            { id: 'a1', label: '甲一', children: [] },
            { id: 'a2', label: '甲二', children: [] },
          ],
        },
        { id: 'b', label: '乙', children: [] },
      ],
    };
    const result = layoutTidy(root, { direction: 'right' });
    const a1 = point(result, 'a1');
    const a2 = point(result, 'a2');
    // 甲的两个孩子纵向间距仍是节点高 + 间隙
    expect(a2.y - a1.y).toBe(NODE_SIZE.height + 20);
    // 乙必须排在甲子树的纵向区间之外
    const b = point(result, 'b');
    expect(b.y).toBeGreaterThan(a2.y);
  });
});
