<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';

import MindMapCanvas from './canvas/MindMapCanvas.vue';
import type { Direction } from './canvas/layout/tidy';
import type { SampleMap } from './dev/samples';
import { displayBody, useMindMapStore } from './stores/mindmap';

const store = useMindMapStore();

const prompt = ref('');
const copied = ref('');

// 开发期示例选择器：只在 dev 服务器下加载，生产构建里这段会被整体去掉。
const samples = ref<{ id: string; label: string }[]>([]);
let sampleMaps: SampleMap[] = [];

if (import.meta.env.DEV) {
  void import('./dev/samples').then(mod => {
    sampleMaps = mod.SAMPLE_MAPS;
    samples.value = mod.SAMPLE_MAPS.map(({ id, label }) => ({ id, label }));
    // 方便复现与截图：地址栏加 ?sample=down 就直接装那个示例。
    const wanted = new URLSearchParams(window.location.search).get('sample');
    const initial = sampleMaps.find(item => item.id === wanted);
    if (initial) store.preview(initial.content, initial.direction);
  });
}

function loadSample(id: string) {
  const found = sampleMaps.find(item => item.id === id);
  if (found) store.preview(found.content, found.direction);
}

const directions: { value: Direction; label: string }[] = [
  { value: 'right', label: '向右' },
  { value: 'left', label: '向左' },
  { value: 'down', label: '向下' },
  { value: 'both', label: '双向' },
];

const selected = computed(() => store.selectedNode);
const pathText = computed(() => store.breadcrumb.map(node => node.label.trim()).join(' / '));

async function generate() {
  // 生成会替换整棵树（并清掉 mapId，避免之后保存覆盖上一张图），先确认不丢未保存的修改。
  if (!confirmDiscard('重新生成')) return;
  await store.generate(prompt.value);
}

/** 选完文件立刻上传解析，不再要第二次确认；change 里拿不到文件说明用户取消了选择。 */
async function onPickFile(event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  // 清掉 value：否则再选同一个文件时 change 不会触发，看起来像"点了没反应"。
  input.value = '';
  if (!file) return;
  // 上传成功同样会整体替换画布上的树，和「生成导图」一样先确认能不能丢掉未保存的修改。
  if (!confirmDiscard('上传文档生成导图')) return;
  // 解析 + 生成可能几十秒，这期间用户还能改画布。结果回来时如果发现改过，
  // 交给用户决定要不要替换——静默覆盖等于把这段时间的工作丢掉。
  await store.uploadAndGenerate(file, {
    confirmReplace: reason =>
      window.confirm(
        reason === 'switched'
          ? '生成结果已就绪，但你已经切换到另一张导图。\n用新生成的导图替换当前画布吗？取消将保留当前内容。'
          : '生成结果已就绪，但等待期间你修改过当前导图。\n用新生成的导图替换你的修改吗？取消将保留你的修改。',
      ),
  });
}

/** 标题栏右侧的保存状态：说清楚"有没有没存的东西"，而不是只显示一个保存按钮。 */
const saveHint = computed(() => {
  if (store.saving) return '保存中…';
  if (!store.content) return '';
  if (store.dirty) return '有未保存的修改';
  return store.savedAt ? '已保存' : '';
});

function saveNow() {
  // 正在输入的标题或节点还没提交，文字只在输入框里。直接保存存的是旧内容，
  // 界面却显示"已保存"，刷新后那段输入就没了。先让它失焦提交一次——
  // 标题走 @change、节点走 @blur，两者都会在 blur 时把值落到 content 上。
  const active = document.activeElement;
  if (active instanceof HTMLElement && active !== document.body) {
    active.blur();
  }
  void store.save();
}

/** 打开/新建都会丢掉当前这张图上的未保存修改，先问一句。 */
function confirmDiscard(action: string): boolean {
  if (!store.dirty) return true;
  return window.confirm(`当前有未保存的修改，${action}会丢掉这些修改，继续？`);
}

function newMap() {
  if (!confirmDiscard('新建导图')) return;
  store.newMap();
}

