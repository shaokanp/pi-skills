#!/usr/bin/env python3
"""Slice 1 tests for the read-only routed Phase tracer."""

from __future__ import annotations

import hashlib
import io
import json
import os
import signal
import shutil
import subprocess
import sys
sys.dont_write_bytecode = True
import tempfile
import threading
import time
import unittest
from unittest import mock
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
FIXTURE_ROOT = SCRIPT_DIR.parent / "fixtures" / "vnext" / "protocol" / "valid"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from artifact_store import ArtifactError, create_once_json
import baseline_gate
import workflow_runtime
import host_validation
from source_workspace import (
    IntegrationConflict,
    SourceWriteError,
    attest_writer_permissions,
    load_isolated_phase,
    prepare_read_only_snapshot,
    prepare_isolated_phase,
    writer_profile_bytes,
)
from phase_protocol import ProtocolError, _validate_source_patch_replay, validate_contract
from recovery_runtime import (
    RecoveryError,
    causal_predecessor_sha256,
    prepare_phase_authority,
    seal_resume_brief,
)
from workflow_runtime import (
    _RUNTIME_BUNDLE_FILES,
    CodexExecConfig,
    HumanGateRequired,
    RawExecution,
    RuntimeFailure,
    _cleanup_codex_auth,
    _drain_stream,
    _attest_worker_permissions,
    _prepare_codex_home,
    _probe_host_capabilities_command,
    _probe_runtime_refs,
    _parse_jsonl,
    _process_identity,
    _repository_root_for,
    _resource_admission,
    _runtime_bundle_sha256,
    _resolve_pinned_runtime,
    _raw_from_supervisor_terminal,
    _seal_runtime_bundle,
    _seal_deadlines,
    _seal_generation_claim,
    _scrub_stale_codex_auth,
    _validate_output_schema_contract,
    _validate_typed_output,
    _validate_admission_inputs,
    _validate_host_capability_provenance,
    _validate_source_write_capability,
    cancel_run,
    codex_task_executor,
    main,
    rebuild_view,
    reconcile_run,
    run_read_only_phase,
)


def digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURE_ROOT / name).read_text())


def capability_receipt(
    statuses: dict[str, str], provenance: dict[str, object]
) -> dict[str, object]:
    return {
        "schema_version": "agent-workflow.capability-receipt.vnext.v3",
        "capabilities": statuses,
        "blocking_transport": {
            "host": "codex-desktop",
            "outer_tool": "functions.exec",
            "inner_tool": "exec_command",
            "outer_yield_ms": 30000,
            "inner_yield_ms": 30000,
            "maximum_blocking_window_ms": 30000,
            "early_exit_observed": True,
            "sparse_continuation_limit": 0,
        },
        "proofs": {
            "blocking_wait": {
                "terminal_json_observed": True,
                "cell_handle_rejected": True,
            },
            "read_only_containment": {
                "exact_profile_attested": True,
                "credential_denial_attested": True,
                "network_restricted": True,
            },
            "route_attestation": {
                "persisted_turn_context": True,
                "top_model": "gpt-5.6-sol",
                "worker_model": "gpt-5.6-terra",
                "reasoning_effort": "xhigh",
            },
            "sandbox_isolation": {"reason": "host_capability_unavailable"},
            "cancel_reap": {
                "owned_pgid_signal": True,
                "live_marker_required": True,
                "post_publish_cancel_fence": True,
            },
            "raw_session_audit": {
                "thread_id_bound": True,
                "terminal_event_required": True,
            },
            "accounting_evidence": {
                "terminal_usage_required": True,
                "confidence": "exact",
            },
            "generation_fence": {
                "create_once": True,
                "authority_revision_bound": True,
                "predecessor_bound": True,
                "plan_digest_bound": True,
            },
        },
        "provenance": provenance,
    }


