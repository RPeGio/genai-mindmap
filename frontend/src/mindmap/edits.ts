import type { DiagramContent, EdgeKind, MindEdge, MindNode, NodeColor } from '../api/types';
import { containersOf, edgesOfContent, isFishbone, isFlow, rootsOf, stageOfNode } from './shape';

/**
 * 与服务端 Pydantic 约束逐条对应（`backend/app/contracts/mindmap.py`）：
 * 节点 id ≤100、label 1–200、children ≤50、总数 ≤1000、深度 ≤8、title 1–200。
 * 前端先拦一次，省掉一次必然失败的请求；服务端仍是最终裁判（422）。
 */
export const LIMITS = {
  nodes: 1000,
  depth: 8,
  children: 50,
  label: 200,
  note: 2000,
  /** 生成产出的说明（契约 `Node.detail`）。与 `note` 同容量、不同语义。 */
  detail: 2000,
  title: 200,
  /** 节点图标（契约 `MAX_NODE_ICON_CHARS`）。emoji 组合字符也要放得下。 */
  icon: 16,
  /**
   * 附加边条数（契约 `MAX_EDGES`）。
   *
   * 与上面几个不同的地方：**它是"建议上限"而不是硬边界**。后端超过 200 只报
   * `EDGE_LIMIT`（warn），图照常能存；这里用它做"还能不能再加一条"的判据，因为
   * 界面上的「连一条边」是用户主动的动作，第 201 条时明确拒绝比默默画上去好。
   */
  edges: 200,
  /** 边上的文字（契约 `MAX_EDGE_LABEL_CHARS`）。 */
  edgeLabel: 40,
} as const;

/**
 * 边的语义类型**运行时清单**，与后端 `mindmap.EDGE_KINDS` 一一对应。
 *
 * 与 `NODE_COLORS` 同一个位置、同一条理由：下拉框按它列选项，渲染层按它挑画法。
 * 写死在两个组件里的话，加一种类型就要改两处，而漏掉的那处不会报错——只是那种
 * 边永远选不到、也画不出区别。
 */
export const EDGE_KINDS = ['flow', 'branch', 'loop'] as const satisfies readonly EdgeKind[];

/** `kind` → 下拉框里的中文名。「顺序」是默认值，所以放在第一。 */
export const EDGE_KIND_LABELS: Record<EdgeKind, string> = {
  flow: '顺序（实线）',
  branch: '条件分支（换色）',
  loop: '回退（虚线）',
};

/**
 * 节点语义色的**运行时清单**，与后端 `mindmap.NODE_COLORS` 一一对应。
 *
 * 放在这里而不是 `api/types.ts`：那个文件是纯类型镜像，而这里是"契约里的数字与
 * 枚举值"的镜像（`LIMITS` 就在同一个位置、同一条理由）。调色板要**两处共读**：
 * 渲染层按它挑 CSS 变量，详情面板的下拉框按它列选项。写死在组件里，加一个颜色
 * 就要改两个文件，而漏掉的那个不会报错——只是那个颜色永远选不到。
 *
 * 顺序就是下拉框里的顺序：先主色，再语义（正向/注意/风险），最后是中性。
 */
export const NODE_COLORS = [
  'blue', 'green', 'amber', 'red', 'purple', 'teal', 'pink', 'gray',
] as const satisfies readonly NodeColor[];

/** 调色板键 → 下拉框里给人看的中文名。键本身是渲染用的标识，不该直接上屏。 */
export const NODE_COLOR_LABELS: Record<NodeColor, string> = {
  blue: '蓝（主色）',
  green: '绿（结论）',
  amber: '琥珀（注意）',
  red: '红（风险）',
  purple: '紫（并列主题）',
  teal: '青（过程）',
  pink: '粉（人/组织）',
  gray: '灰（背景）',
};

/**
 * 一个值是不是调色板键。
 *
 * 执行器用它拒绝 `color: '#2563eb'` 这种 op——**两端必须同一口径**：后端
 * `ops._palette_color` 会抛 ValidationError，前端放过去的话，模型看到的反馈
 * 取决于调用的是哪一端。
 */
export function isNodeColor(value: unknown): value is NodeColor {
  return typeof value === 'string' && (NODE_COLORS as readonly string[]).includes(value);
}

