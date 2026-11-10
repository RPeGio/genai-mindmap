import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it } from 'vitest';

import type { MindMapContent, MindNode } from '../src/api/types';
import { HISTORY_LIMIT, useMindMapStore } from '../src/stores/mindmap';

const tree = (): MindNode => ({
  id: 'root',
  label: '根',
  children: [
    {
      id: 'a',
      label: '甲',
      children: [{ id: 'a1', label: '甲一', children: [] }],
    },
    { id: 'b', label: '乙', children: [] },
  ],
});

const sample = (): MindMapContent => ({ title: '示例', root: tree() });

const node = (id: string, label: string, children: MindNode[] = []): MindNode => ({
  id,
  label,
  children,
});

beforeEach(() => {
  setActivePinia(createPinia());
});

describe('编辑与撤销', () => {
  it('装载示例后没有历史、也不脏', () => {
    const store = useMindMapStore();
    store.preview(sample(), 'right');

    expect(store.canUndo).toBe(false);
    expect(store.canRedo).toBe(false);
    expect(store.dirty).toBe(false);
  });

  it('改文字后可以撤销、重做', () => {
    const store = useMindMapStore();
    store.preview(sample(), 'right');

    store.commitLabel('a1', '改过的甲一');
    expect(store.content?.root.children[0].children[0].label).toBe('改过的甲一');
    expect(store.dirty).toBe(true);
    expect(store.canUndo).toBe(true);

    store.undo();
    expect(store.content?.root.children[0].children[0].label).toBe('甲一');
    expect(store.canRedo).toBe(true);

    store.redo();
    expect(store.content?.root.children[0].children[0].label).toBe('改过的甲一');
  });

  it('文字没变或为空白时不记历史', () => {
    const store = useMindMapStore();
    store.preview(sample(), 'right');

    store.commitLabel('a1', ' 甲一 ');
    store.commitLabel('a1', '   ');

    expect(store.canUndo).toBe(false);
    expect(store.status).toBe('节点标题不能为空');
  });

  it('撤销栈只保留最近 50 步', () => {
    const store = useMindMapStore();
    store.preview(sample(), 'right');

    for (let i = 1; i <= HISTORY_LIMIT + 10; i += 1) {
      store.commitLabel('a', `第${i}次`);
    }
    for (let i = 0; i < HISTORY_LIMIT; i += 1) store.undo();

    expect(store.canUndo).toBe(false);
    expect(store.content?.root.children[0].label).toBe('第10次');
  });
});

