"""语音转写（F07）：阿里云百炼 Qwen-Audio-ASR。

流程位置：
    录音 / 音频文件 ──transcribe_audio()──> Source(kind=audio) ──> pipeline

**为什么用这个接口**：它在一次同步调用里同时返回文本、句级时间戳和词级
时间戳。之前用过 ISI 的「一句话识别」，只有整段文本、没有句子边界，
节点无法指回「这句话在录音的第几秒」。同一批音频上的实测对比：

                    一句话识别          Qwen-Audio-ASR
    60 秒耗时       8.65 s             3.1 s
    60 秒成本       0.0035 元          0.00091 元（按 token）
    单次时长上限    60 秒              300 秒
    句子/词时间戳   无                 有

**不用本地模型的理由**（与本接口无关，但同属选型记录）：sherpa-onnx 的
paraformer-zh-small 在 2C2G 上常驻 143 MB、短音频 RTF 0.013，但架构吃不下
长输入——33.8 秒整段 CER 86.4%，60 秒直接返回空串并抛 onnxruntime Reshape
错误，只能处理 10 秒以内的片段。

**服务端硬边界（实测）**：
  - 时长 ≤300 秒。360 秒返回 400 `AUDIO_DURATION_TOO_LONG`，是明确报错，
    不是静默截断
  - 同时给 300 秒和 120 秒的多样化音频都正常，文本字数与期望值差 3 字以内
  - 注意：用**同一句循环**拼出来的长音频会让模型不停复读，那是测试素材
    的问题，不是接口的问题。验证边界必须用内容多样的音频
  - 采样率 8k/16k，单声道（立体声在这里先拆声道再分别识别）

**凭证**：只需要 `DASHSCOPE_API_KEY` 一个环境变量，在百炼控制台创建。
老域名 `dashscope.aliyuncs.com` 可直接用，不必迁到带 Workspace ID 的新域名。
"""
from __future__ import annotations

import array
import base64
import io
import json
import logging
import os
import time
import wave
from concurrent.futures import ThreadPoolExecutor

import httpx

from app.contracts.source import AudioSentence, Source, SourceKind

logger = logging.getLogger(__name__)

# 服务端硬限制。不做成环境变量——这是产品规格，改大了只会收到 400。
MAX_SLICE_SECONDS = 300
SUPPORTED_RATES = (8000, 16000)

ENDPOINT = ("https://dashscope.aliyuncs.com/api/v1/services/"
            "aigc/multimodal-generation/generation")
MODEL = "qwen-audio-3.1-asr-flash"

# 单次请求超时。300 秒音频实测 12 秒返回，留足余量但不至于挂太久。
REQUEST_TIMEOUT = 300.0

# 双声道两路并发。API 侧按 RPM 600 限流，两路远够。
MAX_WORKERS = 2


class AsrError(Exception):
    """语音转写失败。由路由层转成 422，不返回空 Source。"""


def _api_key() -> str:
    key = os.getenv("DASHSCOPE_API_KEY", "").strip()
    if not key:
        raise AsrError(
            "语音转写未配置：缺少环境变量 DASHSCOPE_API_KEY；"
            "在阿里云百炼控制台创建后填入，详见 docs/DEPLOY.md 的「语音输入」")
    return key


def _decode_wav(raw: bytes) -> tuple[bytes, int, float, int]:
    """读 WAV 头。返回裸 PCM、采样率、时长、声道数。"""
    try:
        with wave.open(io.BytesIO(raw), "rb") as handle:
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            rate = handle.getframerate()
            frames = handle.getnframes()
            pcm = handle.readframes(frames)
    except (wave.Error, EOFError) as exc:
        raise AsrError("音频不是合法的 WAV 文件") from exc

    if channels not in (1, 2):
        raise AsrError(f"音频是 {channels} 声道，只支持单声道或双声道")
    if width != 2:
        raise AsrError(f"音频位宽 {width * 8} bit，语音识别只接受 16 bit")
    if rate not in SUPPORTED_RATES:
        raise AsrError(f"音频采样率 {rate} Hz，只接受 "
                       f"{' / '.join(map(str, SUPPORTED_RATES))} Hz")
    if not pcm:
        raise AsrError("音频内容为空")

    return pcm, rate, frames / rate, channels


