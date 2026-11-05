"""语音转写的边界行为。全部离线，不访问阿里云。"""
import io
import unittest
import wave
from unittest.mock import MagicMock, patch

from app.contracts.source import SourceKind
from app.ingestion import asr
from app.ingestion.asr import AsrError, transcribe_audio
from app.ingestion.router import list_supported_formats
from app.ingestion.service import NotSupportedSource, parse_bytes

ENV = {"DASHSCOPE_API_KEY": "sk-test"}


def make_wav(seconds=1.0, rate=16000, channels=1, width=2):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(width)
        handle.setframerate(rate)
        handle.writeframes(b"\x00" * int(seconds * rate * channels * width))
    return buffer.getvalue()


def body(text="识别结果", begin=100, end=900, usage=None):
    return {
        "output": {"output": {"sentence": {
            "text": text, "begin_time": begin, "end_time": end,
            "words": [{"text": text, "begin_time": begin, "end_time": end}],
        }}},
        "usage": usage or {"duration": 1, "input_tokens": 10, "output_tokens": 5},
    }


class FakeHttp:
    """按顺序吐出预设响应的 httpx.Client 替身。"""

    def __init__(self, bodies):
        self.bodies = list(bodies)
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "payload": json, "headers": headers})
        data = self.bodies.pop(0) if self.bodies else body()
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = data
        return response


def run_with(bodies, wav):
    fake = FakeHttp(bodies)
    with patch.dict("os.environ", ENV, clear=False), \
         patch("app.ingestion.asr.httpx.Client", return_value=fake):
        source = transcribe_audio("录音.wav", wav)
    return source, fake


class WavSpecTest(unittest.TestCase):
    def test_accepts_16k_mono(self):
        pcm, rate, duration, channels = asr._decode_wav(make_wav(2.0))
        self.assertEqual(rate, 16000)
        self.assertEqual(channels, 1)
        self.assertAlmostEqual(duration, 2.0, places=2)

    def test_accepts_8k(self):
        self.assertEqual(asr._decode_wav(make_wav(1.0, rate=8000))[1], 8000)

    def test_accepts_stereo(self):
        self.assertEqual(asr._decode_wav(make_wav(1.0, channels=2))[3], 2)

    def test_rejects_three_channels(self):
        with self.assertRaises(AsrError) as ctx:
            asr._decode_wav(make_wav(1.0, channels=3))
        self.assertIn("声道", str(ctx.exception))

    def test_rejects_8bit(self):
        with self.assertRaises(AsrError):
            asr._decode_wav(make_wav(1.0, width=1))

    def test_rejects_22k(self):
        with self.assertRaises(AsrError):
            asr._decode_wav(make_wav(1.0, rate=22050))

    def test_rejects_non_wav(self):
        with self.assertRaises(AsrError):
            asr._decode_wav(b"not a wav")


class SliceTest(unittest.TestCase):
    def test_300s_stays_single(self):
        self.assertEqual(len(asr._split(b"\x00" * (300 * 16000 * 2), 16000)), 1)

    def test_301s_becomes_two(self):
        self.assertEqual(len(asr._split(b"\x00" * (301 * 16000 * 2), 16000)), 2)

    def test_pieces_are_valid_wav(self):
        pieces = asr._split(b"\x00" * (301 * 16000 * 2), 16000)
        for piece in pieces:
            with wave.open(io.BytesIO(piece), "rb") as handle:
                self.assertEqual(handle.getframerate(), 16000)
                self.assertEqual(handle.getnchannels(), 1)


class SentenceParseTest(unittest.TestCase):
    """实测响应多包了一层 output.output，官方示例是 output.sentence。"""

    def test_nested_form(self):
        self.assertEqual(asr._parse_sentence(body("甲"))["text"], "甲")

    def test_flat_form(self):
        flat = {"output": {"sentence": {"text": "乙"}}}
        self.assertEqual(asr._parse_sentence(flat)["text"], "乙")

    def test_missing_form(self):
        self.assertEqual(asr._parse_sentence({"output": {}}), {})


class ConfigTest(unittest.TestCase):
    def test_missing_key_names_the_variable(self):
        with patch.dict("os.environ", {}, clear=True), \
             self.assertRaises(AsrError) as ctx:
            transcribe_audio("a.wav", make_wav(1.0))
        self.assertIn("DASHSCOPE_API_KEY", str(ctx.exception))


