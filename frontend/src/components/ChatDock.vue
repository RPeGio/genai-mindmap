<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue';

import SourceInputs from './SourceInputs.vue';
import VoicePress from './VoicePress.vue';
import { mergedCharCount, SHORT_MATERIAL_CHARS, materialTitle, SOURCE_LABELS } from '../sources/input';
import { INITIAL_STATUS, useMindMapStore } from '../stores/mindmap';
import { useGenerationOptions, DIAGRAM_TYPE_OPTIONS, THINKING_MODEL_OPTIONS } from '../stores/options';
import { usePrefsStore, DIAGRAM_PREF_KEY, EXTERNAL_SEARCH_PREF_KEY, THINKING_PREF_KEY } from '../stores/prefs';
import { useSourcesStore } from '../stores/sources';
import type { DiagramTypeChoice } from '../api/types';
import { agentDetailVisible } from '../mindmap/run-status';

/**
 * 底部对话框：**唯一**说一句话的地方。
 *
 * 它接管了原来"画布右上角那个框"的全部职责（输入、拆解方式、图型、联网检索、提交），
 * 并且把状态机显式化了——这是这一版的核心交互：
 *
 *   空闲      完整对话框（缩略图 + 输入 + 按钮行）
 *   生成中    收成一条细进度（"正在生成 · 已定好框架 · 12 秒" + 取消）
 *   生成完    淡出，只剩画布 + 悬浮球（结果进消息流）
 *   选中节点  节点旁浮出小工具条；点「改」把对话框叫回来并预填
 *   语音      输入框换成「按住说话」，右侧图标变 ⌨
 *
 * **为什么"生成完就淡出"要留三条刹车**（`shouldKeep`）：输入框里还有没发出去的字、
 * 这次失败了、或者画布上根本还没有图——这三种时候把对话框收走，用户下一步就不知道
 * 该点哪儿了。前两种是"他要接着改"，第三种是"他还什么都没做"。只有"成功且有图"
 * 那一种才真的只剩画布。
 */

const store = useMindMapStore();
const sources = useSourcesStore();
const options = useGenerationOptions();
const prefs = usePrefsStore();

const emit = defineEmits<{ detail: []; submit: [goal: string] }>();

/**
 * 静默多久之后把阶段文案换成「模型正在思考」。
 *
 * 后端只在静默达到 10 秒才发心跳，所以 `waitedMs >= 10` 基本等价于"已经静默 10 秒"。
 * 阈值再写一遍是为了前端自己也站得住：万一别处的实现改成更密的保活帧，这里不会
 * 1 秒就把阶段文案顶掉。
 */
const SILENT_HINT_SECONDS = 10;

/** 生成成功之后停多久再淡出。2 秒够看清"已落到画布"，又不至于一直挡着画布底部。 */
const FADE_DELAY_MS = 2000;

type IntentState = 'create' | 'edit-all' | 'edit-selected';

// ---------------------------------------------------------------- 输入

const intentText = ref('');
const stickyDiagram = ref(false);
const stickyThinking = ref(false);

/**
 * 偏好 → 预填。`prefs.defaults` 一变就重新填（面板里改一条，下拉跟着变）。
 *
 * `applyDefaults` 内部只填"用户没手动改过"的那两个——用户刚选过的值是他的意图，
 * 不能被一条偏好顶回去。`prefs.load()` 在 `App.onMounted` 里发起，这里跟着它的结果填。
 */
watch(() => prefs.defaults, defaults => options.applyDefaults(defaults),
  { immediate: true, deep: true });

/**
 * 「以后都这样」写进偏好。
 *
 * 必须在**发请求之前**完成：这一次请求的 `prefs_applied` 要能记上它，否则用户刚勾完
 * 就看到面板上写着"尚未生效"。
 */
async function syncStickyPrefs(): Promise<void> {
  const writes: Promise<boolean>[] = [];
  if (stickyThinking.value) {
    writes.push(prefs.save(THINKING_PREF_KEY, options.thinkingModel));
  }
  if (stickyDiagram.value && options.diagramType !== 'auto') {
    writes.push(prefs.save(DIAGRAM_PREF_KEY, options.diagramType));
  }
  if (!writes.length) return;
  await Promise.all(writes);
  stickyThinking.value = false;
  stickyDiagram.value = false;
  // `forget()` + `applyDefaults()` 这两步不能省：不调的话，用户手动选了流程图、勾了
  // "以后都这样"，而 `prefilled` 还是 false——这次生成的 `prefs_applied` 就是空的，
  // 面板上那条偏好显示"尚未生效"，明明它刚刚生效过。顺序是"先写、再让选项回到
  // 跟随偏好、最后算 appliedKeys"。
  //
  // 只在**真的写了**的时候做：没有勾选时这一步会清掉 `touched`，把用户刚刚手选的
  // 图型顶回偏好值——"用户显式选了之后偏好不再覆盖"那条性质就是这么坏的。
  options.forget();
  options.applyDefaults(prefs.defaults);
}

