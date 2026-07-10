from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCANNER = PROJECT_ROOT / "scripts" / "public_safety.py"


class PublicSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.run_git("init", "-q")
        self.run_git("config", "user.name", "Public Test")
        self.run_git("config", "user.email", "public@example.test")
        (self.root / ".gitignore").write_text(".pi-skills.local.json\n", encoding="utf-8")
        (self.root / ".pi-skills.local.json").write_text(
            json.dumps({"private_markers": ["internal-" + "codename"]}),
            encoding="utf-8",
        )
        (self.root / "README.md").write_text("# Public fixture\n", encoding="utf-8")
        self.run_git("add", ".gitignore", "README.md")
        self.run_git("commit", "-qm", "initial public fixture")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def scan(
        self,
        mode: str,
        *,
        refs_file: Path | None = None,
        require_markers: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            sys.executable,
            str(SCANNER),
            "--root",
            str(self.root),
            "--mode",
            mode,
        ]
        if refs_file is not None:
            command.extend(["--refs-file", str(refs_file)])
        if require_markers:
            command.append("--require-local-markers")
        return subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_clean_worktree_and_history_pass(self) -> None:
        self.assertEqual(self.scan("worktree", require_markers=True).returncode, 0)
        self.assertEqual(self.scan("history", require_markers=True).returncode, 0)

    def test_generic_private_home_path_fails(self) -> None:
        private_path = "/" + "Users" + "/someone/private-workspace"
        (self.root / "notes.md").write_text(private_path, encoding="utf-8")
        result = self.scan("worktree")
        self.assertEqual(result.returncode, 1)
        self.assertIn("absolute Unix user home path", result.stderr)

    def test_local_private_marker_fails_without_exposing_value(self) -> None:
        marker = "internal-" + "codename"
        (self.root / "notes.md").write_text(marker, encoding="utf-8")
        result = self.scan("worktree", require_markers=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("local private marker", result.stderr)
        self.assertNotIn(marker, result.stderr)

    def test_index_scan_catches_staged_secret_hidden_by_worktree_edit(self) -> None:
        token = "gh" + "p_" + ("A" * 32)
        secret_path = self.root / "config.md"
        secret_path.write_text(token, encoding="utf-8")
        self.run_git("add", "config.md")
        secret_path.write_text("sanitized working copy\n", encoding="utf-8")

        self.assertEqual(self.scan("worktree").returncode, 0)
        result = self.scan("index")
        self.assertEqual(result.returncode, 1)
        self.assertIn("GitHub token", result.stderr)

    def test_history_scan_catches_secret_removed_by_later_commit(self) -> None:
        token = "gh" + "p_" + ("B" * 32)
        secret_path = self.root / "old-config.md"
        secret_path.write_text(token, encoding="utf-8")
        self.run_git("add", "old-config.md")
        self.run_git("commit", "-qm", "add old config")
        secret_path.unlink()
        self.run_git("add", "-u")
        self.run_git("commit", "-qm", "remove old config")

        self.assertEqual(self.scan("worktree").returncode, 0)
        result = self.scan("history")
        self.assertEqual(result.returncode, 1)
        self.assertIn("GitHub token", result.stderr)

    def test_history_scan_ignores_unpublished_local_branch(self) -> None:
        main_branch = self.run_git("branch", "--show-current").stdout.strip()
        self.run_git("switch", "-qc", "local-only")
        private_path = "/" + "Users" + "/someone/private-workspace"
        (self.root / "local-notes.md").write_text(private_path, encoding="utf-8")
        self.run_git("add", "local-notes.md")
        self.run_git("commit", "-qm", "local-only fixture")
        self.run_git("switch", "-q", main_branch)

        result = self.scan("history")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_first_push_scans_full_reachable_history(self) -> None:
        token = "gh" + "p_" + ("C" * 32)
        secret_path = self.root / "push-config.md"
        secret_path.write_text(token, encoding="utf-8")
        self.run_git("add", "push-config.md")
        self.run_git("commit", "-qm", "unsafe push fixture")
        tip = self.run_git("rev-parse", "HEAD").stdout.strip()
        refs_file = self.root / "push-refs.txt"
        refs_file.write_text(
            f"refs/heads/main {tip} refs/heads/main {'0' * len(tip)}\n",
            encoding="utf-8",
        )

        result = self.scan("push", refs_file=refs_file)
        self.assertEqual(result.returncode, 1)
        self.assertIn("GitHub token", result.stderr)

    def test_publish_gate_can_require_local_private_markers(self) -> None:
        (self.root / ".pi-skills.local.json").unlink()
        result = self.scan("history", require_markers=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("no private_markers configured", result.stderr)

    def test_artifact_scan_rejects_removed_skill_archive(self) -> None:
        (self.root / "registry.json").write_text(
            json.dumps({"skills": []}), encoding="utf-8"
        )
        artifact_root = self.root / "dist"
        artifact_root.mkdir()
        (artifact_root / "removed-skill-1.0.0.tar.gz").write_bytes(b"stale")

        result = self.scan("artifacts")
        self.assertEqual(result.returncode, 1)
        self.assertIn("unexpected unregistered artifact", result.stderr)


if __name__ == "__main__":
    unittest.main()
