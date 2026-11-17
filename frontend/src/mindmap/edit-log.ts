/**
 * 编辑日志：把"用户改了什么"记成可以统计的事实。
 *
 * 这一版**只记录、不总结**，理由写在 `docs/` 之外的注释里也一样成立：
 * 后面那几步（LLM 总结 → DPO 微调）需要几百条偏好对，而一天几次的生成要攒几个月；
 * 但"改动集中在一两个具体问题上"（比如"80% 的改动是把长标题改短"）只要跑一次统计
 * 就能看出来，那种问题用规则一天就能修。所以先把事实攒下来。
 *
 * ## 为什么 diff 三份而不是只比节点
 *
 * 一次改动可能落在三处，任何一处漏了，统计就答不上"最常改什么"：
 *
 *  1. **节点字段**（label / detail / note / color / icon）—— 内容与样式；
 *  2. **树结构**（父变更、同层重排、新增、删除）；
 *  3. **边**（附加关系的增删改）。
 *
 * ## 三条判断，都影响统计怎么读
 *
 * **一个字段一条事件。** 改一个节点的 label 同时改 color 会产生 2 条，不是 1 条。
 * 合并成一条的话，"by_field" 就必须把一条事件同时算进两个桶，`sum(by_field)` 与
 * `by_kind.update` 对不上——统计一旦不能自校验，读的人就得先怀疑数字再怀疑结论。
 * 分开之后代价是"一次操作产生几条事件"这件事只留在 `at` 里（同一次提交共享同一个
 * 时刻字符串，`GROUP BY at` 能把它还原成一次操作）。
 *
 * **id 变了就是 remove + add，不做相似度匹配。** 节点 id 由 `newId()`（`n_` +
 * 随机）或后端分配，"删掉再加一个同名节点"必然是新 id。按 label 或位置去猜"这其实
 * 是同一次修改"会把统计变成一件需要解释算法的事，而它现在只需要读 SQL。
 *
 * **顺序变化只在"这一层成员没变"时才算 move。** 单纯比 sibling index 会炸：
 * 一次"在 50 个兄弟中间插一个"会让后面每个兄弟的 index 都 +1，于是 1 次真实操作
 * 变成 50 条 move，`by_kind` 立刻失真。所以 index 变化只有在**该父节点的子 id 集合
 * 完全没变**时才上报——那才是"重排"（agent 的 `move` op 带 index 就会走到这里）。
 *
 * ## 刻意不记的三件事
 *
 *  - **`title` 之外的整图字段**（`layout` / `theme` / `source_usage` / `relations`）。
 *    换朝向（`setDirection`）一次会产生一条噪音，而它回答不了"生成侧该改什么"。
 *  - **撤销 / 重做**：一次被撤销的编辑不是用户的最终意图。记了它，`label_shrunk`
 *    会同时收到"18→9"和"9→18"两条，平均值被拉回中性、次数却翻倍——比不记更糟。
 *    要审计"用户反复撤销"该是另一个指标，这一版不做。
 *  - **create 意图接受候选**（空画布 → 一整棵树）：整棵树都是"新增"，那不是 diff，
 *    是把整棵树重念一遍。与 `stores/mindmap.ts::diffCandidate` 对 `create` 返回
 *    `null` 是同一条判断。
 */
import type { MindEdge, MindMapContent, MindNode } from '../api/types';
import { charLength } from './edits';

/** 谁改的。`user` 是手工编辑（改文字、加节点、删节点、连线），`agent` 是应用候选。 */
export type EditActor = 'user' | 'agent';

/**
 * 改动种类。
 *
 * `update` 是节点**内容**，`style` 是节点**外观**。分成两种而不是都叫 `update`：
 * "用户在调样式上花了多少改动"本身是个信号（样式占比高说明生成侧的视觉默认值不对），
 * 混进 update 就再也拆不出来了。
 */
export type EditKind = 'add' | 'remove' | 'update' | 'move' | 'edge' | 'style';

/**
 * 改动落在哪个字段上。**只有 `kind` 为 `update` / `style` 的事件才有值。**
 *
 * 这条规则让统计能自校验：`sum(by_field) === by_kind.update + by_kind.style`。
 * 新增与删除改的其实是父节点的 `children`，但把它们也标成 `children` 会让上面那条
 * 恒等式失效，而 `by_kind` 已经把"加了多少、删了多少"说清楚了。
 *
 * `title` 是导图标题（不是节点标题）：用户改标题也是一次真实改动，漏掉它，统计就
 * 回答不了"最常改什么"。`label_shrunk` 只统计 `field='label'`，两者不会混。
 */
export type EditField = 'label' | 'title' | 'detail' | 'note' | 'color' | 'icon';

