<script setup lang="ts">
import { computed } from 'vue';

import { agentToolLabel, useMindMapStore, type CandidateChange, type PreviewNode } from '../stores/mindmap';

/**
 * agent 运行面板：工具进度、候选骨架、候选确认、丢弃。
 *
 * 候选**不会自动应用**。这是这次改造的核心约束：模型算出来的是一份提案，
 * 用户点「应用」才算数。自动应用等于把「重新生成冲掉我的编辑」换个说法重演。
 */
const store = useMindMapStore();

const visible = computed(() =>
  store.agentRunning || store.candidate !== null || store.agentEvents.length > 0 || !!store.agentError,
);

/** 毫秒 → 「4.3 秒」。把 4341ms 直接摆给用户看是工程口吻。 */
function seconds(ms: number): string {
  return `${(ms / 1000).toFixed(1)} 秒`;
}

/**
 * 一行进度的文案：`{人话} · {耗时}`，进行中不带耗时。
 *
 * 工具名到人话的映射在 store 里（状态行也要用），这里只负责拼耗时；
 * 失败时保留工具名——那种时候用户和排查都需要能对上后端日志。
 */
function rowText(name: string, state: 'running' | 'done', ok: boolean, ms: number): string {
  if (state === 'running') return agentToolLabel(name, 'running');
  return ok ? `${agentToolLabel(name, 'done')} · ${seconds(ms)}` : `${name} 未成功`;
}

function failureReason(summary?: Record<string, unknown>): string {
  // 工具的摘要结构不同，只展示可读字段，避免把诊断对象或缺失值直接插进界面。
  const parts = [summary?.code, summary?.message]
    .filter((value): value is string => typeof value === 'string' && value.trim().length > 0);
  return parts.join('：') || '工具未返回具体失败原因。';
}

/** 只把工具调用摘出来显示；stage 文案已经有单独的状态行了。 */
interface ToolRow {
  key: string;
  state: 'running' | 'done';
  ok: boolean;
  /** 已经翻成人话的整行文案，模板不再拼字符串 */
  text: string;
  detail: string;
}

const calls = computed<ToolRow[]>(() => {
  // 按 call_id 归并：一次调用会先收到 tool_start 再收到 tool_end，
  // 直接映射成两行的话，界面上会出现"同一次调用既是执行中又已完成"。
  const order: string[] = [];
  const byId = new Map<string, ToolRow>();
  for (const event of store.agentEvents) {
    if (event.type === 'tool_start') {
      if (!byId.has(event.call_id)) order.push(event.call_id);
      byId.set(event.call_id, {
        key: event.call_id,
        state: 'running',
        ok: true,
        text: rowText(event.name, 'running', true, 0),
        detail: '',
      });
    } else if (event.type === 'tool_end') {
      if (!byId.has(event.call_id)) order.push(event.call_id);
      byId.set(event.call_id, {
        key: event.call_id,
        state: 'done',
        ok: event.ok,
        text: rowText(event.name, 'done', event.ok, event.latency_ms ?? 0),
        detail: event.ok ? '' : failureReason(event.result_summary),
      });
    }
  }
  return order.map(id => byId.get(id)!);
});

/** 候选骨架里的一条一级分支。 */
interface CandidateBranch {
  id: string;
  label: string;
  children: number;
}

/**
 * 外部检索那一行：取**最后一条** `external_search` 阶段帧。
 *
 * 为什么单独拎出来而不是只靠状态栏：检索时长为 4–6 秒（最坏 20 秒），这段时间
 * 里工具行写着"正在生成导图"、画布不动，用户看不出后台在联网。开始帧与结束帧
 * 共用同一个 `stage`，所以"取最后一条"天然实现了"检索结束后那句话变成已找到 N 条"。
 *
 * `detail` 是后端给的人话，这里不再翻译：同一句话在状态栏与面板里显示成两种说法，
 * 用户会以为是两件事。
 */
const searchNote = computed(() => {
  for (let index = store.agentEvents.length - 1; index >= 0; index -= 1) {
    const event = store.agentEvents[index];
    if (event.type === 'stage' && event.stage === 'external_search') return event;
  }
  return null;
});

/** 最多列几条分支：再往下列就不是"先看一眼"，而是让用户读长文了。 */
const BRANCH_LIMIT = 12;

const branches = computed<CandidateBranch[]>(() => {
  const root = store.candidate?.root;
  if (!root) return [];
  return root.children.map(child => ({
    id: child.id,
    label: child.label,
    children: child.children.length,
  }));
});

