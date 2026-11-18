import type {
  DiagramContent,
  MindEdge,
  MindMapContent,
  MindMapOp,
  MindNode,
  NodeColor,
  OpBatch,
  SourceRef,
} from '../api/types';
import {
  charLength,
  cloneTree,
  EDGE_KINDS,
  findNode,
  findParent,
  isNodeColor,
  LIMITS,
  NODE_COLORS,
  removeNode,
  subtreeIds,
} from './edits';
import { containersOf, isFlow, isTree, replaceContainer, rootsOf, walkNodes } from './shape';

/**
 * 导图操作执行器（前端侧）。
 *
 * 契约（AddOp / UpdateOp / RemoveOp / MoveOp / OpBatch）在 `api/types.ts`，
 * 那里是后端契约的镜像。本文件只放执行实现，避免契约与实现互相依赖。
 *
 * 需要契约类型的地方继续从本文件 import 也可以——下面做了 re-export，
 * 现有调用方的路径不用改。
 */
export type { AddEdgeOp, AddOp, MindMapOp, MoveOp, OpBatch, RemoveEdgeOp, RemoveOp, UpdateOp } from '../api/types';

/** 单个 op 失败。`path` 形如 `ops.2.id`，会作为工具结果回给模型。 */
export class OpError extends Error {
  constructor(
    readonly code: string,
    readonly path: string,
    message: string,
  ) {
    super(message);
    this.name = 'OpError';
  }
}

export interface ApplyResult<T extends DiagramContent = DiagramContent> {
  content: T;
  /** 被改动的节点 id，用于引用重校验与局部刷新。 */
  changed_ids: string[];
}

// ---------------------------------------------------------------------------
// 执行器
// ---------------------------------------------------------------------------

/**
 * 深查找直接复用 `edits.ts` 的实现：同一条 id 语义只该有一份代码，
 * 否则编辑器与 op 执行器迟早会在"根节点算不算命中"这种细节上分叉。
 */
export { findNode as findDeep, findParent as findParentDeep };

/**
 * `candidateId` 是否是 `ancestorId` 的子孙，**含自己**。
 *
 * 含自己不是省事：`move` 里"挂到自己"和"挂到自己的子孙"在语义上是同一个错误，
 * 拆成两条判断迟早漏掉一条。
 */
export function isDescendant(root: MindNode, ancestorId: string, candidateId: string): boolean {
  const ancestor = findNode(root, ancestorId);
  if (!ancestor) return false;
  return findNode(ancestor, candidateId) !== null;
}

/**
 * 完整的随机 id（UUID 的 32–36 位随机量）。
 *
 * 不复用 `edits.ts::newId()`：那个为了界面可读把 UUID 截成 8 位，
 * 界面上手点一次不会撞，但一批 op 里几十个新节点时碰撞概率不再可以忽略；
 * 一旦撞车整批会被 `duplicate_id` 打回，模型只会看到"随机失败"。
 */
function fullRandomId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  // 非安全上下文（http://）拿不到 randomUUID：退回同量级的随机十六进制。
  const bytes = new Uint8Array(16);
  if (typeof crypto !== 'undefined' && typeof crypto.getRandomValues === 'function') {
    crypto.getRandomValues(bytes);
  } else {
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(Math.random() * 256);
    }
  }
  return Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
}

/**
 * 在指定父节点的子列表里插入一个节点，返回新的根。
 *
 * `edits.ts` 的 `replaceChildren` 没有导出，而"按 index 插入"只有 op 执行器需要，
 * 所以在这里单独写一个；做法仍是结构共享，调用方拿到的旧树不受影响。
 */
function insertChildAt(root: MindNode, parentId: string, index: number, child: MindNode): MindNode {
  if (root.id === parentId) {
    const children = [...root.children];
    children.splice(index, 0, child);
    return { ...root, children };
  }
  return {
    ...root,
    children: root.children.map(item => insertChildAt(item, parentId, index, child)),
  };
}

