<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue';
import { ApiError, generateMindMapStream } from './api/client';
import type { MindMapContent, MindNode } from './api/types';

const prompt = ref('');
const content = ref<MindMapContent | null>(null);
const mode = ref<'demo' | 'model' | null>(null);
const busy = ref(false);
const status = ref('输入主题或文本后生成。先出骨架，再补细节。');
/** 当前阶段文案 + 已等待秒数，避免长时间没有反馈时界面看起来像卡死。 */
const stageText = ref('');
const waitedMs = ref(0);
const degraded = ref(false);

let controller: AbortController | null = null;

interface Row {
  id: string;
  label: string;
  depth: number;
}

/** R1/R2 先用扁平列表验证链路；画布在 R3 接入。 */
const rows = computed<Row[]>(() => {
  const out: Row[] = [];
  const walk = (node: MindNode, depth: number) => {
    out.push({ id: node.id, label: node.label, depth });
    node.children.forEach((child) => walk(child, depth + 1));
  };
  if (content.value) walk(content.value.root, 0);
  return out;
});

const nodeCount = computed(() => rows.value.length);

/**
 * 把增量节点合并进当前树。
 * 同一个 id 会反复到达（文字逐渐变长），所以已存在时更新文字、保留其子节点。
 */
function upsert(node: MindNode, parentId: string, incoming: MindNode): MindNode {
  if (node.id === parentId) {
    const idx = node.children.findIndex(c => c.id === incoming.id);
    const children = idx >= 0
      ? node.children.map((c, i) => (i === idx ? { ...incoming, children: c.children } : c))
      : [...node.children, incoming];
    return { ...node, children };
  }
  return { ...node, children: node.children.map(c => upsert(c, parentId, incoming)) };
}

function applyNode(parentId: string | null, incoming: MindNode) {
  if (!content.value) {
    content.value = { title: incoming.label, root: incoming };
    return;
  }
  const root = content.value.root;
  if (parentId === null) {
    content.value = { ...content.value, root: { ...incoming, children: root.children } };
  } else {
    content.value = { ...content.value, root: upsert(root, parentId, incoming) };
  }
}

async function generate() {
  const text = prompt.value.trim();
  if (!text) {
    status.value = '请输入主题或文本。';
    return;
  }
  if (busy.value) return;

  // 先把上一份结果存起来：新请求失败时恢复它，而不是让用户白等一场还丢了旧图。
  const previous = content.value;
  const previousMode = mode.value;

  busy.value = true;
  degraded.value = false;
  waitedMs.value = 0;
  stageText.value = '正在连接';
  content.value = null;
  mode.value = null;
  const ctrl = new AbortController();
  controller = ctrl;
  // settled 表示这次请求已经有了明确结局（成功或明确失败）；
  // 只有「既没 done 也没 error」才说明连接是被掐断的。
  let settled = false;

  try {
    await generateMindMapStream(text, (event) => {
      switch (event.type) {
        case 'stage':
          stageText.value = event.detail;
          break;
        case 'skeleton':
          // 两段式方案：骨架先到，主结构立刻可见。
          content.value = event.content;
          stageText.value = '骨架已就绪，正在展开分支';
          status.value = '已显示骨架，细节还在补全中…';
          break;
        case 'node':
          // 增量方案：节点一个个长出来，同一 id 的文字会越补越全。
          applyNode(event.parentId, event.node as MindNode);
          stageText.value = '节点生成中';
          status.value = '正在生成，节点会陆续出现…';
          break;
        case 'heartbeat':
          waitedMs.value = Math.round(event.waitedMs / 1000);
          break;
        case 'done':
          settled = true;
          content.value = event.content;
          mode.value = event.mode;
          degraded.value = event.degraded;
          status.value = event.degraded
            ? `展开阶段失败，当前是骨架结果（${(event.latencyMs / 1000).toFixed(1)} 秒）。${event.reason ?? ''}`
            : event.mode === 'demo'
              ? '这是固定结构的演示数据，不是 AI 生成。'
              : `模型已生成候选导图，用时 ${(event.latencyMs / 1000).toFixed(1)} 秒。`;
          stageText.value = '';
          break;
        case 'error':
          settled = true;
          // 后端在流里报错时，之前那份导图同样要还回来，不能只留一个空画布。
          content.value = previous;
          mode.value = previousMode;
          degraded.value = false;
          status.value = event.message;
          stageText.value = '';
          break;
      }
    }, ctrl.signal);
    if (ctrl.signal.aborted) {
      // 主动取消：把画面退回取消前的样子，而不是留下一堆半截节点。
      content.value = previous;
      mode.value = previousMode;
      status.value = '已取消。';
    } else if (!settled) {
      // 流正常结束但没有 done 帧：中途断了，不能假装成功。
      content.value = previous;
      mode.value = previousMode;
      status.value = '连接中断，结果可能不完整，请重试。';
    }
  } catch (error) {
    // 已经被后来的请求接管：旧请求的错误不许再改画面和状态，
    // 否则会出现「新请求的预览被旧导图替换、状态显示已取消」这种鬼现象。
    if (controller !== null && controller !== ctrl) return;
    content.value = previous;
    mode.value = previousMode;
    status.value = ctrl.signal.aborted
      ? '已取消。'
      : error instanceof ApiError
        ? error.message
        : '生成失败，请重试。';
    stageText.value = '';
  } finally {
    // 取消后立刻重新生成时，旧请求的收尾不能把新请求的状态清掉，
    // 否则「取消」按钮会突然消失、busy 被打回 false。
    if (controller === ctrl) {
      busy.value = false;
      controller = null;
    }
  }
}

