import { mount, type VueWrapper } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import App from '../src/App.vue';
import { startRecording } from '../src/audio/recorder';
import type { MindMapContent, Source } from '../src/api/types';
import { useMindMapStore } from '../src/stores/mindmap';
import { useSourcesStore } from '../src/stores/sources';
import { MAX_INPUT_CHARS, MAX_UPLOAD_BYTES } from '../src/sources/input';

// jsdom 里没有 ResizeObserver，真正的 Vue Flow 画布跑不起来；这里只验证上传入口与提示区。
vi.mock('../src/canvas/MindMapCanvas.vue', () => ({
  default: { name: 'MindMapCanvasStub', template: '<div data-test="canvas" />' },
}));
vi.mock('../src/audio/recorder', async importOriginal => ({
  ...await importOriginal<typeof import('../src/audio/recorder')>(),
  startRecording: vi.fn(),
}));

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

interface Call {
  url: string;
  method: string;
  /** 原样保留：上传那一步是 FormData，String() 会把它变成 "[object FormData]"。 */
  body: unknown;
  headers: unknown;
}

let calls: Call[] = [];

function stubFetch(handler: (call: Call) => Response | Promise<Response>) {
  calls = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string, init?: RequestInit) => {
      const call: Call = {
        url: String(input),
        method: init?.method ?? 'GET',
        body: init?.body,
        headers: init?.headers,
      };
      calls.push(call);
      return handler(call);
    }),
  );
}

/** 后端解析成功后返回的来源，默认是一份 3 页的 PDF。 */
function source(overrides: Partial<Source> = {}): Source {
  return {
    id: 's1',
    kind: 'document',
    filename: '手册.pdf',
    text: '第一章 机器学习\n第二章 深度学习',
    span: { page: 3, start: null, end: null },
    status: 'parsed',
    error: null,
    parsed_pages: 3,
    truncated: false,
    failed_pages: [],
    ...overrides,
  };
}

const generated: MindMapContent = {
  title: '机器学习',
  root: {
    id: 'r',
    label: '机器学习',
    children: [{ id: 'c', label: '监督学习', children: [] }],
  },
};

function smallFile(name = '手册.pdf'): File {
  return new File(['第一章 机器学习'], name, { type: 'application/pdf' });
}

/** 超大文件只伪造 name/size，省掉为了测拦截真的分配 10MB 内存。 */
function hugeFile(name = '大文件.pdf'): File {
  return { name, size: MAX_UPLOAD_BYTES + 1 } as unknown as File;
}

const wrappers: VueWrapper[] = [];

function buttonByText(wrapper: VueWrapper, text: string) {
  const found = wrapper.findAll('button').find(button => button.text().includes(text));
  if (!found) throw new Error(`找不到按钮：${text}`);
  return found;
}

async function generateCollected(wrapper: VueWrapper) {
  await vi.waitFor(() => expect(useSourcesStore().parsing).toBe(false));
  await buttonByText(wrapper, '合并生成导图').trigger('click');
}

function mountApp() {
  const pinia = createPinia();
  setActivePinia(pinia);
  const wrapper = mount(App, { global: { plugins: [pinia] } });
  wrappers.push(wrapper);
  return { wrapper, store: useMindMapStore(), sources: useSourcesStore() };
}

/** jsdom 不允许直接给 input.files 赋值，defineProperty 是塞进 FileList 的唯一办法。 */
async function pickFile(wrapper: VueWrapper, file: File) {
  const input = wrapper.get('input[type="file"]');
  Object.defineProperty(input.element, 'files', { value: [file], configurable: true });
  await input.trigger('change');
}

const UPLOAD_URL = '/api/ingestion/files';

beforeEach(() => {
  setActivePinia(createPinia());
});

afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount());
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('上传文档生成导图', () => {
  it('解析成功仅加入列表，上传使用 multipart', async () => {
    stubFetch(() => jsonResponse({ source: source() }));
    const { wrapper } = mountApp();

    await pickFile(wrapper, smallFile());
    await vi.waitFor(() => expect(useSourcesStore().readySources).toHaveLength(1));

    // 解析成功只是把来源放进列表：用户不点生成，一个生成请求都不该发出去。
    expect(calls).toHaveLength(1);

    // 上传必须是 multipart：不能手动带 JSON 的 Content-Type，否则浏览器不给 boundary，
    // 后端拆不出 file 字段。这里直接断言 body 是带 file 的 FormData。
    const upload = calls[0];
    expect(upload.method).toBe('POST');
    expect(upload.url).toBe(UPLOAD_URL);
    expect(upload.headers).toBeUndefined();
    expect(upload.body).toBeInstanceOf(FormData);
    const form = upload.body as FormData;
    expect(form.has('file')).toBe(true);
    expect((form.get('file') as File).name).toBe('手册.pdf');
  });

  it('超过 10MB 的文件不发请求，直接提示', async () => {
    stubFetch(() => jsonResponse({}));
    const { wrapper } = mountApp();

    await pickFile(wrapper, hugeFile());
    await vi.waitFor(() => expect(wrapper.text()).toContain('超过 10MB 上限'));

    expect(calls).toHaveLength(0);
  });

  it('解析期间按秒计时，用户不会以为卡死', async () => {
    vi.useFakeTimers();
    let release!: (value: Response) => void;
    const pending = new Promise<Response>(resolve => {
      release = resolve;
    });
    stubFetch(() => pending);

    const sources = useSourcesStore();
    const done = sources.addFiles([smallFile()]);

    await vi.advanceTimersByTimeAsync(3000);
    expect(sources.items[0].state).toBe('parsing');
    expect(sources.items[0].seconds).toBe(3);

    release(jsonResponse({ source: source() }));
    await done;
    expect(sources.parsing).toBe(false);
  });

  it('截断时明确提示总页数与已处理页数', async () => {
    stubFetch(() =>
      jsonResponse({
        source: source({
          span: { page: 51, start: null, end: null },
          parsed_pages: 50,
          truncated: true,
        }),
      }),
    );
    const { wrapper } = mountApp();

    await pickFile(wrapper, smallFile());
    await vi.waitFor(() => expect(wrapper.text()).toContain('共 51 页，仅处理前 50 页'));
  });

  it('中间有页识别失败时提示具体页码，而不是谎报"仅处理前 N 页"', async () => {
    // 3 页都处理了，但第 2 页没拿到内容。旧实现会输出
    // "共 3 页，仅处理前 3 页"——自相矛盾，也没说清缺哪一页。
    stubFetch(() =>
      jsonResponse({
        source: source({
          span: { page: 3, start: null, end: null },
          parsed_pages: 2,
          truncated: false,
          failed_pages: [2],
        }),
      }),
    );
    const { wrapper } = mountApp();

    await pickFile(wrapper, smallFile());
    await vi.waitFor(() => expect(wrapper.text()).toContain('第 2 页 识别失败'));
    expect(wrapper.text()).not.toContain('仅处理前');
  });

  it('失败页较多时只列前几个并给出总数', async () => {
    stubFetch(() =>
      jsonResponse({
        source: source({
          span: { page: 10, start: null, end: null },
          parsed_pages: 4,
          truncated: false,
          failed_pages: [2, 3, 5, 6, 7, 9],
        }),
      }),
    );
    const { wrapper } = mountApp();

    await pickFile(wrapper, smallFile());
    await vi.waitFor(() => expect(wrapper.text()).toContain('等 6 页'));
  });

  const details = [
    '解析 .doc / .ppt 需要 LibreOffice 做格式转换。安装方式：apt install -y libreoffice-writer libreoffice-impress',
    '视觉提取不可用（当前是 demo 模式）；这份 PDF 也没有文本层可以退回（可能是扫描件），请确认模型服务可用后重试',
    '文件内容为空',
  ];

  for (const detail of details) {
    it(`422 的 detail 原样展示：${detail.slice(0, 12)}…`, async () => {
      stubFetch(() => jsonResponse({ detail }, 422));
      const { wrapper } = mountApp();

      await pickFile(wrapper, smallFile());
      await vi.waitFor(() => expect(wrapper.get('[role="alert"]').text()).toBe(detail));

      // 只发了上传这一步，解析失败就不该继续跑生成。
      expect(calls).toHaveLength(1);
      expect(wrapper.text()).not.toContain('上传失败');
    });
  }

  it('连不上后端时说清是网络问题', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => {
      throw new TypeError('Failed to fetch');
    }));
    const { wrapper } = mountApp();

    await pickFile(wrapper, smallFile());
    await vi.waitFor(() => expect(wrapper.text()).toContain('无法连接后端'));
  });
});

