import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { MapRecord, MindMapContent, MindNode } from '../src/api/types';
import { diffEdits, EDIT_EVENT_LIMIT } from '../src/mindmap/edit-log';
import { useMindMapStore } from '../src/stores/mindmap';

const AT = '2026-11-17T09:31:02.123Z';

function node(id: string, label: string, children: MindNode[] = []): MindNode {
  return { id, label, children };
}

function content(root: MindNode, extra: Partial<MindMapContent> = {}): MindMapContent {
  return { title: '示例', root, ...extra };
}

/** 两层的小树：root → a(→ b)。改动都从这棵树上做，事件数一眼数得清。 */
function tree(): MindMapContent {
  return content(node('root', '主题', [node('a', '甲', [node('b', '乙')])]));
}

// ---------------------------------------------------------------- 纯函数

describe('diffEdits：改了什么', () => {
  it('改标题是一条 label 事件，带层数', () => {
    const events = diffEdits(tree(), content(node('root', '主题', [node('a', '新标题', [node('b', '乙')])])), {
      actor: 'user', at: AT,
    });

    expect(events).toEqual([{
      actor: 'user', at: AT, kind: 'update', field: 'label',
      nodeId: 'a', before: '甲', after: '新标题', depth: 2,
    }]);
  });

  it('改颜色是 style 事件，不是 update', () => {
    const after = content(node('root', '主题', [
      { ...node('a', '甲', [node('b', '乙')]), color: 'red' },
    ]));

    expect(diffEdits(tree(), after, { actor: 'user', at: AT })).toEqual([{
      actor: 'user', at: AT, kind: 'style', field: 'color',
      nodeId: 'a', before: '无', after: 'red', depth: 2,
    }]);
  });

  it('加一个节点是一条 add', () => {
    const after = content(node('root', '主题', [node('a', '甲', [node('b', '乙'), node('c', '丙')])]));

    expect(diffEdits(tree(), after, { actor: 'user', at: AT })).toEqual([{
      actor: 'user', at: AT, kind: 'add', nodeId: 'c', after: '丙', depth: 3,
    }]);
  });

  it('删一棵子树：每个节点一条 remove，各自带删除前的层数', () => {
    const before = content(node('root', '主题', [node('a', '甲', [node('b', '乙', [node('c', '丙')])])]));

    const events = diffEdits(before, content(node('root', '主题', [])), { actor: 'user', at: AT });

    expect(events.map(event => [event.kind, event.nodeId, event.depth])).toEqual([
      ['remove', 'a', 2],
      ['remove', 'b', 3],
      ['remove', 'c', 4],
    ]);
    expect(events.every(event => event.before && event.after === undefined)).toBe(true);
  });

  it('换父是 move，前后是两边的父标题', () => {
    const before = content(node('root', '主题', [node('a', '甲'), node('x', 'X')]));
    const after = content(node('root', '主题', [node('x', 'X', [node('a', '甲')])]));

    const moves = diffEdits(before, after, { actor: 'user', at: AT })
      .filter(event => event.kind === 'move');

    expect(moves).toEqual([{
      actor: 'user', at: AT, kind: 'move', nodeId: 'a',
      before: '主题', after: 'X', depth: 3,
    }]);
  });

  it('同父换序也算 move（这一层成员没变）', () => {
    const before = content(node('root', '主题', [node('a', '甲'), node('b', '乙')]));
    const after = content(node('root', '主题', [node('b', '乙'), node('a', '甲')]));

    const moves = diffEdits(before, after, { actor: 'user', at: AT });

    // 遍历顺序跟着新树走（新树里 b 在前），所以 b 的事件先出现。
    expect(moves.map(event => [event.nodeId, event.before, event.after])).toEqual([
      ['b', '第2位', '第1位'],
      ['a', '第1位', '第2位'],
    ]);
  });

  it('插入一个兄弟不会给其它兄弟记 move', () => {
    // 这是"顺序变化算不算改动"那条判断的验收点：按 index 直接比，插一个节点会让
    // 后面所有兄弟都 +1，1 次操作变成 N 条 move，`by_kind` 立刻失真。
    const before = content(node('root', '主题', [node('a', '甲'), node('b', '乙')]));
    const after = content(node('root', '主题', [node('a', '甲'), node('c', '丙'), node('b', '乙')]));

    const events = diffEdits(before, after, { actor: 'user', at: AT });

    expect(events.map(event => event.kind)).toEqual(['add']);
    expect(events[0].nodeId).toBe('c');
  });

  it('加一条附加边是一条 edge 事件', () => {
    const after = content(node('root', '主题', [node('a', '甲', [node('b', '乙')])]), {
      edges: [{ id: 'e_1', source: 'b', target: 'root', label: '是' }],
    });

    expect(diffEdits(tree(), after, { actor: 'user', at: AT })).toEqual([{
      actor: 'user', at: AT, kind: 'edge', nodeId: 'b', after: '乙 → 主题（是）',
    }]);
  });

  it('一次操作改了两个字段就是两条事件', () => {
    // 合并成一条会让 by_field 把一次改动算进两个桶，`sum(by_field)` 就对不上
    // `by_kind.update + by_kind.style`——统计一旦不能自校验，读的人得先怀疑数字。
    const after = content(node('root', '主题', [
      { ...node('a', '新标题', [node('b', '乙')]), color: 'amber' },
    ]));

    const events = diffEdits(tree(), after, { actor: 'user', at: AT });

    expect(events.map(event => `${event.kind}:${event.field}`)).toEqual([
      'update:label', 'style:color',
    ]);
    expect(new Set(events.map(event => event.at)).size).toBe(1);
  });

  it('detail 只存长度，不存全文', () => {
    const before = content(node('root', '主题', [{
      ...node('a', '甲'), detail: '长'.repeat(1800),
    }]));
    const after = content(node('root', '主题', [{
      ...node('a', '甲'), detail: '短'.repeat(700),
    }]));

    const events = diffEdits(before, after, { actor: 'user', at: AT });

    expect(events).toHaveLength(1);
    expect(events[0].before).toBe('1800字');
    expect(events[0].after).toBe('700字');
    // 2000 字的正文进日志会把这张表撑成第二个 content，长度本身就是情报。
    expect(events[0].before!.length).toBeLessThan(10);
  });

  it('导图标题的改动记成 field=title，不混进 label', () => {
    const after = content(node('root', '主题', [node('a', '甲', [node('b', '乙')])]), {
      title: '新标题',
    });

    expect(diffEdits(tree(), after, { actor: 'user', at: AT })).toEqual([{
      actor: 'user', at: AT, kind: 'update', field: 'title', before: '示例', after: '新标题',
    }]);
  });

  it('空画布 → 一整棵树不产生事件', () => {
    // create 意图接受候选：整棵树都是"新增"，那不是 diff，是把整棵树重念一遍。
    expect(diffEdits(null, tree(), { actor: 'agent', at: AT })).toEqual([]);
  });

  it('runId 只在给出时出现', () => {
    const after = content(node('root', '主题', [node('a', '新标题', [node('b', '乙')])]));
    const events = diffEdits(tree(), after, { actor: 'agent', at: AT, runId: 'run-1' });
    expect(events[0].runId).toBe('run-1');
    expect(diffEdits(tree(), after, { actor: 'user', at: AT })[0]).not.toHaveProperty('runId');
  });
});

