import type { MindNode } from '../../api/types';

/**
 * 树布局方向。
 *
 * 语义在屏幕轴上：
 * - `right` 根在左、层级向右（经典思维导图）
 * - `left`  根在右、层级向左（right 的镜像）
 * - `down`  根在上、层级向下（组织架构/逻辑结构式）
 * - `both`  双向：根居中，子节点对半分列两侧
 *
 * 注意：契约 v2 的 `Theme.direction` 只有 right/left/both，垂直向下在 v2 里
 * 对应 `layout: "logic"`；映射关系见 docs/CONTRACT_V2.md。
 */
export type Direction = 'right' | 'left' | 'down' | 'both';

export interface LayoutOptions {
  direction?: Direction;
  /** 节点宽（px），垂直布局时决定兄弟间距的下限 */
  nodeWidth?: number;
  /** 节点高（px），水平布局时决定兄弟间距的下限 */
  nodeHeight?: number;
  /** 层级之间的间隙 */
  depthGap?: number;
  /** 兄弟节点之间的间隙 */
  siblingGap?: number;
}

export interface Point {
  x: number;
  y: number;
}

export interface LayoutBounds {
  minX: number;
  minY: number;
  maxX: number;
  maxY: number;
  width: number;
  height: number;
}

export interface LayoutResult {
  /** 节点 id → 画布坐标（节点左上角为锚点时请自行减去半宽半高，这里给的是中心点） */
  positions: Map<string, Point>;
  bounds: LayoutBounds;
  direction: Direction;
}

/** 节点框尺寸：布局与节点组件共用，改这里两边一起变。 */
export const NODE_SIZE = { width: 180, height: 48 } as const;

const DEFAULTS = {
  nodeWidth: NODE_SIZE.width,
  nodeHeight: NODE_SIZE.height,
  depthGap: 56,
  siblingGap: 20,
} as const;

/** 抽象坐标：b = 兄弟展开方向（breadth），d = 层级方向（depth）。负数 d 表示双向布局里的左侧。 */
interface AbstractPoint {
  b: number;
  d: number;
}

function isVertical(direction: Direction): boolean {
  return direction === 'down';
}

/** 同一棵子树在兄弟方向上占用的宽度（固定节点尺寸下的紧凑树算法）。 */
function subtreeExtent(
  node: MindNode,
  breadthSize: number,
  siblingGap: number,
  memo: Map<string, number>,
): number {
  const cached = memo.get(node.id);
  if (cached !== undefined) return cached;

  let extent = breadthSize;
  if (node.children.length > 0) {
    const childrenExtent = node.children.reduce(
      (sum, child) => sum + subtreeExtent(child, breadthSize, siblingGap, memo),
      0,
    );
    const gaps = siblingGap * (node.children.length - 1);
    // 子树比节点本身还宽时，按子树宽度算；否则以节点宽度为准，保证父节点不挤进子节点区域。
    extent = Math.max(breadthSize, childrenExtent + gaps);
  }

  memo.set(node.id, extent);
  return extent;
}

/** 把子树放进 [start, start + extent] 这段兄弟方向空间：父节点居中，子节点依次排布。 */
function place(
  node: MindNode,
  start: number,
  depth: number,
  breadthSize: number,
  siblingGap: number,
  memo: Map<string, number>,
  out: Map<string, AbstractPoint>,
): void {
  const extent = subtreeExtent(node, breadthSize, siblingGap, memo);
  out.set(node.id, { b: start + extent / 2, d: depth });

  if (node.children.length === 0) return;

  const childrenExtent = node.children.reduce(
    (sum, child) => sum + subtreeExtent(child, breadthSize, siblingGap, memo),
    0,
  );
  const gaps = siblingGap * (node.children.length - 1);
  let cursor = start + (extent - (childrenExtent + gaps)) / 2;

  for (const child of node.children) {
    place(child, cursor, depth + Math.sign(depth) || 1, breadthSize, siblingGap, memo, out);
    cursor += subtreeExtent(child, breadthSize, siblingGap, memo) + siblingGap;
  }
}

function emptyBounds(): LayoutBounds {
  return { minX: 0, minY: 0, maxX: 0, maxY: 0, width: 0, height: 0 };
}

/**
 * 计算整棵树的位置。
 *
 * 算法是 Reingold–Tilford 的固定尺寸变体：先后序算出每棵子树的兄弟方向占位，
 * 再前序把子节点居中铺开、父节点落在子节点区间的中点。它算的是抽象坐标
 * （breadth / depth），与屏幕方向无关，最后一步才按 `direction` 映射到 x/y —
 * 所以水平延伸、垂直延伸、镜像、双向共用同一套逻辑。
 */