describe('建树与删除', () => {
  it('加子节点后选中并进入编辑', () => {
    const store = useMindMapStore();
    store.preview(sample(), 'right');

    store.addChildTo('a');

    const children = store.content?.root.children[0].children ?? [];
    const added = children[children.length - 1];
    expect(added?.label).toBe('新节点');
    expect(store.selectedId).toBe(added?.id);
    expect(store.editingId).toBe(added?.id);
    expect(store.nodeCount).toBe(5);
  });

  it('加兄弟插在当前节点后面；根节点退化成加子节点', () => {
    const store = useMindMapStore();
    store.preview(sample(), 'right');

    store.addSiblingTo('a1');
    expect(store.content?.root.children[0].children.map(child => child.label)).toEqual([
      '甲一',
      '新节点',
    ]);

    const before = store.nodeCount;
    store.addSiblingTo('root');
    expect(store.nodeCount).toBe(before + 1);
    expect(store.content?.root.children).toHaveLength(3);
  });

  it('编辑中连按 Tab，加的是同一个父节点下的一串兄弟，不是一路下钻', () => {
    const store = useMindMapStore();
    store.preview(sample(), 'right');

    store.select('a');
    store.addChildTo('a');
    const first = store.editingId!;
    store.commitAndAddChild('第一');
    store.commitAndAddChild('第二');

    const children = store.content?.root.children[0].children ?? [];
    expect(children.map(child => child.label)).toEqual(['甲一', '第一', '第二', '新节点']);
    expect(children.map(child => child.id)).toContain(first);
    // 最后一个新节点仍挂在 a 下面，深度没有变。
    expect(store.editingId).toBe(children[children.length - 1]?.id);
  });

  it('Tab 提交被拦下（空标题）时不再接着加节点', () => {
    const store = useMindMapStore();
    store.preview(sample(), 'right');

    store.addChildTo('a');
    const before = store.nodeCount;
    store.commitAndAddChild('   ');

    expect(store.nodeCount).toBe(before);
    expect(store.status).toBe('节点标题不能为空');
  });

  it('别的输入框失焦重提时，不会把正在编辑的新节点关掉', () => {
    const store = useMindMapStore();
    store.preview(sample(), 'right');

    store.addChildTo('root');
    const editing = store.editingId!;
    // 上一个编辑框卸载时浏览器还会补一个 blur，等于把没改过的文字再提交一遍。
    store.commitLabel('a', '甲');

    expect(store.editingId).toBe(editing);
  });

  it('selected 标志跟着 store 的选中走，画布高亮才不会停在旧节点', () => {
    const store = useMindMapStore();
    store.preview(sample(), 'right');

    store.addChildTo('a');
    const added = store.editingId!;

    const selected = store.nodes.filter(item => item.selected === true).map(item => item.id);
    expect(selected).toEqual([added]);
  });

  it('删除节点会删掉子树并选中父节点', () => {
    const store = useMindMapStore();
    store.preview(sample(), 'right');

    store.deleteNode('a');

    expect(store.content?.root.children.map(child => child.id)).toEqual(['b']);
    expect(store.selectedId).toBe('root');
    expect(store.editingId).toBeNull();
  });

  it('根节点不能删除，超 8 层不能再加子节点', () => {
    const store = useMindMapStore();
    store.preview(sample(), 'right');

    store.deleteNode('root');
    expect(store.status).toBe('根节点不能删除');

    let deep = node('d8', '第八层');
    for (let depth = 7; depth >= 1; depth -= 1) deep = node(`d${depth}`, `第${depth}层`, [deep]);
    store.preview({ title: '深树', root: deep }, 'right');

    store.addChildTo('d8');
    expect(store.status).toBe('导图最多 8 层');
    expect(store.nodeCount).toBe(8);
  });
});

describe('保存状态', () => {
  it('打开已保存的导图后不脏、历史清空，模式记为已保存', () => {
    const store = useMindMapStore();
    store.preview(sample(), 'right');
    store.commitLabel('a1', '改过');

    store.loadMap(sample(), { id: 'm1', version: 3, updated_at: '2026-11-04T00:00:00Z' });

    expect(store.dirty).toBe(false);
    expect(store.canUndo).toBe(false);
    expect(store.canRedo).toBe(false);
    expect(store.mapId).toBe('m1');
    expect(store.version).toBe(3);
    expect(store.mode).toBe('saved');
  });

  it('保存成功后清掉脏标记并记住版本号', () => {
    const store = useMindMapStore();
    store.preview(sample(), 'right');
    store.commitLabel('a1', '改过');
    expect(store.dirty).toBe(true);

    store.markSaved({ id: 'm2', version: 1, updated_at: '2026-11-04T00:00:00Z' });

    expect(store.dirty).toBe(false);
    expect(store.mapId).toBe('m2');
    expect(store.version).toBe(1);
  });

  it('标题改名走同一套校验', () => {
    const store = useMindMapStore();
    store.preview(sample(), 'right');

    store.renameTitle('  新标题  ');
    expect(store.content?.title).toBe('新标题');

    store.renameTitle('   ');
    expect(store.status).toBe('标题不能为空');
    expect(store.content?.title).toBe('新标题');
  });
});

