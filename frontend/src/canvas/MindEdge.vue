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
  /**
   * 布局判出来的**回退**（目标层号 ≤ 源层号，只有流程图会给）。
   *
   * 与 `kind` 分工：`kind` 是数据里的语义（是/否/回退），`back` 是这张图**排完版
   * 之后**的几何事实。两者会不一致——一条 `flow`（普通顺序跳转）的边在分层之后
   * 完全可能指向上一层，那时它在读图人的眼里就是回退，得画成虚线。
   */
  back?: boolean;
  /**
   * 这条边在**阶段内部**（`FlowStage.edges`：判定节点的「是 / 否」出边、步骤之间的
   * 顺序跳转），而不是阶段之间的主轴跳转。
   *
   * 两者必须分得开：阶段间是这张图的**主轴**（提交 → 审查 → 付款），阶段内是某一
   * 步上的**分叉**。画成同一个粗细时，一条「否」在画面上与"下一阶段"没有任何区别，
   * 读图的人会顺着它把流程读错。所以阶段内一律细一档、淡一档，`kind` 的颜色仍然
   * 保留（「是 / 否」靠颜色分辨这件事不能被削弱）。
   */
  inner?: boolean;
}>>();

/** 缺省按 `flow` 画：`kind` 为 `null` 的边就是普通顺序箭头。 */
const kind = computed<EdgeKind>(() => props.data?.kind ?? 'flow');

const STYLES: Record<EdgeKind, { stroke: string; dash?: string; width: number }> = {
  flow: { stroke: '#94a3b8', width: 1.5 },
  branch: { stroke: '#6366f1', width: 2 },
  loop: { stroke: '#f59e0b', width: 2, dash: '6 4' },
};

/**
 * 阶段内边的画法：**细一档**，且 `flow` 再淡一档。
 *
 * 只改粗细不该有争议；`flow` 那条额外换色是有原因的——阶段内的顺序跳转没有语义
 * 颜色可用，若与阶段间的 `flow` 同为 `#94a3b8`，两条 1px/1.5px 的灰线在缩放之后
 * 几乎分不开。`branch` / `loop` 保持原色：它们靠颜色说明"是 / 否 / 退回"，那层信息
 * 不能因为"这是细线"而丢掉。
 */
const INNER_STROKE = '#cbd5e1';

const style = computed(() => {
  const preset = STYLES[kind.value];
  const inner = props.data?.inner === true;
  return {
    stroke: inner && kind.value === 'flow' ? INNER_STROKE : preset.stroke,
    strokeWidth: inner ? Math.max(1, preset.width - 0.75) : preset.width,
    // 回退线一律虚线；`loop` 本来就是虚线，不必再叠一层短划。
    strokeDasharray: preset.dash ?? (props.data?.back ? '4 3' : undefined),
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

/**
 * 标签底色跟着 kind 走：徽标落在线上时，颜色是"这条线是什么"的第二重信号。
 */
const labelClass = computed(() => (
  kind.value === 'branch'
    ? 'border-indigo-300 bg-indigo-50 text-indigo-800'
    : kind.value === 'loop'
      ? 'border-amber-300 bg-amber-50 text-amber-800'
      : 'border-slate-200 bg-white text-slate-600'
));

/** 阶段内的标签比主轴的再小一号：细线配大徽标会把那条线压得看不见。 */
const labelSize = computed(() => (props.data?.inner === true ? 'text-[10px]' : 'text-[11px]'));
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
      class="nodrag nopan pointer-events-none absolute rounded border px-1.5 py-0.5 leading-none shadow-sm"
      :class="[labelClass, labelSize]"
      :style="{
        transform: `translate(-50%, -50%) translate(${path[1]}px, ${path[2]}px)`,
      }"
    >
      {{ labelText }}
    </div>
  </EdgeLabelRenderer>
</template>
