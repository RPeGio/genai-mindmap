"""语音转写（F07）：接入阿里云一句话识别。

流程位置：
    录音 / 音频文件 ──transcribe_audio()──> Source(kind=audio) ──> pipeline

**为什么选云端而不是本地模型**：本地方案实测过 sherpa-onnx 的
paraformer-zh-small（78MB），2C2G 上常驻 143MB、短音频 RTF 0.013，
但架构吃不下长输入——33.8 秒整段 CER 86.4%，60 秒直接返回空字符串
并抛 onnxruntime Reshape 错误。它只能安全处理 10 秒以内的片段，
长音频必须自己切，切了就有边界损失。

云端一句话识别在同一批音频上 CER 0%，60 秒整段正常出字，自带标点和
ITN。代价是每次 0.0035 元、依赖网络、不支持离线。2C2G 的部署机
省下 78MB 模型和 143MB 常驻内存。

**一句话识别的硬边界**：单声道 16bit、采样率 8k/16k、单次 ≤60 秒
且 ≤2MB。超长音频在这里按 60 秒切片逐段识别——单片失败即整体失败，
不静默吞掉半截内容（返回残缺正文比报错更难排查）。

**协议细节**（官方文档只给了字段表、没有完整帧示例，以下为实测所得）：
  - header 必须同时有 appkey / message_id / task_id / namespace / name，
    少任何一个都只回 MESSAGE_INVALID，且不告诉缺哪个，只能逐个试
  - StartRecognition 和 StopRecognition **两个帧都要带 appkey**。
    少在 stop 上会收到 `Missing message appkey`，文案与 start 阶段完全
    相同，日志里只能靠"握手失败"还是"识别失败"来区分
  - enable_voice_detection 必须 false：开启后句间停顿超过
    max_end_silence（默认 800ms）服务端直接返回 RecognitionCompleted
    并丢弃后续音频，不报错，结果就是只转出前半句

**凭证**：ALIYUN_AK_ID / ALIYUN_AK_SECRET / NLS_APPKEY 三个环境变量。
Token 有效期约 36 小时，这里缓存在进程内并提前 1 小时刷新，避免每次
识别多付 0.7 秒。AccessKey 建议用只授予 NLS 权限的 RAM 子账号。
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import threading
import time
import uuid
import wave

from app.contracts.source import Source, SourceKind

logger = logging.getLogger(__name__)

# 一句话识别的服务端硬限制，写死的常量不做成环境变量——
# 这是产品规格，不是部署参数，改大了只会得到 41010104 TOO_LONG_SPEECH。
MAX_SLICE_SECONDS = 60
SUPPORTED_RATES = (8000, 16000)

# 每个二进制帧 200ms（16k/16bit/单声道）。太小握手开销占比高，
# 太大会一次性压满 websocket 缓冲区。
SEND_CHUNK = 6400

# Token 提前刷新的余量：避免在识别进行到一半时刚好过期。
TOKEN_REFRESH_MARGIN = 3600
TOKEN_ENDPOINT_REGION = "cn-shanghai"   # CreateToken 固定上海，与语音网关地域无关
GATEWAY_REGION = "cn-beijing"

_token_lock = threading.Lock()
_token_cache: dict[str, object] = {"id": None, "expire": 0.0}

# 一句话识别按并发配额计费，试用版额度很小。默认 2 路，可用环境变量调大。
_concurrency = int(os.getenv("MINDMAP_ASR_CONCURRENCY", "2"))
_gate = threading.BoundedSemaphore(max(1, _concurrency))


class AsrError(Exception):
    """语音转写失败。由路由层转成 422，不返回空 Source。"""


def _config() -> tuple[str, str, str]:
    ak_id = os.getenv("ALIYUN_AK_ID") or ""
    ak_secret = os.getenv("ALIYUN_AK_SECRET") or ""
    appkey = os.getenv("NLS_APPKEY") or ""
    missing = [name for name, value in (
        ("ALIYUN_AK_ID", ak_id),
        ("ALIYUN_AK_SECRET", ak_secret),
        ("NLS_APPKEY", appkey),
    ) if not value]
    if missing:
        raise AsrError(
            f"语音转写未配置：缺少环境变量 {', '.join(missing)}；"
            f"获取方式见 docs/DEPLOY.md 的「语音输入」一节")
    return ak_id, ak_secret, appkey


def _fetch_token(ak_id: str, ak_secret: str) -> tuple[str, float]:
    """调 CreateToken。签名交给官方 SDK，不自己实现 HMAC-SHA1。"""
    try:
        from aliyunsdkcore.acs_exception.exceptions import (
            ClientException,
            ServerException,
        )
        from aliyunsdkcore.client import AcsClient
        from aliyunsdkcore.request import CommonRequest
    except ImportError as exc:  # pragma: no cover - 依赖缺失时的兜底
        raise AsrError(
            "缺少 aliyun-python-sdk-core，请先 pip install -r requirements.txt") from exc

    client = AcsClient(ak_id, ak_secret, TOKEN_ENDPOINT_REGION)
    request = CommonRequest()
    request.set_method("POST")
    request.set_domain(f"nls-meta.{TOKEN_ENDPOINT_REGION}.aliyuncs.com")
    request.set_version("2019-02-28")
    request.set_action_name("CreateToken")
    request.set_protocol_type("https")

    try:
        body = client.do_action_with_exception(request)
    except ServerException as exc:
        raise AsrError(
            f"获取语音 Token 失败 [{exc.get_error_code()}] {exc.get_error_msg()}") from exc
    except ClientException as exc:
        raise AsrError(f"获取语音 Token 请求未发出：{exc.get_error_msg()}") from exc

    data = json.loads(body)
    token = data.get("Token") or {}
    if "Id" not in token:
        raise AsrError("获取语音 Token 失败：响应中没有 Token.Id")
    return token["Id"], float(token["ExpireTime"])


def _token() -> str:
    with _token_lock:
        now = time.time()
        cached = _token_cache["id"]
        if cached and float(_token_cache["expire"]) > now + TOKEN_REFRESH_MARGIN:
            return str(cached)
        ak_id, ak_secret, _ = _config()
        token, expire = _fetch_token(ak_id, ak_secret)
        _token_cache.update(id=token, expire=expire)
        logger.info("语音 Token 已刷新，有效期至 %s",
                    time.strftime("%Y-%m-%d %H:%M", time.localtime(expire)))
        return token


def _decode_wav(raw: bytes) -> tuple[bytes, int, float, int]:
    """读 WAV 头，返回裸 PCM、采样率、时长、声道数以外的必要信息。

    只接受一句话识别支持的规格。采样率不做重采样——2C2G 上跑重采样
    不划算，前端统一输出 16k 单声道即可。
    """
    try:
        with wave.open(io.BytesIO(raw), "rb") as handle:
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            rate = handle.getframerate()
            frames = handle.getnframes()
            pcm = handle.readframes(frames)
    except (wave.Error, EOFError) as exc:
        raise AsrError("音频不是合法的 WAV 文件") from exc

    if channels != 1:
        raise AsrError(f"音频是 {channels} 声道，语音识别只接受单声道")
    if width != 2:
        raise AsrError(f"音频位宽 {width * 8} bit，语音识别只接受 16 bit")
    if rate not in SUPPORTED_RATES:
        raise AsrError(f"音频采样率 {rate} Hz，只接受 {' / '.join(map(str, SUPPORTED_RATES))} Hz")
    if not pcm:
        raise AsrError("音频内容为空")

    duration = frames / rate
    return pcm, rate, duration, channels


def _split(pcm: bytes, rate: int) -> list[bytes]:
    """按 60 秒切片。最后一片可能不足 60 秒，照常送去识别。"""
    limit = MAX_SLICE_SECONDS * rate * 2   # 16bit = 2 字节/样本
    return [pcm[start:start + limit] for start in range(0, len(pcm), limit)]


def _gateway_url(token: str) -> str:
    # 部署机在阿里云同地域时走内网：不产生公网流量费，且绕开 1–5Mbps
    # 的小带宽瓶颈（60 秒音频 1.83MB，1Mbps 公网要传 15 秒）。
    if os.getenv("MINDMAP_ASR_INTERNAL", "").strip() in {"1", "true", "True"}:
        return (f"ws://nls-gateway-{GATEWAY_REGION}-internal.aliyuncs.com:80"
                f"/ws/v1?token={token}")
    return f"wss://nls-gateway-{GATEWAY_REGION}.aliyuncs.com/ws/v1?token={token}"


def _start_frame(appkey: str, task_id: str, rate: int) -> dict:
    """StartRecognition 帧。五个 header 字段缺一不可，否则只回 MESSAGE_INVALID。"""
    return {
        "header": {
            "appkey": appkey,
            "message_id": uuid.uuid4().hex,
            "task_id": task_id,
            "namespace": "SpeechRecognizer",
            "name": "StartRecognition",
        },
        "payload": {
            "format": "pcm",
            "sample_rate": rate,
            "enable_punctuation_prediction": True,
            "enable_inverse_text_normalization": True,
            # 保持 false。开启后句间停顿会触发服务端提前结束并丢弃后续音频。
            "enable_voice_detection": False,
        },
    }


def _stop_frame(appkey: str, task_id: str) -> dict:
    """StopRecognition 帧。

    **appkey 不能省**。少了它服务和 start 阶段缺 appkey 时报同一句
    `Missing message appkey`，但错误出自收尾阶段，看日志前缀才分得清。
    """
    return {
        "header": {
            "appkey": appkey,
            "message_id": uuid.uuid4().hex,
            "task_id": task_id,
            "namespace": "SpeechRecognizer",
            "name": "StopRecognition",
        },
    }


async def _recognize_segment(token: str, appkey: str, pcm: bytes, rate: int) -> str:
    """识别单个 ≤60 秒的片段。"""
    try:
        import websockets
    except ImportError as exc:  # pragma: no cover
        raise AsrError("缺少 websockets，请先 pip install -r requirements.txt") from exc

    url = _gateway_url(token)
    async with websockets.connect(url, max_size=None) as socket:
        task_id = uuid.uuid4().hex
        await socket.send(json.dumps(_start_frame(appkey, task_id, rate)))

        try:
            hello = json.loads(await asyncio.wait_for(socket.recv(), timeout=15))
        except asyncio.TimeoutError as exc:
            raise AsrError("语音识别握手超时") from exc
        if hello.get("header", {}).get("name") != "RecognitionStarted":
            header = hello.get("header", {})
            raise AsrError(
                f"语音识别握手失败 {header.get('status')}: {header.get('status_text')}")

        for start in range(0, len(pcm), SEND_CHUNK):
            await socket.send(pcm[start:start + SEND_CHUNK])
        await socket.send(json.dumps(_stop_frame(appkey, task_id)))
        # stop 帧漏掉 appkey 时，这里会收到 MESSAGE_INVALID: Missing message
        # appkey——与 start 阶段缺 appkey 的报错完全相同，容易找错地方。

        while True:
            try:
                message = json.loads(await asyncio.wait_for(socket.recv(), timeout=60))
            except asyncio.TimeoutError as exc:
                raise AsrError("语音识别等待结果超时") from exc
            header = message.get("header", {})
            name = header.get("name")
            if name == "RecognitionCompleted":
                return message.get("payload", {}).get("result", "") or ""
            if name == "TaskFailed":
                raise AsrError(
                    f"语音识别失败 {header.get('status')}: {header.get('status_text')}")


async def _recognize_all(token: str, appkey: str, pieces: list[bytes],
                         rate: int) -> list[str]:
    results = []
    for piece in pieces:
        results.append(await _recognize_segment(token, appkey, piece, rate))
    return results


def transcribe_audio(filename: str | None, raw: bytes) -> Source:
    """把音频转成 Source。失败抛 AsrError，不返回空正文。

    同步接口，与 parse_bytes 其余分支保持一致——路由层已经把它甩进
    线程池，这里自己开事件循环跑 WebSocket 不会撞上外层循环。
    """
    pcm, rate, duration, _ = _decode_wav(raw)
    _, _, appkey = _config()
    token = _token()

    pieces = _split(pcm, rate)
    if len(pieces) > 1:
        logger.info("音频 %.1f 秒，切成 %d 段识别", duration, len(pieces))

    with _gate:
        try:
            texts = asyncio.run(_recognize_all(token, appkey, pieces, rate))
        except AsrError:
            raise
        except Exception as exc:  # noqa: BLE001 - 网络层异常统一转成 AsrError
            raise AsrError(f"语音识别请求失败：{exc}") from exc

    text = "".join(texts).strip()
    if not text:
        raise AsrError("没有从音频里识别出语音内容")

    logger.info("语音转写完成：%.1f 秒 → %d 字", duration, len(text))
    return Source(
        kind=SourceKind.audio,
        filename=filename,
        text=text,
        duration_seconds=round(duration, 2),
        status="parsed",
    )
