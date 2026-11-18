import dagre from '@dagrejs/dagre';

import type { MindEdge, MindNode } from '../../api/types';
import { NODE_SIZE, type LayoutResult, type Point } from './tidy';

/**
 * 流程图布局：用 dagre 做**分层有向图**排布。
 *
 * 与 `tidy.ts` 的根本差别在"边是什么"：
 *
 *  - 树布局里，边 = 父链，位置由"父居中于子"决定，与边无关；
 *  - 流程图里，边 = **依赖**。一条 `形式审查 → 付款` 的附加边也参与分层，因为
 *    "付款"必须排在"形式审查"之后。只用树布局时，这类跨支关系在画面上没有任何
 *    位置体现——线是画了，但它从一行漂到另一行，读者读不出先后。
 *
 * ## 为什么只取 dagre 的节点坐标，不用它的边路由
 *
 * dagre 会给出每条边的折线（`edge.points`），但本项目的连线由 Vue Flow 按句柄画，
 * 附加边还要按 `kind` 换色/换线型/挂标签（`canvas/MindEdge.vue`）。把 dagre 的
 * 折线也搬过来的话，同一张图上会出现两套连线：父子线（Vue Flow）与附加边
 * （dagre）的拐角风格、端点位置都不一样，看上去像两个人画的。所以这里**只回填
 * x/y**，边怎么走仍然归渲染层。
 */

/** 同层节点之间的水平间隙。比树布局的兄弟间隙大一点：流程图里同层节点是并列的步骤。 */
const NODESEP = 32;

/** 层与层之间的竖直间隙。48 高的节点框之间留 72，垂直连线才有地方摆分支标签。 */
const RANKSEP = 72;

/** 父子边的权重：只影响同层排序，**不承担**"父在上"的保证（理由见下）。 */
const TREE_EDGE_WEIGHT = 4;
/** 附加边的权重：低一档，让树在排序里更有话语权。 */
const ATTACH_EDGE_WEIGHT = 1;

/** 一个节点的外接框（宽高，px）。中心点由布局给，外框由这里给。 */
export interface NodeSize {
  readonly width: number;
  readonly height: number;
}

/**
 * 判定节点的外接框。**只有流程图用，`NODE_SIZE` 一个字都不动。**
 *
 * 为什么不复用 `NODE_SIZE`（180×48）：菱形是外接框的**内接**形状，可用的文字区
 * 只剩中间一段。180×48 的菱形在两行文字（`leading-tight` ≈ 35px）处的宽度是
 * `180 × (1 − 35/48) ≈ 49px`——一行放不下四个汉字，`line-clamp-2` 会当场裁字。
 * 而 `NODE_SIZE` 是布局与渲染共读的一份尺寸（`tidy.ts` 导出，树与鱼骨都在读），
 * 改它等于让几十条钉着逐像素坐标的用例一起变，所以这里给判定节点单独一份。
 *
 * 220×110 = 2:1，与教科书里的判定菱形同一个比例：中带的可用宽度是
 * `220 × (1 − 35/110) ≈ 150px`（约 10 个汉字/行），两行放得下 16 字的上限标题。
 *
 * 尺寸只进 `layoutFlow`，树与鱼骨的入参一个字节都没变。
 */
export const DECISION_SIZE = { width: 220, height: 110 } as const;

/** role → 外接框。除判定节点之外一律是 `NODE_SIZE`（= 与加菱形之前逐像素相同）。 */
export function flowNodeSize(role: MindNode['role']): NodeSize {
  return role === 'decision' ? DECISION_SIZE : NODE_SIZE;
}

export interface FlowLayout extends LayoutResult {
  /**
   * 节点 id → 外接框（**每个**节点都有，不只是判定节点）。
   *
   * 为什么由布局报出来，而不是渲染层自己按 role 再查一次：坐标是节点**中心**，
   * 画到画布上要换成左上角，那一步必须减去半个外接框。两边各算一次尺寸，只要
   * 有一处漏了角色，节点就会被画偏半个 `DECISION_SIZE - NODE_SIZE`，而连线仍然
   * 按布局坐标画——表现是"线从节点旁边擦过去"。几何只有一份真相，在这里。
   */
  sizes: Map<string, NodeSize>;
  /** 节点 id → 层号（0 起、连续）。附加边的回退判定与调试都读它。 */
  layers: Map<string, number>;
  /**
   * **回退边**：目标层号 ≤ 源层号的附加边 id。
   *
   * 渲染层据此把它绕到图的左侧、画成虚线（不然它是一条从下往上穿过整张图的实线，
   * 正好压在主干的连线上）。
   */
  backEdges: Set<string>;
}

/** 树 → 前序序列（父在子前），同时带出父 id。 */
function flatten(root: MindNode): Array<{ node: MindNode; parentId: string | null }> {
  const out: Array<{ node: MindNode; parentId: string | null }> = [];
  const walk = (node: MindNode, parentId: string | null) => {
    out.push({ node, parentId });
    for (const child of node.children) walk(child, node.id);
  };
  walk(root, null);
  return out;
}

