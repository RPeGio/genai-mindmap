import { createPinia, disposePinia, setActivePinia, type Pinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { Source } from '../src/api/types';
import { MAX_AUDIO_BYTES, MAX_INPUT_CHARS, MAX_SOURCES, MAX_UPLOAD_BYTES, mergedCharCount, sourceInputError, textSource } from '../src/sources/input';
import { useMindMapStore } from '../src/stores/mindmap';
import { useSourcesStore } from '../src/stores/sources';

const json = (body: unknown, status = 200) => ({
  ok: status < 400, status, json: async () => body,
}) as Response;

function parsed(id = 's1', overrides: Partial<Source> = {}): Source {
  return { ...textSource('已解析正文'), id, kind: 'document', filename: '资料.pdf', ...overrides };
}

function file(name = '资料.pdf', size?: number): File {
  const result = new File(['content'], name);
  if (size !== undefined) Object.defineProperty(result, 'size', { value: size });
  return result;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}

/** 等一拍：fetch → json() → 状态落盘要跨好几个微任务。 */
const flush = () => new Promise(resolve => setTimeout(resolve, 0));

/** 上传时用的文件名。并发下 fetch 的调用先后不稳定，不能靠 mockResolvedValueOnce 排队。 */
function uploadName(init?: RequestInit): string {
  const form = init?.body as FormData;
  return (form.get('file') as File).name;
}

/** 按文件名路由响应，与调用顺序无关。 */
function routedFetch(routes: Record<string, () => Response | Promise<Response>>) {
  return vi.fn(async (_url: string, init?: RequestInit) => {
    const name = uploadName(init);
    const route = routes[name];
    if (!route) throw new Error(`未预期的上传：${name}`);
    return route();
  });
}

let pinia: Pinia;
beforeEach(() => {
  pinia = createPinia();
  setActivePinia(pinia);
});
afterEach(() => {
  disposePinia(pinia);
  vi.unstubAllGlobals();
});

describe('来源收集', () => {
  it('HTTP 环境没有 randomUUID 时也能加入文本和文件，来源 ID 不重复', async () => {
    vi.stubGlobal('crypto', {});
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ source: parsed('uploaded') })));
    const store = useSourcesStore();
    expect(store.addText('文本一')).toBe(true);
    expect(store.addText('文本二')).toBe(true);
    await store.addFiles([file()]);
    expect(store.items).toHaveLength(3);
    expect(new Set(store.items.map(item => item.id)).size).toBe(3);
    expect(new Set(store.readySources.map(source => source.id)).size).toBe(3);
    expect(store.problem).toBe('');
  });

  it('文本、文档、图片、音频按添加顺序收集，解析不生成且不修改旧图', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(json({ source: parsed('doc') }))
      .mockResolvedValueOnce(json({ source: parsed('image', { kind: 'image', filename: '草图.png' }) }))
      .mockResolvedValueOnce(json({ source: parsed('audio', { kind: 'audio', filename: '录音.wav', duration_seconds: 12 }) }));
    vi.stubGlobal('fetch', fetchMock);
    const store = useSourcesStore();
    const map = useMindMapStore();
    const old = { title: '旧图', root: { id: 'old', label: '旧图', children: [] } };
    map.loadMap(old, { id: 'map-old', version: 3, updated_at: '2026-11-05' });
    store.addText('  补充说明  ');
    await store.addFiles([file(), file('草图.png'), file('录音.wav')]);

    expect(store.readySources.map(source => source.kind)).toEqual(['text', 'document', 'image', 'audio']);
    expect(new Set(store.readySources.map(source => source.id)).size).toBe(4);
    expect(store.readySources[0].text).toBe('补充说明');
    expect(store.readySources[3].duration_seconds).toBe(12);
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual(Array(3).fill('/api/ingestion/files'));
    expect(map.content).toEqual(old);
    expect(map.mapId).toBe('map-old');
    expect(map.dirty).toBe(false);
  });

  it('单个文件失败不终止后续文件；仅重试失败项且顺序不变', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(json({ source: parsed('a') }))
      .mockResolvedValueOnce(json({ detail: '图片识别暂不可用' }, 422))
      .mockResolvedValueOnce(json({ source: parsed('c') }))
      .mockResolvedValueOnce(json({ source: parsed('b', { kind: 'image' }) }));
    vi.stubGlobal('fetch', fetchMock);
    const store = useSourcesStore();
    await store.addFiles([file('a.pdf'), file('b.png'), file('c.wav')]);

    expect(store.items.map(item => item.state)).toEqual(['ready', 'failed', 'ready']);
    expect(store.items[1].error).toBe('图片识别暂不可用');
    expect(store.problem).toContain('重试或移除');
    await store.retry(store.items[1].id);
    expect(store.readySources.map(source => source.id)).toEqual(['a', 'b', 'c']);
    expect(fetchMock).toHaveBeenCalledTimes(4);
    expect(store.problem).toBe('');
  });

  it.each(['remove', 'clear'] as const)('%s 中止在途解析，迟到响应不能恢复已移除来源', async action => {
    const pending = deferred<Response>();
    const fetchMock = vi.fn().mockReturnValue(pending.promise);
    vi.stubGlobal('fetch', fetchMock);
    const store = useSourcesStore();
    const task = store.addFiles([file(), file('排队.pdf')]);
    const signal = fetchMock.mock.calls[0][1].signal as AbortSignal;
    expect(store.parsing).toBe(true);
    expect(store.items.map(item => item.state)).toEqual(['parsing', 'queued']);
    if (action === 'clear') store.clear();
    else store.items.map(item => item.id).forEach(id => store.remove(id));
    expect(signal.aborted).toBe(true);
    pending.resolve(json({ source: parsed() }));
    await task;

    expect(store.items).toEqual([]);
    expect(store.parsing).toBe(false);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    // 清空之后仍可正常追加新来源。
    fetchMock.mockResolvedValue(json({ source: parsed('new') }));
    await store.addFiles([file('新的.pdf')]);
    expect(store.readySources[0].id).toBe('new');
  });

  it('追加文件会进入同一个串行队列', async () => {
    const pending = deferred<Response>();
    const fetchMock = vi.fn().mockReturnValueOnce(pending.promise)
      .mockResolvedValue(json({ source: parsed('second') }));
    vi.stubGlobal('fetch', fetchMock);
    const store = useSourcesStore();
    const first = store.addFiles([file('第一份.pdf')]);
    const second = store.addFiles([file('第二份.pdf')]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    pending.resolve(json({ source: parsed('first') }));
    await Promise.all([first, second]);
    expect(store.readySources.map(source => source.id)).toEqual(['first', 'second']);
  });

  it('两份文档 + 两张图片同时添加：文档串行，图片并发', async () => {
    const running = deferred<Response>();
    const fetchMock = routedFetch({
      '第一份.pdf': () => running.promise,
      '第二份.pdf': () => Promise.resolve(json({ source: parsed('doc-2') })),
      '图一.png': () => Promise.resolve(json({ source: parsed('img-1', { kind: 'image' }) })),
      '图二.png': () => Promise.resolve(json({ source: parsed('img-2', { kind: 'image' }) })),
    });
    vi.stubGlobal('fetch', fetchMock);
    const store = useSourcesStore();

    const task = store.addFiles([file('第一份.pdf'), file('第二份.pdf'), file('图一.png'), file('图二.png')]);
    const started = fetchMock.mock.calls.map(([, init]) => uploadName(init as RequestInit));
    // 第二份文档被第一份挡住（本地 CPU 串行），两张图片则已经各自发出
    expect(started).not.toContain('第二份.pdf');
    expect(new Set(started)).toEqual(new Set(['第一份.pdf', '图一.png', '图二.png']));
    expect(store.items.map(item => item.state)).toEqual(['parsing', 'queued', 'parsing', 'parsing']);

    running.resolve(json({ source: parsed('doc-1') }));
    await task;
    expect(store.items.map(item => item.state)).toEqual(['ready', 'ready', 'ready', 'ready']);
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });

  it('图片并发：两张图片的解析请求同时在途', async () => {
    const both = [deferred<Response>(), deferred<Response>()];
    const fetchMock = routedFetch({
      '图一.png': () => both[0].promise,
      '图二.png': () => both[1].promise,
    });
    vi.stubGlobal('fetch', fetchMock);
    const store = useSourcesStore();

    const task = store.addFiles([file('图一.png'), file('图二.png')]);
    // 第一张还没回来，第二张已经发出去了——两个请求的时间区间是重叠的
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(store.items.map(item => item.state)).toEqual(['parsing', 'parsing']);

    both[1].resolve(json({ source: parsed('img-2', { kind: 'image', filename: '图二.png' }) }));
    both[0].resolve(json({ source: parsed('img-1', { kind: 'image', filename: '图一.png' }) }));
    await task;
    expect(store.items.map(item => item.state)).toEqual(['ready', 'ready']);
  });

  it('items 顺序始终等于添加顺序，后加的图片先解析完也不重排', async () => {
    const slowDoc = deferred<Response>();
    const slowImage = deferred<Response>();
    const fetchMock = routedFetch({
      '资料.pdf': () => slowDoc.promise,
      '慢图.png': () => slowImage.promise,
      '快图.png': () => Promise.resolve(json({ source: parsed('fast', { kind: 'image', filename: '快图.png' }) })),
    });
    vi.stubGlobal('fetch', fetchMock);
    const store = useSourcesStore();

    const task = store.addFiles([file('资料.pdf'), file('慢图.png'), file('快图.png')]);
    const order = store.items.map(item => item.label);
    await flush();
    // 第三项已经就绪、第二项还在解析，列表位置不能因此调换
    expect(store.items.map(item => item.state)).toEqual(['parsing', 'parsing', 'ready']);
    expect(store.items.map(item => item.label)).toEqual(order);

    slowImage.resolve(json({ source: parsed('slow', { kind: 'image', filename: '慢图.png' }) }));
    slowDoc.resolve(json({ source: parsed('doc') }));
    await task;
    expect(store.items.map(item => item.label)).toEqual(order);
    // 解析完成的先后不影响交给生成的顺序
    expect(store.readySources.map(source => source.id)).toEqual(['doc', 'slow', 'fast']);
  });

  it('单个条目失败只影响自己：文档队列继续，图片互不牵连', async () => {
    const fetchMock = routedFetch({
      '坏.pdf': () => Promise.resolve(json({ detail: '文档解析失败' }, 422)),
      '好.pdf': () => Promise.resolve(json({ source: parsed('doc-2') })),
      '坏图.png': () => Promise.resolve(json({ detail: '图片识别暂不可用' }, 422)),
      '好图.png': () => Promise.resolve(json({ source: parsed('img-2', { kind: 'image' }) })),
    });
    vi.stubGlobal('fetch', fetchMock);
    const store = useSourcesStore();

    await store.addFiles([file('坏.pdf'), file('好.pdf'), file('坏图.png'), file('好图.png')]);

    expect(store.items.map(item => item.state)).toEqual(['failed', 'ready', 'failed', 'ready']);
    expect(store.items[0].error).toBe('文档解析失败');
    expect(store.items[2].error).toBe('图片识别暂不可用');
    expect(store.readySources.map(source => source.id)).toEqual(['doc-2', 'img-2']);
  });

  it.each([
    ['大图.png', MAX_UPLOAD_BYTES + 1, '10MB'],
    ['长录音.WAV', MAX_AUDIO_BYTES + 1, '20MB'],
    ['空文件.pdf', 0, '内容为空'],
  ])('本地拦截无效文件 %s，不上传', async (name, size, message) => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const store = useSourcesStore();
    await store.addFiles([file(name, size)]);
    expect(store.items[0].error).toContain(message);
    expect(store.items[0].state).toBe('failed');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('10MB 以上、20MB 以内的 WAV 可以上传', async () => {
    const fetchMock = vi.fn().mockResolvedValue(json({ source: parsed('audio', { kind: 'audio' }) }));
    vi.stubGlobal('fetch', fetchMock);
    const store = useSourcesStore();
    await store.addFiles([file('录音.WAV', MAX_AUDIO_BYTES)]);
    expect(store.items[0].state).toBe('ready');
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it.each([
    { text: '   ' }, { status: 'failed' as const, error: '识别失败' }, { status: 'pending' as const },
  ])('后端返回空正文或未解析来源时不标记就绪：%j', async overrides => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ source: parsed('bad', overrides) })));
    const store = useSourcesStore();
    await store.addFiles([file()]);
    expect(store.items[0].state).toBe('failed');
    expect(store.readySources).toEqual([]);
  });

  it('50 个来源上限包含文本与文件，超出批次整体拒绝且不丢原来源', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const store = useSourcesStore();
    for (let i = 0; i < MAX_SOURCES - 1; i++) store.addText(`文本 ${i}`);
    await store.addFiles([file('a.pdf'), file('b.pdf')]);
    expect(store.items).toHaveLength(49);
    expect(store.error).toContain('本次未添加');
    expect(store.addText('最后一条')).toBe(true);
    expect(store.addText('超出')).toBe(false);
    expect(store.items).toHaveLength(50);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('空白文本不加入列表', () => {
    const store = useSourcesStore();
    expect(store.addText(' \n\t ')).toBe(false);
    expect(store.items).toEqual([]);
  });
});

describe('合并生成边界', () => {
  it('长度按 Unicode 码点并计入分隔换行，刚好到上限可提交，超出拦截', () => {
    const valid = [textSource('😀'.repeat(MAX_INPUT_CHARS - 3)), textSource(' 字 ')];
    expect(mergedCharCount(valid)).toBe(MAX_INPUT_CHARS);
    expect(sourceInputError(valid)).toBe('');
    expect(sourceInputError([...valid, textSource('多')])).toContain(`${MAX_INPUT_CHARS + 3} 字`);
  });

  it.each([
    { sources: [] as Source[], message: '至少一个来源' },
    { sources: [parsed(), parsed()], message: '来源 ID 重复' },
    { sources: [parsed('bad', { status: 'failed' })], message: '尚未解析成功' },
    { sources: Array.from({ length: 51 }, (_, i) => parsed(String(i))), message: '最多添加 50' },
  ])('无效来源过不了界面校验：$message', ({ sources, message }) => {
    // 校验是纯函数，界面拿它决定「合并生成导图」按不按得下去；
    // 直接测它，就不必再借一条已经不存在的生成通道来断言"没发请求"。
    expect(sourceInputError(sources)).toContain(message);
  });
});
