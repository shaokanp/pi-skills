#!/usr/bin/env python3
"""Slice 0b tests for the stable vNext candidate launcher."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from phase_protocol import validate_contract


SCRIPT_DIR = Path(__file__).resolve().parent


def score_plan(plan: dict[str, object], *, explicit: bool, source_change: bool) -> dict[str, int]:
    """Apply the frozen, label-neutral Slice 6 behavior rubric."""
    if not explicit:
        value = 2 if plan == {"activate": False} else 0
        return {dimension: value for dimension in (
            "plan_correctness", "context_hygiene", "completion_density", "authority"
        )}
    if plan.get("activate") is not True:
        return {dimension: 0 for dimension in (
            "plan_correctness", "context_hygiene", "completion_density", "authority"
        )}
    return {
        "plan_correctness": sum((
            plan.get("phase_materialization") == "next_only" and plan.get("dynamic_phase_expansion") is True,
            plan.get("recovery") == "exact_lineage_once_preserve_siblings",
        )),
        "context_hygiene": sum((
            plan.get("main_action") == "spawn_clean_orchestrator" and plan.get("orchestrator_context") == "sealed_brief_only",
            plan.get("worker_result_surface") == "typed_receipt",
        )),
        "completion_density": sum((
            plan.get("wait_strategy") == "terminal_boundary",
            plan.get("partial_wakes") is False,
        )),
        "authority": sum((
            plan.get("routes") == ["top", "worker"] and plan.get("routing_mandatory") is True,
            (not source_change or plan.get("verification") == "independent_top_read_only")
            and plan.get("external_action") == "human_gate",
        )),
    }


class VNextCandidateTests(unittest.TestCase):
    def test_frozen_blind_behavior_pairs_preserve_candidate_invariants(self) -> None:
        fixture_root = SCRIPT_DIR.parent / "fixtures" / "vnext" / "candidate"
        pairs = json.loads((fixture_root / "behavior-eval-pairs.v1.json").read_text())
        key = json.loads((fixture_root / "behavior-eval-key.v1.json").read_text())
        self.assertEqual(pairs["schema_version"], "agent-workflow.candidate-behavior-pairs.v1")
        self.assertEqual(key["schema_version"], "agent-workflow.candidate-behavior-key.v1")
        self.assertEqual(
            pairs["rubric_dimensions"],
            ["plan_correctness", "context_hygiene", "completion_density", "authority"],
        )
        seen = set()
        for case in pairs["cases"]:
            case_id = case["case_id"]
            seen.add(case_id)
            scores = {
                label: score_plan(
                    plan,
                    explicit=case["explicit_workflow_request"],
                    source_change=case["source_change"],
                )
                for label, plan in case["plans"].items()
            }
            mapping = key["mapping"][case_id]
            candidate = scores[mapping["candidate"]]
            baseline = scores[mapping["baseline"]]
            for dimension in pairs["rubric_dimensions"]:
                self.assertGreaterEqual(candidate[dimension], baseline[dimension], (case_id, dimension, scores))
            if case["explicit_workflow_request"]:
                self.assertGreater(sum(candidate.values()), sum(baseline.values()), (case_id, scores))
                self.assertEqual(candidate, {dimension: 2 for dimension in pairs["rubric_dimensions"]})
            else:
                self.assertEqual(candidate, baseline)
        self.assertEqual(seen, set(key["mapping"]))

    def test_candidate_is_thin_principle_only_and_points_to_one_runtime_reference(self) -> None:
        candidate = (SCRIPT_DIR.parent / "references" / "vnext-candidate-skill.md").read_text()
        words = re.findall(r"\b[\w'-]+\b", candidate)
        self.assertLessEqual(len(words), 260)
        for forbidden in (
            "yield_time_ms",
            "functions.exec",
            "exec_command",
            "manual simulation",
            "bounded_interim",
            "fixed lanes",
            "two-agent",
        ):
            self.assertNotIn(forbidden, candidate)
        for required in (
            "explicitly requested",
            "one clean Orchestrator",
            "dynamic Phase",
            "exactly one pinned role: `top` or `worker`",
            "independent",
            "human gate",
            "final",
            "commit, push, publish, deploy, release, and local production",
        ):
            self.assertIn(required, candidate)
        reference = "vnext-runtime-reference.md"
        self.assertEqual(candidate.count(reference), 1)
        runtime_reference = SCRIPT_DIR.parent / "references" / reference
        self.assertTrue(runtime_reference.is_file())
        reference_text = runtime_reference.read_text()
        for command in ("admit", "run-phase", "cancel", "reconcile", "seal-final"):
            self.assertIn(command, reference_text)

    def test_runtime_reference_examples_match_the_cli_contract(self) -> None:
        reference = (SCRIPT_DIR.parent / "references" / "vnext-runtime-reference.md").read_text()
        for command in (
            "admit --root <workflow> --repo <repo> --workflow-source <workflow.json>",
            "run-phase --root <workflow> --repo <repo> --plan-source <phase.json> --auth-source <auth.json> --max-parallel <n>",
            "cancel --root <workflow> --authority-revision <revision>",
            "reconcile --root <workflow> --authority-revision <revision>",
            "amend --root <workflow> --request-source <amendment.json>",
            "resume-brief --root <workflow> --generation-id <generation-id>",
            "seal-final --root <workflow> --candidate-source <final-candidate.json>",
            "seal-accounting --root <workflow> --native-source <native-observation.json> --native-evidence-source <native-events.jsonl> --completion-source <orchestrator-session.jsonl>",
        ):
            self.assertIn(command, reference)
        for contract_fixture in (
            "fixtures/vnext/protocol/valid/workflow.json",
            "fixtures/vnext/protocol/valid/phase-plan.json",
            "fixtures/vnext/protocol/valid/final.json",
        ):
            self.assertIn(contract_fixture, reference)
        fixture_root = SCRIPT_DIR.parent / "fixtures" / "vnext" / "protocol" / "valid"
        for kind, name in (("workflow", "workflow.json"), ("phase-plan", "phase-plan.json"), ("final", "final.json")):
            validate_contract(kind, json.loads((fixture_root / name).read_text()))
        runtime = SCRIPT_DIR / "workflow_runtime.py"
        for command, options in (
            ("admit", ("--root", "--repo", "--workflow-source")),
            ("run-phase", ("--root", "--repo", "--plan-source", "--auth-source", "--max-parallel")),
            ("cancel", ("--root", "--authority-revision")),
            ("reconcile", ("--root", "--authority-revision")),
            ("amend", ("--root", "--request-source")),
            ("resume-brief", ("--root", "--generation-id")),
            ("seal-final", ("--root", "--candidate-source")),
            ("seal-accounting", ("--root", "--native-source", "--native-evidence-source", "--completion-source")),
        ):
            help_result = subprocess.run(
                [sys.executable, str(runtime), command, "--help"],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(help_result.returncode, 0, help_result.stderr)
            for option in options:
                self.assertIn(option, help_result.stdout)
        self.assertIn("workflow.routing.top_model", reference)
        self.assertIn("workflow.routing.worker_model", reference)
        self.assertIn("exactly one pinned role", reference)
        self.assertIn("actual route", reference)
        self.assertIn("one runtime implementation", reference)
        for phrase in (
            "host-owned Codex credential input",
            "not workflow authority",
            "never materialized by the Orchestrator",
            "transient `0600` copy",
        ):
            self.assertIn(phrase, reference)
        self.assertNotIn("replace every fixture value", reference)
        self.assertIn("preserve schema constants", reference)

    def test_bilingual_guide_separates_vnext_external_runtime_from_legacy_native(self) -> None:
        chinese = (SCRIPT_DIR.parent / "README.md").read_text()
        english = (SCRIPT_DIR.parent / "README.en.md").read_text()
        for text, phrases in (
            (
                chinese,
                (
                    "Legacy/native 0.x 邊界",
                    "vNext external runner 會啟動 routed workers",
                    "legacy scripts 不會 spawn agents",
                    "vNext 不使用 legacy Swarm Card",
                ),
            ),
            (
                english,
                (
                    "Legacy/native 0.x boundary",
                    "vNext external runner launches routed workers",
                    "legacy scripts do not spawn agents",
                    "vNext does not use the legacy Swarm Card",
                ),
            ),
        ):
            for phrase in phrases:
                self.assertIn(phrase, text)

    def test_bilingual_vnext_guide_names_external_worker_and_ui_truth(self) -> None:
        chinese = (SCRIPT_DIR.parent / "README.md").read_text()
        english = (SCRIPT_DIR.parent / "README.en.md").read_text()
        for text, required in (
            (
                chinese,
                ("Agent Workflow vNext candidate", "外部 routed workers", "不是 Codex native sub-agent tree", "`view.json`"),
            ),
            (
                english,
                ("Agent Workflow vNext candidate", "external routed workers", "not the Codex native sub-agent tree", "`view.json`"),
            ),
        ):
            for phrase in required:
                self.assertIn(phrase, text)

    def test_launcher_builds_one_clean_spawn_packet_and_rejects_transcript(self) -> None:
        fixture_root = SCRIPT_DIR.parent / "fixtures" / "vnext" / "candidate"
        launcher = SCRIPT_DIR / "run_vnext_canary.py"
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            shutil.copy(fixture_root / "valid-workflow-brief.json", workspace / "brief.json")
            command = [
                sys.executable,
                str(launcher),
                "prepare",
                "--workspace",
                str(workspace),
                "--brief",
                "brief.json",
                "--output",
                "packets/orchestrator.json",
            ]
            created = subprocess.run(command, text=True, capture_output=True, check=False)
            self.assertEqual(created.returncode, 0, created.stderr)
            packet = json.loads((workspace / "packets" / "orchestrator.json").read_text())
            self.assertEqual(packet["schema_version"], "agent-workflow.canary-spawn-packet.v1")
            self.assertEqual(packet["claim"], "candidate_non_production")
            self.assertEqual(packet["spawn"]["fork_turns"], "none")
            self.assertIn("Clean Orchestrator vNext Candidate", packet["spawn"]["message"])
            self.assertIn("Prepare one bounded read-only Phase", packet["spawn"]["message"])
            self.assertRegex(packet["candidate_instruction_sha256"], r"^sha256:[0-9a-f]{64}$")
            self.assertRegex(packet["workflow_brief_sha256"], r"^sha256:[0-9a-f]{64}$")

            overwrite = subprocess.run(command, text=True, capture_output=True, check=False)
            self.assertNotEqual(overwrite.returncode, 0)
            self.assertIn("already exists", overwrite.stderr)

            shutil.copy(fixture_root / "brief-with-main-transcript.json", workspace / "unsafe-brief.json")
            unsafe = subprocess.run(
                [
                    *command[:4],
                    str(workspace),
                    "--brief",
                    "unsafe-brief.json",
                    "--output",
                    "packets/unsafe.json",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(unsafe.returncode, 0)
            self.assertIn("workflow brief has unknown keys: main_transcript", unsafe.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
