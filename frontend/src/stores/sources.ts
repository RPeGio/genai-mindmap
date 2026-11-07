import { defineStore } from 'pinia';
import { computed, onScopeDispose, ref } from 'vue';

import { uploadDocument } from '../api/client';
import type { Source, SourceKind } from '../api/types';
import { fileKind, MAX_AUDIO_BYTES, MAX_SOURCES, MAX_UPLOAD_BYTES, newSourceId, sourceInputError, textSource } from '../sources/input';

export interface SourceItem {
  /** 列表身份独立于服务端 Source.id；重试不会改变列表位置。 */
  id: string;
  label: string;
  kind: SourceKind;
  state: 'queued' | 'parsing' | 'ready' | 'failed';
  source: Source | null;
  error: string;
  seconds: number;
}

/** 解析泳道：文档在本地 CPU 上跑，图片/语音是网络往返。 */
type Lane = 'document' | 'media';

/**
 * 文档泳道并发度 1，保持串行。
 *
 * 文档走 Docling / pymupdf，是本地 CPU 且 Docling 内部有进程级单例锁，
 * 并发只会互相等锁并把内存叠上去，总耗时不会变好。
 */
const DOCUMENT_CONCURRENCY = 1;

/**
 * 图片 / 语音泳道并发度 2。
 *
 * 两者都是网络往返（视觉识别、云端语音转写），2 路足以把一张图的等待时间
 * 藏在另一张的处理过程里，又不会把识别服务打到限流。
 */
const MEDIA_CONCURRENCY = 2;