function cancel() {
  const active = controller;
  controller = null;
  active?.abort();
  busy.value = false;
  stageText.value = '';
  status.value = '已取消。';
}

onBeforeUnmount(() => controller?.abort());
</script>

<template>
  <div class="flex h-full flex-col">
    <header class="border-b border-slate-200 px-6 py-4">
      <h1 class="text-lg font-semibold">生成式 AI 思维导图</h1>
      <p class="text-sm text-slate-500">文本 → 思维导图（画布编辑为 R3 目标）</p>
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
        <div class="mt-3 flex gap-2">
          <button
            type="button"
            :disabled="busy"
            class="flex-1 rounded bg-slate-900 px-4 py-2 text-sm text-white disabled:opacity-50"
            @click="generate"
          >
            {{ busy ? '生成中…' : '生成导图' }}
          </button>
          <button
            v-if="busy"
            type="button"
            class="rounded border border-slate-300 px-3 py-2 text-sm"
            @click="cancel"
          >
            取消
          </button>
        </div>

        <p class="mt-3 text-sm text-slate-600" role="status">{{ status }}</p>

        <!-- 等待期间给出内容级进度，而不是一个假的百分比条 -->
        <div v-if="busy && stageText" class="mt-2 rounded bg-slate-50 px-3 py-2 text-xs text-slate-500">
          <span class="inline-block h-2 w-2 animate-pulse rounded-full bg-emerald-500" />
          <span class="ml-2">{{ stageText }}</span>
          <span v-if="waitedMs > 0" class="ml-1 text-slate-400">已等待 {{ waitedMs }} 秒</span>
        </div>
      </aside>

      <main class="min-w-0 flex-1 overflow-auto p-6">
        <div v-if="content" class="space-y-3">
          <div class="flex items-baseline gap-3">
            <h2 class="text-base font-semibold">{{ content.title }}</h2>
            <span v-if="mode" class="rounded bg-slate-100 px-2 py-0.5 text-xs text-slate-600">
              {{ mode === 'demo' ? '演示模式' : '模型生成' }}
            </span>
            <span v-if="degraded" class="rounded bg-amber-100 px-2 py-0.5 text-xs text-amber-700">
              骨架结果
            </span>
            <span class="text-xs text-slate-400">{{ nodeCount }} 个节点</span>
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
        <p v-else class="text-sm text-slate-400">
          {{ busy ? '正在生成，稍候…' : '还没有导图，先在左侧输入内容。' }}
        </p>
      </main>
    </div>
  </div>
</template>
