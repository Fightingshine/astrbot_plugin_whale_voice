"""离线回归：不调用云端语音，不向 QQ 发消息。"""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from astrbot_plugin_whale_voice.core import (
    ENDPOINT,
    MAX_BYTES,
    MODEL,
    VOICES,
    UsageGate,
    VoiceError,
    atomic_json,
    clean_cache,
    delete_preset_song,
    fingerprint,
    format_bytes,
    get_preset_song_path,
    list_preset_songs,
    list_preset_songs_detail,
    match_preset_song,
    rename_preset_song,
    resolve_voice,
    sanitize_song_name,
    save_preset_song,
    siliconflow_key,
    singing_ready,
    speech_text,
    synthesize,
)

TEST_VOICE_ALIASES = {
    name: "speech:fixture:test:voice" for name in ("dafeiyu", "dafeiyu_ssr", "dafeiyu_cat")
}


def config(base="https://api.siliconflow.cn/v1", key="test-placeholder"):
    return {
        "provider": [
            {
                "id": "embedding-test",
                "type": "openai_embedding",
                "enable": True,
                "embedding_api_base": base,
                "embedding_api_key": key,
            }
        ]
    }


class TextTests(unittest.TestCase):
    def test_chinese_text(self):
        self.assertEqual(speech_text("  小蓝鲸来啦～\n大家好  ", 30), "小蓝鲸来啦～\n大家好")

    def test_controls_removed(self):
        self.assertEqual(speech_text("你\u202e好\x00", 20), "你好")

    def test_empty_and_non_string(self):
        for x in ("", "  ", None, [], 12):
            with self.subTest(x=x), self.assertRaises(VoiceError):
                speech_text(x, 20)

    def test_length_boundary(self):
        self.assertEqual(len(speech_text("鲸" * 280, 280)), 280)
        with self.assertRaises(VoiceError):
            speech_text("鲸" * 281, 280)

    def test_no_secret_reading(self):
        for value in (
            "sk-" + "x" * 20,
            "Authorization: Bearer placeholder-value",
            "-----BEGIN RSA PRIVATE KEY-----",
        ):
            with self.subTest(value=value), self.assertRaises(VoiceError):
                speech_text(value, 280)

    def test_ordinary_numbers_allowed(self):
        self.assertIn("15点", speech_text("明天15点集合。", 280))


class CredentialTests(unittest.TestCase):
    def test_reuses_existing_embedding_credential(self):
        self.assertEqual(siliconflow_key(config(), "embedding-test"), "test-placeholder")

    def test_key_list(self):
        self.assertEqual(
            siliconflow_key(config(key=["", "test-placeholder"]), "embedding-test"),
            "test-placeholder",
        )

    def test_reject_other_domain(self):
        for url in (
            "https://api.siliconflow.cn.evil.test/v1",
            "http://api.siliconflow.cn/v1",
            "https://evil.test/v1",
            "https://u:p@api.siliconflow.cn/v1",
            "https://api.siliconflow.cn/v1?key=abc",
            "https://api.siliconflow.cn/v1#secret",
            "https://api.siliconflow.cn:444/v1",
            "https://api.siliconflow.cn/other",
        ):
            with self.subTest(url=url), self.assertRaises(VoiceError):
                siliconflow_key(config(url), "embedding-test")

    def test_missing_disabled_and_wrong_type(self):
        with self.assertRaises(VoiceError):
            siliconflow_key(config(), "missing")
        for change in (
            {"enable": False},
            {"type": "openai_chat_completion"},
            {"embedding_api_key": ""},
        ):
            c = config()
            c["provider"][0].update(change)
            with self.subTest(change=change), self.assertRaises(VoiceError):
                siliconflow_key(c, "embedding-test")

    def test_singing_not_ordinary_tts(self):
        for provider in (None, object()):
            with self.assertRaises(VoiceError):
                singing_ready(provider)

    def test_singing_style_and_credential_required(self):
        cls = type("ProviderMiMoTTSAPI", (), {})
        p = cls()
        p.chosen_api_key = "test-placeholder"
        p._build_style_prefix = lambda: "<style>温柔</style>"
        with self.assertRaises(VoiceError):
            singing_ready(p)
        p._build_style_prefix = lambda: "<style>唱歌</style>"
        self.assertIs(singing_ready(p), p)
        p.chosen_api_key = ""
        with self.assertRaises(VoiceError):
            singing_ready(p)


class PresetSongTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "人是猫.mp3").write_bytes(b"dummy mp3")
        (self.root / "人是猫_完整版.wav").write_bytes(b"dummy wav")
        (self.root / "not_audio.txt").write_text("hello")

    def tearDown(self):
        self.tmp.cleanup()

    def test_list_preset_songs(self):
        songs = list_preset_songs(self.root)
        self.assertIn("人是猫", songs)
        self.assertIn("人是猫_完整版", songs)
        self.assertNotIn("not_audio", songs)

    def test_match_exact_and_fuzzy(self):
        m1 = match_preset_song(self.root, "人是猫")
        self.assertIsNotNone(m1)
        self.assertEqual(m1[1], "人是猫")

        m2 = match_preset_song(self.root, "唱首人是猫这首歌")
        self.assertIsNotNone(m2)
        self.assertEqual(m2[1], "人是猫")

        m3 = match_preset_song(self.root, "来首人是猫_完整版")
        self.assertIsNotNone(m3)
        self.assertEqual(m3[1], "人是猫_完整版")

        # 重点：无下划线输入 "人是猫完整版" 必须准确匹配 "人是猫_完整版"
        m4 = match_preset_song(self.root, "人是猫完整版")
        self.assertIsNotNone(m4)
        self.assertEqual(m4[1], "人是猫_完整版")

        m5 = match_preset_song(self.root, "播放人是猫完整版")
        self.assertIsNotNone(m5)
        self.assertEqual(m5[1], "人是猫_完整版")

        m6 = match_preset_song(self.root, "人是猫 完整版")
        self.assertIsNotNone(m6)
        self.assertEqual(m6[1], "人是猫_完整版")

    def test_match_non_existent(self):
        self.assertIsNone(match_preset_song(self.root, "不存在的歌"))
        self.assertIsNone(match_preset_song(Path("/non/existent"), "人是猫"))


class VoiceResolutionTests(unittest.TestCase):
    def test_presets(self):
        for v in VOICES:
            self.assertEqual(resolve_voice(v), f"{MODEL}:{v}")

    def test_aliases(self):
        self.assertEqual(
            resolve_voice("dafeiyu", TEST_VOICE_ALIASES),
            "speech:fixture:test:voice",
        )
        self.assertEqual(
            resolve_voice("dafeiyu_ssr", TEST_VOICE_ALIASES),
            "speech:fixture:test:voice",
        )
        self.assertEqual(
            resolve_voice("dafeiyu_cat", TEST_VOICE_ALIASES),
            "speech:fixture:test:voice",
        )

    def test_direct_speech_uri(self):
        uri = "speech:fixture:test:voice"
        self.assertEqual(resolve_voice(uri), uri)

    def test_invalid_voices(self):
        for v in ("", "unknown", "speech:", "speech:bad", None, 123):
            with self.subTest(v=v), self.assertRaises(VoiceError):
                resolve_voice(v)


class GateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.gate = UsageGate(self.root / "limits.json")

    def tearDown(self):
        self.temp.cleanup()

    def test_persists_duplicate_protection(self):
        self.gate.reserve("u", "m1", "speech", now=1000)
        gate = UsageGate(self.root / "limits.json")
        with self.assertRaises(VoiceError):
            gate.reserve("u", "m1", "sing", now=1040)

    def test_cooldown(self):
        self.gate.reserve("u", "m1", "speech", now=1000)
        with self.assertRaises(VoiceError):
            self.gate.reserve("u", "m2", "speech", now=1029)
        self.gate.reserve("u", "m2", "speech", now=1030)

    def test_user_daily_quota(self):
        self.gate.reserve("u", "m1", "speech", now=1000, per_user=1)
        with self.assertRaises(VoiceError):
            self.gate.reserve("u", "m2", "sing", now=1040, per_user=1)

    def test_total_daily_quota(self):
        self.gate.reserve("u1", "m1", "speech", now=1000, total=1)
        with self.assertRaises(VoiceError):
            self.gate.reserve("u2", "m2", "speech", now=1040, total=1)

    def test_next_day_resets_quota_not_recent_duplicates(self):
        self.gate.reserve("u", "m1", "speech", now=1000, per_user=1)
        self.gate.reserve("u", "m2", "speech", now=87400, per_user=1)
        with self.assertRaises(VoiceError):
            self.gate.reserve("u", "m1", "speech", now=87500)

    def test_corruption_fails_closed(self):
        (self.root / "limits.json").write_text("not json")
        with self.assertRaises(VoiceError):
            UsageGate(self.root / "limits.json")

    def test_failed_atomic_write_does_not_mutate_in_memory(self):
        with (
            patch("astrbot_plugin_whale_voice.core.atomic_json", side_effect=OSError),
            self.assertRaises(OSError),
        ):
            self.gate.reserve("u", "m", "speech", now=1000)
        self.assertFalse(self.gate.state["requests"])

    def test_request_identity_does_not_store_ids(self):
        self.gate.reserve(fingerprint("some-user"), fingerprint("some-message"), "speech", now=1000)
        self.assertNotIn("some-user", (self.root / "limits.json").read_text())
        self.assertNotIn("some-message", (self.root / "limits.json").read_text())

    def test_atomic_json_valid(self):
        atomic_json(self.root / "a.json", {"状态": "完成"})
        self.assertEqual(
            json.loads((self.root / "a.json").read_text(encoding="utf-8")),
            {"状态": "完成"},
        )
        self.assertFalse((self.root / "a.tmp").exists())

    def test_cleanup_only_expired_audio(self):
        old = self.root / ("a" * 32 + ".wav")
        old.write_bytes(b"x")
        os.utime(old, (10, 10))
        recent = self.root / ("b" * 32 + ".mp3")
        recent.write_bytes(b"x")
        other = self.root / "original.wav"
        other.write_bytes(b"x")
        os.utime(other, (10, 10))
        clean_cache(self.root, now=90000)
        self.assertFalse(old.exists())
        self.assertTrue(other.exists())
        self.assertTrue(recent.exists())