/** 本页的输入工作区；File、计时器和 UI 状态不属于 Source 契约。 */
export const useSourcesStore = defineStore('sources', () => {
  const items = ref<SourceItem[]>([]);
  const error = ref('');
  const files = new Map<string, File>();
  const active = new Map<string, AbortController>();
  let textNumber = 0;
  let draining: Promise<void> | null = null;

  const parsing = computed(() => items.value.some(item => item.state === 'queued' || item.state === 'parsing'));
  const readySources = computed(() => items.value.flatMap(item =>
    item.state === 'ready' && item.source ? [item.source] : []));
  const problem = computed(() => {
    if (parsing.value) return '来源正在解析，请等待完成后生成。';
    if (items.value.some(item => item.state === 'failed')) return '有来源解析失败，请重试或移除后生成。';
    return sourceInputError(readySources.value);
  });

  function hasRoom(count: number): boolean {
    error.value = '';
    if (items.value.length + count <= MAX_SOURCES) return true;
    error.value = `最多添加 ${MAX_SOURCES} 个来源，本次未添加，请减少数量后重试。`;
    return false;
  }

  function addText(text: string): boolean {
    if (!text.trim()) {
      error.value = '请输入主题或文本。';
      return false;
    }
    if (!hasRoom(1)) return false;
    const source = textSource(text);
    items.value.push({
      id: source.id, label: `文本 ${++textNumber}`, kind: 'text', state: 'ready',
      source, error: '', seconds: 0,
    });
    return true;
  }

  async function parseItem(item: SourceItem): Promise<void> {
    const file = files.get(item.id);
    if (!file) return;
    item.error = '';
    item.seconds = 0;
    const limit = fileKind(file) === 'audio' ? MAX_AUDIO_BYTES : MAX_UPLOAD_BYTES;
    if (!file.size || file.size > limit) {
      item.state = 'failed';
      item.error = file.size
        ? `「${file.name}」超过 ${limit / (1024 * 1024)}MB 上限，请压缩或拆分后再上传。`
        : '文件内容为空';
      return;
    }

    const controller = new AbortController();
    active.set(item.id, controller);
    item.state = 'parsing';
    const started = Date.now();
    const timer = setInterval(() => { item.seconds = Math.floor((Date.now() - started) / 1000); }, 1000);
    try {
      const source = await uploadDocument(file, controller.signal);
      // 已移除/清空的条目不能被迟到的响应重新加回来。
      if (controller.signal.aborted || !files.has(item.id)) return;
      if (source.status !== 'parsed' || !source.text?.trim()) {
        throw new Error(source.error?.trim() || '这个来源没有解析出可用文字，请重试或移除。');
      }
      item.source = source;
      item.kind = source.kind;
      item.state = 'ready';
    } catch (cause) {
      if (controller.signal.aborted || !files.has(item.id)) return;
      item.state = 'failed';
      item.error = cause instanceof Error ? cause.message : '上传失败，请重试。';
    } finally {
      clearInterval(timer);
      active.delete(item.id);
    }
  }

  /**
   * 判泳道用本地文件的类型：item.kind 在解析成功后会改成后端返回的 kind。
   *
   * 只有图片 / 语音走并发泳道，其余（含文本）都归到串行泳道——宁可慢一点，
   * 也不让本地 CPU 的解析两路抢核。
   */
  function laneOf(item: SourceItem): Lane {
    const file = files.get(item.id);
    const kind = file ? fileKind(file) : item.kind;
    return kind === 'image' || kind === 'audio' ? 'media' : 'document';
  }

  /**
   * 跑一条泳道：最多 limit 个解析同时进行，取队时按 items 顺序。
   *
   * 每次都重新找下一个 queued，而不是起跑前切一份快照：解析期间用户可能
   * 追加文件或点重试，它们要能被已经跑起来的泳道接走，而不是等下一轮 drain。
   */
  async function runLane(lane: Lane, limit: number): Promise<void> {
    const skipped = new Set<string>();
    const worker = async (): Promise<void> => {
      for (;;) {
        const next = items.value.find(item =>
          item.state === 'queued' && !skipped.has(item.id) && laneOf(item) === lane);
        if (!next) return;
        await parseItem(next);
        // 没有对应 File 的异常条目不会进入任何终态，不跳过的话同一个 find
        // 会在原地转圈。
        if (next.state === 'queued') skipped.add(next.id);
      }
    };
    await Promise.all(Array.from({ length: limit }, () => worker()));
  }

  /**
   * 按类型分两条泳道：文档串行，图片 / 语音并发 2 路。
   *
   * 只并发「解析过程」：条目入列后就地更新状态，从不按完成顺序重排，
   * 所以 items 的顺序始终等于添加顺序，用户不会看到列表因后加的图片先解析
   * 完而跳动。单条失败只影响它自己——parseItem 内部捕获异常写进 item.error，
   * 泳道继续跑下一项。
   */
  function drain(): Promise<void> {
    if (draining) return draining;
    draining = Promise.all([
      runLane('document', DOCUMENT_CONCURRENCY),
      runLane('media', MEDIA_CONCURRENCY),
    ]).then(() => undefined).finally(() => { draining = null; });
    return draining;
  }

  async function addFiles(selected: File[]): Promise<void> {
    if (!selected.length || !hasRoom(selected.length)) return;
    for (const file of selected) {
      const id = newSourceId();
      files.set(id, file);
      items.value.push({
        id, label: file.name, kind: fileKind(file), state: 'queued',
        source: null, error: '', seconds: 0,
      });
    }
    await drain();
  }

  async function retry(id: string): Promise<void> {
    const item = items.value.find(entry => entry.id === id);
    if (!item || item.state !== 'failed') return;
    item.state = 'queued';
    await drain();
  }

  function remove(id: string): void {
    active.get(id)?.abort();
    files.delete(id);
    items.value = items.value.filter(item => item.id !== id);
    error.value = '';
  }

  function clear(): void {
    active.forEach(controller => controller.abort());
    files.clear();
    items.value = [];
    error.value = '';
  }

  onScopeDispose(clear);
  return { items, error, parsing, readySources, problem, addText, addFiles, retry, remove, clear };
});
