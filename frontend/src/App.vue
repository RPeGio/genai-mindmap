<script setup lang="ts">
import { computed, ref } from 'vue';
import { ApiError, generateMindMap } from './api/client';
import type { MindMapContent, MindNode } from './api/types';

const prompt = ref('');
const content = ref<MindMapContent | null>(null);
const mode = ref<'demo' | 'model' | null>(null);
const busy = ref(false);
const status = ref('输入主题或文本后生成。默认演示模式，配置模型后才会调用 AI。');

interface Row {
  id: string;
  label: string;
  depth: number;
}

/** R1 先用扁平列表验证链路，R2 换成画布布局。 */
const rows = computed<Row[]>(() => {
  const out: Row[] = [];
  const walk = (node: MindNode, depth: number) => {
    out.push({ id: node.id, label: node.label, depth });
    node.children.forEach((child) => walk(child, depth + 1));
  };
  if (content.value) walk(content.value.root, 0);
  return out;
});

async function generate() {
  const text = prompt.value.trim();
  if (!text) {
    status.value = '请输入主题或文本。';
    return;
  }
  busy.value = true;
  try {
    const result = await generateMindMap(text);
    content.value = result.content;
    mode.value = result.mode;
    status.value =
      result.mode === 'demo' ? '这是固定结构的演示数据，不是 AI 生成。' : '模型已生成候选导图，请检查内容。';
  } catch (error) {
    status.value = error instanceof ApiError ? error.message : '生成失败，请重试。';
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div class="flex h-full flex-col">
    <header class="border-b border-slate-200 px-6 py-4">
      <h1 class="text-lg font-semibold">生成式 AI 思维导图</h1>
      <p class="text-sm text-slate-500">文本 → 可编辑导图</p>
    </header>

    <div class="flex min-h-0 flex-1">
      <aside class="w-96 shrink-0 overflow-y-auto border-r border-slate-200 p-6">
        <label for="prompt" class="mb-2 block text-sm font-medium">主题或文本</label>
        <textarea
          id="prompt"
          v-model="prompt"
          :disabled="busy"
          maxlength="10000"
          rows="10"
          placeholder="例如：整理机器学习的主要方向"
          class="w-full resize-y rounded border border-slate-300 p-2 text-sm disabled:bg-slate-100"
        />
        <button
          type="button"
          :disabled="busy"
          class="mt-3 w-full rounded bg-slate-900 px-4 py-2 text-sm text-white disabled:opacity-50"
          @click="generate"
        >
          {{ busy ? '生成中…' : '生成导图' }}
        </button>
        <p class="mt-3 text-sm text-slate-600" role="status">{{ status }}</p>
      </aside>

      <main class="min-w-0 flex-1 overflow-auto p-6">
        <div v-if="content" class="space-y-3">
          <div class="flex items-baseline gap-3">
            <h2 class="text-base font-semibold">{{ content.title }}</h2>
            <span v-if="mode" class="rounded bg-slate-100 px-2 py-0.5 text-xs text-slate-600">
              {{ mode === 'demo' ? '演示模式' : '模型生成' }}
            </span>
          </div>
          <ul class="text-sm">
            <li
              v-for="row in rows"
              :key="row.id"
              class="py-0.5"
              :style="{ paddingLeft: `${row.depth * 1.25}rem` }"
            >
              {{ row.label }}
            </li>
          </ul>
        </div>
        <p v-else class="text-sm text-slate-400">还没有导图，先在左侧输入内容。</p>
      </main>
    </div>
  </div>
</template>
