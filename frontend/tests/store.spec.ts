import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { MindNode } from '../src/api/types';
import { displayBody, displayTitle, TRUNCATE_CHARS, useMindMapStore } from '../src/stores/mindmap';

/** 把若干 JSON 事件包成 SSE 帧，模拟后端 /api/generations/stream。 */
function streamReply(frames: unknown[]): Response {
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

const node = (id: string, label: string): MindNode => ({ id, label, children: [] });

beforeEach(() => {
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('节点文字', () => {
  it('超过 24 个字按码点截断并加省略号', () => {
    const long = '一'.repeat(TRUNCATE_CHARS + 1);
    expect(displayTitle({ ...node('a', long), children: [] })).toBe(`${'一'.repeat(TRUNCATE_CHARS)}…`);
  });

  it('不截断时原样返回，emoji 不会被切坏', () => {
    expect(displayTitle(node('a', '短标题'))).toBe('短标题');
    expect(displayTitle(node('a', '😀😀'))).toBe('😀😀');
  });

  it('note 是正文，缺失时回退 label', () => {
    expect(displayBody({ ...node('a', '标题'), note: '这里是一段很长的正文' })).toBe('这里是一段很长的正文');
    expect(displayBody({ ...node('a', '只有标题'), note: '   ' })).toBe('只有标题');
    expect(displayBody(node('a', '只有标题'))).toBe('只有标题');
  });
});

describe('生成的取消与失效', () => {
  it('用户主动取消生成：旧图要还回来，不能留下半成品', async () => {
    // cancel() 会把 controller 清空，收尾分支因此认不出"这是我取消的"，
    // 走不到回滚。结果画布上留着半棵树，mapId 为空、dirty 为假，
    // 用户既存不了也不知道它没存过。
    const encoder = new TextEncoder();
    let push!: (frame: unknown) => void;
    let closeStream!: () => void;
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        push = frame => controller.enqueue(encoder.encode(`data: ${JSON.stringify(frame)}\n\n`));
        closeStream = () => controller.close();
      },
    });
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true, status: 200, body, json: async () => ({}),
    }) as unknown as Response));

    const store = useMindMapStore();
    // 先有一份已保存的旧图
    store.loadMap(
      { title: '旧图', root: { id: 'old', label: '旧图', children: [] } },
      { id: 'm1', version: 3, updated_at: '2026-11-04T00:00:00Z' },
    );

    const task = store.generate('新主题');
    push({ type: 'node', parentId: null, node: node('r', '半成品'), index: 0 });
    await vi.waitFor(() => expect(store.content?.root.label).toBe('半成品'));

    // 用户点取消。mock 的流不会因为 abort 自己结束，手动收尾。
    store.cancel(true);
    closeStream();
    await task;

    // 旧图（含身份与版本）回来了
    expect(store.content?.title).toBe('旧图');
    expect(store.mapId).toBe('m1');
    expect(store.version).toBe(3);
    expect(store.busy).toBe(false);
  });

  it('换图引起的失效不回滚：新建后不该冒回旧图', async () => {
    const encoder = new TextEncoder();
    let push!: (frame: unknown) => void;
    let closeStream!: () => void;
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        push = frame => controller.enqueue(encoder.encode(`data: ${JSON.stringify(frame)}\n\n`));
        closeStream = () => controller.close();
      },
    });
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true, status: 200, body, json: async () => ({}),
    }) as unknown as Response));

    const store = useMindMapStore();
    store.preview({ title: '示例', root: { id: 's', label: '示例', children: [] } }, 'right');

    const task = store.generate('新主题');
    push({ type: 'node', parentId: null, node: node('r', '半成品'), index: 0 });
    await vi.waitFor(() => expect(store.content?.root.label).toBe('半成品'));

    // 换图（内部 cancel() 不带 restore）
    store.newMap();
    closeStream();
    await task;

    expect(store.content).toBeNull();
  });
});

