<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue';

import { RecordingError, startRecording, type ActiveRecording } from '../audio/recorder';

const props = defineProps<{ disabled?: boolean }>();

const emit = defineEmits<{
  recorded: [file: File];
  failed: [message: string];
  active: [value: boolean];
}>();

// 一句话识别单次上限 60 秒。超过会切成多段识别，耗时线性上升，
// 所以这里只提示不拦截——用户偶尔录长一点是合理需求。
const HINT_SECONDS = 60;
// 硬上限 600 秒 ≈ 19MB，与后端 MAX_AUDIO_BYTES(20MB) 同口径，
// 到点自动停止，免得录完才发现传不上去。
const HARD_LIMIT_SECONDS = 600;

const state = ref<'idle' | 'starting' | 'recording' | 'finishing'>('idle');
const seconds = ref(0);
const error = ref('');

let recording: ActiveRecording | null = null;
let ticker: number | undefined;
let disposed = false;

const label = computed(() => {
  if (state.value === 'starting') return '正在启动麦克风…';
  if (state.value === 'finishing') return '正在整理音频…';
  if (state.value === 'recording') return `停止并添加（${format(seconds.value)}）`;
  return '录音添加来源';
});

function format(total: number): string {
  const minutes = Math.floor(total / 60);
  const rest = total % 60;
  return `${String(minutes).padStart(2, '0')}:${String(rest).padStart(2, '0')}`;
}

function stopTicker(): void {
  if (ticker !== undefined) {
    window.clearInterval(ticker);
    ticker = undefined;
  }
}

async function begin(): Promise<void> {
  error.value = '';
  state.value = 'starting';
  emit('active', true);
  try {
    recording = await startRecording();
    if (disposed) {
      recording.cancel();
      recording = null;
      return;
    }
    state.value = 'recording';
    seconds.value = 0;
    // 用 500ms 轮询而不是每秒 +1：标签页切到后台时 setInterval 会被节流，
    // 累加出来的时间会偏小，而 elapsedSeconds 读的是真实时钟。
    ticker = window.setInterval(() => {
      seconds.value = Math.floor(recording?.elapsedSeconds() ?? 0);
      if (seconds.value >= HARD_LIMIT_SECONDS) void finish();
    }, 500);
  } catch (err) {
    state.value = 'idle';
    error.value = err instanceof RecordingError ? err.message : '录音启动失败';
    emit('failed', error.value);
    emit('active', false);
  }
}

async function finish(): Promise<void> {
  if (!recording) return;
  stopTicker();
  state.value = 'finishing';
  const current = recording;
  recording = null;
  try {
    const file = await current.stop();
    state.value = 'idle';
    emit('recorded', file);
  } catch (err) {
    state.value = 'idle';
    error.value = err instanceof RecordingError ? err.message : '录音处理失败';
    emit('failed', error.value);
  } finally {
    emit('active', false);
  }
}

function toggle(): void {
  if (state.value === 'recording') {
    void finish();
    return;
  }
  if (state.value === 'idle') void begin();
}

onBeforeUnmount(() => {
  disposed = true;
  stopTicker();
  recording?.cancel();
  recording = null;
});
</script>

<template>
  <div class="flex flex-col gap-1">
    <button
      type="button"
      class="rounded-md border px-3 py-2 text-xs transition"
      :class="state === 'recording'
        ? 'border-red-300 bg-red-50 text-red-700 hover:bg-red-100'
        : 'border-slate-300 text-slate-600 hover:border-slate-400 hover:bg-slate-50'"
      :disabled="(props.disabled && state === 'idle') || state === 'starting' || state === 'finishing'"
      @click="toggle"
    >
      {{ label }}
    </button>

    <p v-if="state === 'recording' && seconds >= HINT_SECONDS" class="text-xs text-amber-700">
      已超过 60 秒，将分段识别，转写会慢一些
    </p>
    <p v-if="state === 'recording'" class="text-xs text-slate-400">
      录音在本地进行，停止后才会上传转写。
    </p>
    <p v-if="error" class="text-xs leading-relaxed text-red-600" role="alert">{{ error }}</p>
  </div>
</template>