/**
 * 计算流程图。
 *
 * ## 阶段顺序从哪来
 *
 * 生成侧（`backend/app/agent/tools.py::FLOW_PLAN_PROMPT`）把流程图的形状定死了：
 * 一级分支是**阶段**、按执行先后排列，`edges` **只连 root 与一级分支**。也就是说
 * 真实数据是这样的：
 *
 * ```
 * root ─┬─ 0 提交申请      edges: root→0（flow）、1→2（是）、1→0（驳回）
 *       ├─ 1 形式审查
 *       └─ 2 付款
 * ```
 *
 * 于是"退回上一步"在数据里是**兄弟之间**的边（`1 → 0`），不是"指回祖先"。
 * 这决定了回退的判据不能只用祖先关系，而要用**前序位次**：树是从左到右读的，
 * 前序在前的节点就是更早的阶段。
 *
 * ## 回退边的处理
 *
 * 1. **进图层之前先挑出来**：目标前序位次 ≤ 源的附加边是回退，**不参与分层**。
 *    不挑出来的后果是实测过的：`1 → 0` 会让 dagre 把"提交申请"推到"形式审查"
 *    下面一层（`minlen: 1` 要求目标至少低一层），整条阶段顺序被打乱——而
 *    语义上它只是"退回去"，不是"排在后面"。
 * 2. **剩下的边按前序单向**：所有进入图层的附加边都满足"前序小的 → 前序大的"，
 *    这是一个严格全序，**图必然无环**。所以父子边永远不会被反向：`dagre` 的
 *    `acyclicer: 'greedy'` 在这里是空转，保留它只为兜住"将来有别的调用方绕过
 *    前序过滤直接塞边"这一种情况。
 * 3. **排完再按层号复核**：`backEdges` 取"前序判定的回退"与"目标层号 ≤ 源层号"
 *    的并集。两者在正常数据上重合；后者能额外收进"落在同一层"的边（例如两条
 *    互不相干的分支之间横着连一条）。
 *
 * 悬空边（端点不在树里）与自环直接跳过：前者会让 dagre 凭空建一个只有坐标没有
 * 内容的节点，后者在 dagre 里要被挪成一条七点折线，对只取节点坐标的我们毫无意义。
 */
export function layoutFlow(
  root: MindNode,
  edges: readonly MindEdge[] = [],
): FlowLayout {
  const entries = flatten(root);
  /** 前序位次：它就是"阶段先后"的代理，见函数头"阶段顺序从哪来"。 */
  const order = new Map(entries.map((entry, index) => [entry.node.id, index]));

  const usable = edges.filter(
    edge => edge.source !== edge.target && order.has(edge.source) && order.has(edge.target),
  );
  const backwards = new Set(
    usable
      .filter(edge => (order.get(edge.target) ?? 0) <= (order.get(edge.source) ?? 0))
      .map(edge => edge.id),
  );

  // multigraph：同一对节点之间可能既有父子线又有一条附加关系（"通过"）。
  // 不用 multigraph 的话第二条 `setEdge` 会覆盖第一条，权重与 minlen 一起被吃掉。
  const graph = new dagre.graphlib.Graph({ multigraph: true });
  graph.setGraph({
    rankdir: 'TB',
    nodesep: NODESEP,
    ranksep: RANKSEP,
    // 前序过滤之后图已无环，这一项空转；留着是为了将来多一条边来源时的兜底。
    acyclicer: 'greedy',
    ranker: 'network-simplex',
    marginx: 0,
    marginy: 0,
  });
  graph.setDefaultEdgeLabel(() => ({}));

  // 外接框按角色分（判定节点更大，见 `DECISION_SIZE`）。dagre 用这个尺寸算间距，
  // 所以它必须是**渲染出来的那个框**——用小尺寸排、用大尺寸画，节点会互相压住。
  const sizes = new Map<string, NodeSize>();
  for (const { node } of entries) {
    const size = flowNodeSize(node.role);
    sizes.set(node.id, size);
    graph.setNode(node.id, { width: size.width, height: size.height });
  }
  for (const { node, parentId } of entries) {
    if (!parentId) continue;
    graph.setEdge(
      parentId, node.id,
      { minlen: 1, weight: TREE_EDGE_WEIGHT },
      `tree:${parentId}->${node.id}`,
    );
  }
  for (const edge of usable) {
    if (backwards.has(edge.id)) continue;
    graph.setEdge(
      edge.source, edge.target,
      { minlen: 1, weight: ATTACH_EDGE_WEIGHT },
      `edge:${edge.id}`,
    );
  }

  dagre.layout(graph);

  // ---- 层号归一：dagre 的 rank 有空洞（每条边按 2 计），换成 0 起的连续层号 ----
  const rawRank = new Map<string, number>();
  for (const { node } of entries) {
    const label = graph.node(node.id) as { rank?: number } | undefined;
    rawRank.set(node.id, typeof label?.rank === 'number' ? label.rank : 0);
  }
  const distinct = [...new Set(rawRank.values())].sort((a, b) => a - b);
  const denseOf = new Map(distinct.map((rank, index) => [rank, index]));

  const layers = new Map<string, number>();
  const backEdges = new Set<string>();
  for (const { node } of entries) layers.set(node.id, denseOf.get(rawRank.get(node.id) ?? 0) ?? 0);
  for (const edge of usable) {
    if (backwards.has(edge.id)) {
      backEdges.add(edge.id);
      continue;
    }
    const from = layers.get(edge.source);
    const to = layers.get(edge.target);
    if (from === undefined || to === undefined) continue;
    if (to <= from) backEdges.add(edge.id);
  }

  // ---- 回填坐标：把根平移到原点，与树布局同一个约定（坐标是节点中心） ----
  const origin = graph.node(root.id) as { x?: number; y?: number } | undefined;
  const offsetX = origin?.x ?? 0;
  const offsetY = origin?.y ?? 0;

  const positions = new Map<string, Point>();
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const { node } of entries) {
    const label = graph.node(node.id) as { x?: number; y?: number } | undefined;
    const point: Point = {
      x: (label?.x ?? offsetX) - offsetX,
      y: (label?.y ?? offsetY) - offsetY,
    };
    positions.set(node.id, point);
    // 包围盒按**每个节点自己的**外接框算：判定节点比 `NODE_SIZE` 大，用统一尺寸算
    // 会让它的一半露在包围盒外，`fitView` 就把菱形的尖角切掉了。
    const size = sizes.get(node.id) ?? NODE_SIZE;
    minX = Math.min(minX, point.x - size.width / 2);
    maxX = Math.max(maxX, point.x + size.width / 2);
    minY = Math.min(minY, point.y - size.height / 2);
    maxY = Math.max(maxY, point.y + size.height / 2);
  }

  if (!Number.isFinite(minX)) {
    minX = 0; maxX = 0; minY = 0; maxY = 0;
  }

  return {
    positions,
    sizes,
    layers,
    backEdges,
    bounds: { minX, minY, maxX, maxY, width: maxX - minX, height: maxY - minY },
    /**
     * 报 `down`：层沿 y 增长，于是 `childSide()` 会给父子线选上下句柄
     * （out-bottom → in-top），与 dagre 的 TB 分层一致。换朝向控件在这里不生效
     * ——一张从左往右读的流程图仍然是流程图，但一套"上/下"句柄配一套"左/右"
     * 分层会画出 90° 折的连线，那比不支持朝向更糟。
     */
    direction: 'down',
  };
}

