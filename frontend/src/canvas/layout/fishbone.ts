import type { MindNode } from '../../api/types';
import { NODE_SIZE, type LayoutResult, type Point } from './tidy';

/**
 * 鱼骨图布局（石川图）。
 *
 * 与 `tidy.ts` 并列而不是塞进去：树布局的输入是一棵**任意形状**的树，输出是
 * "父在上/左、子在下/右"的层级；鱼骨图的输入是**固定语义**的两层（一级分支 =
 * 原因大类，二级 = 具体原因），输出是"主骨 + 斜插 + 子原因"的几何。两者共用的
 * 只有 `NODE_SIZE` 与 `LayoutResult` 这个出参形状，算法没有一行重叠。
 *
 * 几何完全手写，不引库：鱼骨的"角度、槽位、骨长"本身就是一张可以逐项对照教科书
 * 的参数表（见下面每个常量的注释），交给通用图层库反而要把这些参数翻译成它的
 * 术语，再猜它把边画到了哪里。
 *
 * ## 教科书鱼骨的四件东西，以及本文件怎么给出它们
 *
 * 1. **主骨**：一条水平线，右端是待分析的"结果"（根节点）。
 * 2. **斜插的骨**：与主骨成 60°，上三个（人/机/料）、下三个（法/环/测）。
 * 3. **骨端的大类名**：贴在斜骨末端。
 * 4. **子原因**：沿斜骨排开，每个由一条**水平短线**接到斜骨上。
 *
 * 骨长按"这一侧最长的那一根"统一（理由见 `sideRise`）：鱼骨图中真正在数的是
 * 骨上短线的根数，而不是骨的长短；把骨长按原因数各排各的会让短骨的大类框压进
 * 邻骨的原因框，实测重叠 52×20 px。上下两侧各算各的，所以一张图通常仍是两档长度。
 *
 * ## 为什么这套几何不会出现重叠（写下来是为了以后改参数时知道边界在哪）
 *
 * 设同一侧相邻两根骨的锚点相距 `D = 2 * SLOT_PITCH = 264`，两个原因框的竖直中心
 * 分别离主骨 `rA`、`rB`。它们横向重叠的条件是
 * `rA - rB > (D - W) * tan60 ≈ 145`；而纵向重叠的条件是 `|rA - rB| < H = 48`。
 * 前者远大于后者，两个条件不可能同时成立 → 同侧不同骨的原因框永不重叠。
 *
 * 同一根骨上 `|rA - rB| ≥ 间距 ≥ H + 8 > 48` → 纵向永远分开。
 *
 * 大类框离主骨比本侧所有原因都远 `H/2 + CAT_GAP` → 与自己的原因不重叠；与**邻骨**
 * 的原因比的是"本侧最高原因 + 44"，差 20 的净空 → 也不重叠。
 *
 * 坐标语义：节点坐标是**中心点**（与 `layoutTidy` 一致），`links` 里的折线是
 * 画布坐标的折线（父节点 → 子节点这条连线该走哪几个点）。连线几何放在这里而不是
 * 渲染层，是因为"骨"和"短线"不是节点之间的直线——它们是布局产物，理应与节点坐标
 * 一起被单测钉住。
 */

/** 斜骨与主骨的夹角（度）。教科书鱼骨的标准角，也是"看起来像鱼骨"的主要来源。 */
export const BONE_ANGLE_DEG = 60;

const RAD = (BONE_ANGLE_DEG * Math.PI) / 180;
/**
 * tan60 ≈ 1.7320：骨上"竖直升高 rise"的那一点，横坐标比锚点左移 `rise / tan60`。
 *
 * 用 tan 而不是 sin + 骨长：骨长是"结果"（`竖直升高 / sin60`），横坐标是直接量。
 * 两个常数都写出来只会让读者去追"哪个是输入、哪个是推导"。
 */
const TAN = Math.tan(RAD);

/** 节点框尺寸取树布局那一份：换个数字，鱼骨的骨长与间距全都要重算。 */
const W = NODE_SIZE.width;
const H = NODE_SIZE.height;

/**
 * 相邻两根骨在**主骨上的锚点间距**。
 *
 * 依据：同名侧的两根骨（槽位相差 2）末端的大类框左右相邻，框宽 180，所以
 * `2 * SLOT_PITCH >= W + 24`（24 是与树布局同源的兄弟间隙）→ 下限 102。取 132
 * 而不是刚好 102：大类名在生成侧是「人（人员）」这种带括号的短句，画布上按 24 字
 * 截断后仍可能顶满 180 宽，多留 30 的余量不会让图变胖，却能让相邻大类的框之间
 * 永远有一条缝。
 */
