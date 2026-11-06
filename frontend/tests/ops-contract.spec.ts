/**
 * 跨端 op 契约一致性：与 backend/tests/test_ops_contract_sync.py 共读同一份
 * `tests/fixtures/mindmap_ops.json`。
 *
 * 执行器有两份实现，各自演进一定会漂。举一个真实会踩的：move 的同父 index 上界，
 * 一边按"摘除前"算、一边按"摘除后"算，同一个 batch 就会一边成功一边报
 * index_out_of_range，而模型只看到"随机失败"。所以语义断言写在这份共享用例上。
 */
import { describe, expect, it } from 'vitest';

import type { MindMapContent, MindNode } from '../src/api/types';
import { applyOps, OpError } from '../src/mindmap/ops';
import type { MindMapOp } from '../src/mindmap/ops';
// 直接 import JSON：改用 node:fs 会要求 @types/node，而本包 tsconfig 的 types
// 只有 vite/client（浏览器侧视图），为一份测试数据引入 node 类型不划算。
import sharedFixture from '../../tests/fixtures/mindmap_ops.json';

interface Case {
  name: string;
  ops: MindMapOp[];
  expect_tree?: Array<[string, string]>;
  expect_error?: string;
  expect_base_unchanged?: boolean;
}

interface Fixture {
  base_tree: MindMapContent;
  base_revision: number;
  cases: Case[];
}

/** JSON 导入的推断类型是宽泛的，断言成契约声明的形状再由下面的用例真正校验内容。 */
const fixture = sharedFixture as unknown as Fixture;

const base = (): MindMapContent => structuredClone(fixture.base_tree);

/** 前序遍历成 [id, label] 序列，与 fixture 的 expect_tree 同构。 */
function flatten(root: MindNode): Array<[string, string]> {
  const out: Array<[string, string]> = [];
  const walk = (node: MindNode): void => {
    out.push([node.id, node.label]);
    node.children.forEach(walk);
  };
  walk(root);
  return out;
}

describe('跨端 op 契约（共享 fixture）', () => {
  it.each(fixture.cases.map(item => [item.name, item] as const))('%s', (_name, item) => {
    const content = base();
    const snapshot = structuredClone(content);
    const batch = { batch_id: 'sync', base_revision: fixture.base_revision, ops: item.ops };

    let error: OpError | null = null;
    try {
      const result = applyOps(content, batch);
      if (item.expect_error) {
        throw new Error(`预期失败 ${item.expect_error}，实际成功`);
      }
      expect(flatten(result.content.root)).toEqual(item.expect_tree);
    } catch (caught) {
      if (!(caught instanceof OpError)) throw caught;
      error = caught;
    }

    if (item.expect_error) {
      expect(error, `${item.name}：预期抛 ${item.expect_error}`).not.toBeNull();
      expect(error!.code).toBe(item.expect_error);
      if (item.expect_base_unchanged) {
        // 失败之后传入对象必须原样：整批原子不能只靠"调用方别用返回值"。
        expect(content).toEqual(snapshot);
      }
    } else {
      expect(error, `${item.name}：不该失败`).toBeNull();
    }
  });

  it('fixture 自身覆盖成功与失败两类', () => {
    expect(fixture.cases.some(item => item.expect_tree)).toBe(true);
    expect(fixture.cases.some(item => item.expect_error)).toBe(true);
    expect(fixture.cases.some(item => item.expect_base_unchanged)).toBe(true);
  });
});