/**
 * 判定节点的形状：**已经画成菱形了**（`clip-path` + 分层描边）。
 *
 * 这份注释原来是"结论：保持矩形"，逐条写着换形状的代价。那些代价里有三条真的成立，
 * 一条不成立，落地时按下面的取舍处理：
 *
 *  1. **`clip-path` 会把 `border` 一起裁掉** —— 成立。解法不是 SVG 描边，而是
 *     「同形三层 `clip-path`」：外层背景当描边色、内层 `inset` 缩进当填充色，中间
 *     那圈就是边框。选中 / 悬停 / `changed` 三态因此不需要各写一套描边，只改三个
 *     CSS 变量（`styles.css` 的 `.mind-diamond`）。
 *  2. **旋转 45° 要反向旋转全部内容** —— 成立，所以没走那条路。文字、图标、引用
 *     角标、悬停卡片、内联输入框全部保持水平：菱形只画在**背景层**，内容层独立
 *     且不裁切（内容层宽度收到 68%，那是菱形中带的安全宽度，见 `MindNode.vue`）。
 *  3. **句柄在外接框各边中点，菱形顶点在外接框四角** —— **不成立**，这条是原记录
 *     里唯一的误判。`clip-path: polygon(50% 0, 100% 50%, 50% 100%, 0 50%)` 的四个
 *     顶点**正好**落在外接框四条边的中点上，而 `Position.Top/Right/Bottom/Left`
 *     就是四条边的中点。所以连线天然落在菱形的顶点上，不需要挪句柄。
 *     （会落到"看不见的棱上"的是**旋转 45° 的正方形**，那时顶点才在外接框对角线上。）
 *  4. **`NODE_SIZE` 是布局与渲染共读的一份尺寸** —— 成立，所以判定节点没有改
 *     `NODE_SIZE`，而是由本文件给一份单独的 `DECISION_SIZE`，并通过 `sizes` 出参
 *     交给渲染层。树与鱼骨仍然只读 `NODE_SIZE`，逐像素不变。
 *
 * 判据是 `Node.role === 'decision'`，不看出边数：用户编辑到一半的节点出边数会闪变，
 * 拿它推断形状会让菱形闪一下又变回矩形（`Node.role` 的注释里写了同一条理由）。
 */
export const DECISION_SHAPE_NOTE = 'diamond-clip-path';
