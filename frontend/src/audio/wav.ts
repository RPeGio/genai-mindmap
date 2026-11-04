/**
 * 把 Float32 采样编码成 16bit 单声道 WAV。
 *
 * 不引库：文件头就 44 字节，而这个项目对每一个依赖都要算它在 2C2G
 * 部署机上的账。编码规则来自 RIFF 规范，字段含义在下方逐行标注。
 */

const HEADER_BYTES = 44;

function writeAscii(view: DataView, offset: number, text: string): void {
  for (let index = 0; index < text.length; index += 1) {
    view.setUint8(offset + index, text.charCodeAt(index));
  }
}

export function encodeWav(samples: Float32Array, sampleRate: number): Blob {
  const dataBytes = samples.length * 2;
  const buffer = new ArrayBuffer(HEADER_BYTES + dataBytes);
  const view = new DataView(buffer);

  writeAscii(view, 0, 'RIFF');
  view.setUint32(4, 36 + dataBytes, true);      // 除 RIFF/本字段外的总长度
  writeAscii(view, 8, 'WAVE');
  writeAscii(view, 12, 'fmt ');
  view.setUint32(16, 16, true);                 // fmt 块长度，PCM 固定 16
  view.setUint16(20, 1, true);                  // 1 = 未压缩 PCM
  view.setUint16(22, 1, true);                  // 单声道
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);     // 每秒字节数
  view.setUint16(32, 2, true);                  // 每帧字节数（1 声道 × 2 字节）
  view.setUint16(34, 16, true);                 // 位深
  writeAscii(view, 36, 'data');
  view.setUint32(40, dataBytes, true);

  let offset = HEADER_BYTES;
  for (let index = 0; index < samples.length; index += 1) {
    // 必须先夹紧：浮点运算后的值可能略微越过 ±1，直接乘会溢出成反相的音爆。
    // 负半轴乘 32768、正半轴乘 32767，因为 int16 的负数范围比正数多一个值。
    const sample = Math.max(-1, Math.min(1, samples[index] ?? 0));
    view.setInt16(offset, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true);
    offset += 2;
  }

  return new Blob([buffer], { type: 'audio/wav' });
}
