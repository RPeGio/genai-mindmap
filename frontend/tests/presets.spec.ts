/**
 * 预置演示导图的守卫测试。
 *
 * 演示数据最容易犯的错不是"不好看"，而是**不合法**：重复 id、超过契约上限、
 * 少写一个 children 字段——这些问题在页面上表现为"点载入没反应"或"保存时 422"，
 * 而它们只会在演示现场被发现。这里用提交前校验与布局器把三张图跑一遍。
 */
import { describe, expect, it } from 'vitest';

import { DEMO_PRESETS } from '../src/demo/presets';
import { layoutTidy } from '../src/canvas/layout/tidy';
import { countNodes, validateDraft } from '../src/mindmap/edits';
import type { MindNode } from '../src/api/types';

function walk(root: MindNode, visit: (node: MindNode, depth: number) => void, depth = 1): void {
  visit(root, depth);
  for (const child of root.children) walk(child, visit, depth + 1);
}

describe('预置演示导图', () => {
  it.each(DEMO_PRESETS.map(preset => [preset.label, preset] as const))('%s 通过提交前校验', (_label, preset) => {
    expect(validateDraft(preset.content)).toBeNull();
  });

  it.each(DEMO_PRESETS.map(preset => [preset.label, preset] as const))('%s 的节点 id 唯一、布局覆盖全部节点', (_label, preset) => {
    const ids = new Set<string>();
    walk(preset.content.root, node => {
      expect(ids.has(node.id)).toBe(false);
      ids.add(node.id);
    });
    expect(ids.size).toBe(countNodes(preset.content.root));

    const layout = layoutTidy(preset.content.root, { direction: preset.direction });
    expect(layout.positions.size).toBe(ids.size);
  });

  it.each(DEMO_PRESETS.map(preset => [preset.label, preset] as const))('%s 的正文与备注都在契约上限内', (_label, preset) => {
    walk(preset.content.root, node => {
      expect(Array.from(node.label).length).toBeLessThanOrEqual(200);
      expect(Array.from(node.detail ?? '').length).toBeLessThanOrEqual(2000);
      expect(Array.from(node.note ?? '').length).toBeLessThanOrEqual(2000);
    });
  });

  it('三张图主题不同，且至少一张用双向布局', () => {
    const titles = DEMO_PRESETS.map(preset => preset.content.title);
    expect(new Set(titles).size).toBe(DEMO_PRESETS.length);
    expect(DEMO_PRESETS.some(preset => preset.direction === 'both')).toBe(true);
  });
});