/**
 * 整批 op 执行完后的**整份内容**校验。
 *
 * 为什么放在最后而不是每条 op 里：add/move 单条合法、合起来越界的组合很常见
 * （比如一条 batch 里补 30 个节点），逐条校验会在中途误报；但整批交付的内容
 * 一旦越界，后端 Pydantic 会 422，所以这里必须兜住。
 *
 * 失败一律抛 `tree_invalid` + path `ops`：没有任何一条 op 能单独定位到问题，
 * 硬塞一个下标只会误导模型去改一条本来正确的 op。
 *
 * ## 三种形状共用这一份
 *
 * 判据与后端三处 validator 逐条对齐：节点 id 在**所有容器之间**唯一、节点总数按
 * 合计算、层号按每棵子树各自从 1 数（`_scan_tree` 一条 `seen` 穿到底就是这么做的）；
 * 边的端点命名空间随形状走——树是节点 id，流程是**阶段 id**，鱼骨没有边。
 */
function assertContentValid(content: DiagramContent): void {
  const sourceUsage = content.source_usage ?? [];
  const invalid = (message: string): never => {
    throw new OpError('tree_invalid', 'ops', message);
  };

  // 后端 MindMapContent.validate_tree 还会校验"节点引用的来源必须在 source_usage 里"。
  // 前端不做这一步的话，脏引用会一路放到 POST 才 422，而那时模型已经拿着"成功"
  // 的反馈继续改了。
  const allowedSources = new Set(sourceUsage.map(item => item.source_id));
  const seen = new Set<string>();

  for (const root of rootsOf(content)) {
    const stack: Array<{ node: MindNode; depth: number }> = [{ node: root, depth: 1 }];

    while (stack.length > 0) {
      const { node, depth } = stack.pop()!;

      if (seen.has(node.id)) invalid(`节点 ID 必须唯一，重复的是 ${node.id}`);
      seen.add(node.id);
      if (seen.size > LIMITS.nodes) invalid(`导图最多 ${LIMITS.nodes} 个节点`);
      if (depth > LIMITS.depth) invalid(`导图最多 ${LIMITS.depth} 层`);

      // 非空按 trim 后的值判；长度按**原始** label 算——后端 `Field(max_length=200)`
      // 量的也是原始值，前端用 trim 后的长度会把「200 字 + 一个尾随空格」放过去，
      // 下一次保存就被 422 打回。
      const label = node.label.trim();
      if (!label) invalid(`节点 ${node.id} 的标题不能为空`);
      if (charLength(node.label) > LIMITS.label) {
        invalid(`节点 ${node.id} 的标题超过 ${LIMITS.label} 个字`);
      }
      if (node.children.length > LIMITS.children) {
        invalid(`节点 ${node.id} 最多 ${LIMITS.children} 个子节点`);
      }
      // 视觉元素：`color` 只收调色板键（后端是 `Literal`，非法值会 422）。
      // 与 `add` 那条入口的校验同一口径：走到整树兜底还留着非法值，说明有一条
      // 写入路径绕过了 op 层的检查——那时报错比把它画成一个灰点好。
      if (node.color != null && !isNodeColor(node.color)) {
        invalid(`节点 ${node.id} 的颜色不在调色板里（${NODE_COLORS.join('/')}）`);
      }
      if (node.icon != null && (!node.icon.trim() || charLength(node.icon) > LIMITS.icon)) {
        invalid(`节点 ${node.id} 的图标应为 1–${LIMITS.icon} 个字符`);
      }
      for (const ref of node.source_refs ?? []) {
        if (!allowedSources.has(ref.source_id)) {
          invalid(`节点 ${node.id} 引用的来源 ${ref.source_id} 不在 source_usage 中`);
        }
        // 与 SourceRef.validate_range 同口径：start / end 要么都给、要么都不给，
        // 且区间不能为空（end 必须严格大于 start）。
        const start = ref.start ?? null;
        const end = ref.end ?? null;
        if ((start === null) !== (end === null)) {
          invalid(`节点 ${node.id} 的引用 start 和 end 必须同时提供`);
        }
        if (start !== null && end !== null && end <= start) {
          invalid(`节点 ${node.id} 的引用 end 必须大于 start`);
        }
      }

      for (const child of node.children) stack.push({ node: child, depth: depth + 1 });
    }
  }

  // 附加边：只拦后端会硬拒的那两种（dangling / 自环），与契约
  // `MindMapContent.validate_tree` 同口径。条数与环长不拦——它们在后端是 warn，
  // 前端拦下来会让一张 250 条边的旧图再也存不回去。
  //
  // 端点按**形状自己的命名空间**查：流程的边连的是阶段（`s_…`），拿节点 id 去查
  // 会把每一条合法的阶段跳转判成 dangling。
  const edgeSpace = isFlow(content) ? new Set(containersOf(content).map(c => c.id)) : seen;
  const edgeName = isFlow(content) ? '阶段' : '节点';
  const edges = 'edges' in content ? content.edges ?? [] : [];
  const edgeIds = new Set<string>();
  for (const edge of edges) {
    if (edgeIds.has(edge.id)) invalid(`边 ID 必须唯一，重复的是 ${edge.id}`);
    edgeIds.add(edge.id);
    if (!edgeSpace.has(edge.source)) {
      invalid(`边 ${edge.id} 的起点${edgeName} ${edge.source} 不在导图中`);
    }
    if (!edgeSpace.has(edge.target)) {
      invalid(`边 ${edge.id} 的终点${edgeName} ${edge.target} 不在导图中`);
    }
    if (edge.source === edge.target) invalid(`边 ${edge.id} 的起点与终点不能是同一个节点`);
    if (charLength(edge.label ?? '') > LIMITS.edgeLabel) {
      invalid(`边 ${edge.id} 的文字超过 ${LIMITS.edgeLabel} 个字`);
    }
  }
}

