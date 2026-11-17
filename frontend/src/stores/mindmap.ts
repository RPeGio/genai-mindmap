import type { Edge as FlowEdge, Node as FlowNode } from '@vue-flow/core';
import { defineStore } from 'pinia';
import { computed, ref } from 'vue';

import { ApiError, createMindMap, getMindMap, getRun, listMindMaps, listRuns, markRunResolved, runAgentStream, updateMindMap } from '../api/client';
import type { AgentEvent, AgentRunRequest, DiagramTypeChoice, EdgeKind, MapRecord, MindEdge, MindMapContent, MindNode, NodeColor, OpBatch, RunSummary, Source, SourceRef } from '../api/types';
import { directionOf, withDirection } from '../mindmap/direction';
import { applyOps } from '../mindmap/ops';
import { buildRefIndex } from '../mindmap/refs';
import { sourceInputError } from '../sources/input';
import { useSourcesStore } from './sources';
import { layoutTidy, childSide, NODE_SIZE, type ChildSide, type Direction, type LayoutResult } from '../canvas/layout/tidy';
import {
  addChild,
  addEdge as addEdgeToContent,
  addSibling,
  charLength,
  countNodes,
  edgesOf,
  findNode,
  findParent,
  LIMITS,
  removeEdge as removeEdgeFromContent,
  removeNode,
  renameNode,
  setNodeNote,
  setNodeVisual,
  subtreeIds,
  validateDraft,
} from '../mindmap/edits';

/** 画布节点上最多显示的字数（按码点算，避免把 emoji 切成半个）。 */
export const TRUNCATE_CHARS = 24;

/** 撤销栈上限。整树快照一步几 KB，50 步封顶，内存与手感都够用。 */
export const HISTORY_LIMIT = 50;/**
 * 撤销栈的总字节上限。
 *
 * 只有步数上限时，节点上限从 300 提到 1000 之后会失真：一份快照就能到几十 KB，
 * 50 步最坏能压到几十 MB。按字节封顶比调小步数更贴近真实内存占用，也不用牺牲
 * "深树也能退很多步"。
 */
export const HISTORY_BYTES_LIMIT = 10 * 1024 * 1024;

/** 保存的结果分类：界面据此决定提示什么，而不是把所有失败都说成"保存失败"。 */
export type SaveResult = 'saved' | 'conflict' | 'invalid' | 'empty' | 'busy' | 'failed';

/** 节点上显示的短标题：按码点截断，超出加省略号。
 *
 * 生成侧现在把 `label` 限制在 16 字以内（契约 `GENERATED_LABEL_MAX_CHARS`），所以
 * 这个截断**对新图不再生效**——它是给旧图留的兜底：库里那些 20–30 字的长标题必须
 * 照常画得下，不能被"我们改好了生成侧"这件事连累。
 */
export function displayTitle(node: MindNode): string {
  const chars = Array.from(node.label.trim());
  return chars.length > TRUNCATE_CHARS ? `${chars.slice(0, TRUNCATE_CHARS).join('')}…` : chars.join('');
}

/**
 * 详情面板 / 悬停卡片里的正文：`detail → note → label`。
 *
 * 三段的语义完全不同，所以回退顺序不能调：
 *   - `detail`：这次生成产出的说明（模型写的，2–3 句）；
 *   - `note`：用户自己写的备注（生成侧永远不碰它）；
 *   - `label`：都没有时的兜底——旧图（v1/v2 早期）只有标题，那时"完整内容"
 *     就是标题本身。
 *
 * 回退顺序写反（比如 `note` 优先）的后果是：用户给一个已有详情的节点随手写了句
 * 备注，画布与导出就只剩那句备注了——而 detail 还在数据里，只是没人显示。
 */
export function displayBody(node: MindNode): string {
  const detail = node.detail?.trim();
  if (detail) return detail;
  const note = node.note?.trim();
  return note && note.length > 0 ? note : node.label.trim();
}

/** 正文本当前来自哪一段。详情面板据此给出"这一段是什么"的说明。 */
export type BodySource = 'detail' | 'note' | 'label';

export function bodySource(node: MindNode): BodySource {
  if (node.detail?.trim()) return 'detail';
  if (node.note?.trim()) return 'note';
  return 'label';
}

/**
 * 候选相对当前画布的一条改动。
 *
 * 存在的理由：`edit` / `expand` 意图的候选是"在当前树上的改动"，而**画布优先渲染
 * `content`**（用户的作品），候选节点在采纳之前根本不在画布上。没有这份清单，
 * 用户面对「候选已就绪」只能盲点「应用」——他不知道 agent 动了哪几个节点、动了什么。
 */
export interface CandidateChange {
  /**
   * 改动种类。前四种是**节点**：`added` / `removed` / `updated` / `moved`；
   * 后两种是**附加边**（`edge_added` / `edge_removed`）。
   *
   * 边单独两种而不是复用 `added` / `removed`：`id` 的语义不同（边 id vs 节点 id），
   * 而清单上要能一眼分清"这张图多了几个节点"与"多了几条连线"——前者是要看内容
   * 的地方，后者是要看流程走向的地方。混在一起时，一条"新增关系"会被读成
   * "新增节点"，用户点它跳不到任何地方。
   */
  kind: 'added' | 'removed' | 'updated' | 'moved' | 'edge_added' | 'edge_removed';
  /** 节点 id（前四种）或边 id（后两种）。 */
  id: string;
  /** 显示用标题：更新/移动取**新**标题，删除取原标题。边则显示「A → B（是）」。 */
  label: string;
  /** 一句话说明改了什么（"标题 → 新标题"、"颜色 blue→red"、"移到「甲」下"）。 */
  detail: string;
  /**
   * 这条改动**碰到了哪几个节点**（节点改动是一个，边改动是两个端点）。
   *
   * 它只服务一件事：判断这条改动落没落在用户当时的选区里。用 `id` 判不行——
   * 边的 `id` 是边 id，与节点 id 是两套命名空间；`added` 的 `id` 是一个新 id，
   * 在基线树里根本查不到，而它确实长在用户选中的那棵子树下面。
   */
  nodes: string[];
}

/**
 * 带上「在不在选区里」的改动条目（`store.candidateDiff` 的返回值）。
 *
 * 为什么不把 `inSelection` 直接塞进 `CandidateChange`：那两个纯函数
 * （`diffCandidate` / `diffEdges`）回答的是"两棵树哪里不一样"，它们**看不到**
 * 选区——把布尔字段放进去就必须先填一个假值，而"假值"与"算出来是 false"
 * 在类型上分不开。用一个派生类型把两件事分开：谁需要范围，谁再算一层。
 */
export interface ScopedChange extends CandidateChange {
  /**
   * 是否落在「这次改图」带上去的选区里（见 `store.candidateSelection`）。
   *
   * 没有选区时恒为 `false`：那时"范围内/外"不成立，界面也不显示这个标记
   * （`candidateSelection` 为空判断）。刻意用布尔而不是三态 `null`——清单每一行
   * 都要渲染，三态会让每个读它的地方都写一遍 `=== false` 的分支。
   */
  inSelection: boolean;
}

/** 一棵树的索引：id → 节点与父 id。前序遍历，`parentId` 为 null 的是根。 */
function indexTree(root: MindNode): Map<string, { node: MindNode; parentId: string | null }> {
  const out = new Map<string, { node: MindNode; parentId: string | null }>();
  const walk = (node: MindNode, parentId: string | null) => {
    out.set(node.id, { node, parentId });
    for (const child of node.children) walk(child, node.id);
  };
  walk(root, null);
  return out;
}

/** 一个节点上"人看得懂的变化"。没有变化返回空数组。 */
function nodeFieldChanges(before: MindNode, after: MindNode): string[] {
  const parts: string[] = [];
  if (before.label.trim() !== after.label.trim()) {
    parts.push(`标题「${before.label.trim()}」→「${after.label.trim()}」`);
  }
  if ((before.detail ?? '').trim() !== (after.detail ?? '').trim()) {
    parts.push(after.detail?.trim() ? '改了详情' : '清空了详情');
  }
  if ((before.color ?? null) !== (after.color ?? null)) {
    parts.push(`颜色 ${before.color ?? '无'} → ${after.color ?? '无'}`);
  }
  if ((before.icon ?? null) !== (after.icon ?? null)) {
    parts.push(`图标 ${before.icon ?? '无'} → ${after.icon ?? '无'}`);
  }
  // `note` 归用户，生成侧碰不到它；这里仍然比一次，是为了让"某条路径改到了用户
  // 备注"这种 bug 在界面上直接看得见，而不是等着用户发现自己的字变了。
  if ((before.note ?? '') !== (after.note ?? '')) {
    parts.push('改了「我的备注」');
  }
  if ((before.source_refs?.length ?? 0) !== (after.source_refs?.length ?? 0)) {
    parts.push(`引用 ${before.source_refs?.length ?? 0} → ${after.source_refs?.length ?? 0} 条`);
  }
  return parts;
}

/**
 * 当前画布与候选之间的差异；**没有可比基线时返回 `null`**（`create` 意图）。
 *
 * 为什么用"两份树对比"而不是"记录收到的 op 批次"：
 *
 *  - op 帧可能丢、可能乱序（`applyOpsToCandidate` 会主动丢弃对不上的批次），
 *    按 op 记账得到的是"我以为改了什么"，而对比得到的是"树到底哪里不一样"；
 *  - `resumeRun` 从服务端捡回候选时一条 op 帧都没有，那时只有对比能给出差异；
 *  - 对比不依赖协议：后端将来加一种下发方式，这里一行都不用改。
 *
 * `create` 返回 `null` 而不是"全部节点都是新增"：一张 80 节点的图会得到 80 条
 * "新增"，那不是 diff，是把整棵树重念一遍。
 */
export function diffCandidate(base: MindNode | null, next: MindNode): CandidateChange[] | null {
  if (!base) return null;
  const before = indexTree(base);
  const after = indexTree(next);
  const changes: CandidateChange[] = [];

  for (const [id, entry] of after) {
    const old = before.get(id);
    if (!old) {
      changes.push({
        kind: 'added', id, label: entry.node.label.trim(), detail: '新增节点', nodes: [id],
      });
      continue;
    }
    const fields = nodeFieldChanges(old.node, entry.node);
    if (fields.length) {
      changes.push({
        kind: 'updated', id, label: entry.node.label.trim(), detail: fields.join('；'), nodes: [id],
      });
    }
    if (old.parentId !== entry.parentId) {
      const target = entry.parentId ? after.get(entry.parentId)?.node.label.trim() ?? entry.parentId : '根';
      // 移动碰到两个节点：被移动的那个，以及它的新父节点。只记前者的话，
      // "把这一支挂到你选中的节点下面"会被标成范围外——而它正是选区边缘的改动。
      changes.push({
        kind: 'moved', id, label: entry.node.label.trim(), detail: `移到「${target}」下`,
        nodes: entry.parentId ? [id, entry.parentId] : [id],
      });
    }
  }
  for (const [id, entry] of before) {
    if (!after.has(id)) {
      changes.push({
        kind: 'removed', id, label: entry.node.label.trim(), detail: '删除节点（含子树）', nodes: [id],
      });
    }
  }
  return changes;
}

/**
 * 两份**边集合**之间的差异（附加关系：条件分支 / 汇合 / 回退）。
 *
 * 与 `diffCandidate`（对比两份树）并列而不是合并进去：树对比走的是"前序遍历 +
 * 父 id"，边对比走的是"按 id 对齐的集合差"，两种算法的输入形状不同，硬塞进一个
 * 函数只会让两边都长出分支。调用方（`candidateDiff`）把两份结果拼起来。
 *
 * `nameOf` 把节点 id 换成标题：清单上写「形式审查 → 付款（是）」比写两个 32 位
 * hex 有用得多，而边本身只有 id。
 *
 * `kind` 不同的同 id 边按"改过"上报（`edge_added`），不新增第三种 kind：画布上
 * 它就是同一条线换了画法，用户要看的也是"这条线变了"。
 */
export function diffEdges(
  base: readonly MindEdge[],
  next: readonly MindEdge[],
  nameOf: (id: string) => string,
): CandidateChange[] {
  const describe = (edge: MindEdge): string => {
    const text = `${nameOf(edge.source)} → ${nameOf(edge.target)}`;
    return edge.label ? `${text}（${edge.label}）` : text;
  };
  const before = new Map(base.map(edge => [edge.id, edge]));
  const changes: CandidateChange[] = [];

  for (const edge of next) {
    const old = before.get(edge.id);
    if (!old) {
      changes.push({
        kind: 'edge_added', id: edge.id, label: describe(edge),
        detail: edge.kind === 'loop' ? '新增回退线' : edge.kind === 'branch' ? '新增条件分支' : '新增连线',
        nodes: [edge.source, edge.target],
      });
      continue;
    }
    if ((old.label ?? null) !== (edge.label ?? null) || (old.kind ?? null) !== (edge.kind ?? null)) {
      changes.push({
        kind: 'edge_added', id: edge.id, label: describe(edge),
        detail: `关系说明改为「${edge.label ?? edge.kind ?? '无'}」`,
        nodes: [edge.source, edge.target],
      });
    }
  }
  for (const [id, edge] of before) {
    if (!next.some(item => item.id === id)) {
      changes.push({
        kind: 'edge_removed', id, label: describe(edge), detail: '删除了一条附加关系',
        nodes: [edge.source, edge.target],
      });
    }
  }
  return changes;
}

/**
 * 选区覆盖到的节点 id 集合：用户选中的每个节点在**两棵树**里的整棵子树。
 *
 * 两棵都要算，各自补上另一种算法看不见的那一半：
 *
 *  - **候选树**：在选中节点下面新增的节点 id 在基线树里根本不存在，只在基线算
 *    的话，最典型的"选一支、让它展开出三个子节点"会整片被标成范围外；
 *  - **基线树**：被删掉的节点在候选树里已经不存在，只在候选算的话，"把选中的
 *    这一支删掉"本身会被标成范围外——那正是用户圈它的原因。
 *
 * 空选区返回空集（`withinScope` 据此一律判 false，界面也不显示这个标记）。
 */
function selectionScope(
  before: MindNode | null, next: MindNode, ids: readonly string[],
): Set<string> {
  const scope = new Set<string>();
  if (ids.length === 0) return scope;
  for (const root of [before, next]) {
    if (!root) continue;
    for (const id of ids) {
      const hit = findNode(root, id);
      if (!hit) continue;
      for (const owned of subtreeIds(hit)) scope.add(owned);
    }
  }
  return scope;
}

/**
 * 一条改动算不算"在选中的范围内"：它碰到的**每个**节点都在选区子树里。
 *
 * 用"每个"而不是"任意一个"：一条从选中节点连到别处的边，改动确实有一半在选区
 * 里，但用户需要知道的是"它伸出去了"——按"任意"判就会把这种跨界的改动混进
 * 范围内那一堆，而那正是这次要给用户看见的东西。
 */
function withinScope(change: CandidateChange, scope: Set<string>): boolean {
  if (scope.size === 0 || change.nodes.length === 0) return false;
  return change.nodes.every(id => scope.has(id));
}

/**
 * agent 工具名 → 用户读得懂的说法。
 *
 * 映射只此一份：面板（要带耗时）与状态行（不带）都从这里取。两处各写一份的话，
 * 同一件事迟早出现两种措辞，用户在两个位置读到不同的词。
 */
