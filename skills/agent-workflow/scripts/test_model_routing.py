#!/usr/bin/env python3
"""Standard-library regression tests for Agent Workflow model routing."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import collect_results
import model_routing
import token_accounting
import verify_workflow


SKILL_ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = SKILL_ROOT / "assets" / "model-routing-policy.v2.json"
POSITIVE_FIXTURE_PATH = SKILL_ROOT / "fixtures" / "model-routing" / "positive.json"
NEGATIVE_FIXTURE_PATH = SKILL_ROOT / "fixtures" / "model-routing" / "negative.json"
NEW_WORKFLOW = SKILL_ROOT / "scripts" / "new_workflow.py"
VERIFY_WORKFLOW = SKILL_ROOT / "scripts" / "verify_workflow.py"

AUTOMATIC_REQUEST = {
    "source": "automatic",
    "requested_route": None,
    "reason": "",
    "evidence_refs": [],
}


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"Expected JSON object in {path}")
    return value


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")


def refresh_content_digest(value: dict[str, Any]) -> dict[str, Any]:
    value["content_sha256"] = model_routing.canonical_sha256(
        value, omitted_keys=("content_sha256",)
    )
    return value


def route_text(value: dict[str, str] | None) -> str | None:
    if value is None:
        return None
    return f"{value['model']}/{value['effort']}"


def rfc3339(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


class ModelRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.policy = model_routing.validate_policy_snapshot(load_json(POLICY_PATH))
        cls.positive = load_json(POSITIVE_FIXTURE_PATH)
        cls.negative = load_json(NEGATIVE_FIXTURE_PATH)
        cls.capabilities = model_routing.prepare_capability_snapshot(
            cls.positive["capabilities"]
        )
        cls.route_cases = {
            case["name"]: case for case in cls.positive["route_cases"]
        }

    def test_tracked_positive_route_fixtures(self) -> None:
        for case in self.positive["route_cases"]:
            with self.subTest(case=case["name"]):
                floor = model_routing.evaluate_verifier_floor(
                    self.policy, self.capabilities, case["facts"]
                )
                verifier_bindings = (
                    {
                        "verifier_lane_ids": ["verify-floor-01"],
                        "independent_of_lane_ids": ["author-lane-01"],
                        "required_evidence": ["routing contract"],
                    }
                    if floor["required"]
                    else {}
                )
                decision = model_routing.build_planned_decision(
                    self.policy,
                    self.capabilities,
                    packet_id=f"packet-{case['name']}",
                    decision_id=f"decision-{case['name']}",
                    facts=case["facts"],
                    **verifier_bindings,
                )
                validated = model_routing.validate_planned_decision(
                    decision, self.policy, self.capabilities
                )
                self.assertEqual(case["expected_status"], validated["status"])
                self.assertEqual(case["expected_route"], route_text(validated["selected"]))
                self.assertEqual(case["expected_rule"], validated["matched_rule_id"])

    def test_tracked_negative_policy_capability_and_fact_fixtures(self) -> None:
        policy_mutators = {
            "free_text_predicate": self._policy_free_text_predicate,
            "duplicate_priority": self._policy_duplicate_priority,
            "missing_default": self._policy_missing_default,
            "luna_route": self._policy_luna_route,
            "effort_in_policy": self._policy_effort_in_policy,
            "remove_approval_rule": self._policy_remove_approval_rule,
            "remove_high_consequence_rule": self._policy_remove_high_consequence_rule,
            "remove_external_rule": self._policy_remove_external_rule,
            "remove_hard_reversal_rule": self._policy_remove_hard_reversal_rule,
            "remove_judgment_rule": self._policy_remove_judgment_rule,
            "disable_verifier_rules": self._policy_disable_verifier_rules,
            "change_policy_id": self._policy_change_id,
            "change_policy_version": self._policy_change_version,
            "digest_mismatch": self._digest_mismatch,
        }
        for case in self.negative["policy_mutations"]:
            with self.subTest(category="policy", case=case["name"]):
                mutated = policy_mutators[case["mutation"]](copy.deepcopy(self.policy))
                with self.assertRaises(model_routing.RoutingError):
                    model_routing.validate_policy_snapshot(mutated)

        capability_mutators = {
            "unsupported_inherited_effort": self._capability_unsupported_inherited_effort,
            "unlocked_inherited_effort": self._capability_unlocked_inherited_effort,
            "malformed_observed_at": self._capability_malformed_observed_at,
            "digest_mismatch": self._digest_mismatch,
        }
        for case in self.negative["capability_mutations"]:
            with self.subTest(category="capability", case=case["name"]):
                mutated = capability_mutators[case["mutation"]](
                    copy.deepcopy(self.capabilities)
                )
                with self.assertRaises(model_routing.RoutingError):
                    model_routing.validate_capability_snapshot(mutated)

        fact_mutators = {
            "mixed_novelty": self._fact_mixed_novelty,
            "missing_blast_radius": self._fact_missing_blast_radius,
            "unknown_fact": self._fact_unknown,
        }
        base_facts = self.route_cases["terra_bounded_execution"]["facts"]
        for case in self.negative["fact_mutations"]:
            with self.subTest(category="facts", case=case["name"]):
                mutated = fact_mutators[case["mutation"]](copy.deepcopy(base_facts))
                with self.assertRaises(model_routing.RoutingError):
                    model_routing.evaluate_route(
                        self.policy, self.capabilities, mutated, AUTOMATIC_REQUEST
                    )

    def test_tracked_negative_attempt_fixtures(self) -> None:
        decision = self._routine_decision("attempt-fixtures")
        for case in self.negative["attempt_mutations"]:
            with self.subTest(case=case["name"]):
                with tempfile.TemporaryDirectory() as temp:
                    evidence_root = Path(temp)
                    attempt_mutators = {
                        "retry_changes_route": self._attempt_retry_changes_route,
                        "retry_cap_exceeded": self._attempt_retry_cap_exceeded,
                        "fallback_wrong_failure": self._attempt_fallback_wrong_failure,
                        "route_unavailable_wrong_outcome": (
                            self._attempt_route_unavailable_wrong_outcome
                        ),
                        "escalation_without_evidence": (
                            self._attempt_escalation_without_evidence
                        ),
                        "escalation_placeholder_evidence": lambda item: (
                            self._attempt_escalation_placeholder_evidence(
                                item, evidence_root
                            )
                        ),
                        "escalation_wrong_attempt_binding": lambda item: (
                            self._attempt_escalation_wrong_binding(item, evidence_root)
                        ),
                        "route_change_cap_exceeded": lambda item: (
                            self._attempt_route_change_cap_exceeded(item, evidence_root)
                        ),
                        "silent_actual_substitution": (
                            self._attempt_silent_actual_substitution
                        ),
                        "terminal_pointer_drift": self._attempt_terminal_pointer_drift,
                        "planned_digest_drift": self._attempt_planned_digest_drift,
                    }
                    record = attempt_mutators[case["mutation"]](decision)
                    with self.assertRaises(model_routing.RoutingError):
                        model_routing.validate_attempts(
                            record,
                            decision,
                            self.policy,
                            self.capabilities,
                            evidence_root=evidence_root,
                        )

    def test_routing_inherits_effort_without_selecting_it(self) -> None:
        capabilities = copy.deepcopy(self.capabilities)
        capabilities["reasoning_effort"]["value"] = "max"
        refresh_content_digest(capabilities)
        capabilities = model_routing.validate_capability_snapshot(capabilities)
        result = model_routing.evaluate_route(
            self.policy,
            capabilities,
            self.route_cases["terra_bounded_execution"]["facts"],
            AUTOMATIC_REQUEST,
        )
        self.assertEqual("planned", result["status"])
        self.assertEqual(
            {"model": "gpt-5.6-terra", "effort": "max"},
            result["selected"],
        )

    def test_workflow_persists_one_locked_user_session_effort(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workflow = self._scaffold(
                Path(temp),
                "locked-user-effort",
                "codex_builtin_subagents",
                lanes="implement,verify",
                routing=True,
            )
            orchestration = load_json(workflow / "orchestration.json")
            capabilities = load_json(workflow / "runtime-capabilities.json")
            expected = {"source": "user_session", "value": "xhigh", "locked": True}
            self.assertEqual(expected, orchestration["model_routing"]["reasoning_effort"])
            self.assertEqual(expected, capabilities["reasoning_effort"])

            orchestration["model_routing"]["reasoning_effort"]["value"] = "high"
            write_json(workflow / "orchestration.json", orchestration)
            result = self.verify(workflow, "scaffold")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("must match the locked user-session effort", result.stdout)

    def test_capability_recheck_is_explicit_bound_and_fresh(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        snapshot = copy.deepcopy(self.capabilities)
        snapshot["observed_at"] = rfc3339(now - timedelta(days=3))
        refresh_content_digest(snapshot)
        snapshot = model_routing.validate_capability_snapshot(snapshot)
        evidence = {
            "source": "lead_agent",
            "summary": "The lead explicitly rechecked the routed native model inventory.",
            "verified": True,
            "checked_at": rfc3339(now),
            "snapshot_content_sha256": snapshot["content_sha256"],
        }
        model_routing.validate_capability_availability_evidence(
            evidence, snapshot, now=now
        )

        stale = copy.deepcopy(evidence)
        stale["checked_at"] = rfc3339(now - timedelta(days=2))
        with self.assertRaises(model_routing.RoutingError):
            model_routing.validate_capability_availability_evidence(
                stale, snapshot, now=now
            )
        mismatched = copy.deepcopy(evidence)
        mismatched["snapshot_content_sha256"] = "sha256:" + "0" * 64
        with self.assertRaises(model_routing.RoutingError):
            model_routing.validate_capability_availability_evidence(
                mismatched, snapshot, now=now
            )

    def test_supplied_capability_file_does_not_certify_runner(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workflow = self._scaffold(
                Path(temp),
                "routed-no-recheck",
                "codex_builtin_subagents",
                lanes="implement",
                routing=True,
                capability_evidence=False,
            )
            self._materialize_strict_contract(workflow)
            self._plan_routed_lanes(
                workflow,
                {"implement-01": self.route_cases["terra_bounded_execution"]["facts"]},
            )
            result = self.verify(workflow, "planned")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("capability_evidence.verified must be true", result.stdout)
            self.assertIn("explicit fresh capability availability evidence", result.stdout)

    def test_completed_attempt_is_terminal(self) -> None:
        decision = self._routine_decision("completed-terminal")
        initial = self._attempt(
            decision,
            ordinal=1,
            transition="initial",
            route=decision["selected"],
            outcome="completed",
        )
        retry = self._attempt(
            decision,
            ordinal=2,
            transition="retry",
            route=decision["selected"],
            outcome="completed",
            parent_attempt_id=initial["attempt_id"],
        )
        record = self._record(decision, [initial, retry])
        with self.assertRaises(model_routing.RoutingError):
            model_routing.validate_attempts(
                record, decision, self.policy, self.capabilities
            )

    def test_valid_fallback_and_bound_escalation_sequences(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            evidence_root = Path(temp)
            fallback_decision = self._routine_decision("valid-fallback")
            unavailable = self._attempt(
                fallback_decision,
                ordinal=1,
                transition="initial",
                route=fallback_decision["selected"],
                outcome="unavailable",
                failure_class="route_unavailable",
            )
            fallback = self._attempt(
                fallback_decision,
                ordinal=2,
                transition="fallback",
                route={"model": "gpt-5.6-sol", "effort": "xhigh"},
                outcome="completed",
                parent_attempt_id=unavailable["attempt_id"],
            )
            model_routing.validate_attempts(
                self._record(fallback_decision, [unavailable, fallback]),
                fallback_decision,
                self.policy,
                self.capabilities,
                evidence_root=evidence_root,
            )

            escalation_decision = self._routine_decision("valid-escalation")
            insufficient = self._attempt(
                escalation_decision,
                ordinal=1,
                transition="initial",
                route=escalation_decision["selected"],
                outcome="failed",
                failure_class="insufficient_reasoning",
            )
            insufficient["evidence_refs"] = [
                self._write_attempt_evidence(evidence_root, insufficient["attempt_id"])
            ]
            escalated = self._attempt(
                escalation_decision,
                ordinal=2,
                transition="escalation",
                route={"model": "gpt-5.6-sol", "effort": "xhigh"},
                outcome="completed",
                parent_attempt_id=insufficient["attempt_id"],
            )
            model_routing.validate_attempts(
                self._record(
                    escalation_decision,
                    [insufficient, escalated],
                    lane_id="implement-01",
                ),
                escalation_decision,
                self.policy,
                self.capabilities,
                evidence_root=evidence_root,
                require_completed_terminal=True,
            )

    def test_override_evidence_refs_are_safe_existing_and_substantive(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            evidence_root = Path(temp)
            (evidence_root / "orchestration.json").write_text(
                json.dumps(
                    {
                        "route_reason": (
                            "The lead recorded a concrete reason to raise the planned route."
                        )
                    }
                ),
                encoding="utf-8",
            )
            base_request = {
                "source": "lead",
                "requested_route": {"model": "gpt-5.6-sol", "effort": "xhigh"},
                "reason": "Raise the route for the evidenced shared review requirement.",
                "evidence_refs": ["orchestration.json#route_reason"],
            }
            decision = model_routing.build_planned_decision(
                self.policy,
                self.capabilities,
                packet_id="packet-safe-override",
                decision_id="decision-safe-override",
                facts=self.route_cases["terra_bounded_execution"]["facts"],
                request=base_request,
            )
            model_routing.validate_planned_decision(
                decision,
                self.policy,
                self.capabilities,
                evidence_root=evidence_root,
            )

            changed_effort = copy.deepcopy(base_request)
            changed_effort["requested_route"]["effort"] = "high"
            with self.assertRaisesRegex(
                model_routing.RoutingError,
                "cannot override the workflow-inherited reasoning effort",
            ):
                model_routing.build_planned_decision(
                    self.policy,
                    self.capabilities,
                    packet_id="packet-effort-override",
                    decision_id="decision-effort-override",
                    facts=self.route_cases["terra_bounded_execution"]["facts"],
                    request=changed_effort,
                )

            for unsafe_ref in ("../../private/secret", "/tmp/private-secret"):
                request = copy.deepcopy(base_request)
                request["evidence_refs"] = [unsafe_ref]
                with self.subTest(ref=unsafe_ref), self.assertRaises(
                    model_routing.RoutingError
                ):
                    model_routing.build_planned_decision(
                        self.policy,
                        self.capabilities,
                        packet_id="packet-unsafe-override",
                        decision_id="decision-unsafe-override",
                        facts=self.route_cases["terra_bounded_execution"]["facts"],
                        request=request,
                    )

            for bad_ref in ("evidence/missing.json", "evidence/short.txt"):
                if bad_ref.endswith("short.txt"):
                    path = evidence_root / bad_ref
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("short", encoding="utf-8")
                request = copy.deepcopy(base_request)
                request["evidence_refs"] = [bad_ref]
                invalid = model_routing.build_planned_decision(
                    self.policy,
                    self.capabilities,
                    packet_id=f"packet-{Path(bad_ref).stem}",
                    decision_id=f"decision-{Path(bad_ref).stem}",
                    facts=self.route_cases["terra_bounded_execution"]["facts"],
                    request=request,
                )
                with self.subTest(ref=bad_ref), self.assertRaises(
                    model_routing.RoutingError
                ):
                    model_routing.validate_planned_decision(
                        invalid,
                        self.policy,
                        self.capabilities,
                        evidence_root=evidence_root,
                    )

    def test_workspace_modes_and_backward_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            legacy = root / "legacy"
            for directory in (legacy / "packets", legacy / "results"):
                directory.mkdir(parents=True, exist_ok=True)
            for name in ("plan.md", "orchestration.md", "final-report.md"):
                (legacy / name).write_text("# Compatibility fixture\n", encoding="utf-8")
            write_json(
                legacy / "state.json",
                {
                    "title": "Legacy",
                    "slug": "legacy",
                    "status": "planned",
                    "approval": {},
                    "packets": [],
                    "verification": {},
                },
            )
            (legacy / "packets" / "one.md").write_text("packet\n", encoding="utf-8")
            (legacy / "results" / "one.md").write_text("result\n", encoding="utf-8")
            self.assertVerifierPasses(legacy, None)

            manual = self._scaffold(
                root, "manual-off", "manual_simulation", lanes="verify"
            )
            self._materialize_strict_contract(manual)
            self.assertVerifierPasses(manual, "scaffold")
            self.assertVerifierPasses(manual, "planned")

            orchestration = load_json(manual / "orchestration.json")
            orchestration["model_routing"] = {"enabled": False}
            write_json(manual / "orchestration.json", orchestration)
            self.assertVerifierPasses(manual, "planned")
            self._write_lane_output(manual, "verify-01", "verify")
            self.assertVerifierPasses(manual, "executed")
            self._materialize_final_contract(manual)
            self.assertVerifierPasses(manual, "final")

            claude = self._scaffold(
                root,
                "claude-off",
                "claude_code_builtin_subagents",
                lanes="verify",
            )
            self._materialize_strict_contract(claude)
            self.assertNotIn("model_routing", load_json(claude / "orchestration.json"))
            self.assertVerifierPasses(claude, "planned")
            claude_output = self._write_lane_output(
                claude, "verify-01", "verify"
            )
            claude_evidence = load_json(claude / "runner-evidence.json")
            claude_evidence["agents"] = [
                self._unrouted_record(
                    lane_id="verify-01",
                    output_path=str(claude_output.relative_to(claude)),
                    spawn_tool="claude_code_agent_tool",
                    agent_id="claude-fixture-agent",
                )
            ]
            write_json(claude / "runner-evidence.json", claude_evidence)
            self.assertVerifierPasses(claude, "executed")
            self._materialize_final_contract(claude)
            self.assertVerifierPasses(claude, "final")

    def test_routed_workspace_scaffold_planned_and_executed_modes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workflow = self._scaffold(
                Path(temp),
                "routed-modes",
                "codex_builtin_subagents",
                lanes="implement",
                routing=True,
            )
            self.assertVerifierPasses(workflow, "scaffold")
            self._materialize_strict_contract(workflow)
            planned_failure = self.verify(workflow, "planned")
            self.assertNotEqual(0, planned_failure.returncode)
            self.assertIn("routing decision.status", planned_failure.stdout)

            decisions = self._plan_routed_lanes(
                workflow,
                {"implement-01": self.route_cases["terra_bounded_execution"]["facts"]},
            )
            self.assertVerifierPasses(workflow, "planned")

            executed_failure = self.verify(workflow, "executed")
            self.assertNotEqual(0, executed_failure.returncode)
            self.assertIn("missing routed runner attempts", executed_failure.stdout)

            output_path = self._write_lane_output(workflow, "implement-01", "implement")
            record = self._record(
                decisions["implement-01"],
                [
                    self._attempt(
                        decisions["implement-01"],
                        ordinal=1,
                        transition="initial",
                        route=decisions["implement-01"]["selected"],
                        outcome="completed",
                        agent_id="implement-agent",
                        output_path=str(output_path.relative_to(workflow)),
                    )
                ],
                lane_id="implement-01",
            )
            evidence = load_json(workflow / "runner-evidence.json")
            evidence["agents"] = [record]
            write_json(workflow / "runner-evidence.json", evidence)
            self.assertVerifierPasses(workflow, "executed")

    def test_final_requires_completed_terminal_attempts_and_executed_nonpass(self) -> None:
        decision = self._routine_decision("terminal-outcomes")
        routing = {
            "policy": self.policy,
            "capabilities": self.capabilities,
            "decisions": {"round-001:implement-01": decision},
        }
        for case in self.negative["terminal_outcomes"]:
            with self.subTest(outcome=case["outcome"]), tempfile.TemporaryDirectory() as temp:
                attempt = self._attempt(
                    decision,
                    ordinal=1,
                    transition="initial",
                    route=decision["selected"],
                    outcome=case["outcome"],
                    failure_class=case["failure_class"],
                )
                record = self._record(decision, [attempt])
                evidence = {"round-001:implement-01": record}
                final_failures: list[str] = []
                verify_workflow.validate_routed_attempt_ledgers(
                    routing,
                    evidence,
                    "final",
                    final_failures,
                    workflow_dir=Path(temp),
                )
                self.assertTrue(
                    any(
                        "terminal outcome must be completed in final mode" in failure
                        for failure in final_failures
                    ),
                    final_failures,
                )

                output = {
                    "round_id": "round-001",
                    "lane_id": "implement-01",
                    "gate": {"decision": "pass"},
                }
                state = {
                    "rounds": [
                        {"round_id": "round-001", "gate_decision": "pending"}
                    ]
                }
                executed_failures: list[str] = []
                verify_workflow.validate_routed_terminal_outcomes(
                    routing,
                    evidence,
                    [output],
                    state,
                    "executed",
                    executed_failures,
                )
                self.assertTrue(executed_failures)
                output["gate"]["decision"] = "revise"
                executed_failures = []
                verify_workflow.validate_routed_terminal_outcomes(
                    routing,
                    evidence,
                    [output],
                    state,
                    "executed",
                    executed_failures,
                )
                self.assertEqual([], executed_failures)

    def test_collect_results_reports_terminal_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workflow = Path(temp)
            decision = self._routine_decision("collect-outcome")
            unavailable = self._attempt(
                decision,
                ordinal=1,
                transition="initial",
                route=decision["selected"],
                outcome="unavailable",
                failure_class="route_unavailable",
            )
            record = self._record(decision, [unavailable])
            write_json(
                workflow / "orchestration.json",
                {
                    "model_routing": {"enabled": True},
                    "rounds": [
                        {
                            "round_id": "round-001",
                            "lanes": [
                                {
                                    "id": "implement-01",
                                    "enabled": True,
                                    "routing": decision,
                                }
                            ],
                        }
                    ],
                },
            )
            write_json(workflow / "runner-evidence.json", {"agents": [record]})
            lines, warnings = collect_results.collect_routing_summary(workflow)
            self.assertEqual([], warnings)
            self.assertTrue(
                any(
                    "planned status `planned`; terminal outcome `unavailable`" in line
                    for line in lines
                ),
                lines,
            )

    def test_planned_mode_enforces_required_verifier_bindings_and_route(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workflow = self._scaffold(
                Path(temp),
                "planned-verifier-floor",
                "codex_builtin_subagents",
                lanes="implement,verify",
                routing=True,
            )
            self._materialize_strict_contract(workflow)
            facts = {
                "implement-01": self.route_cases["sol_high_consequence"]["facts"],
                "verify-01": {
                    **self.route_cases["terra_bounded_execution"]["facts"],
                    "role": "verify",
                },
            }
            self._plan_routed_lanes(workflow, facts)
            missing_bindings = self.verify(workflow, "planned")
            self.assertNotEqual(0, missing_bindings.returncode)
            self.assertIn("verifier_lane_ids is required before dispatch", missing_bindings.stdout)

            bindings = {
                "implement-01": {
                    "verifier_lane_ids": ["verify-01"],
                    "independent_of_lane_ids": ["implement-01"],
                    "required_evidence": ["routing contract"],
                }
            }
            missing_evidence_bindings = copy.deepcopy(bindings)
            missing_evidence_bindings["implement-01"]["required_evidence"] = []
            self._plan_routed_lanes(
                workflow,
                facts,
                verifier_bindings=missing_evidence_bindings,
            )
            missing_evidence = self.verify(workflow, "planned")
            self.assertNotEqual(0, missing_evidence.returncode)
            self.assertIn("required_evidence is required before dispatch", missing_evidence.stdout)

            self._plan_routed_lanes(workflow, facts, verifier_bindings=bindings)
            self.assertVerifierPasses(workflow, "planned")

            bad_bindings = copy.deepcopy(bindings)
            bad_bindings["implement-01"]["independent_of_lane_ids"].append("missing-author")
            raised_request = {
                "source": "lead",
                "requested_route": {"model": "gpt-5.6-sol", "effort": "xhigh"},
                "reason": "Meet the required verifier route floor before dispatch.",
                "evidence_refs": ["orchestration.json#model_routing"],
            }
            self._plan_routed_lanes(
                workflow,
                facts,
                verifier_bindings=bad_bindings,
            )
            missing_author = self.verify(workflow, "planned")
            self.assertNotEqual(0, missing_author.returncode)
            self.assertIn("independent author lane missing-author must be enabled", missing_author.stdout)

            self._plan_routed_lanes(
                workflow,
                facts,
                requests={"verify-01": raised_request},
                verifier_bindings=bindings,
            )
            self.assertVerifierPasses(workflow, "planned")

    def test_verifier_identity_is_independent_of_every_named_author(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workflow = self._scaffold(
                Path(temp),
                "all-author-independence",
                "codex_builtin_subagents",
                lanes="implement,repair,verify",
                routing=True,
            )
            self._materialize_strict_contract(workflow)
            facts = {
                "implement-01": self.route_cases["sol_high_consequence"]["facts"],
                "repair-01": {
                    **self.route_cases["terra_bounded_execution"]["facts"],
                    "role": "repair",
                },
                "verify-01": {
                    **self.route_cases["terra_bounded_execution"]["facts"],
                    "role": "verify",
                },
            }
            decisions = self._plan_routed_lanes(
                workflow,
                facts,
                requests={
                    "verify-01": {
                        "source": "lead",
                        "requested_route": {
                            "model": "gpt-5.6-sol",
                            "effort": "xhigh",
                        },
                        "reason": "Meet the shared high-consequence verifier floor.",
                        "evidence_refs": ["orchestration.json#model_routing"],
                    }
                },
                verifier_bindings={
                    "implement-01": {
                        "verifier_lane_ids": ["verify-01"],
                        "independent_of_lane_ids": ["implement-01", "repair-01"],
                        "required_evidence": ["routing contract"],
                    }
                },
            )
            self.assertVerifierPasses(workflow, "planned")
            implement_output = self._write_lane_output(
                workflow, "implement-01", "implement"
            )
            verify_output = self._write_lane_output(workflow, "verify-01", "verify")
            records: list[dict[str, Any]] = []
            for lane_id, agent_id, native_handle, output_path in (
                (
                    "implement-01",
                    "implement-agent",
                    None,
                    str(implement_output.relative_to(workflow)),
                ),
                (
                    "repair-01",
                    "repair-agent",
                    "shared-native-handle",
                    "rounds/round-001/lane-runs/repair-01.json",
                ),
                (
                    "verify-01",
                    "shared-native-handle",
                    None,
                    str(verify_output.relative_to(workflow)),
                ),
            ):
                attempt = self._attempt(
                    decisions[lane_id],
                    ordinal=1,
                    transition="initial",
                    route=decisions[lane_id]["selected"],
                    outcome="completed",
                    agent_id=agent_id,
                    native_handle=native_handle,
                    output_path=output_path,
                )
                records.append(
                    self._record(decisions[lane_id], [attempt], lane_id=lane_id)
                )
            by_lane = {
                f"round-001:{record['lane_id']}": record for record in records
            }
            orchestration = load_json(workflow / "orchestration.json")
            failures: list[str] = []
            routing = verify_workflow.validate_model_routing(
                workflow, orchestration, "final", failures
            )
            verify_workflow.validate_routing_verifier_floors(
                workflow,
                routing,
                by_lane,
                verify_workflow.planned_lane_specs(orchestration),
                [load_json(implement_output), load_json(verify_output)],
                "final",
                failures,
            )
            self.assertTrue(
                any(
                    "verifier identity must differ from independent author repair-01"
                    in failure
                    for failure in failures
                ),
                failures,
            )

    def test_final_mode_enforces_verifier_floor_and_swarm_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workflow = self._scaffold(
                Path(temp),
                "routed-final",
                "codex_builtin_subagents",
                lanes="implement,verify",
                routing=True,
            )
            self._materialize_strict_contract(workflow)
            decisions = self._plan_routed_lanes(
                workflow,
                {
                    "implement-01": self.route_cases["sol_high_consequence"]["facts"],
                    "verify-01": {
                        **self.route_cases["terra_bounded_execution"]["facts"],
                        "role": "verify",
                    },
                },
                verifier_bindings={
                    "implement-01": {
                        "verifier_lane_ids": ["verify-01"],
                        "independent_of_lane_ids": ["implement-01"],
                        "required_evidence": ["routing contract"],
                    }
                },
            )
            implement_output = self._write_lane_output(
                workflow, "implement-01", "implement"
            )
            verify_output = self._write_lane_output(
                workflow,
                "verify-01",
                "verify",
                check_name="different check",
            )
            verify_output_value = load_json(verify_output)
            verify_output_value["gate"] = {
                "decision": "revise",
                "reason": "The verifier floor is not met.",
                "next_lanes": ["verify"],
            }
            write_json(verify_output, verify_output_value)
            records = [
                self._record(
                    decisions[lane_id],
                    [
                        self._attempt(
                            decisions[lane_id],
                            ordinal=1,
                            transition="initial",
                            route=decisions[lane_id]["selected"],
                            outcome="completed",
                            agent_id="shared-agent",
                            output_path=str(output.relative_to(workflow)),
                        )
                    ],
                    lane_id=lane_id,
                )
                for lane_id, output in (
                    ("implement-01", implement_output),
                    ("verify-01", verify_output),
                )
            ]
            evidence = load_json(workflow / "runner-evidence.json")
            evidence["agents"] = records
            write_json(workflow / "runner-evidence.json", evidence)
            write_json(
                workflow / "swarm-card.json",
                {
                    "phases": [
                        {
                            "agents": [
                                {
                                    "round_id": "round-001",
                                    "lane_id": lane_id,
                                    "routing": {"route_status": "stale"},
                                }
                                for lane_id in ("implement-01", "verify-01")
                            ]
                        }
                    ]
                },
            )

            final_failure = self.verify(workflow, "final")
            self.assertNotEqual(0, final_failure.returncode)
            self.assertIn("verifier identity must differ", final_failure.stdout)
            self.assertIn(
                "verifier verify-01 must complete with a pass gate",
                final_failure.stdout,
            )
            self.assertIn(
                "missing passing substantive checks: routing contract",
                final_failure.stdout,
            )
            self.assertIn("routing projection drift", final_failure.stdout)

            raised_request = {
                "source": "lead",
                "requested_route": {
                    "model": "gpt-5.6-sol",
                    "effort": "xhigh",
                },
                "reason": "Meet the high-consequence verifier floor.",
                "evidence_refs": ["orchestration.json#verification-floor"],
            }
            decisions = self._plan_routed_lanes(
                workflow,
                {
                    "implement-01": self.route_cases["sol_high_consequence"]["facts"],
                    "verify-01": {
                        **self.route_cases["terra_bounded_execution"]["facts"],
                        "role": "verify",
                    },
                },
                requests={"verify-01": raised_request},
                verifier_bindings={
                    "implement-01": {
                        "verifier_lane_ids": ["verify-01"],
                        "independent_of_lane_ids": ["implement-01"],
                        "required_evidence": ["routing contract"],
                    }
                },
            )
            self.assertVerifierPasses(workflow, "planned")
            verify_output = self._write_lane_output(
                workflow,
                "verify-01",
                "verify",
                check_name="routing contract",
            )
            records = []
            for lane_id, output, agent_id in (
                ("implement-01", implement_output, "implement-agent"),
                ("verify-01", verify_output, "verify-agent"),
            ):
                records.append(
                    self._record(
                        decisions[lane_id],
                        [
                            self._attempt(
                                decisions[lane_id],
                                ordinal=1,
                                transition="initial",
                                route=decisions[lane_id]["selected"],
                                outcome="completed",
                                agent_id=agent_id,
                                output_path=str(output.relative_to(workflow)),
                            )
                        ],
                        lane_id=lane_id,
                    )
                )
            evidence["agents"] = records
            write_json(workflow / "runner-evidence.json", evidence)
            by_lane = {
                f"{record['round_id']}:{record['lane_id']}": record for record in records
            }
            write_json(
                workflow / "swarm-card.json",
                {
                    "phases": [
                        {
                            "agents": [
                                {
                                    "round_id": "round-001",
                                    "lane_id": lane_id,
                                    "routing": model_routing.expected_swarm_projection(
                                        decision, by_lane[f"round-001:{lane_id}"]
                                    ),
                                }
                                for lane_id, decision in decisions.items()
                            ]
                        }
                    ]
                },
            )

            self.assertVerifierPasses(workflow, "executed")
            orchestration = load_json(workflow / "orchestration.json")
            routing_failures: list[str] = []
            routing = verify_workflow.validate_model_routing(
                workflow, orchestration, "final", routing_failures
            )
            self.assertIsNotNone(routing)
            verify_workflow.validate_routing_verifier_floors(
                workflow,
                routing,
                by_lane,
                verify_workflow.planned_lane_specs(orchestration),
                [load_json(implement_output), load_json(verify_output)],
                "final",
                routing_failures,
            )
            verify_workflow.validate_swarm_routing_projection(
                workflow, routing, by_lane, "final", routing_failures
            )
            self.assertEqual([], routing_failures)

            self._materialize_final_contract(workflow)
            self.assertVerifierPasses(workflow, "final")

    @staticmethod
    def _policy_free_text_predicate(value: dict[str, Any]) -> dict[str, Any]:
        value["decision_rules"][0]["match"]["when"] = ["approval is needed"]
        return refresh_content_digest(value)

    @staticmethod
    def _policy_duplicate_priority(value: dict[str, Any]) -> dict[str, Any]:
        value["decision_rules"][1]["priority"] = value["decision_rules"][0]["priority"]
        return refresh_content_digest(value)

    @staticmethod
    def _policy_missing_default(value: dict[str, Any]) -> dict[str, Any]:
        value["decision_rules"].pop()
        return refresh_content_digest(value)

    @staticmethod
    def _policy_luna_route(value: dict[str, Any]) -> dict[str, Any]:
        value["decision_rules"][-1]["effect"] = {"model": "gpt-5.6-luna"}
        return refresh_content_digest(value)

    @staticmethod
    def _policy_effort_in_policy(value: dict[str, Any]) -> dict[str, Any]:
        value["decision_rules"][-1]["effect"] = {
            "model": "gpt-5.6-terra",
            "effort": "medium",
        }
        return refresh_content_digest(value)

    @staticmethod
    def _remove_policy_rule(
        value: dict[str, Any], collection: str, rule_id: str
    ) -> dict[str, Any]:
        value[collection] = [
            rule for rule in value[collection] if rule.get("id") != rule_id
        ]
        return refresh_content_digest(value)

    @classmethod
    def _policy_remove_approval_rule(cls, value: dict[str, Any]) -> dict[str, Any]:
        return cls._remove_policy_rule(value, "decision_rules", "gate.approval_required")

    @classmethod
    def _policy_remove_high_consequence_rule(
        cls, value: dict[str, Any]
    ) -> dict[str, Any]:
        return cls._remove_policy_rule(
            value, "decision_rules", "route.sol.judgment_claim"
        )

    @classmethod
    def _policy_remove_external_rule(cls, value: dict[str, Any]) -> dict[str, Any]:
        return cls._remove_policy_rule(
            value, "decision_rules", "route.sol.external_production"
        )

    @classmethod
    def _policy_remove_hard_reversal_rule(
        cls, value: dict[str, Any]
    ) -> dict[str, Any]:
        return cls._remove_policy_rule(
            value, "decision_rules", "route.sol.hard_reversal"
        )

    @classmethod
    def _policy_remove_judgment_rule(cls, value: dict[str, Any]) -> dict[str, Any]:
        return cls._remove_policy_rule(
            value, "decision_rules", "route.sol.weak_verifiability"
        )

    @staticmethod
    def _policy_disable_verifier_rules(value: dict[str, Any]) -> dict[str, Any]:
        value["verifier_rules"] = [copy.deepcopy(value["verifier_rules"][-1])]
        return refresh_content_digest(value)

    @staticmethod
    def _policy_change_id(value: dict[str, Any]) -> dict[str, Any]:
        value["policy_id"] = "responsibility-routing-codex-v2-weakened"
        return refresh_content_digest(value)

    @staticmethod
    def _policy_change_version(value: dict[str, Any]) -> dict[str, Any]:
        value["policy_version"] = 2
        return refresh_content_digest(value)

    @staticmethod
    def _digest_mismatch(value: dict[str, Any]) -> dict[str, Any]:
        value["content_sha256"] = "sha256:" + "0" * 64
        return value

    @staticmethod
    def _capability_unsupported_inherited_effort(value: dict[str, Any]) -> dict[str, Any]:
        value["reasoning_effort"]["value"] = "unsupported"
        return refresh_content_digest(value)

    @staticmethod
    def _capability_unlocked_inherited_effort(value: dict[str, Any]) -> dict[str, Any]:
        value["reasoning_effort"]["locked"] = False
        return refresh_content_digest(value)

    @staticmethod
    def _capability_malformed_observed_at(value: dict[str, Any]) -> dict[str, Any]:
        value["observed_at"] = "not-a-timestamp"
        return refresh_content_digest(value)

    @staticmethod
    def _fact_mixed_novelty(value: dict[str, Any]) -> dict[str, Any]:
        value["novelty"] = "mixed"
        return value

    @staticmethod
    def _fact_missing_blast_radius(value: dict[str, Any]) -> dict[str, Any]:
        del value["blast_radius"]
        return value

    @staticmethod
    def _fact_unknown(value: dict[str, Any]) -> dict[str, Any]:
        value["risk"] = "high"
        return value

    def _routine_decision(self, suffix: str) -> dict[str, Any]:
        return model_routing.build_planned_decision(
            self.policy,
            self.capabilities,
            packet_id=f"packet-{suffix}",
            decision_id=f"decision-{suffix}",
            facts=self.route_cases["terra_bounded_execution"]["facts"],
        )

    @staticmethod
    def _lifecycle(
        *,
        agent_id: str = "fixture-agent",
        native_handle: str | None = None,
        output_path: str = "rounds/round-001/lane-runs/implement-01.json",
    ) -> dict[str, Any]:
        return {
            "execution_kind": "lead_recorded_native",
            "evidence_level": "lead_recorded",
            "agent_id": agent_id,
            "native_handle": native_handle,
            "spawn_tool": "multi_agent_v1",
            "wait_status": "completed",
            "close_status": "closed",
            "output_path": output_path,
        }

    def _attempt(
        self,
        decision: dict[str, Any],
        *,
        ordinal: int,
        transition: str,
        route: dict[str, str],
        outcome: str,
        failure_class: str | None = None,
        evidence_refs: list[str] | None = None,
        parent_attempt_id: str | None = None,
        actual_route: dict[str, str] | None | object = ...,
        agent_id: str = "fixture-agent",
        native_handle: str | None = None,
        output_path: str = "rounds/round-001/lane-runs/implement-01.json",
    ) -> dict[str, Any]:
        if actual_route is ...:
            actual_route = copy.deepcopy(route) if outcome == "completed" else None
        return {
            "attempt_id": f"{decision['decision_id']}-attempt-{ordinal}",
            "ordinal": ordinal,
            "transition": transition,
            "parent_attempt_id": parent_attempt_id,
            "decision_id": decision["decision_id"],
            "planned_decision_sha256": decision["decision_sha256"],
            "route": copy.deepcopy(route),
            "actual_route": copy.deepcopy(actual_route),
            "outcome": outcome,
            "failure_class": failure_class,
            "evidence_refs": list(evidence_refs or []),
            "lifecycle": self._lifecycle(
                agent_id=agent_id,
                native_handle=native_handle,
                output_path=output_path,
            ),
        }

    def _record(
        self,
        decision: dict[str, Any],
        attempts: list[dict[str, Any]],
        *,
        lane_id: str = "implement-01",
    ) -> dict[str, Any]:
        terminal = attempts[-1]
        return {
            "round_id": "round-001",
            "lane_id": lane_id,
            "decision_id": decision["decision_id"],
            "planned_decision_sha256": decision["decision_sha256"],
            "terminal_attempt_id": terminal["attempt_id"],
            "attempts": attempts,
            **terminal["lifecycle"],
        }

    @classmethod
    def _unrouted_record(
        cls,
        *,
        lane_id: str,
        output_path: str,
        spawn_tool: str,
        agent_id: str,
    ) -> dict[str, Any]:
        lifecycle = cls._lifecycle(agent_id=agent_id, output_path=output_path)
        lifecycle["spawn_tool"] = spawn_tool
        return {"round_id": "round-001", "lane_id": lane_id, **lifecycle}

    @staticmethod
    def _write_attempt_evidence(
        root: Path,
        attempt_id: str,
        *,
        filename: str = "evidence/reasoning.json",
        fragment: str | None = None,
    ) -> str:
        path = root / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(
            path,
            {
                "attempt_id": attempt_id,
                "finding": "The preceding route did not resolve the evidenced reasoning gap.",
            },
        )
        return f"{filename}#{fragment or attempt_id}"

    def _attempt_retry_changes_route(self, decision: dict[str, Any]) -> dict[str, Any]:
        first = self._attempt(
            decision,
            ordinal=1,
            transition="initial",
            route=decision["selected"],
            outcome="failed",
            failure_class="context_failure",
        )
        second = self._attempt(
            decision,
            ordinal=2,
            transition="retry",
            route={"model": "gpt-5.6-sol", "effort": "xhigh"},
            outcome="completed",
            parent_attempt_id=first["attempt_id"],
        )
        return self._record(decision, [first, second])

    def _attempt_retry_cap_exceeded(self, decision: dict[str, Any]) -> dict[str, Any]:
        first = self._attempt(
            decision,
            ordinal=1,
            transition="initial",
            route=decision["selected"],
            outcome="failed",
            failure_class="context_failure",
        )
        second = self._attempt(
            decision,
            ordinal=2,
            transition="retry",
            route=decision["selected"],
            outcome="failed",
            failure_class="tool_failure",
            parent_attempt_id=first["attempt_id"],
        )
        third = self._attempt(
            decision,
            ordinal=3,
            transition="retry",
            route=decision["selected"],
            outcome="completed",
            parent_attempt_id=second["attempt_id"],
        )
        return self._record(decision, [first, second, third])

    def _attempt_fallback_wrong_failure(self, decision: dict[str, Any]) -> dict[str, Any]:
        first = self._attempt(
            decision,
            ordinal=1,
            transition="initial",
            route=decision["selected"],
            outcome="failed",
            failure_class="tool_failure",
        )
        second = self._attempt(
            decision,
            ordinal=2,
            transition="fallback",
            route={"model": "gpt-5.6-sol", "effort": "xhigh"},
            outcome="completed",
            parent_attempt_id=first["attempt_id"],
        )
        return self._record(decision, [first, second])

    def _attempt_route_unavailable_wrong_outcome(
        self, decision: dict[str, Any]
    ) -> dict[str, Any]:
        attempt = self._attempt(
            decision,
            ordinal=1,
            transition="initial",
            route=decision["selected"],
            outcome="failed",
            failure_class="route_unavailable",
        )
        return self._record(decision, [attempt])

    def _attempt_escalation_without_evidence(
        self, decision: dict[str, Any]
    ) -> dict[str, Any]:
        first = self._attempt(
            decision,
            ordinal=1,
            transition="initial",
            route=decision["selected"],
            outcome="failed",
            failure_class="insufficient_reasoning",
        )
        second = self._attempt(
            decision,
            ordinal=2,
            transition="escalation",
            route={"model": "gpt-5.6-sol", "effort": "xhigh"},
            outcome="completed",
            parent_attempt_id=first["attempt_id"],
        )
        return self._record(decision, [first, second])

    def _attempt_escalation_placeholder_evidence(
        self, decision: dict[str, Any], evidence_root: Path
    ) -> dict[str, Any]:
        first = self._attempt(
            decision,
            ordinal=1,
            transition="initial",
            route=decision["selected"],
            outcome="failed",
            failure_class="insufficient_reasoning",
            evidence_refs=["abcd"],
        )
        (evidence_root / "abcd").write_text(
            f"Substantive fixture evidence for {first['attempt_id']} but no binding fragment.",
            encoding="utf-8",
        )
        second = self._attempt(
            decision,
            ordinal=2,
            transition="escalation",
            route={"model": "gpt-5.6-sol", "effort": "xhigh"},
            outcome="completed",
            parent_attempt_id=first["attempt_id"],
        )
        return self._record(decision, [first, second])

    def _attempt_escalation_wrong_binding(
        self, decision: dict[str, Any], evidence_root: Path
    ) -> dict[str, Any]:
        first = self._attempt(
            decision,
            ordinal=1,
            transition="initial",
            route=decision["selected"],
            outcome="failed",
            failure_class="insufficient_reasoning",
        )
        first["evidence_refs"] = [
            self._write_attempt_evidence(
                evidence_root,
                first["attempt_id"],
                fragment="different-attempt-id",
            )
        ]
        second = self._attempt(
            decision,
            ordinal=2,
            transition="escalation",
            route={"model": "gpt-5.6-sol", "effort": "xhigh"},
            outcome="completed",
            parent_attempt_id=first["attempt_id"],
        )
        return self._record(decision, [first, second])

    def _attempt_route_change_cap_exceeded(
        self, decision: dict[str, Any], evidence_root: Path
    ) -> dict[str, Any]:
        first = self._attempt(
            decision,
            ordinal=1,
            transition="initial",
            route=decision["selected"],
            outcome="unavailable",
            failure_class="route_unavailable",
        )
        second = self._attempt(
            decision,
            ordinal=2,
            transition="fallback",
            route={"model": "gpt-5.6-sol", "effort": "xhigh"},
            outcome="failed",
            failure_class="insufficient_reasoning",
            parent_attempt_id=first["attempt_id"],
        )
        second["evidence_refs"] = [
            self._write_attempt_evidence(evidence_root, second["attempt_id"])
        ]
        third = self._attempt(
            decision,
            ordinal=3,
            transition="escalation",
            route={"model": "gpt-5.6-sol", "effort": "xhigh"},
            outcome="completed",
            parent_attempt_id=second["attempt_id"],
        )
        return self._record(decision, [first, second, third])

    def _attempt_silent_actual_substitution(
        self, decision: dict[str, Any]
    ) -> dict[str, Any]:
        attempt = self._attempt(
            decision,
            ordinal=1,
            transition="initial",
            route=decision["selected"],
            outcome="completed",
            actual_route={"model": "gpt-5.6-sol", "effort": "xhigh"},
        )
        return self._record(decision, [attempt])

    def _attempt_terminal_pointer_drift(
        self, decision: dict[str, Any]
    ) -> dict[str, Any]:
        attempt = self._attempt(
            decision,
            ordinal=1,
            transition="initial",
            route=decision["selected"],
            outcome="completed",
        )
        record = self._record(decision, [attempt])
        record["terminal_attempt_id"] = "stale-attempt"
        return record

    def _attempt_planned_digest_drift(
        self, decision: dict[str, Any]
    ) -> dict[str, Any]:
        attempt = self._attempt(
            decision,
            ordinal=1,
            transition="initial",
            route=decision["selected"],
            outcome="completed",
        )
        record = self._record(decision, [attempt])
        record["planned_decision_sha256"] = "sha256:" + "0" * 64
        return record

    def _scaffold(
        self,
        root: Path,
        slug: str,
        runner_mode: str,
        *,
        lanes: str = "",
        routing: bool = False,
        capability_evidence: bool = True,
    ) -> Path:
        workflow_root = root / "workflows"
        workflow_root.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            str(NEW_WORKFLOW),
            f"Fixture {slug}",
            "--root",
            str(workflow_root),
            "--slug",
            slug,
            "--runner-mode",
            runner_mode,
            "--round-budget",
            "2",
            "--swarm-card",
            "off",
        ]
        if lanes:
            command.extend(["--lanes", lanes])
        if runner_mode != "manual_simulation" and capability_evidence:
            command.extend(
                ["--runner-capability-evidence", "Test harness observed the native surface."]
            )
        if routing:
            capability_input = root / f"{slug}-capabilities.json"
            capabilities = copy.deepcopy(self.positive["capabilities"])
            capabilities["observed_at"] = rfc3339(
                datetime.now(timezone.utc) - timedelta(minutes=1)
            )
            write_json(capability_input, capabilities)
            command.extend(
                [
                    "--model-routing",
                    "codex",
                    "--runtime-capabilities",
                    str(capability_input),
                    "--reasoning-effort",
                    "xhigh",
                ]
            )
        result = subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self.assertEqual(0, result.returncode, result.stdout)
        return workflow_root / slug

    @staticmethod
    def _materialize_strict_contract(workflow: Path) -> None:
        orchestration = load_json(workflow / "orchestration.json")
        orchestration["workflow"]["goal"] = "Exercise the tracked routing contract."
        orchestration["workflow"]["success_criteria"] = [
            "Routing decisions and evidence satisfy the portable contract."
        ]
        orchestration["rounds"][0]["objective"] = "Exercise model routing gates."
        for lane in orchestration["rounds"][0]["lanes"]:
            lane["purpose"] = f"Exercise the {lane['lane']} routing contract."
            lane["prompt"] = (
                f"Validate the {lane['lane']} contract and return JSON only using "
                "the Agent Workflow lane-output envelope."
            )
        write_json(workflow / "orchestration.json", orchestration)
        state = load_json(workflow / "state.json")
        state["rounds"][0]["objective"] = "Exercise model routing gates."
        write_json(workflow / "state.json", state)

    @staticmethod
    def _materialize_final_contract(workflow: Path) -> None:
        state = load_json(workflow / "state.json")
        state["status"] = "complete"
        state["final_status"] = "complete"
        state["rounds"][0]["status"] = "complete"
        state["rounds"][0]["gate_decision"] = "pass"
        write_json(workflow / "state.json", state)

        integration_path = workflow / "rounds/round-001/integration.json"
        integration = load_json(integration_path)
        integration.update(
            {
                "status": "complete",
                "accepted": ["The routed fixture contract and verifier evidence."],
                "verification_evidence": [
                    {
                        "check": "routing contract",
                        "status": "pass",
                        "evidence": (
                            "rounds/round-001/lane-runs/verify-01.json records "
                            "the passing routing contract check."
                        ),
                    }
                ],
                "next_round": None,
                "stop_reason": "verify_pass",
            }
        )
        write_json(integration_path, integration)

        runtime = (
            "claude"
            if state["runner_mode"] == "claude_code_builtin_subagents"
            else "codex"
        )
        token_runtime = workflow / "rounds" / "round-001" / "fixtures" / "token-runtime"
        lead_path = token_runtime / "fixture-lead.jsonl"
        end_usage = {
            **token_accounting.zero_usage(reasoning_available=runtime == "codex"),
            "input_tokens": 1000,
            "output_tokens": 234,
            "total_tokens": 1234,
        }
        if runtime == "codex":
            append_jsonl(
                lead_path,
                {
                    "timestamp": "2026-07-10T00:00:00+00:00",
                    "type": "session_meta",
                    "payload": {
                        "id": "fixture-lead",
                        "session_id": "fixture-lead",
                        "parent_thread_id": None,
                        "timestamp": "2026-07-10T00:00:00+00:00",
                        "thread_source": "cli",
                    },
                },
            )
            for timestamp, usage in (
                ("2026-07-10T00:00:01+00:00", token_accounting.zero_usage()),
                ("2026-07-10T00:10:00+00:00", end_usage),
            ):
                append_jsonl(
                    lead_path,
                    {
                        "timestamp": timestamp,
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "total_token_usage": {
                                    "input_tokens": usage["input_tokens"],
                                    "cached_input_tokens": usage["cached_input_tokens"],
                                    "output_tokens": usage["output_tokens"],
                                    "reasoning_output_tokens": usage["reasoning_tokens"],
                                    "total_tokens": usage["total_tokens"],
                                }
                            },
                        },
                    },
                )
                if usage["total_tokens"] == 0:
                    start_snapshot = token_accounting.parse_codex_session(
                        lead_path, "fixture-lead"
                    )
            end_snapshot = token_accounting.parse_codex_session(lead_path, "fixture-lead")
        else:
            append_jsonl(
                lead_path,
                {
                    "timestamp": "2026-07-10T00:00:00+00:00",
                    "type": "user",
                    "sessionId": "fixture-lead",
                    "message": {"role": "user", "content": "fixture"},
                },
            )
            for message_id, timestamp, usage in (
                (
                    "fixture-start",
                    "2026-07-10T00:00:01+00:00",
                    token_accounting.zero_usage(reasoning_available=False),
                ),
                ("fixture-end", "2026-07-10T00:10:00+00:00", end_usage),
            ):
                append_jsonl(
                    lead_path,
                    {
                        "timestamp": timestamp,
                        "type": "assistant",
                        "sessionId": "fixture-lead",
                        "message": {
                            "id": message_id,
                            "role": "assistant",
                            "stop_reason": "end_turn",
                            "content": [{"type": "text", "text": "fixture"}],
                            "usage": {
                                "input_tokens": usage["input_tokens"],
                                "cache_creation_input_tokens": usage[
                                    "cache_creation_input_tokens"
                                ],
                                "cache_read_input_tokens": usage["cache_read_input_tokens"],
                                "output_tokens": usage["output_tokens"],
                            },
                        },
                    },
                )
                if usage["total_tokens"] == 0:
                    start_snapshot = token_accounting.parse_claude_session(
                        lead_path, "fixture-lead"
                    )
            end_snapshot = token_accounting.parse_claude_session(lead_path, "fixture-lead")
        measurements = [{
            "subject_kind": "lead",
            "subject_id": "fixture-lead",
            "execution_refs": ["lead"],
            "start": start_snapshot,
            "end": end_snapshot,
            "delta": end_usage,
            "delta_tokens": 1234,
        }]
        runner_evidence = load_json(workflow / "runner-evidence.json")
        participants: list[dict[str, str]] = []
        by_agent: dict[str, list[str]] = {}
        for record in runner_evidence.get("agents", []):
            if not isinstance(record, dict):
                continue
            round_id = str(record.get("round_id"))
            lane_id = str(record.get("lane_id"))
            attempts = record.get("attempts")
            if isinstance(attempts, list) and attempts:
                lifecycle_rows = [
                    (
                        str(attempt.get("attempt_id")),
                        attempt.get("lifecycle") if isinstance(attempt, dict) else None,
                    )
                    for attempt in attempts
                ]
            else:
                lifecycle_rows = [("native", record)]
            for attempt_id, lifecycle in lifecycle_rows:
                if not isinstance(lifecycle, dict):
                    continue
                agent_id = lifecycle.get("agent_id") or lifecycle.get("native_handle")
                if not isinstance(agent_id, str) or not agent_id:
                    continue
                execution_ref = f"{round_id}:{lane_id}:{attempt_id}"
                participants.append(
                    {
                        "execution_ref": execution_ref,
                        "agent_id": agent_id,
                        "round_id": round_id,
                        "lane_id": lane_id,
                    }
                )
                by_agent.setdefault(agent_id, []).append(execution_ref)
        agent_evidence = []
        for index, (agent_id, refs) in enumerate(sorted(by_agent.items()), start=1):
            agent_start_usage = token_accounting.zero_usage(
                reasoning_available=runtime == "codex"
            )
            agent_end_usage = {
                **token_accounting.zero_usage(reasoning_available=runtime == "codex"),
                "input_tokens": 8,
                "output_tokens": 2,
                "total_tokens": 10,
            }
            agent_path = token_runtime / f"{agent_id}.jsonl"
            if runtime == "codex":
                append_jsonl(
                    agent_path,
                    {
                        "timestamp": "2026-07-10T00:01:00+00:00",
                        "type": "session_meta",
                        "payload": {
                            "id": agent_id,
                            "session_id": "fixture-lead",
                            "parent_thread_id": "fixture-lead",
                            "timestamp": "2026-07-10T00:01:00+00:00",
                            "thread_source": "subagent",
                        },
                    },
                )
                append_jsonl(
                    agent_path,
                    {
                        "timestamp": "2026-07-10T00:09:00+00:00",
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "total_token_usage": {
                                    "input_tokens": 8,
                                    "cached_input_tokens": 0,
                                    "output_tokens": 2,
                                    "reasoning_output_tokens": 0,
                                    "total_tokens": 10,
                                }
                            },
                        },
                    },
                )
                append_jsonl(
                    agent_path,
                    {
                        "timestamp": "2026-07-10T00:09:01+00:00",
                        "type": "event_msg",
                        "payload": {"type": "task_complete"},
                    },
                )
                agent_end = token_accounting.parse_codex_session(agent_path, agent_id)
            else:
                append_jsonl(
                    agent_path,
                    {
                        "timestamp": "2026-07-10T00:01:00+00:00",
                        "type": "user",
                        "sessionId": "fixture-lead",
                        "agentId": token_accounting.normalize_agent_id(agent_id),
                        "message": {"role": "user", "content": "fixture"},
                    },
                )
                append_jsonl(
                    agent_path,
                    {
                        "timestamp": "2026-07-10T00:09:00+00:00",
                        "type": "assistant",
                        "sessionId": "fixture-lead",
                        "agentId": token_accounting.normalize_agent_id(agent_id),
                        "message": {
                            "id": f"fixture-{index}",
                            "role": "assistant",
                            "stop_reason": "end_turn",
                            "content": [{"type": "text", "text": "fixture"}],
                            "usage": {
                                "input_tokens": 8,
                                "cache_creation_input_tokens": 0,
                                "cache_read_input_tokens": 0,
                                "output_tokens": 2,
                            },
                        },
                    },
                )
                agent_end = token_accounting.parse_claude_session(agent_path, agent_id)
            agent_start = token_accounting._usage_snapshot(
                runtime=runtime,
                session_id=agent_id,
                usage=agent_start_usage,
                event_ref=f"{runtime}:{agent_id}:session-origin",
                event_sha256=token_accounting.canonical_sha256(
                    {"runtime": runtime, "session_id": agent_id, "origin": 0}
                ),
                captured_at="2026-07-10T00:01:00+00:00",
                terminal=False,
                source_path=agent_path,
            )
            if runtime == "claude":
                agent_start["message_ids"] = []
            measurements.append(
                {
                    "subject_kind": "agent_session",
                    "subject_id": agent_id,
                    "execution_refs": sorted(refs),
                    "start": agent_start,
                    "end": agent_end,
                    "delta": agent_end_usage,
                    "delta_tokens": 10,
                }
            )
            agent_evidence.append(
                {
                    "agent_id": agent_id,
                    "execution_refs": sorted(refs),
                    "round_ids": sorted(
                        {item["round_id"] for item in participants if item["agent_id"] == agent_id}
                    ),
                    "lane_ids": sorted(
                        {item["lane_id"] for item in participants if item["agent_id"] == agent_id}
                    ),
                    "end": agent_end,
                }
            )
        aggregate = token_accounting.add_usage(
            [measurement["delta"] for measurement in measurements]
        )
        expected_refs = ["lead"] + sorted(
            item["execution_ref"] for item in participants
        )
        token_total = int(aggregate["total_tokens"])
        token_evidence = {
            "schema_version": token_accounting.TOKEN_EVIDENCE_SCHEMA,
            "runtime": runtime,
            "lead_session_id": "fixture-lead",
            "started_at": "2026-07-10T00:00:00+00:00",
            "finalized_at": "2026-07-10T00:10:00+00:00",
            "lead": {"start": start_snapshot, "end": end_snapshot},
            "agents": agent_evidence,
        }
        write_json(workflow / "token-evidence.json", token_evidence)
        token_usage = token_accounting.new_token_usage()
        token_usage.update(
            {
                "status": "complete",
                "source": "runtime_session_events",
                "confidence": "exact",
                "total_tokens": aggregate["total_tokens"],
                "input_tokens": aggregate["input_tokens"],
                "cached_input_tokens": aggregate["cached_input_tokens"],
                "cache_creation_input_tokens": aggregate["cache_creation_input_tokens"],
                "cache_read_input_tokens": aggregate["cache_read_input_tokens"],
                "output_tokens": aggregate["output_tokens"],
                "reasoning_tokens": aggregate["reasoning_tokens"],
                "method": "Exact fixture delta from native runtime session events.",
                "accounting": {
                    "runtime": runtime,
                    "lead_session_id": "fixture-lead",
                    "started_at": "2026-07-10T00:00:00+00:00",
                    "finalized_at": "2026-07-10T00:10:00+00:00",
                    "participants": participants,
                },
                "measurements": measurements,
                "coverage": {
                    "expected_execution_refs": expected_refs,
                    "covered_execution_refs": expected_refs,
                    "uncovered_execution_refs": [],
                    "overlapping_execution_refs": [],
                },
                "evidence_sha256": token_accounting.file_sha256(
                    workflow / "token-evidence.json"
                ),
                "agent_breakdown": [
                    {
                        "agent_id": measurement["subject_id"],
                        "execution_refs": measurement["execution_refs"],
                        "tokens": measurement["delta_tokens"],
                        "source": "runtime_session_events",
                    }
                    for measurement in measurements
                    if measurement["subject_kind"] == "agent_session"
                ],
            }
        )
        write_json(workflow / "token-usage.json", token_usage)

        fixture_dir = workflow / "rounds/round-001/fixtures"
        fixture_dir.mkdir(parents=True, exist_ok=True)
        write_json(
            fixture_dir / "results.json",
            {
                "schema_version": "agent-loops.fixture-results.v1",
                "expectations_match": True,
                "expectations": {
                    "routing contract": 0,
                    "workflow final validator": 0,
                },
                "results": [
                    {
                        "name": "routing contract",
                        "command": (
                            "python3 skills/agent-workflow/scripts/verify_workflow.py "
                            "<fixture-workflow> --mode executed"
                        ),
                        "exit_code": 0,
                        "status": "pass",
                        "summary": "Executed-mode routing and lane evidence validation passed.",
                    },
                    {
                        "name": "workflow final validator",
                        "command": (
                            "python3 skills/agent-workflow/scripts/verify_workflow.py "
                            "<fixture-workflow> --mode final"
                        ),
                        "exit_code": 0,
                        "status": "pass",
                        "summary": "The complete terminal fixture is expected to pass final mode.",
                    },
                ],
            },
        )
        runner_mode = state["runner_mode"]
        (workflow / "final-report.md").write_text(
            f"""# Final Report: Routing Fixture