/**
 * 一条改动。
 *
 * `before` / `after` 是**摘要**，不是内容：
 *  - `label` / `title` 存全文（≤200 字，统计要算长度，截了就没法算）；
 *  - `detail` / `note` 只存长度（`"1800字"`）——2000 字的正文进日志会把这张表撑成
 *    第二个 `mindmaps.content`，而"从 1800 字改成 700 字"这件事本身就是情报；
 *  - `color` / `icon` 存原值或 `"无"`；
 *  - `move` 存父节点标题（`"风险"` / `"根"`）或位次（`"第2位"`）；
 *  - `edge` 存 `"A → B（是）"`。
 */
export interface EditEvent {
  actor: EditActor;
  kind: EditKind;
  /** 节点 id。`edge` 事件填边的**源**节点，`title` 事件没有节点。 */
  nodeId?: string;
  field?: EditField;
  before?: string;
  after?: string;
  /**
   * 涉及的层数，**根是 1**（与契约"最多 8 层"同一口径）。
   *
   * `remove` 填删除前所在层，`add` / `move` / `update` / `style` 填改动后的层。
   * 统计里的 `removed_depth` 直接读它，回答"删掉的集中在第几层"——第 3 层最多
   * 就说明初次生成挖得太深。
   */
  depth?: number;
  /** 这次改动动的是哪个 run 的产物。只有应用候选那一步能对上，手工编辑留空。 */
  runId?: string;
  /** ISO 时刻。**同一次提交内所有事件共享同一个值**，它是"一次操作"的分组键。 */
  at: string;
}

/**
 * 单次保存最多带多少条事件。
 *
 * 500 是"一次生成用户可能改 20 处"的 25 倍，正常永远碰不到；它防的是"一次删掉
 * 整棵树"把请求体撑到几 MB。超出的部分**留在队列里等下一次保存**（是分批，不是
 * 丢弃），只把条数（`truncated`）上报给服务端记一条 warning——不在表里插一条假
 * 事件，因为统计的 `total` 必须是"真实改动数"。
 */
export const EDIT_EVENT_LIMIT = 500;

/** 摘要里标题的截断长度。日志是给人看的一行，不需要整段标题。 */
const SUMMARY_LABEL_CHARS = 40;

interface Indexed {
  node: MindNode;
  parentId: string | null;
  /** 根是 1。 */
  depth: number;
  /** 在父节点 `children` 里的下标。 */
  index: number;
}

interface TreeIndex {
  entries: Map<string, Indexed>;
  /**
   * 每个节点的"子 id 集合指纹"（排序后拼起来）。
   *
   * 它只服务一件事：区分"重排"与"插入导致的 index 位移"。前者成员集合不变，
   * 后者变了。
   */
  siblingKeys: Map<string, string>;
}

function fingerprint(children: readonly MindNode[]): string {
  return children.map(child => child.id).sort().join('\u0000');
}

function indexTree(root: MindNode): TreeIndex {
  const entries = new Map<string, Indexed>();
  const siblingKeys = new Map<string, string>();
  entries.set(root.id, { node: root, parentId: null, depth: 1, index: 0 });

  const walk = (node: MindNode, depth: number) => {
    siblingKeys.set(node.id, fingerprint(node.children));
    node.children.forEach((child, index) => {
      entries.set(child.id, { node: child, parentId: node.id, depth: depth + 1, index });
      walk(child, depth + 1);
    });
  };
  walk(root, 1);
  return { entries, siblingKeys };
}

function clip(text: string): string {
  const chars = Array.from(text);
  return chars.length > SUMMARY_LABEL_CHARS
    ? `${chars.slice(0, SUMMARY_LABEL_CHARS).join('')}…`
    : chars.join('');
}

/** 父节点在摘要里怎么称呼。找不到（不该发生）退回 id，好过让一行日志空着。 */
function nameAt(index: TreeIndex, id: string | null): string {
  if (!id) return '根';
  const entry = index.entries.get(id);
  return entry ? clip(entry.node.label.trim()) : id;
}

/** `detail` / `note` 只进长度。`"0字"` 表示被清空，读的时候不用再猜。 */
function charsOf(text: string | null | undefined): string {
  return `${charLength((text ?? '').trim())}字`;
}

function visualOf(value: string | null | undefined): string {
  return value ?? '无';
}

function describeEdge(edge: MindEdge, index: TreeIndex): string {
  const text = `${nameAt(index, edge.source)} → ${nameAt(index, edge.target)}`;
  const label = (edge.label ?? '').trim();
  return label ? `${text}（${label}）` : text;
}

export interface DiffOptions {
  actor: EditActor;
  /** 同一次提交共享的时刻（`new Date().toISOString()` 由调用方取一次）。 */
  at: string;
  runId?: string;
}

