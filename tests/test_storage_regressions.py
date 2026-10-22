"""曲库原文件保护回归；不访问语音API或QQ。"""

import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_whale_voice.core import (
    VoiceError,
    get_preset_song_path,
    list_preset_songs_detail,
    save_preset_song,
)


class StorageRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_duplicate_upload_must_not_replace_original(self):
        p, _ = save_preset_song(self.root, "原歌", ".mp3", b"original-audio")
        with self.assertRaises(VoiceError):
            save_preset_song(self.root, "原歌", ".mp3", b"other-audio")
        self.assertEqual(p.read_bytes(), b"original-audio")

    def test_traversal_must_not_alias_an_existing_song(self):
        save_preset_song(self.root, "原歌", ".mp3", b"original-audio")
        with self.assertRaises(VoiceError):
            get_preset_song_path(self.root, "../原歌.mp3")

    def test_non_audio_file_cannot_be_read_as_song(self):
        (self.root / "private.json").write_text("{}")
        with self.assertRaises(VoiceError):
            get_preset_song_path(self.root, "private.json")

    def test_symlink_inside_library_is_rejected(self):
        p, _ = save_preset_song(self.root, "原歌", ".mp3", b"original-audio")
        try:
            (self.root / "链接.mp3").symlink_to(p)
        except OSError:
            self.skipTest("当前Windows账号没有创建符号链接权限")
        with self.assertRaises(VoiceError):
            get_preset_song_path(self.root, "链接.mp3")

    def test_broken_symlink_does_not_break_catalog(self):
        try:
            (self.root / "坏链接.mp3").symlink_to(self.root / "missing.mp3")
        except OSError:
            self.skipTest("当前Windows账号没有创建符号链接权限")
        self.assertEqual(list_preset_songs_detail(self.root), [])


if __name__ == "__main__":
    unittest.main()
