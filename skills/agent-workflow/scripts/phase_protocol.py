#!/usr/bin/env python3
"""Small, fail-closed protocol core for Agent Workflow vNext artifacts."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
import stat
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from baseline_gate import (
    BaselineError,
    repository_evidence,
    verify_baseline,
    verify_candidate_against_parent,
)


LIFECYCLE_SCHEMAS = {
    "workflow": "agent-workflow.workflow.vnext.v1",
    "phase-plan": "agent-workflow.phase-plan.v1",
    "task-result": "agent-workflow.task-result.v1",
    "phase-receipt": "agent-workflow.phase-receipt.v1",
    "final": "agent-workflow.final.v1",
}
SCOPED_SIDECAR_SCHEMAS = {
    "events": "agent-workflow.events.vnext.v1",
    "amendment": "agent-workflow.amendment.vnext.v1",
    "generation-claim": "agent-workflow.generation-claim.vnext.v1",
    "lineage-claim": "agent-workflow.lineage-claim.vnext.v1",
    "accounting": "agent-workflow.accounting.vnext.v1",
}

TASK_TERMINAL_STATUSES = {
    "completed",
    "failed",
    "timed_out",
    "cancelled",
    "not_started_deadline",
    "not_started_interrupted",
    "concurrent_edit_conflict",
    "route_attestation_failed",
    "escaped_process_detected",
}
PHASE_TERMINAL_STATUSES = {"completed", "completed_with_failures", "failed", "cancelled", "blocked"}
FINAL_STATUSES = {"complete", "blocked", "failed", "cancelled"}
P2_RESOLUTIONS = {"fixed", "accepted_with_rationale", "deferred_with_owner_gate"}
CAPABILITY_NAMES = {
    "blocking_wait",
    "read_only_containment",
    "route_attestation",
    "sandbox_isolation",
    "cancel_reap",
    "raw_session_audit",
    "accounting_evidence",
    "generation_fence",
}
REASONING_EFFORTS = {"low", "medium", "high", "xhigh"}
_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
_MODEL_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{1,127}")
_RESERVED_WRITE_ROOTS = {".git", ".workflow"}


class ProtocolError(ValueError):
    """Raised when a vNext lifecycle contract fails closed."""


def _canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _payload_digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolError(f"{label} must be an object")
    return value


def _exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    missing = sorted(expected - value.keys())
    extra = sorted(value.keys() - expected)
    if missing:
        raise ProtocolError(f"{label} contract missing keys: {', '.join(missing)}")
    if extra:
        raise ProtocolError(f"{label} contract has unknown keys: {', '.join(extra)}")


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError(f"{label} must be a non-empty string")
    return value


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ProtocolError(f"{label} must be an integer >= {minimum}")
    return value


def _rfc3339(value: Any, label: str) -> str:
    text = _text(value, label)
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ProtocolError(f"{label} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise ProtocolError(f"{label} must include a timezone")
    return text


def _parsed_rfc3339(value: Any, label: str) -> datetime:
    text = _rfc3339(value, label)
    return datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)


def _relative_path(value: Any, label: str) -> str:
    text = _text(value, label)
    path = PurePosixPath(text)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ProtocolError(f"{label} must be a safe relative path")
    return text


def _repository_read_root(value: Any, label: str) -> str:
    """Accept the repository root without making `.` a valid artifact path."""

    if value == ".":
        return "."
    return _relative_path(value, label)


def _sha256(value: Any, label: str) -> str:
    text = _text(value, label)
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", text):
        raise ProtocolError(f"{label} must be a lowercase sha256 digest")
    return text


def _slug(value: Any, label: str) -> str:
    text = _text(value, label)
    if not _ID_PATTERN.fullmatch(text):
        raise ProtocolError(f"{label} must be a lowercase path-safe id")
    return text


def _model(value: Any, label: str) -> str:
    text = _text(value, label)
    if not _MODEL_PATTERN.fullmatch(text):
        raise ProtocolError(f"{label} must be a qualified model id")
    return text


def _artifact_digest_map(value: Any, refs: list[str], label: str) -> dict[str, str]:
    mapping = _digest_map(value, label)
    if set(mapping) != set(refs):
        raise ProtocolError(f"{label} must bind every referenced artifact exactly once")
    return mapping


def _canonical_root(value: Any, label: str) -> str:
    root = _relative_path(value, label)
    normalized = PurePosixPath(root).as_posix()
    first = PurePosixPath(normalized).parts[0]
    if first in _RESERVED_WRITE_ROOTS:
        raise ProtocolError(f"{label} cannot target a reserved control root")
    return normalized


def _roots_overlap(left: str, right: str) -> bool:
    left_parts = PurePosixPath(left).parts
    right_parts = PurePosixPath(right).parts
    width = min(len(left_parts), len(right_parts))
    return left_parts[:width] == right_parts[:width]


def _validate_workflow(value: dict[str, Any]) -> None:
    _exact_keys(
        value,
        {
            "schema_version",
            "workflow_id",
            "created_at",
            "objective",
            "success_criteria",
            "authority",
            "routing",
            "limits",
            "runtime_bundle",
            "baseline_ref",
            "baseline_sha256",
            "admission",
        },
        "workflow",
    )
    _slug(value["workflow_id"], "workflow.workflow_id")
    _rfc3339(value["created_at"], "workflow.created_at")
    _text(value["objective"], "workflow.objective")
    criteria = value["success_criteria"]
    if not isinstance(criteria, list) or not criteria:
        raise ProtocolError("workflow.success_criteria must be a non-empty list")
    criterion_ids: set[str] = set()
    for index, raw in enumerate(criteria):
        item = _object(raw, f"workflow.success_criteria[{index}]")
        _exact_keys(item, {"id", "description"}, f"workflow.success_criteria[{index}]")
        criterion_id = _text(item["id"], f"workflow.success_criteria[{index}].id")
        if criterion_id in criterion_ids:
            raise ProtocolError("workflow.success_criteria ids must be unique")
        criterion_ids.add(criterion_id)
        _text(item["description"], f"workflow.success_criteria[{index}].description")

    authority = _object(value["authority"], "workflow.authority")
    _exact_keys(authority, {"revision", "external_actions"}, "workflow.authority")
    _integer(authority["revision"], "workflow.authority.revision", minimum=1)
    if authority["external_actions"] != "host_approval_required":
        raise ProtocolError("workflow.authority.external_actions must be host_approval_required")

    routing = _object(value["routing"], "workflow.routing")
    _exact_keys(
        routing,
        {"policy_version", "top_model", "worker_model", "reasoning_effort"},
        "workflow.routing",
    )
    _slug(routing["policy_version"], "workflow.routing.policy_version")
    top_model = _model(routing["top_model"], "workflow.routing.top_model")
    worker_model = _model(routing["worker_model"], "workflow.routing.worker_model")
    if top_model == worker_model:
        raise ProtocolError("workflow.routing top and worker models must be distinct")
    if routing["reasoning_effort"] not in REASONING_EFFORTS:
        raise ProtocolError("workflow.routing.reasoning_effort is not qualified")

    limits = _object(value["limits"], "workflow.limits")
    _exact_keys(
        limits,
        {"workflow_budget_seconds", "max_additional_phases", "max_parallel_tasks"},
        "workflow.limits",
    )
    _integer(limits["workflow_budget_seconds"], "workflow.limits.workflow_budget_seconds", minimum=1)
    _integer(limits["max_additional_phases"], "workflow.limits.max_additional_phases")
    _integer(limits["max_parallel_tasks"], "workflow.limits.max_parallel_tasks", minimum=1)

    bundle = _object(value["runtime_bundle"], "workflow.runtime_bundle")
    _exact_keys(bundle, {"version", "sha256"}, "workflow.runtime_bundle")
    _text(bundle["version"], "workflow.runtime_bundle.version")
    _sha256(bundle["sha256"], "workflow.runtime_bundle.sha256")
    _relative_path(value["baseline_ref"], "workflow.baseline_ref")
    _sha256(value["baseline_sha256"], "workflow.baseline_sha256")

    admission = _object(value["admission"], "workflow.admission")
    _exact_keys(
        admission,
        {
            "profile",
            "relevant_roots",
            "repository",
            "host_capacity",
            "capabilities",
            "accounting_coverage",
        },
        "workflow.admission",
    )
    profile = admission["profile"]
    if profile not in {"capability_probe", "read_only_canary", "source_write"}:
        raise ProtocolError("workflow.admission.profile is invalid")
    roots = admission["relevant_roots"]
    if not isinstance(roots, list) or not roots:
        raise ProtocolError("workflow.admission.relevant_roots must be a non-empty list")
    normalized_roots = [
        _repository_read_root(root, "workflow.admission.relevant_roots[]")
        for root in roots
    ]
    if len(normalized_roots) != len(set(normalized_roots)):
        raise ProtocolError("workflow.admission.relevant_roots must be unique")

    repository = _object(admission["repository"], "workflow.admission.repository")
    _exact_keys(
        repository,
        {
            "head",
            "branch",
            "staged_diff_sha256",
            "unstaged_diff_sha256",
            "untracked_manifest_sha256",
            "relevant_files_sha256",
            "dirty_paths_sha256",
        },
        "workflow.admission.repository",
    )
    if not re.fullmatch(r"[0-9a-f]{40,64}", _text(repository["head"], "workflow.admission.repository.head")):
        raise ProtocolError("workflow.admission.repository.head must be a git object id")
    _text(repository["branch"], "workflow.admission.repository.branch")
    for key in (
        "staged_diff_sha256",
        "unstaged_diff_sha256",
        "untracked_manifest_sha256",
        "relevant_files_sha256",
        "dirty_paths_sha256",
    ):
        _sha256(repository[key], f"workflow.admission.repository.{key}")

    capacity = _object(admission["host_capacity"], "workflow.admission.host_capacity")
    _exact_keys(capacity, {"max_processes", "max_parallel_tasks"}, "workflow.admission.host_capacity")
    _integer(capacity["max_processes"], "workflow.admission.host_capacity.max_processes", minimum=1)
    admitted_parallel = _integer(
        capacity["max_parallel_tasks"],
        "workflow.admission.host_capacity.max_parallel_tasks",
        minimum=1,
    )
    if admitted_parallel > limits["max_parallel_tasks"]:
        raise ProtocolError("workflow admission capacity exceeds workflow limit")

    capabilities = _object(admission["capabilities"], "workflow.admission.capabilities")
    _exact_keys(capabilities, CAPABILITY_NAMES, "workflow.admission.capabilities")
    required = set()
    if profile == "read_only_canary":
        required = CAPABILITY_NAMES - {"sandbox_isolation"}
    elif profile == "source_write":
        required = set(CAPABILITY_NAMES)
    for name, raw in capabilities.items():
        label = f"workflow.admission.capabilities.{name}"
        capability = _object(raw, label)
        _exact_keys(capability, {"status", "evidence_ref", "evidence_sha256"}, label)
        if capability["status"] not in {"pass", "blocked", "unavailable"}:
            raise ProtocolError(f"{label}.status is invalid")
        _relative_path(capability["evidence_ref"], f"{label}.evidence_ref")
        _sha256(capability["evidence_sha256"], f"{label}.evidence_sha256")
        if name in required and capability["status"] != "pass":
            raise ProtocolError(f"{label} must pass for admission profile {profile}")
    if admission["accounting_coverage"] not in {"exact", "partial"}:
        raise ProtocolError("workflow.admission.accounting_coverage must be exact or partial")


def _validate_phase_plan(value: dict[str, Any]) -> None:
    _exact_keys(
        value,
        {
            "schema_version",
            "phase_id",
            "generation_id",
            "predecessor_sha256",
            "authority_revision",
            "caused_by",
            "intent",
            "phase_budget_seconds",
            "tasks",
        },
        "phase-plan",
    )
    _slug(value["phase_id"], "phase-plan.phase_id")
    _slug(value["generation_id"], "phase-plan.generation_id")
    _sha256(value["predecessor_sha256"], "phase-plan.predecessor_sha256")
    _integer(value["authority_revision"], "phase-plan.authority_revision", minimum=1)

    caused_by = value["caused_by"]
    if not isinstance(caused_by, list):
        raise ProtocolError("phase-plan.caused_by must be a list of non-empty phase ids")
    for index, item in enumerate(caused_by):
        _slug(item, f"phase-plan.caused_by[{index}]")
    if len(caused_by) != len(set(caused_by)):
        raise ProtocolError("phase-plan.caused_by values must be unique")

    intent = _object(value["intent"], "phase-plan.intent")
    _exact_keys(intent, {"reason", "expected_state_change"}, "phase-plan.intent")
    _text(intent["reason"], "phase-plan.intent.reason")
    _text(intent["expected_state_change"], "phase-plan.intent.expected_state_change")

    phase_budget = _integer(
        value["phase_budget_seconds"],
        "phase-plan.phase_budget_seconds",
        minimum=1,
    )
    tasks = value["tasks"]
    if not isinstance(tasks, list) or not tasks:
        raise ProtocolError("phase-plan.tasks must be a non-empty list")
    task_ids: set[str] = set()
    lineage_ids: set[str] = set()
    writer_roots: list[tuple[str, str]] = []
    for index, raw in enumerate(tasks):
        label = f"phase-plan.tasks[{index}]"
        task = _object(raw, label)
        _exact_keys(
            task,
            {
                "task_id",
                "lineage_id",
                "criterion_id",
                "criterion_revision",
                "role",
                "work_mode",
                "packet_path",
                "packet_sha256",
                "input_refs",
                "input_sha256",
                "write_roots",
                "execution_deadline_seconds",
            },
            label,
        )
        task_id = _slug(task["task_id"], f"{label}.task_id")
        lineage_id = _slug(task["lineage_id"], f"{label}.lineage_id")
        if task_id in task_ids:
            raise ProtocolError("phase-plan task ids must be unique")
        if lineage_id in lineage_ids:
            raise ProtocolError("phase-plan lineage ids must be unique within a phase")
        task_ids.add(task_id)
        lineage_ids.add(lineage_id)
        _text(task["criterion_id"], f"{label}.criterion_id")
        _integer(task["criterion_revision"], f"{label}.criterion_revision", minimum=1)
        if task["role"] not in {"top", "worker"}:
            raise ProtocolError(f"{label}.role must be top or worker")
        if task["work_mode"] not in {"read", "write"}:
            raise ProtocolError(f"{label}.work_mode must be read or write")
        _relative_path(task["packet_path"], f"{label}.packet_path")
        _sha256(task["packet_sha256"], f"{label}.packet_sha256")
        input_refs = task["input_refs"]
        if not isinstance(input_refs, list):
            raise ProtocolError(f"{label}.input_refs must be a list")
        for ref_index, ref in enumerate(input_refs):
            _relative_path(ref, f"{label}.input_refs[{ref_index}]")
        if len(input_refs) != len(set(input_refs)):
            raise ProtocolError(f"{label}.input_refs values must be unique")
        _artifact_digest_map(task["input_sha256"], input_refs, f"{label}.input_sha256")
        roots = task["write_roots"]
        if not isinstance(roots, list):
            raise ProtocolError(f"{label}.write_roots must be a list")
        normalized = [_canonical_root(root, f"{label}.write_roots[]") for root in roots]
        if len(normalized) != len(set(normalized)):
            raise ProtocolError(f"{label}.write_roots values must be unique")
        if task["work_mode"] == "read" and task["write_roots"]:
            raise ProtocolError(f"{label}.write_roots must be empty for read tasks")
        if task["work_mode"] == "write" and not task["write_roots"]:
            raise ProtocolError(f"{label}.write_roots must be non-empty for write tasks")
        if task["work_mode"] == "write":
            for root in normalized:
                for prior_task, prior_root in writer_roots:
                    if _roots_overlap(root, prior_root):
                        raise ProtocolError(
                            f"writer roots overlap: {prior_task}:{prior_root} and {task_id}:{root}"
                        )
                writer_roots.append((task_id, root))
        task_budget = _integer(
            task["execution_deadline_seconds"],
            f"{label}.execution_deadline_seconds",
            minimum=1,
        )
        if task_budget > phase_budget:
            raise ProtocolError(f"{label}.execution_deadline_seconds cannot exceed the phase budget")


def _validate_task_result(value: dict[str, Any]) -> None:
    _exact_keys(
        value,
        {
            "schema_version",
            "workflow_id",
            "phase_id",
            "task_id",
            "lineage_id",
            "attempt",
            "status",
            "terminal_reason",
            "actual_route",
            "output_ref",
            "output_sha256",
            "evidence_refs",
            "evidence_sha256",
            "checks",
            "changed_paths",
            "started_at",
            "finished_at",
            "elapsed_ms",
            "token_usage",
        },
        "task-result",
    )
    for key in ("workflow_id", "phase_id", "task_id", "lineage_id"):
        _slug(value[key], f"task-result.{key}")
    terminal_reason = _text(value["terminal_reason"], "task-result.terminal_reason")
    _integer(value["attempt"], "task-result.attempt", minimum=1)
    if value["status"] not in TASK_TERMINAL_STATUSES:
        raise ProtocolError("task-result.status must be terminal")
    reasons = {
        "completed": {"typed_output_validated"},
        "failed": {"codex_turn_failed", "invalid_typed_output", "check_failed", "runner_error"},
        "timed_out": {"execution_deadline"},
        "cancelled": {"authority_cancelled", "user_cancelled"},
        "not_started_deadline": {"queue_deadline"},
        "not_started_interrupted": {"runner_interrupted_before_launch"},
        "concurrent_edit_conflict": {"source_drift", "root_overlap"},
        "route_attestation_failed": {"route_mismatch", "attestation_missing"},
        "escaped_process_detected": {"escaped_process_detected"},
    }
    if terminal_reason not in reasons[value["status"]]:
        raise ProtocolError("task-result terminal_reason contradicts status")

    route = value["actual_route"]
    if route is not None:
        route = _object(route, "task-result.actual_route")
        _exact_keys(
            route,
            {"model", "reasoning_effort", "session_id", "attestation_ref", "attestation_sha256"},
            "task-result.actual_route",
        )
        for key in ("model", "reasoning_effort", "session_id"):
            _text(route[key], f"task-result.actual_route.{key}")
        _relative_path(route["attestation_ref"], "task-result.actual_route.attestation_ref")
        _sha256(route["attestation_sha256"], "task-result.actual_route.attestation_sha256")
    if value["status"] == "completed" and route is None:
        raise ProtocolError("completed task-result requires actual_route attestation")

    output_ref = value["output_ref"]
    if output_ref is not None:
        _relative_path(output_ref, "task-result.output_ref")
        _sha256(value["output_sha256"], "task-result.output_sha256")
    elif value["output_sha256"] is not None:
        raise ProtocolError("task-result.output_sha256 requires output_ref")
    if value["status"] == "completed" and output_ref is None:
        raise ProtocolError("completed task-result requires output_ref")

    for field in ("evidence_refs", "changed_paths"):
        refs = value[field]
        if not isinstance(refs, list):
            raise ProtocolError(f"task-result.{field} must be a list")
        for index, ref in enumerate(refs):
            _relative_path(ref, f"task-result.{field}[{index}]")
        if len(refs) != len(set(refs)):
            raise ProtocolError(f"task-result.{field} values must be unique")
    _artifact_digest_map(
        value["evidence_sha256"],
        value["evidence_refs"],
        "task-result.evidence_sha256",
    )

    checks = value["checks"]
    if not isinstance(checks, list):
        raise ProtocolError("task-result.checks must be a list")
    for index, raw in enumerate(checks):
        label = f"task-result.checks[{index}]"
        check = _object(raw, label)
        _exact_keys(check, {"name", "exit_code", "evidence_ref", "evidence_sha256"}, label)
        _text(check["name"], f"{label}.name")
        if not isinstance(check["exit_code"], int) or isinstance(check["exit_code"], bool):
            raise ProtocolError(f"{label}.exit_code must be an integer")
        _relative_path(check["evidence_ref"], f"{label}.evidence_ref")
        _sha256(check["evidence_sha256"], f"{label}.evidence_sha256")
    if value["status"] == "completed" and any(check["exit_code"] != 0 for check in checks):
        raise ProtocolError("completed task-result cannot contain failed checks")

    started = _parsed_rfc3339(value["started_at"], "task-result.started_at")
    finished = _parsed_rfc3339(value["finished_at"], "task-result.finished_at")
    if finished < started:
        raise ProtocolError("task-result.finished_at cannot precede started_at")
    elapsed_ms = _integer(value["elapsed_ms"], "task-result.elapsed_ms")
    wall_elapsed_ms = int((finished - started).total_seconds() * 1000)
    if abs(elapsed_ms - wall_elapsed_ms) > 1000:
        raise ProtocolError("task-result.elapsed_ms must reconcile with timestamps")

    usage = _object(value["token_usage"], "task-result.token_usage")
    _exact_keys(usage, {"input", "output", "total", "source", "confidence"}, "task-result.token_usage")
    source = usage["source"]
    confidence = usage["confidence"]
    if source == "unavailable":
        if confidence != "partial" or any(usage[key] is not None for key in ("input", "output", "total")):
            raise ProtocolError("unavailable token usage must be partial with null counts")
        if value["status"] == "completed":
            raise ProtocolError("completed task-result requires exact token usage")
    else:
        input_tokens = _integer(usage["input"], "task-result.token_usage.input")
        output_tokens = _integer(usage["output"], "task-result.token_usage.output")
        total_tokens = _integer(usage["total"], "task-result.token_usage.total")
        if total_tokens != input_tokens + output_tokens:
            raise ProtocolError("task-result.token_usage.total must equal input + output")
        if source == "no_session":
            if value["status"] not in {"not_started_deadline", "not_started_interrupted"} or total_tokens != 0 or confidence != "exact":
                raise ProtocolError("no_session accounting is only valid for an unstarted zero-token task")
        elif source != "codex_terminal_events" or confidence != "exact":
            raise ProtocolError("started task token usage must use exact codex_terminal_events evidence")
    if value["status"] in {"not_started_deadline", "not_started_interrupted"} and source != "no_session":
        raise ProtocolError("unstarted task requires exact no_session accounting")
    if source == "codex_terminal_events" and route is None and not (
        value["status"] == "route_attestation_failed"
        and value["terminal_reason"] == "attestation_missing"
    ):
        raise ProtocolError("started task-result requires actual_route evidence")


def _digest_map(value: Any, label: str) -> dict[str, str]:
    mapping = _object(value, label)
    result: dict[str, str] = {}
    for path, digest in mapping.items():
        safe_path = _relative_path(path, f"{label} path")
        result[safe_path] = _sha256(digest, f"{label}.{path}")
    return result


def _validate_phase_receipt(value: dict[str, Any]) -> None:
    _exact_keys(
        value,
        {
            "schema_version",
            "workflow_id",
            "phase_id",
            "generation_id",
            "generation_claim_ref",
            "generation_claim_sha256",
            "plan_sha256",
            "predecessor_sha256",
            "status",
            "task_result_refs",
            "task_result_sha256",
            "task_counts",
            "integration",
            "terminal_reason",
            "created_at",
        },
        "phase-receipt",
    )
    for key in ("workflow_id", "phase_id", "generation_id"):
        _slug(value[key], f"phase-receipt.{key}")
    _relative_path(value["generation_claim_ref"], "phase-receipt.generation_claim_ref")
    _sha256(value["generation_claim_sha256"], "phase-receipt.generation_claim_sha256")
    terminal_reason = _text(value["terminal_reason"], "phase-receipt.terminal_reason")
    _sha256(value["plan_sha256"], "phase-receipt.plan_sha256")
    _sha256(value["predecessor_sha256"], "phase-receipt.predecessor_sha256")
    if value["status"] not in PHASE_TERMINAL_STATUSES:
        raise ProtocolError("phase-receipt.status must be terminal")

    refs = value["task_result_refs"]
    if not isinstance(refs, list) or not refs:
        raise ProtocolError("phase-receipt.task_result_refs must be a non-empty list")
    for index, ref in enumerate(refs):
        _relative_path(ref, f"phase-receipt.task_result_refs[{index}]")
    if len(refs) != len(set(refs)):
        raise ProtocolError("phase-receipt.task_result_refs values must be unique")
    _artifact_digest_map(
        value["task_result_sha256"],
        refs,
        "phase-receipt.task_result_sha256",
    )

    counts = _object(value["task_counts"], "phase-receipt.task_counts")
    terminal_names = {
        "completed",
        "failed",
        "timed_out",
        "cancelled",
        "not_started_deadline",
        "not_started_interrupted",
        "concurrent_edit_conflict",
        "route_attestation_failed",
        "escaped_process_detected",
    }
    _exact_keys(counts, terminal_names | {"total"}, "phase-receipt.task_counts")
    terminal_total = sum(
        _integer(counts[name], f"phase-receipt.task_counts.{name}")
        for name in terminal_names
    )
    total = _integer(counts["total"], "phase-receipt.task_counts.total", minimum=1)
    if total != terminal_total:
        raise ProtocolError("phase-receipt.task_counts.total must equal terminal counts")
    if len(refs) != total:
        raise ProtocolError("phase-receipt must reference one task result per terminal task")
    non_completed = total - counts["completed"]
    status = value["status"]
    if status == "completed" and non_completed:
        raise ProtocolError("completed phase-receipt cannot contain unsuccessful tasks")
    if status == "completed_with_failures" and not (counts["completed"] and non_completed):
        raise ProtocolError("completed_with_failures requires mixed task outcomes")
    if status in {"failed", "cancelled", "blocked"} and not non_completed:
        raise ProtocolError(f"{status} phase-receipt requires at least one unsuccessful task")
    receipt_reasons = {
        "completed": {"all_tasks_terminal"},
        "completed_with_failures": {"task_failures_terminal"},
        "failed": {"task_failures_terminal", "integration_failed"},
        "cancelled": {"phase_cancelled"},
        "blocked": {"admission_blocked", "integration_conflict"},
    }
    if terminal_reason not in receipt_reasons[status]:
        raise ProtocolError("phase-receipt terminal_reason contradicts status")

    integration = _object(value["integration"], "phase-receipt.integration")
    _exact_keys(
        integration,
        {"mode", "status", "patch_ref", "patch_sha256", "target_before", "target_after"},
        "phase-receipt.integration",
    )
    if integration["mode"] not in {"none", "isolated_exact_base"}:
        raise ProtocolError("phase-receipt.integration.mode is invalid")
    before = _digest_map(integration["target_before"], "phase-receipt.integration.target_before")
    after = _digest_map(integration["target_after"], "phase-receipt.integration.target_after")
    if integration["mode"] == "none":
        if (
            integration["status"] != "not_applicable"
            or integration["patch_ref"] is not None
            or integration["patch_sha256"] is not None
            or before
            or after
        ):
            raise ProtocolError("non-writing phase receipt must use empty not_applicable integration")
    else:
        if integration["status"] not in {"applied", "conflict", "not_applied"}:
            raise ProtocolError("isolated integration status is invalid")
        _relative_path(integration["patch_ref"], "phase-receipt.integration.patch_ref")
        _sha256(integration["patch_sha256"], "phase-receipt.integration.patch_sha256")
        if not before:
            raise ProtocolError("isolated integration requires target_before digests")
        if integration["status"] == "applied" and not after:
            raise ProtocolError("applied integration requires target_after digests")
        if integration["status"] == "applied" and set(before) != set(after):
            raise ProtocolError("applied integration must bind identical target sets before and after")
        if integration["status"] == "applied" and status not in {
            "completed", "completed_with_failures"
        }:
            raise ProtocolError("applied integration requires a completed phase status")
        if integration["status"] == "conflict" and not (
            status == "blocked" and terminal_reason == "integration_conflict"
        ):
            raise ProtocolError("integration conflict requires blocked integration_conflict phase")
        if integration["status"] == "not_applied" and status in {
            "completed", "completed_with_failures"
        }:
            raise ProtocolError("completed phase cannot leave isolated integration unapplied")
    _rfc3339(value["created_at"], "phase-receipt.created_at")


def _validate_final(value: dict[str, Any]) -> None:
    _exact_keys(
        value,
        {
            "schema_version",
            "workflow_id",
            "generation_id",
            "status",
            "objective_outcome",
            "verification_ref",
            "verification_sha256",
            "phase_receipt_refs",
            "phase_receipt_sha256",
            "amendment_refs",
            "amendment_sha256",
            "lineage_claim_refs",
            "lineage_claim_sha256",
            "p2_resolutions",
            "accounting",
            "completion_density",
            "final_report_ref",
            "final_report_sha256",
            "runtime_bundle_sha256",
            "created_at",
        },
        "final",
    )
    for key in ("workflow_id", "generation_id"):
        _slug(value[key], f"final.{key}")
    _text(value["objective_outcome"], "final.objective_outcome")
    if value["status"] not in FINAL_STATUSES:
        raise ProtocolError("final.status must be terminal")
    verification_ref = value["verification_ref"]
    if verification_ref is not None:
        _relative_path(verification_ref, "final.verification_ref")
        _sha256(value["verification_sha256"], "final.verification_sha256")
    elif value["verification_sha256"] is not None:
        raise ProtocolError("final.verification_sha256 requires verification_ref")
    if value["status"] == "complete" and verification_ref is None:
        raise ProtocolError("complete final requires verification_ref")

    phase_refs = value["phase_receipt_refs"]
    if not isinstance(phase_refs, list) or not phase_refs:
        raise ProtocolError("final.phase_receipt_refs must be a non-empty list")
    for index, ref in enumerate(phase_refs):
        _relative_path(ref, f"final.phase_receipt_refs[{index}]")
    if len(phase_refs) != len(set(phase_refs)):
        raise ProtocolError("final.phase_receipt_refs values must be unique")
    _artifact_digest_map(
        value["phase_receipt_sha256"],
        phase_refs,
        "final.phase_receipt_sha256",
    )
    amendment_refs = value["amendment_refs"]
    if not isinstance(amendment_refs, list):
        raise ProtocolError("final.amendment_refs must be a list")
    for index, ref in enumerate(amendment_refs):
        _relative_path(ref, f"final.amendment_refs[{index}]")
    if len(amendment_refs) != len(set(amendment_refs)):
        raise ProtocolError("final.amendment_refs values must be unique")
    _artifact_digest_map(
        value["amendment_sha256"],
        amendment_refs,
        "final.amendment_sha256",
    )
    lineage_claim_refs = value["lineage_claim_refs"]
    if not isinstance(lineage_claim_refs, list):
        raise ProtocolError("final.lineage_claim_refs must be a list")
    for index, ref in enumerate(lineage_claim_refs):
        _relative_path(ref, f"final.lineage_claim_refs[{index}]")
    if len(lineage_claim_refs) != len(set(lineage_claim_refs)):
        raise ProtocolError("final.lineage_claim_refs values must be unique")
    _artifact_digest_map(
        value["lineage_claim_sha256"],
        lineage_claim_refs,
        "final.lineage_claim_sha256",
    )

    resolutions = value["p2_resolutions"]
    if not isinstance(resolutions, list):
        raise ProtocolError("final.p2_resolutions must be a list")
    finding_ids: set[str] = set()
    for index, raw in enumerate(resolutions):
        label = f"final.p2_resolutions[{index}]"
        item = _object(raw, label)
        _exact_keys(
            item,
            {
                "finding_id",
                "resolution",
                "rationale",
                "owner",
                "gate",
                "evidence_ref",
                "evidence_sha256",
            },
            label,
        )
        finding_id = _text(item["finding_id"], f"{label}.finding_id")
        if finding_id in finding_ids:
            raise ProtocolError("final.p2_resolutions finding ids must be unique")
        finding_ids.add(finding_id)
        if item["resolution"] not in P2_RESOLUTIONS:
            raise ProtocolError(f"{label}.resolution is invalid")
        _text(item["rationale"], f"{label}.rationale")
        if item["resolution"] == "deferred_with_owner_gate":
            _text(item["owner"], f"{label}.owner")
            _text(item["gate"], f"{label}.gate")
        elif item["owner"] is not None or item["gate"] is not None:
            raise ProtocolError(f"{label} owner/gate are reserved for deferred resolutions")
        if item["evidence_ref"] is not None:
            _relative_path(item["evidence_ref"], f"{label}.evidence_ref")
            _sha256(item["evidence_sha256"], f"{label}.evidence_sha256")
        elif item["evidence_sha256"] is not None:
            raise ProtocolError(f"{label}.evidence_sha256 requires evidence_ref")

    accounting = _object(value["accounting"], "final.accounting")
    _exact_keys(
        accounting,
        {"coverage", "workflow_tokens", "source", "confidence", "boundary"},
        "final.accounting",
    )
    if (
        accounting["coverage"] != "partial"
        or accounting["confidence"] != "partial"
        or accounting["workflow_tokens"] is not None
        or accounting["source"] != "pending_post_terminal_sidecar"
    ):
        raise ProtocolError("semantic final must defer exact usage to the post-terminal accounting sidecar")
    if accounting["boundary"] != "through_orchestrator_terminal":
        raise ProtocolError("final.accounting.boundary must end at orchestrator terminal")

    density = _object(value["completion_density"], "final.completion_density")
    _exact_keys(
        density,
        {"source", "forbidden_wakes", "semantic_wakes", "sparse_wait_continuations", "target_eligible"},
        "final.completion_density",
    )
    if density["source"] != "pending_post_terminal_sidecar" or any(
        density[key] is not None
        for key in ("forbidden_wakes", "semantic_wakes", "sparse_wait_continuations", "target_eligible")
    ):
        raise ProtocolError("semantic final must defer completion counts to the post-terminal completion-density sidecar")

    _relative_path(value["final_report_ref"], "final.final_report_ref")
    _sha256(value["final_report_sha256"], "final.final_report_sha256")
    _sha256(value["runtime_bundle_sha256"], "final.runtime_bundle_sha256")
    _rfc3339(value["created_at"], "final.created_at")


def _validate_amendment(value: dict[str, Any]) -> None:
    common = {
        "schema_version",
        "amendment_kind",
        "workflow_id",
        "amendment_id",
        "previous_authority_revision",
        "authority_revision",
        "user_instruction_ref",
        "user_instruction_sha256",
        "reason",
    }
    kind = value.get("amendment_kind")
    if kind == "criterion_revision":
        _exact_keys(
            value,
            common
            | {
                "criterion_id",
                "from_revision",
                "to_revision",
                "blocked_result_ref",
                "blocked_result_sha256",
            },
            "amendment",
        )
        _text(value["criterion_id"], "amendment.criterion_id")
        from_revision = _integer(value["from_revision"], "amendment.from_revision", minimum=1)
        to_revision = _integer(value["to_revision"], "amendment.to_revision", minimum=2)
        if to_revision != from_revision + 1:
            raise ProtocolError("amendment criterion revision must advance exactly once")
        _relative_path(value["blocked_result_ref"], "amendment.blocked_result_ref")
        _sha256(value["blocked_result_sha256"], "amendment.blocked_result_sha256")
    elif kind == "instruction":
        _exact_keys(
            value,
            common | {"applies_after_ref", "applies_after_sha256"},
            "amendment",
        )
        _relative_path(value["applies_after_ref"], "amendment.applies_after_ref")
        _sha256(value["applies_after_sha256"], "amendment.applies_after_sha256")
    else:
        raise ProtocolError("amendment.amendment_kind must be criterion_revision or instruction")
    _slug(value["workflow_id"], "amendment.workflow_id")
    _slug(value["amendment_id"], "amendment.amendment_id")
    previous = _integer(
        value["previous_authority_revision"],
        "amendment.previous_authority_revision",
        minimum=1,
    )
    authority = _integer(value["authority_revision"], "amendment.authority_revision", minimum=2)
    if authority != previous + 1:
        raise ProtocolError("amendment authority revision must advance exactly once")
    _relative_path(value["user_instruction_ref"], "amendment.user_instruction_ref")
    _sha256(value["user_instruction_sha256"], "amendment.user_instruction_sha256")
    _text(value["reason"], "amendment.reason")


def _validate_generation_claim(value: dict[str, Any]) -> None:
    _exact_keys(
        value,
        {
            "schema_version",
            "workflow_id",
            "generation_id",
            "phase_id",
            "predecessor_sha256",
            "authority_revision",
            "plan_sha256",
            "contention_key",
        },
        "generation-claim",
    )
    for key in ("workflow_id", "generation_id", "phase_id"):
        _slug(value[key], f"generation-claim.{key}")
    _sha256(value["predecessor_sha256"], "generation-claim.predecessor_sha256")
    _integer(value["authority_revision"], "generation-claim.authority_revision", minimum=1)
    _sha256(value["plan_sha256"], "generation-claim.plan_sha256")
    _sha256(value["contention_key"], "generation-claim.contention_key")


def _validate_lineage_claim(value: dict[str, Any]) -> None:
    common = {"schema_version", "claim_kind", "workflow_id", "lineage_id"}
    if value.get("claim_kind") == "origin":
        _exact_keys(
            value,
            common
            | {
                "criterion_id",
                "criterion_revision",
                "role",
                "scope_sha256",
                "origin_phase_id",
                "origin_task_id",
            },
            "lineage-claim origin",
        )
        _text(value["criterion_id"], "lineage-claim.criterion_id")
        _integer(value["criterion_revision"], "lineage-claim.criterion_revision", minimum=1)
        if value["role"] not in {"top", "worker"}:
            raise ProtocolError("lineage-claim.role must be top or worker")
        _sha256(value["scope_sha256"], "lineage-claim.scope_sha256")
        _slug(value["origin_phase_id"], "lineage-claim.origin_phase_id")
        _slug(value["origin_task_id"], "lineage-claim.origin_task_id")
    elif value.get("claim_kind") == "recovery":
        _exact_keys(
            value,
            common
            | {
                "origin_ref",
                "origin_sha256",
                "failed_result_ref",
                "failed_result_sha256",
                "recovery_phase_id",
                "recovery_task_id",
                "recovery_scope_sha256",
                "criterion_id",
                "criterion_revision",
                "authority_revision",
                "recovery_kind",
            },
            "lineage-claim recovery",
        )
        _relative_path(value["origin_ref"], "lineage-claim.origin_ref")
        _sha256(value["origin_sha256"], "lineage-claim.origin_sha256")
        _relative_path(value["failed_result_ref"], "lineage-claim.failed_result_ref")
        _sha256(value["failed_result_sha256"], "lineage-claim.failed_result_sha256")
        _slug(value["recovery_phase_id"], "lineage-claim.recovery_phase_id")
        _slug(value["recovery_task_id"], "lineage-claim.recovery_task_id")
        _sha256(value["recovery_scope_sha256"], "lineage-claim.recovery_scope_sha256")
        _text(value["criterion_id"], "lineage-claim.criterion_id")
        _integer(value["criterion_revision"], "lineage-claim.criterion_revision", minimum=1)
        _integer(value["authority_revision"], "lineage-claim.authority_revision", minimum=1)
        if value["recovery_kind"] not in {"automatic_infra_retry", "evidence_aware_repair"}:
            raise ProtocolError("lineage-claim.recovery_kind is invalid")
    else:
        raise ProtocolError("lineage-claim.claim_kind must be origin or recovery")
    _slug(value["workflow_id"], "lineage-claim.workflow_id")
    _slug(value["lineage_id"], "lineage-claim.lineage_id")


def _validate_accounting_sidecar(value: dict[str, Any]) -> None:
    _exact_keys(
        value,
        {
            "schema_version", "workflow_id", "final_ref", "final_sha256",
            "runtime_bundle_sha256", "boundary", "coverage", "confidence",
            "workflow_tokens", "external_task_usage", "native_orchestrator",
            "completion_density", "created_at",
        },
        "accounting",
    )
    _slug(value["workflow_id"], "accounting.workflow_id")
    if value["final_ref"] != "final.json":
        raise ProtocolError("accounting.final_ref must be final.json")
    _sha256(value["final_sha256"], "accounting.final_sha256")
    _sha256(value["runtime_bundle_sha256"], "accounting.runtime_bundle_sha256")
    if value["boundary"] != "through_orchestrator_terminal":
        raise ProtocolError("accounting boundary must end at orchestrator terminal")

    external = _object(value["external_task_usage"], "accounting.external_task_usage")
    _exact_keys(external, {"source", "confidence", "input", "output", "total"}, "accounting.external_task_usage")
    _text(external["source"], "accounting.external_task_usage.source")
    if external["confidence"] == "exact":
        ext_input = _integer(external["input"], "accounting.external_task_usage.input")
        ext_output = _integer(external["output"], "accounting.external_task_usage.output")
        ext_total = _integer(external["total"], "accounting.external_task_usage.total")
        if ext_total != ext_input + ext_output:
            raise ProtocolError("external task token arithmetic is invalid")
    elif external["confidence"] == "partial":
        if any(external[key] is not None for key in ("input", "output", "total")):
            raise ProtocolError("partial external task usage must use null counts")
        ext_total = None
    else:
        raise ProtocolError("external task usage confidence is invalid")

    native = _object(value["native_orchestrator"], "accounting.native_orchestrator")
    _exact_keys(
        native,
        {"coverage", "source", "confidence", "tokens", "evidence_ref", "evidence_sha256", "raw_evidence_ref", "raw_evidence_sha256", "reason", "late_seal_wake_required"},
        "accounting.native_orchestrator",
    )
    if native["coverage"] not in {"exact", "partial"} or native["confidence"] != native["coverage"]:
        raise ProtocolError("native orchestrator coverage/confidence is invalid")
    _text(native["source"], "accounting.native_orchestrator.source")
    _relative_path(native["evidence_ref"], "accounting.native_orchestrator.evidence_ref")
    _relative_path(native["raw_evidence_ref"], "accounting.native_orchestrator.raw_evidence_ref")
    _sha256(native["raw_evidence_sha256"], "accounting.native_orchestrator.raw_evidence_sha256")
    if native["evidence_sha256"] is not None:
        _sha256(native["evidence_sha256"], "accounting.native_orchestrator.evidence_sha256")
    if native["late_seal_wake_required"] is not False:
        raise ProtocolError("native accounting cannot require a late-seal wake")
    if native["coverage"] == "exact":
        native_tokens = _integer(native["tokens"], "accounting.native_orchestrator.tokens")
        if native["reason"] is not None or native["evidence_sha256"] is None:
            raise ProtocolError("exact native accounting requires evidence and no partial reason")
    else:
        native_tokens = native["tokens"]
        if native_tokens is not None:
            _integer(native_tokens, "accounting.native_orchestrator.tokens")
        _text(native["reason"], "accounting.native_orchestrator.reason")

    density = _object(value["completion_density"], "accounting.completion_density")
    _exact_keys(
        density,
        {
            "source", "session_id", "terminal_turn_id", "forbidden_wakes", "semantic_wakes",
            "sparse_wait_continuations", "target_eligible", "evidence_ref", "evidence_sha256",
            "projection_ref", "projection_sha256",
        },
        "accounting.completion_density",
    )
    if density["source"] != "raw_session_replay_v1":
        raise ProtocolError("accounting completion density requires raw session replay")
    _text(density["session_id"], "accounting.completion_density.session_id")
    _text(density["terminal_turn_id"], "accounting.completion_density.terminal_turn_id")
    _relative_path(density["evidence_ref"], "accounting.completion_density.evidence_ref")
    _relative_path(density["projection_ref"], "accounting.completion_density.projection_ref")
    forbidden = _integer(density["forbidden_wakes"], "accounting.completion_density.forbidden_wakes")
    _integer(density["semantic_wakes"], "accounting.completion_density.semantic_wakes")
    sparse = _integer(density["sparse_wait_continuations"], "accounting.completion_density.sparse_wait_continuations")
    if not isinstance(density["target_eligible"], bool):
        raise ProtocolError("accounting completion density target_eligible must be boolean")
    if density["target_eligible"] and (forbidden or sparse):
        raise ProtocolError("target-eligible accounting cannot contain forbidden or sparse wakes")
    _sha256(density["evidence_sha256"], "accounting.completion_density.evidence_sha256")
    _sha256(density["projection_sha256"], "accounting.completion_density.projection_sha256")

    if value["coverage"] == "exact":
        if value["confidence"] != "exact" or external["confidence"] != "exact" or native["coverage"] != "exact":
            raise ProtocolError("exact accounting requires exact external and native coverage")
        workflow_tokens = _integer(value["workflow_tokens"], "accounting.workflow_tokens")
        if workflow_tokens != ext_total + native_tokens:
            raise ProtocolError("workflow token arithmetic is invalid")
    elif value["coverage"] == "partial":
        if value["confidence"] != "partial":
            raise ProtocolError("partial accounting requires partial confidence")
        if value["workflow_tokens"] is not None:
            _integer(value["workflow_tokens"], "accounting.workflow_tokens")
    else:
        raise ProtocolError("accounting.coverage must be exact or partial")
    _rfc3339(value["created_at"], "accounting.created_at")


def validate_sidecar(kind: str, value: Any) -> dict[str, Any]:
    """Validate one scoped authority sidecar without creating lifecycle state."""

    schema = SCOPED_SIDECAR_SCHEMAS.get(kind)
    if schema is None:
        raise ProtocolError(f"unknown scoped sidecar kind: {kind!r}")
    if not isinstance(value, dict) or value.get("schema_version") != schema:
        raise ProtocolError(f"{kind}.schema_version must be {schema}")
    if kind == "amendment":
        _validate_amendment(value)
    elif kind == "generation-claim":
        _validate_generation_claim(value)
    elif kind == "lineage-claim":
        _validate_lineage_claim(value)
    elif kind == "accounting":
        _validate_accounting_sidecar(value)
    else:
        raise ProtocolError(f"scoped sidecar validator is not implemented for {kind!r}")
    return value


def validate_contract(kind: str, value: Any) -> dict[str, Any]:
    """Validate one authoritative lifecycle contract and return it unchanged."""

    schema = LIFECYCLE_SCHEMAS.get(kind)
    if schema is None:
        raise ProtocolError(f"unknown lifecycle contract kind: {kind!r}")
    if not isinstance(value, dict):
        raise ProtocolError(f"{kind} contract must be an object")
    if value.get("schema_version") != schema:
        raise ProtocolError(f"{kind}.schema_version must be {schema}")
    if kind == "workflow":
        _validate_workflow(value)
    elif kind == "phase-plan":
        _validate_phase_plan(value)
    elif kind == "task-result":
        _validate_task_result(value)
    elif kind == "phase-receipt":
        _validate_phase_receipt(value)
    elif kind == "final":
        _validate_final(value)
    return value


def _read_artifact_bytes(root: Path, relative_path: str, expected_sha256: str) -> bytes:
    """Read one artifact without following links and verify its exact bytes."""

    _relative_path(relative_path, "replay artifact path")
    _sha256(expected_sha256, "replay artifact sha256")
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise ProtocolError("secure no-follow artifact replay is unavailable on this host")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fds: list[int] = []
    try:
        current = os.open(Path(root), flags)
        fds.append(current)
        parts = PurePosixPath(relative_path).parts
        for part in parts[:-1]:
            current = os.open(part, flags, dir_fd=current)
            fds.append(current)
        target = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current)
        fds.append(target)
        if not stat.S_ISREG(os.fstat(target).st_mode):
            raise ProtocolError(f"replay artifact is not a regular file: {relative_path}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(target, 64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > 32 * 1024 * 1024:
                raise ProtocolError(f"replay artifact is too large: {relative_path}")
            chunks.append(chunk)
        payload = b"".join(chunks)
    except (OSError, ProtocolError) as exc:
        if isinstance(exc, ProtocolError):
            raise
        raise ProtocolError(f"could not securely read replay artifact {relative_path}: {exc}") from exc
    finally:
        for descriptor in reversed(fds):
            os.close(descriptor)
    actual = "sha256:" + hashlib.sha256(payload).hexdigest()
    if actual != expected_sha256:
        raise ProtocolError(f"replay artifact digest mismatch: {relative_path}")
    return payload


def _read_json_artifact(
    root: Path,
    relative_path: str,
    expected_sha256: str,
    kind: str,
) -> dict[str, Any]:
    payload = _read_artifact_bytes(root, relative_path, expected_sha256)
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"replay artifact is not valid JSON: {relative_path}") from exc
    return validate_contract(kind, value)


def _validate_source_patch_replay(
    root: Path,
    plan: dict[str, Any],
    receipt: dict[str, Any],
    results: dict[str, dict[str, Any]],
) -> None:
    integration = receipt["integration"]
    if integration["mode"] != "isolated_exact_base":
        return
    payload = _read_artifact_bytes(root, integration["patch_ref"], integration["patch_sha256"])
    try:
        patch = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("bounded patch is invalid JSON") from exc
    if not isinstance(patch, dict) or set(patch) != {
        "schema_version", "phase_id", "target_before", "entries"
    } or patch.get("schema_version") != "agent-workflow.bounded-patch.vnext.v1":
        raise ProtocolError("bounded patch contract is invalid")
    if patch["phase_id"] != plan["phase_id"] or patch["target_before"] != integration["target_before"]:
        raise ProtocolError("bounded patch does not bind its phase target baseline")
    if not isinstance(patch["entries"], list):
        raise ProtocolError("bounded patch entries must be a list")
    planned = {task["task_id"]: task for task in plan["tasks"]}
    result_paths: set[str] = set()
    for task_id, result in results.items():
        task = planned[task_id]
        if task["work_mode"] != "write":
            if result["changed_paths"]:
                raise ProtocolError("read task cannot attest source changes")
            continue
        roots = [PurePosixPath(root_value).parts for root_value in task["write_roots"]]
        for path_value in result["changed_paths"]:
            parts = PurePosixPath(path_value).parts
            if not any(parts[: len(root_parts)] == root_parts for root_parts in roots):
                raise ProtocolError("writer result changed_paths escapes its planned roots")
            if path_value in result_paths:
                raise ProtocolError("multiple writer results claim the same changed path")
            result_paths.add(path_value)
        if result["status"] == "completed" and not result["checks"]:
            raise ProtocolError("completed source writer requires host check evidence")
    patch_paths: set[str] = set()
    for index, entry in enumerate(patch["entries"]):
        if not isinstance(entry, dict) or set(entry) != {
            "path", "before_sha256", "before_mode", "after_sha256", "after_mode", "after_base64"
        }:
            raise ProtocolError(f"bounded patch entry {index} is invalid")
        path_value = _relative_path(entry["path"], f"bounded patch entry {index}.path")
        if path_value in patch_paths:
            raise ProtocolError("bounded patch paths must be unique")
        patch_paths.add(path_value)
        before_present = entry["before_sha256"] is not None
        after_present = entry["after_sha256"] is not None
        if before_present:
            _sha256(entry["before_sha256"], f"bounded patch entry {index}.before_sha256")
            _integer(entry["before_mode"], f"bounded patch entry {index}.before_mode")
        elif entry["before_mode"] is not None:
            raise ProtocolError("absent bounded patch before state cannot have a mode")
        if after_present:
            _sha256(entry["after_sha256"], f"bounded patch entry {index}.after_sha256")
            _integer(entry["after_mode"], f"bounded patch entry {index}.after_mode")
            if not isinstance(entry["after_base64"], str):
                raise ProtocolError("bounded patch after state requires payload bytes")
            try:
                after_payload = base64.b64decode(entry["after_base64"], validate=True)
            except (ValueError, TypeError) as exc:
                raise ProtocolError("bounded patch payload is invalid base64") from exc
            if "sha256:" + hashlib.sha256(after_payload).hexdigest() != entry["after_sha256"]:
                raise ProtocolError("bounded patch payload digest mismatch")
        elif entry["after_mode"] is not None or entry["after_base64"] is not None:
            raise ProtocolError("deleted bounded patch state cannot have mode or payload")
        if not before_present and not after_present:
            raise ProtocolError("bounded patch entry cannot be absent before and after")
    if patch_paths != result_paths:
        raise ProtocolError("bounded patch paths do not match task changed_paths")
    if integration["status"] == "applied":
        terminal_path = root / f"runtime/source-write/{plan['phase_id']}/integration-terminal.json"
        if terminal_path.is_symlink() or not terminal_path.is_file():
            raise ProtocolError("applied source phase lacks integration terminal evidence")
        try:
            terminal = json.loads(terminal_path.read_bytes())
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError("integration terminal evidence is invalid JSON") from exc
        if (
            not isinstance(terminal, dict)
            or terminal.get("status") != "applied"
            or terminal.get("phase_id") != plan["phase_id"]
            or terminal.get("patch_ref") != integration["patch_ref"]
            or terminal.get("patch_sha256") != integration["patch_sha256"]
            or terminal.get("target_before") != integration["target_before"]
            or terminal.get("target_after") != integration["target_after"]
        ):
            raise ProtocolError("integration terminal evidence contradicts the phase receipt")


def _validate_read_snapshot_replay(
    root: Path,
    workflow: dict[str, Any],
    plan: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    manifest_ref = f"runtime/read-snapshots/{plan['phase_id']}/manifest.json"
    matching = [
        check
        for check in result["checks"]
        if check["name"] == "host_read_snapshot_audit"
    ]
    if (
        len(matching) != 1
        or matching[0]["exit_code"] != 0
        or matching[0]["evidence_ref"] != manifest_ref
    ):
        raise ProtocolError("verification result lacks one authoritative read snapshot audit")
    payload = _read_artifact_bytes(root, manifest_ref, matching[0]["evidence_sha256"])
    try:
        manifest = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("read snapshot manifest is invalid JSON") from exc
    expected_keys = {
        "schema_version",
        "phase_id",
        "repository",
        "repository_state",
        "repository_state_sha256",
        "read_roots",
        "checkout_ref",
        "files",
        "files_sha256",
    }
    if not isinstance(manifest, dict) or set(manifest) != expected_keys:
        raise ProtocolError("read snapshot manifest contract is invalid")
    if (
        manifest["schema_version"] != "agent-workflow.read-snapshot.vnext.v1"
        or manifest["phase_id"] != plan["phase_id"]
        or manifest["read_roots"] != workflow["admission"]["relevant_roots"]
        or manifest["checkout_ref"]
        != f"runtime/read-snapshots/{plan['phase_id']}/checkout"
        or not isinstance(manifest["repository"], str)
    ):
        raise ProtocolError("read snapshot manifest identity drifted")
    repository_state = _object(manifest["repository_state"], "read-snapshot.repository_state")
    _exact_keys(
        repository_state,
        {
            "head",
            "branch",
            "tracked_diff_sha256",
            "untracked_manifest_sha256",
            "source_state_sha256",
        },
        "read-snapshot.repository_state",
    )
    state_payload = {
        key: repository_state[key]
        for key in ("head", "branch", "tracked_diff_sha256", "untracked_manifest_sha256")
    }
    _text(repository_state["head"], "read-snapshot.repository_state.head")
    if not isinstance(repository_state["branch"], str):
        raise ProtocolError("read-snapshot.repository_state.branch must be a string")
    for key in ("tracked_diff_sha256", "untracked_manifest_sha256", "source_state_sha256"):
        _sha256(repository_state[key], f"read-snapshot.repository_state.{key}")
    _sha256(manifest["repository_state_sha256"], "read-snapshot.repository_state_sha256")
    if (
        repository_state["source_state_sha256"] != _payload_digest(_canonical_bytes(state_payload))
        or manifest["repository_state_sha256"] != repository_state["source_state_sha256"]
    ):
        raise ProtocolError("read snapshot repository state digest drifted")
    files = _object(manifest["files"], "read-snapshot.files")
    _sha256(manifest["files_sha256"], "read-snapshot.files_sha256")
    if manifest["files_sha256"] != _payload_digest(_canonical_bytes(files)):
        raise ProtocolError("read snapshot file manifest digest drifted")
    checkout = root / manifest["checkout_ref"]
    if checkout.is_symlink() or not checkout.is_dir():
        raise ProtocolError("read snapshot checkout is missing or unsafe")
    observed: dict[str, dict[str, Any]] = {}
    for path in sorted(checkout.rglob("*")):
        relative = path.relative_to(checkout).as_posix()
        if path.is_symlink():
            raise ProtocolError("read snapshot contains a symlink")
        metadata = path.stat()
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink > 1:
            raise ProtocolError("read snapshot contains an unsafe file")
        parts = PurePosixPath(relative).parts
        if any(part.casefold() in _RESERVED_WRITE_ROOTS for part in parts):
            raise ProtocolError("read snapshot contains a control-plane path")
        observed[relative] = {
            "sha256": _payload_digest(path.read_bytes()),
            "mode": stat.S_IMODE(metadata.st_mode),
        }
    if observed != files:
        raise ProtocolError("read snapshot checkout bytes drifted from its manifest")
    return manifest


def validate_host_validation_receipt(
    root: Path,
    receipt_ref: str,
    receipt_sha256: str,
    *,
    workflow_id: str | None = None,
    authority_revision: int | None = None,
    require_pass: bool = False,
) -> dict[str, Any]:
    """Replay one typed host-validation receipt and all referenced command logs."""

    payload = _read_artifact_bytes(root, receipt_ref, receipt_sha256)
    try:
        receipt = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("host-validation receipt is invalid JSON") from exc
    expected = {
        "schema_version",
        "workflow_id",
        "authority_revision",
        "validation_id",
        "status",
        "spec_sha256",
        "integration_receipt_ref",
        "integration_receipt_sha256",
        "cwd",
        "environment",
        "started_at",
        "finished_at",
        "repository_before",
        "repository_after",
        "repository_unchanged",
        "commands",
    }
    if not isinstance(receipt, dict) or set(receipt) != expected:
        raise ProtocolError("host-validation receipt contract is invalid")
    if receipt["schema_version"] != "agent-workflow.host-validation-receipt.v1":
        raise ProtocolError("host-validation receipt schema is invalid")
    _slug(receipt["workflow_id"], "host-validation.workflow_id")
    _integer(receipt["authority_revision"], "host-validation.authority_revision", minimum=1)
    _slug(receipt["validation_id"], "host-validation.validation_id")
    _sha256(receipt["spec_sha256"], "host-validation.spec_sha256")
    _relative_path(receipt["integration_receipt_ref"], "host-validation.integration_receipt_ref")
    _sha256(receipt["integration_receipt_sha256"], "host-validation.integration_receipt_sha256")
    _text(receipt["cwd"], "host-validation.cwd")
    if workflow_id is not None and receipt["workflow_id"] != workflow_id:
        raise ProtocolError("host-validation workflow identity drifted")
    if authority_revision is not None and receipt["authority_revision"] != authority_revision:
        raise ProtocolError("host-validation authority revision drifted")

    environment = _object(receipt["environment"], "host-validation.environment")
    if not environment or any(
        not isinstance(key, str)
        or not key
        or not isinstance(value, str)
        or len(value) > 4096
        for key, value in environment.items()
    ):
        raise ProtocolError("host-validation environment is invalid")

    def validate_repository(value: Any, label: str) -> dict[str, Any]:
        evidence = _object(value, label)
        _exact_keys(
            evidence,
            {
                "head",
                "branch",
                "tracked_diff_sha256",
                "untracked_manifest_sha256",
                "source_state_sha256",
            },
            label,
        )
        _text(evidence["head"], f"{label}.head")
        if not isinstance(evidence["branch"], str):
            raise ProtocolError(f"{label}.branch must be a string")
        _sha256(evidence["tracked_diff_sha256"], f"{label}.tracked_diff_sha256")
        _sha256(evidence["untracked_manifest_sha256"], f"{label}.untracked_manifest_sha256")
        _sha256(evidence["source_state_sha256"], f"{label}.source_state_sha256")
        state_payload = {
            key: evidence[key]
            for key in ("head", "branch", "tracked_diff_sha256", "untracked_manifest_sha256")
        }
        if evidence["source_state_sha256"] != _payload_digest(_canonical_bytes(state_payload)):
            raise ProtocolError(f"{label}.source_state_sha256 drifted")
        return evidence

    before = validate_repository(receipt["repository_before"], "host-validation.repository_before")
    after = validate_repository(receipt["repository_after"], "host-validation.repository_after")
    if not isinstance(receipt["repository_unchanged"], bool):
        raise ProtocolError("host-validation repository_unchanged must be boolean")
    if receipt["repository_unchanged"] != (before == after):
        raise ProtocolError("host-validation repository stability claim is invalid")

    started = _parsed_rfc3339(receipt["started_at"], "host-validation.started_at")
    finished = _parsed_rfc3339(receipt["finished_at"], "host-validation.finished_at")
    if finished < started:
        raise ProtocolError("host-validation timestamps are inverted")
    commands = receipt["commands"]
    if not isinstance(commands, list) or not commands or len(commands) > 16:
        raise ProtocolError("host-validation commands are invalid")
    command_ids: set[str] = set()
    all_pass = True
    for index, raw in enumerate(commands):
        label = f"host-validation.commands[{index}]"
        command = _object(raw, label)
        _exact_keys(
            command,
            {
                "id",
                "argv",
                "argv_sha256",
                "executable_sha256",
                "timeout_seconds",
                "started_at",
                "finished_at",
                "elapsed_ms",
                "exit_code",
                "timed_out",
                "stdout_ref",
                "stdout_sha256",
                "stderr_ref",
                "stderr_sha256",
            },
            label,
        )
        command_id = _slug(command["id"], f"{label}.id")
        if command_id in command_ids:
            raise ProtocolError("host-validation command ids must be unique")
        command_ids.add(command_id)
        argv = command["argv"]
        if not isinstance(argv, list) or not argv or not all(
            isinstance(item, str) and item and len(item) <= 8192 for item in argv
        ):
            raise ProtocolError(f"{label}.argv is invalid")
        if not Path(argv[0]).is_absolute():
            raise ProtocolError(f"{label}.argv executable must be absolute")
        _sha256(command["argv_sha256"], f"{label}.argv_sha256")
        if command["argv_sha256"] != _payload_digest(_canonical_bytes(argv)):
            raise ProtocolError(f"{label}.argv digest drifted")
        _sha256(command["executable_sha256"], f"{label}.executable_sha256")
        timeout_seconds = _integer(
            command["timeout_seconds"], f"{label}.timeout_seconds", minimum=1
        )
        if timeout_seconds > 3600:
            raise ProtocolError(f"{label}.timeout_seconds exceeds the contract maximum")
        command_started = _parsed_rfc3339(command["started_at"], f"{label}.started_at")
        command_finished = _parsed_rfc3339(command["finished_at"], f"{label}.finished_at")
        if command_started < started or command_finished < command_started or command_finished > finished:
            raise ProtocolError(f"{label} chronology is invalid")
        _integer(command["elapsed_ms"], f"{label}.elapsed_ms")
        if not isinstance(command["exit_code"], int) or isinstance(command["exit_code"], bool):
            raise ProtocolError(f"{label}.exit_code must be an integer")
        if not isinstance(command["timed_out"], bool):
            raise ProtocolError(f"{label}.timed_out must be boolean")
        for stream in ("stdout", "stderr"):
            ref = _relative_path(command[f"{stream}_ref"], f"{label}.{stream}_ref")
            digest = _sha256(command[f"{stream}_sha256"], f"{label}.{stream}_sha256")
            _read_artifact_bytes(root, ref, digest)
        if command["exit_code"] != 0 or command["timed_out"]:
            all_pass = False
    derived_status = "pass" if all_pass and receipt["repository_unchanged"] else "fail"
    if receipt["status"] != derived_status:
        raise ProtocolError("host-validation status does not match its evidence")
    if require_pass and receipt["status"] != "pass":
        raise ProtocolError("host-validation receipt did not pass")
    return receipt


def _validate_verification_decision(
    root: Path,
    workflow: dict[str, Any],
    plan: dict[str, Any],
    result: dict[str, Any],
    final: dict[str, Any],
    expected_integration_ref: str,
    expected_integration_sha256: str,
    expected_source_state_sha256: str,
) -> None:
    if result["output_ref"] is None:
        raise ProtocolError("verification result requires a decision output")
    payload = _read_artifact_bytes(root, result["output_ref"], result["output_sha256"])
    try:
        decision = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("verification decision is invalid JSON") from exc
    expected = {
        "schema_version",
        "workflow_id",
        "phase_id",
        "task_id",
        "decision",
        "confidence",
        "criteria",
        "findings",
        "commands",
    }
    if not isinstance(decision, dict) or set(decision) != expected:
        raise ProtocolError("verification decision contract is invalid")
    if (
        decision["schema_version"] != "agent-workflow.verification-decision.vnext.v1"
        or decision["workflow_id"] != workflow["workflow_id"]
        or decision["phase_id"] != plan["phase_id"]
        or decision["task_id"] != result["task_id"]
    ):
        raise ProtocolError("verification decision identity is invalid")
    if decision["decision"] not in {"pass", "revise", "blocked", "human_gate"}:
        raise ProtocolError("verification decision outcome is invalid")
    if decision["confidence"] not in {"high", "medium", "low"}:
        raise ProtocolError("verification decision confidence is invalid")

    allowed_evidence = set(result["evidence_refs"])
    allowed_evidence.update(item["evidence_ref"] for item in result["checks"])
    verification_tasks = [
        task for task in plan["tasks"] if task["task_id"] == result["task_id"]
    ]
    if len(verification_tasks) != 1:
        raise ProtocolError("verification result has no unique planned task")
    verifier_inputs = verification_tasks[0]["input_refs"]
    allowed_evidence.update(verifier_inputs)
    host_validation_commands: dict[str, list[dict[str, Any]]] = {}
    for input_ref in verifier_inputs:
        if not re.fullmatch(r"evidence/host-validations/[^/]+/receipt\.json", input_ref):
            continue
        receipt = validate_host_validation_receipt(
            root,
            input_ref,
            verification_tasks[0]["input_sha256"][input_ref],
            workflow_id=workflow["workflow_id"],
            authority_revision=plan["authority_revision"],
            require_pass=True,
        )
        integration_ref = receipt["integration_receipt_ref"]
        integration_sha256 = receipt["integration_receipt_sha256"]
        if (
            integration_ref != expected_integration_ref
            or integration_sha256 != expected_integration_sha256
            or verification_tasks[0]["input_sha256"].get(integration_ref)
            != integration_sha256
            or final["phase_receipt_sha256"].get(integration_ref)
            != integration_sha256
        ):
            raise ProtocolError("host-validation receipt does not bind the latest authoritative integration")
        if receipt["repository_after"]["source_state_sha256"] != expected_source_state_sha256:
            raise ProtocolError("host-validation receipt is stale relative to the verifier snapshot")
        integration = _read_json_artifact(
            root,
            integration_ref,
            integration_sha256,
            "phase-receipt",
        )
        if (
            integration["workflow_id"] != workflow["workflow_id"]
            or integration["integration"]["mode"] != "isolated_exact_base"
            or integration["integration"]["status"] != "applied"
        ):
            raise ProtocolError("host-validation receipt requires an applied authoritative integration")
        host_validation_commands[input_ref] = receipt["commands"]
    criteria = decision["criteria"]
    expected_criteria = {item["id"] for item in workflow["success_criteria"]}
    observed_criteria: dict[str, str] = {}
    if not isinstance(criteria, list):
        raise ProtocolError("verification criteria coverage must be a list")
    for index, item in enumerate(criteria):
        if not isinstance(item, dict) or set(item) != {
            "criterion_id",
            "status",
            "evidence_refs",
        }:
            raise ProtocolError(f"verification criteria coverage {index} is invalid")
        criterion_id = item["criterion_id"]
        if (
            criterion_id in observed_criteria
            or criterion_id not in expected_criteria
            or item["status"] not in {"pass", "fail", "blocked"}
            or not isinstance(item["evidence_refs"], list)
            or not item["evidence_refs"]
            or not set(item["evidence_refs"]) <= allowed_evidence
        ):
            raise ProtocolError("verification criteria coverage is invalid")
        observed_criteria[criterion_id] = item["status"]
    if set(observed_criteria) != expected_criteria:
        raise ProtocolError("verification criteria coverage is incomplete")

    findings = decision["findings"]
    if not isinstance(findings, list):
        raise ProtocolError("verification findings must be a list")
    finding_ids: set[str] = set()
    severities: dict[str, str] = {}
    for index, item in enumerate(findings):
        if not isinstance(item, dict) or set(item) != {
            "finding_id",
            "severity",
            "summary",
            "evidence_refs",
        }:
            raise ProtocolError(f"verification finding {index} is invalid")
        finding_id = item["finding_id"]
        if (
            not isinstance(finding_id, str)
            or not finding_id
            or finding_id in finding_ids
            or item["severity"] not in {"P0", "P1", "P2", "P3"}
            or not isinstance(item["summary"], str)
            or not item["summary"].strip()
            or not isinstance(item["evidence_refs"], list)
            or not set(item["evidence_refs"]) <= allowed_evidence
        ):
            raise ProtocolError("verification finding is invalid")
        finding_ids.add(finding_id)
        severities[finding_id] = item["severity"]

    commands = decision["commands"]
    if not isinstance(commands, list) or not commands:
        raise ProtocolError("verification decision requires command evidence")
    for index, item in enumerate(commands):
        if not isinstance(item, dict) or set(item) != {
            "command",
            "exit_code",
            "evidence_ref",
        }:
            raise ProtocolError(f"verification command {index} is invalid")
        matching_commands = host_validation_commands.get(item.get("evidence_ref"), [])
        matching = [
            command
            for command in matching_commands
            if shlex.join(command["argv"]) == item.get("command")
            and command["exit_code"] == item.get("exit_code")
        ]
        if (
            not isinstance(item["command"], str)
            or not item["command"].strip()
            or not isinstance(item["exit_code"], int)
            or isinstance(item["exit_code"], bool)
            or len(matching) != 1
        ):
            raise ProtocolError("verification command requires one matching passed host-validation receipt")

    if final["status"] == "complete":
        if (
            decision["decision"] != "pass"
            or decision["confidence"] != "high"
            or any(status != "pass" for status in observed_criteria.values())
            or any(severity in {"P0", "P1"} for severity in severities.values())
        ):
            raise ProtocolError("complete final requires a high-confidence passing verification")
        p2_findings = {finding_id for finding_id, severity in severities.items() if severity == "P2"}
        p2_resolutions = {item["finding_id"] for item in final["p2_resolutions"]}
        if p2_findings != p2_resolutions:
            raise ProtocolError("verification P2 findings do not match final typed resolutions")


def validate_replay(
    root: Path,
    *,
    workflow_sha256: str,
    final_sha256: str | None = None,
    final_value: dict[str, Any] | None = None,
    metrics_out: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Replay the digest-bound lifecycle chain and fail closed on any drift."""

    root = Path(root)
    workflow = _read_json_artifact(root, "workflow.json", workflow_sha256, "workflow")
    if (final_sha256 is None) == (final_value is None):
        raise ProtocolError("replay requires exactly one sealed final or candidate final")
    final = (
        validate_contract("final", final_value)
        if final_value is not None
        else _read_json_artifact(root, "final.json", final_sha256, "final")
    )
    if final["workflow_id"] != workflow["workflow_id"]:
        raise ProtocolError("final workflow id does not match workflow seal")
    baseline_payload = _read_artifact_bytes(root, workflow["baseline_ref"], workflow["baseline_sha256"])
    try:
        baseline = json.loads(baseline_payload)
        verify_baseline(baseline)
    except (UnicodeDecodeError, json.JSONDecodeError, BaselineError) as exc:
        raise ProtocolError("workflow baseline is not a valid replayable baseline") from exc
    if repository_evidence(baseline) != workflow["admission"]["repository"]:
        raise ProtocolError("workflow admission repository evidence does not match baseline")
    if baseline["baseline_kind"] == "candidate_gate":
        parent_ref = baseline["candidate_parent"]
        parent_payload = _read_artifact_bytes(root, parent_ref["path"], parent_ref["sha256"])
        try:
            parent = json.loads(parent_payload)
            verify_candidate_against_parent(baseline, parent)
        except (UnicodeDecodeError, json.JSONDecodeError, BaselineError) as exc:
            raise ProtocolError("candidate baseline parent is invalid") from exc
    for capability in workflow["admission"]["capabilities"].values():
        _read_artifact_bytes(root, capability["evidence_ref"], capability["evidence_sha256"])

    amendments_by_revision: dict[int, tuple[str, dict[str, Any]]] = {}
    for amendment_ref in final["amendment_refs"]:
        payload = _read_artifact_bytes(
            root,
            amendment_ref,
            final["amendment_sha256"][amendment_ref],
        )
        try:
            amendment = json.loads(payload)
            validate_sidecar("amendment", amendment)
        except (UnicodeDecodeError, json.JSONDecodeError, ProtocolError) as exc:
            raise ProtocolError("final amendment evidence is invalid") from exc
        if amendment["workflow_id"] != workflow["workflow_id"]:
            raise ProtocolError("amendment workflow id does not match workflow seal")
        revision = amendment["authority_revision"]
        if revision in amendments_by_revision:
            raise ProtocolError("final amendments contain a duplicate authority revision")
        amendments_by_revision[revision] = (amendment_ref, amendment)

    lineage_claims: dict[str, tuple[dict[str, Any], bytes]] = {}
    for claim_ref in final["lineage_claim_refs"]:
        payload = _read_artifact_bytes(
            root,
            claim_ref,
            final["lineage_claim_sha256"][claim_ref],
        )
        try:
            claim = json.loads(payload)
            validate_sidecar("lineage-claim", claim)
        except (UnicodeDecodeError, json.JSONDecodeError, ProtocolError) as exc:
            raise ProtocolError("final lineage claim evidence is invalid") from exc
        if claim["workflow_id"] != workflow["workflow_id"]:
            raise ProtocolError("lineage claim workflow id does not match workflow seal")
        expected_ref = f"lineages/{claim['lineage_id']}/{claim['claim_kind']}.json"
        if claim_ref != expected_ref:
            raise ProtocolError("lineage claim path does not match its identity")
        lineage_claims[claim_ref] = (claim, payload)

    criteria = {item["id"]: 1 for item in workflow["success_criteria"]}
    current_authority_revision = workflow["authority"]["revision"]
    consumed_amendments: set[int] = set()
    prior_results_by_ref: dict[str, tuple[dict[str, Any], str]] = {}
    prior_results_by_lineage: dict[str, list[tuple[str, dict[str, Any], str]]] = {}
    consumed_lineage_claims: set[str] = set()
    phase_records: dict[str, tuple[dict[str, Any], dict[str, Any], str]] = {}
    phase_results: dict[str, dict[str, dict[str, Any]]] = {}
    prior_phase_ids: set[str] = set()
    immediately_prior_phase_id: str | None = None
    writer_task_ids: set[str] = set()
    writer_lineage_ids: set[str] = set()
    predecessor_digest = workflow["baseline_sha256"]
    aggregate_tokens = 0
    aggregate_input_tokens = 0
    aggregate_output_tokens = 0
    accounting_exact = True
    immediately_prior_receipt_ref: str | None = None
    immediately_prior_receipt_digest: str | None = None

    for receipt_ref in final["phase_receipt_refs"]:
        receipt_digest = final["phase_receipt_sha256"][receipt_ref]
        receipt = _read_json_artifact(root, receipt_ref, receipt_digest, "phase-receipt")
        if receipt["workflow_id"] != workflow["workflow_id"]:
            raise ProtocolError("phase receipt workflow id mismatch")
        plan_ref = f"phases/{receipt['phase_id']}/plan.json"
        plan = _read_json_artifact(root, plan_ref, receipt["plan_sha256"], "phase-plan")
        if plan["phase_id"] != receipt["phase_id"] or plan["generation_id"] != receipt["generation_id"]:
            raise ProtocolError("phase plan identity does not match receipt")
        claim_payload = _read_artifact_bytes(
            root,
            receipt["generation_claim_ref"],
            receipt["generation_claim_sha256"],
        )
        try:
            generation_claim = json.loads(claim_payload)
            validate_sidecar("generation-claim", generation_claim)
        except (UnicodeDecodeError, json.JSONDecodeError, ProtocolError) as exc:
            raise ProtocolError("phase generation claim is invalid") from exc
        expected_contention_key = _payload_digest(
            _canonical_bytes(
                {
                    "predecessor_sha256": plan["predecessor_sha256"],
                    "authority_revision": plan["authority_revision"],
                }
            )
        )
        if generation_claim != {
            "schema_version": "agent-workflow.generation-claim.vnext.v1",
            "workflow_id": workflow["workflow_id"],
            "generation_id": plan["generation_id"],
            "phase_id": plan["phase_id"],
            "predecessor_sha256": plan["predecessor_sha256"],
            "authority_revision": plan["authority_revision"],
            "plan_sha256": receipt["plan_sha256"],
            "contention_key": expected_contention_key,
        }:
            raise ProtocolError("phase generation claim does not bind the exact plan authority")
        while current_authority_revision + 1 in amendments_by_revision:
            revision = current_authority_revision + 1
            _, amendment = amendments_by_revision[revision]
            if amendment["previous_authority_revision"] != current_authority_revision:
                raise ProtocolError("amendment authority chain is discontinuous")
            if amendment["amendment_kind"] == "instruction":
                if amendment["applies_after_ref"] != immediately_prior_receipt_ref:
                    break
                if amendment["applies_after_sha256"] != immediately_prior_receipt_digest:
                    raise ProtocolError("instruction amendment boundary digest drifted")
            else:
                blocked = prior_results_by_ref.get(amendment["blocked_result_ref"])
                if blocked is None:
                    break
                if blocked[1] != amendment["blocked_result_sha256"] or blocked[0]["status"] == "completed":
                    raise ProtocolError("criterion amendment does not bind a prior blocked result")
                criterion_id = amendment["criterion_id"]
                if criteria.get(criterion_id) != amendment["from_revision"]:
                    raise ProtocolError("criterion amendment revision chain is discontinuous")
                criteria[criterion_id] = amendment["to_revision"]
            _read_artifact_bytes(
                root,
                amendment["user_instruction_ref"],
                amendment["user_instruction_sha256"],
            )
            current_authority_revision = revision
            consumed_amendments.add(revision)
        if plan["authority_revision"] != current_authority_revision:
            raise ProtocolError("phase authority revision does not match effective amendment authority")
        if plan["predecessor_sha256"] != receipt["predecessor_sha256"]:
            raise ProtocolError("phase predecessor digest mismatch")
        if plan["predecessor_sha256"] != predecessor_digest:
            raise ProtocolError("phase predecessor is not the immediately prior authoritative seal")
        if not set(plan["caused_by"]) <= prior_phase_ids:
            raise ProtocolError("phase caused_by references a missing or future phase")
        if bool(prior_phase_ids) != bool(plan["caused_by"]):
            raise ProtocolError("non-initial phases require an explicit prior cause")
        if immediately_prior_phase_id is not None and plan["caused_by"][-1] != immediately_prior_phase_id:
            raise ProtocolError("phase final cause is not the immediately prior authoritative phase")

        planned = {task["task_id"]: task for task in plan["tasks"]}
        has_write_tasks = any(task["work_mode"] == "write" for task in plan["tasks"])
        uses_isolated_integration = receipt["integration"]["mode"] == "isolated_exact_base"
        if has_write_tasks != uses_isolated_integration:
            raise ProtocolError("phase work mode does not match receipt integration mode")
        results: dict[str, dict[str, Any]] = {}
        for task in plan["tasks"]:
            if task["criterion_id"] not in criteria:
                raise ProtocolError("phase task references an unknown success criterion")
            if task["criterion_revision"] != criteria[task["criterion_id"]]:
                raise ProtocolError("phase task criterion revision is not effective")
            lineage_id = task["lineage_id"]
            origin_ref = f"lineages/{lineage_id}/origin.json"
            origin_entry = lineage_claims.get(origin_ref)
            if origin_entry is None:
                raise ProtocolError("phase task is missing its exact lineage claim")
            origin, origin_payload = origin_entry
            expected_origin = {
                "schema_version": "agent-workflow.lineage-claim.vnext.v1",
                "claim_kind": "origin",
                "workflow_id": workflow["workflow_id"],
                "lineage_id": lineage_id,
                "criterion_id": task["criterion_id"],
                "criterion_revision": task["criterion_revision"],
                "role": task["role"],
                "scope_sha256": _payload_digest(
                    _canonical_bytes(
                        {
                            "criterion_id": task["criterion_id"],
                            "role": task["role"],
                            "work_mode": task["work_mode"],
                            "write_roots": sorted(task["write_roots"]),
                        }
                    )
                ),
                "origin_phase_id": plan["phase_id"],
                "origin_task_id": task["task_id"],
            }
            prior_lineage_results = prior_results_by_lineage.get(lineage_id, [])
            if not prior_lineage_results:
                if origin != expected_origin:
                    raise ProtocolError("lineage origin claim does not bind its first task")
            else:
                recovery_ref = f"lineages/{lineage_id}/recovery.json"
                recovery_entry = lineage_claims.get(recovery_ref)
                if recovery_entry is None:
                    raise ProtocolError("recovery task is missing its exact lineage claim")
                recovery, _ = recovery_entry
                failed_ref, failed, failed_sha256 = prior_lineage_results[-1]
                if failed["status"] == "completed":
                    raise ProtocolError("successful lineage cannot be replayed as recovery")
                if immediately_prior_receipt_ref is None:
                    raise ProtocolError("recovery task lacks a causal receipt boundary")
                if (
                    failed_ref not in task["input_refs"]
                    or task["input_sha256"].get(failed_ref) != failed_sha256
                    or immediately_prior_receipt_ref not in task["input_refs"]
                    or task["input_sha256"].get(immediately_prior_receipt_ref)
                    != immediately_prior_receipt_digest
                ):
                    raise ProtocolError("recovery task does not consume exact failed and causal evidence")
                automatic = (
                    failed["status"] == "failed"
                    and failed["terminal_reason"] == "runner_error"
                    and not failed["changed_paths"]
                )
                expected_recovery = {
                    "schema_version": "agent-workflow.lineage-claim.vnext.v1",
                    "claim_kind": "recovery",
                    "workflow_id": workflow["workflow_id"],
                    "lineage_id": lineage_id,
                    "origin_ref": origin_ref,
                    "origin_sha256": _payload_digest(origin_payload),
                    "failed_result_ref": failed_ref,
                    "failed_result_sha256": failed_sha256,
                    "recovery_phase_id": plan["phase_id"],
                    "recovery_task_id": task["task_id"],
                    "recovery_scope_sha256": _payload_digest(
                        _canonical_bytes(
                            {
                                "criterion_id": task["criterion_id"],
                                "role": task["role"],
                                "work_mode": task["work_mode"],
                                "write_roots": sorted(task["write_roots"]),
                            }
                        )
                    ),
                    "criterion_id": task["criterion_id"],
                    "criterion_revision": task["criterion_revision"],
                    "authority_revision": plan["authority_revision"],
                    "recovery_kind": (
                        "automatic_infra_retry" if automatic else "evidence_aware_repair"
                    ),
                }
                if recovery != expected_recovery:
                    raise ProtocolError("lineage recovery claim does not bind the exact repair")
                consumed_lineage_claims.add(recovery_ref)
            consumed_lineage_claims.add(origin_ref)
            _read_artifact_bytes(root, task["packet_path"], task["packet_sha256"])
            for input_ref in task["input_refs"]:
                _read_artifact_bytes(root, input_ref, task["input_sha256"][input_ref])
            if task["work_mode"] == "write":
                writer_task_ids.add(task["task_id"])
                writer_lineage_ids.add(task["lineage_id"])

        for result_ref in receipt["task_result_refs"]:
            result = _read_json_artifact(
                root,
                result_ref,
                receipt["task_result_sha256"][result_ref],
                "task-result",
            )
            task = planned.get(result["task_id"])
            if task is None or result["task_id"] in results:
                raise ProtocolError("phase receipt result set does not match plan")
            if (
                result["workflow_id"] != workflow["workflow_id"]
                or result["phase_id"] != plan["phase_id"]
                or result["lineage_id"] != task["lineage_id"]
            ):
                raise ProtocolError("task result identity does not match phase plan")
            route = result["actual_route"]
            if route is not None:
                expected_model = workflow["routing"][f"{task['role']}_model"]
                route_matches = (
                    route["model"] != expected_model
                    or route["reasoning_effort"] != workflow["routing"]["reasoning_effort"]
                )
                if route_matches and not (
                    result["status"] == "route_attestation_failed"
                    and result["terminal_reason"] == "route_mismatch"
                ):
                    raise ProtocolError("task result route does not match admitted role")
                if not route_matches and (
                    result["status"] == "route_attestation_failed"
                    and result["terminal_reason"] == "route_mismatch"
                ):
                    raise ProtocolError("route_mismatch result contains the admitted route")
            if route is not None:
                _read_artifact_bytes(root, route["attestation_ref"], route["attestation_sha256"])
            if result["output_ref"] is not None:
                _read_artifact_bytes(root, result["output_ref"], result["output_sha256"])
            for evidence_ref in result["evidence_refs"]:
                _read_artifact_bytes(root, evidence_ref, result["evidence_sha256"][evidence_ref])
            for check in result["checks"]:
                _read_artifact_bytes(root, check["evidence_ref"], check["evidence_sha256"])
            if result["token_usage"]["confidence"] == "exact":
                aggregate_tokens += result["token_usage"]["total"]
                aggregate_input_tokens += result["token_usage"]["input"]
                aggregate_output_tokens += result["token_usage"]["output"]
            else:
                accounting_exact = False
            results[result["task_id"]] = result
            prior_results_by_ref[result_ref] = (result, receipt["task_result_sha256"][result_ref])
            prior_results_by_lineage.setdefault(result["lineage_id"], []).append(
                (result_ref, result, receipt["task_result_sha256"][result_ref])
            )
        if set(results) != set(planned):
            raise ProtocolError("phase receipt must contain exactly one result per planned task")
        observed_counts = {name: 0 for name in TASK_TERMINAL_STATUSES}
        for result in results.values():
            observed_counts[result["status"]] += 1
        for status, count in observed_counts.items():
            if receipt["task_counts"][status] != count:
                raise ProtocolError("phase receipt counts do not match task results")
        if receipt["integration"]["patch_ref"] is not None:
            _validate_source_patch_replay(root, plan, receipt, results)
        phase_records[receipt_ref] = (plan, receipt, receipt_digest)
        phase_results[receipt_ref] = results
        prior_phase_ids.add(plan["phase_id"])
        immediately_prior_phase_id = plan["phase_id"]
        immediately_prior_receipt_ref = receipt_ref
        immediately_prior_receipt_digest = receipt_digest
        predecessor_digest = receipt_digest

    if consumed_amendments != set(amendments_by_revision):
        raise ProtocolError("final contains amendments that never became effective")
    if consumed_lineage_claims != set(lineage_claims):
        raise ProtocolError("final lineage claim set does not match authoritative tasks")

    verification_ref = final["verification_ref"]
    if final["status"] == "complete":
        if verification_ref != final["phase_receipt_refs"][-1]:
            raise ProtocolError("complete final requires the latest phase to be verification")
        if verification_ref not in phase_records:
            raise ProtocolError("verification_ref must identify an authoritative phase receipt")
        if final["verification_sha256"] != phase_records[verification_ref][2]:
            raise ProtocolError("verification receipt digest does not match final seal")
        verifier_plan = phase_records[verification_ref][0]
        if len(verifier_plan["tasks"]) != 1:
            raise ProtocolError("verification phase must contain exactly one independent top task")
        for task in verifier_plan["tasks"]:
            if task["role"] != "top" or task["work_mode"] != "read":
                raise ProtocolError("verification phase must use clean read-only top tasks")
            if task["task_id"] in writer_task_ids or task["lineage_id"] in writer_lineage_ids:
                raise ProtocolError("verification identity cannot equal a writer identity")
        if phase_records[verification_ref][1]["status"] != "completed":
            raise ProtocolError("verification phase must complete successfully")
        if phase_records[verification_ref][1]["generation_id"] != final["generation_id"]:
            raise ProtocolError("final generation must own the verification phase")
        verifier_result = phase_results[verification_ref][verifier_plan["tasks"][0]["task_id"]]
        verifier_snapshot = _validate_read_snapshot_replay(
            root,
            workflow,
            verifier_plan,
            verifier_result,
        )
        verifier_route = verifier_result["actual_route"]
        prior_session_ids = {
            result["actual_route"]["session_id"]
            for phase_ref, results in phase_results.items()
            if phase_ref != verification_ref
            for result in results.values()
            if result["actual_route"] is not None
        }
        if verifier_route is None or verifier_route["session_id"] in prior_session_ids:
            raise ProtocolError("verification session identity is not independent")
        prior_applied_integrations = [
            (phase_ref, record[2])
            for phase_ref, record in phase_records.items()
            if phase_ref != verification_ref
            and record[1]["integration"]["status"] == "applied"
        ]
        if not prior_applied_integrations:
            raise ProtocolError("complete verification lacks an applied source integration")
        latest_integration_ref, latest_integration_sha256 = prior_applied_integrations[-1]
        _validate_verification_decision(
            root,
            workflow,
            verifier_plan,
            verifier_result,
            final,
            latest_integration_ref,
            latest_integration_sha256,
            verifier_snapshot["repository_state_sha256"],
        )

    _read_artifact_bytes(root, final["final_report_ref"], final["final_report_sha256"])
    for resolution in final["p2_resolutions"]:
        if resolution["evidence_ref"] is not None:
            _read_artifact_bytes(root, resolution["evidence_ref"], resolution["evidence_sha256"])
    if final["accounting"]["coverage"] == "exact":
        if not accounting_exact or final["accounting"]["workflow_tokens"] < aggregate_tokens:
            raise ProtocolError("final accounting does not cover exact external task usage")
    if final["runtime_bundle_sha256"] != workflow["runtime_bundle"]["sha256"]:
        raise ProtocolError("final runtime bundle does not match admitted workflow bundle")
    if metrics_out is not None:
        metrics_out.update({
            "external_input_tokens": aggregate_input_tokens if accounting_exact else None,
            "external_output_tokens": aggregate_output_tokens if accounting_exact else None,
            "external_total_tokens": aggregate_tokens if accounting_exact else None,
            "external_accounting_exact": accounting_exact,
        })
    return final


def validate_replay_candidate(
    root: Path,
    *,
    workflow_sha256: str,
    final: dict[str, Any],
) -> dict[str, Any]:
    """Validate an uncommitted final candidate before create-once publication."""

    return validate_replay(
        root,
        workflow_sha256=workflow_sha256,
        final_value=final,
    )
