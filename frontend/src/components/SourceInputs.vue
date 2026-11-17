<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';

import RecordButton from './RecordButton.vue';
import type { DiagramTypeChoice } from '../api/types';
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

/**
 * 图型。`auto` = 不传字段（后端 `None`，让模型自己判断）——它是默认值，也是
 * "用户没说"这件事在界面上唯一诚实的呈现：不选，就由生成那一次决定。
 *
 * 四个值与后端 `AgentRunRequest.diagram_type` 的 Literal 一一对应（`auto` 那一项
 * 不发出去）。写死在这里而不是拉接口，理由与 `THINKING_MODEL_OPTIONS` 相同：
 * 拼错的值会被后端 422 挡下，不会静默变形。
 *
 * `hint` 是**必须**的：图型不是自解释的选项。用户选「流程图」时，他期待的是
 * "一级分支按阶段排、判断节点分是/否"，而不是"换个颜色的思维导图"；选「鱼骨图」
 * 时更得先知道一级分支固定是那六个类别——否则他会以为模型没听懂他的主题。
 */
const DIAGRAM_TYPE_OPTIONS: { value: DiagramTypeChoice; label: string; hint: string }[] = [
  { value: 'auto', label: '自动判断', hint: '按你的描述判断图型；判断不出时按思维导图生成。' },
  { value: 'tree', label: '思维导图', hint: '一级分支按话题分解，不画附加连线。' },
  { value: 'flowchart', label: '流程图', hint: '一级分支会按执行阶段排，判断节点带是/否分支' },
  { value: 'fishbone', label: '鱼骨图', hint: '一级分支固定为人/机/料/法/环/测' },
];
const diagramType = ref<DiagramTypeChoice>('auto');
const diagramHint = computed(
  () => DIAGRAM_TYPE_OPTIONS.find(item => item.value === diagramType.value)?.hint ?? '',
);

/**
 * 「拆解方式」与「图型」这两个下拉只在**新建**路径上生效：已有导图时点击生成走
 * `edit` 意图，而后端的编辑路径既不消费 `thinking_model`、也不生成骨架（图型只
 * 影响骨架那一次调用的提示词）。摆一个选了不起作用的控件比不摆更糟——用户会以为
 * 自己换过拆法或图型了。
 */
const createOptionsVisible = computed(() => !store.content);

const recording = ref(false);
const generating = computed(() => store.busy);
const combined = computed(() => [
  ...sources.readySources,
  ...(prompt.value.trim() ? [textSource(prompt.value)] : []),
]);
const charCount = computed(() => mergedCharCount(combined.value));
const problem = computed(() => {
  if (!sources.items.length) return '';
  return sources.problem || sourceInputError(combined.value);
});

/**
 * 有导图时才谈"这份来源有没有被引用"。
 *
 * 还没生成就摆一条 amber 提示，等于催用户去做他正要做的下一步；而且刚添加完
 * 材料时所有来源都还没被引用，满屏黄字只会让人以为出了问题。
 */
const showRefState = computed(() => !!store.content);

function refCount(sourceId: string): number {
  return store.refIndex.counts[sourceId] ?? 0;
}

function refHits(sourceId: string) {
  return store.refIndex.hits[sourceId] ?? [];
}

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

  // 输入框里是"要做什么"，来源列表是"依据什么"——两者分开送。
  // 曾经把材料正文拼进 goal 并截到 1000 字：材料大半被丢掉，而且只要输入框
  // 有一个字，附件就完全不参与。是否新建取决于当前有没有导图。
  const text = prompt.value.trim();
  const ready = sources.readySources;
  // 只写了材料、没写目标时给一句占位，别把正文当目标塞进去。
  const goal = text || (ready.length ? `请基于附带的 ${ready.length} 份材料生成导图` : '');
  // 图型跟着一起发：`auto` 时 store 那边会省略这个字段（后端 None = 让模型判断）。
  //
  // 第六个参数把选区**显式清空**：这个按钮旁边没有「已选 N 个节点」徽标，用户
  // 看不到"这次带着画布上的选区"。看不见的东西就不该偷偷发出去——他要改选中的
  // 那几个节点，画布上方那个「改这张图」才是那个入口。
  await store.runGeneration(
    goal, store.content ? 'edit' : 'create', ready,
    thinkingModel.value, diagramType.value, { selection: [] },
  );
}

function cancel() {
  store.cancel();
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
    <template v-if="createOptionsVisible">
      <label for="thinking-model" class="text-xs font-medium text-slate-600">拆解方式</label>
      <select
        id="thinking-model"
        v-model="thinkingModel"
        :disabled="generating"
        title="按这种方式拆解主题"
        class="rounded-md border border-slate-300 p-2 text-sm outline-none focus:border-slate-500 disabled:opacity-50"
      >
        <option v-for="item in THINKING_MODEL_OPTIONS" :key="item.value" :value="item.value">{{ item.label }}</option>
      </select>
      <p class="text-xs leading-relaxed text-slate-400">按这种方式拆解主题</p>
    </template>
    <template v-if="createOptionsVisible">
      <label for="diagram-type" class="text-xs font-medium text-slate-600">图型</label>
      <select
        id="diagram-type"
        v-model="diagramType"
        :disabled="generating"
        title="这张图要画成什么形状"
        class="rounded-md border border-slate-300 p-2 text-sm outline-none focus:border-slate-500 disabled:opacity-50"
      >
        <option v-for="item in DIAGRAM_TYPE_OPTIONS" :key="item.value" :value="item.value">{{ item.label }}</option>
      </select>
      <!-- 说明必须跟着选项变：图型不是自解释的。选「鱼骨图」却按话题分六支的人
           会以为模型没听懂他的话，这一行就是"这个选项到底改了什么"。 -->
      <p class="text-xs leading-relaxed text-slate-400">{{ diagramHint }}</p>
    </template>
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
          <!-- 反向引用：这份材料被哪些节点用上了。没有导图时不提，否则等于在指责用户还没做的事。 -->
          <template v-if="showRefState">
            <details v-if="refCount(item.source.id)" class="mt-1 text-xs text-slate-600">
              <summary class="cursor-pointer">被 {{ refCount(item.source.id) }} 个节点引用</summary>
              <ul class="mt-1 flex flex-col gap-1">
                <li v-for="hit in refHits(item.source.id)" :key="hit.id">
                  <button
                    type="button"
                    class="break-words text-left underline decoration-slate-300 hover:text-slate-900"
                    :title="hit.path.join(' / ')"
                    @click="store.locate(hit.id)"
                  >{{ hit.label }}</button>
                </li>
              </ul>
            </details>
            <p v-else class="mt-1 text-xs leading-relaxed text-amber-700">
              这份来源没有被任何节点引用——生成时可能没用上。
            </p>
          </template>
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
  </div>
</template>
