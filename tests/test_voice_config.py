"""私有音色迁移与开源默认值回归。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from astrbot_plugin_whale_voice.core import VoiceError, resolve_voice
from astrbot_plugin_whale_voice.voice_config import (
    VoiceConfigError,
    extract_legacy_aliases,
    load_voice_aliases,
    migrate_voice_aliases,
)

URI = "speech:fixture:sample:voice"


class VoiceConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old = self.root / "old_core.py"
        self.old.write_text("VOICE_ALIASES = {'dafeiyu': '" + URI + "'}\n", encoding="utf-8")
        self.target = self.root / "private" / "voice-aliases.json"

    def tearDown(self):
        self.temp.cleanup()

    def test_no_hidden_private_default(self):
        with self.assertRaises(VoiceError):
            resolve_voice("dafeiyu")

    def test_explicit_alias_preserves_voice(self):
        self.assertEqual(resolve_voice("dafeiyu", {"dafeiyu": URI}), URI)

    def test_builtin_full_identifier_idempotent(self):
        uri = resolve_voice("claire")
        self.assertEqual(resolve_voice(uri), uri)

    def test_invalid_alias_value_not_sent(self):
        with self.assertRaises(VoiceError):
            resolve_voice("alias", {"alias": "https://not-a-voice.test/"})

    def test_missing_private_file_not_created(self):
        self.assertEqual(load_voice_aliases(self.target), {})
        self.assertFalse(self.target.exists())

    def test_dry_run_does_not_write(self):
        result = migrate_voice_aliases(self.old, self.target)
        self.assertEqual(result["added_count"], 1)
        self.assertFalse(result["written"])
        self.assertFalse(self.target.exists())

    def test_apply_and_repeat_are_idempotent(self):
        self.assertTrue(migrate_voice_aliases(self.old, self.target, apply=True)["written"])
        first = self.target.read_bytes()
        self.assertFalse(migrate_voice_aliases(self.old, self.target, apply=True)["written"])
        self.assertEqual(self.target.read_bytes(), first)
        self.assertEqual(load_voice_aliases(self.target)["dafeiyu"], URI)

    def test_result_contains_no_voice_values(self):
        result = migrate_voice_aliases(self.old, self.target, apply=True)
        self.assertNotIn(URI, json.dumps(result))

    def test_conflicting_private_mapping_not_overwritten(self):
        self.target.parent.mkdir()
        old = {"dafeiyu": "speech:fixture:other:voice"}
        self.target.write_text(json.dumps(old), encoding="utf-8")
        with self.assertRaises(VoiceConfigError):
            migrate_voice_aliases(self.old, self.target, apply=True)
        self.assertEqual(load_voice_aliases(self.target), old)

    def test_migration_does_not_execute_source(self):
        source = "raise RuntimeError('must-not-run')\nVOICE_ALIASES = {'voice': '" + URI + "'}\n"
        self.assertEqual(extract_legacy_aliases(source), {"voice": URI})

    def test_dynamic_source_expression_not_executed(self):
        with self.assertRaises(VoiceConfigError):
            extract_legacy_aliases("VOICE_ALIASES = __import__('os').environ\n")

    def test_malformed_json_fails_closed(self):
        self.target.parent.mkdir()
        self.target.write_text("{broken", encoding="utf-8")
        with self.assertRaises(VoiceConfigError):
            load_voice_aliases(self.target)

    def test_bad_mapping_types_rejected(self):
        for obj in ([URI], {"": URI}, {"voice": 123}, {"voice": "invalid"}):
            with self.subTest(obj=obj), self.assertRaises(VoiceConfigError):
                extract_legacy_aliases("VOICE_ALIASES = " + repr(obj))

    def test_atomic_failure_keeps_previous_configuration(self):
        self.target.parent.mkdir()
        self.target.write_text("{}", encoding="utf-8")
        with (
            patch(
                "astrbot_plugin_whale_voice.voice_config.os.replace", side_effect=PermissionError
            ),
            self.assertRaises(PermissionError),
        ):
            migrate_voice_aliases(self.old, self.target, apply=True)
        self.assertEqual(load_voice_aliases(self.target), {})
        self.assertFalse(list(self.target.parent.glob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
