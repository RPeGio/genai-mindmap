import type { MindNode } from '../../api/types';
import { NODE_SIZE, type LayoutResult, type Point } from './tidy';

/**
 * 鱼骨图布局（石川图）。
 *
 * 与 `tidy.ts` 并列而不是塞进去：树布局的输入是一棵**任意形状**的树，输出是
 * "父在上/左、子在下/右"的层级；鱼骨图的输入是**固定语义**的三层（一级分支 =
 * 原因大类，二级 = 具体原因，三级 = 那条原因的下属原因），输出是"主骨 + 斜插 +
 * 子原因"的几何。两者共用的只有 `NODE_SIZE` 与 `LayoutResult` 这个出参形状，
 * 算法没有一行重叠。
 *
 * 几何完全手写，不引库：鱼骨的"角度、槽位、骨长"本身就是一张可以逐项对照教科书
 * 的参数表（见下面每个常量的注释），交给通用图层库反而要把这些参数翻译成它的
 * 术语，再猜它把边画到了哪里。
 *
 * ## 教科书鱼骨的五件东西，以及本文件怎么给出它们
 *
 * 1. **主骨**：一条水平线，右端是待分析的"结果"（根节点）。
 * 2. **斜插的骨**：与主骨成 60°，上三个（人/机/料）、下三个（法/环/测）。
 * 3. **骨端的大类名**：贴在斜骨末端。
 * 4. **子原因**：沿斜骨排开，每个由一条**水平短线**接到斜骨上。
 * 5. **骨上的骨（中骨）**：某一根原因自己还带原因时，它不画成一个普通原因框，
 *    而是**斜出来一根中骨**，小原因用同样的短线挂在它身上（见下面那节）。
 *
 * 骨长按"这一侧最长的那一根"统一（理由见 `sideRise`）：鱼骨图中真正在数的是
 * 骨上短线的根数，而不是骨的长短；把骨长按原因数各排各的会让短骨的大类框压进
 * 邻骨的原因框，实测重叠 52×20 px。上下两侧各算各的，所以一张图通常仍是两档长度。
 *
 * ## 中骨：骨上的骨
 *
 * 一级分支的子节点如果**自己还有子节点**，它就是一根中骨。中骨与大骨同斜率（都
 * 成 60°）斜出，标签框落在自己那条乐道的**最外一格**（与大骨的"大类框在骨端"同
 * 一条规则），它的小原因用与"原因挂在大骨上"完全相同的水平短线挂在它身上。于是
 * "骨 → 原因 → 原因的原因"在画布上变成"大骨 → 中骨 → 小原因"三级，每一级都看得
 * 见——改动前第三条腿是"往左排一列"，与外层的一列框对齐，读图的人分不出哪个框是
 * 挂在谁的骨上。
 *
 * 两个自由参数，都被同侧骨距逼出来：
 *
 * ```
 * 同侧相邻两根骨在主骨上的锚点相距   2 * SLOT_PITCH = 264
 * 大骨那条乐道已经占掉               挂点 16 + 框宽 180 = 196
 * 剩下的余量                              264 - 196 = 68
 * 中骨乐道需要                     MID_OFFSET + 16 + 180 ≤ 68 - 12(净空)
 * ```
 *
 * 所以 `MID_OFFSET` 的上限是 56，取**上限**：再大，中骨的框就压到外侧邻骨那条斜线
 * 上；再小，两列 180 宽的框在画布上分不出"这是骨上的骨"。56 把 68 的余量用满，只
 * 留 12 给邻骨那条 1.5px 的线。
 *
 * 也正因为那 68 的余量**只有一份**，中骨最多一层：再嵌一层要
 * `56 * 2 + 16 + 180 = 308 > 264`，一定压到邻骨上。第四层及更深回到 `layoutDeeper`
 * 的"往左排一列"——与改动前同一个兜底，同一个理由：宁可画得朴素，也不能让内容从
 * 画布上消失。
 *
 * ## 主骨末端的箭头
 *
 * 教科书的主骨是一条指向鱼头的箭头，表达"这些原因导致这个结果"。这里把它当成
 * **折线的收尾**由 `FishboneEdge.vue` 画一个实心三角形，而不是 `<marker>`：
 *
 *  - 主骨由六条骨线各自绘制同一段（`[spineStart, 锚点]`），`marker-end` 只会落在
 *    **每根斜骨的骨尖**上，方向也由最后一段（斜段）决定——正好是错的位置和错的朝向；
 *  - `<marker>` 的尺寸由 `markerUnits` 决定（缺省 `strokeWidth`：跟着描边宽度走），
 *    哪天有人把骨线加粗，箭头会跟着一起胖；显式三角形没有这层耦合；
 *  - 三角形的三个点在布局这边算死（尖端压在鱼头左沿，朝向 +x），方向不可能是
 *    "最后一段的方向"算出来的。
 *
 * ## 为什么这套几何不会出现重叠（写下来是为了以后改参数时知道边界在哪）
 *
 * 设同一侧相邻两根骨的锚点相距 `D = 2 * SLOT_PITCH = 264`。一根骨的内容带里，所有
 * 框的中心至少相差 `H + 8`（同一条乐道里相邻两项、中骨的标签与外层原因、最外一项
 * 与大类框，三处的最小值都是 56 或更大）——大于框高，**纵向**上永不重叠。
 *
 * 横向则逐列核：大骨那条乐道占 `[骨线-196, 骨线-16]`，中骨乐道占
 * `[骨线-252, 骨线-72]`，两者都比邻骨那条斜线（`骨线-264`）更靠右 ≥ 12；而一根骨
 * 的大类框（±90 宽，落在骨端）与内侧邻骨的乐道在横向上会相交，但大类框在内容带
 * **之外**再加 `CAT_GAP + H`，纵向差 68 → 也不重叠。
 *
 * 同一根骨上 `|rA - rB| ≥ 间距 ≥ H + 8 > 48` → 纵向永远分开。
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
 *
 * 它同时是中骨乐道的预算上限：`264 - 196 = 68` 的余量只有这一份，所以中骨只能
 * 嵌一层（见文件头）。
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
 * 中骨相对大骨线的**横向偏移**（大骨上那个挂点到中骨自身的横坐标差，也就是接它们
 * 的那截水平短线的长度）。
 *
 * 取 56 = `2 * SLOT_PITCH - (STUB + W) - 12`：同侧骨距 264 减去大骨自己那条乐道的
 * 196，再留 12 的净空给邻骨那条 1.5px 的斜线。这是上限——更大就压到邻骨上；更小则
 * 两根 180 宽的列只差十几像素，看不出"骨上的骨"（见文件头那三行算式）。
 */