/** 整棵树里有没有节点带视觉元素。用于"要不要提示旧图没有颜色"这类判断。 */
export function hasVisuals(root: MindNode): boolean {
  if (root.color || root.icon) return true;
  return root.children.some(hasVisuals);
}

// ---- 附加边（流程图的条件跳转 / 汇合 / 回退）----
//
// 三份小工具：找节点、增删边、校验。都放在这里而不是 `ops.ts`，理由是
// `stores/mindmap.ts`（用户手点「连一条边」）与 `ops.ts`（模型发来的 op 批次）
// 都要用同一套语义；两处各写一遍，"用户点的时候能连、模型连的时候被拒"只是时间问题。

/**
 * 边集合里所有边（缺省空数组）。旧图没有这个字段，所有读取点都走它。
 *
 * 三种内容里**只有鱼骨没有 `edges`**（它的顶层结构里只有 `relations`），判定收在
 * `shape.edgesOfContent` 一处：传进来是鱼骨就是没有边，返回空数组——画布少画一条
 * 线好过让每个调用点各判一次形状。
 */
export function edgesOf(content: DiagramContent | null | undefined): MindEdge[] {
  return content ? edgesOfContent(content) : [];
}

/** 按 id 找边；找不到返回 `null`。 */
export function findEdge(edges: readonly MindEdge[], id: string): MindEdge | null {
  return edges.find(edge => edge.id === id) ?? null;
}

/** 新边 id。前缀 `e_` 与后端 `new_edge_id` 一致，日志里一眼分得清线与节点。 */
export function newEdgeId(): string {
  return `e_${newId().slice(2)}`;
}

/**
 * 加一条边。返回新内容与这条边；**不动原对象**。
 *
 * 与 `addChild` 同一套约定：纯函数、结构共享、调用方拿到新内容自己决定何时提交。
 * 两条会立刻被服务端拒的形态在这里先拦：
 *  - 自环（画不出来）；
 *  - 完全相同的 `(source, target, label)`（后端去重，这里直接不允许创建，
 *    免得用户点两次"添加"后看到一条线却没发现自己点了两下）。
 *
 * `label` 不同则允许：判断节点的"是"与"否"经常指向同一个目标。
 *
 * ## 三种形状各连的是什么（这里唯一需要判形状的地方）
 *
 *  - 树：端点是**节点 id**，与入参一致；
 *  - 流程：`FlowContent.edges` 的端点是**阶段 id**，而入参（界面上的下拉框、
 *    模型给的 op）是节点 id。所以这里把两个节点各自映到它所在的阶段再落库——
 *    映射不到的（节点不在流程里）与同一阶段内部的（后端契约明确说"暂不支持
 *    阶段内部步骤之间的边"）都当场拒掉，别让它到保存时才 422；
 *  - 鱼骨：顶层结构里根本没有 `edges`，加不了。说清楚比静默丢弃好。
 */
export function addEdge(
  content: DiagramContent,
  source: string,
  target: string,
  label?: string | null,
  kind?: EdgeKind | null,
): { content: DiagramContent; edge: MindEdge } {
  if (source === target) throw new Error('起点与终点不能是同一个节点');
  if (isFishbone(content)) {
    throw new Error('鱼骨图暂不支持附加关系：它的一级结构是类别，跨骨的关系还没有语义');
  }

  let from = source;
  let to = target;
  if (isFlow(content)) {
    const sourceStage = stageOfNode(content, source);
    const targetStage = stageOfNode(content, target);
    if (!sourceStage || !targetStage) throw new Error('端点节点不存在');
    if (sourceStage === targetStage) {
      throw new Error('同一个阶段里的步骤之间暂不支持连关系，请连到别的阶段');
    }
    from = sourceStage;
    to = targetStage;
  } else {
    if (!findNode(content.root, source)) throw new Error('起点节点不存在');
    if (!findNode(content.root, target)) throw new Error('终点节点不存在');
  }

  const text = (label ?? '').trim();
  const normalized = text ? text.slice(0, LIMITS.edgeLabel) : null;
  const edges = edgesOf(content);
  if (edges.some(edge => edge.source === from && edge.target === to
    && (edge.label ?? null) === normalized)) {
    throw new Error('这条关系已经存在');
  }

  const edge: MindEdge = {
    id: newEdgeId(),
    source: from,
    target: to,
    label: normalized,
    kind: kind ?? null,
  };
  return { content: { ...content, edges: [...edges, edge] }, edge };
}