// ---------------------------------------------------------------- store 集成

interface Call {
  url: string;
  method: string;
  body: Record<string, unknown>;
}

const RECORD: MapRecord = {
  id: 'm1', version: 2, content: tree(), updated_at: '2026-11-17T09:31:02.123Z',
};

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

let calls: Call[] = [];

function stubFetch(handler: (call: Call) => Response | Promise<Response>) {
  calls = [];
  vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
    const call: Call = {
      url: String(input),
      method: init?.method ?? 'GET',
      body: init?.body ? JSON.parse(String(init.body)) : {},
    };
    calls.push(call);
    return handler(call);
  }));
}

/** 只发 `done` 帧再 `[DONE]`：够把候选放进 store，不用真跑一轮 agent。 */
function streamWithDone(next: MindMapContent): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode(
        `data: ${JSON.stringify({ type: 'done', seq: 1, content: next, mode: 'model' })}\n\n`,
      ));
      controller.enqueue(encoder.encode('data: [DONE]\n\n'));
      controller.close();
    },
  });
  return { ok: true, status: 200, body, json: async () => ({}) } as unknown as Response;
}

beforeEach(() => {
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('store：事件攒到保存时提交', () => {
  it('手工编辑记成 user', () => {
    const store = useMindMapStore();
    store.preview(tree(), 'right');

    store.commitLabel('a', '新标题');

    expect(store.pendingEdits).toHaveLength(1);
    expect(store.pendingEdits[0]).toMatchObject({
      actor: 'user', kind: 'update', field: 'label', nodeId: 'a',
      before: '甲', after: '新标题',
    });
  });

  it('应用候选记成 agent，并带上那次 run 的 id', async () => {
    const store = useMindMapStore();
    store.preview(tree(), 'right');
    stubFetch(() => streamWithDone(content(node('root', '主题', [node('a', '改过的甲', [node('b', '乙')])]))));

    await store.runGeneration('把甲改个名', 'edit');
    expect(store.acceptCandidate()).toBe('applied');

    expect(store.pendingEdits.length).toBeGreaterThan(0);
    expect(store.pendingEdits.every(event => event.actor === 'agent')).toBe(true);
    expect(store.pendingEdits[0].runId).toMatch(/^run-/);
    expect(store.pendingEdits[0].after).toBe('改过的甲');
  });

  it('create 意图接受候选不产生事件（整棵树都是新的）', async () => {
    const store = useMindMapStore();
    stubFetch(() => streamWithDone(tree()));

    await store.runGeneration('做一张图', 'create');
    store.acceptCandidate();

    expect(store.pendingEdits).toEqual([]);
  });

  it('保存成功后清空', async () => {
    const store = useMindMapStore();
    store.preview(tree(), 'right');
    store.commitLabel('a', '新标题');
    stubFetch(() => jsonResponse(RECORD));

    expect(await store.save()).toBe('saved');

    expect(store.pendingEdits).toEqual([]);
    const post = calls.find(call => call.method === 'POST')!;
    // 事件跟内容在同一次请求里：第一次保存前没有 map_id，独立端点根本发不出去。
    expect((post.body.events as unknown[]).length).toBe(1);
    expect(post.body.truncated).toBeUndefined();
  });

  it('保存失败不清空（否则这批事件就丢了）', async () => {
    const store = useMindMapStore();
    store.preview(tree(), 'right');
    store.commitLabel('a', '新标题');
    stubFetch(() => jsonResponse({ message: '炸了' }, 500));

    expect(await store.save()).toBe('failed');
    expect(store.pendingEdits).toHaveLength(1);

    // 再存一次就能把它们补上。
    stubFetch(() => jsonResponse(RECORD));
    expect(await store.save()).toBe('saved');
    expect(store.pendingEdits).toEqual([]);
  });

  it('保存期间的新改动留给下一次，不被一起清掉', async () => {
    const store = useMindMapStore();
    store.preview(tree(), 'right');
    store.commitLabel('a', '第一次改动');

    let release: () => void = () => {};
    const gate = new Promise<void>(resolve => { release = resolve; });
    stubFetch(async () => {
      await gate;
      return jsonResponse(RECORD);
    });

    const saving = store.save();
    await vi.waitFor(() => expect(calls).toHaveLength(1));
    store.commitLabel('a', '保存期间的改动');
    release();
    expect(await saving).toBe('saved');

    expect(store.pendingEdits).toHaveLength(1);
    expect(store.pendingEdits[0].after).toBe('保存期间的改动');
  });

  it('单次保存最多 500 条，剩下的留到下一次', async () => {
    const store = useMindMapStore();
    const kids = Array.from({ length: 520 }, (_, index) => node(`k${index}`, `节点${index}`));
    store.preview(content(node('root', '主题', [node('big', '大块', kids)])), 'right');

    store.deleteNode('big');
    expect(store.pendingEdits).toHaveLength(521);

    stubFetch(() => jsonResponse(RECORD));
    await store.save();

    const post = calls.find(call => call.method === 'POST')!;
    expect((post.body.events as unknown[]).length).toBe(EDIT_EVENT_LIMIT);
    // 超出的 21 条不是被丢掉，而是留在队列里等下一次保存——截断是分批，不是丢弃。
    // 它们也不会进表：`total` 必须是真实改动数，插一条"我截断了"的假事件会让每个
    // 统计口径都变脏。
    expect(post.body.truncated).toBe(21);
    expect(store.pendingEdits).toHaveLength(21);

    await store.save();
    expect((calls[calls.length - 1].body.events as unknown[]).length).toBe(21);
    expect(store.pendingEdits).toHaveLength(0);
  });

  it('撤销与重做不产生事件', () => {
    const store = useMindMapStore();
    store.preview(tree(), 'right');
    store.commitLabel('a', '新标题');
    const before = store.pendingEdits.length;

    store.undo();
    store.redo();

    // 一次被撤销的编辑不是用户的最终意图：同时记"18→9"和"9→18"会让平均值
    // 被拉回中性、次数却翻倍，比不记更糟。
    expect(store.pendingEdits).toHaveLength(before);
  });

  it('换图把上一张图的事件作废，不会串到新记录上', () => {
    const store = useMindMapStore();
    store.preview(tree(), 'right');
    store.commitLabel('a', '新标题');
    expect(store.pendingEdits).toHaveLength(1);

    store.preview(tree(), 'right');

    expect(store.pendingEdits).toEqual([]);
  });

  it('朝向变化不产生事件', () => {
    const store = useMindMapStore();
    store.preview(tree(), 'right');

    store.setDirection('left');

    // 朝向写在 content 里，但它回答不了"生成侧该改什么"，记下来只是噪音。
    expect(store.pendingEdits).toEqual([]);
  });
});
