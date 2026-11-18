import { mount } from '@vue/test-utils';
import { describe, expect, it } from 'vitest';

import type { MindNode } from '../src/api/types';
import FishboneEdge from '../src/canvas/FishboneEdge.vue';
import {
  BONE_ANGLE_DEG,
  FISHBONE_SLOTS,
  SPINE_ARROW_SIZE,
  fishboneSlotOf,
  layoutFishbone,
} from '../src/canvas/layout/fishbone';
import { NODE_SIZE } from '../src/canvas/layout/tidy';

/**
 * 鱼骨图的几何单测。
 *
 * 布局是纯函数，所以这里断言的是**坐标与折线**，不挂组件、不碰画布：
 * 角度是不是 60°、六类的上下位置、原因短线是不是水平的、框有没有叠在一起，
 * 全都是可以直接算的事。凡是"看着挺像鱼骨"的说法，都必须能落到下面某一条断言上。
 */

/** 六类按生成侧的顺序给出（人机料法环测），每条挂若干原因。 */
function sixBoneTree(counts: Record<string, number> = {}): MindNode {
  const names = ['人', '机', '料', '法', '环', '测'];
  return {
    id: 'r',
    label: '交付延期',
    children: names.map(name => ({
      id: name,
      label: `${name}（类别）`,
      children: Array.from({ length: counts[name] ?? 1 }, (_, index) => ({
        id: `${name}${index}`,
        label: `${name}原因${index}`,
        children: [],
      })),
    })),
  };
}

function collect(root: MindNode, out: MindNode[] = []): MindNode[] {
  out.push(root);
  for (const child of root.children) collect(child, out);
  return out;
}

function count(root: MindNode): number {
  return collect(root).length;
}

/** 一个节点的框（左上/右下）。 */
function rect(result: ReturnType<typeof layoutFishbone>, id: string) {
  const point = result.positions.get(id);
  if (!point) throw new Error(`缺少节点 ${id}`);
  return {
    x1: point.x - NODE_SIZE.width / 2,
    x2: point.x + NODE_SIZE.width / 2,
    y1: point.y - NODE_SIZE.height / 2,
    y2: point.y + NODE_SIZE.height / 2,
  };
}

/** 任意两个节点框不重叠——与 `samples.spec.ts` 同一条断言，换到鱼骨上再钉一遍。 */
function assertNoOverlap(root: MindNode, result: ReturnType<typeof layoutFishbone>): void {
  const nodes = collect(root);
  for (let i = 0; i < nodes.length; i += 1) {
    for (let j = i + 1; j < nodes.length; j += 1) {
      const a = rect(result, nodes[i]!.id);
      const b = rect(result, nodes[j]!.id);
      const overlap = a.x1 < b.x2 && b.x1 < a.x2 && a.y1 < b.y2 && b.y1 < a.y2;
      expect(overlap, `${nodes[i]!.id} 与 ${nodes[j]!.id} 的节点框重叠`).toBe(false);
    }
  }
}