describe('生成期间的编辑限制', () => {
  it('生成进行中不接受编辑：done 帧会整树替换，期间的改动静默丢失', async () => {
    // 流式生成边出节点边显示，用户看到就想改。但 done 到达时会用完整结果
    // 整树替换，那些编辑必然被覆盖，撤销栈里还会混进生成中间态。
    const encoder = new TextEncoder();
    let push!: (frame: unknown) => void;
    let close!: () => void;
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        push = frame => controller.enqueue(encoder.encode(`data: ${JSON.stringify(frame)}\n\n`));
        close = () => controller.close();
      },
    });
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true, status: 200, body, json: async () => ({}),
    }) as unknown as Response));

    const store = useMindMapStore();
    const task = store.generate('机器学习');

    // 增量阶段先有一个节点
    push({ type: 'node', parentId: null, node: node('r', '机器学习'), index: 0 });
    push({ type: 'node', parentId: 'r', node: node('a', '监督学习'), index: 1 });
    await vi.waitFor(() => expect(store.nodeCount).toBe(2));

    // 生成还没结束，用户试图改名
    store.commitLabel('a', '我改的名字');
    expect(store.content?.root.children[0].label).toBe('监督学习');
    expect(store.status).toContain('完成之后才能编辑');

    // 撤销同样被挡住
    store.undo();
    expect(store.status).toContain('完成之后才能编辑');

    push({
      type: 'done',
      mode: 'model',
      content: {
        title: '机器学习',
        root: { id: 'r', label: '机器学习', children: [node('a', '监督学习')] },
      },
      latencyMs: 800, degraded: false, reason: '',
    });
    push('[DONE]');
    close();
    await task;

    // 生成结束后恢复正常
    expect(store.busy).toBe(false);
    store.commitLabel('a', '现在可以改了');
    expect(store.content?.root.children[0].label).toBe('现在可以改了');
  });
});