const SLOT_PITCH = 132;

/** 头部（根节点）左边沿到第一根骨锚点的距离：留出"结果框"与骨头之间的呼吸。 */
const HEAD_GAP = 48;

/**
 * 同一根骨上相邻两个原因的竖直间距：框高 48 + 16 的呼吸。
 *
 * 这是**舒适档**，只在原因数 ≤ `COMFORTABLE_PER_BONE` 时用。
 */
const CAUSE_PITCH_Y = H + 16;

/**
 * 一根骨上"舒适档"能排几个原因。
 *
 * 贴着生成侧的实际形态：`FISHBONE_PLAN_PROMPT` 让每类给 2–4 条原因，六条骨一共
 * 十几二十个框。6 是"再多人眼就开始竖着数"的那条线。
 */
const COMFORTABLE_PER_BONE = 6;

/**
 * 超过舒适档之后换用的紧凑间距：框高 48 + 8。
 *
 * 为什么选"缩短间距"而不是"换列"——**换列会把框压到邻骨那一列上**：
 * 外侧列要往左再退一个框宽，而同一侧相邻骨的锚点只隔 264，实测 12 个原因时
 * 「人」的外侧原因框与「机」的第一个原因框重叠 154×16 px；要把列距拉到 444 才能
 * 避开，整张图会胖到两倍宽。缩短间距则**在构造上**不可能重叠（见文件头那条
 * 不重叠证明），代价只是"这类原因多的图更高"——而这条路径只服务异常数据。
 *
 * 不再往下压到 48（= 框高）以下：那样相邻原因框会贴着，画布上一眼看不出是两条
 * 原因还是一个框里换行。
 */
const TIGHT_PITCH_Y = H + 8;

/** 第一个原因框与主骨的净空：不小于一个框高，否则原因会压在主骨线上。 */
const SPINE_CLEAR = H;

/** 原因框到斜骨之间的**水平短线**长度（教科书里那截"小横杠"）。 */
const STUB = 16;

/** 最高的那个原因框与「大类框」之间的竖直缝。 */
const CAT_GAP = 20;

/**
 * 六类原因的**固定槽位**。
 *
 * 槽位号 = 从头部往左数第几根骨（0 起）；**偶数槽向上、奇数槽向下**，所以
 * 「人/机/料 在上、法/环/测 在下」与「上下交替」这两个要求同时成立：
 *
 * | 槽位 | 0 | 1 | 2 | 3 | 4 | 5 |
 * |------|---|---|---|---|---|---|
 * | 类别 | 人 | 法 | 机 | 环 | 料 | 测 |
 * | 方向 | ↑ | ↓ | ↑ | ↓ | ↑ | ↓ |
 *
 * 注意它不是「人机料法环测」的书写顺序：按书写顺序排会把三根上的骨挤在头部
 * 一侧、三根下的骨甩到尾部，得到一张"阶梯"而不是鱼骨。交替排才是教科书的样子，
 * 而这个表就是那次交替的唯一事实来源（要改顺序只改这张表）。
 */
export const FISHBONE_SLOTS: Readonly<Record<string, number>> = {
  人: 0,
  法: 1,
  机: 2,
  环: 3,
  料: 4,
  测: 5,
};

export interface FishboneGeometry {
  /**
   * `${父 id}->${子 id}` → 折线。没有登记的父子连线（三层以上的子孙）仍按默认
   * 连线画——鱼骨的语义只覆盖"主骨 → 大类 → 原因"这两级，硬往深处套只会失真。
   */
  links: Map<string, Point[]>;
  /** 主骨两端。渲染上由各根骨的水平段覆盖，这里给出来是为了单测能直接断言。 */
  spine: { from: Point; to: Point };
}

export interface FishboneLayout extends LayoutResult {
  fishbone: FishboneGeometry;
}

/**
 * 从 label 认出它属于哪一类，认不出返回 `null`。
 *
 * 用"最早出现的关键字"而不是"以关键字开头"：生成侧的大类名常写成
 * 「人（操作者）」「机器设备」「测量/检验」，开头未必就是那个字。两个字同时出现时
 * （「人机交互」）取位置更靠前的那个——那是这行字真正在讲的类别。
 */