class TranscribeTest(unittest.TestCase):
    def test_source_fields(self):
        source, _ = run_with([body("识别出来的话", 240, 8480)], make_wav(8.5))
        self.assertEqual(source.kind, SourceKind.audio)
        self.assertEqual(source.text, "识别出来的话")
        self.assertEqual(source.status, "parsed")
        self.assertAlmostEqual(source.duration_seconds, 8.5, places=1)

    def test_sentence_timestamps_are_recorded(self):
        source, _ = run_with([body("第一句", 240, 1200), body("第二句", 1300, 2000)],
                             make_wav(2.0))
        self.assertEqual(len(source.sentences), 1)
        self.assertEqual(source.sentences[0].begin_ms, 240)
        self.assertEqual(source.sentences[0].end_ms, 1200)

    def test_second_slice_timestamps_are_offset(self):
        """301 秒切两片时，第二片的时间必须加上第一片的时长。

        不偏移的话第二段所有时间都从 0 开始，节点会指回错误的位置。
        """
        source, fake = run_with([body("前段", 100, 900), body("后段", 100, 900)],
                                make_wav(301))
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(len(source.sentences), 2)
        self.assertEqual(source.sentences[0].begin_ms, 100)
        # 第二片偏移 300 秒
        self.assertEqual(source.sentences[1].begin_ms, 100 + 300_000)

    def test_request_shape(self):
        _, fake = run_with([body()], make_wav(1.0))
        call = fake.calls[0]
        self.assertEqual(call["payload"]["model"], "qwen-audio-3.1-asr-flash")
        self.assertEqual(call["headers"]["X-DashScope-SSE"], "disable")
        self.assertTrue(call["payload"]["input"]["messages"][0]["content"][0]
                        ["input_audio"]["data"].startswith("data:audio/wav;base64,"))

    def test_empty_text_is_an_error(self):
        with self.assertRaises(AsrError):
            run_with([body("   ", 0, 10)], make_wav(1.0))

    def test_http_error_becomes_asr_error(self):
        fake = MagicMock()
        fake.post.return_value.status_code = 400
        fake.post.return_value.json.return_value = {
            "code": "AUDIO_DURATION_TOO_LONG", "message": "audio too long"}
        with patch.dict("os.environ", ENV, clear=False), \
             patch("app.ingestion.asr.httpx.Client") as client_cls:
            client_cls.return_value.__enter__.return_value = fake
            with self.assertRaises(AsrError) as ctx:
                transcribe_audio("a.wav", make_wav(1.0))
        self.assertIn("AUDIO_DURATION_TOO_LONG", str(ctx.exception))

    def test_network_error_becomes_asr_error(self):
        import httpx

        with patch.dict("os.environ", ENV, clear=False), \
             patch("app.ingestion.asr.httpx.Client") as client_cls:
            client_cls.return_value.__enter__.return_value.post.side_effect = \
                httpx.ReadTimeout("timed out")
            with self.assertRaises(AsrError) as ctx:
                transcribe_audio("a.wav", make_wav(1.0))
        self.assertIn("请求失败", str(ctx.exception))


class StereoTest(unittest.TestCase):
    def test_split_separates_channels(self):
        import array

        interleaved = array.array("h")
        for left, right in zip([1, 2, 3], [10, 20, 30]):
            interleaved.append(left)
            interleaved.append(right)
        got_left, got_right = asr._split_stereo(interleaved.tobytes())
        self.assertEqual(array.array("h", got_left).tolist(), [1, 2, 3])
        self.assertEqual(array.array("h", got_right).tolist(), [10, 20, 30])

    def test_nearly_identical_detects_upmix(self):
        import array

        same = array.array("h", [100, -200, 300] * 1000)
        self.assertTrue(asr._nearly_identical(same.tobytes(), same.tobytes()))
        different = array.array("h", same)
        different[0::2] = array.array("h", [900] * 1500)
        self.assertFalse(asr._nearly_identical(same.tobytes(), different.tobytes()))

    def test_merge_identical_keeps_one(self):
        lane = {"text": "同一段话", "sentences": [], "usage": {}}
        self.assertEqual(asr._merge_lanes(lane, dict(lane))["text"], "同一段话")

    def test_merge_falls_back_to_non_empty(self):
        empty = {"text": "", "sentences": [], "usage": {}}
        full = {"text": "有内容", "sentences": [], "usage": {}}
        self.assertEqual(asr._merge_lanes(full, empty)["text"], "有内容")
        self.assertEqual(asr._merge_lanes(empty, full)["text"], "有内容")

    def test_merge_keeps_both_when_different(self):
        left = {"text": "会议决定周三评审", "sentences": [], "usage": {}}
        right = {"text": "思维导图是可视化方法", "sentences": [], "usage": {}}
        merged = asr._merge_lanes(left, right)["text"]
        self.assertIn("会议决定", merged)
        self.assertIn("思维导图", merged)

    def test_stereo_identical_uses_one_request(self):
        wav = make_wav(1.0, channels=2)   # 两路都是零，必然相同
        _, fake = run_with([body()], wav)
        self.assertEqual(len(fake.calls), 1)


class ServiceBranchTest(unittest.TestCase):
    def test_asr_error_becomes_not_supported(self):
        with patch("app.ingestion.service.transcribe_audio",
                   side_effect=AsrError("缺少 DASHSCOPE_API_KEY")):
            with self.assertRaises(NotSupportedSource) as ctx:
                parse_bytes(SourceKind.audio, "a.wav", make_wav(1))
        self.assertIn("DASHSCOPE_API_KEY", str(ctx.exception))

    def test_formats_lists_audio(self):
        data = list_supported_formats()
        self.assertIn(".wav", data["audio"])
        self.assertGreater(data["maxAudioBytes"], data["maxUploadBytes"])


if __name__ == "__main__":
    unittest.main()
