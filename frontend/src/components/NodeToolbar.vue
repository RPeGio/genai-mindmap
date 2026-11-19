<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';

import { NODE_COLORS, NODE_COLOR_LABELS } from '../mindmap/edits';
import { useMindMapStore } from '../stores/mindmap';

/**
 * 选中节点旁边浮出的小工具条：`[改][展开][删][色]`。
 *
 * **为什么是浮层而不是第二块面板**：这一版把常驻区域全撤了（左栏撤掉、右栏改覆盖式
 * 抽屉、对话框生成完就淡出），"选中一个节点"于是需要一个**就地**的入口——用户此刻
 * 的眼睛在节点上，不在屏幕边缘。
 *
 * **定位靠读 DOM**，不靠布局：节点坐标在 `stores/mindmap` 里算好、由 Vue Flow 画到
 * `.vue-flow__node[data-id]` 上。这里只 `getBoundingClientRect()`，一个像素都不改渲染。
 * 平移/缩放/长出新节点之后位置会变，所以 resize、节点数、方向、选中变化都要重算。
 */
const store = useMindMapStore();

/**
 * `expand` 交回 `App.vue`，与「改」同一条路。
 *
 * 工具条自己**不**调 `store.expandSelected()`：那条函数收一个 `sources` 参数，而
 * 工具条手里没有材料——只有 `useSourcesStore()` 里的当前那一份。两个组件各自读一次
 * 材料列表，就会出现"工具条传的是旧快照、App 传的是新快照"这种两条真相。
 * `App.vue` 的 `expandBranch` 是带材料的唯一入口，工具条只发一个意图。
 */
const emit = defineEmits<{ edit: [label: string]; expand: [] }>();

const anchor = ref<{ x: number; y: number } | null>(null);
const paletteOpen = ref(false);
/** 工具条自己，用来量它有多大——贴边时要靠这个尺寸把它按回视口里。 */
const barRef = ref<HTMLElement | null>(null);

/** 工具条离视口边缘至少留这么多像素：贴边节点的工具条也不该压在屏幕边上。 */
const VIEWPORT_MARGIN = 8;

const selected = computed(() => store.selectedNode);
const visible = computed(() => !!selected.value && !!anchor.value);

/** 这个节点是不是某块区域的根（中心主题 / 一根骨 / 一个阶段）——那种不能删。 */
const deleteDisabled = computed(
  () => (selected.value ? store.isContainerRootId(selected.value.node.id) : true),
);

function clamp(value: number, low: number, high: number): number {
  return Math.min(Math.max(value, low), Math.max(low, high));
}

/**
 * 工具条允许待的区域：**画布那块**（`main > section`），不是整个视口。
 *
 * 用视口的话，一个被平移到屏幕外的节点会把工具条按在 (8, 8)——正好压在顶栏上，
 * "没飘出去"变成了"盖住了返回键"。量不到画布（jsdom、还没布局）时退回整个视口。
 */
function bounds(): { left: number; top: number; right: number; bottom: number } {
  const rect = document.querySelector('main > section')?.getBoundingClientRect();
  if (rect && rect.width > 0 && rect.height > 0) {
    return { left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom };
  }
  return { left: 0, top: 0, right: window.innerWidth, bottom: window.innerHeight };
}

function measure(): void {
  const id = selected.value?.node.id;
  if (!id) {
    anchor.value = null;
    return;
  }
  const el = document.querySelector(`.vue-flow__node[data-id="${CSS.escape(id)}"]`);
  if (!(el instanceof HTMLElement)) {
    // 画布还没画出这个节点（刚落地、还没测量）：保持原位，别把它甩到左上角。
    anchor.value = null;
    return;
  }
  const rect = el.getBoundingClientRect();
  // 工具条是 `-translate-x-1/2 -translate-y-full`：锚点就是它自己的**下边中点**。
  // 所以横向按半个宽度让位，纵向整块高度都得在锚点上方。
  const bar = barRef.value?.getBoundingClientRect();
  const halfWidth = (bar?.width ?? 0) / 2;
  const height = bar?.height ?? 0;
  const area = bounds();
  anchor.value = {
    x: clamp(
      rect.left + rect.width / 2,
      area.left + VIEWPORT_MARGIN + halfWidth,
      area.right - VIEWPORT_MARGIN - halfWidth,
    ),
    // 节点顶到画布上沿时往下让：宁可压在节点上，也不飘出屏幕（或压住顶栏）。
    y: clamp(rect.top, area.top + VIEWPORT_MARGIN + height, area.bottom - VIEWPORT_MARGIN),
  };
}

let frame = 0;
function schedule(): void {
  if (frame) return;
  frame = requestAnimationFrame(() => {
    frame = 0;
    measure();
  });
}

/**
 * 盯住 Vue Flow 那个**真正承载平移/缩放**的元素。
 *
 * 平移与缩放不是 window 事件：Vue Flow 把整张图挂在一个元素上，用一句
 * `transform: translate(...) scale(...)` 挪它——既不发 scroll 也不发 resize。
 * 只听 window 的 resize/scroll 时，用户按住画布一拖，工具条就停在拖之前的坐标，
 * 而节点早就跟着走了；缩放到 0.5 倍之后同样对不上。
 *
 * 注意落点：`@vue-flow/core` 1.x 的 DOM 是
 * `.vue-flow__viewport > .vue-flow__pane > .vue-flow__transformationpane`，那句
 * transform 写在**最里面**那个 `transformationpane` 上（`viewport` 只是外框，
 * 它身上只有 `-webkit-tap-highlight-color`）。盯错了元素，监听装得上、一次都不响。
 * 两个都盯：将来搬回外层也不会悄悄失效。
 */
