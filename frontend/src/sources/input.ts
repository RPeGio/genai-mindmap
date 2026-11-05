import type { Source, SourceKind } from '../api/types';

// 与 ingestion/router.py、pipeline/router.py 和 pipeline/service.py 保持一致。
export const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;
export const MAX_AUDIO_BYTES = 20 * 1024 * 1024;
export const MAX_SOURCES = 50;
export const MAX_INPUT_CHARS = 20000;
export const FILE_ACCEPT = '.pdf,.docx,.doc,.pptx,.ppt,.xlsx,.xls,.html,.htm,.md,.asciidoc,.adoc,.wav,.png,.jpg,.jpeg,.webp,.gif,.bmp';

export const SOURCE_LABELS: Record<SourceKind, string> = {
  text: '文本', markdown: 'Markdown', document: '文档', image: '图片', audio: '语音',
};

let sourceSequence = 0;

/** HTTP 局域网访问时 randomUUID 可能不可用，文本和文件输入仍应能使用。 */
export function newSourceId(): string {
  return globalThis.crypto?.randomUUID?.()
    ?? `source_${Date.now().toString(36)}_${++sourceSequence}_${Math.random().toString(36).slice(2)}`;
}

export function fileKind(file: File): SourceKind {
  const extension = file.name.split('.').pop()?.toLowerCase();
  if (extension === 'wav') return 'audio';
  if (['png', 'jpg', 'jpeg', 'webp', 'gif', 'bmp'].includes(extension ?? '')) return 'image';
  return 'document';
}

/** Python len 按 Unicode 码点计数；包含来源之间的两个换行，不能只累加正文。 */
export function mergedCharCount(sources: Source[]): number {
  return Array.from(sources.map(source => source.text?.trim() ?? '').join('\n\n')).length;
}

export function sourceInputError(sources: Source[]): string {
  if (!sources.length) return '请先添加至少一个来源。';
  if (sources.length > MAX_SOURCES) return `最多添加 ${MAX_SOURCES} 个来源，请移除部分来源。`;
  if (new Set(sources.map(source => source.id)).size !== sources.length) return '来源 ID 重复，请移除重复来源后重试。';
  if (sources.some(source => source.status !== 'parsed' || !source.text?.trim())) {
    return '有来源尚未解析成功或内容为空，请重试或移除后生成。';
  }
  const count = mergedCharCount(sources);
  return count > MAX_INPUT_CHARS
    ? `合并后内容为 ${count} 字，超过当前 ${MAX_INPUT_CHARS} 字限制；请缩短或分批处理。`
    : '';
}

export function textSource(text: string): Source {
  return {
    id: newSourceId(), kind: 'text', filename: null, text: text.trim(),
    span: null, status: 'parsed', error: null, parsed_pages: null,
    truncated: false, failed_pages: [],
  };
}

/** 每份来源单独提示缺页与截断，不能被随后加入的文件覆盖。 */
export function sourceWarning(source: Source): string {
  const parts: string[] = [];
  const failed = source.failed_pages ?? [];
  if (failed.length) {
    const shown = failed.slice(0, 5).map(page => `第 ${page} 页`).join('、');
    parts.push(`${shown}${failed.length > 5 ? ` 等 ${failed.length} 页` : ''} 识别失败，这些页未包含在来源正文中`);
  }
  if (source.truncated) {
    const total = source.span?.page;
    const parsed = source.parsed_pages;
    if (typeof total === 'number' && typeof parsed === 'number') {
      parts.push(failed.length
        ? `本文档共 ${total} 页，成功解析 ${parsed} 页，超出页数上限的尾部未处理`
        : `本文档共 ${total} 页，仅处理前 ${parsed} 页`);
    } else {
      parts.push('文档较长，本次只处理了一部分内容');
    }
  }
  return parts.join('；');
}
