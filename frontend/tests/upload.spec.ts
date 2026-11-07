import { mount, type VueWrapper } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import App from '../src/App.vue';
import { startRecording } from '../src/audio/recorder';
import type { MindMapContent, PipelineResponse, Source } from '../src/api/types';
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
const GENERATE_URL = '/api/pipeline/generate';

beforeEach(() => {
  setActivePinia(createPinia());
});

afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount());
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('多来源生成期间用户改动不会被静默覆盖', () => {
  /**
   * 让生成请求挂起，返回放行函数。
   *
   * 直接调 store.generateSources 而不 mount App：这里验证的是覆盖判定逻辑，
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
    const running = store.generateSources([source()]);
    await gate.waitUntilPending();

    // 生成请求在途时用户又改了内容
    store.commitLabel('c', '等待期间又改了一次');
    gate.release();
    await running;

    // 画布上仍是用户最后那次修改，生成结果没有盖上来
    expect(store.content?.root.children[0].label).toBe('等待期间又改了一次');
    expect(store.sourceGenerationError).toContain('保留你的修改');
    expect(store.sourceGenerating).toBe(false);
  });

  it('没有改动时正常落图', async () => {
    const { store } = mountApp();
    store.preview(generated, 'right');
    store.commitLabel('c', '我改过的字');

    const gate = stubGeneratePending();
    const running = store.generateSources([source()]);
    await gate.waitUntilPending();
    gate.release();
    await running;

    // 用户没动过，生成结果照常替换
    expect(store.content?.root.children[0].label).toBe('监督学习');
    expect(store.sourceGenerationError).toBe('');
    expect(store.dirty).toBe(true);
  });

  it('confirmReplace 返回 true 时按用户意愿替换', async () => {
    const { store } = mountApp();
    store.preview(generated, 'right');
    store.commitLabel('c', '原始文字');

    const gate = stubGeneratePending();
    const asked: string[] = [];
    const running = store.generateSources([source()], {
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

  it('等待期间正在输入框里打字（尚未提交）：同样不落图', async () => {
    // 打字时文字只在 MindNode.vue 的本地 draft 里，store 的 content 引用没变。
    // 只比引用会漏掉这种"输入到一半"的修改，所以要连编辑态一起判断。
    const { store } = mountApp();
    store.preview(generated, 'right');

    const gate = stubGeneratePending();
    const running = store.generateSources([source()]);
    await gate.waitUntilPending();

    // 打开编辑器但不提交
    store.beginEdit('c');
    expect(store.editingId).toBe('c');
    gate.release();
    await running;

    expect(store.content?.root.children[0].label).toBe('监督学习');
    expect(store.sourceGenerationError).toContain('保留你的修改');
  });

  it('等待期间标题框有未提交的输入：同样不落图', async () => {
    // 标题走 :value + @change，打字时 content 不变也碰不到 editingId，
    // 只有 titleDraft 能反映"这里有没提交的输入"。
    const { store } = mountApp();
    store.preview(generated, 'right');

    const gate = stubGeneratePending();
    const running = store.generateSources([source()]);
    await gate.waitUntilPending();

    store.setTitleDraft('我在改标题');
    gate.release();
    await running;

    expect(store.content?.title).toBe('机器学习');
    expect(store.sourceGenerationError).toContain('保留你的修改');
  });

  it('等待期间切换导图：不套到当前图上', async () => {
    const { store } = mountApp();
    store.preview(generated, 'right');

    const other: MindMapContent = {
      title: '另一张图',
      root: { id: 'r2', label: '另一张', children: [] },
    };

    const gate = stubGeneratePending();
    const running = store.generateSources([source()]);
    await gate.waitUntilPending();

    // 生成还没回来，用户打开了另一张导图
    store.loadMap(other, { id: 'm9', version: 4, updated_at: '2026-11-04T00:00:00Z' });
    gate.release();
    await running;

    expect(store.mapId).toBe('m9');
    expect(store.content?.title).toBe('另一张图');
    expect(store.sourceGenerationError).toContain('切换到别的导图');
  });
});

describe('上传文档生成导图', () => {
  it('解析成功仅加入列表，点击合并才生成；上传使用 multipart', async () => {
    stubFetch(call =>
      call.url === UPLOAD_URL ? jsonResponse({ source: source() }) : jsonResponse(pipelineReply()),
    );
    const { wrapper } = mountApp();

    await pickFile(wrapper, smallFile());
    await vi.waitFor(() => expect(useSourcesStore().readySources).toHaveLength(1));
    expect(calls).toHaveLength(1);
    await generateCollected(wrapper);
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

  it('统一生成结果放到画布上，状态栏说明来源数量', async () => {
    stubFetch(call =>
      call.url === UPLOAD_URL ? jsonResponse({ source: source() }) : jsonResponse(pipelineReply()),
    );
    const { wrapper } = mountApp();

    await pickFile(wrapper, smallFile());
    await generateCollected(wrapper);
    await vi.waitFor(() =>
      expect(wrapper.get('[role="status"]').text()).toContain('已合并 1 个来源'));

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
    const detail = '材料约 380000 字，估算 240000 token，超过模型上下文上限 200000。请精简后重试，或分成多次生成。';
    stubFetch(call => (call.url === UPLOAD_URL ? jsonResponse({ source: source() }) : jsonResponse({ detail }, 422)));
    const { wrapper, store, sources } = mountApp();

    await pickFile(wrapper, smallFile());
    await generateCollected(wrapper);
    await vi.waitFor(() => expect(wrapper.get('[role="alert"]').text()).toBe(detail));

    // 来源留着：页数与截断信息仍要在界面上，不能因为生成失败就清空。
    expect(sources.readySources[0]?.id).toBe('s1');
    expect(store.sourceGenerating).toBe(false);
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

describe('多来源输入界面', () => {
  it('多文件、已添加文本和未添加的补充文本一次提交，保留每份来源的元数据', async () => {
    const docs = [
      source({ id: 'doc', truncated: true, span: { page: 30, start: null, end: null }, parsed_pages: 20 }),
      source({ id: 'image', kind: 'image', filename: '草图.png', text: '图里的关系', parsed_pages: null }),
      source({ id: 'audio', kind: 'audio', filename: '讨论.wav', text: '会议记录', parsed_pages: null, duration_seconds: 10 }),
    ];
    let index = 0;
    stubFetch(call => call.url === UPLOAD_URL
      ? jsonResponse({ source: docs[index++] })
      : jsonResponse(pipelineReply()));
    const { wrapper, sources } = mountApp();
    await wrapper.get('#prompt').setValue('先加入的说明');
    await buttonByText(wrapper, '添加文本到来源').trigger('click');
    expect((wrapper.get('#prompt').element as HTMLTextAreaElement).value).toBe('');
    const input = wrapper.get('input[type="file"]');
    expect(input.attributes('multiple')).toBeDefined();
    Object.defineProperty(input.element, 'files', {
      value: [smallFile(), smallFile('草图.png'), smallFile('讨论.wav')], configurable: true,
    });
    await input.trigger('change');
    await vi.waitFor(() => expect(sources.readySources).toHaveLength(4));
    expect(calls).toHaveLength(3);
    await wrapper.get('#prompt').setValue('还没点添加的补充');
    await generateCollected(wrapper);

    await vi.waitFor(() => expect(calls).toHaveLength(4));
    const sent = JSON.parse(String(calls[3].body)).sources as Source[];
    expect(sent.map(item => item.kind)).toEqual(['text', 'document', 'image', 'audio', 'text']);
    expect(sent.slice(1, 4)).toEqual(docs);
    expect(sent[0].text).toBe('先加入的说明');
    expect(sent[4].text).toBe('还没点添加的补充');
    expect(sent.every(item => !('state' in item) && !('seconds' in item))).toBe(true);
    expect(wrapper.text()).toContain('共 30 页，仅处理前 20 页');
    expect(sources.items).toHaveLength(5);
  });

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

  it('存在失败项时阻止生成，移除后可使用剩余来源', async () => {
    stubFetch(call => call.url === UPLOAD_URL
      ? jsonResponse({ detail: '图片识别失败' }, 422)
      : jsonResponse(pipelineReply()));
    const { wrapper, sources } = mountApp();
    sources.addText('有效的文本');
    await pickFile(wrapper, smallFile('失败.png'));
    await vi.waitFor(() => expect(wrapper.text()).toContain('图片识别失败'));
    expect(buttonByText(wrapper, '合并生成导图').attributes('disabled')).toBeDefined();
    await wrapper.get('[aria-label="移除 失败.png"]').trigger('click');
    await generateCollected(wrapper);
    const sent = JSON.parse(String(calls[1].body)).sources as Source[];
    expect(sent).toHaveLength(1);
    expect(sent[0].text).toBe('有效的文本');
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

  it('生成时冻结来源列表，取消后可继续管理来源', async () => {
    let release!: (response: Response) => void;
    stubFetch(() => new Promise(resolve => { release = resolve; }));
    const { wrapper, sources, store } = mountApp();
    sources.addText('文本');
    await generateCollected(wrapper);
    expect(store.sourceGenerating).toBe(true);
    expect(wrapper.get('input[type="file"]').attributes('disabled')).toBeDefined();
    expect(buttonByText(wrapper, '清空来源').attributes('disabled')).toBeDefined();
    expect(buttonByText(wrapper, '移除').attributes('disabled')).toBeDefined();
    await buttonByText(wrapper, '取消').trigger('click');
    expect(store.sourceGenerating).toBe(false);
    expect(sources.items).toHaveLength(1);
    expect(buttonByText(wrapper, '移除').attributes('disabled')).toBeUndefined();
    release(jsonResponse(pipelineReply()));
    await vi.waitFor(() => expect(store.content).toBeNull());
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
    expect(wrapper.get('aside').text()).toContain('不对应具体节点');
    store.loadMap(generated, { id: 'legacy', version: 1, updated_at: '2026-11-05' });
    await wrapper.vm.$nextTick();
    expect(wrapper.get('aside').text()).not.toContain('本图来源');
  });
});
