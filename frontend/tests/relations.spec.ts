import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it } from 'vitest';

import type { FishboneContent, FlowContent, MindMapContent, MindNode } from '../src/api/types';
import { dropDanglingEdges, pruneDanglingRefs, validateDraft } from '../src/mindmap/edits';
import { applyOps } from '../src/mindmap/ops';
import { useMindMapStore } from '../src/stores/mindmap';

/**
 * 删节点连带删掉的**跨节点关系**（`relations`）。
 *
 * 这一组用例钉的是"同一个概念在两处只有一处被记住"：`edges` 与 `relations` 是两套
 * 字段、**同一条 dangling 判据**（契约 `_validate_relations` / `_validate_edges` 对
 * 指向不存在节点的那一条都是硬拒），而删节点的清理此前只写了 `edges` 那一半。
 *
 * 后果不是"少删了几行数据"，是**用户改了半天、点保存 422**：错的是某条 `relations`，
 * 而界面根本不渲染它——用户既看不到那条残留关系，也没有任何办法删掉它。
 *
 * 三种形状的顶层都有 `relations`（契约 `ContentBase`），所以树、流程、鱼骨各有一条。
 */

const node = (id: string, label: string, children: MindNode[] = []): MindNode => ({
  id, label, children,
});

const tree = (): MindMapContent => ({
  title: '带关系的树',
  root: node('root', '根', [node('a', '甲', [node('a1', '甲一')]), node('b', '乙')]),
  relations: [
    { id: 'r1', source: 'a', target: 'b', type: 'depends' },
    // 端点在**子孙**上：级联删除会把它一起摘掉，只清被点名那一个的话这条会悬空
    { id: 'r2', source: 'a1', target: 'root', type: 'ref' },
    // 完全不命中，必须留下
    { id: 'r3', source: 'root', target: 'b', type: 'cause' },
  ],
});

const flow = (): FlowContent => ({
  title: '报销流程',
  diagram_type: 'flowchart',
  stages: [
    {
      id: 's_1',
      root: node('n1', '审不审', [node('n11', '填表'), node('n12', '补材料')]),
      edges: [
        { id: 'e_i1', source: 'n1', target: 'n12', label: '否', kind: 'branch' },
      ],
    },
    { id: 's_2', root: node('n2', '付款') },
  ],
  edges: [{ id: 'e_1', source: 's_1', target: 's_2', label: '通过', kind: 'flow' }],
  relations: [
    { id: 'r1', source: 'n12', target: 'n2', type: 'ref' },
    { id: 'r2', source: 'n11', target: 'n2', type: 'cause' },
  ],
});

const fishbone = (): FishboneContent => ({
  title: '为什么报销要 11 天',
  diagram_type: 'fishbone',
  problem: '为什么报销要 11 天',
  bones: [
    { category: '人', root: node('b1', '人', [node('b11', '只有一个人审')]) },
    { category: '法', root: node('b2', '法') },
  ],
  relations: [
    { id: 'r1', source: 'b11', target: 'b2', type: 'cause' },
    { id: 'r2', source: 'b1', target: 'b2', type: 'ref' },
  ],
});

beforeEach(() => {
  setActivePinia(createPinia());
});

describe('删节点：relations 与 edges 一起清', () => {
  it('树：被子树任意节点引用的关系都消失，不命中的那条留下', () => {
    const next = dropDanglingEdges(tree(), new Set(['a', 'a1']));
    expect((next as MindMapContent).relations!.map(relation => relation.id)).toEqual(['r3']);
  });

  it('一条都不命中时返回**同一个对象**（调用方靠引用相等判断"什么都没变"）', () => {
    const content = tree();
    expect(dropDanglingEdges(content, new Set(['ghost']))).toBe(content);
  });

  it('流程：阶段内边与 relations 一起清，阶段之间的边一个字节不动', () => {
    const next = dropDanglingEdges(flow(), new Set(['n12'])) as FlowContent;
    expect(next.stages[0]!.edges!.map(edge => edge.id)).toEqual([]);
    expect(next.edges!.map(edge => edge.id)).toEqual(['e_1']);
    expect(next.relations!.map(relation => relation.id)).toEqual(['r2']);
  });

  it('鱼骨：没有 edges，但 relations 一样要清', () => {
    const next = dropDanglingEdges(fishbone(), new Set(['b11'])) as FishboneContent;
    expect(next.relations!.map(relation => relation.id)).toEqual(['r2']);
    expect(next.bones.map(bone => bone.root.id)).toEqual(['b1', 'b2']);
  });
});

describe('校验：悬空的 relations 拦在保存之前', () => {
  it('指向不存在的节点 → 给出人话，而不是等后端 422 的 Pydantic 错误树', () => {
    const content = tree();
    content.relations = [{ id: 'r9', source: 'a', target: 'ghost', type: 'ref' }];
    expect(validateDraft(content)).toBe('关系 r9 指向不存在的节点 ghost');
  });

  it('删完之后内容通过校验（保存这条路不再 422）', () => {
    expect(validateDraft(dropDanglingEdges(tree(), new Set(['a', 'a1'])))).toBeNull();
    expect(validateDraft(dropDanglingEdges(flow(), new Set(['n12'])))).toBeNull();
    expect(validateDraft(dropDanglingEdges(fishbone(), new Set(['b11'])))).toBeNull();
  });

  it('合法的 relations 不受影响', () => {
    expect(validateDraft(tree())).toBeNull();
  });
});

describe('store.deleteNode：删完就能存', () => {
  it('删掉被关系引用的节点，内容里那条关系消失且校验通过', () => {
    const store = useMindMapStore();
    store.preview(tree(), 'right');
    // 前提：原样保存是通过的（悬空关系还没出现）
    expect(validateDraft(store.content!)).toBeNull();

    store.deleteNode('a');
    const content = store.content as MindMapContent;
    expect(content.relations!.map(relation => relation.id)).toEqual(['r3']);
    expect(validateDraft(store.content!)).toBeNull();
  });
});

/**
 * `ops.applyOps`（模型发来的 op 批次在前端预演那条路）是删节点的**另一份实现**，
 * 与后端 `ops._apply_remove` / `tools._prune_stage_edges` 逐条对齐。
 *
 * 它此前也只清 `content.edges`：同一个 op 批次在"前端预演"里整批失败
 * （`assertContentValid` 拦住悬空关系 / 悬空阶段内边），在后端执行器里却能成功。
 */
describe('op 批次：删节点也清悬空引用', () => {
  const removeBatch = (id: string) => ({
    batch_id: 'b1', base_revision: 1, ops: [{ op: 'remove' as const, id }],
  });

  it('树：删掉被引用节点，那批 op 不再被悬空关系打回', () => {
    const result = applyOps(tree(), removeBatch('a'));
    expect((result.content as MindMapContent).relations!.map(item => item.id)).toEqual(['r3']);
  });

  it('鱼骨：relations 属于整图，写回之后那一步也要清', () => {
    const result = applyOps(fishbone(), removeBatch('b11'));
    expect((result.content as FishboneContent).relations!.map(item => item.id)).toEqual(['r2']);
  });

  it('流程：阶段内边与 relations 一起清', () => {
    const result = applyOps(flow(), removeBatch('n12'));
    const content = result.content as FlowContent;
    expect(content.stages[0]!.edges).toEqual([]);
    expect(content.relations!.map(item => item.id)).toEqual(['r2']);
  });

  it('一个也不命中时返回同一个对象', () => {
    const content = tree();
    expect(pruneDanglingRefs(content)).toBe(content);
  });
});
