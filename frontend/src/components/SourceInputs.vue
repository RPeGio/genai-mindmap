<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';

import RecordButton from './RecordButton.vue';
import { FILE_ACCEPT, MAX_INPUT_CHARS, MAX_SOURCES, mergedCharCount, SOURCE_LABELS, sourceInputError, sourceWarning, textSource } from '../sources/input';
import { useMindMapStore } from '../stores/mindmap';
import { useSourcesStore } from '../stores/sources';

const props = defineProps<{ confirmDiscard: (action: string) => boolean }>();
const store = useMindMapStore();
const sources = useSourcesStore();
const prompt = ref('');

/**
 * 拆解方式（思维模型）。值与后端 `generation.service.THINKING_MODELS` 的键
 * 一一对应；选项写死在这里而不是拉接口，是因为这一批不做那个接口，而写死的
 * 五个值也正是后端 Literal 允许的全集——两边不一致时后端会 422，不会静默变形。
 */
const THINKING_MODEL_OPTIONS = [
  { value: 'default', label: '通用' },
  { value: 'first-principles', label: '第一性原理' },
  { value: 'feynman', label: '费曼' },
  { value: 'systematic', label: '5W1H' },
  { value: 'critical', label: '批判性' },
];
const thinkingModel = ref('default');

const recording = ref(false);
const generating = computed(() => store.busy || store.sourceGenerating);
const combined = computed(() => [
  ...sources.readySources,
  ...(prompt.value.trim() ? [textSource(prompt.value)] : []),
]);
const charCount = computed(() => mergedCharCount(combined.value));
const problem = computed(() => {
  if (!sources.items.length) return '';
  return sources.problem || sourceInputError(combined.value);
});

function addText() {
  if (sources.addText(prompt.value)) prompt.value = '';
}

async function onPickFiles(event: Event) {
  const input = event.target as HTMLInputElement;
  const selected = Array.from(input.files ?? []);
  // 再选同一个文件也应触发 change。
  input.value = '';
  await sources.addFiles(selected);
}

async function generate() {
  if (generating.value || sources.parsing || recording.value || problem.value) return;
  if (!props.confirmDiscard('重新生成')) return;

  // agent 路径：输入框里是"要做什么"，来源列表是"依据什么"——两者分开送。
  // 曾经把材料正文拼进 goal 并截到 1000 字：材料大半被丢掉，而且只要输入框
  // 有一个字，附件就完全不参与。是否新建取决于当前有没有导图。
  if (store.agentEnabled) {
    const text = prompt.value.trim();
    const ready = sources.readySources;
    // 只写了材料、没写目标时给一句占位，别把正文当目标塞进去。
    const goal = text || (ready.length ? `请基于附带的 ${ready.length} 份材料生成导图` : '');
    await store.runGeneration(goal, store.content ? 'edit' : 'create', ready, thinkingModel.value);
    return;
  }

  if (!sources.items.length) {
    await store.generate(prompt.value);
    return;
  }
  // 输入框里还没点「添加」的补充文本也要参与，不能默默忽略。
  if (prompt.value.trim()) {
    if (!sources.addText(prompt.value)) return;
    prompt.value = '';
  }
  await store.generateSources(sources.readySources, {
    confirmReplace: reason => window.confirm(reason === 'switched'
      ? '生成结果已就绪，但你已经切换到另一张导图。\n用新生成的导图替换当前画布吗？取消将保留当前内容。'
      : '生成结果已就绪，但等待期间你修改过当前导图。\n用新生成的导图替换你的修改吗？取消将保留你的修改。'),
  });
}

function cancel() {
  if (store.sourceGenerating) store.cancelSourceGeneration();
  else store.cancel(true);
}

function beforeUnload(event: BeforeUnloadEvent) {
  if (!sources.items.length && !prompt.value.trim() && !recording.value) return;
  event.preventDefault();
  event.returnValue = '';
}

onMounted(() => window.addEventListener('beforeunload', beforeUnload));
onBeforeUnmount(() => window.removeEventListener('beforeunload', beforeUnload));
</script>

