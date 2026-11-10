/**
 * agent 增量帧的回归测试。
 *
 * 两件事在这一步才第一次有了消费者：
 *   - `ops` 帧：`edit` 意图的改动**只**走它，不接就是"打开旧图 → 让 AI 改 →
 *     界面没反应"；
 *   - `save()` 的生成期早退：生成中途保存会 POST 出一条半截记录。
 */
import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { MindMapContent, OpBatch } from '../src/api/types';
import { useMindMapStore } from '../src/stores/mindmap';

const content = (label = '根'): MindMapContent => ({
  title: '示例',
  root: { id: 'root', label, children: [{ id: 'a', label: '甲', children: [] }] },
});

/** 一批只改某个节点标题的 op：`edit` 意图下最典型的那一帧。 */
function opsFrame(baseRevision: number, label: string, seq = 1) {
  return {
    type: 'ops',
    seq,
    call_id: 'c1',
    batch: {
      batch_id: `b${baseRevision}`,
      base_revision: baseRevision,
      ops: [{ op: 'update', id: 'a', changes: { label } }],
    } satisfies OpBatch,
  };
}

const doneFrame = (payload: MindMapContent) => ({ type: 'done', seq: 9, content: payload });

/**
 * 手动控制节奏的 SSE 响应。
 *
 * 状态行只在流进行中才有值：一次性把帧全灌完，收尾会立刻把它清掉，
 * 断言不到"已应用 N 条修改"这句话。
 */
function controllableReply() {
  const encoder = new TextEncoder();
  let push: (frame: unknown) => void = () => {};
  let finish: () => void = () => {};
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      push = (frame) => { controller.enqueue(encoder.encode(`data: ${JSON.stringify(frame)}\n\n`)); };
      finish = () => {
        controller.enqueue(encoder.encode('data: [DONE]\n\n'));
        controller.close();
      };
    },
  });
  const response = { ok: true, status: 200, body, json: async () => ({}) } as unknown as Response;
  return { response, push, finish };
}

/** 记录每次请求的 URL：验证"没发保存请求"靠它。 */
function stubFetch(reply: () => Response) {
  const urls: string[] = [];
  const mock = vi.fn(async (input: RequestInfo | URL) => {
    urls.push(String(input));
    return reply();
  });
  vi.stubGlobal('fetch', mock);
  return urls;
}

beforeEach(() => {
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('ops 帧进候选', () => {
  it('合法批次改的是候选，画布不动', async () => {
    const gate = controllableReply();
    stubFetch(() => gate.response);
    const store = useMindMapStore();
    store.preview(content(), 'right');

    const running = store.runGeneration('把甲改个名字', 'edit');
    gate.push(opsFrame(0, '改后的甲'));

    await vi.waitFor(() => expect(store.candidate?.root.children[0].label).toBe('改后的甲'));
    // 关键约束：增量帧只进候选，采纳仍然要用户点「应用」
    expect(store.content?.root.children[0].label).toBe('甲');
    expect(store.dirty).toBe(false);
    // 有东西在动，用户才相信这次运行真的在改
    expect(store.status).toContain('已应用 1 条修改');

    gate.push(doneFrame(content('改后的甲')));
    gate.finish();
    await running;
  });

  it('base_revision 对不上的批次静默丢弃，候选保持原样', async () => {
    const gate = controllableReply();
    stubFetch(() => gate.response);
    const store = useMindMapStore();
    store.preview(content(), 'right');

    const running = store.runGeneration('把甲改个名字', 'edit');
    gate.push(opsFrame(0, '第一次改'));
    await vi.waitFor(() => expect(store.candidate?.root.children[0].label).toBe('第一次改'));

    // 中间漏了一批（丢帧 / 乱序）：这一批的依据不是当前这棵树，套上去只会错位
    gate.push(opsFrame(5, '错位的一批', 2));
    // 等这一帧被消费：进了 agentEvents 就说明 switch 已经走过一遍
    await vi.waitFor(() => expect(store.agentEvents).toHaveLength(2));

    // 静默丢弃：候选不变、不抛错、状态行也不改成报错
    expect(store.candidate?.root.children[0].label).toBe('第一次改');
    expect(store.status).not.toContain('失败');

    // 丢弃不会让这一轮跑崩，后续的 done 照常收尾
    gate.push(doneFrame(content('最终结果')));
    gate.finish();
    await expect(running).resolves.toBeUndefined();
    expect(store.candidate?.root.label).toBe('最终结果');
  });
});

describe('生成期保存', () => {
  it('busy 时 save() 不发请求，并给出可操作的提示', async () => {
    const gate = controllableReply();
    const urls = stubFetch(() => gate.response);
    const store = useMindMapStore();
    store.preview(content(), 'right');

    const running = store.runGeneration('把甲改个名字', 'edit');
    await vi.waitFor(() => expect(store.agentRunning).toBe(true));
    expect(store.busy).toBe(true);

    // 生成中途以为能存：POST 出去的是一条半截记录，done 到达后再存又多一条
    await expect(store.save()).resolves.toBe('busy');
    expect(store.status).toContain('完成之后才能保存');
    // 只发出了那一个 agent 请求，保存请求一个都没有
    expect(urls.filter(url => url.includes('/mindmaps'))).toHaveLength(0);

    gate.finish();
    await running;
  });
});