def _split_stereo(pcm: bytes) -> tuple[bytes, bytes]:
    """拆开交错的立体声 PCM。每 4 字节是一组 L(2) + R(2)。"""
    samples = array.array("h")
    samples.frombytes(pcm)
    return samples[0::2].tobytes(), samples[1::2].tobytes()


def _to_wav(pcm: bytes, rate: int) -> bytes:
    """把裸 PCM 包回 WAV。接口要的是完整文件而不是裸流。"""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm)
    return buffer.getvalue()


def _split(pcm: bytes, rate: int) -> list[bytes]:
    """按 300 秒切片。最后一片可能不足 300 秒，照常送去识别。"""
    limit = MAX_SLICE_SECONDS * rate * 2   # 16bit = 2 字节/样本
    chunks = [pcm[start:start + limit] for start in range(0, len(pcm), limit)]
    return [_to_wav(chunk, rate) for chunk in chunks]


def _parse_sentence(body: dict) -> dict:
    """从响应里取出 sentence。

    实测响应多包了一层：`output.output.sentence`；官方示例写的是
    `output.sentence`。两种都认，免得换模型版本就解析不出来。
    """
    output = body.get("output") or {}
    inner = output.get("output")
    if not isinstance(inner, dict):
        inner = output
    return inner.get("sentence") or {}


