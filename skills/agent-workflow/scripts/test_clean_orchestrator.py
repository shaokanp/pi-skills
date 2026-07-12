#!/usr/bin/env python3
"""Standard-library regressions for the Clean Orchestrator portable contract."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from pathlib import Path
from typing import Any

import clean_orchestrator
import token_accounting
import workflow_controller
import verify_workflow


SCRIPT_DIR = Path(__file__).resolve().parent
NEW_WORKFLOW = SCRIPT_DIR / "new_workflow.py"
VERIFY_WORKFLOW = SCRIPT_DIR / "verify_workflow.py"


def admitted_orchestration(*, delivery: str = "bounded_interim") -> dict[str, Any]:
    round_plan = clean_orchestrator.build_round_runtime_contract(
        round_id="round-001",
        objective="Resolve one bounded implementation uncertainty.",
        lane_ids=["discover-01", "verify-01"],
    )
    round_plan["runtime_mode"] = delivery
    round_plan["dispatch_mode"] = (
        "queued_barrier" if delivery == "target" else "native_direct_terminal_events"
    )
    round_plan["completion_budget"]["native_sibling_terminal_reactivations_max"] = (
        0 if delivery == "target" else 2
    )
    round_plan["completion_budget"]["initial_dispatch_reactivations_max"] = (
        1 if delivery == "target" else 2
    )
    round_plan["completion_budget"]["deterministic_tool_result_reactivations_max"] = (
        0 if delivery == "target" else 4
    )
    round_plan["completion_budget"]["absolute_coordinator_completions_max"] = (
        8 if delivery == "target" else 12
    )
    round_plan["lanes"] = [
        {"id": "discover-01", "enabled": True},
        {"id": "verify-01", "enabled": True},
    ]
    clean_orchestrator.seal_round_gate_graph(round_plan)
    runtime = clean_orchestrator.build_clean_runtime_contract()
    runtime["delivery_level"] = delivery
    runtime["capabilities"] = {
        "schema_version": clean_orchestrator.CAPABILITY_SCHEMA,
        "protocol_version": "clean-orchestrator-host.v1",
        "runtime_class": "codex_builtin_subagents",
        "observed_at": "2026-07-11T00:00:00Z",
        "source": "runtime_probe_fixture",
        "status": "observed",
        "values": {
            "atomic_orchestrator_spawn_and_block": delivery == "target",
            "all_terminal_durable_barrier": delivery == "target",
            "progress_suppression": delivery == "target",
            "automatic_session_registration": delivery == "target",
            "resumable_barrier": delivery == "target",
            "minimal_profile": delivery == "target",
            "terminal_host_finalization": delivery == "target",
            "clean_context": True,
            "direct_terminal_event_wait": True,
            "subtree_discovery": True,
            "exact_cumulative_token_events": True,
            "queued_native_dispatch": delivery == "target",
            "generation_rotation": delivery == "target",
            "max_native_wait_ms": 3_600_000,
            "max_concurrent_sessions": 4,
            "available_child_slots": 2,
        },
    }
    runtime["admission"].update(
        {
            "workflow_deadline_ms": 900_000,
            "estimated_coordinator_completions_worst_case": 4 if delivery == "target" else 11,
            "estimated_coordinator_tokens_worst_case": 120_000 if delivery == "target" else 330_000,
            "max_coordinator_completions": 8 if delivery == "target" else 11,
            "max_coordinator_tokens": 330_000,
            "coordinator_token_calibration": {
                "status": "observed",
                "source": "fixture upper-bound calibration",
                "tokens_per_completion_upper_bound": 30_000
            },
            "fixed_protocol_overhead_completions": 2,
        }
    )
    orchestration = {
        "rounds": [round_plan],
        "clean_orchestrator_runtime": runtime,
    }
    clean_orchestrator.prepare_clean_runtime_dispatch(orchestration)
    return orchestration


def completion_ledger(orchestration: dict[str, Any]) -> dict[str, Any]:
    round_plan = orchestration["rounds"][0]
    counts = {name: 0 for name in clean_orchestrator.COMPLETION_CLASSES}
    counts["decision_gate"] = 1
    return {
        "schema_version": clean_orchestrator.COMPLETION_DENSITY_SCHEMA,
        "source": "runtime_session_events",
        "entries": [
            {
                "event_ref": "codex:orchestrator:token_count:10",
                "round_id": "round-001",
                "class": "decision_gate",
                "gate_id": "round-001-all-terminal",
            }
        ],
        "rounds": {
            "round-001": {
                "gate_graph_sha256": round_plan["gate_graph_seal"]["content_sha256"],
                "actual_counts": counts,
                "actual_coordinator_completions": 1,
                "budget_resolution": None,
            }
        },
    }


class CleanOrchestratorTests(unittest.TestCase):
    def _accounting_start_fixture(
        self,
        root: Path,
        *,
        agent_id: str = "reviewer",
        initial_evidence: bool = False,
    ) -> tuple[Path, Path, Path]:
        workflow = root / "workflow"
        workflow.mkdir()
        (workflow / "controller-receipts").mkdir()
        (workflow / "token-usage.json").write_text(
            json.dumps(token_accounting.new_token_usage()) + "\n",
            encoding="utf-8",
        )
        if initial_evidence:
            (workflow / "token-evidence.json").write_text(
                '{"preexisting":true}\n', encoding="utf-8"
            )
        (workflow / "orchestration.json").write_text(
            json.dumps(
                {
                    "rounds": [
                        {
                            "round_id": "round-001",
                            "lanes": [{"id": "verify-01", "enabled": True}],
                        }
                    ]
                }
            )
            + "\n",
            encoding="utf-8",
        )
        manifest = workflow / workflow_controller.ACCOUNTING_START_MANIFEST
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": workflow_controller.ACCOUNTING_START_SCHEMA,
                    "participants": [
                        {
                            "execution_ref": "round-001:verify-01:attempt-001",
                            "agent_id": agent_id,
                            "round_id": "round-001",
                            "lane_id": "verify-01",
                            "registration_mode": "reuse_existing_session",
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        runtime = root / "codex"
        sessions = runtime / "sessions"
        sessions.mkdir(parents=True)

        def append(path: Path, value: dict[str, Any]) -> None:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(value) + "\n")

        for session_id, parent_id, input_tokens in (
            ("lead", None, 100),
            ("reviewer", "lead", 200),
        ):
            session_path = sessions / f"rollout-{session_id}.jsonl"
            append(
                session_path,
                {
                    "timestamp": "2026-07-11T00:00:00Z",
                    "type": "session_meta",
                    "payload": {
                        "id": session_id,
                        "session_id": session_id,
                        "parent_thread_id": parent_id,
                        "timestamp": "2026-07-11T00:00:00Z",
                        "thread_source": "cli",
                    },
                },
            )
            append(
                session_path,
                {
                    "timestamp": "2026-07-11T00:00:01Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": input_tokens,
                                "cached_input_tokens": 50,
                                "output_tokens": 10,
                                "reasoning_output_tokens": 2,
                                "total_tokens": input_tokens + 10,
                            }
                        },
                    },
                },
            )
        return workflow, manifest, runtime

    def test_unknown_capabilities_pass_scaffold_and_fail_planned(self) -> None:
        runtime = clean_orchestrator.build_clean_runtime_contract()
        round_plan = clean_orchestrator.build_round_runtime_contract(
            round_id="round-001",
            objective="Draft round",
            lane_ids=[],
        )
        orchestration = {
            "rounds": [round_plan],
            "clean_orchestrator_runtime": runtime,
        }
        clean_orchestrator.validate_clean_runtime_contract(
            orchestration, allow_draft=True, required=True
        )
        with self.assertRaisesRegex(
            clean_orchestrator.CleanOrchestratorError,
            "capabilities are unknown",
        ):
            clean_orchestrator.validate_clean_runtime_contract(
                orchestration, allow_draft=False, required=True
            )

    def test_bounded_interim_and_target_admission(self) -> None:
        for delivery, decision in (
            ("bounded_interim", "admit_bounded_interim"),
            ("target", "admit_target"),
        ):
            with self.subTest(delivery=delivery):
                orchestration = admitted_orchestration(delivery=delivery)
                clean_orchestrator.validate_clean_runtime_contract(
                    orchestration, allow_draft=False, required=True
                )
                self.assertEqual(
                    decision,
                    orchestration["clean_orchestrator_runtime"]["admission"]["decision"],
                )

    def test_target_rejects_missing_host_primitive(self) -> None:
        orchestration = admitted_orchestration(delivery="target")
        orchestration["clean_orchestrator_runtime"]["capabilities"]["values"][
            "all_terminal_durable_barrier"
        ] = False
        clean_orchestrator.prepare_clean_runtime_dispatch(orchestration)
        with self.assertRaisesRegex(
            clean_orchestrator.CleanOrchestratorError,
            "admission rejected dispatch: reject_unsupported",
        ):
            clean_orchestrator.validate_clean_runtime_contract(
                orchestration, allow_draft=False, required=True
            )

    def test_interim_rejects_deadline_capacity_and_token_bounds(self) -> None:
        mutations = {
            "deadline": lambda runtime: runtime["admission"].update(
                {"workflow_deadline_ms": 3_600_001}
            ),
            "capacity": lambda runtime: runtime["admission"].update(
                {"max_parallelism": 3}
            ),
            "tokens": lambda runtime: runtime["admission"].update(
                {"estimated_coordinator_tokens_worst_case": 300_001}
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                orchestration = admitted_orchestration()
                runtime = orchestration["clean_orchestrator_runtime"]
                mutate(runtime)
                if name != "capacity":
                    clean_orchestrator.prepare_clean_runtime_dispatch(orchestration)
                else:
                    runtime["admission"]["decision"] = "reject_bounds"
                with self.assertRaises(clean_orchestrator.CleanOrchestratorError):
                    clean_orchestrator.validate_clean_runtime_contract(
                        orchestration, allow_draft=False, required=True
                    )

    def test_interim_rejects_understated_seal_or_unobserved_token_calibration(self) -> None:
        for mutation in ("completion", "calibration", "token_formula"):
            with self.subTest(mutation=mutation):
                orchestration = admitted_orchestration()
                admission = orchestration["clean_orchestrator_runtime"]["admission"]
                if mutation == "completion":
                    admission["estimated_coordinator_completions_worst_case"] = 9
                elif mutation == "calibration":
                    admission["coordinator_token_calibration"]["status"] = "unknown"
                else:
                    admission["estimated_coordinator_tokens_worst_case"] = 299_999
                clean_orchestrator.prepare_clean_runtime_dispatch(orchestration)
                with self.assertRaisesRegex(
                    clean_orchestrator.CleanOrchestratorError,
                    "reject_bounds",
                ):
                    clean_orchestrator.validate_clean_runtime_contract(
                        orchestration, allow_draft=False, required=True
                    )

    def test_round_gate_seal_and_budget_are_non_gameable(self) -> None:
        orchestration = admitted_orchestration()
        round_plan = orchestration["rounds"][0]
        round_plan["semantic_gates"][0]["trigger"] = "renamed after dispatch"
        with self.assertRaisesRegex(
            clean_orchestrator.CleanOrchestratorError,
            "gate_graph_seal digest mismatch",
        ):
            clean_orchestrator.validate_round_contract(round_plan, allow_draft=False)
        round_plan = admitted_orchestration(delivery="target")["rounds"][0]
        round_plan["completion_budget"]["native_sibling_terminal_reactivations_max"] = 1
        clean_orchestrator.seal_round_gate_graph(round_plan)
        with self.assertRaisesRegex(
            clean_orchestrator.CleanOrchestratorError,
            "target mode forbids sibling-terminal",
        ):
            clean_orchestrator.validate_round_contract(round_plan, allow_draft=False)

    def test_completion_classes_are_exclusive_sealed_and_bounded(self) -> None:
        orchestration = admitted_orchestration()
        ledger = completion_ledger(orchestration)
        clean_orchestrator.validate_completion_density(
            ledger, orchestration, final=True
        )
        duplicated = copy.deepcopy(ledger)
        duplicated["entries"].append(copy.deepcopy(duplicated["entries"][0]))
        with self.assertRaisesRegex(
            clean_orchestrator.CleanOrchestratorError,
            "event_ref values must be unique",
        ):
            clean_orchestrator.validate_completion_density(
                duplicated, orchestration, final=True
            )
        forbidden = copy.deepcopy(ledger)
        forbidden["entries"][0]["class"] = "wrapper_wait"
        counts = {name: 0 for name in clean_orchestrator.COMPLETION_CLASSES}
        counts["wrapper_wait"] = 1
        forbidden["rounds"]["round-001"]["actual_counts"] = counts
        with self.assertRaisesRegex(
            clean_orchestrator.CleanOrchestratorError,
            "forbidden completion classes",
        ):
            clean_orchestrator.validate_completion_density(
                forbidden, orchestration, final=True
            )

    def test_bounded_interim_budget_overrun_always_fails_closed(self) -> None:
        orchestration = admitted_orchestration()
        orchestration["rounds"][0]["completion_budget"][
            "absolute_coordinator_completions_max"
        ] = 1
        clean_orchestrator.seal_round_gate_graph(orchestration["rounds"][0])
        ledger = completion_ledger(orchestration)
        extra = {
            "event_ref": "codex:orchestrator:token_count:11",
            "round_id": "round-001",
            "class": "deterministic_tool_result_reactivation",
            "gate_id": None,
        }
        ledger["entries"].append(extra)
        ledger["rounds"]["round-001"]["actual_counts"][
            "deterministic_tool_result_reactivation"
        ] = 1
        ledger["rounds"]["round-001"]["actual_coordinator_completions"] = 2
        ledger["rounds"]["round-001"]["gate_graph_sha256"] = orchestration[
            "rounds"
        ][0]["gate_graph_seal"]["content_sha256"]
        with self.assertRaisesRegex(
            clean_orchestrator.CleanOrchestratorError,
            "exceeds its absolute completion budget",
        ):
            clean_orchestrator.validate_completion_density(
                ledger, orchestration, final=True
            )
        ledger["rounds"]["round-001"]["budget_resolution"] = {
            "type": "deferred_with_reason",
            "reason": "Bootstrap run predates the source-owned compound controller.",
            "owner": "host_runtime_follow_up",
        }
        with self.assertRaisesRegex(
            clean_orchestrator.CleanOrchestratorError,
            "exceeds its absolute completion budget",
        ):
            clean_orchestrator.validate_completion_density(
                ledger, orchestration, final=True
            )

    def test_bounded_interim_per_class_overrun_always_fails_closed(self) -> None:
        orchestration = admitted_orchestration()
        orchestration["rounds"][0]["completion_budget"][
            "deterministic_tool_result_reactivations_max"
        ] = 0
        clean_orchestrator.seal_round_gate_graph(orchestration["rounds"][0])
        ledger = completion_ledger(orchestration)
        ledger["entries"].append(
            {
                "event_ref": "codex:orchestrator:token_count:11",
                "round_id": "round-001",
                "class": "deterministic_tool_result_reactivation",
                "gate_id": None,
            }
        )
        ledger["rounds"]["round-001"]["actual_counts"][
            "deterministic_tool_result_reactivation"
        ] = 1
        ledger["rounds"]["round-001"]["actual_coordinator_completions"] = 2
        ledger["rounds"]["round-001"]["gate_graph_sha256"] = orchestration[
            "rounds"
        ][0]["gate_graph_seal"]["content_sha256"]
        ledger["rounds"]["round-001"]["budget_resolution"] = {
            "type": "deferred_with_reason",
            "reason": "Historical evidence only.",
            "owner": "host_runtime_follow_up",
        }
        with self.assertRaisesRegex(
            clean_orchestrator.CleanOrchestratorError,
            "exceeds deterministic-result wake bound",
        ):
            clean_orchestrator.validate_completion_density(
                ledger, orchestration, final=True
            )

    def test_compound_controller_has_no_native_lifecycle_operation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workflow = Path(temp) / "workflow"
            workflow.mkdir()
            (workflow / "orchestration.json").write_text(
                '{"rounds": []}\n', encoding="utf-8"
            )
            for operation in ("start", "prepare", "collect", "finalize"):
                commands = workflow_controller.controller_commands(
                    operation, workflow, Path(temp)
                )
                flattened = " ".join(part for _, command in commands for part in command)
                self.assertNotIn("spawn_agent", flattened)
                self.assertNotIn("wait_agent", flattened)
                self.assertNotIn("join_agents", flattened)

    def test_compound_start_fresh_scaffold_creates_evidence_and_registers_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workflow, manifest, runtime = self._accounting_start_fixture(Path(temp))
            original_usage = (workflow / "token-usage.json").read_bytes()
            static_commands = [
                ("prepare_dispatch", [sys.executable, "-c", "pass"]),
                ("validate_planned", [sys.executable, "-c", "pass"]),
                ("render_event_card", [sys.executable, "-c", "pass"]),
            ]
            with mock.patch.object(
                workflow_controller,
                "controller_commands",
                return_value=static_commands,
            ):
                receipt = workflow_controller.run_operation(
                    "start",
                    workflow,
                    "compound-start-positive",
                    manifest_path=manifest,
                    runtime="codex",
                    lead_session_id="lead",
                    runtime_root=runtime,
                )
            self.assertEqual("pass", receipt["status"])
            self.assertTrue(receipt["portable_transaction"]["committed"])
            self.assertTrue(receipt["accounting_snapshot_after_static_steps"])
            self.assertEqual("not_claimed", receipt["host_atomicity"])
            self.assertEqual(
                [
                    "snapshot_original_ledgers",
                    "validate_accounting_start_input",
                    "prepare_dispatch",
                    "validate_planned",
                    "render_event_card",
                    "start_exact_token_accounting",
                    "register_participant_001",
                    "commit_accounting_boundary",
                ],
                [step["name"] for step in receipt["steps"]],
            )
            flattened = " ".join(
                part for step in receipt["steps"] for part in step["command"]
            )
            for forbidden in ("spawn_agent", "wait_agent", "join_agents", "queue_agent"):
                self.assertNotIn(forbidden, flattened)
            self.assertEqual(
                workflow_controller.HOST_OWNED_BOUNDARIES,
                receipt["host_owned_boundaries"],
            )
            self.assertEqual(
                {
                    "present": True,
                    "sha256": "sha256:" + hashlib.sha256(original_usage).hexdigest(),
                },
                receipt["original_ledger_state"]["token-usage.json"],
            )
            self.assertEqual(
                {"present": False},
                receipt["original_ledger_state"]["token-evidence.json"],
            )
            usage = json.loads((workflow / "token-usage.json").read_text(encoding="utf-8"))
            self.assertEqual("lead", usage["accounting"]["lead_session_id"])
            self.assertEqual(1, len(usage["accounting"]["participants"]))
            evidence = json.loads(
                (workflow / "token-evidence.json").read_text(encoding="utf-8")
            )
            self.assertEqual(token_accounting.TOKEN_EVIDENCE_SCHEMA, evidence["schema_version"])
            self.assertEqual("lead", evidence["lead_session_id"])
            participant = usage["accounting"]["participants"][0]
            self.assertEqual("reuse_existing_session", participant["registration_mode"])
            self.assertEqual("reviewer", participant["start_snapshot"]["session_id"])
            planned_failures: list[str] = []
            verify_workflow.validate_token_usage(
                workflow, planned_failures, "planned"
            )
            self.assertTrue(
                any("must remain unstarted in planned mode" in item for item in planned_failures)
            )

            (workflow / "runner-evidence.json").write_text(
                json.dumps(
                    {
                        "agents": [
                            {
                                "round_id": "round-001",
                                "lane_id": "verify-01",
                                "agent_id": "reviewer",
                                "native_handle": "/root/reviewer",
                                "attempt_kind": "planned_reuse",
                                "wait_status": "pending",
                                "close_status": "open",
                                "status": "running",
                            }
                        ],
                        "execution_efficiency": {
                            "lead_model_completions": 0,
                            "status_only_completions": 0,
                            "functions_wait_calls": 0,
                            "wait_waves": [],
                            "card_events": [],
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            lead_path = runtime / "sessions" / "rollout-lead.jsonl"
            rows = [
                {
                    "timestamp": "2026-07-11T00:00:01.5Z",
                    "type": "response_item",
                    "payload": {
                        "type": "agent_message",
                        "author": "/root/reviewer",
                        "recipient": "/root",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "Message Type: FINAL_ANSWER\nstale pre-followup",
                            }
                        ],
                    },
                },
                {
                    "timestamp": "2026-07-11T00:00:02Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "call_id": "call-followup",
                        "name": "collaboration.followup_task",
                        "arguments": json.dumps({"target": "/root/unrelated"}),
                    },
                },
                {
                    "timestamp": "2026-07-11T00:00:03Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "call-followup",
                        "output": "{}",
                    },
                },
                {
                    "timestamp": "2026-07-11T00:00:04Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "call_id": "call-wait",
                        "name": "collaboration.wait_agent",
                        "arguments": '{"timeout_ms":3600000}',
                    },
                },
                {
                    "timestamp": "2026-07-11T00:00:05Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "call-wait",
                        "output": '{"message":"complete","timed_out":false}',
                    },
                },
                {
                    "timestamp": "2026-07-11T00:00:06Z",
                    "type": "response_item",
                    "payload": {
                        "type": "agent_message",
                        "author": "/root/wrong-author",
                        "recipient": "/root",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "Message Type: FINAL_ANSWER\npost-wait terminal",
                            }
                        ],
                    },
                },
                {
                    "timestamp": "2026-07-11T00:00:07Z",
                    "type": "response_item",
                    "payload": {
                        "type": "agent_message",
                        "author": "/root/reviewer",
                        "recipient": "/root",
                        "content": [
                            {
                                "type": "input_text",
                                "text": (
                                    "Message Type: MESSAGE\nstill working; no "
                                    "Message Type: FINAL_ANSWER yet"
                                ),
                            }
                        ],
                    },
                },
                {
                    "timestamp": "2026-07-11T00:00:08Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "call_id": "call-collect",
                        "name": "functions.exec",
                        "arguments": "{}",
                    },
                },
            ]
            with lead_path.open("a", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row) + "\n")
            with self.assertRaisesRegex(
                ValueError, "raw followup target must bind exactly one"
            ):
                workflow_controller.bind_raw_wait_evidence(
                    workflow, runtime_root=runtime
                )
            lead_path.write_text(
                lead_path.read_text(encoding="utf-8").replace(
                    "/root/unrelated", "/root/reviewer"
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ValueError, "lacks a terminal agent message"
            ):
                workflow_controller.bind_raw_wait_evidence(
                    workflow, runtime_root=runtime
                )
            lead_path.write_text(
                lead_path.read_text(encoding="utf-8").replace(
                    "/root/wrong-author",
                    "/root/reviewer",
                ),
                encoding="utf-8",
            )
            binding = workflow_controller.bind_raw_wait_evidence(
                workflow, runtime_root=runtime
            )
            self.assertEqual("call-followup", binding["followup_call_id"])
            self.assertEqual("call-wait", binding["wait_call_id"])
            runner = json.loads(
                (workflow / "runner-evidence.json").read_text(encoding="utf-8")
            )
            self.assertEqual("terminal", runner["agents"][0]["status"])
            self.assertEqual(1, runner["execution_efficiency"]["functions_wait_calls"])
            self.assertIn(
                "call-wait",
                runner["execution_efficiency"]["wait_waves"][0]["trigger_ref"],
            )

    def test_compound_start_rejects_missing_or_invalid_participants_without_accounting(self) -> None:
        cases = ("missing", "invalid_lane", "missing_session")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                agent_id = "missing-reviewer" if case == "missing_session" else "reviewer"
                workflow, manifest, runtime = self._accounting_start_fixture(
                    Path(temp), agent_id=agent_id
                )
                manifest_arg: Path | None = manifest
                if case == "missing":
                    manifest.unlink()
                elif case == "invalid_lane":
                    value = json.loads(manifest.read_text(encoding="utf-8"))
                    value["participants"][0]["lane_id"] = "unknown-01"
                    manifest.write_text(json.dumps(value) + "\n", encoding="utf-8")
                with mock.patch.object(
                    workflow_controller,
                    "controller_commands",
                    return_value=[("prepare_dispatch", [sys.executable, "-c", "pass"])],
                ):
                    receipt = workflow_controller.run_operation(
                        "start",
                        workflow,
                        f"compound-start-{case}",
                        manifest_path=manifest_arg,
                        runtime="codex",
                        lead_session_id="lead",
                        runtime_root=runtime,
                    )
                self.assertEqual("fail", receipt["status"])
                self.assertFalse(receipt["portable_transaction"]["committed"])
                usage = json.loads(
                    (workflow / "token-usage.json").read_text(encoding="utf-8")
                )
                self.assertIsNone(usage["accounting"]["started_at"])
                self.assertEqual([], usage["accounting"]["participants"])

    def test_compound_start_snapshot_read_failure_never_fabricates_absence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workflow, manifest, runtime = self._accounting_start_fixture(Path(temp))
            real_read_bytes = Path.read_bytes

            def fail_usage_read(path: Path) -> bytes:
                if path.name == "token-usage.json":
                    raise PermissionError("injected unreadable original usage ledger")
                return real_read_bytes(path)

            with mock.patch.object(Path, "read_bytes", new=fail_usage_read):
                receipt = workflow_controller.run_operation(
                    "start",
                    workflow,
                    "compound-start-snapshot-failure",
                    manifest_path=manifest,
                    runtime="codex",
                    lead_session_id="lead",
                    runtime_root=runtime,
                )
            self.assertEqual("fail", receipt["status"])
            self.assertFalse(receipt["portable_transaction"]["committed"])
            usage_state = receipt["original_ledger_state"]["token-usage.json"]
            self.assertIs(usage_state["present"], True)
            self.assertIsNone(usage_state["sha256"])
            self.assertIn("unreadable original usage ledger", usage_state["snapshot_error"])
            self.assertEqual(
                {"present": False},
                receipt["original_ledger_state"]["token-evidence.json"],
            )

    def test_raw_terminal_window_closes_at_first_post_followup_action(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workflow, manifest, runtime = self._accounting_start_fixture(Path(temp))
            with mock.patch.object(
                workflow_controller,
                "controller_commands",
                return_value=[("prepare_dispatch", [sys.executable, "-c", "pass"])],
            ):
                receipt = workflow_controller.run_operation(
                    "start",
                    workflow,
                    "compound-start-window-boundary",
                    manifest_path=manifest,
                    runtime="codex",
                    lead_session_id="lead",
                    runtime_root=runtime,
                )
            self.assertEqual("pass", receipt["status"])
            (workflow / "runner-evidence.json").write_text(
                json.dumps(
                    {
                        "agents": [
                            {
                                "round_id": "round-001",
                                "lane_id": "verify-01",
                                "agent_id": "reviewer",
                                "native_handle": "/root/reviewer",
                                "wait_status": "pending",
                                "close_status": "open",
                                "status": "running",
                            }
                        ],
                        "execution_efficiency": {
                            "lead_model_completions": 0,
                            "status_only_completions": 0,
                            "functions_wait_calls": 0,
                            "wait_waves": [],
                            "card_events": [],
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            rows = [
                {
                    "timestamp": "2026-07-11T00:00:02Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "call_id": "call-followup",
                        "name": "collaboration.followup_task",
                        "arguments": '{"target":"/root/reviewer"}',
                    },
                },
                {
                    "timestamp": "2026-07-11T00:00:03Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "call_id": "call-interrupting-action",
                        "name": "functions.exec",
                        "arguments": "{}",
                    },
                },
                {
                    "timestamp": "2026-07-11T00:00:04Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "call_id": "call-wait",
                        "name": "collaboration.wait_agent",
                        "arguments": '{"timeout_ms":3600000}',
                    },
                },
                {
                    "timestamp": "2026-07-11T00:00:05Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "call-wait",
                        "output": '{"message":"complete","timed_out":false}',
                    },
                },
                {
                    "timestamp": "2026-07-11T00:00:06Z",
                    "type": "response_item",
                    "payload": {
                        "type": "agent_message",
                        "author": "/root/reviewer",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "Message Type: FINAL_ANSWER\nlate terminal",
                            }
                        ],
                    },
                },
            ]
            lead_path = runtime / "sessions" / "rollout-lead.jsonl"
            with lead_path.open("a", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row) + "\n")
            with self.assertRaisesRegex(
                ValueError,
                "non-collaboration action before wait completion",
            ):
                workflow_controller.bind_raw_wait_evidence(
                    workflow, runtime_root=runtime
                )

    def test_compound_start_replacement_failures_restore_absent_and_present_ledgers(self) -> None:
        for initial_evidence in (False, True):
            for fail_index in (1, 2):
                with self.subTest(
                    initial_evidence=initial_evidence,
                    fail_index=fail_index,
                ), tempfile.TemporaryDirectory() as temp:
                    workflow, manifest, runtime = self._accounting_start_fixture(
                        Path(temp), initial_evidence=initial_evidence
                    )
                    originals = {
                        name: ((workflow / name).read_bytes() if (workflow / name).is_file() else None)
                        for name in ("token-usage.json", "token-evidence.json")
                    }
                    real_atomic_write = workflow_controller.atomic_write_bytes
                    commit_calls = 0
                    failed = False

                    def fail_selected(path: Path, payload: bytes) -> None:
                        nonlocal commit_calls, failed
                        if not failed and path.parent == workflow.resolve():
                            commit_calls += 1
                            if commit_calls == fail_index:
                                failed = True
                                real_atomic_write(path, payload)
                                raise OSError(
                                    f"injected ledger replacement failure {fail_index}"
                                )
                        real_atomic_write(path, payload)

                    with (
                        mock.patch.object(
                            workflow_controller,
                            "controller_commands",
                            return_value=[
                                ("prepare_dispatch", [sys.executable, "-c", "pass"])
                            ],
                        ),
                        mock.patch.object(
                            workflow_controller,
                            "atomic_write_bytes",
                            side_effect=fail_selected,
                        ),
                    ):
                        receipt = workflow_controller.run_operation(
                            "start",
                            workflow,
                            f"compound-start-rollback-{initial_evidence}-{fail_index}",
                            manifest_path=manifest,
                            runtime="codex",
                            lead_session_id="lead",
                            runtime_root=runtime,
                        )
                    self.assertEqual("fail", receipt["status"])
                    self.assertFalse(receipt["portable_transaction"]["committed"])
                    for name, payload in originals.items():
                        path = workflow / name
                        recorded = receipt["original_ledger_state"][name]
                        if payload is None:
                            self.assertEqual({"present": False}, recorded)
                            self.assertFalse(path.exists(), f"orphan ledger: {name}")
                        else:
                            self.assertEqual(
                                {
                                    "present": True,
                                    "sha256": "sha256:"
                                    + hashlib.sha256(payload).hexdigest(),
                                },
                                recorded,
                            )
                            self.assertEqual(payload, path.read_bytes())

    def test_compound_collect_writes_terminal_state_and_integration(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workflow = Path(temp)
            lane_dir = workflow / "rounds" / "round-001" / "lane-runs"
            lane_dir.mkdir(parents=True)
            (workflow / "state.json").write_text(
                json.dumps(
                    {
                        "status": "planned",
                        "final_status": "pending",
                        "current_round": "round-001",
                        "rounds": [
                            {
                                "round_id": "round-001",
                                "status": "planned",
                                "gate_decision": "pending",
                            }
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (workflow / "orchestration.json").write_text(
                json.dumps(
                    {
                        "rounds": [
                            {
                                "round_id": "round-001",
                                "lanes": [
                                    {"id": "verify-01", "lane": "verify", "enabled": True}
                                ],
                            }
                        ]
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (lane_dir / "verify-01.json").write_text(
                json.dumps(
                    {
                        "status": "complete",
                        "lane": "verify",
                        "summary": "Independent terminal verification passed.",
                        "findings": [],
                        "gate": {"decision": "pass"},
                        "payload": {
                            "checks": [
                                {
                                    "name": "compound collect",
                                    "status": "pass",
                                    "evidence": "lane output records compound collect",
                                }
                            ],
                            "remaining_uncertainty": [],
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            result = workflow_controller.integrate_terminal_lane_outputs(workflow)
            self.assertEqual("pass", result["gate_decision"])
            state = json.loads((workflow / "state.json").read_text(encoding="utf-8"))
            integration = json.loads(
                (workflow / "rounds/round-001/integration.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual("complete", state["final_status"])
            self.assertEqual("verify_pass", integration["stop_reason"])
            self.assertEqual(1, len(integration["verification_evidence"]))

    def test_failed_compound_collect_does_not_publish_staged_terminal_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workflow = Path(temp) / "workflow"
            workflow.mkdir()
            original_state = {
                "status": "planned",
                "final_status": "pending",
            }
            (workflow / "state.json").write_text(
                json.dumps(original_state) + "\n", encoding="utf-8"
            )

            def staged_integration(staged: Path) -> dict[str, Any]:
                (staged / "state.json").write_text(
                    json.dumps({"status": "complete", "final_status": "complete"})
                    + "\n",
                    encoding="utf-8",
                )
                return {
                    "round_id": "round-001",
                    "lane_count": 1,
                    "gate_decision": "pass",
                    "verification_evidence_count": 1,
                }

            with (
                mock.patch.object(
                    workflow_controller,
                    "bind_raw_wait_evidence",
                    return_value={"wait_call_id": "call-wait"},
                ),
                mock.patch.object(
                    workflow_controller,
                    "integrate_terminal_lane_outputs",
                    side_effect=staged_integration,
                ),
                mock.patch.object(
                    workflow_controller,
                    "controller_commands",
                    return_value=[
                        (
                            "emit_lane_receipt_round-001_verify-01",
                            [sys.executable, "-c", "raise SystemExit(1)"],
                        )
                    ],
                ),
            ):
                receipt = workflow_controller.run_operation(
                    "collect", workflow, "collect-staged-failure"
                )
            self.assertEqual("fail", receipt["status"])
            self.assertFalse(receipt["portable_transaction"]["committed"])
            self.assertFalse(
                receipt["portable_transaction"]["original_mutated_before_commit"]
            )
            self.assertEqual(
                original_state,
                json.loads((workflow / "state.json").read_text(encoding="utf-8")),
            )

    def test_finalize_faults_never_commit_staged_accounting(self) -> None:
        for fault in ("marker_missing", "reconcile_drift", "final_verifier_failure"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temp:
                workflow = Path(temp) / "workflow"
                workflow.mkdir()
                usage = workflow / "token-usage.json"
                pending = '{"status":"pending"}\n'
                usage.write_text(pending, encoding="utf-8")
                (workflow / "token-evidence.json").write_text("{}\n", encoding="utf-8")
                (workflow / "runtime-observations.json").write_text("{}\n", encoding="utf-8")
                (workflow / "runner-evidence.json").write_text("{}\n", encoding="utf-8")
                (workflow / "final-report.md").write_text(
                    "{{WORKFLOW_TOTAL_TOKENS}} {{WORKFLOW_TOKEN_SOURCE}} "
                    "{{WORKFLOW_TOKEN_CONFIDENCE}}\n",
                    encoding="utf-8",
                )

                def staged_commands(operation: str, staged: Path, repo: Path) -> list[tuple[str, list[str]]]:
                    complete = json.dumps(
                        {
                            "status": "complete",
                            "total_tokens": 1,
                            "source": "runtime_session_events",
                            "confidence": "exact",
                        }
                    )
                    return [
                        (
                            "finalize_exact_token_accounting",
                            [
                                sys.executable,
                                "-c",
                                f"from pathlib import Path; Path({str(staged / 'token-usage.json')!r}).write_text({(complete + chr(10))!r})",
                            ],
                        ),
                        ("reconcile_runtime_observations", [sys.executable, "-c", "pass"]),
                    ]

                with (
                    mock.patch.object(workflow_controller, "controller_commands", side_effect=staged_commands),
                    mock.patch.dict(os.environ, {"AGENT_WORKFLOW_FINALIZE_FAULT": fault}),
                ):
                    receipt = workflow_controller.run_operation("finalize", workflow, fault)
                self.assertEqual("fail", receipt["status"])
                self.assertFalse(receipt["portable_transaction"]["committed"])
                self.assertEqual(pending, usage.read_text(encoding="utf-8"))

    def test_terminal_commit_manifest_rejects_mixed_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workflow = Path(temp)
            for name in workflow_controller.TERMINAL_PROJECTIONS:
                (workflow / name).write_text(name + "\n", encoding="utf-8")
            workflow_controller.write_terminal_commit_manifest(workflow)
            failures: list[str] = []
            verify_workflow.validate_terminal_commit_manifest(workflow, failures, "final")
            self.assertEqual([], failures)
            (workflow / "final-report.md").write_text("new revision\n", encoding="utf-8")
            verify_workflow.validate_terminal_commit_manifest(workflow, failures, "final")
            self.assertTrue(any("mixed revision" in item for item in failures))

    def test_new_codex_scaffold_keeps_clean_contract_with_mandatory_routing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            capabilities = json.loads(
                (
                    SCRIPT_DIR.parent
                    / "fixtures"
                    / "model-routing"
                    / "positive.json"
                ).read_text(encoding="utf-8")
            )["capabilities"]
            capabilities["observed_at"] = (
                datetime.now(timezone.utc) - timedelta(minutes=1)
            ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            capability_path = root / "capabilities.json"
            capability_path.write_text(
                json.dumps(capabilities, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            session_path = (
                root / "codex-home" / "sessions" / "rollout-fixture-session.jsonl"
            )
            session_path.parent.mkdir(parents=True)
            session_id = "fixture-session"
            session_path.write_text(
                "\n".join(
                    json.dumps(row)
                    for row in (
                        {"type": "session_meta", "payload": {"id": session_id}},
                        {
                            "timestamp": "2026-07-11T00:00:00Z",
                            "type": "turn_context",
                            "payload": {"model": "gpt-5.6-sol", "effort": "xhigh"},
                        },
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(NEW_WORKFLOW),
                    "Clean default",
                    "--root",
                    str(root),
                    "--runner-mode",
                    "codex_builtin_subagents",
                    "--runtime-capabilities",
                    str(capability_path),
                    "--runtime-session-log",
                    str(session_path),
                    "--runner-capability-evidence",
                    "fixture observed native collaboration",
                    "--lanes",
                    "discover,verify",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
                env={
                    **os.environ,
                    "CODEX_THREAD_ID": session_id,
                    "CODEX_HOME": str(session_path.parent.parent),
                },
            )
            self.assertEqual(0, result.returncode, result.stdout)
            workflow = root / "clean-default"
            orchestration = json.loads(
                (workflow / "orchestration.json").read_text(encoding="utf-8")
            )
            self.assertIn("clean_orchestrator_runtime", orchestration)
            self.assertIn("model_routing", orchestration)
            self.assertEqual(
                "mandatory_native",
                orchestration["model_routing_requirement"]["mode"],
            )
            self.assertEqual(
                "capability_required",
                orchestration["clean_orchestrator_runtime"]["delivery_level"],
            )
            scaffold = subprocess.run(
                [
                    sys.executable,
                    str(VERIFY_WORKFLOW),
                    str(workflow),
                    "--mode",
                    "scaffold",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            self.assertEqual(0, scaffold.returncode, scaffold.stdout)


if __name__ == "__main__":
    unittest.main()