## Outcome

The fixture reached a complete terminal state with every enabled lane accepted by the current round integration gate.
Its planned routes, terminal attempts, lane outputs, integration decision, and
token ledger are all present in the temporary workflow workspace. The outcome
therefore exercises the complete routed workflow contract rather than only the
model-routing helper functions.

## Verification Evidence

- routing contract command: `rounds/round-001/fixtures/results.json` records result name routing contract, command `python3 skills/agent-workflow/scripts/verify_workflow.py <fixture-workflow> --mode executed`, exit_code 0, and status pass.
- verify lane: `rounds/round-001/lane-runs/verify-01.json` records check name routing contract, success criterion Routing contract is enforced, status pass, and evidence `rounds/round-001/lane-runs/verify-01.json records routing contract`.
- integration gate: `rounds/round-001/integration.json` records status complete, stop_reason verify_pass, and routing contract verification evidence with status pass.
- workflow final validator command: `rounds/round-001/fixtures/results.json` records result name workflow final validator, command `python3 skills/agent-workflow/scripts/verify_workflow.py <fixture-workflow> --mode final`, exit_code 0, and status pass.
- runner ledger: `runner-evidence.json` records lead-recorded lifecycle fields and does not independently attest the runtime.

The current verify lane also records a substantive passing routing-contract
check. Its evidence binds the named check to the persisted verify output and
the fixture result containing the same command and zero exit code.