async function openSaved(id: string) {
  if (!confirmDiscard('打开另一张导图')) return;
  await store.openMap(id);
}

function formatTime(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString('zh-CN', { hour12: false });
}

/** Ctrl/Cmd+S 保存。画布那边的键盘处理也会收到事件，但 S 不在它的分支里，不冲突。 */
function onKeydown(event: KeyboardEvent) {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
    event.preventDefault();
    saveNow();
  }
}

/** 有未保存的修改时，关标签页/刷新要弹浏览器自己的确认框。 */
function onBeforeUnload(event: BeforeUnloadEvent) {
  // 除了脏标记，还要看有没有"输入到一半"的文字：标题草稿和节点草稿都不改
  // content，dirty 是 false，但那些字一样会丢。
  const hasDraft = store.editingId !== null || store.titleDraft !== null;
  if (!store.dirty && !hasDraft) return;
  event.preventDefault();
  event.returnValue = '';
}

onMounted(() => {
  window.addEventListener('keydown', onKeydown);
  window.addEventListener('beforeunload', onBeforeUnload);
});

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown);
  window.removeEventListener('beforeunload', onBeforeUnload);
});

/** 标题用 change 提交（失焦或回车时），不在每次按键上都记一步撤销。 */
function commitTitle(event: Event) {
  store.renameTitle((event.target as HTMLInputElement).value);
}

/**
 * 标题框走 `:value` + `@change`，打字期间 content 不会变。把草稿同步给 store，
 * 上传生成完成时才知道"这里还有没提交的输入"，否则标题会被静默换掉。
 */