/** 删一条边。只动 `edges`，树一个字节都不变。 */
export function removeEdge(content: DiagramContent, edgeId: string): DiagramContent {
  return { ...content, edges: edgesOf(content).filter(edge => edge.id !== edgeId) };
}

/**
 * AI 生成时标题的软上限（契约 `GENERATED_LABEL_MAX_CHARS`）。
 *
 * 与 `LIMITS.label`（200，契约硬上限、旧图读取口径）是两件事：生成侧要求 16 字，
 * 但旧图里那句 20–30 字的长标题必须照常读、照常存。前端用它只做**提示**（"这个
 * 标题有点长，画布上会折行"），不做校验——把 16 写进校验会让用户改不动旧节点。
 */
export const GENERATED_LABEL_HINT = 16;

/** 复制一棵树：编辑操作都在副本上做，撤销栈里的快照才不会被后续修改带跑。 */
export function cloneTree<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

/** 生成新节点 id。加前缀是为了和模型返回的 id 区分开，避免撞车。 */
export function newId(): string {
  const random =
    typeof crypto !== 'undefined' && 'randomUUID' in crypto
      ? crypto.randomUUID().replace(/-/g, '').slice(0, 8)
      : Math.random().toString(16).slice(2, 10);
  return `n_${random}`;
}

export function findNode(root: MindNode, id: string): MindNode | null {
  if (root.id === id) return root;
  for (const child of root.children) {
    const hit = findNode(child, id);
    if (hit) return hit;
  }
  return null;
}

/** 父节点；根节点返回 null。 */
export function findParent(root: MindNode, id: string): MindNode | null {
  for (const child of root.children) {
    if (child.id === id) return root;
    const hit = findParent(child, id);
    if (hit) return hit;
  }
  return null;
}

export function countNodes(root: MindNode): number {
  return 1 + root.children.reduce((sum, child) => sum + countNodes(child), 0);
}

export function depthOf(root: MindNode): number {
  if (root.children.length === 0) return 1;
  return 1 + Math.max(...root.children.map(depthOf));
}

function replaceChildren(
  node: MindNode,
  parentId: string,
  mapper: (children: MindNode[]) => MindNode[],
): MindNode {
  if (node.id === parentId) return { ...node, children: mapper(node.children) };
  return {
    ...node,
    children: node.children.map(child => replaceChildren(child, parentId, mapper)),
  };
}

/** 改节点文字。返回新的根，原树不动。 */
export function renameNode(root: MindNode, id: string, label: string): MindNode {
  if (root.id === id) return { ...root, label };
  return {
    ...root,
    children: root.children.map(child => renameNode(child, id, label)),
  };
}

/**
 * 改节点正文（契约 v2 的 `note`）。返回新的根，原树不动。
 *
 * 纯空白等价于"没有正文"：后端把纯空白视为 `None`，这里先归一，
 * 免得界面上出现一个 `note: '   '`、保存后又被服务端悄悄改成 null。
 */
export function setNodeNote(root: MindNode, id: string, note: string | null): MindNode {
  if (root.id === id) {
    const trimmed = note?.trim();
    return { ...root, note: trimmed ? trimmed : null };
  }
  return {
    ...root,
    children: root.children.map(child => setNodeNote(child, id, note)),
  };
}

/**
 * 改节点的视觉元素（`color` / `icon`）。返回新的根，原树不动。
 *
 * 与 `setNodeNote` 分开而不是合成一个"改任意字段"的通用函数：通用版本会诱使
 * 调用方把 `label`、`note`、`source_refs` 也顺手塞进来，而那三条各自有更严格的
 * 前置条件（非空、2000 字、引用必须落在 source_usage 里）。窄函数让"哪条路径
 * 能改什么"在签名上就看得出来。
 *
 * `null` 是**显式清除**（与 `undefined` 不同）：界面上选「默认」就是清掉颜色。
 */
