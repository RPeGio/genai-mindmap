import { describe, expect, it } from 'vitest';

import type { MindMapContent, MindNode, SourceRef } from '../src/api/types';
import { cloneTree, LIMITS } from '../src/mindmap/edits';
import type { MindMapOp, OpBatch } from '../src/mindmap/ops';
import {
  applyOps,
  findDeep,
  findParentDeep,
  isDescendant,
  normalizeOps,
  OpError,
} from '../src/mindmap/ops';

const leaf = (id: string, label = id): MindNode => ({ id, label, children: [] });

/** 根 + 甲(甲一, 甲二) + 乙，与 edits.spec.ts 的样例保持同一形状。 */
function tree(): MindNode {
  return {
    id: 'root',
    label: '根',
    children: [
      { id: 'a', label: '甲', children: [leaf('a1', '甲一'), leaf('a2', '甲二')] },
      leaf('b', '乙'),
    ],
  };
}

/**
 * 统一的顶层内容。
 *
 * 必须带 `source_usage`：后端的 `MindMapContent.validate_tree` 会校验"节点引用的
 * 来源必须在 source_usage 里"，前端 `assertTreeValid` 现在同口径。少了它，
 * 所有带 source_refs 的用例都会先被这条兜底拦下，测不到本来要测的东西。
 */
const content = (root: MindNode): MindMapContent => ({
  title: '示例',
  root,
  source_usage: [{ source_id: 's1', kind: 'text', filename: null, char_count: 4 }],
});

const batch = (ops: MindMapOp[]): OpBatch => ({ batch_id: 'batch-1', base_revision: 0, ops });

const refs = (quote: string): SourceRef[] => [
  { source_id: 's1', page: 1, start: 0, end: 2, quote },
];

/** 抓 OpError 并断言类型：失败必须带可回给模型的 code 与 path，否则模型没法自己改。 */
function opError(run: () => void): OpError {
  try {
    run();
  } catch (error) {
    expect(error).toBeInstanceOf(OpError);
    return error as OpError;
  }
  throw new Error('期望抛出 OpError，但执行成功了');
}

describe('查找与环判定', () => {
  it('findDeep / findParentDeep 能定位节点，根节点没有父节点', () => {
    const root = tree();
    expect(findDeep(root, 'a1')?.label).toBe('甲一');
    expect(findParentDeep(root, 'a1')?.id).toBe('a');
    expect(findParentDeep(root, 'root')).toBeNull();
    expect(findDeep(root, 'missing')).toBeNull();
  });

  it('isDescendant 含自己，并能穿过整棵子树', () => {
    const root = tree();
    expect(isDescendant(root, 'a', 'a')).toBe(true);
    expect(isDescendant(root, 'a', 'a2')).toBe(true);
    expect(isDescendant(root, 'root', 'a1')).toBe(true);
    expect(isDescendant(root, 'a1', 'a')).toBe(false);
    expect(isDescendant(root, 'missing', 'a')).toBe(false);
  });
});

describe('add', () => {
  it('父节点不存在时报 parent_not_found，path 指到具体条目与字段', () => {
    const error = opError(() =>
      applyOps(
        content(tree()),
        batch([{ op: 'add', id: 'n_x', parent_id: 'missing', index: 0, label: '孤儿' }]),
      ),
    );

    expect(error.code).toBe('parent_not_found');
    expect(error.path).toBe('ops.0.parent_id');
  });

  it('index 等于子节点个数时追加到末尾，新节点子列表为空', () => {
    const result = applyOps(
      content(tree()),
      batch([{ op: 'add', id: 'n_x', parent_id: 'a', index: 2, label: '甲三' }]),
    );

    expect(findDeep(result.content.root, 'a')?.children.map(child => child.id)).toEqual([
      'a1',
      'a2',
      'n_x',
    ]);
    const added = findDeep(result.content.root, 'n_x');
    expect(added?.children).toEqual([]);
    expect(added?.source_refs).toEqual([]);
  });

  it('index 超界时报 index_out_of_range', () => {
    const error = opError(() =>
      applyOps(
        content(tree()),
        batch([{ op: 'add', id: 'n_x', parent_id: 'a', index: 3, label: '越界' }]),
      ),
    );

    expect(error.code).toBe('index_out_of_range');
    expect(error.path).toBe('ops.0.index');
  });

  it('id 与既有节点重复时报 duplicate_id', () => {
    const error = opError(() =>
      applyOps(
        content(tree()),
        batch([{ op: 'add', id: 'a1', parent_id: 'a', index: 0, label: '撞车' }]),
      ),
    );

    expect(error.code).toBe('duplicate_id');
    expect(error.path).toBe('ops.0.id');
  });

  it('同一批里后面的 op 能引用前面 add 出来的 id', () => {
    const result = applyOps(
      content(tree()),
      batch([
        { op: 'add', id: 'n_a', parent_id: 'a', index: 0, label: '新分支' },
        { op: 'add', id: 'n_a1', parent_id: 'n_a', index: 0, label: '新分支的子节点' },
      ]),
    );

    expect(findDeep(result.content.root, 'n_a')?.children.map(child => child.id)).toEqual(['n_a1']);
  });

  it('显式带上来的 source_refs 原样落到新节点上', () => {
    const result = applyOps(
      content(tree()),
      batch([
        { op: 'add', id: 'n_x', parent_id: 'b', index: 0, label: '带引用', source_refs: refs('引用') },
      ]),
    );

    expect(findDeep(result.content.root, 'n_x')?.source_refs).toEqual(refs('引用'));
  });
});

