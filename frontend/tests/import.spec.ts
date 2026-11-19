import { enableAutoUnmount, mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import App from '../src/App.vue';
import ImportMindmap from '../src/components/ImportMindmap.vue';
import { ApiError, exportFilename, exportLabel, importMindMap } from '../src/api/client';
import type { ImportResult, MindMapContent } from '../src/api/types';
import { useMindMapStore } from '../src/stores/mindmap';

// jsdom 里没有 ResizeObserver，真正的 Vue Flow 画布跑不起来。这里只验证导入入口与
// 结果面板；画布能否渲染导图由 `smoke-canvas.spec.ts` 那批用例管。
vi.mock('../src/canvas/MindMapCanvas.vue', () => ({
  default: { name: 'MindMapCanvasStub', template: '<div data-test="canvas" />' },
}));
vi.mock('../src/audio/recorder', async importOriginal => ({
  ...await importOriginal<typeof import('../src/audio/recorder')>(),
  startRecording: vi.fn(),
}));

const CONTENT: MindMapContent = {
  title: '导入的图',
  root: {
    id: 'n_root',
    label: '根',
    children: [{ id: 'n_a', label: '甲', children: [] }],
  },
  layout: 'tree',
  theme: { direction: 'left' },
  edges: [{ id: 'e_1', source: 'n_root', target: 'n_a', label: '是' }],
} as MindMapContent;

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

function ok(body: Partial<ImportResult> = {}): ImportResult {
  return { content: CONTENT, warnings: [], source_format: 'json', ...body };
}

interface Call {
  url: string;
  method: string;
  /** 原样保留：导入那一步是 FormData，String() 会变成 "[object FormData]"。 */
  body: unknown;
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
      };
      calls.push(call);
      return handler(call);
    }),
  );
}

// 文件选择框被 teleport 到 body，所以必须卸载组件才会把它带走；不卸载的话上一个
// 用例留下的 input 会被后一个用例查到（`document.querySelector` 命中第一个）。
enableAutoUnmount(afterEach);

/**
 * 文件选择框被 teleport 到 body（见组件里的注释），所以不能用 `wrapper.find`：
 * Vue Test Utils 只在组件子树里找。这里查 document，并且取**最后一个**——同一次
 * 运行时里可能还有别的 wrapper 尚未卸载。
 */
function fileInput(): HTMLInputElement {
  const nodes = document.querySelectorAll('[data-test="import-file"]');
  const element = nodes[nodes.length - 1];
  if (!(element instanceof HTMLInputElement)) throw new Error('找不到文件选择框');
  return element;
}

/** 把一份文件塞进 `<input type=file>`。jsdom 的 `files` 是只读的，只能重定义。 */
function attachFile(file: File): void {
  Object.defineProperty(fileInput(), 'files', { value: [file], configurable: true });
}

/** 触发 change：`@change` 监听在元素上，dispatchEvent 就够了。 */
async function chooseFile(): Promise<void> {
  fileInput().dispatchEvent(new Event('change'));
  await Promise.resolve();
}

function mountImport(confirmDiscard = () => true) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useMindMapStore();
  const wrapper = mount(ImportMindmap, {
    props: { confirmDiscard },
    global: { plugins: [pinia] },
  });
  return { wrapper, store };
}

