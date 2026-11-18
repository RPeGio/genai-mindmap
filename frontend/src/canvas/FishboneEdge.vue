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
 *  - 大类 → 原因：一条只有十几像素的水平短线，起点**在斜骨上**（不是大类框的边），
 *    终点是原因框的右沿。
 *
 * 内置的 `smoothstep` / `straight` 只能连两个句柄，画不出"点在骨上"的短线，也画
 * 不出主骨那种多段折线。这些点全部由 `layout/fishbone.ts` 算好放进 `data.points`，
 * 这个组件只负责把点连成线——几何归布局、描边归渲染，两边都能单独验。
 *
 * 没有箭头：鱼骨的骨线表示"归类"，不表示流向；带箭头的鱼骨图会让读者以为原因在
 * 往结果上流动，而石川图恰恰相反——它是在往回追问"这件事由什么造成"。
 */
const props = defineProps<EdgeProps<{ points?: Point[] }>>();

const path = computed(() => {
  const points = props.data?.points ?? [];
  if (points.length < 2) return '';
  return points
    .map((point, index) => `${index === 0 ? 'M' : 'L'} ${point.x.toFixed(2)} ${point.y.toFixed(2)}`)
    .join(' ');
});
</script>

<template>
  <BaseEdge
    v-if="path"
    :path="path"
    :style="{ stroke: '#cbd5e1', strokeWidth: 1.5, strokeLinejoin: 'round' }"
  />
</template>
