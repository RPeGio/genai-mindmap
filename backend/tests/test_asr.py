"""语音转写的边界行为。全部离线，不访问阿里云。

真实识别效果由手工端到端验证覆盖；这里只锁住容易回归的规则：
WAV 规格校验、60 秒切片边界、协议帧字段、Token 缓存策略、
以及失败时必须是报错而不是返回空正文。
"""
import io
import time
import unittest
import wave
from unittest.mock import AsyncMock, patch

from app.contracts.source import Source, SourceKind
from app.ingestion import asr
from app.ingestion.asr import AsrError, transcribe_audio
from app.ingestion.router import list_supported_formats
from app.ingestion.service import NotSupportedSource, parse_bytes


def make_wav(seconds=1.0, rate=16000, channels=1, width=2):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(width)
        handle.setframerate(rate)
        handle.writeframes(b"\x00" * int(seconds * rate * channels * width))
    return buffer.getvalue()


class WavSpecTest(unittest.TestCase):
    """一句话识别只接受 16bit 单声道 8k/16k，其余在本地就拦住。

    本地拦下的好处是错误信息能说清哪一项不合格；等发到服务端，
    只会收到 41010101 UNSUPPORTED_SAMPLE_RATE 这种不带上下文的码。
    """

    def test_accepts_16k_mono(self):
        pcm, rate, duration, _ = asr._decode_wav(make_wav(2.0))
        self.assertEqual(rate, 16000)
        self.assertAlmostEqual(duration, 2.0, places=2)
        self.assertEqual(len(pcm), 2 * 16000 * 2)

    def test_accepts_8k(self):
        _, rate, _, _ = asr._decode_wav(make_wav(1.0, rate=8000))
        self.assertEqual(rate, 8000)

    def test_rejects_stereo(self):
        """服务端不拒绝立体声，会静默产出乱码，所以必须本地拦死（实测）。

        把左右声道内容不同的立体声发过去，服务端返回 200 和 39 个字，
        但内容是「向人手一下中山笑声风雨…」这种噪音。这类错误不会触发
        任何告警，会一路流进导图。
        """
        with self.assertRaises(AsrError) as ctx:
            asr._decode_wav(make_wav(1.0, channels=2))
        message = str(ctx.exception)
        self.assertIn("声道", message)
        self.assertIn("不报错", message)

    def test_rejects_8bit(self):
        with self.assertRaises(AsrError) as ctx:
            asr._decode_wav(make_wav(1.0, width=1))
        self.assertIn("16 bit", str(ctx.exception))

    def test_rejects_22k(self):
        with self.assertRaises(AsrError) as ctx:
            asr._decode_wav(make_wav(1.0, rate=22050))
        self.assertIn("采样率", str(ctx.exception))

    def test_rejects_non_wav(self):
        with self.assertRaises(AsrError):
            asr._decode_wav(b"not a wav at all")

    def test_rejects_zero_length(self):
        with self.assertRaises(AsrError):
            asr._decode_wav(make_wav(0.0))


class SliceTest(unittest.TestCase):
    """60 秒是服务端硬上限：切少了报 TOO_LONG_SPEECH，切多了边界损失变大。"""

    def test_exactly_60s_stays_single(self):
        self.assertEqual(len(asr._split(b"\x00" * (60 * 16000 * 2), 16000)), 1)

    def test_61s_becomes_two(self):
        self.assertEqual(len(asr._split(b"\x00" * (61 * 16000 * 2), 16000)), 2)

    def test_130s_becomes_three(self):
        self.assertEqual(len(asr._split(b"\x00" * (130 * 16000 * 2), 16000)), 3)

    def test_chunks_cover_every_byte(self):
        """切片只负责分段，不能丢字节——丢了就是静默丢语音。"""
        pcm = bytes(range(256)) * 30000
        self.assertEqual(b"".join(asr._split(pcm, 8000)), pcm)


class FrameTest(unittest.TestCase):
    """协议帧字段回归。

    这五个 header 字段和 stop 帧的 appkey 都是逐个报错试出来的，
    改动任何一处都会让整条链路退回 MESSAGE_INVALID。
    """

    def test_start_frame_has_all_header_fields(self):
        frame = asr._start_frame("app-1", "task-1", 16000)
        self.assertEqual(set(frame["header"]),
                         {"appkey", "message_id", "task_id", "namespace", "name"})
        self.assertEqual(frame["header"]["name"], "StartRecognition")
        self.assertEqual(frame["header"]["namespace"], "SpeechRecognizer")
        self.assertEqual(frame["header"]["appkey"], "app-1")

    def test_start_frame_disables_voice_detection(self):
        """开启后句间停顿会让服务端提前结束并丢弃后续音频，且不报错。"""
        frame = asr._start_frame("a", "t", 16000)
        self.assertFalse(frame["payload"]["enable_voice_detection"])

    def test_stop_frame_carries_appkey(self):
        """少这个字段，收尾会收到与 start 阶段完全相同的报错。"""
        frame = asr._stop_frame("app-1", "task-1")
        self.assertEqual(frame["header"]["appkey"], "app-1")
        self.assertEqual(frame["header"]["name"], "StopRecognition")
        self.assertEqual(frame["header"]["task_id"], "task-1")