class Content:
    def __init__(self, chunks):
        self.chunks = chunks

    async def iter_chunked(self, _):
        for chunk in self.chunks:
            yield chunk


class Response:
    def __init__(self, status=200, kind="audio/mpeg", chunks=None):
        self.status = status
        self.headers = {"Content-Type": kind}
        self.content = Content(chunks if chunks is not None else [b"ID3" + b"x" * 30])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class Session:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.response


class SynthesisTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "test.mp3"

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_payload_and_one_request(self):
        s = Session(Response())
        await synthesize(s, "test-placeholder", "小蓝鲸说你好", "claire", self.out)
        self.assertEqual(len(s.calls), 1)
        args, kwargs = s.calls[0]
        self.assertEqual(args, (ENDPOINT,))
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["json"]["voice"], MODEL + ":claire")
        self.assertEqual(kwargs["json"]["input"], "小蓝鲸说你好")
        self.assertTrue(self.out.is_file())

    async def test_cloned_voice_alias_payload(self):
        s = Session(Response())
        await synthesize(
            s,
            "test-placeholder",
            "大肥鱼游啊游",
            "dafeiyu_ssr",
            self.out,
            voice_aliases=TEST_VOICE_ALIASES,
        )
        self.assertEqual(len(s.calls), 1)
        _args, kwargs = s.calls[0]
        self.assertEqual(kwargs["json"]["voice"], TEST_VOICE_ALIASES["dafeiyu_ssr"])
        self.assertEqual(kwargs["json"]["input"], "大肥鱼游啊游")

    async def test_direct_speech_uri_payload(self):
        s = Session(Response())
        uri = "speech:fixture:test:voice"
        await synthesize(s, "test-placeholder", "测试语音", uri, self.out)
        self.assertEqual(len(s.calls), 1)
        _args, kwargs = s.calls[0]
        self.assertEqual(kwargs["json"]["voice"], uri)

    async def test_http_failures_no_retry(self):
        for code in (302, 401, 403, 429, 500):
            s = Session(Response(code))
            with self.subTest(code=code), self.assertRaises(VoiceError):
                await synthesize(s, "test-placeholder", "你好", "claire", self.out)
            self.assertEqual(len(s.calls), 1)
            self.assertFalse(self.out.exists())

    async def test_reject_html_json(self):
        for kind in ("text/html", "application/json"):
            with self.subTest(kind=kind), self.assertRaises(VoiceError):
                await synthesize(
                    Session(Response(kind=kind)),
                    "test-placeholder",
                    "你好",
                    "claire",
                    self.out,
                )

    async def test_oversized_response_removed(self):
        with self.assertRaises(VoiceError):
            await synthesize(
                Session(Response(chunks=[b"x" * (MAX_BYTES + 1)])),
                "test-placeholder",
                "你好",
                "claire",
                self.out,
            )
        self.assertFalse(self.out.exists())

    async def test_empty_response_removed(self):
        with self.assertRaises(VoiceError):
            await synthesize(
                Session(Response(chunks=[])),
                "test-placeholder",
                "你好",
                "claire",
                self.out,
            )
        self.assertFalse(self.out.exists())

    async def test_invalid_voice_before_http(self):
        s = Session(Response())
        with self.assertRaises(VoiceError):
            await synthesize(s, "test-placeholder", "你好", "invalid", self.out)
        self.assertFalse(s.calls)

    async def test_cancellation_cleans_partial(self):
        class CancelContent:
            async def iter_chunked(self, _):
                yield b"ID3" + b"x" * 30
                raise asyncio.CancelledError()

        response = Response()
        response.content = CancelContent()
        with self.assertRaises(asyncio.CancelledError):
            await synthesize(Session(response), "test-placeholder", "你好", "claire", self.out)
        self.assertFalse(self.out.exists())


class SongManagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_sanitize_song_name(self):
        self.assertEqual(sanitize_song_name("  人是猫  "), "人是猫")
        self.assertEqual(sanitize_song_name("人是猫/..\\test"), "人是猫..test")
        self.assertEqual(sanitize_song_name('歌曲 *?"<>|: test'), "歌曲 test")
        with self.assertRaises(VoiceError):
            sanitize_song_name("")
        with self.assertRaises(VoiceError):
            sanitize_song_name("   ")
        with self.assertRaises(VoiceError):
            sanitize_song_name('/\\:*?"<>|')

    def test_format_bytes(self):
        self.assertEqual(format_bytes(500), "500 B")
        self.assertEqual(format_bytes(2048), "2.0 KB")
        self.assertEqual(format_bytes(2 * 1024 * 1024), "2.0 MB")

    def test_save_and_list_songs(self):
        p1, n1 = save_preset_song(self.dir, "人是猫", ".mp3", b"ID3" + b"x" * 100)
        self.assertTrue(p1.is_file())
        self.assertEqual(n1, "人是猫")
        self.assertEqual(p1.name, "人是猫.mp3")

        p2, n2 = save_preset_song(self.dir, "小星星", "wav", b"RIFF" + b"y" * 200)
        self.assertTrue(p2.is_file())
        self.assertEqual(n2, "小星星")
        self.assertEqual(p2.name, "小星星.wav")

        songs = list_preset_songs_detail(self.dir)
        self.assertEqual(len(songs), 2)
        names = {s["name"] for s in songs}
        self.assertEqual(names, {"人是猫", "小星星"})

    def test_save_invalid_extension(self):
        with self.assertRaises(VoiceError):
            save_preset_song(self.dir, "恶意文件", ".exe", b"binary")

    def test_save_empty_content(self):
        with self.assertRaises(VoiceError):
            save_preset_song(self.dir, "空文件", ".mp3", b"")

    def test_delete_song(self):
        save_preset_song(self.dir, "测试歌曲", ".mp3", b"ID3" + b"z" * 50)
        self.assertTrue((self.dir / "测试歌曲.mp3").exists())
        del_name = delete_preset_song(self.dir, "测试歌曲.mp3")
        self.assertEqual(del_name, "测试歌曲")
        self.assertFalse((self.dir / "测试歌曲.mp3").exists())

    def test_delete_nonexistent(self):
        with self.assertRaises(VoiceError):
            delete_preset_song(self.dir, "不存在.mp3")

    def test_delete_traversal(self):
        with self.assertRaises(VoiceError):
            delete_preset_song(self.dir, "../etc/passwd")

    def test_rename_song(self):
        save_preset_song(self.dir, "旧名称", ".mp3", b"ID3" + b"a" * 50)
        new_path, new_name = rename_preset_song(self.dir, "旧名称.mp3", "新名称")
        self.assertEqual(new_name, "新名称")
        self.assertEqual(new_path.name, "新名称.mp3")
        self.assertFalse((self.dir / "旧名称.mp3").exists())
        self.assertTrue((self.dir / "新名称.mp3").exists())

    def test_rename_conflict(self):
        save_preset_song(self.dir, "歌曲A", ".mp3", b"ID3" + b"a" * 50)
        save_preset_song(self.dir, "歌曲B", ".mp3", b"ID3" + b"b" * 50)
        with self.assertRaises(VoiceError):
            rename_preset_song(self.dir, "歌曲A.mp3", "歌曲B")

    def test_get_preset_song_path_security(self):
        save_preset_song(self.dir, "安全歌曲", ".mp3", b"ID3" + b"a" * 50)
        path = get_preset_song_path(self.dir, "安全歌曲.mp3")
        self.assertEqual(path.name, "安全歌曲.mp3")
        with self.assertRaises(VoiceError):
            get_preset_song_path(self.dir, "../../secret")


if __name__ == "__main__":
    unittest.main()