const AGENT_TOOL_LABELS: Record<string, { running: string; done: string }> = {
  mindmap_generate: { running: '正在定框架', done: '已定好框架' },
  node_expand: { running: '正在展开分支', done: '已展开分支' },
  rag_query: { running: '正在查资料', done: '已查好资料' },
  mindmap_edit: { running: '正在改结构', done: '已改好结构' },
  // 四个细粒度工具。`edit` 意图下模型可能连调几次（一次改一处），所以文案要能
  // 让人看出"它在逐个改"，而不是以为同一件事被重复执行了。
  add_node: { running: '正在加节点', done: '已加节点' },
  update_node: { running: '正在改节点', done: '已改节点' },
  remove_node: { running: '正在删节点', done: '已删节点' },
  move_node: { running: '正在移动节点', done: '已移动节点' },
  // 两条边工具。文案要让人看出"它在连线"，而不是"它在改结构"——连线不改层级，
  // 用户盯着画布时看到的是"多了一个箭头"，不是"某个节点换了位置"。
  add_edge: { running: '正在连关系', done: '已连关系' },
  remove_edge: { running: '正在删关系', done: '已删关系' },
};

/**
 * 工具进度的人话文案。
 *
 * 未知工具名原样返回，不硬翻：模型新接一个工具时，界面出现英文也比出现一个
 * 编出来的中文词强——至少排查时能直接对上后端日志。
 */
export function agentToolLabel(name: string, phase: 'running' | 'done'): string {
  const pair = AGENT_TOOL_LABELS[name];
  if (!pair) return name;
  return phase === 'running' ? pair.running : pair.done;
}

/**
 * 一次 agent 运行里后端报出来的告警。
 *
 * 分片生成可能返回 `ok=true` 但结果不完整：`result_summary.partial` 为真，
 * 或带一串 `diagnostics`（BRANCH_FAILED / BRANCH_SKIPPED_BUDGET / DUPLICATE_POINT…）。
 * 只看 ok 的话，「3 个分支失败」和「全部成功」在界面上长得一模一样，
 * 用户点了接受才发现树缺一块。两种信号都收进同一个列表。
 */
export interface AgentWarning {
  /** partial=分片没跑完；diagnostic=后端给的某条具体诊断 */
  kind: 'partial' | 'diagnostic';
  /** 诊断码（如 BRANCH_FAILED）；partial 项固定为 PARTIAL */
  code: string;
  message: string;
}

/** 告警上限：面板只显示前几条并折叠，这里防的是后端一路刷屏。 */
const AGENT_WARNING_LIMIT = 20;

/**
 * 预览树里一个节点。
 *
 * 没有 id：`node_grow` 那一侧的事件里本来就没有 id（展开调用的响应只有 label
 * 和 expandable），位置全靠路径。为了渲染顺手编一个假 id 没有意义——它一旦
 * 混进 content 就会变成一个后端不认识的节点。
 */
export interface PreviewNode {
  label: string;
  /** 模型写出的可展开性；null 表示还没写到，界面不显示任何标记。 */
  expandable: boolean | null;
  children: PreviewNode[];
}

/**
 * 预览节点 id 的前缀。
 *
 * 预览要进布局就得有 id（`layoutTidy` 用它做 memo 的键，扁平化用它建父子映射），
 * 但 `node_grow` 帧里根本没有 id。前缀把派生 id 和真实 id 分开：真实 id 由模型或
 * 后端给出，将来两者若出现在同一份数据里，靠前缀就能一眼认出哪些不可编辑。
 */
export const PREVIEW_ID_PREFIX = 'preview-';

/**
 * 预览节点的派生 id：根是 `preview-root`，其余按「从根出发的子节点索引链」拼。
 *
 * 不用递增计数器：帧是增量到达的，计数器会随插入顺序变化，同一棵树两次渲染就
 * 会拿到不同的 id，布局缓存与 Vue 的 key 一起抖（节点闪一下、视图跳一下）。路径只
 * 取决于节点在树里的位置，同一棵树永远得到同一套 id。
 */
export function previewNodeId(path: readonly number[]): string {
  return path.length === 0 ? `${PREVIEW_ID_PREFIX}root` : `${PREVIEW_ID_PREFIX}${path.join('-')}`;
}

/**
 * 预览树 → 布局能直接吃的 `MindNode` 树，顺手把可展开性收进 `expandable`。
 *
 * `note` / `source_refs` 一律不填：预览帧里没有正文与引用，编一份假的会让悬停卡片
 * 和详情面板显示不存在的出处。两者都是契约里的可选字段，缺省时 `displayBody()`
 * 回退到 `label`——预览节点显示的永远是它自己的文字。
 */
function previewSubtree(
  node: PreviewNode,
  path: number[],
  expandable: Map<string, boolean | null>,
): MindNode {
  const id = previewNodeId(path);
  expandable.set(id, node.expandable);
  return {
    id,
    label: node.label,
    children: node.children.map((child, index) =>
      previewSubtree(child, [...path, index], expandable)),
  };
}

/**
 * 等待父节点的帧最多攒几条。
 *
 * 正常运行时并发展开的 slot 只有个位数；攒到几百条只可能是父节点的帧一直没来
 * （丢帧或后端换了语义），这时继续攒只是把内存和重放成本一起拖长。
 */
const PREVIEW_PENDING_LIMIT = 200;

/**
 * 从 `tool_end` 的 `result_summary` 里摘出"这次结果不完整"的告警。
 *
 * `result_summary` 是后端 `ToolResult.data` 原样透传，键形态不走类型契约
 * （`diagnostics` 在 types.ts 里就是 unknown[]），所以这里逐字段收窄，
 * 不假设形状——模型工具换一版数据结构时，面板最多少显示几条，不会崩。
 */
function agentWarningsFrom(summary?: Record<string, unknown>): AgentWarning[] {
  if (!summary) return [];
  const out: AgentWarning[] = [];
  if (summary.partial === true) {
    out.push({
      kind: 'partial',
      code: 'PARTIAL',
      message: '有分支没能生成完整，候选可能缺内容。',
    });
  }
  const raw = summary.diagnostics;
  if (Array.isArray(raw)) {
    for (const item of raw) {
      if (!item || typeof item !== 'object') continue;
      const entry = item as Record<string, unknown>;
      const message = typeof entry.message === 'string' ? entry.message.trim() : '';
      if (!message) continue;
      out.push({
        kind: 'diagnostic',
        code: typeof entry.code === 'string' ? entry.code : '',
        message,
      });
    }
  }
  return out;
}

export interface MindNodeData {
  /** 截断后的短标题，画在节点框里 */
  label: string;
  /**
   * 悬停卡片里的正文：`detail → note → label`（见 `displayBody`）。
   *
   * 画布上**只画 `label`**，正文不再往节点框里挤——这正是"画布上短、点开看长文"
   * 的那条分界。`body` 只服务悬停卡片（用户没点开时的快速预览）。
   */
  body: string;
  /** 完整 label（不截断），供 title 属性与无障碍读屏使用 */
  fullLabel: string;
  /** 正文来自哪一段（detail / note / label）。卡片据此决定要不要标"我的备注"。 */
  bodySource?: BodySource;
  depth: number;
  /** 需要渲染的源句柄：子节点分布在哪几侧就要哪几个（双向布局的根两侧都有） */
  out: ChildSide[];
  /** 需要渲染的目标句柄：连线从父节点进来时落在哪一侧 */
  in: ChildSide[];
  /** 这个节点有几条来源引用；画布角标用，0 或缺省都不显示角标 */
  refs?: number;
  /**
   * 这一帧画的是还没进画布的树：`preview` 是生成中，`candidate` 是已就绪但未应用。
   * 缺省表示它就在 `content` 里，也就是用户的作品。画布据此弱化样式。
   */
  draft?: 'preview' | 'candidate';
  /** 预览里模型明确标了「不用再展开」的节点，再弱一档；`null` 与 `true` 都不额外标记。 */
  previewLeaf?: boolean;
  /**
   * 语义色键（`blue` / `green` / …）。渲染层把它映射到 CSS 变量，色值不在数据里。
   * 缺省 `undefined` 表示"没有语义色"，节点保持原来的中性样式。
   */
  color?: NodeColor;
  /** 图标（目前是 emoji）。画在 label 前面。 */
  icon?: string;
  /**
   * 这个节点在待确认的候选里被改过（新增除外——新增的节点在画布上还没有位置）。
   *
   * 只对 `edit` / `expand` 意图有意义：那两个意图的候选是"在当前树上的改动"，
   * 而画布优先渲染 `content`（用户的作品），所以必须另给一个视觉标记，否则
   * 用户点了「应用」才发现哪里变了。
   */
  changed?: boolean;
}

export interface FlatNode {
  node: MindNode;
  depth: number;
  parentId: string | null;
}

/**
 * `runGeneration` 的可选参数。
 *
 * 只有一个字段也走对象，不再加第六个位置参数：那个签名已经有五个位置参数了，
 * 第六个数组读代码时得数一数才知道对应哪个。
 */
export interface RunOptions {
  /**
   * 这次运行**显式**指定的选区，覆盖 store 里当前的选中。
   *
   * 存在的理由只有一条：**看得见的才发**。画布右上角那个框旁边挂着「已选 N 个
   * 节点」徽标，它按徽标上的数把选区带上；而将来任何不显示选区的入口（快捷动作、
   * 测试直调）必须显式传 `[]`——否则它会静默带着用户在画布上圈的节点去"重新生成"，
   * 而用户看不到这件事正在发生。
   */
  selection?: string[];
  /**
   * 这一次运行**实际带着哪些偏好**（`["diagram_type.default=fishbone"]`）。
   *
   * 只由界面那层填：只有它知道"下拉里这个值是偏好预填的、用户没动过"。`store`
   * 不自己算——它看不出一个值和它的来源，把推断写在这里就会和界面各算一份。
   *
   * 落进 `runs.request.prefs_applied`，是偏好面板上"上次生效"的唯一数据源。
   */
  prefsApplied?: string[];
}

/**
 * 单次请求最多带几个选中 id。
 *
 * **与后端 `AgentRunRequest.selected_ids` 的 `max_length=50` 同一个数**，两处一个
 * 值：前端先截断，用户就不用为"按着 Ctrl 点了一整片"收一条 422。改这里时后端
 * 那个字段要一起看，反过来也一样。
 */
const MAX_REQUEST_SELECTION = 50;

/**
 * 画布节点。Vue Flow 的 `Node` 输入类型里没有 `selected`（它把选中当内部状态），
 * 但 `:nodes` 变化时 applyChanges 会把 `selected` 同步进去，所以这里补上这个字段的类型。
 */
export type MindFlowNode = FlowNode<MindNodeData> & { selected: boolean };

/**
 * 画布当前渲染的是哪棵树。
 *
 * 三级优先级：已应用的 `content` → 已就绪但未应用的 `candidate` → 生长中的
 * `agentPreview`。前两级之外的一切（保存、导出、撤销、编辑、采纳）仍然只认
 * `content`——渲染源只决定"画什么"，不决定"什么是用户的作品"。
 */
export type RenderKind = 'content' | 'candidate' | 'preview';

interface RenderSource {
  root: MindNode;
  kind: RenderKind;
  /** 预览节点的可展开性（派生 id → expandable）；非预览时是空表。 */
  expandable: Map<string, boolean | null>;
  /**
   * 附加边。**只画不参与布局**：节点坐标仍然由 `layoutTidy(root)` 决定。
   *
   * 这是"树 + 边"能成立的前提之一——边要是参与布局，用户每连一条线，整张画布
   * 都会重排，他刚记住的位置就没了。预览（生成中）没有边，是空数组。
   */
  edges: MindEdge[];
}

/** 共用的空表：非预览来源不需要这份映射，别每次重算都新建一个。 */
const NO_EXPANDABLE = new Map<string, boolean | null>();

function flatten(root: MindNode): FlatNode[] {
  const out: FlatNode[] = [];
  const walk = (node: MindNode, depth: number, parentId: string | null) => {
    out.push({ node, depth, parentId });
    for (const child of node.children) walk(child, depth + 1, node.id);
  };
  walk(root, 0, null);
  return out;
}

/**
 * 还没跑过任何一轮时的状态行。
 *
 * 导出成常量是为了让界面能判断"这条状态是不是还没有内容"：画布右上角那个框在
 * 初始状态下已经有一行提示了，再摆一句"写一个主题后生成"就是在同一个框里说两遍
 * 同一件事。
 */
export const INITIAL_STATUS = '写一个主题后生成，节点会边生成边出现。';

