<script setup lang="ts">
import { Handle, Position } from '@vue-flow/core';
import { computed, onBeforeUnmount, ref } from 'vue';

import type { MindNodeData } from '../stores/mindmap';

const props = defineProps<{
  id: string;
  data: MindNodeData;
  selected?: boolean;
}>();

/** 目标句柄（连线进入的一侧）由布局方向决定；源句柄在对面。 */
const targetPosition = computed(() => {
  if (props.data.side === 'top') return Position.Top;
  return props.data.side === 'right' ? Position.Right : Position.Left;
});

const sourcePosition = computed(() => {
  if (props.data.side === 'top') return Position.Bottom;
  return props.data.side === 'right' ? Position.Left : Position.Right;
});

/** 文字被截断时才有必要浮现卡片；没截断也照样允许悬停查看（标题本身可能就是全文）。 */
const truncated = computed(() => props.data.label !== props.data.fullLabel);

const card = ref(false);
let timer: ReturnType<typeof setTimeout> | undefined;

function scheduleCard() {
  clearTimeout(timer);
  // 200ms 延迟：鼠标扫过画布时不该到处弹卡片。
  timer = setTimeout(() => {
    card.value = true;
  }, 200);
}

function hideCard() {
  clearTimeout(timer);
  card.value = false;
}

onBeforeUnmount(() => clearTimeout(timer));
</script>

<template>
  <div
    class="relative"
    @mouseenter="scheduleCard"
    @mouseleave="hideCard"
    @focusin="scheduleCard"
    @focusout="hideCard"
  >
    <Handle id="in" type="target" :position="targetPosition" class="!h-1.5 !w-1.5 !border-0 !bg-slate-300" />
    <Handle id="out" type="source" :position="sourcePosition" class="!h-1.5 !w-1.5 !border-0 !bg-slate-300" />

    <div
      class="flex h-12 w-[180px] items-center justify-center rounded-md border px-3 text-center text-sm leading-tight"
      :class="
        selected
          ? 'border-slate-900 bg-white font-medium shadow-md'
          : data.depth === 0
            ? 'border-slate-900 bg-slate-900 font-medium text-white'
            : 'border-slate-300 bg-white text-slate-700 hover:border-slate-400'
      "
      :title="data.fullLabel"
      tabindex="0"
    >
      <span class="line-clamp-2 break-words">{{ data.label }}</span>
    </div>

    <div
      v-if="card"
      class="absolute bottom-full left-1/2 z-30 mb-2 w-72 -translate-x-1/2 rounded-md border border-slate-300 bg-white p-3 text-left shadow-lg"
    >
      <p class="max-h-56 overflow-y-auto whitespace-pre-wrap break-words text-sm text-slate-700">
        {{ data.body }}
      </p>
      <p v-if="truncated" class="mt-2 text-xs text-slate-400">
        节点上只显示前 24 个字，完整内容见此处或右侧详情面板
      </p>
    </div>
  </div>
</template>