const intentState = computed<IntentState>(() => {
  if (!store.content) return 'create';
  return store.selectedIds.length ? 'edit-selected' : 'edit-all';
});

/**
 * 选区大小。只认 `content` 里真实存在的节点：`selectedIds` 可能残留着上一次的
 * 选中，而那时树已经换了一张——徽标说"已选 3 个"、请求里却是空，用户没法察觉。
 */
const selectionCount = computed(() => (store.content ? store.selectedIds.length : 0));

/** 材料那边的闸门（解析中、有失败项、超长）。没有材料时它是空串——材料是可选的。 */
const materialProblem = computed(() => sources.materialProblem);

/** 只给材料、不给主题时的备选主题。判据在 `materialTitle` 里，这里只取结果。 */
const materialTopicChoice = computed(() => materialTitle(sources.readySources));

const runBlocked = computed(() => store.busy || store.agentRunning);
const typed = computed(() => intentText.value.trim());

/**
 * 这次点下去会发什么；空串 = 还发不出去（按钮禁用，原因写在下面那行提示里）。
 *
 * 「只给材料」的那条路在这里落地：`mindmap_generate_deep` 只需要一个 topic 字符串，
 * 而模型看不见文件名，所以标题必须**写进 goal** 才真的算"用了它当主题"。
 */
const submitGoal = computed(() => {
  if (typed.value) return typed.value;
  if (intentState.value !== 'create') return '';
  const topic = materialTopicChoice.value.topic;
  return topic ? `用材料标题「${topic}」当主题，把材料整理成导图` : '';
});

const canSubmit = computed(() => {
  if (runBlocked.value || !submitGoal.value) return false;
  if (intentState.value !== 'create') return true;
  // 录音停下来的那一刻会多出一份材料：此刻发出去的请求一定缺这一块。
  return !materialProblem.value && !sources.recording;
});

/** 材料够长到"这次一定不走外部检索"没有。够长时那个勾置灰。 */
const materialLongEnough = computed(
  () => mergedCharCount(sources.readySources) >= SHORT_MATERIAL_CHARS,
);

/** 这个开关这次真的会起作用：新建、且材料不够长。 */
const externalSearchApplies = computed(
  () => intentState.value === 'create' && !materialLongEnough.value,
);

const externalSearchHint = computed(() => {
  // edit / expand 这条路不生成骨架、也不发检索请求：灰着就得说明原因，
  // 否则用户会以为"勾了没反应"是坏了。
  if (intentState.value !== 'create') return '这次是改图，不走联网检索';
  if (materialLongEnough.value) {
    // 置灰的原因必须写出来。灰着不解释，用户只会以为功能坏了。
    return '这次带材料，不走联网检索';
  }
  return prefs.externalSearchEnabled
    ? '没材料时先联网搜一轮资料，命中会变成能点回原文的引用（约 4 秒）'
    : '已关闭：没材料时直接按主题生成，节点上不会有引用';
});

/**
 * 勾选即落库。写失败时把勾**拨回去**：`:checked` 绑的是 store，而 store 没变就等于
 * 绑定的值没变、Vue 不会重渲染这个 prop——不手动复原的话，界面上显示"已关闭"、
 * 后端还是"开"，下一次生成照样联网。
 */
async function toggleExternalSearch(next: boolean, event: Event): Promise<void> {
  if (await prefs.save(EXTERNAL_SEARCH_PREF_KEY, next ? 'true' : 'false')) return;
  (event.target as HTMLInputElement).checked = prefs.externalSearchEnabled;
}

/** 这一次运行**实际带着哪些偏好**（与 `stores/options.ts` 同一口径，多一条联网检索）。 */
function appliedKeys(): string[] {
  const keys = options.appliedKeys();
  if (externalSearchApplies.value && prefs.value(EXTERNAL_SEARCH_PREF_KEY) !== null) {
    keys.push(`${EXTERNAL_SEARCH_PREF_KEY}=${prefs.externalSearchEnabled ? 'true' : 'false'}`);
  }
  return keys;
}

const wantedIntent = computed<'create' | 'edit'>(
  () => (intentState.value === 'create' ? 'create' : 'edit'),
);

const intentButton = computed(() => {
  // 只认**这一个入口**发起的那一轮：展开在跑的时候，「改这张图」不该显示「改中…」。
  const busyHere = store.agentRunning && store.runIntent === wantedIntent.value;
  if (busyHere) return wantedIntent.value === 'create' ? '生成中…' : '改中…';
  if (intentState.value === 'create') return '生成导图';
  if (intentState.value === 'edit-all') return '改这张图';
  return selectionCount.value === 1 ? '改这个节点' : `改这 ${selectionCount.value} 个节点`;
});

