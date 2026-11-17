<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';

import MindMapCanvas from './canvas/MindMapCanvas.vue';
import type { Direction } from './canvas/layout/tidy';
import AgentPanel from './components/AgentPanel.vue';
import SourceInputs from './components/SourceInputs.vue';
import { SOURCE_LABELS } from './sources/input';
import { ApiError, exportFilename, exportLabel, exportMindMap, type ExportFormat } from './api/client';
import type { SampleMap } from './dev/samples';
import { displayBody, bodySource, useMindMapStore } from './stores/mindmap';
import { useSourcesStore } from './stores/sources';
import { resolveRefs, type ResolvedRef } from './mindmap/refs';
import { EDGE_KINDS, EDGE_KIND_LABELS, NODE_COLORS, NODE_COLOR_LABELS } from './mindmap/edits';
import type { EdgeKind } from './api/types';

const store = useMindMapStore();
const sources = useSourcesStore();

/**
 * 选中节点的引用，解析成"能直接画"的形状。
 *
 * 原文取自 sources store：本次会话里解析过的来源才能回查，从已保存导图打开时
 * readySources 是空的，解析结果里 source 为 null，抽屉会据此降级成"只看摘录"。
 */
const selectedRefs = computed(() =>
  resolveRefs(store.selectedRefs, sources.readySources, store.content?.source_usage ?? []),
);

/** 正在右侧抽屉里查看的引用；null 表示抽屉关着。 */
const activeRef = ref<ResolvedRef | null>(null);

const copied = ref('');

/**
 * 屏幕底部的短暂提示。
 *
 * 导出的结果原来只写进左侧来源面板的 `store.status` 小字里，位置在折叠区下方，
 * 失败时看上去就是"点了没反应"（用户实测：控制台 422，界面无提示）。成功和失败
 * 都要能一眼看到，所以单独开一条浮层，四秒后自动消失。
 */
const notice = ref<{ kind: 'ok' | 'error'; text: string } | null>(null);
let noticeTimer: ReturnType<typeof setTimeout> | null = null;

function flash(kind: 'ok' | 'error', text: string): void {
  if (noticeTimer) clearTimeout(noticeTimer);
  notice.value = { kind, text };
  noticeTimer = setTimeout(() => {
    notice.value = null;
    noticeTimer = null;
  }, kind === 'error' ? 6000 : 3000);
}

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

/**
 * 「节点详情」分区的正文。
 *
 * 取值来自 `displayBody`（`detail → note → label`），与卡片、导出、复制全文同一个
 * 函数——四处各写一遍回退链，迟早出现"卡片里有正文、面板里没有"这种说不清的分歧。
 *
 * `bodyFrom` 决定那行小字怎么写。它存在的理由：旧图没有 `detail`，回退到 `note`
 * 之后界面上那段文字**同时也是「我的备注」输入框里的内容**，不说明来源的话，用户
 * 会以为生成说明了什么，其实那是他自己以前写的备注。
 */
const bodyText = computed(() => (selected.value ? displayBody(selected.value.node) : ''));
const bodyFrom = computed(() => (selected.value ? bodySource(selected.value.node) : 'label'));
const BODY_SOURCE_NOTE: Record<string, string> = {
  detail: '这次生成写的说明',
  note: '这个节点没有生成详情，下面是你以前写的备注',
  label: '这个节点没有正文，下面是标题全文',
};

/**
 * 正文编辑框的草稿。
 *
 * 和标题一样不逐键写回 content：每敲一个字都进撤销栈的话，撤销一次只退一个字符。
 * 选中变化时重新取一次，避免把上一个节点的正文带到另一个节点上。
 */
const noteTargetId = ref<string | null>(null);
const noteDraft = ref('');

watch(
  () => [selected.value?.node.id ?? null, selected.value?.node.note ?? ''] as const,
  ([id, note]) => {
    noteTargetId.value = id;
    noteDraft.value = note;
  },
  { immediate: true },
);