class ConfigTest(unittest.TestCase):
    def test_missing_env_names_what_is_missing(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(AsrError) as ctx:
                asr._config()
        message = str(ctx.exception)
        self.assertIn("ALIYUN_AK_ID", message)
        self.assertIn("NLS_APPKEY", message)


class TokenCacheTest(unittest.TestCase):
    """Token 有效期约 36 小时，每次识别都重取要多付约 0.7 秒。"""

    def setUp(self):
        self._saved = dict(asr._token_cache)
        asr._token_cache.update(id=None, expire=0.0)

    def tearDown(self):
        asr._token_cache.update(self._saved)

    @patch("app.ingestion.asr._config", return_value=("ak", "sk", "app"))
    @patch("app.ingestion.asr._fetch_token")
    def test_reused_within_validity(self, fetch, _config):
        fetch.return_value = ("tok-1", time.time() + 7200)
        self.assertEqual(asr._token(), "tok-1")
        self.assertEqual(asr._token(), "tok-1")
        self.assertEqual(fetch.call_count, 1)

    @patch("app.ingestion.asr._config", return_value=("ak", "sk", "app"))
    @patch("app.ingestion.asr._fetch_token")
    def test_refreshed_near_expiry(self, fetch, _config):
        """剩余不足 1 小时就提前换，避免识别中途过期。"""
        fetch.return_value = ("tok-1", time.time() + 1800)
        self.assertEqual(asr._token(), "tok-1")
        fetch.return_value = ("tok-2", time.time() + 7200)
        self.assertEqual(asr._token(), "tok-2")
        self.assertEqual(fetch.call_count, 2)


class TranscribeTest(unittest.TestCase):
    """端到端行为，网络层用 mock 替掉。"""

    def _run(self, wav, results):
        with patch("app.ingestion.asr._config", return_value=("ak", "sk", "app")), \
             patch("app.ingestion.asr._token", return_value="tok"), \
             patch("app.ingestion.asr._recognize_all",
                   new_callable=AsyncMock) as recognize_all:
            recognize_all.return_value = results
            source = transcribe_audio("录音.wav", wav)
        return source, recognize_all

    def test_single_segment_source_fields(self):
        source, _ = self._run(make_wav(10), ["识别出来的话"])
        self.assertEqual(source.kind, SourceKind.audio)
        self.assertEqual(source.text, "识别出来的话")
        self.assertEqual(source.status, "parsed")
        self.assertEqual(source.filename, "录音.wav")
        self.assertAlmostEqual(source.duration_seconds, 10.0, places=1)

    def test_segments_are_joined_in_order(self):
        source, recognize_all = self._run(make_wav(61), ["前半段", "后半段"])
        self.assertEqual(source.text, "前半段后半段")
        pieces = recognize_all.call_args[0][2]
        self.assertEqual(len(pieces), 2)

    def test_empty_result_is_an_error(self):
        """识别不出内容要报错，不能返回空正文让下游生成空树。"""
        with self.assertRaises(AsrError):
            self._run(make_wav(5), ["", "   "])

    def test_blank_edges_are_stripped(self):
        source, _ = self._run(make_wav(5), ["  有内容  "])
        self.assertEqual(source.text, "有内容")


class ServiceBranchTest(unittest.TestCase):
    """parse_bytes 的 audio 分支要么给出 Source，要么抛 NotSupportedSource。"""

    def test_asr_error_becomes_not_supported(self):
        with patch("app.ingestion.service.transcribe_audio",
                   side_effect=AsrError("缺少 ALIYUN_AK_ID")):
            with self.assertRaises(NotSupportedSource) as ctx:
                parse_bytes(SourceKind.audio, "a.wav", make_wav(1))
        self.assertIn("ALIYUN_AK_ID", str(ctx.exception))

    def test_success_source_passes_through(self):
        source = Source(kind=SourceKind.audio, text="hi", status="parsed")
        with patch("app.ingestion.service.transcribe_audio", return_value=source):
            self.assertIs(parse_bytes(SourceKind.audio, "a.wav", make_wav(1)), source)


class FormatsTest(unittest.TestCase):
    def test_audio_listed_with_own_limit(self):
        data = list_supported_formats()
        self.assertIn(".wav", data["audio"])
        self.assertGreater(data["maxAudioBytes"], data["maxUploadBytes"])


if __name__ == "__main__":
    unittest.main()