export const useMindMapStore = defineStore('mindmap', () => {
  // 只为了在收到 `sources` 帧时把来源 id 换成持久 id（见 runGeneration）。
  // 依赖方向是单向的：sources 不认识 mindmap，反过来才会。
  const sourcesStore = useSourcesStore();
  const content = ref<MindMapContent | null>(null);
  /**
   * 这张图是怎么来的。顶栏那一小块徽标按它换文案。
   *
   * `import` 是新增的一档：导入进来的图既不是示例（`demo`），也不是模型生成的
   * （`model`），更不是"已保存导图"（`saved`，那个意味着从库里打开、有 mapId）。
   * 它**没有 mapId**、`dirty` 为真——第一次保存会创建一条新记录。混进任何一档
   * 都会在界面上说错话：「已保存导图」会让人以为导入的内容已经在库里了。
   */
  const mode = ref<'demo' | 'model' | 'saved' | 'import' | null>(null);
  const status = ref(INITIAL_STATUS);
  const stageText = ref('');
  const waitedMs = ref(0);
  const busy = ref(false);
  const selectedId = ref<string | null>(null);
  /**
   * 追加选区（主选区之外的那几个）。
   *
   * 为什么要主 + 追加，而不是一个 `string[]`：详情面板、面包屑、Tab/Enter/Delete
   * 快捷键、`expand` 请求全都只认**一个**节点，它们区分不了"哪个是我正在看的那
   * 一个"。把主选区继续放在 `selectedId` 上，那些路径一行都不用改；多出来的这层
   * 只服务一件事——「改这张图」那句话是对谁说的（`selectedIds`）。
   *
   * 必须是列表而不是单个"上一个选中"：用户圈三个节点说「这几个合并成一个」时，
   * `selected_ids` 要发三条，模型才不用从整棵树里猜是哪三个。
   */
  const extraSelectedIds = ref<string[]>([]);
  /**
   * 朝向是 content 的派生值（layout + theme.direction），不单独存一份：
   * 否则保存后再打开，画布上的朝向与库里的记录会各说各话。
   */
  const direction = computed<Direction>(() => directionOf(content.value));
  /** 用户手动平移/缩放后不再自动 fitView，否则每次节点增长都会把视图拽回去。 */
  const viewAdjusted = ref(false);

  /** 正在内联编辑的节点 id；null 表示没有编辑框打开。 */
  const editingId = ref<string | null>(null);

  /**
   * 标题输入框里还没提交的文字。
   *
   * 标题走 `:value` + `@change`，用户打字时 DOM 的 value 变了但 content 没变，
   * 也碰不到 editingId。没有这个草稿，上传生成完成后标题会被直接换掉，
   * 那半截输入一声不响就没了。
   */
  const titleDraft = ref<string | null>(null);

  /** 保存相关状态：mapId 为空表示这份导图还没存过。 */
  const mapId = ref<string | null>(null);
  const version = ref(0);
  const savedAt = ref<string | null>(null);
  const dirty = ref(false);

  /** 撤销/重做存整树 JSON 快照：比命令模式少一半代码。节点上限 1000、每个节点还带正文，一份快照能到几十到几百 KB，只按 50 步计会涨到十几 MB，所以除了步数还按字节截（10MB）。 */
  const past = ref<string[]>([]);
  const future = ref<string[]>([]);
  /** 撤销栈已占的字节数。维护这个数，免得每次压栈都把整栈重新编码一遍。 */
  const pastBytes = ref(0);

  const saving = ref(false);

  /**
   * 每次已提交的编辑都递增。agent 的接受动作靠它判断"候选是不是基于一棵已经
   * 被改过的树算出来的"——用 revision 比比较整树引用更便宜，也比比 dirty 更准
   * （dirty 只说明"有改动"，不说明"改动发生在候选生成之后"）。
   */
  const clientRevision = ref(0);

  /** agent 算出来的候选。它**不是** content：用户确认之前不写进画布。 */
  const candidate = ref<MindMapContent | null>(null);
  /** 候选产出时的 clientRevision，接受时用来判过时。 */
  let candidateBaseRevision = 0;

  /**
   * 候选产出时所在的导图会话号（见 `mapSession`）。
   *
   * `clientRevision` 只在编辑时递增，切图（loadMap / newMap / preview）不碰它。
   * 只看 revision 的话："生成候选 A → 打开另一张图 B → 点接受"会把 A 的内容
   * 装进 B，还沿用 B 的 mapId 与 version，下次保存就把 A 写进 B 的记录。
   */
  let candidateSession = 0;

  /**
   * 候选是在哪种意图下产出的。
   *
   * 接受时要按它区分两件事：`create` 是一份新作品，不能继承上一张图的
   * `mapId` / `version` / 历史；`expand` / `edit` 改的就是当前这张图，身份必须留着。
   */
  let candidateIntent: 'create' | 'expand' | 'edit' = 'create';

  /**
   * 正在跑的这一轮是哪种意图（`agentRunning` 为假时只表示"上一次是什么"）。
   *
   * 与 `candidateIntent` 分开：那个是候选的归属（接受时用），这个是**这一轮在做什么**
   * （按钮文字用）。两者在候选就绪之后会分叉——候选已经产出、运行却还在收尾，
   * 那时按钮该说的是"展开中…"，而候选的身份早就不变了。
   */
  const runIntent = ref<'create' | 'expand' | 'edit'>('create');

  /**
   * 这一轮「改这张图」（`edit`）带上去的 `selected_ids`，原样快照。
   *
   * 为什么要存一份而不是直接读 `selectedIds`：候选确认的整个过程中用户随时会改
   * 选中（点一下清单里的一行就会跳转并选中）。读实时选区的话，diff 上那句
   * "在不在你选中的范围内"会随着用户点来点去而变，而那说的其实是**另一个时刻**
   * 的选区——用户没法靠它判断"这次改图动没动我圈的那三块"。
   *
   * 只有 `edit` 会写它：`expand` 的选区是"要展开的那一个节点"，新节点按构造就
   * 落在它下面，"范围内/外"在那条路上没有信息量。
   *
   * 是 `ref` 而不是 `candidateIntent` 那样的普通 `let`：`candidateDiff` 要在它
   * 变化时重算（选区在 `candidate` 之前就位，读 `candidate` 不会带来这次重算）。
   */
  const candidateSelection = ref<string[]>([]);

  /**
   * 候选树当前走到第几版 op。
   *
   * 每批 op 的 `base_revision` 是后端在**它那棵树**上算的序号（0 起，一批加一），
   * 这里记的是"我这份候选已经应用到这里"。两者对不上就说明中间漏了一批
   * （丢帧、乱序），在错位的树上继续套用比不套用更糟。
   *
   * 只放在 store 内部，不进返回对象：界面不需要读它，它只是消费端的一致性账本。
   */
  let candidateRevision = 0;

  /**
   * 候选整棵作废时，把 op 版本账本一起归零。
   *
   * 零是后端每一轮的新起点（`RunContext.revision` 从 0 起算）。漏掉这一步，
   * 下一轮的第一批 op 会因为 `base_revision=0` 对不上旧账本而被整批丢掉，
   * 表现出来就是"改了半天候选一点没动"。
   */
  function resetCandidateOps() {
    candidateRevision = 0;
  }

  /** 给面板显示的工具进度与阶段。 */
  const agentEvents = ref<AgentEvent[]>([]);
  const agentRunning = ref(false);
  const agentError = ref('');

  /**
   * 这一轮 agent 产出的树是演示还是模型给的，取自 `done` 帧的 `mode`。
   *
   * `null` 表示"没说"（旧后端省略了这个字段），接受时退回 `'model'`——那是
   * 这条通道一直以来的行为，不能因为多了个字段就让老后端的结果显示成未知。
   * 而一旦帧里明确写了 `demo`，界面就必须说明白：占位树不能顶着"模型生成"。
   */
  const agentMode = ref<'demo' | 'model' | null>(null);

  /**
   * 上次跑完（或被中断）、但用户还没处理过的那次运行。
   *
   * 这是"run 身份"落到界面上的唯一痕迹。此前 run 只活在一个 HTTP 连接的寿命里：
   * 关掉页面，已经生成出来的候选跟着连接一起没了，用户回来什么都看不到。现在
   * 服务端把它落进 `runs` 表，进页面时问一句"有没有我还没接住的结果"。
   *
   * 只是**一条提示**，不是弹窗：用户可能正在做别的事，打断他比让他知道这件事更烦。
   */
  const resumableRun = ref<RunSummary | null>(null);

  /**
   * 这一次会话里已经明确表示"不要再问"了。
   *
   * 丢弃是**本次会话**的决定：服务端那边同样会记 `dismissed_at`，但如果那次请求
   * 没发出去（后端挂了），不该让同一句话立刻又冒出来——用户刚说完"不要"，界面
   * 就当没听见，比不让捡回结果更伤。
   */
  let resumableSettled = false;

  /**
   * 当前候选属于哪一次运行。应用/丢弃时用它上报，服务端据此分清"采纳过"与
   * "嫌弃过"——两者都表示"别再提醒我"，但含义完全不同。
   */
  let activeRunId: string | null = null;

  /** 每次点击生成一个新的 run 身份。服务端只在这个 id 相同时才做幂等。 */
  function newRunId(): string {
    // 不用 crypto.randomUUID：它在非安全上下文（http://192.168.x.x 打开的开发页）
    // 里是 undefined，而那正是最需要它的时候。时间戳 + 随机后缀的碰撞概率对
    // "一次会话里的几十次运行"绰绰有余。
    return `run-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
  }

  /**
   * 问一次服务端：有没有"跑完了但我还没接住"的结果。
   *
   * 失败一声不响：进页面时后端没起来、或者 `runs` 表还是空的，都不该弹一句
   * "查询失败"——用户此刻什么也没要求。
   */
  async function probeResumableRun(): Promise<void> {
    if (resumableSettled) return;
    try {
      const pending = await listRuns();
      resumableRun.value = pending.length > 0 ? pending[0] : null;
    } catch {
      resumableRun.value = null;
    }
  }

  /**
   * 把上次那个候选载入**现有的**候选确认流程。
   *
   * 落点与一次真跑结束时完全一样（`candidate` + 版本账本 + 意图），所以「应用」
   * 「丢弃」、过时检查、整树校验一步都不少——不是另开一条"恢复"的旁路。
   */
  async function resumeRun(): Promise<void> {
    const target = resumableRun.value;
    if (!target) return;
    try {
      const detail = await getRun(target.run_id);
      if (!detail.candidate) {
        resumableRun.value = null;
        status.value = '那次运行没有留下可用的内容。';
        return;
      }
      agentEvents.value = [];
      agentWarnings.value = [];
      agentError.value = '';
      clearAgentPreview();
      resetCandidateOps();
      candidate.value = detail.candidate;
      candidateBaseRevision = clientRevision.value;
      candidateSession = mapSession;
      candidateIntent = detail.intent;
      // 捡回来的候选不知道当时圈了谁（`runs` 表存的是请求摘要，没有回读这条路），
      // 所以范围标记一律按"没有选区"处理：宁可不说"范围内/外"，也不能拿此刻的
      // 选区去标一份几分钟前产出的改动——那会把用户引到错的地方去核对。
      candidateSelection.value = [];
      runIntent.value = detail.intent;
      // 来历照原样带过来：占位树恢复成"模型生成"就是界面替它撒谎。
      agentMode.value = detail.mode ?? null;
      activeRunId = detail.run_id;
      // 候选已经在面板上等着确认了，再留一条提示只会问两遍。
      resumableRun.value = null;
      status.value = '已载入上次生成的候选；确认后才会应用到画布。';
    } catch (error) {
      resumableRun.value = null;
      status.value = error instanceof ApiError ? error.message : '读取上次的结果失败，请重试。';
    }
  }

  /** 丢弃上次那份候选。服务端也记一笔，否则下次进页面还会问同一句。 */
  async function dismissResumable(): Promise<void> {
    const target = resumableRun.value;
    resumableRun.value = null;
    resumableSettled = true;
    if (!target) return;
    status.value = '已丢弃上次的候选，画布没有变。';
    try {
      await markRunResolved(target.run_id, 'dismissed');
    } catch {
      // 上报失败不影响"已经不问"这件事：这只是记账，用户要的是别再被问。
    }
  }

  /**
   * 上报候选的归宿（应用 / 丢弃）。失败不提示：写台账失败不该让用户以为
   * 他没保存成功——那是两件事。
   */
  function reportRunResolution(kind: 'applied' | 'dismissed'): void {
    const id = activeRunId;
    activeRunId = null;
    if (!id) return;
    void markRunResolved(id, kind).catch(() => {});
  }

  /**
   * 本轮运行里"结果不完整"的证据：部分完成标记 + 每条诊断。
   *
   * 面板在候选上方按琥珀色列出，`status` 也会因此改成「部分分支未完成」。
   * 只认 `ok` 的时候，这三条信息一条都没地方落。
   */
  const agentWarnings = ref<AgentWarning[]>([]);

  /**
   * 生成过程中正在长出来的预览树。
   *
   * 与 `candidate` 分开存，不共用一份状态：预览没有经过整树校验，也不保证和
   * 最终候选一致（某一路失败被裁掉、去重丢掉重复项，都会让预览多出几个节点）。
   * 一旦让它顶替 `candidate`，用户点「应用到画布」就会把半成品装进画布。
   */
  const agentPreview = ref<PreviewNode | null>(null);

  /**
   * 已经收下的 `(父路径, label)`。
   *
   * 协议说同一组合只发一次，但那是在一条完整的连接上；重连重放、后端补发都会
   * 打破它，而界面上多出两个同名兄弟是看得见的错。
   */
  const previewSeen = new Set<string>();

  /** 父节点还没到的帧，按父路径攒着等它出现。 */
  const previewPending = new Map<string, PreviewNode[]>();

  /** 被真正丢掉的预览帧数。界面不显示，排障时用它回答"少的那几个节点去哪了"。 */
  const agentPreviewDropped = ref(0);

  /**
   * 预览树的"顶部是谁"。
   *
   * 生成时预览从中心主题开始长，顶部就是根，面板显示它的 label 即可。展开时预览是
   * 一条**祖先链**（见 `seedExpandPreview`）：顶部仍然是根，但用户关心的是"正在展开
   * 哪个节点"，把根的名字摆在标题行会让他以为整张图在重生成。所以展开时把它设成
   * 目标节点的标题，面板优先显示它。
   */
  const agentPreviewScope = ref<string | null>(null);

  /**
   * 预览整棵作废。
   *
   * 新一轮开始、收到 snapshot / done / error、换图，都要走这里：上一轮的残留
   * 一旦留到下一轮，用户看到的就是两棵树拼在一起的幻影。
   */
  function clearAgentPreview() {
    agentPreview.value = null;
    agentPreviewScope.value = null;
    previewSeen.clear();
    previewPending.clear();
    agentPreviewDropped.value = 0;
  }

  /** 不可能合法的帧直接丢掉，但别静默：留一条调试线索，否则少节点时无从下手。 */
  function dropPreviewFrame(reason: string) {
    agentPreviewDropped.value += 1;
    previewDebug(`[agent] 丢弃 node_grow 帧：${reason}`);
  }

  /**
   * 预览帧的调试线索只在开发/测试里输出。
   *
   * 它回答的是"少的那几个节点去哪了"，属于排障信息；用户不该在控制台看到这些。
   */
  function previewDebug(message: string) {
    if (import.meta.env.DEV) console.debug(message);
  }

  /** 路径 → 键。索引只可能是非负整数，用 '.' 连接不会撞。 */
  function previewPathKey(path: number[]): string {
    return path.join('.');
  }

  /**
   * 沿 path 找父节点；找不到返回 null。
   *
   * 返回 null 不代表这一帧有问题：索引超出当前子节点数量，更常见的原因是中间
   * 某个父节点的帧还没到（丢帧、重连后只接上了后半段）。调用方该把它攒起来。
   */
  function findPreviewParent(path: number[]): PreviewNode | null {
    let node: PreviewNode | null = agentPreview.value;
    for (const index of path) {
      if (!node) return null;
      const child: PreviewNode | undefined = node.children[index];
      if (!child) return null;
      node = child;
    }
    return node;
  }

  /**
   * 把帧挂到预览树上；父节点还不在就返回 false。
   *
   * 新节点自己也可能正是别人在等的父节点，所以挂上之后立刻放一批缓存出来。
   */
  function mountPreviewNode(path: number[], node: PreviewNode): boolean {
    const parent = findPreviewParent(path);
    if (!parent) return false;
    parent.children.push(node);
    // 自己的完整路径 = 父路径 + 自己在末尾的下标：追加到末尾，下标就是新的长度 - 1。
    flushPreviewPending([...path, parent.children.length - 1]);
    return true;
  }

  /** 收下还挂不上的帧，等它的父节点出现。 */
  function queuePreviewNode(path: number[], node: PreviewNode) {
    const key = previewPathKey(path);
    const queued = previewPending.get(key) ?? [];
    if (queued.length >= PREVIEW_PENDING_LIMIT) {
      dropPreviewFrame(`父路径 ${key} 下待挂帧超过 ${PREVIEW_PENDING_LIMIT} 条`);
      return;
    }
    queued.push(node);
    previewPending.set(key, queued);
    // 协议保证的顺利路径上永远不会走到这里，所以只留调试信息，不打扰用户。
    previewDebug(`[agent] node_grow 父节点还没到，暂存：path=${key} label=${node.label}`);
  }

  /**
   * 某个节点挂上之后，把它名下等着挂的帧放出来。
   *
   * 先整批取走再挂：挂的过程中会继续 flush 同一批键，边遍历边删会漏掉后半截。
   */
  function flushPreviewPending(path: number[]) {
    const key = previewPathKey(path);
    const queued = previewPending.get(key);
    if (!queued) return;
    previewPending.delete(key);
    for (const node of queued) {
      // 仍然挂不上说明它等的其实是更深的父节点，放回去继续等。
      if (!mountPreviewNode(path, node)) queuePreviewNode(path, node);
    }
  }

  /**
   * 消费一帧 `node_grow`。
   *
   * 顺序上后端保证父先于子，这里仍然按"可能乱序"处理：预览是纯展示，宁可多花
   * 一点缓存的代价，也不能因为一次丢帧就让整支子树静默消失。
   */
  function growPreviewNode(path: number[] | null, label: string, expandable: boolean | null) {
    const text = label.trim();
    if (!text) {
      dropPreviewFrame('label 为空');
      return;
    }
    if (charLength(text) > LIMITS.label) {
      dropPreviewFrame(`label 超过 ${LIMITS.label} 字`);
      return;
    }
    // 根帧的 `path` 是 null，不走索引校验；只有真正带路径的帧才需要。
    if (path !== null) {
      // 负索引、小数、超深路径都不是"父节点还没到"，是数据本身坏了。
      if (!path.every(index => Number.isInteger(index) && index >= 0)) {
        dropPreviewFrame(`path 不是有效的索引链：${path.join(',')}`);
        return;
      }
      if (path.length >= LIMITS.depth) {
        dropPreviewFrame(`path 深过 ${LIMITS.depth} 层：${path.join(',')}`);
        return;
      }
    }

    // 根用单独的键前缀：根没有路径，`[]` 已经被"挂在根上的孩子"占着了。
    const key = `${path === null ? 'root' : previewPathKey(path)}|${text}`;
    if (previewSeen.has(key)) return;
    if (previewSeen.size >= LIMITS.nodes) {
      dropPreviewFrame(`预览节点数超过 ${LIMITS.nodes}`);
      return;
    }
    // 收下就记，重放过来的同一帧到此为止；挂不上时它已经进了缓存，也不该再收一次。
    previewSeen.add(key);

    const node: PreviewNode = { label: text, expandable, children: [] };

    if (path === null) {
      // 根已经立起来又来了第二帧：只在标签不同时才算数据出错。同一个标签的重复
      // 帧上面已经被 previewSeen 挡掉，走到这里就说明后端给了两个不同的根。
      if (agentPreview.value) {
        dropPreviewFrame(`重复的根帧：${text}`);
        return;
      }
      agentPreview.value = node;
      // 根一到，之前攒着等它的分支（`[]`）立刻可以挂了。
      flushPreviewPending([]);
      return;
    }

    if (!agentPreview.value) {
      // 根还没到：带路径的帧全是孤儿，攒着等根。它们不会被误当成根——那是根
      // 帧丢失时唯一能察觉到的信号。
      queuePreviewNode(path, node);
      return;
    }
    if (!mountPreviewNode(path, node)) queuePreviewNode(path, node);
  }

  /**
   * 节点 id → 从根出发的子索引链（`[]` 是根自己）；找不到返回 null。
   *
   * 与后端 `tools._index_path` 同一口径：`node_grow` 的 `path` 指的是**父节点**的
   * 索引链，而展开时父节点就是被展开的那个节点，所以两边必须用同一套编号，
   * 否则预览会把新节点挂到别的分支下面。
   */
  function indexPathOf(root: MindNode, id: string): number[] | null {
    if (root.id === id) return [];
    const walk = (node: MindNode, path: number[]): number[] | null => {
      for (let index = 0; index < node.children.length; index += 1) {
        const child = node.children[index];
        if (child.id === id) return [...path, index];
        const found = walk(child, [...path, index]);
        if (found) return found;
      }
      return null;
    };
    return walk(root, []);
  }

  /**
   * 展开前把预览的**祖先链**搭起来，让 `node_grow` 帧能挂到被展开的那个节点下。
   *
   * 不搭这条链会怎样：后端的 `path` 是 `[0, 0]`（第三层那个叶子），而预览树此时是
   * 空的，`findPreviewParent` 找不到父节点，帧会被攒进 `previewPending` 里永远不
   * 出现——界面上就是"点了展开，什么预览都没有"。
   *
   * 只搭链上的节点（不是整棵树）：预览面板显示的是"正在展开 风险"，而不是把用户
   * 已有的整张图复制一遍；链上每一层只有一个孩子，正是路径的形状。
   *
   * 返回是否搭成功。拿不到路径（节点不在 content 里、比如正在被删）时返回 false，
   * 这一次展开就没有预览——那比画一条错的链好。
   */
  function seedExpandPreview(nodeId: string): boolean {
    const root = content.value?.root;
    if (!root) return false;
    const path = indexPathOf(root, nodeId);
    if (!path) return false;

    const labels = [root.label.trim()];
    let cursor = root;
    for (const index of path) {
      cursor = cursor.children[index];
      labels.push(cursor.label.trim());
    }

    const build = (depth: number): PreviewNode => ({
      label: labels[depth],
      expandable: null,
      // 链上只往下走一格；最后一格就是目标节点，它的 children 留给真实的帧。
      children: depth + 1 < labels.length ? [build(depth + 1)] : [],
    });

    agentPreview.value = build(0);
    agentPreviewScope.value = labels[labels.length - 1];
    return true;
  }

  /** 记下这一帧的告警：同 kind + code + message 只留一条，两个工具报同一件事时不重复刷屏。 */
  function recordWarnings(next: AgentWarning[]) {
    if (!next.length) return;
    const seen = new Set(agentWarnings.value.map(item => `${item.kind}|${item.code}|${item.message}`));
    const merged = [...agentWarnings.value];
    for (const item of next) {
      const key = `${item.kind}|${item.code}|${item.message}`;
      if (seen.has(key)) continue;
      seen.add(key);
      merged.push(item);
    }
    agentWarnings.value = merged.slice(0, AGENT_WARNING_LIMIT);
  }

  /**
   * 「候选已就绪」这句的完整文案。
   *
   * 结果不完整时必须和全部成功区分开：只写「候选已就绪」，用户点接受之后
   * 才发现某几支是空的。partial 与"只有诊断"分开说——「分支未完成」这种话
   * 放在一棵只是有重复节点待合并的树上并不成立。
   */
  function candidateStatus(base: string): string {
    if (agentWarnings.value.some(item => item.kind === 'partial')) {
      return `${base}（部分分支未完成），确认后才会应用到画布。`;
    }
    if (agentWarnings.value.length) return `${base}（有诊断提示），确认后才会应用到画布。`;
    return `${base}，确认后才会应用到画布。`;
  }

  /** 保存撞上 409 时置位，由界面弹窗让用户选"重新加载 / 另存为新导图 / 取消"。 */
  const conflict = ref<{ message: string } | null>(null);
  /** 打开列表用的缓存。 */
  const maps = ref<MapRecord[]>([]);
  const listOpen = ref(false);
  const listBusy = ref(false);

  let controller: AbortController | null = null;

  /**
   * 画布该画哪棵树。
   *
   * 三级优先级各有理由：
   * 1. `content` 最优先。用户已经打开/生成了一张图时，让还没定稿的东西顶掉它，
   *    看起来就是"我的图被改了"——而且失败或取消之后才发现内容消失过。
   * 2. `candidate` 其次。`snapshot` 到达时预览会被整棵作废（那是必须的：不能再
   *    挂着一棵没校验的树），如果这一级不存在，画布会在"预览→候选"之间空一帧，
   *    退回到空态占位，用户还得点「应用」才看得见图。
   * 3. `agentPreview` 垫底。节点一个一个到达，画布跟着长。
   *
   * 注意候选只是"看得见"：`acceptCandidate()` 的校验一步都没少，候选照样要用户
   * 点「应用」才会写进 `content`。渲染与采纳是两件事。
   */
  const renderSource = computed<RenderSource | null>(() => {
    if (content.value) {
      return {
        root: content.value.root, kind: 'content', expandable: NO_EXPANDABLE,
        edges: edgesOf(content.value),
      };
    }
    if (candidate.value) {
      return {
        root: candidate.value.root, kind: 'candidate', expandable: NO_EXPANDABLE,
        edges: edgesOf(candidate.value),
      };
    }
    if (agentPreview.value) {
      const expandable = new Map<string, boolean | null>();
      const root = previewSubtree(agentPreview.value, [], expandable);
      return { root, kind: 'preview', expandable, edges: [] };
    }
    return null;
  });

  const layout = computed<LayoutResult | null>(() => {
    const source = renderSource.value;
    return source ? layoutTidy(source.root, { direction: direction.value }) : null;
  });

  const flatNodes = computed<FlatNode[]>(() => {
    const source = renderSource.value;
    return source ? flatten(source.root) : [];
  });

  /**
   * 附加边在画布上的几何信息：用哪两个句柄。
   *
   * 附件的两端往往**不是父子**（汇合、回退跨越两个 subtree），所以不能像父子线
   * 那样只按 depth 猜方向，只能看两者的实际坐标：`childSide(from, to, direction)`
   * 给出的就是"从 from 看 to 在哪一侧"，接线端点即可。
   *
   * 端点找不到（不该发生：契约层硬拒 dangling，这里只是兜底）就跳过这条边，
   * 而不是画一条 `source: undefined` 的线——Vue Flow 会因此抛错，整张画布白屏。
   */
  const attachEdges = computed(() => {
    const current = layout.value;
    const source = renderSource.value;
    if (!current || !source) return [];

    const out: Array<{ edge: MindEdge; side: ChildSide; targetSide: ChildSide }> = [];
    for (const edge of source.edges) {
      const from = current.positions.get(edge.source);
      const to = current.positions.get(edge.target);
      if (!from || !to) continue;
      const side = childSide(from, to, direction.value);
      // 与父子线同一套端点约定（见 `edges` 里 targetHandle 那一行）：从哪一侧出去，
      // 就从对面那侧进来。
      const targetSide: ChildSide = side === 'left' ? 'right' : side === 'right' ? 'left' : 'top';
      out.push({ edge, side, targetSide });
    }
    return out;
  });

  /**
   * 每个节点要为附加边多开哪几个句柄。
   *
   * `MindNode.vue` 只渲染 `data.in` / `data.out` 里列出的句柄，而那两个数组原本只
   * 按**父子线**算。一条从叶子指向远处的回退线，源节点根本没有子节点、也就没有
   * 任何 `out-*` 句柄——Vue Flow 找不到句柄时线会从头节点中心乱连（或干脆不画），
   * 而报错只出现在浏览器控制台。
   */
  const edgeSideIndex = computed(() => {
    const index = new Map<string, { out: ChildSide[]; in: ChildSide[] }>();
    const ensure = (id: string) => {
      const hit = index.get(id);
      if (hit) return hit;
      const fresh = { out: [] as ChildSide[], in: [] as ChildSide[] };
      index.set(id, fresh);
      return fresh;
    };
    for (const { edge, side, targetSide } of attachEdges.value) {
      const from = ensure(edge.source);
      if (!from.out.includes(side)) from.out.push(side);
      const to = ensure(edge.target);
      if (!to.in.includes(targetSide)) to.in.push(targetSide);
    }
    return index;
  });

  const nodes = computed<MindFlowNode[]>(() => {
    const current = layout.value;
    const source = renderSource.value;
    if (!current || !source) return [];
    // 只有已应用的树才可编辑：候选与预览都能画出来，但点不动、改不了。
    const editable = source.kind === 'content';

    const parentOf = new Map<string, string | null>();
    for (const { node, parentId } of flatNodes.value) parentOf.set(node.id, parentId);

    /**
     * 颜色继承：沿父链取**最近的有色祖先**。
     *
     * 一级分支一个颜色、它的所有后代用同一个色——这是思维导图"按分支分组"的
     * 标准做法。每个节点各一色会像调色板测试：颜色不再指示"这属于哪一支"。
     *
     * 为什么在**渲染层**继承，而不是生成时给每个节点都填上色：
     *   1. 旧图立刻有颜色。生成时填只对那一次生成有效，库里已有的树仍然是黑的；
     *   2. 用户手工改一级分支的颜色时，整支跟着变——他改的是"这一支的分组色"，
     *      而生成时填下去的是快照，改完只有那一个节点变，视觉上像改坏了；
     *   3. 落进库里的数据仍然是"一级分支才有 color"（与后端
     *      `PLAN_PROMPT` / `_slot_from` 的边界一致），导出、保存都不多出噪音。
     *
     * 一次前向遍历算完：`flatNodes` 是父在子前的 DFS 序，所以取父节点的结论时
     * 它一定已经算好了。逐节点沿父链往上走是 O(深度)，这条路是 O(节点数)，而且
     * 这段代码每次 `flatNodes` 变化都会重跑（画布重排的同一条路）。
     */
    const inheritedColor = new Map<string, NodeColor | undefined>();
    for (const { node, parentId } of flatNodes.value) {
      const own = node.color ?? undefined;
      inheritedColor.set(
        node.id,
        own ?? (parentId ? inheritedColor.get(parentId) : undefined),
      );
    }

    return flatNodes.value.map(({ node, depth }) => {
      const point = current.positions.get(node.id) ?? { x: 0, y: 0 };

      // 子节点在哪几侧，就在哪几侧留出口（双向布局里根的两边都有）。
      const outSides: ChildSide[] = [];
      for (const child of node.children) {
        const childPoint = current.positions.get(child.id);
        if (!childPoint) continue;
        const side = childSide(point, childPoint, direction.value);
        if (!outSides.includes(side)) outSides.push(side);
      }

      // 入口在父节点到自己的连线的对面：父在左边就从左边进来。
      const inSides: ChildSide[] = [];
      const parentPoint = current.positions.get(parentOf.get(node.id) ?? '');
      if (parentPoint) {
        const side = childSide(parentPoint, point, direction.value);
        inSides.push(side === 'left' ? 'right' : side === 'right' ? 'left' : 'top');
      }

      // 附加边也要句柄（理由见 `edgeSideIndex`）。并进来而不是替换：一个节点
      // 可以同时有父子线和回退线，两侧的句柄都要在。
      const extra = edgeSideIndex.value.get(node.id);
      if (extra) {
        for (const side of extra.out) if (!outSides.includes(side)) outSides.push(side);
        for (const side of extra.in) if (!inSides.includes(side)) inSides.push(side);
      }

      return {
        id: node.id,
        type: 'mind',
        draggable: false,
        connectable: false,
        // 预览与候选都不可选：选中一个只在"提案"里存在的 id，会让详情面板对着
        // 空内容说话，expand 请求也会把后端不认识的 id 发回去。
        selectable: editable,
        // 选中态由 store 说了算。只在点击时靠 Vue Flow 自己切换的话，代码里改选中
        // （加节点、删节点后跳到父节点）画布上看不出来，用户会以为还停在原来那个节点上。
        // 不可编辑时一律不显示选中：残留的 selectedId 可能正好撞上候选里的同名 id。
        //
        // 多个节点一起高亮而不是只高亮主选区：「改这张图」吃的是整个选区，
        // 只亮一个的话，用户圈完三个节点看不出另外两个算不算数。
        selected: editable && selectedIds.value.includes(node.id),
        position: {
          x: point.x - NODE_SIZE.width / 2,
          y: point.y - NODE_SIZE.height / 2,
        },
        data: {
          label: displayTitle(node),
          body: displayBody(node),
          fullLabel: node.label,
          bodySource: bodySource(node),
          depth,
          out: outSides,
          in: inSides,
          refs: node.source_refs?.length ?? 0,
          draft: source.kind === 'content' ? undefined : source.kind,
          previewLeaf: source.kind === 'preview' && source.expandable.get(node.id) === false,
          // 视觉元素原样带过去：只有已应用的那棵树才画（候选/预览用弱化样式，
          // 它们的颜色会让"这是一份提案"这件事变得模糊）。
          // 颜色取**继承**的结果：自身没有就沿用最近的有色祖先（见上面那段）。
          color: editable ? inheritedColor.get(node.id) : undefined,
          icon: editable ? node.icon ?? undefined : undefined,
          // 高亮只在"画布上这棵树 + 有候选"时成立，所以它天然只对 content 生效。
          changed: changedNodeIds.value.has(node.id),
        },
      } satisfies MindFlowNode;
    });
  });

  const edges = computed<FlowEdge[]>(() => {
    const current = layout.value;
    if (!current) return [];

    const out: FlowEdge[] = [];
    for (const { node, parentId } of flatNodes.value) {
      if (!parentId) continue;
      const parentPoint = current.positions.get(parentId);
      const childPoint = current.positions.get(node.id);
      if (!parentPoint || !childPoint) continue;

      const side = childSide(parentPoint, childPoint, direction.value);
      out.push({
        id: `${parentId}->${node.id}`,
        source: parentId,
        sourceHandle: `out-${side}`,
        target: node.id,
        targetHandle: `in-${side === 'left' ? 'right' : side === 'right' ? 'left' : 'top'}`,
        type: 'smoothstep',
      });
    }

    // 附加边（汇合 / 回退 / 条件跳转）。用自定义类型 `mindEdge`：它要画标签、
    // 按 kind 换颜色/线型，而内置的 smoothstep 做不到（见 `canvas/MindEdge.vue`）。
    //
    // 与父子线不同的两点，都在这里定死：
    //   - 用 `data` 传 label/kind，而不是 Vue Flow 的 `label` 字段：那个会被渲染成
    //     一个默认标签块，样式没法跟着 kind 变。
    //   - id 前缀 `attach:`，避免与 `${parentId}->${node.id}` 这种父子线 id 撞车
    //     （两个节点之间可以同时存在父子线与一条回退线，id 撞了只会剩一条）。
    for (const { edge, side, targetSide } of attachEdges.value) {
      out.push({
        id: `attach:${edge.id}`,
        source: edge.source,
        sourceHandle: `out-${side}`,
        target: edge.target,
        targetHandle: `in-${targetSide}`,
        type: 'mindEdge',
        data: {
          label: edge.label ?? undefined,
          kind: edge.kind ?? 'flow',
          edgeId: edge.id,
        },
      });
    }
    return out;
  });

  const nodeCount = computed(() => flatNodes.value.length);

  /**
   * 详情面板要显示的节点。
   *
   * 必须落在 `content` 里：画布上可能正画着候选或预览（`flatNodes` 跟着渲染源走），
   * 而面板上的「改文字 / 加子节点 / 删除」全都写 `content`。没有已应用的树就没有
   * 可编辑的节点，否则面板会摆出一排点了没反应的按钮。
   */
  /**
   * 这一轮"用户圈住的节点"全集（主选区在前）。
   *
   * 顺序有意义：第一个就是详情面板/展开按钮对着的那一个，所以调用方拿
   * `selectedIds[0]` 与 `selectedId` 是同一件事。
   *
   * 这里按 `content` 过滤一次，而不是指望每条删节点的路径都记得清追加选区：
   * 删掉的节点留在选区里，下次「改这张图」就会把后端根本不认识的 id 发过去
   * （后端会静默跳过，但那份提示词里就少了一条、而用户以为自己选了三个）。
   */
  const selectedIds = computed<string[]>(() => {
    const all = content.value;
    if (!all) return [];
    const primary = selectedId.value;
    const extra = extraSelectedIds.value.filter(
      id => id !== primary && findNode(all.root, id) !== null,
    );
    return primary ? [primary, ...extra] : extra;
  });

  const selectedNode = computed<FlatNode | null>(() => {
    if (!content.value) return null;
    return flatNodes.value.find(entry => entry.node.id === selectedId.value) ?? null;
  });

  /** 选中节点的引用。空节点也有自己的空数组，详情面板不必再判空。 */
  const selectedRefs = computed<SourceRef[]>(() => selectedNode.value?.node.source_refs ?? []);

  /**
   * 已应用内容里的**全部**附加边。
   *
   * 只读 `content` 而不是 `renderSource`：侧栏的「附加关系」区块要能删边、要能
   * 显示"这条线的端点是谁"，而候选/预览里的边在用户点「应用」之前并不是他的作品。
   * 画布那条路（`edges` computed）才跟着 `renderSource` 走。
   */
  const contentEdges = computed<MindEdge[]>(() => edgesOf(content.value));

  /**
   * 与当前选中节点相关的边（源或目标命中它）。
   *
   * 侧栏只用这一份而不是全量：一张流程图可能有几十条线，全列出来用户得自己在
   * 里面找"这条跟选中的节点有关系吗"。而"改这条线 / 删这条线"的操作入口，本来看
   * 的就是选中节点周围那几条。
   */
  const selectedEdges = computed<MindEdge[]>(() => {
    const id = selectedId.value;
    if (!id) return [];
    return contentEdges.value.filter(edge => edge.source === id || edge.target === id);
  });

  /**
   * 来源的反向索引。
   *
   * 只从 `content` 算：画布上可能正画着候选或预览节点，把它们算进引用统计会让
   * 左侧说"被 5 个节点引用"、而其中 3 个根本不在导图里。
   */
  const refIndex = computed(() => buildRefIndex(content.value?.root ?? null));

  /**
   * 待确认候选相对画布的改动清单；`create` 意图（没有基线）为 `null`。
   *
   * 只在"候选是当前树上的改动"时有意义，而那条判据就是 `candidateIntent`：
   * `create` 的候选是一份新作品，谈差异无从谈起。`candidateIntent` 是个普通
   * `let`（不参与响应式），但它只在 `candidate.value` 被赋值的同时变化，
   * 所以把 `candidate` 读进来就能保证这个 computed 在该重算时重算。
   *
   * 每一条都带 `inSelection`：这次「改这张图」带着选区，diff 就必须说清
   * "这处改动在你圈的范围里，还是跑到了范围外"。范围用的是**请求时**的选区
   * （`candidateSelection`），不是此刻的选区（理由见那个 ref 的注释）。
   */
  const candidateDiff = computed<ScopedChange[] | null>(() => {
    const next = candidate.value;
    if (!next) return null;
    if (candidateIntent === 'create') return null;
    const before = content.value;
    const nodeChanges = diffCandidate(before?.root ?? null, next.root) ?? [];
    // 边单独算一份再拼起来（理由见 `diffEdges`）。基线没有树时上面已经返回 null，
    // 走到这里 `before` 一定存在。
    const names = new Map(flatNodes.value.map(entry => [entry.node.id, displayTitle(entry.node)]));
    const nameOf = (id: string) => names.get(id) ?? id;
    const changes: CandidateChange[] = [
      ...nodeChanges,
      ...diffEdges(edgesOf(before), edgesOf(next), nameOf),
    ];
    const scope = selectionScope(before?.root ?? null, next.root, candidateSelection.value);
    return changes.map(item => ({ ...item, inSelection: withinScope(item, scope) }));
  });

  /**
   * 画布上要**高亮**的节点 id：候选改动过、且在当前树里还存在的那些。
   *
   * 新增的节点没有 id 可高亮（它在 `content` 里根本不存在），删除的节点还在
   * `content` 里所以能高亮——那正是"这个节点会消失，你看一眼再决定"。
   *
   * `edge_*` 两种改动被排除：它们的 `id` 是**边 id**，与节点 id 是两套命名空间，
   * 混进来只会让一个随机字符串去"高亮"某个可能同名的节点。
   */
  const changedNodeIds = computed<Set<string>>(() => {
    const diff = candidateDiff.value;
    if (!diff) return new Set<string>();
    const nodeKinds = new Set<CandidateChange['kind']>(['removed', 'updated', 'moved']);
    return new Set(diff.filter(item => nodeKinds.has(item.kind)).map(item => item.id));
  });

  /**
   * "把某个节点挪到视野中心"的触发计数。
   *
   * 用计数器而不是监听 `selectedId`：在画布上手动点节点时也会改选中，若监听选中，
   * 视图会把人刚拖到的位置又拽回中心。只有从引用列表主动跳转时才加一。
   */
  const focusTick = ref(0);

  /** 从根到当前节点的路径，详情面板用来标明"这句话出自哪一支"。 */
  const breadcrumb = computed<MindNode[]>(() => {
    const target = selectedId.value;
    if (!content.value || !target) return [];
    const path: MindNode[] = [];
    const walk = (node: MindNode): boolean => {
      path.push(node);
      if (node.id === target) return true;
      for (const child of node.children) {
        if (walk(child)) return true;
      }
      path.pop();
      return false;
    };
    return walk(content.value.root) ? path : [];
  });

  const canUndo = computed(() => past.value.length > 0);
  const canRedo = computed(() => future.value.length > 0);

  function snapshot(): string | null {
    return content.value ? JSON.stringify(content.value) : null;
  }
  function snapshotBytes(snap: string): number {
    return new TextEncoder().encode(snap).length;
  }

  /**
   * 压一步撤销，并按字节与步数两个上限淘汰最旧的。
   *
   * 至少保留一步：一份快照自身就超过上限时（极端大的树）清空栈会让撤销直接失效，
   * 那比占着这 10MB 更糟。
   */
  function recordPast(snap: string) {
    past.value.push(snap);
    pastBytes.value += snapshotBytes(snap);
    while (past.value.length > 1 && (past.value.length > HISTORY_LIMIT || pastBytes.value > HISTORY_BYTES_LIMIT)) {
      const dropped = past.value.shift()!;
      pastBytes.value -= snapshotBytes(dropped);
    }
  }

  /** 改动前调用：记一步历史，并让重做栈失效（树从这里长出新的分支了）。 */
  function pushHistory() {
    const snap = snapshot();
    if (!snap) return;
    recordPast(snap);
    future.value = [];
  }

  /** 还能撤销几步、占了多少字节：界面与测试都靠它确认字节上限真的生效。 */
  const undoDepth = computed(() => past.value.length);
  const undoBytes = computed(() => pastBytes.value);

  function resetHistory() {
    past.value = [];
    future.value = [];
    pastBytes.value = 0;
  }

  /**
   * create 意图接受候选后的那一步撤销。
   *
   * 与 `pushHistory()` 只差一处：画布本来是空白时也要留下一步（`'null'` 就是
   * JSON 里的空画布），否则用户点了「应用到画布」就再也退不回接受之前的样子。
   */
  function pushHistoryOrEmpty() {
    recordPast(snapshot() ?? 'null');
    // 树从这里长出新的分支了，重做栈必须失效。
    future.value = [];
  }

  /** 选中的节点可能已被删掉：兜底清空，免得详情面板指向不存在的节点。 */
  function ensureSelection() {
    const target = selectedId.value;
    if (target && content.value && !findNode(content.value.root, target)) {
      setPrimary(null);
    }
  }

  /**
   * 把一步历史快照装回画布。
   *
   * 快照可能是字符串 `'null'`——接受候选之前画布是空的时候，`pushHistoryOrEmpty()`
   * 压的就是它。`JSON.parse('null')` 得到 null，正好是 `MindMapContent | null`
   * 的另一半；但选中与编辑态还指着已经不存在的旧节点，必须一起清掉，
   * 否则详情面板和编辑框会继续对着空白画布说话。
   */
  function restoreSnapshot(snap: string) {
    const parsed = JSON.parse(snap) as MindMapContent | null;
    content.value = parsed;
    if (!parsed) {
      setPrimary(null);
      editingId.value = null;
    }
  }

  function applyContent(next: MindMapContent, history: boolean): boolean {
    // 生成还在进行时禁止用户改动：done 帧会用完整结果整树替换，期间的编辑
    // 必然被覆盖；撤销栈里也会混进生成中间态，撤销起来只剩半成品。
    // 返回 false 让调用方提前退出，免得它接着把 status 改成「已修改」。
    if (busy.value) {
      status.value = '正在生成导图，完成之后才能编辑。';
      return false;
    }
    if (history) pushHistory();
    content.value = next;
    dirty.value = true;
    clientRevision.value += 1;
    return true;
  }

  /** 节点深度，1 起算（与契约的"最多 8 层"同一口径）。 */
  function depthOfId(id: string): number | null {
    const hit = flatNodes.value.find(entry => entry.node.id === id);
    return hit ? hit.depth + 1 : null;
  }

  function beginEdit(id: string) {
    if (!content.value || !findNode(content.value.root, id)) return;
    setPrimary(id);
    editingId.value = id;
  }

  function endEdit() {
    editingId.value = null;
  }

  function commitLabel(id: string, label: string) {
    if (!content.value) return;
    const node = findNode(content.value.root, id);
    if (!node) return;

    const next = label.trim();
    if (next === node.label.trim()) {
      // 只收拾自己的编辑态：连按 Tab 时上一个输入框失焦会再提交一次，
      // 那时正在编辑的其实是新节点，这里不能把新节点的编辑态一起关掉。
      if (editingId.value === id) editingId.value = null;
      return;
    }
    if (!next) {
      status.value = '节点标题不能为空';
      return;
    }
    if (Array.from(next).length > LIMITS.label) {
      status.value = `节点标题最多 ${LIMITS.label} 个字`;
      return;
    }

    if (!applyContent({ ...content.value, root: renameNode(content.value.root, id, next) }, true)) return;
    if (editingId.value === id) editingId.value = null;
    status.value = '已修改，记得保存。';
  }

  /**
   * 提交节点正文（契约 v2 的 `note`）。
   *
   * 和改标题一样：内容没变就只收拾编辑态，不记历史；空正文是合法输入——
   * 它表示"这个节点只保留短标题"，所以清空必须能存下去（不能当成"非法"）。
   */
  function commitNote(id: string, note: string | null) {
    if (!content.value) return;
    const node = findNode(content.value.root, id);
    if (!node) return;

    const next = note?.trim() ?? '';
    const before = node.note?.trim() ?? '';
    if (next === before) return;
    if (charLength(next) > LIMITS.note) {
      status.value = `节点正文最多 ${LIMITS.note} 个字`;
      return;
    }

    if (!applyContent({ ...content.value, root: setNodeNote(content.value.root, id, next) }, true)) return;
    status.value = next ? '已修改正文，记得保存。' : '已清空正文，只保留标题，记得保存。';
  }

  /**
   * 改节点的语义色与图标（详情面板上的两个控件）。
   *
   * 与改标题、改备注同一套语义：内容没变就什么都不做（不记撤销、不置脏），
   * 变了才是**一次可撤销的内容修改**——换颜色凭什么不能 Ctrl+Z 退回去。
   *
   * 传 `null` 是显式清除（选「默认」就是清掉颜色）；要"不动"就别传这个字段。
   * 图标按码点截到契约上限（`LIMITS.icon`），空串归一成 `null`：一个只含空格的
   * 图标在画布上什么都不显示，却会让"有没有图标"这件事变成两个分支。
   */
  function setNodeVisualById(
    id: string,
    patch: { color?: NodeColor | null; icon?: string | null },
  ): void {
    if (!content.value) return;
    const node = findNode(content.value.root, id);
    if (!node) return;

    const nextColor = patch.color === undefined ? node.color ?? null : patch.color;
    const nextIcon = patch.icon === undefined
      ? node.icon ?? null
      : (() => {
        const text = (patch.icon ?? '').trim();
        return text ? Array.from(text).slice(0, LIMITS.icon).join('') : null;
      })();

    if ((node.color ?? null) === nextColor && (node.icon ?? null) === nextIcon) return;

    if (!applyContent(
      { ...content.value, root: setNodeVisual(content.value.root, id, nextColor, nextIcon) },
      true,
    )) return;
    status.value = '已修改样式，记得保存。';
  }

  function renameTitle(title: string) {
    if (!content.value) return;
    const next = title.trim();
    // 提交后草稿就作废：已经落到 content 里了，再留着会让「等待期间有未提交
    // 输入」的判断一直为真。
    titleDraft.value = null;
    if (next === content.value.title) return;
    if (!next) {
      status.value = '标题不能为空';
      return;
    }
    if (Array.from(next).length > LIMITS.title) {
      status.value = `标题最多 ${LIMITS.title} 个字`;
      return;
    }
    if (!applyContent({ ...content.value, title: next }, true)) return;
  }

  /** 标题框每次按键都同步草稿，只记"有未提交的输入"，不改 content。 */
  function setTitleDraft(value: string | null) {
    titleDraft.value = value;
  }

  function addChildTo(parentId: string) {
    if (!content.value) return;
    if (countNodes(content.value.root) >= LIMITS.nodes) {
      status.value = `导图最多 ${LIMITS.nodes} 个节点`;
      return;
    }
    const parent = findNode(content.value.root, parentId);
    if (!parent) return;
    if (parent.children.length >= LIMITS.children) {
      status.value = `一个节点最多 ${LIMITS.children} 个子节点`;
      return;
    }
    if ((depthOfId(parentId) ?? 1) + 1 > LIMITS.depth) {
      status.value = `导图最多 ${LIMITS.depth} 层`;
      return;
    }

    const { root, node } = addChild(content.value.root, parentId);
    if (!applyContent({ ...content.value, root }, true)) return;
    setPrimary(node.id);
    editingId.value = node.id;
    status.value = '已加子节点，输入文字后按回车确认。';
  }

  /**
   * 编辑新节点时按 Tab：先提交文字，再回到它的父节点下加一个兄弟。
   * 若只看 selectedId，新节点一被选中，Tab 就变成"给子节点加子节点"——连按会一路下钻；
   * 用户预期是同一个父节点下排出一串兄弟，所以这里固定回到父节点。
   */
  function commitAndAddChild(label: string) {
    const id = editingId.value;
    if (!id) return;
    const parent = content.value ? findParent(content.value.root, id) : null;
    commitLabel(id, label);
    // 提交被拦下（空标题、超长）时编辑态还在，别在错误的基础上接着加节点。
    if (editingId.value === id) return;
    addChildTo(parent?.id ?? selectedId.value ?? id);
  }

  function addSiblingTo(id: string) {
    if (!content.value) return;
    if (countNodes(content.value.root) >= LIMITS.nodes) {
      status.value = `导图最多 ${LIMITS.nodes} 个节点`;
      return;
    }
    const parent = findParent(content.value.root, id);
    // 根节点没有兄弟，退化成一个子节点；这时要按"加子节点"的规则校验层数与个数。
    if (!parent) {
      addChildTo(id);
      return;
    }
    if (parent.children.length >= LIMITS.children) {
      status.value = `一个节点最多 ${LIMITS.children} 个子节点`;
      return;
    }
    if ((depthOfId(parent.id) ?? 1) + 1 > LIMITS.depth) {
      status.value = `导图最多 ${LIMITS.depth} 层`;
      return;
    }

    const { root, node } = addSibling(content.value.root, id);
    if (!applyContent({ ...content.value, root }, true)) return;
    setPrimary(node.id);
    editingId.value = node.id;
    status.value = '已加兄弟节点，输入文字后按回车确认。';
  }

  function deleteNode(id: string) {
    if (!content.value) return;
    if (content.value.root.id === id) {
      status.value = '根节点不能删除';
      return;
    }

    // id 不在树上（预览的派生 id、候选的 id 都会走到这里）就直接退出。
    // 交给 removeNode 的话它只会原样返回整棵树，而 applyContent 照样记一步历史、
    // 递增 clientRevision——用户什么都没删，撤销栈和候选的新鲜度却被改脏了。
    if (!findNode(content.value.root, id)) return;

    const parent = findParent(content.value.root, id);
    // 连带删边：命中**这棵子树**任意节点的边全部消失，与后端 `_apply_remove` 同一
    // 语义。只清被点名的那一个节点的话，子孙身上的边会悬空，而 dangling 是契约层
    // 的硬拒——下一次保存就 422，用户却只是"删了一个节点"。
    const doomed = subtreeIds(findNode(content.value.root, id)!);
    const keptEdges = edgesOf(content.value).filter(
      edge => !doomed.has(edge.source) && !doomed.has(edge.target),
    );
    const nextContent: MindMapContent = {
      ...content.value,
      root: removeNode(content.value.root, id),
      edges: keptEdges,
    };
    if (!applyContent(nextContent, true)) return;
    setPrimary(parent?.id ?? null);
    editingId.value = null;
    status.value = '已删除该节点及其子节点，可撤销。';
  }

  /**
   * 在当前树里连一条**附加关系**（侧栏「连一条关系」的入口）。
   *
   * 三件事与 `addEdge`（`edits.ts` 的纯函数）分工，这里只做"界面语义"那部分：
   *   - **节点必须存在**：面板上的下拉框只会列出真实节点，但 store 是公开 API，
   *     别处（测试、将来的快捷键）传进来的 id 也要走同一条判据；
   *   - **自环与重复在 `addEdge` 里抛错**，这里转成 `status` 文案而不是异常：
   *     用户点错一次不该让整个组件树炸掉；
   *   - **记一步撤销**：连线是内容修改，凭什么不能 Ctrl+Z。
   */
  function connectNodes(
    source: string, target: string, label?: string | null, kind?: EdgeKind | null,
  ): boolean {
    if (!content.value) return false;
    if (!findNode(content.value.root, source) || !findNode(content.value.root, target)) {
      status.value = '端点节点不存在';
      return false;
    }
    if (edgesOf(content.value).length >= LIMITS.edges) {
      status.value = `最多 ${LIMITS.edges} 条附加关系，请先删掉一些`;
      return false;
    }
    let next: MindMapContent;
    try {
      next = addEdgeToContent(content.value, source, target, label, kind).content;
    } catch (error) {
      status.value = error instanceof Error ? error.message : '这条关系加不上';
      return false;
    }
    if (!applyContent(next, true)) return false;
    status.value = '已加一条附加关系，记得保存。';
    return true;
  }

  /** 删一条附加关系。删线不动结构，所以不检查节点数/层数，也不需要重算布局。 */
  function deleteEdge(edgeId: string): boolean {
    if (!content.value) return false;
    if (!edgesOf(content.value).some(edge => edge.id === edgeId)) return false;
    if (!applyContent(removeEdgeFromContent(content.value, edgeId), true)) return false;
    status.value = '已删除这条附加关系，可撤销。';
    return true;
  }

  function undo() {
    if (busy.value) {
      status.value = '正在生成导图，完成之后才能编辑。';
      return;
    }
    const snap = past.value.pop();
    if (!snap) return;
    pastBytes.value -= snapshotBytes(snap);
    // 当前是空画布时也要压一步 `'null'`，否则「空 → 候选 → 撤销 → 重做 → 撤销」
    // 的最后一步会丢掉：重做那一次不记历史，再撤销就没得可撤，回不到空白。
    future.value.push(snapshot() ?? 'null');
    restoreSnapshot(snap);
    dirty.value = true;
    // 撤销改的就是画布本身，必须让「候选基于哪个版本算出来」的判断看见这一步。
    // 不递增的话，用户撤销一步再点「应用到画布」，撤销白做、候选照样落地。
    clientRevision.value += 1;
    ensureSelection();
    editingId.value = null;
    status.value = '已撤销。';
  }

  function redo() {
    if (busy.value) {
      status.value = '正在生成导图，完成之后才能编辑。';
      return;
    }
    const snap = future.value.pop();
    if (!snap) return;
    // 与 undo 对称：当前是空画布时把空画布压进撤销栈，这样重做之后仍能撤回空白。
    recordPast(snapshot() ?? 'null');
    restoreSnapshot(snap);
    dirty.value = true;
    // 同 undo：重做同样把画布换成了另一棵树，候选再不成立。
    clientRevision.value += 1;
    ensureSelection();
    editingId.value = null;
    status.value = '已重做。';
  }

  /** 打开一张已保存的导图：撤销栈、选中、脏标记全部重置到"刚读出来"的状态。 */
  /**
   * 当前页面"正在看哪张图"的会话号。每次 loadMap / newMap 递增。
   *
   * 保存请求在途时用户可能切到另一张导图，那份响应回来时不能再拿它改当前
   * 页面的身份：否则 mapId 会被改回上一张，而画布上显示的还是新的那张，
   * 之后保存就会把 B 的内容写进 A。用会话号比比较 mapId 更严格——它也覆盖
   * 「保存中点了新建」（mapId 前后都是 null）这种情形。
   */
  let mapSession = 0;

  function loadMap(next: MindMapContent, record: { id: string; version: number; updated_at: string }) {
    cancel();
    mapSession += 1;
    // 换了一张图，上一张图算出来的候选没有任何意义：留着它，一次误点就会把
    // 那张图的内容装进这张图。
    candidate.value = null;
    resetCandidateOps();
    saving.value = false;
    titleDraft.value = null;
    content.value = next;
    mapId.value = record.id;
    version.value = record.version;
    savedAt.value = record.updated_at;
    mode.value = 'saved';
    dirty.value = false;
    setPrimary(null);
    editingId.value = null;
    viewAdjusted.value = false;
    resetHistory();
    status.value = `已打开导图，版本 ${record.version}。`;
  }

  function newMap() {
    cancel();
    mapSession += 1;
    // 同上：新建之后旧候选不再属于画布上的任何一张图。
    candidate.value = null;
    resetCandidateOps();
    saving.value = false;
    titleDraft.value = null;
    content.value = null;
    mapId.value = null;
    version.value = 0;
    savedAt.value = null;
    mode.value = null;
    dirty.value = false;
    setPrimary(null);
    editingId.value = null;
    resetHistory();
    status.value = '新建导图：输入主题生成，或直接在画布上手动编辑。';
  }

  function markSaved(record: { id: string; version: number; updated_at: string }) {
    mapId.value = record.id;
    version.value = record.version;
    savedAt.value = record.updated_at;
    dirty.value = false;
    mode.value = 'saved';
    status.value = `已保存，版本 ${record.version}。`;
  }

  /**
   * 保存。返回结果让界面分流提示：正常、内容不合法、版本冲突各走各的。
   * 失败时保留 dirty —— 一次网络抖动不能让用户以为已经存上了。
   */
  async function save(): Promise<SaveResult> {
    // 生成中途保存必然出脏数据：`dirty` 还是 false、`mapId` 还可能是 null，
    // POST 出去就是一条半截记录，`done` 到达后再保存又多出第二条。挡在这里，
    // 而不是留给后端去猜哪条该丢。
    if (busy.value || agentRunning.value) {
      status.value = '正在生成导图，完成之后才能保存。';
      return 'busy';
    }
    const current = content.value;
    if (!current) {
      status.value = '没有可保存的导图。';
      return 'empty';
    }
    // 朝向存在 content 里，但生成与示例拿到的 content 可能还带着后端默认值：
    // 保存前按当前视图归一，否则会出现"看着是向左、存下去是向右"。
    const payload = withDirection(current, direction.value);
    const invalid = validateDraft(payload);
    if (invalid) {
      status.value = invalid;
      return 'invalid';
    }
    if (saving.value) return 'busy';

    saving.value = true;
    status.value = '正在保存…';
    const session = mapSession;
    try {
      const record = mapId.value
        ? await updateMindMap(mapId.value, payload, version.value)
        : await createMindMap(payload);

      // 请求在途时用户可能已经切到别的导图（或点了新建）。这时响应属于
      // 上一张图，不能拿它改当前页面的 mapId —— 改了之后画布显示的是 B、
      // 身份却是 A，再点保存就会把 B 的内容写进 A。
      if (session !== mapSession) {
        status.value = `上一张导图已保存（版本 ${record.version}），当前导图未受影响。`;
        return 'saved';
      }

      // 请求在途时用户可能还在编辑。applyContent() 是整体替换 content.value，
      // 所以比一次引用就知道内容有没有变过。变了必须把脏标记恢复回来——
      // markSaved() 无条件把它清成 false，那样后改的内容会被误标成"已保存"，
      // 而请求体里发出去的只是点保存那一刻的树，刷新即丢。
      const changedDuringSave = content.value !== current;
      markSaved(record);
      if (changedDuringSave) {
        dirty.value = true;
        status.value = `已保存版本 ${record.version}，但保存期间又有修改，请再次保存。`;
      }
      return 'saved';
    } catch (error) {
      if (session !== mapSession) {
        return 'saved';
      }
      if (error instanceof ApiError && error.status === 409) {
        conflict.value = { message: error.message };
        status.value = '这张导图已被别人改过，本地修改还没保存。';
        return 'conflict';
      }
      status.value = error instanceof ApiError ? error.message : '保存失败，请重试。';
      return 'failed';
    } finally {
      // 切图后新页面的保存状态不能被打扫：只清理自己这一轮。
      if (session === mapSession) saving.value = false;
    }
  }

  /** 409 的选项一：放弃本地修改，换成服务端最新版本（撤销栈一并清空）。 */
  async function reloadFromServer(): Promise<void> {
    const id = mapId.value;
    if (!id) return;
    const session = mapSession;
    try {
      const record = await getMindMap(id);
      // 等待期间用户可能已经新建或切到别的图，这份响应不能再盖上去。
      if (session !== mapSession) return;
      loadMap(record.content, record);
      conflict.value = null;
    } catch (error) {
      status.value = error instanceof ApiError ? error.message : '读取服务端版本失败。';
    }
  }

  /** 409 的选项二：把当前内容存成一条新记录，两边的修改都保留。 */
  async function saveAsNew(): Promise<void> {
    const current = content.value;
    if (!current) return;
    const session = mapSession;
    try {
      const record = await createMindMap(current);
      if (session !== mapSession) {
        return;
      }
      const changedDuringSave = content.value !== current;
      markSaved(record);
      conflict.value = null;
      if (changedDuringSave) {
        dirty.value = true;
        status.value = `已另存为新导图（版本 ${record.version}），但保存期间又有修改，请再次保存。`;
      } else {
        status.value = `已另存为新导图，版本 ${record.version}。`;
      }
    } catch (error) {
      status.value = error instanceof ApiError ? error.message : '另存失败，请重试。';
    }
  }

  /** 409 的选项三：先不处理，本地修改继续留在页面上。 */
  function dismissConflict() {
    conflict.value = null;
    status.value = '已取消保存，本地修改还在，可以再试一次。';
  }

  async function openList() {
    listOpen.value = true;
    listBusy.value = true;
    try {
      maps.value = await listMindMaps();
    } catch (error) {
      maps.value = [];
      status.value = error instanceof ApiError ? error.message : '读取导图列表失败。';
    } finally {
      listBusy.value = false;
    }
  }

  function closeList() {
    listOpen.value = false;
  }

  async function openMap(id: string) {
    const session = mapSession;
    listBusy.value = true;
    try {
      const record = await getMindMap(id);
      // 点开之后等网络这段时间，用户可能已经关掉列表并新建了一张图。
      // 直接 loadMap 会把那张新画布替换成刚读回来的旧记录。
      if (session !== mapSession) return;
      loadMap(record.content, record);
      listOpen.value = false;
      conflict.value = null;
    } catch (error) {
      status.value = error instanceof ApiError ? error.message : '打开导图失败。';
    } finally {
      listBusy.value = false;
    }
  }

  /**
   * 换主选区，并丢掉追加选区。
   *
   * 所有"跳到某个节点去干活"的路径都走它（点选、加子节点、加兄弟、删完跳父节点、
   * 开始内联编辑、换图）：那些动作的语义是"现在只对着这一个节点"，留着上一批
   * 追加选区，下一次「改这张图」会带上一串用户早就忘了自己选过的 id。
   */
  function setPrimary(id: string | null) {
    extraSelectedIds.value = [];
    selectedId.value = id;
  }

  /**
   * 选中一个节点（清掉多选）。
   *
   * 只接受 `content` 里真实存在的节点。预览的派生 id 和候选的 id 都还没进画布：
   * 选中它们会让右侧详情面板、面包屑，以及 `expand` 请求里的 `selected_ids` 一起
   * 对着一个不存在的节点说话。画布那边同样给这些节点设了 `selectable: false`，
   * 这里是不依赖渲染层的最后一道。
   */
  function select(id: string | null) {
    if (id !== null && !(content.value && findNode(content.value.root, id))) return;
    setPrimary(id);
  }

  /**
   * 在画布上按住 Ctrl / Cmd / Shift 点一个节点：加进选区或从选区里拿掉。
   *
   * 主选区被取消时把第一个追加选区提上来（详情面板不能停在"没选中但有选区"
   * 那种状态：`selectedIds` 非空而 `selectedNode` 为 null，界面上每一个按钮都
   * 不知道自己该对谁说话）。
   *
   * 用例与 `select` 同一条判据：只有 `content` 里真实存在的节点才进得来。
   */
  function toggleSelect(id: string): void {
    if (!content.value || !findNode(content.value.root, id)) return;
    const rest = extraSelectedIds.value.filter(item => item !== id);
    if (selectedId.value === id) {
      const [promoted, ...remaining] = rest;
      selectedId.value = promoted ?? null;
      extraSelectedIds.value = remaining;
      return;
    }
    if (rest.length !== extraSelectedIds.value.length) {
      extraSelectedIds.value = rest;
      return;
    }
    if (selectedId.value === null) {
      selectedId.value = id;
      return;
    }
    extraSelectedIds.value = [...rest, id];
  }

  /** 清空整个选区：画布上点空白处走它。 */
  function clearSelection(): void {
    setPrimary(null);
  }

  /**
   * 从引用列表跳到节点：选中它，并让画布把它移到视野中心。
   *
   * 与 `select()` 分开的原因见 `focusTick`：跳转是"我要看这个"，点选是"我在看这个"。
   */
  function locate(id: string) {
    select(id);
    if (selectedId.value !== id) return;
    focusTick.value += 1;
  }

  /**
   * 换朝向。朝向写在 content 里，所以这是一次可撤销的内容修改：
   * 换了方向之后按 Ctrl+Z 能退回原布局，和改文字是同一套语义。
   */
  function setDirection(next: Direction) {
    if (!content.value) return;
    if (direction.value === next) return;
    if (!applyContent(withDirection(content.value, next), true)) return;
    // 换方向后整体重排，视图必须重新适配，否则可能停在空白处。
    viewAdjusted.value = false;
  }

  /**
   * 不经过网络直接装载一份导图，开发期的示例选择器用它预览布局。
   * 走 cancel() 先掐掉在途请求，避免旧流回来后把示例顶掉。
   */
  function preview(next: MindMapContent, dir: Direction) {
    cancel();
    // 换了一张图，会话必须跟着换：否则上一张图在途的保存响应回来时，
    // 会因为会话号没变而把它的 id 写进 mapId——画布上是新图、身份却是旧图，
    // 下次保存就把新内容 PUT 到旧记录上。
    mapSession += 1;
    // 换图即作废旧候选（preview 同样会换掉整棵树）。
    candidate.value = null;
    resetCandidateOps();
    titleDraft.value = null;
    // 上一张图在途的保存到这里就作废了：它的 finally 因为会话不匹配不会
    // 清理 saving，这里必须自己复位，否则新图永远卡在"保存中"，点保存
    // 只会返回 busy。loadMap / newMap / generate 都是同一套处理。
    saving.value = false;
    content.value = withDirection(next, dir);
    mode.value = 'demo';
    setPrimary(null);
    editingId.value = null;
    viewAdjusted.value = false;
    // 示例只是用来看布局的，不算用户的作品，别让它挂上"未保存"的包袱。
    mapId.value = null;
    version.value = 0;
    savedAt.value = null;
    dirty.value = false;
    resetHistory();
    status.value = '这是本地示例数据，用来检查布局与截断，不是模型生成的。';
  }

  /**
   * 载入一张预置的演示导图（内容见 `frontend/src/demo/presets.ts`）。
   *
   * 复用 `preview` 的"换图即换会话"处理：不做的话，上一张图在途的保存响应回来时
   * 会把旧 id 写进 `mapId`，画布是新图、身份却是旧图。`mapId` 保持为空，所以第一次
   * 保存会新建记录，不会覆盖别人的图。
   *
   * 只改一句状态文案：预置图是内容完整的演示材料，不是"用来看布局的示例"。
   */
  function loadPreset(preset: {
    label: string;
    content: MindMapContent;
    direction: Direction;
  }) {
    preview(preset.content, preset.direction);
    status.value = `已载入预置演示导图「${preset.label}」：可以直接改，也可以保存成一张新导图。`;
  }

  /**
   * 装载一份**导入**的导图。
   *
   * 与 `preview` 的三处差别，每一处都是刻意的：
   *
   *  1. `dirty = true`。导入的是用户的作品，他必须被提醒"还没保存"。示例数据是
   *     拿来检查布局的，不算作品，所以那边是 false。
   *  2. `mode = 'import'`。徽标要说清这张图的来历；混进 `saved` 会让人以为它已经
   *     在库里了，混进 `demo` 会让人以为这是假数据（他会不敢保存）。
   *  3. `mapId = null`。后端导入**只解析不落库**，所以第一次保存走 `createMindMap`
   *     建一条新记录，而不是 PUT 到某张既有图上。
   *
   * 朝向不经过 `withDirection`：文件自带的 `theme.direction` / `layout` 就是用户
   * 原来看到的样子，`direction` 是 content 的派生值（`directionOf`），设好 content
   * 之后画布自然按它排。强行套一个默认朝向会把一份"向左"的图导成向右。
   *
   * `resetHistory()`：导入不是一个可以撤销的编辑步骤——撤销回到导入前就意味着
   * "把画布清空"，而那不是用户点撤销时想要的东西。
   */
  function importMap(next: MindMapContent) {
    cancel();
    // 换图必须换会话号，否则上一张图在途的保存响应回来时会把它的 id 写进 mapId，
    // 而画布上是这张新导入的图——下次保存就把导入的内容 PUT 到旧记录上。
    mapSession += 1;
    candidate.value = null;
    resetCandidateOps();
    titleDraft.value = null;
    // 与 preview 同一条：上一张图在途的保存作废，saving 必须自己复位，否则新图
    // 永远卡在"保存中"。
    saving.value = false;
    content.value = next;
    mode.value = 'import';
    setPrimary(null);
    editingId.value = null;
    viewAdjusted.value = false;
    mapId.value = null;
    version.value = 0;
    savedAt.value = null;
    dirty.value = true;
    resetHistory();
    status.value = `已导入「${next.title}」，还没有保存。`;
  }

  /**
   * 掐掉在途的 agent 运行。
   *
   * 取消只是停止这一轮：画布上的 `content` 从来没被这一轮碰过（候选落地要用户
   * 点「应用」），所以不需要回滚，清掉属于这一轮的中间态就够了。
   * 换图场景（loadMap / newMap / preview）也走它，把旧流掐死。
   */  function cancel() {
    const active = controller;
    controller = null;
    active?.abort();
    busy.value = false;
    agentRunning.value = false;
    // 这一轮就此作废：告警跟着一起清，否则取消之后面板还挂着"部分分支未完成"。
    agentWarnings.value = [];
    // 取消/换图同理：长到一半的预览属于已经结束的那一次运行。
    clearAgentPreview();
    stageText.value = '';
    // 这一轮已经结束，"等了多久"不再有意义：不清的话，静默超 10s 后点取消，
    // 界面会一边写"已取消。"、一边挂着"模型正在思考，已等 20 秒"。
    waitedMs.value = 0;
    status.value = '已取消。';
  }

  // ---- agent 运行与候选接受 ----

  /**
   * 把一批 op 应用到候选上，返回是否应用成功。
   *
   * 只动 `candidate`，绝不写 `content`：采纳仍然要用户点「应用」。这是整条 agent
   * 路径的核心约束，不因为帧是增量的就默认落地。
   *
   * 失败与乱序都**静默丢弃**：`done` 帧会带完整树覆盖候选，少应用一批不会丢数据；
   * 而在这种时候弹一句"修改没能应用"，用户只会以为最终结果不可信。
   */
  function applyOpsToCandidate(batch: OpBatch): boolean {
    // edit / expand 这两条路上后端不发 snapshot（候选从请求带过去的树起步），
    // 所以候选要由画布那份树的副本兜底——`applyOps` 自己会克隆，不会改到 content。
    // 不兜底的话第一批 op 就没有落点，"打开旧图 → 让 AI 改"依旧一点反应都没有。
    const base = candidate.value ?? (candidateIntent === 'create' ? null : content.value);
    if (!base) return false;
    // 对不上就是中间漏了一批：在错位的树上继续套用只会套出错位的结构。
    if (batch.base_revision !== candidateRevision) {
      previewDebug(`[agent] 丢弃 ops 批次：base_revision=${batch.base_revision}，当前 ${candidateRevision}`);
      return false;
    }
    try {
      candidate.value = applyOps(base, batch).content;
    } catch (error) {
      previewDebug(`[agent] 丢弃 ops 批次：${error instanceof Error ? error.message : String(error)}`);
      return false;
    }
    // 只有真正落进候选才算这一版完成，失败那批不能推进账本。
    candidateRevision = batch.base_revision + 1;
    return true;
  }

  async function runGeneration(
    goal: string,
    intent: 'create' | 'expand' | 'edit' = 'create',
    sources?: Source[],
    thinkingModel: string = 'default',
    diagramType: DiagramTypeChoice = 'auto',
    options: RunOptions = {},
  ): Promise<void> {
    if (busy.value || agentRunning.value) return;
    // 来源合法性在这里再判一次。组件里已有同样的闸门，但 `runGeneration` 是唯一
    // 入口——绕过组件直调（测试，或将来加的快捷入口）时那一层就不在了。判一次
    // 只省一次 422 往返，权威仍是后端契约校验。
    if (sources && sources.length > 0) {
      const problem = sourceInputError(sources);
      if (problem) {
        agentError.value = problem;
        status.value = problem;
        return;
      }
    }
    const prompt = goal.trim();
    if (!prompt) {
      // 这里最容易踩的坑：用户把主题写进了「导图标题」（那个框只改名字、
      // 不触发生成），然后点生成，界面毫无动静。所以提示必须指出是哪个框，
      // 同时清掉上一次运行的进度——不清的话面板还挂着上一轮的失败卡片，
      // 看起来像"这次又失败了"。
      agentEvents.value = [];
      agentError.value = '';
      agentWarnings.value = [];
      clearAgentPreview();
      status.value = '请在画布右上角的输入框里写下主题或要改什么；「导图标题」那个框只改名字，不会重新生成。';
      return;
    }
    if (intent !== 'create' && !content.value) {
      status.value = '当前还没有导图，无法在此基础上修改。';
      return;
    }

    agentError.value = '';
    agentEvents.value = [];
    // 上一轮的告警不能带到这一轮：面板里挂着旧告警，用户会以为这次又缺分支。
    agentWarnings.value = [];
    // 同理，上一轮是演示还是模型，跟这一轮没关系；不清就会把上一轮的 demo
    // 标记挂到这一轮的候选上。
    agentMode.value = null;
    candidate.value = null;
    // 新一轮的 op 从第 0 版重新开始，上一轮的账本留着只会把首批 op 判成乱序。
    resetCandidateOps();
    // 上一轮长到一半的预览同样不能留：它的节点属于上一次运行，混进这一轮的
    // 面板里，用户会以为新任务已经长出了这些分支。
    clearAgentPreview();
    // 记下候选所依据的版本与所在的图：接受时用它判断期间有没有被改过、有没有换了图。
    // 意图也要记：接受时得知道这份内容算「新作品」还是「改现有这张图」。
    candidateBaseRevision = clientRevision.value;
    candidateSession = mapSession;
    candidateIntent = intent;
    // 选区按意图取：`expand` 只展开**一个**节点（后端开流前会 422 掉多选的请求），
    // `edit` 吃整个选区（用户圈了三个节点说「这几个合并成一个」，模型得知道是
    // 哪三个，否则只能从整棵树里猜）。`create` 没有选区。
    //
    // `options.selection` 是**显式覆盖**：看不见选区的入口（左侧「生成」按钮，
    // 那里没有「已选 N 个」徽标）必须传空数组，否则它会偷偷带着用户在画布上
    // 圈的东西去"重新生成"——用户看不到这件正在发生的事。
    const wantedSelection = intent === 'expand'
      ? (selectedId.value ? [selectedId.value] : [])
      : intent === 'edit' ? (options.selection ?? [...selectedIds.value]) : [];
    // 后端 `AgentRunRequest.selected_ids` 的 `max_length` 是 50：超了会被开流前的
    // 校验直接 422，而用户只是"按着 Ctrl 把一整片都点了一遍"。截断比报错好——
    // 多出来的那几个在提示词里本来就只是上下文，前 50 个足够说明他圈的是哪一片。
    const selectionIds = wantedSelection.slice(0, MAX_REQUEST_SELECTION);
    // diff 的「范围内/外」按**请求时**的选区判，之后用户点来点去不会改写这份记录。
    // 只对 `edit` 记：`expand` 的选区是"要展开的那一个节点"，新节点按构造就落在
    // 它下面，每一行都标「范围内」只是噪音；`create` 没有选区。
    candidateSelection.value = intent === 'edit' ? selectionIds : [];
    runIntent.value = intent;
    // 展开的预览要有一条祖先链兜着（`node_grow` 的 path 是父节点的索引链）。
    // 生成不用：它的根帧自带 path=null，预览从中心主题开始长。
    if (intent === 'expand' && selectedId.value) {
      seedExpandPreview(selectedId.value);
    }

    const ctrl = new AbortController();
    controller = ctrl;
    agentRunning.value = true;
    busy.value = true;
    stageText.value = '正在连接';
    status.value = '正在与模型协作…';
    waitedMs.value = 0;

    // 流只在 done / error 帧上才算有了明确结局；两者都没到就 EOF，说明连接被掐断了，
    // 不能当成正常结束——否则面板会停在「正在与模型协作…」，工具行永远显示「执行中…」。
    let settled = false;

    try {
      // 每次点击一个新的身份：幂等只在"带同一个 run_id"时生效，而"点了两次生成"
      // 本来就是两次运行（按钮禁用与 busy 闸门管那个）。真正需要同一个 id 的场景
      // 是刷新重发、换标签页、客户端重试——那时服务端能认出这是同一次运行。
      const runId = newRunId();
      activeRunId = runId;
      const body: AgentRunRequest = {
        goal: prompt,
        intent,
        run_id: runId,
        // 始终显式带上：即使宿主页面没给值也发 default，而不是省略字段。
        // 省略会把「前端没传」和「后端默认」两个概念混在一起，排查请求体时看不出
        // 用户到底选了哪套拆解方式；值本身与后端默认一致，传了不影响结果。
        thinking_model: thinkingModel || 'default',
        // create 不能带 snapshot（后端开流前会 422）；另外两种必须带
        snapshot: intent === 'create' ? null : content.value,
        // 选中节点是**上下文提示**，不是作用域限制：后端会把它拼进 system
        // （`service.selection_text`），说清"优先改这些节点及其子树，意图明显
        // 不限于它们时也允许改别处，但要在回复里说明"。所以这里是"我圈了谁"，
        // 不是"只许改谁"——「整张图都加上时间维度」这类请求仍然照常发得出去。
        selected_ids: selectionIds,
      };
      // 来源原样送去，一个字都不截：材料是"依据"，goal 是"要做什么"。
      // 把正文拼进 goal 会同时踩两个坑——被截断的材料丢掉大半，且模型分不清
      // 目标与素材。空数组不发：旧后端对多出来的字段是 extra="forbid"。
      if (sources && sources.length > 0) body.sources = sources;
      // 图型只在用户真的选了、且这次是新建时发出去。两条边界各有理由：
      //   - `auto`（= 没选）**省略字段**，不发假值：后端 `None` 的语义就是"让模型
      //     自己判断"，省略与它同一件事，而 `"auto"` 这种字符串会被 Literal 挡成
      //     422。省略还多换来一条性质：没选图型的请求与老前端逐字节相同。
      //   - `expand` / `edit` 不生成骨架，后端的编辑工具集里连生成工具都没有。
      //     那两个意图带图型只会让"图型只在新建时生效"这条边界变成一句空话。
      if (intent === 'create' && diagramType !== 'auto') body.diagram_type = diagramType;
      // 空数组不发：老后端对多出来的字段是 `extra="forbid"`，而"这次一条偏好都没用上"
      // 与"这个字段不存在"对读的人来说是同一件事。
      if (options.prefsApplied?.length) body.prefs_applied = [...options.prefsApplied];

      await runAgentStream(body, event => {
        // 被取消或被新请求接管的旧流：一帧都不许再改界面状态，
        // 否则旧回调会把新请求刚设好的 stageText / status / candidate 覆盖掉。
        if (controller !== ctrl) return;
        // 心跳不进 `agentEvents`：它只驱动状态行的"已等 N 秒"，面板里没有任何地方
        // 读它。而每帧都整数组复制是 O(n²)——长静默会累积几十帧，没必要为纯保活
        // 的帧付这个成本。
        if (event.type === 'heartbeat') {
          waitedMs.value = Math.round(event.waited_ms / 1000);
          return;
        }
        agentEvents.value = [...agentEvents.value, event];
        // 真进度帧到了，静默就结束了：等待计时必须跟着归零，否则「已等 25 秒」
        // 会一直挂在状态行上，看起来像卡住了——而那会儿节点已经在长。
        waitedMs.value = 0;
        switch (event.type) {
          case 'stage':
            stageText.value = event.detail || event.stage;
            break;
          case 'sources':
            // 后端刚把材料写进知识库，临时 id 已被换成持久 id。不换的话这张图里
            // 的 `source_refs.source_id` 重新打开后指不到原文——引用计数、点开看
            // 原文、会话内反向索引全部失效，而且是静默失效。
            sourcesStore.remap(event.mapping);
            break;
          case 'tool_start':
            // 状态行显示的也是人话：用户读不到 `mindmap_generate` 这种函数名。
            stageText.value = agentToolLabel(event.name, 'running');
            break;
          case 'tool_end': {
            stageText.value = event.ok
              ? agentToolLabel(event.name, 'done')
              : `${event.name} 未成功`;
            // 后端分片生成会以 ok=true 报"结果不完整"：partial 与 diagnostics 都在
            // result_summary 里。不在这里接下来，面板与状态行都看不见它。
            const warnings = agentWarningsFrom(event.result_summary);
            if (warnings.length) {
              recordWarnings(warnings);
              // 候选可能已经先到了（snapshot 与工具帧的先后不固定）。这时状态行
              // 还写着"候选已就绪"，得跟着改，不能让两句文案在同一个面板上打架。
              if (candidate.value && event.ok) status.value = candidateStatus('候选已就绪');
            }
            break;
          }
          case 'node_grow':
            // 只进预览树。真正可采纳的内容在 snapshot / done 的 `content` 里，
            // 两者可能不一致——把预览当候选，等于把一棵没校验过的树交给用户应用。
            growPreviewNode(event.path, event.label, event.expandable);
            break;
          case 'ops':
            // `edit` 意图的改动**只**以 op 批次下发（没有整树快照）。不接这一帧，
            // "打开旧图 → 让 AI 改 → 界面没反应"就是必然结果。
            if (applyOpsToCandidate(event.batch)) {
              status.value = `已应用 ${event.batch.ops.length} 条修改，确认后才会应用到画布。`;
            }
            break;
          case 'snapshot':
            // 后端整树校验通过后先推一帧 snapshot：done 之前用户就该看得见候选长什么样。
            // 完整候选一到，预览就没有存在意义了，整棵作废（含还等着挂的缓存）。
            clearAgentPreview();
            // 只放候选、不碰 content——没经用户确认的内容不许进画布。
            candidate.value = event.content;
            status.value = candidateStatus('候选已就绪');
            break;
          case 'done':
            settled = true;
            // 这棵树的来历只有 done 帧说得准，落在这里等「接受」时再写进 mode。
            // 后端 `exclude_none=True`，所以正常帧一定带值；缺字段只说明对面是
            // 没填 mode 的旧版本，记作"没说"，不能替它认成模型产出。
            agentMode.value = event.mode ?? null;
            // 终态已到，预览再留着只会让人以为还有节点在路上。
            clearAgentPreview();
            if (event.content) {
              // 只放进候选，不动 content：用户确认之前画布保持原样
              candidate.value = event.content;
              status.value = candidateStatus(
                agentMode.value === 'demo' ? '演示导图已就绪（不是 AI 生成的）' : '模型给出了候选导图',
              );
            } else if (!candidate.value) {
              // 已经收到过 snapshot 的候选就留着：这一帧只是没重复带内容。
              status.value = '模型这次没有产出导图。';
            }
            break;
          case 'error':
            settled = true;
            // 失败时预览同样作废：它可能正好停在半截，留着会被当成"这次的结果"。
            clearAgentPreview();
            agentError.value = event.message;
            status.value = event.message;
            break;
        }
      }, ctrl.signal);

      // 已经被后来的请求接管：这个旧请求无论成功、失败还是被取消，都不许再改状态。
      if (controller !== ctrl) return;
      if (!settled && !ctrl.signal.aborted) {
        // 流正常结束但没有终态帧：中途断了，不能假装成功。
        agentError.value = '连接中断，结果可能不完整，请重试。';
        status.value = '连接中断，结果可能不完整，请重试。';
      }
    } catch (error) {
      if (controller !== ctrl) return;
      const message = error instanceof ApiError ? error.message : '运行失败，请重试。';
      agentError.value = message;
      status.value = message;
    } finally {
      // 取消后立刻重新运行、或换图后新请求已接管时，旧请求的收尾不能清掉新请求的状态。
      if (controller === ctrl) {
        controller = null;
        agentRunning.value = false;
        busy.value = false;
        stageText.value = '';
        // 断流/异常退出时同理：这一轮已经结束，别再留着"模型正在思考"。
        waitedMs.value = 0;
      }
    }
  }

  /** 这一轮是不是"展开某个选中节点"。按钮文字与互斥判定都读它。 */
  const expanding = computed(() => agentRunning.value && runIntent.value === 'expand');

  /**
   * 这一轮是不是"在生成"（新建 / 编辑也算），用来与展开互斥。
   *
   * 展开与生成互斥不是保守，是必要的：`runGeneration` 一进来就 `clearAgentPreview`
   * 并换掉 `controller`，两轮流重叠时后到的那个会把前一个刚长出来的预览与候选一起
   * 作废——用户看到的是"点了一下，之前的进度消失了"。
   */
  const generating = computed(() => agentRunning.value && runIntent.value !== 'expand');

  /**
   * 展开当前选中的节点：走同一个 `/agent/runs/stream`，只是意图换成 `expand`。
   *
   * 与生成的差别只有三处，其余全部复用（候选、op 帧、版本检查、接受/丢弃都还是
   * 那一条路）：
   *
   *  1. `goal` 写的是"展开哪个节点"，而不是用户的主题；
   *  2. `intent="expand"` + `selected_ids=[node_id]`（`runGeneration` 里按意图补的）；
   *  3. `snapshot` 是**当前整棵树**——后端要从它上面挂子节点，而它也可能有一堆
   *     没保存的编辑，重读数据库会把那些编辑丢掉。
   *
   * `sources` 允许为空：用户重新打开一张旧导图时前端手里没有正文，后端会按节点上
   * 的 `source_refs` 从知识库里把材料找回来（`tools._expand_sources`）。这里不因为
   * "没带材料"就拦下按钮——那正是这条链要打通的那个场景。
   *
   * `expandable === 'no'` 时不发请求：后端也会判（`too_deep` / 模型说没有更多），
   * 但让用户先点一次再看一句"到底了"是白花一次调用。
   */
  async function expandSelected(sources?: Source[], count = 3): Promise<void> {
    if (busy.value || agentRunning.value) return;
    const node = selectedNode.value?.node;
    if (!node) {
      status.value = '先在画布上选中一个节点，再展开它。';
      return;
    }
    if (node.expandable === 'no') {
      status.value = `「${node.label.trim()}」已经到底了，没有更细的内容可展开。`;
      return;
    }
    const label = node.label.trim();
    // `goal` 是外层模型看到的那一句话（`_expand` 自己也拿得到选中节点与路径，
    // 所以这里不需要把路径拼进去——拼了就有两处口径）。
    const goal = `展开节点「${label}」，补充 ${count} 个子节点。`;
    await runGeneration(goal, 'expand', sources);
  }

  /**
   * 把候选应用到画布。
   *
   * 四道校验缺一不可：候选属于当前这张图、有候选、没有未提交的输入、树没被改过。
   * 少任何一条，用户手打了一半的标题、刚做的编辑，或另一张图的内容都会被候选
   * 静默覆盖——那正是这次改造要解决的问题，不能在新路径上重演。
   */
  function acceptCandidate(): 'applied' | 'stale' | 'invalid' {
    // 换图判在"有没有候选"之前：切图会把 candidate 清成 null，但 candidateSession
    // 还留着上一张图的号码。两者不等就说明这次候选属于另一张图，装上去等于把
    // 那张图的内容写进当前记录。
    if (candidateSession !== mapSession) {
      candidate.value = null;
      resetCandidateOps();
      status.value = '候选是在另一张导图上生成的，已失效，请重新运行。';
      return 'stale';
    }
    if (!candidate.value) return 'invalid';
    if (editingId.value !== null || titleDraft.value !== null) {
      status.value = '请先提交或取消正在编辑的内容。';
      return 'stale';
    }
    if (clientRevision.value !== candidateBaseRevision) {
      status.value = '候选生成之后这张图被改过，已失效，请重新运行。';
      return 'stale';
    }
    // 装进画布之前必须整树校验一次。候选是模型产出的外部数据，它的 label
    // 长度、引用区间、节点数都可能越界；不在这里拦，脏数据会先进画布、
    // 直到用户点保存才被后端 422 拒绝，而那时他已经在上面改了半天。
    const invalid = validateDraft(candidate.value);
    if (invalid) {
      status.value = `候选不合法，已丢弃：${invalid}`;
      candidate.value = null;
      resetCandidateOps();
      return 'invalid';
    }
    // 到这里候选可以落地了。先按意图处理"这份内容是谁"：
    // create 是一份新作品，绝不能继承上一张图的身份——否则接受之后点保存会走
    // PUT，把刚打开的那条旧记录整条覆盖掉。
    if (candidateIntent === 'create') {
      mapId.value = null;
      version.value = 0;
      savedAt.value = null;
      // 上一张图在途的保存响应回来时不能再把它的 id 写回 mapId（那是"新图挂旧身份"
      // 的另一条路径），所以会话跟着换；saving 也自己复位——那一轮请求的 finally
      // 会因为会话不匹配跳过清理，不复位就会永远卡在"保存中"。
      mapSession += 1;
      saving.value = false;
      resetHistory();
      // 新作品不接上一张图的历史：撤销栈里只留「接受之前」这一步。
      pushHistoryOrEmpty();
    } else {
      // 改的是现有这张图：身份、版本、历史都沿用，撤销回到改动前的那棵树。
      pushHistory();
    }
    content.value = candidate.value;
    candidate.value = null;
    // 候选落地之后账本归零：这份内容已经进了 content，下一轮从第 0 版重来。
    resetCandidateOps();
    // 来源标记跟着 done 帧走，不再一律写成「模型」：演示模式产出的是一棵占位树，
    // 给它贴"模型生成"的标签就是让界面替它撒谎。没说（旧后端）才退回 model。
    mode.value = agentMode.value ?? 'model';
    dirty.value = true;
    clientRevision.value += 1;
    editingId.value = null;
    // 告警说的是"这份候选缺了什么"，候选已落地就没有再挂着的理由；
    // 留着它，用户会以为画布上这棵树还有问题等着处理。
    agentWarnings.value = [];
    ensureSelection();
    status.value = mode.value === 'demo'
      ? '已应用演示导图（固定结构，不是 AI 生成），记得保存。'
      : '已应用模型的修改，记得保存。';
    // 告诉服务端这次候选被采纳了：没有这一步，"done 但已应用"与"done 但没应用"
    // 分不开，下次进页面还会问同一句。放在最后：上报失败不能影响已经落地的内容。
    reportRunResolution('applied');
    return 'applied';
  }

  function discardCandidate(): void {
    const had = candidate.value !== null;
    candidate.value = null;
    resetCandidateOps();
    agentError.value = '';
    agentWarnings.value = [];
    status.value = had ? '已丢弃候选，画布未变。' : '没有待处理的候选。';
    // 丢弃同样要落一笔：否则那张表分不清"用户还没看到"和"用户已经嫌弃过"。
    // 只在**真有候选**时上报——没有候选却被记成"用户嫌弃过"，会把"关掉页面那
    // 40 秒"刚捡回来的草稿永久地判死。
    if (had) reportRunResolution('dismissed');
  }

  return {
    content,
    mode,
    status,
    stageText,
    waitedMs,
    busy,
    selectedId,
    // 多选：主选区（`selectedId`）+ 追加选区 → `selectedIds`。界面上的「已选 N 个」
    // 与请求里的 `selected_ids` 都读后者。
    selectedIds,
    toggleSelect,
    clearSelection,
    direction,
    viewAdjusted,
    editingId,
    titleDraft,
    setTitleDraft,
    mapId,
    version,
    savedAt,
    dirty,
    layout,
    flatNodes,
    nodes,
    edges,
    nodeCount,
    // 附加关系：当前树上的全部边 + 与"选中节点"相关的那几条（侧栏用）。
    contentEdges,
    selectedEdges,
    connectNodes,
    deleteEdge,
    selectedNode,
    selectedRefs,
    refIndex,
    // 候选 diff：`edit` / `expand` 意图的确认界面靠它说清"改了哪几个节点"
    candidateDiff,
    // 这一轮请求带上去的选区：diff 的「范围内/外」按它判（不是按此刻的选区）
    candidateSelection,
    changedNodeIds,
    focusTick,
    breadcrumb,
    canUndo,
    canRedo,
    undoDepth,
    undoBytes,
    saving,
    conflict,
    maps,
    listOpen,
    listBusy,
    cancel,
    preview,
    loadPreset,
    importMap,
    select,
    locate,
    setDirection,
    beginEdit,
    endEdit,
    commitLabel,
    commitNote,
    setNodeVisualById,
    renameTitle,
    addChildTo,
    addSiblingTo,
    commitAndAddChild,
    deleteNode,
    undo,
    redo,
    loadMap,
    newMap,
    markSaved,
    save,
    reloadFromServer,
    saveAsNew,
    dismissConflict,
    openList,
    closeList,
    openMap,
    // agent 路径
    clientRevision,
    candidate,
    agentPreview,
    agentPreviewScope,
    agentPreviewDropped,
    agentEvents,
    agentWarnings,
    agentRunning,
    agentError,
    agentMode,
    runIntent,
    expanding,
    generating,
    runGeneration,
    expandSelected,
    acceptCandidate,
    discardCandidate,
    // 上次运行留下的结果（run 身份）
    resumableRun,
    probeResumableRun,
    resumeRun,
    dismissResumable,
  };
});
