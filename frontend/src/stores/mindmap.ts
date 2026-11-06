import type { Edge as FlowEdge, Node as FlowNode } from '@vue-flow/core';
import { defineStore } from 'pinia';
import { computed, ref } from 'vue';

import { ApiError, createMindMap, generateFromSources, generateMindMapStream, getMindMap, listMindMaps, runAgentStream, updateMindMap } from '../api/client';
import type { AgentEvent, MapRecord, MindMapContent, MindNode, Source } from '../api/types';
import { sourceInputError } from '../sources/input';
import { layoutTidy, childSide, NODE_SIZE, type ChildSide, type Direction, type LayoutResult } from '../canvas/layout/tidy';
import {
  addChild,
  addSibling,
  countNodes,
  findNode,
  findParent,
  LIMITS,
  removeNode,
  renameNode,
  validateDraft,
} from '../mindmap/edits';

/** 画布节点上最多显示的字数（按码点算，避免把 emoji 切成半个）。 */
export const TRUNCATE_CHARS = 24;

/** 撤销栈上限。整树快照一步几 KB，50 步封顶，内存与手感都够用。 */
export const HISTORY_LIMIT = 50;

/** 保存的结果分类：界面据此决定提示什么，而不是把所有失败都说成"保存失败"。 */
export type SaveResult = 'saved' | 'conflict' | 'invalid' | 'empty' | 'busy' | 'failed';

/** 节点上显示的短标题：按码点截断，超出加省略号。 */
export function displayTitle(node: MindNode): string {
  const chars = Array.from(node.label.trim());
  return chars.length > TRUNCATE_CHARS ? `${chars.slice(0, TRUNCATE_CHARS).join('')}…` : chars.join('');
}

/**
 * 卡片与详情面板里的正文：契约 v2 的 `note` 优先，v1 没有该字段时回退 `label` 全文。
 * v2 落地后这里不需要改调用方，只改这一个函数的取值。
 */
export function displayBody(node: MindNode): string {
  const note = node.note?.trim();
  return note && note.length > 0 ? note : node.label.trim();
}

export interface MindNodeData {
  /** 截断后的短标题，画在节点框里 */
  label: string;
  /** 悬停卡片与详情面板里的全文 */
  body: string;
  /** 完整 label（不截断），供 title 属性与无障碍读屏使用 */
  fullLabel: string;
  depth: number;
  /** 需要渲染的源句柄：子节点分布在哪几侧就要哪几个（双向布局的根两侧都有） */
  out: ChildSide[];
  /** 需要渲染的目标句柄：连线从父节点进来时落在哪一侧 */
  in: ChildSide[];
}

export interface FlatNode {
  node: MindNode;
  depth: number;
  parentId: string | null;
}

/**
 * 画布节点。Vue Flow 的 `Node` 输入类型里没有 `selected`（它把选中当内部状态），
 * 但 `:nodes` 变化时 applyChanges 会把 `selected` 同步进去，所以这里补上这个字段的类型。
 */
export type MindFlowNode = FlowNode<MindNodeData> & { selected: boolean };

function flatten(root: MindNode): FlatNode[] {
  const out: FlatNode[] = [];
  const walk = (node: MindNode, depth: number, parentId: string | null) => {
    out.push({ node, depth, parentId });
    for (const child of node.children) walk(child, depth + 1, node.id);
  };
  walk(root, 0, null);
  return out;
}

