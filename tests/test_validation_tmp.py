from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "skills/agent-workflow/scripts/validation_tmp.py"


class ValidationTemporaryRootTests(unittest.TestCase):
    def run_wrapper(
        self,
        policy_root: Path,
        capture: Path,
        *,
        child_exit: int,
        tamper_lease: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        tamper = (
            "lease['nonce']='tampered';"
            "pathlib.Path(os.environ['PI_SKILLS_VALIDATION_TMP_LEASE']).write_text(json.dumps(lease));"
            if tamper_lease
            else ""
        )
        program = (
            "import json,os,pathlib,sys;"
            "root=pathlib.Path(os.environ['TMPDIR']);"
            "lease=json.loads(pathlib.Path(os.environ['PI_SKILLS_VALIDATION_TMP_LEASE']).read_text());"
            "(root/'.canary-fixture').mkdir();"
            "control=root/'.agent-workflow-canary-control'/'fixture'/'frozen-repository';"
            "control.mkdir(parents=True);"
            "evidence=control/'evidence.json';"
            "evidence.write_text('{}\\n');"
            "evidence.chmod(0o400);"
            "control.chmod(0o500);"
            f"pathlib.Path({str(capture)!r}).write_text(json.dumps({{'tmpdir':str(root),'lease':lease}}));"
            f"{tamper}"
            f"sys.exit({child_exit})"
        )
        environment = os.environ.copy()
        environment.update(
            {
                "PI_SKILLS_TMP_ROOT": str(policy_root),
                "PI_SKILLS_OWC_ROOT": str(policy_root / "unavailable-default"),
            }
        )
        environment.pop("PI_SKILLS_VALIDATION_TMP_ACTIVE", None)
        return subprocess.run(
            [
                sys.executable,
                "-B",
                str(WRAPPER),
                "run",
                "--repo",
                str(ROOT),
                "--",
                sys.executable,
                "-B",
                "-c",
                program,
            ],
            cwd=ROOT,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_wrapper_cleans_normal_failure_and_readonly_residue(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            case = Path(raw)
            policy_root = case / "policy-tmp"
            policy_root.mkdir()
            policy_root = policy_root.resolve()
            for child_exit in (0, 17):
                with self.subTest(child_exit=child_exit):
                    capture = case / f"capture-{child_exit}.json"
                    observed = self.run_wrapper(
                        policy_root,
                        capture,
                        child_exit=child_exit,
                    )
                    self.assertEqual(observed.returncode, child_exit, observed.stderr)
                    captured = json.loads(capture.read_text(encoding="utf-8"))
                    used = Path(captured["tmpdir"])
                    lease = captured["lease"]
                    self.assertTrue(used.is_relative_to(policy_root))
                    self.assertEqual(lease["run_root"], str(used))
                    self.assertEqual(lease["base_root"], str(policy_root))
                    self.assertEqual(lease["selection"], "explicit")
                    self.assertIsInstance(lease["nonce"], str)
                    self.assertIsInstance(lease["base_device"], int)
                    self.assertEqual(lease["base_device"], lease["mount_device"])
                    self.assertFalse(used.exists())
                    self.assertEqual(list(policy_root.iterdir()), [])

    def test_wrapper_preserves_evidence_when_lease_authority_drifts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            case = Path(raw)
            policy_root = case / "policy-tmp"
            policy_root.mkdir()
            capture = case / "capture-tampered.json"
            observed = self.run_wrapper(
                policy_root.resolve(),
                capture,
                child_exit=0,
                tamper_lease=True,
            )
            self.assertEqual(observed.returncode, 74, observed.stderr)
            self.assertIn("lease authority drifted", observed.stderr)
            used = Path(json.loads(capture.read_text(encoding="utf-8"))["tmpdir"])
            self.assertTrue(used.is_dir())
            self.assertTrue((used / ".agent-workflow-canary-control").is_dir())

    def test_wrapper_fails_closed_without_a_policy_managed_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            missing = Path(raw) / "not-mounted"
            environment = os.environ.copy()
            for key in (
                "PI_SKILLS_TMP_ROOT",
                "PI_SKILLS_VALIDATION_TMP_ACTIVE",
                "RUNNER_TEMP",
                "CI",
            ):
                environment.pop(key, None)
            environment["PI_SKILLS_OWC_ROOT"] = str(missing)
            observed = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(WRAPPER),
                    "run",
                    "--repo",
                    str(ROOT),
                    "--",
                    sys.executable,
                    "-c",
                    "raise SystemExit(0)",
                ],
                cwd=ROOT,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertNotEqual(observed.returncode, 0)
            self.assertIn("policy-managed temporary root is unavailable", observed.stderr)
            self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
