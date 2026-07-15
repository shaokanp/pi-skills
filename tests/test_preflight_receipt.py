from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RECEIPT_TOOL = PROJECT_ROOT / "scripts" / "preflight_receipt.py"


class PreflightReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "fixture"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "Fixture"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "fixture@example.test"], cwd=self.root, check=True
        )
        (self.root / ".gitignore").write_text(".workflow/\n", encoding="utf-8")
        (self.root / "tracked.txt").write_text("stable\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "fixture"], cwd=self.root, check=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_tool(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(RECEIPT_TOOL), *arguments, "--root", str(self.root)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def fingerprint(self) -> str:
        result = self.run_tool("fingerprint")
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)["fingerprint"]

    def test_pass_receipt_is_reused_only_for_the_exact_public_tree(self) -> None:
        stages_path = self.root / ".workflow" / "stages.json"
        stages_path.parent.mkdir()
        stages_path.write_text(
            json.dumps(
                [
                    {"name": "validate-all", "status": "pass"},
                    {"name": "repository-tests", "status": "pass"},
                    {"name": "package-artifacts", "status": "pass"},
                ]
            ),
            encoding="utf-8",
        )
        recorded = self.run_tool(
            "record",
            "--stages-json",
            str(stages_path),
            "--expected-fingerprint",
            self.fingerprint(),
        )
        self.assertEqual(recorded.returncode, 0, recorded.stderr)
        receipt = json.loads(recorded.stdout)
        self.assertEqual(receipt["status"], "pass")

        reused = self.run_tool("check")
        self.assertEqual(reused.returncode, 0, reused.stderr)
        self.assertEqual(json.loads(reused.stdout)["fingerprint"], receipt["fingerprint"])

        ignored = self.root / ".workflow" / "runtime-noise.bin"
        ignored.write_bytes(b"ignored runtime state")
        still_reused = self.run_tool("check")
        self.assertEqual(still_reused.returncode, 0, still_reused.stderr)

        (self.root / "tracked.txt").write_text("changed\n", encoding="utf-8")
        invalidated = self.run_tool("check")
        self.assertEqual(invalidated.returncode, 1)
        self.assertIn("fingerprint mismatch", invalidated.stderr)

    def test_failed_stage_cannot_authorize_a_receipt(self) -> None:
        stages_path = self.root / "stages.json"
        stages_path.write_text(
            json.dumps([{"name": "validate-all", "status": "fail"}]), encoding="utf-8"
        )
        result = self.run_tool(
            "record",
            "--stages-json",
            str(stages_path),
            "--expected-fingerprint",
            self.fingerprint(),
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("all preflight stages must pass", result.stderr)

    def test_source_drift_during_preflight_cannot_be_recorded(self) -> None:
        expected = self.fingerprint()
        stages_path = self.root / "stages.json"
        stages_path.write_text(
            json.dumps([{"name": "validate-all", "status": "pass"}]), encoding="utf-8"
        )
        (self.root / "tracked.txt").write_text("changed during test\n", encoding="utf-8")
        result = self.run_tool(
            "record",
            "--stages-json",
            str(stages_path),
            "--expected-fingerprint",
            expected,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("inputs changed while stages were running", result.stderr)


if __name__ == "__main__":
    unittest.main()