/**
 * 按序执行一整批 op，返回新内容与被触及的 id。
 *
 * **树走原路**（`applyOpsOnTree`，逐字节等于阶段 2 之前的那份实现），鱼骨与流程
 * 借道同一条路：把命中的那棵子树当作一棵临时树的根跑一遍，再把结果写回原位
 * （镜像后端 `tools._apply_ops_any`）。原子性与 id 规范化因此仍然只有一份实现——
 * "失败一个 op 都不落"这件事不该被实现三遍。
 *
 * **一批 op 只允许落在一棵子树里**（一根骨 / 一个阶段）。跨子树的搬动在数据上
 * 没有"它该属于哪根骨"的确定答案：那是三套结构各自的语义，属于阶段 3。这里明确
 * 报 `cross_container` 而不是猜一个位置——猜错的后果是内容静默地换了骨，而那种
 * 错位事后没人看得出来。
 *
 * 一个已知的口径缺口（**后端已经补上，这里还没跟**）：非树路径只把 `root` 写回原位，
 * 临时树上的 `edges` 不落回内容。后端那边现在按层级判（`ops.resolve_flow_edge_placement`：
 * 两个阶段 id → `FlowContent.edges`，同阶段两个节点 → `FlowStage.edges`），所以
 * **`edit` 意图在流程图上能连出阶段内的边**。
 *
 * 这里不跟的理由是这条路当前不可达：`stores/mindmap.ts::runGeneration` 在非树内容上
 * 直接拒绝 `expand` / `edit`（后端 `AgentRunRequest.snapshot` 收的是三种形状，但
 * 前端那一层先拦了，给的是人话）。前端 `ops.ts` 只用于镜像"生成过程中收到的 op 批
 * 次"，而那条路上不会有流程图。要放开那条闸门时，这里必须一起改——否则模型发来的
 * `add_edge` 会在这层被静默丢掉，画布与后端候选图不一致。
 */
