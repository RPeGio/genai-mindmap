<script setup lang="ts">
import { computed, ref } from 'vue';

import MindMapCanvas from './canvas/MindMapCanvas.vue';
import type { Direction } from './canvas/layout/tidy';
import { displayBody, useMindMapStore } from './stores/mindmap';

const store = useMindMapStore();

const prompt = ref('');
const copied = ref('');

const directions: { value: Direction; label: string }[] = [
  { value: 'right', label: '向右' },
  { value: 'left', label: '向左' },
  { value: 'down', label: '向下' },
  { value: 'both', label: '双向' },
];

const selected = computed(() => store.selectedNode);
const pathText = computed(() => store.breadcrumb.map(node => node.label.trim()).join(' / '));

async function generate() {
  await store.generate(prompt.value);
}

async function copyBody() {
  const node = selected.value?.node;
  if (!node) return;
  try {
    await navigator.clipboard.writeText(displayBody(node));
    copied.value = '已复制';
  } catch {
    // 非 HTTPS 或浏览器拒绝剪贴板权限时只能提示，不能假装成功。
    copied.value = '复制失败，请手动选择文字';
  }
  setTimeout(() => {
    copied.value = '';
  }, 2000);
}
</script>

<template>
  <div class="flex h-full w-full flex-col bg-slate-50 text-slate-900">
    <header class="flex items-center justify-between border-b border-slate-200 bg-white px-4 py-2">
      <h1 class="text-sm font-semibold">生成式 AI 思维导图</h1>
      <p class="text-xs text-slate-500">
        <span v-if="store.mode === 'demo'" class="rounded bg-amber-100 px-2 py-0.5 text-amber-700">
          演示模式（非 AI 生成）
        </span>
        <span v-else-if="store.mode === 'model'" class="rounded bg-emerald-100 px-2 py-0.5 text-emerald-700">
          模型生成
        </span>
        <span v-else>未生成</span>
        <span class="ml-3">{{ store.nodeCount }} 个节点</span>
      </p>
    </header>

    <main class="flex min-h-0 flex-1">
      <section class="flex w-80 shrink-0 flex-col gap-3 border-r border-slate-200 bg-white p-4">
        <label for="prompt" class="text-xs font-medium text-slate-600">输入主题或文本</label>
        <textarea
          id="prompt"
          v-model="prompt"
          rows="8"
          class="resize-none rounded-md border border-slate-300 p-2 text-sm outline-none focus:border-slate-500"
          placeholder="例如：机器学习的主要方向"
        />
        <div class="flex gap-2">
          <button
            type="button"
            class="flex-1 rounded-md bg-slate-900 px-3 py-2 text-sm text-white disabled:opacity-50"
            :disabled="store.busy"
            @click="generate"
          >
            {{ store.busy ? '生成中…' : '生成导图' }}
          </button>
          <button
            v-if="store.busy"
            type="button"
            class="rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-700"
            @click="store.cancel()"
          >
            取消
          </button>
        </div>
        <p role="status" class="text-xs leading-relaxed text-slate-500">
          {{ store.status }}
        </p>
        <p v-if="store.stageText" class="text-xs text-slate-400">
          {{ store.stageText }}<span v-if="store.waitedMs > 0">（已等待 {{ store.waitedMs }} 秒）</span>
        </p>
        <p class="mt-auto text-xs leading-relaxed text-slate-400">
          当前版本是只读画布：可以平移、缩放、点选节点查看全文，不能拖动或编辑节点。
        </p>
      </section>

      <section class="relative min-w-0 flex-1">
        <div class="absolute left-4 top-4 z-10 flex gap-1 rounded-md border border-slate-200 bg-white p-1 shadow-sm">
          <button
            v-for="item in directions"
            :key="item.value"
            type="button"
            class="rounded px-2 py-1 text-xs"
            :class="store.direction === item.value ? 'bg-slate-900 text-white' : 'text-slate-600 hover:bg-slate-100'"
            @click="store.setDirection(item.value)"
          >
            {{ item.label }}
          </button>
        </div>
        <MindMapCanvas />
      </section>

      <aside class="flex w-80 shrink-0 flex-col gap-3 border-l border-slate-200 bg-white p-4">
        <template v-if="selected">
          <p class="text-xs text-slate-400">{{ pathText }}</p>
          <h2 class="text-sm font-semibold">
            {{ selected.node.label.trim() }}
          </h2>
          <p class="flex-1 overflow-y-auto whitespace-pre-wrap break-words text-sm leading-relaxed text-slate-700">
            {{ displayBody(selected.node) }}
          </p>
          <div class="flex items-center gap-2">
            <button
              type="button"
              class="rounded-md border border-slate-300 px-3 py-1.5 text-xs text-slate-700"
              @click="copyBody"
            >
              复制全文
            </button>
            <span class="text-xs text-slate-400">{{ copied }}</span>
          </div>
        </template>
        <template v-else>
          <h2 class="text-sm font-semibold">节点详情</h2>
          <p class="text-xs leading-relaxed text-slate-500">
            点击画布上的任意节点查看完整内容与它所在的层级路径。生成的导图只是候选结构，编辑与保存将在下一版本提供。
          </p>
        </template>
      </aside>
    </main>
  </div>
</template>
