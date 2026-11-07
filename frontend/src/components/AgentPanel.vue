<script setup lang="ts">
import { computed } from 'vue';

import { agentToolLabel, useMindMapStore } from '../stores/mindmap';

/**
 * agent 运行面板：工具进度、候选骨架、候选确认、丢弃。
 *
 * 候选**不会自动应用**。这是这次改造的核心约束：模型算出来的是一份提案，
 * 用户点「应用」才算数。自动应用等于把「重新生成冲掉我的编辑」换个说法重演。
 */
const store = useMindMapStore();

const visible = computed(() =>
  store.agentEnabled
  && (store.agentRunning || store.candidate !== null || store.agentEvents.length > 0 || !!store.agentError),
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

/** 只把工具调用摘出来显示；stage 文案已经有单独的状态行了。 */
interface ToolRow {
  key: string;
  state: 'running' | 'done';
  ok: boolean;
  /** 已经翻成人话的整行文案，模板不再拼字符串 */
  text: string;
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
      });
    } else if (event.type === 'tool_end') {
      if (!byId.has(event.call_id)) order.push(event.call_id);
      byId.set(event.call_id, {
        key: event.call_id,
        state: 'done',
        ok: event.ok,
        text: rowText(event.name, 'done', event.ok, event.latency_ms ?? 0),
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

const canAccept = computed(() => store.candidate !== null && !store.agentRunning);

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

    <ol v-if="calls.length" class="mt-2 flex flex-col gap-1">
      <li
        v-for="call in calls"
        :key="call.key"
        class="rounded bg-white px-2 py-1"
      >
        <span :class="call.state === 'running' ? 'text-slate-500' : call.ok ? 'text-emerald-700' : 'text-rose-700'">
          {{ call.text }}
        </span>
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