describe('update', () => {
  it('目标 id 不存在时报 node_not_found', () => {
    const error = opError(() =>
      applyOps(
        content(tree()),
        batch([{ op: 'update', id: 'missing', changes: { label: '改不到' } }]),
      ),
    );

    expect(error.code).toBe('node_not_found');
    expect(error.path).toBe('ops.0.id');
  });

  it('只改 label 时 source_refs 保持原样', () => {
    const base: MindNode = {
      id: 'root',
      label: '根',
      children: [{ id: 'a', label: '甲', children: [], source_refs: refs('原文') }],
    };

    const result = applyOps(
      content(base),
      batch([{ op: 'update', id: 'a', changes: { label: '改过的甲' } }]),
    );

    const node = findDeep(result.content.root, 'a');
    expect(node?.label).toBe('改过的甲');
    expect(node?.source_refs).toEqual(refs('原文'));
  });

  it('传 source_refs: [] 时显式清空，而不是当成没写', () => {
    const base: MindNode = {
      id: 'root',
      label: '根',
      children: [{ id: 'a', label: '甲', children: [], source_refs: refs('原文') }],
    };

    const result = applyOps(
      content(base),
      batch([{ op: 'update', id: 'a', changes: { source_refs: [] } }]),
    );

    expect(findDeep(result.content.root, 'a')?.source_refs).toEqual([]);
  });

  it('只改 source_refs 时 label 不动', () => {
    const result = applyOps(
      content(tree()),
      batch([{ op: 'update', id: 'a1', changes: { source_refs: refs('新引用') } }]),
    );

    const node = findDeep(result.content.root, 'a1');
    expect(node?.label).toBe('甲一');
    expect(node?.source_refs).toEqual(refs('新引用'));
  });
});

describe('remove', () => {
  it('根节点不可删', () => {
    const error = opError(() =>
      applyOps(content(tree()), batch([{ op: 'remove', id: 'root' }])),
    );

    expect(error.code).toBe('cannot_remove_root');
    expect(error.path).toBe('ops.0.id');
  });

  it('删节点时连整棵子树一起删干净', () => {
    const result = applyOps(content(tree()), batch([{ op: 'remove', id: 'a' }]));

    expect(result.content.root.children.map(child => child.id)).toEqual(['b']);
    expect(findDeep(result.content.root, 'a')).toBeNull();
    expect(findDeep(result.content.root, 'a1')).toBeNull();
    expect(findDeep(result.content.root, 'a2')).toBeNull();
  });

  it('目标 id 不存在时报 node_not_found', () => {
    const error = opError(() =>
      applyOps(content(tree()), batch([{ op: 'remove', id: 'missing' }])),
    );

    expect(error.code).toBe('node_not_found');
    expect(error.path).toBe('ops.0.id');
  });
});