describe('节点引用', () => {
  const withRefs = (): MindMapContent => ({
    title: '带引用',
    root: {
      id: 'root',
      label: '根',
      children: [
        {
          id: 'a',
          label: '甲',
          children: [],
          source_refs: [
            { source_id: 's1', page: 1, quote: '矩阵的秩' },
            { source_id: 's1', page: 2 },
          ],
        },
        { id: 'b', label: '乙', children: [] },
      ],
    },
  });

  it('引用条数进画布节点数据，没有引用的节点是 0', () => {
    const store = useMindMapStore();
    store.preview(withRefs(), 'right');

    const counts = new Map(store.nodes.map(node => [node.id, node.data?.refs]));
    expect(counts.get('root')).toBe(0);
    expect(counts.get('a')).toBe(2);
    expect(counts.get('b')).toBe(0);
  });

  it('选中节点后能拿到它的引用，未选中或节点没有引用时是空数组', () => {
    const store = useMindMapStore();
    store.preview(withRefs(), 'right');

    store.select('a');
    expect(store.selectedRefs.map(ref => ref.page ?? 0)).toEqual([1, 2]);

    store.select('b');
    expect(store.selectedRefs).toEqual([]);

    store.select(null);
    expect(store.selectedRefs).toEqual([]);
  });

  it('删掉节点后它的引用一起消失', () => {
    const store = useMindMapStore();
    store.preview(withRefs(), 'right');

    store.select('a');
    store.deleteNode('a');

    expect(store.selectedRefs).toEqual([]);
    expect(store.nodes.some(node => node.id === 'a')).toBe(false);
  });

  it('改文字不动引用：引用说的是来源，不是节点标题', () => {
    const store = useMindMapStore();
    store.preview(withRefs(), 'right');

    store.commitLabel('a', '改过的甲');
    expect(store.selectedRefs.length).toBe(0); // 没选中时为空
    store.select('a');
    expect(store.selectedRefs.length).toBe(2);
  });

  it('改正文记一步历史，并标记为未保存', () => {
    const store = useMindMapStore();
    store.preview(sample(), 'right');

    store.commitNote('a', '甲的正文');
    expect(store.content?.root.children[0].note).toBe('甲的正文');
    expect(store.dirty).toBe(true);

    store.undo();
    expect(store.content?.root.children[0].note).toBeUndefined();
    store.redo();
    expect(store.content?.root.children[0].note).toBe('甲的正文');
  });

  it('正文清空后存成 null，而不是空字符串', () => {
    // 空字符串会被后端当成"有正文"，导出时就多出一行空白。
    const store = useMindMapStore();
    store.preview(sample(), 'right');
    store.commitNote('a', '先写点东西');

    store.commitNote('a', '   ');
    expect(store.content?.root.children[0].note).toBeNull();
  });

  it('正文没变就不记历史：失焦也会触发提交', () => {
    const store = useMindMapStore();
    store.preview(sample(), 'right');
    store.commitNote('a', '甲的正文');
    const depth = store.undoDepth;

    store.commitNote('a', '甲的正文');
    expect(store.undoDepth).toBe(depth);
  });

  it('正文超过 2000 字时拒绝并提示', () => {
    const store = useMindMapStore();
    store.preview(sample(), 'right');

    store.commitNote('a', '字'.repeat(2001));
    expect(store.content?.root.children[0].note).toBeUndefined();
    expect(store.status).toBe('节点正文最多 2000 个字');
  });
});

describe('朝向跟着内容走', () => {
  it('换方向写进 layout，并且可以撤销', () => {
    const store = useMindMapStore();
    store.preview(sample(), 'right');

    store.setDirection('left');
    expect(store.content?.theme?.direction).toBe('left');
    expect(store.direction).toBe('left');

    store.setDirection('down');
    expect(store.content?.layout).toBe('vertical');
    expect(store.direction).toBe('down');

    store.undo();
    expect(store.direction).toBe('left');
  });

  it('打开已保存的导图时恢复朝向', () => {
    const store = useMindMapStore();

    store.loadMap({ ...sample(), layout: 'vertical' }, {
      id: 'm1',
      version: 2,
      updated_at: '2026-11-10T00:00:00Z',
    });
    expect(store.direction).toBe('down');

    store.loadMap({ ...sample(), layout: 'tree', theme: { direction: 'left' } }, {
      id: 'm2',
      version: 1,
      updated_at: '2026-11-10T00:00:00Z',
    });
    expect(store.direction).toBe('left');
  });

  it('没有内容时换方向不做任何事', () => {
    const store = useMindMapStore();
    store.setDirection('down');
    expect(store.content).toBeNull();
    expect(store.direction).toBe('right');
  });
});