export const useMindMapStore = defineStore('mindmap', () => {
  const content = ref<MindMapContent | null>(null);
  const mode = ref<'demo' | 'model' | 'saved' | null>(null);
  const status = ref('输入主题或文本后生成，节点会边生成边出现。');
  const stageText = ref('');
  const waitedMs = ref(0);
  const busy = ref(false);
  const selectedId = ref<string | null>(null);
  const direction = ref<Direction>('right');
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

  /** 撤销/重做存整树 JSON 快照：比命令模式少一半代码，300 节点的树一步也不到 100KB。 */
  const past = ref<string[]>([]);
  const future = ref<string[]>([]);

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

  /** 给面板显示的工具进度与阶段。 */
  const agentEvents = ref<AgentEvent[]>([]);
  const agentRunning = ref(false);
  const agentError = ref('');

  /** 保存撞上 409 时置位，由界面弹窗让用户选"重新加载 / 另存为新导图 / 取消"。 */
  const conflict = ref<{ message: string } | null>(null);
  /** 打开列表用的缓存。 */
  const maps = ref<MapRecord[]>([]);
  const listOpen = ref(false);
  const listBusy = ref(false);

  let controller: AbortController | null = null;

  /**
   * 当前这轮流式生成的"回滚到生成前"函数。
   *
   * 用户主动点取消时要用它把旧图还回来；而换图（loadMap / newMap / preview）
   * 引起的失效则不能回滚——那时画布上已经是要展示的新内容了。
   */
  let pendingRestore: (() => void) | null = null;

  const layout = computed<LayoutResult | null>(() =>
    content.value ? layoutTidy(content.value.root, { direction: direction.value }) : null,
  );

  const flatNodes = computed<FlatNode[]>(() =>
    content.value ? flatten(content.value.root) : [],
  );

  const nodes = computed<MindFlowNode[]>(() => {
    const current = layout.value;
    if (!current) return [];

    const parentOf = new Map<string, string | null>();
    for (const { node, parentId } of flatNodes.value) parentOf.set(node.id, parentId);

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

      return {
        id: node.id,
        type: 'mind',
        draggable: false,
        connectable: false,
        // 选中态由 store 说了算。只在点击时靠 Vue Flow 自己切换的话，代码里改选中
        // （加节点、删节点后跳到父节点）画布上看不出来，用户会以为还停在原来那个节点上。
        selected: selectedId.value === node.id,
        position: {
          x: point.x - NODE_SIZE.width / 2,
          y: point.y - NODE_SIZE.height / 2,
        },
        data: {
          label: displayTitle(node),
          body: displayBody(node),
          fullLabel: node.label,
          depth,
          out: outSides,
          in: inSides,
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
    return out;
  });

  const nodeCount = computed(() => flatNodes.value.length);

  const selectedNode = computed<FlatNode | null>(
    () => flatNodes.value.find(entry => entry.node.id === selectedId.value) ?? null,
  );

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

  /** 改动前调用：记一步历史，并让重做栈失效（树从这里长出新的分支了）。 */
  function pushHistory() {
    const snap = snapshot();
    if (!snap) return;
    past.value.push(snap);
    if (past.value.length > HISTORY_LIMIT) past.value.shift();
    future.value = [];
  }

  function resetHistory() {
    past.value = [];
    future.value = [];
  }

  /** 选中的节点可能已被删掉：兜底清空，免得详情面板指向不存在的节点。 */
  function ensureSelection() {
    const target = selectedId.value;
    if (target && content.value && !findNode(content.value.root, target)) {
      selectedId.value = null;
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
    selectedId.value = id;
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
    selectedId.value = node.id;
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
    selectedId.value = node.id;
    editingId.value = node.id;
    status.value = '已加兄弟节点，输入文字后按回车确认。';
  }

  function deleteNode(id: string) {
    if (!content.value) return;
    if (content.value.root.id === id) {
      status.value = '根节点不能删除';
      return;
    }

    const parent = findParent(content.value.root, id);
    if (!applyContent({ ...content.value, root: removeNode(content.value.root, id) }, true)) return;
    selectedId.value = parent?.id ?? null;
    editingId.value = null;
    status.value = '已删除该节点及其子节点，可撤销。';
  }

  function undo() {
    if (busy.value) {
      status.value = '正在生成导图，完成之后才能编辑。';
      return;
    }
    const snap = past.value.pop();
    if (!snap) return;
    const current = snapshot();
    if (current) future.value.push(current);
    content.value = JSON.parse(snap) as MindMapContent;
    dirty.value = true;
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
    const current = snapshot();
    if (current) past.value.push(current);
    content.value = JSON.parse(snap) as MindMapContent;
    dirty.value = true;
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
    saving.value = false;
    titleDraft.value = null;
    content.value = next;
    mapId.value = record.id;
    version.value = record.version;
    savedAt.value = record.updated_at;
    mode.value = 'saved';
    dirty.value = false;
    selectedId.value = null;
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
    saving.value = false;
    titleDraft.value = null;
    content.value = null;
    mapId.value = null;
    version.value = 0;
    savedAt.value = null;
    mode.value = null;
    dirty.value = false;
    selectedId.value = null;
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
    const current = content.value;
    if (!current) {
      status.value = '没有可保存的导图。';
      return 'empty';
    }
    const invalid = validateDraft(current);
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
        ? await updateMindMap(mapId.value, current, version.value)
        : await createMindMap(current);

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
   * 把增量节点合并进当前树。
   * 同一个 id 会反复到达（文字逐渐变长），所以已存在时更新文字、保留其子节点。
   */
  function upsert(node: MindNode, parentId: string, incoming: MindNode): MindNode {
    if (node.id === parentId) {
      const index = node.children.findIndex(child => child.id === incoming.id);
      const children =
        index >= 0
          ? node.children.map((child, i) =>
              i === index ? { ...incoming, children: child.children } : child,
            )
          : [...node.children, incoming];
      return { ...node, children };
    }
    return { ...node, children: node.children.map(child => upsert(child, parentId, incoming)) };
  }

  function applyNode(parentId: string | null, incoming: MindNode) {
    if (!content.value) {
      content.value = { title: incoming.label, root: incoming };
      return;
    }
    const root = content.value.root;
    if (parentId === null) {
      content.value = { ...content.value, root: { ...incoming, children: root.children } };
    } else {
      content.value = { ...content.value, root: upsert(root, parentId, incoming) };
    }
  }

  function select(id: string | null) {
    selectedId.value = id;
  }

  function setDirection(next: Direction) {
    direction.value = next;
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
    // 换图即作废旧候选（preview 也被「多来源生成」复用，它同样会换掉整棵树）。
    candidate.value = null;
    titleDraft.value = null;
    // 上一张图在途的保存到这里就作废了：它的 finally 因为会话不匹配不会
    // 清理 saving，这里必须自己复位，否则新图永远卡在"保存中"，点保存
    // 只会返回 busy。loadMap / newMap / generate 都是同一套处理。
    saving.value = false;
    direction.value = dir;
    content.value = next;
    mode.value = 'demo';
    selectedId.value = null;
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
   * 掐掉在途的流式生成。
   *
   * `restorePrevious` 为真表示这是用户主动取消，应该把生成前的导图还回来；
   * 换图场景（loadMap / newMap / preview）传 false —— 那时画布上已经是要
   * 展示的新内容，再回滚旧图反而是错的。
   */
  function cancel(restorePrevious = false) {
    const active = controller;
    controller = null;
    active?.abort();
    busy.value = false;
    agentRunning.value = false;
    stageText.value = '';
    sourceGenerationError.value = '';
    if (restorePrevious && pendingRestore) {
      pendingRestore();
    }
    pendingRestore = null;
    status.value = '已取消。';
  }

  async function generate(text: string): Promise<void> {
    const prompt = text.trim();
    if (!prompt) {
      status.value = '请输入主题或文本。';
      return;
    }
    if (busy.value || sourceGenerating.value) return;
    sourceGenerationError.value = '';

    // 先把上一份结果存起来：新请求失败时恢复它，而不是让用户白等一场还丢了旧图。
    const previous = content.value;
    const previousMode = mode.value;
    const previousSave = {
      mapId: mapId.value,
      version: version.value,
      savedAt: savedAt.value,
      dirty: dirty.value,
    };

    busy.value = true;
    waitedMs.value = 0;
    stageText.value = '正在连接';
    content.value = null;
    mode.value = null;
    selectedId.value = null;
    editingId.value = null;
    viewAdjusted.value = false;
    // 生成出来的是新导图，还没存过；上一份已保存的导图不能因此被覆盖。
    // 会话也要跟着换：上一份图在途的保存响应回来时不能把它的 id 写进 mapId，
    // 否则画布上是新图、身份是旧图，下次保存就把新内容 PUT 到旧记录上。
    mapSession += 1;
    titleDraft.value = null;
    mapId.value = null;
    version.value = 0;
    savedAt.value = null;
    saving.value = false;
    dirty.value = false;
    resetHistory();

    const ctrl = new AbortController();
    controller = ctrl;
    // settled 表示这次请求已经有了明确结局（成功或明确失败）；
    // 只有「既没 done 也没 error」才说明连接是被掐断的。
    let settled = false;

    const restore = () => {
      content.value = previous;
      mode.value = previousMode;
      mapId.value = previousSave.mapId;
      version.value = previousSave.version;
      savedAt.value = previousSave.savedAt;
      dirty.value = previousSave.dirty;
    };
    // 注册给 cancel()：用户点取消时由它负责回滚，因为那时 controller 已经被
    // 清空，收尾分支认不出这次中断属于"主动取消"还是"换图"。
    pendingRestore = restore;

    try {
      await generateMindMapStream(
        prompt,
        event => {
          switch (event.type) {
            case 'stage':
              stageText.value = event.detail;
              break;
            case 'skeleton':
              // 只有 demo 模式会发这个事件：它一次性给出整棵树，不走增量推送。
              content.value = event.content;
              break;
            case 'node':
              // 增量方案：节点一个个长出来，同一 id 的文字会越补越全。
              applyNode(event.parentId, event.node as MindNode);
              stageText.value = '节点生成中';
              status.value = '正在生成，节点会陆续出现…';
              break;
            case 'heartbeat':
              waitedMs.value = Math.round(event.waitedMs / 1000);
              break;
            case 'done':
              settled = true;
              content.value = event.content;
              mode.value = event.mode;
              // 生成结果还没存过，标脏：离开页面前会提醒，保存时走"创建"。
              dirty.value = true;
              status.value =
                event.mode === 'demo'
                  ? '这是固定结构的演示数据，不是 AI 生成。'
                  : `模型已生成候选导图，用时 ${(event.latencyMs / 1000).toFixed(1)} 秒，可直接编辑，记得保存。`;
              stageText.value = '';
              break;
            case 'error':
              settled = true;
              // 后端在流里报错时，之前那份导图同样要还回来，不能只留一个空画布。
              restore();
              status.value = event.message;
              stageText.value = '';
              break;
          }
        },
        ctrl.signal,
      );

      // 已经被后来的请求接管：这个旧请求无论成功、失败还是被取消，
      // 都不许再改画面和状态，否则新请求的预览会被旧导图替换。
      // 只要当前 controller 不是自己（被新请求接管，或被 cancel() 
      // 清成了 null），这个旧请求就无权再改画面。旧写法要求 controller 非空，
      // 于是「生成中点新建」时 controller 被清成 null，restore() 照样执行，
      // 新建出来的空白画布又被旧图盖了回去。
      if (controller !== ctrl) return;
      if (ctrl.signal.aborted) {
        restore();
        status.value = '已取消。';
      } else if (!settled) {
        // 流正常结束但没有 done 帧：中途断了，不能假装成功。
        restore();
        status.value = '连接中断，结果可能不完整，请重试。';
      }
    } catch (error) {
      if (controller !== ctrl) return;
      restore();
      status.value = ctrl.signal.aborted
        ? '已取消。'
        : error instanceof ApiError
          ? error.message
          : '生成失败，请重试。';
      stageText.value = '';
    } finally {
      // 取消后立刻重新生成时，旧请求的收尾不能把新请求的状态清掉。
      if (controller === ctrl) {
        busy.value = false;
        controller = null;
      }
      // 本轮已经收尾，回滚注册作废，免得下一次 cancel() 拿到过期的闭包。
      if (pendingRestore === restore) pendingRestore = null;
    }
  }

  // ---- 多来源统一生成；收集与解析在 sources store 中完成 ----

  const sourceGenerating = ref(false);
  const sourceGenerationError = ref('');
  let sourceController: AbortController | null = null;

  function cancelSourceGeneration() {
    sourceController?.abort();
    sourceController = null;
    sourceGenerating.value = false;
    sourceGenerationError.value = '';
    status.value = '已取消生成，来源和当前导图已保留。';
  }

  async function generateSources(
    sources: Source[],
    options?: { confirmReplace?: (reason: 'edited' | 'switched') => boolean },
  ): Promise<void> {
    if (sourceGenerating.value || busy.value) return;
    sourceGenerationError.value = sourceInputError(sources);
    if (sourceGenerationError.value) {
      status.value = sourceGenerationError.value;
      return;
    }

    const sessionAtStart = mapSession;
    const contentAtStart = content.value;
    const ctrl = new AbortController();
    sourceController = ctrl;
    sourceGenerating.value = true;
    status.value = `正在合并 ${sources.length} 个来源生成导图…`;

    try {
      const result = await generateFromSources(sources, ctrl.signal);
      if (sourceController !== ctrl) return;

      // 生成期间仍可编辑旧图；包括还没有提交的节点或标题草稿。
      const switched = sessionAtStart !== mapSession;
      const edited = content.value !== contentAtStart
        || editingId.value !== null
        || titleDraft.value !== null;
      if ((switched || edited) && !options?.confirmReplace?.(switched ? 'switched' : 'edited')) {
        sourceGenerationError.value = switched
          ? '生成结果已就绪，但期间已切换到别的导图，已保留当前内容。'
          : '生成结果已就绪，但期间你修改过当前导图，已保留你的修改。';
        status.value = sourceGenerationError.value;
        return;
      }

      preview(result.content, direction.value);
      mode.value = result.mode;
      dirty.value = true;
      status.value = result.mode === 'demo'
        ? `已合并 ${sources.length} 个来源生成演示导图（固定结构，非 AI 生成），记得保存。`
        : `已合并 ${sources.length} 个来源生成导图，可直接编辑，记得保存。`;
    } catch (error) {
      if (sourceController !== ctrl) return;
      sourceGenerationError.value = error instanceof ApiError ? error.message : '生成失败，请重试。';
      status.value = sourceGenerationError.value;
    } finally {
      // 取消之后马上重试时，迟到的旧响应不能清掉新一轮状态。
      if (sourceController === ctrl) {
        sourceController = null;
        sourceGenerating.value = false;
      }
    }
  }

  // ---- agent 运行与候选接受 ----

  /**
   * 是否走 agent 新路径。
   *
   * 默认关闭：现有两条生成通道（流式 / 批量）已经过端到端验证，新路径要能被
   * 一条环境变量关掉退回旧行为，而不是靠回滚代码。
   */
  const agentEnabled = String(import.meta.env?.VITE_MINDMAP_AGENT ?? '') === '1';

  async function runGeneration(
    goal: string,
    intent: 'create' | 'expand' | 'edit' = 'create',
  ): Promise<void> {
    if (busy.value || agentRunning.value) return;
    const prompt = goal.trim();
    if (!prompt) {
      // 这里最容易踩的坑：用户把主题写进了「导图标题」（那个框只改名字、
      // 不触发生成），然后点生成，界面毫无动静。所以提示必须指出是哪个框，
      // 同时清掉上一次运行的进度——不清的话面板还挂着上一轮的失败卡片，
      // 看起来像"这次又失败了"。
      agentEvents.value = [];
      agentError.value = '';
      status.value = '请在下方「输入主题或文本」里写下要生成的内容；上面的「导图标题」只改名字，不会重新生成。';
      return;
    }
    if (intent !== 'create' && !content.value) {
      status.value = '当前还没有导图，无法在此基础上修改。';
      return;
    }

    agentError.value = '';
    agentEvents.value = [];
    candidate.value = null;
    // 记下候选所依据的版本与所在的图：接受时用它判断期间有没有被改过、有没有换了图
    candidateBaseRevision = clientRevision.value;
    candidateSession = mapSession;

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
      await runAgentStream({
        goal: prompt,
        intent,
        // create 不能带 snapshot（后端开流前会 422）；另外两种必须带
        snapshot: intent === 'create' ? null : content.value,
        selected_ids: intent === 'expand' && selectedId.value ? [selectedId.value] : [],
      }, event => {
        // 被取消或被新请求接管的旧流：一帧都不许再改界面状态，
        // 否则旧回调会把新请求刚设好的 stageText / status / candidate 覆盖掉。
        if (controller !== ctrl) return;
        agentEvents.value = [...agentEvents.value, event];
        switch (event.type) {
          case 'stage':
            stageText.value = event.detail || event.stage;
            break;
          case 'tool_start':
            stageText.value = `正在调用 ${event.name}`;
            break;
          case 'tool_end':
            stageText.value = event.ok ? `${event.name} 完成` : `${event.name} 未成功`;
            break;
          case 'done':
            settled = true;
            if (event.content) {
              // 只放进候选，不动 content：用户确认之前画布保持原样
              candidate.value = event.content;
              status.value = '模型给出了候选导图，确认后才会应用到画布。';
            } else {
              status.value = '模型这次没有产出导图。';
            }
            break;
          case 'error':
            settled = true;
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
      }
    }
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
      return 'invalid';
    }
    // 一次 agent 运行只记一条撤销记录，而不是每个 op 一条
    pushHistory();
    content.value = candidate.value;
    candidate.value = null;
    mode.value = 'model';
    dirty.value = true;
    clientRevision.value += 1;
    editingId.value = null;
    ensureSelection();
    status.value = '已应用模型的修改，记得保存。';
    return 'applied';
  }

  function discardCandidate(): void {
    const had = candidate.value !== null;
    candidate.value = null;
    agentError.value = '';
    status.value = had ? '已丢弃候选，画布未变。' : '没有待处理的候选。';
  }

  return {
    content,
    mode,
    status,
    stageText,
    waitedMs,
    busy,
    selectedId,
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
    selectedNode,
    breadcrumb,
    canUndo,
    canRedo,
    saving,
    conflict,
    maps,
    listOpen,
    listBusy,
    generate,
    cancel,
    preview,
    select,
    setDirection,
    beginEdit,
    endEdit,
    commitLabel,
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
    sourceGenerating,
    sourceGenerationError,
    generateSources,
    cancelSourceGeneration,
    // agent 路径
    clientRevision,
    candidate,
    agentEvents,
    agentRunning,
    agentError,
    agentEnabled,
    runGeneration,
    acceptCandidate,
    discardCandidate,
  };
});
