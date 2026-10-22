"""公开源码、可重复归档与维护脚本的跨目录回归。"""

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def load(name):
    key = "whale_public_" + name
    spec = importlib.util.spec_from_file_location(key, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


public = load("public_tree")
with patch.dict(sys.modules, {"public_tree": public}):
    release = load("build_release")


class PublicReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_current_public_tree_has_no_known_secrets(self):
        self.assertEqual(public.scan_tree(ROOT), [])

    def test_runtime_ignore_does_not_hide_native_page(self):
        patterns = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("/songs/", patterns)
        self.assertNotIn("songs/", patterns)

    def test_scanner_does_not_echo_secret(self):
        key = "sk-" + "abc123" * 8
        (self.root / "main.py").write_text("key = '" + key + "'", encoding="utf-8")
        result = public.scan_tree(self.root)
        self.assertTrue(result)
        self.assertNotIn(key, json.dumps(result))

    def test_scanner_rejects_nonfixture_voice_uri(self):
        uri = "speech:" + "private:actual:voice"
        (self.root / "main.py").write_text("voice = '" + uri + "'", encoding="utf-8")
        self.assertTrue(public.scan_tree(self.root))

    def test_fixture_voice_uri_is_explicitly_allowed(self):
        (self.root / "main.py").write_text("voice = 'speech:fixture:test:voice'", encoding="utf-8")
        self.assertEqual(public.scan_tree(self.root), [])

    def test_unknown_or_runtime_files_not_packaged(self):
        (self.root / "main.py").write_text("pass", encoding="utf-8")
        (self.root / "voice-aliases.json").write_text("{}")
        (self.root / "songs").mkdir()
        (self.root / "songs/secret.mp3").write_bytes(b"audio")
        self.assertEqual([p.name for p in public.public_files(self.root)], ["main.py"])

    def test_forced_git_runtime_file_is_rejected(self):
        (self.root / ".git").mkdir()
        result = types.SimpleNamespace(stdout=b"voice-aliases.json\0")
        with patch.object(public.subprocess, "run", return_value=result):
            problems = public.scan_tree(self.root)
        self.assertEqual(problems[0]["file"], "voice-aliases.json")

    def test_archive_deterministic_and_contains_manifest(self):
        first = release.build(ROOT, self.root / "one")
        second = release.build(ROOT, self.root / "two")
        self.assertEqual(first["sha256"], second["sha256"])
        with zipfile.ZipFile(first["archive"]) as archive:
            self.assertIsNone(archive.testzip())
            manifest = json.loads(archive.read("astrbot_plugin_whale_voice/PUBLIC_FILES.json"))
            self.assertIn("LICENSE", manifest["files"])
            self.assertIn("pages/songs/index.html", manifest["files"])
            self.assertIn("voice_config.py", manifest["files"])
            self.assertIn("backup.py", manifest["files"])
            self.assertIn("scripts/manage_library.py", manifest["files"])
            self.assertFalse(
                any(name.endswith((".mp3", ".wav", ".db", ".log")) for name in archive.namelist())
            )

    def test_archive_normalizes_windows_line_endings_and_bom(self):
        copied = self.root / "windows-checkout"
        for source in public.public_files(ROOT):
            target = copied / source.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            blob = source.read_bytes()
            if source.suffix.lower() not in {".png", ".webp"}:
                blob = b"\xef\xbb\xbf" + blob.decode("utf-8-sig").replace("\r\n", "\n").replace(
                    "\n", "\r\n"
                ).encode("utf-8")
            target.write_bytes(blob)
        first = release.build(ROOT, self.root / "linux-output")
        second = release.build(copied, self.root / "windows-output")
        self.assertEqual(first["sha256"], second["sha256"])

    def test_existing_different_release_not_overwritten(self):
        target = self.root / "out"
        target.mkdir()
        archive = target / ("astrbot_plugin_whale_voice-v" + release.release_version(ROOT) + ".zip")
        archive.write_bytes(b"previous-release")
        with self.assertRaises(ValueError):
            release.build(ROOT, target)
        self.assertEqual(archive.read_bytes(), b"previous-release")

    def test_schema_does_not_authorize_or_embed_private_data(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(schema["allowed_groups"]["default"], [])
        self.assertEqual(schema["custom_voice_uri"]["default"], "")
        self.assertEqual(schema["speech_credential_provider_id"]["default"], "")
        self.assertEqual(schema["sing_provider_id"]["default"], "")
        self.assertFalse(schema["allow_experimental_tts_singing"]["default"])

    def test_runner_works_with_renamed_repository(self):
        root = self.root / "renamed-repository"
        (root / "scripts").mkdir(parents=True)
        (root / "tests").mkdir()
        (root / "__init__.py").write_text("")
        for name in ("_bootstrap.py", "run_tests.py"):
            shutil.copy2(SCRIPTS / name, root / "scripts" / name)
        (root / "tests/test_one.py").write_text(
            "import unittest\nclass One(unittest.TestCase):\n def test_one(self): self.assertTrue(True)\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            [sys.executable, str(root / "scripts/run_tests.py")],
            cwd=root,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_migration_cli_with_renamed_repository(self):
        root = self.root / "another-name"
        (root / "scripts").mkdir(parents=True)
        (root / "__init__.py").write_text("")
        for name in ("_bootstrap.py", "migrate_voice_config.py"):
            shutil.copy2(SCRIPTS / name, root / "scripts" / name)
        shutil.copy2(ROOT / "voice_config.py", root / "voice_config.py")
        old = self.root / "legacy.py"
        old.write_text("VOICE_ALIASES={'old':'speech:fixture:test:voice'}", encoding="utf-8")
        target = self.root / "private/voice-aliases.json"
        result = subprocess.run(
            [
                sys.executable,
                str(root / "scripts/migrate_voice_config.py"),
                "--legacy-source",
                str(old),
                "--destination",
                str(target),
                "--apply",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(target.exists())
        self.assertNotIn("speech:fixture", result.stdout)


if __name__ == "__main__":
    unittest.main()
