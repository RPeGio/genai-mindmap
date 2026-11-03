<script setup lang="ts">
import { Background } from '@vue-flow/background';
import { VueFlow, useVueFlow } from '@vue-flow/core';
import { nextTick, onBeforeUnmount, watch } from 'vue';

import { useMindMapStore } from '../stores/mindmap';
import MindNode from './MindNode.vue';

const store = useMindMapStore();
const { fitView, onNodeClick, onPaneClick, onMoveEnd, updateNodeInternals } = useVueFlow();

onNodeClick(({ node }) => store.select(node.id));
onPaneClick(() => store.select(null));
// 用户手动平移或缩放后停止自动适配，否则每长一个节点视图就会被拽回全图。
onMoveEnd(() => {
  store.viewAdjusted = true;
});

let fitTimer: ReturnType<typeof setTimeout> | undefined;
function scheduleFit() {
  if (store.viewAdjusted) return;
  clearTimeout(fitTimer);
  // 增量节点到达很密，节流一下，避免每帧都重新适配。
  fitTimer = setTimeout(() => fitView({ duration: 250, padding: 0.2 }), 150);
}

watch(() => store.nodeCount, scheduleFit);
watch(() => store.direction, scheduleFit);

// 换方向会改掉每个节点的句柄位置（左 / 右 / 上）。Vue Flow 只在自己重新测量节点时
// 才知道句柄动了，否则连线依旧按旧坐标绘制——表现为线头悬空、或从错误的一侧进出。
// 节点数量变化时也一样，新节点第一次测量往往赶不上布局。
watch(
  [() => store.direction, () => store.nodeCount],
  async () => {
    await nextTick();
    updateNodeInternals(store.nodes.map(node => node.id));
  },
  { flush: 'post' },
);

onBeforeUnmount(() => clearTimeout(fitTimer));
</script>

<template>
  <div class="relative h-full w-full">
    <VueFlow
      :nodes="store.nodes"
      :edges="store.edges"
      :nodes-draggable="false"
      :nodes-connectable="false"
      :elements-selectable="true"
      :pan-on-drag="true"
      :zoom-on-scroll="true"
      :min-zoom="0.2"
      :max-zoom="1.6"
      :fit-view-on-init="true"
      class="h-full w-full"
    >
      <Background :gap="20" pattern-color="#e2e8f0" />
      <template #node-mind="nodeProps">
        <MindNode v-bind="nodeProps" />
      </template>
    </VueFlow>

    <p
      v-if="!store.content && !store.busy"
      class="pointer-events-none absolute inset-0 flex items-center justify-center text-sm text-slate-400"
    >
      还没有导图，先在左侧输入内容。
    </p>
    <p
      v-else-if="!store.content && store.busy"
      class="pointer-events-none absolute inset-0 flex items-center justify-center text-sm text-slate-400"
    >
      正在生成，稍候…
    </p>
  </div>
</template>