describe('鱼骨的六类位置映射', () => {
  it('人机料在上、法环测在下，且沿主骨交替', () => {
    const root = sixBoneTree();
    const result = layoutFishbone(root);
    // 画布 y 轴朝下：在上 = y 更小。
    const y = (id: string) => result.positions.get(id)!.y;
    for (const name of ['人', '机', '料']) expect(y(name), name).toBeLessThan(0);
    for (const name of ['法', '环', '测']) expect(y(name), name).toBeGreaterThan(0);

    // 交替：槽位 0/2/4 向上，1/3/5 向下。人=0 机=2 料=4，法=1 环=3 测=5。
    expect([FISHBONE_SLOTS.人, FISHBONE_SLOTS.机, FISHBONE_SLOTS.料]).toEqual([0, 2, 4]);
    expect([FISHBONE_SLOTS.法, FISHBONE_SLOTS.环, FISHBONE_SLOTS.测]).toEqual([1, 3, 5]);
  });

  it('槽位沿主骨从头部（右）向左递增', () => {
    const result = layoutFishbone(sixBoneTree());
    /** 骨在主骨上的锚点：槽位顺序的唯一可靠体现（大类框会被骨长推得更左，不能直接比）。 */
    const anchor = (id: string) => result.fishbone.links.get(`r->${id}`)![1]!.x;
    // 槽位 0..5 = 人 法 机 环 料 测，锚点递减
    expect(anchor('人')).toBeGreaterThan(anchor('法'));
    expect(anchor('法')).toBeGreaterThan(anchor('机'));
    expect(anchor('机')).toBeGreaterThan(anchor('环'));
    expect(anchor('环')).toBeGreaterThan(anchor('料'));
    expect(anchor('料')).toBeGreaterThan(anchor('测'));
  });

  it('label 不是六类时按顺序交替补空槽', () => {
    const root: MindNode = {
      id: 'r', label: '结果',
      children: [
        { id: 'a', label: '甲', children: [] },
        { id: 'b', label: '乙', children: [] },
        { id: 'c', label: '丙', children: [] },
      ],
    };
    const result = layoutFishbone(root);
    // 槽位 0/1/2 → 上 / 下 / 上
    expect(result.positions.get('a')!.y).toBeLessThan(0);
    expect(result.positions.get('b')!.y).toBeGreaterThan(0);
    expect(result.positions.get('c')!.y).toBeLessThan(0);
  });

  it('重复的大类名不会抢同一个槽：第二个落到空槽上', () => {
    const root: MindNode = {
      id: 'r', label: '结果',
      children: [
        { id: 'x', label: '人', children: [] },
        { id: 'y', label: '人', children: [] },
      ],
    };
    const result = layoutFishbone(root);
    // 一个是槽 0（向上），另一个被挤到槽 1（向下）
    expect(result.positions.get('x')!.y).not.toBe(result.positions.get('y')!.y);
    expect(result.positions.get('x')!.x).not.toBe(result.positions.get('y')!.x);
  });
});

describe('fishboneSlotOf：从 label 认类别', () => {
  it.each([
    ['人', '人'],
    ['人员', '人'],
    ['人：培训不足', '人'],
    ['机器设备', '机'],
    ['料（物料）', '料'],
    ['工艺方法', '法'],
    ['环境', '环'],
    ['测量', '测'],
  ])('%s → %s', (label, name) => {
    expect(fishboneSlotOf(label)).toBe(FISHBONE_SLOTS[name]);
  });

  it('两个字都出现时取位置靠前的那个', () => {
    // 「测量方法」讲的是"测"；「方法测量」讲的是"法"？
    expect(fishboneSlotOf('测量方法')).toBe(FISHBONE_SLOTS.测);
    expect(fishboneSlotOf('方法测量')).toBe(FISHBONE_SLOTS.法);
  });

  it('认不出返回 null', () => {
    expect(fishboneSlotOf('交付延期')).toBeNull();
    expect(fishboneSlotOf('')).toBeNull();
  });
});

