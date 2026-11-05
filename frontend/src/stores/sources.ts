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

  /** 串行解析，避免一次选中多个 PDF 时同时占满识别服务。失败只影响对应条目。 */
  function drain(): Promise<void> {
    if (draining) return draining;
    draining = (async () => {
      for (;;) {
        const next = items.value.find(item => item.state === 'queued');
        if (!next) break;
        await parseItem(next);
      }
    })().finally(() => { draining = null; });
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
