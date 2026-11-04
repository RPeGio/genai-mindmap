import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { startRecording } from '../src/audio/recorder';
import { encodeWav } from '../src/audio/wav';

function ascii(bytes: Uint8Array, offset: number, length: number): string {
  return String.fromCharCode(...Array.from(bytes.slice(offset, offset + length)));
}

describe('encodeWav', () => {
  it('写出合法的 RIFF/WAVE 头', async () => {
    const blob = encodeWav(new Float32Array([0, 0.5, -0.5]), 16000);
    const bytes = new Uint8Array(await blob.arrayBuffer());
    const view = new DataView(bytes.buffer);

    expect(ascii(bytes, 0, 4)).toBe('RIFF');
    expect(ascii(bytes, 8, 4)).toBe('WAVE');
    expect(ascii(bytes, 12, 4)).toBe('fmt ');
    expect(ascii(bytes, 36, 4)).toBe('data');

    expect(view.getUint32(4, true)).toBe(36 + 6); // 除前 8 字节外的总长
    expect(view.getUint32(16, true)).toBe(16); // fmt 块长度
    expect(view.getUint16(20, true)).toBe(1); // PCM
    expect(view.getUint16(22, true)).toBe(1); // 单声道
    expect(view.getUint32(24, true)).toBe(16000); // 采样率
    expect(view.getUint32(28, true)).toBe(32000); // 每秒字节数
    expect(view.getUint16(32, true)).toBe(2); // 每帧字节数
    expect(view.getUint16(34, true)).toBe(16); // 位深
    expect(view.getUint32(40, true)).toBe(6); // data 块长度
    expect(bytes.length).toBe(44 + 6);
    expect(blob.type).toBe('audio/wav');
  });

  it('把浮点采样映射到 int16 并夹紧越界值', async () => {
    const samples = new Float32Array([0, 1, -1, 2, -2]);
    const bytes = new Uint8Array(await encodeWav(samples, 16000).arrayBuffer());
    const view = new DataView(bytes.buffer);

    expect(view.getInt16(44, true)).toBe(0);
    expect(view.getInt16(46, true)).toBe(32767);
    expect(view.getInt16(48, true)).toBe(-32768);
    // 越界值必须夹紧，否则会溢出成反相的音爆
    expect(view.getInt16(50, true)).toBe(32767);
    expect(view.getInt16(52, true)).toBe(-32768);
  });

  it('空采样只产出文件头', async () => {
    const blob = encodeWav(new Float32Array(0), 16000);
    expect(blob.size).toBe(44 + 0);
  });
});

class FakeProcessor {
  static latest: FakeProcessor | null = null;

  onaudioprocess: ((event: unknown) => void) | null = null;

  constructor() {
    FakeProcessor.latest = this;
  }

  connect(): void {}

  disconnect(): void {
    this.onaudioprocess = null;
  }

  emit(samples: number[]): void {
    this.onaudioprocess?.({
      inputBuffer: { getChannelData: () => new Float32Array(samples) },
    });
  }
}

class FakeAudioContext {
  readonly sampleRate: number;

  readonly destination = {};

  constructor(options?: { sampleRate?: number }) {
    this.sampleRate = options?.sampleRate ?? 48000;
  }

  async resume(): Promise<void> {}

  async close(): Promise<void> {}

  createMediaStreamSource() {
    return { connect: () => {}, disconnect: () => {} };
  }

  createScriptProcessor() {
    return new FakeProcessor();
  }
}

describe('startRecording', () => {
  beforeEach(() => {
    FakeProcessor.latest = null;
    vi.stubGlobal('AudioContext', FakeAudioContext);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('没有 getUserMedia 时提示需要 HTTPS', async () => {
    // 部署到 http://IP:port 时就是这个状态，报错必须指向协议问题
    vi.stubGlobal('navigator', { mediaDevices: {} });
    await expect(startRecording()).rejects.toThrow(/HTTPS/);
  });

  it('权限被拒时给出可操作的提示', async () => {
    const denied = new Error('denied');
    denied.name = 'NotAllowedError';
    vi.stubGlobal('navigator', {
      mediaDevices: { getUserMedia: vi.fn().mockRejectedValue(denied) },
    });
    await expect(startRecording()).rejects.toThrow(/权限/);
  });

  it('没有麦克风设备时提示找不到设备', async () => {
    const missing = new Error('no device');
    missing.name = 'NotFoundError';
    vi.stubGlobal('navigator', {
      mediaDevices: { getUserMedia: vi.fn().mockRejectedValue(missing) },
    });
    await expect(startRecording()).rejects.toThrow(/没有找到麦克风/);
  });

  it('采集到的数据编码成 16kHz WAV 文件', async () => {
    const track = { stop: vi.fn() };
    vi.stubGlobal('navigator', {
      mediaDevices: {
        getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [track] }),
      },
    });

    const recording = await startRecording();
    const processor = FakeProcessor.latest as FakeProcessor;
    expect(processor).not.toBeNull();

    processor.emit([0.1, 0.2, 0.3, 0.4]);
    processor.emit([0.5, 0.6]);

    const file = await recording.stop();

    expect(file.name).toMatch(/\.wav$/);
    expect(file.type).toBe('audio/wav');
    expect(file.size).toBe(44 + 6 * 2);
    // 停止后必须释放麦克风，否则标签页上的录音指示灯一直亮
    expect(track.stop).toHaveBeenCalled();
  });

  it('stop 之后再 stop 会报错', async () => {
    vi.stubGlobal('navigator', {
      mediaDevices: {
        getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [{ stop: vi.fn() }] }),
      },
    });

    const recording = await startRecording();
    (FakeProcessor.latest as FakeProcessor).emit([0.1, 0.2]);
    await recording.stop();
    await expect(recording.stop()).rejects.toThrow(/已经结束/);
  });

  it('一个采样都没采到时报错而不是产出空文件', async () => {
    vi.stubGlobal('navigator', {
      mediaDevices: {
        getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [{ stop: vi.fn() }] }),
      },
    });

    const recording = await startRecording();
    await expect(recording.stop()).rejects.toThrow(/没有采集到/);
  });
});
