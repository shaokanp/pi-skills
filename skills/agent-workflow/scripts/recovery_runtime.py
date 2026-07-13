#!/usr/bin/env python3
"""Bounded lineage, amendment, expansion, and resume authority for vNext.

This module does not choose product semantics.  It only proves that a proposed
Phase is causally reachable, within the one workflow expansion ceiling, and
cannot reset an original lineage's single autonomous recovery budget.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from artifact_store import (
    ArtifactError,
    create_once_json,
    shared_authority_transaction,
)
from phase_protocol import ProtocolError, validate_contract, validate_sidecar
from process_supervisor import SupervisorFailure, reconcile as reconcile_supervisors
from source_workspace import _tree_digest_path


class RecoveryError(RuntimeError):
    """Raised when recovery or amendment authority cannot be proven."""


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _safe_id(value: str, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789._-" for character in value)
        or value[0] not in "abcdefghijklmnopqrstuvwxyz0123456789"
    ):
        raise RecoveryError(f"{label} is invalid")
    return value


def _load_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink() or not path.is_file():
        raise RecoveryError(f"{label} is missing or unsafe")
    payload = path.read_bytes()
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecoveryError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise RecoveryError(f"{label} must be an object")
    return value, payload


def _create_or_verify(root: Path, relative: str, value: dict[str, Any]) -> Path:
    expected = _canonical(value)
    path = root / relative
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != expected:
            raise RecoveryError(f"immutable authority artifact drifted: {relative}")
        return path
    try:
        return create_once_json(root, relative, value)
    except ArtifactError as exc:
        raise RecoveryError(str(exc)) from exc


def scope_sha256(task: dict[str, Any]) -> str:
    """Stable scope continuity proof; packet/name changes do not mint a new budget."""

    scope = {
        "criterion_id": task["criterion_id"],
        "role": task["role"],
        "work_mode": task["work_mode"],
        "write_roots": sorted(task["write_roots"]),
    }
    return _digest(_canonical(scope))


def causal_predecessor_sha256(root: Path, caused_by: list[str]) -> str:
    """Return the exact immediately prior receipt after validating every named cause."""

    if not caused_by or len(caused_by) != len(set(caused_by)):
        raise RecoveryError("caused_by must contain unique terminal phase ids")
    receipts: list[dict[str, str]] = []
    for phase_id in caused_by:
        _safe_id(phase_id, "caused_by phase id")
        relative = f"phases/{phase_id}/receipt.json"
        receipt, payload = _load_json(root / relative, f"causal receipt {phase_id}")
        try:
            validate_contract("phase-receipt", receipt)
        except ProtocolError as exc:
            raise RecoveryError(f"causal receipt {phase_id} is invalid") from exc
        receipts.append({"phase_id": phase_id, "receipt_ref": relative, "receipt_sha256": _digest(payload)})
    return receipts[-1]["receipt_sha256"]


def _validate_amendment_evidence(
    root: Path,
    amendment: dict[str, Any],
    criteria: dict[str, int],
) -> tuple[str | None, int | None]:
    instruction_path = root / amendment["user_instruction_ref"]
    if (
        instruction_path.is_symlink()
        or not instruction_path.is_file()
        or _digest(instruction_path.read_bytes()) != amendment["user_instruction_sha256"]
    ):
        raise RecoveryError("amendment user instruction evidence drifted")
    if amendment["amendment_kind"] == "instruction":
        receipt, receipt_payload = _load_json(
            root / amendment["applies_after_ref"],
            "instruction amendment boundary receipt",
        )
        try:
            validate_contract("phase-receipt", receipt)
        except ProtocolError as exc:
            raise RecoveryError("instruction amendment boundary receipt is invalid") from exc
        if _digest(receipt_payload) != amendment["applies_after_sha256"]:
            raise RecoveryError("instruction amendment boundary receipt drifted")
        return None, None

    criterion = amendment["criterion_id"]
    if criterion not in criteria or amendment["from_revision"] != criteria[criterion]:
        raise RecoveryError("criterion amendment revision chain is discontinuous")
    blocked_ref = amendment["blocked_result_ref"]
    blocked, blocked_payload = _load_json(root / blocked_ref, "amendment blocked result")
    try:
        validate_contract("task-result", blocked)
    except ProtocolError as exc:
        raise RecoveryError("amendment blocked result is invalid") from exc
    if blocked["status"] == "completed" or _digest(blocked_payload) != amendment["blocked_result_sha256"]:
        raise RecoveryError("amendment must bind an exact non-completed result")
    receipt_ref = f"phases/{blocked['phase_id']}/receipt.json"
    receipt, _ = _load_json(root / receipt_ref, "amendment blocked phase receipt")
    try:
        validate_contract("phase-receipt", receipt)
    except ProtocolError as exc:
        raise RecoveryError("amendment blocked phase receipt is invalid") from exc
    if (
        blocked_ref not in receipt["task_result_refs"]
        or receipt["task_result_sha256"].get(blocked_ref) != amendment["blocked_result_sha256"]
    ):
        raise RecoveryError("amendment blocked result is not authoritative in its phase receipt")
    return criterion, amendment["to_revision"]


def _amendment_state(root: Path, workflow: dict[str, Any]) -> tuple[int, dict[str, int]]:
    current_authority = workflow["authority"]["revision"]
    criteria = {item["id"]: 1 for item in workflow["success_criteria"]}
    amendments_root = root / "amendments" / "criteria"
    paths = sorted(amendments_root.glob("*.json")) if amendments_root.is_dir() else []
    amendments: list[dict[str, Any]] = []
    for path in paths:
        amendment, _ = _load_json(path, "authority amendment")
        try:
            validate_sidecar("amendment", amendment)
        except ProtocolError as exc:
            raise RecoveryError("authority amendment contract is invalid") from exc
        amendments.append(amendment)
    amendments.sort(key=lambda item: item["authority_revision"])
    for amendment in amendments:
        if amendment["workflow_id"] != workflow["workflow_id"]:
            raise RecoveryError("authority amendment belongs to another workflow")
        if amendment["previous_authority_revision"] != current_authority:
            raise RecoveryError("authority amendment chain is discontinuous")
        if amendment["authority_revision"] != current_authority + 1:
            raise RecoveryError("authority amendment must advance by exactly one")
        criterion, revision = _validate_amendment_evidence(root, amendment, criteria)
        if criterion is not None and revision is not None:
            criteria[criterion] = revision
        current_authority = amendment["authority_revision"]
    return current_authority, criteria


def current_authority_revision(root: Path, workflow: dict[str, Any]) -> int:
    return _amendment_state(Path(root), workflow)[0]


@shared_authority_transaction
def seal_amendment(root: Path, workflow: dict[str, Any], amendment: dict[str, Any]) -> Path:
    """Seal one user-evidenced authority amendment for the next Phase boundary."""

    if (Path(root) / "final.json").exists() or (Path(root) / "final.json").is_symlink():
        raise RecoveryError("final seal rejects a later amendment")
    try:
        validate_contract("workflow", workflow)
        validate_sidecar("amendment", amendment)
    except ProtocolError as exc:
        raise RecoveryError(str(exc)) from exc
    current_authority, criteria = _amendment_state(root, workflow)
    for plan_ref in _committed_plan_refs(root, workflow):
        phase_id = PurePosixPath(plan_ref).parts[1]
        if not (root / "phases" / phase_id / "receipt.json").is_file():
            raise RecoveryError("normal amendment can only seal at a terminal phase boundary")
    try:
        reconcile_summary = reconcile_supervisors(root, grace_seconds=0.0)
    except SupervisorFailure as exc:
        raise RecoveryError("normal amendment reconcile proof is invalid") from exc
    if reconcile_summary["active"]:
        raise RecoveryError("normal amendment cannot overtake an active attempt")
    if amendment["workflow_id"] != workflow["workflow_id"]:
        raise RecoveryError("amendment belongs to another workflow")
    if amendment["previous_authority_revision"] != current_authority:
        raise RecoveryError("amendment previous authority revision is stale")
    if amendment["authority_revision"] != current_authority + 1:
        raise RecoveryError("amendment must advance authority by exactly one")
    _validate_amendment_evidence(root, amendment, criteria)
    projection = build_resume_brief(root, workflow, "amendment-boundary")
    terminal_phases = projection["terminal_phases"]
    if not terminal_phases:
        raise RecoveryError("normal amendment requires a terminal boundary")
    latest = terminal_phases[-1]
    if amendment["amendment_kind"] == "instruction":
        if (
            amendment["applies_after_ref"] != latest["receipt_ref"]
            or amendment["applies_after_sha256"] != latest["receipt_sha256"]
        ):
            raise RecoveryError("instruction amendment must bind the latest terminal boundary")
    else:
        blocked, _ = _load_json(
            root / amendment["blocked_result_ref"],
            "criterion amendment blocked result",
        )
        if latest["receipt_ref"] != f"phases/{blocked['phase_id']}/receipt.json":
            raise RecoveryError(
                "criterion amendment blocked result must belong to the latest terminal boundary"
            )
    relative = f"amendments/criteria/{amendment['authority_revision']:04d}-{amendment['amendment_id']}.json"
    path = _create_or_verify(root, relative, amendment)
    _amendment_state(root, workflow)
    return path


@dataclass(frozen=True)
class PhaseAuthority:
    current_authority_revision: int
    claim_values: tuple[tuple[str, dict[str, Any]], ...]
    causal_predecessor_sha256: str
    additional_phase_index: int


def _lineage_origins(root: Path) -> dict[str, tuple[str, dict[str, Any], bytes]]:
    origins: dict[str, tuple[str, dict[str, Any], bytes]] = {}
    lineages_root = root / "lineages"
    paths = sorted(lineages_root.glob("*/origin.json")) if lineages_root.is_dir() else []
    for path in paths:
        value, payload = _load_json(path, "lineage origin")
        try:
            validate_sidecar("lineage-claim", value)
        except ProtocolError as exc:
            raise RecoveryError("lineage origin contract is invalid") from exc
        if value["claim_kind"] != "origin":
            raise RecoveryError("lineage origin path contains a non-origin claim")
        lineage_id = value["lineage_id"]
        relative = path.relative_to(root).as_posix()
        if path.parent.name != lineage_id or lineage_id in origins:
            raise RecoveryError("lineage origin namespace is inconsistent")
        origins[lineage_id] = (relative, value, payload)
    return origins


def _results_by_lineage(root: Path) -> dict[str, list[tuple[str, dict[str, Any], bytes]]]:
    result: dict[str, list[tuple[str, dict[str, Any], bytes]]] = {}
    for path in sorted(root.glob("phases/*/tasks/*/result.json")):
        value, payload = _load_json(path, "task result")
        try:
            validate_contract("task-result", value)
        except ProtocolError as exc:
            raise RecoveryError("task result contract is invalid") from exc
        result.setdefault(value["lineage_id"], []).append(
            (path.relative_to(root).as_posix(), value, payload)
        )
    return result


def _expected_generation_claim(
    workflow: dict[str, Any],
    plan: dict[str, Any],
    plan_payload: bytes,
) -> tuple[str, dict[str, Any]]:
    contention = {
        "predecessor_sha256": plan["predecessor_sha256"],
        "authority_revision": plan["authority_revision"],
    }
    contention_key = _digest(_canonical(contention))
    claim = {
        "schema_version": "agent-workflow.generation-claim.vnext.v1",
        "workflow_id": workflow["workflow_id"],
        "generation_id": plan["generation_id"],
        "phase_id": plan["phase_id"],
        "predecessor_sha256": plan["predecessor_sha256"],
        "authority_revision": plan["authority_revision"],
        "plan_sha256": _digest(plan_payload),
        "contention_key": contention_key,
    }
    return (
        f"generations/claims/{contention_key.removeprefix('sha256:')}.json",
        claim,
    )


def _committed_plan_refs(root: Path, workflow: dict[str, Any]) -> set[str]:
    refs: set[str] = set()
    claims_root = root / "generations" / "claims"
    paths = sorted(claims_root.glob("*.json")) if claims_root.is_dir() else []
    for path in paths:
        claim, claim_payload = _load_json(path, "generation claim")
        try:
            validate_sidecar("generation-claim", claim)
        except ProtocolError as exc:
            raise RecoveryError("generation claim contract is invalid") from exc
        plan_ref = f"phases/{claim['phase_id']}/plan.json"
        plan_path = root / plan_ref
        if plan_path.is_symlink() or not plan_path.is_file():
            raise RecoveryError("generation claim references a missing phase plan")
        plan, plan_payload = _load_json(plan_path, "generation claim phase plan")
        try:
            validate_contract("phase-plan", plan)
        except ProtocolError as exc:
            raise RecoveryError("generation claim phase plan contract is invalid") from exc
        if plan_payload != _canonical(plan):
            raise RecoveryError("generation claim phase plan bytes are not canonical")
        expected_ref, expected_claim = _expected_generation_claim(
            workflow, plan, plan_payload
        )
        actual_ref = path.relative_to(root).as_posix()
        if (
            actual_ref != expected_ref
            or claim != expected_claim
            or claim_payload != _canonical(expected_claim)
        ):
            raise RecoveryError("generation claim does not bind the exact plan authority")
        refs.add(plan_ref)
    return refs


def prepare_phase_authority(
    root: Path,
    workflow: dict[str, Any],
    plan: dict[str, Any],
    *,
    reconciling: bool = False,
) -> PhaseAuthority:
    """Validate one proposed dynamic Phase without claiming execution authority yet."""

    root = Path(root)
    try:
        validate_contract("workflow", workflow)
        validate_contract("phase-plan", plan)
    except ProtocolError as exc:
        raise RecoveryError(str(exc)) from exc
    current_authority, criterion_revisions = _amendment_state(root, workflow)
    if plan["authority_revision"] != current_authority:
        raise RecoveryError("phase authority revision does not match current amendment authority")
    for task in plan["tasks"]:
        if criterion_revisions.get(task["criterion_id"]) != task["criterion_revision"]:
            raise RecoveryError("phase task criterion revision is not current")

    current_plan_ref = f"phases/{plan['phase_id']}/plan.json"
    committed_plan_refs = _committed_plan_refs(root, workflow)
    if reconciling and current_plan_ref not in committed_plan_refs:
        raise RecoveryError("reconcile phase requires its exact committed generation claim")
    if reconciling and (root / current_plan_ref).read_bytes() != _canonical(plan):
        raise RecoveryError("reconcile phase plan drifted from its exact committed authority")
    other_plans = sorted(ref for ref in committed_plan_refs if ref != current_plan_ref)
    terminal_receipts = sorted(root.glob("phases/*/receipt.json"))
    if not terminal_receipts:
        if other_plans:
            raise RecoveryError("an unterminated predecessor phase blocks a new phase")
        if plan["caused_by"] or plan["predecessor_sha256"] != workflow["baseline_sha256"]:
            raise RecoveryError("initial phase must be caused by the workflow baseline only")
        if plan["generation_id"] != "generation-001" or not plan["phase_id"].startswith("001-"):
            raise RecoveryError("initial phase must use generation-001 and an 001-* phase id")
        predecessor = workflow["baseline_sha256"]
        additional_index = 0
    else:
        if not plan["caused_by"]:
            raise RecoveryError("additional phase must name terminal causal phases")
        current_projection = build_resume_brief(
            root,
            workflow,
            plan["generation_id"],
            reconciling_phase_id=plan["phase_id"] if reconciling else None,
        )
        terminal_phases = current_projection["terminal_phases"]
        if not terminal_phases or plan["caused_by"][-1] != terminal_phases[-1]["phase_id"]:
            raise RecoveryError("additional phase must descend from the latest terminal phase")
        predecessor = causal_predecessor_sha256(root, plan["caused_by"])
        if (
            plan["predecessor_sha256"] != predecessor
            or plan["predecessor_sha256"] != current_projection["predecessor_sha256"]
        ):
            raise RecoveryError("additional phase predecessor does not match its causal receipts")
        immediate_receipt, _ = _load_json(
            root / "phases" / plan["caused_by"][-1] / "receipt.json",
            "immediately prior phase receipt",
        )
        if plan["generation_id"] != immediate_receipt["generation_id"]:
            resume_path = root / "generations" / plan["generation_id"] / "resume-brief.json"
            expected_resume = current_projection
            if (
                resume_path.is_symlink()
                or not resume_path.is_file()
                or resume_path.read_bytes() != _canonical(expected_resume)
                or expected_resume["predecessor_sha256"] != plan["predecessor_sha256"]
            ):
                raise RecoveryError("new generation requires a current create-once resume brief")
        additional_index = len(other_plans)
        if additional_index > workflow["limits"]["max_additional_phases"]:
            raise RecoveryError("workflow max_additional_phases is exhausted")

    origins = _lineage_origins(root)
    results = _results_by_lineage(root)
    claims: list[tuple[str, dict[str, Any]]] = []
    for task in plan["tasks"]:
        lineage_id = task["lineage_id"]
        scope = scope_sha256(task)
        if lineage_id not in origins:
            for _, prior_origin, _ in origins.values():
                prior_results = results.get(prior_origin["lineage_id"], [])
                if (
                    prior_origin["criterion_id"] == task["criterion_id"]
                    and prior_origin["criterion_revision"] == task["criterion_revision"]
                ):
                    if any(item[1]["status"] != "completed" for item in prior_results):
                        raise RecoveryError("new lineage would bypass a failed criterion lineage")
                    if (
                        prior_origin["role"] == task["role"]
                        and any(item[1]["status"] == "completed" for item in prior_results)
                    ):
                        raise RecoveryError("new lineage would rerun successful same-role work")
            claim = {
                "schema_version": "agent-workflow.lineage-claim.vnext.v1",
                "claim_kind": "origin",
                "workflow_id": workflow["workflow_id"],
                "lineage_id": lineage_id,
                "criterion_id": task["criterion_id"],
                "criterion_revision": task["criterion_revision"],
                "role": task["role"],
                "scope_sha256": scope,
                "origin_phase_id": plan["phase_id"],
                "origin_task_id": task["task_id"],
            }
            validate_sidecar("lineage-claim", claim)
            claims.append((f"lineages/{lineage_id}/origin.json", claim))
            continue

        origin_ref, origin, origin_payload = origins[lineage_id]
        if origin["criterion_id"] != task["criterion_id"]:
            raise RecoveryError("lineage criterion identity drifted")
        if origin["criterion_revision"] != task["criterion_revision"]:
            raise RecoveryError("criterion amendment must start a new lineage")
        prior_results = [
            item for item in results.get(lineage_id, [])
            if item[1]["phase_id"] != plan["phase_id"]
        ]
        if not prior_results:
            if (
                reconciling
                and origin["origin_phase_id"] == plan["phase_id"]
                and origin["origin_task_id"] == task["task_id"]
                and origin["scope_sha256"] == scope
            ):
                continue
            raise RecoveryError("lineage is already claimed by an unfinished task")
        if any(value["status"] == "completed" for _, value, _ in prior_results):
            raise RecoveryError("successful lineage cannot be rerun")
        recovery_ref = f"lineages/{lineage_id}/recovery.json"
        failed_ref, failed, failed_payload = prior_results[-1]
        if failed["status"] == "cancelled":
            raise RecoveryError("cancelled lineage requires a new user authority boundary")
        if failed_ref not in task["input_refs"]:
            raise RecoveryError("recovery task must consume the failed result it repairs")
        causal_ref = f"phases/{plan['caused_by'][-1]}/receipt.json"
        if (
            causal_ref not in task["input_refs"]
            or task["input_sha256"].get(causal_ref)
            != _digest((root / causal_ref).read_bytes())
        ):
            raise RecoveryError("recovery task must consume the exact causal receipt")
        automatic = (
            failed["status"] == "failed"
            and failed["terminal_reason"] == "runner_error"
            and not failed["changed_paths"]
        )
        claim = {
            "schema_version": "agent-workflow.lineage-claim.vnext.v1",
            "claim_kind": "recovery",
            "workflow_id": workflow["workflow_id"],
            "lineage_id": lineage_id,
            "origin_ref": origin_ref,
            "origin_sha256": _digest(origin_payload),
            "failed_result_ref": failed_ref,
            "failed_result_sha256": _digest(failed_payload),
            "recovery_phase_id": plan["phase_id"],
            "recovery_task_id": task["task_id"],
            "recovery_scope_sha256": scope,
            "criterion_id": task["criterion_id"],
            "criterion_revision": task["criterion_revision"],
            "authority_revision": plan["authority_revision"],
            "recovery_kind": "automatic_infra_retry" if automatic else "evidence_aware_repair",
        }
        validate_sidecar("lineage-claim", claim)
        recovery_path = root / recovery_ref
        if recovery_path.exists() or recovery_path.is_symlink():
            if (
                reconciling
                and not recovery_path.is_symlink()
                and recovery_path.is_file()
                and recovery_path.read_bytes() == _canonical(claim)
            ):
                continue
            raise RecoveryError("lineage autonomous recovery is already exhausted")
        claims.append((recovery_ref, claim))
    return PhaseAuthority(current_authority, tuple(claims), predecessor, additional_index)


def commit_phase_authority(root: Path, authority: PhaseAuthority) -> list[str]:
    """Publish lineage origin/recovery claims after the generation claim wins."""

    refs: list[str] = []
    for relative, value in authority.claim_values:
        _create_or_verify(Path(root), relative, value)
        refs.append(relative)
    return refs


def build_resume_brief(
    root: Path,
    workflow: dict[str, Any],
    generation_id: str,
    *,
    reconciling_phase_id: str | None = None,
) -> dict[str, Any]:
    """Build one compact, deterministic brief from authoritative artifacts only."""

    generation_id = _safe_id(generation_id, "resume generation id")
    if reconciling_phase_id is not None:
        reconciling_phase_id = _safe_id(reconciling_phase_id, "reconciling phase id")
        current_plan_ref = f"phases/{reconciling_phase_id}/plan.json"
        if current_plan_ref not in _committed_plan_refs(root, workflow):
            raise RecoveryError("reconcile phase requires its exact committed generation claim")
    try:
        reconcile_summary = reconcile_supervisors(root, grace_seconds=0.0)
    except SupervisorFailure as exc:
        raise RecoveryError("deterministic reconcile proof is invalid") from exc
    if reconcile_summary["active"]:
        raise RecoveryError("deterministic reconcile must terminalize active attempts before resume")
    current_authority, criteria = _amendment_state(root, workflow)
    unordered: list[tuple[dict[str, Any], bytes, dict[str, Any], str]] = []
    for path in sorted(root.glob("phases/*/receipt.json")):
        receipt, payload = _load_json(path, "resume phase receipt")
        validate_contract("phase-receipt", receipt)
        plan_path = path.parent / "plan.json"
        plan, plan_payload = _load_json(plan_path, "resume phase plan")
        validate_contract("phase-plan", plan)
        if (
            plan["phase_id"] != receipt["phase_id"]
            or _digest(plan_payload) != receipt["plan_sha256"]
            or plan["predecessor_sha256"] != receipt["predecessor_sha256"]
        ):
            raise RecoveryError("resume phase plan and receipt binding drifted")
        unordered.append((receipt, payload, plan, path.relative_to(root).as_posix()))
    receipts: list[dict[str, Any]] = []
    predecessor = workflow["baseline_sha256"]
    remaining = list(unordered)
    immediately_prior_phase: str | None = None
    while remaining:
        candidates = [item for item in remaining if item[2]["predecessor_sha256"] == predecessor]
        if len(candidates) != 1:
            raise RecoveryError("terminal phase receipts do not form one authoritative chain")
        receipt, payload, plan, relative = candidates[0]
        if immediately_prior_phase is not None and (
            not plan["caused_by"] or plan["caused_by"][-1] != immediately_prior_phase
        ):
            raise RecoveryError("terminal phase cause chain is discontinuous")
        receipts.append(
            {
                "phase_id": receipt["phase_id"],
                "generation_id": receipt["generation_id"],
                "status": receipt["status"],
                "receipt_ref": relative,
                "receipt_sha256": _digest(payload),
            }
        )
        predecessor = _digest(payload)
        immediately_prior_phase = receipt["phase_id"]
        remaining.remove(candidates[0])
    unfinished = sorted(
        PurePosixPath(plan_ref).parts[1]
        for plan_ref in _committed_plan_refs(root, workflow)
        if PurePosixPath(plan_ref).parts[1] != reconciling_phase_id
        if not (root / "phases" / PurePosixPath(plan_ref).parts[1] / "receipt.json").is_file()
    )
    if unfinished:
        raise RecoveryError("unfinished committed phase requires deterministic reconcile before resume")
    recoveries = sorted(
        path.parent.name
        for path in (root / "lineages").glob("*/recovery.json")
    ) if (root / "lineages").is_dir() else []
    displaced_source_edits: list[dict[str, Any]] = []
    for path in sorted(root.glob("runtime/source-write/*/displaced-anchor.json")):
        value, payload = _load_json(path, "displaced source edit")
        expected = {
            "schema_version",
            "phase_id",
            "anchor",
            "reason",
            "displaced_state",
            "staging_ref",
            "staging_sha256",
            "cleanup_allowed",
        }
        if (
            set(value) != expected
            or value["schema_version"] != "agent-workflow.displaced-anchor.vnext.v1"
            or value["cleanup_allowed"] is not False
        ):
            raise RecoveryError("displaced source edit evidence is invalid")
        state = value["displaced_state"]
        if state == "retained_tree":
            staging_ref = value["staging_ref"]
            if not isinstance(staging_ref, str):
                raise RecoveryError("displaced source edit retained ref is invalid")
            staging_parts = PurePosixPath(staging_ref).parts
            if staging_parts[:2] != ("runtime", "integration-staging"):
                raise RecoveryError("displaced source edit retained ref escapes staging")
            staging_path = root / staging_ref
            if (
                staging_path.is_symlink()
                or not staging_path.exists()
                or _tree_digest_path(staging_path) != value["staging_sha256"]
            ):
                raise RecoveryError("displaced source edit retained tree drifted")
        elif state == "missing":
            if (
                value["staging_ref"] is not None
                or value["staging_sha256"] != _digest(_canonical({}))
            ):
                raise RecoveryError("displaced source edit missing tombstone is invalid")
        else:
            raise RecoveryError("displaced source edit state is invalid")
        displaced_source_edits.append(
            {
                "phase_id": value["phase_id"],
                "reason": value["reason"],
                "evidence_ref": path.relative_to(root).as_posix(),
                "evidence_sha256": _digest(payload),
                "recovery_ref": value["staging_ref"],
            }
        )
    return {
        "schema_version": "agent-workflow.resume-brief.vnext.v1",
        "workflow_id": workflow["workflow_id"],
        "generation_id": generation_id,
        "authority_revision": current_authority,
        "predecessor_sha256": predecessor,
        "criterion_revisions": criteria,
        "terminal_phases": receipts,
        "unfinished_phases": unfinished,
        "recovery_claimed_lineages": recoveries,
        "displaced_source_edits": displaced_source_edits,
    }


@shared_authority_transaction
def seal_resume_brief(root: Path, workflow: dict[str, Any], generation_id: str) -> Path:
    if (Path(root) / "final.json").exists() or (Path(root) / "final.json").is_symlink():
        raise RecoveryError("final seal rejects a later resume brief")
    brief = build_resume_brief(Path(root), workflow, generation_id)
    return _create_or_verify(
        Path(root),
        f"generations/{generation_id}/resume-brief.json",
        brief,
    )