const intentPlaceholder = computed(() => {
  if (intentState.value === 'create') return '发消息或写一个主题…';
  if (intentState.value === 'edit-all') return '说说你想怎么改';
  return selectionCount.value === 1 ? '改选中的这个节点…' : `改选中的这 ${selectionCount.value} 个节点…`;
});

const intentLabel = computed(() => (intentState.value === 'create' ? '主题或要求' : '怎么改'));

const selectionLabel = computed(
  () => (selectionCount.value > 0 ? `已选 ${selectionCount.value} 个节点` : ''),
);

const intentHint = computed(() => {
  if (runBlocked.value) {
    return store.agentRunning ? '正在跑，等这一次结束。' : '正在生成，等这一次跑完再改。';
  }
  if (intentState.value === 'create') {
    if (sources.recording) return '正在录音：先松开「按住说话」，再生成。';
    if (materialProblem.value) return materialProblem.value;
    if (typed.value) {
      return sources.items.length
        ? '会带上这里附的材料一起生成。'
        : '没传材料，会先去外部搜资料。';
    }
    if (materialTopicChoice.value.topic) {
      return `没写主题：用材料标题「${materialTopicChoice.value.topic}」当中心主题。`;
    }
    return materialTopicChoice.value.reason
      ? `${materialTopicChoice.value.reason}——写一个主题，或者换一份标题像样的材料。`
      : '写一个主题，或者点「+」添加材料。';
  }
  if (intentState.value === 'edit-all') {
    return '先在画布上选中节点，或直接描述要改哪里（按住 Ctrl 点节点可以多选）。';
  }
  return selectionCount.value === 1
    ? '优先改这个节点及其子分支；真要动别处也能改，清单里会标出来。'
    : `优先改这 ${selectionCount.value} 个节点及其子分支；真要动别处也能改，清单里会标出来。`;
});

const intentHintWarn = computed(() => intentState.value === 'create' && !!materialProblem.value);

/** 状态行只在"有一件事要讲"时出现。 */
const showStatus = computed(() => store.status !== INITIAL_STATUS);

/**
 * 「深度思考」：**这一次**走不走推理模型。
 *
 * 刻意不做成偏好：用户会在"随便出一张"和"这段逻辑要拆准"之间来回切，写进库里就把
 * 一次性的选择变成了永久状态。所以它只是组件里的一个开关，跟着这一次请求发出去
 * （`deep_thinking`），用完不复位——同一轮里连点两次生成时，第二句通常还是同一类任务。
 */
const deepThinking = ref(false);

/** 材料面板（粘贴 / 文件 / 录音 / 来源列表）开着没有。 */
const materialOpen = ref(false);
const fileInput = ref<HTMLInputElement | null>(null);

async function submitIntent() {
  if (!canSubmit.value) return;
  const goal = submitGoal.value;
  if (!goal) return;
  // 选区随状态**显式**带上：徽标说「已选 3 个」时，请求里就正好是这三个。
  const selection = intentState.value === 'create' ? [] : [...store.selectedIds];
  const intent = intentState.value === 'create' ? 'create' : 'edit';
  const wanted = goal;
  intentText.value = '';
  // 这句话发出去之后，"用户要求留着"这件事就结束了：输入框已经空出来，
  // 接下来该按状态判（生成中就收起、成功就淡出）。
  pinned.value = false;
  // 发出去的那一刻就把这句话记进时间线：对话框随后会收成进度条，"我说了什么"
  // 不能只活在输入框里（那个框马上要清空）。
  emit('submit', wanted);
  await syncStickyPrefs();
  await store.runGeneration(
    goal, intent, sources.readySources,
    options.thinkingModel, options.diagramType,
    { selection, prefsApplied: appliedKeys(), deepThinking: deepThinking.value },
  );
}

/**
 * Enter 提交、Shift+Enter 换行。
 *
 * 输入法候选框里的回车**不是**提交：中文主题几乎都要经过那一步，而老写法
 * （`@keydown.enter.prevent`）会在组词过程中就把半截主题发出去。
 */
function onIntentKeydown(event: KeyboardEvent) {
  if (event.isComposing || event.keyCode === 229) return;
  if (event.key !== 'Enter' || event.shiftKey || event.ctrlKey || event.metaKey) return;
  event.preventDefault();
  void submitIntent();
}

// ---------------------------------------------------------------- 语音

const voiceMode = ref(false);

function toggleVoice(): void {
  voiceMode.value = !voiceMode.value;
  if (voiceMode.value) {
    // 切进语音就先把输入框的焦点交出去：移动端键盘会顶起半个屏幕，
    // 而「按住说话」期间根本不需要它。
    (document.activeElement as HTMLElement | null)?.blur?.();
  }
}

function onRecorded(file: File): void {
  void sources.addFiles([file]);
}

// ---------------------------------------------------------------- 状态行

type StatusKind = 'running' | 'error' | 'warn' | 'ok' | 'conflict' | 'info';

const agentPartial = computed(
  () => store.agentWarnings.find(item => item.kind === 'partial') ?? null,
);

