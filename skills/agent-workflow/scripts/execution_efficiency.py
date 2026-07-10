#!/usr/bin/env python3
"""Portable execution-efficiency contracts for Agent Workflow."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any


POLICY_SCHEMA = "agent-workflow.execution-efficiency.v1"
LANE_SCHEMA = "agent-workflow.lane-execution.v1"
RECEIPT_SCHEMA = "agent-workflow.lane-receipt.v1"
INDEX_SCHEMA = "agent-workflow.integration-index.v1"

NATIVE_RUNNERS = {
    "codex_builtin_subagents",
    "claude_code_builtin_subagents",
}
RISK_CLASSES = {"low", "medium", "high", "judgment"}
WAIT_OUTCOMES = {"completed", "timeout", "error"}
EVENT_CARD_REASONS = {
    "preview",
    "dispatch",
    "phase_terminal",
    "material_failure",
    "gate",
    "round_transition",
    "final_stop",
}
LANE_TERMINAL_STATUSES = {"complete", "skipped", "blocked", "invalid_output"}
GATE_DECISIONS = {
    "pass",
    "revise",
    "more_discovery",
    "challenge",
    "second_opinion",
    "human_gate",
    "blocked",
}
SEVERITIES = {"P0", "P1", "P2", "P3"}
RECEIPT_PAYLOAD_KEYS = {
    "discover_payload.v1": (
        "sources_read",
        "current_state",
        "constraints",
        "unknowns",
        "risks",
        "recommended_next_lanes",
    ),
    "plan_payload.v1": (
        "approach",
        "work_slices",
        "dependencies",
        "approval_gates",
        "recommended_next_lanes",
    ),
    "roundtable_payload.v1": (
        "topic",
        "participants",
        "tension_map",
        "rounds",
        "open_questions",
        "decision_options",
        "recommended_next_lanes",
    ),
    "implement_payload.v1": (
        "changes",
        "assumptions",
        "tests_or_checks_run",
        "needs_review",
        "recommended_next_lanes",
    ),
    "seam_payload.v1": (
        "interfaces",
        "ownership_boundaries",
        "integration_risks",
        "adapter_or_contract_changes",
        "recommended_next_lanes",
    ),
    "review_payload.v1": (
        "findings",
        "assumptions_attacked",
        "missing_evidence",
        "repair_packets",
        "recommended_next_lanes",
    ),
    "challenge_payload.v1": (
        "findings",
        "assumptions_attacked",
        "missing_evidence",
        "repair_packets",
        "recommended_next_lanes",
    ),
    "verify_payload.v1": (
        "checks",
        "success_criteria_status",
        "confidence_drivers",
        "remaining_uncertainty",
        "recommended_gate",
    ),
    "repair_payload.v1": (
        "repair_objective",
        "source_findings",
        "changes",
        "checks_run",
        "remaining_risk",
        "recommended_next_lanes",
    ),
    "custom_payload.v1": (),
}


class ExecutionEfficiencyError(ValueError):
    """Raised when an execution-efficiency contract is invalid."""


def canonical_sha256(value: Any, omitted_keys: tuple[str, ...] = ()) -> str:
    candidate = copy.deepcopy(value)
    if isinstance(candidate, dict):
        for key in omitted_keys:
            candidate.pop(key, None)
    payload = json.dumps(
        candidate,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ExecutionEfficiencyError(f"{label} must be an object")
    return value


def _require_keys(value: dict[str, Any], keys: tuple[str, ...], label: str) -> None:
    missing = [key for key in keys if key not in value]
    if missing:
        raise ExecutionEfficiencyError(f"{label} missing keys: {', '.join(missing)}")


def _require_nonempty(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExecutionEfficiencyError(f"{label} must be a non-empty string")
    return value.strip()


def _is_placeholder(value: Any) -> bool:
    return not isinstance(value, str) or not value.strip() or "todo" in value.lower()


def safe_relative_path(value: Any, label: str) -> str:
    text = _require_nonempty(value, label)
    path = Path(text)
    if path.is_absolute() or path.as_posix() == "." or ".." in path.parts:
        raise ExecutionEfficiencyError(f"{label} must be a safe workspace-relative path")
    return path.as_posix()


def build_execution_policy(runner_mode: str) -> dict[str, Any]:
    if runner_mode not in NATIVE_RUNNERS:
        raise ExecutionEfficiencyError(
            "execution efficiency requires a native Codex or Claude Code runner"
        )
    return {
        "schema_version": POLICY_SCHEMA,
        "enabled": True,
        "activation": "explicit_opt_in",
        "risk_class": "medium",
        "context": {
            "default_mode": "isolated",
            "lead_fork_context": False,
            "lane_fork_context": False,
            "parent_transcript": "excluded",
            "prior_lane_outputs": "references_only",
            "input_refs": "digest_bound_files",
        },
        "admission": {
            "lead_owned": ["plan", "integration"],
            "conditional_lanes": ["seam"],
            "deterministic_work": "script_only",
            "duplicate_question_policy": "reject",
        },
        "quality": {
            "write_lanes_require": ["review", "verify"],
            "high_risk_require": ["challenge", "verify"],
            "assessment_identity": "independent_from_writer",
        },
        "wait": {
            "strategy": "notification_first",
            "barrier": "multi_target",
            "max_native_wait_ms": 3600000,
            "min_repoll_ms": 300000,
            "status_polling": False,
            "card_updates": "event_only",
        },
        "budgets": {
            "default_max_tool_turns": 24,
            "default_max_test_runs": 3,
            "max_writer_reuse": 1,
            "on_exhausted": "checkpoint_then_gate",
        },
        "result_transport": {
            "mode": "artifact_receipt",
            "max_inline_chars": 2048,
            "integration_input": "compact_index",
        },
        "rollback": {
            "mode": "disable_policy",
            "artifact_migration_required": False,
        },
    }


def validate_execution_policy(value: Any, runner_mode: str) -> dict[str, Any]:
    policy = _require_object(value, "execution_efficiency")
    _require_keys(
        policy,
        (
            "schema_version",
            "enabled",
            "activation",
            "risk_class",
            "context",
            "admission",
            "quality",
            "wait",
            "budgets",
            "result_transport",
            "rollback",
        ),
        "execution_efficiency",
    )
    if policy["schema_version"] != POLICY_SCHEMA:
        raise ExecutionEfficiencyError(
            f"execution_efficiency.schema_version must be {POLICY_SCHEMA}"
        )
    if policy["enabled"] is not True or policy["activation"] != "explicit_opt_in":
        raise ExecutionEfficiencyError(
            "execution_efficiency must be enabled through explicit_opt_in"
        )
    if runner_mode not in NATIVE_RUNNERS:
        raise ExecutionEfficiencyError(
            "execution_efficiency is supported only by native Codex or Claude Code runners"
        )
    if policy["risk_class"] not in RISK_CLASSES:
        raise ExecutionEfficiencyError(
            f"execution_efficiency.risk_class must be one of {sorted(RISK_CLASSES)}"
        )

    context = _require_object(policy["context"], "execution_efficiency.context")
    if context != {
        "default_mode": "isolated",
        "lead_fork_context": False,
        "lane_fork_context": False,
        "parent_transcript": "excluded",
        "prior_lane_outputs": "references_only",
        "input_refs": "digest_bound_files",
    }:
        raise ExecutionEfficiencyError(
            "execution_efficiency.context must require isolated, reference-only context"
        )

    admission = _require_object(policy["admission"], "execution_efficiency.admission")
    if admission != {
        "lead_owned": ["plan", "integration"],
        "conditional_lanes": ["seam"],
        "deterministic_work": "script_only",
        "duplicate_question_policy": "reject",
    }:
        raise ExecutionEfficiencyError(
            "execution_efficiency.admission must preserve lead-owned planning, conditional seams, and scripted deterministic work"
        )

    quality = _require_object(policy["quality"], "execution_efficiency.quality")
    if quality != {
        "write_lanes_require": ["review", "verify"],
        "high_risk_require": ["challenge", "verify"],
        "assessment_identity": "independent_from_writer",
    }:
        raise ExecutionEfficiencyError(
            "execution_efficiency.quality must preserve review, challenge, and independent verification gates"
        )

    wait = _require_object(policy["wait"], "execution_efficiency.wait")
    _require_keys(
        wait,
        (
            "strategy",
            "barrier",
            "max_native_wait_ms",
            "min_repoll_ms",
            "status_polling",
            "card_updates",
        ),
        "execution_efficiency.wait",
    )
    if wait["strategy"] != "notification_first" or wait["barrier"] != "multi_target":
        raise ExecutionEfficiencyError(
            "execution_efficiency.wait must use notification_first multi_target barriers"
        )
    if wait["status_polling"] is not False:
        raise ExecutionEfficiencyError(
            "execution_efficiency.wait.status_polling must be false"
        )
    if wait["card_updates"] != "event_only":
        raise ExecutionEfficiencyError(
            "execution_efficiency.wait.card_updates must be event_only"
        )
    max_wait = wait["max_native_wait_ms"]
    min_repoll = wait["min_repoll_ms"]
    if not isinstance(max_wait, int) or isinstance(max_wait, bool) or not 300000 <= max_wait <= 3600000:
        raise ExecutionEfficiencyError(
            "execution_efficiency.wait.max_native_wait_ms must be 300000..3600000"
        )
    if not isinstance(min_repoll, int) or isinstance(min_repoll, bool) or min_repoll < 300000:
        raise ExecutionEfficiencyError(
            "execution_efficiency.wait.min_repoll_ms must be >= 300000"
        )

    budgets = _require_object(policy["budgets"], "execution_efficiency.budgets")
    _require_keys(
        budgets,
        (
            "default_max_tool_turns",
            "default_max_test_runs",
            "max_writer_reuse",
            "on_exhausted",
        ),
        "execution_efficiency.budgets",
    )
    for key in ("default_max_tool_turns", "default_max_test_runs"):
        if not isinstance(budgets[key], int) or isinstance(budgets[key], bool) or budgets[key] < 1:
            raise ExecutionEfficiencyError(
                f"execution_efficiency.budgets.{key} must be an integer >= 1"
            )
    if budgets["max_writer_reuse"] != 1:
        raise ExecutionEfficiencyError(
            "execution_efficiency.budgets.max_writer_reuse must be 1"
        )
    if budgets["on_exhausted"] != "checkpoint_then_gate":
        raise ExecutionEfficiencyError(
            "execution_efficiency.budgets.on_exhausted must be checkpoint_then_gate"
        )

    transport = _require_object(
        policy["result_transport"], "execution_efficiency.result_transport"
    )
    if transport.get("mode") != "artifact_receipt" or transport.get("integration_input") != "compact_index":
        raise ExecutionEfficiencyError(
            "execution_efficiency.result_transport must use artifact_receipt and compact_index"
        )
    max_inline = transport.get("max_inline_chars")
    if not isinstance(max_inline, int) or isinstance(max_inline, bool) or not 256 <= max_inline <= 4096:
        raise ExecutionEfficiencyError(
            "execution_efficiency.result_transport.max_inline_chars must be 256..4096"
        )

    rollback = _require_object(policy["rollback"], "execution_efficiency.rollback")
    if rollback != {"mode": "disable_policy", "artifact_migration_required": False}:
        raise ExecutionEfficiencyError(
            "execution_efficiency.rollback must disable the policy without migration"
        )
    return policy


def _lane_output_path(round_id: str, lane_id: str) -> str:
    return f"rounds/{round_id}/lane-runs/{lane_id}.json"


def workflow_contract_sha256(orchestration: dict[str, Any]) -> str:
    workflow = _require_object(orchestration.get("workflow"), "orchestration.workflow")
    contract = {
        key: workflow.get(key)
        for key in (
            "title",
            "slug",
            "goal",
            "success_criteria",
            "constraints",
            "non_goals",
        )
    }
    return canonical_sha256(contract)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _workspace_root(workflow_dir: Path, orchestration: dict[str, Any]) -> Path:
    workflow = _require_object(orchestration.get("workflow"), "orchestration.workflow")
    value = _require_nonempty(
        workflow.get("workspace_root"), "orchestration.workflow.workspace_root"
    )
    path = Path(value)
    root = path.resolve() if path.is_absolute() else (workflow_dir / path).resolve()
    if not root.is_dir():
        raise ExecutionEfficiencyError(
            "orchestration.workflow.workspace_root must resolve to an existing directory"
        )
    return root


def _resolve_input_ref(
    workflow_dir: Path,
    workspace_root: Path,
    root_name: str,
    relative: str,
    label: str,
) -> Path:
    safe = safe_relative_path(relative, label)
    base = workflow_dir.resolve() if root_name == "workflow" else workspace_root
    if root_name not in {"workflow", "workspace"}:
        raise ExecutionEfficiencyError(f"{label}.root must be workflow or workspace")
    resolved = (base / safe).resolve()
    try:
        resolved.relative_to(base)
    except ValueError as exc:
        raise ExecutionEfficiencyError(f"{label} escapes its declared root") from exc
    if not resolved.is_file():
        raise ExecutionEfficiencyError(f"{label} must resolve to an existing regular file")
    return resolved


def bind_input_refs(workflow_dir: Path, orchestration: dict[str, Any]) -> int:
    workspace_root = _workspace_root(workflow_dir, orchestration)
    count = 0
    for round_plan in orchestration.get("rounds", []):
        if not isinstance(round_plan, dict):
            continue
        for lane in round_plan.get("lanes", []):
            if not isinstance(lane, dict) or lane.get("enabled") is not True:
                continue
            refs = lane.get("input_refs")
            if not isinstance(refs, list):
                raise ExecutionEfficiencyError(f"{lane.get('id')}.input_refs must be a list")
            bound: list[dict[str, str]] = []
            for index, ref in enumerate(refs, start=1):
                label = f"{lane.get('id')}.input_refs[{index}]"
                if isinstance(ref, str):
                    relative = safe_relative_path(ref, label)
                    workflow_candidate = workflow_dir / relative
                    workspace_candidate = workspace_root / relative
                    workflow_file = workflow_candidate.is_file()
                    workspace_file = workspace_candidate.is_file()
                    if workflow_file and workspace_file and workflow_candidate.resolve() != workspace_candidate.resolve():
                        raise ExecutionEfficiencyError(
                            f"{label} is ambiguous between workflow and workspace roots"
                        )
                    if workflow_file:
                        root_name = "workflow"
                    elif workspace_file:
                        root_name = "workspace"
                    else:
                        raise ExecutionEfficiencyError(
                            f"{label} must resolve to an existing regular file"
                        )
                else:
                    item = _require_object(ref, label)
                    _require_keys(item, ("root", "path"), label)
                    root_name = _require_nonempty(item["root"], f"{label}.root")
                    relative = safe_relative_path(item["path"], f"{label}.path")
                resolved = _resolve_input_ref(
                    workflow_dir,
                    workspace_root,
                    root_name,
                    relative,
                    label,
                )
                bound.append(
                    {
                        "root": root_name,
                        "path": relative,
                        "content_sha256": file_sha256(resolved),
                    }
                )
                count += 1
            lane["input_refs"] = bound
    return count


def validate_bound_input_refs(
    workflow_dir: Path,
    workspace_root: Path,
    refs: Any,
    label: str,
) -> None:
    if not isinstance(refs, list) or not 1 <= len(refs) <= 12:
        raise ExecutionEfficiencyError(f"{label} must contain 1..12 bounded references")
    seen: set[tuple[str, str]] = set()
    for index, ref in enumerate(refs, start=1):
        ref_label = f"{label}[{index}]"
        item = _require_object(ref, ref_label)
        _require_keys(item, ("root", "path", "content_sha256"), ref_label)
        root_name = _require_nonempty(item["root"], f"{ref_label}.root")
        relative = safe_relative_path(item["path"], f"{ref_label}.path")
        key = (root_name, relative)
        if key in seen:
            raise ExecutionEfficiencyError(f"{ref_label} duplicates another input reference")
        seen.add(key)
        resolved = _resolve_input_ref(
            workflow_dir,
            workspace_root,
            root_name,
            relative,
            ref_label,
        )
        if item["content_sha256"] != file_sha256(resolved):
            raise ExecutionEfficiencyError(f"{ref_label}.content_sha256 does not match the file")


def _dispatch_digest_input(lane: dict[str, Any], round_id: str) -> dict[str, Any]:
    execution = copy.deepcopy(lane.get("execution"))
    if isinstance(execution, dict):
        execution.pop("dispatch_sha256", None)
    return {
        "round_id": round_id,
        "lane_id": lane.get("id"),
        "lane": lane.get("lane"),
        "purpose": lane.get("purpose"),
        "prompt": lane.get("prompt"),
        "input_refs": lane.get("input_refs"),
        "output_schema": lane.get("output_schema"),
        "gate": lane.get("gate"),
        "runner": lane.get("runner"),
        "execution": execution,
    }


def refresh_dispatch_digest(
    lane: dict[str, Any],
    round_id: str,
    workflow_contract_digest: str | None = None,
) -> str:
    execution = _require_object(lane.get("execution"), f"{round_id}:{lane.get('id')}.execution")
    if workflow_contract_digest is not None:
        execution["workflow_contract_sha256"] = workflow_contract_digest
    digest = canonical_sha256(_dispatch_digest_input(lane, round_id))
    execution["dispatch_sha256"] = digest
    return digest


def build_lane_execution(
    lane: dict[str, Any],
    round_id: str,
    runner_mode: str,
    policy: dict[str, Any],
) -> dict[str, Any]:
    budgets = policy["budgets"]
    lane_id = str(lane["id"])
    lane_type = str(lane["lane"])
    execution: dict[str, Any] = {
        "schema_version": LANE_SCHEMA,
        "context_mode": "isolated",
        "parent_transcript": "excluded",
        "raw_prior_outputs": False,
        "output_path": _lane_output_path(round_id, lane_id),
        "workflow_contract_sha256": "",
        "dispatch_sha256": "",
        "admission": {
            "decision": "draft",
            "unique_question": "TODO",
            "expected_state_change": "TODO",
            "reason": "TODO",
            "deterministic": False,
            "exception_reason": "" if lane_type not in {"plan", "seam"} else "TODO",
        },
        "budget": {
            "max_tool_turns": budgets["default_max_tool_turns"],
            "max_test_runs": budgets["default_max_test_runs"],
            "on_exhausted": budgets["on_exhausted"],
        },
        "repair_affinity": {
            "strategy": "reuse_writer_once" if lane_type == "repair" else "not_applicable",
            "source_lane_id": None,
            "max_writer_reuse": budgets["max_writer_reuse"] if lane_type == "repair" else 0,
            "verifier_must_be_independent": True,
        },
        "receipt": {
            "schema_version": RECEIPT_SCHEMA,
            "max_inline_chars": policy["result_transport"]["max_inline_chars"],
        },
    }
    lane["execution"] = execution
    if runner_mode == "codex_builtin_subagents":
        lane["runner"]["fork_context"] = False
    elif runner_mode == "claude_code_builtin_subagents":
        lane["runner"]["context_mode"] = "isolated"
    refresh_dispatch_digest(lane, round_id)
    return execution


def validate_lane_execution(
    lane: dict[str, Any],
    round_id: str,
    runner_mode: str,
    policy: dict[str, Any],
    *,
    allow_draft: bool,
    workflow_dir: Path | None = None,
    workspace_root: Path | None = None,
    workflow_contract_digest: str | None = None,
) -> dict[str, Any]:
    label = f"{round_id}:{lane.get('id')}.execution"
    execution = _require_object(lane.get("execution"), label)
    _require_keys(
        execution,
        (
            "schema_version",
            "context_mode",
            "parent_transcript",
            "raw_prior_outputs",
            "output_path",
            "workflow_contract_sha256",
            "dispatch_sha256",
            "admission",
            "budget",
            "repair_affinity",
            "receipt",
        ),
        label,
    )
    if execution["schema_version"] != LANE_SCHEMA:
        raise ExecutionEfficiencyError(f"{label}.schema_version must be {LANE_SCHEMA}")
    if not allow_draft and workflow_contract_digest is not None:
        if execution["workflow_contract_sha256"] != workflow_contract_digest:
            raise ExecutionEfficiencyError(
                f"{label}.workflow_contract_sha256 does not match the current workflow target"
            )
    if lane.get("agent_count") != 1:
        raise ExecutionEfficiencyError(
            f"{label} supports exactly one agent per lane in execution-efficiency v1"
        )
    if execution["context_mode"] != "isolated" or execution["parent_transcript"] != "excluded":
        raise ExecutionEfficiencyError(f"{label} must use isolated context without parent transcript")
    if execution["raw_prior_outputs"] is not False:
        raise ExecutionEfficiencyError(f"{label}.raw_prior_outputs must be false")
    expected_output = _lane_output_path(round_id, str(lane.get("id")))
    if safe_relative_path(execution["output_path"], f"{label}.output_path") != expected_output:
        raise ExecutionEfficiencyError(f"{label}.output_path must be {expected_output}")

    runner = _require_object(lane.get("runner"), f"{label}.runner")
    if runner_mode == "codex_builtin_subagents" and runner.get("fork_context") is not False:
        raise ExecutionEfficiencyError(f"{label}.runner.fork_context must be false")
    if runner_mode == "claude_code_builtin_subagents" and runner.get("context_mode") != "isolated":
        raise ExecutionEfficiencyError(f"{label}.runner.context_mode must be isolated")

    admission = _require_object(execution["admission"], f"{label}.admission")
    _require_keys(
        admission,
        (
            "decision",
            "unique_question",
            "expected_state_change",
            "reason",
            "deterministic",
            "exception_reason",
        ),
        f"{label}.admission",
    )
    if not isinstance(admission["deterministic"], bool):
        raise ExecutionEfficiencyError(f"{label}.admission.deterministic must be boolean")
    if not allow_draft:
        if admission["decision"] != "enabled":
            raise ExecutionEfficiencyError(f"{label}.admission.decision must be enabled before dispatch")
        for key in ("unique_question", "expected_state_change", "reason"):
            if _is_placeholder(admission[key]):
                raise ExecutionEfficiencyError(f"{label}.admission.{key} must be specific before dispatch")
        if admission["deterministic"] is True:
            raise ExecutionEfficiencyError(
                f"{label} is deterministic and should run as a script instead of an agent lane"
            )
        if lane.get("lane") in {"plan", "seam"} and _is_placeholder(admission["exception_reason"]):
            raise ExecutionEfficiencyError(
                f"{label}.admission.exception_reason is required for independent {lane.get('lane')} lanes"
            )
        prompt = _require_nonempty(lane.get("prompt"), f"{label}.prompt")
        output_schema = _require_nonempty(lane.get("output_schema"), f"{label}.output_schema")
        if expected_output not in prompt or output_schema not in prompt or "json" not in prompt.lower():
            raise ExecutionEfficiencyError(
                f"{label}.prompt must bind JSON output to {expected_output} using {output_schema}"
            )
        input_refs = lane.get("input_refs")
        if workflow_dir is not None and workspace_root is not None:
            validate_bound_input_refs(
                workflow_dir,
                workspace_root,
                input_refs,
                f"{label}.input_refs",
            )
        else:
            if not isinstance(input_refs, list) or not 1 <= len(input_refs) <= 12:
                raise ExecutionEfficiencyError(
                    f"{label}.input_refs must contain 1..12 bounded references"
                )
            for index, input_ref in enumerate(input_refs, start=1):
                if isinstance(input_ref, dict):
                    safe_relative_path(
                        input_ref.get("path"), f"{label}.input_refs[{index}].path"
                    )
                else:
                    safe_relative_path(input_ref, f"{label}.input_refs[{index}]")

    budget = _require_object(execution["budget"], f"{label}.budget")
    for key in ("max_tool_turns", "max_test_runs"):
        if not isinstance(budget.get(key), int) or isinstance(budget.get(key), bool) or budget[key] < 1:
            raise ExecutionEfficiencyError(f"{label}.budget.{key} must be an integer >= 1")
    if budget.get("on_exhausted") != "checkpoint_then_gate":
        raise ExecutionEfficiencyError(f"{label}.budget.on_exhausted must be checkpoint_then_gate")

    affinity = _require_object(execution["repair_affinity"], f"{label}.repair_affinity")
    if affinity.get("verifier_must_be_independent") is not True:
        raise ExecutionEfficiencyError(
            f"{label}.repair_affinity.verifier_must_be_independent must be true"
        )
    if lane.get("lane") == "repair":
        if affinity.get("strategy") != "reuse_writer_once" or affinity.get("max_writer_reuse") != 1:
            raise ExecutionEfficiencyError(f"{label} repair affinity must reuse a writer at most once")
        if not allow_draft:
            _require_nonempty(affinity.get("source_lane_id"), f"{label}.repair_affinity.source_lane_id")
    elif affinity.get("strategy") != "not_applicable" or affinity.get("max_writer_reuse") != 0:
        raise ExecutionEfficiencyError(f"{label} non-repair affinity must be not_applicable")

    receipt = _require_object(execution["receipt"], f"{label}.receipt")
    if receipt.get("schema_version") != RECEIPT_SCHEMA:
        raise ExecutionEfficiencyError(f"{label}.receipt.schema_version must be {RECEIPT_SCHEMA}")
    if receipt.get("max_inline_chars") != policy["result_transport"]["max_inline_chars"]:
        raise ExecutionEfficiencyError(f"{label}.receipt.max_inline_chars must match policy")

    expected_digest = canonical_sha256(_dispatch_digest_input(lane, round_id))
    if execution["dispatch_sha256"] != expected_digest:
        raise ExecutionEfficiencyError(f"{label}.dispatch_sha256 does not match the lane contract")
    return execution


def validate_orchestration_efficiency(
    orchestration: dict[str, Any],
    runner_mode: str,
    policy: dict[str, Any],
    *,
    allow_draft: bool,
    workflow_dir: Path | None = None,
) -> dict[str, dict[str, Any]]:
    lanes_by_ref: dict[str, dict[str, Any]] = {}
    enabled_types: set[str] = set()
    unique_questions: set[str] = set()
    workspace_root = (
        _workspace_root(workflow_dir, orchestration)
        if workflow_dir is not None and not allow_draft
        else None
    )
    contract_digest = (
        workflow_contract_sha256(orchestration)
        if isinstance(orchestration.get("workflow"), dict)
        else None
    )
    for round_plan in orchestration.get("rounds", []):
        if not isinstance(round_plan, dict):
            continue
        round_id = _require_nonempty(round_plan.get("round_id"), "round.round_id")
        lanes = round_plan.get("lanes")
        if not isinstance(lanes, list):
            raise ExecutionEfficiencyError(f"{round_id}.lanes must be a list")
        for lane in lanes:
            if not isinstance(lane, dict) or lane.get("enabled") is not True:
                continue
            lane_id = _require_nonempty(lane.get("id"), f"{round_id}.lane.id")
            lane_ref = f"{round_id}:{lane_id}"
            if lane_ref in lanes_by_ref:
                raise ExecutionEfficiencyError(f"duplicate execution lane ref: {lane_ref}")
            validate_lane_execution(
                lane,
                round_id,
                runner_mode,
                policy,
                allow_draft=allow_draft,
                workflow_dir=workflow_dir if not allow_draft else None,
                workspace_root=workspace_root,
                workflow_contract_digest=contract_digest,
            )
            lanes_by_ref[lane_ref] = lane
            lane_type = _require_nonempty(lane.get("lane"), f"{lane_ref}.lane")
            enabled_types.add(lane_type)
            if not allow_draft:
                question = str(lane["execution"]["admission"]["unique_question"]).strip().casefold()
                if question in unique_questions:
                    raise ExecutionEfficiencyError(
                        f"{lane_ref}.admission.unique_question duplicates another enabled lane"
                    )
                unique_questions.add(question)

    if not allow_draft and enabled_types.intersection({"implement", "repair"}):
        missing = set(policy["quality"]["write_lanes_require"]) - enabled_types
        if missing:
            raise ExecutionEfficiencyError(
                "write lanes require independent quality lanes: " + ", ".join(sorted(missing))
            )
    if not allow_draft and policy["risk_class"] in {"high", "judgment"}:
        missing = set(policy["quality"]["high_risk_require"]) - enabled_types
        if missing:
            raise ExecutionEfficiencyError(
                "high-risk workflows require lanes: " + ", ".join(sorted(missing))
            )
    return lanes_by_ref


def receipt_relative_path(round_id: str, lane_id: str) -> str:
    return f"rounds/{round_id}/receipts/{lane_id}.json"


def _validate_lane_output_for_receipt(
    output: dict[str, Any],
    lane: dict[str, Any],
    round_id: str,
    output_rel: str,
) -> None:
    lane_id = str(lane.get("id"))
    if output.get("schema_version") != "agent-loops.lane-output.v1":
        raise ExecutionEfficiencyError(
            f"{output_rel}.schema_version must be agent-loops.lane-output.v1"
        )
    if output.get("run_id") != f"{round_id}-{lane_id}":
        raise ExecutionEfficiencyError(f"{output_rel}.run_id does not match its lane")
    if output.get("round_id") != round_id or output.get("lane_id") != lane_id:
        raise ExecutionEfficiencyError(f"{output_rel} does not match {round_id}:{lane_id}")
    if output.get("lane") != lane.get("lane"):
        raise ExecutionEfficiencyError(f"{output_rel}.lane does not match the lane contract")
    if output.get("status") not in LANE_TERMINAL_STATUSES:
        raise ExecutionEfficiencyError(
            f"{output_rel}.status must be a terminal lane status, not {output.get('status')!r}"
        )
    _require_nonempty(output.get("summary"), f"{output_rel}.summary")

    confidence = _require_object(output.get("confidence"), f"{output_rel}.confidence")
    _require_keys(
        confidence,
        ("self", "independent", "source", "rationale"),
        f"{output_rel}.confidence",
    )
    for key in ("self", "independent"):
        score = confidence[key]
        if score is not None and (
            not isinstance(score, (int, float))
            or isinstance(score, bool)
            or not 0 <= score <= 1
        ):
            raise ExecutionEfficiencyError(
                f"{output_rel}.confidence.{key} must be null or a number from 0 to 1"
            )
    _require_nonempty(confidence["source"], f"{output_rel}.confidence.source")
    _require_nonempty(confidence["rationale"], f"{output_rel}.confidence.rationale")

    findings = output.get("findings")
    if not isinstance(findings, list):
        raise ExecutionEfficiencyError(f"{output_rel}.findings must be a list")
    for index, finding in enumerate(findings, start=1):
        label = f"{output_rel}.findings[{index}]"
        item = _require_object(finding, label)
        _require_keys(item, ("id", "severity", "claim", "evidence", "recommendation"), label)
        _require_nonempty(item["id"], f"{label}.id")
        if item["severity"] not in SEVERITIES:
            raise ExecutionEfficiencyError(f"{label}.severity must be P0, P1, P2, or P3")
        _require_nonempty(item["claim"], f"{label}.claim")
        if not isinstance(item["evidence"], list) or not item["evidence"]:
            raise ExecutionEfficiencyError(f"{label}.evidence must be a non-empty list")
        _require_nonempty(item["recommendation"], f"{label}.recommendation")

    gate = _require_object(output.get("gate"), f"{output_rel}.gate")
    _require_keys(gate, ("decision", "reason", "next_lanes"), f"{output_rel}.gate")
    if gate["decision"] not in GATE_DECISIONS:
        raise ExecutionEfficiencyError(
            f"{output_rel}.gate.decision must be one of {sorted(GATE_DECISIONS)}"
        )
    _require_nonempty(gate["reason"], f"{output_rel}.gate.reason")
    if not isinstance(gate["next_lanes"], list):
        raise ExecutionEfficiencyError(f"{output_rel}.gate.next_lanes must be a list")
    if gate["decision"] == "pass" and output["status"] != "complete":
        raise ExecutionEfficiencyError(f"{output_rel} pass gate requires status complete")

    payload = _require_object(output.get("payload"), f"{output_rel}.payload")
    output_schema = lane.get("output_schema")
    for key in RECEIPT_PAYLOAD_KEYS.get(str(output_schema), ()):
        if key not in payload:
            raise ExecutionEfficiencyError(
                f"{output_rel}.payload missing key for {output_schema}: {key}"
            )
    if gate["decision"] == "pass" and lane.get("lane") in {"verify", "challenge"}:
        independent = confidence.get("independent")
        if not isinstance(independent, (int, float)) or isinstance(independent, bool) or independent < 0.7:
            raise ExecutionEfficiencyError(
                f"{output_rel}.confidence.independent must be >= 0.7 for assessment pass"
            )
    if gate["decision"] == "pass" and lane.get("lane") == "verify":
        checks = payload.get("checks")
        criteria = payload.get("success_criteria_status")
        if not isinstance(checks, list) or not checks:
            raise ExecutionEfficiencyError(f"{output_rel}.payload.checks must be non-empty for pass")
        if not isinstance(criteria, list) or not criteria:
            raise ExecutionEfficiencyError(
                f"{output_rel}.payload.success_criteria_status must be non-empty for pass"
            )
        for index, check in enumerate(checks, start=1):
            if not isinstance(check, dict) or check.get("status") != "pass" or not check.get("evidence"):
                raise ExecutionEfficiencyError(
                    f"{output_rel}.payload.checks[{index}] requires pass status and evidence"
                )
        for index, criterion in enumerate(criteria, start=1):
            if not isinstance(criterion, dict) or criterion.get("status") != "pass" or not criterion.get("evidence"):
                raise ExecutionEfficiencyError(
                    f"{output_rel}.payload.success_criteria_status[{index}] requires pass status and evidence"
                )


def build_lane_receipt(
    workflow_dir: Path,
    lane: dict[str, Any],
    round_id: str,
) -> dict[str, Any]:
    execution = _require_object(lane.get("execution"), "lane.execution")
    output_rel = safe_relative_path(execution.get("output_path"), "lane.execution.output_path")
    output_path = workflow_dir / output_rel
    if not output_path.is_file():
        raise ExecutionEfficiencyError(f"missing lane output: {output_rel}")
    try:
        output = json.loads(output_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ExecutionEfficiencyError(f"invalid lane output JSON: {exc}") from exc
    output = _require_object(output, output_rel)
    for key in (
        "schema_version",
        "run_id",
        "round_id",
        "lane_id",
        "lane",
        "status",
        "summary",
        "findings",
        "gate",
        "payload",
    ):
        if key not in output:
            raise ExecutionEfficiencyError(f"{output_rel} missing key: {key}")
    _validate_lane_output_for_receipt(output, lane, round_id, output_rel)
    lane_id = str(lane.get("id"))
    findings = output.get("findings") if isinstance(output.get("findings"), list) else []
    gate = output.get("gate") if isinstance(output.get("gate"), dict) else {}
    summary = str(output.get("summary", "")).strip().replace("\n", " ")[:240]
    receipt = {
        "schema_version": RECEIPT_SCHEMA,
        "round_id": round_id,
        "lane_id": lane_id,
        "status": output.get("status"),
        "output_path": output_rel,
        "output_sha256": canonical_sha256(output),
        "workflow_contract_sha256": execution.get("workflow_contract_sha256"),
        "dispatch_sha256": execution.get("dispatch_sha256"),
        "gate": gate.get("decision"),
        "finding_count": len(findings),
        "summary": summary,
    }
    max_chars = execution["receipt"]["max_inline_chars"]
    if len(json.dumps(receipt, ensure_ascii=False, separators=(",", ":"))) > max_chars:
        raise ExecutionEfficiencyError("lane receipt exceeds max_inline_chars")
    return receipt


def validate_lane_receipt(
    workflow_dir: Path,
    lane: dict[str, Any],
    round_id: str,
    receipt: Any,
) -> dict[str, Any]:
    value = _require_object(receipt, f"{round_id}:{lane.get('id')} receipt")
    expected = build_lane_receipt(workflow_dir, lane, round_id)
    if value != expected:
        raise ExecutionEfficiencyError(
            f"{round_id}:{lane.get('id')} receipt does not match its lane output"
        )
    return value


def build_integration_index(
    workflow_dir: Path,
    orchestration: dict[str, Any],
) -> dict[str, Any]:
    lanes: list[dict[str, Any]] = []
    for round_plan in orchestration.get("rounds", []):
        if not isinstance(round_plan, dict):
            continue
        round_id = round_plan.get("round_id")
        if not isinstance(round_id, str):
            continue
        for lane in round_plan.get("lanes", []):
            if not isinstance(lane, dict) or lane.get("enabled") is not True:
                continue
            lane_id = lane.get("id")
            if not isinstance(lane_id, str):
                continue
            receipt_path = workflow_dir / receipt_relative_path(round_id, lane_id)
            if not receipt_path.is_file():
                raise ExecutionEfficiencyError(
                    f"missing enabled-lane receipt: {receipt_relative_path(round_id, lane_id)}"
                )
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            validate_lane_receipt(workflow_dir, lane, round_id, receipt)
            output = json.loads((workflow_dir / receipt["output_path"]).read_text(encoding="utf-8"))
            finding_ids = [
                item.get("id")
                for item in output.get("findings", [])
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            ]
            confidence = output.get("confidence") if isinstance(output.get("confidence"), dict) else {}
            lanes.append(
                {
                    "round_id": round_id,
                    "lane_id": lane_id,
                    "lane": lane.get("lane"),
                    "status": receipt.get("status"),
                    "gate": receipt.get("gate"),
                    "summary": receipt.get("summary"),
                    "finding_ids": finding_ids,
                    "finding_count": receipt.get("finding_count"),
                    "independent_confidence": confidence.get("independent"),
                    "output_path": receipt.get("output_path"),
                    "output_sha256": receipt.get("output_sha256"),
                    "workflow_contract_sha256": receipt.get("workflow_contract_sha256"),
                    "dispatch_sha256": receipt.get("dispatch_sha256"),
                }
            )
    value = {
        "schema_version": INDEX_SCHEMA,
        "lane_count": len(lanes),
        "lanes": lanes,
    }
    value["content_sha256"] = canonical_sha256(value, ("content_sha256",))
    return value


def _parse_rfc3339(value: Any, label: str) -> datetime:
    text = _require_nonempty(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExecutionEfficiencyError(f"{label} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ExecutionEfficiencyError(f"{label} must include a timezone")
    return parsed


def validate_wait_telemetry(
    value: Any,
    *,
    final: bool,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    telemetry = _require_object(value, "runner-evidence.execution_efficiency")
    _require_keys(
        telemetry,
        (
            "lead_model_completions",
            "status_only_completions",
            "functions_wait_calls",
            "wait_waves",
            "card_events",
        ),
        "runner-evidence.execution_efficiency",
    )
    for key in ("lead_model_completions", "status_only_completions", "functions_wait_calls"):
        if not isinstance(telemetry[key], int) or isinstance(telemetry[key], bool) or telemetry[key] < 0:
            raise ExecutionEfficiencyError(
                f"runner-evidence.execution_efficiency.{key} must be an integer >= 0"
            )
    if final and telemetry["status_only_completions"] != 0:
        raise ExecutionEfficiencyError(
            "runner-evidence.execution_efficiency.status_only_completions must be 0 in final mode"
        )
    if telemetry["status_only_completions"] > telemetry["lead_model_completions"]:
        raise ExecutionEfficiencyError(
            "runner-evidence.execution_efficiency.status_only_completions cannot exceed lead_model_completions"
        )
    waves = telemetry["wait_waves"]
    if not isinstance(waves, list):
        raise ExecutionEfficiencyError(
            "runner-evidence.execution_efficiency.wait_waves must be a list"
        )
    seen_wave_ids: set[str] = set()
    previous: tuple[dict[str, Any], datetime] | None = None
    current_barrier_id: str | None = None
    active_targets: set[str] = set()
    wait_policy = policy.get("wait", {}) if isinstance(policy, dict) else {}
    max_wait_ms = wait_policy.get("max_native_wait_ms", 3600000)
    min_repoll_ms = wait_policy.get("min_repoll_ms", 300000)
    for index, wave in enumerate(waves, start=1):
        label = f"runner-evidence.execution_efficiency.wait_waves[{index}]"
        item = _require_object(wave, label)
        _require_keys(
            item,
            (
                "wave_id",
                "barrier_id",
                "targets",
                "timeout_ms",
                "outcome",
                "started_at",
                "completed_at",
                "trigger",
                "terminal_targets",
            ),
            label,
        )
        wave_id = _require_nonempty(item["wave_id"], f"{label}.wave_id")
        if wave_id in seen_wave_ids:
            raise ExecutionEfficiencyError(f"{label}.wave_id duplicates {wave_id}")
        seen_wave_ids.add(wave_id)
        barrier_id = _require_nonempty(item["barrier_id"], f"{label}.barrier_id")
        if not isinstance(item["targets"], list) or not item["targets"]:
            raise ExecutionEfficiencyError(f"{label}.targets must be a non-empty list")
        if any(not isinstance(target, str) or not target for target in item["targets"]):
            raise ExecutionEfficiencyError(f"{label}.targets must contain non-empty strings")
        if len(set(item["targets"])) != len(item["targets"]):
            raise ExecutionEfficiencyError(f"{label}.targets must not contain duplicates")
        timeout = item["timeout_ms"]
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 300000 <= timeout <= max_wait_ms:
            raise ExecutionEfficiencyError(
                f"{label}.timeout_ms must be 300000..{max_wait_ms}"
            )
        if item["outcome"] not in WAIT_OUTCOMES:
            raise ExecutionEfficiencyError(f"{label}.outcome must be one of {sorted(WAIT_OUTCOMES)}")
        started_at = _parse_rfc3339(item["started_at"], f"{label}.started_at")
        completed_at = _parse_rfc3339(item["completed_at"], f"{label}.completed_at")
        if completed_at < started_at:
            raise ExecutionEfficiencyError(f"{label}.completed_at cannot precede started_at")
        elapsed_wait_ms = int((completed_at - started_at).total_seconds() * 1000)
        if item["outcome"] == "timeout" and elapsed_wait_ms < item["timeout_ms"]:
            raise ExecutionEfficiencyError(
                f"{label} cannot claim timeout before timeout_ms elapsed"
            )
        trigger = item["trigger"]
        allowed_triggers = {
            "dispatch",
            "prior_terminal_event",
            "prior_timeout",
            "material_failure",
            "user_interruption",
        }
        if trigger not in allowed_triggers:
            raise ExecutionEfficiencyError(
                f"{label}.trigger must be one of {sorted(allowed_triggers)}"
            )
        terminal_targets = item["terminal_targets"]
        if not isinstance(terminal_targets, list):
            raise ExecutionEfficiencyError(f"{label}.terminal_targets must be a list")
        if any(target not in item["targets"] for target in terminal_targets):
            raise ExecutionEfficiencyError(
                f"{label}.terminal_targets must be a subset of targets"
            )
        if len(set(terminal_targets)) != len(terminal_targets):
            raise ExecutionEfficiencyError(
                f"{label}.terminal_targets must not contain duplicates"
            )
        if item["outcome"] == "completed" and not terminal_targets:
            raise ExecutionEfficiencyError(
                f"{label}.terminal_targets must be non-empty for completed outcome"
            )
        if item["outcome"] != "completed" and terminal_targets:
            raise ExecutionEfficiencyError(
                f"{label}.terminal_targets must be empty unless outcome is completed"
            )
        if previous is not None and started_at < previous[1]:
            raise ExecutionEfficiencyError(f"{label}.started_at overlaps the prior wait call")
        new_barrier = barrier_id != current_barrier_id
        if new_barrier:
            if active_targets:
                raise ExecutionEfficiencyError(
                    f"{label} cannot start a new barrier before {current_barrier_id} reaches terminal coverage"
                )
            if trigger != "dispatch":
                raise ExecutionEfficiencyError(
                    f"{label}.trigger must be dispatch for a new barrier"
                )
            _require_nonempty(item.get("trigger_ref"), f"{label}.trigger_ref")
            current_barrier_id = barrier_id
            active_targets = set(item["targets"])
        else:
            if set(item["targets"]) != active_targets:
                raise ExecutionEfficiencyError(
                    f"{label}.targets must equal the prior active target set"
                )
            if previous is None:
                raise ExecutionEfficiencyError(f"{label} continuation is missing a prior wait")
            previous_wave, previous_completed_at = previous
            if trigger == "dispatch":
                raise ExecutionEfficiencyError(f"{label}.trigger cannot repeat dispatch")
            elapsed_ms = int((started_at - previous_completed_at).total_seconds() * 1000)
            if trigger == "prior_timeout":
                if previous_wave.get("outcome") != "timeout":
                    raise ExecutionEfficiencyError(
                        f"{label}.trigger prior_timeout requires a preceding timeout"
                    )
                if elapsed_ms < min_repoll_ms:
                    raise ExecutionEfficiencyError(
                        f"{label} re-wait after timeout must wait at least {min_repoll_ms}ms"
                    )
            elif trigger == "prior_terminal_event":
                if previous_wave.get("outcome") != "completed":
                    raise ExecutionEfficiencyError(
                        f"{label}.trigger prior_terminal_event requires a completed prior wait"
                    )
            elif trigger in {"material_failure", "user_interruption"}:
                _require_nonempty(item.get("trigger_ref"), f"{label}.trigger_ref")
        if item["outcome"] == "completed":
            active_targets.difference_update(terminal_targets)
        previous = (item, completed_at)
    if final and active_targets:
        raise ExecutionEfficiencyError(
            "runner-evidence.execution_efficiency final wait barrier has nonterminal targets: "
            + ", ".join(sorted(active_targets))
        )
    if telemetry["functions_wait_calls"] != len(waves):
        raise ExecutionEfficiencyError(
            "runner-evidence.execution_efficiency.functions_wait_calls must equal recorded wait_waves"
        )
    card_events = telemetry["card_events"]
    if not isinstance(card_events, list):
        raise ExecutionEfficiencyError(
            "runner-evidence.execution_efficiency.card_events must be a list"
        )
    seen_hashes: set[str] = set()
    for index, event in enumerate(card_events, start=1):
        label = f"runner-evidence.execution_efficiency.card_events[{index}]"
        item = _require_object(event, label)
        if item.get("reason") not in EVENT_CARD_REASONS:
            raise ExecutionEfficiencyError(
                f"{label}.reason must be an event transition, not a heartbeat"
            )
        if item.get("state_changed") is not True:
            raise ExecutionEfficiencyError(f"{label}.state_changed must be true")
        digest = _require_nonempty(item.get("rendered_sha256"), f"{label}.rendered_sha256")
        if digest in seen_hashes:
            raise ExecutionEfficiencyError(f"{label} duplicates a rendered card hash")
        seen_hashes.add(digest)
    return telemetry


def _agent_identifiers(record: dict[str, Any]) -> frozenset[str]:
    return frozenset(
        value
        for key in ("agent_id", "native_handle")
        if isinstance((value := record.get(key)), str) and value
    )


def validate_agent_execution_evidence(
    lanes_by_ref: dict[str, dict[str, Any]],
    evidence_by_lane: dict[str, dict[str, Any]],
    telemetry: dict[str, Any],
    *,
    final: bool,
) -> None:
    if not final:
        return
    identities_by_ref: dict[str, frozenset[str]] = {}
    metrics_by_ref: dict[str, dict[str, Any]] = {}
    lane_refs_by_id: dict[str, list[str]] = {}
    repair_sources: set[str] = set()
    reused_writer_identifiers: set[str] = set()

    for lane_ref, lane in lanes_by_ref.items():
        lane_id = str(lane.get("id"))
        lane_refs_by_id.setdefault(lane_id, []).append(lane_ref)
        record = evidence_by_lane.get(lane_ref)
        if not isinstance(record, dict):
            raise ExecutionEfficiencyError(f"{lane_ref} missing execution-efficiency runner evidence")
        identifiers = _agent_identifiers(record)
        if not identifiers:
            raise ExecutionEfficiencyError(f"{lane_ref} runner evidence requires an agent identity")
        identities_by_ref[lane_ref] = identifiers
        metrics = _require_object(record.get("execution_metrics"), f"{lane_ref}.execution_metrics")
        metrics_by_ref[lane_ref] = metrics
        _require_keys(
            metrics,
            (
                "model_completions",
                "tool_turns",
                "test_runs",
                "repair_reuse_count",
                "budget_outcome",
                "context_forked",
                "received_parent_transcript",
                "dispatch_sha256",
                "receipt_path",
            ),
            f"{lane_ref}.execution_metrics",
        )
        for key in ("model_completions", "tool_turns", "test_runs", "repair_reuse_count"):
            value = metrics[key]
            minimum = 1 if key == "model_completions" else 0
            if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
                raise ExecutionEfficiencyError(
                    f"{lane_ref}.execution_metrics.{key} must be an integer >= {minimum}"
                )
        if metrics["context_forked"] is not False or metrics["received_parent_transcript"] is not False:
            raise ExecutionEfficiencyError(
                f"{lane_ref}.execution_metrics must attest isolated context without parent transcript"
            )
        execution = lane["execution"]
        if metrics["dispatch_sha256"] != execution["dispatch_sha256"]:
            raise ExecutionEfficiencyError(
                f"{lane_ref}.execution_metrics.dispatch_sha256 must match the planned dispatch"
            )
        round_id, _ = lane_ref.split(":", 1)
        expected_receipt = receipt_relative_path(round_id, lane_id)
        if safe_relative_path(metrics["receipt_path"], f"{lane_ref}.execution_metrics.receipt_path") != expected_receipt:
            raise ExecutionEfficiencyError(
                f"{lane_ref}.execution_metrics.receipt_path must be {expected_receipt}"
            )
        budget = execution["budget"]
        exceeded = (
            metrics["tool_turns"] > budget["max_tool_turns"]
            or metrics["test_runs"] > budget["max_test_runs"]
        )
        allowed_outcomes = {"within_budget", "checkpoint", "rotated", "human_gate", "blocked"}
        if metrics["budget_outcome"] not in allowed_outcomes:
            raise ExecutionEfficiencyError(
                f"{lane_ref}.execution_metrics.budget_outcome must be one of {sorted(allowed_outcomes)}"
            )
        if exceeded:
            raise ExecutionEfficiencyError(
                f"{lane_ref} exceeded its hard tool/test budget instead of checkpointing before exhaustion"
            )
        if metrics["budget_outcome"] in {"human_gate", "blocked"}:
            raise ExecutionEfficiencyError(
                f"{lane_ref} cannot claim final completion with budget_outcome {metrics['budget_outcome']}"
            )
        if metrics["budget_outcome"] in {"checkpoint", "rotated"}:
            _require_nonempty(
                metrics.get("budget_note"), f"{lane_ref}.execution_metrics.budget_note"
            )
            _require_nonempty(
                metrics.get("successor_lane_ref"),
                f"{lane_ref}.execution_metrics.successor_lane_ref",
            )

        lane_type = lane.get("lane")
        reuse_count = metrics["repair_reuse_count"]
        if lane_type != "repair" and reuse_count != 0:
            raise ExecutionEfficiencyError(
                f"{lane_ref}.execution_metrics.repair_reuse_count must be 0 outside repair lanes"
            )
        if lane_type == "repair" and reuse_count > 1:
            raise ExecutionEfficiencyError(f"{lane_ref} may reuse its writer at most once")

    writer_refs = [
        lane_ref
        for lane_ref, lane in lanes_by_ref.items()
        if lane.get("lane") in {"implement", "repair"}
    ]
    writer_identifiers = set().union(
        *(identities_by_ref[lane_ref] for lane_ref in writer_refs)
    ) if writer_refs else set()
    for lane_ref, lane in lanes_by_ref.items():
        if lane.get("lane") in {"review", "challenge", "verify"}:
            if identities_by_ref[lane_ref] & writer_identifiers:
                raise ExecutionEfficiencyError(
                    f"{lane_ref} assessment identity must be independent from writer identities"
                )

    for lane_ref, metrics in metrics_by_ref.items():
        if metrics["budget_outcome"] not in {"checkpoint", "rotated"}:
            continue
        successor_ref = metrics["successor_lane_ref"]
        if successor_ref == lane_ref or successor_ref not in lanes_by_ref:
            raise ExecutionEfficiencyError(
                f"{lane_ref}.execution_metrics.successor_lane_ref must identify another enabled lane"
            )
        if metrics["budget_outcome"] == "rotated" and (
            identities_by_ref[lane_ref] & identities_by_ref[successor_ref]
        ):
            raise ExecutionEfficiencyError(
                f"{lane_ref} rotated budget outcome requires a distinct successor identity"
            )

    for lane_ref, lane in lanes_by_ref.items():
        if lane.get("lane") != "repair":
            continue
        source_lane_id = lane["execution"]["repair_affinity"]["source_lane_id"]
        candidates = lane_refs_by_id.get(str(source_lane_id), [])
        if len(candidates) != 1:
            raise ExecutionEfficiencyError(
                f"{lane_ref}.repair_affinity.source_lane_id must identify exactly one lane"
            )
        source_ref = candidates[0]
        if lanes_by_ref[source_ref].get("lane") != "implement":
            raise ExecutionEfficiencyError(
                f"{lane_ref} repair source must be an implement lane, not another repair"
            )
        if source_ref in repair_sources:
            raise ExecutionEfficiencyError(f"{source_ref} cannot be reused by more than one repair lane")
        repair_sources.add(source_ref)
        same_identity = bool(identities_by_ref[lane_ref] & identities_by_ref[source_ref])
        reuse_count = evidence_by_lane[lane_ref]["execution_metrics"]["repair_reuse_count"]
        if same_identity and reuse_count != 1:
            raise ExecutionEfficiencyError(f"{lane_ref} reused its writer but did not record reuse_count=1")
        if not same_identity and reuse_count != 0:
            raise ExecutionEfficiencyError(f"{lane_ref} rotated writers but recorded a reuse")
        if same_identity:
            reused = identities_by_ref[lane_ref] & identities_by_ref[source_ref]
            duplicate_reuse = sorted(reused & reused_writer_identifiers)
            if duplicate_reuse:
                raise ExecutionEfficiencyError(
                    f"{lane_ref} exceeds cumulative writer reuse for identities: "
                    + ", ".join(duplicate_reuse)
                )
            reused_writer_identifiers.update(reused)

    terminal_targets = {
        target
        for wave in telemetry.get("wait_waves", [])
        for target in wave.get("terminal_targets", [])
        if isinstance(wave, dict) and isinstance(wave.get("terminal_targets"), list)
    }
    registered_identifiers = set().union(*identities_by_ref.values()) if identities_by_ref else set()
    unexpected_wait_targets = sorted(
        {
            target
            for wave in telemetry.get("wait_waves", [])
            if isinstance(wave, dict) and isinstance(wave.get("targets"), list)
            for target in wave["targets"]
        }
        - registered_identifiers
    )
    if unexpected_wait_targets:
        raise ExecutionEfficiencyError(
            "execution-efficiency wait telemetry contains unregistered identities: "
            + ", ".join(unexpected_wait_targets)
        )
    for index, wave in enumerate(telemetry.get("wait_waves", []), start=1):
        if not isinstance(wave, dict) or not isinstance(wave.get("targets"), list):
            continue
        targets = set(wave["targets"])
        for lane_ref, identifiers in identities_by_ref.items():
            aliases = sorted(targets & identifiers)
            if len(aliases) > 1:
                raise ExecutionEfficiencyError(
                    f"wait_waves[{index}] includes multiple aliases for {lane_ref}: "
                    + ", ".join(aliases)
                )
    missing_waits = sorted(
        lane_ref
        for lane_ref, identifiers in identities_by_ref.items()
        if not identifiers.intersection(terminal_targets)
    )
    if missing_waits:
        raise ExecutionEfficiencyError(
            "execution-efficiency terminal wait telemetry does not cover agent identities: "
            + ", ".join(missing_waits)
        )
