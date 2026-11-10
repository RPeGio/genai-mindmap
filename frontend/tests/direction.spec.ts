import { describe, expect, it } from 'vitest';

import type { MindMapContent } from '../src/api/types';
import { directionOf, withDirection } from '../src/mindmap/direction';

const base = (): MindMapContent => ({
  title: '示例',
  root: { id: 'root', label: '根', children: [] },
});

describe('directionOf', () => {
  it('没有内容时按向右处理：控件要有确定值', () => {
    expect(directionOf(null)).toBe('right');
    expect(directionOf(undefined)).toBe('right');
  });

  it('旧记录没有 layout 与 theme 时读成向右', () => {
    expect(directionOf(base())).toBe('right');
  });

  it('横向布局读 theme.direction', () => {
    expect(directionOf({ ...base(), layout: 'tree', theme: { direction: 'left' } })).toBe('left');
    expect(directionOf({ ...base(), layout: 'tree', theme: { direction: 'both' } })).toBe('both');
  });

  it('纵向布局优先于 theme.direction', () => {
    // 纵向时 direction 不参与排版，存着 left 也不该读成"向左"。
    expect(directionOf({ ...base(), layout: 'vertical', theme: { direction: 'left' } })).toBe('down');
  });
});

describe('withDirection', () => {
  it('写朝向时补上 schema_version，旧记录保存一次就升级', () => {
    const next = withDirection(base(), 'right');
    expect(next.schema_version).toBe(2);
    expect(next.layout).toBe('tree');
    expect(next.theme?.direction).toBe('right');
  });

  it('向下写成 vertical，同时把 direction 归到 right', () => {
    const next = withDirection({ ...base(), theme: { direction: 'left' } }, 'down');
    expect(next.layout).toBe('vertical');
    expect(next.theme?.direction).toBe('right');
  });

  it('不改入参：撤销栈拿的是旧快照', () => {
    const content = base();
    const next = withDirection(content, 'left');
    expect(content.layout).toBeUndefined();
    expect(content.theme).toBeUndefined();
    expect(next).not.toBe(content);
  });

  it('保留 theme 的其它字段', () => {
    const next = withDirection({ ...base(), theme: { preset: 'forest', font: 'serif' } }, 'both');
    expect(next.theme).toEqual({ preset: 'forest', font: 'serif', direction: 'both' });
  });

  it('不把 logic / timeline / manual 降级成 tree', () => {
    // 界面只认四种朝向，但别人（或将来）存下的布局类型不能被一次换方向抹掉。
    const timeline = withDirection({ ...base(), layout: 'timeline' }, 'left');
    expect(timeline.layout).toBe('timeline');
    const manual = withDirection({ ...base(), layout: 'manual' }, 'right');
    expect(manual.layout).toBe('manual');
  });

  it('往返一致：写进去再读出来还是同一个朝向', () => {
    for (const direction of ['right', 'left', 'down', 'both'] as const) {
      expect(directionOf(withDirection(base(), direction))).toBe(direction);
    }
  });
});