describe('mindmap store', () => {
  it('增量 node 事件按 id 合并进树，重复到达时更新文字并保留子节点', async () => {
    // 手动控制流的推进：只有这样才能在 done 到达之前检查中间的树。
    const encoder = new TextEncoder();
    let push!: (frame: unknown) => void;
    let close!: () => void;
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        push = frame => controller.enqueue(encoder.encode(`data: ${JSON.stringify(frame)}\n\n`));
        close = () => controller.close();
      },
    });
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: true, status: 200, body, json: async () => ({}) }) as unknown as Response),
    );

    const store = useMindMapStore();
    const task = store.generate('机器学习');

    push({ type: 'node', parentId: null, node: node('r', '机器学习'), index: 0 });
    push({ type: 'node', parentId: 'r', node: node('a', '监'), index: 1 });
    push({ type: 'node', parentId: 'r', node: node('a', '监督学习'), index: 1 });
    push({ type: 'node', parentId: 'a', node: node('a1', '梯度下降'), index: 2 });

    // 同一 id 的文字会分几批变长：只能更新那一份，不能长出重复节点
    await vi.waitFor(() => expect(store.nodeCount).toBe(3));
    expect(store.flatNodes.map(entry => entry.node.id)).toEqual(['r', 'a', 'a1']);
    expect(store.flatNodes.find(entry => entry.node.id === 'a')?.node.label).toBe('监督学习');

    push({
      type: 'done',
      mode: 'model',
      content: {
        title: '机器学习',
        root: {
          id: 'r',
          label: '机器学习',
          children: [
            { id: 'a', label: '监督学习', children: [node('a1', '梯度下降')] },
          ],
        },
      },
      latencyMs: 1200,
      degraded: false,
      reason: '',
    });
    push('[DONE]');
    close();
    await task;

    expect(store.mode).toBe('model');
    expect(store.nodeCount).toBe(3);
  });

  it('done 事件决定 mode 与状态文案', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        streamReply([
          {
            type: 'done',
            mode: 'model',
            content: { title: '机器学习', root: node('r', '机器学习') },
            latencyMs: 6100,
            degraded: false,
            reason: '',
          },
        ]),
      ),
    );

    const store = useMindMapStore();
    await store.generate('机器学习');

    expect(store.mode).toBe('model');
    expect(store.busy).toBe(false);
    expect(store.status).toContain('6.1 秒');
    expect(store.layout?.positions.size).toBe(1);
  });

  it('流结束但没有 done 帧时判为中断，不留半截结果', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => streamReply([{ type: 'node', parentId: null, node: node('r', '机器学习'), index: 0 }])),
    );

    const store = useMindMapStore();
    await store.generate('机器学习');

    expect(store.content).toBeNull();
    expect(store.status).toContain('连接中断');
  });

  it('失败时恢复上一份导图，而不是清空', async () => {
    let call = 0;
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        call += 1;
        if (call === 1) {
          return streamReply([
            {
              type: 'done',
              mode: 'demo',
              content: { title: '旧图', root: node('old', '旧图') },
              latencyMs: 120,
              degraded: false,
              reason: '',
            },
          ]);
        }
        return streamReply([{ type: 'error', code: 'generation_failed', message: '模型返回超时' }]);
      }),
    );

    const store = useMindMapStore();
    await store.generate('第一次');
    expect(store.content?.title).toBe('旧图');

    await store.generate('第二次');
    expect(store.content?.title).toBe('旧图');
    expect(store.status).toBe('模型返回超时');
    expect(store.busy).toBe(false);
  });

  it('切换方向会重新布局并恢复自动适配', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        streamReply([
          {
            type: 'done',
            mode: 'model',
            content: {
              title: '机器学习',
              root: {
                id: 'r',
                label: '机器学习',
                children: [node('a', '监督学习'), node('b', '无监督学习')],
              },
            },
            latencyMs: 800,
            degraded: false,
            reason: '',
          },
        ]),
      ),
    );

    const store = useMindMapStore();
    await store.generate('机器学习');

    const rightA = store.nodes.find(item => item.id === 'a');
    store.setDirection('down');
    const downA = store.nodes.find(item => item.id === 'a');

    // 向右时子节点从左侧接入父节点；向下时改为从顶部接入
    expect(rightA?.data?.in).toEqual(['left']);
    expect(downA?.data?.in).toEqual(['top']);
    expect(store.viewAdjusted).toBe(false);
  });

  it('选中节点后给出面包屑路径，点空白清除选中', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        streamReply([
          {
            type: 'done',
            mode: 'model',
            content: {
              title: '机器学习',
              root: {
                id: 'r',
                label: '机器学习',
                children: [
                  {
                    id: 'a',
                    label: '监督学习',
                    children: [node('a1', '梯度下降')],
                  },
                ],
              },
            },
            latencyMs: 900,
            degraded: false,
            reason: '',
          },
        ]),
      ),
    );

    const store = useMindMapStore();
    await store.generate('机器学习');
    store.select('a1');

    expect(store.breadcrumb.map(item => item.id)).toEqual(['r', 'a', 'a1']);
    expect(store.selectedNode?.node.label).toBe('梯度下降');

    store.select(null);
    expect(store.breadcrumb).toEqual([]);
  });

  it('连线按几何位置选句柄，双向布局的根左右各一个出口', () => {
    // 只看 direction 会把双向布局两侧的线都挂到同一个句柄上，
    // 表现就是连线绕过根节点、从头节点另一侧到子节点。
    const store = useMindMapStore();
    store.preview(
      {
        title: '示例',
        root: {
          id: 'r',
          label: '根',
          children: [node('a', '甲'), node('b', '乙'), node('c', '丙')],
        },
      },
      'both',
    );

    const root = store.nodes.find(item => item.id === 'r');
    expect([...(root?.data?.out ?? [])].sort()).toEqual(['left', 'right']);

    const fromLeft = store.edges.find(edge => edge.sourceHandle === 'out-left');
    const fromRight = store.edges.find(edge => edge.sourceHandle === 'out-right');
    expect(fromLeft?.targetHandle).toBe('in-right');
    expect(fromRight?.targetHandle).toBe('in-left');

    store.setDirection('right');
    expect(
      store.edges.every(
        edge => edge.sourceHandle === 'out-right' && edge.targetHandle === 'in-left',
      ),
    ).toBe(true);
  });
});
