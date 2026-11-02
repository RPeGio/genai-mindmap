import { mount } from '@vue/test-utils';
import { afterEach, describe, expect, it, vi } from 'vitest';
import App from '../src/App.vue';

/**
 * 构造一个假的 SSE 响应：把若干 JSON 事件包成 `data: ...\n\n` 塞进 ReadableStream。
 * 前端 client 用 reader.read() 逐块消费，所以这里不依赖 jsdom 是否实现 fetch。
 */
function streamReply(frames: unknown[], status = 200): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const frame of frames) {
        controller.enqueue(encoder.encode(`data: ${JSON.stringify(frame)}\n\n`));
      }
      controller.close();
    },
  });
  return { ok: status < 400, status, body, json: async () => ({}) } as unknown as Response;
}

const skeleton = {
  type: 'skeleton',
  content: {
    title: '机器学习',
    root: { id: 'r', label: '机器学习', children: [{ id: 'c', label: '监督学习', children: [] }] },
  },
};

const done = {
  type: 'done',
  mode: 'model',
  content: {
    title: '机器学习',
    root: {
      id: 'r',
      label: '机器学习',
      children: [
        { id: 'c', label: '监督学习', children: [{ id: 'g', label: '梯度下降', children: [] }] },
      ],
    },
  },
  latencyMs: 8600,
  degraded: false,
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('App', () => {
  it('空输入时不发请求，并提示用户', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = mount(App);

    await wrapper.get('button').trigger('click');

    expect(fetchMock).not.toHaveBeenCalled();
    expect(wrapper.get('[role="status"]').text()).toContain('请输入主题或文本');
  });

  it('骨架先到就先渲染，done 到了再补全', async () => {
    const fetchMock = vi.fn(async () => streamReply([skeleton, done]));
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = mount(App);

    await wrapper.get('textarea').setValue('机器学习');
    await wrapper.get('button').trigger('click');

    await vi.waitFor(() => expect(wrapper.findAll('li')).toHaveLength(3));
    // 用的是流式端点，不是老的 /generations
    expect(fetchMock).toHaveBeenCalledWith('/api/generations/stream', expect.anything());
    expect(wrapper.text()).toContain('梯度下降');
    expect(wrapper.text()).toContain('模型生成');
    expect(wrapper.get('[role="status"]').text()).toContain('8.6 秒');
  });

  it('心跳会显示已等待秒数', async () => {
    // 流不关闭：模拟长生成仍在进行，此时进度条与等待秒数应当可见
    const encoder = new TextEncoder();
    vi.stubGlobal('fetch', vi.fn(async () => {
      const body = new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(encoder.encode(`data: ${JSON.stringify(skeleton)}\n\n`));
          controller.enqueue(encoder.encode(`data: ${JSON.stringify({ type: 'heartbeat', waitedMs: 3200 })}\n\n`));
          // 故意不 close
        },
      });
      return { ok: true, status: 200, body, json: async () => ({}) } as unknown as Response;
    }));

    const wrapper = mount(App);
    await wrapper.get('textarea').setValue('机器学习');
    void wrapper.get('button').trigger('click');   // 不 await：流不会结束

    // 心跳只负责显示等待秒数；节点出现的文案由 node 事件驱动。
    await vi.waitFor(() => expect(wrapper.text()).toContain('已等待 3 秒'));
  });

  it('后端返回非 2xx 时展示 message 文案', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: false,
        status: 502,
        body: null,
        json: async () => ({ code: 'http_502', message: '模型服务暂时不可用' }),
      }) as unknown as Response),
    );
    const wrapper = mount(App);

    await wrapper.get('textarea').setValue('x');
    await wrapper.get('button').trigger('click');
    await vi.waitFor(() =>
      expect(wrapper.get('[role="status"]').text()).toContain('模型服务暂时不可用'));
  });

  it('流里出现 error 事件时显示消息且不崩', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        streamReply([{ type: 'error', code: 'generation_failed', message: '模型返回超时' }])),
    );
    const wrapper = mount(App);

    await wrapper.get('textarea').setValue('x');
    await wrapper.get('button').trigger('click');
    await vi.waitFor(() =>
      expect(wrapper.get('[role="status"]').text()).toContain('模型返回超时'));
  });
});