const noteLength = computed(() => Array.from(noteDraft.value).length);

function commitNoteDraft() {
  const id = noteTargetId.value;
  if (!id) return;
  store.commitNote(id, noteDraft.value);
  // 提交后以 store 里的值为准：空正文会被归一成 null，草稿也要跟着归一，
  // 否则框里留着空格、界面上却显示"没有正文"。
  noteDraft.value = store.selectedNode?.node.note ?? '';
}

function resetNoteDraft() {
  noteDraft.value = store.selectedNode?.node.note ?? '';
}

// ---- 附加关系（流程图的条件分支 / 汇合 / 回退）--------------------------------
//
// 最小可用入口，三条理由让它必须存在：
//   1. 模型只在 `flowchart` 生成时给边，用户手里一张 `tree` 图永远补不出流程；
//   2. 模型给的边可能是错的，而"删掉那条画错的回退线"没有别的入口；
//   3. 边不参与布局，所以增删它不会重排画布——这件事只有在界面上点一次才看得出。

/** 新边连到哪个节点（下拉框的值）。默认空：用户得自己挑，不替他猜。 */
const edgeTarget = ref('');
const edgeLabel = ref('');
const edgeKind = ref<EdgeKind>('branch');

/** 候选目标：当前树里除选中节点之外的所有节点。自环在契约层就是硬拒的，列出来也没用。 */
const edgeTargets = computed(() => {
  const current = selected.value?.node.id;
  return store.flatNodes
    .filter(entry => entry.node.id !== current)
    .map(entry => ({ id: entry.node.id, label: entry.node.label.trim(), depth: entry.depth }));
});

/** 边端点的显示名：`id → 标题`。删边/列边都要它，用户认标题不认 id。 */
const edgeNodeName = computed(() => {
  const names = new Map(store.flatNodes.map(entry => [entry.node.id, entry.node.label.trim()]));
  return (id: string) => names.get(id) ?? id;
});

/** 选中节点相关的边，拼成"谁 → 谁（标签）"给列表用。 */
const selectedEdgeRows = computed(() => store.selectedEdges.map(edge => ({
  id: edge.id,
  text: `${edgeNodeName.value(edge.source)} → ${edgeNodeName.value(edge.target)}`,
  label: edge.label ?? '',
  kind: edge.kind ?? 'flow',
})));

const edgeHint = computed(() => {
  if (!selected.value) return '';
  if (!store.content) return '先生成或打开一张导图。';
  if (store.busy) return '正在生成，完成之后才能加关系。';
  if (store.contentEdges.length >= 200) return '附加关系已到上限（200 条），请先删掉一些。';
  if (!edgeTarget.value) return '选一个终点节点，再点「连一条关系」。';
  return '关系只画连线，不会改变节点位置。';
});

function connectEdge() {
  const source = selected.value?.node.id;
  const target = edgeTarget.value;
  if (!source || !target) return;
  if (store.connectNodes(source, target, edgeLabel.value, edgeKind.value)) {
    edgeLabel.value = '';
    edgeTarget.value = '';
  }
}

function disconnectEdge(edgeId: string) {
  store.deleteEdge(edgeId);
}

// 换选中节点时清掉上一轮的目标：留着它会让"给甲连到乙"在切到丙之后变成
// "给丙连到乙"，而用户以为自己点的还是甲。
watch(() => selected.value?.node.id ?? null, () => {
  edgeTarget.value = '';
  edgeLabel.value = '';
});