class ReadOnlyTracerTests(unittest.TestCase):
    def test_workflow_admission_accepts_repository_root_read_scope(self) -> None:
        workflow = json.loads(
            (SCRIPT_DIR.parent / "fixtures/vnext/protocol/valid/workflow.json").read_bytes()
        )
        workflow["admission"]["relevant_roots"] = ["."]
        validate_contract("workflow", workflow)

    def test_host_capability_admission_revalidates_sealed_snapshot_not_live_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repository = Path(raw) / "repo"
            repository.mkdir(parents=True)
            subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
            subprocess.run(
                ["git", "config", "user.email", "fixture@example.com"],
                cwd=repository,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Fixture"], cwd=repository, check=True
            )
            (repository / "src").mkdir()
            target = repository / "src/value.txt"
            target.write_text("fixture\n")
            subprocess.run(["git", "add", "."], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repository, check=True)
            root = repository / ".workflow/run"

            expected = prepare_read_only_snapshot(
                root, repository, "000-host-capability-probe", (".",)
            )
            snapshot_ref = "runtime/read-snapshots/000-host-capability-probe/manifest.json"
            snapshot_path = root / snapshot_ref
            snapshot_evidence = {
                "evidence_ref": snapshot_ref,
                "evidence_sha256": digest(snapshot_path.read_bytes()),
            }
            observed = workflow_runtime._host_capability_worker_root(
                root, repository, (".",), snapshot_evidence, require_live_state=True
            )
            self.assertEqual(observed, expected)
            self.assertNotEqual(observed, repository)
            self.assertFalse((observed / ".git").exists())
            self.assertFalse((observed / ".workflow").exists())

            target.write_text("drifted\n")
            with self.assertRaisesRegex(RuntimeFailure, "snapshot failed closed"):
                workflow_runtime._host_capability_worker_root(
                    root,
                    repository,
                    (".",),
                    snapshot_evidence,
                    require_live_state=True,
                )
            self.assertEqual(
                workflow_runtime._host_capability_worker_root(
                    root,
                    repository,
                    (".",),
                    snapshot_evidence,
                    require_live_state=False,
                ),
                expected,
            )

            shutil.rmtree(root / "runtime/read-snapshots/000-host-capability-probe")
            with self.assertRaisesRegex(RuntimeFailure, "snapshot is missing"):
                workflow_runtime._host_capability_worker_root(
                    root,
                    repository,
                    (".",),
                    snapshot_evidence,
                    require_live_state=True,
                )

            prepare_read_only_snapshot(root, repository, "000-host-capability-probe", (".",))
            with self.assertRaisesRegex(RuntimeFailure, "snapshot manifest digest drifted"):
                workflow_runtime._host_capability_worker_root(
                    root,
                    repository,
                    (".",),
                    snapshot_evidence,
                    require_live_state=True,
                )

            replacement_ref = snapshot_ref
            replacement_evidence = {
                "evidence_ref": replacement_ref,
                "evidence_sha256": digest((root / replacement_ref).read_bytes()),
            }
            self.assertEqual(
                workflow_runtime._host_capability_worker_root(
                    root,
                    repository,
                    (".",),
                    replacement_evidence,
                    require_live_state=False,
                ),
                expected,
            )

    def test_admit_and_run_phase_choose_snapshot_freshness_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repository = Path(raw) / "repo"
            repository.mkdir(parents=True)
            subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
            root = repository / ".workflow/run"
            root.mkdir(parents=True)
            codex = Path(raw) / "codex"
            codex.write_text("fixture\n")
            identity = (codex, digest(codex.read_bytes()), "fixture")

            with (
                mock.patch("workflow_runtime._load_source", return_value={}),
                mock.patch("workflow_runtime.validate_contract"),
                mock.patch("workflow_runtime._codex_identity", return_value=identity),
                mock.patch(
                    "workflow_runtime._validate_admission_inputs",
                    side_effect=RuntimeFailure("stop after admission fence"),
                ) as validate,
            ):
                with self.assertRaisesRegex(RuntimeFailure, "stop after admission fence"):
                    workflow_runtime._admit_command(
                        root, repository, root / "workflow-source.json", os.fspath(codex)
                    )
            self.assertTrue(validate.call_args.kwargs["require_host_snapshot_live_state"])

            with (
                mock.patch("workflow_runtime._load_source", return_value={}),
                mock.patch("workflow_runtime._load_fixed_json", return_value={}),
                mock.patch("workflow_runtime._codex_identity", return_value=identity),
                mock.patch(
                    "workflow_runtime._validate_admission_inputs",
                    side_effect=RuntimeFailure("stop after phase fence"),
                ) as validate,
            ):
                with self.assertRaisesRegex(RuntimeFailure, "stop after phase fence"):
                    workflow_runtime._run_phase_command(
                        root,
                        repository,
                        root / "plan-source.json",
                        root / "auth.json",
                        os.fspath(codex),
                        1,
                    )
            self.assertFalse(validate.call_args.kwargs["require_host_snapshot_live_state"])

    def test_public_cli_exposes_fresh_host_capability_materializer(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            main(["probe-host-capabilities", "--help"])
        self.assertEqual(raised.exception.code, 0)

    def test_host_capability_receipt_replays_routes_permissions_tokens_and_denials(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            worker_root = root / "runtime/read-snapshots/000-host-capability-probe/checkout"
            worker_root.mkdir(parents=True)
            schema_path = root / "evidence" / "host-capability-probe" / "output-schema.json"
            schema_path.parent.mkdir(parents=True)
            schema_path.write_text(
                '{"additionalProperties":false,"properties":{"answer":{"const":"probe-ok","type":"string"}},"required":["answer"],"type":"object"}\n'
            )
            codex_binary = root / "codex-release" / "bin" / "codex"
            runtime_zsh = root / "codex-release" / "codex-resources" / "zsh" / "bin" / "zsh"
            codex_binary.parent.mkdir(parents=True)
            runtime_zsh.parent.mkdir(parents=True)
            codex_binary.write_text("fixture\n")
            runtime_zsh.write_text("fixture\n")
            artifacts: dict[str, tuple[str, str]] = {}

            def write(relative: str, value: object) -> tuple[str, str]:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                payload = (
                    value
                    if isinstance(value, bytes)
                    else (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
                )
                path.write_bytes(payload)
                result = (relative, digest(payload))
                artifacts[relative] = result
                return result

            sessions: dict[str, object] = {}
            for role, model in (("worker", "gpt-5.6-terra"), ("top", "gpt-5.6-sol")):
                session_id = f"{role}-session"
                codex_home = root / "evidence" / "host-capability-probe" / f"{role}-home"
                arg0 = codex_home / "tmp" / "arg0" / "codex-arg0fixture"
                arg0.parent.mkdir(parents=True)
                arg0.write_text("fixture\n")
                profile = {
                    "type": "managed",
                    "network": "restricted",
                    "file_system": {
                        "type": "restricted",
                        "entries": [
                            {"path": {"type": "special", "value": {"kind": "minimal"}}, "access": "read"},
                            {"path": {"type": "path", "path": os.fspath(worker_root)}, "access": "read"},
                            {"path": {"type": "path", "path": os.fspath(runtime_zsh)}, "access": "read"},
                            {"path": {"type": "path", "path": os.fspath(arg0)}, "access": "read"},
                        ],
                    },
                }
                context = {
                    "model": model,
                    "effort": "xhigh",
                    "session_id": session_id,
                    "workspace_roots": [os.fspath(worker_root)],
                    "sandbox_policy": {"type": "read-only"},
                    "permission_profile": profile,
                }
                events = b"".join(
                    (json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n").encode()
                    for item in (
                        {"type": "thread.started", "thread_id": session_id},
                        {"type": "item.completed", "item": {"type": "agent_message", "text": '{"answer":"probe-ok"}'}},
                        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}},
                    )
                )
                events_ref, events_sha = write(f"evidence/{role}-events.jsonl", events)
                context_ref, context_sha = write(f"evidence/{role}-context.json", context)
                output_ref, output_sha = write(f"evidence/{role}-output.json", {"answer": "probe-ok"})
                request_ref, request_sha = write(f"runtime/{role}-request.json", {"role": role})
                terminal_ref, terminal_sha = write(f"runtime/{role}-terminal.json", {"role": role})
                rollout = codex_home / "sessions" / "2026" / "07" / "13" / f"{session_id}.jsonl"
                rollout.parent.mkdir(parents=True)
                rollout.write_bytes(
                    b"".join(
                        (json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n").encode()
                        for item in (
                            {"type": "session_meta", "payload": {"id": session_id}},
                            {"type": "turn_context", "payload": context},
                        )
                    )
                )
                sessions[role] = {
                    "session_id": session_id,
                    "model": model,
                    "reasoning_effort": "xhigh",
                    "codex_home": os.fspath(codex_home),
                    "events_ref": events_ref,
                    "events_sha256": events_sha,
                    "turn_context_ref": context_ref,
                    "turn_context_sha256": context_sha,
                    "output_ref": output_ref,
                    "output_sha256": output_sha,
                    "supervisor_request_ref": request_ref,
                    "supervisor_request_sha256": request_sha,
                    "supervisor_terminal_ref": terminal_ref,
                    "supervisor_terminal_sha256": terminal_sha,
                    "rollout_path": os.fspath(rollout),
                    "rollout_bytes": rollout.stat().st_size,
                    "rollout_sha256": digest(rollout.read_bytes()),
                }

            denial_ref, denial_sha = write(
                "evidence/denials.json",
                {
                    "workspace_read_exit": 0,
                    "workspace_write_exit": 1,
                    "sibling_read_exit": 1,
                    "control_read_exit": 1,
                    "credential_read_exit": 1,
                    "network_exit": 1,
                },
            )
            names = [
                "test_cancel_rejects_record_without_live_unforgeable_marker",
                "test_cancel_signals_active_group_and_terminalizes_queued_task",
                "test_generation_claim_uses_one_predecessor_authority_contention_key",
                "test_log_drainer_caps_event_object_count_even_within_durable_limit",
                "test_runner_sigkill_reconcile_materializes_task_and_phase_receipt",
                "test_terminal_fence_runs_after_workers_and_before_results_or_receipt",
            ]
            tests_ref, tests_sha = write(
                "evidence/focused-tests.txt",
                ("\n".join(f"{name} ... ok" for name in names) + "\n\nOK\n").encode(),
            )
            snapshot_ref, snapshot_sha = write(
                "runtime/read-snapshots/000-host-capability-probe/manifest.json",
                {
                    "schema_version": "agent-workflow.read-snapshot.vnext.v1",
                    "phase_id": "000-host-capability-probe",
                    "checkout_ref": "runtime/read-snapshots/000-host-capability-probe/checkout",
                },
            )
            now = datetime.now(timezone.utc)
            receipt = {
                "schema_version": "agent-workflow.host-capability-receipt.v1",
                "observed_at": now.isoformat().replace("+00:00", "Z"),
                "producer": {
                    "name": "agent-workflow-host-capability-probe",
                    "runtime_bundle_sha256": _runtime_bundle_sha256(),
                    "codex_cli_version": "fixture",
                    "codex_binary_sha256": "sha256:" + "2" * 64,
                },
                "relevant_root": os.fspath(worker_root),
                "snapshot_manifest": {
                    "evidence_ref": snapshot_ref,
                    "evidence_sha256": snapshot_sha,
                },
                "execution": {
                    "started_at": now.isoformat().replace("+00:00", "Z"),
                    "finished_at": now.isoformat().replace("+00:00", "Z"),
                    "role_count": 2,
                    "terminal_count": 2,
                },
                "sessions": sessions,
                "deterministic_denials": {"evidence_ref": denial_ref, "evidence_sha256": denial_sha},
                "focused_tests": {"evidence_ref": tests_ref, "evidence_sha256": tests_sha},
                "capabilities": {
                    name: "unavailable" if name == "sandbox_isolation" else "pass"
                    for name in workflow_runtime.CAPABILITY_NAMES
                },
            }
            command_suffixes: dict[str, list[str]] = {"worker": [], "top": []}
            terminal_stdout_overrides: dict[str, str | None] = {"worker": None, "top": None}

            def load_request(_root: Path, ref: str, *, enforce_boot: bool = True):
                role = json.loads((root / ref).read_text())["role"]
                session = sessions[role]
                model = session["model"]
                codex_home = session["codex_home"]
                marker = f"agent-workflow:fixture:{role}"
                command = workflow_runtime._host_probe_command(
                    codex_binary=os.fspath(codex_binary),
                    model=model,
                    audit_marker=marker,
                    output_schema=schema_path,
                    worker_root=worker_root,
                ) + command_suffixes[role]
                request = {
                    "codex_binary": os.fspath(codex_binary),
                    "command": command,
                    "command_sha256": digest(
                        (json.dumps(command, sort_keys=True, separators=(",", ":")) + "\n").encode()
                    ),
                    "environment": {
                        "AGENT_WORKFLOW_AUDIT_MARKER": marker,
                        "CODEX_HOME": codex_home,
                        "HOME": os.fspath(Path(codex_home) / "home"),
                        "LANG": "C.UTF-8",
                        "PATH": "/usr/bin:/bin",
                        "TMPDIR": os.fspath(Path(codex_home) / "tmp"),
                    },
                    "audit_marker": marker,
                    "work_mode": "read",
                    "write_roots": [],
                    "runtime_bundle_sha256": _runtime_bundle_sha256(),
                    "codex_binary_sha256": "sha256:" + "2" * 64,
                    "stdout_ref": session["events_ref"],
                    "stderr_ref": session["events_ref"],
                }
                return request, (root / ref).read_bytes()

            def validate_terminal(value: dict[str, object]):
                role = value["role"]
                session = sessions[role]
                return {
                    "request_ref": session["supervisor_request_ref"],
                    "request_sha256": session["supervisor_request_sha256"],
                    "status": "completed",
                    "exit_code": 0,
                    "group_reaped": True,
                    "group_gone_observed": True,
                    "stdout_ref": session["events_ref"],
                    "stdout_sha256": terminal_stdout_overrides[role] or session["events_sha256"],
                    "stderr_ref": session["events_ref"],
                }

            def validate(value: dict[str, object]) -> None:
                with (
                    mock.patch("workflow_runtime.load_supervisor_request", side_effect=load_request),
                    mock.patch("workflow_runtime.validate_supervisor_receipt", side_effect=validate_terminal),
                ):
                    _validate_host_capability_provenance(
                        root,
                        value,
                        running_bundle=_runtime_bundle_sha256(),
                        codex_sha256="sha256:" + "2" * 64,
                        codex_version="fixture",
                        worker_root=worker_root,
                    )

            validate(receipt)
            tampered = deepcopy(receipt)
            tampered["execution"]["terminal_count"] = 1
            with self.assertRaisesRegex(RuntimeFailure, "blocking barrier"):
                validate(tampered)
            tampered = deepcopy(receipt)
            tampered["sessions"]["worker"]["model"] = "gpt-5.6-sol"
            with self.assertRaisesRegex(RuntimeFailure, "route attestation"):
                validate(tampered)
            stale = deepcopy(receipt)
            stale["observed_at"] = "2020-01-01T00:00:00Z"
            with self.assertRaisesRegex(RuntimeFailure, "stale"):
                validate(stale)
            invalid_root = deepcopy(receipt)
            invalid_root["relevant_root"] = None
            with self.assertRaisesRegex(RuntimeFailure, "relevant root drifted"):
                validate(invalid_root)
            command_suffixes["worker"] = ["--dangerously-extra"]
            with self.assertRaisesRegex(RuntimeFailure, "command is not exact"):
                validate(receipt)
            command_suffixes["worker"] = []
            terminal_stdout_overrides["worker"] = "sha256:" + "9" * 64
            with self.assertRaisesRegex(RuntimeFailure, "routed evidence"):
                validate(receipt)
            terminal_stdout_overrides["worker"] = None
            worker_rollout = Path(sessions["worker"]["rollout_path"])
            rollout_events = [json.loads(line) for line in worker_rollout.read_text().splitlines()]
            rollout_events[-1]["payload"]["model"] = "gpt-5.6-sol"
            worker_rollout.write_text(
                "".join(json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n" for item in rollout_events)
            )
            sessions["worker"]["rollout_bytes"] = worker_rollout.stat().st_size
            sessions["worker"]["rollout_sha256"] = digest(worker_rollout.read_bytes())
            with self.assertRaisesRegex(RuntimeFailure, "routed evidence"):
                validate(receipt)
            rollout_events[-1]["payload"]["model"] = "gpt-5.6-terra"
            worker_rollout.write_text(
                "".join(json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n" for item in rollout_events)
            )
            sessions["worker"]["rollout_bytes"] = worker_rollout.stat().st_size
            sessions["worker"]["rollout_sha256"] = digest(worker_rollout.read_bytes())
            worker_rollout = Path(sessions["worker"]["rollout_path"])
            worker_rollout.write_text(worker_rollout.read_text() + "{}\n")
            with self.assertRaisesRegex(RuntimeFailure, "canonical rollout"):
                validate(receipt)

    def test_host_capability_receipt_only_crash_recovers_summary_and_scrubs_auth(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repository = Path(raw) / "repo"
            repository.mkdir(parents=True)
            subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.email", "fixture@example.com"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.name", "Fixture"], cwd=repository, check=True)
            (repository / "src").mkdir()
            (repository / "src/value.txt").write_text("fixture\n")
            subprocess.run(["git", "add", "."], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repository, check=True)
            root = repository / ".workflow" / "run"
            (root / "evidence").mkdir(parents=True)
            auth_path = root / "runtime" / "codex-homes" / "host-capability-worker" / "auth.json"
            auth_path.parent.mkdir(parents=True)
            auth_path.write_text('{"tokens":{"access_token":"fixture"}}\n')
            auth_path.chmod(0o600)
            statuses = {
                name: "unavailable" if name == "sandbox_isolation" else "pass"
                for name in workflow_runtime.CAPABILITY_NAMES
            }
            receipt = {
                "schema_version": "agent-workflow.host-capability-receipt.v1",
                "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "producer": {},
                "relevant_root": os.fspath(repository / "src"),
                "execution": {},
                "sessions": {},
                "deterministic_denials": {},
                "focused_tests": {},
                "capabilities": statuses,
            }
            receipt_path = root / "evidence" / "host-capability-receipt.json"
            receipt_path.write_text(json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n")
            binary = Path(sys.executable).resolve()
            identity = (binary, digest(binary.read_bytes()), "codex fixture")
            with (
                mock.patch("workflow_runtime._codex_identity", return_value=identity),
                mock.patch("workflow_runtime._validate_host_capability_provenance"),
            ):
                result = _probe_host_capabilities_command(
                    root,
                    repository,
                    "src",
                    root / "unused-auth.json",
                    os.fspath(binary),
                )
                self.assertTrue(result["replayed"])
                self.assertFalse(auth_path.exists())
                summary_path = root / result["evidence_ref"]
                self.assertTrue(summary_path.is_file())
                summary = json.loads(summary_path.read_text())
                self.assertEqual(
                    result["capability_bindings"]["blocking_wait"]["evidence_ref"],
                    "evidence/host-capability-summary.json",
                )
                summary["capabilities"]["blocking_wait"]["status"] = "blocked"
                summary_path.write_text(
                    json.dumps(summary, sort_keys=True, separators=(",", ":")) + "\n"
                )
                with self.assertRaisesRegex(RuntimeFailure, "summary drifted"):
                    _probe_host_capabilities_command(
                        root,
                        repository,
                        "src",
                        root / "unused-auth.json",
                        os.fspath(binary),
                    )

    def test_host_capability_pre_receipt_crash_uses_one_recovery_and_replays_exact_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            request = (
                root
                / "runtime/watchdogs/000-host-capability-probe"
                / "host-capability-worker/request.json"
            )
            request.parent.mkdir(parents=True)
            request.write_text("{}\n")
            task_id, observed = workflow_runtime._select_host_probe_attempt(root, "worker")
            self.assertEqual(task_id, "host-capability-worker-recovery")
            self.assertIsNone(observed)

            recovery_request = request.parent.parent / "host-capability-worker-recovery/request.json"
            recovery_request.parent.mkdir(parents=True)
            recovery_request.write_text("{}\n")
            with self.assertRaisesRegex(RuntimeFailure, "partial attempt"):
                workflow_runtime._select_host_probe_attempt(root, "worker")

            relative = "evidence/host-capability-probe/worker-events.jsonl"
            payload = b'{"type":"thread.started","thread_id":"worker"}\n'
            first = workflow_runtime._create_once_or_verify_bytes(root, relative, payload)
            second = workflow_runtime._create_once_or_verify_bytes(root, relative, payload)
            self.assertEqual(first, second)
            first.write_bytes(payload + b"{}\n")
            with self.assertRaisesRegex(RuntimeFailure, "replayed artifact drifted"):
                workflow_runtime._create_once_or_verify_bytes(root, relative, payload)

    def test_host_capability_full_command_reenters_after_pre_receipt_crash(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repository = Path(raw).resolve() / "repo"
            repository.mkdir(parents=True)
            subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.email", "fixture@example.com"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.name", "Fixture"], cwd=repository, check=True)
            (repository / "src").mkdir()
            (repository / "src/value.txt").write_text("fixture\n")
            subprocess.run(["git", "add", "."], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repository, check=True)
            root = repository / ".workflow" / "run"
            binary = Path(raw) / "release/bin/codex"
            runtime_zsh = Path(raw) / "release/codex-resources/zsh/bin/zsh"
            binary.parent.mkdir(parents=True)
            runtime_zsh.parent.mkdir(parents=True)
            binary.write_text("fixture\n")
            runtime_zsh.write_text("fixture\n")
            identity = (binary, digest(binary.read_bytes()), "codex fixture")
            executions: dict[str, RawExecution] = {}
            executor_calls: list[str] = []

            def prepare(_root: Path, _auth: Path, owner_id: str) -> Path:
                home = root / "runtime/codex-homes" / owner_id
                (home / "tmp/arg0/codex-arg0fixture").mkdir(parents=True, exist_ok=True)
                return home

            def fake_executor(config: CodexExecConfig):
                def execute(task: dict[str, object], _packet: dict[str, object]) -> RawExecution:
                    role = str(task["role"])
                    executor_calls.append(role)
                    self.assertTrue((config.repo_root / "src/value.txt").is_file())
                    self.assertFalse((config.repo_root / ".git").exists())
                    self.assertFalse((config.repo_root / ".workflow").exists())
                    self.assertFalse((root / "auth.json").is_relative_to(config.repo_root))
                    session_id = f"{role}-session"
                    context = {
                        "model": "gpt-5.6-terra" if role == "worker" else "gpt-5.6-sol",
                        "effort": "xhigh",
                        "session_id": session_id,
                        "workspace_roots": [os.fspath(config.repo_root)],
                        "sandbox_policy": {"type": "read-only"},
                        "permission_profile": {
                            "type": "managed",
                            "network": "restricted",
                            "file_system": {
                                "type": "restricted",
                                "entries": [
                                    {"path": {"type": "special", "value": {"kind": "minimal"}}, "access": "read"},
                                    {"path": {"type": "path", "path": os.fspath(config.repo_root)}, "access": "read"},
                                    {"path": {"type": "path", "path": os.fspath(runtime_zsh)}, "access": "read"},
                                    {"path": {"type": "path", "path": os.fspath(config.codex_home / "tmp/arg0/codex-arg0fixture")}, "access": "read"},
                                ],
                            },
                        },
                    }
                    events = [
                        {"type": "thread.started", "thread_id": session_id},
                        {"type": "item.completed", "item": {"type": "agent_message", "text": '{"answer":"probe-ok"}'}},
                        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}},
                    ]
                    payload = b"".join(workflow_runtime._canonical(item) for item in events)
                    rollout = config.codex_home / "sessions/2026/07/13" / f"{session_id}.jsonl"
                    rollout.parent.mkdir(parents=True, exist_ok=True)
                    rollout.write_bytes(
                        workflow_runtime._canonical({"type": "session_meta", "payload": {"id": session_id}})
                        + workflow_runtime._canonical({"type": "turn_context", "payload": context})
                    )
                    base = root / "runtime/watchdogs/000-host-capability-probe" / str(task["task_id"])
                    base.mkdir(parents=True, exist_ok=True)
                    (base / "request.json").write_text("{}\n")
                    (base / "terminal.json").write_text("{}\n")
                    result = RawExecution(0, events, "", context, stdout_bytes=payload)
                    executions[role] = result
                    return result

                return execute

            original_publish = workflow_runtime._create_once_or_verify_bytes
            fail_once = {"armed": True}

            def crash_before_receipt(run_root: Path, relative: str, payload: bytes) -> Path:
                if relative.endswith("focused-tests.txt") and fail_once["armed"]:
                    fail_once["armed"] = False
                    raise RuntimeFailure("injected pre-receipt crash")
                return original_publish(run_root, relative, payload)

            denials = {
                "workspace_read_exit": 0,
                "workspace_write_exit": 1,
                "sibling_read_exit": 1,
                "control_read_exit": 1,
                "credential_read_exit": 1,
                "network_exit": 1,
            }
            patches = (
                mock.patch("workflow_runtime._codex_identity", return_value=identity),
                mock.patch("workflow_runtime._prepare_codex_home", side_effect=prepare),
                mock.patch("workflow_runtime.codex_task_executor", side_effect=fake_executor),
                mock.patch("workflow_runtime.reconcile_supervisors", return_value={"active": []}),
                mock.patch("workflow_runtime._run_host_read_only_denials", return_value=denials),
                mock.patch("workflow_runtime._run_host_contract_tests", return_value=b"focused ... ok\n\nOK\n"),
                mock.patch("workflow_runtime._validate_host_capability_provenance"),
                mock.patch("workflow_runtime._cleanup_codex_auth"),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7]:
                with mock.patch(
                    "workflow_runtime._create_once_or_verify_bytes",
                    side_effect=crash_before_receipt,
                ):
                    with self.assertRaisesRegex(RuntimeFailure, "injected pre-receipt crash"):
                        _probe_host_capabilities_command(
                            root, repository, ".", root / "auth.json", os.fspath(binary)
                        )
                self.assertFalse((root / "evidence/host-capability-receipt.json").exists())
                with mock.patch(
                    "workflow_runtime._raw_from_supervisor_terminal",
                    side_effect=lambda _root, request_ref, _terminal_ref: executions[
                        "worker" if "worker" in request_ref else "top"
                    ],
                ):
                    result = _probe_host_capabilities_command(
                        root, repository, ".", root / "auth.json", os.fspath(binary)
                    )
            self.assertEqual(result["status"], "pass")
            self.assertFalse(result["replayed"])
            self.assertEqual(sorted(executor_calls), ["top", "worker"])

    def test_source_write_probe_uses_phase_namespaced_supervisor_refs(self) -> None:
        self.assertEqual(
            _probe_runtime_refs("000-source-write-probe", "source-write-probe"),
            {
                "request_ref": "evidence/source-write-probe/runtime/watchdogs/000-source-write-probe/source-write-probe/request.json",
                "terminal_ref": "evidence/source-write-probe/runtime/watchdogs/000-source-write-probe/source-write-probe/terminal.json",
                "events_ref": "evidence/source-write-probe/transient/000-source-write-probe/source-write-probe/stdout.jsonl",
                "stderr_ref": "evidence/source-write-probe/transient/000-source-write-probe/source-write-probe/stderr.log",
            },
        )

    def test_source_write_probe_installs_a_real_launch_fence(self) -> None:
        class ProbeCaptured(RuntimeError):
            pass

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            binary = root / "codex"
            binary.write_text("fixture\n")
            binary.chmod(0o755)
            auth = root / "auth.json"
            auth.write_text(json.dumps({"OPENAI_API_KEY": "fixture"}) + "\n")

            def fake_executor(_config: CodexExecConfig):
                def execute(task: dict[str, object], _packet: dict[str, object]) -> RawExecution:
                    fence = task.get("_runtime_source_launch_fence")
                    self.assertTrue(callable(fence))
                    fence()
                    workspace = Path(task["_runtime_worker_root"])
                    (workspace / "unexpected.txt").write_text("drift\n")
                    with self.assertRaisesRegex(RuntimeFailure, "probe workspace drifted"):
                        fence()
                    (workspace / "unexpected.txt").unlink()
                    (workspace / "escape").symlink_to(root)
                    with self.assertRaisesRegex(RuntimeFailure, "probe workspace drifted"):
                        fence()
                    (workspace / "escape").unlink()
                    outside = root / "outside.txt"
                    outside.write_text("outside\n")
                    os.link(outside, workspace / "hardlink.txt")
                    with self.assertRaisesRegex(RuntimeFailure, "probe workspace drifted"):
                        fence()
                    (workspace / "hardlink.txt").unlink()
                    os.mkfifo(workspace / "fifo")
                    with self.assertRaisesRegex(RuntimeFailure, "probe workspace drifted"):
                        fence()
                    raise ProbeCaptured("launch fence observed")

                return execute

            with (
                mock.patch(
                    "workflow_runtime._codex_identity",
                    return_value=(binary, digest(binary.read_bytes()), "fixture-cli"),
                ),
                mock.patch("workflow_runtime.codex_task_executor", side_effect=fake_executor),
                self.assertRaisesRegex(ProbeCaptured, "launch fence observed"),
            ):
                workflow_runtime._probe_source_write_command(root, auth, os.fspath(binary))

    def test_source_write_probe_launch_fence_rejects_root_and_ancestor_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            probe_root = root / "evidence/source-write-probe"
            workspace = probe_root / "workspace"
            (workspace / "src/api").mkdir(parents=True)
            (workspace / ".git").mkdir()
            fence = workflow_runtime._seal_source_write_probe_launch_fence(workspace)

            saved_workspace = probe_root / "workspace.saved"
            workspace.rename(saved_workspace)
            with self.assertRaisesRegex(RuntimeFailure, "probe workspace drifted"):
                fence()
            workspace.symlink_to(root)
            with self.assertRaisesRegex(RuntimeFailure, "probe workspace drifted"):
                fence()
            workspace.unlink()
            os.mkfifo(workspace)
            with self.assertRaisesRegex(RuntimeFailure, "probe workspace drifted"):
                fence()
            workspace.unlink()
            saved_workspace.rename(workspace)

            saved_probe_root = root / "source-write-probe.saved"
            probe_root.rename(saved_probe_root)
            probe_root.symlink_to(saved_probe_root)
            with self.assertRaisesRegex(RuntimeFailure, "probe workspace drifted"):
                fence()
            probe_root.unlink()
            probe_root.mkdir()
            (saved_probe_root / "workspace").rename(workspace)
            with self.assertRaisesRegex(RuntimeFailure, "probe workspace drifted"):
                fence()

    def test_source_write_probe_launch_fence_wraps_initial_descriptor_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw).resolve()
            with (
                mock.patch("workflow_runtime.os.open", side_effect=OSError("EMFILE")),
                self.assertRaisesRegex(RuntimeFailure, "workspace root cannot be opened"),
            ):
                workflow_runtime._seal_source_write_probe_launch_fence(workspace)

    def test_source_write_probe_rejects_preexisting_workspace_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            probe_root = root / "evidence/source-write-probe"
            probe_root.mkdir(parents=True)
            outside = root / "outside"
            (outside / "src/api").mkdir(parents=True)
            (probe_root / "workspace").symlink_to(outside)
            binary = root / "codex"
            binary.write_text("fixture\n")
            binary.chmod(0o755)
            auth = root / "auth.json"
            auth.write_text(json.dumps({"OPENAI_API_KEY": "fixture"}) + "\n")
            with (
                mock.patch(
                    "workflow_runtime._codex_identity",
                    return_value=(binary, digest(binary.read_bytes()), "fixture-cli"),
                ),
                mock.patch("workflow_runtime.codex_task_executor") as executor,
                self.assertRaisesRegex(RuntimeFailure, "workspace is unsafe"),
            ):
                workflow_runtime._probe_source_write_command(root, auth, os.fspath(binary))
            executor.assert_not_called()

    def test_source_write_probe_rejects_preexisting_workspace_content(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            workspace = root / "evidence/source-write-probe/workspace"
            (workspace / "src/api").mkdir(parents=True)
            (workspace / ".git").mkdir()
            (workspace / "stale-secret.txt").write_text("must not be exposed\n")
            binary = root / "codex"
            binary.write_text("fixture\n")
            binary.chmod(0o755)
            auth = root / "auth.json"
            auth.write_text(json.dumps({"OPENAI_API_KEY": "fixture"}) + "\n")
            with (
                mock.patch(
                    "workflow_runtime._codex_identity",
                    return_value=(binary, digest(binary.read_bytes()), "fixture-cli"),
                ),
                mock.patch("workflow_runtime.codex_task_executor") as executor,
                self.assertRaisesRegex(RuntimeFailure, "empty synthetic layout"),
            ):
                workflow_runtime._probe_source_write_command(root, auth, os.fspath(binary))
            executor.assert_not_called()

    def test_source_write_launch_fence_failure_prevents_watchdog_launch(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            schema = root / "schemas/output.json"
            schema.parent.mkdir()
            schema.write_text(
                json.dumps(
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["answer"],
                        "properties": {"answer": {"type": "string"}},
                    }
                )
                + "\n"
            )
            binary = root / "codex"
            binary.write_text("fixture\n")
            binary.chmod(0o755)
            task = {
                **self.direct_runtime_fence(root),
                "task_id": "fenced-writer",
                "role": "worker",
                "work_mode": "write",
                "write_roots": ["src/api"],
                "execution_deadline_seconds": 5,
                "_runtime_source_launch_fence": mock.Mock(
                    side_effect=RuntimeFailure("probe workspace drifted before actor launch")
                ),
            }
            execute = codex_task_executor(
                CodexExecConfig(
                    run_root=root,
                    repo_root=root,
                    codex_home=root / "codex-home",
                    codex_binary=os.fspath(binary),
                )
            )
            with (
                mock.patch("workflow_runtime.launch_supervisor") as launch,
                self.assertRaisesRegex(RuntimeFailure, "probe workspace drifted"),
            ):
                execute(
                    task,
                    {"output_schema_ref": "schemas/output.json", "prompt": "bounded"},
                )
            launch.assert_not_called()

    def test_direct_source_write_actor_without_launch_fence_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            execute = codex_task_executor(
                CodexExecConfig(
                    run_root=root,
                    repo_root=root,
                    codex_home=root / "codex-home",
                    codex_binary=os.fspath(root / "not-reached-codex"),
                )
            )
            with self.assertRaisesRegex(
                RuntimeFailure, "source task lacks its launch-time dependency fence"
            ):
                execute(
                    {
                        "task_id": "unfenced-writer",
                        "role": "worker",
                        "work_mode": "write",
                        "write_roots": ["src/api"],
                        "execution_deadline_seconds": 5,
                    },
                    {"output_schema_ref": "schemas/output.json", "prompt": "bounded"},
                )

    def test_cancel_at_terminal_fence_cannot_publish_completed_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plan = self.build_run(root, task_count=1)

            def completed(task: dict[str, object], _packet: dict[str, object]) -> RawExecution:
                session = f"terminal-{task['task_id']}"
                return RawExecution(
                    0,
                    [
                        {"type": "thread.started", "thread_id": session},
                        {"type": "item.completed", "item": {"type": "agent_message", "text": '{"answer":"ok"}'}},
                        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
                    ],
                    "",
                    {"model": "gpt-5.6-terra", "effort": "xhigh", "session_id": session},
                )

            with self.assertRaisesRegex(RuntimeFailure, "cancel fence"):
                run_read_only_phase(
                    root,
                    plan,
                    completed,
                    max_parallel=1,
                    terminal_fence=lambda: cancel_run(root, 1, grace_seconds=0),
                )
            self.assertFalse((root / "phases/001-research/receipt.json").exists())
            self.assertFalse((root / "phases/001-research/tasks/research-1/result.json").exists())

    def direct_runtime_fence(self, root: Path) -> dict[str, object]:
        claim_ref = "generations/claims/direct-fixture.json"
        claim_path = create_once_json(
            root,
            claim_ref,
            {"schema_version": "agent-workflow.generation-claim.vnext.v1", "generation_id": "generation-001"},
        )
        return {
            "_runtime_plan_sha256": "sha256:" + "1" * 64,
            "_runtime_generation_claim_ref": claim_ref,
            "_runtime_generation_claim_sha256": digest(claim_path.read_bytes()),
            "_runtime_bundle_sha256": _runtime_bundle_sha256(),
            "_runtime_boot_identity": _process_identity(1),
        }

    def test_materialized_jsonl_event_count_fails_closed(self) -> None:
        payload = b'{"type":"item"}\n' * 4097
        with self.assertRaisesRegex(RuntimeFailure, "event-count cap"):
            _parse_jsonl(payload)

    def test_resource_admission_fails_closed_on_low_fd_or_disk(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plan = self.build_run(root, task_count=1)
            workflow = json.loads((root / "workflow.json").read_text())
            with mock.patch("workflow_runtime.resource.getrlimit", return_value=(32, 32)):
                with self.assertRaisesRegex(RuntimeFailure, "file-descriptor"):
                    _resource_admission(root, workflow, 2, log_limit_bytes=4096)
            disk = mock.Mock(free=1024)
            with (
                mock.patch("workflow_runtime.resource.getrlimit", return_value=(1024, 1024)),
                mock.patch("workflow_runtime.shutil.disk_usage", return_value=disk),
            ):
                with self.assertRaisesRegex(RuntimeFailure, "disk floor"):
                    _resource_admission(root, workflow, 2, log_limit_bytes=4096)

    def test_stale_auth_scrub_requires_safe_file_and_no_live_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            auth = root / "runtime/codex-home/auth.json"
            auth.parent.mkdir(parents=True)
            auth.write_text('{"tokens":{"access_token":"stale"}}\n')
            auth.chmod(0o600)
            self.assertTrue(_scrub_stale_codex_auth(root))
            self.assertFalse(auth.exists())
            auth.write_text('{"tokens":{"access_token":"unsafe"}}\n')
            auth.chmod(0o644)
            with self.assertRaisesRegex(RuntimeFailure, "permissions are unsafe"):
                _scrub_stale_codex_auth(root)

    def test_deadline_seal_does_not_reset_on_resume(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plan = self.build_run(root, task_count=1)
            workflow = json.loads((root / "workflow.json").read_text())
            first, first_monotonic = _seal_deadlines(root, workflow, plan)
            time.sleep(0.02)
            second, second_monotonic = _seal_deadlines(root, workflow, plan)
            self.assertEqual(second, first)
            self.assertAlmostEqual(second_monotonic, first_monotonic, delta=0.1)
            workflow["created_at"] = "2000-01-01T00:00:00Z"
            with self.assertRaisesRegex(RuntimeFailure, "deadline seal drifted"):
                _seal_deadlines(root, workflow, plan)

    def test_generation_claim_uses_one_predecessor_authority_contention_key(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workflow = fixture("workflow.json")
            plan = fixture("phase-plan.json")
            payload = (json.dumps(plan, sort_keys=True, separators=(",", ":")) + "\n").encode()
            ref, path = _seal_generation_claim(root, workflow, plan, payload)
            self.assertTrue(ref.startswith("generations/claims/"))
            winner = json.loads(path.read_text())
            self.assertEqual(winner["generation_id"], plan["generation_id"])
            competing = deepcopy(plan)
            competing["generation_id"] = "generation-competitor"
            competing_payload = (json.dumps(competing, sort_keys=True, separators=(",", ":")) + "\n").encode()
            with self.assertRaisesRegex(RuntimeFailure, "contention lost"):
                _seal_generation_claim(root, workflow, competing, competing_payload)
            self.assertEqual(json.loads(path.read_text())["generation_id"], plan["generation_id"])

    def test_reconcile_cli_rebuilds_view_and_checks_authority(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plan = self.build_run(root, task_count=1)
            summary = reconcile_run(root, 1, grace_seconds=0.0)
            self.assertEqual(summary["status"], "reconciled")
            self.assertEqual(summary["attempt_count"], 0)
            view = json.loads((root / "view.json").read_text())
            self.assertEqual(view["workflow_id"], "fixture-workflow")
            self.assertEqual(rebuild_view(root), view)
            with self.assertRaisesRegex(RuntimeFailure, "authority revision"):
                reconcile_run(root, 2)

    def test_runner_sigkill_reconcile_materializes_task_and_phase_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plan = self.build_run(root, task_count=1)
            fake = root / "fake-reconcile-codex"
            fake.write_text(
                """#!/usr/bin/env python3
import json, os, pathlib, sys, time
args = sys.argv[1:]
model = args[args.index('-m') + 1]
thread_id = 'reconcile-thread'
home = pathlib.Path(os.environ['CODEX_HOME'])
session = home / 'sessions' / 'reconcile.jsonl'
session.parent.mkdir(parents=True, exist_ok=True)
context = {'model': model, 'effort': 'xhigh', 'workspace_roots': [str(pathlib.Path.cwd())], 'sandbox_policy': {'type': 'read-only'}, 'permission_profile': {'type': 'managed', 'file_system': {'type': 'restricted', 'entries': [{'path': {'type': 'special', 'value': {'kind': 'minimal'}}, 'access': 'read'}, {'path': {'type': 'path', 'path': str(pathlib.Path.cwd())}, 'access': 'read'}, {'path': {'type': 'path', 'path': str(home / 'tmp' / 'arg0' / 'codex-arg0fixture')}, 'access': 'read'}]}, 'network': 'restricted'}}
session.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': thread_id}}) + '\\n' + json.dumps({'type': 'turn_context', 'payload': context}) + '\\n')
time.sleep(0.4)
print(json.dumps({'type': 'thread.started', 'thread_id': thread_id}))
print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '{\"answer\":\"reconciled\"}'}}))
print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 3, 'output_tokens': 2}}))
"""
            )
            fake.chmod(0o755)
            helper_code = (
                "import json,pathlib,sys;"
                f"sys.path.insert(0,{str(SCRIPT_DIR)!r});"
                "from workflow_runtime import CodexExecConfig,codex_task_executor,run_read_only_phase;"
                f"root=pathlib.Path({str(root)!r});"
                "plan=json.loads((root/'plan-helper.json').read_text());"
                f"cfg=CodexExecConfig(run_root=root,repo_root=root,codex_home=root/'codex-home',codex_binary={str(fake)!r},workflow_id='fixture-workflow',authority_revision=1,terminate_grace_seconds=0.1);"
                "run_read_only_phase(root,plan,codex_task_executor(cfg),max_parallel=1)"
            )
            (root / "plan-helper.json").write_text(json.dumps(plan))
            helper = subprocess.Popen(
                [sys.executable, "-c", helper_code],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            active = root / "runtime/processes/001-research/research-1.json"
            deadline = time.monotonic() + 3
            while not active.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(active.exists())
            os.killpg(helper.pid, signal.SIGKILL)
            helper.wait(timeout=2)
            terminal = root / "runtime/watchdogs/001-research/research-1/terminal.json"
            deadline = time.monotonic() + 3
            while not terminal.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(terminal.exists())
            partial_events = root / "phases/001-research/tasks/research-1/attempts/001/events.jsonl"
            partial_events.parent.mkdir(parents=True, exist_ok=True)
            partial_events.write_bytes((root / "transient/001-research/research-1/stdout.jsonl").read_bytes())
            summary = reconcile_run(root, 1, grace_seconds=0.1)
            self.assertEqual(
                summary["materialized_phase_receipts"],
                ["phases/001-research/receipt.json"],
            )
            result = json.loads(
                (root / "phases/001-research/tasks/research-1/result.json").read_text()
            )
            self.assertEqual(
                result["status"],
                "completed",
                {"result": result, "terminal": json.loads(terminal.read_text()), "stderr": (root / "transient/001-research/research-1/stderr.log").read_text()},
            )
            receipt = json.loads((root / "phases/001-research/receipt.json").read_text())
            self.assertEqual(receipt["status"], "completed")

    def test_run_once_is_one_cli_transaction_and_one_terminal_json(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            workflow_source = root / "workflow-source.json"
            plan_source = root / "plan-source.json"
            auth_source = root / "auth.json"
            repo.mkdir()
            output = io.StringIO()
            admission = {
                "status": "admitted",
                "workflow_ref": "workflow.json",
                "workflow_sha256": "sha256:" + "1" * 64,
            }
            phase = {
                "status": "completed",
                "receipt_ref": "phases/001/receipt.json",
                "receipt_sha256": "sha256:" + "2" * 64,
            }
            with (
                mock.patch("workflow_runtime._admit_command", return_value=admission) as admit,
                mock.patch("workflow_runtime._run_phase_command", return_value=phase) as run,
                mock.patch("sys.stdout", output),
            ):
                exit_code = main(
                    [
                        "run-once",
                        "--root",
                        os.fspath(root),
                        "--repo",
                        os.fspath(repo),
                        "--workflow-source",
                        os.fspath(workflow_source),
                        "--plan-source",
                        os.fspath(plan_source),
                        "--auth-source",
                        os.fspath(auth_source),
                        "--codex-binary",
                        "/fixture/codex",
                        "--max-parallel",
                        "4",
                    ]
                )
            self.assertEqual(exit_code, 0)
            self.assertEqual(len(output.getvalue().splitlines()), 1)
            self.assertEqual(json.loads(output.getvalue())["admission"], admission)
            admit.assert_called_once_with(
                root, repo, workflow_source, "/fixture/codex"
            )
            run.assert_called_once_with(
                root, repo, plan_source, auth_source, "/fixture/codex", 4
            )

    def test_dirty_overlap_cli_returns_typed_human_gate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            output = io.StringIO()
            with (
                mock.patch(
                    "workflow_runtime._run_phase_command",
                    side_effect=HumanGateRequired("writer roots overlap existing dirty paths: src/a"),
                ),
                mock.patch("sys.stdout", output),
            ):
                exit_code = main(
                    [
                        "run-phase",
                        "--root",
                        os.fspath(root),
                        "--repo",
                        os.fspath(root),
                        "--plan-source",
                        os.fspath(root / "plan.json"),
                        "--auth-source",
                        os.fspath(root / "auth.json"),
                        "--max-parallel",
                        "1",
                    ]
                )
            self.assertEqual(exit_code, 3)
            self.assertEqual(json.loads(output.getvalue())["status"], "human_gate")

    def test_runtime_bundle_covers_all_executable_control_modules(self) -> None:
        self.assertEqual(
            set(_RUNTIME_BUNDLE_FILES),
            {
                "app_resume_adapter.py",
                "host_validation.py",
                "artifact_store.py",
                "baseline_gate.py",
                "phase_protocol.py",
                "process_supervisor.py",
                "recovery_runtime.py",
                "repository_state.py",
                "source_workspace.py",
                "test_vnext_runtime.py",
                "vnext_accounting.py",
                "workflow_runtime.py",
            },
        )
        self.assertRegex(_runtime_bundle_sha256(), r"^sha256:[0-9a-f]{64}$")

    def test_admission_bundle_pin_is_create_once_replayable_and_survives_selector_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            expected = _runtime_bundle_sha256()
            first = _seal_runtime_bundle(root)
            second = _seal_runtime_bundle(root)
            self.assertEqual(first, second)
            self.assertEqual(first, root / "runtime-bundle/workflow_runtime.py")
            self.assertEqual(
                {path.name for path in (root / "runtime-bundle").iterdir()},
                set(_RUNTIME_BUNDLE_FILES),
            )
            create_once_json(root, "workflow.json", {"runtime_bundle": {"sha256": expected}})
            with mock.patch("workflow_runtime._runtime_bundle_sha256", return_value="sha256:" + "f" * 64):
                self.assertEqual(_resolve_pinned_runtime(root, expected), first)
            observed = subprocess.run(
                [sys.executable, os.fspath(first), "pinned-runtime", "--root", os.fspath(root)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(observed.returncode, 0, observed.stderr)
            self.assertEqual(json.loads(observed.stdout)["runtime_bundle_sha256"], expected)
            self.assertEqual(
                {path.name for path in (root / "runtime-bundle").iterdir()},
                set(_RUNTIME_BUNDLE_FILES),
            )

    def test_direct_pinned_entry_points_never_materialize_bytecode(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            _seal_runtime_bundle(root)
            bundle_root = root / "runtime-bundle"
            for entry_point in (
                "workflow_runtime.py",
                "process_supervisor.py",
                "app_resume_adapter.py",
                "baseline_gate.py",
                "host_validation.py",
                "test_vnext_runtime.py",
            ):
                observed = subprocess.run(
                    [sys.executable, os.fspath(bundle_root / entry_point), "--help"],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertEqual(observed.returncode, 0, observed.stderr)
                self.assertEqual(
                    {path.name for path in bundle_root.iterdir()},
                    set(_RUNTIME_BUNDLE_FILES),
                    entry_point,
                )

    def test_host_validation_binds_integration_command_logs_and_repository_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repository = Path(raw).resolve() / "repo"
            repository.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.email", "fixture@example.com"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.name", "Fixture"], cwd=repository, check=True)
            (repository / "tracked.txt").write_text("stable\n")
            subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repository, check=True)
            subprocess.run(["git", "checkout", "-qb", "功能"], cwd=repository, check=True)
            root = repository / ".workflow/host-validation"
            receipt_ref = "phases/001-write/receipt.json"
            (root / "phases/001-write").mkdir(parents=True)
            workflow = {
                "workflow_id": "host-validation",
                "authority": {"revision": 1},
                "success_criteria": [],
            }
            (root / "workflow.json").write_text(json.dumps(workflow) + "\n")
            integration = fixture("phase-receipt.json")
            integration.update({
                "workflow_id": "host-validation",
                "phase_id": "001-write",
                "status": "completed",
                "terminal_reason": "all_tasks_terminal",
            })
            integration["integration"] = {
                "mode": "isolated_exact_base",
                "status": "applied",
                "patch_ref": "runtime/source-write/001-write/bounded-patch.json",
                "patch_sha256": "sha256:" + "a" * 64,
                "target_before": {"src": "sha256:" + "b" * 64},
                "target_after": {"src": "sha256:" + "c" * 64},
            }
            integration_payload = (json.dumps(integration, sort_keys=True) + "\n").encode()
            (root / receipt_ref).write_bytes(integration_payload)
            spec = {
                "schema_version": "agent-workflow.host-validation-spec.v1",
                "workflow_id": "host-validation",
                "authority_revision": 1,
                "validation_id": "focused",
                "integration_receipt_ref": receipt_ref,
                "integration_receipt_sha256": digest(integration_payload),
                "cwd": ".",
                "environment": {},
                "commands": [
                    {
                        "id": "python-smoke",
                        "argv": [sys.executable, "-c", "print('host-validation-功能')"],
                        "timeout_seconds": 30,
                    }
                ],
            }
            spec_path = root / "focused-spec.json"
            spec_path.write_text(json.dumps(spec) + "\n")
            invalid_cwd_spec = deepcopy(spec)
            invalid_cwd_spec["cwd"] = None
            invalid_cwd_path = root / "invalid-cwd-spec.json"
            invalid_cwd_path.write_text(json.dumps(invalid_cwd_spec) + "\n")
            invalid_output = io.StringIO()
            with mock.patch("sys.stdout", invalid_output):
                invalid_exit = host_validation.main([
                    "--root",
                    os.fspath(root),
                    "--repo",
                    os.fspath(repository),
                    "--spec-source",
                    os.fspath(invalid_cwd_path),
                ])
            self.assertEqual(invalid_exit, 2)
            self.assertEqual(json.loads(invalid_output.getvalue())["status"], "runtime_failed")
            result = host_validation.run_validation(root, repository, spec_path)
            self.assertEqual(result["status"], "pass")
            receipt = json.loads((root / result["receipt_ref"]).read_bytes())
            self.assertTrue(receipt["repository_unchanged"])
            self.assertEqual(receipt["commands"][0]["exit_code"], 0)
            self.assertEqual(
                (root / receipt["commands"][0]["stdout_ref"]).read_text(),
                "host-validation-功能\n",
            )
            self.assertTrue(receipt["commands"][0]["argv_sha256"].startswith("sha256:"))
            replayed = host_validation.run_validation(root, repository, spec_path)
            self.assertTrue(replayed["replayed"])

            changed_spec = deepcopy(spec)
            changed_spec["commands"][0]["argv"] = [sys.executable, "-c", "raise SystemExit(9)"]
            changed_path = root / "changed-spec.json"
            changed_path.write_text(json.dumps(changed_spec) + "\n")
            with self.assertRaisesRegex(host_validation.HostValidationError, "drifted"):
                host_validation.run_validation(root, repository, changed_path)

            prefix_spec = deepcopy(spec)
            prefix_spec["commands"].append({
                "id": "second-command",
                "argv": [sys.executable, "-c", "print('second')"],
                "timeout_seconds": 30,
            })
            prefix_path = root / "prefix-spec.json"
            prefix_path.write_text(json.dumps(prefix_spec) + "\n")
            prefix_receipt = deepcopy(receipt)
            prefix_receipt["spec_sha256"] = digest(prefix_path.read_bytes())
            receipt_path = root / result["receipt_ref"]
            original_receipt = receipt_path.read_bytes()
            receipt_path.write_text(
                json.dumps(prefix_receipt, sort_keys=True, separators=(",", ":")) + "\n"
            )
            with self.assertRaisesRegex(host_validation.HostValidationError, "omitted sealed commands"):
                host_validation.run_validation(root, repository, prefix_path)
            receipt_path.write_bytes(original_receipt)

            receipt_path.write_text('{"status":"pass"}\n')
            with self.assertRaisesRegex(host_validation.HostValidationError, "replay failed"):
                host_validation.run_validation(root, repository, spec_path)
            receipt_path.write_bytes(original_receipt)

            drift_spec = deepcopy(spec)
            drift_spec["validation_id"] = "drift"
            drift_spec["commands"][0]["argv"] = [
                sys.executable,
                "-c",
                "from pathlib import Path; Path('tracked.txt').write_text('drift\\n')",
            ]
            drift_path = root / "drift-spec.json"
            drift_path.write_text(json.dumps(drift_spec) + "\n")
            drift = host_validation.run_validation(root, repository, drift_path)
            self.assertEqual(drift["status"], "fail")
            drift_receipt = json.loads((root / drift["receipt_ref"]).read_bytes())
            self.assertFalse(drift_receipt["repository_unchanged"])

            (repository / "untracked.txt").write_text("before\n")
            untracked_spec = deepcopy(spec)
            untracked_spec["validation_id"] = "untracked-drift"
            untracked_spec["commands"][0]["argv"] = [
                sys.executable,
                "-c",
                "from pathlib import Path; Path('untracked.txt').write_text('after\\n')",
            ]
            untracked_path = root / "untracked-drift-spec.json"
            untracked_path.write_text(json.dumps(untracked_spec) + "\n")
            untracked = host_validation.run_validation(root, repository, untracked_path)
            self.assertEqual(untracked["status"], "fail")
            untracked_receipt = json.loads((root / untracked["receipt_ref"]).read_bytes())
            self.assertFalse(untracked_receipt["repository_unchanged"])

            mode_file = repository / "mode-only.txt"
            mode_file.write_text("same-bytes\n")
            mode_file.chmod(0o644)
            mode_spec = deepcopy(spec)
            mode_spec["validation_id"] = "untracked-mode-drift"
            mode_spec["commands"][0]["argv"] = [
                sys.executable,
                "-c",
                "from pathlib import Path; Path('mode-only.txt').chmod(0o600)",
            ]
            mode_path = root / "untracked-mode-drift-spec.json"
            mode_path.write_text(json.dumps(mode_spec) + "\n")
            mode_result = host_validation.run_validation(root, repository, mode_path)
            self.assertEqual(mode_result["status"], "fail")
            mode_receipt = json.loads((root / mode_result["receipt_ref"]).read_bytes())
            self.assertFalse(mode_receipt["repository_unchanged"])

            amended_spec = deepcopy(spec)
            amended_spec["authority_revision"] = 2
            amended_spec["validation_id"] = "amended-authority"
            amended_path = root / "amended-authority-spec.json"
            amended_path.write_text(json.dumps(amended_spec) + "\n")
            with mock.patch("host_validation.current_authority_revision", return_value=2):
                amended = host_validation.run_validation(root, repository, amended_path)
            self.assertEqual(amended["status"], "pass")
            amended_receipt = json.loads((root / amended["receipt_ref"]).read_bytes())
            self.assertEqual(amended_receipt["authority_revision"], 2)

    def test_partial_admission_bundle_crash_repairs_before_workflow_commit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            bundle_root = root / "runtime-bundle"
            bundle_root.mkdir()
            first_name = _RUNTIME_BUNDLE_FILES[0]
            (bundle_root / first_name).write_bytes((SCRIPT_DIR / first_name).read_bytes())
            self.assertFalse((root / "workflow.json").exists())
            pinned = _seal_runtime_bundle(root)
            self.assertEqual(pinned, bundle_root / "workflow_runtime.py")
            self.assertEqual({path.name for path in bundle_root.iterdir()}, set(_RUNTIME_BUNDLE_FILES))

    def test_missing_or_drifted_pinned_bundle_blocks_without_writer_fallback(self) -> None:
        for mutation in ("missing", "drift"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as raw:
                root = Path(raw).resolve()
                expected = _runtime_bundle_sha256()
                pinned = _seal_runtime_bundle(root)
                create_once_json(root, "workflow.json", {"runtime_bundle": {"sha256": expected}})
                member = root / "runtime-bundle/artifact_store.py"
                if mutation == "missing":
                    member.unlink()
                else:
                    member.write_text("# drift\n")
                output = io.StringIO()
                with mock.patch("sys.stdout", output):
                    exit_code = main(["pinned-runtime", "--root", os.fspath(root)])
                self.assertEqual(exit_code, 4)
                result = json.loads(output.getvalue())
                self.assertEqual(result["status"], "blocked_incompatible_release")
                self.assertNotIn("fallback", result)
                self.assertFalse((root / "orchestration.json").exists())

    def test_json_schema_enum_and_const_do_not_treat_boolean_as_number(self) -> None:
        with self.assertRaisesRegex(RuntimeFailure, "enum"):
            _validate_typed_output(True, {"enum": [1]})
        with self.assertRaisesRegex(RuntimeFailure, "const"):
            _validate_typed_output(False, {"const": 0})
        _validate_typed_output(1.0, {"enum": [1]})

    def test_output_schema_preflight_rejects_provider_invalid_const_and_enum(self) -> None:
        for schema in (
            {
                "type": "object",
                "properties": {"schema_version": {"const": "v1"}},
                "required": ["schema_version"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {"decision": {"enum": ["pass", "blocked"]}},
                "required": ["decision"],
                "additionalProperties": False,
            },
        ):
            with self.assertRaisesRegex(RuntimeFailure, "explicit supported type"):
                _validate_output_schema_contract(schema)
        _validate_output_schema_contract(
            {
                "type": "object",
                "properties": {
                    "schema_version": {"type": "string", "const": "v1"},
                    "decision": {"type": "string", "enum": ["pass", "blocked"]},
                },
                "required": ["schema_version", "decision"],
                "additionalProperties": False,
            }
        )

    def test_output_schema_preflight_accepts_supported_anyof_and_nullable_types(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "value": {
                    "anyOf": [
                        {"type": "string"},
                        {"type": "integer"},
                    ]
                },
                "note": {"type": ["string", "null"]},
            },
            "required": ["value", "note"],
            "additionalProperties": False,
        }
        _validate_output_schema_contract(schema)
        _validate_typed_output({"value": "supported", "note": None}, schema)
        _validate_typed_output({"value": 7, "note": "also supported"}, schema)

    def test_output_schema_preflight_rejects_more_than_five_thousand_properties(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                f"field_{index}": {"type": "string"}
                for index in range(5_001)
            },
            "required": [f"field_{index}" for index in range(5_001)],
            "additionalProperties": False,
        }
        with self.assertRaisesRegex(RuntimeFailure, "property count"):
            _validate_output_schema_contract(schema)

    def test_output_schema_preflight_rejects_more_than_ten_object_levels(self) -> None:
        leaf: dict[str, object] = {"type": "string"}
        for index in range(10, 0, -1):
            leaf = {
                "type": "object",
                "properties": {f"level_{index}": leaf},
                "required": [f"level_{index}"],
                "additionalProperties": False,
            }
        _validate_output_schema_contract(leaf)
        too_deep = {
            "type": "object",
            "properties": {"level_0": leaf},
            "required": ["level_0"],
            "additionalProperties": False,
        }
        with self.assertRaisesRegex(RuntimeFailure, "nesting depth"):
            _validate_output_schema_contract(too_deep)

    def test_invalid_output_schema_is_rejected_before_phase_authority_or_executor(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plan = self.build_run(root, task_count=1)
            task = plan["tasks"][0]
            schema_ref = "schemas/provider-invalid.json"
            schema_payload = (
                json.dumps(
                    {
                        "type": "object",
                        "properties": {"schema_version": {"const": "v1"}},
                        "required": ["schema_version"],
                        "additionalProperties": False,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode()
            (root / schema_ref).write_bytes(schema_payload)
            packet_path = root / task["packet_path"]
            packet = json.loads(packet_path.read_bytes())
            packet["output_schema_ref"] = schema_ref
            packet["output_schema_sha256"] = digest(schema_payload)
            packet_payload = (
                json.dumps(packet, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode()
            packet_path.write_bytes(packet_payload)
            task["packet_sha256"] = digest(packet_payload)
            launched = False

            def execute(_task: dict[str, object], _packet: dict[str, object]) -> RawExecution:
                nonlocal launched
                launched = True
                raise AssertionError("invalid schema must not launch an actor")

            with self.assertRaisesRegex(RuntimeFailure, "explicit supported type"):
                run_read_only_phase(root, plan, execute, max_parallel=1)
            self.assertFalse(launched)
            self.assertFalse((root / "phases/001-research/plan.json").exists())
            self.assertFalse((root / "generations/claims").exists())

    def test_non_json_schema_constants_are_rejected_before_actor_launch(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plan = self.build_run(root, task_count=1)
            task = plan["tasks"][0]
            schema_ref = "schemas/non-json-number.json"
            schema_payload = (
                json.dumps(
                    {
                        "type": "object",
                        "properties": {
                            "score": {"type": "number", "const": float("inf")}
                        },
                        "required": ["score"],
                        "additionalProperties": False,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode()
            (root / schema_ref).write_bytes(schema_payload)
            packet_path = root / task["packet_path"]
            packet = json.loads(packet_path.read_bytes())
            packet["output_schema_ref"] = schema_ref
            packet["output_schema_sha256"] = digest(schema_payload)
            packet_payload = (
                json.dumps(packet, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode()
            packet_path.write_bytes(packet_payload)
            task["packet_sha256"] = digest(packet_payload)
            launched = False

            def execute(_task: dict[str, object], _packet: dict[str, object]) -> RawExecution:
                nonlocal launched
                launched = True
                raise AssertionError("non-JSON schema must not launch an actor")

            with self.assertRaisesRegex(RuntimeFailure, "invalid output schema"):
                run_read_only_phase(root, plan, execute, max_parallel=1)
            self.assertFalse(launched)
            self.assertFalse((root / "phases/001-research/plan.json").exists())

    def test_source_dependency_drift_is_rechecked_immediately_before_actor_launch(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repository = Path(raw) / "repo"
            repository.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.email", "fixture@example.com"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.name", "Fixture"], cwd=repository, check=True)
            (repository / "src").mkdir()
            (repository / "lib").mkdir()
            (repository / "src/value.txt").write_text("base\n")
            dependency = repository / "lib/context.txt"
            dependency.write_text("sealed\n")
            subprocess.run(["git", "add", "."], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repository, check=True)
            baseline = self.source_baseline(repository)
            baseline_payload = baseline_gate._canonical(baseline)
            root = repository / ".workflow/run"
            root.mkdir(parents=True)
            plan = self.build_run(root, task_count=1)
            workflow = json.loads((root / "workflow.json").read_text())
            workflow["admission"]["profile"] = "source_write"
            workflow["admission"]["capabilities"]["sandbox_isolation"]["status"] = "pass"
            workflow["baseline_sha256"] = digest(baseline_payload)
            workflow["admission"]["repository"] = baseline_gate.repository_evidence(baseline)
            (root / workflow["baseline_ref"]).write_bytes(baseline_payload)
            (root / "workflow.json").write_text(
                json.dumps(workflow, sort_keys=True, separators=(",", ":")) + "\n"
            )
            task = plan["tasks"][0]
            task["work_mode"] = "write"
            task["write_roots"] = ["src"]
            task["input_sha256"] = {workflow["baseline_ref"]: digest(baseline_payload)}
            plan["predecessor_sha256"] = digest(baseline_payload)
            self.bind_writer_schema(root, task)
            phase = prepare_isolated_phase(
                root,
                repository,
                plan,
                read_roots=("src", "lib"),
                admission_baseline=baseline,
            )
            dependency.write_text("external-before-launch\n")
            launched = False

            def execute(_task: dict[str, object], _packet: dict[str, object]) -> RawExecution:
                nonlocal launched
                launched = True
                raise AssertionError("drifted source phase must not launch an actor")

            with self.assertRaisesRegex(RuntimeFailure, "external drift"):
                run_read_only_phase(
                    root,
                    plan,
                    execute,
                    max_parallel=1,
                    source_phase=phase,
                )
            self.assertFalse(launched)
            self.assertFalse((root / "phases/001-research/plan.json").exists())

    def test_permission_attestation_rejects_every_extra_read_or_write(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            worker_root = root / "worker"
            codex_home = root / "codex-home"
            worker_root.mkdir()
            context = {
                "workspace_roots": [str(worker_root)],
                "sandbox_policy": {"type": "read-only"},
                "permission_profile": {
                    "type": "managed",
                    "network": "restricted",
                    "file_system": {
                        "type": "restricted",
                        "entries": [
                            {
                                "path": {
                                    "type": "special",
                                    "value": {"kind": "minimal"},
                                },
                                "access": "read",
                            },
                            {
                                "path": {"type": "path", "path": str(worker_root)},
                                "access": "read",
                            },
                            {
                                "path": {
                                    "type": "path",
                                    "path": str(codex_home / "tmp/arg0/codex-arg0fixture"),
                                },
                                "access": "read",
                            },
                        ],
                    },
                },
            }
            self.assertIsNone(_attest_worker_permissions(context, worker_root, codex_home))
            no_tool_context = deepcopy(context)
            no_tool_context["permission_profile"]["file_system"]["entries"].pop()
            self.assertIsNone(
                _attest_worker_permissions(no_tool_context, worker_root, codex_home)
            )
            duplicate_arg0 = deepcopy(context)
            duplicate_arg0["permission_profile"]["file_system"]["entries"].append(
                deepcopy(duplicate_arg0["permission_profile"]["file_system"]["entries"][-1])
            )
            self.assertIn(
                "incomplete or duplicated",
                _attest_worker_permissions(duplicate_arg0, worker_root, codex_home),
            )
            extra_read = deepcopy(context)
            extra_read["permission_profile"]["file_system"]["entries"].append(
                {
                    "path": {"type": "path", "path": str(root / "credential")},
                    "access": "read",
                }
            )
            self.assertIn(
                "unexpected readable path",
                _attest_worker_permissions(extra_read, worker_root, codex_home),
            )
            extra_write = deepcopy(context)
            extra_write["permission_profile"]["file_system"]["entries"][1]["access"] = "write"
            self.assertIn(
                "non-read access",
                _attest_worker_permissions(extra_write, worker_root, codex_home),
            )

    def test_candidate_parent_paths_resolve_from_repository_not_workflow_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repository = Path(raw).resolve()
            (repository / ".git").mkdir()
            workflow_root = repository / ".workflow" / "nested-run"
            workflow_root.mkdir(parents=True)
            self.assertEqual(_repository_root_for(workflow_root), repository)

    def test_live_repository_evidence_detects_checkout_drift(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repository = Path(raw).resolve()
            subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
            subprocess.run(
                ["git", "config", "user.email", "fixture@example.com"],
                cwd=repository,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Fixture"],
                cwd=repository,
                check=True,
            )
            tracked = repository / "tracked.txt"
            tracked.write_text("sealed\n")
            subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repository, check=True)
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repository,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            branch = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=repository,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            parent = {
                "schema_version": "agent-workflow.vnext-pre-slice-summary.v1",
                "head": head,
                "branch": branch,
                "staged_diff_sha256": digest(b""),
                "staged_diff_bytes": 0,
                "unstaged_diff_sha256": digest(b""),
                "unstaged_diff_bytes": 0,
                "untracked": [],
            }
            baseline = baseline_gate.collect_baseline(
                repository,
                untracked_includes=[],
                parent_summary=parent,
            )
            self.assertEqual(
                baseline_gate.current_repository_evidence(repository, baseline),
                baseline_gate.repository_evidence(baseline),
            )
            tracked.write_text("drifted\n")
            self.assertNotEqual(
                baseline_gate.current_repository_evidence(repository, baseline),
                baseline_gate.repository_evidence(baseline),
            )

    def build_run(self, root: Path, *, task_count: int = 2) -> dict[str, object]:
        evidence = b'{"baseline":"fixture"}\n'
        evidence_path = root / "evidence" / "pre-slice-baseline.json"
        evidence_path.parent.mkdir(parents=True)
        evidence_path.write_bytes(evidence)
        workflow = fixture("workflow.json")
        workflow["created_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        workflow["baseline_sha256"] = digest(evidence)
        create_once_json(root, "workflow.json", workflow)
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["answer"],
            "properties": {"answer": {"type": "string"}},
        }
        schema_bytes = json.dumps(schema).encode()
        schema_path = root / "schemas" / "worker-output.json"
        schema_path.parent.mkdir(parents=True)
        schema_path.write_bytes(schema_bytes)
        plan = fixture("phase-plan.json")
        plan["predecessor_sha256"] = digest(evidence)
        template = plan["tasks"][0]
        tasks = []
        for index in range(task_count):
            task = deepcopy(template)
            task_id = f"research-{index + 1}"
            task.update({
                "task_id": task_id,
                "lineage_id": f"lineage-{index + 1}",
                "role": "worker" if index % 2 == 0 else "top",
                "packet_path": f"phases/001-research/tasks/{task_id}/packet.json",
                "input_refs": ["evidence/pre-slice-baseline.json"],
                "input_sha256": {"evidence/pre-slice-baseline.json": digest(evidence)},
            })
            packet = {
                "schema_version": "agent-workflow.task-packet.vnext.v1",
                "prompt": f"Return the bounded answer for {task_id}.",
                "output_schema_ref": "schemas/worker-output.json",
                "output_schema_sha256": digest(schema_bytes),
            }
            packet_bytes = (json.dumps(packet, sort_keys=True, separators=(",", ":")) + "\n").encode()
            packet_path = root / task["packet_path"]
            packet_path.parent.mkdir(parents=True)
            packet_path.write_bytes(packet_bytes)
            task["packet_sha256"] = digest(packet_bytes)
            tasks.append(task)
        plan["tasks"] = tasks
        return plan

    def source_baseline(self, repository: Path) -> dict[str, object]:
        staged = baseline_gate._diff(repository, cached=True, excludes=[], binary=False)
        unstaged = baseline_gate._diff(repository, cached=False, excludes=[], binary=False)
        untracked = [
            item.decode()
            for item in baseline_gate._git(
                repository, "ls-files", "--others", "--exclude-standard", "-z"
            ).split(b"\0")
            if item
        ]
        parent = {
            "schema_version": "agent-workflow.vnext-pre-slice-summary.v1",
            "head": baseline_gate._git(repository, "rev-parse", "HEAD").decode().strip(),
            "branch": baseline_gate._git(repository, "branch", "--show-current").decode().strip(),
            "staged_diff_sha256": digest(staged),
            "staged_diff_bytes": len(staged),
            "unstaged_diff_sha256": digest(unstaged),
            "unstaged_diff_bytes": len(unstaged),
            "untracked": [
                {
                    "path": relative,
                    "sha256": digest((repository / relative).read_bytes()),
                    "bytes": (repository / relative).stat().st_size,
                }
                for relative in sorted(untracked)
            ],
        }
        return baseline_gate.collect_baseline(repository, parent_summary=parent)

    def bind_writer_schema(self, root: Path, task: dict[str, object]) -> None:
        schema = {
            "type": "object",
            "properties": {
                "answer": {"type": "string"},
                "changed_paths": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["answer", "changed_paths"],
            "additionalProperties": False,
        }
        schema_payload = (
            json.dumps(schema, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        schema_ref = "schemas/writer-output.json"
        (root / schema_ref).write_bytes(schema_payload)
        packet_path = root / task["packet_path"]
        packet = json.loads(packet_path.read_bytes())
        packet["output_schema_ref"] = schema_ref
        packet["output_schema_sha256"] = digest(schema_payload)
        packet_payload = (
            json.dumps(packet, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        packet_path.write_bytes(packet_payload)
        task["packet_sha256"] = digest(packet_payload)

    def test_seal_final_validates_before_create_once_publication(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.build_run(root, task_count=1)
            workflow = json.loads((root / "workflow.json").read_text())
            candidate = fixture("final.json")
            candidate["runtime_bundle_sha256"] = workflow["runtime_bundle"]["sha256"]
            terminal_phases = [
                {
                    "phase_id": Path(ref).parts[1],
                    "generation_id": candidate["generation_id"],
                    "status": "completed",
                    "receipt_ref": ref,
                    "receipt_sha256": candidate["phase_receipt_sha256"][ref],
                }
                for ref in candidate["phase_receipt_refs"]
            ]
            with (
                mock.patch(
                    "workflow_runtime._reconcile_run",
                    return_value={"active": [], "materialized_phase_receipts": []},
                ),
                mock.patch(
                    "workflow_runtime.build_resume_brief",
                    return_value={"terminal_phases": terminal_phases},
                ),
                mock.patch(
                    "workflow_runtime.validate_replay_candidate",
                    return_value=candidate,
                ) as validate_candidate,
            ):
                path = workflow_runtime.seal_final(root, candidate)
                self.assertEqual(path, root.resolve() / "final.json")
                self.assertEqual(json.loads(path.read_text()), candidate)
                validate_candidate.assert_called_once()
                with self.assertRaisesRegex(RuntimeFailure, "final"):
                    workflow_runtime.seal_final(root, candidate)

    def test_seal_final_rejects_cancel_and_invalid_candidate_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.build_run(root, task_count=1)
            workflow = json.loads((root / "workflow.json").read_text())
            candidate = fixture("final.json")
            candidate["runtime_bundle_sha256"] = workflow["runtime_bundle"]["sha256"]
            create_once_json(
                root,
                "amendments/cancel.json",
                {
                    "schema_version": "agent-workflow.cancel-request.vnext.v1",
                    "workflow_id": workflow["workflow_id"],
                    "authority_revision": 1,
                    "requested_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                },
            )
            with self.assertRaisesRegex(RuntimeFailure, "cancel"):
                workflow_runtime.seal_final(root, candidate)
            self.assertFalse((root / "final.json").exists())

    def test_final_seal_fences_new_phase_and_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plan = self.build_run(root, task_count=1)
            create_once_json(root, "final.json", fixture("final.json"))
            with self.assertRaisesRegex(RuntimeFailure, "final"):
                cancel_run(root, 1, grace_seconds=0.0)
            with self.assertRaisesRegex(RuntimeFailure, "final"):
                reconcile_run(root, 1, grace_seconds=0.0)
            with self.assertRaisesRegex(RuntimeFailure, "final"):
                run_read_only_phase(
                    root,
                    plan,
                    lambda _task, _packet: execution("late", valid=True),
                    max_parallel=1,
                )

    def test_final_publication_serializes_concurrent_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plan = self.build_run(root, task_count=1)
            workflow = json.loads((root / "workflow.json").read_text())
            candidate = fixture("final.json")
            candidate["runtime_bundle_sha256"] = workflow["runtime_bundle"]["sha256"]
            terminal_phases = [
                {
                    "phase_id": Path(ref).parts[1],
                    "generation_id": candidate["generation_id"],
                    "status": "completed",
                    "receipt_ref": ref,
                    "receipt_sha256": candidate["phase_receipt_sha256"][ref],
                }
                for ref in candidate["phase_receipt_refs"]
            ]
            publish_started = threading.Event()
            allow_publish = threading.Event()
            original_create = workflow_runtime.create_once_json
            original_reconcile = workflow_runtime._reconcile_run

            def delayed_create(run_root: Path, relative: str, value: object) -> Path:
                if relative == "final.json":
                    publish_started.set()
                    self.assertTrue(allow_publish.wait(timeout=2))
                return original_create(run_root, relative, value)

            def reconcile_for_test(*args: object, **kwargs: object) -> dict[str, object]:
                if threading.current_thread().name == "final-publisher":
                    return {"active": [], "materialized_phase_receipts": []}
                return original_reconcile(*args, **kwargs)

            final_errors: list[Exception] = []
            mutation_errors: dict[str, list[Exception]] = {
                name: []
                for name in ("cancel", "phase", "amend", "resume", "reconcile")
            }

            def publish() -> None:
                try:
                    workflow_runtime.seal_final(root, candidate)
                except Exception as exc:  # surfaced after both threads join
                    final_errors.append(exc)

            def mutate(name: str, operation: object) -> None:
                try:
                    operation()
                except Exception as exc:  # expected after final wins
                    mutation_errors[name].append(exc)

            operations = {
                "cancel": lambda: cancel_run(root, 1, grace_seconds=0.0),
                "phase": lambda: run_read_only_phase(
                    root,
                    plan,
                    lambda _task, _packet: execution("late", valid=True),
                    max_parallel=1,
                ),
                "amend": lambda: workflow_runtime.seal_amendment(root, workflow, {}),
                "resume": lambda: workflow_runtime.seal_resume_brief(
                    root,
                    workflow,
                    "generation-002",
                ),
                "reconcile": lambda: reconcile_run(root, 1, grace_seconds=0.0),
            }

            with (
                mock.patch(
                    "workflow_runtime._reconcile_run",
                    side_effect=reconcile_for_test,
                ),
                mock.patch(
                    "workflow_runtime.build_resume_brief",
                    return_value={"terminal_phases": terminal_phases},
                ),
                mock.patch(
                    "workflow_runtime.validate_replay_candidate",
                    return_value=candidate,
                ),
                mock.patch("workflow_runtime.create_once_json", side_effect=delayed_create),
            ):
                final_thread = threading.Thread(target=publish, name="final-publisher")
                final_thread.start()
                self.assertTrue(publish_started.wait(timeout=2))
                mutation_threads = {
                    name: threading.Thread(target=mutate, args=(name, operation))
                    for name, operation in operations.items()
                }
                for thread in mutation_threads.values():
                    thread.start()
                time.sleep(0.05)
                mutations_waited_for_final = all(
                    thread.is_alive() for thread in mutation_threads.values()
                )
                allow_publish.set()
                final_thread.join(timeout=2)
                for thread in mutation_threads.values():
                    thread.join(timeout=2)

            self.assertTrue(mutations_waited_for_final)
            self.assertEqual(final_errors, [])
            for name, errors in mutation_errors.items():
                self.assertEqual(len(errors), 1, name)
                self.assertRegex(str(errors[0]), "final")
            self.assertTrue((root / "final.json").is_file())
            self.assertFalse((root / "amendments/cancel.json").exists())

    def test_two_role_pinned_tasks_run_concurrently_and_emit_one_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plan = self.build_run(root)
            active = 0
            max_active = 0
            lock = threading.Lock()

            def execute(task: dict[str, object], packet: dict[str, object]) -> RawExecution:
                nonlocal active, max_active
                with lock:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.04)
                with lock:
                    active -= 1
                model = "gpt-5.6-terra" if task["role"] == "worker" else "gpt-5.6-sol"
                session_id = f"session-{task['task_id']}"
                return RawExecution(
                    exit_code=0,
                    events=[
                        {"type": "thread.started", "thread_id": session_id},
                        {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps({"answer": packet["prompt"]})}},
                        {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 10}},
                    ],
                    stderr="",
                    turn_context={"model": model, "effort": "xhigh", "session_id": session_id},
                )

            summary = run_read_only_phase(root, plan, execute, max_parallel=2)
            self.assertEqual(max_active, 2)
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(summary["receipt_count"], 1)
            self.assertEqual(summary["task_counts"]["completed"], 2)
            self.assertEqual(summary["terminal_reason"], "all_tasks_terminal")
            self.assertEqual(
                summary["completion_density_source"],
                "host_raw_session_audit_required",
            )
            claim = json.loads((root / summary["generation_claim_ref"]).read_text())
            self.assertEqual(claim["generation_id"], "generation-001")
            self.assertEqual(
                claim["plan_sha256"],
                digest((root / "phases/001-research/plan.json").read_bytes()),
            )
            receipt = json.loads((root / summary["receipt_ref"]).read_text())
            validate_contract("phase-receipt", receipt)
            self.assertEqual(receipt["task_counts"]["completed"], 2)
            for task in plan["tasks"]:
                result_path = root / f"phases/001-research/tasks/{task['task_id']}/result.json"
                result = json.loads(result_path.read_text())
                validate_contract("task-result", result)
                expected = "gpt-5.6-terra" if task["role"] == "worker" else "gpt-5.6-sol"
                self.assertEqual(result["actual_route"]["model"], expected)

            with self.assertRaisesRegex(RuntimeFailure, "exactly one initial phase"):
                run_read_only_phase(root, plan, execute, max_parallel=2)

    def test_additional_phase_terminal_fence_excludes_only_its_committed_plan(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            initial = self.build_run(root, task_count=1)

            def execute(task: dict[str, object], packet: dict[str, object]) -> RawExecution:
                session_id = f"session-{task['task_id']}"
                model = "gpt-5.6-sol" if task["role"] == "top" else "gpt-5.6-terra"
                return RawExecution(
                    exit_code=0,
                    events=[
                        {"type": "thread.started", "thread_id": session_id},
                        {
                            "type": "item.completed",
                            "item": {
                                "type": "agent_message",
                                "text": json.dumps({"answer": packet["prompt"]}),
                            },
                        },
                        {
                            "type": "turn.completed",
                            "usage": {"input_tokens": 10, "output_tokens": 2},
                        },
                    ],
                    stderr="",
                    turn_context={
                        "model": model,
                        "effort": "xhigh",
                        "session_id": session_id,
                    },
                )

            first = run_read_only_phase(root, initial, execute, max_parallel=1)
            self.assertEqual(first["status"], "completed")

            second = deepcopy(initial)
            second["phase_id"] = "002-followup"
            second["generation_id"] = "generation-002"
            second["caused_by"] = ["001-research"]
            second["predecessor_sha256"] = causal_predecessor_sha256(
                root, second["caused_by"]
            )
            task = second["tasks"][0]
            task["task_id"] = "verify-followup"
            task["lineage_id"] = "lineage-followup"
            task["role"] = "top"
            task["packet_path"] = "phases/002-followup/tasks/verify-followup/packet.json"
            causal_ref = "phases/001-research/receipt.json"
            task["input_refs"] = [causal_ref]
            task["input_sha256"] = {causal_ref: digest((root / causal_ref).read_bytes())}
            packet = {
                "schema_version": "agent-workflow.task-packet.vnext.v1",
                "prompt": "Verify the completed prior phase.",
                "output_schema_ref": "schemas/worker-output.json",
                "output_schema_sha256": digest((root / "schemas/worker-output.json").read_bytes()),
            }
            packet_payload = (
                json.dumps(packet, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode()
            packet_path = root / task["packet_path"]
            packet_path.parent.mkdir(parents=True)
            packet_path.write_bytes(packet_payload)
            task["packet_sha256"] = digest(packet_payload)
            workflow = json.loads((root / "workflow.json").read_text())
            seal_resume_brief(root, workflow, second["generation_id"])

            with self.assertRaisesRegex(
                RecoveryError,
                "exact committed generation claim",
            ):
                prepare_phase_authority(root, workflow, second, reconciling=True)

            summary = run_read_only_phase(
                root,
                second,
                execute,
                max_parallel=1,
                terminal_fence=lambda: prepare_phase_authority(
                    root, workflow, second, reconciling=True
                ),
            )
            self.assertEqual(summary["status"], "completed")
            self.assertTrue((root / "phases/002-followup/receipt.json").is_file())

    def test_failed_initial_lineage_runs_one_evidence_bound_recovery_phase(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            initial = self.build_run(root, task_count=1)

            def execution(text: str, *, valid: bool) -> RawExecution:
                session_id = (
                    "019f566c-4899-7d03-83a5-3e7043b74fcd"
                    if valid
                    else "019f566c-4899-7d03-83a5-3e7043b74fcc"
                )
                return RawExecution(
                    exit_code=0,
                    events=[
                        {"type": "thread.started", "thread_id": session_id},
                        {
                            "type": "item.completed",
                            "item": {
                                "type": "agent_message",
                                "text": json.dumps({"answer": text} if valid else {"wrong": text}),
                            },
                        },
                        {"type": "turn.completed", "usage": {"input_tokens": 20, "output_tokens": 5}},
                    ],
                    stderr="",
                    turn_context={
                        "model": "gpt-5.6-terra",
                        "effort": "xhigh",
                        "session_id": session_id,
                    },
                )

            first = run_read_only_phase(
                root,
                initial,
                lambda _task, _packet: execution("first", valid=False),
                max_parallel=1,
            )
            self.assertEqual(first["status"], "failed")
            failed_ref = "phases/001-research/tasks/research-1/result.json"
            causal_ref = "phases/001-research/receipt.json"

            recovery = deepcopy(initial)
            recovery["phase_id"] = "002-recover"
            recovery["caused_by"] = ["001-research"]
            recovery["predecessor_sha256"] = causal_predecessor_sha256(
                root,
                recovery["caused_by"],
            )
            task = recovery["tasks"][0]
            task["task_id"] = "research-recovery"
            task["packet_path"] = "phases/002-recover/tasks/research-recovery/packet.json"
            task["input_refs"] = [failed_ref, causal_ref]
            task["input_sha256"] = {
                failed_ref: digest((root / failed_ref).read_bytes()),
                causal_ref: digest((root / causal_ref).read_bytes()),
            }
            original_packet = json.loads(
                (root / initial["tasks"][0]["packet_path"]).read_text()
            )
            original_packet["prompt"] = "Repair the failed result once."
            packet_payload = (
                json.dumps(original_packet, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode()
            packet_path = root / task["packet_path"]
            packet_path.parent.mkdir(parents=True)
            packet_path.write_bytes(packet_payload)
            task["packet_sha256"] = digest(packet_payload)

            failed_home = root / "runtime/codex-homes/failed-lineage-owner"
            failed_home.mkdir(parents=True)
            failed_result = json.loads((root / failed_ref).read_text())
            request = {
                "environment": {"CODEX_HOME": os.fspath(failed_home)},
                "phase_id": "001-research",
                "task_id": "research-1",
            }
            context = {
                "model": failed_result["actual_route"]["model"],
                "effort": failed_result["actual_route"]["reasoning_effort"],
                "session_id": failed_result["actual_route"]["session_id"],
            }
            rollout = failed_home / "sessions/recovery.jsonl"
            rollout.parent.mkdir(parents=True)
            rollout.write_text(json.dumps({"type": "session_meta", "payload": {"id": context["session_id"]}}) + "\n")
            with (
                mock.patch("workflow_runtime.load_supervisor_request", return_value=(request, b"request")),
                mock.patch("workflow_runtime._find_turn_context", return_value=context),
                mock.patch("workflow_runtime._find_session_rollout", return_value=rollout),
            ):
                binding = workflow_runtime._recovery_resume_binding(root, recovery, task)
            self.assertEqual(binding["failed_result_ref"], failed_ref)
            self.assertEqual(binding["session_id"], failed_result["actual_route"]["session_id"])
            self.assertEqual(binding["codex_home"], os.fspath(failed_home.resolve()))

            second = run_read_only_phase(
                root,
                recovery,
                lambda _task, _packet: execution("repaired", valid=True),
                max_parallel=1,
            )
            self.assertEqual(second["status"], "completed")
            self.assertTrue((root / "lineages/lineage-1/recovery.json").is_file())
            self.assertTrue((root / "runtime/deadlines/001-research.json").is_file())
            self.assertTrue((root / "runtime/deadlines/002-recover.json").is_file())

            third = deepcopy(recovery)
            third["phase_id"] = "003-retry-again"
            third["caused_by"] = ["002-recover"]
            third["predecessor_sha256"] = causal_predecessor_sha256(
                root,
                third["caused_by"],
            )
            third["tasks"][0]["task_id"] = "retry-again"
            with self.assertRaisesRegex(RuntimeFailure, "successful lineage|already exhausted"):
                run_read_only_phase(
                    root,
                    third,
                    lambda _task, _packet: execution("again", valid=True),
                    max_parallel=1,
                )

    def test_reconcile_repairs_claim_gap_before_publishing_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plan = self.build_run(root, task_count=1)
            workflow = json.loads((root / "workflow.json").read_text())
            plan_path = create_once_json(root, "phases/001-research/plan.json", plan)
            _seal_generation_claim(root, workflow, plan, plan_path.read_bytes())
            self.assertFalse((root / "lineages/lineage-1/origin.json").exists())
            execution = RawExecution(
                exit_code=0,
                events=[
                    {"type": "thread.started", "thread_id": "claim-gap"},
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "agent_message",
                            "text": json.dumps({"answer": "reconciled"}),
                        },
                    },
                    {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}},
                ],
                stderr="",
                turn_context={
                    "model": "gpt-5.6-terra",
                    "effort": "xhigh",
                    "session_id": "claim-gap",
                },
            )
            summary = run_read_only_phase(
                root,
                plan,
                lambda _task, _packet: execution,
                max_parallel=1,
                reconciled_executions={"research-1": execution},
            )
            self.assertEqual(summary["status"], "completed")
            self.assertTrue((root / "lineages/lineage-1/origin.json").is_file())

    def test_reconcile_terminalizes_winning_plan_crashed_before_watchdog_launch(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plan = self.build_run(root, task_count=1)
            workflow = json.loads((root / "workflow.json").read_text())
            plan_path = create_once_json(root, "phases/001-research/plan.json", plan)
            _seal_generation_claim(root, workflow, plan, plan_path.read_bytes())
            summary = reconcile_run(root, 1, grace_seconds=0.0)
            self.assertEqual(summary["materialized_phase_receipts"], ["phases/001-research/receipt.json"])
            result = json.loads(
                (root / "phases/001-research/tasks/research-1/result.json").read_text()
            )
            self.assertEqual(result["status"], "not_started_interrupted")
            self.assertEqual(result["token_usage"]["total"], 0)
            receipt = json.loads((root / "phases/001-research/receipt.json").read_text())
            self.assertEqual(receipt["status"], "failed")
            self.assertEqual(receipt["task_counts"]["not_started_interrupted"], 1)
            self.assertTrue((root / "lineages/lineage-1/origin.json").is_file())

    def test_reconcile_terminalizes_request_only_crash_before_watchdog_launch(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plan = self.build_run(root, task_count=1)
            workflow = json.loads((root / "workflow.json").read_text())
            plan_path = create_once_json(root, "phases/001-research/plan.json", plan)
            claim_ref, claim_path = _seal_generation_claim(
                root,
                workflow,
                plan,
                plan_path.read_bytes(),
            )
            task = plan["tasks"][0]
            marker = "agent-workflow:fixture-workflow:001-research:research-1:request-only"
            command = [
                os.fspath(Path(sys.executable).resolve()),
                "-c",
                "pass",
                "-c",
                f'agent_workflow_audit_marker="{marker}"',
            ]
            request_ref = "runtime/watchdogs/001-research/research-1/request.json"
            create_once_json(
                root,
                request_ref,
                {
                    "schema_version": "agent-workflow.supervisor-request.vnext.v2",
                    "workflow_id": workflow["workflow_id"],
                    "authority_revision": plan["authority_revision"],
                    "generation_id": plan["generation_id"],
                    "phase_id": plan["phase_id"],
                    "task_id": task["task_id"],
                    "plan_sha256": digest(plan_path.read_bytes()),
                    "generation_claim_ref": claim_ref,
                    "generation_claim_sha256": digest(claim_path.read_bytes()),
                    "runtime_bundle_sha256": "sha256:" + "2" * 64,
                    "codex_binary": os.fspath(Path(sys.executable).resolve()),
                    "codex_binary_sha256": digest(Path(sys.executable).resolve().read_bytes()),
                    "transport_executable_sha256": digest(Path(sys.executable).resolve().read_bytes()),
                    "transport_adapter_sha256": None,
                    "command": command,
                    "command_sha256": digest(
                        (json.dumps(command, sort_keys=True, separators=(",", ":")) + "\n").encode()
                    ),
                    "cwd": str(root),
                    "work_mode": "read",
                    "write_roots": [],
                    "environment": {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                    "audit_marker": marker,
                    "deadline_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "deadline_monotonic": time.monotonic() + 60,
                    "boot_identity": _process_identity(1),
                    "terminate_grace_seconds": 0.1,
                    "log_limit_bytes": 4096,
                    "stdout_ref": "transient/001-research/research-1/stdout.jsonl",
                    "stderr_ref": "transient/001-research/research-1/stderr.log",
                    "receipt_ref": "runtime/watchdogs/001-research/research-1/terminal.json",
                },
            )

            summary = reconcile_run(root, 1, grace_seconds=0.0)
            self.assertEqual(
                summary["materialized_phase_receipts"],
                ["phases/001-research/receipt.json"],
            )
            result = json.loads(
                (root / "phases/001-research/tasks/research-1/result.json").read_text()
            )
            self.assertEqual(result["status"], "not_started_interrupted")

    def test_reconcile_ignores_plan_without_winning_generation_claim(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plan = self.build_run(root, task_count=1)
            create_once_json(root, "phases/001-orphan/plan.json", plan)

            summary = reconcile_run(root, 1, grace_seconds=0.0)
            self.assertEqual(summary["materialized_phase_receipts"], [])
            self.assertFalse((root / "phases/001-orphan/receipt.json").exists())

    def test_reconcile_cannot_ratify_completed_worker_after_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plan = self.build_run(root, task_count=1)
            workflow = json.loads((root / "workflow.json").read_text())
            plan_path = create_once_json(root, "phases/001-research/plan.json", plan)
            _seal_generation_claim(root, workflow, plan, plan_path.read_bytes())
            create_once_json(
                root,
                "amendments/cancel.json",
                {
                    "schema_version": "agent-workflow.cancel-request.vnext.v1",
                    "workflow_id": workflow["workflow_id"],
                    "authority_revision": 1,
                    "requested_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                },
            )
            execution = RawExecution(
                exit_code=0,
                events=[
                    {"type": "thread.started", "thread_id": "cancel-before-reconcile"},
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": json.dumps({"answer": "late"})},
                    },
                    {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}},
                ],
                stderr="",
                turn_context={
                    "model": "gpt-5.6-terra",
                    "effort": "xhigh",
                    "session_id": "cancel-before-reconcile",
                },
            )
            with self.assertRaisesRegex(RuntimeFailure, "cancel fence"):
                run_read_only_phase(
                    root,
                    plan,
                    lambda _task, _packet: execution,
                    max_parallel=1,
                    reconciled_executions={"research-1": execution},
                )
            self.assertFalse((root / "phases/001-research/receipt.json").exists())

    def test_source_phase_integrates_before_result_and_receipt_publication(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repository = Path(raw) / "repo"
            repository.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.email", "fixture@example.com"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.name", "Fixture"], cwd=repository, check=True)
            (repository / "src/api").mkdir(parents=True)
            (repository / "src/web").mkdir(parents=True)
            (repository / "src/api/value.txt").write_text("api-v1\n")
            (repository / "src/web/value.txt").write_text("web-v1\n")
            subprocess.run(["git", "add", "."], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repository, check=True)
            baseline = self.source_baseline(repository)
            baseline_payload = baseline_gate._canonical(baseline)
            root = repository / ".workflow/run"
            root.mkdir(parents=True)
            plan = self.build_run(root, task_count=2)
            workflow = json.loads((root / "workflow.json").read_text())
            workflow["admission"]["profile"] = "source_write"
            workflow["admission"]["capabilities"]["sandbox_isolation"]["status"] = "pass"
            workflow["baseline_sha256"] = digest(baseline_payload)
            workflow["admission"]["repository"] = baseline_gate.repository_evidence(baseline)
            (root / workflow["baseline_ref"]).write_bytes(baseline_payload)
            (root / "workflow.json").write_text(
                json.dumps(workflow, sort_keys=True, separators=(",", ":")) + "\n"
            )
            for task, write_root in zip(plan["tasks"], ("src/api", "src/web"), strict=True):
                task["work_mode"] = "write"
                task["write_roots"] = [write_root]
                self.bind_writer_schema(root, task)
            plan["predecessor_sha256"] = digest(baseline_payload)
            for task in plan["tasks"]:
                task["input_sha256"] = {workflow["baseline_ref"]: digest(baseline_payload)}
            source_phase = prepare_isolated_phase(
                root,
                repository,
                plan,
                admission_baseline=baseline,
            )
            overrides = {
                task_id: {"_runtime_worker_root": os.fspath(workspace.root)}
                for task_id, workspace in source_phase.tasks.items()
            }

            def execute(task: dict[str, object], _packet: dict[str, object]) -> RawExecution:
                workspace = Path(task["_runtime_worker_root"])
                target = workspace / task["write_roots"][0] / "value.txt"
                target.write_text(f"{task['task_id']}-v2\n")
                session = f"writer-{task['task_id']}"
                model = "gpt-5.6-terra" if task["role"] == "worker" else "gpt-5.6-sol"
                output = {
                    "answer": "written",
                    "changed_paths": [f"{task['write_roots'][0]}/value.txt"],
                }
                return RawExecution(
                    exit_code=0,
                    events=[
                        {"type": "thread.started", "thread_id": session},
                        {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(output)}},
                        {"type": "turn.completed", "usage": {"input_tokens": 2, "output_tokens": 1}},
                    ],
                    stderr="",
                    turn_context={"model": model, "effort": "xhigh", "session_id": session},
                )

            summary = run_read_only_phase(
                root,
                plan,
                execute,
                max_parallel=2,
                source_phase=source_phase,
                runtime_task_overrides=overrides,
            )
            self.assertEqual(summary["status"], "completed")
            self.assertEqual((repository / "src/api/value.txt").read_text(), "research-1-v2\n")
            self.assertEqual((repository / "src/web/value.txt").read_text(), "research-2-v2\n")
            receipt = json.loads((root / summary["receipt_ref"]).read_text())
            self.assertEqual(receipt["integration"]["status"], "applied")
            replay_results = {}
            for task in plan["tasks"]:
                result = json.loads(
                    (root / f"phases/001-research/tasks/{task['task_id']}/result.json").read_text()
                )
                self.assertEqual(result["changed_paths"], [f"{task['write_roots'][0]}/value.txt"])
                validate_contract("task-result", result)
                replay_results[task["task_id"]] = result
            _validate_source_patch_replay(root, plan, receipt, replay_results)
            tampered = deepcopy(replay_results)
            tampered["research-1"]["changed_paths"] = []
            with self.assertRaisesRegex(ProtocolError, "do not match"):
                _validate_source_patch_replay(root, plan, receipt, tampered)

    def test_source_phase_drift_reclassifies_results_before_blocked_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repository = Path(raw) / "repo"
            repository.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.email", "fixture@example.com"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.name", "Fixture"], cwd=repository, check=True)
            (repository / "src").mkdir()
            target = repository / "src/value.txt"
            target.write_text("base\n")
            subprocess.run(["git", "add", "."], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repository, check=True)
            baseline = self.source_baseline(repository)
            baseline_payload = baseline_gate._canonical(baseline)
            root = repository / ".workflow/run"
            root.mkdir(parents=True)
            plan = self.build_run(root, task_count=1)
            workflow = json.loads((root / "workflow.json").read_text())
            workflow["admission"]["profile"] = "source_write"
            workflow["admission"]["capabilities"]["sandbox_isolation"]["status"] = "pass"
            workflow["baseline_sha256"] = digest(baseline_payload)
            workflow["admission"]["repository"] = baseline_gate.repository_evidence(baseline)
            (root / workflow["baseline_ref"]).write_bytes(baseline_payload)
            (root / "workflow.json").write_text(json.dumps(workflow, sort_keys=True, separators=(",", ":")) + "\n")
            plan["tasks"][0]["work_mode"] = "write"
            plan["tasks"][0]["write_roots"] = ["src"]
            self.bind_writer_schema(root, plan["tasks"][0])
            plan["predecessor_sha256"] = digest(baseline_payload)
            for task in plan["tasks"]:
                task["input_sha256"] = {workflow["baseline_ref"]: digest(baseline_payload)}
            source_phase = prepare_isolated_phase(
                root,
                repository,
                plan,
                admission_baseline=baseline,
            )
            workspace = source_phase.tasks["research-1"].root

            def execute(task: dict[str, object], _packet: dict[str, object]) -> RawExecution:
                (Path(task["_runtime_worker_root"]) / "src/value.txt").write_text("worker\n")
                target.write_text("human\n")
                return RawExecution(
                    exit_code=0,
                    events=[
                        {"type": "thread.started", "thread_id": "writer-drift"},
                        {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps({"answer": "written", "changed_paths": ["src/value.txt"]})}},
                        {"type": "turn.completed", "usage": {"input_tokens": 2, "output_tokens": 1}},
                    ],
                    stderr="",
                    turn_context={"model": "gpt-5.6-terra", "effort": "xhigh", "session_id": "writer-drift"},
                )

            summary = run_read_only_phase(
                root,
                plan,
                execute,
                max_parallel=1,
                source_phase=source_phase,
                runtime_task_overrides={"research-1": {"_runtime_worker_root": os.fspath(workspace)}},
            )
            self.assertEqual(summary["status"], "blocked")
            self.assertEqual(target.read_text(), "human\n")
            result = json.loads((root / "phases/001-research/tasks/research-1/result.json").read_text())
            self.assertEqual(result["status"], "concurrent_edit_conflict")
            self.assertEqual(result["terminal_reason"], "source_drift")
            receipt = json.loads((root / summary["receipt_ref"]).read_text())
            self.assertEqual(receipt["integration"]["status"], "conflict")
            validate_contract("phase-receipt", receipt)

    def test_source_phase_reconciles_after_integration_before_result_publication(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repository = Path(raw) / "repo"
            repository.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.email", "fixture@example.com"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.name", "Fixture"], cwd=repository, check=True)
            (repository / "src").mkdir()
            target = repository / "src/value.txt"
            target.write_text("base\n")
            subprocess.run(["git", "add", "."], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repository, check=True)
            baseline = self.source_baseline(repository)
            baseline_payload = baseline_gate._canonical(baseline)
            root = repository / ".workflow/run"
            root.mkdir(parents=True)
            plan = self.build_run(root, task_count=1)
            workflow = json.loads((root / "workflow.json").read_text())
            workflow["admission"]["profile"] = "source_write"
            workflow["admission"]["capabilities"]["sandbox_isolation"]["status"] = "pass"
            workflow["baseline_sha256"] = digest(baseline_payload)
            workflow["admission"]["repository"] = baseline_gate.repository_evidence(baseline)
            (root / workflow["baseline_ref"]).write_bytes(baseline_payload)
            (root / "workflow.json").write_text(
                json.dumps(workflow, sort_keys=True, separators=(",", ":")) + "\n"
            )
            task = plan["tasks"][0]
            task["work_mode"] = "write"
            task["write_roots"] = ["src"]
            self.bind_writer_schema(root, task)
            task["input_sha256"] = {workflow["baseline_ref"]: digest(baseline_payload)}
            plan["predecessor_sha256"] = digest(baseline_payload)
            source_phase = prepare_isolated_phase(
                root,
                repository,
                plan,
                admission_baseline=baseline,
            )
            raw_execution = RawExecution(
                exit_code=0,
                events=[
                    {"type": "thread.started", "thread_id": "writer-reconcile"},
                    {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps({"answer": "written", "changed_paths": ["src/value.txt"]})}},
                    {"type": "turn.completed", "usage": {"input_tokens": 2, "output_tokens": 1}},
                ],
                stderr="",
                turn_context={"model": "gpt-5.6-terra", "effort": "xhigh", "session_id": "writer-reconcile"},
            )

            def execute(task_value: dict[str, object], _packet: dict[str, object]) -> RawExecution:
                (Path(task_value["_runtime_worker_root"]) / "src/value.txt").write_text("worker\n")
                return raw_execution

            with mock.patch(
                "workflow_runtime.create_once_bytes",
                side_effect=ArtifactError("simulated result publication crash"),
            ):
                with self.assertRaisesRegex(RuntimeFailure, "simulated result publication crash"):
                    run_read_only_phase(
                        root,
                        plan,
                        execute,
                        max_parallel=1,
                        source_phase=source_phase,
                        runtime_task_overrides={
                            "research-1": {
                                "_runtime_worker_root": os.fspath(
                                    source_phase.tasks["research-1"].root
                                )
                            }
                        },
                    )
            self.assertEqual(target.read_text(), "worker\n")
            self.assertTrue(
                (root / "runtime/source-write/001-research/integration-terminal.json").is_file()
            )
            self.assertFalse((root / "phases/001-research/receipt.json").exists())
            loaded = load_isolated_phase(
                root,
                repository,
                plan,
                admission_baseline=baseline,
            )
            summary = run_read_only_phase(
                root,
                plan,
                lambda _task, _packet: raw_execution,
                max_parallel=1,
                reconciled_executions={"research-1": raw_execution},
                source_phase=loaded,
            )
            self.assertEqual(summary["status"], "completed")
            self.assertTrue((root / summary["receipt_ref"]).is_file())
            result = json.loads(
                (root / "phases/001-research/tasks/research-1/result.json").read_text()
            )
            self.assertEqual(result["changed_paths"], ["src/value.txt"])

    def test_additional_source_phase_reconcile_integrates_and_publishes_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repository = Path(raw) / "repo"
            repository.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
            subprocess.run(
                ["git", "config", "user.email", "fixture@example.com"],
                cwd=repository,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Fixture"],
                cwd=repository,
                check=True,
            )
            (repository / "src").mkdir()
            target = repository / "src/value.txt"
            target.write_text("base\n")
            subprocess.run(["git", "add", "."], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repository, check=True)
            baseline = self.source_baseline(repository)
            baseline_payload = baseline_gate._canonical(baseline)

            root = repository / ".workflow/run"
            initial = self.build_run(root, task_count=1)
            workflow = json.loads((root / "workflow.json").read_text())
            workflow["success_criteria"].append(
                {"id": "AC-2", "description": "Additional source work is integrated."}
            )
            workflow["admission"]["profile"] = "source_write"
            workflow["admission"]["capabilities"]["sandbox_isolation"]["status"] = "pass"
            workflow["baseline_sha256"] = digest(baseline_payload)
            workflow["admission"]["repository"] = baseline_gate.repository_evidence(baseline)
            (root / workflow["baseline_ref"]).write_bytes(baseline_payload)
            (root / "workflow.json").write_text(
                json.dumps(workflow, sort_keys=True, separators=(",", ":")) + "\n"
            )
            initial["predecessor_sha256"] = digest(baseline_payload)
            initial_task = initial["tasks"][0]
            initial_task["input_sha256"] = {
                workflow["baseline_ref"]: digest(baseline_payload)
            }

            def raw_execution(task: dict[str, object], text: str) -> RawExecution:
                session_id = f"session-{task['task_id']}"
                output: dict[str, object] = {"answer": text}
                if task["work_mode"] == "write":
                    output["changed_paths"] = ["src/value.txt"]
                return RawExecution(
                    exit_code=0,
                    events=[
                        {"type": "thread.started", "thread_id": session_id},
                        {
                            "type": "item.completed",
                            "item": {
                                "type": "agent_message",
                                "text": json.dumps(output),
                            },
                        },
                        {
                            "type": "turn.completed",
                            "usage": {"input_tokens": 10, "output_tokens": 2},
                        },
                    ],
                    stderr="",
                    turn_context={
                        "model": "gpt-5.6-terra",
                        "effort": "xhigh",
                        "session_id": session_id,
                    },
                )

            first = run_read_only_phase(
                root,
                initial,
                lambda task, _packet: raw_execution(task, "discovered"),
                max_parallel=1,
            )
            self.assertEqual(first["status"], "completed")

            second = deepcopy(initial)
            second["phase_id"] = "002-write"
            second["generation_id"] = "generation-002"
            second["caused_by"] = ["001-research"]
            second["predecessor_sha256"] = causal_predecessor_sha256(
                root, second["caused_by"]
            )
            task = second["tasks"][0]
            task["task_id"] = "write-followup"
            task["lineage_id"] = "lineage-write-followup"
            task["criterion_id"] = "AC-2"
            task["work_mode"] = "write"
            task["write_roots"] = ["src"]
            task["packet_path"] = "phases/002-write/tasks/write-followup/packet.json"
            causal_ref = "phases/001-research/receipt.json"
            task["input_refs"] = [causal_ref]
            task["input_sha256"] = {causal_ref: digest((root / causal_ref).read_bytes())}
            packet = {
                "schema_version": "agent-workflow.task-packet.vnext.v1",
                "prompt": "Write the bounded follow-up.",
                "output_schema_ref": "schemas/worker-output.json",
                "output_schema_sha256": digest((root / "schemas/worker-output.json").read_bytes()),
            }
            packet_payload = (
                json.dumps(packet, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode()
            packet_path = root / task["packet_path"]
            packet_path.parent.mkdir(parents=True)
            packet_path.write_bytes(packet_payload)
            task["packet_sha256"] = digest(packet_payload)
            self.bind_writer_schema(root, task)
            seal_resume_brief(root, workflow, second["generation_id"])

            source_phase = prepare_isolated_phase(
                root,
                repository,
                second,
                admission_baseline=baseline,
                predecessor_sha256=second["predecessor_sha256"],
            )
            (source_phase.tasks["write-followup"].root / "src/value.txt").write_text(
                "worker\n"
            )
            plan_path = create_once_json(root, "phases/002-write/plan.json", second)
            _seal_generation_claim(root, workflow, second, plan_path.read_bytes())
            execution = raw_execution(task, "written")

            summary = run_read_only_phase(
                root,
                second,
                lambda _task, _packet: execution,
                max_parallel=1,
                reconciled_executions={"write-followup": execution},
                source_phase=source_phase,
            )
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(target.read_text(), "worker\n")
            self.assertTrue((root / "phases/002-write/receipt.json").is_file())
            result = json.loads(
                (root / "phases/002-write/tasks/write-followup/result.json").read_text()
            )
            self.assertEqual(result["changed_paths"], ["src/value.txt"])

    def test_cumulative_source_head_replays_cross_anchor_and_allows_same_anchor_followup(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repository = Path(raw) / "repo"
            repository.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.email", "fixture@example.com"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.name", "Fixture"], cwd=repository, check=True)
            (repository / "server").mkdir()
            (repository / "src").mkdir()
            (repository / "server/contract.txt").write_text("server-v1\n")
            (repository / "src/types.txt").write_text("types-v1\n")
            (repository / "src/App.txt").write_text("app-v1\n")
            subprocess.run(["git", "add", "."], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repository, check=True)
            baseline = self.source_baseline(repository)
            baseline_payload = baseline_gate._canonical(baseline)
            root = repository / ".workflow/run"
            root.mkdir(parents=True)
            first = self.build_run(root, task_count=1)
            workflow = json.loads((root / "workflow.json").read_text())
            workflow["success_criteria"].extend(
                [
                    {"id": "AC-2", "description": "UI types consume the server contract."},
                    {"id": "AC-3", "description": "A later UI repair may reuse the src anchor."},
                ]
            )
            workflow["admission"]["profile"] = "source_write"
            workflow["admission"]["capabilities"]["sandbox_isolation"]["status"] = "pass"
            workflow["admission"]["relevant_roots"] = ["."]
            workflow["baseline_sha256"] = digest(baseline_payload)
            workflow["admission"]["repository"] = baseline_gate.repository_evidence(baseline)
            (root / workflow["baseline_ref"]).write_bytes(baseline_payload)
            (root / "workflow.json").write_text(
                json.dumps(workflow, sort_keys=True, separators=(",", ":")) + "\n"
            )
            first_task = first["tasks"][0]
            first_task["work_mode"] = "write"
            first_task["write_roots"] = ["server/contract.txt"]
            first_task["input_sha256"] = {workflow["baseline_ref"]: digest(baseline_payload)}
            first["predecessor_sha256"] = digest(baseline_payload)
            self.bind_writer_schema(root, first_task)
            first_phase = prepare_isolated_phase(
                root,
                repository,
                first,
                read_roots=(".",),
                admission_baseline=baseline,
                predecessor_sha256=first["predecessor_sha256"],
            )

            def execution(task: dict[str, object], path: str, value: str) -> RawExecution:
                target = Path(task["_runtime_worker_root"]) / path
                target.write_text(value)
                session = f"session-{task['task_id']}"
                output = {"answer": "written", "changed_paths": [path]}
                return RawExecution(
                    exit_code=0,
                    events=[
                        {"type": "thread.started", "thread_id": session},
                        {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(output)}},
                        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}},
                    ],
                    stderr="",
                    turn_context={"model": "gpt-5.6-terra", "effort": "xhigh", "session_id": session},
                )

            first_summary = run_read_only_phase(
                root,
                first,
                lambda task, _packet: execution(task, "server/contract.txt", "server-v2\n"),
                max_parallel=1,
                source_phase=first_phase,
                runtime_task_overrides={
                    first_task["task_id"]: {"_runtime_worker_root": os.fspath(first_phase.tasks[first_task["task_id"]].root)}
                },
            )
            first_receipt = root / first_summary["receipt_ref"]
            self.assertEqual((repository / "server/contract.txt").read_text(), "server-v2\n")

            first_receipt_value = json.loads(first_receipt.read_bytes())
            first_results = {
                first_task["task_id"]: json.loads(
                    (root / first_receipt_value["task_result_refs"][0]).read_bytes()
                )
            }
            runtime_path = root / "runtime"
            foreign_runtime_path = root / "foreign-runtime"
            runtime_path.rename(foreign_runtime_path)
            runtime_path.symlink_to(foreign_runtime_path, target_is_directory=True)
            with self.assertRaisesRegex(ProtocolError, "artifact"):
                _validate_source_patch_replay(root, first, first_receipt_value, first_results)
            runtime_path.unlink()
            foreign_runtime_path.rename(runtime_path)

            claim_path = root / first_receipt_value["generation_claim_ref"]
            original_claim_payload = claim_path.read_bytes()
            forged_claim = json.loads(original_claim_payload)
            forged_claim["phase_id"] = "forged-authority"
            claim_path.write_text(
                json.dumps(forged_claim, sort_keys=True, separators=(",", ":")) + "\n"
            )

            second = deepcopy(first)
            second["phase_id"] = "002-ui"
            second["caused_by"] = [first["phase_id"]]
            second["predecessor_sha256"] = digest(first_receipt.read_bytes())
            second_task = second["tasks"][0]
            second_task.update(
                {
                    "task_id": "ui-types",
                    "lineage_id": "lineage-ui-types",
                    "criterion_id": "AC-2",
                    "write_roots": ["src/types.txt"],
                    "packet_path": "phases/002-ui/tasks/ui-types/packet.json",
                    "input_refs": [first_summary["receipt_ref"]],
                    "input_sha256": {first_summary["receipt_ref"]: digest(first_receipt.read_bytes())},
                }
            )
            packet = {
                "schema_version": "agent-workflow.task-packet.vnext.v1",
                "prompt": "Update UI types from the integrated server contract.",
                "output_schema_ref": "schemas/writer-output.json",
                "output_schema_sha256": digest((root / "schemas/writer-output.json").read_bytes()),
            }
            packet_payload = (json.dumps(packet, sort_keys=True, separators=(",", ":")) + "\n").encode()
            packet_path = root / second_task["packet_path"]
            packet_path.parent.mkdir(parents=True)
            packet_path.write_bytes(packet_payload)
            second_task["packet_sha256"] = digest(packet_payload)
            with self.assertRaisesRegex(SourceWriteError, "generation claim"):
                prepare_isolated_phase(
                    root,
                    repository,
                    second,
                    read_roots=(".",),
                    admission_baseline=baseline,
                    predecessor_sha256=second["predecessor_sha256"],
                )
            claim_path.write_bytes(original_claim_payload)

            phases_path = root / "phases"
            real_phases_path = root / "foreign-phases"
            phases_path.rename(real_phases_path)
            phases_path.symlink_to(real_phases_path, target_is_directory=True)
            with self.assertRaisesRegex(SourceWriteError, "unsafe"):
                prepare_isolated_phase(
                    root,
                    repository,
                    second,
                    read_roots=(".",),
                    admission_baseline=baseline,
                    predecessor_sha256=second["predecessor_sha256"],
                )
            phases_path.unlink()
            real_phases_path.rename(phases_path)

            (repository / "server/contract.txt").write_text("external-cross-anchor\n")
            with self.assertRaisesRegex(IntegrationConflict, "external drift"):
                prepare_isolated_phase(
                    root,
                    repository,
                    second,
                    read_roots=(".",),
                    admission_baseline=baseline,
                    predecessor_sha256=second["predecessor_sha256"],
                )
            shutil.rmtree(root / "runtime/source-workspaces/002-ui")
            (repository / "server/contract.txt").write_text("server-v2\n")
            second_phase = prepare_isolated_phase(
                root,
                repository,
                second,
                read_roots=(".",),
                admission_baseline=baseline,
                predecessor_sha256=second["predecessor_sha256"],
            )
            second_workspace = second_phase.tasks["ui-types"].root
            self.assertEqual((second_workspace / "server/contract.txt").read_text(), "server-v2\n")
            second_summary = run_read_only_phase(
                root,
                second,
                lambda task, _packet: execution(task, "src/types.txt", "types-v2\n"),
                max_parallel=1,
                source_phase=second_phase,
                runtime_task_overrides={"ui-types": {"_runtime_worker_root": os.fspath(second_workspace)}},
            )
            second_receipt = root / second_summary["receipt_ref"]
            self.assertEqual((repository / "src/types.txt").read_text(), "types-v2\n")

            third = deepcopy(second)
            third["phase_id"] = "003-ui-repair"
            third["caused_by"] = [second["phase_id"]]
            third["predecessor_sha256"] = digest(second_receipt.read_bytes())
            third_task = third["tasks"][0]
            third_task.update(
                {
                    "task_id": "ui-repair",
                    "lineage_id": "lineage-ui-repair",
                    "criterion_id": "AC-3",
                    "write_roots": ["src/App.txt"],
                    "packet_path": "phases/003-ui-repair/tasks/ui-repair/packet.json",
                    "input_refs": [second_summary["receipt_ref"]],
                    "input_sha256": {second_summary["receipt_ref"]: digest(second_receipt.read_bytes())},
                }
            )
            third_packet_path = root / third_task["packet_path"]
            third_packet_path.parent.mkdir(parents=True)
            third_packet_path.write_bytes(packet_payload)
            third_task["packet_sha256"] = digest(packet_payload)
            third_phase = prepare_isolated_phase(
                root,
                repository,
                third,
                read_roots=(".",),
                admission_baseline=baseline,
                predecessor_sha256=third["predecessor_sha256"],
            )
            self.assertEqual((third_phase.tasks["ui-repair"].root / "src/types.txt").read_text(), "types-v2\n")
            third_workspace = third_phase.tasks["ui-repair"].root
            third_summary = run_read_only_phase(
                root,
                third,
                lambda task, _packet: execution(task, "src/App.txt", "app-v2\n"),
                max_parallel=1,
                source_phase=third_phase,
                runtime_task_overrides={
                    "ui-repair": {"_runtime_worker_root": os.fspath(third_workspace)}
                },
            )
            third_receipt = root / third_summary["receipt_ref"]
            self.assertEqual((repository / "src/types.txt").read_text(), "types-v2\n")
            self.assertEqual((repository / "src/App.txt").read_text(), "app-v2\n")

            (repository / "src/types.txt").write_text("external-edit\n")
            fourth = deepcopy(third)
            fourth["phase_id"] = "004-ui-external-drift"
            fourth["caused_by"] = [third["phase_id"]]
            fourth["predecessor_sha256"] = digest(third_receipt.read_bytes())
            fourth["tasks"][0]["task_id"] = "ui-external-drift"
            fourth["tasks"][0]["lineage_id"] = "lineage-ui-external-drift"
            fourth["tasks"][0]["packet_path"] = "phases/004-ui-external-drift/tasks/ui-external-drift/packet.json"
            fourth_packet_path = root / fourth["tasks"][0]["packet_path"]
            fourth_packet_path.parent.mkdir(parents=True)
            fourth_packet_path.write_bytes(packet_payload)
            fourth["tasks"][0]["packet_sha256"] = digest(packet_payload)
            with self.assertRaisesRegex(IntegrationConflict, "external drift"):
                prepare_isolated_phase(
                    root,
                    repository,
                    fourth,
                    read_roots=(".",),
                    admission_baseline=baseline,
                    predecessor_sha256=fourth["predecessor_sha256"],
                )
            (repository / "src/types.txt").write_text("types-v2\n")
            (root / "phases/002-ui/integration.patch.json").write_text("{}\n")
            fifth = deepcopy(fourth)
            fifth["phase_id"] = "005-tampered-source-head"
            fifth["tasks"][0]["task_id"] = "tampered-source-head"
            fifth["tasks"][0]["lineage_id"] = "lineage-tampered-source-head"
            fifth["tasks"][0]["packet_path"] = "phases/005-tampered-source-head/tasks/tampered-source-head/packet.json"
            fifth_packet_path = root / fifth["tasks"][0]["packet_path"]
            fifth_packet_path.parent.mkdir(parents=True)
            fifth_packet_path.write_bytes(packet_payload)
            fifth["tasks"][0]["packet_sha256"] = digest(packet_payload)
            with self.assertRaisesRegex(SourceWriteError, "integration replay failed"):
                prepare_isolated_phase(
                    root,
                    repository,
                    fifth,
                    read_roots=(".",),
                    admission_baseline=baseline,
                    predecessor_sha256=fifth["predecessor_sha256"],
                )

    def test_source_writer_declared_paths_must_equal_the_host_patch(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repository = Path(raw) / "repo"
            repository.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.email", "fixture@example.com"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.name", "Fixture"], cwd=repository, check=True)
            (repository / "src").mkdir()
            target = repository / "src/value.txt"
            target.write_text("base\n")
            subprocess.run(["git", "add", "."], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repository, check=True)
            baseline = self.source_baseline(repository)
            baseline_payload = baseline_gate._canonical(baseline)
            root = repository / ".workflow/run"
            root.mkdir(parents=True)
            plan = self.build_run(root, task_count=1)
            workflow = json.loads((root / "workflow.json").read_text())
            workflow["admission"]["profile"] = "source_write"
            workflow["admission"]["capabilities"]["sandbox_isolation"]["status"] = "pass"
            workflow["baseline_sha256"] = digest(baseline_payload)
            workflow["admission"]["repository"] = baseline_gate.repository_evidence(baseline)
            (root / workflow["baseline_ref"]).write_bytes(baseline_payload)
            (root / "workflow.json").write_text(json.dumps(workflow, sort_keys=True, separators=(",", ":")) + "\n")
            task = plan["tasks"][0]
            task["work_mode"] = "write"
            task["write_roots"] = ["src/value.txt"]
            task["input_sha256"] = {workflow["baseline_ref"]: digest(baseline_payload)}
            plan["predecessor_sha256"] = digest(baseline_payload)
            self.bind_writer_schema(root, task)
            phase = prepare_isolated_phase(
                root,
                repository,
                plan,
                admission_baseline=baseline,
                predecessor_sha256=plan["predecessor_sha256"],
            )

            def execute(runtime_task: dict[str, object], _packet: dict[str, object]) -> RawExecution:
                (Path(runtime_task["_runtime_worker_root"]) / "src/value.txt").write_text("worker\n")
                session = "declared-path-mismatch"
                return RawExecution(
                    exit_code=0,
                    events=[
                        {"type": "thread.started", "thread_id": session},
                        {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps({"answer": "blocked", "changed_paths": []})}},
                        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}},
                    ],
                    stderr="",
                    turn_context={"model": "gpt-5.6-terra", "effort": "xhigh", "session_id": session},
                )

            with self.assertRaisesRegex(RuntimeFailure, "changed_paths differ from host patch"):
                run_read_only_phase(
                    root,
                    plan,
                    execute,
                    max_parallel=1,
                    source_phase=phase,
                    runtime_task_overrides={task["task_id"]: {"_runtime_worker_root": os.fspath(phase.tasks[task["task_id"]].root)}},
                )
            self.assertEqual(target.read_text(), "base\n")
            self.assertFalse((root / "phases/001-research/integration.patch.json").exists())

    def test_turn_failed_beats_exit_zero_and_route_mismatch_is_typed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plan = self.build_run(root, task_count=1)

            def failed(_task: dict[str, object], _packet: dict[str, object]) -> RawExecution:
                return RawExecution(
                    exit_code=0,
                    events=[
                        {"type": "thread.started", "thread_id": "failed-session"},
                        {"type": "turn.failed", "error": {"message": "fixture failure"}},
                    ],
                    stderr="",
                    turn_context={"model": "gpt-5.6-terra", "effort": "xhigh", "session_id": "failed-session"},
                )

            summary = run_read_only_phase(root, plan, failed, max_parallel=1)
            self.assertEqual(summary["status"], "failed")
            result = json.loads((root / "phases/001-research/tasks/research-1/result.json").read_text())
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["terminal_reason"], "codex_turn_failed")

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plan = self.build_run(root, task_count=1)

            def wrong_route(_task: dict[str, object], _packet: dict[str, object]) -> RawExecution:
                return RawExecution(
                    exit_code=0,
                    events=[
                        {"type": "thread.started", "thread_id": "wrong-route"},
                        {"type": "item.completed", "item": {"type": "agent_message", "text": '{"answer":"ok"}'}},
                        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}},
                    ],
                    stderr="",
                    turn_context={"model": "gpt-5.6-sol", "effort": "xhigh", "session_id": "wrong-route"},
                )

            summary = run_read_only_phase(root, plan, wrong_route, max_parallel=1)
            self.assertEqual(summary["status"], "failed")
            result = json.loads((root / "phases/001-research/tasks/research-1/result.json").read_text())
            self.assertEqual(result["status"], "route_attestation_failed")
            self.assertEqual(result["terminal_reason"], "route_mismatch")

    def test_adapter_failure_is_runner_error_not_false_attestation_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plan = self.build_run(root, task_count=1)

            def broken(_task: dict[str, object], _packet: dict[str, object]) -> RawExecution:
                raise OSError("fixture adapter failure")

            summary = run_read_only_phase(root, plan, broken, max_parallel=1)
            self.assertEqual(summary["status"], "failed")
            result = json.loads(
                (root / "phases/001-research/tasks/research-1/result.json").read_text()
            )
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["terminal_reason"], "runner_error")
            self.assertIsNone(result["actual_route"])

    def test_terminal_fence_runs_after_workers_and_before_results_or_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plan = self.build_run(root, task_count=1)

            def execute(task: dict[str, object], packet: dict[str, object]) -> RawExecution:
                session_id = f"session-{task['task_id']}"
                return RawExecution(
                    exit_code=0,
                    events=[
                        {"type": "thread.started", "thread_id": session_id},
                        {
                            "type": "item.completed",
                            "item": {
                                "type": "agent_message",
                                "text": json.dumps({"answer": packet["prompt"]}),
                            },
                        },
                        {
                            "type": "turn.completed",
                            "usage": {"input_tokens": 100, "output_tokens": 10},
                        },
                    ],
                    stderr="",
                    turn_context={
                        "model": "gpt-5.6-terra",
                        "effort": "xhigh",
                        "session_id": session_id,
                    },
                )

            def reject_drift() -> None:
                raise RuntimeFailure("terminal repository fence drifted")

            with self.assertRaisesRegex(RuntimeFailure, "terminal repository fence"):
                run_read_only_phase(
                    root,
                    plan,
                    execute,
                    max_parallel=1,
                    terminal_fence=reject_drift,
                )
            self.assertFalse((root / "phases/001-research/receipt.json").exists())
            self.assertFalse(
                (root / "phases/001-research/tasks/research-1/result.json").exists()
            )

    def test_log_drainer_caps_durable_bytes_without_backpressure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "bounded.log"
            outcome: dict[str, object] = {}
            _drain_stream(io.BytesIO(b"0123456789abcdef"), path, 8, outcome)
            self.assertEqual(path.read_bytes(), b"01234567")
            self.assertEqual(outcome["seen"], 16)
            self.assertTrue(outcome["overflow"])
            self.assertIsNone(outcome["error"])

    def test_log_drainer_caps_parsed_jsonl_memory_to_durable_limit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "bounded-events.jsonl"
            outcome: dict[str, object] = {}
            payload = b'{"x":1}\n' * 10000
            _drain_stream(io.BytesIO(payload), path, 128, outcome)
            self.assertEqual(outcome["seen"], len(payload))
            self.assertTrue(outcome["overflow"])
            self.assertLessEqual(len(outcome["events"]), 16)
            self.assertLessEqual(path.stat().st_size, 128)

    def test_log_drainer_caps_event_object_count_even_within_durable_limit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "bounded-event-count.jsonl"
            outcome: dict[str, object] = {}
            payload = b'{"type":"tick"}\n' * 10000
            _drain_stream(io.BytesIO(payload), path, len(payload), outcome)
            self.assertEqual(path.read_bytes(), payload)
            self.assertTrue(outcome["event_overflow"])
            self.assertTrue(outcome["overflow"])
            self.assertLessEqual(len(outcome["events"]), 4096)

    def test_codex_adapter_builds_isolated_route_and_reads_persisted_context(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            codex_home = root / "codex-home"
            codex_home.mkdir()
            schema_path = root / "schemas" / "worker-output.json"
            schema_path.parent.mkdir()
            schema_path.write_text(
                json.dumps(
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["answer"],
                        "properties": {"answer": {"type": "string"}},
                    }
                )
            )
            fake = root / "fake-codex"
            fake.write_text(
                """#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
model = args[args.index('-m') + 1]
effort_arg = next(item for item in args if item.startswith('model_reasoning_effort='))
effort = effort_arg.split('=', 1)[1].strip('\\\"')
thread_id = 'fake-adapter-thread'
session = pathlib.Path(os.environ['CODEX_HOME']) / 'sessions' / 'fake.jsonl'
session.parent.mkdir(parents=True)
events = [
    {'type': 'session_meta', 'payload': {'id': thread_id}},
    {'type': 'turn_context', 'payload': {
        'model': model,
        'effort': effort,
        'workspace_roots': [str(pathlib.Path.cwd())],
        'sandbox_policy': {'type': 'read-only'},
        'permission_profile': {
            'type': 'managed',
            'file_system': {
                'type': 'restricted',
                'entries': [
                    {'path': {'type': 'special', 'value': {'kind': 'minimal'}}, 'access': 'read'},
                    {'path': {'type': 'path', 'path': str(pathlib.Path.cwd())}, 'access': 'read'},
                    {'path': {'type': 'path', 'path': str(pathlib.Path(os.environ['CODEX_HOME']) / 'tmp' / 'arg0' / 'codex-arg0fixture')}, 'access': 'read'},
                ],
            },
            'network': 'restricted',
        },
    }},
]
session.write_text(''.join(json.dumps(item) + '\\n' for item in events))
print(json.dumps({'type': 'thread.started', 'thread_id': thread_id}))
print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '{\\\"answer\\\":\\\"adapter-ok\\\"}'}}))
print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 7, 'output_tokens': 3}}))
print(json.dumps(args), file=sys.stderr)
"""
            )
            fake.chmod(0o755)
            config = CodexExecConfig(
                run_root=root,
                repo_root=root,
                codex_home=codex_home,
                codex_binary=os.fspath(fake),
                log_limit_bytes=4096,
            )
            execute = codex_task_executor(config)
            observed = execute(
                {
                    "task_id": "adapter-probe",
                    "role": "worker",
                    "execution_deadline_seconds": 5,
                    **self.direct_runtime_fence(root),
                },
                {"output_schema_ref": "schemas/worker-output.json", "prompt": "bounded"},
            )
            self.assertEqual(observed.exit_code, 0)
            self.assertFalse(observed.adapter_error)
            self.assertFalse(observed.log_limit_exceeded)
            self.assertEqual(
                observed.turn_context,
                {
                    "model": "gpt-5.6-terra",
                    "effort": "xhigh",
                    "session_id": "fake-adapter-thread",
                    "workspace_roots": [str(root.resolve())],
                    "sandbox_policy": {"type": "read-only"},
                    "permission_profile": {
                        "type": "managed",
                        "file_system": {
                            "type": "restricted",
                            "entries": [
                                {
                                    "path": {
                                        "type": "special",
                                        "value": {"kind": "minimal"},
                                    },
                                    "access": "read",
                                },
                                {
                                    "path": {"type": "path", "path": str(root.resolve())},
                                    "access": "read",
                                },
                                {
                                    "path": {
                                        "type": "path",
                                        "path": str(
                                            (codex_home / "tmp" / "arg0" / "codex-arg0fixture").resolve()
                                        ),
                                    },
                                    "access": "read",
                                },
                            ],
                        },
                        "network": "restricted",
                    },
                },
            )
            self.assertEqual(
                [event["type"] for event in observed.events],
                ["thread.started", "item.completed", "turn.completed"],
            )
            self.assertNotIn('"--ignore-user-config"', observed.stderr)
            self.assertNotIn('"--sandbox"', observed.stderr)
            self.assertIn('developer_instructions=', observed.stderr)

    def test_invalid_typed_output_and_stale_phase_fences_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plan = self.build_run(root, task_count=1)

            def invalid(_task: dict[str, object], _packet: dict[str, object]) -> RawExecution:
                return RawExecution(
                    exit_code=0,
                    events=[
                        {"type": "thread.started", "thread_id": "invalid-output"},
                        {
                            "type": "item.completed",
                            "item": {"type": "agent_message", "text": '{"wrong":123}'},
                        },
                        {
                            "type": "turn.completed",
                            "usage": {"input_tokens": 10, "output_tokens": 2},
                        },
                    ],
                    stderr="",
                    turn_context={
                        "model": "gpt-5.6-terra",
                        "effort": "xhigh",
                        "session_id": "invalid-output",
                    },
                )

            summary = run_read_only_phase(root, plan, invalid, max_parallel=1)
            self.assertEqual(summary["status"], "failed")
            result = json.loads(
                (root / "phases/001-research/tasks/research-1/result.json").read_text()
            )
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["terminal_reason"], "invalid_typed_output")

        for field, value, message in (
            ("authority_revision", 999, "authority revision"),
            ("generation_id", "generation-stale", "generation-001"),
            ("phase_id", "002-repeat", "001-"),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                plan = self.build_run(root, task_count=1)
                plan[field] = value
                with self.assertRaisesRegex(RuntimeFailure, message):
                    run_read_only_phase(root, plan, invalid, max_parallel=1)

    def test_cancel_signals_active_group_and_terminalizes_queued_task(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plan = self.build_run(root, task_count=2)
            fake = root / "fake-sleeper"
            fake.write_text(
                """#!/usr/bin/env python3
import json, signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print(json.dumps({'type': 'thread.started', 'thread_id': 'cancelled-worker'}), flush=True)
while True:
    time.sleep(0.1)
"""
            )
            fake.chmod(0o755)
            config = CodexExecConfig(
                run_root=root,
                repo_root=root,
                codex_home=root / "codex-home",
                codex_binary=os.fspath(fake),
                workflow_id="fixture-workflow",
                authority_revision=1,
                terminate_grace_seconds=0.1,
            )
            observed: dict[str, object] = {}

            def run() -> None:
                observed.update(
                    run_read_only_phase(
                        root,
                        plan,
                        codex_task_executor(config),
                        max_parallel=1,
                    )
                )

            thread = threading.Thread(target=run)
            thread.start()
            record = root / "runtime" / "processes" / "001-research" / "research-1.json"
            deadline = time.monotonic() + 3
            while not record.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(record.exists())
            cancelled = cancel_run(root, 1, grace_seconds=0.1)
            self.assertEqual(cancelled["status"], "cancel_requested")
            self.assertEqual(cancelled["signalled_tasks"], ["research-1"])
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(observed["status"], "cancelled")
            receipt = json.loads((root / observed["receipt_ref"]).read_text())
            self.assertEqual(receipt["task_counts"]["cancelled"], 2)
            self.assertEqual(receipt["terminal_reason"], "phase_cancelled")
            self.assertTrue(record.is_file())

    def test_cancel_rejects_forged_pgid_before_signalling(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workflow = fixture("workflow.json")
            create_once_json(root, "workflow.json", workflow)
            identity = _process_identity(os.getpid())
            self.assertIsNotNone(identity)
            create_once_json(
                root,
                "runtime/processes/forged.json",
                {
                    "workflow_id": workflow["workflow_id"],
                    "authority_revision": 1,
                    "task_id": "forged",
                    "pid": os.getpid(),
                    "pgid": os.getpid() + 1,
                    "audit_marker": f"agent-workflow:{workflow['workflow_id']}:forged:fixture",
                    "process_identity": identity,
                    "command": ["/fixture/codex"],
                    "command_sha256": "sha256:" + "1" * 64,
                },
            )
            with mock.patch("workflow_runtime.os.killpg") as killpg:
                with self.assertRaisesRegex(RuntimeFailure, "ownership proof"):
                    cancel_run(root, 1)
                killpg.assert_not_called()

    def test_cancel_rejects_record_without_live_unforgeable_marker(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workflow = fixture("workflow.json")
            create_once_json(root, "workflow.json", workflow)
            process = subprocess.Popen(["/bin/sleep", "5"], start_new_session=True)
            try:
                identity = _process_identity(process.pid)
                self.assertIsNotNone(identity)
                marker = (
                    f"agent-workflow:{workflow['workflow_id']}:forged-live:fixture"
                )
                command = [
                    "/fixture/codex",
                    "-c",
                    f'agent_workflow_audit_marker="{marker}"',
                ]
                command_payload = (
                    json.dumps(command, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    + "\n"
                ).encode()
                create_once_json(
                    root,
                    "runtime/processes/forged-live.json",
                    {
                        "workflow_id": workflow["workflow_id"],
                        "authority_revision": 1,
                        "task_id": "forged-live",
                        "pid": process.pid,
                        "pgid": process.pid,
                        "audit_marker": marker,
                        "process_identity": identity,
                        "command": command,
                        "command_sha256": digest(command_payload),
                    },
                )
                with self.assertRaisesRegex(RuntimeFailure, "live marker ownership"):
                    cancel_run(root, 1)
                self.assertIsNone(process.poll())
            finally:
                process.terminate()
                process.wait(timeout=3)

    def test_phase_deadline_prevents_queued_launch_and_seals_timeout_event(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plan = self.build_run(root, task_count=2)
            plan["phase_budget_seconds"] = 1
            for task in plan["tasks"]:
                task["execution_deadline_seconds"] = 1
            fake = root / "fake-timeout"
            fake.write_text(
                """#!/usr/bin/env python3
import json, signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print(json.dumps({'type': 'thread.started', 'thread_id': 'timeout-worker'}), flush=True)
while True:
    time.sleep(0.1)
"""
            )
            fake.chmod(0o755)
            execute = codex_task_executor(
                CodexExecConfig(
                    run_root=root,
                    repo_root=root,
                    codex_home=root / "codex-home",
                    codex_binary=os.fspath(fake),
                    workflow_id="fixture-workflow",
                    authority_revision=1,
                    terminate_grace_seconds=0.1,
                )
            )
            summary = run_read_only_phase(root, plan, execute, max_parallel=1)
            self.assertEqual(summary["status"], "failed")
            receipt = json.loads((root / summary["receipt_ref"]).read_text())
            self.assertEqual(receipt["task_counts"]["timed_out"], 1)
            self.assertEqual(receipt["task_counts"]["not_started_deadline"], 1)
            events = (
                root
                / "phases/001-research/tasks/research-1/attempts/001/events.jsonl"
            ).read_text()
            self.assertIn('"type":"runtime.timeout"', events)

    def test_admission_binds_runtime_baseline_and_capability_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            worker_root = root / "src"
            worker_root.mkdir()
            source_codex_home = root / "source-codex-home"
            workflow = fixture("workflow.json")
            empty = baseline_gate._packed(b"")
            baseline = {
                "schema_version": "agent-workflow.vnext-replayable-baseline.v2",
                "baseline_kind": "pre_slice",
                "head": workflow["admission"]["repository"]["head"],
                "branch": workflow["admission"]["repository"]["branch"],
                "environment": {
                    "codex_cli": "codex-cli test",
                    "platform": "test",
                    "model": "test-model",
                    "reasoning_effort": "xhigh",
                },
                "selection": {
                    "tracked_excludes": [],
                    "untracked_mode": "explicit",
                    "untracked_paths": [],
                },
                "staged_patch": empty,
                "unstaged_patch": empty,
                "staged_binary_patch": empty,
                "unstaged_binary_patch": empty,
                "untracked": [],
                "relevant_files": [],
                "parent_summary": {
                    "schema_version": "agent-workflow.vnext-pre-slice-baseline.v1",
                    "summary_sha256": "sha256:" + "1" * 64,
                    "head": workflow["admission"]["repository"]["head"],
                    "branch": workflow["admission"]["repository"]["branch"],
                },
                "candidate_parent": None,
                "intended_changes": [],
                "immutability": "create_once_do_not_rewrite",
            }
            baseline["seal_sha256"] = baseline_gate._seal(baseline)
            baseline_bytes = baseline_gate._canonical(baseline)
            baseline_path = root / "evidence" / "baseline.json"
            baseline_path.parent.mkdir(parents=True)
            baseline_path.write_bytes(baseline_bytes)
            statuses = {
                name: "unavailable" if name == "sandbox_isolation" else "pass"
                for name in workflow["admission"]["capabilities"]
            }
            artifacts: dict[str, tuple[str, str]] = {}

            def write_probe(name: str, value: object, kind: str) -> None:
                path = root / "evidence" / name
                payload = (
                    value
                    if isinstance(value, bytes)
                    else (json.dumps(value, sort_keys=True) + "\n").encode()
                )
                path.write_bytes(payload)
                artifacts[name] = (kind, digest(payload))

            observed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            runner_probe = {
                "schema_version": "agent-workflow.runner-evidence.vnext.slice1.v2",
                "runtime_bundle_sha256": _runtime_bundle_sha256(),
                "execution_scope": {"target_eligible": True},
                "orchestrator_session": {"session_id": "orchestrator-fixture"},
                "completion_density": {
                    "actual_orchestrator_completions": 2,
                    "forbidden_polling_or_wrapper_wakes": 0,
                },
                "security": {
                    "all_permission_profiles_exact": True,
                    "all_routes_attested": True,
                },
                "p1_repairs": {
                    "repository_runtime_and_codex_terminal_fence_passed": True,
                },
                "parallelism": {"all_tasks_started_at": observed_at},
            }
            main_probe = {
                "schema_version": "agent-workflow.main-delivery-audit.vnext.slice1.v1",
                "workflow_id": "fixture-source",
                "main_session": {"session_id": "main-fixture"},
                "child_session": {"session_id": "orchestrator-fixture"},
                "delivery": {"matching_child_terminal_callbacks_received": 1},
            }
            context_profile = {
                "type": "managed",
                "network": "restricted",
                "file_system": {
                    "type": "restricted",
                    "entries": [
                        {
                            "path": {
                                "type": "special",
                                "value": {"kind": "minimal"},
                            },
                            "access": "read",
                        },
                        {
                            "path": {"type": "path", "path": os.fspath(worker_root.resolve())},
                            "access": "read",
                        },
                        {
                            "path": {
                                "type": "path",
                                "path": os.fspath(
                                    source_codex_home.resolve() / "tmp/arg0/codex-arg0fixture"
                                ),
                            },
                            "access": "read",
                        },
                    ],
                },
            }
            write_probe("runner-probe.json", runner_probe, "runner_evidence")
            write_probe("main-probe.json", main_probe, "main_delivery_audit")
            write_probe(
                "worker-context.json",
                {
                    "model": "gpt-5.6-terra",
                    "effort": "xhigh",
                    "session_id": "worker-session",
                    "workspace_roots": [os.fspath(worker_root.resolve())],
                    "sandbox_policy": {"type": "read-only"},
                    "permission_profile": context_profile,
                },
                "turn_context",
            )
            write_probe(
                "top-context.json",
                {
                    "model": "gpt-5.6-sol",
                    "effort": "xhigh",
                    "session_id": "top-session",
                    "workspace_roots": [os.fspath(worker_root.resolve())],
                    "sandbox_policy": {"type": "read-only"},
                    "permission_profile": context_profile,
                },
                "turn_context",
            )
            write_probe(
                "denial-output.json",
                {"answer": "workspace=read;transient=denied;source=denied"},
                "typed_output",
            )
            test_names = [
                "test_cancel_rejects_record_without_live_unforgeable_marker",
                "test_cancel_signals_active_group_and_terminalizes_queued_task",
                "test_log_drainer_caps_event_object_count_even_within_durable_limit",
                "test_terminal_fence_runs_after_workers_and_before_results_or_receipt",
            ]
            write_probe(
                "focused-tests.txt",
                ("\n".join(f"{name} ... ok" for name in test_names) + "\n\nOK\n").encode(),
                "focused_test_report",
            )
            write_probe(
                "source-codex.json",
                {
                    "schema_version": "agent-workflow.slice0b-capability-summary.v2",
                    "codex_cli_version": "codex-cli fixture",
                    "codex_binary_sha256": "sha256:" + "2" * 64,
                },
                "source_codex_identity",
            )

            def proof(name: str) -> dict[str, str]:
                kind, sha256 = artifacts[name]
                return {
                    "kind": kind,
                    "evidence_ref": f"evidence/{name}",
                    "evidence_sha256": sha256,
                }

            runner_entry = proof("runner-probe.json")
            context_entries = [proof("worker-context.json"), proof("top-context.json")]
            provenance = {
                "producer": {
                    "name": "agent-workflow-slice1-probe",
                    "validator_runtime_bundle_sha256": _runtime_bundle_sha256(),
                    "source_runtime_bundle_sha256": _runtime_bundle_sha256(),
                    "codex_cli_version": "codex-cli fixture",
                    "codex_binary_sha256": "sha256:" + "2" * 64,
                },
                "observed_at": observed_at,
                "source_run": {
                    "workflow_id": "fixture-source",
                    "orchestrator_session_id": "orchestrator-fixture",
                    "main_session_id": "main-fixture",
                    "codex_home": os.fspath(source_codex_home.resolve()),
                    "worker_session_ids": ["worker-session"],
                    "top_session_ids": ["top-session"],
                },
                "source_codex_identity": {
                    "evidence_ref": "evidence/source-codex.json",
                    "evidence_sha256": artifacts["source-codex.json"][1],
                },
                "proof_artifacts": {
                    "blocking_wait": [runner_entry],
                    "read_only_containment": [
                        runner_entry,
                        *context_entries,
                        proof("denial-output.json"),
                    ],
                    "route_attestation": [runner_entry, *context_entries],
                    "sandbox_isolation": [runner_entry],
                    "cancel_reap": [proof("focused-tests.txt")],
                    "raw_session_audit": [runner_entry, proof("main-probe.json")],
                    "accounting_evidence": [runner_entry],
                    "generation_fence": [runner_entry, proof("focused-tests.txt")],
                },
            }
            receipt = capability_receipt(statuses, provenance)
            receipt_bytes = (json.dumps(receipt, sort_keys=True) + "\n").encode()
            receipt_path = root / "evidence" / "capability-receipt.json"
            receipt_path.write_bytes(receipt_bytes)
            capability = {
                "schema_version": "agent-workflow.slice0b-capability-summary.v2",
                "observed_at": "2026-07-12T00:00:00Z",
                "codex_cli_version": "codex-cli fixture",
                "codex_binary_sha256": "sha256:" + "2" * 64,
                "capabilities": {
                    name: {
                        "status": status,
                        "evidence_ref": "evidence/capability-receipt.json",
                        "evidence_sha256": digest(receipt_bytes),
                    }
                    for name, status in statuses.items()
                },
            }
            capability_bytes = (json.dumps(capability, sort_keys=True) + "\n").encode()
            capability_path = root / "evidence" / "capabilities.json"
            capability_path.write_bytes(capability_bytes)
            workflow["baseline_ref"] = "evidence/baseline.json"
            workflow["baseline_sha256"] = digest(baseline_bytes)
            workflow["admission"]["repository"] = baseline_gate.repository_evidence(baseline)
            workflow["runtime_bundle"]["sha256"] = _runtime_bundle_sha256()
            for value in workflow["admission"]["capabilities"].values():
                value["evidence_ref"] = "evidence/capabilities.json"
                value["evidence_sha256"] = digest(capability_bytes)
            _validate_admission_inputs(
                root, workflow, require_host_snapshot_live_state=True
            )
            prefix_drift = deepcopy(capability)
            prefix_drift["capabilities"]["cancel_reap"]["status"] = (
                "pass_with_missing_terminal"
            )
            prefix_bytes = (json.dumps(prefix_drift, sort_keys=True) + "\n").encode()
            capability_path.write_bytes(prefix_bytes)
            for value in workflow["admission"]["capabilities"].values():
                value["evidence_sha256"] = digest(prefix_bytes)
            with self.assertRaisesRegex(RuntimeFailure, "contradicts declared status"):
                _validate_admission_inputs(
                    root, workflow, require_host_snapshot_live_state=True
                )
            capability_path.write_bytes(capability_bytes)
            for value in workflow["admission"]["capabilities"].values():
                value["evidence_sha256"] = digest(capability_bytes)
            incomplete_receipt = deepcopy(receipt)
            incomplete_receipt["blocking_transport"]["outer_yield_ms"] = 10000
            incomplete_bytes = (json.dumps(incomplete_receipt, sort_keys=True) + "\n").encode()
            receipt_path.write_bytes(incomplete_bytes)
            for item in capability["capabilities"].values():
                item["evidence_sha256"] = digest(incomplete_bytes)
            incomplete_capability_bytes = (
                json.dumps(capability, sort_keys=True) + "\n"
            ).encode()
            capability_path.write_bytes(incomplete_capability_bytes)
            for value in workflow["admission"]["capabilities"].values():
                value["evidence_sha256"] = digest(incomplete_capability_bytes)
            with self.assertRaisesRegex(RuntimeFailure, "blocking transport probe"):
                _validate_admission_inputs(
                    root, workflow, require_host_snapshot_live_state=True
                )
            receipt_path.write_bytes(receipt_bytes)
            capability_path.write_bytes(capability_bytes)
            for value in workflow["admission"]["capabilities"].values():
                value["evidence_sha256"] = digest(capability_bytes)
            capability_path.write_text("{}\n")
            with self.assertRaisesRegex(RuntimeFailure, "capability evidence"):
                _validate_admission_inputs(
                    root, workflow, require_host_snapshot_live_state=True
                )

    def test_source_write_capability_requires_profile_context_and_observed_denials(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            workspace = root / "probe-workspace"
            codex_home = root / "probe-codex-home"
            (workspace / "src/api").mkdir(parents=True)
            (codex_home / "tmp/arg0/codex-arg0fixture").mkdir(parents=True)
            profile = writer_profile_bytes(("src/api",))
            profile_path = root / "evidence/writer-profile.toml"
            profile_path.parent.mkdir()
            profile_path.write_bytes(profile)
            context = {
                "model": "gpt-5.6-terra",
                "effort": "xhigh",
                "session_id": "writer-session",
                "workspace_roots": [os.fspath(workspace)],
                "permission_profile": {
                    "type": "managed",
                    "network": "restricted",
                    "file_system": {
                        "type": "restricted",
                        "entries": [
                            {"path": {"type": "special", "value": {"kind": "minimal"}}, "access": "read"},
                            {"path": {"type": "path", "path": os.fspath(workspace)}, "access": "read"},
                            {"path": {"type": "path", "path": os.fspath(workspace / "src/api")}, "access": "write"},
                            {"path": {"type": "path", "path": os.fspath(codex_home / "tmp/arg0/codex-arg0fixture")}, "access": "read"},
                        ],
                    },
                },
            }
            context_payload = (json.dumps(context, sort_keys=True) + "\n").encode()
            context_path = root / "evidence/writer-context.json"
            context_path.write_bytes(context_payload)
            request = {
                "command": ["codex", "exec", "-p", "vnext-writer"],
                "cwd": os.fspath(workspace),
                "environment": {"CODEX_HOME": os.fspath(codex_home)},
            }
            request_payload = (json.dumps(request, sort_keys=True) + "\n").encode()
            (root / "evidence/supervisor-request.json").write_bytes(request_payload)
            events_payload = (
                b'{"type":"item.completed","item":{"type":"agent_message",'
                b'"text":"{\\\"answer\\\":\\\"ok\\\"}"}}\n'
                b'{"type":"turn.completed"}\n'
            )
            stderr_payload = b""
            (root / "evidence/supervisor-events.jsonl").write_bytes(events_payload)
            (root / "evidence/supervisor-stderr.log").write_bytes(stderr_payload)
            terminal = {
                "status": "completed",
                "request_sha256": digest(request_payload),
                "stdout_sha256": digest(events_payload),
                "stderr_sha256": digest(stderr_payload),
            }
            terminal_payload = (json.dumps(terminal, sort_keys=True) + "\n").encode()
            (root / "evidence/supervisor-terminal.json").write_bytes(terminal_payload)
            sandbox_command = ["codex", "sandbox", "-P", "vnext_writer", "/bin/true"]
            sandbox_stdout = b"allowed=0 git=1 sibling=1 control=1 credential=1 network=1\n"
            sandbox_stderr = b""
            (root / "evidence/sandbox.stdout").write_bytes(sandbox_stdout)
            (root / "evidence/sandbox.stderr").write_bytes(sandbox_stderr)
            report = {
                "schema_version": "agent-workflow.source-write-denial-probe.vnext.v1",
                "profile_sha256": digest(profile),
                "workspace_root": os.fspath(workspace),
                "command": sandbox_command,
                "command_sha256": digest(
                    (json.dumps(sandbox_command, sort_keys=True, separators=(",", ":")) + "\n").encode()
                ),
                "stdout_ref": "evidence/sandbox.stdout",
                "stdout_sha256": digest(sandbox_stdout),
                "stderr_ref": "evidence/sandbox.stderr",
                "stderr_sha256": digest(sandbox_stderr),
                "allowed_write_exit": 0,
                "git_write_exit": 1,
                "sibling_write_exit": 1,
                "control_read_exit": 1,
                "credential_read_exit": 1,
                "network_exit": 6,
            }
            report_payload = (json.dumps(report, sort_keys=True) + "\n").encode()
            report_path = root / "evidence/writer-denials.json"
            report_path.write_bytes(report_payload)
            evidence = {
                "schema_version": "agent-workflow.source-write-capability.vnext.v1",
                "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "producer": {
                    "name": "agent-workflow-slice3-writer-probe",
                    "runtime_bundle_sha256": _runtime_bundle_sha256(),
                    "codex_cli_version": "fixture-cli",
                    "codex_binary_sha256": "sha256:" + "9" * 64,
                },
                "workspace": {
                    "root": os.fspath(workspace),
                    "codex_home": os.fspath(codex_home),
                    "write_roots": ["src/api"],
                    "profile_ref": "evidence/writer-profile.toml",
                    "profile_sha256": digest(profile),
                    "turn_context_ref": "evidence/writer-context.json",
                    "turn_context_sha256": digest(context_payload),
                },
                "session": {
                    "id": "writer-session",
                    "model": "gpt-5.6-terra",
                    "reasoning_effort": "xhigh",
                },
                "supervisor": {
                    "request_ref": "evidence/supervisor-request.json",
                    "request_sha256": digest(request_payload),
                    "terminal_ref": "evidence/supervisor-terminal.json",
                    "terminal_sha256": digest(terminal_payload),
                    "events_ref": "evidence/supervisor-events.jsonl",
                    "events_sha256": digest(events_payload),
                    "stderr_ref": "evidence/supervisor-stderr.log",
                    "stderr_sha256": digest(stderr_payload),
                },
                "deterministic_probe": {
                    "evidence_ref": "evidence/writer-denials.json",
                    "evidence_sha256": digest(report_payload),
                },
                "environment": {
                    "inherit": ["AGENT_WORKFLOW_AUDIT_MARKER", "CODEX_HOME", "HOME", "LANG", "PATH", "TMPDIR"],
                    "plugins_disabled": True,
                    "mcp_disabled": True,
                    "network_enabled": False,
                },
            }
            evidence_payload = (json.dumps(evidence, sort_keys=True) + "\n").encode()
            evidence_path = root / "evidence/source-write-capability.json"
            evidence_path.write_bytes(evidence_payload)
            capability = {
                "status": "pass",
                "evidence_ref": "evidence/source-write-capability.json",
                "evidence_sha256": digest(evidence_payload),
            }
            with mock.patch("workflow_runtime.validate_supervisor_receipt"):
                _validate_source_write_capability(
                    root,
                    capability,
                    running_bundle=_runtime_bundle_sha256(),
                    codex_sha256="sha256:" + "9" * 64,
                    codex_version="fixture-cli",
                )
            context["permission_profile"]["file_system"]["entries"].append(
                {"path": {"type": "path", "path": os.fspath(root / "escape")}, "access": "write"}
            )
            drifted = (json.dumps(context, sort_keys=True) + "\n").encode()
            context_path.write_bytes(drifted)
            evidence["workspace"]["turn_context_sha256"] = digest(drifted)
            drifted_evidence = (json.dumps(evidence, sort_keys=True) + "\n").encode()
            evidence_path.write_bytes(drifted_evidence)
            capability["evidence_sha256"] = digest(drifted_evidence)
            with mock.patch("workflow_runtime.validate_supervisor_receipt"):
                with self.assertRaisesRegex(RuntimeFailure, "effective permission"):
                    _validate_source_write_capability(
                        root,
                        capability,
                        running_bundle=_runtime_bundle_sha256(),
                        codex_sha256="sha256:" + "9" * 64,
                        codex_version="fixture-cli",
                    )

    def test_external_log_flood_is_drained_and_capped(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            schema = root / "schemas" / "worker-output.json"
            schema.parent.mkdir()
            schema.write_text('{"type":"object"}\n')
            fake = root / "fake-flood"
            fake.write_text(
                """#!/usr/bin/env python3
import sys
sys.stdout.write('x' * 200000)
"""
            )
            fake.chmod(0o755)
            observed = codex_task_executor(
                CodexExecConfig(
                    run_root=root,
                    repo_root=root,
                    codex_home=root / "codex-home",
                    codex_binary=os.fspath(fake),
                    log_limit_bytes=1024,
                )
            )(
                {
                    "task_id": "flood",
                    "role": "worker",
                    "execution_deadline_seconds": 5,
                    **self.direct_runtime_fence(root),
                },
                {"output_schema_ref": "schemas/worker-output.json", "prompt": "bounded"},
            )
            self.assertTrue(observed.log_limit_exceeded)
            self.assertLess(len(observed.stdout_bytes or b""), 2048)
            self.assertTrue(
                any(event.get("type") == "runtime.log_limit_exceeded" for event in observed.events)
            )

    def test_parallel_waves_obey_one_two_and_four_capacity(self) -> None:
        for capacity in (1, 2, 4):
            with self.subTest(capacity=capacity), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                plan = self.build_run(root, task_count=4)
                active = 0
                maximum = 0
                lock = threading.Lock()

                def execute(task: dict[str, object], _packet: dict[str, object]) -> RawExecution:
                    nonlocal active, maximum
                    with lock:
                        active += 1
                        maximum = max(maximum, active)
                    time.sleep(0.03)
                    with lock:
                        active -= 1
                    model = "gpt-5.6-terra" if task["role"] == "worker" else "gpt-5.6-sol"
                    session = f"wave-{task['task_id']}"
                    return RawExecution(
                        exit_code=0,
                        events=[
                            {"type": "thread.started", "thread_id": session},
                            {
                                "type": "item.completed",
                                "item": {"type": "agent_message", "text": '{"answer":"ok"}'},
                            },
                            {
                                "type": "turn.completed",
                                "usage": {"input_tokens": 1, "output_tokens": 1},
                            },
                        ],
                        stderr="",
                        turn_context={"model": model, "effort": "xhigh", "session_id": session},
                    )

                summary = run_read_only_phase(root, plan, execute, max_parallel=capacity)
                self.assertEqual(summary["status"], "completed")
                self.assertEqual(maximum, capacity)

    def test_runtime_uses_least_privilege_profile_and_transient_auth_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            auth = root / "source-auth.json"
            auth.write_text('{"tokens":{"access_token":"fixture-access"}}\n')
            codex_home = _prepare_codex_home(root, auth)
            target = codex_home / "auth.json"
            self.assertTrue(target.is_file())
            self.assertEqual(target.stat().st_mode & 0o077, 0)
            profile = (codex_home / "vnext-read-only.config.toml").read_text()
            self.assertIn('":minimal" = "read"', profile)
            self.assertNotIn('extends = ":read-only"', profile)
            _cleanup_codex_auth(codex_home)
            self.assertFalse(target.exists())
            owned = _prepare_codex_home(root, auth, owner_id="generation-001-owner")
            self.assertEqual(
                owned,
                root.resolve() / "runtime/codex-homes/generation-001-owner",
            )
            self.assertTrue((owned / "vnext-read-only.config.toml").is_file())
            self.assertTrue((owned / "auth.json").is_file())
            _cleanup_codex_auth(owned)

    def test_recovery_executor_uses_pinned_app_server_resume_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            resume_session = "019f566c-4899-7d03-83a5-3e7043b74fcc"
            auth = root / "source-auth.json"
            auth.write_text('{"tokens":{"access_token":"fixture-access"}}\n')
            codex_home = _prepare_codex_home(
                root,
                auth,
                owner_id="failed-lineage-home",
            )
            schema = root / "schemas/worker-output.json"
            schema.parent.mkdir()
            schema.write_text(
                '{"type":"object","additionalProperties":false,'
                '"required":["answer"],"properties":{"answer":{"type":"string"}}}\n'
            )
            _seal_runtime_bundle(root)
            session = codex_home / "sessions/recovery.jsonl"
            session.parent.mkdir(parents=True)
            session.write_text(json.dumps({"type": "session_meta", "payload": {"id": resume_session}}) + "\n")
            prior_rollout_sha256 = digest(session.read_bytes())
            prior_rollout_size = session.stat().st_size
            fake = root / "fake-codex"
            fake.write_text(
                """#!/usr/bin/env python3
import json, os, pathlib, sys
thread_id = '019f566c-4899-7d03-83a5-3e7043b74fcc'
session = pathlib.Path(os.environ['CODEX_HOME']) / 'sessions' / 'recovery.jsonl'
for line in sys.stdin:
    request = json.loads(line)
    method = request.get('method')
    if method == 'initialize':
        print(json.dumps({'id': request['id'], 'result': {}}), flush=True)
    elif method == 'thread/resume':
        print(json.dumps({'id': request['id'], 'result': {'thread': {'id': thread_id}}}), flush=True)
    elif method == 'turn/start':
        params = request['params']
        turn_id = '019f5691-1e66-77f1-93c8-3fb7dab0218c'
        context = {
            'turn_id': turn_id,
            'cwd': params['cwd'],
            'model': params['model'],
            'effort': params['effort'],
            'workspace_roots': params['runtimeWorkspaceRoots'],
            'sandbox_policy': {'type': 'read-only'},
            'permission_profile': {
                'type': 'managed',
                'file_system': {'type': 'restricted', 'entries': [
                    {'path': {'type': 'special', 'value': {'kind': 'minimal'}}, 'access': 'read'},
                    {'path': {'type': 'path', 'path': params['cwd']}, 'access': 'read'},
                    {'path': {'type': 'path', 'path': str(pathlib.Path(os.environ['CODEX_HOME']) / 'tmp' / 'arg0' / 'codex-arg0fixture')}, 'access': 'read'},
                ]},
                'network': 'restricted',
            },
        }
        appended = [
            {'type': 'turn_context', 'payload': context},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': params['input'][0]['text']}]}},
            {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {'last_token_usage': {'input_tokens': 7, 'output_tokens': 3}}}},
            {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': turn_id, 'last_agent_message': '{\\\"answer\\\":\\\"recovered\\\"}'}},
        ]
        with session.open('a') as target:
            target.write(''.join(json.dumps(item) + '\\n' for item in appended))
        print(json.dumps({'id': request['id'], 'result': {'turn': {'id': turn_id}}}), flush=True)
        print(json.dumps({'method': 'turn/completed', 'params': {'turn': {'id': turn_id, 'status': 'completed'}}}), flush=True)
"""
            )
            fake.chmod(0o755)
            plan_path = create_once_json(root, "phases/001-unknown/plan.json", {"kind": "recovery-plan"})
            failed_path = create_once_json(root, "phases/001-failed/tasks/failed/result.json", {"kind": "failed-result"})
            causal_path = create_once_json(root, "phases/001-failed/receipt.json", {"kind": "causal-receipt"})
            runtime_fence = self.direct_runtime_fence(root)
            runtime_fence["_runtime_plan_sha256"] = digest(plan_path.read_bytes())
            observed = codex_task_executor(
                CodexExecConfig(
                    run_root=root,
                    repo_root=root,
                    codex_home=codex_home,
                    codex_binary=os.fspath(fake),
                )
            )(
                {
                    "task_id": "recovery-task",
                    "lineage_id": "lineage-recovery",
                    "role": "worker",
                    "work_mode": "read",
                    "write_roots": [],
                    "execution_deadline_seconds": 5,
                    "_runtime_resume_binding": {
                        "failed_result_ref": "phases/001-failed/tasks/failed/result.json",
                        "failed_result_sha256": digest(failed_path.read_bytes()),
                        "causal_receipt_ref": "phases/001-failed/receipt.json",
                        "causal_receipt_sha256": digest(causal_path.read_bytes()),
                        "session_id": resume_session,
                        "codex_home": os.fspath(codex_home),
                        "session_rollout_path": os.fspath(session),
                        "prior_rollout_sha256": prior_rollout_sha256,
                        "prior_rollout_size": prior_rollout_size,
                    },
                    **runtime_fence,
                },
                {"output_schema_ref": "schemas/worker-output.json", "prompt": "recover exact failure"},
            )
            self.assertEqual(observed.exit_code, 0)
            request = json.loads(
                (root / "runtime/watchdogs/001-unknown/recovery-task/request.json").read_text()
            )
            command = request["command"]
            adapter = root / "runtime-bundle/app_resume_adapter.py"
            self.assertEqual(command[:2], [os.fspath(Path(sys.executable).resolve()), os.fspath(adapter)])
            self.assertEqual(request["transport_executable_sha256"], digest(Path(sys.executable).resolve().read_bytes()))
            self.assertEqual(request["transport_adapter_sha256"], digest(adapter.read_bytes()))
            self.assertEqual(request["codex_binary"], os.fspath(fake))
            self.assertEqual(command[2:4], ["--spec", os.fspath(root / "runtime/resume/001-unknown/recovery-task/spec.json")])
            self.assertIn("--codex", command)
            self.assertEqual(command[command.index("--codex") + 1], os.fspath(fake))
            self.assertNotIn("exec", command)
            self.assertNotIn("resume", command)
            self.assertTrue((codex_home / "config.toml").is_file())
            receipt_ref = "runtime/watchdogs/001-unknown/recovery-task/terminal.json"
            receipt_path = root / receipt_ref
            receipt = json.loads(receipt_path.read_text())
            receipt.update({
                "producer": "reconciler",
                "status": "failed",
                "exit_code": None,
                "group_reaped": False,
                "group_gone_observed": True,
                "reconcile_reason": "watchdog_lost",
            })
            receipt_path.write_text(json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n")
            replayed = _raw_from_supervisor_terminal(
                root,
                "runtime/watchdogs/001-unknown/recovery-task/request.json",
                receipt_ref,
            )
            self.assertEqual(replayed.exit_code, 0)
            self.assertFalse(replayed.adapter_error)
            self.assertTrue(any(event.get("type") == "turn.completed" for event in replayed.events))

            resume_terminal_path = root / "runtime/resume/001-unknown/recovery-task/terminal.json"
            resume_terminal = json.loads(resume_terminal_path.read_text())
            resume_terminal["events"][-1]["usage"]["input_tokens"] = 999
            resume_terminal_path.write_text(json.dumps(resume_terminal, sort_keys=True, separators=(",", ":")) + "\n")
            with self.assertRaisesRegex(RuntimeFailure, "differs from raw projection"):
                _raw_from_supervisor_terminal(
                    root,
                    "runtime/watchdogs/001-unknown/recovery-task/request.json",
                    receipt_ref,
                )
            resume_terminal["events"][-1]["usage"]["input_tokens"] = 7
            resume_terminal_path.write_text(json.dumps(resume_terminal, sort_keys=True, separators=(",", ":")) + "\n")

            request_path = root / "runtime/watchdogs/001-unknown/recovery-task/request.json"
            request["work_mode"] = "write"
            request["write_roots"] = ["src/api"]
            request_path.write_text(json.dumps(request, sort_keys=True, separators=(",", ":")) + "\n")
            receipt["request_sha256"] = digest(request_path.read_bytes())
            receipt_path.write_text(json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n")
            with (
                mock.patch("workflow_runtime.attest_writer_permissions", return_value=None) as writer_attest,
                mock.patch("workflow_runtime._attest_worker_permissions") as reader_attest,
            ):
                writer_replayed = _raw_from_supervisor_terminal(
                    root,
                    "runtime/watchdogs/001-unknown/recovery-task/request.json",
                    receipt_ref,
                )
            self.assertEqual(writer_replayed.exit_code, 0)
            writer_attest.assert_called_once()
            reader_attest.assert_not_called()

    @unittest.skipUnless(
        os.environ.get("RUN_LIVE_CODEX_WRITER") == "1" and shutil.which("codex"),
        "set RUN_LIVE_CODEX_WRITER=1 for the real routed writer smoke",
    )
    def test_live_codex_writer_profile_is_effective_and_attested(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            workspace = root / "isolated-checkout"
            (workspace / "src/api").mkdir(parents=True)
            (workspace / ".git").mkdir()
            schema = root / "schemas/writer-output.json"
            schema.parent.mkdir()
            schema.write_text(
                json.dumps(
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["answer"],
                        "properties": {"answer": {"type": "string", "const": "ok"}},
                    }
                )
            )
            auth_source = Path.home() / ".codex/auth.json"
            self.assertTrue(auth_source.is_file(), "live writer smoke requires ~/.codex/auth.json")
            codex_home = _prepare_codex_home(
                root,
                auth_source,
                owner_id="live-writer-smoke",
                writer_roots=("src/api",),
            )
            try:
                execute = codex_task_executor(
                    CodexExecConfig(
                        run_root=root,
                        repo_root=workspace,
                        codex_home=codex_home,
                        codex_binary=shutil.which("codex") or "codex",
                        workflow_id="live-writer-smoke",
                        authority_revision=1,
                        permissions_profile="vnext-writer",
                    )
                )
                observed = execute(
                    {
                        **self.direct_runtime_fence(root),
                        "task_id": "writer-smoke",
                        "role": "worker",
                        "work_mode": "write",
                        "write_roots": ["src/api"],
                        "execution_deadline_seconds": 120,
                        "_runtime_worker_root": os.fspath(workspace),
                        "_runtime_codex_home": os.fspath(codex_home),
                        "_runtime_permissions_profile": "vnext-writer",
                        "_runtime_write_roots": ["src/api"],
                        "_runtime_source_launch_fence": (
                            workflow_runtime._seal_source_write_probe_launch_fence(workspace)
                        ),
                    },
                    {
                        "output_schema_ref": "schemas/writer-output.json",
                        "prompt": (
                            "Use the shell once to write the exact UTF-8 text 'live-writer-ok\\n' "
                            "to src/api/live-writer.txt. Do not inspect or modify anything else. "
                            "Then return exactly the schema-compliant JSON answer ok."
                        ),
                    },
                )
                self.assertFalse(observed.adapter_error, observed.stderr)
                self.assertEqual((workspace / "src/api/live-writer.txt").read_text(), "live-writer-ok\n")
                self.assertIsNone(
                    attest_writer_permissions(
                        observed.turn_context,
                        workspace,
                        codex_home,
                        ("src/api",),
                    )
                )
            finally:
                _cleanup_codex_auth(codex_home)


if __name__ == "__main__":
    unittest.main(verbosity=2)
