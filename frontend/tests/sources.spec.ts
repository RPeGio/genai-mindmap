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
    const task = store.addFiles([file(), file('排队.png')]);
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
  it('长度按 Unicode 码点并计入分隔换行，刚好 20000 可提交，超出拦截', async () => {
    const valid = [textSource('😀'.repeat(MAX_INPUT_CHARS - 3)), textSource(' 字 ')];
    expect(mergedCharCount(valid)).toBe(MAX_INPUT_CHARS);
    expect(sourceInputError(valid)).toBe('');
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    await useMindMapStore().generateSources([...valid, textSource('多')]);
    expect(useMindMapStore().sourceGenerationError).toContain('20003 字');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each([
    { sources: [] as Source[], message: '至少一个来源' },
    { sources: [parsed(), parsed()], message: '来源 ID 重复' },
    { sources: [parsed('bad', { status: 'failed' })], message: '尚未解析成功' },
    { sources: Array.from({ length: 51 }, (_, i) => parsed(String(i))), message: '最多添加 50' },
  ])('无效来源不能绕过界面校验：$message', async ({ sources, message }) => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const map = useMindMapStore();
    await map.generateSources(sources);
    expect(map.sourceGenerationError).toContain(message);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('取消后重试，迟到旧响应或旧错误不覆盖新结果', async () => {
    const first = deferred<Response>();
    const second = deferred<Response>();
    const fetchMock = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    vi.stubGlobal('fetch', fetchMock);
    const map = useMindMapStore();
    const old = { title: '旧图', root: { id: 'old', label: '旧图', children: [] } };
    map.loadMap(old, { id: 'old-id', version: 2, updated_at: '2026-11-05' });
    const sources = [textSource('正文')];
    const canceled = map.generateSources(sources);
    const signal = fetchMock.mock.calls[0][1].signal as AbortSignal;
    map.cancelSourceGeneration();
    expect(signal.aborted).toBe(true);
    expect(map.content).toEqual(old);
    expect(map.mapId).toBe('old-id');
    const retried = map.generateSources(sources);
    first.reject(new Error('旧请求断开'));
    await canceled;
    expect(map.sourceGenerating).toBe(true);
    expect(map.sourceGenerationError).toBe('');
    const next = { title: '新图', root: { id: 'new', label: '新图', children: [] } };
    second.resolve(json({ mode: 'model', content: next, stages: [], diagnostics: [] }));
    await retried;
    expect(map.content).toEqual(next);
    expect(map.mapId).toBeNull();
    expect(map.version).toBe(0);
    expect(map.dirty).toBe(true);
    expect(map.sourceGenerating).toBe(false);
  });

  it('两条生成链路互斥，不能重复提交或由流式生成覆盖合并结果', async () => {
    const pending = deferred<Response>();
    const fetchMock = vi.fn().mockReturnValue(pending.promise);
    vi.stubGlobal('fetch', fetchMock);
    const map = useMindMapStore();
    const running = map.generateSources([textSource('正文')]);
    await map.generateSources([textSource('重复')]);
    await map.generate('另一个流式任务');
    expect(fetchMock).toHaveBeenCalledTimes(1);
    map.cancelSourceGeneration();
    pending.resolve(json({ mode: 'demo', content: { title: '迟到', root: { id: 'late', label: '迟到', children: [] } } }));
    await running;
    expect(map.content).toBeNull();
  });
});
