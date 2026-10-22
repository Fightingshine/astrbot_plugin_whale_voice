"""备份/恢复的往返、边界、反穿越与只读回归；全部使用临时自制数据。"""

from __future__ import annotations

import copy
import hashlib
import json
import stat
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from astrbot_plugin_whale_voice import backup
from astrbot_plugin_whale_voice.library import LibraryError, SongLibrary


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.library = SongLibrary(self.data)
        self.archive = self.root / "private/library.zip"

    def tearDown(self):
        self.temp.cleanup()

    def song(self, name="原音", enabled=True, aliases=None):
        stage = self.library.create_stage()
        body = b"RIFF" + hashlib.sha256(name.encode("utf-8")).digest() + bytes(range(32))
        stage.write_bytes(body)
        row = self.library.commit(stage, name, ".wav", {"duration_seconds": 1.25})
        row = self.library.update(
            row["filename"],
            row["revision"],
            aliases=aliases or ["测试别名"],
            description="用户自己的说明",
            enabled=enabled,
        )
        return row, body

    def test_regular_opens_file_rewritten_after_creation(self):
        path = self.root / "rewritten.wav"
        path.write_bytes(b"initial")
        time.sleep(0.02)
        path.write_bytes(b"updated")
        with backup._regular(path) as source:
            self.assertEqual(source.read(), b"updated")

    def test_open_boundary_handles_creation_and_change_time_difference(self):
        path = self.root / "stat-clock.wav"
        path.write_bytes(b"fixture")
        actual = path.stat()
        fields = {
            key: getattr(actual, key)
            for key in ("st_mode", "st_dev", "st_ino", "st_size", "st_mtime_ns")
        }
        path_stat = SimpleNamespace(**fields, st_ctime_ns=100, st_birthtime_ns=100)
        opened_stat = SimpleNamespace(**fields, st_ctime_ns=200, st_birthtime_ns=100)
        self.assertNotEqual(backup._signature(path_stat), backup._signature(opened_stat))
        with (
            patch.object(Path, "stat", return_value=path_stat),
            patch.object(backup.os, "fstat", return_value=opened_stat),
            backup._regular(path) as source,
        ):
            self.assertEqual(source.read(), b"fixture")

    def test_open_boundary_still_rejects_identity_size_and_write_changes(self):
        path = self.root / "identity.wav"
        path.write_bytes(b"fixture")
        actual = path.stat()
        fields = {
            key: getattr(actual, key)
            for key in ("st_mode", "st_dev", "st_ino", "st_size", "st_mtime_ns")
        }
        fields.update(st_ctime_ns=100, st_birthtime_ns=100)
        for key in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_birthtime_ns"):
            with self.subTest(changed=key):
                changed = {**fields, key: fields[key] + 1}
                with (
                    patch.object(Path, "stat", return_value=SimpleNamespace(**fields)),
                    patch.object(backup.os, "fstat", return_value=SimpleNamespace(**changed)),
                    self.assertRaises(backup.BackupError),
                    backup._regular(path),
                ):
                    self.fail("不能接受已变化的文件。")

    def test_open_boundary_without_birthtime_keeps_change_time_check(self):
        path = self.root / "change-time.wav"
        path.write_bytes(b"fixture")
        actual = path.stat()
        fields = {
            key: getattr(actual, key)
            for key in ("st_mode", "st_dev", "st_ino", "st_size", "st_mtime_ns")
        }
        fields["st_ctime_ns"] = 100
        with (
            patch.object(Path, "stat", return_value=SimpleNamespace(**fields)),
            patch.object(
                backup.os, "fstat", return_value=SimpleNamespace(**{**fields, "st_ctime_ns": 200})
            ),
            self.assertRaises(backup.BackupError),
            backup._regular(path),
        ):
            self.fail("没有创建时间字段时仍要检查状态变化时间。")

    def entries(self):
        with zipfile.ZipFile(self.archive) as z:
            return [(copy.copy(info), z.read(info)) for info in z.infolist()]

    def rewrite(self, entries, name="modified.zip", *, comment=b""):
        path = self.root / name
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(path, "w") as z:
                for info, body in entries:
                    z.writestr(info, body)
                z.comment = comment
        return path

    def manifest_change(self, mutator):
        entries = self.entries()
        for i, (info, body) in enumerate(entries):
            if info.filename == backup.MANIFEST:
                obj = json.loads(body)
                mutator(obj)
                entries[i] = (info, json.dumps(obj, ensure_ascii=False).encode())
        return self.rewrite(entries)

    def test_roundtrip_preserves_bytes_metadata_and_pauses_all(self):
        first, body = self.song(aliases=["猫猫", "小曲"])
        self.song("另一首", enabled=False)
        result = backup.create_backup(self.data, self.archive)
        self.assertEqual(result["song_count"], 2)
        self.assertEqual(
            result["archive_sha256"], hashlib.sha256(self.archive.read_bytes()).hexdigest()
        )
        self.assertTrue(backup.verify_backup(self.archive)["valid"])
        target = self.root / "restored"
        result = backup.restore_backup(self.archive, target)
        self.assertEqual(result["enabled_count"], 0)
        restored = SongLibrary(target, readonly=True)
        rows = restored.list()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(not r["enabled"] for r in rows))
        row = restored.get(first["filename"])
        self.assertEqual(row["aliases"], ["猫猫", "小曲"])
        self.assertEqual(row["description"], "用户自己的说明")
        self.assertEqual((target / "songs" / first["filename"]).read_bytes(), body)
        self.assertFalse((target / ".restore-incomplete").exists())

    def test_source_files_and_index_not_modified(self):
        self.song()
        before = {
            p.relative_to(self.data).as_posix(): p.read_bytes()
            for p in self.data.rglob("*")
            if p.is_file()
        }
        backup.create_backup(self.data, self.archive)
        self.assertEqual(
            before,
            {
                p.relative_to(self.data).as_posix(): p.read_bytes()
                for p in self.data.rglob("*")
                if p.is_file()
            },
        )

    def test_backup_does_not_create_runtime_staging_in_legacy_library(self):
        root = self.root / "legacy"
        (root / "songs").mkdir(parents=True)
        (root / "songs/original.mp3").write_bytes(b"original audio")
        backup.create_backup(root, self.archive)
        self.assertFalse((root / ".song-staging").exists())
        self.assertFalse((root / "song-library.json").exists())

    def test_neighbouring_credentials_and_private_refs_excluded(self):
        self.song()
        (self.data / "voice-aliases.json").write_text('{"voice":"speech:fixture:test:voice"}')
        (self.data / "config.json").write_text('{"api_key":"test-placeholder"}')
        backup.create_backup(self.data, self.archive)
        with zipfile.ZipFile(self.archive) as z:
            self.assertEqual(set(z.namelist()), {backup.MANIFEST, "songs/原音.wav"})
            self.assertNotIn("speech:fixture", z.read(backup.MANIFEST).decode())

    def test_unknown_source_index_fields_not_archived(self):
        row, _ = self.song()
        index = json.loads(self.library.index.read_text(encoding="utf-8"))
        index["songs"][row["filename"]]["api_key"] = "test-private-placeholder"
        backup.atomic_json(self.library.index, index)
        backup.create_backup(self.data, self.archive)
        with zipfile.ZipFile(self.archive) as z:
            self.assertNotIn("test-private-placeholder", z.read(backup.MANIFEST).decode())

    def test_metadata_looking_like_credential_rejected(self):
        row, _ = self.song()
        self.library.update(
            row["filename"], row["revision"], aliases=[], description="sk-" + "a" * 32, enabled=True
        )
        with self.assertRaises(backup.BackupError):
            backup.create_backup(self.data, self.archive)
        self.assertFalse(self.archive.exists())

    def test_existing_backup_not_overwritten(self):
        self.song()
        self.archive.parent.mkdir()
        self.archive.write_bytes(b"existing")
        with self.assertRaises(backup.BackupError):
            backup.create_backup(self.data, self.archive)
        self.assertEqual(self.archive.read_bytes(), b"existing")

    def test_output_inside_source_rejected(self):
        self.song()
        with self.assertRaises(backup.BackupError):
            backup.create_backup(self.data, self.data / "copy.zip")

    def test_missing_source_not_created(self):
        root = self.root / "missing"
        with self.assertRaises(LibraryError):
            backup.create_backup(root, self.archive)
        self.assertFalse(root.exists())

    def test_empty_library_roundtrip(self):
        backup.create_backup(self.data, self.archive)
        self.assertEqual(backup.verify_backup(self.archive)["song_count"], 0)
        target = self.root / "empty-restored"
        backup.restore_backup(self.archive, target)
        self.assertEqual(SongLibrary(target, readonly=True).list(), [])

    def test_source_change_prevents_publication(self):
        row, _ = self.song()
        original = backup._copy

        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            self.library.update(
                row["filename"],
                self.library.get(row["filename"])["revision"],
                aliases=["changed"],
                description="",
                enabled=True,
            )
            return result

        with (
            patch.object(backup, "_copy", side_effect=changed),
            self.assertRaises(backup.BackupError),
        ):
            backup.create_backup(self.data, self.archive)
        self.assertFalse(self.archive.exists())
        self.assertFalse(list(self.archive.parent.glob("*.tmp")))

    def test_readonly_library_blocks_all_mutators(self):
        row, _ = self.song()
        view = SongLibrary(self.data, readonly=True)
        actions = [
            lambda: view.create_stage(),
            lambda: view.commit(self.root / "none", "x", ".mp3", {}),
            lambda: view.update(
                row["filename"], row["revision"], aliases=[], description="", enabled=True
            ),
            lambda: view.rename(row["filename"], row["revision"], "new"),
            lambda: view.delete(row["filename"], row["revision"]),
            lambda: view.snapshot(row["filename"], row["revision"], self.root / "copy"),
        ]
        for action in actions:
            with self.assertRaises(LibraryError):
                action()

    def test_existing_restore_directory_rejected_even_if_empty(self):
        self.song()
        backup.create_backup(self.data, self.archive)
        target = self.root / "exists"
        target.mkdir()
        with self.assertRaises(backup.BackupError):
            backup.restore_backup(self.archive, target)
        self.assertEqual(list(target.iterdir()), [])

    def test_original_library_cannot_be_restore_target(self):
        row, body = self.song()
        backup.create_backup(self.data, self.archive)
        with self.assertRaises(backup.BackupError):
            backup.restore_backup(self.archive, self.data)
        self.assertEqual((self.data / "songs" / row["filename"]).read_bytes(), body)

    def test_invalid_archive_does_not_create_target_or_parent(self):
        path = self.root / "bad.zip"
        path.write_bytes(b"not zip")
        target = self.root / "missing-parent/target"
        with self.assertRaises(backup.BackupError):
            backup.restore_backup(path, target)
        self.assertFalse(target.parent.exists())

    def test_incomplete_restore_is_disabled_and_marked(self):
        self.song()
        backup.create_backup(self.data, self.archive)
        original = backup._copy

        def fail_write(source, target, *args, **kwargs):
            if target is not None:
                raise OSError("simulated disk failure")
            return original(source, target, *args, **kwargs)

        target = self.root / "partial"
        with (
            patch.object(backup, "_copy", side_effect=fail_write),
            self.assertRaises(backup.BackupError),
        ):
            backup.restore_backup(self.archive, target)
        self.assertTrue((target / ".restore-incomplete").exists())
        index = json.loads((target / "song-library.json").read_text(encoding="utf-8"))
        self.assertTrue(all(not m["enabled"] for m in index["songs"].values()))
        with self.assertRaises(LibraryError):
            SongLibrary(target)

    def test_interrupted_directory_cannot_be_backed_up(self):
        self.song()
        (self.data / ".restore-incomplete").write_text("interrupted")
        with self.assertRaises(LibraryError):
            backup.create_backup(self.data, self.archive)

    def test_archive_modified_during_precheck_rejected(self):
        self.song()
        backup.create_backup(self.data, self.archive)
        original = backup._verify_members

        def mutate(*args):
            original(*args)
            with self.archive.open("ab") as f:
                f.write(b"changed")

        target = self.root / "no-target"
        with (
            patch.object(backup, "_verify_members", side_effect=mutate),
            self.assertRaises(backup.BackupError),
        ):
            backup.restore_backup(self.archive, target)
        self.assertFalse(target.exists())

    def test_limits_applied_to_source(self):
        self.song()
        for name, value in [("MAX_COUNT", 0), ("MAX_TOTAL", 1), ("MAX_FILE", 1)]:
            with (
                self.subTest(name=name),
                patch.object(backup, name, value),
                self.assertRaises(backup.BackupError),
            ):
                backup.create_backup(self.data, self.archive)

    def test_unsafe_member_paths_rejected(self):
        self.song()
        backup.create_backup(self.data, self.archive)
        for name in (
            "../outside.wav",
            "/absolute.wav",
            "songs/../outside.wav",
            "songs/dir/file.wav",
            "songs\\bad.wav",
            "songs/C:bad.wav",
            "songs/CON.wav",
            "songs/trailing.wav ",
        ):
            info = zipfile.ZipInfo(name)
            path = self.rewrite(self.entries() + [(info, b"x")])
            with self.subTest(name=name), self.assertRaises(backup.BackupError):
                backup.verify_backup(path)
        self.assertFalse((self.root / "outside.wav").exists())

    def test_duplicate_casefold_and_unicode_entries_rejected(self):
        self.song()
        backup.create_backup(self.data, self.archive)
        for names in (
            ("songs/原音.wav", "songs/原音.wav"),
            ("songs/Cafe.wav", "songs/cafe.WAV"),
            ("songs/caf\u00e9.wav", "songs/cafe\u0301.wav"),
        ):
            path = self.rewrite(self.entries() + [(zipfile.ZipInfo(n), b"x") for n in names])
            with self.subTest(names=names), self.assertRaises(backup.BackupError):
                backup.verify_backup(path)

    def test_link_directory_and_special_entries_rejected(self):
        self.song()
        backup.create_backup(self.data, self.archive)
        for mode in (stat.S_IFLNK | 0o777, stat.S_IFDIR | 0o700, stat.S_IFIFO | 0o600):
            info = zipfile.ZipInfo("songs/unsafe.wav")
            info.create_system = 3
            info.external_attr = mode << 16
            path = self.rewrite(self.entries() + [(info, b"target")])
            with self.subTest(mode=mode), self.assertRaises(backup.BackupError):
                backup.verify_backup(path)

    def test_unexpected_file_not_accepted(self):
        self.song()
        backup.create_backup(self.data, self.archive)
        path = self.rewrite(self.entries() + [(zipfile.ZipInfo("voice-aliases.json"), b"{}")])
        with self.assertRaises(backup.BackupError):
            backup.verify_backup(path)

    def test_checksum_and_size_mismatch_rejected(self):
        self.song()
        backup.create_backup(self.data, self.archive)
        for key, value in [
            ("sha256", "0" * 64),
            ("size_bytes", 1),
            ("size_bytes", True),
            ("sha256", 123),
        ]:
            path = self.manifest_change(lambda m, k=key, v=value: m["songs"][0].update({k: v}))
            with self.subTest(key=key, value=value), self.assertRaises(backup.BackupError):
                backup.verify_backup(path)

    def test_extra_metadata_and_wrong_types_rejected(self):
        self.song()
        backup.create_backup(self.data, self.archive)
        changes = [
            lambda m: m.update(api_key="test-private-placeholder"),
            lambda m: m["songs"][0].update(extra="unknown"),
            lambda m: m["songs"][0]["metadata"].update(api_key="unknown"),
            lambda m: m["songs"][0]["metadata"].update(enabled="false"),
            lambda m: m["songs"][0]["metadata"].update(duration_seconds=float("nan")),
            lambda m: m.update(format_version=1.0),
            lambda m: m.update(total_bytes=True),
            lambda m: m.update(created_at="not-a-date"),
        ]
        for change in changes:
            path = self.manifest_change(change)
            with self.assertRaises(backup.BackupError):
                backup.verify_backup(path)

    def test_duplicate_json_fields_rejected(self):
        self.song()
        backup.create_backup(self.data, self.archive)
        entries = self.entries()
        for i, (info, body) in enumerate(entries):
            if info.filename == backup.MANIFEST:
                entries[i] = (
                    info,
                    body.replace(
                        b'"format_version": 1', b'"format_version": 1, "format_version": 1'
                    ),
                )
        with self.assertRaises(backup.BackupError):
            backup.verify_backup(self.rewrite(entries))

    def test_archive_comment_rejected(self):
        self.song()
        backup.create_backup(self.data, self.archive)
        with self.assertRaises(backup.BackupError):
            backup.verify_backup(self.rewrite(self.entries(), comment=b"unknown payload"))

    def test_unsupported_compression_rejected(self):
        self.song()
        backup.create_backup(self.data, self.archive)
        entries = self.entries()
        entries[0][0].compress_type = zipfile.ZIP_BZIP2
        with self.assertRaises(backup.BackupError):
            backup.verify_backup(self.rewrite(entries))

    def test_corrupt_compressed_or_stored_audio_rejected(self):
        self.song()
        backup.create_backup(self.data, self.archive)
        raw = bytearray(self.archive.read_bytes())
        raw[raw.index(b"RIFF") + 4] ^= 0xFF
        bad = self.root / "crc.zip"
        bad.write_bytes(raw)
        with self.assertRaises(backup.BackupError):
            backup.verify_backup(bad)

    def test_central_directory_bounds_before_zipfile_parse(self):
        self.song()
        backup.create_backup(self.data, self.archive)
        for kind in ("count", "size", "disks"):
            raw = bytearray(self.archive.read_bytes())
            offset = raw.rfind(b"PK\x05\x06")
            if kind == "count":
                struct.pack_into("<HH", raw, offset + 8, 65535, 65535)
            elif kind == "size":
                struct.pack_into("<L", raw, offset + 12, backup.MAX_CENTRAL + 1)
            else:
                struct.pack_into("<H", raw, offset + 4, 1)
            path = self.root / ("bounds-" + kind + ".zip")
            path.write_bytes(raw)
            with (
                self.subTest(kind=kind),
                patch.object(
                    backup.zipfile, "ZipFile", side_effect=AssertionError("不应进入ZIP解析")
                ),
                self.assertRaises(backup.BackupError),
            ):
                backup.verify_backup(path)

    def test_header_claims_too_large_file_rejected(self):
        self.song()
        backup.create_backup(self.data, self.archive)
        raw = bytearray(self.archive.read_bytes())
        offset = raw.index(b"PK\x01\x02")
        struct.pack_into("<L", raw, offset + 24, backup.MAX_FILE + 1)
        path = self.root / "huge.zip"
        path.write_bytes(raw)
        with self.assertRaises(backup.BackupError):
            backup.verify_backup(path)

    def test_manifest_and_total_bounds(self):
        self.song()
        backup.create_backup(self.data, self.archive)
        with patch.object(backup, "MAX_MANIFEST", 1), self.assertRaises(backup.BackupError):
            backup.verify_backup(self.archive)
        with patch.object(backup, "MAX_TOTAL", 1), self.assertRaises(backup.BackupError):
            backup.verify_backup(self.archive)

    def test_member_comments_and_extra_data_rejected(self):
        self.song()
        backup.create_backup(self.data, self.archive)
        for field, value in (("comment", b"unknown payload"), ("extra", b"\xfe\xca\x00\x00")):
            entries = self.entries()
            setattr(entries[0][0], field, value)
            with self.subTest(field=field), self.assertRaises(backup.BackupError):
                backup.verify_backup(self.rewrite(entries))

    def test_invalid_utf8_zip_filename_is_sanitized_error(self):
        self.song()
        backup.create_backup(self.data, self.archive)
        token = "songs/原音.wav".encode()
        raw = self.archive.read_bytes()
        raw = raw.replace(token, token[:6] + b"\xff" + token[7:])
        path = self.root / "bad-name.zip"
        path.write_bytes(raw)
        with self.assertRaises(backup.BackupError):
            backup.verify_backup(path)
        with self.assertRaises(backup.BackupError):
            backup.restore_backup(path, self.root / "not-restored")
        self.assertFalse((self.root / "not-restored").exists())

    def test_deflate_damage_is_sanitized_error(self):
        self.song()
        backup.create_backup(self.data, self.archive)
        entries = self.entries()
        entries[0][0].compress_type = zipfile.ZIP_DEFLATED
        path = self.rewrite(entries)
        raw = bytearray(path.read_bytes())
        name_len, extra_len = struct.unpack_from("<HH", raw, 26)
        position = 30 + name_len + extra_len
        raw[position] ^= 0xFF
        path.write_bytes(raw)
        with self.assertRaises(backup.BackupError):
            backup.verify_backup(path)

    def test_encrypted_flag_rejected_without_password_prompt(self):
        self.song()
        backup.create_backup(self.data, self.archive)
        raw = bytearray(self.archive.read_bytes())
        central = raw.index(b"PK\x01\x02")
        flags = struct.unpack_from("<H", raw, central + 8)[0]
        struct.pack_into("<H", raw, central + 8, flags | 1)
        path = self.root / "encrypted.zip"
        path.write_bytes(raw)
        with self.assertRaises(backup.BackupError):
            backup.verify_backup(path)

    def test_windows_reserved_name_variants_rejected(self):
        self.song()
        backup.create_backup(self.data, self.archive)
        for name in ("CON .wav", "COM¹.wav", "LPT³.mp3"):
            path = self.rewrite(self.entries() + [(zipfile.ZipInfo("songs/" + name), b"x")])
            with self.subTest(name=name), self.assertRaises(backup.BackupError):
                backup.verify_backup(path)

    def test_restore_race_does_not_replace_winner_directory(self):
        self.song()
        backup.create_backup(self.data, self.archive)
        target = self.root / "race-target"
        original = Path.mkdir

        def race(path, *args, **kwargs):
            if path == target:
                original(path)
                (path / "sentinel.txt").write_text("other operation")
                raise FileExistsError("race")
            return original(path, *args, **kwargs)

        with patch.object(Path, "mkdir", race), self.assertRaises(OSError):
            backup.restore_backup(self.archive, target)
        self.assertEqual((target / "sentinel.txt").read_text(), "other operation")
        self.assertFalse((target / "songs").exists())

    def test_publish_race_never_overwrites_existing_archive(self):
        self.song()
        original = backup.os.link

        def race(source, target, *args, **kwargs):
            if Path(target) == self.archive:
                self.archive.write_bytes(b"winner")
                raise FileExistsError("race")
            return original(source, target, *args, **kwargs)

        with (
            patch.object(backup.os, "link", side_effect=race),
            self.assertRaises(backup.BackupError),
        ):
            backup.create_backup(self.data, self.archive)
        self.assertEqual(self.archive.read_bytes(), b"winner")

    def test_restored_tree_contains_no_account_configuration(self):
        self.song()
        backup.create_backup(self.data, self.archive)
        target = self.root / "clean-restore"
        backup.restore_backup(self.archive, target)
        self.assertEqual({p.name for p in target.iterdir()}, {"songs", "song-library.json"})
        self.assertFalse((target / "voice-aliases.json").exists())

    def test_private_reference_in_metadata_rejected(self):
        self.song()
        backup.create_backup(self.data, self.archive)
        path = self.manifest_change(
            lambda m: m["songs"][0]["metadata"].update(description="speech:fixture:test:voice")
        )
        with self.assertRaises(backup.BackupError):
            backup.verify_backup(path)

    def test_audio_reads_are_chunk_bounded(self):
        self.song()
        original = backup._regular
        reads = []
        import contextlib

        class Reader:
            def __init__(self, wrapped):
                self.wrapped = wrapped

            def read(self, size=-1):
                reads.append(size)
                if not 0 < size <= backup.CHUNK:
                    raise AssertionError("音频不能无界读取")
                return self.wrapped.read(size)

            def __getattr__(self, name):
                return getattr(self.wrapped, name)

        @contextlib.contextmanager
        def wrapped(path):
            with original(path) as handle:
                yield Reader(handle) if Path(path).suffix == ".wav" else handle

        with patch.object(backup, "_regular", wrapped):
            backup.create_backup(self.data, self.archive)
        self.assertTrue(reads)
        self.assertTrue(all(0 < n <= backup.CHUNK for n in reads))

    def test_broken_index_symlink_is_not_treated_as_missing_index(self):
        self.song()
        original = Path.is_symlink

        def symlink(path):
            return path == self.library.index or original(path)

        with patch.object(Path, "is_symlink", symlink), self.assertRaises(LibraryError):
            self.library.list()

    def test_cli_roundtrip_and_errors(self):
        self.song()
        script = Path(__file__).resolve().parents[1] / "scripts/manage_library.py"

        def run(*args):
            return subprocess.run(
                [sys.executable, str(script), *map(str, args)],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )

        first = run("backup", "--data-dir", self.data, "--output", self.archive)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertTrue(json.loads(first.stdout)["backup_created"])
        second = run("verify", "--archive", self.archive)
        self.assertEqual(second.returncode, 0)
        self.assertTrue(json.loads(second.stdout)["valid"])
        third = run("restore", "--archive", self.archive, "--destination", self.root / "cli-target")
        self.assertEqual(third.returncode, 0)
        self.assertEqual(json.loads(third.stdout)["enabled_count"], 0)
        error = run("restore", "--archive", self.archive, "--destination", self.data)
        self.assertNotEqual(error.returncode, 0)
        self.assertFalse(json.loads(error.stdout)["ok"])


if __name__ == "__main__":
    unittest.main()