/**
 * 「展开这一支」按钮的状态。
 *
 * 三件事一起决定它能不能点，缺一条都会让用户白花一次模型调用：
 *   - `expandable === 'no'`：后端/模型已经说过到底了，按钮禁用并**说明原因**
 *     （灰着不解释，用户只会以为功能坏了）；
 *   - 正在展开：转成「展开中…」，同时禁用——重复点击会发两次请求，而第二次
 *     会被 `runGeneration` 的闸门静默丢掉，看起来像"点了没反应"；
 *   - 正在生成 / 编辑：互斥。两轮 agent 运行重叠时后到的那个会清掉前一个的预览
 *     与候选，用户看到的是"之前的进度消失了"。
 *
 * `unknown` **不禁用**：那正是"旧图，或者上次被额度截断"的情况，按钮可点才有
 * 意义——把不确定当成"到头了"，唯一能继续长的地方就灰掉了。旧图没有这个字段
 * （`undefined`）时与 `unknown` 同义：它从来没被问过，而不是被问过说"没有"。
 */
const expandState = computed(() => {
  const node = selected.value?.node;
  if (!node) return { disabled: true, label: '展开这一支', hint: '' };
  const done = node.expandable === 'no';
  const busyElsewhere = store.agentRunning || store.busy;
  let hint = '让模型给这个节点补 2–4 个子节点；结果先落候选，确认后才进画布。';
  if (done) {
    hint = '已经到底了：模型说过这个节点没有更细的内容可展开。';
  } else if (store.expanding) {
    hint = '正在展开这一支，新节点会先出现在候选里。';
  } else if (store.generating) {
    hint = '正在生成，先等这一次跑完再展开。';
  } else if (node.expandable !== 'yes') {
    hint = '不确定还能不能再拆（旧图、或上次是被额度截断的）；可以试一次。';
  }
  return {
    disabled: done || busyElsewhere,
    label: store.expanding ? '展开中…' : '展开这一支',
    hint,
  };
});

function expandBranch() {
  // 材料正文只在本次会话内存里；重新打开旧图时它是空的，后端会按节点的
  // `source_refs` 从知识库把来源找回来，所以这里照传、传空也不拦。
  void store.expandSelected(sources.readySources);
}

/**
 * 画布上方的「改这张图」输入框。
 *
 * 与左侧「输入主题或文本」是两个入口、两件事：那个既是主题输入框又会顺手把文本
 * 加进来源列表，语义上更接近"新建"；用户想把「把第三支拆细一点」这种**针对现有图
 * 的指令**说清楚时，在那个框里打字的心理预期是"加材料"。所以这里单独开一个：
 * 标题写明意图（edit），发出去的 `intent` 也是 edit。
 *
 * 后端 `edit` 意图给的是四个细粒度工具 + 批量 `mindmap_edit`，改动只落在候选上，
 * 用户点「应用」才进画布——与展开、生成完全同一条确认流程。
 */
const editPrompt = ref('');
const editBlocked = computed(() => !store.content || store.busy || store.agentRunning);

const editHint = computed(() => {
  if (!store.content) return '先生成或打开一张导图，再让它改。';
  if (store.busy || store.agentRunning) return '正在跑，等这一次结束。';
  return '改动只落候选，确认后才进画布。';
});

const editGoalDisabled = computed(() => editBlocked.value || !editPrompt.value.trim());

async function submitEdit() {
  const goal = editPrompt.value.trim();
  if (!goal || editBlocked.value) return;
  editPrompt.value = '';
  // 材料照传：edit 路径不读来源，但后端会按节点的 `source_refs` 找回原文，
  // 与展开那条路同一份处理（`tools._expand_sources`）。
  await store.runGeneration(goal, 'edit', sources.readySources);
}

/** 详情面板的「样式」两个控件：颜色是下拉（只列调色板），图标是一个输入框。 */
function changeColor(event: Event) {
  const id = selected.value?.node.id;
  if (!id) return;
  const value = (event.target as HTMLSelectElement).value;
  store.setNodeVisualById(id, { color: value ? (value as (typeof NODE_COLORS)[number]) : null });
}

