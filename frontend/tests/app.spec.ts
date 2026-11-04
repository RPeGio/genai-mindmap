import { mount, type VueWrapper } from '@vue/test-utils';
import { createPinia } from 'pinia';
import { afterEach, describe, expect, it, vi } from 'vitest';

import App from '../src/App.vue';

// jsdom 里没有 ResizeObserver，真正的 Vue Flow 画布跑不起来；
// 界面测试只关心输入区与详情面板，画布替换成占位元素。
vi.mock('../src/canvas/MindMapCanvas.vue', () => ({
  default: { name: 'MindMapCanvasStub', template: '<div data-test="canvas" />' },
}));

/** 构造一个假的 SSE 响应：把若干 JSON 事件包成 `data: ...\n\n` 塞进 ReadableStream。 */
function streamReply(frames: unknown[], status = 200): Response {
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
  return { ok: status < 400, status, body, json: async () => ({}) } as unknown as Response;
}

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
  reason: '',
};

function mountApp() {
  return mount(App, { global: { plugins: [createPinia()] } });
}

/**
 * 按文字找按钮：开发期示例选择器会异步插入一排按钮，
 * 用 `findAll('button')[0]` 会随加载时机拿到不同的东西。
 */
function buttonByText(wrapper: VueWrapper, text: string) {
  const found = wrapper.findAll('button').find(item => item.text().includes(text));
  if (!found) throw new Error(`找不到按钮：${text}`);
  return found;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('App', () => {
  it('空输入时不发请求，并提示用户', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = mountApp();

    await buttonByText(wrapper, '生成导图').trigger('click');

    expect(fetchMock).not.toHaveBeenCalled();
    expect(wrapper.get('[role="status"]').text()).toContain('请输入主题或文本');
  });

  it('生成完成后显示节点数、模式与耗时', async () => {
    const fetchMock = vi.fn(async () => streamReply([done]));
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = mountApp();

    await wrapper.get('textarea').setValue('机器学习');
    await buttonByText(wrapper, '生成导图').trigger('click');

    await vi.waitFor(() => expect(wrapper.text()).toContain('3 个节点'));
    // 用的是流式端点
    expect(fetchMock).toHaveBeenCalledWith('/api/generations/stream', expect.anything());
    expect(wrapper.text()).toContain('模型生成');
    expect(wrapper.get('[role="status"]').text()).toContain('8.6 秒');
  });

  it('没有选中节点时详情面板给出说明', () => {
    const wrapper = mountApp();
    expect(wrapper.text()).toContain('点击画布上的任意节点');
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
    const wrapper = mountApp();

    await wrapper.get('textarea').setValue('x');
    await buttonByText(wrapper, '生成导图').trigger('click');
    await vi.waitFor(() =>
      expect(wrapper.get('[role="status"]').text()).toContain('模型服务暂时不可用'));
  });

  it('流里出现 error 事件时显示消息且不崩', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        streamReply([{ type: 'error', code: 'generation_failed', message: '模型返回超时' }])),
    );
    const wrapper = mountApp();

    await wrapper.get('textarea').setValue('x');
    await buttonByText(wrapper, '生成导图').trigger('click');
    await vi.waitFor(() =>
      expect(wrapper.get('[role="status"]').text()).toContain('模型返回超时'));
  });

  it('开发期示例按钮不经过后端就能装载导图', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = mountApp();

    await vi.waitFor(() => expect(buttonByText(wrapper, '单节点')).toBeTruthy());
    await buttonByText(wrapper, '单节点').trigger('click');

    expect(fetchMock).not.toHaveBeenCalled();
    expect(wrapper.text()).toContain('1 个节点');
    expect(wrapper.text()).toContain('演示模式');
  });

  it('点保存把当前导图发给后端，成功后显示版本号', async () => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 201,
      json: async () => ({
        id: 'm1',
        version: 1,
        content: { title: '单节点', root: { id: 's', label: '单节点', children: [] } },
        updated_at: '2026-11-04T00:00:00Z',
      }),
    }) as unknown as Response);
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = mountApp();

    await vi.waitFor(() => expect(buttonByText(wrapper, '单节点')).toBeTruthy());
    await buttonByText(wrapper, '单节点').trigger('click');
    await buttonByText(wrapper, '保存').trigger('click');

    await vi.waitFor(() =>
      expect(wrapper.get('[role="status"]').text()).toContain('已保存，版本 1'));
    expect(fetchMock).toHaveBeenCalledWith('/api/mindmaps', expect.objectContaining({ method: 'POST' }));
  });

  it('Ctrl+S 保存前让正在输入的控件失焦，草稿才会被提交', async () => {
    // 点「保存」按钮时浏览器已经让输入框失焦了，问题只出在键盘快捷键：
    // Ctrl+S 不会触发 blur，草稿留在输入框里，保存发出去的是旧内容，
    // 界面却显示"已保存"。
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true, status: 201,
      json: async () => ({
        id: 'm1', version: 1,
        content: { title: '单节点', root: { id: 's', label: '单节点', children: [] } },
        updated_at: '2026-11-04T00:00:00Z',
      }),
    })) as unknown as typeof fetch);
    const wrapper = mountApp();

    await vi.waitFor(() => expect(buttonByText(wrapper, '单节点')).toBeTruthy());
    await buttonByText(wrapper, '单节点').trigger('click');

    const titleInput = wrapper.get('#title');
    (titleInput.element as HTMLInputElement).focus();
    // jsdom 的 activeElement 需要显式设置：focus() 在无头环境里不一定生效
    Object.defineProperty(document, 'activeElement', {
      value: titleInput.element, configurable: true,
    });
    const blurSpy = vi.spyOn(titleInput.element as HTMLInputElement, 'blur');

    window.dispatchEvent(new KeyboardEvent('keydown', {
      key: 's', ctrlKey: true, bubbles: true, cancelable: true,
    }));

    expect(blurSpy).toHaveBeenCalled();
  });

  it('打开列表列出已保存的导图，点开后进入该导图', async () => {
    const record = {
      id: 'm9',
      version: 2,
      content: {
        title: '打开这张',
        root: {
          id: 'r',
          label: '打开这张',
          children: [{ id: 'a', label: '甲', children: [] }],
        },
      },
      updated_at: '2026-11-04T03:00:00Z',
    };
    const fetchMock = vi.fn(async (url: string) => ({
      ok: true,
      status: 200,
      json: async () => (url === '/api/mindmaps' ? [record] : record),
    }) as unknown as Response);
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = mountApp();

    await buttonByText(wrapper, '打开').trigger('click');
    await vi.waitFor(() => expect(wrapper.text()).toContain('打开这张'));

    await wrapper.get('ul li button').trigger('click');
    await vi.waitFor(() =>
      expect(wrapper.get('[role="status"]').text()).toContain('已打开导图，版本 2'));
    expect(wrapper.text()).toContain('2 个节点');
  });
});
