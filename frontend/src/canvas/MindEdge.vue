<script setup lang="ts">
import { BaseEdge, EdgeLabelRenderer, getSmoothStepPath, type EdgeProps } from '@vue-flow/core';
import { computed } from 'vue';

import type { EdgeKind } from '../api/types';

/**
 * 附加边（`MindMapContent.edges`）的画法。
 *
 * 为什么必须自定义：内置的 `smoothstep` 只会画一条灰线。而"树 + 边"这个折中里，
 * 边承担的是**树表达不了的那部分语义**——条件分支要走哪个出口、回退回到哪一步。
 * 那些信息如果画成和父子线一样，图上就只多出几条看不出方向的线，用户仍然读不出
 * "这是判断，那是返工"。
 *
 * 三种 `kind` 的画法，各自对应一种读图动作：
 *
 *  - `flow`（缺省）：实线 + 细箭头。跳过若干阶段的顺序跳转，与父子线同色但更浅
 *    ——它是**补充**，不该抢走主干（树）的注意力。
 *  - `branch`：靛蓝色实线 + 标签（"是"/"否"）。用户要顺着颜色找判断出口。
 *  - `loop`：橙色**虚线** + 箭头。虚线是回退的通用视觉语言（返工、重试、驳回），
 *    一眼就能与"往前走"的线分开。
 *
 * 标签用 `EdgeLabelRenderer` 画在 SVG 之前的 HTML 层，而不是 SVG `<text>`：文字要
 * 跟随缩放、要能换行、要能被读屏念出来，这三点 HTML 都比 `<text>` 省事。
 */
const props = defineProps<EdgeProps<{
  label?: string;
  kind?: EdgeKind;
  /** 原始的边 id（画布上的边 id 带了 `attach:` 前缀，删线要用原始那个）。 */
  edgeId?: string;
}>>();

/** 缺省按 `flow` 画：`kind` 为 `null` 的边就是普通顺序箭头。 */
const kind = computed<EdgeKind>(() => props.data?.kind ?? 'flow');

const STYLES: Record<EdgeKind, { stroke: string; dash?: string; width: number }> = {
  flow: { stroke: '#94a3b8', width: 1.5 },
  branch: { stroke: '#6366f1', width: 2 },
  loop: { stroke: '#f59e0b', width: 2, dash: '6 4' },
};

const style = computed(() => {
  const preset = STYLES[kind.value];
  return {
    stroke: preset.stroke,
    strokeWidth: preset.width,
    strokeDasharray: preset.dash,
  };
});

/**
 * 箭头 marker 的 id 必须**每条边唯一**。
 *
 * 共用同一个 id 时，第二条边会把 `<marker>` 定义覆盖成自己的颜色——于是两条不同
 * kind 的边共用一种箭头色，而这个错误在画布上看不出是哪一条错了。
 */
const markerId = computed(() => `edge-arrow-${props.id.replace(/[^\w-]/g, '_')}`);

/** `getSmoothStepPath` 的返回值：`[路径, 标签 x, 标签 y, ...]`。 */
const path = computed(() => getSmoothStepPath({
  sourceX: props.sourceX,
  sourceY: props.sourceY,
  targetX: props.targetX,
  targetY: props.targetY,
  sourcePosition: props.sourcePosition,
  targetPosition: props.targetPosition,
  borderRadius: 12,
}));

const labelText = computed(() => (props.data?.label ?? '').trim());

/** 标签底色跟着 kind 走：徽标落在线上时，颜色是"这条线是什么"的第二重信号。 */
const labelClass = computed(() => (
  kind.value === 'branch'
    ? 'border-indigo-300 bg-indigo-50 text-indigo-800'
    : kind.value === 'loop'
      ? 'border-amber-300 bg-amber-50 text-amber-800'
      : 'border-slate-200 bg-white text-slate-600'
));
</script>

<template>
  <defs>
    <marker
      :id="markerId"
      viewBox="0 0 10 10"
      refX="9"
      refY="5"
      markerWidth="6"
      markerHeight="6"
      orient="auto-start-reverse"
    >
      <path d="M 0 0 L 10 5 L 0 10 z" :fill="STYLES[kind].stroke" />
    </marker>
  </defs>
  <BaseEdge
    :path="path[0]"
    :marker-end="`url(#${markerId})`"
    :style="style"
  />
  <EdgeLabelRenderer v-if="labelText">
    <div
      class="nodrag nopan pointer-events-none absolute rounded border px-1.5 py-0.5 text-[11px] leading-none shadow-sm"
      :class="labelClass"
      :style="{
        transform: `translate(-50%, -50%) translate(${path[1]}px, ${path[2]}px)`,
      }"
    >
      {{ labelText }}
    </div>
  </EdgeLabelRenderer>
</template>
