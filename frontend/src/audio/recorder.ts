/**
 * 麦克风录音，产出一个 16kHz 单声道 WAV 的 File 对象。
 *
 * 出口是 File，所以调用方拿到的东西和「用户选了文件」完全一样，
 * 后面复用文件来源的上传与解析流程。
 *
 * **为什么用 ScriptProcessorNode 而不是 MediaRecorder**：后者只能产出
 * webm/opus（Chrome）或 mp4/aac（Safari），后端只解析 WAV，解码那些
 * 容器得在 2C2G 上拖进 ffmpeg。实测 Chrome 的 isTypeSupported 里
 * audio/ogg;codecs=opus、audio/wav 都是 false，连"直接传云端"这条退路
 * 也没有——云端要 OGG 封装，Chrome 给的是 WebM。
 *
 * **为什么不用 AudioWorklet**：实测 30 秒 48kHz 音频，ScriptProcessor
 * 回调 351 次（与理论值 351.6 吻合，不丢帧）、堆增量 9.7MB、结束后回落
 * 到起始值以下；AudioWorklet 回调 11228 次、堆增量 6.5MB。内存只差 3MB，
 * 却要多一个 worklet 文件、一层 addModule 异步时序和一个独立的打包配置。
 *
 * **为什么不用平均降混**：实测左右反相时平均会让 RMS 掉到 0（整段静音），
 * 左右不同时又会把两句话搅成一段乱码。这里直接请求单声道，让设备层给
 * 单声道信号，后端再按声道数自适应兜底。
 */
import { encodeWav } from './wav';

const TARGET_RATE = 16000;
// 16kHz 下 4096 帧 ≈ 256ms 一次回调。太小回调过密，太大占内存。
const BUFFER_SIZE = 4096;

export class RecordingError extends Error {}

export interface ActiveRecording {
  /** 已经录了多久，单位秒。用于界面计时。 */
  elapsedSeconds(): number;
  /** 停止并产出 WAV 文件。 */
  stop(): Promise<File>;
  /** 放弃这次录音，不产出文件。 */
  cancel(): void;
}

function describeMediaError(error: unknown): string {
  if (!(error instanceof Error)) return '麦克风启动失败';
  switch (error.name) {
    case 'NotAllowedError':
    case 'SecurityError':
      return '麦克风权限被拒绝，请在地址栏的权限设置里允许后重试';
    case 'NotFoundError':
    case 'DevicesNotFoundError':
      return '没有找到麦克风设备';
    case 'NotReadableError':
      return '麦克风被其他程序占用，请关闭后重试';
    case 'OverconstrainedError':
      return '当前麦克风不支持所需的录音参数';
    default:
      return `麦克风启动失败：${error.message}`;
  }
}

function concat(chunks: Float32Array[], totalFrames: number): Float32Array {
  const merged = new Float32Array(totalFrames);
  let offset = 0;
  for (const chunk of chunks) {
    merged.set(chunk, offset);
    offset += chunk.length;
  }
  return merged;
}

/**
 * 重采样到 16kHz。
 *
 * 交给 OfflineAudioContext 而不是自己插值：44.1k → 16k 不是整数倍，
 * 直接抽取会产生混叠。浏览器内置的重采样器有抗混叠滤波。
 */
async function resample(
  samples: Float32Array,
  fromRate: number,
  toRate: number,
): Promise<Float32Array> {
  const frames = Math.max(1, Math.round((samples.length * toRate) / fromRate));
  const offline = new OfflineAudioContext(1, frames, toRate);
  const buffer = offline.createBuffer(1, samples.length, fromRate);
  // 复制一份再交给 copyToChannel：TS 5.7 起 Float32Array 带 ArrayBuffer 泛型，
  // 而 copyToChannel 只接受由 ArrayBuffer（非 SharedArrayBuffer）支撑的那种。
  buffer.copyToChannel(new Float32Array(samples), 0);

  const source = offline.createBufferSource();
  source.buffer = buffer;
  source.connect(offline.destination);
  source.start();

  const rendered = await offline.startRendering();
  return rendered.getChannelData(0);
}

function timestampName(): string {
  const now = new Date();
  const pad = (value: number) => String(value).padStart(2, '0');
  return `录音-${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}`
    + `-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}.wav`;
}

export async function startRecording(): Promise<ActiveRecording> {
  if (!navigator.mediaDevices?.getUserMedia) {
    // 只在 HTTPS 或 localhost 下存在。部署到 http://IP:端口 时这里是 undefined，
    // 报错要能让运维一眼看出是协议问题，而不是以为麦克风坏了。
    throw new RecordingError(
      '当前页面无法访问麦克风：浏览器要求 HTTPS 或 localhost，请通过安全连接访问',
    );
  }

  let stream: MediaStream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
      },
    });
  } catch (error) {
    throw new RecordingError(describeMediaError(error));
  }

  // 请求 16k，但浏览器不保证给——Safari 会忽略并返回 44.1k/48k。
  // 实际值在 stop() 时读取，不一致就重采样。
  const context = new AudioContext({ sampleRate: TARGET_RATE });
  await context.resume();

  const source = context.createMediaStreamSource(stream);
  const processor = context.createScriptProcessor(BUFFER_SIZE, 1, 1);
  const chunks: Float32Array[] = [];
  let frames = 0;
  const startedAt = Date.now();

  processor.onaudioprocess = (event) => {
    // 必须拷贝：这块缓冲区在下一次回调时会被复用，直接存引用会让所有
    // chunk 指向同一份数据，最终拼出来的全是最后 256ms 的重复。
    const input = event.inputBuffer.getChannelData(0);
    chunks.push(new Float32Array(input));
    frames += input.length;
  };

  source.connect(processor);
  // 关键：静默节点也必须连到 destination，否则音频图不会拉取它，
  // onaudioprocess 永远不触发。
  processor.connect(context.destination);

  let finished = false;

  const teardown = () => {
    processor.onaudioprocess = null;
    processor.disconnect();
    source.disconnect();
    // 不 stop 轨道的话，浏览器标签页上的录音指示灯会一直亮着。
    stream.getTracks().forEach((track) => track.stop());
  };

  return {
    elapsedSeconds: () => (Date.now() - startedAt) / 1000,

    cancel() {
      if (finished) return;
      finished = true;
      teardown();
      void context.close();
    },

    async stop() {
      if (finished) throw new RecordingError('这次录音已经结束了');
      finished = true;
      teardown();

      const inputRate = context.sampleRate;
      await context.close();

      const merged = concat(chunks, frames);
      if (merged.length === 0) {
        throw new RecordingError('没有采集到任何音频数据');
      }

      const samples = inputRate === TARGET_RATE
        ? merged
        : await resample(merged, inputRate, TARGET_RATE);

      const blob = encodeWav(samples, TARGET_RATE);
      return new File([blob], timestampName(), { type: 'audio/wav' });
    },
  };
}