/** 树上的调用点（含既有用例）拿到的仍是 `MindMapContent`，`content.root` 照旧可用。 */
export function applyOps(content: MindMapContent, batch: OpBatch): ApplyResult<MindMapContent>;
export function applyOps(content: DiagramContent, batch: OpBatch): ApplyResult<DiagramContent>;
export function applyOps(content: DiagramContent, batch: OpBatch): ApplyResult {
  if (isTree(content)) return applyOpsOnTree(content, batch);

  const ids = opTargetIds(batch);
  const holders = holdersOf(content, ids);
  if (holders.length === 0) {
    throw new OpError(
      'node_not_found', 'ops',
      '这一批操作提到的节点都不在当前导图里（鱼骨图与流程图按骨/阶段分片，'
      + '改之前先确认节点 id 取自这份导图）',
    );
  }
  if (holders.length > 1) {
    const names = holders.slice(0, 5)
      .map(({ id }) => containersOf(content).find(item => item.id === id)?.label ?? id)
      .join('、');
    throw new OpError(
      'cross_container', 'ops',
      `这一批操作跨了 ${holders.length} 处（${names}）：鱼骨图的每一根骨、流程图的`
      + '每一个阶段是一棵独立的子树，一次只能改其中一处；请拆成几批',
    );
  }

  const rootId = holders[0].id;
  const root = findAcross(content, rootId);
  if (!root) throw new OpError('node_not_found', 'ops', `子树根 ${rootId} 不在这份导图里`);

  const temp: MindMapContent = {
    title: content.title,
    root,
    theme: content.theme,
    source_usage: content.source_usage ?? [],
  };
  const applied = applyOpsOnTree(temp, batch);
  const moved = replaceContainer(content, rootId, () => applied.content.root);
  // 写回之后再校验一次整份内容：临时树只能看到自己那一棵，跨容器的 id 唯一性与
  // 边/阶段的关系要在这里兜住（后端那一步是 `shapes.revalidate`）。
  assertContentValid(moved);
  return { content: moved, changed_ids: applied.changed_ids };
}

/** 一批 op 里提到的**所有**节点 id（含临时 id，交给调用方去筛）。 */
const OP_NODE_FIELDS = ['id', 'parent_id', 'new_parent_id', 'source', 'target'] as const;

function opTargetIds(batch: OpBatch): string[] {
  const seen: string[] = [];
  for (const op of batch.ops) {
    const record = op as unknown as Record<string, unknown>;
    for (const name of OP_NODE_FIELDS) {
      const value = record[name];
      if (typeof value === 'string' && value && !seen.includes(value)) seen.push(value);
    }
  }
  return seen;
}

/** 这些 id 分别落在哪棵并列子树里（去重保序）。 */
function holdersOf(content: DiagramContent, ids: readonly string[]): Array<{ index: number; id: string }> {
  const wanted = new Set(ids);
  const holders: Array<{ index: number; id: string }> = [];
  rootsOf(content).forEach((root, index) => {
    for (const node of walkNodes(root)) {
      if (wanted.has(node.id)) {
        holders.push({ index, id: root.id });
        return;
      }
    }
  });
  return holders;
}

function findAcross(content: DiagramContent, nodeId: string): MindNode | null {
  for (const root of rootsOf(content)) {
    for (const node of walkNodes(root)) {
      if (node.id === nodeId) return node;
    }
  }
  return null;
}

/**
 * 在**一棵树**上执行整批 op。树图型唯一走这条路的入口，也是鱼骨/流程借道的那份
 * 实现（入参是一棵临时 `MindMapContent`，出参是它的新根）。
 *
 * 整批原子生效：全部操作都在 `cloneTree` 出来的副本上做，任何一条抛 `OpError`
 * 就把副本整个丢掉。调用方不可能拿到"改了一半"的树 —— 半批生效会让树的 shape
 * 和模型以为的不一致，而模型下一轮会基于那个错误前提继续改，越修越远。
 *
 * 顺序敏感是刻意的：后面的 op 可以引用前面 `add` 出来的 id，这正是一批 op 的
 * 意义所在（先建父节点再往里挂子节点）。也正因为如此，中间没有"延迟解析"，
 * 每条 op 都拿当时的树做判断。
 */
