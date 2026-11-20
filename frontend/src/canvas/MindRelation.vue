<script setup lang="ts">
import { BaseEdge, EdgeLabelRenderer, getBezierPath, type EdgeProps } from '@vue-flow/core';
import { computed } from 'vue';

import type { RelationKind } from '../api/types';

/**
 * 跨节点关系线（`MindMapContent.relations`）的画法。
 *
 * 这是三种线里**唯一一种跨分支**的：父子线是布局的一部分（跟着 tidy 的句柄走），
 * 附加边是流程图的条件出口（贴着阶段走），而关系线连接的是两个可能隔着半张图的节点
 * ——它必须有自己的一套画法，否则读者分不清"这是层级"还是"这是依赖"。
 *
 * 三条造型决定，各自对应一种读图动作：
 *
 *  - **曲线（bezier）而不是折线**：关系线的两个端点没有共同的父节点，走折线会在
 *    中间的节点上拐出直角，看起来像"穿过了某个节点"。曲线从中间绕过去，压住节点的
 *    概率低得多。
 *  - **虚线**：实线在这个画布里已经有两个意思（父子线、`flow` 附加边）。虚线是
 *    "这一层语义与树无关"的唯一可读信号，也让密集的关系线不抢主干的注意力。
 *  - **半透明**：一条关系线往往是横穿全图的，不透明时它比任何节点都显眼——那是
 *    本末倒置。0.7 的不透明度让它"在"但不"抢"。
 *
 * `conflicts` 不画箭头：冲突是对称的（A 与 B 冲突 = B 与 A 冲突），画成有向线会让
 * 读者去猜"到底是哪个方向"。其余五种都有方向，箭头指向 `target`。
 */
const props = defineProps<EdgeProps<{
  kind?: RelationKind;
  /** 这条边成立的理由。悬停时显示——它就是"凭什么"那个问题的答案。 */
  why?: string;
  /** 原始的 relation id（画布上的 id 带了 `relation:` 前缀，删线要用原始那个）。 */
  relationId?: string;
}>>();

/** 缺省按 `references` 画：最弱的一种关系，不该因为字段缺失而显得重要。 */
const kind = computed<RelationKind>(() => props.data?.kind ?? 'references');

/**
 * 六种 kind → 颜色 + 中文短标签。
 *
 * 标签是**中文短词**而不是 kind 原文：画布上读图的人不需要知道字段名，他需要一眼看出
 * "这两个节点是依赖还是冲突"。字段名留在悬停提示里（`why`）。
 */
const STYLES: Record<RelationKind, { stroke: string; label: string; arrow: boolean }> = {
  depends_on: { stroke: '#0ea5e9', label: '依赖', arrow: true },
  causes: { stroke: '#ef4444', label: '因果', arrow: true },
  conflicts: { stroke: '#f43f5e', label: '冲突', arrow: false },
  part_of: { stroke: '#8b5cf6', label: '属于', arrow: true },
  refines: { stroke: '#14b8a6', label: '细化', arrow: true },
  references: { stroke: '#94a3b8', label: '引用', arrow: true },
};

const preset = computed(() => STYLES[kind.value]);

/**
 * 箭头 marker 的 id 必须**每条边唯一**（与 `MindEdge.vue` 同一条理由）：
 * 共用同一个 id 时，后一条边会把 `<marker>` 定义覆盖成自己的颜色。
 */
const markerId = computed(() => `rel-arrow-${props.id.replace(/[^\w-]/g, '_')}`);

/** `getBezierPath` 的返回值：`[路径, 标签 x, 标签 y, ...]`。 */
const path = computed(() => getBezierPath({
  sourceX: props.sourceX,
  sourceY: props.sourceY,
  targetX: props.targetX,
  targetY: props.targetY,
  sourcePosition: props.sourcePosition,
  targetPosition: props.targetPosition,
  curvature: 0.35,
}));

const labelText = computed(() => preset.value.label);
const hint = computed(() => (props.data?.why ?? '').trim());
</script>

<template>
  <defs>
    <marker
      :id="markerId"
      viewBox="0 0 10 10"
      refX="9"
      refY="5"
      markerWidth="5"
      markerHeight="5"
      orient="auto-start-reverse"
    >
      <path d="M 0 0 L 10 5 L 0 10 z" :fill="preset.stroke" />
    </marker>
  </defs>
  <BaseEdge
    :path="path[0]"
    :marker-end="preset.arrow ? `url(#${markerId})` : undefined"
    :style="{
      stroke: preset.stroke,
      strokeWidth: 1.25,
      strokeDasharray: '5 4',
      opacity: 0.7,
    }"
  />
  <!--
    `pointer-events-none`：标签只是读图时的注脚，点它不该抢走节点的事件。
    理由留给我们自己看的是 `why`——它放在 `title` 里，悬停可读，不占画布。
  -->
  <EdgeLabelRenderer>
    <div
      class="nodrag nopan pointer-events-none absolute rounded border bg-white/90 px-1 py-0.5 text-[10px] leading-none shadow-sm"
      :style="{
        transform: `translate(-50%, -50%) translate(${path[1]}px, ${path[2]}px)`,
        borderColor: preset.stroke,
        color: preset.stroke,
      }"
      :title="hint || undefined"
    >
      {{ labelText }}
    </div>
  </EdgeLabelRenderer>
</template>