/**
 * 这一行现在在说什么。**优先级从上到下**，同一时刻只说一件。
 *
 * 冲突排第一：它要用户在两个都会丢东西的选择里做决定，压住它就是丢数据。
 * 运行中排第二：进度要压过上一次的结果。异常排第三：失败不能因为"上次成功过"就不显示。
 */
const statusKind = computed<StatusKind | null>(() => {
  if (store.conflictDraft) return 'conflict';
  if (store.busy || store.agentRunning) return 'running';
  if (store.agentError) return 'error';
  if (agentPartial.value) return 'warn';
  if (store.generatedSummary) return 'ok';
  if (store.resumableRun) return 'info';
  return null;
});

const runningVerb = computed(() => {
  if (store.runIntent === 'expand') return '正在展开这一支';
  if (store.runIntent === 'edit') return '正在改这张图';
  return '正在生成';
});

/**
 * 已经跑了多少秒。**只用于状态行**，不写进 store：它是纯展示计时器，放进 store
 * 会让每一下心跳都触发一轮与数据无关的重算。
 */
const elapsedSec = ref(0);
let elapsedTimer: ReturnType<typeof setInterval> | null = null;

function stopElapsed(): void {
  if (elapsedTimer) clearInterval(elapsedTimer);
  elapsedTimer = null;
  elapsedSec.value = 0;
}

watch(
  () => store.busy || store.agentRunning,
  running => {
    if (!running) {
      stopElapsed();
      return;
    }
    if (elapsedTimer) return;
    elapsedSec.value = 0;
    elapsedTimer = setInterval(() => { elapsedSec.value += 1; }, 1000);
  },
  { immediate: true },
);

const statusText = computed(() => {
  switch (statusKind.value) {
    case 'conflict':
      return '生成期间你改过这张图：两棵树都还在，选一个';
    case 'running': {
      const parts = [runningVerb.value];
      if (store.waitedMs >= SILENT_HINT_SECONDS) {
        parts.push(`模型正在思考，已等 ${store.waitedMs} 秒`);
      } else {
        if (store.stageText) parts.push(store.stageText);
        if (elapsedSec.value > 0) parts.push(`${elapsedSec.value} 秒`);
      }
      return parts.join(' · ');
    }
    case 'error':
      return `生成失败：${store.agentError}`;
    case 'warn':
      return `部分完成：${agentPartial.value?.message ?? '有几支没能生成'}`;
    case 'ok': {
      const summary = store.generatedSummary;
      if (!summary) return '';
      const nodes = summary.nodes ? `${summary.nodes} 个节点` : '';
      const changes = summary.changes == null ? '' : `，改了 ${summary.changes} 处`;
      return `已落到画布${nodes ? `（${nodes}${changes}）` : changes} · 还没保存`;
    }
    case 'info': {
      // 只给时间与目标，不给整棵树：这是一句通知，要不要捡回来是用户的决定。
      // "完成了但没保存"与"被中断了但已产出一部分"对他的意义完全不同，不能糊成一句。
      const run = store.resumableRun;
      if (!run) return '';
      const date = new Date(run.updated_at);
      const clock = Number.isNaN(date.getTime())
        ? ''
        : date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false });
      const when = clock ? `（${clock}）` : '';
      const what = run.status === 'cancelled'
        ? `上次${when}那次生成被中断了，已经生成的部分还留着`
        : `上次${when}生成完的导图还没保存`;
      return run.goal ? `${what}：「${run.goal}」` : `${what}。`;
    }
    default:
      return '';
  }
});

interface StatusAction { key: string; label: string; test: string; run: () => void }

const statusActions = computed<StatusAction[]>(() => {
  switch (statusKind.value) {
    case 'conflict':
      return [
        { key: 'use', label: '用模型的结果', test: 'run-status-use',
          run: () => store.resolveConflict('generated') },
        { key: 'keep', label: '保留我的', test: 'run-status-keep',
          run: () => store.resolveConflict('mine') },
      ];
    case 'running':
      return [
        { key: 'cancel', label: '取消', test: 'run-status-cancel', run: () => store.cancel() },
      ];
    case 'ok':
      return store.canUndoGenerate
        ? [{ key: 'undo', label: '回到生成之前', test: 'run-status-undo',
            run: () => store.undoGenerate() }]
        : [];
    case 'info':
      return [
        { key: 'load', label: '载入', test: 'run-status-load', run: () => store.resumeRun() },
        { key: 'skip', label: '不用了', test: 'run-status-skip', run: () => store.ignoreResumable() },
      ];
    default:
      return [];
  }
});

const statusTone = computed(() => {
  switch (statusKind.value) {
    case 'error':
      return 'border-rose-300 bg-rose-50 text-rose-900 ring-1 ring-rose-200';
    case 'warn':
    case 'conflict':
      return 'border-amber-300 bg-amber-50 text-amber-900 ring-1 ring-amber-200';
    case 'ok':
      return 'border-emerald-200 bg-white text-slate-700';
    case 'info':
      return 'border-sky-200 bg-sky-50 text-sky-900';
    default:
      return 'border-slate-200 bg-white text-slate-700';
  }
});