export function applyOpsOnTree(
  content: MindMapContent, batch: OpBatch,
): ApplyResult<MindMapContent> {
  const next: MindMapContent = cloneTree(content);
  const changed = new Set<string>();

  batch.ops.forEach((op, index) => {
    /** 失败路径回给模型，形如 `ops.2.parent_id`；下标必须是本批里的真实位置。 */
    const path = (field: string) => `ops.${index}.${field}`;

    switch (op.op) {
      case 'add': {
        const parent = findNode(next.root, op.parent_id);
        if (!parent) {
          throw new OpError('parent_not_found', path('parent_id'), `父节点 ${op.parent_id} 不存在`);
        }
        if (findNode(next.root, op.id)) {
          throw new OpError('duplicate_id', path('id'), `节点 ID ${op.id} 已经存在`);
        }
        // 上界取 length：等于长度就是追加到末尾，与 splice 的语义一致。
        if (!Number.isInteger(op.index) || op.index < 0 || op.index > parent.children.length) {
          throw new OpError(
            'index_out_of_range',
            path('index'),
            `插入位置 ${op.index} 超出范围 0..${parent.children.length}`,
          );
        }

        const node: MindNode = {
          id: op.id,
          label: op.label,
          children: [],
          // 缺省给空数组而不是 undefined：模型漏写这一项时，节点不该凭空继承
          // 任何引用，也不该让下游 `node.source_refs.length` 抛错。
          source_refs: cloneTree(op.source_refs ?? []),
          // `detail` 是生成产出的说明，与用户备注 `note` 分开存；`add` 不带
          // note 字段，正是为了这两条写入路径永不交叉。
          detail: op.detail ?? null,
          // 视觉元素：空串归一成 null（`add` 没有"清空"语义，新节点本来就没有
          // 颜色）。值是调色板键——校验在 `assertTreeValid` 里统一做。
          color: op.color ? op.color : null,
          icon: op.icon ? op.icon : null,
        };
        next.root = insertChildAt(next.root, op.parent_id, op.index, node);
        changed.add(op.id);
        // 父节点的 children 变了，局部刷新要能重排它的分支；
        // 只报新节点 id 的话，新增节点的父分支不会被重算。
        changed.add(op.parent_id);
        break;
      }

      case 'update': {
        const node = findNode(next.root, op.id);
        if (!node) {
          throw new OpError('node_not_found', path('id'), `节点 ${op.id} 不存在`);
        }
        const changes: {
          label?: string;
          source_refs?: SourceRef[];
          detail?: string | null;
          color?: NodeColor | '';
          icon?: string | null;
        } = op.changes ?? {};
        // 后端 UpdateChanges.at_least_one 直接拒绝空 changes。前端不拦的话，
        // 同一条 op 在这里成功、在服务端 ValidationError，模型收到的反馈相反。
        if (
          changes.label === undefined &&
          changes.source_refs === undefined &&
          changes.detail === undefined &&
          changes.color === undefined &&
          changes.icon === undefined
        ) {
          throw new OpError('empty_changes', path('changes'), `节点 ${op.id} 的 update 至少要改一个字段`);
        }
        // `undefined` 是"这一项不动"，`[]` 是"显式清空"。这两句必须分开写，
        // 合并成 `node.source_refs = changes.source_refs ?? node.source_refs`
        // 会把模型的清空意图当成漏字段吞掉。
        if (changes.label !== undefined) node.label = changes.label;
        if (changes.source_refs !== undefined) {
          node.source_refs = cloneTree(changes.source_refs);
        }
        // `detail` 同一套约定：`undefined` 不动，空串归一成 `null`（清空）。
        // 这里**不碰 note**：那是用户的备注，op 通道无权重写它。
        if (changes.detail !== undefined) {
          const detail = (changes.detail ?? '').trim();
          node.detail = detail ? detail : null;
        }
        // 视觉元素同一条约定：`undefined` 不动，空串是**显式清除**。
        // 与后端 `_apply_update` 逐条对齐（`color: ''` → `null`），否则同一个
        // batch 会一边清掉颜色、一边在树里留一个查不到 CSS 变量的空串。
        if (changes.color !== undefined) {
          if (changes.color !== '' && !isNodeColor(changes.color)) {
            throw new OpError(
              'invalid_color',
              path('changes.color'),
              `颜色只能是 ${NODE_COLORS.join('/')} 之一，收到 ${String(changes.color)}`,
            );
          }
          node.color = changes.color === '' ? null : changes.color;
        }
        if (changes.icon !== undefined) {
          const icon = (changes.icon ?? '').trim();
          if (icon && charLength(icon) > LIMITS.icon) {
            throw new OpError(
              'invalid_icon',
              path('changes.icon'),
              `图标最多 ${LIMITS.icon} 个字符`,
            );
          }
          node.icon = icon ? icon : null;
        }
        changed.add(op.id);
        break;
      }

      case 'remove': {
        if (op.id === next.root.id) {
          throw new OpError('cannot_remove_root', path('id'), '根节点不能删除');
        }
        const doomed = findNode(next.root, op.id);
        if (!doomed) {
          throw new OpError('node_not_found', path('id'), `节点 ${op.id} 不存在`);
        }
        next.root = removeNode(next.root, op.id);
        // 连带删边（与后端 `_apply_remove` 逐条对齐）：源或目标命中**被删子树**
        // 的边全部消失。只清被点名那一个节点的话，子孙身上的边会悬空，下一次
        // 缆校验就报 dangling，而那条报错指的位置与用户做的事隔得很远。
        const removedIds = subtreeIds(doomed);
        next.edges = (next.edges ?? []).filter(
          edge => !removedIds.has(edge.source) && !removedIds.has(edge.target),
        );
        changed.add(op.id);
        break;
      }

      case 'add_edge': {
        const source = findNode(next.root, op.source);
        if (!source) {
          throw new OpError('node_not_found', path('source'), `起点节点 ${op.source} 不存在`);
        }
        if (!findNode(next.root, op.target)) {
          throw new OpError('node_not_found', path('target'), `终点节点 ${op.target} 不存在`);
        }
        if (op.source === op.target) {
          throw new OpError(
            'self_loop',
            path('target'),
            `${op.source} 不能连到自己：自环在画布上只会渲染成一个点`,
          );
        }
        const edges = next.edges ?? [];
        if (edges.some(edge => edge.id === op.id)) {
          throw new OpError('duplicate_edge', path('id'), `边 id ${op.id} 已经存在`);
        }
        const label = (op.label ?? '').trim() || null;
        if (edges.some(edge => edge.source === op.source && edge.target === op.target
          && (edge.label ?? null) === label)) {
          throw new OpError(
            'duplicate_edge',
            path('target'),
            `${op.source} → ${op.target}${label ? `（${label}）` : ''} 这条边已经存在`,
          );
        }
        if (op.kind != null && !(EDGE_KINDS as readonly string[]).includes(op.kind)) {
          throw new OpError(
            'invalid_kind',
            path('kind'),
            `边的类型只能是 ${EDGE_KINDS.join('/')} 之一，收到 ${String(op.kind)}`,
          );
        }
        if (edges.length >= LIMITS.edges) {
          throw new OpError(
            'too_many_edges',
            path('id'),
            `这张图已经有 ${edges.length} 条附加边，达到上限 ${LIMITS.edges} 条`,
          );
        }
        const edge: MindEdge = {
          id: op.id,
          source: op.source,
          target: op.target,
          label,
          kind: op.kind ?? null,
        };
        next.edges = [...edges, edge];
        changed.add(op.source);
        changed.add(op.target);
        break;
      }

      case 'remove_edge': {
        const edges = next.edges ?? [];
        const hit = edges.find(edge => edge.id === op.id);
        if (!hit) {
          throw new OpError('edge_not_found', path('id'), `边 ${op.id} 不存在`);
        }
        next.edges = edges.filter(edge => edge.id !== op.id);
        changed.add(hit.source);
        changed.add(hit.target);
        break;
      }

      case 'move': {
        const target = findNode(next.root, op.id);
        if (!target) {
          throw new OpError('node_not_found', path('id'), `节点 ${op.id} 不存在`);
        }
        const newParent = findNode(next.root, op.new_parent_id);
        if (!newParent) {
          throw new OpError(
            'parent_not_found',
            path('new_parent_id'),
            `新的父节点 ${op.new_parent_id} 不存在`,
          );
        }
        // isDescendant 含自己，所以"挂到自己"和"挂到自己的子孙"都由这一句拦住。
        // 顺带拦住根节点：根的任何一个候选父节点要么是它自己、要么是它的子孙。
        if (isDescendant(next.root, op.id, op.new_parent_id)) {
          throw new OpError(
            'cycle_detected',
            path('new_parent_id'),
            `节点 ${op.id} 不能挂到 ${op.new_parent_id} 下：会形成环`,
          );
        }

        // 同父内移动按"先摘除、再插入"执行，所以合法上界是摘除后的长度，
        // 也就是 length - 1；跨父移动不缩短新父节点的列表，上界仍是 length。
        // 这样 `index` 对调用方只有一种解释，不用记"同父时要减一"。
        const oldParent = findParent(next.root, op.id);
        const sameParent = oldParent !== null && oldParent.id === op.new_parent_id;
        const limit = sameParent ? newParent.children.length - 1 : newParent.children.length;
        if (!Number.isInteger(op.index) || op.index < 0 || op.index > limit) {
          throw new OpError(
            'index_out_of_range',
            path('index'),
            `插入位置 ${op.index} 超出范围 0..${limit}`,
          );
        }

        next.root = insertChildAt(removeNode(next.root, op.id), op.new_parent_id, op.index, target);
        changed.add(op.id);
        changed.add(op.new_parent_id);
        // 旧父节点的子列表也变了，局部刷新要能重排它的分支。
        if (oldParent) changed.add(oldParent.id);
        break;
      }

      default: {
        // 契约外的 op 类型（后端将来加新 op，或模型自己编了一个）：静默跳过等于
        // 假装成功，模型会拿着"已生效"的反馈继续往下改。必须明确报错。
        const unknown = op as { op: string };
        throw new OpError('unknown_op', path('op'), `未知的 op 类型 ${unknown.op}`);
      }
    }
  });

  assertContentValid(next);

  return { content: next, changed_ids: Array.from(changed) };
}

