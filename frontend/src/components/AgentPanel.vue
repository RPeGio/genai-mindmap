<script setup lang="ts">
import { computed } from 'vue';

import { useMindMapStore } from '../stores/mindmap';

/**
 * agent 运行面板：工具进度、候选确认、丢弃。
 *
 * 候选**不会自动应用**。这是这次改造的核心约束：模型算出来的是一份提案，
 * 用户点「应用」才算数。自动应用等于把「重新生成冲掉我的编辑」换个说法重演。
 */
const store = useMindMapStore();

const visible = computed(() =>
  store.agentEnabled
  && (store.agentRunning || store.candidate !== null || store.agentEvents.length > 0 || !!store.agentError),
);

/** 只把工具调用摘出来显示；stage 文案已经有单独的状态行了。 */
interface ToolRow {
  key: string;
  name: string;
  state: 'running' | 'done';
  ok: boolean;
  ms: number;
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
        key: event.call_id, name: event.name, state: 'running', ok: true, ms: 0,
      });
    } else if (event.type === 'tool_end') {
      if (!byId.has(event.call_id)) order.push(event.call_id);
      byId.set(event.call_id, {
        key: event.call_id,
        name: event.name,
        state: 'done',
        ok: event.ok,
        ms: event.latency_ms ?? 0,
      });
    }
  }
  return order.map(id => byId.get(id)!);
});

const canAccept = computed(() => store.candidate !== null && !store.agentRunning);

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
        class="flex items-center justify-between rounded bg-white px-2 py-1"
      >
        <span class="font-mono text-[11px] text-slate-600">{{ call.name }}</span>
        <span :class="call.state === 'running' ? 'text-slate-400' : call.ok ? 'text-emerald-700' : 'text-rose-700'">
          {{ call.state === 'running' ? '执行中…' : call.ok ? `完成 ${call.ms}ms` : '未成功' }}
        </span>
      </li>
    </ol>

    <p v-if="store.agentError" role="alert" class="mt-2 leading-relaxed text-rose-700">
      {{ store.agentError }}
    </p>

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