describe('importMindMap', () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it('用 multipart 发到 /mindmaps/import，字段名是 file', async () => {
    stubFetch(() => jsonResponse(ok()));
    const file = new File(['{}'], '图.json', { type: 'application/json' });

    const result = await importMindMap(file);

    expect(result.content.title).toBe('导入的图');
    const [call] = calls;
    expect(call.url).toContain('/mindmaps/import');
    expect(call.method).toBe('POST');
    // 不能设 Content-Type：boundary 必须由浏览器生成
    const body = call.body as FormData;
    expect(body).toBeInstanceOf(FormData);
    expect((body.get('file') as File).name).toBe('图.json');
  });

  it('把后端的 code 与 message 带进 ApiError', async () => {
    stubFetch(() => jsonResponse({ code: 'parse_failed', message: '第 3 行解析失败' }, 400));

    await expect(importMindMap(new File(['x'], 'a.json'))).rejects.toMatchObject({
      code: 'parse_failed',
      message: '第 3 行解析失败',
      status: 400,
    });
  });

  it('后端返回空壳时给一句可读的错，而不是把 undefined 塞进画布', async () => {
    stubFetch(() => jsonResponse({ warnings: [], source_format: 'json' }));

    await expect(importMindMap(new File(['x'], 'a.json'))).rejects.toThrow(/没有返回解析结果/);
  });

  it('连不上后端时提示检查服务', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')));

    await expect(importMindMap(new File(['x'], 'a.json'))).rejects.toThrow(/无法连接后端/);
  });
});

