#!/usr/bin/env python3
"""Deterministic scenario coverage for raw Clean Orchestrator observations."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import clean_orchestrator
import execution_efficiency
import runtime_harness
import token_accounting


SCRIPT_DIR = Path(__file__).resolve().parent
FIXTURES = SCRIPT_DIR.parent / "fixtures" / "runtime-harness"


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def orchestration_for(value: dict[str, Any]) -> dict[str, Any]:
    rounds: list[dict[str, Any]] = []
    repair_rounds = set(value.get("repair_rounds", []))
    for round_id in value["rounds"]:
        plan = clean_orchestrator.build_round_runtime_contract(
            round_id=round_id,
            objective=f"Deterministic scenario {value['name']} {round_id}",
            lane_ids=[f"{round_id}-reviewer"],
        )
        plan["runtime_mode"] = "bounded_interim"
        plan["dispatch_mode"] = "native_direct_terminal_events"
        plan["completion_budget"]["absolute_coordinator_completions_max"] = 8
        if round_id in repair_rounds:
            plan["completion_budget"]["extra_semantic_repairs"] = 1
            plan["completion_budget"]["deterministic_tool_result_reactivations_max"] = 1
            plan["semantic_gates"].append(
                {
                    "gate_id": f"{round_id}-bounded-repair",
                    "gate_class": "repair_gate",
                    "trigger": "independent reviewer returns an actionable P0 or P1",
                    "allowed_decisions": ["repair", "blocked"],
                }
            )
        clean_orchestrator.seal_round_gate_graph(plan)
        rounds.append(plan)
    return {"rounds": rounds}


def write_codex_fixture(path: Path, value: dict[str, Any]) -> tuple[str, int]:
    session_id = f"fixture-{value['name']}"
    rows: list[dict[str, Any]] = [
        {
            "timestamp": "2026-07-11T00:00:00Z",
            "type": "session_meta",
            "payload": {"id": session_id, "timestamp": "2026-07-11T00:00:00Z"},
        },
        {
            "timestamp": "2026-07-11T00:00:01Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 50,
                        "output_tokens": 10,
                        "reasoning_output_tokens": 2,
                        "total_tokens": 110,
                    },
                    "last_token_usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 50,
                        "output_tokens": 10,
                        "reasoning_output_tokens": 2,
                        "total_tokens": 110,
                    },
                },
            },
        },
    ]
    total_input = 100
    total_output = 10
    events = list(value["events"]) + [
        {
            "round_id": value["rounds"][-1],
            "tool": "functions.exec",
            "output": "sentinel action output outside observed trigger set",
        }
    ]
    for index, event in enumerate(events, start=1):
        call_id = f"call-{index}"
        agent_id = f"agent-{event['round_id']}"
        output_value = (
            json.dumps(
                {
                    "message": event["output"] + " " + agent_id,
                    "timed_out": "timeout" in event["output"].lower(),
                }
            )
            if event["tool"].endswith("wait_agent")
            else event["output"] + " " + agent_id
        )
        rows.extend(
            [
                {
                    "timestamp": f"2026-07-11T00:00:{index * 3:02d}Z",
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call",
                        "call_id": call_id,
                        "name": event["tool"],
                        "input": json.dumps(
                            {
                                "task_name": (
                                    "reviewer_without_round_marker"
                                    if event.get("omit_round_marker")
                                    else f"{event['round_id']}_reviewer"
                                ),
                                "target": agent_id,
                                **({} if event.get("omit_round_marker") else {"round_id": event["round_id"]}),
                            }
                        ),
                    },
                },
                {
                    "timestamp": f"2026-07-11T00:00:{index * 3 + 1:02d}Z",
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call_output",
                        "call_id": call_id,
                        "output": output_value,
                    },
                },
            ]
        )
        total_input += 100 + index
        total_output += 10
        rows.append(
            {
                "timestamp": f"2026-07-11T00:00:{index * 3 + 2:02d}Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": total_input,
                            "cached_input_tokens": 50 + index,
                            "output_tokens": total_output,
                            "reasoning_output_tokens": 2 + index,
                            "total_tokens": total_input + total_output,
                        },
                        "last_token_usage": {
                            "input_tokens": 100 + index,
                            "cached_input_tokens": 50 + index,
                            "output_tokens": 10,
                            "reasoning_output_tokens": 1,
                            "total_tokens": 110 + index,
                        },
                    },
                },
            }
        )
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return session_id, 2


class RuntimeHarnessScenarioTests(unittest.TestCase):
    def test_raw_completion_scenarios(self) -> None:
        for filename in (
            "normal-two-round.json",
            "bounded-repair.json",
            "worker-interruption-timeout.json",
        ):
            value = fixture(filename)
            with self.subTest(scenario=value["name"]), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "session.jsonl"
                session_id, start_line = write_codex_fixture(path, value)
                orchestration = orchestration_for(value)
                participants = [
                    {
                        "agent_id": f"agent-{round_id}",
                        "execution_ref": f"attempt-{round_id}",
                        "round_id": round_id,
                        "lane_id": f"{round_id}-reviewer",
                    }
                    for round_id in value["rounds"]
                ]
                agents = [
                    {
                        "round_id": round_id,
                        "lane_id": f"{round_id}-reviewer",
                        "agent_id": f"agent-{round_id}",
                        "native_handle": f"agent-{round_id}",
                        "attempt_kind": (
                            "repair" if round_id in set(value.get("repair_rounds", [])) else "initial"
                        ),
                    }
                    for round_id in value["rounds"]
                ]
                wait_waves = []
                for index, event in enumerate(value["events"], start=1):
                    if not event["tool"].endswith("wait_agent"):
                        continue
                    target = f"agent-{event['round_id']}"
                    wait_waves.append(
                        {
                            "round_id": event["round_id"],
                            "wave_id": f"wave-{index}",
                            "barrier_id": f"barrier-{event['round_id']}",
                            "trigger_ref": f"fixture:call-{index}",
                            "targets": [target],
                            "terminal_targets": (
                                [] if "timeout" in event["output"].lower() else [target]
                            ),
                        }
                    )
                runner = {
                    "agents": agents,
                    "execution_efficiency": {"wait_waves": wait_waves},
                }
                events = runtime_harness.codex_completion_events(
                    path,
                    session_id,
                    start_line=start_line,
                    orchestration=orchestration,
                    harness={"default_round_id": value["rounds"][-1]},
                    participants=participants,
                    runner_evidence=runner,
                )
                ledger = runtime_harness.completion_projection(orchestration, events)
                clean_orchestrator.validate_completion_density(
                    ledger, orchestration, final=True
                )
                counts = runtime_harness.density_metrics(orchestration, ledger)[
                    "completion_counts"
                ]
                for completion_class, expected in value["expected_classes"].items():
                    self.assertEqual(expected, counts[completion_class])
                self.assertEqual(
                    value["expected_status_wrapper_partial"],
                    counts["status_only"]
                    + counts["wrapper_wait"]
                    + counts["partial_terminal"],
                )
                self.assertTrue(all(event["input_context"] for event in events))
                if "expected_outcome" in value:
                    self.assertIn(value["expected_outcome"], {event["outcome"] for event in events})
                if "barrier_resume" in value:
                    resume = value["barrier_resume"]
                    runtime_harness.validate_barrier_resume(resume)
                    self.assertEqual(resume["attempt_id_before"], resume["attempt_id_after"])
                    self.assertEqual(resume["deadline_before"], resume["deadline_after"])
                    self.assertFalse(resume["duplicate_spawn"])
                    self.assertEqual(resume["terminal_before"], resume["terminal_after"])
                    self.assertEqual(resume["active_before"], resume["active_after"])
                    mutated = dict(resume, duplicate_spawn=True)
                    with self.assertRaisesRegex(
                        runtime_harness.RuntimeHarnessError,
                        "duplicated native dispatch",
                    ):
                        runtime_harness.validate_barrier_resume(mutated)

    def test_semantic_fixture_mutations_fail_closed(self) -> None:
        value = fixture("normal-two-round.json")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "session.jsonl"
            session_id, start_line = write_codex_fixture(path, value)
            with self.assertRaisesRegex(
                runtime_harness.RuntimeHarnessError,
                "unambiguous round binding|runner-bound attempt",
            ):
                runtime_harness.codex_completion_events(
                    path,
                    session_id,
                    start_line=start_line,
                    orchestration=orchestration_for(value),
                    harness={"default_round_id": "round-002"},
                    participants=[],
                    runner_evidence={"agents": [], "execution_efficiency": {"wait_waves": []}},
                )
        repair = fixture("bounded-repair.json")
        orchestration = orchestration_for(repair)
        orchestration["rounds"][0]["semantic_gates"] = [
            orchestration["rounds"][0]["semantic_gates"][0]
        ]
        clean_orchestrator.seal_round_gate_graph(orchestration["rounds"][0])
        with self.assertRaisesRegex(
            ValueError,
            "sealed repair_gate",
        ):
            runtime_harness.classify_completion(
                trigger_tool_name="collaboration.wait_agent",
                trigger_call_id="call-wait",
                trigger_input='{"timeout_ms":900000}',
                trigger_output='{"message":"complete","timed_out":false}',
                action_tool_name="collaboration.followup_task",
                action_input='{"target":"agent-round-001"}',
                round_plan=orchestration["rounds"][0],
                expected_agents=1,
                runner_evidence={
                    "agents": [
                        {"round_id":"round-001","agent_id":"agent-round-001","native_handle":"agent-round-001","attempt_kind":"repair"}
                    ],
                    "execution_efficiency": {"wait_waves": []},
                },
            )

    def test_followup_uses_structured_target_and_attempt_kind(self) -> None:
        plan = clean_orchestrator.build_round_runtime_contract(
            round_id="round-001", objective="reuse", lane_ids=["verify-01"]
        )
        plan["runtime_mode"] = "bounded_interim"
        plan["dispatch_mode"] = "native_direct_terminal_events"
        clean_orchestrator.seal_round_gate_graph(plan)
        runner = {
            "agents": [{
                "round_id": "round-001",
                "agent_id": "session-1",
                "native_handle": "/root/reviewer",
                "attempt_kind": "planned_reuse",
            }],
            "execution_efficiency": {"wait_waves": []},
        }
        action = json.dumps({"target": "/root/reviewer", "message": "ENCRYPTED_BLOB"})
        completion, outcome, gate, evidence = runtime_harness.classify_completion(
            trigger_tool_name="functions.exec",
            trigger_call_id="call-register",
            trigger_input="{}",
            trigger_output="registered",
            action_tool_name="collaboration.followup_task",
            action_input=action,
            round_plan=plan,
            expected_agents=1,
            runner_evidence=runner,
        )
        self.assertEqual("deterministic_tool_result_reactivation", completion)
        self.assertEqual("planned_reuse_dispatch_requested", outcome)
        self.assertIsNone(gate)
        self.assertEqual("session-1", evidence["attempt_agent_id"])

    def test_terminal_input_drift_uses_frozen_dispatch_only_after_complete_transport(self) -> None:
        value = fixture("terminal-input-drift.json")
        self.assertEqual("terminal_transport_contract", value["kind"])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "input.txt"
            source.write_text("dispatch-time", encoding="utf-8")
            refs = [
                {
                    "root": "workspace",
                    "path": "input.txt",
                    "content_sha256": execution_efficiency.file_sha256(source),
                }
            ]
            source.write_text("later-round-change", encoding="utf-8")
            resolved = root.resolve()
            with self.assertRaisesRegex(
                execution_efficiency.ExecutionEfficiencyError,
                "does not match the file",
            ):
                execution_efficiency.validate_bound_input_refs(
                    resolved, resolved, refs, "lane.input_refs", check_content=True
                )
            execution_efficiency.validate_bound_input_refs(
                resolved, resolved, refs, "lane.input_refs", check_content=False
            )
            lane = {
                "id": "verify-01",
                "execution": {"output_path": "rounds/round-001/lane-runs/verify-01.json"},
            }
            output = root / "rounds/round-001/lane-runs/verify-01.json"
            receipt = root / "rounds/round-001/receipts/verify-01.json"
            output.parent.mkdir(parents=True)
            receipt.parent.mkdir(parents=True)
            output.write_text("{}", encoding="utf-8")
            receipt.write_text("{}", encoding="utf-8")
            self.assertEqual(
                "terminal",
                execution_efficiency.lane_transport_state(root, "round-001", lane),
            )
            receipt.unlink()
            self.assertEqual(
                "partial",
                execution_efficiency.lane_transport_state(root, "round-001", lane),
            )

    def test_unattested_successor_fixture_is_rejected(self) -> None:
        value = fixture("unattested-successor.json")
        usage = token_accounting.new_token_usage()
        usage["accounting"].update(
            {
                "runtime": "codex",
                "lead_session_id": "lead-one",
                "started_at": "2026-07-11T00:00:00Z",
                "lead_generations": [
                    {
                        "generation": 1,
                        "session_id": "lead-one",
                        "started_at": "2026-07-11T00:00:00Z",
                    },
                ],
            }
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual([], token_accounting.validate_v2(usage, root, final=False))
            usage["accounting"]["lead_generations"].append(
                {
                    "generation": 2,
                    "session_id": "lead-two",
                    "started_at": "2026-07-11T00:01:00Z",
                }
            )
            (root / "token-usage.json").write_text(json.dumps(usage), encoding="utf-8")
            failures = token_accounting.validate_v2(usage, root, final=False)
        self.assertTrue(
            any(value["expected_error"] in failure for failure in failures), failures
        )


if __name__ == "__main__":
    unittest.main()
