import { mount, type VueWrapper } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import App from '../src/App.vue';
import type { MindMapContent, PipelineResponse, Source } from '../src/api/types';
import { MAX_UPLOAD_BYTES, useMindMapStore } from '../src/stores/mindmap';

// jsdom 里没有 ResizeObserver，真正的 Vue Flow 画布跑不起来；这里只验证上传入口与提示区。
vi.mock('../src/canvas/MindMapCanvas.vue', () => ({
  default: { name: 'MindMapCanvasStub', template: '<div data-test="canvas" />' },
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

function pipelineReply(overrides: Partial<PipelineResponse> = {}): PipelineResponse {
  return { mode: 'demo', content: generated, diagnostics: [], stages: [], ...overrides };
}

function smallFile(name = '手册.pdf'): File {
  return new File(['第一章 机器学习'], name, { type: 'application/pdf' });
}

/** 超大文件只伪造 name/size，省掉为了测拦截真的分配 10MB 内存。 */
function hugeFile(name = '大文件.pdf'): File {
  return { name, size: MAX_UPLOAD_BYTES + 1 } as unknown as File;
}

function mountApp() {
  const pinia = createPinia();
  setActivePinia(pinia);
  const wrapper = mount(App, { global: { plugins: [pinia] } });
  return { wrapper, store: useMindMapStore() };
}

/** jsdom 不允许直接给 input.files 赋值，defineProperty 是塞进 FileList 的唯一办法。 */
async function pickFile(wrapper: VueWrapper, file: File) {
  const input = wrapper.get('input[type="file"]');
  Object.defineProperty(input.element, 'files', { value: [file], configurable: true });
  await input.trigger('change');
}

const UPLOAD_URL = '/api/ingestion/files';
const GENERATE_URL = '/api/pipeline/generate';

beforeEach(() => {
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('上传期间用户改动不会被静默覆盖', () => {
  /**
   * 让生成请求挂起，返回放行函数。
   *
   * 直接调 store.uploadAndGenerate 而不 mount App：这里验证的是覆盖判定逻辑，
   * 走 UI 会引入 Vue Test Utils 的异步时序干扰（trigger 不等 async handler 结束），
   * 反而不稳定。界面层的 confirmDiscard 由其他用例覆盖。
   */
  function stubGeneratePending() {
    let release: (() => void) | null = null;
    stubFetch(async call => {
      if (call.url === UPLOAD_URL) return jsonResponse({ source: source() });
      await new Promise<void>(resolve => { release = resolve; });
      return jsonResponse(pipelineReply());
    });
    return {
      async waitUntilPending() {
        await vi.waitFor(() => expect(release).not.toBeNull());
      },
      release() {
        release?.();
      },
    };
  }

  it('等待期间编辑过节点：保留用户改动，不落生成结果', async () => {
    const { store } = mountApp();
    store.preview(generated, 'right');
    store.commitLabel('c', '我改过的字');

    const gate = stubGeneratePending();
    const running = store.uploadAndGenerate(smallFile());
    await gate.waitUntilPending();

    // 生成请求在途时用户又改了内容
    store.commitLabel('c', '等待期间又改了一次');
    gate.release();
    await running;

    // 画布上仍是用户最后那次修改，生成结果没有盖上来
    expect(store.content?.root.children[0].label).toBe('等待期间又改了一次');
    expect(store.uploadError).toContain('保留你的修改');
    expect(store.uploadState).toBe('idle');
  });

  it('没有改动时正常落图', async () => {
    const { store } = mountApp();
    store.preview(generated, 'right');
    store.commitLabel('c', '我改过的字');

    const gate = stubGeneratePending();
    const running = store.uploadAndGenerate(smallFile());
    await gate.waitUntilPending();
    gate.release();
    await running;

    // 用户没动过，生成结果照常替换
    expect(store.content?.root.children[0].label).toBe('监督学习');
    expect(store.uploadError).toBe('');
    expect(store.dirty).toBe(true);
  });

  it('confirmReplace 返回 true 时按用户意愿替换', async () => {
    const { store } = mountApp();
    store.preview(generated, 'right');
    store.commitLabel('c', '原始文字');

    const gate = stubGeneratePending();
    const asked: string[] = [];
    const running = store.uploadAndGenerate(smallFile(), {
      confirmReplace: reason => {
        asked.push(reason);
        return true;
      },
    });
    await gate.waitUntilPending();
    store.commitLabel('c', '等待期间的修改');
    gate.release();
    await running;

    expect(asked).toEqual(['edited']);
    expect(store.content?.root.children[0].label).toBe('监督学习');
  });

  it('等待期间切换导图：不套到当前图上', async () => {
    const { store } = mountApp();
    store.preview(generated, 'right');

    const other: MindMapContent = {
      title: '另一张图',
      root: { id: 'r2', label: '另一张', children: [] },
    };

    const gate = stubGeneratePending();
    const running = store.uploadAndGenerate(smallFile());
    await gate.waitUntilPending();

    // 生成还没回来，用户打开了另一张导图
    store.loadMap(other, { id: 'm9', version: 4, updated_at: '2026-11-04T00:00:00Z' });
    gate.release();
    await running;

    expect(store.mapId).toBe('m9');
    expect(store.content?.title).toBe('另一张图');
    expect(store.uploadError).toContain('切换到别的导图');
  });
});

describe('上传文档生成导图', () => {
  it('解析成功后自动送进生成流水线，第一次请求是 multipart', async () => {
    stubFetch(call =>
      call.url === UPLOAD_URL ? jsonResponse({ source: source() }) : jsonResponse(pipelineReply()),
    );
    const { wrapper } = mountApp();

    await pickFile(wrapper, smallFile());
    await vi.waitFor(() => expect(calls).toHaveLength(2));

    expect(calls.map(call => call.method)).toEqual(['POST', 'POST']);
    expect(calls.map(call => call.url)).toEqual([UPLOAD_URL, GENERATE_URL]);

    // 上传必须是 multipart：不能手动带 JSON 的 Content-Type，否则浏览器不给 boundary，
    // 后端拆不出 file 字段。这里直接断言 body 是带 file 的 FormData。
    const upload = calls[0];
    expect(upload.headers).toBeUndefined();
    expect(upload.body).toBeInstanceOf(FormData);
    const form = upload.body as FormData;
    expect(form.has('file')).toBe(true);
    expect((form.get('file') as File).name).toBe('手册.pdf');

    // 第二步把解析出来的 source 原样送回后端。
    const sent = JSON.parse(String(calls[1].body)) as { sources: Source[] };
    expect(sent.sources).toHaveLength(1);
    expect(sent.sources[0].id).toBe('s1');
    expect(sent.sources[0].text).toContain('第一章');
  });

  it('生成结果放到画布上，状态栏说明来源是上传的文档', async () => {
    stubFetch(call =>
      call.url === UPLOAD_URL ? jsonResponse({ source: source() }) : jsonResponse(pipelineReply()),
    );
    const { wrapper } = mountApp();

    await pickFile(wrapper, smallFile());
    await vi.waitFor(() =>
      expect(wrapper.get('[role="status"]').text()).toContain('已根据上传的「手册.pdf」'));

    expect(wrapper.text()).toContain('2 个节点');
    // preview() 之后按真实来源改回来的：生成的图还没保存过。
    expect(wrapper.text()).toContain('有未保存的修改');
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
    stubFetch(call => (call.url === UPLOAD_URL ? pending : jsonResponse(pipelineReply())));

    const store = useMindMapStore();
    const done = store.uploadAndGenerate(smallFile());

    await vi.advanceTimersByTimeAsync(3000);
    expect(store.uploadState).toBe('parsing');
    expect(store.parseSeconds).toBe(3);

    release(jsonResponse({ source: source() }));
    await done;
    expect(store.uploadState).toBe('idle');
  });

  it('截断时明确提示总页数与已处理页数', async () => {
    stubFetch(call =>
      call.url === UPLOAD_URL
        ? jsonResponse({
            source: source({
              span: { page: 51, start: null, end: null },
              parsed_pages: 50,
              truncated: true,
            }),
          })
        : jsonResponse(pipelineReply()),
    );
    const { wrapper } = mountApp();

    await pickFile(wrapper, smallFile());
    await vi.waitFor(() => expect(wrapper.text()).toContain('共 51 页，仅处理前 50 页'));
  });

  it('中间有页识别失败时提示具体页码，而不是谎报"仅处理前 N 页"', async () => {
    // 3 页都处理了，但第 2 页没拿到内容。旧实现会输出
    // "共 3 页，仅处理前 3 页"——自相矛盾，也没说清缺哪一页。
    stubFetch(call =>
      call.url === UPLOAD_URL
        ? jsonResponse({
            source: source({
              span: { page: 3, start: null, end: null },
              parsed_pages: 2,
              truncated: false,
              failed_pages: [2],
            }),
          })
        : jsonResponse(pipelineReply()),
    );
    const { wrapper } = mountApp();

    await pickFile(wrapper, smallFile());
    await vi.waitFor(() => expect(wrapper.text()).toContain('第 2 页 识别失败'));
    expect(wrapper.text()).not.toContain('仅处理前');
  });

  it('失败页较多时只列前几个并给出总数', async () => {
    stubFetch(call =>
      call.url === UPLOAD_URL
        ? jsonResponse({
            source: source({
              span: { page: 10, start: null, end: null },
              parsed_pages: 4,
              truncated: false,
              failed_pages: [2, 3, 5, 6, 7, 9],
            }),
          })
        : jsonResponse(pipelineReply()),
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

  it('生成失败（超字数）时显示后端 detail，解析结果不丢', async () => {
    const detail = '合并后内容为 51762 字，超过当前 20000 字限制；请缩短或分批处理';
    stubFetch(call => (call.url === UPLOAD_URL ? jsonResponse({ source: source() }) : jsonResponse({ detail }, 422)));
    const { wrapper, store } = mountApp();

    await pickFile(wrapper, smallFile());
    await vi.waitFor(() => expect(wrapper.get('[role="alert"]').text()).toBe(detail));

    // 来源留着：页数与截断信息仍要在界面上，不能因为生成失败就清空。
    expect(store.uploadSource?.id).toBe('s1');
    expect(store.uploadState).toBe('idle');
  });

  it('连不上后端时说清是网络问题', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => {
      throw new TypeError('Failed to fetch');
    }));
    const { wrapper } = mountApp();

    await pickFile(wrapper, smallFile());
    await vi.waitFor(() => expect(wrapper.text()).toContain('无法连接后端'));
  });
});