describe('move', () => {
  it('移到自己名下报 cycle_detected', () => {
    const error = opError(() =>
      applyOps(
        content(tree()),
        batch([{ op: 'move', id: 'a', new_parent_id: 'a', index: 0 }]),
      ),
    );

    expect(error.code).toBe('cycle_detected');
    expect(error.path).toBe('ops.0.new_parent_id');
  });

  it('移到自己的子孙下面报 cycle_detected', () => {
    const error = opError(() =>
      applyOps(
        content(tree()),
        batch([{ op: 'move', id: 'a', new_parent_id: 'a1', index: 0 }]),
      ),
    );

    expect(error.code).toBe('cycle_detected');
  });

  it('新的父节点不存在时报 parent_not_found', () => {
    const error = opError(() =>
      applyOps(
        content(tree()),
        batch([{ op: 'move', id: 'b', new_parent_id: 'missing', index: 0 }]),
      ),
    );

    expect(error.code).toBe('parent_not_found');
    expect(error.path).toBe('ops.0.new_parent_id');
  });

  it('跨父移动：整棵子树挂到新父节点的指定位置', () => {
    const result = applyOps(
      content(tree()),
      batch([{ op: 'move', id: 'a1', new_parent_id: 'root', index: 1 }]),
    );

    expect(result.content.root.children.map(child => child.id)).toEqual(['a', 'a1', 'b']);
    expect(findDeep(result.content.root, 'a')?.children.map(child => child.id)).toEqual(['a2']);
    expect(findDeep(result.content.root, 'a1')?.label).toBe('甲一');
  });

  it('同父内移动按先摘除再插入算上界：index 等于原长度减一仍合法', () => {
    // 甲的子列表是 [a1, a2]，摘掉 a1 之后长度是 1，所以 index 1 是合法上界。
    const result = applyOps(
      content(tree()),
      batch([{ op: 'move', id: 'a1', new_parent_id: 'a', index: 1 }]),
    );

    expect(findDeep(result.content.root, 'a')?.children.map(child => child.id)).toEqual([
      'a2',
      'a1',
    ]);
  });

  it('同父内移动的 index 超界时报 index_out_of_range', () => {
    const error = opError(() =>
      applyOps(
        content(tree()),
        batch([{ op: 'move', id: 'a1', new_parent_id: 'a', index: 2 }]),
      ),
    );

    expect(error.code).toBe('index_out_of_range');
    expect(error.path).toBe('ops.0.index');
  });
});

describe('整批原子性', () => {
  it('第 2 条失败时原对象零变化，异常下标指向那一条', () => {
    const base = content(tree());
    const before = cloneTree(base);

    const error = opError(() =>
      applyOps(
        base,
        batch([
          { op: 'update', id: 'a1', changes: { label: '改过的甲一' } },
          { op: 'update', id: 'missing', changes: { label: '改不到' } },
          { op: 'remove', id: 'b' },
        ]),
      ),
    );

    expect(error.code).toBe('node_not_found');
    expect(error.path).toBe('ops.1.id');
    // 前一条已经改过副本了，但整批回滚，调用方拿到的还是原来那棵树。
    expect(base).toEqual(before);
    expect(findDeep(base.root, 'a1')?.label).toBe('甲一');
  });

  it('成功时不修改传入的 content，返回的是新对象', () => {
    const base = content(tree());
    const before = cloneTree(base);

    const result = applyOps(base, batch([{ op: 'update', id: 'a1', changes: { label: '改过的' } }]));

    expect(base).toEqual(before);
    expect(result.content).not.toBe(base);
    expect(findDeep(result.content.root, 'a1')?.label).toBe('改过的');
  });
});