describe('ImportMindmap', () => {
  beforeEach(() => {
    stubFetch(() => jsonResponse(ok()));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('导入成功后把导图装进画布，并且是"未保存"状态', async () => {
    const { store } = mountImport();
    attachFile(new File(['{}'], '图.json'));

    await chooseFile();
    await vi.waitFor(() => expect(store.content?.title).toBe('导入的图'));

    expect(store.mode).toBe('import');
    // 关键三条：不落库 → 没有 mapId；要提醒保存 → dirty；文件自带的朝向不被覆盖
    expect(store.mapId).toBeNull();
    expect(store.dirty).toBe(true);
    expect(store.direction).toBe('left');
    // 导入不是可撤销的编辑步骤（撤销回到导入前等于清空画布）
    expect(store.canUndo).toBe(false);
    expect(store.content?.edges?.length).toBe(1);
  });

  it('有警告时在界面上列出来，不只写日志', async () => {
    stubFetch(() => jsonResponse(ok({
      warnings: [
        { code: 'truncated_nodes', message: '文件共有 1200 个节点，已按层序保留前 1000 个' },
        { code: 'source_refs_cleared', message: '导入的引用已清空（3 处）' },
      ],
    })));
    const { wrapper } = mountImport();
    attachFile(new File(['{}'], '图.json'));

    await chooseFile();
    await vi.waitFor(() => expect(wrapper.findAll('[data-test="import-warning"]').length).toBe(2));

    const text = wrapper.find('[data-test="import-panel"]').text();
    expect(text).toContain('1000');
    expect(text).toContain('引用已清空');
    expect(text).toContain('未保存');
  });

  it('告警面板可以关闭', async () => {
    stubFetch(() => jsonResponse(ok({
      warnings: [{ code: 'truncated_depth', message: '9 层被截到 8 层' }],
    })));
    const { wrapper } = mountImport();
    attachFile(new File(['{}'], '图.json'));

    await chooseFile();
    await vi.waitFor(() => expect(wrapper.find('[data-test="import-panel"]').exists()).toBe(true));

    await wrapper.find('[data-test="import-dismiss"]').trigger('click');
    expect(wrapper.find('[data-test="import-panel"]').exists()).toBe(false);
  });

  it('没有警告时不弹面板（成功只写状态栏那条）', async () => {
    const { wrapper, store } = mountImport();
    attachFile(new File(['{}'], '图.json'));

    await chooseFile();
    await vi.waitFor(() => expect(store.content?.title).toBe('导入的图'));

    // 一次干净的导入不该在顶栏留一块面板——它没有需要用户处理的东西。
    expect(wrapper.find('[data-test="import-panel"]').exists()).toBe(false);
    expect(store.status).toContain('已导入');
  });

  it('失败时显示后端原话，而不是笼统的"导入失败"', async () => {
    stubFetch(() => jsonResponse({
      code: 'parse_failed',
      message: 'XMind content.json 第 12 行第 5 列解析失败：Expecting value',
    }, 400));
    const { wrapper, store } = mountImport();
    attachFile(new File(['{}'], '坏.xmind'));

    await chooseFile();
    await vi.waitFor(() => expect(wrapper.find('[data-test="import-panel"]').exists()).toBe(true));

    const text = wrapper.find('[data-test="import-panel"]').text();
    expect(text).toContain('第 12 行第 5 列');
    // 失败不能动画布：上一次的内容还在，也没被标成未保存
    expect(store.content).toBeNull();
    expect(store.dirty).toBe(false);
  });

  it('未知格式的 400 也原样显示', async () => {
    stubFetch(() => jsonResponse({
      code: 'unsupported_format',
      message: '认不出这个文件的格式（文件名：数据.csv）',
    }, 400));
    const { wrapper } = mountImport();
    attachFile(new File(['a,b'], '数据.csv'));

    await chooseFile();
    await vi.waitFor(() => expect(wrapper.find('[data-test="import-panel"]').exists()).toBe(true));
    expect(wrapper.find('[data-test="import-panel"]').text()).toContain('认不出这个文件的格式');
  });

  it('有未保存修改且用户取消时，什么都不发', async () => {
    const confirmDiscard = vi.fn(() => false);
    const { store } = mountImport(confirmDiscard);
    attachFile(new File(['{}'], '图.json'));

    await chooseFile();
    await Promise.resolve();

    expect(confirmDiscard).toHaveBeenCalled();
    expect(calls.length).toBe(0);
    expect(store.content).toBeNull();
  });

  it('点按钮打开文件选择框', async () => {
    const { wrapper } = mountImport();
    const click = vi.spyOn(fileInput(), 'click');

    await wrapper.find('[data-test="import-button"]').trigger('click');
    expect(click).toHaveBeenCalled();
  });
});

describe('store.importMap', () => {
  it('换图时清掉上一张图的身份与候选', () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const store = useMindMapStore();
    store.loadMap(
      { title: '旧的', root: { id: 'r', label: '旧根', children: [] } } as MindMapContent,
      { id: 'map-1', version: 4, updated_at: '2026-01-01T00:00:00Z' },
    );

    store.importMap(CONTENT);

    expect(store.mapId).toBeNull();
    expect(store.version).toBe(0);
    expect(store.savedAt).toBeNull();
    expect(store.mode).toBe('import');
    expect(store.dirty).toBe(true);
    // 上一张图的生成记录也一起清掉：它说的是另一棵树"改了什么"
    expect(store.generatedSummary).toBeNull();
    expect(store.generatedChanges).toBeNull();
  });

  it('朝向来自文件本身，而不是默认值', () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const store = useMindMapStore();

    store.importMap({ ...CONTENT, theme: { direction: 'both' } } as MindMapContent);

    expect(store.direction).toBe('both');
  });
});

describe('导出 JSON', () => {
  it('有可读名称与 .json 扩展名', () => {
    expect(exportLabel('json')).toBe('JSON');
    expect(exportFilename('json', '季度规划')).toBe('季度规划.json');
  });
});

describe('顶栏入口', () => {
  it('App 顶栏有「导入」按钮', async () => {
    stubFetch(() => jsonResponse(ok()));
    const pinia = createPinia();
    setActivePinia(pinia);
    const wrapper = mount(App, { global: { plugins: [pinia] } });

    expect(wrapper.find('[data-test="import-button"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="import-button"]').text()).toContain('导入');
    // 导出下拉里也有 JSON 这一项（导入的出入口必须成对出现）
    const options = wrapper.findAll('select option').map(option => option.attributes('value'));
    expect(options).toContain('json');
    wrapper.unmount();
  });

  it('ApiError 的 code 可用于分流（unsupported_format / parse_failed）', () => {
    const error = new ApiError(400, 'unsupported_format', '认不出来');
    expect(error.code).toBe('unsupported_format');
  });
});