export function fishboneSlotOf(label: string): number | null {
  // 空白与常见标点先去掉再找：`人：招聘不足` 与 `人员` 都要能认出「人」。
  const text = label.replace(/[\s:：、,，.。·\-—_/（）()【】[\]]/g, '');
  let best: { at: number; slot: number } | null = null;
  for (const [name, slot] of Object.entries(FISHBONE_SLOTS)) {
    const at = text.indexOf(name);
    if (at < 0) continue;
    if (!best || at < best.at || (at === best.at && slot < best.slot)) best = { at, slot };
  }
  return best ? best.slot : null;
}

/**
 * 一根骨上 n 个原因的竖坐标（离主骨的距离，恒为正；方向由槽位决定）。
 *
 * 单调递增，所以"沿斜骨向外排"是构造出来的性质，不是渲染时凑的。
 *
 * 超过 `COMFORTABLE_PER_BONE` 就换紧凑间距（48 + 8）。**为什么不换列**、以及
 * 为什么这套间距在构造上不会重叠，见文件头那段证明。
 */
function causeRises(count: number): number[] {
  const pitch = count > COMFORTABLE_PER_BONE ? TIGHT_PITCH_Y : CAUSE_PITCH_Y;
  return Array.from({ length: count }, (_, index) => SPINE_CLEAR + (index + 0.5) * pitch);
}

/** 斜骨上"高度为 rise 的点"的横坐标：从锚点向左上（或左下）走 rise / tan60。 */
function boneX(anchorX: number, rise: number): number {
  return anchorX - rise / TAN;
}

/**
 * 计算整张鱼骨图。
 *
 * 一级分支（根的直接子节点）落在六根骨上；每个一级分支的子节点沿自己的骨排开；
 * 更深的后代仍按"往左一列"的简单堆叠处理（生成侧只给两级，这是兜底）。
 */