describe('鱼骨的斜骨几何', () => {
  it('斜骨与主骨成 60°（水平位移 = 竖直位移 / tan60）', () => {
    const root = sixBoneTree();
    const result = layoutFishbone(root);
    expect(BONE_ANGLE_DEG).toBe(60);
    const tan = Math.tan((60 * Math.PI) / 180);

    for (const [id, points] of result.fishbone.links) {
      if (points.length !== 3) continue; // 只验主骨→大类那三点的骨线
      if (!id.startsWith('r->')) continue; // 中骨的骨线另有自己的起点（见下面那组）
      const anchor = points[1]!;
      const tip = points[2]!;
      const dx = Math.abs(tip.x - anchor.x);
      const dy = Math.abs(tip.y - anchor.y);
      expect(dy, id).toBeGreaterThan(0);
      expect(dx / dy).toBeCloseTo(1 / tan, 9);
      // 锚点必须落在主骨上（y = 0）——不在主骨上的"锚点"等于骨是从虚空里长出来的。
      expect(anchor.y, id).toBe(0);
    }
  });

  it('主骨从头部左沿出发、一路上没有节点压在它上面', () => {
    const root = sixBoneTree();
    const result = layoutFishbone(root);
    const spine = result.fishbone.spine;
    expect(spine.from).toEqual({ x: -NODE_SIZE.width / 2, y: 0 });
    expect(spine.to.y).toBe(0);
    // 主骨向左延伸：末端在头部左边
    expect(spine.to.x).toBeLessThan(spine.from.x);
    // 六根骨的锚点都在主骨上，且总长度的末端就是最远那根骨的锚点
    const anchors = [...result.fishbone.links.values()]
      .filter(points => points.length === 3)
      .map(points => points[1]!.x);
    expect(spine.to.x).toBeCloseTo(Math.min(...anchors), 6);
  });

  it('原因用一条水平短线接在斜骨上', () => {
    const root = sixBoneTree({ 机: 3 });
    const result = layoutFishbone(root);
    for (const index of [0, 1, 2]) {
      const link = result.fishbone.links.get(`机->机${index}`);
      expect(link, `机${index}`).toBeDefined();
      const [hook, end] = link!;
      // 短线是水平的：两端 y 相同
      expect(hook!.y).toBe(end!.y);
      // 从斜骨往左接：终点在挂点左侧
      expect(end!.x).toBeLessThan(hook!.x);
      // 端的右沿就是原因框的左...（框的中心再往左半个宽）
      const box = rect(result, `机${index}`);
      expect(end!.x).toBeCloseTo(box.x2, 6);
    }
  });

  it('原因沿斜骨向外排：越靠后的原因离主骨越远、越靠左', () => {
    const root = sixBoneTree({ 人: 4 });
    const result = layoutFishbone(root);
    const rises = [0, 1, 2, 3].map(index => result.positions.get(`人${index}`)!);
    for (let i = 1; i < rises.length; i += 1) {
      expect(Math.abs(rises[i]!.y)).toBeGreaterThan(Math.abs(rises[i - 1]!.y));
      expect(rises[i]!.x).toBeLessThan(rises[i - 1]!.x);
    }
  });

  it('同一侧的骨长取齐：短骨的大类框不会压进邻骨的原因列', () => {
    // 环 0 个原因紧挨着法 2 个原因——这正是"各长各的"会重叠的那种组合。
    const root = sixBoneTree({ 人: 1, 机: 1, 料: 1, 法: 2, 环: 0, 测: 3 });
    const result = layoutFishbone(root);
    assertNoOverlap(root, result);
    // 同侧（向下那侧）的三根骨：大类框落在同一条水平线上
    const downY = ['法', '环', '测'].map(name => result.positions.get(name)!.y);
    expect(new Set(downY.map(y => Math.round(y * 1000))).size).toBe(1);
  });

  it('原因超过 6 个就换紧凑间距：骨变长，但框不叠', () => {
    const six = layoutFishbone(sixBoneTree({ 人: 6 }));
    const twelveTree = sixBoneTree({ 人: 12 });
    const twelve = layoutFishbone(twelveTree);

    // 6 个原因用舒适间距 64：第一个原因离主骨 48 + 32 = 80，第六个 400。
    expect(Math.abs(six.positions.get('人0')!.y)).toBeCloseTo(80, 6);
    expect(Math.abs(six.positions.get('人5')!.y)).toBeCloseTo(400, 6);

    // 12 个原因换 56 的紧凑间距：间距变小、链更长——不是换列，所以仍然是一列。
    const gaps = [0, 1, 2].map(index =>
      Math.abs(twelve.positions.get(`人${index + 1}`)!.y) - Math.abs(twelve.positions.get(`人${index}`)!.y));
    for (const gap of gaps) expect(gap).toBeCloseTo(56, 6);

    // 紧凑间距仍严格大于框高：这是"不重叠"的充分条件（见 fishbone.ts 文件头）。
    expect(56).toBeGreaterThan(NODE_SIZE.height);
    assertNoOverlap(twelveTree, twelve);

    // 骨长随原因数增长（12 个原因比 6 个长），大类框跟着往上走
    const sixTip = six.fishbone.links.get('r->人')![2]!;
    const twelveTip = twelve.fishbone.links.get('r->人')![2]!;
    expect(Math.abs(twelveTip.y)).toBeGreaterThan(Math.abs(sixTip.y));
    expect(twelveTip.x).toBeLessThan(sixTip.x);
  });
});

