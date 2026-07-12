#!/usr/bin/env python3
"""Fault tests for the Agent Workflow vNext crash-independent watchdog."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from artifact_store import create_once_json
from process_supervisor import SupervisorFailure, _canonical, _digest, _worker_entry, launch, process_identity, reconcile, supervise
from unittest import mock


class ProcessSupervisorTests(unittest.TestCase):
    def test_parallel_tasks_use_distinct_release_fences(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            worker = self.worker(root)
            refs = [
                self.request(root, worker, delay_seconds=0.1, deadline_seconds=3, task_id=task_id)
                for task_id in ("task-1", "task-2")
            ]
            receipts: dict[str, dict[str, object]] = {}
            threads = [
                threading.Thread(
                    target=lambda ref=ref: receipts.__setitem__(ref, supervise(root, ref))
                )
                for ref in refs
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=4)
                self.assertFalse(thread.is_alive())
            self.assertEqual({receipt["status"] for receipt in receipts.values()}, {"completed"})
            self.assertTrue((root / "runtime/watchdogs/001-research/task-1/release.json").is_file())
            self.assertTrue((root / "runtime/watchdogs/001-research/task-2/release.json").is_file())

    def test_same_task_id_in_two_phases_uses_distinct_attempt_namespaces(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            worker = self.worker(root)
            first = self.request(
                root,
                worker,
                delay_seconds=0.01,
                deadline_seconds=2,
                phase_id="001-research",
                task_id="shared-task",
            )
            second = self.request(
                root,
                worker,
                delay_seconds=0.01,
                deadline_seconds=2,
                phase_id="002-recover",
                task_id="shared-task",
            )
            self.assertEqual(supervise(root, first)["status"], "completed")
            self.assertEqual(supervise(root, second)["status"], "completed")
            self.assertTrue(
                (root / "runtime/processes/001-research/shared-task.json").is_file()
            )
            self.assertTrue(
                (root / "runtime/processes/002-recover/shared-task.json").is_file()
            )

    def test_stale_boot_identity_rejects_launch(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            ref = self.request(
                root,
                self.worker(root),
                delay_seconds=1,
                deadline_seconds=2,
                boot_identity="different-boot",
            )
            with self.assertRaisesRegex(SupervisorFailure, "another host boot"):
                supervise(root, ref)

    def test_monotonic_deadline_wins_over_future_wall_clock(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            ref = self.request(
                root,
                self.worker(root),
                delay_seconds=5,
                deadline_seconds=0.2,
                wall_deadline_seconds=3600,
                ignore_term=True,
            )
            started = time.monotonic()
            receipt = supervise(root, ref)
            self.assertEqual(receipt["status"], "timed_out")
            self.assertLess(time.monotonic() - started, 2)

    def test_worker_release_fence_rechecks_cancel_before_exec(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            ref = self.request(root, self.worker(root), delay_seconds=1, deadline_seconds=3)
            request = json.loads((root / ref).read_text())
            create_once_json(root, "runtime/watchdogs/001-research/task-1/release.json", {"released": True})
            create_once_json(root, "amendments/cancel.json", {"authority_revision": 1})
            with mock.patch("process_supervisor.os.execvpe") as execvpe:
                self.assertEqual(_worker_entry(root, ref, request["audit_marker"]), 125)
                execvpe.assert_not_called()

    def request(
        self,
        root: Path,
        worker: Path,
        *,
        delay_seconds: float,
        deadline_seconds: float,
        ignore_term: bool = False,
        phase_id: str = "001-research",
        wall_deadline_seconds: float | None = None,
        boot_identity: str | None = None,
        task_id: str = "task-1",
    ) -> str:
        marker = f"agent-workflow:fixture-workflow:{phase_id}:{task_id}:watchdog-test-{root.name}"
        python = Path(sys.executable).resolve(strict=True)
        command = [
            os.fspath(python),
            os.fspath(worker),
            str(delay_seconds),
            "1" if ignore_term else "0",
            "-c",
            f'agent_workflow_audit_marker="{marker}"',
        ]
        claim_ref = "generations/claims/fixture.json"
        claim_path = root / claim_ref
        if not claim_path.exists():
            claim_path = create_once_json(
                root,
                claim_ref,
                {"schema_version": "agent-workflow.generation-claim.vnext.v1", "generation_id": "generation-001"},
            )
        request = {
            "schema_version": "agent-workflow.supervisor-request.vnext.v2",
            "workflow_id": "fixture-workflow",
            "authority_revision": 1,
            "generation_id": "generation-001",
            "phase_id": phase_id,
            "task_id": task_id,
            "plan_sha256": "sha256:" + "1" * 64,
            "generation_claim_ref": claim_ref,
            "generation_claim_sha256": _digest(claim_path.read_bytes()),
            "runtime_bundle_sha256": "sha256:" + "2" * 64,
            "codex_binary": os.fspath(python),
            "codex_binary_sha256": _digest(python.read_bytes()),
            "transport_executable_sha256": _digest(Path(command[0]).read_bytes()),
            "transport_adapter_sha256": None,
            "command": command,
            "command_sha256": _digest(_canonical(command)),
            "cwd": os.fspath(root),
            "work_mode": "read",
            "write_roots": [],
            "environment": {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": os.fspath(root),
                "TMPDIR": os.fspath(root),
                "LANG": "en_US.UTF-8",
            },
            "audit_marker": marker,
            "deadline_at": (
                datetime.now(timezone.utc)
                + timedelta(
                    seconds=(
                        wall_deadline_seconds
                        if wall_deadline_seconds is not None
                        else deadline_seconds
                    )
                )
            ).isoformat().replace("+00:00", "Z"),
            "deadline_monotonic": time.monotonic() + deadline_seconds,
            "boot_identity": boot_identity if boot_identity is not None else process_identity(1),
            "terminate_grace_seconds": 0.1,
            "log_limit_bytes": 4096,
            "stdout_ref": f"transient/{phase_id}/{task_id}/stdout.jsonl",
            "stderr_ref": f"transient/{phase_id}/{task_id}/stderr.log",
            "receipt_ref": f"runtime/watchdogs/{phase_id}/{task_id}/terminal.json",
        }
        ref = f"runtime/watchdogs/{phase_id}/{task_id}/request.json"
        create_once_json(root, ref, request)
        return ref

    def worker(self, root: Path) -> Path:
        path = root / "worker.py"
        path.write_text(
            """import json, signal, sys, time
