#!/usr/bin/env python3
"""Focused tests for bounded vNext lineage, amendment, expansion, and resume authority."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from artifact_store import create_once_json
import source_workspace
from recovery_runtime import (
    RecoveryError,
    causal_predecessor_sha256,
    commit_phase_authority,
    prepare_phase_authority,
    seal_amendment,
    seal_resume_brief,
)


FIXTURES = SCRIPT_DIR.parent / "fixtures" / "vnext" / "protocol" / "valid"


def fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text())


def canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def digest(payload: bytes) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(payload).hexdigest()


class RecoveryRuntimeTests(unittest.TestCase):
    def commit_plan_claim(self, root: Path, workflow: dict[str, object], plan: dict[str, object]) -> None:
        contention = {
            "predecessor_sha256": plan["predecessor_sha256"],
            "authority_revision": plan["authority_revision"],
        }
        contention_key = digest(canonical(contention))
        claim = {
            "schema_version": "agent-workflow.generation-claim.vnext.v1",
            "workflow_id": workflow["workflow_id"],
            "generation_id": plan["generation_id"],
            "phase_id": plan["phase_id"],
            "predecessor_sha256": plan["predecessor_sha256"],
            "authority_revision": plan["authority_revision"],
            "plan_sha256": digest(canonical(plan)),
            "contention_key": contention_key,
        }
        create_once_json(
            root,
            f"generations/claims/{contention_key.removeprefix('sha256:')}.json",
            claim,
        )

    def initial(self, root: Path, *, max_additional: int = 2) -> tuple[dict[str, object], dict[str, object]]:
        workflow = fixture("workflow.json")
        workflow["limits"]["max_additional_phases"] = max_additional
        plan = fixture("phase-plan.json")
        plan["predecessor_sha256"] = workflow["baseline_sha256"]
        authority = prepare_phase_authority(root, workflow, plan)
        self.assertEqual(authority.additional_phase_index, 0)
        commit_phase_authority(root, authority)
        create_once_json(root, "workflow.json", workflow)
        create_once_json(root, f"phases/{plan['phase_id']}/plan.json", plan)
        self.commit_plan_claim(root, workflow, plan)
        return workflow, plan

    def terminal_result(
        self,
        root: Path,
        plan: dict[str, object],
        *,
        completed: bool = False,
    ) -> tuple[str, dict[str, object]]:
        task = plan["tasks"][0]
        result = fixture("task-result.json")
        result.update(
            {
                "workflow_id": "fixture-workflow",
                "phase_id": plan["phase_id"],
                "task_id": task["task_id"],
                "lineage_id": task["lineage_id"],
            }
        )
        if not completed:
            result.update(
                {
                    "status": "failed",
                    "terminal_reason": "runner_error",
                    "output_ref": None,
                    "output_sha256": None,
                    "changed_paths": [],
                }
            )
        result_ref = f"phases/{plan['phase_id']}/tasks/{task['task_id']}/result.json"
        result_path = create_once_json(root, result_ref, result)
        receipt = fixture("phase-receipt.json")
        receipt.update(
            {
                "workflow_id": "fixture-workflow",
                "phase_id": plan["phase_id"],
                "generation_id": plan["generation_id"],
                "plan_sha256": digest(canonical(plan)),
                "predecessor_sha256": plan["predecessor_sha256"],
                "status": "completed" if completed else "failed",
                "task_result_refs": [result_ref],
                "task_result_sha256": {result_ref: digest(result_path.read_bytes())},
                "terminal_reason": "all_tasks_terminal" if completed else "task_failures_terminal",
            }
        )
        counts = {key: 0 for key in receipt["task_counts"]}
        counts["total"] = 1
        counts["completed" if completed else "failed"] = 1
        receipt["task_counts"] = counts
        create_once_json(root, f"phases/{plan['phase_id']}/receipt.json", receipt)
        return result_ref, result

    def recovery_plan(
        self,
        root: Path,
        initial: dict[str, object],
        failed_ref: str,
        *,
        lineage_id: str | None = None,
        phase_id: str = "002-recover",
        authority_revision: int = 1,
        criterion_revision: int = 1,
    ) -> dict[str, object]:
        plan = deepcopy(initial)
        plan["phase_id"] = phase_id
        plan["caused_by"] = [initial["phase_id"]]
        plan["predecessor_sha256"] = causal_predecessor_sha256(root, plan["caused_by"])
        plan["authority_revision"] = authority_revision
        task = plan["tasks"][0]
        task["task_id"] = f"task-{phase_id}"
        task["lineage_id"] = lineage_id or initial["tasks"][0]["lineage_id"]
        task["criterion_revision"] = criterion_revision
        task["packet_path"] = f"phases/{phase_id}/tasks/{task['task_id']}/packet.md"
        causal_ref = f"phases/{initial['phase_id']}/receipt.json"
        task["input_refs"] = [failed_ref, causal_ref]
        task["input_sha256"] = {
            failed_ref: digest((root / failed_ref).read_bytes()),
            causal_ref: digest((root / causal_ref).read_bytes()),
        }
        return plan

    def test_one_failed_lineage_gets_one_recovery_and_id_scope_changes_do_not_reset_it(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workflow, initial = self.initial(root)
            failed_ref, _ = self.terminal_result(root, initial)
            recovery = self.recovery_plan(root, initial, failed_ref)
            authority = prepare_phase_authority(root, workflow, recovery)
            refs = commit_phase_authority(root, authority)
            self.assertIn("lineages/lineage-contract/recovery.json", refs)
            changed = deepcopy(recovery)
            changed["phase_id"] = "003-renamed"
            changed["tasks"][0]["task_id"] = "renamed-task"
            changed["tasks"][0]["write_roots"] = []
            with self.assertRaisesRegex(RecoveryError, "already exhausted"):
                prepare_phase_authority(root, workflow, changed)

    def test_new_lineage_cannot_bypass_failed_equivalent_scope(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workflow, initial = self.initial(root)
            failed_ref, _ = self.terminal_result(root, initial)
            bypass = self.recovery_plan(root, initial, failed_ref, lineage_id="lineage-renamed")
            bypass["tasks"][0]["work_mode"] = "write"
            bypass["tasks"][0]["write_roots"] = ["src/renamed-scope"]
            with self.assertRaisesRegex(RecoveryError, "bypass"):
                prepare_phase_authority(root, workflow, bypass)

    def test_recovery_must_consume_exact_failed_result_and_causal_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workflow, initial = self.initial(root)
            failed_ref, _ = self.terminal_result(root, initial)
            recovery = self.recovery_plan(root, initial, failed_ref)
            causal_ref = f"phases/{initial['phase_id']}/receipt.json"
            recovery["tasks"][0]["input_refs"].remove(causal_ref)
            recovery["tasks"][0]["input_sha256"].pop(causal_ref)
            with self.assertRaisesRegex(RecoveryError, "causal receipt"):
                prepare_phase_authority(root, workflow, recovery)

    def test_successful_sibling_lineage_cannot_be_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workflow, initial = self.initial(root)
            result_ref, _ = self.terminal_result(root, initial, completed=True)
            rerun = self.recovery_plan(root, initial, result_ref)
            with self.assertRaisesRegex(RecoveryError, "successful lineage"):
                prepare_phase_authority(root, workflow, rerun)
            renamed = self.recovery_plan(
                root,
                initial,
                result_ref,
                lineage_id="lineage-renamed-success",
            )
            renamed["tasks"][0]["work_mode"] = "write"
            renamed["tasks"][0]["write_roots"] = ["src/changed-scope"]
            with self.assertRaisesRegex(RecoveryError, "successful same-role work"):
                prepare_phase_authority(root, workflow, renamed)

    def test_mixed_sibling_phase_recovers_only_the_failed_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workflow = fixture("workflow.json")
            initial = fixture("phase-plan.json")
            initial["predecessor_sha256"] = workflow["baseline_sha256"]
            failed_task = deepcopy(initial["tasks"][0])
            failed_task.update(
                {
                    "task_id": "research-failing-sibling",
                    "lineage_id": "lineage-failing-sibling",
                    "packet_path": "phases/001-research/tasks/research-failing-sibling/packet.md",
                }
            )
            initial["tasks"].append(failed_task)
            authority = prepare_phase_authority(root, workflow, initial)
            commit_phase_authority(root, authority)
            create_once_json(root, "workflow.json", workflow)
            create_once_json(root, "phases/001-research/plan.json", initial)
            self.commit_plan_claim(root, workflow, initial)

            result_refs: list[str] = []
            result_sha256: dict[str, str] = {}
            for task, completed in zip(initial["tasks"], (True, False), strict=True):
                result = fixture("task-result.json")
                result.update(
                    {
                        "workflow_id": workflow["workflow_id"],
                        "phase_id": initial["phase_id"],
                        "task_id": task["task_id"],
                        "lineage_id": task["lineage_id"],
                    }
                )
                if not completed:
                    result.update(
                        {
                            "status": "failed",
                            "terminal_reason": "runner_error",
                            "output_ref": None,
                            "output_sha256": None,
                            "changed_paths": [],
                        }
                    )
                result_ref = f"phases/001-research/tasks/{task['task_id']}/result.json"
                result_path = create_once_json(root, result_ref, result)
                result_refs.append(result_ref)
                result_sha256[result_ref] = digest(result_path.read_bytes())
            receipt = fixture("phase-receipt.json")
            task_counts = {key: 0 for key in receipt["task_counts"]}
            task_counts.update({"total": 2, "completed": 1, "failed": 1})
            receipt.update(
                {
                    "workflow_id": workflow["workflow_id"],
                    "phase_id": initial["phase_id"],
                    "generation_id": initial["generation_id"],
                    "plan_sha256": digest(canonical(initial)),
                    "predecessor_sha256": initial["predecessor_sha256"],
                    "status": "failed",
                    "task_result_refs": result_refs,
                    "task_result_sha256": result_sha256,
                    "task_counts": task_counts,
                    "terminal_reason": "task_failures_terminal",
                }
            )
            receipt_path = create_once_json(root, "phases/001-research/receipt.json", receipt)

            recovery = deepcopy(initial)
            recovery["phase_id"] = "002-recover"
            recovery["caused_by"] = [initial["phase_id"]]
            recovery["predecessor_sha256"] = digest(receipt_path.read_bytes())
            recovery_task = deepcopy(failed_task)
            recovery_task.update(
                {
                    "task_id": "recover-failing-sibling",
                    "packet_path": "phases/002-recover/tasks/recover-failing-sibling/packet.md",
                    "input_refs": [result_refs[1], "phases/001-research/receipt.json"],
                    "input_sha256": {
                        result_refs[1]: result_sha256[result_refs[1]],
                        "phases/001-research/receipt.json": digest(receipt_path.read_bytes()),
                    },
                }
            )
            recovery["tasks"] = [recovery_task]
            prepared = prepare_phase_authority(root, workflow, recovery)
            self.assertEqual(
                [ref for ref, _claim in prepared.claim_values],
                ["lineages/lineage-failing-sibling/recovery.json"],
            )

            successful_rerun = deepcopy(initial["tasks"][0])
            successful_rerun.update(
                {
                    "task_id": "rerun-successful-sibling",
                    "packet_path": "phases/002-recover/tasks/rerun-successful-sibling/packet.md",
                }
            )
            recovery["tasks"].append(successful_rerun)
            with self.assertRaisesRegex(RecoveryError, "successful lineage"):
                prepare_phase_authority(root, workflow, recovery)

    def test_max_additional_phases_is_one_workflow_ceiling(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workflow, initial = self.initial(root, max_additional=1)
            failed_ref, _ = self.terminal_result(root, initial)
            second = self.recovery_plan(root, initial, failed_ref)
            second_authority = prepare_phase_authority(root, workflow, second)
            create_once_json(root, "phases/002-recover/plan.json", second)
            self.commit_plan_claim(root, workflow, second)
            commit_phase_authority(root, second_authority)
            self.terminal_result(root, second)
            third = self.recovery_plan(root, initial, failed_ref, phase_id="003-extra")
            third["caused_by"] = ["002-recover"]
            third["predecessor_sha256"] = causal_predecessor_sha256(root, third["caused_by"])
            with self.assertRaisesRegex(RecoveryError, "max_additional_phases"):
                prepare_phase_authority(root, workflow, third)

    def test_new_generation_requires_current_resume_brief(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workflow, initial = self.initial(root)
            failed_ref, _ = self.terminal_result(root, initial)
            recovery = self.recovery_plan(root, initial, failed_ref)
            recovery["generation_id"] = "generation-999"
            with self.assertRaisesRegex(RecoveryError, "resume brief"):
                prepare_phase_authority(root, workflow, recovery)
            seal_resume_brief(root, workflow, "generation-002")
            recovery["generation_id"] = "generation-002"
            authority = prepare_phase_authority(root, workflow, recovery)
            self.assertEqual(authority.causal_predecessor_sha256, recovery["predecessor_sha256"])

    def test_stale_or_reordered_causes_cannot_branch_from_an_old_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workflow, initial = self.initial(root)
            failed_ref, _ = self.terminal_result(root, initial)
            recovery = self.recovery_plan(root, initial, failed_ref)
            authority = prepare_phase_authority(root, workflow, recovery)
            create_once_json(root, "phases/002-recover/plan.json", recovery)
            self.commit_plan_claim(root, workflow, recovery)
            commit_phase_authority(root, authority)
            self.terminal_result(root, recovery)

            stale = deepcopy(recovery)
            stale["phase_id"] = "003-stale"
            stale["caused_by"] = ["002-recover", "001-research"]
            stale["predecessor_sha256"] = causal_predecessor_sha256(root, stale["caused_by"])
            with self.assertRaisesRegex(RecoveryError, "latest terminal phase"):
                prepare_phase_authority(root, workflow, stale)

    def test_user_evidenced_amendment_advances_authority_and_allows_new_revision_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workflow, initial = self.initial(root)
            failed_ref, _ = self.terminal_result(root, initial)
            instruction_ref = "amendments/user-instruction-0002.md"
            instruction = b"Revise AC-1 after the blocked outcome.\n"
            (root / "amendments").mkdir(exist_ok=True)
            (root / instruction_ref).write_bytes(instruction)
            amendment = {
                "schema_version": "agent-workflow.amendment.vnext.v1",
                "amendment_kind": "criterion_revision",
                "workflow_id": workflow["workflow_id"],
                "amendment_id": "revise-ac-1",
                "previous_authority_revision": 1,
                "authority_revision": 2,
                "criterion_id": "AC-1",
                "from_revision": 1,
                "to_revision": 2,
                "blocked_result_ref": failed_ref,
                "blocked_result_sha256": digest((root / failed_ref).read_bytes()),
                "user_instruction_ref": instruction_ref,
                "user_instruction_sha256": digest(instruction),
                "reason": "User changed the criterion after reviewing the blocked result.",
            }
            invalid = deepcopy(amendment)
            invalid["user_instruction_sha256"] = "sha256:" + "0" * 64
            with self.assertRaisesRegex(RecoveryError, "instruction evidence drifted"):
                seal_amendment(root, workflow, invalid)
            self.assertFalse((root / "amendments/criteria/0002-revise-ac-1.json").exists())
            seal_amendment(root, workflow, amendment)
            reused = self.recovery_plan(
                root,
                initial,
                failed_ref,
                authority_revision=2,
                criterion_revision=2,
            )
            with self.assertRaisesRegex(RecoveryError, "new lineage"):
                prepare_phase_authority(root, workflow, reused)
            revised = self.recovery_plan(
                root,
                initial,
                failed_ref,
                lineage_id="lineage-revised",
                authority_revision=2,
                criterion_revision=2,
            )
            authority = prepare_phase_authority(root, workflow, revised)
            self.assertEqual(authority.current_authority_revision, 2)

    def test_criterion_amendment_rejects_blocked_result_before_latest_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workflow, initial = self.initial(root)
            failed_ref, _ = self.terminal_result(root, initial)
            recovery = self.recovery_plan(root, initial, failed_ref)
            authority = prepare_phase_authority(root, workflow, recovery)
            create_once_json(root, "phases/002-recover/plan.json", recovery)
            self.commit_plan_claim(root, workflow, recovery)
            commit_phase_authority(root, authority)
            self.terminal_result(root, recovery, completed=True)

            instruction_ref = "amendments/late-criterion.md"
            instruction = b"Revise AC-1 only at the current terminal boundary.\n"
            (root / "amendments").mkdir(exist_ok=True)
            (root / instruction_ref).write_bytes(instruction)
            amendment = {
                "schema_version": "agent-workflow.amendment.vnext.v1",
                "amendment_kind": "criterion_revision",
                "workflow_id": workflow["workflow_id"],
                "amendment_id": "late-criterion",
                "previous_authority_revision": 1,
                "authority_revision": 2,
                "criterion_id": "AC-1",
                "from_revision": 1,
                "to_revision": 2,
                "blocked_result_ref": failed_ref,
                "blocked_result_sha256": digest((root / failed_ref).read_bytes()),
                "user_instruction_ref": instruction_ref,
                "user_instruction_sha256": digest(instruction),
                "reason": "A historical blocked result cannot rewrite an intervening phase.",
            }
            with self.assertRaisesRegex(RecoveryError, "latest terminal boundary"):
                seal_amendment(root, workflow, amendment)

    def test_instruction_amendment_is_opaque_and_applies_after_exact_terminal_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workflow, initial = self.initial(root)
            self.terminal_result(root, initial, completed=True)
            instruction_ref = "amendments/instruction-0002.md"
            instruction = b"At the next phase, inspect the compatibility seam too.\n"
            (root / "amendments").mkdir(exist_ok=True)
            (root / instruction_ref).write_bytes(instruction)
            receipt_ref = f"phases/{initial['phase_id']}/receipt.json"
            amendment = {
                "schema_version": "agent-workflow.amendment.vnext.v1",
                "amendment_kind": "instruction",
                "workflow_id": workflow["workflow_id"],
                "amendment_id": "compatibility-seam",
                "previous_authority_revision": 1,
                "authority_revision": 2,
                "applies_after_ref": receipt_ref,
                "applies_after_sha256": digest((root / receipt_ref).read_bytes()),
                "user_instruction_ref": instruction_ref,
                "user_instruction_sha256": digest(instruction),
                "reason": "User added a bounded instruction for the next phase.",
            }
            path = seal_amendment(root, workflow, amendment)
            self.assertTrue(path.is_file())

    def test_instruction_amendment_rejects_a_stale_terminal_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workflow, initial = self.initial(root)
            self.terminal_result(root, initial, completed=True)
            second = deepcopy(initial)
            second.update(
                {
                    "phase_id": "002-verify",
                    "caused_by": [initial["phase_id"]],
                    "predecessor_sha256": causal_predecessor_sha256(
                        root,
                        [initial["phase_id"]],
                    ),
                }
            )
            second["tasks"][0].update(
                {
                    "task_id": "independent-verifier",
                    "lineage_id": "lineage-independent-verifier",
                    "role": "top",
                    "packet_path": "phases/002-verify/tasks/independent-verifier/packet.md",
                    "input_refs": ["phases/001-research/receipt.json"],
                    "input_sha256": {
                        "phases/001-research/receipt.json": digest(
                            (root / "phases/001-research/receipt.json").read_bytes()
                        )
                    },
                }
            )
            authority = prepare_phase_authority(root, workflow, second)
            create_once_json(root, "phases/002-verify/plan.json", second)
            self.commit_plan_claim(root, workflow, second)
            commit_phase_authority(root, authority)
            self.terminal_result(root, second, completed=True)

            instruction_ref = "amendments/stale-instruction.md"
            instruction = b"This must apply only after the latest terminal boundary.\n"
            (root / "amendments").mkdir(exist_ok=True)
            (root / instruction_ref).write_bytes(instruction)
            stale_receipt_ref = "phases/001-research/receipt.json"
            amendment = {
                "schema_version": "agent-workflow.amendment.vnext.v1",
                "amendment_kind": "instruction",
                "workflow_id": workflow["workflow_id"],
                "amendment_id": "stale-boundary",
                "previous_authority_revision": 1,
                "authority_revision": 2,
                "applies_after_ref": stale_receipt_ref,
                "applies_after_sha256": digest((root / stale_receipt_ref).read_bytes()),
                "user_instruction_ref": instruction_ref,
                "user_instruction_sha256": digest(instruction),
                "reason": "A stale boundary must not be accepted.",
            }
            with self.assertRaisesRegex(RecoveryError, "latest terminal boundary"):
                seal_amendment(root, workflow, amendment)

    def test_amendment_rejects_forged_nested_process_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workflow, initial = self.initial(root)
            self.terminal_result(root, initial, completed=True)
            instruction_ref = "amendments/instruction-0002.md"
            instruction = b"Apply only after a proven terminal boundary.\n"
            (root / "amendments").mkdir(exist_ok=True)
            (root / instruction_ref).write_bytes(instruction)
            receipt_ref = f"phases/{initial['phase_id']}/receipt.json"
            amendment = {
                "schema_version": "agent-workflow.amendment.vnext.v1",
                "amendment_kind": "instruction",
                "workflow_id": workflow["workflow_id"],
                "amendment_id": "forged-boundary",
                "previous_authority_revision": 1,
                "authority_revision": 2,
                "applies_after_ref": receipt_ref,
                "applies_after_sha256": digest((root / receipt_ref).read_bytes()),
                "user_instruction_ref": instruction_ref,
                "user_instruction_sha256": digest(instruction),
                "reason": "Must not trust a forged terminal file.",
            }
            create_once_json(root, "runtime/watchdogs/001-research/task/terminal.json", {})
            create_once_json(
                root,
                "runtime/processes/001-research/task.json",
                {
                    "task_id": "task",
                    "terminal_ref": "runtime/watchdogs/001-research/task/terminal.json",
                },
            )
            with self.assertRaisesRegex(RecoveryError, "reconcile proof"):
                seal_amendment(root, workflow, amendment)

    def test_final_seal_fences_new_amendment(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workflow, _initial = self.initial(root)
            create_once_json(root, "final.json", {"status": "complete"})
            with self.assertRaisesRegex(RecoveryError, "final"):
                seal_amendment(root, workflow, {})
            with self.assertRaisesRegex(RecoveryError, "final"):
                seal_resume_brief(root, workflow, "generation-002")

    def test_resume_brief_is_compact_create_once_and_rejects_drift(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workflow, initial = self.initial(root)
            self.terminal_result(root, initial)
            create_once_json(
                root,
                "runtime/processes/still-active.json",
                {"task_id": "still-active", "terminal_ref": "runtime/missing-terminal.json"},
            )
            with self.assertRaisesRegex(RecoveryError, "reconcile"):
                seal_resume_brief(root, workflow, "generation-002")
            (root / "runtime/processes/still-active.json").unlink()
            orphan = deepcopy(initial)
            orphan["phase_id"] = "003-loser"
            create_once_json(root, "phases/003-loser/plan.json", orphan)
            first = seal_resume_brief(root, workflow, "generation-002")
            second = seal_resume_brief(root, workflow, "generation-002")
            self.assertEqual(first.read_bytes(), second.read_bytes())
            brief = json.loads(first.read_text())
            self.assertEqual(set(brief), {
                "schema_version",
                "workflow_id",
                "generation_id",
                "authority_revision",
                "predecessor_sha256",
                "criterion_revisions",
                "terminal_phases",
                "unfinished_phases",
                "recovery_claimed_lineages",
                "displaced_source_edits",
            })
            self.assertNotIn("003-loser", brief["unfinished_phases"])
            first.write_text("{}\n")
            with self.assertRaisesRegex(RecoveryError, "drifted"):
                seal_resume_brief(root, workflow, "generation-002")

    def test_resume_rejects_winning_plan_crashed_before_watchdog_launch(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workflow, _initial = self.initial(root)
            with self.assertRaisesRegex(RecoveryError, "unfinished committed phase"):
                seal_resume_brief(root, workflow, "generation-002")

    def test_reconcile_exclusion_requires_exact_claim_plan_and_sole_unfinished_phase(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workflow, initial = self.initial(root)
            claim_path = next((root / "generations/claims").glob("*.json"))
            original_claim = claim_path.read_bytes()

            forged_claim = json.loads(original_claim)
            forged_claim["workflow_id"] = "forged-workflow"
            claim_path.write_bytes(canonical(forged_claim))
            with self.assertRaisesRegex(RecoveryError, "exact plan authority"):
                prepare_phase_authority(root, workflow, initial, reconciling=True)

            claim_path.write_bytes(original_claim)
            drifted_plan = deepcopy(initial)
            drifted_plan["phase_budget_seconds"] += 1
            drifted_payload = canonical(drifted_plan)
            (root / "phases/001-research/plan.json").write_bytes(drifted_payload)
            paired_claim = json.loads(original_claim)
            paired_claim["plan_sha256"] = digest(drifted_payload)
            claim_path.write_bytes(canonical(paired_claim))
            with self.assertRaisesRegex(RecoveryError, "plan drifted"):
                prepare_phase_authority(root, workflow, initial, reconciling=True)

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workflow, initial = self.initial(root)
            self.terminal_result(root, initial, completed=True)
            current = deepcopy(initial)
            current["phase_id"] = "002-current"
            current["generation_id"] = "generation-002"
            current["caused_by"] = [initial["phase_id"]]
            current["predecessor_sha256"] = causal_predecessor_sha256(
                root, current["caused_by"]
            )
            create_once_json(root, "phases/002-current/plan.json", current)
            self.commit_plan_claim(root, workflow, current)

            unrelated = deepcopy(initial)
            unrelated["phase_id"] = "009-unrelated"
            unrelated["generation_id"] = "generation-009"
            unrelated["predecessor_sha256"] = "sha256:" + "9" * 64
            create_once_json(root, "phases/009-unrelated/plan.json", unrelated)
            self.commit_plan_claim(root, workflow, unrelated)
            with self.assertRaisesRegex(RecoveryError, "unfinished committed phase"):
                prepare_phase_authority(root, workflow, current, reconciling=True)

    def test_resume_rejects_forged_terminal_file_without_reconcile_proof(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workflow, initial = self.initial(root)
            self.terminal_result(root, initial)
            create_once_json(root, "runtime/watchdogs/001-research/task/terminal.json", {})
            create_once_json(
                root,
                "runtime/processes/001-research/task.json",
                {
                    "task_id": "task",
                    "terminal_ref": "runtime/watchdogs/001-research/task/terminal.json",
                },
            )
            with self.assertRaisesRegex(RecoveryError, "reconcile proof"):
                seal_resume_brief(root, workflow, "generation-002")

    def test_resume_revalidates_retained_external_edit_digest_before_sealing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workflow, initial = self.initial(root)
            self.terminal_result(root, initial)
            retained = root / "runtime/integration-staging/001-research/next-anchor"
            retained.mkdir(parents=True)
            (retained / "external.txt").write_text("preserved\n")
            evidence = {
                "schema_version": "agent-workflow.displaced-anchor.vnext.v1",
                "phase_id": "001-research",
                "anchor": "src",
                "reason": "post_swap_shared_edit",
                "displaced_state": "retained_tree",
                "staging_ref": retained.relative_to(root).as_posix(),
                "staging_sha256": source_workspace._tree_digest_path(retained),
                "cleanup_allowed": False,
            }
            create_once_json(
                root,
                "runtime/source-write/001-research/displaced-anchor.json",
                evidence,
            )
            (retained / "external.txt").write_text("drifted\n")
            with self.assertRaisesRegex(RecoveryError, "displaced source edit"):
                seal_resume_brief(root, workflow, "generation-002")


if __name__ == "__main__":
    unittest.main(verbosity=2)
