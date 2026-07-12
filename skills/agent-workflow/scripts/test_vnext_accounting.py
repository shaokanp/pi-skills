#!/usr/bin/env python3
"""Slice 7 accounting and completion-density contract tests."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from vnext_accounting import (  # noqa: E402
    AccountingError,
    classify_completion_density,
    observe_app_server,
    observe_exec_jsonl,
    observe_stop_hook,
    seal_accounting,
    verify_accounting,
)
from phase_protocol import ProtocolError, validate_contract, validate_sidecar  # noqa: E402
from artifact_store import ArtifactError  # noqa: E402


FIXTURES = SCRIPT_DIR.parent / "fixtures" / "vnext" / "accounting"
CODEX_VERSION = "codex-cli 0.144.0-alpha.4"
SCHEMA_SHA256 = "sha256:fb0d6bf6b9f192257f452340de3fdca6b4b2c8e1a216aafaf48837a006e14bea"


def read_jsonl(path: Path) -> tuple[bytes, list[dict[str, object]]]:
    raw = path.read_bytes()
    return raw, [json.loads(line) for line in raw.splitlines()]


def encode_jsonl(events: list[dict[str, object]]) -> bytes:
    return b"".join(
        json.dumps(event, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
        for event in events
    )


class VNextAccountingTests(unittest.TestCase):
    def test_app_server_sums_last_usage_within_one_turn(self) -> None:
        def usage(amount: int, total: int) -> dict[str, object]:
            return {
                "last": {"cachedInputTokens": 0, "inputTokens": amount, "outputTokens": 0, "reasoningOutputTokens": 0, "totalTokens": amount},
                "total": {"cachedInputTokens": 0, "inputTokens": total, "outputTokens": 0, "reasoningOutputTokens": 0, "totalTokens": total},
            }
        events = [
            {"method": "turn/started", "params": {"threadId": "orchestrator-1", "turn": {"id": "turn-1", "status": "inProgress", "items": []}}},
            {"method": "thread/tokenUsage/updated", "params": {"threadId": "orchestrator-1", "turnId": "turn-1", "tokenUsage": usage(10, 10)}},
            {"method": "thread/tokenUsage/updated", "params": {"threadId": "orchestrator-1", "turnId": "turn-1", "tokenUsage": usage(20, 30)}},
            {"method": "turn/completed", "params": {"threadId": "orchestrator-1", "turn": {"id": "turn-1", "status": "completed", "items": []}}},
        ]
        raw = encode_jsonl(events)
        observed = observe_app_server(
            events,
            raw_evidence=raw,
            evidence_sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
            codex_version=CODEX_VERSION,
            protocol_schema_sha256=SCHEMA_SHA256,
            thread_id="orchestrator-1",
            turn_ids=["turn-1"],
        )
        self.assertEqual(observed["workflow_tokens"], 30)

    def test_app_server_resume_subtracts_digest_bound_prior_turn_usage(self) -> None:
        prior = {
            "cached_input_tokens": 0,
            "input_tokens": 100,
            "output_tokens": 20,
            "reasoning_output_tokens": 10,
            "total_tokens": 120,
        }
        events = [
            {"method": "turn/started", "params": {"threadId": "worker-1", "turn": {"id": "turn-2", "status": "inProgress", "items": []}}},
            {"method": "thread/tokenUsage/updated", "params": {"threadId": "worker-1", "turnId": "turn-2", "tokenUsage": {
                "last": {"cachedInputTokens": 0, "inputTokens": 30, "outputTokens": 5, "reasoningOutputTokens": 2, "totalTokens": 35},
                "total": {"cachedInputTokens": 0, "inputTokens": 130, "outputTokens": 25, "reasoningOutputTokens": 12, "totalTokens": 155},
            }}},
            {"method": "turn/completed", "params": {"threadId": "worker-1", "turn": {"id": "turn-2", "status": "completed", "items": []}}},
        ]
        raw = encode_jsonl(events)
        observed = observe_app_server(
            events,
            raw_evidence=raw,
            evidence_sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
            codex_version=CODEX_VERSION,
            protocol_schema_sha256=SCHEMA_SHA256,
            thread_id="worker-1",
            turn_ids=["turn-2"],
            prior_breakdown=prior,
        )
        self.assertEqual(observed["workflow_tokens"], 35)
        self.assertEqual(observed["breakdown"]["total_tokens"], 35)

    def test_completion_count_rejects_duplicate_cumulative_total(self) -> None:
        rows = [
            {"type": "session_meta", "payload": {"id": "orchestrator-1", "timestamp": "2026-07-12T00:00:00Z"}},
            {"type": "turn_context", "payload": {"turn_id": "turn-1", "model": "gpt-5.6-sol", "effort": "xhigh"}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 2, "reasoning_output_tokens": 1, "total_tokens": 12},
                "last_token_usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 2, "reasoning_output_tokens": 1, "total_tokens": 12},
            }}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 2, "reasoning_output_tokens": 1, "total_tokens": 12},
                "last_token_usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 2, "reasoning_output_tokens": 1, "total_tokens": 12},
            }}},
            {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-1", "last_agent_message": "done"}},
        ]
        raw = encode_jsonl(rows)
        with self.assertRaisesRegex(AccountingError, "cumulative token usage"):
            classify_completion_density(raw, session_id="orchestrator-1")

    def test_codex_exec_jsonl_worker_usage_is_exact(self) -> None:
        events = [
            {"type": "thread.started", "thread_id": "worker-1"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
            {"type": "turn.completed", "usage": {
                "input_tokens": 120,
                "cached_input_tokens": 20,
                "output_tokens": 30,
                "reasoning_output_tokens": 10,
            }},
        ]
        raw = encode_jsonl(events)
        result = observe_exec_jsonl(
            events,
            raw_evidence=raw,
            evidence_sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
            codex_version=CODEX_VERSION,
            thread_id="worker-1",
            turn_id="worker-turn-1",
        )
        self.assertEqual(result["coverage"], "exact")
        self.assertEqual(result["source"], "codex_exec_jsonl_v1")
        self.assertEqual(result["workflow_tokens"], 150)

    def test_codex_exec_jsonl_rejects_missing_terminal_usage(self) -> None:
        events = [{"type": "thread.started", "thread_id": "worker-1"}, {"type": "turn.started"}]
        raw = encode_jsonl(events)
        with self.assertRaisesRegex(AccountingError, "terminal boundary is incomplete"):
            observe_exec_jsonl(
                events,
                raw_evidence=raw,
                evidence_sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
                codex_version=CODEX_VERSION,
                thread_id="worker-1",
                turn_id="worker-turn-1",
            )

    def test_codex_exec_jsonl_rejects_events_after_terminal(self) -> None:
        events = [
            {"type": "thread.started", "thread_id": "worker-1"},
            {"type": "turn.started"},
            {"type": "turn.completed", "usage": {
                "input_tokens": 1,
                "cached_input_tokens": 0,
                "output_tokens": 1,
                "reasoning_output_tokens": 0,
            }},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "late"}},
        ]
        raw = encode_jsonl(events)
        with self.assertRaisesRegex(AccountingError, "unexpected Codex exec event"):
            observe_exec_jsonl(
                events,
                raw_evidence=raw,
                evidence_sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
                codex_version=CODEX_VERSION,
                thread_id="worker-1",
                turn_id="worker-turn-1",
            )

    def test_semantic_final_cannot_claim_post_terminal_exact_accounting(self) -> None:
        final = json.loads((SCRIPT_DIR.parent / "fixtures" / "vnext" / "protocol" / "valid" / "final.json").read_text())
        final["accounting"] = {
            "coverage": "exact",
            "workflow_tokens": 120,
            "source": "routed_terminal_events_plus_host_audit",
            "confidence": "exact",
            "boundary": "through_orchestrator_terminal",
        }
        with self.assertRaisesRegex(ProtocolError, "post-terminal accounting sidecar"):
            validate_contract("final", final)
        final = json.loads((SCRIPT_DIR.parent / "fixtures" / "vnext" / "protocol" / "valid" / "final.json").read_text())
        final["completion_density"] = {
            "source": "raw_session_audit",
            "forbidden_wakes": 0,
            "semantic_wakes": 3,
            "sparse_wait_continuations": 0,
            "target_eligible": True,
        }
        with self.assertRaisesRegex(ProtocolError, "post-terminal completion-density sidecar"):
            validate_contract("final", final)

    def test_post_terminal_accounting_seals_once_without_model_wake(self) -> None:
        raw, events = read_jsonl(FIXTURES / "app-server-valid.jsonl")
        native = observe_app_server(
            events,
            raw_evidence=raw,
            evidence_sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
            codex_version=CODEX_VERSION,
            protocol_schema_sha256=SCHEMA_SHA256,
            thread_id="orchestrator-1",
            turn_ids=["turn-1", "turn-2"],
        )
        with TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            bundle = "sha256:" + "2" * 64
            (root / "workflow.json").write_text(json.dumps({"workflow_id": "fixture-workflow"}))
            (root / "final.json").write_text(json.dumps({
                "workflow_id": "fixture-workflow", "runtime_bundle_sha256": bundle,
            }))
            native_source = root.parent / f"{root.name}-native.json"
            completion_source = root.parent / f"{root.name}-completion-session.jsonl"
            native_source.write_text(json.dumps(native))
            completion_source.write_bytes((FIXTURES / "completion-session-clean.jsonl").read_bytes())

            def replay(_root: Path, **kwargs: object) -> dict[str, object]:
                metrics = kwargs["metrics_out"]
                metrics.update({"external_input_tokens": 80, "external_output_tokens": 20, "external_total_tokens": 100, "external_accounting_exact": True})
                return json.loads((root / "final.json").read_text())

            with mock.patch("vnext_accounting.validate_replay", side_effect=replay):
                with mock.patch("vnext_accounting.create_once_json", side_effect=ArtifactError("injected crash")):
                    with self.assertRaisesRegex(AccountingError, "injected crash"):
                        seal_accounting(root, native_source=native_source, native_evidence_source=FIXTURES / "app-server-valid.jsonl", completion_source=completion_source, running_bundle=bundle)
                self.assertTrue((root / "accounting/evidence/native-observation.json").is_file())
                self.assertTrue((root / "accounting/evidence/native-raw.jsonl").is_file())
                self.assertTrue((root / "accounting/evidence/completion-session.jsonl").is_file())
                self.assertTrue((root / "accounting/evidence/completion-projection.json").is_file())
                self.assertFalse((root / "accounting/final.json").exists())
                path = seal_accounting(root, native_source=native_source, native_evidence_source=FIXTURES / "app-server-valid.jsonl", completion_source=completion_source, running_bundle=bundle)
                sidecar = json.loads(path.read_text())
                self.assertEqual(sidecar["workflow_tokens"], 140)
                self.assertEqual(sidecar["coverage"], "exact")
                self.assertFalse(sidecar["native_orchestrator"]["late_seal_wake_required"])
                self.assertEqual(
                    seal_accounting(root, native_source=native_source, native_evidence_source=FIXTURES / "app-server-valid.jsonl", completion_source=completion_source, running_bundle=bundle),
                    path,
                )
                self.assertEqual(verify_accounting(root, running_bundle=bundle), sidecar)
                with self.assertRaisesRegex(AccountingError, "running runtime bundle"):
                    verify_accounting(root, running_bundle="sha256:" + "9" * 64)
                raw_session = root / sidecar["completion_density"]["evidence_ref"]
                raw_session.chmod(0o600)
                raw_session.write_bytes(raw_session.read_bytes() + b"{}\n")
                with self.assertRaisesRegex(AccountingError, "completion evidence digest drifted"):
                    verify_accounting(root, running_bundle=bundle)
    def test_accounting_sidecar_binds_final_external_native_and_density(self) -> None:
        sidecar = {
            "schema_version": "agent-workflow.accounting.vnext.v1",
            "workflow_id": "fixture-workflow",
            "final_ref": "final.json",
            "final_sha256": "sha256:" + "1" * 64,
            "runtime_bundle_sha256": "sha256:" + "2" * 64,
            "boundary": "through_orchestrator_terminal",
            "coverage": "exact",
            "confidence": "exact",
            "workflow_tokens": 140,
            "external_task_usage": {
                "source": "codex_terminal_events",
                "confidence": "exact",
                "input": 80,
                "output": 20,
                "total": 100,
            },
            "native_orchestrator": {
                "coverage": "exact",
                "source": "codex_app_server_thread_token_usage_v2",
                "confidence": "exact",
                "tokens": 40,
                "evidence_ref": "accounting/evidence/native.json",
                "evidence_sha256": "sha256:" + "3" * 64,
                "raw_evidence_ref": "accounting/evidence/native-raw.jsonl",
                "raw_evidence_sha256": "sha256:" + "5" * 64,
                "reason": None,
                "late_seal_wake_required": False,
            },
            "completion_density": {
                "source": "raw_session_replay_v1",
                "session_id": "orchestrator-1",
                "terminal_turn_id": "turn-2",
                "forbidden_wakes": 0,
                "semantic_wakes": 5,
                "sparse_wait_continuations": 0,
                "target_eligible": True,
                "evidence_ref": "accounting/evidence/completion-session.jsonl",
                "evidence_sha256": "sha256:" + "4" * 64,
                "projection_ref": "accounting/evidence/completion-projection.json",
                "projection_sha256": "sha256:" + "6" * 64,
            },
            "created_at": "2026-07-12T00:00:03Z",
        }
        self.assertEqual(validate_sidecar("accounting", sidecar), sidecar)
        bad_total = copy.deepcopy(sidecar)
        bad_total["workflow_tokens"] = 139
        with self.assertRaisesRegex(ProtocolError, "workflow token arithmetic"):
            validate_sidecar("accounting", bad_total)
        late = copy.deepcopy(sidecar)
        late["native_orchestrator"]["late_seal_wake_required"] = True
        with self.assertRaisesRegex(ProtocolError, "late-seal wake"):
            validate_sidecar("accounting", late)
        partial = copy.deepcopy(sidecar)
        partial.update({"coverage": "partial", "confidence": "partial", "workflow_tokens": None})
        partial["native_orchestrator"].update({
            "coverage": "partial", "confidence": "partial", "tokens": None,
            "reason": "unsupported_codex_version",
        })
        self.assertEqual(validate_sidecar("accounting", partial), partial)

    def test_app_server_exact_observation_binds_turns_arithmetic_and_digest(self) -> None:
        raw, events = read_jsonl(FIXTURES / "app-server-valid.jsonl")
        result = observe_app_server(
            events,
            raw_evidence=raw,
            evidence_sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
            codex_version=CODEX_VERSION,
            protocol_schema_sha256=SCHEMA_SHA256,
            thread_id="orchestrator-1",
            turn_ids=["turn-1", "turn-2"],
        )
        self.assertEqual(result["coverage"], "exact")
        self.assertEqual(result["confidence"], "exact")
        self.assertEqual(result["workflow_tokens"], 40)
        self.assertEqual(result["breakdown"], {
            "cached_input_tokens": 7,
            "input_tokens": 30,
            "output_tokens": 10,
            "reasoning_output_tokens": 3,
            "total_tokens": 40,
        })
        self.assertEqual(result["boundary"], "through_orchestrator_terminal")

    def test_app_server_rejects_drift_tamper_missing_extra_and_bad_arithmetic(self) -> None:
        raw, valid = read_jsonl(FIXTURES / "app-server-valid.jsonl")
        common = dict(
            codex_version=CODEX_VERSION,
            protocol_schema_sha256=SCHEMA_SHA256,
            thread_id="orchestrator-1",
            turn_ids=["turn-1", "turn-2"],
        )
        cases: list[tuple[str, list[dict[str, object]], dict[str, object]]] = []
        decreasing = copy.deepcopy(valid)
        last_usage = [item for item in decreasing if item["method"] == "thread/tokenUsage/updated"][-1]
        last_usage["params"]["tokenUsage"]["total"]["inputTokens"] = 8
        last_usage["params"]["tokenUsage"]["total"]["totalTokens"] = 18
        cases.append(("moved backwards", decreasing, {}))
        arithmetic = copy.deepcopy(valid)
        [item for item in arithmetic if item["method"] == "thread/tokenUsage/updated"][-1]["params"]["tokenUsage"]["last"]["totalTokens"] = 99
        cases.append(("arithmetic", arithmetic, {}))
        missing = [
            item for item in valid
            if not (item["method"] == "turn/completed" and item["params"]["turn"]["id"] == "turn-2")
        ]
        cases.append(("terminal", missing, {}))
        extra = copy.deepcopy(valid)
        [item for item in extra if item["method"] == "thread/tokenUsage/updated"][-1]["params"]["turnId"] = "turn-3"
        cases.append(("unexpected turn", extra, {}))
        duplicate_terminal = copy.deepcopy(valid) + [copy.deepcopy(valid[-1])]
        cases.append(("terminal", duplicate_terminal, {}))
        lone = [item for item in copy.deepcopy(valid) if item["method"] == "thread/tokenUsage/updated"][:1]
        cases.append(("terminal", lone, {"turn_ids": ["turn-1"]}))
        cases.append(("evidence digest", valid, {"evidence_sha256": "sha256:" + "0" * 64}))
        cases.append(("unsupported Codex version", valid, {"codex_version": "codex-cli 0.145.0"}))
        cases.append(("protocol schema", valid, {"protocol_schema_sha256": "sha256:" + "1" * 64}))
        for message, events, overrides in cases:
            with self.subTest(message=message):
                case_raw = encode_jsonl(events)
                evidence = {
                    "raw_evidence": case_raw,
                    "evidence_sha256": "sha256:" + hashlib.sha256(case_raw).hexdigest(),
                }
                with self.assertRaisesRegex(AccountingError, message):
                    observe_app_server(events, **(common | evidence | overrides))

    def test_stop_hook_is_version_gated_partial_and_drift_never_upgrades(self) -> None:
        payload = json.loads((FIXTURES / "stop-hook-input.json").read_text())
        transcript = FIXTURES / "orchestrator-rollout.jsonl"
        parsed = observe_stop_hook(payload, transcript_path=transcript, codex_version=CODEX_VERSION)
        self.assertEqual(parsed["coverage"], "partial")
        self.assertEqual(parsed["confidence"], "partial")
        self.assertEqual(parsed["workflow_tokens"], 15)
        self.assertFalse(parsed["late_seal_wake_required"])
        drift = observe_stop_hook(payload, transcript_path=transcript, codex_version="codex-cli 0.145.0")
        self.assertEqual(drift["coverage"], "partial")
        self.assertIsNone(drift["workflow_tokens"])
        self.assertEqual(drift["reason"], "unsupported_codex_version")
        malformed = observe_stop_hook(payload | {"hook_event_name": "PostToolUse"}, transcript_path=transcript, codex_version=CODEX_VERSION)
        self.assertEqual(malformed["coverage"], "partial")
        self.assertIsNone(malformed["workflow_tokens"])
        wrong_session = observe_stop_hook(payload | {"session_id": "other"}, transcript_path=transcript, codex_version=CODEX_VERSION)
        self.assertEqual(wrong_session["reason"], "transcript_session_mismatch")
        self.assertIsNone(wrong_session["workflow_tokens"])
        wrong_turn = observe_stop_hook(payload | {"turn_id": "other"}, transcript_path=transcript, codex_version=CODEX_VERSION)
        self.assertEqual(wrong_turn["reason"], "transcript_turn_mismatch")
        self.assertIsNone(wrong_turn["workflow_tokens"])

    def test_completion_classifier_rejects_forbidden_and_sparse_wakes(self) -> None:
        clean = classify_completion_density(
            (FIXTURES / "completion-session-clean.jsonl").read_bytes(),
            session_id="orchestrator-1",
        )
        self.assertEqual(clean["semantic_wakes"], 5)
        self.assertEqual(clean["forbidden_wakes"], 0)
        self.assertTrue(clean["target_eligible"])
        dirty = classify_completion_density(
            (FIXTURES / "completion-session-forbidden.jsonl").read_bytes(),
            session_id="orchestrator-1",
        )
        self.assertEqual(dirty["forbidden_wakes"], 3)
        self.assertFalse(dirty["target_eligible"])
        caller_labels = json.dumps([{"completion_id": "x", "class": "final"}]).encode()
        with self.assertRaisesRegex(AccountingError, "raw session"):
            classify_completion_density(caller_labels, session_id="orchestrator-1")
        fake_lifecycle = (FIXTURES / "completion-session-clean.jsonl").read_bytes().replace(
            b"python3 workflow_runtime.py admit",
            b"echo workflow_runtime.py admit",
        )
        self.assertFalse(classify_completion_density(fake_lifecycle, session_id="orchestrator-1")["target_eligible"])
        aborted = (FIXTURES / "completion-session-clean.jsonl").read_bytes().replace(
            b'"type":"task_complete"',
            b'"type":"turn_aborted"',
        )
        with self.assertRaisesRegex(AccountingError, "successful terminal"):
            classify_completion_density(aborted, session_id="orchestrator-1")

    def test_seal_accounting_rejects_runtime_bundle_drift_before_writes(self) -> None:
        with TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            old_bundle = "sha256:" + "2" * 64
            (root / "workflow.json").write_text(json.dumps({"workflow_id": "fixture-workflow"}))
            (root / "final.json").write_text(json.dumps({"workflow_id": "fixture-workflow", "runtime_bundle_sha256": old_bundle}))
            with self.assertRaisesRegex(AccountingError, "running runtime bundle"):
                seal_accounting(
                    root,
                    native_source=FIXTURES / "stop-hook-input.json",
                    native_evidence_source=FIXTURES / "orchestrator-rollout.jsonl",
                    completion_source=FIXTURES / "completion-session-clean.jsonl",
                    running_bundle="sha256:" + "9" * 64,
                )
            self.assertFalse((root / "accounting").exists())

    def test_seal_accounting_cross_binds_native_and_completion_terminal_turn(self) -> None:
        raw, events = read_jsonl(FIXTURES / "app-server-valid.jsonl")
        truncated_events = events[:4]
        truncated_raw = encode_jsonl(truncated_events)
        native = observe_app_server(
            truncated_events,
            raw_evidence=truncated_raw,
            evidence_sha256="sha256:" + hashlib.sha256(truncated_raw).hexdigest(),
            codex_version=CODEX_VERSION,
            protocol_schema_sha256=SCHEMA_SHA256,
            thread_id="orchestrator-1",
            turn_ids=["turn-1"],
        )
        self.assertEqual(native["workflow_tokens"], 26)
        with TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            bundle = "sha256:" + "2" * 64
            (root / "workflow.json").write_text(json.dumps({"workflow_id": "fixture-workflow"}))
            (root / "final.json").write_text(json.dumps({"workflow_id": "fixture-workflow", "runtime_bundle_sha256": bundle}))
            native_source = root.parent / f"{root.name}-native.json"
            native_raw_source = root.parent / f"{root.name}-native.jsonl"
            native_source.write_text(json.dumps(native))
            native_raw_source.write_bytes(truncated_raw)

            def replay(_root: Path, **kwargs: object) -> dict[str, object]:
                kwargs["metrics_out"].update({
                    "external_input_tokens": 80,
                    "external_output_tokens": 20,
                    "external_total_tokens": 100,
                    "external_accounting_exact": True,
                })
                return json.loads((root / "final.json").read_text())

            with mock.patch("vnext_accounting.validate_replay", side_effect=replay):
                with self.assertRaisesRegex(AccountingError, "terminal turn"):
                    seal_accounting(
                        root,
                        native_source=native_source,
                        native_evidence_source=native_raw_source,
                        completion_source=FIXTURES / "completion-session-clean.jsonl",
                        running_bundle=bundle,
                    )
            self.assertFalse((root / "accounting").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
