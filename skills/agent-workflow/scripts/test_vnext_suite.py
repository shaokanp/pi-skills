#!/usr/bin/env python3
"""Release-gated tests for the Agent Workflow vNext protocol."""

from __future__ import annotations

import sys
import subprocess
import tempfile
import unittest
import hashlib
import json
from copy import deepcopy
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parent
FIXTURE_ROOT = SCRIPT_DIR.parent / "fixtures" / "vnext" / "protocol"
CANARY_ROOT = SCRIPT_DIR.parent / "fixtures" / "vnext" / "canary"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import phase_protocol
import artifact_store
import baseline_gate
import workflow_runtime
from phase_protocol import ProtocolError, validate_contract, validate_sidecar
from artifact_store import ArtifactError, create_once_json
from baseline_gate import BaselineError, collect_baseline, verify_baseline
from recovery_runtime import scope_sha256


def load_fixture(relative_path: str) -> dict[str, object]:
    import json

    return json.loads((FIXTURE_ROOT / relative_path).read_text(encoding="utf-8"))


class VNextProtocolTests(unittest.TestCase):
    def test_scoped_sidecar_taxonomy_is_small_and_explicit(self) -> None:
        self.assertEqual(
            phase_protocol.SCOPED_SIDECAR_SCHEMAS,
            {
                "events": "agent-workflow.events.vnext.v1",
                "amendment": "agent-workflow.amendment.vnext.v1",
                "generation-claim": "agent-workflow.generation-claim.vnext.v1",
                "lineage-claim": "agent-workflow.lineage-claim.vnext.v1",
                "accounting": "agent-workflow.accounting.vnext.v1",
            },
        )

    def test_unknown_contract_kind_fails_closed(self) -> None:
        with self.assertRaisesRegex(ProtocolError, "unknown lifecycle contract kind"):
            validate_contract("unknown", {})

    def test_recovery_authority_sidecars_are_narrow_and_typed(self) -> None:
        origin = {
            "schema_version": "agent-workflow.lineage-claim.vnext.v1",
            "claim_kind": "origin",
            "workflow_id": "fixture-workflow",
            "lineage_id": "lineage-contract",
            "criterion_id": "AC-1",
            "criterion_revision": 1,
            "role": "worker",
            "scope_sha256": "sha256:" + "a" * 64,
            "origin_phase_id": "001-research",
            "origin_task_id": "research-contract",
        }
        self.assertEqual(validate_sidecar("lineage-claim", origin), origin)
        malformed = deepcopy(origin)
        malformed["retry_count"] = 0
        with self.assertRaisesRegex(ProtocolError, "unknown keys"):
            validate_sidecar("lineage-claim", malformed)

        amendment = {
            "schema_version": "agent-workflow.amendment.vnext.v1",
            "amendment_kind": "criterion_revision",
            "workflow_id": "fixture-workflow",
            "amendment_id": "revise-ac-1",
            "previous_authority_revision": 1,
            "authority_revision": 2,
            "criterion_id": "AC-1",
            "from_revision": 1,
            "to_revision": 2,
            "blocked_result_ref": "phases/001/tasks/a/result.json",
            "blocked_result_sha256": "sha256:" + "b" * 64,
            "user_instruction_ref": "amendments/user.md",
            "user_instruction_sha256": "sha256:" + "c" * 64,
            "reason": "User revised the success criterion.",
        }
        self.assertEqual(validate_sidecar("amendment", amendment), amendment)
        amendment["to_revision"] = 3
        with self.assertRaisesRegex(ProtocolError, "advance exactly once"):
            validate_sidecar("amendment", amendment)

    def test_workflow_contract_accepts_complete_fixture_and_rejects_drift(self) -> None:
        accepted = load_fixture("valid/workflow.json")
        self.assertEqual(validate_contract("workflow", accepted), accepted)

        cases = (
            ("negative/workflow-missing-objective.json", "workflow contract missing keys"),
            ("negative/workflow-schema-drift.json", "workflow.schema_version"),
        )
        for relative_path, message in cases:
            with self.subTest(relative_path=relative_path):
                with self.assertRaisesRegex(ProtocolError, message):
                    validate_contract("workflow", load_fixture(relative_path))

    def test_phase_plan_accepts_valid_fixture_and_rejects_path_traversal(self) -> None:
        accepted = load_fixture("valid/phase-plan.json")
        self.assertEqual(validate_contract("phase-plan", accepted), accepted)

        with self.assertRaisesRegex(ProtocolError, "packet_path must be a safe relative path"):
            validate_contract(
                "phase-plan",
                load_fixture("negative/phase-plan-path-traversal.json"),
            )

    def test_phase_plan_rejects_path_derived_ids_reserved_roots_and_writer_overlap(self) -> None:
        accepted = load_fixture("valid/phase-plan.json")
        traversal_id = deepcopy(accepted)
        traversal_id["tasks"][0]["task_id"] = "../../escape"
        with self.assertRaisesRegex(ProtocolError, "path-safe id"):
            validate_contract("phase-plan", traversal_id)

        writer = deepcopy(accepted["tasks"][0])
        writer.update({"work_mode": "write", "write_roots": ["src"]})
        reserved = deepcopy(accepted)
        reserved["tasks"] = [{**writer, "write_roots": [".git/hooks"]}]
        with self.assertRaisesRegex(ProtocolError, "reserved control root"):
            validate_contract("phase-plan", reserved)

        overlap = deepcopy(accepted)
        overlap["tasks"] = [
            writer,
            {
                **deepcopy(writer),
                "task_id": "second-writer",
                "lineage_id": "lineage-second",
                "write_roots": ["src/api"],
            },
        ]
        with self.assertRaisesRegex(ProtocolError, "writer roots overlap"):
            validate_contract("phase-plan", overlap)

    def test_task_result_accepts_terminal_fixture_and_rejects_running_status(self) -> None:
        accepted = load_fixture("valid/task-result.json")
        self.assertEqual(validate_contract("task-result", accepted), accepted)

        with self.assertRaisesRegex(ProtocolError, "task-result.status must be terminal"):
            validate_contract(
                "task-result",
                load_fixture("negative/task-result-invalid-status.json"),
            )

    def test_task_result_rejects_contradictory_checks_elapsed_and_accounting(self) -> None:
        accepted = load_fixture("valid/task-result.json")
        failed_check = deepcopy(accepted)
        failed_check["checks"] = [{
            "name": "focused",
            "exit_code": 1,
            "evidence_ref": "checks/focused.txt",
            "evidence_sha256": "sha256:" + "1" * 64,
        }]
        with self.assertRaisesRegex(ProtocolError, "failed checks"):
            validate_contract("task-result", failed_check)
        elapsed = deepcopy(accepted)
        elapsed["elapsed_ms"] = 1
        with self.assertRaisesRegex(ProtocolError, "reconcile"):
            validate_contract("task-result", elapsed)
        unstarted = deepcopy(accepted)
        unstarted.update({
            "status": "not_started_deadline",
            "terminal_reason": "queue_deadline",
            "actual_route": None,
            "output_ref": None,
            "output_sha256": None,
            "started_at": "2026-07-12T00:00:00Z",
            "finished_at": "2026-07-12T00:00:00Z",
            "elapsed_ms": 0,
            "token_usage": {"input": 0, "output": 0, "total": 0, "source": "no_session", "confidence": "exact"},
        })
        self.assertEqual(validate_contract("task-result", unstarted), unstarted)
        started_without_route = deepcopy(accepted)
        started_without_route.update({
            "status": "failed",
            "terminal_reason": "codex_turn_failed",
            "actual_route": None,
            "output_ref": None,
            "output_sha256": None,
        })
        with self.assertRaisesRegex(ProtocolError, "actual_route evidence"):
            validate_contract("task-result", started_without_route)

    def test_phase_receipt_accepts_terminal_fixture_and_rejects_count_drift(self) -> None:
        accepted = load_fixture("valid/phase-receipt.json")
        self.assertEqual(validate_contract("phase-receipt", accepted), accepted)

        with self.assertRaisesRegex(ProtocolError, "task_counts.total must equal terminal counts"):
            validate_contract(
                "phase-receipt",
                load_fixture("negative/phase-receipt-count-drift.json"),
            )

    def test_phase_receipt_rejects_failed_all_completed_and_disjoint_integration_sets(self) -> None:
        accepted = load_fixture("valid/phase-receipt.json")
        failed = deepcopy(accepted)
        failed.update({"status": "failed", "terminal_reason": "task_failures_terminal"})
        with self.assertRaisesRegex(ProtocolError, "unsuccessful task"):
            validate_contract("phase-receipt", failed)
        disjoint = deepcopy(accepted)
        disjoint["integration"] = {
            "mode": "isolated_exact_base",
            "status": "applied",
            "patch_ref": "patches/phase.patch",
            "patch_sha256": "sha256:" + "2" * 64,
            "target_before": {"src/a.py": "sha256:" + "3" * 64},
            "target_after": {"src/b.py": "sha256:" + "4" * 64},
        }
        with self.assertRaisesRegex(ProtocolError, "identical target sets"):
            validate_contract("phase-receipt", disjoint)
        conflict = deepcopy(accepted)
        conflict["integration"] = {
            "mode": "isolated_exact_base",
            "status": "conflict",
            "patch_ref": "patches/phase.patch",
            "patch_sha256": "sha256:" + "2" * 64,
            "target_before": {"src/a.py": "sha256:" + "3" * 64},
            "target_after": {},
        }
        with self.assertRaisesRegex(ProtocolError, "requires blocked"):
            validate_contract("phase-receipt", conflict)

    def test_final_accepts_verified_fixture_and_rejects_unverified_completion(self) -> None:
        accepted = load_fixture("valid/final.json")
        self.assertEqual(validate_contract("final", accepted), accepted)

        with self.assertRaisesRegex(ProtocolError, "complete final requires verification_ref"):
            validate_contract(
                "final",
                load_fixture("negative/final-missing-verification.json"),
            )

    def test_create_once_artifact_rejects_overwrite_and_preserves_original(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = create_once_json(root, "phases/001/receipt.json", {"revision": 1})
            original = path.read_bytes()

            with self.assertRaisesRegex(ArtifactError, "already exists"):
                create_once_json(root, "phases/001/receipt.json", {"revision": 2})

            self.assertEqual(path.read_bytes(), original)

    def test_create_once_requires_secure_directory_flags(self) -> None:
        with mock.patch.object(artifact_store.os, "O_NOFOLLOW", new=0):
            with self.assertRaisesRegex(ArtifactError, "O_DIRECTORY and O_NOFOLLOW"):
                create_once_json(Path("unused"), "receipt.json", {})

    def test_create_once_fsyncs_new_ancestors_and_rejects_intermediate_symlink(self) -> None:
        import os

        with tempfile.TemporaryDirectory() as raw:
            parent = Path(raw)
            with mock.patch.object(artifact_store.os, "fsync", wraps=os.fsync) as fsync:
                create_once_json(parent / "new" / "root", "phases/001/receipt.json", {"ok": True})
            self.assertGreaterEqual(fsync.call_count, 8)

            outside = parent / "outside"
            outside.mkdir()
            unsafe_root = parent / "unsafe"
            unsafe_root.mkdir()
            (unsafe_root / "phases").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ArtifactError, "could not create immutable artifact"):
                create_once_json(unsafe_root, "phases/receipt.json", {"unsafe": True})

    def test_replay_reader_rejects_non_regular_artifacts(self) -> None:
        import os

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            os.mkfifo(root / "events")
            with self.assertRaisesRegex(ProtocolError, "not a regular file"):
                phase_protocol._read_artifact_bytes(root, "events", "sha256:" + "0" * 64)

    def test_replayable_baseline_covers_staged_unstaged_and_untracked(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            (repo / "tracked.txt").write_text("base\n")
            subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
            (repo / "staged.txt").write_text("staged\n")
            subprocess.run(["git", "add", "staged.txt"], cwd=repo, check=True)
            (repo / "tracked.txt").write_text("changed\n")
            (repo / "untracked.txt").write_text("untracked\n")
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True, capture_output=True, check=True
            ).stdout.strip()
            branch = subprocess.run(
                ["git", "branch", "--show-current"], cwd=repo, text=True, capture_output=True, check=True
            ).stdout.strip()
            empty = baseline_gate._packed(b"")
            parent_manifest = {
                "schema_version": "agent-workflow.vnext-replayable-baseline.v2",
                "baseline_kind": "pre_slice",
                "head": head,
                "branch": branch,
                "environment": {
                    "codex_cli": "codex-cli test",
                    "platform": "test",
                    "model": "test-model",
                    "reasoning_effort": "xhigh",
                },
                "selection": {"tracked_excludes": [], "untracked_mode": "explicit", "untracked_paths": []},
                "staged_patch": empty,
                "unstaged_patch": empty,
                "staged_binary_patch": empty,
                "unstaged_binary_patch": empty,
                "untracked": [],
                "relevant_files": [],
                "parent_summary": {
                    "schema_version": "agent-workflow.vnext-pre-slice-baseline.v1",
                    "summary_sha256": "sha256:" + "1" * 64,
                    "head": head,
                    "branch": branch,
                },
                "candidate_parent": None,
                "intended_changes": [],
                "immutability": "create_once_do_not_rewrite",
            }
            parent_manifest["seal_sha256"] = baseline_gate._seal(parent_manifest)
            parent_bytes = baseline_gate._canonical(parent_manifest)
            baseline = collect_baseline(
                repo,
                baseline_kind="candidate_gate",
                candidate_parent={"path": "parent.json", "sha256": baseline_gate._digest(parent_bytes)},
                candidate_parent_manifest=parent_manifest,
                intended_changes=["staged.txt", "tracked.txt", "untracked.txt"],
                model="test-model",
                reasoning_effort="xhigh",
            )
            with self.assertRaisesRegex(BaselineError, "full tracked and untracked"):
                collect_baseline(
                    repo,
                    baseline_kind="candidate_gate",
                    tracked_excludes=["tracked.txt"],
                    candidate_parent={
                        "path": "parent.json",
                        "sha256": baseline_gate._digest(parent_bytes),
                    },
                    candidate_parent_manifest=parent_manifest,
                    intended_changes=["staged.txt", "untracked.txt"],
                )
            with self.assertRaisesRegex(BaselineError, "do not cover dirty paths"):
                collect_baseline(
                    repo,
                    baseline_kind="candidate_gate",
                    candidate_parent={
                        "path": "parent.json",
                        "sha256": baseline_gate._digest(parent_bytes),
                    },
                    candidate_parent_manifest=parent_manifest,
                    intended_changes=["declared-but-unchanged.txt"],
                )
            self.assertEqual(verify_baseline(baseline), baseline)
            head_drift = deepcopy(baseline)
            head_drift["head"] = "2" * 40
            head_drift["seal_sha256"] = baseline_gate._seal(head_drift)
            with self.assertRaisesRegex(BaselineError, "preserve parent HEAD"):
                baseline_gate.verify_candidate_against_parent(head_drift, parent_manifest)
            self.assertGreater(baseline["staged_patch"]["bytes"], 0)
            self.assertGreater(baseline["unstaged_patch"]["bytes"], 0)
            self.assertEqual([item["path"] for item in baseline["untracked"]], ["untracked.txt"])
            self.assertEqual(
                {item["path"] for item in baseline["relevant_files"]},
                {"staged.txt", "tracked.txt", "untracked.txt"},
            )
            mutations = (
                ("head", "not-a-git-object"),
                ("branch", 7),
                ("immutability", "rewrite_allowed"),
            )
            for field, replacement in mutations:
                with self.subTest(field=field):
                    changed = deepcopy(baseline)
                    changed[field] = replacement
                    with self.assertRaises(BaselineError):
                        verify_baseline(changed)
            unsafe_selection = deepcopy(baseline)
            unsafe_selection["selection"]["tracked_excludes"] = ["../../outside"]
            unsafe_selection["seal_sha256"] = baseline_gate._seal(unsafe_selection)
            with self.assertRaisesRegex(BaselineError, "safe relative"):
                verify_baseline(unsafe_selection)
            invalid_mode = deepcopy(baseline)
            invalid_mode["selection"]["untracked_mode"] = "invented"
            invalid_mode["seal_sha256"] = baseline_gate._seal(invalid_mode)
            with self.assertRaisesRegex(BaselineError, "untracked mode"):
                verify_baseline(invalid_mode)
            untracked_drift = deepcopy(baseline)
            for item in untracked_drift["relevant_files"]:
                if item["path"] == "untracked.txt":
                    item["sha256"] = "sha256:" + "9" * 64
            untracked_drift["seal_sha256"] = baseline_gate._seal(untracked_drift)
            with self.assertRaisesRegex(BaselineError, "untracked content"):
                verify_baseline(untracked_drift)

    def test_full_replay_binds_results_routes_counts_verification_and_evidence(self) -> None:
        import hashlib
        import json

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)

            def put(relative: str, payload: bytes) -> str:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
                return "sha256:" + hashlib.sha256(payload).hexdigest()

            def put_json(relative: str, value: dict[str, object]) -> str:
                payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
                return put(relative, payload)

            def put_generation_claim(plan: dict[str, object], plan_sha256: str) -> tuple[str, str]:
                contention = {
                    "predecessor_sha256": plan["predecessor_sha256"],
                    "authority_revision": plan["authority_revision"],
                }
                contention_key = "sha256:" + hashlib.sha256(
                    (json.dumps(contention, sort_keys=True, separators=(",", ":")) + "\n").encode()
                ).hexdigest()
                ref = f"generations/claims/{contention_key.removeprefix('sha256:')}.json"
                claim = {
                    "schema_version": "agent-workflow.generation-claim.vnext.v1",
                    "workflow_id": "fixture-workflow",
                    "generation_id": plan["generation_id"],
                    "phase_id": plan["phase_id"],
                    "predecessor_sha256": plan["predecessor_sha256"],
                    "authority_revision": plan["authority_revision"],
                    "plan_sha256": plan_sha256,
                    "contention_key": contention_key,
                }
                return ref, put_json(ref, claim)

            baseline = {
                "schema_version": "agent-workflow.vnext-replayable-baseline.v2",
                "baseline_kind": "pre_slice",
                "head": "1" * 40,
                "branch": "main",
                "environment": {
                    "codex_cli": "codex-cli test",
                    "platform": "test",
                    "model": "test-top",
                    "reasoning_effort": "xhigh",
                },
                "selection": {"tracked_excludes": [], "untracked_mode": "explicit", "untracked_paths": []},
                "staged_patch": baseline_gate._packed(b""),
                "unstaged_patch": baseline_gate._packed(b""),
                "staged_binary_patch": baseline_gate._packed(b""),
                "unstaged_binary_patch": baseline_gate._packed(b""),
                "untracked": [],
                "relevant_files": [],
                "parent_summary": {
                    "schema_version": "agent-workflow.vnext-pre-slice-baseline.v1",
                    "summary_sha256": "sha256:" + "2" * 64,
                    "head": "1" * 40,
                    "branch": "main",
                },
                "candidate_parent": None,
                "intended_changes": [],
                "immutability": "create_once_do_not_rewrite",
            }
            baseline["seal_sha256"] = baseline_gate._seal(baseline)
            baseline_digest = put_json("evidence/pre-slice-baseline.json", baseline)
            workflow = load_fixture("valid/workflow.json")
            workflow["baseline_sha256"] = baseline_digest
            workflow["admission"]["repository"] = baseline_gate.repository_evidence(baseline)
            for name, capability in workflow["admission"]["capabilities"].items():
                capability["evidence_sha256"] = put(
                    capability["evidence_ref"],
                    (name + "\n").encode(),
                )
            workflow_digest = put_json("workflow.json", workflow)

            research_plan = load_fixture("valid/phase-plan.json")
            research_task = research_plan["tasks"][0]
            research_task["packet_sha256"] = put(research_task["packet_path"], b"research packet\n")
            research_task["input_sha256"] = {research_task["input_refs"][0]: baseline_digest}
            research_plan["predecessor_sha256"] = baseline_digest
            research_plan_digest = put_json("phases/001-research/plan.json", research_plan)
            research_claim_ref, research_claim_digest = put_generation_claim(
                research_plan,
                research_plan_digest,
            )
            research_lineage_ref = "lineages/lineage-contract/origin.json"
            research_lineage_digest = put_json(
                research_lineage_ref,
                {
                    "schema_version": "agent-workflow.lineage-claim.vnext.v1",
                    "claim_kind": "origin",
                    "workflow_id": workflow["workflow_id"],
                    "lineage_id": research_task["lineage_id"],
                    "criterion_id": research_task["criterion_id"],
                    "criterion_revision": research_task["criterion_revision"],
                    "role": research_task["role"],
                    "scope_sha256": scope_sha256(research_task),
                    "origin_phase_id": research_plan["phase_id"],
                    "origin_task_id": research_task["task_id"],
                },
            )

            research_result = load_fixture("valid/task-result.json")
            research_result["actual_route"]["attestation_ref"] = "phases/001-research/attempts/001/turn-context.json"
            research_result["actual_route"]["attestation_sha256"] = put(
                research_result["actual_route"]["attestation_ref"], b"worker route\n"
            )
            research_result["output_ref"] = "phases/001-research/tasks/research-contract/output.json"
            research_result["output_sha256"] = put(research_result["output_ref"], b"{}\n")
            research_result["evidence_refs"] = ["phases/001-research/attempts/001/events.jsonl"]
            research_result["evidence_sha256"] = {
                research_result["evidence_refs"][0]: put(research_result["evidence_refs"][0], b"terminal\n")
            }
            research_result_ref = "phases/001-research/tasks/research-contract/result.json"
            research_result_digest = put_json(research_result_ref, research_result)

            research_receipt = load_fixture("valid/phase-receipt.json")
            research_receipt["generation_claim_ref"] = research_claim_ref
            research_receipt["generation_claim_sha256"] = research_claim_digest
            research_receipt["plan_sha256"] = research_plan_digest
            research_receipt["predecessor_sha256"] = baseline_digest
            research_receipt["task_result_sha256"] = {research_result_ref: research_result_digest}
            research_receipt_digest = put_json("phases/001-research/receipt.json", research_receipt)

            instruction_ref = "amendments/instruction-0002.md"
            instruction_digest = put(instruction_ref, b"Verify the compatibility seam too.\n")
            put_json(
                "amendments/criteria/0002-compatibility-seam.json",
                {
                    "schema_version": "agent-workflow.amendment.vnext.v1",
                    "amendment_kind": "instruction",
                    "workflow_id": workflow["workflow_id"],
                    "amendment_id": "compatibility-seam",
                    "previous_authority_revision": 1,
                    "authority_revision": 2,
                    "applies_after_ref": "phases/001-research/receipt.json",
                    "applies_after_sha256": research_receipt_digest,
                    "user_instruction_ref": instruction_ref,
                    "user_instruction_sha256": instruction_digest,
                    "reason": "User added a bounded instruction for the next phase.",
                },
            )

            verify_plan = deepcopy(research_plan)
            verify_plan.update({
                "phase_id": "002-verify",
                "generation_id": "generation-002",
                "authority_revision": 2,
                "caused_by": ["001-research"],
                "predecessor_sha256": research_receipt_digest,
            })
            verify_task = verify_plan["tasks"][0]
            verify_task.update({
                "task_id": "independent-verifier",
                "lineage_id": "lineage-independent-verifier",
                "role": "top",
                "packet_path": "phases/002-verify/tasks/independent-verifier/packet.md",
                "input_refs": ["phases/001-research/receipt.json"],
                "input_sha256": {"phases/001-research/receipt.json": research_receipt_digest},
            })
            verify_task["packet_sha256"] = put(verify_task["packet_path"], b"verify packet\n")
            verify_plan_digest = put_json("phases/002-verify/plan.json", verify_plan)
            verify_claim_ref, verify_claim_digest = put_generation_claim(
                verify_plan,
                verify_plan_digest,
            )
            verify_lineage_ref = "lineages/lineage-independent-verifier/origin.json"
            verify_lineage_digest = put_json(
                verify_lineage_ref,
                {
                    "schema_version": "agent-workflow.lineage-claim.vnext.v1",
                    "claim_kind": "origin",
                    "workflow_id": workflow["workflow_id"],
                    "lineage_id": verify_task["lineage_id"],
                    "criterion_id": verify_task["criterion_id"],
                    "criterion_revision": verify_task["criterion_revision"],
                    "role": verify_task["role"],
                    "scope_sha256": scope_sha256(verify_task),
                    "origin_phase_id": verify_plan["phase_id"],
                    "origin_task_id": verify_task["task_id"],
                },
            )

            verify_result = deepcopy(research_result)
            verify_result.update({
                "phase_id": "002-verify",
                "task_id": "independent-verifier",
                "lineage_id": "lineage-independent-verifier",
            })
            verify_result["actual_route"].update({
                "model": workflow["routing"]["top_model"],
                "session_id": "01900000-0000-7000-8000-000000000002",
                "attestation_ref": "phases/002-verify/attempts/001/turn-context.json",
            })
            verify_result["actual_route"]["attestation_sha256"] = put(
                verify_result["actual_route"]["attestation_ref"], b"top route\n"
            )
            verify_result["output_ref"] = "phases/002-verify/tasks/independent-verifier/output.json"
            verify_result["evidence_refs"] = ["phases/002-verify/attempts/001/events.jsonl"]
            verify_result["evidence_sha256"] = {
                verify_result["evidence_refs"][0]: put(verify_result["evidence_refs"][0], b"verified\n")
            }
            verification_decision = {
                "schema_version": "agent-workflow.verification-decision.vnext.v1",
                "workflow_id": workflow["workflow_id"],
                "phase_id": "002-verify",
                "task_id": "independent-verifier",
                "decision": "pass",
                "confidence": "high",
                "criteria": [
                    {
                        "criterion_id": "AC-1",
                        "status": "pass",
                        "evidence_refs": verify_result["evidence_refs"],
                    }
                ],
                "findings": [],
                "commands": [
                    {
                        "command": "python3 -m unittest focused",
                        "exit_code": 0,
                        "evidence_ref": verify_result["evidence_refs"][0],
                    }
                ],
            }
            verify_result["output_sha256"] = put_json(
                verify_result["output_ref"],
                verification_decision,
            )
            verify_result_ref = "phases/002-verify/tasks/independent-verifier/result.json"
            verify_result_digest = put_json(verify_result_ref, verify_result)

            verify_receipt = deepcopy(research_receipt)
            verify_receipt.update({
                "phase_id": "002-verify",
                "generation_id": "generation-002",
                "generation_claim_ref": verify_claim_ref,
                "generation_claim_sha256": verify_claim_digest,
                "plan_sha256": verify_plan_digest,
                "predecessor_sha256": research_receipt_digest,
                "task_result_refs": [verify_result_ref],
                "task_result_sha256": {verify_result_ref: verify_result_digest},
            })
            verify_receipt_digest = put_json("phases/002-verify/receipt.json", verify_receipt)

            report_digest = put("final-report.md", b"verified\n")
            final = load_fixture("valid/final.json")
            final["generation_id"] = "generation-002"
            final["phase_receipt_sha256"] = {
                "phases/001-research/receipt.json": research_receipt_digest,
                "phases/002-verify/receipt.json": verify_receipt_digest,
            }
            amendment_ref = "amendments/criteria/0002-compatibility-seam.json"
            final["amendment_refs"] = [amendment_ref]
            final["amendment_sha256"] = {
                amendment_ref: "sha256:" + hashlib.sha256((root / amendment_ref).read_bytes()).hexdigest()
            }
            final["lineage_claim_refs"] = [research_lineage_ref, verify_lineage_ref]
            final["lineage_claim_sha256"] = {
                research_lineage_ref: research_lineage_digest,
                verify_lineage_ref: verify_lineage_digest,
            }
            final["verification_sha256"] = verify_receipt_digest
            final["final_report_sha256"] = report_digest
            candidate_replay = phase_protocol.validate_replay_candidate(
                root,
                workflow_sha256=workflow_digest,
                final=final,
            )
            self.assertEqual(candidate_replay["status"], "complete")
            final_path = workflow_runtime.seal_final(root, final)
            final_digest = "sha256:" + hashlib.sha256(final_path.read_bytes()).hexdigest()

            metrics: dict[str, object] = {}
            replayed = phase_protocol.validate_replay(
                root,
                workflow_sha256=workflow_digest,
                final_sha256=final_digest,
                metrics_out=metrics,
            )
            self.assertEqual(replayed["status"], "complete")
            self.assertEqual(metrics, {
                "external_input_tokens": 200,
                "external_output_tokens": 40,
                "external_total_tokens": 240,
                "external_accounting_exact": True,
            })

            incomplete_verification = deepcopy(verification_decision)
            incomplete_verification["criteria"] = []
            incomplete_output_digest = put_json(
                verify_result["output_ref"],
                incomplete_verification,
            )
            incomplete_verify_result = deepcopy(verify_result)
            incomplete_verify_result["output_sha256"] = incomplete_output_digest
            incomplete_verify_result_digest = put_json(
                verify_result_ref,
                incomplete_verify_result,
            )
            incomplete_verify_receipt = deepcopy(verify_receipt)
            incomplete_verify_receipt["task_result_sha256"] = {
                verify_result_ref: incomplete_verify_result_digest,
            }
            incomplete_verify_receipt_digest = put_json(
                "phases/002-verify/receipt.json",
                incomplete_verify_receipt,
            )
            incomplete_verification_final = deepcopy(final)
            incomplete_verification_final["phase_receipt_sha256"][
                "phases/002-verify/receipt.json"
            ] = incomplete_verify_receipt_digest
            incomplete_verification_final["verification_sha256"] = (
                incomplete_verify_receipt_digest
            )
            with self.assertRaisesRegex(ProtocolError, "criteria coverage"):
                phase_protocol.validate_replay_candidate(
                    root,
                    workflow_sha256=workflow_digest,
                    final=incomplete_verification_final,
                )
            put_json(verify_result["output_ref"], verification_decision)
            put_json(verify_result_ref, verify_result)
            put_json("phases/002-verify/receipt.json", verify_receipt)

            self_verifying_result = deepcopy(verify_result)
            self_verifying_result["actual_route"]["session_id"] = research_result[
                "actual_route"
            ]["session_id"]
            self_verifying_result_digest = put_json(
                verify_result_ref,
                self_verifying_result,
            )
            self_verifying_receipt = deepcopy(verify_receipt)
            self_verifying_receipt["task_result_sha256"] = {
                verify_result_ref: self_verifying_result_digest,
            }
            self_verifying_receipt_digest = put_json(
                "phases/002-verify/receipt.json",
                self_verifying_receipt,
            )
            self_verifying_final = deepcopy(final)
            self_verifying_final["phase_receipt_sha256"][
                "phases/002-verify/receipt.json"
            ] = self_verifying_receipt_digest
            self_verifying_final["verification_sha256"] = self_verifying_receipt_digest
            with self.assertRaisesRegex(ProtocolError, "session identity"):
                phase_protocol.validate_replay_candidate(
                    root,
                    workflow_sha256=workflow_digest,
                    final=self_verifying_final,
                )
            put_json(verify_result_ref, verify_result)
            put_json("phases/002-verify/receipt.json", verify_receipt)

            incomplete_lineage_final = deepcopy(final)
            incomplete_lineage_final["lineage_claim_refs"].remove(verify_lineage_ref)
            incomplete_lineage_final["lineage_claim_sha256"].pop(verify_lineage_ref)
            incomplete_lineage_final_digest = put_json("final.json", incomplete_lineage_final)
            with self.assertRaisesRegex(ProtocolError, "lineage claim"):
                phase_protocol.validate_replay(
                    root,
                    workflow_sha256=workflow_digest,
                    final_sha256=incomplete_lineage_final_digest,
                )
            put_json("final.json", final)

            foreign_amendment = json.loads((root / amendment_ref).read_text())
            foreign_amendment["workflow_id"] = "foreign-workflow"
            foreign_amendment_digest = put_json(amendment_ref, foreign_amendment)
            foreign_final = deepcopy(final)
            foreign_final["amendment_sha256"][amendment_ref] = foreign_amendment_digest
            foreign_final_digest = put_json("final.json", foreign_final)
            with self.assertRaisesRegex(ProtocolError, "amendment workflow id"):
                phase_protocol.validate_replay(
                    root,
                    workflow_sha256=workflow_digest,
                    final_sha256=foreign_final_digest,
                )
            put_json(
                amendment_ref,
                {
                    **foreign_amendment,
                    "workflow_id": workflow["workflow_id"],
                },
            )
            put_json("final.json", final)

            wrong_plan = deepcopy(verify_plan)
            wrong_plan["predecessor_sha256"] = baseline_digest
            wrong_plan_digest = put_json("phases/002-verify/plan.json", wrong_plan)
            wrong_claim_ref, wrong_claim_digest = put_generation_claim(
                wrong_plan,
                wrong_plan_digest,
            )
            wrong_receipt = deepcopy(verify_receipt)
            wrong_receipt.update({
                "plan_sha256": wrong_plan_digest,
                "predecessor_sha256": baseline_digest,
                "generation_claim_ref": wrong_claim_ref,
                "generation_claim_sha256": wrong_claim_digest,
            })
            wrong_receipt_digest = put_json("phases/002-verify/receipt.json", wrong_receipt)
            wrong_final = deepcopy(final)
            wrong_final["phase_receipt_sha256"]["phases/002-verify/receipt.json"] = wrong_receipt_digest
            wrong_final["verification_sha256"] = wrong_receipt_digest
            wrong_final_digest = put_json("final.json", wrong_final)
            with self.assertRaisesRegex(ProtocolError, "immediately prior"):
                phase_protocol.validate_replay(
                    root,
                    workflow_sha256=workflow_digest,
                    final_sha256=wrong_final_digest,
                )
            put_json("phases/002-verify/plan.json", verify_plan)
            put_json("phases/002-verify/receipt.json", verify_receipt)
            put_json("final.json", final)

            (root / research_result["output_ref"]).write_bytes(b"tampered\n")
            with self.assertRaisesRegex(ProtocolError, "digest mismatch"):
                phase_protocol.validate_replay(
                    root,
                    workflow_sha256=workflow_digest,
                    final_sha256=final_digest,
                )

    def test_replay_rejects_authority_causality_and_runtime_bundle_drift(self) -> None:
        workflow = load_fixture("valid/workflow.json")
        plan = load_fixture("valid/phase-plan.json")
        plan["tasks"][0]["role"] = "top"
        result = load_fixture("valid/task-result.json")
        result["actual_route"]["model"] = workflow["routing"]["top_model"]
        receipt = load_fixture("valid/phase-receipt.json")
        final = load_fixture("valid/final.json")
        receipt_ref = "phases/001-research/receipt.json"
        final.update({
            "verification_ref": receipt_ref,
            "verification_sha256": final["phase_receipt_sha256"][receipt_ref],
            "phase_receipt_refs": [receipt_ref],
            "phase_receipt_sha256": {receipt_ref: final["phase_receipt_sha256"][receipt_ref]},
            "lineage_claim_refs": ["lineages/lineage-contract/origin.json"],
            "lineage_claim_sha256": {
                "lineages/lineage-contract/origin.json": "sha256:" + "8" * 64,
            },
        })

        def run(candidate_plan: dict[str, object], candidate_final: dict[str, object]) -> None:
            def read_json(_root: Path, relative: str, _digest: str, kind: str) -> dict[str, object]:
                if kind == "workflow":
                    return workflow
                if kind == "final":
                    return candidate_final
                if kind == "phase-receipt":
                    return receipt
                if kind == "phase-plan":
                    return candidate_plan
                if kind == "task-result":
                    return result
                raise AssertionError(kind)

            def read_bytes(_root: Path, relative: str, _digest: str) -> bytes:
                if relative == result["output_ref"]:
                    decision = {
                        "schema_version": "agent-workflow.verification-decision.vnext.v1",
                        "workflow_id": workflow["workflow_id"],
                        "phase_id": candidate_plan["phase_id"],
                        "task_id": candidate_plan["tasks"][0]["task_id"],
                        "decision": "pass",
                        "confidence": "high",
                        "criteria": [
                            {
                                "criterion_id": "AC-1",
                                "status": "pass",
                                "evidence_refs": result["evidence_refs"],
                            }
                        ],
                        "findings": [],
                        "commands": [
                            {
                                "command": "focused-test",
                                "exit_code": 0,
                                "evidence_ref": result["evidence_refs"][0],
                            }
                        ],
                    }
                    return (json.dumps(decision, sort_keys=True, separators=(",", ":")) + "\n").encode()
                if relative == "lineages/lineage-contract/origin.json":
                    task = candidate_plan["tasks"][0]
                    claim = {
                        "schema_version": "agent-workflow.lineage-claim.vnext.v1",
                        "claim_kind": "origin",
                        "workflow_id": workflow["workflow_id"],
                        "lineage_id": task["lineage_id"],
                        "criterion_id": task["criterion_id"],
                        "criterion_revision": task["criterion_revision"],
                        "role": task["role"],
                        "scope_sha256": scope_sha256(task),
                        "origin_phase_id": candidate_plan["phase_id"],
                        "origin_task_id": task["task_id"],
                    }
                    return (json.dumps(claim, sort_keys=True, separators=(",", ":")) + "\n").encode()
                if relative == receipt["generation_claim_ref"]:
                    contention = {
                        "predecessor_sha256": candidate_plan["predecessor_sha256"],
                        "authority_revision": candidate_plan["authority_revision"],
                    }
                    contention_key = "sha256:" + hashlib.sha256(
                        (json.dumps(contention, sort_keys=True, separators=(",", ":")) + "\n").encode()
                    ).hexdigest()
                    claim = {
                        "schema_version": "agent-workflow.generation-claim.vnext.v1",
                        "workflow_id": workflow["workflow_id"],
                        "generation_id": candidate_plan["generation_id"],
                        "phase_id": candidate_plan["phase_id"],
                        "predecessor_sha256": candidate_plan["predecessor_sha256"],
                        "authority_revision": candidate_plan["authority_revision"],
                        "plan_sha256": receipt["plan_sha256"],
                        "contention_key": contention_key,
                    }
                    return (json.dumps(claim, sort_keys=True, separators=(",", ":")) + "\n").encode()
                return b'{"baseline_kind":"pre_slice"}'

            with (
                mock.patch.object(phase_protocol, "_read_json_artifact", side_effect=read_json),
                mock.patch.object(
                    phase_protocol,
                    "_read_artifact_bytes",
                    side_effect=read_bytes,
                ),
                mock.patch.object(phase_protocol, "verify_baseline", return_value={}),
                mock.patch.object(
                    phase_protocol,
                    "repository_evidence",
                    return_value=workflow["admission"]["repository"],
                ),
            ):
                phase_protocol.validate_replay(
                    Path("unused"),
                    workflow_sha256="sha256:" + "1" * 64,
                    final_sha256="sha256:" + "2" * 64,
                )

        authority = deepcopy(plan)
        authority["authority_revision"] = 999
        with self.assertRaisesRegex(ProtocolError, "authority revision"):
            run(authority, deepcopy(final))

        causality = deepcopy(plan)
        causality["caused_by"] = ["missing-phase"]
        with self.assertRaisesRegex(ProtocolError, "missing or future"):
            run(causality, deepcopy(final))

        bundle = deepcopy(final)
        bundle["runtime_bundle_sha256"] = "sha256:" + "9" * 64
        with self.assertRaisesRegex(ProtocolError, "runtime bundle"):
            run(deepcopy(plan), bundle)

        original_model = result["actual_route"]["model"]
        result["actual_route"]["model"] = "wrong-model"
        with self.assertRaisesRegex(ProtocolError, "route does not match"):
            run(deepcopy(plan), deepcopy(final))
        result["actual_route"]["model"] = original_model

        write_without_integration = deepcopy(plan)
        write_without_integration["tasks"][0].update({
            "work_mode": "write",
            "write_roots": ["src"],
        })
        with self.assertRaisesRegex(ProtocolError, "work mode"):
            run(write_without_integration, deepcopy(final))

    def test_replay_binds_exact_lineage_recovery_claim(self) -> None:
        workflow = load_fixture("valid/workflow.json")
        first_plan = load_fixture("valid/phase-plan.json")
        first_plan["predecessor_sha256"] = workflow["baseline_sha256"]
        first_task = first_plan["tasks"][0]
        first_result = load_fixture("valid/task-result.json")
        first_result.update(
            {
                "status": "failed",
                "terminal_reason": "runner_error",
                "output_ref": None,
                "output_sha256": None,
                "changed_paths": [],
            }
        )
        first_result_ref = "phases/001-research/tasks/research-contract/result.json"
        first_result_sha = "sha256:" + "3" * 64
        first_receipt_ref = "phases/001-research/receipt.json"
        first_receipt_sha = "sha256:" + "4" * 64
        first_receipt = load_fixture("valid/phase-receipt.json")
        first_receipt.update(
            {
                "status": "failed",
                "predecessor_sha256": first_plan["predecessor_sha256"],
                "task_result_refs": [first_result_ref],
                "task_result_sha256": {first_result_ref: first_result_sha},
                "terminal_reason": "task_failures_terminal",
            }
        )
        first_counts = {key: 0 for key in first_receipt["task_counts"]}
        first_counts.update({"total": 1, "failed": 1})
        first_receipt["task_counts"] = first_counts

        recovery_plan = deepcopy(first_plan)
        recovery_plan.update(
            {
                "phase_id": "002-recover",
                "caused_by": ["001-research"],
                "predecessor_sha256": first_receipt_sha,
            }
        )
        recovery_task = recovery_plan["tasks"][0]
        recovery_task.update(
            {
                "task_id": "recover-contract",
                "packet_path": "phases/002-recover/tasks/recover-contract/packet.md",
                "input_refs": [first_result_ref, first_receipt_ref],
                "input_sha256": {
                    first_result_ref: first_result_sha,
                    first_receipt_ref: first_receipt_sha,
                },
            }
        )
        recovery_result = deepcopy(first_result)
        recovery_result.update(
            {
                "phase_id": "002-recover",
                "task_id": "recover-contract",
                "status": "completed",
                "terminal_reason": "completed",
                "output_ref": "phases/002-recover/tasks/recover-contract/output.json",
                "output_sha256": "sha256:" + "5" * 64,
            }
        )
        recovery_result_ref = "phases/002-recover/tasks/recover-contract/result.json"
        recovery_result_sha = "sha256:" + "6" * 64
        recovery_receipt_ref = "phases/002-recover/receipt.json"
        recovery_receipt_sha = "sha256:" + "7" * 64
        recovery_receipt = deepcopy(first_receipt)
        recovery_receipt.update(
            {
                "phase_id": "002-recover",
                "status": "completed",
                "predecessor_sha256": first_receipt_sha,
                "task_result_refs": [recovery_result_ref],
                "task_result_sha256": {recovery_result_ref: recovery_result_sha},
                "terminal_reason": "all_tasks_terminal",
            }
        )
        recovery_counts = {key: 0 for key in recovery_receipt["task_counts"]}
        recovery_counts.update({"total": 1, "completed": 1})
        recovery_receipt["task_counts"] = recovery_counts

        def generation_claim(plan: dict[str, object], plan_sha: str) -> dict[str, object]:
            contention = {
                "predecessor_sha256": plan["predecessor_sha256"],
                "authority_revision": plan["authority_revision"],
            }
            contention_key = "sha256:" + hashlib.sha256(
                (json.dumps(contention, sort_keys=True, separators=(",", ":")) + "\n").encode()
            ).hexdigest()
            return {
                "schema_version": "agent-workflow.generation-claim.vnext.v1",
                "workflow_id": workflow["workflow_id"],
                "generation_id": plan["generation_id"],
                "phase_id": plan["phase_id"],
                "predecessor_sha256": plan["predecessor_sha256"],
                "authority_revision": plan["authority_revision"],
                "plan_sha256": plan_sha,
                "contention_key": contention_key,
            }

        first_plan_sha = first_receipt["plan_sha256"]
        recovery_plan_sha = "sha256:" + "8" * 64
        recovery_receipt["plan_sha256"] = recovery_plan_sha
        first_generation = generation_claim(first_plan, first_plan_sha)
        recovery_generation = generation_claim(recovery_plan, recovery_plan_sha)
        first_receipt["generation_claim_ref"] = "generations/claims/first.json"
        first_receipt["generation_claim_sha256"] = "sha256:" + "9" * 64
        recovery_receipt["generation_claim_ref"] = "generations/claims/recovery.json"
        recovery_receipt["generation_claim_sha256"] = "sha256:" + "a" * 64

        origin_ref = "lineages/lineage-contract/origin.json"
        origin = {
            "schema_version": "agent-workflow.lineage-claim.vnext.v1",
            "claim_kind": "origin",
            "workflow_id": workflow["workflow_id"],
            "lineage_id": first_task["lineage_id"],
            "criterion_id": first_task["criterion_id"],
            "criterion_revision": first_task["criterion_revision"],
            "role": first_task["role"],
            "scope_sha256": scope_sha256(first_task),
            "origin_phase_id": first_plan["phase_id"],
            "origin_task_id": first_task["task_id"],
        }
        origin_payload = (json.dumps(origin, sort_keys=True, separators=(",", ":")) + "\n").encode()
        recovery_ref = "lineages/lineage-contract/recovery.json"
        recovery_claim = {
            "schema_version": "agent-workflow.lineage-claim.vnext.v1",
            "claim_kind": "recovery",
            "workflow_id": workflow["workflow_id"],
            "lineage_id": first_task["lineage_id"],
            "origin_ref": origin_ref,
            "origin_sha256": "sha256:" + hashlib.sha256(origin_payload).hexdigest(),
            "failed_result_ref": first_result_ref,
            "failed_result_sha256": first_result_sha,
            "recovery_phase_id": recovery_plan["phase_id"],
            "recovery_task_id": recovery_task["task_id"],
            "recovery_scope_sha256": scope_sha256(recovery_task),
            "criterion_id": recovery_task["criterion_id"],
            "criterion_revision": recovery_task["criterion_revision"],
            "authority_revision": recovery_plan["authority_revision"],
            "recovery_kind": "automatic_infra_retry",
        }
        recovery_payload = (
            json.dumps(recovery_claim, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()

        final = load_fixture("valid/final.json")
        final.update(
            {
                "status": "blocked",
                "verification_ref": None,
                "verification_sha256": None,
                "phase_receipt_refs": [first_receipt_ref, recovery_receipt_ref],
                "phase_receipt_sha256": {
                    first_receipt_ref: first_receipt_sha,
                    recovery_receipt_ref: recovery_receipt_sha,
                },
                "lineage_claim_refs": [origin_ref, recovery_ref],
                "lineage_claim_sha256": {
                    origin_ref: "sha256:" + hashlib.sha256(origin_payload).hexdigest(),
                    recovery_ref: "sha256:" + hashlib.sha256(recovery_payload).hexdigest(),
                },
                "runtime_bundle_sha256": workflow["runtime_bundle"]["sha256"],
            }
        )

        plans = {first_plan["phase_id"]: first_plan, recovery_plan["phase_id"]: recovery_plan}
        receipts = {first_receipt_ref: first_receipt, recovery_receipt_ref: recovery_receipt}
        results = {first_result_ref: first_result, recovery_result_ref: recovery_result}
        payloads = {
            first_receipt["generation_claim_ref"]: (
                json.dumps(first_generation, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode(),
            recovery_receipt["generation_claim_ref"]: (
                json.dumps(recovery_generation, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode(),
            origin_ref: origin_payload,
            recovery_ref: recovery_payload,
        }

        def read_json(_root: Path, relative: str, _digest: str, kind: str) -> dict[str, object]:
            if kind == "workflow":
                return workflow
            if kind == "phase-receipt":
                return receipts[relative]
            if kind == "phase-plan":
                return plans[Path(relative).parts[1]]
            if kind == "task-result":
                return results[relative]
            raise AssertionError(kind)

        def run(candidate_claim: dict[str, object]) -> None:
            payloads[recovery_ref] = (
                json.dumps(candidate_claim, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode()
            candidate = deepcopy(final)
            candidate["lineage_claim_sha256"][recovery_ref] = (
                "sha256:" + hashlib.sha256(payloads[recovery_ref]).hexdigest()
            )
            def read_bytes(_root: Path, relative: str, _digest: str) -> bytes:
                if relative == workflow["baseline_ref"]:
                    return b'{"baseline_kind":"pre_slice"}\n'
                return payloads.get(relative, b"{}\n")

            with (
                mock.patch.object(phase_protocol, "_read_json_artifact", side_effect=read_json),
                mock.patch.object(
                    phase_protocol,
                    "_read_artifact_bytes",
                    side_effect=read_bytes,
                ),
                mock.patch.object(phase_protocol, "verify_baseline", return_value={}),
                mock.patch.object(
                    phase_protocol,
                    "repository_evidence",
                    return_value=workflow["admission"]["repository"],
                ),
            ):
                phase_protocol.validate_replay_candidate(
                    Path("unused"),
                    workflow_sha256="sha256:" + "1" * 64,
                    final=candidate,
                )

        run(deepcopy(recovery_claim))
        tampered = deepcopy(recovery_claim)
        tampered["failed_result_sha256"] = "sha256:" + "f" * 64
        with self.assertRaisesRegex(ProtocolError, "exact repair"):
            run(tampered)

    def test_promotion_corpus_and_hidden_checks_are_frozen_and_cross_referenced(self) -> None:
        import hashlib
        import json

        corpus_bytes = (CANARY_ROOT / "corpus.v1.json").read_bytes()
        hidden_bytes = (CANARY_ROOT / "hidden-checks.v1.json").read_bytes()
        seal_bytes = (CANARY_ROOT / "seal.v1.json").read_bytes()
        self.assertEqual(
            "sha256:" + hashlib.sha256(seal_bytes).hexdigest(),
            "sha256:d56691672c5700ce91fbb7d31d9f9288cf5790db276eff14ec9283805fc09148",
        )
        seal = json.loads(seal_bytes)
        self.assertEqual(seal["schema_version"], "agent-workflow.canary-seal.v1")
        self.assertEqual(
            "sha256:" + hashlib.sha256(corpus_bytes).hexdigest(),
            seal["corpus_sha256"],
        )
        self.assertEqual(
            "sha256:" + hashlib.sha256(hidden_bytes).hexdigest(),
            seal["hidden_checks_sha256"],
        )
        corpus = json.loads(corpus_bytes)
        hidden = json.loads(hidden_bytes)

        self.assertEqual(corpus["schema_version"], "agent-workflow.canary-corpus.v1")
        self.assertEqual(hidden["schema_version"], "agent-workflow.hidden-checks.v1")
        self.assertEqual(corpus["paired_trials_per_workload"], 5)
        self.assertEqual(
            {item["workload_class"] for item in corpus["workloads"]},
            {
                "read_research",
                "disjoint_multi_writer",
                "single_writer_integration",
                "failure_recovery",
                "long_verification",
            },
        )
        check_ids = [item["id"] for item in hidden["checks"]]
        self.assertEqual(len(check_ids), len(set(check_ids)))
        self.assertTrue(check_ids)
        for workload in corpus["workloads"]:
            self.assertTrue(workload["hard_check_ids"])
            self.assertLessEqual(set(workload["hard_check_ids"]), set(check_ids))
        run_seal_schema = json.loads(
            (CANARY_ROOT / "run-seal.schema.v1.json").read_text(encoding="utf-8")
        )
        self.assertIn("candidate_bundle_sha256", run_seal_schema["required"])
        self.assertIn("blind_label_map_sha256", run_seal_schema["required"])
        self.assertIn("rubric_sha256", run_seal_schema["required"])

if __name__ == "__main__":
    unittest.main(verbosity=2)