const statusDot = computed(() => {
  switch (statusKind.value) {
    case 'running': return 'bg-sky-500';
    case 'error': return 'bg-rose-500';
    case 'warn':
    case 'conflict': return 'bg-amber-500';
    case 'ok': return 'bg-emerald-500';
    default: return 'bg-sky-400';
  }
});

const showDetailAction = computed(
  () => statusKind.value !== null && statusKind.value !== 'info',
);

/** 抽屉里有没有东西可展开（判据与 `AgentPanel` 共用）。 */
const agentHasDetail = computed(() => agentDetailVisible(store));

// ---------------------------------------------------------------- 状态机

/**
 * 强制把对话框叫回来（选中节点点「改」、点「+」、点悬浮球里的一条）。
 *
 * 它只压住"淡出"这一件事，不改生成中的形态：正在跑的时候点「改」仍然只看到进度条，
 * 因为那会儿改不了任何东西。
 */
const pinned = ref(false);
const dismissed = ref(false);
let dismissTimer: ReturnType<typeof setTimeout> | null = null;

function clearDismiss(): void {
  if (dismissTimer) clearTimeout(dismissTimer);
  dismissTimer = null;
}

/**
 * 生成成功后"只剩画布"这一件事，什么时候该让位。
 *
 * 三条刹车（缺一条都会让用户下一步不知道点哪儿）：
 *   1. 画布上还没有图——他从没成功过，收走对话框等于收走了唯一的入口；
 *   2. 输入框里还有没发出去的字——那是他正在写的东西，收走就丢了；
 *   3. 这一次失败了 / 部分完成 / 有冲突 / 有上次没保存的结果——这几种都在等他做决定。
 */
const shouldKeep = computed(() => {
  if (pinned.value) return true;
  if (!store.content) return true;
  if (typed.value) return true;
  const kind = statusKind.value;
  if (kind && kind !== 'ok' && kind !== 'running') return true;
  if (kind === 'running') return true;
  return false;
});

watch(
  () => [statusKind.value, typed.value, store.nodeCount] as const,
  ([kind]) => {
    clearDismiss();
    dismissed.value = false;
    // 只有"成功且有图、没有草稿"这一种才安排淡出。其余状态对话框留着。
    if (kind === 'ok' && store.content && !typed.value) {
      dismissTimer = setTimeout(() => {
        // 定时器到点时把 `pinned` 一并松开：用户点过「改」把对话框叫回来之后，
        // 那一轮结束后它仍然该按状态判（否则它再也淡不出去）。
        pinned.value = false;
        dismissed.value = true;
      }, FADE_DELAY_MS);
    }
  },
  { immediate: true },
);

const running = computed(() => statusKind.value === 'running');
/** 生成中：整块收成一条细进度。 */
const compact = computed(() => running.value);
/** 生成完淡出：**opacity 归零而不是 v-if**，DOM 留着，测试与键盘用户都还找得到它。 */
const faded = computed(() => dismissed.value && !shouldKeep.value && !compact.value);

function openComposer(prefill?: string): void {
  pinned.value = true;
  dismissed.value = false;
  clearDismiss();
  if (prefill !== undefined) intentText.value = prefill;
  void nextTick(() => {
    const el = document.getElementById('intent') as HTMLTextAreaElement | null;
    el?.focus();
  });
}

function closeMaterial(): void {
  materialOpen.value = false;
}

function openMaterial(): void {
  materialOpen.value = true;
}

function pickFiles(): void {
  // 「+」既开材料面板又直接弹文件选择框：它是截图里那个加号，用户点它是想传东西。
  materialOpen.value = true;
  void nextTick(() => fileInput.value?.click());
}

/** `+` 弹出来的文件选择：与材料面板里的入口走同一条上传链路。 */
async function onPickMaterials(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement;
  const selected = Array.from(input.files ?? []);
  // 再选同一个文件也要触发 change。
  input.value = '';
  await sources.addFiles(selected);
}

onBeforeUnmount(() => {
  stopElapsed();
  clearDismiss();
});

defineExpose({ openComposer, openMaterial, closeMaterial, focusInput: () => openComposer() });
</script>

