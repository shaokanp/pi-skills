#!/usr/bin/env python3
"""Standard-library regressions for Agent Workflow execution efficiency."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

import execution_efficiency
import new_workflow
import verify_workflow


SKILL_ROOT = Path(__file__).resolve().parents[1]
NEW_WORKFLOW = SKILL_ROOT / "scripts" / "new_workflow.py"
PREPARE_DISPATCH = SKILL_ROOT / "scripts" / "prepare_dispatch.py"
LANE_RECEIPT = SKILL_ROOT / "scripts" / "lane_receipt.py"
COLLECT_RESULTS = SKILL_ROOT / "scripts" / "collect_results.py"
VERIFY_WORKFLOW = SKILL_ROOT / "scripts" / "verify_workflow.py"


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"Expected JSON object in {path}")
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def run_command(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


class ExecutionEfficiencyTests(unittest.TestCase):
    def scaffold(
        self,
        root: Path,
        slug: str,
        runner_mode: str,
        *,
        lanes: str = "discover,verify",
        efficiency: str | None = None,
    ) -> Path:
        workflow_root = root / "workflows"
        command = [
            str(NEW_WORKFLOW),
            f"Fixture {slug}",
            "--root",
            str(workflow_root),
            "--slug",
            slug,
            "--runner-mode",
            runner_mode,
            "--lanes",
            lanes,
        ]
        if runner_mode != "manual_simulation":
            command.extend(
                [
                    "--runner-capability-evidence",
                    "Fixture observed the native subagent surface.",
                ]
            )
        if efficiency is not None:
            command.extend(["--execution-efficiency", efficiency])
        result = run_command(*command)
        self.assertEqual(0, result.returncode, result.stdout)
        return workflow_root / new_workflow.slugify(slug)

    def prepare_planned(self, workflow: Path) -> dict[str, Any]:
        orchestration = load_json(workflow / "orchestration.json")
        orchestration["workflow"]["goal"] = "Exercise isolated dispatch contracts."
        orchestration["workflow"]["success_criteria"] = [
            "Every enabled lane writes a validated artifact and compact receipt."
        ]
        orchestration["rounds"][0]["objective"] = "Exercise bounded native lanes."
        for index, lane in enumerate(orchestration["rounds"][0]["lanes"], start=1):
            lane["purpose"] = f"Answer the bounded {lane['lane']} fixture question."
            output_path = lane["execution"]["output_path"]
            lane["prompt"] = (
                f"Inspect only the declared references for question {index}. Write JSON "
                f"using {lane['output_schema']} to {output_path}."
            )
            lane["execution"]["admission"].update(
                {
                    "decision": "enabled",
                    "unique_question": f"What is fixture question {index}?",
                    "expected_state_change": f"Persist fixture result {index}.",
                    "reason": "This question needs independent judgment.",
                    "deterministic": False,
                    "exception_reason": "",
                }
            )
        write_json(workflow / "orchestration.json", orchestration)
        state = load_json(workflow / "state.json")
        state["rounds"][0]["objective"] = "Exercise bounded native lanes."
        write_json(workflow / "state.json", state)
        evidence = load_json(workflow / "runner-evidence.json")
        evidence["evidence_level"] = "lead_recorded"
        write_json(workflow / "runner-evidence.json", evidence)
        result = run_command(str(PREPARE_DISPATCH), str(workflow))
        self.assertEqual(0, result.returncode, result.stdout)
        return load_json(workflow / "orchestration.json")

    def write_lane_output(self, workflow: Path, lane_id: str, lane_type: str) -> None:
        relative = f"rounds/round-001/lane-runs/{lane_id}.json"
        if lane_type == "discover":
            payload = {
                "sources_read": ["plan.md", "orchestration.md"],
                "current_state": ["The fixture contract is planned."],
                "constraints": ["Use bounded references only."],
                "unknowns": [],
                "risks": [],
                "recommended_next_lanes": ["verify"],
            }
            confidence = {
                "self": 0.9,
                "independent": None,
                "source": "fixture",
                "rationale": "The persisted sources determine the discovery result.",
            }
        else:
            evidence = f"{relative} records the passing efficiency contract inspection."
            payload = {
                "checks": [
                    {
                        "name": "efficiency contract inspection",
                        "kind": "inspection",
                        "status": "pass",
                        "evidence": evidence,
                    }
                ],
                "success_criteria_status": [
                    {
                        "criterion": "Every enabled lane writes a validated artifact and compact receipt.",
                        "status": "pass",
                        "evidence": evidence,
                    }
                ],
                "confidence_drivers": ["Persisted fixture evidence"],
                "remaining_uncertainty": [],
                "recommended_gate": "pass",
            }
            confidence = {
                "self": 0.9,
                "independent": 0.9,
                "source": "fixture verifier",
                "rationale": "The verifier inspects persisted receipt-bound output.",
            }
        write_json(
            workflow / relative,
            {
                "schema_version": "agent-loops.lane-output.v1",
                "run_id": f"round-001-{lane_id}",
                "round_id": "round-001",
                "lane_id": lane_id,
                "lane": lane_type,
                "status": "complete",
                "summary": f"Completed the {lane_type} fixture lane.",
                "confidence": confidence,
                "findings": [],
                "gate": {
                    "decision": "pass",
                    "reason": "Fixture contract passed.",
                    "next_lanes": [],
                },
                "payload": payload,
            },
        )

    def test_native_scaffolds_default_to_isolated_efficiency(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for mode in (
                "codex_builtin_subagents",
                "claude_code_builtin_subagents",
            ):
                with self.subTest(mode=mode):
                    workflow = self.scaffold(root, mode, mode)
                    orchestration = load_json(workflow / "orchestration.json")
                    self.assertEqual(
                        "native_default",
                        orchestration["execution_efficiency"]["activation"],
                    )
                    context = orchestration["execution_efficiency"]["context"]
                    self.assertFalse(context["lead_fork_context"])
                    self.assertFalse(context["lane_fork_context"])
                    for lane in orchestration["rounds"][0]["lanes"]:
                        if mode == "codex_builtin_subagents":
                            self.assertFalse(lane["runner"]["fork_context"])
                        else:
                            self.assertEqual("isolated", lane["runner"]["context_mode"])
                    result = run_command(str(VERIFY_WORKFLOW), str(workflow), "--mode", "scaffold")
                    self.assertEqual(0, result.returncode, result.stdout)

    def test_native_default_accepts_existing_explicit_opt_in_policies(self) -> None:
        policy = execution_efficiency.build_execution_policy("codex_builtin_subagents")
        self.assertEqual("native_default", policy["activation"])
        execution_efficiency.validate_execution_policy(
            policy, "codex_builtin_subagents"
        )

        existing = copy.deepcopy(policy)
        existing["activation"] = "explicit_opt_in"
        execution_efficiency.validate_execution_policy(
            existing, "codex_builtin_subagents"
        )

    def test_manual_and_legacy_workflows_remain_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manual = self.scaffold(
                root,
                "manual-off",
                "manual_simulation",
            )
            self.assertNotIn("execution_efficiency", load_json(manual / "orchestration.json"))
            result = run_command(str(VERIFY_WORKFLOW), str(manual), "--mode", "scaffold")
            self.assertEqual(0, result.returncode, result.stdout)

            rejected = run_command(
                str(NEW_WORKFLOW),
                "Manual native rejection",
                "--root",
                str(root / "rejected"),
                "--runner-mode",
                "manual_simulation",
                "--execution-efficiency",
                "native",
            )
            self.assertNotEqual(0, rejected.returncode)

            legacy = root / "legacy"
            (legacy / "packets").mkdir(parents=True)
            (legacy / "results").mkdir()
            for name in ("plan.md", "orchestration.md", "final-report.md"):
                (legacy / name).write_text("# Fixture\n\nSubstantive legacy content.\n", encoding="utf-8")
            write_json(
                legacy / "state.json",
                {
                    "title": "Legacy fixture",
                    "slug": "legacy-fixture",
                    "status": "complete",
                    "approval": {},
                    "packets": [],
                    "verification": {},
                },
            )
            (legacy / "packets/p-01.md").write_text("# Packet\n\nWork.\n", encoding="utf-8")
            (legacy / "results/r-01.md").write_text("# Result\n\nDone.\n", encoding="utf-8")
            result = run_command(str(VERIFY_WORKFLOW), str(legacy))
            self.assertEqual(0, result.returncode, result.stdout)
            collected = run_command(str(COLLECT_RESULTS), str(legacy))
            self.assertEqual(0, collected.returncode, collected.stdout)
            self.assertIn("Legacy Markdown Results", collected.stdout)

            native_off = self.scaffold(
                root,
                "native-off",
                "codex_builtin_subagents",
                efficiency="off",
            )
            self.assertNotIn(
                "execution_efficiency", load_json(native_off / "orchestration.json")
            )

    def test_disabled_policy_and_exact_runner_bindings(self) -> None:
        policy = execution_efficiency.build_execution_policy("codex_builtin_subagents")
        policy["enabled"] = False
        orchestration = {
            "orchestrator": {"runner_mode": "codex_builtin_subagents"},
            "execution_efficiency": policy,
        }
        with tempfile.TemporaryDirectory() as temp:
            for mode in ("scaffold", "planned", "executed", "final"):
                with self.subTest(mode=mode):
                    failures: list[str] = []
                    result, lanes = verify_workflow.validate_execution_efficiency_contract(
                        Path(temp), orchestration, mode, failures
                    )
                    self.assertIsNone(result)
                    self.assertEqual({}, lanes)
                    self.assertEqual([], failures)

            workflow = self.scaffold(
                Path(temp),
                "runner-binding",
                "codex_builtin_subagents",
                efficiency="off",
            )
            state = load_json(workflow / "state.json")
            orchestration_file = load_json(workflow / "orchestration.json")
            state["runner_adapter"]["dispatch_surface"] = "claude_code_agent_tool"
            orchestration_file["orchestrator"]["runner_adapter"][
                "dispatch_surface"
            ] = "claude_code_agent_tool"
            write_json(workflow / "state.json", state)
            write_json(workflow / "orchestration.json", orchestration_file)
            result = run_command(str(VERIFY_WORKFLOW), str(workflow), "--mode", "scaffold")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("must be multi_agent_v1", result.stdout)

            state["runner_adapter"]["dispatch_surface"] = "multi_agent_v1"
            orchestration_file["orchestrator"]["runner_adapter"][
                "dispatch_surface"
            ] = "multi_agent_v1"
            orchestration_file["rounds"][0]["lanes"][1]["runner"][
                "dispatch_method"
            ] = "lead_owned"
            write_json(workflow / "state.json", state)
            write_json(workflow / "orchestration.json", orchestration_file)
            result = run_command(str(VERIFY_WORKFLOW), str(workflow), "--mode", "scaffold")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("spawn_agent", result.stdout)

    def test_planned_and_executed_validator_bind_dispatch_receipts_and_index(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workflow = self.scaffold(
                Path(temp),
                "validator-integration",
                "codex_builtin_subagents",
            )
            orchestration = self.prepare_planned(workflow)
            planned = run_command(str(VERIFY_WORKFLOW), str(workflow), "--mode", "planned")
            self.assertEqual(0, planned.returncode, planned.stdout)

            missing = run_command(str(VERIFY_WORKFLOW), str(workflow), "--mode", "executed")
            self.assertNotEqual(0, missing.returncode)
            self.assertIn("Missing execution-efficiency lane receipt", missing.stdout)
            self.assertIn("Missing compact integration index", missing.stdout)
            incomplete_collection = run_command(str(COLLECT_RESULTS), str(workflow))
            self.assertNotEqual(0, incomplete_collection.returncode)
            self.assertIn("missing enabled-lane receipt", incomplete_collection.stdout)

            for lane in orchestration["rounds"][0]["lanes"]:
                self.write_lane_output(workflow, lane["id"], lane["lane"])
                receipt = run_command(
                    str(LANE_RECEIPT),
                    str(workflow),
                    "round-001",
                    lane["id"],
                )
                self.assertEqual(0, receipt.returncode, receipt.stdout)
            collected = run_command(str(COLLECT_RESULTS), str(workflow))
            self.assertEqual(0, collected.returncode, collected.stdout)
            executed = run_command(str(VERIFY_WORKFLOW), str(workflow), "--mode", "executed")
            self.assertEqual(0, executed.returncode, executed.stdout)

            stale_target = copy.deepcopy(orchestration)
            stale_target["workflow"]["goal"] = "A different goal after lane execution."
            write_json(workflow / "orchestration.json", stale_target)
            stale = run_command(str(VERIFY_WORKFLOW), str(workflow), "--mode", "executed")
            self.assertNotEqual(0, stale.returncode)
            self.assertIn("workflow_contract_sha256", stale.stdout)
            write_json(workflow / "orchestration.json", orchestration)

            index = load_json(workflow / "integration-index.json")
            index["lane_count"] = 99
            write_json(workflow / "integration-index.json", index)
            tampered = run_command(str(VERIFY_WORKFLOW), str(workflow), "--mode", "executed")
            self.assertNotEqual(0, tampered.returncode)
            self.assertIn("must exactly match", tampered.stdout)

    def test_input_refs_are_existing_digest_bound_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workflow = self.scaffold(
                Path(temp),
                "input-ref-binding",
                "codex_builtin_subagents",
            )
            orchestration = self.prepare_planned(workflow)
            for lane in orchestration["rounds"][0]["lanes"]:
                for input_ref in lane["input_refs"]:
                    self.assertIn(input_ref["root"], {"workflow", "workspace"})
                    self.assertTrue(input_ref["content_sha256"].startswith("sha256:"))

            bad = copy.deepcopy(orchestration)
            bad["rounds"][0]["lanes"][0]["input_refs"][0]["path"] = "."
            write_json(workflow / "orchestration.json", bad)
            result = run_command(str(PREPARE_DISPATCH), str(workflow))
            self.assertNotEqual(0, result.returncode)
            self.assertIn("safe workspace-relative path", result.stdout)

            write_json(workflow / "orchestration.json", orchestration)
            (workflow / "plan.md").write_text(
                (workflow / "plan.md").read_text(encoding="utf-8") + "\nChanged after binding.\n",
                encoding="utf-8",
            )
            result = run_command(str(VERIFY_WORKFLOW), str(workflow), "--mode", "planned")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("content_sha256 does not match", result.stdout)

    def test_digest_drift_and_lane_admission_fail_closed(self) -> None:
        policy = execution_efficiency.build_execution_policy("codex_builtin_subagents")
        lanes = new_workflow.build_lane_specs(
            ["discover", "verify"],
            "codex_builtin_subagents",
            execution_policy=policy,
        )
        orchestration = {
            "rounds": [{"round_id": "round-001", "lanes": lanes}],
        }
        for index, lane in enumerate(lanes, start=1):
            lane["purpose"] = f"Bounded purpose {index}."
            lane["prompt"] = (
                f"Answer question {index} and write JSON using {lane['output_schema']} "
                f"to {lane['execution']['output_path']}."
            )
            lane["execution"]["admission"].update(
                {
                    "decision": "enabled",
                    "unique_question": f"Unique question {index}",
                    "expected_state_change": f"State change {index}",
                    "reason": "Independent judgment is required.",
                    "deterministic": False,
                    "exception_reason": "",
                }
            )
            execution_efficiency.refresh_dispatch_digest(lane, "round-001")
        execution_efficiency.validate_orchestration_efficiency(
            orchestration,
            "codex_builtin_subagents",
            policy,
            allow_draft=False,
        )

        drifted = copy.deepcopy(orchestration)
        drifted["rounds"][0]["lanes"][0]["prompt"] += " Changed after digest."
        with self.assertRaises(execution_efficiency.ExecutionEfficiencyError):
            execution_efficiency.validate_orchestration_efficiency(
                drifted,
                "codex_builtin_subagents",
                policy,
                allow_draft=False,
            )

        deterministic = copy.deepcopy(orchestration)
        deterministic["rounds"][0]["lanes"][0]["execution"]["admission"]["deterministic"] = True
        execution_efficiency.refresh_dispatch_digest(
            deterministic["rounds"][0]["lanes"][0], "round-001"
        )
        with self.assertRaises(execution_efficiency.ExecutionEfficiencyError):
            execution_efficiency.validate_orchestration_efficiency(
                deterministic,
                "codex_builtin_subagents",
                policy,
                allow_draft=False,
            )

        multi_agent_lane = copy.deepcopy(orchestration)
        multi_agent_lane["rounds"][0]["lanes"][0]["agent_count"] = 2
        execution_efficiency.refresh_dispatch_digest(
            multi_agent_lane["rounds"][0]["lanes"][0], "round-001"
        )
        with self.assertRaises(execution_efficiency.ExecutionEfficiencyError):
            execution_efficiency.validate_orchestration_efficiency(
                multi_agent_lane,
                "codex_builtin_subagents",
                policy,
                allow_draft=False,
            )

    def test_payload_registry_and_finding_mirrors_are_canonical(self) -> None:
        self.assertIs(
            verify_workflow.PAYLOAD_REQUIRED_KEYS,
            execution_efficiency.RECEIPT_PAYLOAD_KEYS,
        )
        self.assertEqual(
            (
                "findings",
                "assumptions_attacked",
                "missing_evidence",
                "repair_packets",
                "recommended_next_lanes",
            ),
            execution_efficiency.RECEIPT_PAYLOAD_KEYS["review_payload.v1"],
        )
        finding = {
            "id": "F-1",
            "severity": "P2",
            "claim": "Canonical mirror",
            "evidence": ["source evidence"],
            "recommendation": "Keep one finding",
        }
        output = {"findings": [finding], "payload": {"findings": [copy.deepcopy(finding)]}}
        self.assertEqual(1, len(verify_workflow.all_lane_findings(output)))
        divergent = copy.deepcopy(output)
        divergent["payload"]["findings"][0]["claim"] = "Divergent mirror"
        failures: list[str] = []
        verify_workflow.validate_finding_mirrors(
            divergent, "fixture", failures, "final"
        )
        self.assertTrue(any("diverges" in failure for failure in failures))

    def test_policy_rejects_busy_polling_timeout_and_heartbeat_cards(self) -> None:
        policy = execution_efficiency.build_execution_policy("codex_builtin_subagents")
        short_poll = copy.deepcopy(policy)
        short_poll["wait"]["min_repoll_ms"] = 30000
        with self.assertRaises(execution_efficiency.ExecutionEfficiencyError):
            execution_efficiency.validate_execution_policy(
                short_poll, "codex_builtin_subagents"
            )

        telemetry = {
            "lead_model_completions": 2,
            "status_only_completions": 0,
            "functions_wait_calls": 1,
            "wait_waves": [
                {
                    "wave_id": "wave-01",
                    "barrier_id": "barrier-01",
                    "targets": ["agent-a", "agent-b"],
                    "timeout_ms": 900000,
                    "outcome": "completed",
                    "started_at": "2026-07-10T00:00:00Z",
                    "completed_at": "2026-07-10T00:05:00Z",
                    "trigger": "dispatch",
                    "trigger_ref": "round-001 dispatch",
                    "terminal_targets": ["agent-a", "agent-b"],
                }
            ],
            "card_events": [
                {
                    "reason": "dispatch",
                    "state_changed": True,
                    "rendered_sha256": "sha256:dispatch",
                }
            ],
        }
        execution_efficiency.validate_wait_telemetry(
            telemetry, final=True, policy=policy
        )
        for mutation in ("poll", "timeout", "heartbeat", "thirty_seconds"):
            broken = copy.deepcopy(telemetry)
            if mutation == "poll":
                broken["status_only_completions"] = 1
            elif mutation == "timeout":
                broken["wait_waves"][0]["outcome"] = "timeout"
                broken["wait_waves"][0]["terminal_targets"] = []
            elif mutation == "heartbeat":
                broken["card_events"][0]["state_changed"] = False
            else:
                broken["wait_waves"][0]["timeout_ms"] = 30000
            with self.subTest(mutation=mutation):
                with self.assertRaises(execution_efficiency.ExecutionEfficiencyError):
                    execution_efficiency.validate_wait_telemetry(broken, final=True)

        early_rewait = copy.deepcopy(telemetry)
        early_rewait["functions_wait_calls"] = 2
        early_rewait["wait_waves"][0].update(
            {
                "outcome": "timeout",
                "terminal_targets": [],
                "completed_at": "2026-07-10T00:15:00Z",
            }
        )
        early_rewait["wait_waves"].append(
            {
                "wave_id": "wave-02",
                "barrier_id": "barrier-01",
                "targets": ["agent-a", "agent-b"],
                "timeout_ms": 900000,
                "outcome": "completed",
                "started_at": "2026-07-10T00:15:30Z",
                "completed_at": "2026-07-10T00:16:30Z",
                "trigger": "prior_timeout",
                "terminal_targets": ["agent-b"],
            }
        )
        with self.assertRaises(execution_efficiency.ExecutionEfficiencyError):
            execution_efficiency.validate_wait_telemetry(
                early_rewait, final=False, policy=policy
            )

        compliant_timeout = copy.deepcopy(early_rewait)
        compliant_timeout["wait_waves"][1]["started_at"] = "2026-07-10T00:20:00Z"
        compliant_timeout["wait_waves"][1]["completed_at"] = "2026-07-10T00:21:00Z"
        compliant_timeout["wait_waves"][1]["terminal_targets"] = [
            "agent-a",
            "agent-b",
        ]
        execution_efficiency.validate_wait_telemetry(
            compliant_timeout, final=True, policy=policy
        )

        early_timeout_claim = copy.deepcopy(compliant_timeout)
        early_timeout_claim["wait_waves"][0]["completed_at"] = (
            "2026-07-10T00:14:59Z"
        )
        with self.assertRaises(execution_efficiency.ExecutionEfficiencyError):
            execution_efficiency.validate_wait_telemetry(
                early_timeout_claim, final=True, policy=policy
            )

        repeated_terminal = copy.deepcopy(telemetry)
        repeated_terminal["functions_wait_calls"] = 2
        repeated_terminal["wait_waves"][0]["terminal_targets"] = ["agent-a"]
        repeated_terminal["wait_waves"].append(
            {
                "wave_id": "wave-02",
                "barrier_id": "barrier-01",
                "targets": ["agent-a", "agent-b"],
                "timeout_ms": 900000,
                "outcome": "completed",
                "started_at": "2026-07-10T00:05:01Z",
                "completed_at": "2026-07-10T00:06:00Z",
                "trigger": "prior_terminal_event",
                "terminal_targets": ["agent-b"],
            }
        )
        with self.assertRaises(execution_efficiency.ExecutionEfficiencyError):
            execution_efficiency.validate_wait_telemetry(
                repeated_terminal, final=True, policy=policy
            )

    def test_receipt_detects_raw_output_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workflow = Path(temp)
            policy = execution_efficiency.build_execution_policy("codex_builtin_subagents")
            lane = new_workflow.build_lane_specs(
                ["discover"],
                "codex_builtin_subagents",
                execution_policy=policy,
            )[0]
            self.write_lane_output(workflow, lane["id"], lane["lane"])
            output_path = workflow / lane["execution"]["output_path"]
            valid_output = load_json(output_path)
            malformed = copy.deepcopy(valid_output)
            malformed["status"] = "pass"
            write_json(output_path, malformed)
            with self.assertRaises(execution_efficiency.ExecutionEfficiencyError):
                execution_efficiency.build_lane_receipt(workflow, lane, "round-001")
            write_json(output_path, valid_output)
            receipt = execution_efficiency.build_lane_receipt(workflow, lane, "round-001")
            execution_efficiency.validate_lane_receipt(
                workflow, lane, "round-001", receipt
            )
            output = load_json(output_path)
            output["summary"] = "Tampered after receipt construction."
            write_json(output_path, output)
            with self.assertRaises(execution_efficiency.ExecutionEfficiencyError):
                execution_efficiency.validate_lane_receipt(
                    workflow, lane, "round-001", receipt
                )

    def test_budget_repair_affinity_and_verifier_independence(self) -> None:
        policy = execution_efficiency.build_execution_policy("codex_builtin_subagents")
        lanes = new_workflow.build_lane_specs(
            ["implement", "repair", "review", "verify"],
            "codex_builtin_subagents",
            execution_policy=policy,
        )
        orchestration = {"rounds": [{"round_id": "round-001", "lanes": lanes}]}
        for index, lane in enumerate(lanes, start=1):
            lane["purpose"] = f"Bounded lane {index}."
            lane["prompt"] = (
                f"Perform lane {index}; write JSON using {lane['output_schema']} "
                f"to {lane['execution']['output_path']}."
            )
            lane["execution"]["admission"].update(
                {
                    "decision": "enabled",
                    "unique_question": f"Question {index}",
                    "expected_state_change": f"Change {index}",
                    "reason": "The lane has a distinct responsibility.",
                    "deterministic": False,
                    "exception_reason": "",
                }
            )
            if lane["lane"] == "repair":
                lane["execution"]["repair_affinity"]["source_lane_id"] = "implement-01"
            execution_efficiency.refresh_dispatch_digest(lane, "round-001")
        lanes_by_ref = execution_efficiency.validate_orchestration_efficiency(
            orchestration,
            "codex_builtin_subagents",
            policy,
            allow_draft=False,
        )

        identities = {
            "round-001:implement-01": "writer-a",
            "round-001:repair-01": "writer-a",
            "round-001:review-01": "reviewer-b",
            "round-001:verify-01": "verifier-c",
        }
        evidence: dict[str, dict[str, Any]] = {}
        for lane_ref, lane in lanes_by_ref.items():
            round_id, lane_id = lane_ref.split(":", 1)
            evidence[lane_ref] = {
                "agent_id": identities[lane_ref],
                "execution_metrics": {
                    "model_completions": 1,
                    "tool_turns": 2,
                    "test_runs": 1,
                    "repair_reuse_count": 1 if lane["lane"] == "repair" else 0,
                    "budget_outcome": "within_budget",
                    "context_forked": False,
                    "received_parent_transcript": False,
                    "dispatch_sha256": lane["execution"]["dispatch_sha256"],
                    "receipt_path": execution_efficiency.receipt_relative_path(
                        round_id, lane_id
                    ),
                },
            }
        telemetry = {
            "wait_waves": [
                {
                    "targets": ["writer-a", "reviewer-b", "verifier-c"],
                    "terminal_targets": ["writer-a", "reviewer-b", "verifier-c"],
                }
            ]
        }
        execution_efficiency.validate_agent_execution_evidence(
            lanes_by_ref, evidence, telemetry, final=True
        )

        chained_lanes = copy.deepcopy(lanes_by_ref)
        chained_evidence = copy.deepcopy(evidence)
        repair_two = copy.deepcopy(chained_lanes["round-001:repair-01"])
        repair_two["id"] = "repair-02"
        repair_two["execution"]["output_path"] = (
            "rounds/round-001/lane-runs/repair-02.json"
        )
        repair_two["execution"]["repair_affinity"]["source_lane_id"] = "repair-01"
        repair_two["prompt"] = (
            "Perform chained repair; write JSON using repair_payload.v1 "
            "to rounds/round-001/lane-runs/repair-02.json."
        )
        execution_efficiency.refresh_dispatch_digest(repair_two, "round-001")
        chained_lanes["round-001:repair-02"] = repair_two
        repair_two_evidence = copy.deepcopy(chained_evidence["round-001:repair-01"])
        repair_two_evidence["execution_metrics"]["dispatch_sha256"] = (
            repair_two["execution"]["dispatch_sha256"]
        )
        repair_two_evidence["execution_metrics"]["receipt_path"] = (
            "rounds/round-001/receipts/repair-02.json"
        )
        chained_evidence["round-001:repair-02"] = repair_two_evidence
        with self.assertRaises(execution_efficiency.ExecutionEfficiencyError):
            execution_efficiency.validate_agent_execution_evidence(
                chained_lanes, chained_evidence, telemetry, final=True
            )

        repeated_writer_lanes = copy.deepcopy(lanes_by_ref)
        repeated_writer_evidence = copy.deepcopy(evidence)
        implement_two = copy.deepcopy(repeated_writer_lanes["round-001:implement-01"])
        implement_two["id"] = "implement-02"
        implement_two["execution"]["output_path"] = (
            "rounds/round-001/lane-runs/implement-02.json"
        )
        execution_efficiency.refresh_dispatch_digest(implement_two, "round-001")
        repeated_writer_lanes["round-001:implement-02"] = implement_two
        implement_two_evidence = copy.deepcopy(
            repeated_writer_evidence["round-001:implement-01"]
        )
        implement_two_evidence["execution_metrics"]["dispatch_sha256"] = (
            implement_two["execution"]["dispatch_sha256"]
        )
        implement_two_evidence["execution_metrics"]["receipt_path"] = (
            "rounds/round-001/receipts/implement-02.json"
        )
        repeated_writer_evidence["round-001:implement-02"] = implement_two_evidence
        repair_two = copy.deepcopy(repeated_writer_lanes["round-001:repair-01"])
        repair_two["id"] = "repair-02"
        repair_two["execution"]["output_path"] = (
            "rounds/round-001/lane-runs/repair-02.json"
        )
        repair_two["execution"]["repair_affinity"]["source_lane_id"] = "implement-02"
        execution_efficiency.refresh_dispatch_digest(repair_two, "round-001")
        repeated_writer_lanes["round-001:repair-02"] = repair_two
        repair_two_evidence = copy.deepcopy(
            repeated_writer_evidence["round-001:repair-01"]
        )
        repair_two_evidence["execution_metrics"]["dispatch_sha256"] = (
            repair_two["execution"]["dispatch_sha256"]
        )
        repair_two_evidence["execution_metrics"]["receipt_path"] = (
            "rounds/round-001/receipts/repair-02.json"
        )
        repeated_writer_evidence["round-001:repair-02"] = repair_two_evidence
        with self.assertRaises(execution_efficiency.ExecutionEfficiencyError):
            execution_efficiency.validate_agent_execution_evidence(
                repeated_writer_lanes,
                repeated_writer_evidence,
                telemetry,
                final=True,
            )

        shared_verifier = copy.deepcopy(evidence)
        shared_verifier["round-001:verify-01"]["agent_id"] = "writer-a"
        with self.assertRaises(execution_efficiency.ExecutionEfficiencyError):
            execution_efficiency.validate_agent_execution_evidence(
                lanes_by_ref, shared_verifier, telemetry, final=True
            )

        aliased_verifier = copy.deepcopy(evidence)
        aliased_verifier["round-001:implement-01"]["native_handle"] = "shared-native"
        aliased_verifier["round-001:verify-01"]["native_handle"] = "shared-native"
        with self.assertRaises(execution_efficiency.ExecutionEfficiencyError):
            execution_efficiency.validate_agent_execution_evidence(
                lanes_by_ref, aliased_verifier, telemetry, final=True
            )

        aliased_wait_evidence = copy.deepcopy(evidence)
        aliased_wait_evidence["round-001:implement-01"]["native_handle"] = (
            "writer-native"
        )
        aliased_wait = copy.deepcopy(telemetry)
        aliased_wait["wait_waves"][0]["targets"].append("writer-native")
        aliased_wait["wait_waves"][0]["terminal_targets"].append("writer-native")
        with self.assertRaises(execution_efficiency.ExecutionEfficiencyError):
            execution_efficiency.validate_agent_execution_evidence(
                lanes_by_ref, aliased_wait_evidence, aliased_wait, final=True
            )

        over_budget = copy.deepcopy(evidence)
        over_budget["round-001:implement-01"]["execution_metrics"]["tool_turns"] = 99
        with self.assertRaises(execution_efficiency.ExecutionEfficiencyError):
            execution_efficiency.validate_agent_execution_evidence(
                lanes_by_ref, over_budget, telemetry, final=True
            )

    def test_high_risk_requires_challenge_and_verify(self) -> None:
        policy = execution_efficiency.build_execution_policy("codex_builtin_subagents")
        policy["risk_class"] = "high"
        lanes = new_workflow.build_lane_specs(
            ["discover", "verify"],
            "codex_builtin_subagents",
            execution_policy=policy,
        )
        for index, lane in enumerate(lanes, start=1):
            lane["purpose"] = f"Risk lane {index}."
            lane["prompt"] = (
                f"Assess risk {index}; write JSON using {lane['output_schema']} "
                f"to {lane['execution']['output_path']}."
            )
            lane["execution"]["admission"].update(
                {
                    "decision": "enabled",
                    "unique_question": f"Risk question {index}",
                    "expected_state_change": f"Risk result {index}",
                    "reason": "Independent risk judgment is required.",
                    "deterministic": False,
                    "exception_reason": "",
                }
            )
            execution_efficiency.refresh_dispatch_digest(lane, "round-001")
        with self.assertRaises(execution_efficiency.ExecutionEfficiencyError):
            execution_efficiency.validate_orchestration_efficiency(
                {"rounds": [{"round_id": "round-001", "lanes": lanes}]},
                "codex_builtin_subagents",
                policy,
                allow_draft=False,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