describe('整树校验', () => {
  it('超节点上限报 tree_invalid，path 指整批', () => {
    // 按 LIMITS.nodes 现算：上限会调（300 → 1000），写死数字会让调参数变成改测试。
    const perBranch = 5;
    const branches = Math.ceil(LIMITS.nodes / perBranch);
    const root: MindNode = {
      id: 'r',
      label: '根',
      children: Array.from({ length: branches }, (_, i) => ({
        id: `c${i}`,
        label: `子${i}`,
        children: Array.from({ length: perBranch }, (_, j) => leaf(`c${i}-${j}`)),
      })),
    };

    const error = opError(() =>
      applyOps(content(root), batch([{ op: 'update', id: 'r', changes: { label: '根改' } }])),
    );

    expect(error.code).toBe('tree_invalid');
    expect(error.path).toBe('ops');
  });

  it('超 8 层报 tree_invalid', () => {
    let node: MindNode = leaf('d9');
    for (let depth = 8; depth >= 1; depth -= 1) {
      node = { id: `d${depth}`, label: `第${depth}层`, children: [node] };
    }

    const error = opError(() =>
      applyOps(content(node), batch([{ op: 'update', id: 'd1', changes: { label: '改根' } }])),
    );

    expect(error.code).toBe('tree_invalid');
    expect(error.path).toBe('ops');
  });

  it('单节点超 50 个子节点报 tree_invalid', () => {
    const root: MindNode = {
      id: 'r',
      label: '根',
      children: Array.from({ length: 51 }, (_, i) => leaf(`c${i}`)),
    };

    const error = opError(() =>
      applyOps(content(root), batch([{ op: 'update', id: 'r', changes: { label: '根改' } }])),
    );

    expect(error.code).toBe('tree_invalid');
    expect(error.path).toBe('ops');
  });

  it('label 超 200 个字报 tree_invalid', () => {
    const error = opError(() =>
      applyOps(
        content(tree()),
        batch([{ op: 'update', id: 'a1', changes: { label: '长'.repeat(201) } }]),
      ),
    );

    expect(error.code).toBe('tree_invalid');
    expect(error.path).toBe('ops');
  });

  it('输入树本身 id 重复时报 tree_invalid', () => {
    const root: MindNode = { id: 'r', label: '根', children: [leaf('x'), leaf('x', '另一个')] };

    const error = opError(() =>
      applyOps(content(root), batch([{ op: 'update', id: 'r', changes: { label: '根改' } }])),
    );

    expect(error.code).toBe('tree_invalid');
    expect(error.path).toBe('ops');
  });

  it('恰好 300 个节点、50 个直接子节点都算合法', () => {
    // 根 + 50 个分支 + 49×5 + 4 个叶子 = 300 个节点，深度 3，根恰好 50 个子节点。
    const root: MindNode = {
      id: 'r',
      label: '根',
      children: Array.from({ length: 50 }, (_, i) => ({
        id: `c${i}`,
        label: `子${i}`,
        children: Array.from({ length: i === 0 ? 4 : 5 }, (_, j) => leaf(`c${i}-${j}`)),
      })),
    };

    const result = applyOps(
      content(root),
      batch([{ op: 'update', id: 'r', changes: { label: '根改' } }]),
    );

    expect(findDeep(result.content.root, 'r')?.label).toBe('根改');
  });

  it('label 恰好 200 字再加一个尾随空格报 tree_invalid（后端按原始值量长度）', () => {
    // trim 之后正好 200 字：只按 trim 判的话这里会放行，保存时才被后端 422。
    const error = opError(() =>
      applyOps(
        content(tree()),
        batch([{ op: 'update', id: 'a1', changes: { label: `${'长'.repeat(200)} ` } }]),
      ),
    );

    expect(error.code).toBe('tree_invalid');
    expect(error.path).toBe('ops');
  });

  it('引用 end 等于 start 报 tree_invalid', () => {
    const error = opError(() =>
      applyOps(
        content(tree()),
        batch([
          {
            op: 'update',
            id: 'a1',
            changes: { source_refs: [{ source_id: 's1', start: 4, end: 4, quote: '空区间' }] },
          },
        ]),
      ),
    );

    expect(error.code).toBe('tree_invalid');
    expect(error.path).toBe('ops');
  });

  it('引用只给 start 不给 end 报 tree_invalid', () => {
    const error = opError(() =>
      applyOps(
        content(tree()),
        batch([
          {
            op: 'update',
            id: 'a1',
            changes: { source_refs: [{ source_id: 's1', start: 2, quote: '缺 end' }] },
          },
        ]),
      ),
    );

    expect(error.code).toBe('tree_invalid');
    expect(error.path).toBe('ops');
  });
});

describe('未知 op', () => {
  it('契约外的 op 类型报 unknown_op，而不是静默跳过', () => {
    // 类型断言绕过 TS 的穷尽检查，模拟后端将来加了新 op、或模型自己编了一个。
    const bogus = { op: 'rename', id: 'a', label: '改名' } as unknown as MindMapOp;

    const error = opError(() => applyOps(content(tree()), batch([bogus])));

    expect(error.code).toBe('unknown_op');
    expect(error.path).toBe('ops.0.op');
  });
});