<template>
  <div class="pointer-events-none absolute inset-x-0 bottom-0 z-30 flex justify-center px-4 pb-4">
    <div
      data-test="chat-dock"
      :data-faded="faded ? 'true' : 'false'"
      class="pointer-events-auto w-full max-w-[760px] transition-all duration-300"
      :class="faded ? 'pointer-events-none translate-y-2 opacity-0' : 'translate-y-0 opacity-100'"
      :aria-hidden="faded ? 'true' : undefined"
    >
      <!-- 生成中：一条细进度。它取代了整个对话框（不是叠在上面）——两处都说"正在生成"
           只会互相削弱，而画布此刻正是用户想看的东西。 -->
      <div
        v-if="compact"
        data-test="run-status"
        class="flex items-center gap-2 rounded-full border border-slate-200 bg-white px-4 py-2 text-xs shadow-lg"
        aria-live="polite"
      >
        <span class="h-2 w-2 shrink-0 animate-pulse rounded-full bg-sky-500" aria-hidden="true" />
        <span class="min-w-0 flex-1 truncate font-medium text-slate-700" data-test="run-status-text">
          {{ statusText }}
        </span>
        <button
          v-for="action in statusActions"
          :key="action.key"
          type="button"
          :data-test="action.test"
          class="shrink-0 rounded-full border border-slate-300 bg-white px-3 py-0.5 text-xs hover:bg-slate-50"
          @click="action.run()"
        >{{ action.label }}</button>
      </div>

      <!-- 空闲 / 失败 / 有草稿：完整对话框。 -->
      <div
        v-else
        class="overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-xl shadow-slate-900/5"
      >
        <!-- 材料缩略图区：左栏那个 320px 常驻区块的内容全在这里（横向），画布因此
             回到全宽。每份材料一个缩略块 + ✕；状态（解析中/失败）就地显示。 -->
        <div
          v-if="sources.items.length"
          data-test="thumbs"
          class="flex gap-2 overflow-x-auto border-b border-slate-100 px-3 py-2"
        >
          <div
            v-for="item in sources.items"
            :key="item.key"
            class="group relative flex w-[132px] shrink-0 flex-col gap-1 rounded-lg border border-slate-200 bg-slate-50 px-2 py-1.5"
            :title="item.label"
          >
            <button
              type="button"
              class="absolute -right-1.5 -top-1.5 z-10 flex h-5 w-5 items-center justify-center rounded-full border border-slate-300 bg-white text-[11px] text-slate-500 hover:text-rose-600 disabled:opacity-50"
              :aria-label="`移除 ${item.label}`"
              :disabled="store.busy"
              @click="sources.remove(item.key)"
            >✕</button>
            <span class="truncate text-[11px] font-medium text-slate-700">{{ item.label }}</span>
            <span class="text-[10px] text-slate-500">
              {{ SOURCE_LABELS[item.kind] }}
              <template v-if="item.state === 'ready' && item.source">
                · {{ Array.from(item.source.text?.trim() ?? '').length }} 字
              </template>
            </span>
            <span v-if="item.state === 'queued'" class="text-[10px] text-slate-500">等待解析…</span>
            <span v-else-if="item.state === 'parsing'" class="text-[10px] text-slate-500" aria-live="polite">
              正在解析…{{ item.seconds }} 秒
            </span>
            <span v-else-if="item.state === 'failed'" class="text-[10px] leading-tight text-rose-700" role="alert">
              {{ item.error }}
            </span>
            <button
              v-if="item.state === 'failed'"
              type="button"
              class="text-left text-[10px] text-slate-600 underline disabled:opacity-50"
              :disabled="store.busy"
              @click="sources.retry(item.key)"
            >重试</button>
          </div>
          <button
            type="button"
            class="shrink-0 rounded-lg border border-dashed border-slate-300 px-2 text-[11px] text-slate-500 hover:bg-slate-50"
            @click="openMaterial"
          >管理材料</button>
        </div>

        <!--
          `data-test="intent-box"` 保留在这个容器上：一批用例按它定位提交按钮
          （`[data-test="intent-box"] button` 取文档顺序第一个）。所以提交按钮是这个
          容器里的**第一个** button，视觉位置靠 `order-last` 排到最右。
        -->
        <div data-test="intent-box" role="group" aria-label="生成与改图" class="flex flex-col gap-1.5 p-2.5">
          <div class="flex items-center justify-between gap-2">
            <label for="intent" class="text-xs font-medium text-slate-600">{{ intentLabel }}</label>
            <span
              v-if="selectionLabel"
              data-test="edit-selection"
              class="shrink-0 rounded bg-sky-50 px-1 py-0.5 text-[11px] text-sky-700"
            >{{ selectionLabel }}</span>
          </div>

          <!-- 语音态：输入框换成「按住说话」。右侧那个 ⌨ 把键盘输入换回来。 -->
          <VoicePress
            v-if="voiceMode"
            :disabled="runBlocked"
            @recorded="onRecorded"
            @active="sources.recording = $event"
            @failed="() => {}"
          />

          <textarea
            v-if="!voiceMode"
            id="intent"
            v-model="intentText"
            rows="2"
            aria-label="主题或修改指令"
            class="resize-none rounded border border-slate-300 px-2 py-1 text-xs outline-none focus:border-slate-500 disabled:opacity-50"
            :disabled="runBlocked"
            :placeholder="intentPlaceholder"
            @keydown="onIntentKeydown"
          />

          <!--
            拆解方式与图型只在**新建**那一次生效：后端的编辑路径既不消费
            `thinking_model`，也不生成骨架。摆一个选了不起作用的控件比不摆更糟。
          -->
          <details
            v-if="intentState === 'create'"
            class="rounded border border-slate-100 bg-slate-50/60 px-2 py-1"
          >
            <summary class="cursor-pointer text-[11px] text-slate-500">
              拆解方式与图型<span v-if="options.prefilled.thinking || options.prefilled.diagram" class="text-sky-700">（含来自偏好的预填）</span>
            </summary>
            <div class="mt-1 flex flex-col gap-1">
              <div class="flex items-center gap-1.5 text-[11px] text-slate-500">
                <label for="thinking-model" class="w-14 shrink-0">拆解方式</label>
                <select
                  id="thinking-model"
                  :value="options.thinkingModel"
                  :disabled="runBlocked"
                  title="按这种方式拆解主题"
                  class="min-w-0 flex-1 rounded border border-slate-300 px-1 py-1 text-[11px] outline-none focus:border-slate-500 disabled:opacity-50"
                  @change="options.chooseThinking(($event.target as HTMLSelectElement).value)"
                >
                  <option v-for="item in THINKING_MODEL_OPTIONS" :key="item.value" :value="item.value">{{ item.label }}</option>
                </select>
              </div>
              <div class="flex items-center justify-between gap-2 text-[11px]">
                <p class="leading-tight text-slate-400">
                  按这种方式拆解主题
                  <span v-if="options.prefilled.thinking" class="text-sky-700">（来自你的偏好：{{ options.thinkingLabel }}）</span>
                </p>
                <label class="flex shrink-0 items-center gap-1 text-slate-500">
                  <input
                    v-model="stickyThinking"
                    type="checkbox"
                    :disabled="runBlocked"
                    data-test="sticky-thinking"
                  />
                  以后都这样
                </label>
              </div>
              <div class="flex items-center gap-1.5 text-[11px] text-slate-500">
                <label for="diagram-type" class="w-14 shrink-0">图型</label>
                <select
                  id="diagram-type"
                  :value="options.diagramType"
                  :disabled="runBlocked"
                  title="这张图要画成什么形状"
                  class="min-w-0 flex-1 rounded border border-slate-300 px-1 py-1 text-[11px] outline-none focus:border-slate-500 disabled:opacity-50"
                  @change="options.chooseDiagram(($event.target as HTMLSelectElement).value as DiagramTypeChoice)"
                >
                  <option v-for="item in DIAGRAM_TYPE_OPTIONS" :key="item.value" :value="item.value">{{ item.label }}</option>
                </select>
              </div>
              <div class="flex items-center justify-between gap-2 text-[11px]">
                <p class="leading-tight text-slate-400">
                  {{ options.diagramHint }}
                  <span v-if="options.prefilled.diagram" class="text-sky-700">（来自你的偏好：{{ options.diagramLabel }}）</span>
                </p>
                <label class="flex shrink-0 items-center gap-1 text-slate-500">
                  <input
                    v-model="stickyDiagram"
                    type="checkbox"
                    :disabled="runBlocked"
                    data-test="sticky-diagram"
                  />
                  以后都这样
                </label>
              </div>
            </div>
          </details>

          <!-- 按钮行。视觉顺序靠 `order`，DOM 顺序靠"提交在最前"——后者是测试契约。 -->
          <div class="flex items-center gap-1.5 text-[11px]">
            <button
              type="button"
              class="order-last flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-slate-900 text-white before:content-['↑'] disabled:opacity-30"
              :disabled="!canSubmit"
              :aria-label="intentButton"
              :title="intentButton"
              @click="submitIntent"
            >
              <!-- 箭头用 CSS 伪元素画：`text()` 里只剩按钮语义（"生成导图 / 改这张图"），
                   读屏不会读成一个孤零零的箭头，用例也不必为它改断言。 -->
              <span class="sr-only">{{ intentButton }}</span>
            </button>
            <!-- 「深度思考」：这一次的开关（不是设置）。 -->
            <button
              type="button"
              data-test="deep-thinking"
              class="order-1 shrink-0 rounded-full border px-2 py-0.5"
              :class="deepThinking
                ? 'border-sky-300 bg-sky-50 text-sky-700'
                : 'border-slate-200 text-slate-500 hover:bg-slate-50'"
              :aria-pressed="deepThinking"
              :disabled="runBlocked"
              title="这一次走推理模型：会先想一遍再动手，慢一些，拆解更稳（模型由后端配置决定）"
              @click="deepThinking = !deepThinking"
            >深度思考</button>
            <!-- 「智能搜索」：原来那个联网检索开关。它写的是偏好，所以勾一下就落库。 -->
            <label
              class="order-2 shrink-0 cursor-pointer rounded-full border px-2 py-0.5"
              :class="prefs.externalSearchEnabled && externalSearchApplies
                ? 'border-sky-300 bg-sky-50 text-sky-700'
                : externalSearchApplies
                  ? 'border-slate-200 text-slate-500 hover:bg-slate-50'
                  : 'border-slate-200 text-slate-400'"
              :title="externalSearchHint"
            >
              <input
                type="checkbox"
                class="sr-only"
                :checked="prefs.externalSearchEnabled"
                :disabled="runBlocked || !externalSearchApplies || prefs.saving === EXTERNAL_SEARCH_PREF_KEY"
                data-test="external-search"
                @change="toggleExternalSearch(($event.target as HTMLInputElement).checked, $event)"
              />
              智能搜索
            </label>
            <p class="order-3 min-w-0 flex-1 truncate text-right leading-tight text-slate-400" data-test="external-search-hint">
              {{ externalSearchHint }}
            </p>
            <button
              type="button"
              data-test="attach"
              class="order-4 flex h-8 w-8 shrink-0 items-center justify-center rounded-full border border-slate-200 text-slate-600 hover:bg-slate-50 disabled:opacity-40"
              :disabled="runBlocked"
              aria-label="添加材料"
              title="添加材料（文档 / 图片 / 粘贴文字 / 录音）"
              @click="pickFiles"
            >+</button>
            <button
              type="button"
              data-test="voice-toggle"
              class="order-5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full border border-slate-200 text-slate-600 hover:bg-slate-50 disabled:opacity-40"
              :class="voiceMode ? 'border-slate-900 bg-slate-100 text-slate-900' : ''"
              :disabled="runBlocked"
              :aria-pressed="voiceMode"
              :aria-label="voiceMode ? '切回键盘输入' : '语音输入'"
              :title="voiceMode ? '切回键盘输入' : '语音输入（按住说话）'"
              @click="toggleVoice"
            >{{ voiceMode ? '⌨' : '🎤' }}</button>
          </div>

          <p
            class="text-[11px] leading-tight"
            :class="intentHintWarn ? 'text-amber-700' : 'text-slate-400'"
          >{{ intentHint }}</p>

          <!-- 状态行：失败 / 部分完成 / 冲突 / 上次没保存的结果。运行中的那一版在
               上面的细进度条里，两者不会同时出现。 -->
          <div
            v-if="statusKind"
            data-test="run-status"
            aria-live="polite"
            :aria-label="statusKind === 'info' ? '上次的运行结果' : undefined"
            class="flex items-center gap-2 rounded-lg border px-2 py-1.5 text-xs"
            :class="statusTone"
          >
            <span class="h-2 w-2 shrink-0 rounded-full" :class="statusDot" aria-hidden="true" />
            <span class="min-w-0 flex-1 truncate font-medium" :title="statusText" data-test="run-status-text">
              {{ statusText }}
            </span>
            <button
              v-for="action in statusActions"
              :key="action.key"
              type="button"
              :data-test="action.test"
              class="shrink-0 rounded-md border border-slate-300 bg-white px-2 py-0.5 text-xs hover:bg-slate-50"
              @click="action.run()"
            >{{ action.label }}</button>
            <button
              v-if="showDetailAction && agentHasDetail"
              type="button"
              data-test="run-status-detail"
              class="shrink-0 rounded-md px-2 py-0.5 text-xs underline decoration-dotted underline-offset-2 hover:bg-white/60"
              title="打开右侧抽屉：改动清单、诊断逐条、生成中的预览"
              @click="emit('detail')"
            >明细</button>
          </div>

          <!-- `role="status"` 只该有一个来源：这一行。生成中不渲染（细进度条已经占位）。 -->
          <p
            v-if="showStatus && statusKind !== 'running'"
            role="status"
            class="text-[11px] leading-tight text-slate-500"
          >{{ store.status }}</p>
        </div>

        <!--
          材料面板：粘贴正文、上传文件、录音、来源列表与引用计数。

          它**在 `intent-box` 之外**：面板里的说明会重复输入框提示里的"写一个主题"，
          混在同一个分组里会让"这个框里有几句话在说同一件事"变成一件要数一数才知道的事。
          默认收起——它是"准备材料"这一步，不是每一轮都要看的东西。
        -->
        <div
          v-show="materialOpen"
          data-test="material-panel"
          class="border-t border-slate-100 bg-slate-50/60 px-2.5 py-2"
        >
          <div class="mb-1 flex items-center justify-between">
            <p class="text-[11px] font-medium text-slate-600">材料（可选）</p>
            <button type="button" class="text-[11px] text-slate-500 hover:text-slate-900" @click="closeMaterial">收起</button>
          </div>
          <SourceInputs />
          <!-- 「+」弹出的那个文件选择框。全页只有这一个 `input[type=file]` 的材料入口：
               再放一个（比如 import）会让"选文件"的定位二义。 -->
          <input ref="fileInput" type="file" multiple class="hidden" aria-hidden="true" tabindex="-1" @change="onPickMaterials" />
        </div>
      </div>
    </div>
  </div>
</template>