const shownBranches = computed(() => branches.value.slice(0, BRANCH_LIMIT));
const hiddenBranches = computed(() => branches.value.length - shownBranches.value.length);

/** 生长中的预览里的一行。层级摊平成缩进，模板里就不用递归组件。 */
interface PreviewRow {
  key: string;
  label: string;
  depth: number;
  /** false 才做标记：null 是「模型还没写到」，画出来会和「明确不展开」混作一谈。 */
  expandable: boolean | null;
}

/** 预览最多列几行：生成中它还会继续长，列到几十行就不再是"先看一眼"了。 */
const PREVIEW_LIMIT = 24;

function countPreview(node: PreviewNode): number {
  return 1 + node.children.reduce((sum, child) => sum + countPreview(child), 0);
}

const previewRows = computed<PreviewRow[]>(() => {
  const root = store.agentPreview;
  if (!root) return [];
  const rows: PreviewRow[] = [];
  const walk = (node: PreviewNode, depth: number, key: string) => {
    if (rows.length >= PREVIEW_LIMIT) return;
    rows.push({ key, label: node.label, depth, expandable: node.expandable });
    node.children.forEach((child, index) => walk(child, depth + 1, `${key}.${index}`));
  };
  walk(root, 0, '0');
  return rows;
});

/** 根节点单独摆在标题行，列表从它的孩子开始。 */
const previewBranchRows = computed(() => previewRows.value.filter(row => row.depth > 0));
const previewTotal = computed(() => (store.agentPreview ? countPreview(store.agentPreview) : 0));
const hiddenPreview = computed(() => previewTotal.value - previewRows.value.length);

/**
 * 预览折叠区的标题：展开与生成是两件事，标题也该分开说。
 *
 * `agentPreviewScope` 只在展开时被设置（那是"正在展开哪个节点"），所以它同时是
 * "这一轮是不是展开"的判据——不必再读一次 `runIntent`。
 */
const previewTitle = computed(() => {
  const expand = store.agentPreviewScope !== null;
  if (store.agentRunning) return expand ? '展开中（预览）' : '生成中（预览）';
  return expand ? '上次展开中断时的预览' : '上次生成中断时的预览';
});

const canAccept = computed(() => store.candidate !== null && !store.agentRunning);

/**
 * 待确认候选相对画布的改动清单（`edit` / `expand` 意图）。
 *
 * `null` 表示"没有可比基线"（`create` 是一整张新图，谈差异无从谈起），
 * 空数组表示"算出来了，但一处都没改"——两者必须分开：前者要显示"这是新图"，
 * 后者要显示"这次什么都没改"，否则用户面对一个"候选已就绪"看不出该不该点。
 */
const diff = computed<CandidateChange[] | null>(() => store.candidateDiff);

/** 改动清单最多列几条：再往下列就不是"确认一下"，而是让用户读变更日志。 */
const DIFF_LIMIT = 8;
const shownDiff = computed(() => (diff.value ?? []).slice(0, DIFF_LIMIT));
const hiddenDiff = computed(() => Math.max(0, (diff.value?.length ?? 0) - shownDiff.value.length));

/** 六种改动的中文标签。用徽标而不是句子：一屏能扫完"哪几处被动了"。 */
const DIFF_LABELS: Record<CandidateChange['kind'], string> = {
  added: '新增',
  removed: '删除',
  updated: '修改',
  moved: '移动',
  // 边单独两个徽标：连线不改层级，"新增"与"新增关系"混在一起时，用户分不清
  // 清单上这一条是要去看节点内容还是去看流程走向。
  edge_added: '连关系',
  edge_removed: '断关系',
};

/** 颜色也在这里区分一次：删除用玫红，新增用翠绿，其余中性。 */
const DIFF_CLASS: Record<CandidateChange['kind'], string> = {
  added: 'bg-emerald-100 text-emerald-700',
  removed: 'bg-rose-100 text-rose-700',
  updated: 'bg-amber-100 text-amber-800',
  moved: 'bg-sky-100 text-sky-700',
  edge_added: 'bg-indigo-100 text-indigo-700',
  edge_removed: 'bg-orange-100 text-orange-700',
};

/**
 * 点一条改动就跳到那个节点。
 *
 * 新增的节点在画布上还不存在（画布画的是已应用的 `content`），`locate` 会静默
 * 不选中它——这是刻意的：为了跳转方便就往 `content` 里塞一个预览节点，等于让
 * "画布上有什么"和"用户的作品是什么"分叉。
 */