export function layoutFishbone(root: MindNode): FishboneLayout {
  const positions = new Map<string, Point>();
  const links = new Map<string, Point[]>();

  // 头部（结果）固定在原点，主骨沿 y = 0 向左延伸。
  const headCenter: Point = { x: 0, y: 0 };
  positions.set(root.id, headCenter);
  const spineStart: Point = { x: headCenter.x - W / 2, y: 0 };

  // ---- 槽位分配：认得出的按固定表，认不出的按顺序补空位 ----
  const taken = new Map<number, number>(); // 槽位 → 子节点下标
  const pending: number[] = [];
  root.children.forEach((child, index) => {
    const slot = fishboneSlotOf(child.label);
    if (slot !== null && !taken.has(slot)) taken.set(slot, index);
    else pending.push(index);
  });
  // 剩下的按子节点顺序填最小的空槽：既不打乱已有映射，也不会两个类别抢同一个槽。
  let cursor = 0;
  for (const index of pending) {
    while (taken.has(cursor)) cursor += 1;
    taken.set(cursor, index);
    cursor += 1;
  }

  const slotOfChild = new Map<number, number>(); // 子节点下标 → 槽位
  for (const [slot, index] of taken) slotOfChild.set(index, slot);

  /** 一根骨的初算：锚点、各个原因的竖坐标、以及它自己需要的骨长。 */
  interface BonePlan {
    category: MindNode;
    side: 1 | -1;
    anchorX: number;
    rises: number[];
    /** 这根骨自己想长到多高（最高那个原因框之上再留一个缝，给大类框让位）。 */
    ownTipRise: number;
  }

  const plans: BonePlan[] = root.children.map((category, childIndex) => {
    const slot = slotOfChild.get(childIndex) ?? childIndex;
    const rises = causeRises(category.children.length);
    const deepest = rises.reduce((max, rise) => Math.max(max, rise), 0);
    return {
      category,
      // 偶数槽向上、奇数槽向下。**画布的 y 轴朝下**，所以"向上"是 y 取负——
      // 这一处写反的话，整张鱼骨会上下颠倒，而几何关系与包围盒一个数都不会变。
      side: slot % 2 === 0 ? -1 : 1,
      anchorX: spineStart.x - HEAD_GAP - slot * SLOT_PITCH,
      rises,
      ownTipRise: Math.max(deepest, SPINE_CLEAR) + H / 2 + CAT_GAP,
    };
  });

  /**
   * 同一侧共用一个骨长：取该侧最长的那一根。
   *
   * 为什么不能各长各的：骨端的大类框是 180 宽的实体，它落在骨的最高处；而原因框是
   * 180 宽的实体、落在骨的中段，一个原因多的类别会把这排框伸到相邻骨的正上方。
   * 骨一短，它的大类框正好压进邻居的原因框里——实测「0 个原因的环」紧挨着
   * 「2 个原因的法」时重叠 52×20 px。要让"各长各的"成立，同侧骨间距得从 264 拉到
   * 600 以上（否则标签的横坐标一定落进邻居原因列的横坐标区间），六根骨会撑到
   * 2000px 宽，缩放后 12px 的字变成 8px，得不偿失。
   *
   * 取齐之后的代价说清楚：**骨长不再逐类反映原因数**，同侧三根一样长；"这一类原因
   * 更多"改由骨上短线的根数体现（这正是读鱼骨图时真正在数的东西）。上下两侧各算
   * 各的，所以一整张图的六根骨通常仍是两档长度。
   */
  const sideRise = new Map<1 | -1, number>([[1, 0], [-1, 0]]);
  for (const plan of plans) {
    sideRise.set(plan.side, Math.max(sideRise.get(plan.side) ?? 0, plan.ownTipRise));
  }

  let spineEnd = spineStart.x;

  for (const plan of plans) {
    const { category, side, anchorX, rises } = plan;
    const anchor: Point = { x: anchorX, y: 0 };
    const tipRise = sideRise.get(side) ?? plan.ownTipRise;
    const tip: Point = { x: boneX(anchor.x, tipRise), y: side * tipRise };
    // 大类框贴在骨端：骨正好落在框的下沿（向下那侧是上沿），框不再额外偏移。
    positions.set(category.id, { x: tip.x, y: side * (tipRise + H / 2) });
    links.set(`${root.id}->${category.id}`, [spineStart, anchor, tip]);
    spineEnd = Math.min(spineEnd, anchor.x);

    category.children.forEach((cause, causeIndex) => {
      const rise = rises[causeIndex];
      if (rise === undefined) return;
      const hook: Point = { x: boneX(anchor.x, rise), y: side * rise };
      // 原因框在短线的左端：框的右沿在挂点左边一个短线长。
      const boxRight = hook.x - STUB;
      positions.set(cause.id, { x: boxRight - W / 2, y: hook.y });
      links.set(`${category.id}->${cause.id}`, [hook, { x: boxRight, y: hook.y }]);

      // 三层以上：往左排一列，竖直方向按兄弟顺序铺开。生成侧不产这种树，
      // 真出现了也不该丢节点——宁可画得朴素，也不能让内容从画布上消失。
      layoutDeeper(cause, positions, boxRight - W / 2, hook.y);
    });
  }

  const bounds = boundsOf(positions);
  return {
    positions,
    bounds,
    // 鱼骨的"朝向"是固定的（头在右、骨向左斜），不随 theme.direction 变：
    // 一个上下翻转的鱼骨图不再像鱼骨，所以这里只报 'right' 让连线句柄走左右两侧。
    direction: 'right',
    fishbone: { links, spine: { from: spineStart, to: { x: spineEnd, y: 0 } } },
  };
}

/**
 * 三层及更深的后代：在祖先左侧排一列。
 *
 * 与鱼骨语义无关，纯粹是"不能把节点丢了"。间距沿用树布局的兄弟间隙，纵向居中于
 * 父节点；同一父节点的子节点等距铺开。这种树在生成侧不会出现（鱼骨提示词只要求
 * 两级），所以不做重叠规避——真需要的话那说明数据形态已经变了，应该换布局。
 */
function layoutDeeper(node: MindNode, positions: Map<string, Point>, parentX: number, parentY: number): void {
  node.children.forEach((child, index) => {
    const offset = (index - (node.children.length - 1) / 2) * CAUSE_PITCH_Y;
    const point = { x: parentX - W - 24, y: parentY + offset };
    positions.set(child.id, point);
    layoutDeeper(child, positions, point.x, point.y);
  });
}

/** 包围盒：只看节点框（主骨与短线一定落在某个节点框的左边，不会越界）。 */
function boundsOf(positions: Map<string, Point>): LayoutResult['bounds'] {
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const point of positions.values()) {
    minX = Math.min(minX, point.x - W / 2);
    maxX = Math.max(maxX, point.x + W / 2);
    minY = Math.min(minY, point.y - H / 2);
    maxY = Math.max(maxY, point.y + H / 2);
  }
  if (!Number.isFinite(minX)) return { minX: 0, minY: 0, maxX: 0, maxY: 0, width: 0, height: 0 };
  return { minX, minY, maxX, maxY, width: maxX - minX, height: maxY - minY };
}