function onTitleInput(event: Event) {
  store.setTitleDraft((event.target as HTMLInputElement).value);
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
      <div class="flex items-center gap-3">
        <h1 class="text-sm font-semibold">生成式 AI 思维导图</h1>
        <div v-if="samples.length" class="flex items-center gap-1 text-xs text-slate-500">
          <span class="mr-1">布局示例</span>
          <button
            v-for="item in samples"
            :key="item.id"
            type="button"
            class="rounded border border-slate-200 px-2 py-0.5 hover:bg-slate-100"
            @click="loadSample(item.id)"
          >
            {{ item.label }}
          </button>
        </div>
      </div>
      <div class="flex items-center gap-3 text-xs text-slate-500">
        <span v-if="saveHint" :class="store.dirty ? 'text-amber-600' : 'text-slate-400'">
          {{ saveHint }}
        </span>
        <button
          type="button"
          class="rounded-md bg-slate-900 px-3 py-1.5 text-xs text-white disabled:opacity-40"
          :disabled="!store.content || store.saving"
          title="保存（Ctrl+S）"
          @click="saveNow"
        >
          保存
        </button>
        <button
          type="button"
          class="rounded-md border border-slate-300 px-3 py-1.5 text-xs text-slate-700 disabled:opacity-40"
          :disabled="store.listBusy"
          @click="store.openList()"
        >
          打开
        </button>
        <button
          type="button"
          class="rounded-md border border-slate-300 px-3 py-1.5 text-xs text-slate-700"
          @click="newMap"
        >
          新建
        </button>
        <p>
          <span v-if="store.mode === 'demo'" class="rounded bg-amber-100 px-2 py-0.5 text-amber-700">
            演示模式（非 AI 生成）
          </span>
          <span v-else-if="store.mode === 'model'" class="rounded bg-emerald-100 px-2 py-0.5 text-emerald-700">
            模型生成
          </span>
          <span v-else-if="store.mode === 'saved'" class="rounded bg-slate-100 px-2 py-0.5 text-slate-600">
            已保存导图
          </span>
          <span v-else>未生成</span>
          <span class="ml-3">{{ store.nodeCount }} 个节点</span>
        </p>
      </div>
    </header>

    <main class="flex min-h-0 flex-1">
      <section class="flex w-80 shrink-0 flex-col gap-3 overflow-y-auto border-r border-slate-200 bg-white p-4">
        <div class="flex flex-col gap-1">
          <label for="title" class="text-xs font-medium text-slate-600">导图标题</label>
          <input
            v-if="store.content"
            id="title"
            :value="store.content.title"
            class="rounded-md border border-slate-300 px-2 py-1.5 text-sm outline-none focus:border-slate-500"
            @input="onTitleInput"
            @change="commitTitle"
            @blur="store.setTitleDraft(null)"
          />
          <p v-else class="text-xs text-slate-400">生成或新建之后可以改标题</p>
        </div>

        <label for="prompt" class="text-xs font-medium text-slate-600">输入主题或文本</label>
        <textarea
          id="prompt"
          v-model="prompt"
          rows="6"
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
            @click="store.cancel(true)"
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

        <div class="flex items-center gap-2 text-xs">
          <span class="h-px flex-1 bg-slate-200" />
          <span class="text-slate-400">或者上传文档</span>
          <span class="h-px flex-1 bg-slate-200" />
        </div>

        <div class="flex flex-col gap-2">
          <label
            class="cursor-pointer rounded-md border border-dashed border-slate-300 px-3 py-2 text-center text-xs text-slate-600 hover:border-slate-400 hover:bg-slate-50 focus-within:border-slate-500 focus-within:ring-2 focus-within:ring-slate-200"
            :class="store.uploadBusy ? 'pointer-events-none opacity-60' : ''"
          >
            <input
              type="file"
              class="sr-only"
              accept=".pdf,.docx,.doc,.pptx,.ppt,.xlsx,.xls,.html,.htm,.md,.asciidoc,.adoc"
              :disabled="store.uploadBusy || store.busy"
              @change="onPickFile"
            />
            {{ store.uploadBusy ? '处理中…' : '选择文件：PDF / Word / PPT / Excel / HTML' }}
          </label>
          <p class="text-xs leading-relaxed text-slate-400">
            选完自动上传解析，上限 10MB。PDF 逐页识别，扫描件也能读，最多 20 页。
          </p>

          <p v-if="store.uploadState === 'parsing'" class="text-xs text-slate-600">
            正在解析…已等待 {{ store.parseSeconds }} 秒
          </p>
          <p v-if="store.uploadState === 'parsing'" class="text-xs leading-relaxed text-slate-400">
            PDF 会逐页识别，页数越多越慢（约每页 5–10 秒，最多 20 页），请先别关页面。
          </p>
          <p v-if="store.uploadState === 'generating'" class="text-xs text-slate-600">
            正在生成导图…
          </p>

          <p
            v-if="store.truncateHint"
            role="alert"
            class="rounded-md border border-amber-300 bg-amber-50 px-2 py-1.5 text-xs leading-relaxed text-amber-800"
          >
            {{ store.truncateHint }}
          </p>

          <p
            v-if="store.uploadError"
            role="alert"
            class="whitespace-pre-wrap break-words rounded-md border border-rose-200 bg-rose-50 px-2 py-1.5 text-xs leading-relaxed text-rose-700"
          >
            {{ store.uploadError }}
          </p>
        </div>

        <p class="mt-auto text-xs leading-relaxed text-slate-400">
          双击节点改文字，Tab 加子节点，Enter 加兄弟，Delete 删除，Ctrl+Z 撤销、Ctrl+Shift+Z 重做。
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
          <span class="mx-1 w-px bg-slate-200" />
          <button
            type="button"
            class="rounded px-2 py-1 text-xs text-slate-600 hover:bg-slate-100 disabled:opacity-40 disabled:hover:bg-transparent"
            :disabled="!store.canUndo"
            title="撤销（Ctrl+Z）"
            @click="store.undo()"
          >
            撤销
          </button>
          <button
            type="button"
            class="rounded px-2 py-1 text-xs text-slate-600 hover:bg-slate-100 disabled:opacity-40 disabled:hover:bg-transparent"
            :disabled="!store.canRedo"
            title="重做（Ctrl+Shift+Z）"
            @click="store.redo()"
          >
            重做
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
          <div class="grid grid-cols-2 gap-2">
            <button
              type="button"
              class="rounded-md border border-slate-300 px-3 py-1.5 text-xs text-slate-700 hover:bg-slate-50"
              @click="store.beginEdit(selected.node.id)"
            >
              改文字（F2）
            </button>
            <button
              type="button"
              class="rounded-md border border-slate-300 px-3 py-1.5 text-xs text-slate-700 hover:bg-slate-50"
              @click="store.addChildTo(selected.node.id)"
            >
              加子节点（Tab）
            </button>
            <button
              type="button"
              class="rounded-md border border-slate-300 px-3 py-1.5 text-xs text-slate-700 hover:bg-slate-50"
              @click="store.addSiblingTo(selected.node.id)"
            >
              加兄弟（Enter）
            </button>
            <button
              type="button"
              class="rounded-md border border-slate-300 px-3 py-1.5 text-xs text-slate-700 hover:bg-slate-50 disabled:opacity-40"
              :disabled="selected.node.id === store.content?.root.id"
              @click="store.deleteNode(selected.node.id)"
            >
              删除（Delete）
            </button>
          </div>
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
            点击画布上的任意节点查看完整内容与它所在的层级路径；双击节点可以直接改文字。
          </p>
        </template>
      </aside>
    </main>

    <div
      v-if="store.listOpen"
      class="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/30 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="list-title"
    >
      <div class="flex max-h-[70vh] w-full max-w-lg flex-col rounded-lg border border-slate-200 bg-white p-5 shadow-xl">
        <div class="flex items-center justify-between">
          <h2 id="list-title" class="text-sm font-semibold">已保存的导图</h2>
          <button type="button" class="text-xs text-slate-500 hover:text-slate-900" @click="store.closeList()">
            关闭
          </button>
        </div>
        <p v-if="store.listBusy" class="mt-3 text-xs text-slate-400">正在读取…</p>
        <p v-else-if="store.maps.length === 0" class="mt-3 text-xs text-slate-400">
          还没有保存过任何导图。
        </p>
        <ul v-else class="mt-3 flex-1 divide-y divide-slate-100 overflow-y-auto">
          <li
            v-for="item in store.maps"
            :key="item.id"
            class="flex items-center justify-between gap-3 py-2"
          >
            <div class="min-w-0">
              <p class="truncate text-sm text-slate-800">{{ item.content.title }}</p>
              <p class="text-xs text-slate-400">
                版本 {{ item.version }} · {{ item.content.root.children.length }} 个一级分支 ·
                {{ formatTime(item.updated_at) }}
              </p>
            </div>
            <button
              type="button"
              class="shrink-0 rounded-md border border-slate-300 px-3 py-1 text-xs text-slate-700 hover:bg-slate-50"
              @click="openSaved(item.id)"
            >
              打开
            </button>
          </li>
        </ul>
      </div>
    </div>

    <div
      v-if="store.conflict"
      class="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/30 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="conflict-title"
    >
      <div class="w-full max-w-md rounded-lg border border-slate-200 bg-white p-5 shadow-xl">
        <h2 id="conflict-title" class="text-sm font-semibold">保存冲突</h2>
        <p class="mt-2 text-xs leading-relaxed text-slate-600">
          这张导图在别处被改过（{{ store.conflict.message }}）。你本地的修改还没有保存，选择怎么处理：
        </p>
        <div class="mt-4 flex flex-col gap-2">
          <button
            type="button"
            class="rounded-md border border-slate-300 px-3 py-2 text-left text-xs text-slate-700 hover:bg-slate-50"
            @click="store.reloadFromServer()"
          >
            放弃本地修改，加载服务端最新版本
          </button>
          <button
            type="button"
            class="rounded-md border border-slate-300 px-3 py-2 text-left text-xs text-slate-700 hover:bg-slate-50"
            @click="store.saveAsNew()"
          >
            保留本地修改，另存为新导图
          </button>
          <button
            type="button"
            class="rounded-md px-3 py-2 text-left text-xs text-slate-500 hover:bg-slate-50"
            @click="store.dismissConflict()"
          >
            取消，稍后再说
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
