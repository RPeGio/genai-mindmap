import { defineStore } from 'pinia';
import { computed, onScopeDispose, ref } from 'vue';

import { uploadDocument } from '../api/client';
import type { Source, SourceKind } from '../api/types';
import { fileKind, MAX_AUDIO_BYTES, MAX_SOURCES, MAX_UPLOAD_BYTES, newSourceId, sourceInputError, textSource } from '../sources/input';

export interface SourceItem {
  /**
   * 这一行的本地身份：列表 key、`files`/`active` 的键，`remap` 永远不动它。
   *
   * 不能拿 `id` 兼任：两份内容相同的材料会被知识库收成同一份，映射之后
   * `id` 允许相同，但行必须还是两行独立可删的。
   */
  key: string;
  /** 材料在库里的标识；生成前是临时 id，入库后由 `remap` 换成持久 id。 */
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
  /**
   * 正在录音。
   *
   * 放在这里而不是某个组件里，因为**两个组件都要看它**：`RecordButton` 那边决定
   * 按钮形态，画布右上角的生成按钮要据此禁用——录音停下来的那一刻会新增一份材料，
   * 此刻发出去的请求里一定缺这一块。
   */
  const recording = ref(false);
  const files = new Map<string, File>();
  const active = new Map<string, AbortController>();
  let textNumber = 0;
  let draining: Promise<void> | null = null;

  const parsing = computed(() => items.value.some(item => item.state === 'queued' || item.state === 'parsing'));
  /**
   * 送去生成的材料，按 id 去重。
   *
   * 两份一模一样的材料会入库成同一条记录，`remap` 之后它们共享一个 id；
   * 请求体里重复两次没有意义（后端按 id 建索引），`sourceInputError` 还会
   * 判成"来源 ID 重复"把生成按钮锁死。
   */
  const readySources = computed(() => {
    const seen = new Set<string>();
    const sources: Source[] = [];
    for (const item of items.value) {
      if (item.state !== 'ready' || !item.source || seen.has(item.source.id)) continue;
      seen.add(item.source.id);
      sources.push(item.source);
    }
    return sources;
  });
  const problem = computed(() => {
    if (parsing.value) return '来源正在解析，请等待完成后生成。';
    if (items.value.some(item => item.state === 'failed')) return '有来源解析失败，请重试或移除后生成。';
    return sourceInputError(readySources.value);
  });
  /**
   * 材料作为**可选**输入时的闸门：一份都没有不是错误。
   *
   * `problem` 对空列表返回「请先添加至少一个来源」，那是"合并生成"时代的判据
   * （生成必须要来源）。现在材料可以不传，谁把 `problem` 直接摆到生成按钮上，
   * 用户就会看到一个永远禁用的按钮和一句要求他去加材料的话。
   */
  const materialProblem = computed(() => (items.value.length ? problem.value : ''));

  function hasRoom(count: number): boolean {
    error.value = '';
    if (items.value.length + count <= MAX_SOURCES) return true;
    error.value = `最多添加 ${MAX_SOURCES} 个来源，本次未添加，请减少数量后重试。`;
    return false;
  }

  function addText(text: string): boolean {
    if (!text.trim()) {
      // 这个入口加的是**材料**：说"请输入主题或文本"会让用户以为主题也该贴在这里。
      error.value = '粘贴的内容是空的，没有加进来源。';
      return false;
    }
    if (!hasRoom(1)) return false;
    const source = textSource(text);
    items.value.push({
      key: source.id, id: source.id, label: `文本 ${++textNumber}`, kind: 'text',
      state: 'ready', source, error: '', seconds: 0,
    });
    return true;
  }

  async function parseItem(item: SourceItem): Promise<void> {
    const file = files.get(item.key);
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
    active.set(item.key, controller);
    item.state = 'parsing';
    const started = Date.now();
    const timer = setInterval(() => { item.seconds = Math.floor((Date.now() - started) / 1000); }, 1000);
    try {
      const source = await uploadDocument(file, controller.signal);
      // 已移除/清空的条目不能被迟到的响应重新加回来。
      if (controller.signal.aborted || !files.has(item.key)) return;
      if (source.status !== 'parsed' || !source.text?.trim()) {
        throw new Error(source.error?.trim() || '这个来源没有解析出可用文字，请重试或移除。');
      }
      item.source = source;
      item.kind = source.kind;
      item.state = 'ready';
    } catch (cause) {
      if (controller.signal.aborted || !files.has(item.key)) return;
      item.state = 'failed';
      item.error = cause instanceof Error ? cause.message : '上传失败，请重试。';
    } finally {
      clearInterval(timer);
      active.delete(item.key);
    }
  }

  /**
   * 判泳道用本地文件的类型：item.kind 在解析成功后会改成后端返回的 kind。
   *
   * 只有图片 / 语音走并发泳道，其余（含文本）都归到串行泳道——宁可慢一点，
   * 也不让本地 CPU 的解析两路抢核。
   */
  function laneOf(item: SourceItem): Lane {
    const file = files.get(item.key);
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
          item.state === 'queued' && !skipped.has(item.key) && laneOf(item) === lane);
        if (!next) return;
        await parseItem(next);
        // 没有对应 File 的异常条目不会进入任何终态，不跳过的话同一个 find
        // 会在原地转圈。
        if (next.state === 'queued') skipped.add(next.key);
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
      const key = newSourceId();
      files.set(key, file);
      items.value.push({
        key, id: key, label: file.name, kind: fileKind(file), state: 'queued',
        source: null, error: '', seconds: 0,
      });
    }
    await drain();
  }

  async function retry(key: string): Promise<void> {
    const item = items.value.find(entry => entry.key === key);
    if (!item || item.state !== 'failed') return;
    item.state = 'queued';
    await drain();
  }

  /**
   * 按服务端给的对照表把来源 id 换成持久 id。
   *
   * 生成开始时后端会把材料写进知识库，同时用 `sources` 帧告诉前端"旧 id → 新 id"。
   * 只换 id：`item.source.id` 是导图引用与引用计数用的，`item.id` 是给用户看的
   * 材料标识。行身份是 `item.key`，`files`/`active` 都挂在它上面，这里一律不碰——
   * 两份内容相同的材料映射到同一个持久 id 时，两行才还能各自删除、各自解析。
   */
  function remap(ids: Record<string, string>): void {
    const entries = Object.entries(ids);
    if (!entries.length) return;
    for (const item of items.value) {
      const next = ids[item.id] ?? (item.source ? ids[item.source.id] : undefined);
      if (!next || next === item.id) continue;
      if (item.source) item.source = { ...item.source, id: next };
      item.id = next;
    }
  }

  function remove(key: string): void {
    active.get(key)?.abort();
    files.delete(key);
    items.value = items.value.filter(item => item.key !== key);
    error.value = '';
  }

  function clear(): void {
    active.forEach(controller => controller.abort());
    files.clear();
    items.value = [];
    error.value = '';
    recording.value = false;
  }

  onScopeDispose(clear);
  // `remap` 是本仓库加的：生成时后端把材料收进知识库并换了持久 id，前端要就地换掉
  // （见 `frontend/src/stores/mindmap.ts` 的 `case 'sources'`）。上游那段 `recording`
  // 与 `materialProblem` 一并保留，两边的字段都是界面在用的。
  return {
    items, error, parsing, recording, readySources, problem, materialProblem,
    addText, addFiles, retry, remove, clear, remap,
  };
});
