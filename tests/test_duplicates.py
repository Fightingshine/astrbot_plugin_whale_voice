"""原音去重与有界指纹缓存；全部使用临时自制字节，不调用云端。"""

from __future__ import annotations

import concurrent.futures
import hashlib
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from astrbot_plugin_whale_voice import fingerprints
from astrbot_plugin_whale_voice.library import DuplicateAudioError, LibraryError, SongLibrary


class FingerprintTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "原音.wav"
        self.path.write_bytes(b"RIFF" + bytes(range(256)) * 1000)
        self.cache = fingerprints.FingerprintCache(max_entries=2)

    def tearDown(self):
        self.temp.cleanup()

    def test_digest_and_size_match(self):
        result = self.cache.fingerprint(self.path)
        self.assertEqual(result.sha256, hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(result.size_bytes, self.path.stat().st_size)

    def test_unchanged_cache_hit_does_not_read_audio_again(self):
        first = self.cache.fingerprint(self.path)
        with (
            patch.object(fingerprints, "_settled", return_value=True),
            patch.object(
                fingerprints, "_stream_hash", side_effect=AssertionError("不能重复读取原音")
            ),
        ):
            self.assertEqual(self.cache.fingerprint(self.path), first)

    def test_modified_same_size_and_restored_mtime_invalidates_cache(self):
        first = self.cache.fingerprint(self.path)
        before = self.path.stat()
        self.path.write_bytes(b"x" * first.size_bytes)
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns))
        second = self.cache.fingerprint(self.path)
        self.assertNotEqual(first.sha256, second.sha256)

    def test_replaced_file_same_size_and_mtime_invalidates_cache(self):
        first = self.cache.fingerprint(self.path)
        before = self.path.stat()
        replacement = self.root / "替换.wav"
        replacement.write_bytes(b"z" * first.size_bytes)
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.replace(replacement, self.path)
        self.assertNotEqual(self.cache.fingerprint(self.path).sha256, first.sha256)

    def test_modified_during_hash_is_rejected_and_not_cached(self):
        original = fingerprints._stream_hash

        def changed(handle, expected):
            result = original(handle, expected)
            self.path.write_bytes(b"changed")
            return result

        with patch.object(fingerprints, "_stream_hash", side_effect=changed):
            with self.assertRaises(fingerprints.FingerprintError):
                self.cache.fingerprint(self.path)
        self.assertEqual(len(self.cache), 0)

    def test_streaming_reads_are_bounded(self):
        body = self.path.read_bytes()

        class Stream:
            def __init__(self):
                self.offset = 0
                self.sizes = []

            def read(self, size):
                self.sizes.append(size)
                result = body[self.offset : self.offset + size]
                self.offset += len(result)
                return result

        source = Stream()
        self.assertEqual(
            fingerprints._stream_hash(source, len(body)), hashlib.sha256(body).hexdigest()
        )
        self.assertTrue(all(size == 65536 for size in source.sizes))

    def test_recent_timestamp_cache_is_not_trusted(self):
        self.cache.fingerprint(self.path)
        with (
            patch.object(fingerprints, "_settled", return_value=False),
            patch.object(fingerprints, "_stream_hash", wraps=fingerprints._stream_hash) as stream,
        ):
            self.cache.fingerprint(self.path)
        self.assertEqual(stream.call_count, 1)

    def test_stable_real_file_uses_cache_after_settling_window(self):
        first = self.cache.fingerprint(self.path)
        time.sleep(3.2)
        with patch.object(
            fingerprints, "_stream_hash", side_effect=AssertionError("稳定原音不应重复读取")
        ):
            self.assertEqual(self.cache.fingerprint(self.path), first)

    def test_coalesced_recent_metadata_still_rehashes_content(self):
        actual = self.path.stat()
        recent = (actual.st_dev, actual.st_ino, actual.st_size, time.time_ns(), time.time_ns())
        with patch.object(fingerprints, "_state", return_value=recent):
            first = self.cache.fingerprint(self.path)
            self.path.write_bytes(b"z" * first.size_bytes)
            second = self.cache.fingerprint(self.path)
        self.assertNotEqual(first.sha256, second.sha256)

    def test_change_stamp_failure_does_not_publish_cache(self):
        with patch.object(
            fingerprints,
            "_native_change_stamp",
            side_effect=fingerprints.FingerprintError("无法核对状态"),
        ):
            with self.assertRaises(fingerprints.FingerprintError):
                self.cache.fingerprint(self.path)
        self.assertEqual(len(self.cache), 0)

    def test_stream_size_mismatch_rejected(self):
        import io

        for expected in (1, 100):
            with self.subTest(expected=expected), self.assertRaises(fingerprints.FingerprintError):
                fingerprints._stream_hash(io.BytesIO(b"abc"), expected)

    def test_cache_has_lru_bound_and_forget(self):
        self.cache.fingerprint(self.path)
        for i in range(3):
            path = self.root / (str(i) + ".wav")
            path.write_bytes(b"fixture" + bytes([i]))
            self.cache.fingerprint(path)
        self.assertEqual(len(self.cache), 2)
        self.cache.forget(path)
        self.assertEqual(len(self.cache), 1)

    def test_parallel_reads_share_cache(self):
        with (
            patch.object(fingerprints, "_settled", return_value=True),
            patch.object(fingerprints, "_stream_hash", wraps=fingerprints._stream_hash) as stream,
        ):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(self.cache.fingerprint, [self.path, self.path]))
        self.assertEqual(results[0], results[1])
        self.assertEqual(stream.call_count, 1)

    def test_empty_oversized_and_directory_rejected(self):
        self.path.write_bytes(b"")
        for path in (self.path, self.root):
            with self.subTest(path=path.name), self.assertRaises(fingerprints.FingerprintError):
                self.cache.fingerprint(path)
        self.path.write_bytes(b"abcd")
        with (
            patch.object(fingerprints, "MAX_BYTES", 3),
            self.assertRaises(fingerprints.FingerprintError),
        ):
            self.cache.fingerprint(self.path)

    def test_link_not_followed(self):
        link = self.root / "link.wav"
        try:
            link.symlink_to(self.path)
        except OSError:
            self.skipTest("当前Windows会话无符号链接权限。")
        with self.assertRaises(fingerprints.FingerprintError):
            self.cache.fingerprint(link)

    def test_read_failure_is_sanitized(self):
        with patch.object(
            fingerprints.os, "open", side_effect=PermissionError("private-source-path")
        ):
            with self.assertRaises(fingerprints.FingerprintError) as raised:
                self.cache.fingerprint(self.path)
        self.assertNotIn("private-source-path", str(raised.exception))


class DuplicateLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.library = SongLibrary(self.root / "data")
        self.body = b"RIFF" + b"original" * 15

    def tearDown(self):
        self.temp.cleanup()

    def stage(self, body=None):
        path = self.library.create_stage()
        path.write_bytes(self.body if body is None else body)
        return path

    def add(self, name="原歌", enabled=False, body=None):
        row = self.library.commit(self.stage(body), name, ".wav", {"duration_seconds": 1})
        if enabled:
            row = self.library.update(
                row["filename"],
                row["revision"],
                aliases=["旧别名"],
                description="原说明",
                enabled=True,
            )
        return row

    def test_identical_different_name_rejected_without_changing_original(self):
        old = self.add(enabled=True)
        before = self.library.index.read_bytes()
        stage = self.stage()
        with self.assertRaises(DuplicateAudioError) as raised:
            self.library.commit(stage, "不同名字", ".wav", {"duration_seconds": 1})
        self.assertEqual(
            raised.exception.duplicates,
            [{"filename": old["filename"], "name": old["name"], "enabled": True}],
        )
        self.assertEqual(self.library.index.read_bytes(), before)
        self.assertEqual(self.library.list(), [old])
        self.assertTrue(stage.exists())

    def test_draft_and_legacy_audio_both_checked(self):
        row = self.add()
        with self.assertRaises(DuplicateAudioError) as raised:
            self.library.check_upload(self.stage(), "新名", ".mp3")
        self.assertFalse(raised.exception.duplicates[0]["enabled"])
        self.library.delete(row["filename"], row["revision"])
        (self.library.songs / "旧版原音.mp3").write_bytes(self.body)
        with self.assertRaises(DuplicateAudioError) as raised:
            self.library.check_upload(self.stage(), "新名", ".wav")
        self.assertTrue(raised.exception.duplicates[0]["enabled"])

    def test_different_bytes_may_be_added(self):
        self.add()
        self.add("另一首", body=b"RIFF" + b"different")
        self.assertEqual(len(self.library.list()), 2)

    def test_rename_does_not_leave_stale_duplicate_name(self):
        row = self.add()
        new = self.library.rename(row["filename"], row["revision"], "重命名")
        with self.assertRaises(DuplicateAudioError) as raised:
            self.library.check_upload(self.stage(), "新上传", ".wav")
        self.assertEqual(raised.exception.duplicates[0]["filename"], new["filename"])

    def test_deleted_file_no_longer_blocks_new_upload(self):
        row = self.add()
        self.library.delete(row["filename"], row["revision"])
        self.add("重新上传")
        self.assertEqual(len(self.library.list()), 1)

    def test_external_content_change_invalidates_cached_digest(self):
        row = self.add()
        different = b"z" * len(self.body)
        staged = self.stage(different)
        self.library.check_upload(staged, "预检查", ".wav")
        path = self.library.songs / row["filename"]
        before = path.stat()
        path.write_bytes(different)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 10000000))
        with patch.object(fingerprints, "_settled", return_value=True):
            with self.assertRaises(DuplicateAudioError):
                self.library.commit(staged, "新名", ".wav", {"duration_seconds": 1})

    def test_commit_rechecks_after_preflight(self):
        stage = self.stage()
        expected = self.library.check_upload(stage, "后上传", ".wav")
        self.add()
        with self.assertRaises(DuplicateAudioError):
            self.library.commit(
                stage, "后上传", ".wav", {"duration_seconds": 1}, expected_fingerprint=expected
            )

    def test_staged_audio_changed_after_probe_rejected(self):
        stage = self.stage()
        expected = self.library.check_upload(stage, "新名", ".wav")
        stage.write_bytes(b"changed")
        with self.assertRaises(LibraryError):
            self.library.commit(
                stage, "新名", ".wav", {"duration_seconds": 1}, expected_fingerprint=expected
            )
        self.assertEqual(self.library.list(), [])

    def test_parallel_same_content_only_committed_once(self):
        stages = [self.stage(), self.stage()]

        def commit(pair):
            i, path = pair
            try:
                return self.library.commit(path, "并发" + str(i), ".wav", {"duration_seconds": 1})
            except DuplicateAudioError:
                return None

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(commit, enumerate(stages)))
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual(len(self.library.list()), 1)

    def test_list_and_chat_matching_do_not_hash_audio(self):
        self.add(enabled=True)
        with patch.object(
            self.library.hashes, "fingerprint", side_effect=AssertionError("列表不能读取整首歌")
        ):
            self.assertEqual(len(self.library.list()), 1)
            self.assertIsNotNone(self.library.match("原歌"))

    def test_duplicate_error_does_not_expose_digest_or_path(self):
        self.add()
        with self.assertRaises(DuplicateAudioError) as raised:
            self.library.check_upload(self.stage(), "新名", ".wav")
        text = str(raised.exception)
        self.assertNotIn(str(self.root), text)
        self.assertNotIn(hashlib.sha256(self.body).hexdigest(), text)
