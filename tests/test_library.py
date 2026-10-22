"""曲库索引、原件保护及音频边界回归；不请求付费服务。"""

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from astrbot_plugin_whale_voice.library import (
    LibraryError,
    SongLibrary,
    disk_io,
    probe_audio,
    render_song,
)


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.library = SongLibrary(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, name="测试歌", enabled=False):
        stage = self.library.create_stage()
        stage.write_bytes(b"RIFF" + b"x" * 80)
        row = self.library.commit(stage, name, ".wav", {"duration_seconds": 1.0})
        return (
            self.library.update(
                row["filename"],
                row["revision"],
                aliases=[],
                description="",
                enabled=enabled,
            )
            if enabled
            else row
        )

    def test_legacy_songs_enabled_without_rewriting_files(self):
        p = self.library.songs / "原歌.mp3"
        p.write_bytes(b"original")
        self.assertTrue(self.library.list()[0]["enabled"])
        self.assertFalse(self.library.index.exists())
        self.assertEqual(p.read_bytes(), b"original")

    def test_upload_is_draft(self):
        row = self.create()
        self.assertFalse(row["enabled"])
        self.assertIsNone(self.library.match("测试歌"))

    def test_duplicate_keeps_original(self):
        row = self.create()
        before = (self.library.songs / row["filename"]).read_bytes()
        with self.assertRaises(LibraryError):
            self.create()
        self.assertEqual((self.library.songs / row["filename"]).read_bytes(), before)

    def test_enable_alias_and_description(self):
        row = self.create()
        row = self.library.update(
            row["filename"],
            row["revision"],
            aliases=["小鲸鱼", "好听的歌"],
            description="测试说明",
            enabled=True,
        )
        self.assertEqual(self.library.match("小鲸鱼")["filename"], row["filename"])
        self.assertEqual(row["description"], "测试说明")

    def test_pause_excludes_lookup(self):
        row = self.create(enabled=True)
        self.library.update(
            row["filename"], row["revision"], aliases=[], description="", enabled=False
        )
        self.assertIsNone(self.library.match("测试歌"))

    def test_stale_update_rejected(self):
        row = self.create()
        self.library.update(
            row["filename"],
            row["revision"],
            aliases=["新"],
            description="",
            enabled=True,
        )
        with self.assertRaises(LibraryError):
            self.library.update(
                row["filename"],
                row["revision"],
                aliases=["旧"],
                description="",
                enabled=True,
            )
        self.assertEqual(self.library.get(row["filename"])["aliases"], ["新"])

    def test_stale_delete_rejected(self):
        row = self.create()
        self.library.update(
            row["filename"], row["revision"], aliases=[], description="", enabled=True
        )
        with self.assertRaises(LibraryError):
            self.library.delete(row["filename"], row["revision"])
        self.assertTrue((self.library.songs / row["filename"]).exists())

    def test_missing_revision_rejected(self):
        row = self.create()
        with self.assertRaises(LibraryError):
            self.library.delete(row["filename"], "")

    def test_bad_metadata_fails_closed(self):
        self.create()
        self.library.index.write_text("broken", encoding="utf-8")
        with self.assertRaises(LibraryError):
            self.library.list()
        self.assertTrue(any(self.library.songs.iterdir()))

    def test_rename_keeps_alias_and_disabled_state(self):
        row = self.create()
        row = self.library.update(
            row["filename"],
            row["revision"],
            aliases=["别名"],
            description="说明",
            enabled=False,
        )
        renamed = self.library.rename(row["filename"], row["revision"], "新名字")
        self.assertEqual(renamed["aliases"], ["别名"])
        self.assertFalse(renamed["enabled"])
        self.assertEqual(renamed["description"], "说明")

    def test_rename_collision_preserves_both(self):
        a = self.create("甲")
        self.create("乙")
        with self.assertRaises(LibraryError):
            self.library.rename(a["filename"], a["revision"], "乙")
        self.assertEqual(len(self.library.list()), 2)

    def test_delete_returns_name_and_removes_only_target(self):
        a = self.create("甲")
        self.create("乙")
        self.assertEqual(self.library.delete(a["filename"], a["revision"]), "甲")
        self.assertEqual([r["name"] for r in self.library.list()], ["乙"])

    def test_delete_index_failure_restores_original(self):
        row = self.create()
        with (
            patch("astrbot_plugin_whale_voice.library.atomic_json", side_effect=OSError),
            self.assertRaises(OSError),
        ):
            self.library.delete(row["filename"], row["revision"])
        self.assertEqual(self.library.get(row["filename"])["revision"], row["revision"])

    def test_rename_unlink_failure_restores_metadata(self):
        row = self.create()
        row = self.library.update(
            row["filename"],
            row["revision"],
            aliases=["原别名"],
            description="原说明",
            enabled=False,
        )
        old = self.library.songs / row["filename"]
        unlink = Path.unlink

        def fail(path, *a, **k):
            if path == old:
                raise PermissionError("simulated")
            return unlink(path, *a, **k)

        with patch.object(Path, "unlink", fail), self.assertRaises(PermissionError):
            self.library.rename(row["filename"], row["revision"], "新歌")
        restored = self.library.get(row["filename"])
        self.assertFalse(restored["enabled"])
        self.assertEqual(restored["aliases"], ["原别名"])

    def test_commit_index_failure_rolls_back_new_file(self):
        stage = self.library.create_stage()
        stage.write_bytes(b"x" * 80)
        with (
            patch("astrbot_plugin_whale_voice.library.atomic_json", side_effect=OSError),
            self.assertRaises(OSError),
        ):
            self.library.commit(stage, "新歌", ".mp3", {"duration_seconds": 1})
        self.assertFalse(list(self.library.songs.iterdir()))
        self.assertTrue(stage.exists())

    def test_total_capacity_and_count(self):
        self.create("甲")
        with (
            patch("astrbot_plugin_whale_voice.library.MAX_SONGS", 1),
            self.assertRaises(LibraryError),
        ):
            self.create("乙")
        with (
            patch("astrbot_plugin_whale_voice.library.MAX_LIBRARY_BYTES", 1),
            self.assertRaises(LibraryError),
        ):
            self.create("丙")

    def test_exact_name_before_partial(self):
        self.create("人是猫", True)
        self.create("人是猫完整版", True)
        self.assertEqual(self.library.match("人是猫")["name"], "人是猫")

    def test_multiple_partial_results_require_clarification(self):
        self.create("人是猫试听", True)
        self.create("人是猫完整版", True)
        with self.assertRaisesRegex(LibraryError, "多个版本"):
            self.library.match("人是猫")

    def test_ambiguous_aliases_not_silently_selected(self):
        for name in ("甲", "乙"):
            r = self.create(name)
            self.library.update(
                r["filename"],
                r["revision"],
                aliases=["同一个别名"],
                description="",
                enabled=True,
            )
        with self.assertRaises(LibraryError):
            self.library.match("同一个别名")

    def test_disabled_song_preview_allowed_but_play_denied(self):
        row = self.create()
        target = self.root / "copy.wav"
        with self.assertRaises(LibraryError):
            self.library.snapshot(row["filename"], row["revision"], target)
        self.library.snapshot(row["filename"], row["revision"], target, allow_disabled=True)
        self.assertTrue(target.is_file())

    def test_snapshot_remains_after_library_delete(self):
        row = self.create(enabled=True)
        target = self.root / "playing.wav"
        self.library.snapshot(row["filename"], row["revision"], target)
        self.library.delete(row["filename"], row["revision"])
        self.assertTrue(target.is_file())

    def test_invalid_settings(self):
        for aliases, description, enabled in (
            ([""], "", True),
            (["x" * 41], "", True),
            (["a"] * 13, "", True),
            ([], "x" * 201, True),
            ([], "", "false"),
        ):
            row = self.create(str(len(self.library.list())))
            with self.subTest(aliases=aliases), self.assertRaises(LibraryError):
                self.library.update(
                    row["filename"],
                    row["revision"],
                    aliases=aliases,
                    description=description,
                    enabled=enabled,
                )

    def test_quoted_names_are_data(self):
        self.create("O'Clock", True)
        self.assertEqual(self.library.match("O'Clock")["name"], "O'Clock")


class MediaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "input.mp3"
        self.source.write_bytes(b"x" * 100)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_probe_accepts_audio(self):
        with patch(
            "astrbot_plugin_whale_voice.library.process",
            return_value=json.dumps(
                {"format": {"duration": "240"}, "streams": [{"codec_type": "audio"}]}
            ).encode(),
        ):
            self.assertEqual((await probe_audio(self.source))["duration_seconds"], 240)

    async def test_probe_blocks_network_protocols_and_playlists(self):
        payload = {"format": {"duration": "5"}, "streams": [{"codec_type": "audio"}]}
        with patch(
            "astrbot_plugin_whale_voice.library.process",
            return_value=json.dumps(payload).encode(),
        ) as process:
            await probe_audio(self.source)
        args = process.call_args.args
        self.assertEqual(args[args.index("-protocol_whitelist") + 1], "file,pipe")
        self.assertNotIn("hls", args[args.index("-format_whitelist") + 1])

    async def test_probe_rejects_invalid_media(self):
        for payload in (
            {"format": {"duration": "nan"}, "streams": [{"codec_type": "audio"}]},
            {"format": {"duration": "700"}, "streams": [{"codec_type": "audio"}]},
            {"format": {"duration": "10"}, "streams": [{"codec_type": "video"}]},
            {
                "format": {"duration": "10"},
                "streams": [{"codec_type": "audio"}, {"codec_type": "video"}],
            },
        ):
            with (
                self.subTest(payload=payload),
                patch(
                    "astrbot_plugin_whale_voice.library.process",
                    return_value=json.dumps(payload).encode(),
                ),
                self.assertRaises(LibraryError),
            ):
                await probe_audio(self.source)

    async def test_album_art_is_not_rejected_as_video(self):
        payload = {
            "format": {"duration": "10"},
            "streams": [
                {"codec_type": "audio"},
                {"codec_type": "video", "disposition": {"attached_pic": 1}},
            ],
        }
        with patch(
            "astrbot_plugin_whale_voice.library.process",
            return_value=json.dumps(payload).encode(),
        ):
            self.assertEqual((await probe_audio(self.source))["duration_seconds"], 10)

    async def test_full_song_not_limited_to_short_tts(self):
        out = self.root / "play.mp3"

        async def render(*args, **kw):
            out.write_bytes(b"ID3" + b"x" * 80)
            self.assertNotIn("-t", args)
            return b""

        with (
            patch(
                "astrbot_plugin_whale_voice.library.probe_audio",
                return_value={"duration_seconds": 240},
            ),
            patch("astrbot_plugin_whale_voice.library.process", side_effect=render),
        ):
            self.assertEqual(await render_song(self.source, out), 240)

    async def test_preview_is_bounded(self):
        out = self.root / "preview.mp3"

        async def render(*args, **kw):
            out.write_bytes(b"ID3" + b"x" * 80)
            self.assertEqual(args[args.index("-t") + 1], "30")
            return b""

        with (
            patch(
                "astrbot_plugin_whale_voice.library.probe_audio",
                return_value={"duration_seconds": 240},
            ),
            patch("astrbot_plugin_whale_voice.library.process", side_effect=render),
        ):
            self.assertEqual(await render_song(self.source, out, preview=True), 30)

    async def test_disk_worker_finishes_before_cancel_releases_lock(self):
        completed = []

        def work():
            time.sleep(0.06)
            completed.append(True)

        task = asyncio.create_task(disk_io(work))
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(completed, [True])


if __name__ == "__main__":
    unittest.main()
