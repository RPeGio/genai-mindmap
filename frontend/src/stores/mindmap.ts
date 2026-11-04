import type { Edge as FlowEdge, Node as FlowNode } from '@vue-flow/core';
import { defineStore } from 'pinia';
import { computed, ref } from 'vue';

import { ApiError, createMindMap, generateFromSources, generateMindMapStream, getMindMap, listMindMaps, updateMindMap, uploadDocument } from '../api/client';
import type { MapRecord, MindMapContent, MindNode, Source } from '../api/types';
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

/**
 * 上传上限，与后端 `ingestion/router.py` 的 MAX_UPLOAD_BYTES 同一口径。
 * 前端先拦一道：超过上限的文件传一遍必定 413，白等一趟网络还占着带宽。
 */
export const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;

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

  /** 保存相关状态：mapId 为空表示这份导图还没存过。 */
  const mapId = ref<string | null>(null);
  const version = ref(0);
  const savedAt = ref<string | null>(null);
  const dirty = ref(false);

  /** 撤销/重做存整树 JSON 快照：比命令模式少一半代码，300 节点的树一步也不到 100KB。 */
  const past = ref<string[]>([]);
  const future = ref<string[]>([]);

  const saving = ref(false);
  /** 保存撞上 409 时置位，由界面弹窗让用户选"重新加载 / 另存为新导图 / 取消"。 */
  const conflict = ref<{ message: string } | null>(null);
  /** 打开列表用的缓存。 */
  const maps = ref<MapRecord[]>([]);
  const listOpen = ref(false);
  const listBusy = ref(false);

  let controller: AbortController | null = null;

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

  function applyContent(next: MindMapContent, history: boolean) {
    if (history) pushHistory();
    content.value = next;
    dirty.value = true;
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

    applyContent({ ...content.value, root: renameNode(content.value.root, id, next) }, true);
    if (editingId.value === id) editingId.value = null;
    status.value = '已修改，记得保存。';
  }

  function renameTitle(title: string) {
    if (!content.value) return;
    const next = title.trim();
    if (next === content.value.title) return;
    if (!next) {
      status.value = '标题不能为空';
      return;
    }
    if (Array.from(next).length > LIMITS.title) {
      status.value = `标题最多 ${LIMITS.title} 个字`;
      return;
    }
    applyContent({ ...content.value, title: next }, true);
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
    applyContent({ ...content.value, root }, true);
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
    applyContent({ ...content.value, root }, true);
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
    applyContent({ ...content.value, root: removeNode(content.value.root, id) }, true);
    selectedId.value = parent?.id ?? null;
    editingId.value = null;
    status.value = '已删除该节点及其子节点，可撤销。';
  }

  function undo() {
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
    saving.value = false;
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
    saving.value = false;
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
    try {
      const record = await getMindMap(id);
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
    listBusy.value = true;
    try {
      const record = await getMindMap(id);
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

  function cancel() {
    const active = controller;
    controller = null;
    active?.abort();
    busy.value = false;
    stageText.value = '';
    status.value = '已取消。';
  }

  async function generate(text: string): Promise<void> {
    const prompt = text.trim();
    if (!prompt) {
      status.value = '请输入主题或文本。';
      return;
    }
    if (busy.value) return;

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
    mapId.value = null;
    version.value = 0;
    savedAt.value = null;
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
      if (controller !== null && controller !== ctrl) return;
      if (ctrl.signal.aborted) {
        restore();
        status.value = '已取消。';
      } else if (!settled) {
        // 流正常结束但没有 done 帧：中途断了，不能假装成功。
        restore();
        status.value = '连接中断，结果可能不完整，请重试。';
      }
    } catch (error) {
      if (controller !== null && controller !== ctrl) return;
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
    }
  }

  // ---- 上传文档 → 解析 → 生成 ----

  /** 上传链路的进度。idle 之外的两种状态界面各有各的提示，不能并成一个布尔值。 */
  const uploadState = ref<'idle' | 'parsing' | 'generating'>('idle');
  /** 解析已等待的秒数：首次解析 DOCX 要加载模型，实测 47 秒，只转圈用户会以为卡死。 */
  const parseSeconds = ref(0);
  const uploadError = ref('');
  /** 最近一次解析出的来源：截断提示要从它身上取总页数与已解析页数。 */
  const uploadSource = ref<Source | null>(null);

  const uploadBusy = computed(() => uploadState.value !== 'idle');

  /**
   * 截断提示。后端最多解析 50 页，正文缺了尾巴；界面若只说"解析成功"，
   * 用户看导图时会以为后面那些章节本来就没有内容。
   */
  const truncateHint = computed(() => {
    const source = uploadSource.value;
    if (!source?.truncated) return '';
    const total = source.span?.page;
    const parsed = source.parsed_pages;
    if (typeof total === 'number' && typeof parsed === 'number') {
      return `本文档共 ${total} 页，仅处理前 ${parsed} 页`;
    }
    // 拿不到总页数时也不能让提示消失，否则截断又变回静默行为。
    return typeof parsed === 'number'
      ? `文档较长，本次只处理了前 ${parsed} 页`
      : '文档较长，本次只处理了一部分内容';
  });

  let parseTimer: ReturnType<typeof setInterval> | null = null;

  function stopParseTimer() {
    if (parseTimer !== null) {
      clearInterval(parseTimer);
      parseTimer = null;
    }
  }

  function startParseTimer() {
    stopParseTimer();
    parseSeconds.value = 0;
    parseTimer = setInterval(() => {
      parseSeconds.value += 1;
    }, 1000);
  }

  /**
   * 上传 → 解析 → 生成一条龙。选完文件直接调，不再要第二次确认。
   *
   * 失败时不动画布上的旧图：解析失败和生成失败都只提示原因。
   * 用户手上那张导图仍在，比清成白板更接近预期，也免得无谓地丢掉未保存的修改。
   *
   * 解析 + 生成可能持续几十秒（PDF 逐页识别尤其慢）。这期间画布仍然可编辑，
   * 用户在等待过程中改的东西不能被结果静默盖掉，所以落图前要检查：
   *   - 导图身份变了（切到别的图或点了新建）→ 结果属于上一张，不能套到当前图上
   *   - 内容变过（用户改了节点）→ 尊重用户的改动，先问一句
   * 两种情形都交给 confirmReplace 决定，没提供回调时一律保留现状。
   */
  async function uploadAndGenerate(
    file: File,
    options?: { confirmReplace?: (reason: 'edited' | 'switched') => boolean },
  ): Promise<void> {
    // 同一时刻只走一条链路：preview() 会掐掉在途的流式请求，反过来流式结束也会顶掉新结果。
    if (uploadState.value !== 'idle' || busy.value) return;

    // 记录等待开始时的状态，生成回来时用来判断用户有没有动过
    const sessionAtStart = mapSession;
    const contentAtStart = content.value;

    uploadError.value = '';
    uploadSource.value = null;
    parseSeconds.value = 0;

    if (file.size > MAX_UPLOAD_BYTES) {
      uploadError.value = `「${file.name}」超过 10MB 上限，请压缩或拆分后再上传。`;
      status.value = uploadError.value;
      return;
    }

    uploadState.value = 'parsing';
    status.value = `正在解析「${file.name}」…`;
    startParseTimer();

    let source: Source;
    try {
      source = await uploadDocument(file);
    } catch (error) {
      stopParseTimer();
      uploadState.value = 'idle';
      // 后端的 detail 已经写清了原因（旧版二进制格式、扫描件没有文本层、内容为空），
      // 原样展示即可；统一成"上传失败"用户就不知道该改什么了。
      uploadError.value = error instanceof ApiError ? error.message : '上传失败，请重试。';
      status.value = uploadError.value;
      return;
    }

    stopParseTimer();
    uploadSource.value = source;

    const text = source.text?.trim() ?? '';
    if (!text) {
      // 后端对空正文会走 422，这里再兜一层：真收下空来源也不该送去生成。
      uploadState.value = 'idle';
      uploadError.value = source.error?.trim() || '这份文档没有解析出任何文字。';
      status.value = uploadError.value;
      return;
    }

    uploadState.value = 'generating';
    status.value = `已解析「${source.filename ?? file.name}」，正在生成导图…`;

    try {
      const result = await generateFromSources([source]);

      // 落图之前先看用户在这几十秒里做了什么
      const switched = sessionAtStart !== mapSession;
      const edited = content.value !== contentAtStart;
      if (switched || edited) {
        const reason = switched ? 'switched' : 'edited';
        const replace = options?.confirmReplace?.(reason) ?? false;
        if (!replace) {
          uploadError.value = switched
            ? '生成结果已就绪，但期间已切换到别的导图，已保留当前内容。'
            : '生成结果已就绪，但期间你修改过当前导图，已保留你的修改。';
          status.value = uploadError.value;
          return;
        }
      }

      // preview() 会把模式与脏标记按"本地示例"重置，所以调完再按真实来源改回来。
      preview(result.content, direction.value);
      mode.value = result.mode;
      // 生成结果还没存过，标脏：离开页面要提醒，保存时走"创建"。
      dirty.value = true;

      const label = source.filename ?? file.name;
      const cut = source.truncated ? '（正文已截断）' : '';
      status.value =
        result.mode === 'demo'
          ? `已根据上传的「${label}」生成演示导图（固定结构，非 AI 生成）${cut}，记得保存。`
          : `已根据上传的「${label}」生成导图${cut}，可直接编辑，记得保存。`;
    } catch (error) {
      // 生成失败不要把解析结果也一并吞掉：来源还留着，界面才能继续显示页数与截断提示。
      uploadError.value = error instanceof ApiError ? error.message : '生成失败，请重试。';
      status.value = uploadError.value;
    } finally {
      uploadState.value = 'idle';
    }
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
    uploadState,
    uploadBusy,
    parseSeconds,
    uploadError,
    uploadSource,
    truncateHint,
    uploadAndGenerate,
  };
});
