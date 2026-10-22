"""面板权限、并发版本及无隐性付费的点播路由测试。"""

import json
import unittest
from unittest.mock import patch

import astrbot_plugin_whale_voice.main as module
import test_main as fixtures
from astrbot_plugin_whale_voice.library import LibraryError


class Request:
    def __init__(
        self,
        body=None,
        *,
        username="admin",
        method="POST",
        headers=None,
        files=None,
        query=None,
    ):
        self.body = body or {}
        self.username = username
        self.method = method
        self.headers = headers or {}
        self.files = files or {}
        self.form = {}
        self.query = query or {}
        self.reads = 0

    async def json(self):
        self.reads += 1
        return self.body


class Upload:
    filename = "测试曲.wav"

    def __init__(self, body=b"RIFF" + b"x" * 80):
        self.body = body
        self.reads = []

    async def read(self, size):
        self.reads.append(size)
        result = self.body[:size]
        self.body = self.body[size:]
        return result


class WebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.PluginTests.asyncSetUp(self)

    async def asyncTearDown(self):
        await fixtures.PluginTests.asyncTearDown(self)

    def song(self, name="测试曲", enabled=True, aliases=None):
        stage = self.plugin.library.create_stage()
        stage.write_bytes(b"RIFF" + b"x" * 80)
        row = self.plugin.library.commit(stage, name, ".wav", {"duration_seconds": 1})
        return self.plugin.library.update(
            row["filename"],
            row["revision"],
            aliases=aliases or [],
            description="",
            enabled=enabled,
        )

    async def test_unauthenticated_mutation_rejected_before_body_read(self):
        request = Request({"filename": "anything"}, username="")
        with patch.object(module, "request", request):
            result = await self.plugin.api_songs_delete()
        self.assertEqual(result["status_code"], 403)
        self.assertEqual(request.reads, 0)

    async def test_cross_origin_write_rejected(self):
        request = Request(headers={"origin": "https://evil.test", "host": "127.0.0.1:6185"})
        with patch.object(module, "request", request):
            result = await self.plugin.api_songs_upload()
        self.assertEqual(result["status_code"], 403)
        self.assertEqual(request.reads, 0)

    async def test_request_size_rejected_before_parsing(self):
        request = Request(
            headers={
                "content-length": str(100 * 1024 * 1024),
                "content-type": "application/json",
            }
        )
        with patch.object(module, "request", request):
            result = await self.plugin.api_songs_upload()
        self.assertEqual(result["status_code"], 413)
        self.assertEqual(request.reads, 0)

    async def test_upload_stream_reads_bounded_chunks_and_creates_draft(self):
        upload = Upload()
        request = Request(files={"file": upload})
        with (
            patch.object(module, "request", request),
            patch.object(module, "probe_audio", return_value={"duration_seconds": 1}),
        ):
            result = await self.plugin.api_songs_upload()
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["data"]["enabled"])
        self.assertTrue(upload.reads)
        self.assertTrue(all(n == 65536 for n in upload.reads))

    async def test_invalid_audio_not_saved(self):
        request = Request(files={"file": Upload()})
        with (
            patch.object(module, "request", request),
            patch.object(module, "probe_audio", side_effect=LibraryError("音频无效")),
        ):
            result = await self.plugin.api_songs_upload()
        self.assertEqual(result["status"], "error")
        self.assertFalse(self.plugin.library.list())
        self.assertFalse(list(self.plugin.library.staging.iterdir()))

    async def test_duplicate_upload_does_not_replace(self):
        row = self.song()
        before = (self.plugin.songs_dir / row["filename"]).read_bytes()
        request = Request(files={"file": Upload(b"RIFF" + b"y" * 80)})
        with (
            patch.object(module, "request", request),
            patch.object(module, "probe_audio", return_value={"duration_seconds": 1}),
        ):
            result = await self.plugin.api_songs_upload()
        self.assertEqual(result["status_code"], 409)
        self.assertEqual((self.plugin.songs_dir / row["filename"]).read_bytes(), before)

    async def test_invalid_base64(self):
        with patch.object(
            module, "request", Request({"filename": "歌.wav", "content_base64": "????"})
        ):
            result = await self.plugin.api_songs_upload()
        self.assertEqual(result["status"], "error")
        self.assertFalse(self.plugin.library.list())

    async def test_api_error_does_not_echo_internal_exception(self):
        with (
            patch.object(module, "request", Request(files={"file": Upload()})),
            patch.object(
                module,
                "probe_audio",
                side_effect=RuntimeError("private-internal-path-and-token"),
            ),
        ):
            result = await self.plugin.api_songs_upload()
        self.assertNotIn("private-internal", json.dumps(result))
        self.assertEqual(result["status_code"], 500)

    async def test_update_stale_conflict(self):
        row = self.song()
        self.plugin.library.update(
            row["filename"],
            row["revision"],
            aliases=["新"],
            description="",
            enabled=True,
        )
        with patch.object(
            module,
            "request",
            Request(
                {
                    "filename": row["filename"],
                    "revision": row["revision"],
                    "aliases": ["旧"],
                    "description": "",
                    "enabled": True,
                }
            ),
        ):
            result = await self.plugin.api_songs_update()
        self.assertEqual(result["status_code"], 409)
        self.assertEqual(self.plugin.library.get(row["filename"])["aliases"], ["新"])

    async def test_list_contains_draft_status_and_limits(self):
        self.song(enabled=False)
        with patch.object(module, "request", Request(method="GET")):
            result = await self.plugin.api_songs_list()
        self.assertEqual(result["data"]["enabled_count"], 0)
        self.assertEqual(result["data"]["total_count"], 1)
        self.assertEqual(result["data"]["limits"]["duration_seconds"], 600)

    async def test_delete_returns_nonempty_bridge_data(self):
        row = self.song()
        with patch.object(
            module,
            "request",
            Request({"filename": row["filename"], "revision": row["revision"]}),
        ):
            result = await self.plugin.api_songs_delete()
        self.assertTrue(result["data"]["deleted"])

    async def test_unknown_song_never_uses_credentials_or_synthesis(self):
        self.plugin._speech_key = lambda: (_ for _ in ()).throw(AssertionError("不应取云端凭据"))
        result = await self.plugin.whale_sing_short_song(self.event, "不存在")
        self.assertIn("没有找到", result)
        self.plugin._make_audio.assert_not_called()
        self.assertFalse(self.event.sent)

    async def test_preset_works_without_cloud_key_or_singer(self):
        self.song(aliases=["小鲸鱼"])
        self.plugin._speech_key = lambda: (_ for _ in ()).throw(AssertionError())
        self.plugin._singer = lambda: (_ for _ in ()).throw(AssertionError())
        result = await self.plugin.whale_sing_short_song(self.event, "小鲸鱼")
        self.assertIn("QQ", result)
        self.assertEqual(self.plugin._make_audio.call_args.args[0], "preset")
        row = json.loads(next(self.plugin.records.glob("*.json")).read_text(encoding="utf-8"))
        self.assertFalse(row["network_attempted"])
        self.assertEqual(row["source"], "preset_library")

    async def test_ambiguous_song_no_audio_no_reservation(self):
        self.song("人是猫试听")
        self.song("人是猫完整版")
        result = await self.plugin.whale_sing_short_song(self.event, "人是猫")
        self.assertIn("多个版本", result)
        self.plugin._make_audio.assert_not_called()
        self.assertFalse(self.plugin.gate.state["requests"])

    async def test_paused_song_not_available(self):
        self.song(enabled=False)
        result = await self.plugin.whale_sing_short_song(self.event, "测试曲")
        self.assertIn("没有找到", result)
        self.assertFalse(self.event.sent)

    async def test_song_list_respects_authorization(self):
        self.song()
        self.event.group = "unauthorized"
        result = [r async for r in self.plugin.list_songs(self.event)]
        self.assertNotIn("测试曲", str(result))

    async def test_catalog_excludes_paused_songs(self):
        self.song("启用曲")
        self.song("暂停曲", enabled=False)
        result = json.loads(await self.plugin.whale_song_catalog(self.event))
        self.assertEqual([r["name"] for r in result["songs"]], ["启用曲"])

    async def test_improvise_not_configured_does_not_fallback(self):
        result = await self.plugin.whale_create_short_song(self.event, "原创歌词")
        self.assertIn("真正的歌唱服务", result)
        self.plugin._make_audio.assert_not_called()

    async def test_corrupt_catalog_does_not_break_speech_readiness(self):
        self.plugin.library.index.write_text("invalid", encoding="utf-8")
        status = self.plugin.readiness()
        self.assertTrue(status["speech_ready"])
        self.assertFalse(status["library_ready"])

    async def test_invalid_private_voice_config_not_reserved_or_sent(self):
        (self.plugin.root / "voice-aliases.json").write_text("broken", encoding="utf-8")
        self.plugin.config["voice"] = "custom"
        result = await self.plugin.whale_voice_reply(self.event, "你好")
        self.assertIn("配置读取失败", result)
        self.plugin._make_audio.assert_not_called()
        self.assertFalse(self.plugin.gate.state["requests"])

    async def test_custom_voice_explicit_config_accepted(self):
        self.plugin.config.update(voice="custom", custom_voice_uri="speech:fixture:test:voice")
        result = await self.plugin.whale_voice_reply(self.event, "你好")
        self.assertIn("QQ", result)

    async def test_legacy_alias_private_file_accepted(self):
        self.plugin.config["voice"] = "dafeiyu_ssr"
        (self.plugin.root / "voice-aliases.json").write_text(
            json.dumps({"dafeiyu_ssr": "speech:fixture:test:voice"}), encoding="utf-8"
        )
        result = await self.plugin.whale_voice_reply(self.event, "你好")
        self.assertIn("QQ", result)

    async def test_normal_feedback_not_unspaced_command(self):
        for text in ("说得有道理", "唱得真好听", "唱歌真好听"):
            event = fixtures.Event(text=text, is_at_or_wake_command=True)
            self.assertFalse(module.UnspacedSingFilter().filter(event, {}))
            self.assertFalse(module.UnspacedSpeechFilter().filter(event, {}))


if __name__ == "__main__":
    unittest.main()
