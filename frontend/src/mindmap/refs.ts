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
import type { MindNode, Source, SourceRef, SourceUsage } from '../api/types';
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
  /**
   * 原文地址：非空时界面把它渲染成可点开的链接。
   *
   * 外部检索回来的来源（维基条目、arXiv 论文）才有这个值。没有它的引用仍然只能
   * 看摘录——"这句话出自哪篇论文"必须能自己核对，否则"自动填充或验证节点内容"
   * 只闭环了一半。
   */
  url: string | null;
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

/**
 * 来源显示名：文件名优先，没有就用 kind 的中文名加序号。
 *
 * 三级退让，顺序有理由：
 *   1. `source_usage`——导图自己声明的来源清单，保存后重开也还在；
 *   2. 本次会话里那份 `Source`——外部检索刚抓回来的来源可能还没进 `source_usage`
 *      （比如生成失败但引用已经在树上），这时文件名比 `来源 s1a2b3c` 有用得多；
 *   3. 只有 id 时退回 `来源 <前 8 位>`：不给一个看起来像名字的假名字。
 */
export function refName(
  ref: SourceRef | string,
  usage: SourceUsage[],
  source: Source | null = null,
): string {
  const sourceId = typeof ref === 'string' ? ref : ref.source_id;
  const found = usage.find(item => item.source_id === sourceId);
  if (found) {
    const label = SOURCE_LABELS[found.kind] ?? '来源';
    return found.filename ? `${label} · ${found.filename}` : label;
  }
  if (source) {
    const label = SOURCE_LABELS[source.kind] ?? '来源';
    return source.filename ? `${label} · ${source.filename}` : label;
  }
  return `来源 ${sourceId.slice(0, 8)}`;
}

/**
 * 引用能不能点回原文。
 *
 * 两个来源依次退让：本次会话里那份来源的 `metadata.url`（最完整），然后是摘要
 * （`source_usage`）里的 `url`——保存过的旧图不带 `Source`，只把它留在 `metadata`
 * 上等于"这次能点、下次打开就没了"。
 *
 * 再判一次 http/https：后端契约已经过滤过一遍，但手写或导入的导图不经过那一道，
 * 而这个值会被直接塞进 `<a href>`。
 */
export function refUrl(
  ref: SourceRef,
  source: Source | null,
  usage: SourceUsage[],
): string | null {
  const fromSource = source?.metadata?.url ?? null;
  const candidate = fromSource
    ?? usage.find(item => item.source_id === ref.source_id)?.url
    ?? null;
  if (!candidate) return null;
  return /^https?:\/\//i.test(candidate) ? candidate : null;
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
    name: refName(ref, usage, source),
    url: refUrl(ref, source, usage),
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

/** 一条"这个节点引用了这份来源"的记录。 */
export interface RefHit {
  /** 引用它的节点 id。 */
  id: string;
  /** 节点标题（未截断）。 */
  label: string;
  /** 从根到该节点的标题路径（含该节点），与详情面板的面包屑同口径。 */
  path: string[];
}

export interface RefIndex {
  /** source_id → 引用它的节点数。同一节点重复引用同一来源只算一次。 */
  counts: Record<string, number>;
  /** source_id → 引用它的节点，按先序排列（列表展开的顺序要稳定）。 */
  hits: Record<string, RefHit[]>;
}

/**
 * 反向索引：从"来源"看"哪些节点引用了它"。
 *
 * 正向链路（节点 → 引用）已经在详情面板里了；这里补反向，是因为用户读材料时
 * 更关心"这段话被用到哪去了"。按先序遍历建，列表顺序与大纲顺序一致。
 *
 * 第一参数吃**一个根或一组根**：鱼骨与流程是多棵并列子树（`shape.rootsOf` 给的就是
 * 数组），而每条路径的起点是**它自己那棵子树的根**——一根骨里的引用不该在路径上
 * 出现另一根骨的内容（与后端 `path_to` 同一条口径）。树那边传一个根，行为与以前
 * 逐字节相同。
 */
export function buildRefIndex(root: MindNode | readonly MindNode[] | null): RefIndex {
  const index: RefIndex = { counts: {}, hits: {} };
  if (!root) return index;
  const roots: readonly MindNode[] = Array.isArray(root) ? root : [root as MindNode];

  const walk = (node: MindNode, path: string[]) => {
    const trail = [...path, node.label];
    const seen = new Set<string>();
    for (const ref of node.source_refs ?? []) {
      if (!ref.source_id || seen.has(ref.source_id)) continue;
      seen.add(ref.source_id);
      (index.hits[ref.source_id] ??= []).push({ id: node.id, label: node.label, path: trail });
      index.counts[ref.source_id] = (index.counts[ref.source_id] ?? 0) + 1;
    }
    for (const child of node.children) walk(child, trail);
  };

  for (const item of roots) walk(item, []);
  return index;
}
