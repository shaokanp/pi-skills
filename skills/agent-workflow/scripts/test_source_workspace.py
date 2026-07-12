#!/usr/bin/env python3
"""Disposable-repository tests for the vNext isolated writer transaction."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
import shutil
import shlex
from unittest import mock
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from source_workspace import (
    DirtyOverlap,
    SourceWriteError,
    attest_writer_permissions,
    integrate_isolated_phase,
    prepare_isolated_phase,
    validate_write_roots,
    writer_profile_bytes,
)
import baseline_gate
import source_workspace


class SourceWorkspaceTests(unittest.TestCase):
    def repository(self, root: Path) -> Path:
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "fixture@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Fixture"], cwd=repo, check=True)
        (repo / "src/api").mkdir(parents=True)
        (repo / "src/web").mkdir(parents=True)
        (repo / "src/api/value.txt").write_text("api-v1\n")
        (repo / "src/web/value.txt").write_text("web-v1\n")
        (repo / "notes.txt").write_text("notes-v1\n")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)
        return repo

    def baseline(self, repo: Path) -> dict[str, object]:
        staged = baseline_gate._diff(repo, cached=True, excludes=[], binary=False)
        unstaged = baseline_gate._diff(repo, cached=False, excludes=[], binary=False)
        untracked_paths = [
            item.decode()
            for item in baseline_gate._git(
                repo, "ls-files", "--others", "--exclude-standard", "-z"
            ).split(b"\0")
            if item
        ]
        parent = {
            "schema_version": "agent-workflow.vnext-pre-slice-summary.v1",
            "head": baseline_gate._git(repo, "rev-parse", "HEAD").decode().strip(),
            "branch": baseline_gate._git(repo, "branch", "--show-current").decode().strip(),
            "staged_diff_sha256": baseline_gate._digest(staged),
            "staged_diff_bytes": len(staged),
            "unstaged_diff_sha256": baseline_gate._digest(unstaged),
            "unstaged_diff_bytes": len(unstaged),
            "untracked": [
                {
                    "path": relative,
                    "sha256": baseline_gate._digest((repo / relative).read_bytes()),
                    "bytes": (repo / relative).stat().st_size,
                }
                for relative in sorted(untracked_paths)
            ],
        }
        return baseline_gate.collect_baseline(
            repo,
            parent_summary=parent,
        )

    def plan(self, *roots: tuple[str, list[str]]) -> dict[str, object]:
        return {
            "phase_id": "002-implement",
            "tasks": [
                {"task_id": task_id, "work_mode": "write", "write_roots": write_roots}
                for task_id, write_roots in roots
            ],
        }

    def test_dirty_overlap_blocks_before_workspace_but_disjoint_dirty_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            repo = self.repository(base)
            control = repo / ".workflow/run"
            (repo / "notes.txt").write_text("user notes\n")
            baseline = self.baseline(repo)
            phase = prepare_isolated_phase(
                control,
                repo,
                self.plan(("api", ["src/api"])),
                admission_baseline=baseline,
            )
            self.assertTrue(phase.tasks["api"].root.is_dir())
            self.assertEqual((repo / "notes.txt").read_text(), "user notes\n")
            (repo / "src/api/value.txt").write_text("user api\n")
            with self.assertRaisesRegex(DirtyOverlap, "src/api/value.txt"):
                prepare_isolated_phase(
                    repo / ".workflow/second",
                    repo,
                    self.plan(("api", ["src/api"])),
                    admission_baseline=baseline,
                )

    def test_snapshot_replays_sealed_staged_unstaged_and_untracked_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            repo = self.repository(base)
            (repo / "lib").mkdir()
            source = repo / "lib/input.txt"
            source.write_text("head\n")
            subprocess.run(["git", "add", "lib/input.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "add lib"], cwd=repo, check=True)
            source.write_text("staged\n")
            subprocess.run(["git", "add", "lib/input.txt"], cwd=repo, check=True)
            source.write_text("sealed-worktree\n")
            (repo / "lib/untracked.bin").write_bytes(b"\x00sealed\xff")
            baseline = self.baseline(repo)
            self.assertTrue(baseline_gate._unpack(baseline["staged_binary_patch"], "staged"))
            self.assertTrue(baseline_gate._unpack(baseline["unstaged_binary_patch"], "unstaged"))
            source.write_text("later-live-drift\n")
            phase = prepare_isolated_phase(
                repo / ".workflow/run",
                repo,
                self.plan(("api", ["src/api"])),
                read_roots=("src", "lib"),
                admission_baseline=baseline,
            )
            workspace = phase.tasks["api"].root
            self.assertEqual((workspace / "lib/input.txt").read_text(), "sealed-worktree\n")
            self.assertEqual((workspace / "lib/untracked.bin").read_bytes(), b"\x00sealed\xff")
            self.assertEqual(source.read_text(), "later-live-drift\n")

    def test_snapshot_caps_fail_before_writer_workspace_launch(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            repo = self.repository(base)
            baseline = self.baseline(repo)
            control = repo / ".workflow/run"
            with mock.patch("source_workspace.MAX_SNAPSHOT_BYTES", 1):
                with self.assertRaisesRegex(SourceWriteError, "snapshot file or byte cap"):
                    prepare_isolated_phase(
                        control,
                        repo,
                        self.plan(("web", ["src/web"])),
                        read_roots=("src/api",),
                        admission_baseline=baseline,
                    )
            self.assertFalse(
                (control / "runtime/source-workspaces/002-implement/web/checkout").exists()
            )

    def test_snapshot_cap_is_rechecked_after_compressed_patch_expansion(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            repo = self.repository(base)
            compressible = repo / "src/api/compressible.bin"
            compressible.write_bytes(b"\x00a" * 32_000)
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "add compressible fixture"], cwd=repo, check=True)
            compressible.write_bytes(b"\x00b" * 128_000)
            baseline = self.baseline(repo)
            self.assertLess(
                baseline["unstaged_binary_patch"]["bytes"],
                128_000,
                "fixture must exercise compressed patch expansion rather than raw patch size",
            )
            control = repo / ".workflow/run"
            with mock.patch("source_workspace.MAX_SNAPSHOT_BYTES", 128_000):
                with self.assertRaisesRegex(SourceWriteError, "materialized source snapshot"):
                    prepare_isolated_phase(
                        control,
                        repo,
                        self.plan(("web", ["src/web"])),
                        read_roots=("src/api",),
                        admission_baseline=baseline,
                    )
            self.assertFalse(
                (control / "runtime/source-workspaces/002-implement/web/checkout").exists()
            )

    def test_two_disjoint_writers_integrate_one_bounded_exact_base_patch(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            repo = self.repository(base)
            baseline = self.baseline(repo)
            phase = prepare_isolated_phase(
                repo / ".workflow/run",
                repo,
                self.plan(("api", ["src/api"]), ("web", ["src/web"])),
                admission_baseline=baseline,
            )
            (phase.tasks["api"].root / "src/api/value.txt").write_text("api-v2\n")
            (phase.tasks["web"].root / "src/web/new.txt").write_text("new web\n")
            result = integrate_isolated_phase(
                phase,
                completed_task_ids={"api", "web"},
            )
            self.assertEqual(result["status"], "applied")
            self.assertEqual((repo / "src/api/value.txt").read_text(), "api-v2\n")
            self.assertEqual((repo / "src/web/new.txt").read_text(), "new web\n")
            self.assertEqual(result["changed_by_task"]["api"], ["src/api/value.txt"])
            self.assertEqual(result["changed_by_task"]["web"], ["src/web/new.txt"])
            patch = json.loads((phase.control_root / result["patch_ref"]).read_text())
            self.assertEqual(len(patch["entries"]), 2)

    def test_live_source_drift_returns_conflict_without_applying_worker_change(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            repo = self.repository(base)
            baseline = self.baseline(repo)
            phase = prepare_isolated_phase(
                repo / ".workflow/run",
                repo,
                self.plan(("api", ["src/api"])),
                admission_baseline=baseline,
            )
            (phase.tasks["api"].root / "src/api/value.txt").write_text("worker\n")
            (repo / "src/api/value.txt").write_text("human\n")
            result = integrate_isolated_phase(phase, completed_task_ids={"api"})
            self.assertEqual(result["status"], "conflict")
            self.assertEqual((repo / "src/api/value.txt").read_text(), "human\n")

    def test_mid_integration_drift_rolls_back_the_atomic_anchor_without_partial_writes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            repo = self.repository(base)
            baseline = self.baseline(repo)
            phase = prepare_isolated_phase(
                repo / ".workflow/run",
                repo,
                self.plan(("api", ["src/api"]), ("web", ["src/web"])),
                admission_baseline=baseline,
            )
            (phase.tasks["api"].root / "src/api/value.txt").write_text("worker-api\n")
            (phase.tasks["web"].root / "src/web/value.txt").write_text("worker-web\n")

            def drift_after_last_precheck() -> None:
                (repo / "src/web/value.txt").write_text("human-web\n")

            result = integrate_isolated_phase(
                phase,
                completed_task_ids={"api", "web"},
                pre_apply_fence=drift_after_last_precheck,
            )
            self.assertEqual(result["status"], "conflict")
            self.assertEqual((repo / "src/api/value.txt").read_text(), "api-v1\n")
            self.assertEqual((repo / "src/web/value.txt").read_text(), "human-web\n")

    def test_crash_after_atomic_swap_recovers_terminal_evidence_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            repo = self.repository(base)
            baseline = self.baseline(repo)
            phase = prepare_isolated_phase(
                repo / ".workflow/run",
                repo,
                self.plan(("api", ["src/api"])),
                admission_baseline=baseline,
            )
            (phase.tasks["api"].root / "src/api/value.txt").write_text("worker-api\n")
            original = source_workspace._create_or_verify_json

            def fail_terminal(root: Path, relative: str, value: object) -> Path:
                if relative.endswith("integration-terminal.json"):
                    raise SourceWriteError("simulated runner crash after atomic swap")
                return original(root, relative, value)

            with mock.patch("source_workspace._create_or_verify_json", side_effect=fail_terminal):
                with self.assertRaisesRegex(SourceWriteError, "simulated runner crash"):
                    integrate_isolated_phase(phase, completed_task_ids={"api"})
            self.assertEqual((repo / "src/api/value.txt").read_text(), "worker-api\n")
            self.assertFalse(
                (phase.control_root / "runtime/source-write/002-implement/integration-terminal.json").exists()
            )
            recovered = integrate_isolated_phase(phase, completed_task_ids={"api"})
            self.assertEqual(recovered["status"], "applied")
            self.assertTrue(
                (phase.control_root / "runtime/source-write/002-implement/integration-terminal.json").is_file()
            )

    def test_recovery_cancel_after_crash_rolls_back_instead_of_ratifying_applied_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            repo = self.repository(base)
            baseline = self.baseline(repo)
            phase = prepare_isolated_phase(
                repo / ".workflow/run",
                repo,
                self.plan(("api", ["src/api"])),
                admission_baseline=baseline,
            )
            (phase.tasks["api"].root / "src/api/value.txt").write_text("worker-api\n")
            original = source_workspace._create_or_verify_json

            def fail_terminal(root: Path, relative: str, value: object) -> Path:
                if relative.endswith("integration-terminal.json"):
                    raise SourceWriteError("simulated runner crash after atomic swap")
                return original(root, relative, value)

            with mock.patch("source_workspace._create_or_verify_json", side_effect=fail_terminal):
                with self.assertRaisesRegex(SourceWriteError, "simulated runner crash"):
                    integrate_isolated_phase(phase, completed_task_ids={"api"})
            self.assertEqual((repo / "src/api/value.txt").read_text(), "worker-api\n")

            with self.assertRaisesRegex(SourceWriteError, "cancelled during recovery"):
                integrate_isolated_phase(
                    phase,
                    completed_task_ids={"api"},
                    pre_apply_fence=lambda: (_ for _ in ()).throw(
                        SourceWriteError("cancelled during recovery")
                    ),
                )
            self.assertEqual((repo / "src/api/value.txt").read_text(), "api-v1\n")
            self.assertFalse(
                (phase.control_root / "runtime/source-write/002-implement/integration-terminal.json").exists()
            )

    def test_cancel_arriving_after_atomic_swap_rolls_back_before_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            repo = self.repository(base)
            baseline = self.baseline(repo)
            phase = prepare_isolated_phase(
                repo / ".workflow/run",
                repo,
                self.plan(("api", ["src/api"])),
                admission_baseline=baseline,
            )
            (phase.tasks["api"].root / "src/api/value.txt").write_text("worker-api\n")
            calls = 0

            def cancel_fence() -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise SourceWriteError("cancelled after swap")

            with self.assertRaisesRegex(SourceWriteError, "cancelled after swap"):
                integrate_isolated_phase(
                    phase,
                    completed_task_ids={"api"},
                    pre_apply_fence=cancel_fence,
                )
            self.assertEqual((repo / "src/api/value.txt").read_text(), "api-v1\n")
            self.assertFalse(
                (phase.control_root / "runtime/source-write/002-implement/integration-terminal.json").exists()
            )

    def test_external_edit_after_swap_rolls_back_before_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            repo = self.repository(base)
            baseline = self.baseline(repo)
            phase = prepare_isolated_phase(
                repo / ".workflow/run",
                repo,
                self.plan(("api", ["src/api"])),
                admission_baseline=baseline,
            )
            (phase.tasks["api"].root / "src/api/value.txt").write_text("worker-api\n")
            calls = 0

            def edit_after_swap() -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    (repo / "src/api/value.txt").write_text("external-after-swap\n")

            result = integrate_isolated_phase(
                phase,
                completed_task_ids={"api"},
                pre_apply_fence=edit_after_swap,
            )
            self.assertEqual(result["status"], "conflict")
            self.assertEqual((repo / "src/api/value.txt").read_text(), "api-v1\n")
            displaced_path = (
                phase.control_root
                / "runtime/source-write/002-implement/displaced-anchor.json"
            )
            displaced = json.loads(displaced_path.read_text())
            self.assertFalse(displaced["cleanup_allowed"])
            self.assertEqual(displaced["reason"], "post_swap_shared_edit")
            retained = phase.control_root / displaced["staging_ref"]
            self.assertEqual((retained / "api/value.txt").read_text(), "external-after-swap\n")

    def test_case_unicode_symlink_and_hardlink_collisions_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            repo = self.repository(base)
            with self.assertRaisesRegex(SourceWriteError, "overlap"):
                validate_write_roots(
                    repo,
                    self.plan(("one", ["src/API"]), ("two", ["src/api/file"]))["tasks"],
                )
            with self.assertRaisesRegex(SourceWriteError, "Unicode-equivalent"):
                validate_write_roots(
                    repo,
                    self.plan(("one", ["src/caf\u00e9", "src/cafe\u0301"]))["tasks"],
                )
            os.symlink(repo / "src/api", repo / "linked")
            with self.assertRaisesRegex(SourceWriteError, "symlink"):
                validate_write_roots(repo, self.plan(("one", ["linked"]))["tasks"])
            os.link(repo / "src/api/value.txt", repo / "hard.txt")
            with self.assertRaisesRegex(SourceWriteError, "hard-linked"):
                validate_write_roots(repo, self.plan(("one", ["hard.txt"]))["tasks"])

    def test_directory_fd_walk_rejects_a_symlinked_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw).resolve()
            actual = base / "actual/child"
            actual.mkdir(parents=True)
            os.symlink(base / "actual", base / "alias")
            with self.assertRaisesRegex(SourceWriteError, "directory-FD path is unsafe"):
                source_workspace._open_directory_no_follow(base / "alias/child")

    def test_worker_symlink_and_out_of_root_changes_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            repo = self.repository(base)
            baseline = self.baseline(repo)
            phase = prepare_isolated_phase(
                repo / ".workflow/run",
                repo,
                self.plan(("api", ["src/api"])),
                read_roots=("src", "notes.txt"),
                admission_baseline=baseline,
            )
            (phase.tasks["api"].root / "notes.txt").write_text("escaped\n")
            with self.assertRaisesRegex(SourceWriteError, "outside its roots"):
                integrate_isolated_phase(phase, completed_task_ids={"api"})
            second = prepare_isolated_phase(
                repo / ".workflow/second",
                repo,
                self.plan(("api", ["src/api"])),
                admission_baseline=baseline,
            )
            os.symlink("value.txt", second.tasks["api"].root / "src/api/link.txt")
            with self.assertRaisesRegex(SourceWriteError, "symlink"):
                integrate_isolated_phase(second, completed_task_ids={"api"})

    def test_named_writer_profile_and_effective_allowlist_are_exact(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw).resolve()
            workspace = base / "workspace"
            home = base / "codex-home"
            (workspace / "src/api").mkdir(parents=True)
            (home / "tmp/arg0/codex-arg0fixture").mkdir(parents=True)
            profile = writer_profile_bytes(("src/api",)).decode()
            self.assertIn('":minimal" = "read"', profile)
            self.assertIn('"src/api" = "write"', profile)
            self.assertIn('"src/api/**" = "write"', profile)
            self.assertNotIn("extends", profile)
            context = {
                "workspace_roots": [str(workspace)],
                "permission_profile": {
                    "type": "managed",
                    "network": "restricted",
                    "file_system": {
                        "type": "restricted",
                        "entries": [
                            {"path": {"type": "special", "value": {"kind": "minimal"}}, "access": "read"},
                            {"path": {"type": "path", "path": str(workspace)}, "access": "read"},
                            {"path": {"type": "path", "path": str(workspace / "src/api")}, "access": "write"},
                            {"path": {"type": "path", "path": str(home / "tmp/arg0/codex-arg0fixture")}, "access": "read"},
                        ],
                    },
                },
            }
            self.assertIsNone(attest_writer_permissions(context, workspace, home, ("src/api",)))
            context["permission_profile"]["file_system"]["entries"].append(
                {"path": {"type": "path", "path": str(base / "escape")}, "access": "write"}
            )
            self.assertIn(
                "not exact",
                attest_writer_permissions(context, workspace, home, ("src/api",)),
            )

    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("codex"), "requires Codex macOS sandbox")
    def test_live_named_profile_denies_git_sibling_and_network(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw).resolve()
            workspace = base / "workspace"
            home = base / "codex-home"
            allowed = workspace / "src/api"
            git_dir = workspace / ".git"
            outside = base / "outside"
            allowed.mkdir(parents=True)
            git_dir.mkdir()
            outside.mkdir()
            home.mkdir()
            control_secret = base / "control-secret.txt"
            credential = home / "auth.json"
            control_secret.write_text("control\n")
            credential.write_text('{"token":"fixture"}\n')
            (home / "vnext-writer.config.toml").write_bytes(writer_profile_bytes(("src/api",)))
            probe = (
                "printf allowed > src/api/ok.txt; a=$?; "
                "printf git > .git/index; g=$?; "
                "printf outside > ../outside/escape.txt; o=$?; "
                f"/bin/cat {shlex.quote(os.fspath(control_secret))} >/dev/null 2>&1; c=$?; "
                f"/bin/cat {shlex.quote(os.fspath(credential))} >/dev/null 2>&1; r=$?; "
                "/usr/bin/curl -m 1 -fsS http://1.1.1.1 >/dev/null 2>&1; n=$?; "
                "printf 'allowed=%s git=%s outside=%s control=%s credential=%s network=%s\\n' \"$a\" \"$g\" \"$o\" \"$c\" \"$r\" \"$n\""
            )
            result = subprocess.run(
                [
                    "codex",
                    "sandbox",
                    "-p",
                    "vnext-writer",
                    "-P",
                    "vnext_writer",
                    "-C",
                    os.fspath(workspace),
                    "/bin/sh",
                    "-c",
                    probe,
                ],
                env={**os.environ, "CODEX_HOME": os.fspath(home)},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            observed = dict(item.split("=", 1) for item in result.stdout.splitlines()[-1].split())
            self.assertEqual(observed["allowed"], "0")
            self.assertNotEqual(observed["git"], "0")
            self.assertNotEqual(observed["outside"], "0")
            self.assertNotEqual(observed["control"], "0")
            self.assertNotEqual(observed["credential"], "0")
            self.assertNotEqual(observed["network"], "0")


if __name__ == "__main__":
    unittest.main(verbosity=2)
