import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it } from 'vitest';

import type { MindMapContent, MindNode } from '../src/api/types';
import { HISTORY_BYTES_LIMIT, HISTORY_LIMIT, useMindMapStore } from '../src/stores/mindmap';

/** 造一棵够大的树：节点上限提到 1000 之后，单份快照本来就不小。 */
function bigTree(branches: number): MindNode {
  const label = '节点文字'.repeat(20); // 80 字，接近 label 上限的一半
  return {
    id: 'root',
    label: '根节点',
    children: Array.from({ length: branches }, (_, i) => ({
      id: `n${i}`,
      label: `${label}${i}`,
      children: [],
    })),
  };
}

function bigContent(branches: number): MindMapContent {
  return { title: '大图', root: bigTree(branches) };
}

beforeEach(() => {
  setActivePinia(createPinia());
});

describe('撤销栈的字节上限', () => {
  it('步数上限仍然生效', () => {
    const store = useMindMapStore();
    store.preview(bigContent(3), 'right');

    for (let i = 0; i < HISTORY_LIMIT + 10; i += 1) {
      store.commitLabel('n0', `第 ${i} 次改动`);
    }

    expect(store.undoDepth).toBe(HISTORY_LIMIT);
  });

  it('字节超上限时丢最旧的，栈不会一直涨', () => {
    const store = useMindMapStore();
    // 每个节点 80 字中文，快照约 20KB 量级；300 个分支足够在几十步内撑满 10MB
    store.preview(bigContent(300), 'right');
    expect(store.undoBytes).toBe(0);

    for (let i = 0; i < 80; i += 1) {
      store.commitLabel(`n${i % 300}`, `第 ${i} 次改动`);
    }

    expect(store.undoDepth).toBeLessThan(80);
    expect(store.undoBytes).toBeLessThanOrEqual(HISTORY_BYTES_LIMIT);
    // 仍然至少留得下一步
    expect(store.undoDepth).toBeGreaterThan(0);
    expect(store.canUndo).toBe(true);
  });

  it('撤销会同步减掉字节，不会越用越大', () => {
    const store = useMindMapStore();
    store.preview(bigContent(50), 'right');

    for (let i = 0; i < 5; i += 1) {
      store.commitLabel(`n${i}`, `第 ${i} 次改动`);
    }
    const before = store.undoBytes;
    expect(before).toBeGreaterThan(0);

    store.undo();
    expect(store.undoBytes).toBeLessThan(before);
    // 重做把快照压回撤销栈，字节又回来
    store.redo();
    expect(store.undoBytes).toBe(before);
  });

  it('清空历史时字节归零', () => {
    const store = useMindMapStore();
    store.preview(bigContent(10), 'right');
    store.commitLabel('n0', '改一下');
    expect(store.undoBytes).toBeGreaterThan(0);

    store.preview(bigContent(10), 'right');
    expect(store.undoBytes).toBe(0);
    expect(store.undoDepth).toBe(0);
  });
});