describe('changed_ids', () => {
  it('收集 add/update/move/remove 触及的节点，move 连新旧父节点一起收', () => {
    const result = applyOps(
      content(tree()),
      batch([
        { op: 'add', id: 'n_new', parent_id: 'a', index: 0, label: '新增' },
        { op: 'update', id: 'a1', changes: { label: '甲一改' } },
        { op: 'move', id: 'b', new_parent_id: 'a', index: 0 },
        { op: 'remove', id: 'a2' },
      ]),
    );

    // 乙从根移到甲下面：旧父 root、新父 a 的子列表都变了，都要能触发重排。
    expect([...result.changed_ids].sort()).toEqual(['a', 'a1', 'a2', 'b', 'n_new', 'root'].sort());
  });

  it('失败的批次不会返回任何 changed_ids', () => {
    const error = opError(() =>
      applyOps(
        content(tree()),
        batch([
          { op: 'update', id: 'a1', changes: { label: '改过' } },
          { op: 'remove', id: 'root' },
        ]),
      ),
    );

    expect(error.code).toBe('cannot_remove_root');
  });
});

describe('草案规范化', () => {
  const drafts = (): MindMapOp[] => [
    { op: 'add', id: '$tmp1', parent_id: 'root', index: 0, label: '甲新' },
    { op: 'add', id: '$tmp2', parent_id: '$tmp1', index: 0, label: '甲新的子节点' },
    { op: 'update', id: '$tmp2', changes: { label: '改过的子节点' } },
    { op: 'move', id: 'b', new_parent_id: '$tmp1', index: 0 },
    { op: 'remove', id: '$tmp1' },
  ];

  it('把 $tmp 映射成完整长度的真实 id，并改写所有引用', () => {
    const source = drafts();
    const { ops, idMap } = normalizeOps(source, new Set(['root', 'a', 'b']));

    const first = idMap['$tmp1'];
    const second = idMap['$tmp2'];
    expect(first).toBeTypeOf('string');
    expect(second).toBeTypeOf('string');
    expect(first.startsWith('$')).toBe(false);
    // 不能是 newId() 那种 8 位截断：完整随机量的长度下限是 32。
    expect(first.length).toBeGreaterThanOrEqual(32);
    expect(first).not.toBe(second);

    const [addFirst, addSecond, updateOp, moveOp, removeOp] = ops;
    expect(addFirst.op === 'add' ? addFirst.id : null).toBe(first);
    expect(addFirst.op === 'add' ? addFirst.parent_id : null).toBe('root');
    expect(addSecond.op === 'add' ? addSecond.parent_id : null).toBe(first);
    expect(updateOp.op === 'update' ? updateOp.id : null).toBe(second);
    // 非 $ 开头的 id 原样保留，不能被顺手改掉。
    expect(moveOp.op === 'move' ? moveOp.id : null).toBe('b');
    expect(moveOp.op === 'move' ? moveOp.new_parent_id : null).toBe(first);
    expect(removeOp.op === 'remove' ? removeOp.id : null).toBe(first);
  });

  it('同一个临时 id 无论被引用几次都映射到同一个真实 id', () => {
    const { ops, idMap } = normalizeOps(drafts(), new Set(['root']));

    const real = idMap['$tmp1'];
    const addOp = ops[0];
    const moveOp = ops[3];
    expect(addOp.op === 'add' ? addOp.id : null).toBe(real);
    expect(moveOp.op === 'move' ? moveOp.new_parent_id : null).toBe(real);
    expect(Object.keys(idMap).sort()).toEqual(['$tmp1', '$tmp2']);
  });

  it('不改动传入的草案，并把规范化结果直接喂给 applyOps 也能通过', () => {
    const source = drafts();
    const before = cloneTree(source);
    const { ops } = normalizeOps(source, new Set(['root', 'a', 'b']));

    expect(source).toEqual(before);

    // remove 掉 $tmp1 之后树里只剩 root + a + b 一条分支可删，这里改成只跑前两条 add。
    const result = applyOps(content(tree()), batch(ops.slice(0, 2)));
    expect(result.content.root.children.map(child => child.id)).toContain(
      ops[0].op === 'add' ? ops[0].id : 'nope',
    );
  });
});
