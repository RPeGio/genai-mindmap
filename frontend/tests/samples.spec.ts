import { describe, expect, it } from 'vitest';

import type { MindNode } from '../src/api/types';
import { layoutTidy, NODE_SIZE } from '../src/canvas/layout/tidy';
import { SAMPLE_MAPS } from '../src/dev/samples';

function collect(node: MindNode, out: MindNode[] = []): MindNode[] {
  out.push(node);
  for (const child of node.children) collect(child, out);
  return out;
}

// 肉眼验收看的就是这些树，所以先用断言把它们钉住：
// 真实尺寸下如果出现节点框重叠，屏幕上一定看得出来，但那属于布局算法的缺陷，不该靠眼睛发现。
for (const sample of SAMPLE_MAPS) {
  describe(`布局示例「${sample.label}」`, () => {
    const nodes = collect(sample.content.root);
    const ids = nodes.map(node => node.id);
    const result = layoutTidy(sample.content.root, { direction: sample.direction });

    it('id 唯一，且布局覆盖全部节点', () => {
      expect(new Set(ids).size).toBe(ids.length);
      expect(result.positions.size).toBe(nodes.length);
    });

    it('任意两个节点框不重叠', () => {
      const rects = ids.map(id => {
        const point = result.positions.get(id);
        if (!point) throw new Error(`缺少节点 ${id}`);
        return {
          id,
          x1: point.x - NODE_SIZE.width / 2,
          x2: point.x + NODE_SIZE.width / 2,
          y1: point.y - NODE_SIZE.height / 2,
          y2: point.y + NODE_SIZE.height / 2,
        };
      });

      for (let i = 0; i < rects.length; i += 1) {
        for (let j = i + 1; j < rects.length; j += 1) {
          const a = rects[i];
          const b = rects[j];
          const overlap = a.x1 < b.x2 && b.x1 < a.x2 && a.y1 < b.y2 && b.y1 < a.y2;
          expect(overlap, `${a.id} 与 ${b.id} 的节点框重叠`).toBe(false);
        }
      }
    });
  });
}
