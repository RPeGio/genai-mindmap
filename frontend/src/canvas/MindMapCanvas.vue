<script setup lang="ts">
import { Background } from '@vue-flow/background';
import { VueFlow, useVueFlow } from '@vue-flow/core';
import { computed, nextTick, onBeforeUnmount, onMounted, watch } from 'vue';

import { useMindMapStore } from '../stores/mindmap';
import { NODE_SIZE } from './layout/tidy';
import FishboneEdge from './FishboneEdge.vue';
import MindEdge from './MindEdge.vue';
import MindNode from './MindNode.vue';

/**
 * 画布是**纯渲染**：节点坐标、连线句柄、鱼骨的骨线折线都由 store 算好
 * （分派在 `canvas/layout/index.ts` 的 `layoutDiagram`，按 `content.diagram_type`）。
 *
 * 分派为什么不在这里：本组件手上没有那棵树，只有扁平的 `nodes` / `edges`；而连线
 * 的句柄是按节点坐标反推的，两处各算一次布局，节点与连线立刻分家。这里唯一按图型
 * 分叉的地方是连线的**渲染器**——`smoothstep`（父子线）/ `mindEdge`（附加关系）/
 * `fishbone`（骨线）。
 */
const store = useMindMapStore();
const { fitView, setCenter, onNodeClick, onNodeDoubleClick, onPaneClick, onMoveEnd, updateNodeInternals } =
  useVueFlow();

// 预览节点也在同一份 nodes 里，但 store.select 只接受 content 中真实存在的 id：
// 画布这边不必自己判断来源，点不中就是点不中。
//
// 按住 Ctrl / Cmd / Shift 点是**追加选区**：「改这张图」吃的是整个选区（用户圈三个
// 节点说「这几个合并成一个」，模型得知道是哪三个）。不加修饰键的单击仍然是
// "只看这一个"，并把追加选区清掉——那才是用户在画布上随手点一下的预期。
onNodeClick(({ node, event }) => {
  const additive = event.ctrlKey || event.metaKey || event.shiftKey;
  if (additive) store.toggleSelect(node.id);
  else store.select(node.id);
});
onNodeDoubleClick(({ node }) => store.beginEdit(node.id));
onPaneClick(() => store.clearSelection());
// 用户手动平移或缩放后停止自动适配，否则每长一个节点视图就会被拽回全图。
onMoveEnd(() => {
  store.viewAdjusted = true;
});

/** 焦点在输入框里时不能抢键：那边的 Tab、Delete 归输入框自己管。 */
function isTyping(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  if (!el) return false;
  return el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable;
}

function onKeydown(event: KeyboardEvent) {
  if (isTyping(event.target)) return;

  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'z') {
    event.preventDefault();
    if (event.shiftKey) store.redo();
    else store.undo();
    return;
  }

  const id = store.selectedId;
  if (!id) return;

  switch (event.key) {
    case 'Tab':
      event.preventDefault();
      store.addChildTo(id);
      break;
    case 'Enter':
      event.preventDefault();
      store.addSiblingTo(id);
      break;
    case 'Delete':
    case 'Backspace':
      event.preventDefault();
      store.deleteNode(id);
      break;
    case 'F2':
      event.preventDefault();
      store.beginEdit(id);
      break;
  }
}

onMounted(() => window.addEventListener('keydown', onKeydown));

let fitTimer: ReturnType<typeof setTimeout> | undefined;
function scheduleFit() {
  if (store.viewAdjusted) return;
  clearTimeout(fitTimer);
  // 增量节点到达很密，节流一下，避免每帧都重新适配。
  fitTimer = setTimeout(() => fitView({ duration: 250, padding: 0.2 }), 150);
}

watch(() => store.nodeCount, scheduleFit);
watch(() => store.direction, scheduleFit);

// 从引用列表跳过来时把目标挪到视野中心。位置就是布局算出的绝对坐标，
// 中心点 = 左上角 + 半个节点；不需要访问 Vue Flow 内部的节点实例。
// 跳转后关掉自动适配：用户按自己的意志看某一支，下一帧被拉回全图会很难受。
//
// 半个节点取**这个节点自己的**外接框（`data.size`）：判定节点的菱形是 220×110，
// 按 `NODE_SIZE` 加半个会把判定节点跳偏 20/31 像素，而偏移在跳转动画里看不出来。
// 树与鱼骨没有 `data.size`，回退到 `NODE_SIZE`，与改动前逐像素相同。
watch(() => store.focusTick, () => {
  const target = store.nodes.find(node => node.id === store.selectedId);
  if (!target) return;
  store.viewAdjusted = true;
  const width = target.data?.size?.width ?? NODE_SIZE.width;
  const height = target.data?.size?.height ?? NODE_SIZE.height;
  void setCenter(
    target.position.x + width / 2,
    target.position.y + height / 2,
    { duration: 250, zoom: 1 },
  );
});