const VIEWPORT_SELECTORS = ['.vue-flow__transformationpane', '.vue-flow__viewport'];

let viewportObserver: MutationObserver | null = null;

function watchViewport(): void {
  if (viewportObserver) return;
  const targets = document.querySelectorAll(VIEWPORT_SELECTORS.join(', '));
  if (!targets.length) return;  // 画布还没渲染完：下一次 watch 触发时会再试
  viewportObserver = new MutationObserver(schedule);
  targets.forEach(target =>
    viewportObserver?.observe(target, { attributes: true, attributeFilter: ['style'] }));
}

watch(
  () => [selected.value?.node.id, store.nodeCount, store.direction, store.focusTick] as const,
  async () => {
    paletteOpen.value = false;
    await nextTick();
    watchViewport();
    schedule();
  },
  { immediate: true },
);

/**
 * 工具条第一次真的画出来之后再量一次。
 *
 * 第一次 `measure` 时它还不存在（`v-if` 跟着 `anchor` 走），量不到自己的宽高，
 * 贴边的节点就会踩着边缘飘出去半个工具条。`visible` 变真之后补这一枪——只补一次，
 * 而且不是在 `anchor` 上循环（那会在 rAF 里自激）。
 */
watch(visible, async on => {
  if (!on) return;
  await nextTick();
  schedule();
});

onMounted(() => {
  window.addEventListener('resize', schedule);
  window.addEventListener('scroll', schedule, true);
  watchViewport();
});

onBeforeUnmount(() => {
  if (frame) cancelAnimationFrame(frame);
  viewportObserver?.disconnect();
  viewportObserver = null;
  window.removeEventListener('resize', schedule);
  window.removeEventListener('scroll', schedule, true);
});

function edit(): void {
  const label = selected.value?.node.label.trim();
  if (!label) return;
  emit('edit', label);
}

function expand(): void {
  // 不调 `store.expandSelected()`：那条路不带材料（见 `emit` 的注释）。
  emit('expand');
}

function remove(): void {
  const id = selected.value?.node.id;
  if (!id) return;
  store.deleteNode(id);
}

function setColor(value: string): void {
  const id = selected.value?.node.id;
  if (!id) return;
  store.setNodeVisualById(id, { color: value ? (value as (typeof NODE_COLORS)[number]) : null });
  paletteOpen.value = false;
}
</script>

<template>
  <div
    v-if="visible && anchor"
    ref="barRef"
    data-test="node-toolbar"
    class="fixed z-40 flex -translate-x-1/2 -translate-y-full flex-col items-center gap-1 pb-2"
    :style="{ left: `${anchor.x}px`, top: `${anchor.y}px` }"
    role="toolbar"
    aria-label="节点操作"
  >
    <div class="flex items-center gap-0.5 rounded-lg border border-slate-200 bg-white p-0.5 shadow-lg">
      <button
        type="button"
        data-test="node-action-edit"
        class="rounded px-2 py-1 text-[11px] text-slate-700 hover:bg-slate-100"
        title="让 AI 改这个节点（对话框会重新出现并预填这一句）"
        @click="edit"
      >改</button>
      <button
        type="button"
        data-test="node-action-expand"
        class="rounded px-2 py-1 text-[11px] text-slate-700 hover:bg-slate-100 disabled:opacity-40"
        :disabled="store.agentRunning || selected?.node.expandable === 'no'"
        title="展开这一支（补 3 个子节点）"
        @click="expand"
      >展开</button>
      <button
        type="button"
        data-test="node-action-delete"
        class="rounded px-2 py-1 text-[11px] text-slate-700 hover:bg-slate-100 disabled:opacity-40"
        :disabled="deleteDisabled"
        title="删除这个节点"
        @click="remove"
      >删</button>
      <button
        type="button"
        data-test="node-action-color"
        class="rounded px-2 py-1 text-[11px] text-slate-700 hover:bg-slate-100"
        title="换颜色"
        :aria-expanded="paletteOpen"
        @click="paletteOpen = !paletteOpen"
      >色</button>
    </div>

    <div
      v-if="paletteOpen"
      data-test="node-palette"
      class="flex items-center gap-1 rounded-lg border border-slate-200 bg-white p-1 shadow-lg"
    >
      <button
        type="button"
        class="h-4 w-4 rounded-full border border-slate-300 bg-white text-[9px] leading-none text-slate-500"
        title="默认（继承上级）"
        @click="setColor('')"
      >–</button>
      <button
        v-for="key in NODE_COLORS"
        :key="key"
        type="button"
        class="h-4 w-4 rounded-full border border-slate-300"
        :style="{ background: `var(--mind-color-${key}, #cbd5e1)` }"
        :title="NODE_COLOR_LABELS[key]"
        :aria-label="NODE_COLOR_LABELS[key]"
        @click="setColor(key)"
      />
    </div>
  </div>
</template>