<template>
  <div class="flex flex-col gap-3">
    <div class="flex items-center justify-between">
      <h2 class="text-sm font-semibold">多来源输入</h2>
      <span class="text-xs text-slate-400">文本 · 文档 · 图片 · 语音</span>
    </div>
    <label for="prompt" class="text-xs font-medium text-slate-600">输入主题或文本</label>
    <label for="thinking-model" class="text-xs font-medium text-slate-600">拆解方式</label>
    <select
      id="thinking-model"
      v-model="thinkingModel"
      :disabled="generating"
      class="rounded-md border border-slate-300 p-2 text-sm outline-none focus:border-slate-500 disabled:opacity-50"
    >
      <option v-for="item in THINKING_MODEL_OPTIONS" :key="item.value" :value="item.value">{{ item.label }}</option>
    </select>
    <textarea
      id="prompt"
      v-model="prompt"
      rows="4"
      :disabled="generating"
      class="resize-y rounded-md border border-slate-300 p-2 text-sm outline-none focus:border-slate-500 disabled:opacity-50"
      placeholder="例如：机器学习的主要方向，也可粘贴一段补充材料"
    />
    <button
      type="button"
      class="rounded-md border border-slate-300 px-3 py-1.5 text-xs text-slate-700 hover:bg-slate-50 disabled:opacity-50"
      :disabled="generating || recording || !prompt.trim()"
      @click="addText"
    >添加文本到来源</button>

    <label
      class="rounded-md border border-dashed border-slate-300 px-3 py-2 text-center text-xs text-slate-600 focus-within:ring-2 focus-within:ring-slate-200"
      :class="generating || recording ? 'opacity-50' : 'cursor-pointer hover:bg-slate-50'"
    >
      <input
        type="file"
        multiple
        class="sr-only"
        :accept="FILE_ACCEPT"
        :disabled="generating || recording"
        @change="onPickFiles"
      />
      添加文件（可多选）
    </label>
    <p class="text-xs leading-relaxed text-slate-400">
      PDF / Word / PPT / Excel / HTML / Markdown / 图片 / WAV。文档、图片每份 10MB，WAV 每份 20MB；PDF 最多识别 20 页。
    </p>
    <RecordButton
      :disabled="generating || sources.items.length >= MAX_SOURCES"
      @active="recording = $event"
      @recorded="sources.addFiles([$event])"
    />

    <div class="flex items-center justify-between text-xs">
      <h3 class="font-medium text-slate-600">来源列表（{{ sources.items.length }}/{{ MAX_SOURCES }}）</h3>
      <button
        v-if="sources.items.length"
        type="button"
        class="text-slate-500 hover:text-slate-900 disabled:opacity-50"
        :disabled="generating"
        @click="sources.clear()"
      >清空来源</button>
    </div>
    <p v-if="!sources.items.length" class="text-xs leading-relaxed text-slate-400">
      添加多个来源后一起生成；也可以只输入文本，直接生成导图。
    </p>
    <ul v-else aria-label="待生成来源" class="flex flex-col gap-2">
      <li v-for="item in sources.items" :key="item.id" class="rounded-md border border-slate-200 p-2">
        <div class="flex items-start gap-2 text-xs">
          <span class="shrink-0 rounded bg-slate-100 px-1 py-0.5 text-slate-500">{{ SOURCE_LABELS[item.kind] }}</span>
          <span class="min-w-0 flex-1 break-words font-medium text-slate-700">{{ item.label }}</span>
          <button
            type="button"
            class="shrink-0 text-slate-400 hover:text-rose-600 disabled:opacity-50"
            :aria-label="`移除 ${item.label}`"
            :disabled="generating"
            @click="sources.remove(item.id)"
          >移除</button>
        </div>
        <p v-if="item.state === 'queued'" class="mt-1 text-xs text-slate-500">等待解析…</p>
        <p v-else-if="item.state === 'parsing'" class="mt-1 text-xs text-slate-500" aria-live="polite">
          正在解析…已等待 {{ item.seconds }} 秒
        </p>
        <template v-else-if="item.state === 'ready' && item.source">
          <p class="mt-1 text-xs text-emerald-700">
            已就绪 · {{ Array.from(item.source.text?.trim() ?? '').length }} 字
            <span v-if="item.source.parsed_pages != null"> · {{ item.source.parsed_pages }} 页</span>
            <span v-if="item.source.duration_seconds != null"> · {{ Math.round(item.source.duration_seconds) }} 秒</span>
          </p>
          <p v-if="sourceWarning(item.source)" role="alert" class="mt-1 text-xs leading-relaxed text-amber-700">
            {{ sourceWarning(item.source) }}
          </p>
          <details class="mt-1 text-xs text-slate-500">
            <summary class="cursor-pointer">查看正文</summary>
            <p class="mt-1 max-h-40 overflow-y-auto whitespace-pre-wrap break-words leading-relaxed">{{ item.source.text }}</p>
          </details>
        </template>
        <template v-else-if="item.state === 'failed'">
          <p role="alert" class="mt-1 whitespace-pre-wrap break-words text-xs leading-relaxed text-rose-700">{{ item.error }}</p>
          <button type="button" class="mt-1 text-xs text-slate-700 underline disabled:opacity-50" :disabled="generating" @click="sources.retry(item.id)">
            重试解析
          </button>
        </template>
      </li>
    </ul>
    <p v-if="sources.parsing" class="text-xs leading-relaxed text-slate-400">
      文件按顺序解析；PDF 逐页识别，约每页 5–10 秒。
    </p>
    <p v-if="sources.error" role="alert" class="text-xs leading-relaxed text-rose-700">{{ sources.error }}</p>
    <template v-if="sources.items.length">
      <p class="text-xs" :class="charCount > MAX_INPUT_CHARS ? 'text-rose-700' : 'text-slate-500'">
        合并后 {{ charCount }} / {{ MAX_INPUT_CHARS }} 字（含分隔换行）
      </p>
      <p v-if="prompt.trim()" class="text-xs text-slate-500">生成时会一并加入输入框中的补充文本。</p>
      <p v-if="problem" class="text-xs leading-relaxed text-amber-700">{{ problem }}</p>
      <p class="text-xs leading-relaxed text-slate-400">来源正文仅在本页保留；保存导图会保留来源摘要。</p>
    </template>
    <div class="flex gap-2">
      <button
        type="button"
        class="flex-1 rounded-md bg-slate-900 px-3 py-2 text-sm text-white disabled:opacity-50"
        :disabled="generating || sources.parsing || recording || !!problem"
        @click="generate"
      >{{ generating ? '生成中…' : sources.items.length ? '合并生成导图' : '生成导图' }}</button>
      <button v-if="generating" type="button" class="rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-700" @click="cancel">取消</button>
    </div>
    <p role="status" class="text-xs leading-relaxed text-slate-500">{{ store.status }}</p>
    <p v-if="store.stageText" class="text-xs text-slate-400">
      {{ store.stageText }}<span v-if="store.waitedMs > 0">（已等待 {{ store.waitedMs }} 秒）</span>
    </p>
    <p v-if="store.sourceGenerationError" role="alert" class="whitespace-pre-wrap break-words text-xs leading-relaxed text-rose-700">{{ store.sourceGenerationError }}</p>
  </div>
</template>
