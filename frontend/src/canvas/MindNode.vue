<script setup lang="ts">
import { Handle, Position } from '@vue-flow/core';
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue';

import type { ChildSide } from '../canvas/layout/tidy';
import type { MindNodeData } from '../stores/mindmap';

const props = defineProps<{
  id: string;
  data: MindNodeData;
  selected?: boolean;
  editing?: boolean;
}>();

const emit = defineEmits<{
  commit: [label: string];
  cancel: [];
}>();

const POSITIONS: Record<ChildSide, Position> = {
  left: Position.Left,
  right: Position.Right,
  top: Position.Top,
  bottom: Position.Bottom,
};

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

const draft = ref('');
const input = ref<HTMLInputElement | null>(null);
/** 防止"回车提交"之后紧跟的 blur 再提交一次（第二次会带着旧值覆盖结果）。 */
let settled = false;

watch(
  () => props.editing,
  async editing => {
    if (!editing) {
      settled = false;
      return;
    }
    draft.value = props.data.fullLabel;
    settled = false;
    await nextTick();
    input.value?.focus();
    input.value?.select();
  },
  { immediate: true },
);

function finish(commit: boolean) {
  if (settled) return;
  settled = true;
  if (commit) emit('commit', draft.value);
  else emit('cancel');
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
    <Handle
      v-for="side in data.in"
      :id="`in-${side}`"
      :key="`in-${side}`"
      type="target"
      :position="POSITIONS[side]"
      class="!h-1.5 !w-1.5 !border-0 !bg-slate-300"
    />
    <Handle
      v-for="side in data.out"
      :id="`out-${side}`"
      :key="`out-${side}`"
      type="source"
      :position="POSITIONS[side]"
      class="!h-1.5 !w-1.5 !border-0 !bg-slate-300"
    />

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
      <input
        v-if="editing"
        ref="input"
        v-model="draft"
        class="w-full bg-transparent text-center outline-none"
        :maxlength="200"
        @keydown.enter.prevent="finish(true)"
        @keydown.esc.prevent="finish(false)"
        @keydown.tab.prevent="finish(true)"
        @blur="finish(true)"
      />
      <span v-else class="line-clamp-2 break-words">{{ data.label }}</span>
    </div>

    <div
      v-if="card && !editing"
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