export function setNodeVisual(
  root: MindNode,
  id: string,
  color: NodeColor | null,
  icon: string | null,
): MindNode {
  if (root.id === id) {
    const trimmed = (icon ?? '').trim();
    return { ...root, color, icon: trimmed ? trimmed : null };
  }
  return {
    ...root,
    children: root.children.map(child => setNodeVisual(child, id, color, icon)),
  };
}

/** 加子节点。返回新树与新节点，方便界面立刻选中它并进入编辑。 */
export function addChild(
  root: MindNode,
  parentId: string,
  label = '新节点',
): { root: MindNode; node: MindNode } {
  if (!findNode(root, parentId)) throw new Error('父节点不存在');

  const node: MindNode = { id: newId(), label, children: [] };
  return {
    root: replaceChildren(root, parentId, children => [...children, node]),
    node,
  };
}

/**
 * 加兄弟：插在当前节点后面，位置符合直觉（紧接着自己）。
 * 根节点没有兄弟，退化成加子节点。
 */
export function addSibling(
  root: MindNode,
  id: string,
  label = '新节点',
): { root: MindNode; node: MindNode } {
  const parent = findParent(root, id);
  if (!parent) return addChild(root, root.id, label);

  const node: MindNode = { id: newId(), label, children: [] };
  return {
    root: replaceChildren(root, parent.id, children => {
      const at = children.findIndex(child => child.id === id);
      return [...children.slice(0, at + 1), node, ...children.slice(at + 1)];
    }),
    node,
  };
}

/** 删除节点及其子树。根节点不能删（删了导图就没了）。 */
export function removeNode(root: MindNode, id: string): MindNode {
  if (root.id === id) throw new Error('根节点不能删除');
  return {
    ...root,
    children: root.children
      .filter(child => child.id !== id)
      .map(child => removeNode(child, id)),
  };
}

/**
 * 节点及其全部子孙的 id。
 *
 * 「删节点要连带删边」（`store.deleteNode`、`ops.applyOps` 的 `remove`）都要这份
 * 集合。写成两处的话，漏掉子孙那一处的后果是可复现的：删掉一棵子树之后，子节点
 * 身上的边仍然留在 `content.edges` 里，**下一次保存 422**，而报错指向某条边，
 * 与用户刚才做的事隔了很远。
 */
export function subtreeIds(node: MindNode): Set<string> {
  const ids = new Set<string>();
  const stack = [node];
  while (stack.length > 0) {
    const current = stack.pop()!;
    ids.add(current.id);
    stack.push(...current.children);
  }
  return ids;
}

/** 字数按码点算，和 Pydantic 的 len() 以及画布上的截断保持一致。 */
export function charLength(text: string): number {
  return Array.from(text).length;
}

function shortLabel(label: string): string {
  const chars = Array.from(label.trim());
  return chars.length > 12 ? `${chars.slice(0, 12).join('')}…` : chars.join('');
}

/**
 * 提交前校验整份内容，返回第一条错误文案；没问题返回 null。
 * 文案尽量与服务端一致，避免同一条规则出现两种说法。
 *
 * **三种形状共用这一份**，判据逐条对齐后端：`_scan_tree` 的 `seen` 集合在多棵并列
 * 子树之间**共用**（节点 id 必须全局唯一、节点总数按各容器合计算），而层号按每棵
 * 子树**各自**从 1 数；边的端点命名空间随形状走——树是节点 id，流程是**阶段 id**，
 * 鱼骨没有边。分开写三份的话，某一天"树拦了、鱼骨没拦"的分叉只会表现为一次 422。
 */