const MID_OFFSET = 56;

/**
 * 中骨上第一个小原因离**挂点**（中骨与大骨相交的那一点）的净空。
 *
 * 与 `SPINE_CLEAR` 同一条理由，量的是另一件事：这里要的不是"别压在主骨线上"，而是
 * "挂点与第一个小原因之间得看得见一段斜线"。小于一个框高时，中骨会被第一个小原因
 * 的短线整个盖住，看起来像一根横杠而不是一根斜着长出来的骨。
 */
const MID_CLEAR = H;

/**
 * 中骨上相邻两个小原因的竖直间距：框高 48 + 8，即**永远走紧凑档**。
 *
 * 为什么不像大骨那样分舒适/紧凑两档：嵌套本来就是例外形态（生成侧只给两层），
 * 一棵三层树已经在纵向吃掉"一整根骨"的高度；再按 16 的舒适间距铺，一棵三层鱼骨
 * 会比两层的同规模高 40%，缩放后 14px 的字掉到 10px 以下。
 *
 * 48 + 8 仍是"不重叠"的充分条件（间距 > 框高），与 `TIGHT_PITCH_Y` 同一个数、同一
 * 个理由。
 */
const MID_PITCH = TIGHT_PITCH_Y;

/**
 * 主骨末端箭头的尺寸（布局坐标）。
 *
 * 尖端落在鱼头（根节点框）的左沿，所以箭头是**画在骨线上**的一小段收尾：长 12、
 * 半高 5（12:10，教科书那种瘦长的实心箭），1.5px 的骨线末端收成这个三角形，比
 * 线本身重一档，一眼能看出"方向"。
 *
 * 单位是**布局坐标**，跟着画布一起等比缩放（与节点框、骨线完全同一个变换）——不是
 * 屏幕像素。理由见文件头"主骨末端的箭头"那节。
 */
