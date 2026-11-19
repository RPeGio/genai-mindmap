<script setup lang="ts">
import { BaseEdge, type EdgeProps } from '@vue-flow/core';
import { computed } from 'vue';

import type { Point } from './layout/tidy';

/**
 * 鱼骨图的骨线。
 *
 * 为什么必须是自定义边：鱼骨的连线**不是节点之间的直线**。
 *
 *  - 主骨 → 大类：一条水平段（主骨）+ 一条 60° 斜段（骨），起点是头部左沿、拐点是
 *    主骨上的锚点；
 *  - 大类 → 中骨：一截横向连接短线 + 一条与大骨同斜率的斜段（骨上的骨）；
 *  - 骨 → 原因：一条只有十几像素的水平短线，起点**在骨上**（不是父节点的框边），
 *    终点是原因框的右沿。
 *
 * 内置的 `smoothstep` / `straight` 只能连两个句柄，画不出"点在骨上"的短线，也画
 * 不出主骨那种多段折线。这些点全部由 `layout/fishbone.ts` 算好放进 `data.points`，
 * 这个组件只负责把点连成线——几何归布局、描边归渲染，两边都能单独验。
 *
 * 骨线本身仍然**没有箭头**：它表示"归类"，不表示流向；带箭头的骨线会让读者以为
 * 原因在往结果上流动，而石川图恰恰相反——它是在往回追问"这件事由什么造成"。
 * 唯一的箭头在主骨末端（`data.arrow`），它表示的是"整条主骨都指向这个结果"，
 * 与"某个原因流向某处"是两件事。
 */
const props = defineProps<EdgeProps<{
  points?: Point[];
  /**
   * 主骨末端的箭头：三角形的三个点（尖端压在鱼头左沿）。
   *
   * 只有**一条**骨线会拿到它（锚点最靠右的那条，见 `layout/fishbone.ts`）——主骨
   * 那一小段被六条骨线各画了一遍，六份箭头在 DOM 里是六份，在画面上是一份。
   */
  arrow?: Point[];
}>>();

const path = computed(() => {
  const points = props.data?.points ?? [];
  if (points.length < 2) return '';
  return points
    .map((point, index) => `${index === 0 ? 'M' : 'L'} ${point.x.toFixed(2)} ${point.y.toFixed(2)}`)
    .join(' ');
});

/**
 * 箭头画成**显式的三角形**，不是 `<marker>`。
 *
 * 三条理由，都指向"这块几何必须是它自己的"：
 *
 *  1. 边组件本来就渲染在 Vue Flow 那块 pane 的 `<svg>` 里，坐标就是布局坐标。缩放
 *     与平移由 pane 每一帧施加的那一个 transform 统一负责——箭头和骨线、节点框共享
 *     同一个变换，不存在"第二个变换要与它同步"这件事；
 *  2. `<marker>` 的尺寸由 `markerUnits` 决定（缺省 `strokeWidth`：跟着描边宽度走）。
 *     哪天有人把骨线从 1.5 改粗，箭头会跟着一起胖；显式三角形的尺寸只由布局给的
 *     三个点决定，与描边无关；
 *  3. `marker-end` 的方向取自路径**最后一段**，而骨线最后一段恰好是 60° 的斜段——
 *     箭头会横着插在骨尖上。这里的朝向就是三角形自己的朝向，写死的 +x。
 */
const arrowPoints = computed(() => (
  (props.data?.arrow ?? []).map(point => `${point.x.toFixed(2)},${point.y.toFixed(2)}`).join(' ')
));
</script>

<template>
  <BaseEdge
    v-if="path"
    :path="path"
    :style="{ stroke: '#cbd5e1', strokeWidth: 1.5, strokeLinejoin: 'round' }"
  />
  <!--
    与骨线同一个灰度：主骨是结构，不该因为多了一个箭头就比节点边框更显眼。
    实心填充负责"看得见"——同样颜色的 1.5px 细线末端收成一个 12×10 的实心三角，
    重量差足够读出来，不必换色。
  -->
  <polygon
    v-if="arrowPoints"
    class="fishbone-spine-arrow"
    :points="arrowPoints"
    fill="#cbd5e1"
  />
</template>