export function layoutTidy(root: MindNode, options: LayoutOptions = {}): LayoutResult {
  const direction = options.direction ?? 'right';
  const nodeWidth = options.nodeWidth ?? DEFAULTS.nodeWidth;
  const nodeHeight = options.nodeHeight ?? DEFAULTS.nodeHeight;

  const vertical = isVertical(direction);
  // 水平布局时兄弟上下排，间距受节点高度约束；垂直布局时兄弟左右排，受节点宽度约束。
  const breadthSize = vertical ? nodeWidth : nodeHeight;
  const depthSize = vertical ? nodeHeight : nodeWidth;
  const siblingGap = options.siblingGap ?? (vertical ? 24 : DEFAULTS.siblingGap);
  const depthGap = options.depthGap ?? (vertical ? 64 : DEFAULTS.depthGap);

  const memo = new Map<string, number>();
  const abstract = new Map<string, AbstractPoint>();

  if (direction === 'both' && root.children.length > 1) {
    const kids = root.children;
    const split = Math.ceil(kids.length / 2);
    const rightKids = kids.slice(0, split);
    const leftKids = kids.slice(split);

    abstract.set(root.id, { b: 0, d: 0 });

    const groupExtent = (nodes: MindNode[]) => {
      const total = nodes.reduce(
        (sum, node) => sum + subtreeExtent(node, breadthSize, siblingGap, memo),
        0,
      );
      return total + siblingGap * (nodes.length - 1);
    };

    const placeGroup = (nodes: MindNode[], sign: 1 | -1) => {
      let cursor = -groupExtent(nodes) / 2;
      for (const node of nodes) {
        place(node, cursor, sign, breadthSize, siblingGap, memo, abstract);
        cursor += subtreeExtent(node, breadthSize, siblingGap, memo) + siblingGap;
      }
    };

    placeGroup(rightKids, 1);
    placeGroup(leftKids, -1);
  } else {
    // 'both' 只有一个子节点时退化为单向，避免根节点两侧不对称。
    const effective: Direction = direction === 'both' ? 'right' : direction;
    place(root, 0, 0, breadthSize, siblingGap, memo, abstract);
    if (effective === 'left') {
      for (const point of abstract.values()) point.d = -point.d;
    }
  }

  const positions = new Map<string, Point>();
  const bounds = emptyBounds();
  let first = true;

  // 把根节点平移到原点：布局本身是相对量，但坐标从 0 起算更便于 fitView 与调试。
  const offsetB = abstract.get(root.id)?.b ?? 0;

  for (const [id, point] of abstract) {
    const b = point.b - offsetB;
    const point2d: Point = vertical
      ? { x: b, y: point.d * (depthSize + depthGap) }
      : { x: point.d * (depthSize + depthGap), y: b };

    positions.set(id, point2d);

    const halfW = nodeWidth / 2;
    const halfH = nodeHeight / 2;
    const minX = point2d.x - halfW;
    const maxX = point2d.x + halfW;
    const minY = point2d.y - halfH;
    const maxY = point2d.y + halfH;

    if (first) {
      bounds.minX = minX;
      bounds.maxX = maxX;
      bounds.minY = minY;
      bounds.maxY = maxY;
      first = false;
    } else {
      bounds.minX = Math.min(bounds.minX, minX);
      bounds.maxX = Math.max(bounds.maxX, maxX);
      bounds.minY = Math.min(bounds.minY, minY);
      bounds.maxY = Math.max(bounds.maxY, maxY);
    }
  }

  bounds.width = bounds.maxX - bounds.minX;
  bounds.height = bounds.maxY - bounds.minY;

  return { positions, bounds, direction };
}

/**
 * 子节点相对父节点在哪一侧，决定连线从父节点的哪条边走、进入子节点的哪条边。
 * 用几何位置判断而不是看 direction：双向布局里根的两侧都有子节点，
 * 只看 direction 会把两侧的线都挂到同一个句柄上（正是连线绕过父节点的原因）。
 * 轴向仍要看 direction——垂直布局里兄弟间距比层间距大，比大小会判错。
 */
export type ChildSide = 'left' | 'right' | 'bottom' | 'top';

export function childSide(parent: Point, child: Point, direction: Direction): ChildSide {
  if (direction === 'down') return child.y >= parent.y ? 'bottom' : 'top';
  return child.x < parent.x ? 'left' : 'right';
}