export const SPINE_ARROW_SIZE = { length: 12, halfHeight: 5 } as const;

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
   * `${父 id}->${子 id}` → 折线。三种形态，点数就能分清：
   *
   *  - 主骨 → 大类（3 点）：`[主骨起点, 主骨上的锚点, 骨尖]`；
   *  - 大类 → 中骨（4 点）：`[大骨上的挂点, 中骨起点, 中骨骨尖, 标签框右沿]`——
   *    第一段是横向的连接短线，第二段是与大骨同斜率的骨身，第三段是挂标签的短线；
   *  - 骨 → 原因（2 点）：一段水平的短线，起点**在骨上**。
   *
   * 没有登记的父子连线（第四层及更深的子孙）仍按默认连线画——中骨最多一层，再深
   * 就没有乐道可分了（见文件头）。
   */
  links: Map<string, Point[]>;
  /** 主骨两端。渲染上由各根骨的水平段覆盖，这里给出来是为了单测能直接断言。 */
  spine: { from: Point; to: Point };
  /**
   * 主骨末端的箭头（三角形的三个点，尖端在 `spine.from`），以及**它归哪条骨线画**。
   *
   * 只登记一条：主骨那一小段被六条骨线各画了一遍，箭头谁都能画——都画就多出五份
   * 一模一样的 DOM。交给锚点最靠右（离鱼头最近）的那一根，那一段本来就归它。
   *
   * 没有骨时为 `null`：一条骨都没有的鱼骨图没有方向可言。
   */
  spineArrow: { link: string; points: Point[] } | null;
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

/** 骨上相邻两项的间距档位：原因多到一定数量才换紧凑档（与改动前同一个判据）。 */
function pitchOf(count: number): number {
  return count > COMFORTABLE_PER_BONE ? TIGHT_PITCH_Y : CAUSE_PITCH_Y;
}

/**
 * 内容带里"第一项"离骨原点的距离。
 *
 * 每档间距都把它多出来的那 16 / 8 像素**对半分**：一半放在第一个框之前、一半放在
 * 框与框之间。所以舒适档的第一格中心是 `48 + 8 + 24 = 80`，紧凑档是
 * `48 + 4 + 24 = 76`——与改动前 `SPINE_CLEAR + (i + 0.5) * pitch` 那句一字不差地
 * 相等（`fishbone.spec.ts` 里有一组坐标把这个等式钉住）。
 */
function bandLead(pitch: number): number {
  return SPINE_CLEAR + (pitch - H) / 2;
}

/**
 * 中骨上 n 个小原因的竖坐标（离**挂点**的距离，恒为正）。
 *
 * 与大骨那套 `CAUSE_PITCH_Y / TIGHT_PITCH_Y` 分开：中骨挂在骨身的中间，起点不是
 * 主骨；它的净空、间距、能挂几条都是另一套数（`MID_CLEAR` / `MID_PITCH`）。
 * 单调递增，所以"沿中骨向外排"是构造出来的性质，不是渲染时凑的。
 */
function midRises(count: number): number[] {
  return Array.from({ length: count }, (_, index) => MID_CLEAR + (index + 0.5) * MID_PITCH);
}

/** 斜骨上"高度为 rise 的点"的横坐标：从锚点向左上（或左下）走 rise / tan60。 */
function boneX(anchorX: number, rise: number): number {
  return anchorX - rise / TAN;
}

/**
 * 骨上要摆的一项：叶子原因（一个框），或者中骨（一根斜骨 + 它自己那几个框）。
 *
 * 三种 rise 都是**离这根骨的原点**（大骨：主骨上的锚点）的距离，绝对值，不是相对
 * 上一项——画出坐标时不必再做累加，单测也能直接对着数看。
 */
type BoneItem =
  | { kind: 'cause'; node: MindNode; rise: number }
  | {
    kind: 'mid';
    node: MindNode;
    /** 中骨挂在大骨上的那一点（rise）。 */
    hook: number;
    /** 中骨骨尖 = 它自己标签框的中心线（末段那截短线从骨尖接到框的右沿）。 */
    tipRise: number;
    children: Array<{ node: MindNode; rise: number }>;
  };

