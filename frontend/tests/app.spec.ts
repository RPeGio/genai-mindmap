import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import { createPinia } from 'pinia';
import { afterEach, describe, expect, it, vi } from 'vitest';

import App from '../src/App.vue';
import { useMindMapStore } from '../src/stores/mindmap';
import { useSourcesStore } from '../src/stores/sources';

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

describe('节点引用', () => {
  const withRefs = (sourceRefs: { source_id: string; page?: number; start?: number; end?: number; quote?: string }[]) => ({
    title: '带引用',
    root: {
      id: 'r',
      label: '根',
      children: [{ id: 'a', label: '甲', children: [], source_refs: sourceRefs }],
    },
  });

  it('选中带引用的节点时，详情面板列出它的引用', async () => {
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(withRefs([{ source_id: 's1', page: 2, quote: '矩阵的秩' }]), 'right');
    store.select('a');
    await wrapper.vm.$nextTick();

    expect(wrapper.text()).toContain('来源引用（1）');
    expect(wrapper.text()).toContain('第 2 页');
    expect(wrapper.text()).toContain('矩阵的秩');
  });

  it('没有引用的节点明确说明没有，而不是留一片空白', async () => {
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(withRefs([]), 'right');
    store.select('r');
    await wrapper.vm.$nextTick();

    expect(wrapper.text()).toContain('这个节点没有来源引用');
  });

  it('点引用打开右侧抽屉；手边没有原文时说明只能看摘录', async () => {
    const wrapper = mountApp();
    const store = useMindMapStore();
    store.preview(withRefs([{ source_id: 's1', page: 2, quote: '矩阵的秩' }]), 'right');
    store.select('a');
    await wrapper.vm.$nextTick();

    await buttonByText(wrapper, '矩阵的秩').trigger('click');
    const drawer = wrapper.find('[aria-labelledby="ref-title"]');
    expect(drawer.exists()).toBe(true);
    expect(drawer.text()).toContain('矩阵的秩');
    expect(drawer.text()).toContain('原文没有随导图保存');
  });

  it('本次会话解析过的来源能在抽屉里高亮命中句', async () => {
    const wrapper = mountApp();
    const sources = useSourcesStore();
    sources.addText('矩阵的秩等于行向量组的极大线性无关组的个数。');
    const sourceId = sources.readySources[0].id;

    const store = useMindMapStore();
    store.preview(withRefs([{ source_id: sourceId, start: 0, end: 6, quote: '矩阵的秩等于' }]), 'right');
    store.select('a');
    await wrapper.vm.$nextTick();

    await buttonByText(wrapper, '矩阵的秩等于').trigger('click');
    const drawer = wrapper.find('[aria-labelledby="ref-title"]');
    expect(drawer.find('mark').text()).toBe('矩阵的秩等于');
    expect(drawer.text()).not.toContain('原文没有随导图保存');
  });
});

describe('导出提示', () => {
  const CONTENT = {
    title: '季度规划',
    root: { id: 'r', label: '产品', children: [] },
  };

  /** 真实导出会建 blob 链接并点一次 <a>，jsdom 没有这两样。 */
  function stubDownload() {
    Object.defineProperty(URL, 'createObjectURL', { value: () => 'blob:stub', configurable: true });
    Object.defineProperty(URL, 'revokeObjectURL', { value: () => {}, configurable: true });
  }

  async function pickFormat(wrapper: VueWrapper, format: string) {
    await wrapper.find('select[title="导出为文本格式"]').setValue(format);
    // onExport 是 async：请求落地、浮层渲染都要等一轮微任务。
    await flushPromises();
  }

  it('后端拒绝时底部浮层给出后端文案，而不是只有左侧小字', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 422,
      json: async () => ({ code: 'validation_error', message: '请求参数校验失败' }),
    }));
    const wrapper = mountApp();
    useMindMapStore().preview(CONTENT, 'right');
    await wrapper.vm.$nextTick();

    await pickFormat(wrapper, 'markdown');

    const alert = wrapper.find('[role="alert"]');
    expect(alert.exists()).toBe(true);
    expect(alert.text()).toContain('请求参数校验失败');
  });

  it('导出成功时给出成功浮层', async () => {
    stubDownload();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      text: async () => '@startmindmap\n@endmindmap\n',
    }));
    const wrapper = mountApp();
    useMindMapStore().preview(CONTENT, 'right');
    await wrapper.vm.$nextTick();

    await pickFormat(wrapper, 'puml');

    const status = wrapper.find('[role="status"]');
    expect(status.exists()).toBe(true);
    expect(wrapper.text()).toContain('已导出 PlantUML');
  });
});