delay = float(sys.argv[1])
if sys.argv[2] == '1':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
print(json.dumps({'type': 'thread.started', 'thread_id': 'watchdog-worker'}), flush=True)
time.sleep(delay)
print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 1, 'output_tokens': 1}}), flush=True)
"""
        )
        return path

    def wait_for(self, path: Path, seconds: float = 4.0) -> None:
        deadline = time.monotonic() + seconds
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(path.exists(), f"timed out waiting for {path}")

    def test_deadline_terminates_then_kills_and_reaps_owned_group(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            ref = self.request(
                root,
                self.worker(root),
                delay_seconds=10,
                deadline_seconds=0.25,
                ignore_term=True,
            )
            receipt = supervise(root, ref)
            self.assertEqual(receipt["status"], "timed_out")
            self.assertTrue(receipt["term_sent"])
            self.assertTrue(receipt["kill_sent"])
            self.assertTrue((root / "runtime/processes/001-research/task-1.json").is_file())
            self.assertTrue((root / receipt["stdout_ref"]).is_file())

    def test_watchdog_finishes_after_launching_runner_is_sigkilled(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            ref = self.request(
                root,
                self.worker(root),
                delay_seconds=0.35,
                deadline_seconds=3,
            )
            pid_path = root / "watchdog.pid"
            helper = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    (
                        "import pathlib,sys,time;"
                        f"sys.path.insert(0,{str(SCRIPT_DIR)!r});"
                        "from process_supervisor import launch;"
                        f"p=launch(pathlib.Path({str(root)!r}),{ref!r});"
                        f"target=pathlib.Path({str(pid_path)!r});"
                        "tmp=target.with_suffix('.tmp');"
                        "tmp.write_text(str(p.pid));"
                        "tmp.replace(target);"
                        "time.sleep(30)"
                    ),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                self.wait_for(pid_path)
                watchdog_pid = int(pid_path.read_text())
                self.assertIsNotNone(process_identity(watchdog_pid))
                os.killpg(helper.pid, signal.SIGKILL)
                helper.wait(timeout=2)
                receipt_path = root / "runtime/watchdogs/001-research/task-1/terminal.json"
                self.wait_for(receipt_path)
                receipt = json.loads(receipt_path.read_text())
                self.assertEqual(receipt["status"], "completed")
                deadline = time.monotonic() + 2
                while process_identity(watchdog_pid) is not None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertIsNone(process_identity(watchdog_pid))
                self.assertTrue((root / "runtime/processes/001-research/task-1.json").is_file())
            finally:
                if helper.poll() is None:
                    os.killpg(helper.pid, signal.SIGKILL)
                    helper.wait(timeout=2)

    def test_reconcile_seals_terminal_when_watchdog_is_sigkilled(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            ref = self.request(
                root,
                self.worker(root),
                delay_seconds=30,
                deadline_seconds=5,
                ignore_term=True,
            )
            watchdog = launch(root, ref)
            active_path = root / "runtime/processes/001-research/task-1.json"
            self.wait_for(active_path)
            active = json.loads(active_path.read_text())
            os.killpg(watchdog.pid, signal.SIGKILL)
            watchdog.wait(timeout=2)
            summary = reconcile(root, grace_seconds=0.1)
            self.assertEqual(summary["reconciled"], ["task-1"])
            receipt = json.loads(
                (root / "runtime/watchdogs/001-research/task-1/terminal.json").read_text()
            )
            self.assertEqual(receipt["producer"], "reconciler")
            self.assertEqual(receipt["reconcile_reason"], "watchdog_lost")
            deadline = time.monotonic() + 2
            while process_identity(active["pid"]) is not None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIsNone(process_identity(active["pid"]))

    def test_reconcile_rejects_corrupt_existing_terminal_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            ref = self.request(
                root,
                self.worker(root),
                delay_seconds=0.01,
                deadline_seconds=2,
            )
            receipt = supervise(root, ref)
            terminal = root / "runtime/watchdogs/001-research/task-1/terminal.json"
            terminal.unlink()
            terminal.write_text("{}\n")

            with self.assertRaisesRegex(SupervisorFailure, "terminal receipt"):
                reconcile(root, grace_seconds=0.0)

    def test_reconcile_rejects_terminal_receipt_that_contradicts_live_worker(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            ref = self.request(
                root,
                self.worker(root),
                delay_seconds=0.01,
                deadline_seconds=2,
            )
            supervise(root, ref)
            record = json.loads(
                (root / "runtime/processes/001-research/task-1.json").read_text()
            )

            with mock.patch(
                "process_supervisor.process_birth",
                return_value=record["process_birth"],
            ):
                with self.assertRaisesRegex(
                    SupervisorFailure,
                    "contradicts live process evidence",
                ):
                    reconcile(root, grace_seconds=0.0)

    def test_detached_marker_process_is_terminalized_as_escape(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            worker = root / "escape.py"
            child_pid_path = root / "escaped.pid"
            worker.write_text(
                """import pathlib, subprocess, sys
marker = sys.argv[-1]
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)', marker], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
pathlib.Path('escaped.pid').write_text(str(child.pid))
"""
            )
            ref = self.request(root, worker, delay_seconds=0, deadline_seconds=3)
            receipt = supervise(root, ref)
            self.assertEqual(receipt["status"], "escaped_process_detected")
            self.assertEqual(receipt["escape_scan"]["status"], "detected")
            escaped_pid = int(child_pid_path.read_text())
            deadline = time.monotonic() + 2
            while process_identity(escaped_pid) is not None and time.monotonic() < deadline:
                time.sleep(0.01)
            if process_identity(escaped_pid) is not None:
                try:
                    os.killpg(escaped_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            self.assertIsNone(process_identity(escaped_pid))


if __name__ == "__main__":
    unittest.main(verbosity=2)