/**
 * 把模型草案里的临时 id（`$tmp1` 这类）换成真实 id，并改写所有引用。
 *
 * 两件必须做到的事：一是同一个临时 id 无论被引用多少次都映射到同一个真实 id
 * （否则 add 出来的父节点和后面的子节点指向两个不同节点，树会静默分叉）；
 * 二是 `parent_id` 可能出现在它的定义之前，所以映射要在第一次遇到时就确定，
 * 而不是先扫一遍 add 再改引用。
 *
 * 非 `$` 开头的 id 原样保留：那是模型从当前树里抄来的真实 id，不能动。
 */
export function normalizeOps(
  drafts: MindMapOp[],
  existing: Set<string>,
): { ops: MindMapOp[]; idMap: Record<string, string> } {
  const idMap = new Map<string, string>();
  const used = new Set(existing);

  const realIdOf = (id: string): string => {
    if (!id.startsWith('$')) return id;
    const cached = idMap.get(id);
    if (cached !== undefined) return cached;

    // 和现有节点撞车的可能性极低，但一旦撞上就是 `duplicate_id`，重抽一次最省事。
    let fresh = fullRandomId();
    while (used.has(fresh)) fresh = fullRandomId();
    used.add(fresh);
    idMap.set(id, fresh);
    return fresh;
  };

  /** 边 id 走 `e_` 前缀（与后端 `new_edge_id` 同口径），端点是节点 id 走上面那个。 */
  const realEdgeIdOf = (id: string): string => {
    if (!id.startsWith('$')) return id;
    const cached = idMap.get(id);
    if (cached !== undefined) return cached;
    let fresh = `e_${fullRandomId().replace(/-/g, '').slice(0, 32)}`;
    while (used.has(fresh)) fresh = `e_${fullRandomId().replace(/-/g, '').slice(0, 32)}`;
    used.add(fresh);
    idMap.set(id, fresh);
    return fresh;
  };

  const ops = drafts.map((draft): MindMapOp => {
    switch (draft.op) {
      case 'add':
        return { ...draft, id: realIdOf(draft.id), parent_id: realIdOf(draft.parent_id) };
      case 'update':
        // changes 也复制一份：规范化后的批次不该和草案共用同一个可变对象。
        return { ...draft, id: realIdOf(draft.id), changes: { ...draft.changes } };
      case 'remove':
        return { ...draft, id: realIdOf(draft.id) };
      case 'move':
        return { ...draft, id: realIdOf(draft.id), new_parent_id: realIdOf(draft.new_parent_id) };
      case 'add_edge':
        // 端点也是临时名：同一批里"先建两个节点、再用它们的 `$` 名连一条边"
        // 是合法且常用的一种形状（模型一条批次描述整段流程时就会这样写）。
        return {
          ...draft,
          id: realEdgeIdOf(draft.id),
          source: realIdOf(draft.source),
          target: realIdOf(draft.target),
        };
      case 'remove_edge':
        // 边 id 可能是刚在**同一条批次**里 add 出来的临时名。
        return { ...draft, id: realEdgeIdOf(draft.id) };
    }
  });

  return { ops, idMap: Object.fromEntries(idMap) };
}