def _recognize_one(wav_bytes: bytes, rate: int, offset_ms: int) -> dict:
    """识别一段 ≤300 秒的 WAV，返回 {text, sentences, usage}。

    offset_ms 是这一段在原始音频里的起始偏移：拼接多段时要把时间戳平移，
    否则第二段的所有时间都会从 0 重新开始，节点会指回错误的位置。
    """
    data_uri = "data:audio/wav;base64," + base64.b64encode(wav_bytes).decode()
    payload = {
        "model": MODEL,
        "input": {"messages": [{
            "role": "user",
            "content": [{"type": "input_audio", "input_audio": {"data": data_uri}}],
        }]},
        "parameters": {"format": "wav", "sample_rate": str(rate)},
    }
    headers = {
        "Authorization": f"Bearer {_api_key()}",
        "Content-Type": "application/json",
        # 非流式：时间戳同样返回，还免去拼接 SSE 事件
        "X-DashScope-SSE": "disable",
    }

    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            response = client.post(ENDPOINT, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        raise AsrError(f"语音识别请求失败：{type(exc).__name__}") from exc

    if response.status_code != 200:
        detail = ""
        try:
            error = response.json()
            # code 和 message 都带上：code 是稳定的机器可读标识（比如
            # AUDIO_DURATION_TOO_LONG），message 是给人看的说明。只留一个
            # 都会让排查时缺一半信息。
            code = str(error.get("code") or "").strip()
            message = str(error.get("message") or "").strip()
            detail = " ".join(part for part in (code, message) if part)
        except ValueError:
            detail = response.text[:120]
        raise AsrError(f"语音识别失败 HTTP {response.status_code}：{detail}")

    body = response.json()
    sentence = _parse_sentence(body)
    if not sentence:
        raise AsrError("语音识别返回里没有句子结果")

    words = sentence.get("words") or []
    sentences = [{
        "text": sentence.get("text", ""),
        "begin_ms": int(sentence.get("begin_time", 0)) + offset_ms,
        "end_ms": int(sentence.get("end_time", 0)) + offset_ms,
        "words": [{
            "text": word.get("text", ""),
            "begin_ms": int(word.get("begin_time", 0)) + offset_ms,
            "end_ms": int(word.get("end_time", 0)) + offset_ms,
        } for word in words],
    }]
    return {
        "text": sentence.get("text", "") or "",
        "sentences": sentences,
        "usage": body.get("usage") or {},
    }


def _recognize_lane(pieces: list[bytes], rate: int) -> dict:
    """识别一条声道（可能被切成多段），把时间戳按段偏移拼接。"""
    texts: list[str] = []
    sentences: list[dict] = []
    total_usage = {"duration": 0, "input_tokens": 0, "output_tokens": 0}

    offset = 0
    for piece in pieces:
        result = _recognize_one(piece, rate, offset)
        texts.append(result["text"])
        sentences.extend(result["sentences"])
        for key in total_usage:
            total_usage[key] += int(result["usage"].get(key) or 0)
        # 下一段的起点 = 这一段自己的时长（从 WAV 帧数算，不用服务端返回的）
        offset += int(round((len(piece) - 44) / 2 / rate * 1000))

    return {
        "text": "".join(texts).strip(),
        "sentences": sentences,
        "usage": total_usage,
    }


def _nearly_identical(left: bytes, right: bytes, step: int = 97) -> bool:
    """抽样比对两路是否几乎相同（单麦被浏览器 upmix 成立体声的情形）。"""
    a = array.array("h")
    b = array.array("h")
    a.frombytes(left)
    b.frombytes(right)
    count = min(len(a), len(b))
    if count == 0:
        return True
    diff = 0
    scale = 0
    for index in range(0, count, step):
        diff += abs(a[index] - b[index])
        scale += abs(a[index]) + 1
    return scale > 0 and diff / scale < 0.01


def _normalize(text: str) -> str:
    return "".join(c for c in text.lower()
                   if c.isalnum() or "\u4e00" <= c <= "\u9fff")


def _edit_distance(a: str, b: str) -> int:
    if not a:
        return len(b)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1,
                               previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def _similarity(a: str, b: str) -> float:
    left, right = _normalize(a), _normalize(b)
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    return 1 - _edit_distance(left, right) / max(len(left), len(right))


def _merge_lanes(left: dict, right: dict) -> dict:
    """合并两个声道。

    实测三种情形：一路为空取另一路；相似度 > 80% 取一路（单麦 upmix）；
    内容确实不同则两段都留（双人分轨录音，丢任何一路都是信息损失）。
    """
    if not _normalize(left["text"]):
        return right
    if not _normalize(right["text"]):
        return left
    if _similarity(left["text"], right["text"]) > 0.8:
        return left if len(left["text"]) >= len(right["text"]) else right

    return {
        "text": f"{left['text']}\n{right['text']}",
        "sentences": left["sentences"] + right["sentences"],
        "usage": {key: left["usage"].get(key, 0) + right["usage"].get(key, 0)
                  for key in set(left["usage"]) | set(right["usage"])},
    }


def transcribe_audio(filename: str | None, raw: bytes) -> Source:
    """把音频转成 Source。失败抛 AsrError，不返回空正文。

    同步接口，与 parse_bytes 其余分支一致——路由层已经把它甩进线程池。
    """
    pcm, rate, duration, channels = _decode_wav(raw)
    _api_key()   # 配置预检放在最前，缺凭证要立刻暴露

    if channels == 1:
        lanes = [_split(pcm, rate)]
        lane_count = 1
    else:
        left, right = _split_stereo(pcm)
        if _nearly_identical(left, right):
            logger.info("双声道内容相同，按单声道处理")
            lanes = [_split(left, rate)]
            lane_count = 1
        else:
            lanes = [_split(left, rate), _split(right, rate)]
            lane_count = 2

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        results = list(pool.map(lambda pieces: _recognize_lane(pieces, rate), lanes))
    elapsed = time.perf_counter() - started

    merged = results[0] if len(results) == 1 else _merge_lanes(results[0], results[1])
    text = merged["text"].strip()
    if not _normalize(text):
        raise AsrError("没有从音频里识别出语音内容")

    segments = sum(len(lane) for lane in lanes)
    logger.info("语音转写完成：%.1f 秒 / %d 声道 / %d 段 → %d 字，耗时 %.2fs，"
                "用量 %s", duration, lane_count, segments, len(text), elapsed,
                merged["usage"])

    sentences = [
        AudioSentence(text=item["text"], begin_ms=item["begin_ms"],
                      end_ms=item["end_ms"])
        for item in merged["sentences"] if item.get("text")
    ]

    return Source(
        kind=SourceKind.audio,
        filename=filename,
        text=text,
        duration_seconds=round(duration, 2),
        sentences=sentences,
        status="parsed",
    )