function changeIcon(event: Event) {
  const id = selected.value?.node.id;
  if (!id) return;
  const value = (event.target as HTMLInputElement).value;
  store.setNodeVisualById(id, { icon: value });
  // 以 store 归一后的值为准（空串→清空、超长→截断），否则框里留着空格，
  // 界面上却显示"没有图标"——与备注框同一条处理。
  (event.target as HTMLInputElement).value = store.selectedNode?.node.icon ?? '';
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

const exporting = ref(false);
// 必须由 Vue 管住选中值：把占位项写成 disabled 时，浏览器会把第一个可用项
// （PlantUML）当成初始选中，用户第一次点它就等于"值没变"，change 不触发，
// 表现为点了没反应。
const exportChoice = ref<ExportFormat | ''>('');

/** 浏览器端落盘：三种导出都是纯文本，不需要后端再跑一趟。 */
function saveTextFile(filename: string, text: string): void {
  const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

async function onExport(): Promise<void> {
  const format = exportChoice.value;
  // 复位，否则连着导同一种格式时 change 不会再次触发
  exportChoice.value = '';
  const content = store.content;
  if (!content || !format) return;

  exporting.value = true;
  try {
    const text = await exportMindMap(format, content);
    saveTextFile(exportFilename(format, content.title), text);
    store.status = `已导出 ${exportLabel(format)}（${content.title}）`;
    flash('ok', store.status);
  } catch (error) {
    const message = error instanceof ApiError ? error.message : '导出失败，请重试。';
    store.status = message;
    flash('error', `导出失败：${message}`);
  } finally {
    exporting.value = false;
  }
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

/**
 * 「上次的运行结果」这条提示的文案。
 *
 * 只给时间与目标，不给整棵树：这是一句通知，用户要不要捡回来是他的决定。两种状态
 * 分开说——"完成了但没应用"和"被中断了但已经产出了一部分"对他的意义完全不同，
 * 用同一句话糊过去，他会以为后者是一次完整的结果。
 */
const resumableText = computed(() => {
  const run = store.resumableRun;
  if (!run) return '';
  const date = new Date(run.updated_at);
  const clock = Number.isNaN(date.getTime())
    ? ''
    : date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false });
  const when = clock ? `（${clock}）` : '';
  const what = run.status === 'cancelled'
    ? `上次${when}那次生成被中断了，已经生成的部分还留着`
    : `上次${when}有一次生成完成了，但还没有应用到画布`;
  return run.goal ? `${what}：「${run.goal}」` : `${what}。`;
});

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
  if (noticeTimer) clearTimeout(noticeTimer);
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
        <select
          v-model="exportChoice"
          class="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-xs text-slate-700 disabled:opacity-40"
          :disabled="!store.content || exporting"
          title="导出为文本格式"
          @change="onExport"
        >
          <option value="" hidden>{{ exporting ? '导出中…' : '导出' }}</option>
          <option value="puml">PlantUML</option>
          <option value="markdown">Markdown</option>
          <option value="opml">OPML（XMind）</option>
        </select>
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
          <label for="title" class="text-xs font-medium text-slate-600">
            导图标题 <span class="font-normal text-slate-400">· 只改名，不重新生成</span>
          </label>
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

        <SourceInputs :confirm-discard="confirmDiscard" />

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
        <!--
          画布上方的「改这张图」入口。**专门开一个输入框**而不是复用左侧那个：
          左侧「输入主题或文本」同时会把文字加进来源列表，在那里打「把第三支拆细
          一点」会被当成材料。这一个只发 `intent=edit`，改动落候选，确认后才进画布。
        -->
        <div
          class="absolute right-4 top-4 z-10 flex w-[380px] max-w-[calc(100%-2rem)] flex-col gap-1 rounded-md border border-slate-200 bg-white p-2 shadow-sm"
          role="group"
          aria-label="改这张图"
        >
          <div class="flex items-center gap-1">
            <span class="shrink-0 text-xs font-medium text-slate-600">改这张图</span>
            <input
              v-model="editPrompt"
              aria-label="改这张图"
              class="min-w-0 flex-1 rounded border border-slate-300 px-2 py-1 text-xs outline-none focus:border-slate-500 disabled:opacity-50"
              :disabled="editBlocked"
              placeholder="把第三支拆细一点"
              @keydown.enter.prevent="submitEdit"
            />
            <button
              type="button"
              class="shrink-0 rounded-md bg-slate-900 px-2 py-1 text-xs text-white disabled:opacity-40"
              :disabled="editGoalDisabled"
              @click="submitEdit"
            >
              {{ store.agentRunning && store.runIntent === 'edit' ? '改中…' : '让它改' }}
            </button>
          </div>
          <p class="text-[11px] leading-tight text-slate-400">{{ editHint }}</p>
        </div>
        <MindMapCanvas />
      </section>

      <aside class="flex w-80 shrink-0 flex-col gap-3 overflow-y-auto border-l border-slate-200 bg-white p-4">
        <!--
          上次运行留下的结果。**只是一条提示，不是弹窗**：用户可能正在做别的事，
          打断他比让他知道这件事更烦。点「查看」把那份候选载入现有的确认流程
          （应用/丢弃、过时检查、整树校验一步都不少），点「丢弃」就再也不问。
        -->
        <section
          v-if="store.resumableRun"
          aria-label="上次的运行结果"
          class="rounded-md border border-sky-200 bg-sky-50 p-2 text-xs"
        >
          <p class="break-words leading-relaxed text-slate-700">{{ resumableText }}</p>
          <div class="mt-1.5 flex gap-2">
            <button
              type="button"
              class="rounded-md border border-sky-300 bg-white px-2 py-1 text-sky-800 hover:bg-sky-100"
              @click="store.resumeRun()"
            >
              查看
            </button>
            <button
              type="button"
              class="rounded-md border border-slate-300 bg-white px-2 py-1 text-slate-600 hover:bg-slate-50"
              @click="store.dismissResumable()"
            >
              丢弃
            </button>
          </div>
        </section>
        <AgentPanel />
        <template v-if="selected">
          <p class="text-xs text-slate-400">{{ pathText }}</p>
          <!-- 标题：与画布上那颗节点是同一段文字（双击节点改的就是它） -->
          <h2 class="text-sm font-semibold">
            {{ selected.node.label.trim() }}
          </h2>

          <!--
            详情分区（只读）：生成产出的正文。
            `detail` 是模型写的，界面不提供编辑入口——要改内容就重新生成，
            或者写到下面的「我的备注」里。两个字段分开存，正是为了这两种意图
            不会互相覆盖。
          -->
          <section class="flex flex-col gap-1" aria-label="节点详情">
            <h3 class="text-xs font-medium text-slate-500">详情</h3>
            <p class="whitespace-pre-wrap break-words rounded-md bg-slate-50 px-2 py-1.5 text-sm leading-relaxed text-slate-700">{{ bodyText }}</p>
            <p class="text-xs text-slate-400">{{ BODY_SOURCE_NOTE[bodyFrom] }}</p>
          </section>

          <!--
            按需展开：初次只出 4 层，往下的内容由用户点哪个节点就展开哪个。
            走的是与生成同一条 `/api/agent/runs/stream`（intent=expand），
            结果同样是**候选**——确认与版本检查一步都没少。
          -->
          <section class="flex flex-col gap-1" aria-label="展开这一支">
            <button
              type="button"
              class="rounded-md border border-slate-300 px-3 py-1.5 text-xs text-slate-700 hover:bg-slate-50 disabled:opacity-40 disabled:hover:bg-transparent"
              :disabled="expandState.disabled"
              @click="expandBranch"
            >
              {{ expandState.label }}
            </button>
            <p class="text-xs leading-relaxed text-slate-400">{{ expandState.hint }}</p>
          </section>

          <!--
            样式：语义色 + 图标。两个值都可选、缺省为空（"默认"）。
            颜色只给调色板里的键——不是取色器。理由与契约那边一致：任意 hex 会
            得到一堆脏色，而且换主题（暗色/亮色）时无法自动适配；键是语义，
            色值在 CSS 变量里（`styles.css`），换主题只改那一处。
            图标是**一个 emoji**：前端没有图标库，为一个纯装饰字段新增依赖不划算。
          -->
          <section class="flex flex-col gap-1" aria-label="节点样式">
            <h3 class="text-xs font-medium text-slate-500">样式</h3>
            <div class="flex items-center gap-2">
              <select
                aria-label="节点颜色"
                class="min-w-0 flex-1 rounded-md border border-slate-300 bg-white px-2 py-1.5 text-xs text-slate-700"
                :value="selected.node.color ?? ''"
                @change="changeColor"
              >
                <option value="">默认（无色）</option>
                <option v-for="key in NODE_COLORS" :key="key" :value="key">
                  {{ NODE_COLOR_LABELS[key] }}
                </option>
              </select>
              <input
                aria-label="节点图标"
                class="w-16 rounded-md border border-slate-300 px-2 py-1.5 text-center text-xs text-slate-700"
                :value="selected.node.icon ?? ''"
                :maxlength="16"
                placeholder="🎯"
                title="一个 emoji；清空表示去掉图标"
                @change="changeIcon"
              />
            </div>
            <p class="text-[11px] leading-relaxed text-slate-400">
              颜色只收预设的 8 个语义色（换主题时自动适配）；图标是一个 emoji。生成时只有一级分支会带这两个值。
            </p>
          </section>

          <label class="flex flex-1 flex-col gap-1">
            <span class="text-xs text-slate-400">
              我的备注 {{ noteLength }} / 2000
              <span v-if="!(selected.node.note ?? '').trim()">· 现在是空的，不影响详情与导出</span>
            </span>
            <textarea
              v-model="noteDraft"
              aria-label="我的备注"
              class="min-h-24 flex-1 resize-y rounded-md border border-slate-200 px-2 py-1.5 text-sm leading-relaxed text-slate-700 focus:border-slate-400 focus:outline-none"
              maxlength="2000"
              placeholder="写你自己的备忘；这里的字不会被重新生成覆盖"
              @blur="commitNoteDraft"
              @keydown.esc="resetNoteDraft"
            />
          </label>

          <section v-if="selectedRefs.length" class="flex flex-col gap-2 border-t border-slate-200 pt-3">
            <h3 class="text-xs font-medium text-slate-500">来源引用（{{ selectedRefs.length }}）</h3>
            <ul class="flex flex-col gap-1.5">
              <li v-for="(item, index) in selectedRefs" :key="`${item.ref.source_id}-${index}`">
                <div class="rounded-md border border-slate-200 px-2 py-1.5 hover:border-slate-300 hover:bg-slate-50">
                  <!--
                    有原文地址的来源（外部检索的维基条目 / arXiv 论文）名字就是链接：
                    用户读完这句可以直接去核对出处，而不是只能在抽屉里看一段没有上下文的
                    摘录。没有地址时保持原来的样子（点开抽屉），不给一个点不动的假链接。
                  -->
                  <a
                    v-if="item.url"
                    :href="item.url"
                    target="_blank"
                    rel="noopener noreferrer"
                    class="block truncate text-xs text-sky-700 underline decoration-sky-300 underline-offset-2 hover:text-sky-900"
                    :title="`在新窗口打开原文：${item.url}`"
                  >
                    {{ item.name }}<span v-if="item.where" class="text-slate-500"> · {{ item.where }}</span>
                  </a>
                  <button
                    v-else
                    type="button"
                    class="w-full text-left"
                    @click="activeRef = item"
                  >
                    <span class="block truncate text-xs text-slate-600">
                      {{ item.name }}<span v-if="item.where"> · {{ item.where }}</span>
                    </span>
                  </button>
                  <button
                    type="button"
                    class="mt-0.5 block w-full text-left"
                    @click="activeRef = item"
                  >
                    <span class="line-clamp-2 block text-xs leading-relaxed text-slate-500">
                      {{ item.quote || '（这条引用没有摘录）' }}
                    </span>
                  </button>
                </div>
              </li>
            </ul>
          </section>
          <p v-else class="border-t border-slate-200 pt-3 text-xs text-slate-400">
            这个节点没有来源引用。
          </p>

          <!--
            附加关系（流程图的条件分支 / 汇合 / 回退）。

            这一段是**唯一**的手工补边入口：模型只在 flowchart 生成时给边，
            而它给错的那些（比如画到了错的出口、或者本该是顺序却标成了回退）
            没有别的地方能改。删边同样在这里——画布上点线不做选中，因为节点
            已经有选中态了，两套选中语义混在一起会让「Delete 删什么」变得不可预测。
          -->
          <section class="flex flex-col gap-2 border-t border-slate-200 pt-3" aria-label="附加关系">
            <h3 class="text-xs font-medium text-slate-500">
              附加关系（{{ selectedEdgeRows.length }}）
            </h3>
            <ul v-if="selectedEdgeRows.length" class="flex flex-col gap-1">
              <li
                v-for="row in selectedEdgeRows"
                :key="row.id"
                class="flex items-center gap-2 rounded-md border border-slate-200 px-2 py-1"
                :class="row.kind === 'loop' ? 'border-amber-200 bg-amber-50' : ''"
              >
                <span class="min-w-0 flex-1 truncate text-xs text-slate-700" :title="row.text">
                  {{ row.text }}
                  <span v-if="row.label" class="text-slate-500">（{{ row.label }}）</span>
                </span>
                <button
                  type="button"
                  class="shrink-0 rounded border border-slate-300 bg-white px-1.5 py-0.5 text-[11px] text-slate-600 hover:bg-slate-50"
                  :title="`删除这条关系（${row.id}）`"
                  @click="disconnectEdge(row.id)"
                >
                  删除
                </button>
              </li>
            </ul>
            <p v-else class="text-xs text-slate-400">
              这个节点没有附加关系。流程图里的条件分支与回退线都记在这里。
            </p>

            <div class="flex items-center gap-1">
              <select
                v-model="edgeTarget"
                aria-label="关系终点"
                class="min-w-0 flex-1 rounded-md border border-slate-300 bg-white px-2 py-1.5 text-xs text-slate-700"
              >
                <option value="">连到哪个节点…</option>
                <option v-for="item in edgeTargets" :key="item.id" :value="item.id">
                  {{ '·'.repeat(item.depth) }} {{ item.label }}
                </option>
              </select>
              <select
                v-model="edgeKind"
                aria-label="关系类型"
                class="w-24 shrink-0 rounded-md border border-slate-300 bg-white px-1 py-1.5 text-xs text-slate-700"
              >
                <option v-for="kind in EDGE_KINDS" :key="kind" :value="kind">
                  {{ EDGE_KIND_LABELS[kind] }}
                </option>
              </select>
            </div>
            <div class="flex items-center gap-1">
              <input
                v-model="edgeLabel"
                aria-label="关系说明"
                class="min-w-0 flex-1 rounded-md border border-slate-300 px-2 py-1.5 text-xs text-slate-700"
                :maxlength="40"
                placeholder="边上的文字：是 / 否 / 驳回"
              />
              <button
                type="button"
                class="shrink-0 rounded-md border border-slate-300 bg-white px-2 py-1.5 text-xs text-slate-700 hover:bg-slate-50 disabled:opacity-40"
                :disabled="!edgeTarget || store.busy"
                @click="connectEdge"
              >
                连一条关系
              </button>
            </div>
            <p class="text-[11px] leading-relaxed text-slate-400">{{ edgeHint }}</p>
          </section>
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
        <details v-if="store.content?.source_usage?.length" class="mt-auto border-t border-slate-200 pt-3 text-xs text-slate-600" open>
          <summary class="cursor-pointer font-medium">本图来源（{{ store.content.source_usage.length }}）</summary>
          <ul class="mt-2 flex flex-col gap-2">
            <li v-for="(source, index) in store.content.source_usage" :key="source.source_id" class="break-words">
              {{ SOURCE_LABELS[source.kind] }} · {{ source.filename || `来源 ${index + 1}` }} · {{ source.char_count }} 字
              <span
                v-if="store.refIndex.counts[source.source_id]"
                class="text-slate-400"
              >· 被 {{ store.refIndex.counts[source.source_id] }} 个节点引用</span>
              <span v-else class="text-amber-700">· 没有节点引用</span>
            </li>
          </ul>
          <p class="mt-2 leading-relaxed text-slate-400">生成时用到的来源（整图口径），具体出处见各节点的引用；原文未随导图保存。</p>
        </details>
      </aside>
    </main>

    <!-- 导出等一次性操作的结果浮层；错误停 6 秒，成功 3 秒。 -->
    <div
      v-if="notice"
      class="fixed bottom-6 left-1/2 z-50 max-w-[90vw] -translate-x-1/2 rounded-md px-4 py-2 text-sm shadow-lg"
      :class="notice.kind === 'error' ? 'bg-rose-600 text-white' : 'bg-slate-900 text-white'"
      :role="notice.kind === 'error' ? 'alert' : 'status'"
      aria-live="polite"
    >
      {{ notice.text }}
    </div>

    <!--
      原文抽屉：从右侧滑出，不挤掉详情面板。只有本次会话解析过的来源才有原文，
      从已保存导图打开时只能看摘录——这一点在底部明确写出来，别让用户以为功能坏了。
    -->
    <aside
      v-if="activeRef"
      class="fixed inset-y-0 right-0 z-40 flex w-[480px] max-w-full flex-col border-l border-slate-200 bg-white shadow-2xl"
      role="dialog"
      aria-modal="true"
      aria-labelledby="ref-title"
    >
      <header class="flex items-start justify-between gap-3 border-b border-slate-200 p-4">
        <div class="min-w-0">
          <h2 id="ref-title" class="truncate text-sm font-semibold">{{ activeRef.name }}</h2>
          <p class="mt-0.5 text-xs text-slate-500">{{ activeRef.where || '未标注位置' }}</p>
        </div>
        <button
          type="button"
          class="shrink-0 text-xs text-slate-500 hover:text-slate-900"
          @click="activeRef = null"
        >
          关闭
        </button>
      </header>
      <div class="flex-1 overflow-y-auto p-4">
        <p v-if="activeRef.excerpt" class="whitespace-pre-wrap break-words text-sm leading-relaxed text-slate-700">
          {{ activeRef.before }}<mark class="rounded bg-amber-200 px-0.5 text-slate-900">{{ activeRef.hit }}</mark>{{ activeRef.after }}
        </p>
        <p v-else-if="activeRef.quote" class="whitespace-pre-wrap break-words text-sm leading-relaxed text-slate-700">
          {{ activeRef.quote }}
        </p>
        <p v-else class="text-sm text-slate-400">这条引用没有摘录，也没有可回查的原文。</p>

        <p v-if="!activeRef.source" class="mt-4 rounded-md bg-slate-50 p-3 text-xs leading-relaxed text-slate-500">
          原文没有随导图保存，这里只能看到生成时记下的摘录。
          {{ activeRef.start != null ? '打开这张图时重新解析同一份来源，才能回到原文。' : '' }}
        </p>
      </div>
    </aside>

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
