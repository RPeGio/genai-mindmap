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

    // 向右时子节点在父节点左侧接入；向下时改为顶部
    expect(rightA?.data?.side).toBe('left');
    expect(downA?.data?.side).toBe('top');
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
});