interface BoneBand {
  items: BoneItem[];
  /** 这块内容带里**最外那个框的中心**离骨原点多远；一个框都没有时为 0。 */
  outerCenter: number;
}

/**
 * 一根骨上的内容带：按兄弟顺序从主骨往外铺，叶子一格、中骨一整块。
 *
 * 只有叶子时它必须与改动前逐像素相同（`bandLead` + `H + gap` 的累加正好给出
 * `SPINE_CLEAR + (i + 0.5) * pitch`）；混进中骨时，中骨整块独占一段，下一项从它
 * 最外那一格再让出一个 `gap`——所以同一条骨上任意两个框的中心至少差 `H + 8`。
 */
function boneBand(children: readonly MindNode[]): BoneBand {
  const pitch = pitchOf(children.length);
  const gap = pitch - H;
  let cursor = bandLead(pitch);
  const items: BoneItem[] = [];
  let outerCenter = 0;

  for (const child of children) {
    if (child.children.length === 0) {
      const rise = cursor + H / 2;
      items.push({ kind: 'cause', node: child, rise });
      outerCenter = rise;
      cursor += H + gap;
      continue;
    }

    // 中骨：挂点在内沿，骨尖落在标签框那一行。整块的高度 = 标签中心 + 半个框。
    const hook = cursor;
    const rises = midRises(child.children.length);
    const children = child.children.map((node, index) => ({ node, rise: hook + rises[index]! }));
    const lastRise = children[children.length - 1]!.rise;
    // 与"最外那个原因框到它的大类框"同一个净空（H + CAT_GAP）：中骨的标签框在乐道的
    // 最外一格，离它自己的最后一个小原因 68，中间那条缝比框与框之间的 16 宽，一眼能
    // 看出"这里换了一种东西"。
    const tipRise = lastRise + H + CAT_GAP;
    items.push({ kind: 'mid', node: child, hook, tipRise, children });
    outerCenter = tipRise;
    cursor = tipRise + H / 2 + gap;
  }

  return { items, outerCenter };
}

