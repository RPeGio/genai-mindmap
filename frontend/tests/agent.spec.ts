import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { MindMapContent } from '../src/api/types';
import { useMindMapStore } from '../src/stores/mindmap';

/** 把若干事件包成 SSE 帧，模拟后端 /api/agent/runs/stream。 */
function agentReply(frames: unknown[]): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const frame of frames) {
        controller.enqueue(encoder.encode(`data: ${JSON.stringify(frame)}\n\n`));
      }
      controller.enqueue(encoder.encode('data: [DONE]\n\n'));
      controller.close();
    },
  });
  return { ok: true, status: 200, body, json: async () => ({}) } as unknown as Response;
}

const content = (label = '根'): MindMapContent => ({
  title: '示例',
  root: { id: 'root', label, children: [{ id: 'a', label: '甲', children: [] }] },
});

/** 记录每次 fetch 的 URL 与 body，方便断言请求形态。 */
function stubFetch(reply: () => Response) {
  const calls: Array<{ url: string; body: unknown }> = [];
  const mock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ url: String(input), body: init?.body ? JSON.parse(String(init.body)) : null });
    return reply();
  });
  vi.stubGlobal('fetch', mock);
  return calls;
}

const doneFrame = (payload: MindMapContent) => ({ type: 'done', seq: 4, content: payload });