## Remaining Risk

The fixture tests deterministic contracts only; its lifecycle and identity entries remain lead-recorded evidence rather than external attestation.
It does not establish real provider availability, performance, token cost, or
model quality. Those runtime properties remain outside this portable fixture.

## Stop Gate

The round stopped on verify_pass after `rounds/round-001/integration.json` recorded a pass decision and no unresolved repair packet.
State, orchestration, integration, and lane gates agree on the same terminal
round. No later round, human gate, blocker, or unresolved P2 finding exists in
this fixture.

## Runner

Runner mode is `{runner_mode}`. The lead-recorded lifecycle ledger names the fixture identities, output paths, and terminal states without claiming independent runtime proof.
The routing attempts are treated as lead-recorded contract data. Swarm Card
fields are checked only as a display projection and never replace runner or
lane evidence.

## Token Usage

Total workflow usage is {token_total} tokens from runtime_session_events accounting with exact confidence, matching the persisted token ledger.
The fixture records exact start/end delta arithmetic and explicitly excludes the
accounting finalizer completion and final user response from its boundary.
""",
            encoding="utf-8",
        )

    def _plan_routed_lanes(
        self,
        workflow: Path,
        facts_by_lane: dict[str, dict[str, Any]],
        *,
        requests: dict[str, dict[str, Any]] | None = None,
        verifier_bindings: dict[str, dict[str, list[str]]] | None = None,
    ) -> dict[str, dict[str, Any]]:
        policy = model_routing.validate_policy_snapshot(
            load_json(workflow / "routing-policy.json")
        )
        capabilities = model_routing.validate_capability_snapshot(
            load_json(workflow / "runtime-capabilities.json")
        )
        orchestration = load_json(workflow / "orchestration.json")
        decisions: dict[str, dict[str, Any]] = {}
        for lane in orchestration["rounds"][0]["lanes"]:
            lane_id = lane["id"]
            binding = (verifier_bindings or {}).get(lane_id, {})
            decision = model_routing.build_planned_decision(
                policy,
                capabilities,
                packet_id=f"packet-{lane_id}",
                decision_id=f"decision-{lane_id}",
                facts=facts_by_lane[lane_id],
                request=(requests or {}).get(lane_id),
                verifier_lane_ids=binding.get("verifier_lane_ids"),
                independent_of_lane_ids=binding.get("independent_of_lane_ids"),
                required_evidence=binding.get("required_evidence"),
            )
            lane["routing"] = decision
            decisions[lane_id] = decision
        write_json(workflow / "orchestration.json", orchestration)
        return decisions

    def _write_lane_output(
        self,
        workflow: Path,
        lane_id: str,
        lane: str,
        *,
        check_name: str = "routing contract",
    ) -> Path:
        relative = f"rounds/round-001/lane-runs/{lane_id}.json"
        evidence = f"{relative} records {check_name}"
        if lane == "implement":
            payload = {
                "changes": [],
                "assumptions": [],
                "tests_or_checks_run": [],
                "needs_review": [],
                "recommended_next_lanes": ["verify"],
            }
            confidence = {
                "self": 0.9,
                "independent": None,
                "source": "fixture",
                "rationale": "The fixture exercises deterministic routing checks.",
            }
        else:
            payload = {
                "checks": [
                    {
                        "name": check_name,
                        "kind": "inspection",
                        "status": "pass",
                        "evidence": evidence,
                    }
                ],
                "success_criteria_status": [
                    {
                        "criterion": "Routing contract is enforced",
                        "status": "pass",
                        "evidence": evidence,
                    }
                ],
                "confidence_drivers": ["Tracked fixture evidence"],
                "remaining_uncertainty": [],
                "recommended_gate": "pass",
            }
            confidence = {
                "self": 0.9,
                "independent": 0.9,
                "source": "fixture_verifier",
                "rationale": "The fixture binds the check to persisted lane evidence.",
            }
        output = {
            "schema_version": "agent-loops.lane-output.v1",
            "run_id": f"round-001-{lane_id}",
            "round_id": "round-001",
            "lane_id": lane_id,
            "lane": lane,
            "status": "complete",
            "summary": f"Completed the {lane} routing fixture.",
            "confidence": confidence,
            "findings": [],
            "gate": {"decision": "pass", "reason": "Fixture checks passed.", "next_lanes": []},
            "payload": payload,
        }
        path = workflow / relative
        write_json(path, output)
        return path

    def verify(self, workflow: Path, mode: str | None) -> subprocess.CompletedProcess[str]:
        command = [sys.executable, str(VERIFY_WORKFLOW), str(workflow)]
        if mode is not None:
            command.extend(["--mode", mode])
        return subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

    def assertVerifierPasses(self, workflow: Path, mode: str | None) -> None:
        result = self.verify(workflow, mode)
        self.assertEqual(0, result.returncode, result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