export function validateDraft(content: DiagramContent): string | null {
  const title = content.title.trim();
  if (!title) return '标题不能为空';
  if (charLength(title) > LIMITS.title) return `标题最多 ${LIMITS.title} 个字`;

  if (isFishbone(content)) {
    const categories = new Set<string>();
    for (const bone of content.bones) {
      if (!bone.category.trim()) return '骨的类别不能为空';
      if (categories.has(bone.category)) return `骨的类别不能重复（${bone.category}）`;
      categories.add(bone.category);
    }
  }
  if (isFlow(content)) {
    if (content.stages.length === 0) return '流程图至少要有一个阶段';
    const stageIds = new Set<string>();
    for (const stage of content.stages) {
      if (!stage.id) return '阶段 ID 不能为空';
      if (stageIds.has(stage.id)) return `阶段 ID 必须唯一（重复的是 ${stage.id}）`;
      stageIds.add(stage.id);
    }
  }

  const seen = new Set<string>();
  for (const root of rootsOf(content)) {
    const stack: Array<{ node: MindNode; depth: number }> = [{ node: root, depth: 1 }];

    while (stack.length > 0) {
      const { node, depth } = stack.pop()!;

      if (seen.has(node.id)) return '节点 ID 必须唯一';
      seen.add(node.id);
      if (seen.size > LIMITS.nodes) return `导图最多 ${LIMITS.nodes} 个节点`;
      if (depth > LIMITS.depth) return `导图最多 ${LIMITS.depth} 层`;

      const label = node.label.trim();
      if (!label) return '节点标题不能为空';
      if (charLength(label) > LIMITS.label) {
        return `节点「${shortLabel(label)}」超过 ${LIMITS.label} 个字`;
      }
      if (node.note && charLength(node.note.trim()) > LIMITS.note) {
        return `节点「${shortLabel(label)}」的正文超过 ${LIMITS.note} 个字`;
      }
      if (node.detail && charLength(node.detail.trim()) > LIMITS.detail) {
        return `节点「${shortLabel(label)}」的详情超过 ${LIMITS.detail} 个字`;
      }
      // 视觉元素：与后端 `Node.color` 的 Literal / `Node.icon` 的 max_length 对齐。
      // 不在这里拦的话，一个非法颜色要到保存时才 422，而那时用户已经在图上改了
      // 半天——他看到的是一句"内容不合法"，指不到是哪个节点。
      if (node.color != null && !isNodeColor(node.color)) {
        return `节点「${shortLabel(label)}」的颜色不在调色板里（${NODE_COLORS.join('/')}）`;
      }
      if (node.icon != null && (!node.icon.trim() || charLength(node.icon) > LIMITS.icon)) {
        return `节点「${shortLabel(label)}」的图标应为 1–${LIMITS.icon} 个字符`;
      }
      if (node.children.length > LIMITS.children) {
        return `节点「${shortLabel(label)}」最多 ${LIMITS.children} 个子节点`;
      }

      for (const child of node.children) stack.push({ node: child, depth: depth + 1 });
    }
  }

  // 附加边：只拦**后端会硬拒**的那两种（dangling 与自环），与契约
  // `MindMapContent.validate_tree` / `FlowContent.validate_flow` 逐条对齐。
  //
  // 条数（>200）与环长（>12 节点）**不在这里拦**：后端把它们报成 warn 而不是
  // 拒绝，前端拦下来的话，一张从库里读出来的、有 250 条边的图就再也存不回去了
  // ——用户什么都没改，图却存不上。校验只该拦"服务端一定会拒"的东西。
  //
  // 端点按**形状自己的命名空间**查：树的边连节点，流程的边连阶段。用节点 id 去查
  // 一张流程图的边，每一条都会变成"指向不存在的节点"，而那份图其实好得很。
  const edgeSpace = isFlow(content)
    ? new Set(containersOf(content).map(container => container.id))
    : seen;
  const edgeNoun = isFlow(content) ? '阶段' : '节点';
  const edgeIds = new Set<string>();
  for (const edge of edgesOf(content)) {
    if (edgeIds.has(edge.id)) return `边 ID 必须唯一（重复的是 ${edge.id}）`;
    edgeIds.add(edge.id);
    if (!edgeSpace.has(edge.source) || !edgeSpace.has(edge.target)) {
      const missing = !edgeSpace.has(edge.source) ? edge.source : edge.target;
      return `附加关系 ${edge.id} 指向不存在的${edgeNoun} ${missing}`;
    }
    if (edge.source === edge.target) return `附加关系 ${edge.id} 的起点与终点不能是同一个${edgeNoun}`;
    if (charLength(edge.label ?? '') > LIMITS.edgeLabel) {
      return `附加关系 ${edge.id} 的文字超过 ${LIMITS.edgeLabel} 个字`;
    }
  }

  return null;
}
