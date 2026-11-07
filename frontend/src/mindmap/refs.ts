/**
 * 节点引用的展示逻辑。纯函数，不碰任何 store：定位、摘录、命名都在这里算完，
 * 组件只负责摆位置。
 *
 * 两个前提决定了这里必须做降级：
 * 1. 原文不随导图保存（`source_usage` 只记了 kind / filename / char_count），
 *    从"已保存的导图"打开时手里只有 `quote`，没有 `Source.text`。
 * 2. 字符区间按 Unicode 码点算（后端契约如此），不是 UTF-16 下标；
 *    直接拿 `String.slice` 切会把 emoji 和部分汉字切在半个上。
 */
import type { Source, SourceRef, SourceUsage } from '../api/types';
import { SOURCE_LABELS } from '../sources/input';

export interface ResolvedRef {
  ref: SourceRef;
  /** 本次会话里同一份来源的原文；从已保存导图打开时为 null。 */
  source: Source | null;
  /** 摘录：`quote` 优先，没有就从原文按字符区间切。 */
  quote: string;
  /** 原文片段（带前后文）；没有原文时为空串。 */
  excerpt: string;
  /** `excerpt` 的三段切分，界面据此把命中部分包进 <mark>；没有原文时都是空串。 */
  before: string;
  hit: string;
  after: string;
  /** 高亮在原文里的码点区间；定位不到为 null。 */
  start: number | null;
  end: number | null;
  /** "第 3 页 · 第 120–168 字" 这类位置说明，单独一项都没有时为空串。 */
  where: string;
  /** 来源显示名，如"文档 · 线代第一章.pdf"。 */
  name: string;
}

/** UTF-16 下标 → 码点下标。 */
function codePointIndex(text: string, utf16Index: number): number {
  return Array.from(text.slice(0, utf16Index)).length;
}

/** 位置说明。页码与字符区间互不排斥，都有就都写。 */
export function refWhere(ref: SourceRef): string {
  const parts: string[] = [];
  if (ref.page != null) parts.push(`第 ${ref.page} 页`);
  if (ref.start != null && ref.end != null) parts.push(`第 ${ref.start + 1}–${ref.end} 字`);
  return parts.join(' · ');
}

/**
 * 在原文里定位引用。
 *
 * 优先用字符区间（后端给的就是它）；区间缺失或越界时退回到按 `quote` 查找，
 * 这样手工编辑过的引用仍有救。两样都对不上就返回 null——调用方显示摘录，
 * 但别假装能高亮。
 */
export function locateInText(
  text: string,
  ref: SourceRef,
): { start: number; end: number } | null {
  if (!text) return null;
  const chars = Array.from(text);

  if (ref.start != null && ref.end != null && ref.end > ref.start && ref.end <= chars.length) {
    return { start: ref.start, end: ref.end };
  }

  const quote = ref.quote?.trim();
  if (!quote) return null;

  const at = text.indexOf(quote);
  if (at >= 0) {
    const start = codePointIndex(text, at);
    return { start, end: start + Array.from(quote).length };
  }
  // 模型给的引文可能把换行压成了空格；按空白归一后再找一次。
  const collapsed = text.replace(/\s+/g, ' ');
  const flatQuote = quote.replace(/\s+/g, ' ');
  const flatAt = collapsed.indexOf(flatQuote);
  if (flatAt >= 0) {
    return { start: codePointIndex(collapsed, flatAt), end: codePointIndex(collapsed, flatAt + flatQuote.length) };
  }
  return null;
}

/** 取原文片段：高亮区间前后各留 `pad` 个字，截断处加省略号。 */
export function excerptOf(text: string, start: number, end: number, pad = 60): string {
  const { before, hit, after } = windowOf(text, start, end, pad);
  return `${before}${hit}${after}`;
}

/**
 * 同上的三段形式。
 *
 * 界面要在原文片段里把命中的那句标出来，只有一整串字符串就没法插 <mark>；
 * 在这里切好，组件只负责摆放，不重复算下标。
 */
export function windowOf(
  text: string,
  start: number,
  end: number,
  pad = 60,
): { before: string; hit: string; after: string } {
  const chars = Array.from(text);
  const from = Math.max(0, start - pad);
  const to = Math.min(chars.length, end + pad);
  return {
    before: `${from > 0 ? '…' : ''}${chars.slice(from, start).join('')}`,
    hit: chars.slice(start, end).join(''),
    after: `${chars.slice(end, to).join('')}${to < chars.length ? '…' : ''}`,
  };
}

/** 来源显示名：文件名优先，没有就用 kind 的中文名加序号。 */
export function refName(ref: SourceRef, usage: SourceUsage[]): string {
  const found = usage.find(item => item.source_id === ref.source_id);
  if (!found) return `来源 ${ref.source_id.slice(0, 8)}`;
  const label = SOURCE_LABELS[found.kind] ?? '来源';
  return found.filename ? `${label} · ${found.filename}` : label;
}

/** 把一条引用解析成"能直接画"的形状。 */
export function resolveRef(ref: SourceRef, sources: Source[], usage: SourceUsage[] = []): ResolvedRef {
  const source = sources.find(item => item.id === ref.source_id) ?? null;
  const text = source?.text ?? '';
  const located = text ? locateInText(text, ref) : null;
  const quote = ref.quote?.trim() || (located ? Array.from(text).slice(located.start, located.end).join('') : '');
  const window = located ? windowOf(text, located.start, located.end) : { before: '', hit: '', after: '' };

  return {
    ref,
    source,
    quote,
    excerpt: `${window.before}${window.hit}${window.after}`,
    before: window.before,
    hit: window.hit,
    after: window.after,
    start: located?.start ?? null,
    end: located?.end ?? null,
    where: refWhere(ref),
    name: refName(ref, usage),
  };
}

/** 批量解析；`refs` 为空时返回空数组，调用方不必先判空。 */
export function resolveRefs(
  refs: SourceRef[] | undefined,
  sources: Source[],
  usage: SourceUsage[] = [],
): ResolvedRef[] {
  return (refs ?? []).map(ref => resolveRef(ref, sources, usage));
}