/**
 * 两份内容之间的改动清单。
 *
 * `before` 或 `after` 为空（create 意图、空画布）时返回空数组：整棵树都是新的，
 * 不是 diff。见文件头那段"刻意不记"。
 */
export function diffEdits(
  before: MindMapContent | null,
  after: MindMapContent | null,
  options: DiffOptions,
): EditEvent[] {
  if (!before || !after) return [];

  const events: EditEvent[] = [];
  const emit = (event: Omit<EditEvent, 'actor' | 'at' | 'runId'>) => {
    events.push({
      actor: options.actor,
      at: options.at,
      ...(options.runId ? { runId: options.runId } : {}),
      ...event,
    });
  };

  const oldTitle = (before.title ?? '').trim();
  const newTitle = (after.title ?? '').trim();
  if (oldTitle !== newTitle) {
    emit({ kind: 'update', field: 'title', before: oldTitle, after: newTitle });
  }

  const oldIndex = indexTree(before.root);
  const newIndex = indexTree(after.root);

  for (const [id, entry] of newIndex.entries) {
    const old = oldIndex.entries.get(id);
    if (!old) {
      emit({ kind: 'add', nodeId: id, after: clip(entry.node.label.trim()), depth: entry.depth });
      continue;
    }

    const oldLabel = old.node.label.trim();
    const newLabel = entry.node.label.trim();
    if (oldLabel !== newLabel) {
      emit({
        kind: 'update', field: 'label', nodeId: id,
        before: oldLabel, after: newLabel, depth: entry.depth,
      });
    }
    if ((old.node.detail ?? '').trim() !== (entry.node.detail ?? '').trim()) {
      emit({
        kind: 'update', field: 'detail', nodeId: id,
        before: charsOf(old.node.detail), after: charsOf(entry.node.detail), depth: entry.depth,
      });
    }
    if ((old.node.note ?? '').trim() !== (entry.node.note ?? '').trim()) {
      emit({
        kind: 'update', field: 'note', nodeId: id,
        before: charsOf(old.node.note), after: charsOf(entry.node.note), depth: entry.depth,
      });
    }
    if ((old.node.color ?? null) !== (entry.node.color ?? null)) {
      emit({
        kind: 'style', field: 'color', nodeId: id,
        before: visualOf(old.node.color), after: visualOf(entry.node.color), depth: entry.depth,
      });
    }
    if ((old.node.icon ?? null) !== (entry.node.icon ?? null)) {
      emit({
        kind: 'style', field: 'icon', nodeId: id,
        before: visualOf(old.node.icon), after: visualOf(entry.node.icon), depth: entry.depth,
      });
    }

    if (old.parentId !== entry.parentId) {
      emit({
        kind: 'move', nodeId: id,
        before: nameAt(oldIndex, old.parentId), after: nameAt(newIndex, entry.parentId),
        depth: entry.depth,
      });
      // 根没有"位次"（index 恒 0），所以下面那条 `old.parentId !== null` 不是补丁，
      // 是这条判据的适用前提。
    } else if (
      old.parentId !== null
      && old.index !== entry.index
      // 只有"这一层成员没变"才是重排；插一个兄弟会让后面所有人的 index 都变。
      && oldIndex.siblingKeys.get(old.parentId) === newIndex.siblingKeys.get(old.parentId)
    ) {
      emit({
        kind: 'move', nodeId: id,
        before: `第${old.index + 1}位`, after: `第${entry.index + 1}位`, depth: entry.depth,
      });
    }
  }

  for (const [id, entry] of oldIndex.entries) {
    if (newIndex.entries.has(id)) continue;
    emit({
      kind: 'remove', nodeId: id,
      before: clip(entry.node.label.trim()), depth: entry.depth,
    });
  }

  const oldEdges = new Map((before.edges ?? []).map(edge => [edge.id, edge]));
  const newEdges = new Map((after.edges ?? []).map(edge => [edge.id, edge]));

  for (const [id, edge] of newEdges) {
    const previous = oldEdges.get(id);
    if (!previous) {
      emit({ kind: 'edge', nodeId: edge.source, after: describeEdge(edge, newIndex) });
      continue;
    }
    // 端点不变而 label / kind 变了：还是同一条线，只是换了画法与说明。
    if ((previous.label ?? null) !== (edge.label ?? null)
      || (previous.kind ?? null) !== (edge.kind ?? null)
      || previous.source !== edge.source || previous.target !== edge.target) {
      emit({
        kind: 'edge', nodeId: edge.source,
        before: describeEdge(previous, oldIndex), after: describeEdge(edge, newIndex),
      });
    }
  }
  for (const [id, edge] of oldEdges) {
    if (newEdges.has(id)) continue;
    emit({ kind: 'edge', nodeId: edge.source, before: describeEdge(edge, oldIndex) });
  }

  return events;
}
