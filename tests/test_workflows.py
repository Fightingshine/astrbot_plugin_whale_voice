"""CI定义的静态安全约束，不冒充已经在GitHub运行。"""

import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


class WorkflowTests(unittest.TestCase):
    def test_ci_has_cross_platform_matrix_and_no_write_token(self):
        obj = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8-sig"))
        self.assertEqual(obj["permissions"], {"contents": "read"})
        self.assertIn("pull_request", obj["on"])
        self.assertNotIn("pull_request_target", obj["on"])
        matrix = obj["jobs"]["test"]["strategy"]["matrix"]
        self.assertEqual(set(matrix["os"]), {"ubuntu-latest", "windows-latest"})
        self.assertEqual(set(matrix["python"]), {"3.10", "3.12"})

    def test_external_actions_pinned_to_commit(self):
        for path in (ROOT / ".github/workflows").glob("*.yml"):
            obj = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
            for job in obj["jobs"].values():
                for step in job.get("steps", []):
                    action = step.get("uses", "")
                    if action and not action.startswith("./"):
                        self.assertRegex(action, r"^[\w-]+/[\w-]+@[0-9a-f]{40}$")

    def test_release_requires_checks_and_version_match(self):
        obj = yaml.safe_load(
            (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8-sig")
        )
        self.assertEqual(obj["jobs"]["publish"]["needs"], "checks")
        commands = "\n".join(str(s.get("run", "")) for s in obj["jobs"]["publish"]["steps"])
        self.assertIn("release_version", commands)
        self.assertIn("--verify-tag", commands)
        self.assertEqual(obj["jobs"]["publish"]["permissions"], {"contents": "write"})

    def test_ci_does_not_need_audio_provider_keys(self):
        for path in (ROOT / ".github/workflows").glob("*.yml"):
            content = path.read_text(encoding="utf-8-sig")
            self.assertNotRegex(
                content, re.compile(r"secrets\.(?:OPENAI|SILICON|MIMO|TTS|VOICE)", re.I)
            )


if __name__ == "__main__":
    unittest.main()