function jumpTo(id: string) {
  store.locate(id);
}

/**
 * 结果不完整的证据：partial 标记单独摆在标题行，诊断逐条列 message。
 *
 * 后端在 `ok=true` 的同时给出这些信息，界面只认 ok 的话，「3 个分支失败」
 * 和「全部成功」长得一模一样，用户点完接受才发现树缺了一块。
 */
const partialWarning = computed(
  () => store.agentWarnings.find(item => item.kind === 'partial') ?? null,
);
const diagnostics = computed(() => store.agentWarnings.filter(item => item.kind === 'diagnostic'));

/** 最多列三条诊断：再往下列就不是提个醒，而是让用户读长文了。 */
const DIAGNOSTIC_LIMIT = 3;
const shownDiagnostics = computed(() => diagnostics.value.slice(0, DIAGNOSTIC_LIMIT));
const hiddenDiagnostics = computed(() => diagnostics.value.length - shownDiagnostics.value.length);

function accept() {
  store.acceptCandidate();
}

function discard() {
  store.discardCandidate();
}

function cancel() {
  store.cancel();
}
</script>

<template>
  <section
    v-if="visible"
    class="rounded-md border border-slate-200 bg-slate-50 p-2 text-xs"
    aria-label="模型协作进度"
  >
    <div class="flex items-center justify-between">
      <h3 class="font-medium text-slate-700">模型协作</h3>
      <button
        v-if="store.agentRunning"
        type="button"
        class="text-slate-500 hover:text-slate-900"
        @click="cancel"
      >取消</button>
    </div>

    <p v-if="searchNote" class="mt-1 text-[11px] leading-relaxed text-slate-500">
      {{ searchNote.detail }}
    </p>

    <ol v-if="calls.length" class="mt-2 flex flex-col gap-1">
      <li
        v-for="call in calls"
        :key="call.key"
        class="rounded bg-white px-2 py-1"
      >
        <span :class="call.state === 'running' ? 'text-slate-500' : call.ok ? 'text-emerald-700' : 'text-rose-700'">
          {{ call.text }}
        </span>
        <p v-if="call.detail" class="mt-0.5 break-words text-[11px] leading-relaxed text-rose-700">
          {{ call.detail }}
        </p>
      </li>
    </ol>

    <p v-if="store.agentError" role="alert" class="mt-2 leading-relaxed text-rose-700">
      {{ store.agentError }}
    </p>

    <!-- 部分完成与诊断：放在候选上方，用户先看到"这棵树不完整"，再决定接不接受。 -->
    <div
      v-if="store.agentWarnings.length"
      role="status"
      class="mt-2 rounded border border-amber-200 bg-amber-50 px-2 py-1.5 leading-relaxed text-amber-800"
    >
      <p v-if="partialWarning" class="font-medium">
        部分完成<span class="font-normal">：{{ partialWarning.message }}</span>
      </p>
      <p v-else class="font-medium">这次生成有诊断提示</p>
      <ul v-if="shownDiagnostics.length" class="mt-1 flex flex-col gap-0.5 text-[11px]">
        <li v-for="item in shownDiagnostics" :key="`${item.code}|${item.message}`" class="break-words">
          {{ item.message }}
        </li>
      </ul>
      <p v-if="hiddenDiagnostics > 0" class="mt-1 text-[11px]">
        还有 {{ hiddenDiagnostics }} 条
      </p>
    </div>

    <!-- 生成中的预览：节点边跑边上屏，用户先看清树在往哪个方向长。
         它和候选是两份内容——预览没经过整树校验，最终以候选为准。 -->
    <details
      v-if="store.agentPreview && !store.candidate"
      open
      class="mt-2 rounded bg-white px-2 py-1"
    >
      <summary class="cursor-pointer font-medium text-slate-700">
        {{ previewTitle }}
      </summary>
      <!--
        展开时预览的顶部是「正在展开哪个节点」，不是中心主题：链上那几层祖先只是
        为了让 `node_grow` 的索引链有落点（`seedExpandPreview`），把根的标题摆在
        这里会让用户以为整张图在重生成。
      -->
      <p class="mt-1 break-words font-medium text-slate-700">
        {{ store.agentPreviewScope ? `正在展开：${store.agentPreviewScope}` : store.agentPreview.label }}
      </p>
      <ul
        v-if="previewBranchRows.length"
        class="mt-1 flex flex-col gap-0.5 text-[11px] leading-relaxed text-slate-600"
      >
        <li
          v-for="row in previewBranchRows"
          :key="row.key"
          class="break-words"
          :style="{ paddingLeft: `${(row.depth - 1) * 12}px` }"
        >
          <!-- 只有模型明确写了「不用再拆」才加这个点；null 与 true 都不加。 -->
          <span
            v-if="row.expandable === false"
            class="mr-1 inline-block h-1 w-1 rounded-full bg-slate-300 align-middle"
            aria-hidden="true"
          />
          <span :class="row.expandable === false ? 'text-slate-400' : ''">{{ row.label }}</span>
        </li>
      </ul>
      <p v-if="hiddenPreview > 0" class="mt-1 text-[11px] text-slate-400">
        ……还有 {{ hiddenPreview }} 个节点
      </p>
      <p class="mt-1 text-[11px] text-slate-400">这是生成过程中的预览，以最终候选为准。</p>
    </details>

    <!-- 候选骨架：让用户在点「应用」之前先看清这棵树长什么样，而不是盲点。 -->
    <details v-if="store.candidate" open class="mt-2 rounded bg-white px-2 py-1">
      <summary class="cursor-pointer font-medium text-slate-700">候选（未应用）</summary>
      <p class="mt-1 break-words font-medium text-slate-700">{{ store.candidate.root.label }}</p>
      <ul
        v-if="shownBranches.length"
        class="mt-1 flex flex-col gap-0.5 text-[11px] leading-relaxed text-slate-600"
      >
        <li v-for="branch in shownBranches" :key="branch.id" class="break-words">
          {{ branch.label }}
          <span class="text-slate-400">（{{ branch.children }} 个子节点）</span>
        </li>
      </ul>
      <p v-else class="mt-1 text-[11px] text-slate-400">只有根节点，没有分支。</p>
      <p v-if="hiddenBranches > 0" class="mt-1 text-[11px] text-slate-400">
        ……还有 {{ hiddenBranches }} 个分支
      </p>
    </details>

    <!--
      这次改动：`edit` / `expand` 意图的候选是"在当前树上的改动"，而画布优先渲染
      已应用的那棵树（用户的作品），所以候选节点根本不在画布上。没有这份清单，
      用户面对「候选已就绪」只能盲点「应用」——他不知道 agent 动了哪几个节点。
      清单是**对比两份树算出来的**（`stores/mindmap.diffCandidate`），不是按收到的
      op 帧记账：op 帧可能丢、也可能被主动丢弃，而"树到底哪里不一样"永远算得出来。
    -->
    <details
      v-if="store.candidate && diff"
      open
      class="mt-2 rounded bg-white px-2 py-1"
      aria-label="这次改动"
    >
      <summary class="cursor-pointer font-medium text-slate-700">
        这次改动（{{ diff.length }} 处）
      </summary>
      <p v-if="!diff.length" class="mt-1 text-[11px] text-slate-400">
        候选和当前画布一模一样，没有改动。
      </p>
      <ul v-else class="mt-1 flex flex-col gap-1 text-[11px] leading-relaxed">
        <li v-for="item in shownDiff" :key="`${item.kind}:${item.id}`" class="break-words">
          <span class="mr-1 rounded px-1" :class="DIFF_CLASS[item.kind]">{{ DIFF_LABELS[item.kind] }}</span>
          <button
            type="button"
            class="text-left text-slate-700 underline decoration-slate-300 hover:text-slate-900"
            :title="`在画布上定位「${item.label}」`"
            @click="jumpTo(item.id)"
          >{{ item.label }}</button>
          <span class="ml-1 text-slate-400">{{ item.detail }}</span>
        </li>
      </ul>
      <p v-if="hiddenDiff > 0" class="mt-1 text-[11px] text-slate-400">……还有 {{ hiddenDiff }} 处</p>
    </details>

    <div v-if="canAccept" class="mt-2 flex flex-col gap-1">
      <p class="leading-relaxed text-slate-600">候选已就绪，应用后才会改动画布。</p>
      <div class="flex gap-2">
        <button
          type="button"
          class="rounded-md bg-slate-900 px-3 py-1.5 text-white"
          @click="accept"
        >应用到画布</button>
        <button
          type="button"
          class="rounded-md border border-slate-300 px-3 py-1.5 text-slate-700"
          @click="discard"
        >丢弃</button>
      </div>
    </div>
  </section>
</template>
