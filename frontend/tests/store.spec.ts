import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it } from 'vitest';

import type { MindNode } from '../src/api/types';
import { displayBody, displayTitle, TRUNCATE_CHARS, useMindMapStore } from '../src/stores/mindmap';

const node = (id: string, label: string): MindNode => ({ id, label, children: [] });

beforeEach(() => {
  setActivePinia(createPinia());
});

describe('节点文字', () => {
  it('超过 24 个字按码点截断并加省略号', () => {
    const long = '一'.repeat(TRUNCATE_CHARS + 1);
    expect(displayTitle({ ...node('a', long), children: [] })).toBe(`${'一'.repeat(TRUNCATE_CHARS)}…`);
  });

  it('不截断时原样返回，emoji 不会被切坏', () => {
    expect(displayTitle(node('a', '短标题'))).toBe('短标题');
    expect(displayTitle(node('a', '😀😀'))).toBe('😀😀');
  });

  it('note 是正文，缺失时回退 label', () => {
    expect(displayBody({ ...node('a', '标题'), note: '这里是一段很长的正文' })).toBe('这里是一段很长的正文');
    expect(displayBody({ ...node('a', '只有标题'), note: '   ' })).toBe('只有标题');
    expect(displayBody(node('a', '只有标题'))).toBe('只有标题');
  });
});

describe('布局与选中', () => {
  /** 直接装一份内容：这些用例验的是画布的派生视图，不需要经过生成通道。 */
  const tree = () => useMindMapStore().preview(
    {
      title: '机器学习',
      root: {
        id: 'r',
        label: '机器学习',
        children: [
          { id: 'a', label: '监督学习', children: [node('a1', '梯度下降')] },
          node('b', '无监督学习'),
        ],
      },
    },
    'right',
  );

  it('切换方向会重新布局并恢复自动适配', () => {
    const store = useMindMapStore();
    tree();

    const rightA = store.nodes.find(item => item.id === 'a');
    store.setDirection('down');
    const downA = store.nodes.find(item => item.id === 'a');

    // 向右时子节点从左侧接入父节点；向下时改为从顶部接入
    expect(rightA?.data?.in).toEqual(['left']);
    expect(downA?.data?.in).toEqual(['top']);
    expect(store.viewAdjusted).toBe(false);
  });

  it('选中节点后给出面包屑路径，点空白清除选中', () => {
    const store = useMindMapStore();
    tree();

    store.select('a1');
    expect(store.breadcrumb.map(item => item.id)).toEqual(['r', 'a', 'a1']);
    expect(store.selectedNode?.node.label).toBe('梯度下降');

    store.select(null);
    expect(store.breadcrumb).toEqual([]);
  });

  it('连线按几何位置选句柄，双向布局的根左右各一个出口', () => {
    // 只看 direction 会把双向布局两侧的线都挂到同一个句柄上，
    // 表现就是连线绕过根节点、从头节点另一侧到子节点。
    const store = useMindMapStore();
    store.preview(
      {
        title: '示例',
        root: {
          id: 'r',
          label: '根',
          children: [node('a', '甲'), node('b', '乙'), node('c', '丙')],
        },
      },
      'both',
    );

    const root = store.nodes.find(item => item.id === 'r');
    expect([...(root?.data?.out ?? [])].sort()).toEqual(['left', 'right']);

    const fromLeft = store.edges.find(edge => edge.sourceHandle === 'out-left');
    const fromRight = store.edges.find(edge => edge.sourceHandle === 'out-right');
    expect(fromLeft?.targetHandle).toBe('in-right');
    expect(fromRight?.targetHandle).toBe('in-left');

    store.setDirection('right');
    expect(
      store.edges.every(
        edge => edge.sourceHandle === 'out-right' && edge.targetHandle === 'in-left',
      ),
    ).toBe(true);
  });
});
