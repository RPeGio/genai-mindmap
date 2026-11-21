/**
 * 画布上两个浮层（节点工具条、节点详情卡）的位置。**一处算，两处用。**
 *
 * 为什么必须是一处：这两样东西原先各自摆自己的位——工具条贴在节点**上方**，
 * 详情卡也是贴在节点**上方**。于是"选中一个节点"再"悬停它"时，工具条正好压在那段
 * 正文上（实测「明确目标分数」那一支：字被 `[改][展开][删][色]` 挡掉一半）。
 * 各自加一条避让规则解决不了：两个组件不知道对方在哪，规则会互相打架。
 *
 * 所以位置在这里合成一次：先摆**详情卡**（它是被动的内容块，语义就是"在节点旁边"），
 * 再摆**工具条**，并且让它躲开详情卡已经占下的那块矩形。工具条的候选顺序是
 * **侧面 → 下方 → 上方**——它是可交互控件，宁可挪远一点，也不要盖住用户要读的字。
 *
 * 这个文件只做几何，不碰 DOM 之外的东西（`nodeRect` / `canvasArea` 是读 DOM 的
 * 两个入口，同样只此一份：两处各写一次 `main > section` 的结果已经出过一次偏差）。
 */

export interface Rect {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

export interface Size {
  width: number;
  height: number;
}

export type Placement = 'right' | 'left' | 'below' | 'above';

/** 一个浮层摆好之后的位置：**左上角**（不再用 `translate` 居中，省得两处算法不一致）。 */
export interface Spot {
  x: number;
  y: number;
  placement: Placement;
}

/** 离画布边缘至少留这么多像素：贴边节点的浮层不压在屏幕上。 */
export const OVERLAY_MARGIN = 8;

/** 浮层与节点之间留的缝。 */
export const OVERLAY_GAP = 8;

/** 详情卡的固定宽度：`w-72` 是 288px，与 `MindNode` 的类名同一份口径。 */
export const DETAIL_WIDTH = 288;

export interface OverlayItem {
  /** 浮层贴着的那个节点（视口坐标）。 */
  anchor: Rect;
  /** 浮层自己多大。 */
  size: Size;
}

export interface PlanInput {
  area: Rect;
  toolbar: OverlayItem | null;
  detail: OverlayItem | null;
  margin?: number;
  gap?: number;
}

export interface Plan {
  toolbar: Spot | null;
  detail: Spot | null;
}

/** 详情卡的候选顺序：先上方（不挡节点、视线连续），放不下再依次让位。 */
const DETAIL_ORDER: Placement[] = ['above', 'below', 'right', 'left'];
/** 工具条的候选顺序：侧面优先，其次下方，最后才是上方。 */
const TOOLBAR_ORDER: Placement[] = ['right', 'left', 'below', 'above'];

export function rectOf(spot: Spot, size: Size): Rect {
  return {
    left: spot.x,
    top: spot.y,
    right: spot.x + size.width,
    bottom: spot.y + size.height,
  };
}

function intersect(a: Rect, b: Rect): boolean {
  return a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;
}

function inside(rect: Rect, area: Rect, margin: number): boolean {
  return rect.left >= area.left + margin
    && rect.top >= area.top + margin
    && rect.right <= area.right - margin
    && rect.bottom <= area.bottom - margin;
}

function spotFor(item: OverlayItem, placement: Placement, gap: number): Spot {
  const { anchor, size } = item;
  const middleX = anchor.left + (anchor.right - anchor.left) / 2 - size.width / 2;
  const middleY = anchor.top + (anchor.bottom - anchor.top) / 2 - size.height / 2;
  switch (placement) {
    case 'right':
      return { x: anchor.right + gap, y: middleY, placement };
    case 'left':
      return { x: anchor.left - gap - size.width, y: middleY, placement };
    case 'below':
      return { x: middleX, y: anchor.bottom + gap, placement };
    default:
      return { x: middleX, y: anchor.top - gap - size.height, placement };
  }
}

function clamp(value: number, low: number, high: number): number {
  return Math.min(Math.max(value, low), Math.max(low, high));
}

/** 一个位置都放不下时的兜底：夹进画布，宁可压住节点也不飘出去。 */
function clampInto(spot: Spot, size: Size, area: Rect, margin: number): Spot {
  return {
    x: clamp(spot.x, area.left + margin, area.right - margin - size.width),
    y: clamp(spot.y, area.top + margin, area.bottom - margin - size.height),
    placement: spot.placement,
  };
}

/** 两个矩形重叠的面积（不重叠时是 0）。 */
export function overlapArea(a: Rect, b: Rect): number {
  const width = Math.min(a.right, b.right) - Math.max(a.left, b.left);
  const height = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
  return width > 0 && height > 0 ? width * height : 0;
}

function place(
  item: OverlayItem,
  area: Rect,
  order: Placement[],
  margin: number,
  gap: number,
  avoid: Rect[],
): Spot {
  for (const placement of order) {
    const spot = spotFor(item, placement, gap);
    const rect = rectOf(spot, item.size);
    // 三条同时成立才算"放得下"：整块在画布里、没盖住自己的节点、没盖住对方。
    if (!inside(rect, area, margin)) continue;
    if (intersect(rect, item.anchor)) continue;
    if (avoid.some(other => intersect(rect, other))) continue;
    return spot;
  }
  // 一个都放不下（画布太窄、节点卡在角里）：夹进画布，并挑**压得最少**的那一个。
  // 摆放顺序在这里已经失效，但"宁可压住一点、也不飘出屏幕"这条仍然成立。
  let best: Spot | null = null;
  let least = Infinity;
  for (const placement of order) {
    const spot = clampInto(spotFor(item, placement, gap), item.size, area, margin);
    const rect = rectOf(spot, item.size);
    const cost = avoid.reduce((sum, other) => sum + overlapArea(rect, other), 0)
      + overlapArea(rect, item.anchor);
    if (cost < least) {
      least = cost;
      best = spot;
    }
    if (least === 0) break;
  }
  return best ?? clampInto(spotFor(item, order[0], gap), item.size, area, margin);
}

export function planOverlays(input: PlanInput): Plan {
  const margin = input.margin ?? OVERLAY_MARGIN;
  const gap = input.gap ?? OVERLAY_GAP;

  // 详情卡先摆：它是被动的正文块，位置诉求只有"贴着这个节点"。
  const detail = input.detail
    ? place(input.detail, input.area, DETAIL_ORDER, margin, gap, [])
    : null;
  const taken = detail && input.detail ? [rectOf(detail, input.detail.size)] : [];
  // 工具条后摆：它有四个候选方向，是那个"该让路的人"。
  const toolbar = input.toolbar
    ? place(input.toolbar, input.area, TOOLBAR_ORDER, margin, gap, taken)
    : null;

  return { toolbar, detail };
}

// ---------------------------------------------------------------- 读 DOM

/**
 * 画布那块（`main > section`）的边界，不是整个视口。
 *
 * 用视口的话，一个被平移到屏幕外的节点会把浮层按在 (8, 8)——正好压在顶栏上，
 * "没飘出去"变成了"盖住了返回键"。量不到画布（jsdom、还没布局）时退回视口。
 */
export function canvasArea(): Rect {
  const rect = document.querySelector('main > section')?.getBoundingClientRect();
  if (rect && rect.width > 0 && rect.height > 0) {
    return { left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom };
  }
  return { left: 0, top: 0, right: window.innerWidth, bottom: window.innerHeight };
}

/** 某个节点在视口里的外接框；画布还没画出它时返回 `null`（调用方保持原位，别甩到左上角）。 */
export function nodeRect(id: string): Rect | null {
  const el = document.querySelector(`.vue-flow__node[data-id="${CSS.escape(id)}"]`);
  if (!(el instanceof HTMLElement)) return null;
  const rect = el.getBoundingClientRect();
  return { left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom };
}

export function sizeOf(rect: Rect): Size {
  return { width: rect.right - rect.left, height: rect.bottom - rect.top };
}

/**
 * 画布的平移/缩放监听。
 *
 * 平移与缩放不是 window 事件：Vue Flow 把整张图挂在一个元素上，用一句
 * `transform: translate(...) scale(...)` 挪它——既不发 scroll 也不发 resize。
 * 只听 window 的 resize/scroll 时，用户按住画布一拖，浮层就停在拖之前的坐标。
 *
 * 注意落点：`@vue-flow/core` 1.x 的 DOM 是
 * `.vue-flow__viewport > .vue-flow__pane > .vue-flow__transformationpane`，那句
 * transform 写在**最里面**那个上。盯错了元素，监听装得上、一次都不响。两个都盯。
 *
 * `ensure()` 可以反复调（画布首次渲染完成之前那几次都装不上，之后每次重算顺手补装）。
 */
export function canvasWatcher(onChange: () => void): {
  ensure: () => void;
  dispose: () => void;
} {
  const selectors = ['.vue-flow__transformationpane', '.vue-flow__viewport'];
  let observer: MutationObserver | null = null;
  let frame = 0;

  const schedule = (): void => {
    if (frame) return;
    frame = requestAnimationFrame(() => {
      frame = 0;
      onChange();
    });
  };

  window.addEventListener('resize', schedule);
  window.addEventListener('scroll', schedule, true);

  return {
    ensure(): void {
      if (observer) return;
      if (typeof MutationObserver === 'undefined') return;
      const targets = document.querySelectorAll(selectors.join(', '));
      if (!targets.length) return;  // 画布还没渲染完：下一次 ensure 再试
      observer = new MutationObserver(schedule);
      targets.forEach(target =>
        observer?.observe(target, { attributes: true, attributeFilter: ['style'] }));
    },
    dispose(): void {
      if (frame) cancelAnimationFrame(frame);
      frame = 0;
      observer?.disconnect();
      observer = null;
      window.removeEventListener('resize', schedule);
      window.removeEventListener('scroll', schedule, true);
    },
  };
}