describe('多来源输入界面', () => {
  it('录音结束仅添加语音来源；录音期间不能提前生成', async () => {
    const audioFile = smallFile('浏览器录音.wav');
    vi.mocked(startRecording).mockResolvedValue({
      elapsedSeconds: () => 1, stop: async () => audioFile, cancel: vi.fn(),
    });
    stubFetch(() => jsonResponse({ source: source({ kind: 'audio', filename: audioFile.name }) }));
    const { wrapper, sources } = mountApp();
    sources.addText('讨论主题');
    await buttonByText(wrapper, '录音添加来源').trigger('click');
    await vi.waitFor(() => expect(buttonByText(wrapper, '停止并添加')).toBeTruthy());
    expect(buttonByText(wrapper, '合并生成导图').attributes('disabled')).toBeDefined();
    await buttonByText(wrapper, '停止并添加').trigger('click');
    await vi.waitFor(() => expect(sources.readySources).toHaveLength(2));
    expect(sources.readySources[1].kind).toBe('audio');
    expect(calls.map(call => call.url)).toEqual([UPLOAD_URL]);
  });

  it('存在失败项时阻止生成，移除后恢复可生成', async () => {
    stubFetch(() => jsonResponse({ detail: '图片识别失败' }, 422));
    const { wrapper, sources } = mountApp();
    sources.addText('有效的文本');
    await pickFile(wrapper, smallFile('失败.png'));
    await vi.waitFor(() => expect(wrapper.text()).toContain('图片识别失败'));
    expect(buttonByText(wrapper, '合并生成导图').attributes('disabled')).toBeDefined();
    await wrapper.get('[aria-label="移除 失败.png"]').trigger('click');
    // 失败项移除后闸门放开：剩下的来源仍然可用（投递形态由 agent 那条链路的用例验）。
    expect(buttonByText(wrapper, '合并生成导图').attributes('disabled')).toBeUndefined();
    expect(sources.readySources).toHaveLength(1);
  });

  it('合并长度包含输入框草稿，超限时禁用生成并保留全部输入', async () => {
    stubFetch(() => jsonResponse({}));
    const { wrapper, sources } = mountApp();
    // 正文留出分隔换行的两个字符，加上输入框草稿正好超出上限一个字
    sources.addText('字'.repeat(MAX_INPUT_CHARS - 2));
    await wrapper.get('#prompt').setValue('补');
    expect(wrapper.text()).toContain(`${MAX_INPUT_CHARS + 1} / ${MAX_INPUT_CHARS} 字`);
    expect(buttonByText(wrapper, '合并生成导图').attributes('disabled')).toBeDefined();
    expect(calls).toHaveLength(0);
    expect(sources.items).toHaveLength(1);
    await wrapper.get('#prompt').setValue('');
    expect(buttonByText(wrapper, '合并生成导图').attributes('disabled')).toBeUndefined();
  });

  it('采集来源不询问是否丢弃导图，只在合并生成前确认', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    stubFetch(() => jsonResponse({ source: source() }));
    const { wrapper, store, sources } = mountApp();
    store.preview(generated, 'right');
    store.commitLabel('c', '未保存的修改');
    await pickFile(wrapper, smallFile());
    await vi.waitFor(() => expect(sources.readySources).toHaveLength(1));
    expect(confirm).not.toHaveBeenCalled();
    await generateCollected(wrapper);
    expect(confirm).toHaveBeenCalledOnce();
    expect(calls).toHaveLength(1);
    expect(store.content?.root.children[0].label).toBe('未保存的修改');
    expect(sources.items).toHaveLength(1);
  });

  it('打开带来源摘要的导图显示本图来源，兼容旧导图', async () => {
    const { wrapper, store } = mountApp();
    store.loadMap({ ...generated, source_usage: [
      { source_id: 'image', kind: 'image', filename: '结构.png', char_count: 100 },
      { source_id: 'text', kind: 'text', filename: null, char_count: 5 },
    ] }, { id: 'saved', version: 1, updated_at: '2026-11-05' });
    await wrapper.vm.$nextTick();
    expect(wrapper.get('aside').text()).toContain('本图来源（2）');
    expect(wrapper.get('aside').text()).toContain('图片 · 结构.png · 100 字');
    expect(wrapper.get('aside').text()).toContain('生成时用到的来源');
    store.loadMap(generated, { id: 'legacy', version: 1, updated_at: '2026-11-05' });
    await wrapper.vm.$nextTick();
    expect(wrapper.get('aside').text()).not.toContain('本图来源');
  });
});