// 换方向会改掉每个节点的句柄位置（左 / 右 / 上 / 下）。Vue Flow 只在自己重新测量节点时
// 才知道句柄动了，否则连线依旧按旧坐标绘制——表现为线头悬空、或从错误的一侧进出。
// 用"每条连线用了哪两个句柄"做签名：它变了才需要重测，标签更新之类的变化不用。
const handleSignature = computed(() =>
  store.edges.map(edge => `${edge.sourceHandle}>${edge.targetHandle}`).join('|'),
);

/**
 * 节点外接框的签名。
 *
 * 判定节点的菱形比 `NODE_SIZE` 大，句柄的**绝对位置**跟着变（左右句柄在 x 上偏
 * 20 像素、上下句柄在 y 上偏 31 像素）。Vue Flow 的 ResizeObserver 通常能自己发现，
 * 但"测量"与"句柄注册"是两条路，漏一次的表现为"线还挂在旧坐标上"。这里把尺寸也
 * 算进重测的触发条件。
 *
 * 树与鱼骨没有 `data.size`（`stores/mindmap` 只在流程图里发它），签名恒为空串，
 * 这条 watch 对它们一次都不会额外触发。
 */
const sizeSignature = computed(() =>
  store.nodes
    .map(node => `${node.id}:${node.data?.size?.width ?? 0}x${node.data?.size?.height ?? 0}`)
    .join('|'),
);

watch(
  [() => store.direction, () => store.nodeCount, handleSignature, sizeSignature],
  async () => {
    await nextTick();
    updateNodeInternals(store.nodes.map(node => node.id));
  },
  { flush: 'post' },
);

onBeforeUnmount(() => {
  clearTimeout(fitTimer);
  window.removeEventListener('keydown', onKeydown);
});
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
        <MindNode
          v-bind="nodeProps"
          :editing="store.editingId === nodeProps.id"
          @commit="label => store.commitLabel(nodeProps.id, label)"
          @next="label => store.commitAndAddChild(label)"
          @cancel="store.endEdit()"
        />
      </template>
      <!--
        附加边（条件分支 / 汇合 / 回退）。注册成 `edge-mindEdge` 就够——
        `stores/mindmap` 只给附加边挂 `type: 'mindEdge'`，父子线仍然走内置的
        smoothstep。两条路分开是刻意的：父子线是布局的一部分（必须跟着 tidy 的
        句柄走），附加边是叠加语义（要标签、要按 kind 换画法），合成一个组件只会
        让父子线的渲染被附加边的逻辑拖住。
      -->
      <template #edge-mindEdge="edgeProps">
        <MindEdge v-bind="edgeProps" />
      </template>
      <!--
        鱼骨图的骨线（主骨段 + 60° 斜骨、以及原因那截水平短线）。点的来源是
        `layout/fishbone.ts` 算好的折线，这里只描边——几何不在这层猜。
      -->
      <template #edge-fishbone="edgeProps">
        <FishboneEdge v-bind="edgeProps" />
      </template>
    </VueFlow>

    <!-- 空态用「画布上一个节点都没有」判断，而不是「content 为空」：生成中预览
         会长出节点，这时再压一条占位文字，正好盖住用户想看的生长过程。
         文案要指出**输入框在哪**：合并之后左侧只剩材料区，老用户会先往左看。 -->
    <p
      v-if="store.nodeCount === 0 && !store.busy"
      class="pointer-events-none absolute inset-0 flex items-center justify-center text-sm text-slate-400"
    >
      还没有导图：在右上角写个主题，或先在左侧添加材料。
    </p>
    <p
      v-else-if="store.nodeCount === 0 && store.busy"
      class="pointer-events-none absolute inset-0 flex items-center justify-center text-sm text-slate-400"
    >
      正在生成，稍候…
    </p>
  </div>
</template>