/**
 * 计算整张鱼骨图。
 *
 * 一级分支（根的直接子节点）落在六根骨上；每个一级分支的子节点沿自己的骨排开，
 * 自己还带子节点的那些斜出一根中骨、再往下挂一层；第四层及更深仍按"往左一列"
 * 兜底（生成侧只给两层，这是异常数据的出口）。
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

  /** 一根骨的初算：锚点、它的内容带、以及它自己需要的骨长。 */
  interface BonePlan {
    category: MindNode;
    side: 1 | -1;
    anchorX: number;
    band: BoneBand;
    /** 这根骨自己想长到多高（最外那个框之上再留一个缝，给大类框让位）。 */
    ownTipRise: number;
  }

  const plans: BonePlan[] = root.children.map((category, childIndex) => {
    const slot = slotOfChild.get(childIndex) ?? childIndex;
    const band = boneBand(category.children);
    return {
      category,
      // 偶数槽向上、奇数槽向下。**画布的 y 轴朝下**，所以"向上"是 y 取负——
      // 这一处写反的话，整张鱼骨会上下颠倒，而几何关系与包围盒一个数都不会变。
      side: slot % 2 === 0 ? -1 : 1,
      anchorX: spineStart.x - HEAD_GAP - slot * SLOT_PITCH,
      band,
      ownTipRise: Math.max(band.outerCenter, SPINE_CLEAR) + H / 2 + CAT_GAP,
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
  /** 锚点最靠右（离鱼头最近）的那条骨线：主骨末端箭头归它画，只画一次。 */
  let headLink: string | null = null;
  let headAnchorX = -Infinity;

  for (const plan of plans) {
    const { category, side, anchorX, band } = plan;
    const anchor: Point = { x: anchorX, y: 0 };
    const tipRise = sideRise.get(side) ?? plan.ownTipRise;
    const tip: Point = { x: boneX(anchor.x, tipRise), y: side * tipRise };
    // 大类框贴在骨端：骨正好落在框的下沿（向下那侧是上沿），框不再额外偏移。
    positions.set(category.id, { x: tip.x, y: side * (tipRise + H / 2) });
    links.set(`${root.id}->${category.id}`, [spineStart, anchor, tip]);
    spineEnd = Math.min(spineEnd, anchor.x);
    if (anchorX > headAnchorX) {
      headAnchorX = anchorX;
      headLink = `${root.id}->${category.id}`;
    }

    /** 中骨那条线的"锚点"：与大骨同斜率，只是整体左移 `MID_OFFSET`。 */
    const midAnchorX = anchor.x - MID_OFFSET;

    for (const item of band.items) {
      if (item.kind === 'cause') {
        const hook: Point = { x: boneX(anchor.x, item.rise), y: side * item.rise };
        // 原因框在短线的左端：框的右沿在挂点左边一个短线长。
        const boxRight = hook.x - STUB;
        positions.set(item.node.id, { x: boxRight - W / 2, y: hook.y });
        links.set(`${category.id}->${item.node.id}`, [hook, { x: boxRight, y: hook.y }]);

        // 第四层及更深：往左排一列（中骨已经用掉了同侧骨距里唯一一份余量）。
        layoutDeeper(item.node, positions, boxRight - W / 2, hook.y);
        continue;
      }

      // ---- 中骨：一截横向连接短线 + 一段 60° 的骨身 + 一截挂标签的短线 ----
      const hook: Point = { x: boneX(anchor.x, item.hook), y: side * item.hook };
      const midStart: Point = { x: boneX(midAnchorX, item.hook), y: hook.y };
      // 中骨的骨尖落在**标签框的中心线**上，最后那 16 像素是接标签框的水平短线——
      // 与大骨上的原因框同一条规则（框的右沿恒在骨线左边一个短线长）。少了这一截，
      // 中骨的标签框就是全图唯一一个"没有短线、悬在骨旁边"的框。
      const midTip: Point = { x: boneX(midAnchorX, item.tipRise), y: side * item.tipRise };
      const labelRight: Point = { x: midTip.x - STUB, y: midTip.y };
      links.set(`${category.id}->${item.node.id}`, [hook, midStart, midTip, labelRight]);

      // 标签框落在**自己那条乐道**上，与它的小原因同一列（乐道是斜的，所以"同一列"
      // 指"到骨线的距离相同"，不是 x 相同）。不居中于骨尖——那会让框骑到外侧邻骨
      // 那条斜线上（骨尖横向偏左 `MID_OFFSET`，120 宽的框居中于它会跨过去）。
      positions.set(item.node.id, { x: labelRight.x - W / 2, y: labelRight.y });

      for (const child of item.children) {
        const hookX = boneX(midAnchorX, child.rise);
        const boxRight = hookX - STUB;
        positions.set(child.node.id, { x: boxRight - W / 2, y: side * child.rise });
        // 短线从**中骨上**那一点出发：这是"挂在骨上的原因"，与大骨上的原因同一条规则。
        links.set(`${item.node.id}->${child.node.id}`, [
          { x: hookX, y: side * child.rise },
          { x: boxRight, y: side * child.rise },
        ]);
        layoutDeeper(child.node, positions, boxRight - W / 2, side * child.rise);
      }
    }
  }

  const bounds = boundsOf(positions);
  return {
    positions,
    bounds,
    // 鱼骨的"朝向"是固定的（头在右、骨向左斜），不随 theme.direction 变：
    // 一个上下翻转的鱼骨图不再像鱼骨，所以这里只报 'right' 让连线句柄走左右两侧。
    direction: 'right',
    fishbone: {
      links,
      spine: { from: spineStart, to: { x: spineEnd, y: 0 } },
      spineArrow: headLink === null
        ? null
        : { link: headLink, points: arrowHead(spineStart) },
    },
  };
}

/**
 * 主骨末端的箭头：尖端压在鱼头的左沿，底边在左边，朝向 +x（指向结果）。
 *
 * 三个点写死成"尖 / 上后角 / 下后角"，所以方向与形状都不依赖任何既有线段的走向。
 */
function arrowHead(from: Point): Point[] {
  return [
    { x: from.x, y: from.y },
    { x: from.x - SPINE_ARROW_SIZE.length, y: from.y - SPINE_ARROW_SIZE.halfHeight },
    { x: from.x - SPINE_ARROW_SIZE.length, y: from.y + SPINE_ARROW_SIZE.halfHeight },
  ];
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

/** 包围盒：只看节点框（主骨、骨、短线与箭头一定落在某个节点框的左边，不会越界）。 */
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