describe('鱼骨图的完整性', () => {
  it('每棵子树里的节点都有坐标（三层以上也不丢）', () => {
    const root: MindNode = {
      id: 'r', label: '结果',
      children: [{
        id: '人', label: '人',
        children: [{
          id: 'c1', label: '培训不足',
          children: [{ id: 'g1', label: '新人多', children: [] }],
        }],
      }],
    };
    const result = layoutFishbone(root);
    expect(result.positions.size).toBe(count(root));
    const point = (id: string) => result.positions.get(id)!;
    // `c1` 带子节点 → 它是一根中骨：标签落在自己那条乐道的**最外**一格，`g1` 用水平
    // 短线挂在它身上。乐道是 60° 斜的，所以"往外" = 更高、更左——两个方向一起读才是
    // "沿骨向外"，只看 x 会得出反的结论（改动前一律往左排一列，那里的判据才是 x）。
    expect(Math.abs(point('g1').y)).toBeLessThan(Math.abs(point('c1').y));
    expect(point('g1').x).toBeGreaterThan(point('c1').x);
  });

  it('根节点在原点，包围盒包住所有节点框', () => {
    const root = sixBoneTree({ 人: 3, 测: 4 });
    const result = layoutFishbone(root);
    expect(result.positions.get('r')).toEqual({ x: 0, y: 0 });
    for (const node of collect(root)) {
      const box = rect(result, node.id);
      expect(box.x1).toBeGreaterThanOrEqual(result.bounds.minX - 1e-9);
      expect(box.x2).toBeLessThanOrEqual(result.bounds.maxX + 1e-9);
      expect(box.y1).toBeGreaterThanOrEqual(result.bounds.minY - 1e-9);
      expect(box.y2).toBeLessThanOrEqual(result.bounds.maxY + 1e-9);
    }
  });

  it('朝向恒为 right（骨线是左右走的，不随 theme.direction 变）', () => {
    expect(layoutFishbone(sixBoneTree()).direction).toBe('right');
  });

  it('只有一个根节点时不炸，也没有骨线', () => {
    const result = layoutFishbone({ id: 'r', label: '结果', children: [] });
    expect(result.positions.size).toBe(1);
    expect(result.fishbone.links.size).toBe(0);
    expect(result.bounds.width).toBe(NODE_SIZE.width);
    expect(result.bounds.height).toBe(NODE_SIZE.height);
    // 没有骨就没有方向：箭头是"主骨指向结果"这件事的记号，主骨不存在时它无从谈起
    expect(result.fishbone.spineArrow).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 两层鱼骨：改动前什么样，现在还得什么样
// ---------------------------------------------------------------------------

/**
 * 当前的六成鱼骨都是两层（骨 → 原因），中骨那套几何**只能**在真的有第三层时才
 * 出场。这一组把改动前那套数字逐项写死：坐标、骨长、折线。
 *
 * 写死而不是"用公式再算一遍"：公式在这条用例里是**被检验对象**，拿它自己验自己
 * 什么也证明不了。
 */
describe('两层鱼骨与改动前逐像素相同', () => {
  const golden = () => layoutFishbone(sixBoneTree());

  it('节点坐标：锚点 -138、第一格 80、骨长 124、大类框 -148', () => {
    const result = golden();
    // 主骨头在原点，第一根骨（人，槽位 0）的锚点 = -90 - 48
    expect(result.positions.get('r')).toEqual({ x: 0, y: 0 });
    // 原因框中心：锚点左移 80/tan60，再退一个短线 16 与半个框宽 90
    expect(result.positions.get('人0')!.y).toBeCloseTo(-80, 9);
    expect(result.positions.get('人0')!.x).toBeCloseTo(-90 - 48 - 80 / Math.tan(Math.PI / 3) - 16 - 90, 9);
    // 一个原因 → 骨长 124（80 + 24 + 20），大类框再往外半个框
    expect(result.positions.get('人')!.y).toBeCloseTo(-148, 9);
    expect(result.positions.get('人')!.x).toBeCloseTo(-138 - 124 / Math.tan(Math.PI / 3), 9);
    // 法在槽位 1：锚点再往左 132，方向朝下
    expect(result.positions.get('法0')!.y).toBeCloseTo(80, 9);
    expect(result.positions.get('法')!.y).toBeCloseTo(148, 9);
  });

  it('折线：三点骨线 + 两点水平短线，主骨从 -90 走到最远的锚点', () => {
    const result = golden();
    expect(result.fishbone.links.get('r->人')).toEqual([
      { x: -90, y: 0 },
      { x: -138, y: 0 },
      { x: -138 - 124 / Math.tan(Math.PI / 3), y: -124 },
    ]);
    const stub = result.fishbone.links.get('人->人0')!;
    expect(stub[0]).toEqual({ x: -138 - 80 / Math.tan(Math.PI / 3), y: -80 });
    expect(stub[1]).toEqual({ x: -138 - 80 / Math.tan(Math.PI / 3) - 16, y: -80 });
    // 主骨末端就是最远那根骨（槽位 5）的锚点
    expect(result.fishbone.spine.to).toEqual({ x: -90 - 48 - 5 * 132, y: 0 });
  });

  it('同侧骨长仍然取齐，12 个原因仍然换紧凑档 56', () => {
    const twelve = layoutFishbone(sixBoneTree({ 人: 12 }));
    const gaps = [0, 1, 2].map(index =>
      Math.abs(twelve.positions.get(`人${index + 1}`)!.y) - Math.abs(twelve.positions.get(`人${index}`)!.y));
    for (const gap of gaps) expect(gap).toBeCloseTo(56, 9);
    // 紧凑档的"对半分余量"：48 + (56-48)/2 + 24 = 76
    expect(Math.abs(twelve.positions.get('人0')!.y)).toBeCloseTo(76, 9);
  });
});

// ---------------------------------------------------------------------------
// 中骨：骨上的骨
// ---------------------------------------------------------------------------

/**
 * 三层深（骨 → 原因 → 原因的原因）。上下两侧各留一根中骨，且中骨与叶子原因做
 * 兄弟——混合才是最难的排布，"全是中骨"的图反而好排。
 */
function threeLevelTree(): MindNode {
  return {
    id: 'r',
    label: '交付延期',
    children: [
      {
        id: '人', label: '人（人员）',
        children: [
          {
            id: '培训不足', label: '培训不足',
            children: [
              { id: '新人多', label: '新人多', children: [] },
              { id: '老人走了', label: '老人走了', children: [] },
            ],
          },
          { id: '激励不足', label: '激励不足', children: [] },
        ],
      },
      {
        id: '法', label: '法（方法）',
        children: [
          {
            id: '流程不清', label: '流程不清',
            children: [{ id: '没写下来', label: '没写下来', children: [] }],
          },
          { id: '无审批', label: '无审批', children: [] },
        ],
      },
    ],
  };
}

/**
 * 一根骨那条**斜线**在给定 rise 上的横坐标。
 *
 * 由它自己的三点折线（主骨起点 → 锚点 → 骨尖）里的后两点反推，不借布局的公式：
 * 公式在这种用例里是被检验对象。
 */
function boneLineAt(result: ReturnType<typeof layoutFishbone>, id: string, rise: number): number {
  const [, anchor, tip] = result.fishbone.links.get(`r->${id}`)!;
  const ratio = (rise - Math.abs(anchor!.y)) / (Math.abs(tip!.y) - Math.abs(anchor!.y));
  return anchor!.x + (tip!.x - anchor!.x) * ratio;
}

describe('中骨：骨上的骨', () => {
  it('中骨沿大骨斜出：一截横向连接短线 + 一段与大骨同斜率的骨身 + 挂标签的短线', () => {
    const result = layoutFishbone(threeLevelTree());
    const points = result.fishbone.links.get('人->培训不足')!;
    expect(points).toHaveLength(4);

    const [hook, midStart, midTip, labelRight] = points;
    // 挂点在大骨那条线上（同一个 rise 的横坐标）
    expect(hook!.x).toBeCloseTo(boneLineAt(result, '人', Math.abs(hook!.y)), 9);
    // 第一段是水平的：连接短线两边 y 相同、x 差一个偏移
    expect(midStart!.y).toBe(hook!.y);
    expect(hook!.x - midStart!.x).toBeGreaterThan(0);
    expect(hook!.x - midStart!.x).toBeLessThanOrEqual(56); // 同侧骨距里唯一一份余量（见 fishbone.ts 文件头）
    // 第二段与大骨**同斜率**：60°，只是整体平移了
    const midSlope = (midTip!.x - midStart!.x) / (midTip!.y - midStart!.y);
    const bigSlope = (result.fishbone.links.get('r->人')![2]!.x - result.fishbone.links.get('r->人')![1]!.x)
      / (result.fishbone.links.get('r->人')![2]!.y - result.fishbone.links.get('r->人')![1]!.y);
    expect(midSlope).toBeCloseTo(bigSlope, 9);
    expect(Math.abs(midSlope)).toBeCloseTo(1 / Math.tan(Math.PI / 3), 9);
    // 骨身确实是往外长的：越往上越左
    expect(Math.abs(midTip!.y)).toBeGreaterThan(Math.abs(midStart!.y));
    expect(midTip!.x).toBeLessThan(midStart!.x);
    // 末段是接标签框的水平短线（与大骨上那些"小横杠"同一个长度、同一个方向）
    expect(labelRight!.y).toBe(midTip!.y);
    expect(midTip!.x - labelRight!.x).toBeCloseTo(16, 9);
    expect(labelRight!.x).toBeCloseTo(rect(result, '培训不足').x2, 9);
  });

  it('小原因挂在**中骨**上：短线起点正好落在中骨那条线上', () => {
    const result = layoutFishbone(threeLevelTree());
    const [, midStart, midTip] = result.fishbone.links.get('人->培训不足')!;
    for (const id of ['新人多', '老人走了']) {
      const link = result.fishbone.links.get(`培训不足->${id}`)!;
      const [hook, end] = link;
      const rise = Math.abs(hook!.y);
      // 由中骨自己的两个端点线性插值出"这个高度上骨在哪"——起点必须就是那一点
      const ratio = (rise - Math.abs(midStart!.y)) / (Math.abs(midTip!.y) - Math.abs(midStart!.y));
      expect(hook!.x, id).toBeCloseTo(midStart!.x + (midTip!.x - midStart!.x) * ratio, 9);
      expect(hook!.y).toBe(-rise); // 人这根骨在上半侧
      // 短线水平，终点是原因框的右沿
      expect(end!.y).toBe(hook!.y);
      expect(end!.x).toBeLessThan(hook!.x);
      expect(end!.x).toBeCloseTo(rect(result, id).x2, 9);
    }
  });

  it('中骨的标签落在自己那条乐道的最外一格，不与自己的小原因挤在一起', () => {
    const result = layoutFishbone(threeLevelTree());
    const labelId = '培训不足';
    const kidIds = ['新人多', '老人走了'];
    const midStart = result.fishbone.links.get('人->培训不足')![1]!;
    const midTip = result.fishbone.links.get('人->培训不足')![2]!;
    const midLineAt = (rise: number) => midStart.x
      + (midTip.x - midStart.x) * ((rise - Math.abs(midStart.y)) / (Math.abs(midTip.y) - Math.abs(midStart.y)));

    // 乐道是斜的（60°）：同一列的框在**自己的高度上**各有一个横坐标。判据不是"x 相同"，
    // 而是"框的右沿恒在骨线左边一个短线长"——中骨的标签与它的小原因共用这一条。
    for (const id of [labelId, ...kidIds]) {
      const point = result.positions.get(id)!;
      expect(rect(result, id).x2, id).toBeCloseTo(midLineAt(Math.abs(point.y)) - 16, 9);
    }
    // 标签在三个框里最外，且与最近的小原因隔着一个框高以上
    const label = result.positions.get(labelId)!;
    for (const id of kidIds) {
      expect(Math.abs(label.y)).toBeGreaterThan(Math.abs(result.positions.get(id)!.y) + NODE_SIZE.height);
    }
    // 中骨的骨尖就落在标签框的**中心线**上（末段那截短线接到框的右沿）
    expect(Math.abs(midTip.y)).toBeCloseTo(Math.abs(label.y), 9);
    // 标签框整体比骨尖靠左：不骑到外侧邻骨那条斜线上（见下一条）
    expect(rect(result, labelId).x2).toBeLessThan(midTip.x);
  });

  it('中骨那条乐道留得住 12px 净空：不压到同侧外侧邻骨的那条斜线', () => {
    // 六根骨全都长中骨——最挤的形态，邻骨就在旁边。
    const root: MindNode = {
      id: 'r', label: '结果',
      children: ['人', '机', '料', '法', '环', '测'].map(name => ({
        id: name,
        label: name,
        children: [
          {
            id: `${name}中骨`,
            label: `${name}中骨`,
            children: [
              { id: `${name}小1`, label: `${name}小1`, children: [] },
              { id: `${name}小2`, label: `${name}小2`, children: [] },
            ],
          },
          { id: `${name}叶`, label: `${name}叶`, children: [] },
        ],
      })),
    };
    const result = layoutFishbone(root);
    assertNoOverlap(root, result);

    // 槽位 0/2/4 与 1/3/5 各自一侧；同一侧相邻骨的锚点相距 2 * 132 = 264。
    const SLOT_SPACING = 264;
    for (const name of ['人', '机']) {
      for (const id of [`${name}小1`, `${name}小2`, `${name}中骨`]) {
        const box = rect(result, id);
        const rise = Math.abs(result.positions.get(id)!.y);
        const neighbourLine = boneLineAt(result, name, rise) - SLOT_SPACING;
        // 中骨那条乐道的左沿在大骨线左边 252（偏移 56 + 短线 16 + 框宽 180），
        // 外侧邻骨的斜线在大骨线左边 264 —— 两者之间正好 12。
        expect(box.x1 - neighbourLine, `${id} 与外侧邻骨的斜线`).toBeGreaterThanOrEqual(12 - 1e-9);
      }
    }
  });

  it('文字不会被裁：中骨的框与原因框同尺寸，一个字符都不少地待在框内', () => {
    // 布局层能保证的是"框还是那个 180×48"，以及乐道宽度够——换行与截断由节点组件负责。
    expect(NODE_SIZE).toEqual({ width: 180, height: 48 });
    const result = layoutFishbone(threeLevelTree());
    for (const id of ['培训不足', '新人多', '老人走了', '流程不清', '没写下来']) {
      const box = rect(result, id);
      expect(box.x2 - box.x1).toBeCloseTo(NODE_SIZE.width, 9);
      expect(box.y2 - box.y1).toBeCloseTo(NODE_SIZE.height, 9);
    }
  });

  it('第四层及更深仍然不丢节点', () => {
    const root: MindNode = {
      id: 'r', label: '结果',
      children: [{
        id: '人', label: '人',
        children: [{
          id: 'm', label: '中骨',
          children: [{
            id: 's', label: '小原因',
            children: [{ id: 'deep', label: '再下层的', children: [] }],
          }],
        }],
      }],
    };
    const result = layoutFishbone(root);
    expect(result.positions.size).toBe(count(root));
    // 兜底仍然是"往左排一列"：在父节点左侧一个框宽 + 24
    expect(result.positions.get('deep')!.x)
      .toBeCloseTo(result.positions.get('s')!.x - NODE_SIZE.width - 24, 9);
  });
});

// ---------------------------------------------------------------------------
// 主骨末端的箭头
// ---------------------------------------------------------------------------

describe('主骨末端的箭头', () => {
  it('尖端压在鱼头左沿、朝向 +x，交给锚点最靠右的那条骨线画', () => {
    const result = layoutFishbone(sixBoneTree());
    const arrow = result.fishbone.spineArrow!;
    expect(arrow.link).toBe('r->人'); // 槽位 0 = 离鱼头最近
    expect(arrow.points).toEqual([
      { x: -NODE_SIZE.width / 2, y: 0 },
      { x: -NODE_SIZE.width / 2 - 12, y: -5 },
      { x: -NODE_SIZE.width / 2 - 12, y: 5 },
    ]);
    // 尖端与主骨起点重合 = 箭头正好收在主骨指向鱼头的那一端
    expect(arrow.points[0]).toEqual(result.fishbone.spine.from);
    // 所有点在底边的右侧（朝向 +x，不可能反过来）
    expect(arrow.points[1]!.x).toBeLessThan(arrow.points[0]!.x);
    expect(arrow.points[2]!.x).toBeLessThan(arrow.points[0]!.x);
  });

  it('换了谁离鱼头最近，箭头就跟着换那条骨线', () => {
    const root: MindNode = {
      id: 'r', label: '结果',
      children: [{
        id: '法', label: '法',
        children: [{ id: 'x', label: 'x', children: [] }],
      }],
    };
    expect(layoutFishbone(root).fishbone.spineArrow!.link).toBe('r->法');
  });

  it('箭头尺寸是布局坐标里的常量，与描边粗细无关', () => {
    expect(SPINE_ARROW_SIZE).toEqual({ length: 12, halfHeight: 5 });
    const arrow = layoutFishbone(sixBoneTree()).fishbone.spineArrow!;
    const [tip, top, bottom] = arrow.points;
    expect(tip!.x - top!.x).toBe(SPINE_ARROW_SIZE.length);
    expect(bottom!.y - top!.y).toBe(SPINE_ARROW_SIZE.halfHeight * 2);
  });
});

// ---------------------------------------------------------------------------
// 渲染：折线怎么变成 SVG path
// ---------------------------------------------------------------------------

/** `BaseEdge` 在真实画布里负责把 path 画进 SVG，这里只需要一个能读出 `d` 的 `<path>`。 */
const stubs = {
  BaseEdge: {
    props: ['path', 'markerEnd', 'style'],
    template: '<path class="vue-flow__edge-path" :d="path" :marker-end="markerEnd" :style="style" />',
  },
};

function mountBone(
  points: Array<{ x: number; y: number }>,
  arrow?: Array<{ x: number; y: number }>,
) {
  return mount(FishboneEdge, {
    props: {
      id: 'r->人',
      source: 'r',
      target: '人',
      sourceX: 0,
      sourceY: 0,
      targetX: 0,
      targetY: 0,
      sourcePosition: 'left',
      targetPosition: 'right',
      data: arrow ? { points, arrow } : { points },
    } as never,
    global: { stubs },
  });
}

describe('鱼骨连线的渲染', () => {
  it('把布局算好的折线原样画成一条 path（不重排、不取整到整数）', () => {
    const path = mountBone([
      { x: 0, y: 0 },
      { x: -48, y: 0 },
      { x: -100, y: -90.5 },
    ]).find('path.vue-flow__edge-path');
    expect(path.attributes('d')).toBe('M 0.00 0.00 L -48.00 0.00 L -100.00 -90.50');
    // 骨线是中性的浅灰：它是结构，不该比节点边框更显眼
    expect(path.attributes('style')).toContain('rgb(203, 213, 225)');
    // 骨线自己不带 marker：鱼骨表达归类，不表达流向（唯一的箭头在主骨末端，见下一条）
    expect(path.attributes('marker-end')).toBeUndefined();
  });

  it('点数不足两个时不画（空 path 会让 SVG 记一条报错）', () => {
    expect(mountBone([]).find('path').exists()).toBe(false);
    expect(mountBone([{ x: 1, y: 2 }]).find('path').exists()).toBe(false);
  });

  it('真实布局产出的折线可以直接喂给它', () => {
    const root = sixBoneTree({ 人: 2 });
    const result = layoutFishbone(root);
    const points = result.fishbone.links.get('r->人')!;
    const d = mountBone(points).find('path').attributes('d')!;
    // 三点：主骨起点、锚点、斜骨末端
    expect(d.match(/L/g)).toHaveLength(2);
    expect(d.startsWith(`M ${points[0]!.x.toFixed(2)} ${points[0]!.y.toFixed(2)}`)).toBe(true);
  });
});