beforeEach(() => {
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('agent 运行', () => {
  it('create 意图不带 snapshot，请求打到 agent 路由', async () => {
    const calls = stubFetch(() => agentReply([
      { type: 'stage', seq: 1, stage: '准备' },
      doneFrame(content()),
    ]));
    const store = useMindMapStore();

    await store.runGeneration('给机器学习做一张导图', 'create');

    expect(calls).toHaveLength(1);
    expect(calls[0].url).toContain('/agent/runs/stream');
    const body = calls[0].body as Record<string, unknown>;
    expect(body.intent).toBe('create');
    // create 带 snapshot 会被后端 422 拒，前端不能顺手传一个
    expect(body.snapshot ?? null).toBeNull();
  });

  it('edit 意图带上当前树', async () => {
    const calls = stubFetch(() => agentReply([doneFrame(content('改后'))]));
    const store = useMindMapStore();
    store.preview(content(), 'right');

    await store.runGeneration('把根改名', 'edit');

    const body = calls[0].body as Record<string, unknown>;
    expect(body.intent).toBe('edit');
    expect((body.snapshot as MindMapContent).root.label).toBe('根');
  });

  it('done 只产生候选，不动画布', async () => {
    stubFetch(() => agentReply([
      { type: 'stage', seq: 1, stage: '准备' },
      { type: 'tool_start', seq: 2, call_id: 'c1', name: 'mindmap_generate' },
      { type: 'tool_end', seq: 3, call_id: 'c1', name: 'mindmap_generate', ok: true, latency_ms: 12 },
      doneFrame(content('新图')),
    ]));
    const store = useMindMapStore();
    store.preview(content('旧图'), 'right');

    await store.runGeneration('做一张导图', 'create');

    expect(store.candidate?.root.label).toBe('新图');
    // 关键约束：候选不自动应用，画布仍是旧内容
    expect(store.content?.root.label).toBe('旧图');
    expect(store.dirty).toBe(false);
    expect(store.agentEvents.map(event => event.type))
      .toEqual(['stage', 'tool_start', 'tool_end', 'done']);
  });

  it('error 事件写进 agentError 且不产生候选', async () => {
    stubFetch(() => agentReply([
      { type: 'error', seq: 1, code: 'generation_failed', message: '模型服务不可用' },
    ]));
    const store = useMindMapStore();

    await store.runGeneration('做一张导图', 'create');

    expect(store.agentError).toBe('模型服务不可用');
    expect(store.candidate).toBeNull();
    expect(store.agentRunning).toBe(false);
  });

  it('空目标不发请求', async () => {
    const calls = stubFetch(() => agentReply([doneFrame(content())]));
    const store = useMindMapStore();

    await store.runGeneration('   ', 'create');

    expect(calls).toHaveLength(0);
    expect(store.status).toContain('请输入');
  });

  it('没有导图时不允许 edit', async () => {
    const calls = stubFetch(() => agentReply([doneFrame(content())]));
    const store = useMindMapStore();

    await store.runGeneration('改一下', 'edit');

    expect(calls).toHaveLength(0);
    expect(store.status).toContain('还没有导图');
  });

  it('流只发 tool_start 就 EOF：报中断，不留「正在与模型协作」', async () => {
    stubFetch(() => agentReply([
      { type: 'tool_start', seq: 1, call_id: 'c1', name: 'mindmap_generate' },
    ]));
    const store = useMindMapStore();

    await store.runGeneration('做一张导图', 'create');

    // 既没有 done 也没有 error，不能当成正常结束
    expect(store.agentError).not.toBe('');
    expect(store.agentError).toContain('中断');
    expect(store.status).toContain('中断');
    expect(store.agentRunning).toBe(false);
    expect(store.busy).toBe(false);
    expect(store.candidate).toBeNull();
  });
});

describe('候选接受', () => {
  async function prepare() {
    stubFetch(() => agentReply([doneFrame(content('候选'))]));
    const store = useMindMapStore();
    store.preview(content('原图'), 'right');
    await store.runGeneration('做一张图', 'create');
    return store;
  }

  it('接受后落到画布，并只记一条撤销', async () => {
    const store = await prepare();
    const before = store.canUndo;

    expect(store.acceptCandidate()).toBe('applied');

    expect(store.content?.root.label).toBe('候选');
    expect(store.candidate).toBeNull();
    expect(store.dirty).toBe(true);
    expect(before).toBe(false);
    expect(store.canUndo).toBe(true);

    store.undo();
    expect(store.content?.root.label).toBe('原图');
  });

  it('候选生成之后改过树，接受要判过时', async () => {
    const store = await prepare();

    // 模拟用户在等待期间改了节点文字
    store.commitLabel('a', '改过的甲');
    expect(store.clientRevision).toBeGreaterThan(0);

    expect(store.acceptCandidate()).toBe('stale');
    // 画布必须保持用户的修改，不能被候选顶掉
    expect(store.content?.root.children[0].label).toBe('改过的甲');
    expect(store.candidate).not.toBeNull();
  });

  it('有未提交的编辑时接受要判过时', async () => {
    const store = await prepare();
    store.beginEdit('a');

    expect(store.acceptCandidate()).toBe('stale');
    expect(store.candidate).not.toBeNull();
  });

  it('没有候选时接受返回 invalid', () => {
    setActivePinia(createPinia());
    expect(useMindMapStore().acceptCandidate()).toBe('invalid');
  });

  it('候选本身不合法时拒绝应用并清掉', async () => {
    // 候选是模型产出的外部数据：label 长度、引用区间、节点数都可能越界。
    // 不在装上画布之前拦一次，脏数据会先入画布，直到用户点保存才被后端 422
    // 拒绝——那时他已经在这棵树上改了半天。
    const dirty = content('脏候选');
    dirty.root.children[0].label = '超'.repeat(201);
    stubFetch(() => agentReply([doneFrame(dirty)]));
    const store = useMindMapStore();
    store.preview(content('原图'), 'right');

    await store.runGeneration('做一张图', 'create');
    expect(store.candidate).not.toBeNull();

    expect(store.acceptCandidate()).toBe('invalid');
    expect(store.candidate).toBeNull();
    expect(store.content?.root.label).toBe('原图');
    expect(store.status).toContain('候选不合法');
  });

  it('丢弃清空候选但不动画布', async () => {
    const store = await prepare();

    store.discardCandidate();

    expect(store.candidate).toBeNull();
    expect(store.content?.root.label).toBe('原图');
  });
});

describe('候选与换图', () => {
  const record = (id: string) => ({ id, version: 1, updated_at: '2024-01-01T00:00:00Z' });

  /** 先在同一张图上产出候选，返回 store，供各用例接上"换图"这一步。 */
  async function prepared() {
    stubFetch(() => agentReply([doneFrame(content('候选'))]));
    const store = useMindMapStore();
    store.loadMap(content('原图'), record('map-a'));
    await store.runGeneration('做一张图', 'create');
    expect(store.candidate?.root.label).toBe('候选');
    return store;
  }

  it('生成候选后 loadMap 打开另一张图，候选被清掉', async () => {
    const store = await prepared();

    store.loadMap(content('另一张'), record('map-b'));

    expect(store.candidate).toBeNull();
    expect(store.content?.root.label).toBe('另一张');
    expect(store.mapId).toBe('map-b');
  });

  it('生成候选后切图，接受要判过时', async () => {
    const store = await prepared();

    // 关键：切图不会递增 clientRevision，只有会话号能区分"这是另一张图"
    const revisionBefore = store.clientRevision;
    store.newMap();
    expect(store.clientRevision).toBe(revisionBefore);

    expect(store.acceptCandidate()).toBe('stale');
    // 画布必须保持切图后的样子，候选不能借道 mapId 写进新记录
    expect(store.content).toBeNull();
    expect(store.mapId).toBeNull();
  });

  it('preview 换图同样作废旧候选', async () => {
    const store = await prepared();

    store.preview(content('示例'), 'right');

    expect(store.candidate).toBeNull();
    expect(store.acceptCandidate()).toBe('stale');
  });
});
