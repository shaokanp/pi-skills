#!/usr/bin/env python3
"""Portable Clean Orchestrator runtime contracts and validators.

This module owns schemas, admission arithmetic, semantic-gate sealing, and
completion-density validation. Native spawn/block/join/queue/finalization
primitives remain host-owned and are represented only as capabilities.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any


CLEAN_RUNTIME_SCHEMA = "agent-workflow.clean-orchestrator-runtime.v1"
CAPABILITY_SCHEMA = "agent-workflow.runtime-capabilities.v1"
SEMANTIC_GATE_SCHEMA = "agent-workflow.semantic-gate-graph.v1"
COMPLETION_DENSITY_SCHEMA = "agent-workflow.completion-density.v1"
CONTROLLER_RECEIPT_SCHEMA = "agent-workflow.controller-receipt.v1"

DELIVERY_LEVELS = {"capability_required", "bounded_interim", "target"}
ADMISSION_DECISIONS = {
    "reject_capability_required",
    "reject_unsupported",
    "reject_bounds",
    "admit_bounded_interim",
    "admit_target",
}
COMPOUND_OPERATIONS = {"execute_round", "advance_run", "finalize_run"}
COMPLETION_CLASSES = {
    "initial_dispatch",
    "decision_gate",
    "repair_gate",
    "human_gate",
    "final_synthesis",
    "native_sibling_terminal_reactivation",
    "deterministic_tool_result_reactivation",
    "status_only",
    "wrapper_wait",
    "partial_terminal",
}
FORBIDDEN_COMPLETION_CLASSES = {"status_only", "wrapper_wait", "partial_terminal"}
TARGET_ONLY_CAPABILITIES = {
    "atomic_orchestrator_spawn_and_block",
    "all_terminal_durable_barrier",
    "progress_suppression",
    "automatic_session_registration",
    "resumable_barrier",
    "minimal_profile",
    "terminal_host_finalization",
}
INTERIM_REQUIRED_CAPABILITIES = {
    "clean_context",
    "direct_terminal_event_wait",
    "subtree_discovery",
    "exact_cumulative_token_events",
}
ROUND_DIGEST_FIELDS = (
    "round_id",
    "objective",
    "runtime_mode",
    "dispatch_mode",
    "max_parallelism",
    "semantic_return_gate",
    "compound_operation",
    "deterministic_steps",
    "completion_budget",
    "semantic_gates",
)


class CleanOrchestratorError(ValueError):
    """Raised when a clean runtime contract fails closed."""


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CleanOrchestratorError(f"{label} must be an object")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise CleanOrchestratorError(f"{label} must be a list")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CleanOrchestratorError(f"{label} must be a non-empty string")
    return value


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise CleanOrchestratorError(f"{label} must be an integer >= {minimum}")
    return value


def _rfc3339(value: Any, label: str) -> str:
    text = _text(value, label)
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise CleanOrchestratorError(f"{label} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise CleanOrchestratorError(f"{label} must include a timezone")
    return text


def build_unknown_capabilities(runtime_class: str = "codex_builtin_subagents") -> dict[str, Any]:
    """Return a fail-closed capability scaffold; it is not probe evidence."""

    values: dict[str, Any] = {
        name: None
        for name in sorted(TARGET_ONLY_CAPABILITIES | INTERIM_REQUIRED_CAPABILITIES)
    }
    values.update(
        {
            "queued_native_dispatch": None,
            "generation_rotation": None,
            "max_native_wait_ms": None,
            "max_concurrent_sessions": None,
            "available_child_slots": None,
        }
    )
    return {
        "schema_version": CAPABILITY_SCHEMA,
        "protocol_version": "clean-orchestrator-host.v1",
        "runtime_class": runtime_class,
        "observed_at": None,
        "source": "host_runtime_required",
        "status": "unknown",
        "values": values,
    }


def build_clean_runtime_contract(runtime_class: str = "codex_builtin_subagents") -> dict[str, Any]:
    """Scaffold the default-safe contract without inventing host facts."""

    return {
        "schema_version": CLEAN_RUNTIME_SCHEMA,
        "activation": "native_default",
        "topology": "main_single_clean_orchestrator_nested_workers",
        "context_origin": "clean_packet",
        "parent_transcript": "excluded",
        "orchestrator_fork_turns": "none",
        "delivery_level": "capability_required",
        "legacy_main_fanout": "forbidden_production",
        "capabilities": build_unknown_capabilities(runtime_class),
        "admission": {
            "planned_rounds": 0,
            "planned_lanes": 0,
            "max_parallelism": 0,
            "workflow_deadline_ms": 0,
            "estimated_coordinator_completions_worst_case": 0,
            "estimated_coordinator_tokens_worst_case": 0,
            "max_coordinator_completions": 8,
            "max_coordinator_tokens": 0,
            "coordinator_token_calibration": {
                "status": "unknown",
                "source": "host_runtime_calibration_required",
                "tokens_per_completion_upper_bound": 0,
            },
            "fixed_protocol_overhead_completions": 0,
            "decision": "reject_capability_required",
            "reason": "Host capability evidence is required before dispatch.",
        },
        "completion_classification_version": COMPLETION_DENSITY_SCHEMA,
        "host_owned_boundaries": sorted(
            TARGET_ONLY_CAPABILITIES | {"queued_native_dispatch", "generation_rotation"}
        ),
    }


def round_gate_digest(round_plan: dict[str, Any]) -> str:
    return canonical_sha256({key: round_plan.get(key) for key in ROUND_DIGEST_FIELDS})


def seal_round_gate_graph(round_plan: dict[str, Any]) -> dict[str, Any]:
    seal = {
        "schema_version": SEMANTIC_GATE_SCHEMA,
        "sealed_before_dispatch": True,
        "content_sha256": round_gate_digest(round_plan),
    }
    round_plan["gate_graph_seal"] = seal
    return seal


def build_round_runtime_contract(
    *,
    round_id: str,
    objective: str,
    lane_ids: list[str],
) -> dict[str, Any]:
    max_parallelism = len(lane_ids)
    round_plan: dict[str, Any] = {
        "round_id": round_id,
        "objective": objective,
        "runtime_mode": "capability_required",
        "dispatch_mode": "capability_required",
        "max_parallelism": max_parallelism,
        "semantic_return_gate": "all_terminal",
        "compound_operation": "execute_round",
        "deterministic_steps": [
            "workspace_and_schema_validation",
            "batch_dispatch_and_registration",
            "durable_join_and_result_collection",
            "artifact_commit_render_and_accounting",
        ],
        "completion_budget": {
            "initial_dispatch_reactivations_max": max_parallelism,
            "round_result_reactivations": 1,
            "extra_semantic_repairs": 0,
            "housekeeping_completions": 0,
            "status_only_completions": 0,
            "wrapper_wait_completions": 0,
            "partial_terminal_completions": 0,
            "deterministic_tool_result_reactivations_max": 0,
            "native_sibling_terminal_reactivations_max": 0,
            "absolute_coordinator_completions_max": 8,
        },
        "semantic_gates": [
            {
                "gate_id": f"{round_id}-all-terminal",
                "gate_class": "decision_gate",
                "trigger": "all planned lane attempts are terminal",
                "allowed_decisions": ["advance", "repair", "human_gate", "blocked"],
            }
        ],
    }
    seal_round_gate_graph(round_plan)
    return round_plan


def validate_capabilities(value: Any, *, allow_unknown: bool) -> dict[str, Any]:
    capabilities = _object(value, "clean_orchestrator_runtime.capabilities")
    if capabilities.get("schema_version") != CAPABILITY_SCHEMA:
        raise CleanOrchestratorError(
            f"capabilities.schema_version must be {CAPABILITY_SCHEMA}"
        )
    if capabilities.get("protocol_version") != "clean-orchestrator-host.v1":
        raise CleanOrchestratorError(
            "capabilities.protocol_version must be clean-orchestrator-host.v1"
        )
    _text(capabilities.get("runtime_class"), "capabilities.runtime_class")
    status = capabilities.get("status")
    if status not in {"unknown", "observed"}:
        raise CleanOrchestratorError("capabilities.status must be unknown or observed")
    if status == "unknown":
        if not allow_unknown:
            raise CleanOrchestratorError("host capabilities are unknown; dispatch is unsupported")
        return capabilities
    _rfc3339(capabilities.get("observed_at"), "capabilities.observed_at")
    _text(capabilities.get("source"), "capabilities.source")
    values = _object(capabilities.get("values"), "capabilities.values")
    boolean_names = (
        TARGET_ONLY_CAPABILITIES
        | INTERIM_REQUIRED_CAPABILITIES
        | {"queued_native_dispatch", "generation_rotation"}
    )
    for name in boolean_names:
        if not isinstance(values.get(name), bool):
            raise CleanOrchestratorError(f"capabilities.values.{name} must be boolean")
    for name in ("max_native_wait_ms", "max_concurrent_sessions", "available_child_slots"):
        _integer(values.get(name), f"capabilities.values.{name}", minimum=1)
    return capabilities


def derive_admission(
    delivery_level: str,
    capabilities: dict[str, Any],
    admission: dict[str, Any],
    rounds: list[dict[str, Any]],
) -> str:
    if capabilities.get("status") != "observed":
        return "reject_capability_required"
    values = _object(capabilities.get("values"), "capabilities.values")
    if not all(values.get(name) is True for name in INTERIM_REQUIRED_CAPABILITIES):
        return "reject_unsupported"
    deadline = _integer(admission.get("workflow_deadline_ms"), "admission.workflow_deadline_ms")
    max_parallelism = _integer(admission.get("max_parallelism"), "admission.max_parallelism")
    worst_completions = _integer(
        admission.get("estimated_coordinator_completions_worst_case"),
        "admission.estimated_coordinator_completions_worst_case",
    )
    worst_tokens = _integer(
        admission.get("estimated_coordinator_tokens_worst_case"),
        "admission.estimated_coordinator_tokens_worst_case",
    )
    max_completions = _integer(
        admission.get("max_coordinator_completions"),
        "admission.max_coordinator_completions",
        minimum=1,
    )
    max_tokens = _integer(
        admission.get("max_coordinator_tokens"),
        "admission.max_coordinator_tokens",
        minimum=1,
    )
    calibration = _object(
        admission.get("coordinator_token_calibration"),
        "admission.coordinator_token_calibration",
    )
    if calibration.get("status") != "observed":
        return "reject_bounds"
    _text(calibration.get("source"), "admission.coordinator_token_calibration.source")
    tokens_per_completion = _integer(
        calibration.get("tokens_per_completion_upper_bound"),
        "admission.coordinator_token_calibration.tokens_per_completion_upper_bound",
        minimum=1,
    )
    fixed_overhead = _integer(
        admission.get("fixed_protocol_overhead_completions"),
        "admission.fixed_protocol_overhead_completions",
        minimum=1,
    )
    derived_worst_completions = fixed_overhead
    for item in rounds:
        budget = _object(item.get("completion_budget"), f"{item.get('round_id')}.completion_budget")
        round_worst = (
            _integer(budget.get("initial_dispatch_reactivations_max"), "initial_dispatch_reactivations_max")
            + _integer(budget.get("round_result_reactivations"), "round_result_reactivations")
            + 2 * _integer(budget.get("extra_semantic_repairs"), "extra_semantic_repairs")
            + _integer(budget.get("deterministic_tool_result_reactivations_max"), "deterministic_tool_result_reactivations_max")
            + _integer(budget.get("native_sibling_terminal_reactivations_max"), "native_sibling_terminal_reactivations_max")
        )
        if round_worst > _integer(
            budget.get("absolute_coordinator_completions_max"),
            "absolute_coordinator_completions_max",
            minimum=1,
        ):
            return "reject_bounds"
        derived_worst_completions += round_worst
    derived_worst_tokens = derived_worst_completions * tokens_per_completion
    bounds_ok = (
        deadline <= values["max_native_wait_ms"]
        and worst_completions == derived_worst_completions
        and worst_tokens == derived_worst_tokens
        and worst_completions <= max_completions
        and worst_tokens <= max_tokens
    )
    if not values["queued_native_dispatch"]:
        bounds_ok = bounds_ok and max_parallelism <= values["available_child_slots"]
    if not bounds_ok:
        return "reject_bounds"
    if delivery_level == "target":
        if not all(values.get(name) is True for name in TARGET_ONLY_CAPABILITIES):
            return "reject_unsupported"
        return "admit_target"
    if delivery_level == "bounded_interim":
        return "admit_bounded_interim"
    return "reject_capability_required"


def validate_round_contract(round_plan: Any, *, allow_draft: bool) -> dict[str, Any]:
    plan = _object(round_plan, "round")
    round_id = _text(plan.get("round_id"), "round.round_id")
    _text(plan.get("objective"), f"{round_id}.objective")
    runtime_mode = plan.get("runtime_mode")
    if runtime_mode not in DELIVERY_LEVELS:
        raise CleanOrchestratorError(f"{round_id}.runtime_mode must be a delivery level")
    _text(plan.get("dispatch_mode"), f"{round_id}.dispatch_mode")
    _integer(plan.get("max_parallelism"), f"{round_id}.max_parallelism")
    _text(plan.get("semantic_return_gate"), f"{round_id}.semantic_return_gate")
    if plan.get("compound_operation") not in COMPOUND_OPERATIONS:
        raise CleanOrchestratorError(
            f"{round_id}.compound_operation must be one of {sorted(COMPOUND_OPERATIONS)}"
        )
    steps = _list(plan.get("deterministic_steps"), f"{round_id}.deterministic_steps")
    if not steps or any(not isinstance(step, str) or not step.strip() for step in steps):
        raise CleanOrchestratorError(f"{round_id}.deterministic_steps must be non-empty strings")
    if len(steps) != len(set(steps)):
        raise CleanOrchestratorError(f"{round_id}.deterministic_steps must be unique")

    budget = _object(plan.get("completion_budget"), f"{round_id}.completion_budget")
    required_budget = {
        "initial_dispatch_reactivations_max",
        "round_result_reactivations",
        "extra_semantic_repairs",
        "housekeeping_completions",
        "status_only_completions",
        "wrapper_wait_completions",
        "partial_terminal_completions",
        "deterministic_tool_result_reactivations_max",
        "native_sibling_terminal_reactivations_max",
        "absolute_coordinator_completions_max",
    }
    missing = sorted(required_budget - set(budget))
    if missing:
        raise CleanOrchestratorError(
            f"{round_id}.completion_budget missing: {', '.join(missing)}"
        )
    for name in required_budget:
        _integer(budget.get(name), f"{round_id}.completion_budget.{name}")
    if budget["round_result_reactivations"] != 1:
        raise CleanOrchestratorError(
            f"{round_id} must plan exactly one round-result reactivation"
        )
    if (
        not allow_draft
        and runtime_mode != "capability_required"
        and budget["initial_dispatch_reactivations_max"] < 1
    ):
        raise CleanOrchestratorError(
            f"{round_id} must budget at least one initial dispatch reactivation"
        )
    if runtime_mode == "bounded_interim" and budget[
        "initial_dispatch_reactivations_max"
    ] < plan["max_parallelism"]:
        raise CleanOrchestratorError(
            f"{round_id} bounded interim must budget one initial dispatch reactivation per lane"
        )
    for name in (
        "housekeeping_completions",
        "status_only_completions",
        "wrapper_wait_completions",
        "partial_terminal_completions",
    ):
        if budget[name] != 0:
            raise CleanOrchestratorError(f"{round_id}.completion_budget.{name} must be zero")
    if budget["absolute_coordinator_completions_max"] > 20:
        raise CleanOrchestratorError(
            f"{round_id}.absolute_coordinator_completions_max must be <= 20"
        )
    if runtime_mode == "target":
        if budget["deterministic_tool_result_reactivations_max"] != 0:
            raise CleanOrchestratorError(
                f"{round_id} target mode forbids deterministic tool-result reactivations"
            )
        if budget["native_sibling_terminal_reactivations_max"] != 0:
            raise CleanOrchestratorError(
                f"{round_id} target mode forbids sibling-terminal reactivations"
            )
        if budget["absolute_coordinator_completions_max"] > 8:
            raise CleanOrchestratorError(
                f"{round_id} target mode absolute coordinator budget must be <= 8"
            )

    gates = _list(plan.get("semantic_gates"), f"{round_id}.semantic_gates")
    if not gates:
        raise CleanOrchestratorError(f"{round_id}.semantic_gates must not be empty")
    gate_ids: set[str] = set()
    for index, raw_gate in enumerate(gates, start=1):
        gate = _object(raw_gate, f"{round_id}.semantic_gates[{index}]")
        gate_id = _text(gate.get("gate_id"), f"{round_id}.semantic_gates[{index}].gate_id")
        if gate_id in gate_ids:
            raise CleanOrchestratorError(f"{round_id}.semantic_gates gate_id values must be unique")
        gate_ids.add(gate_id)
        if gate.get("gate_class") not in {
            "decision_gate",
            "repair_gate",
            "human_gate",
            "final_gate",
        }:
            raise CleanOrchestratorError(f"{gate_id}.gate_class is invalid")
        _text(gate.get("trigger"), f"{gate_id}.trigger")
        decisions = _list(gate.get("allowed_decisions"), f"{gate_id}.allowed_decisions")
        if not decisions or any(not isinstance(item, str) or not item for item in decisions):
            raise CleanOrchestratorError(f"{gate_id}.allowed_decisions must be non-empty strings")

    seal = _object(plan.get("gate_graph_seal"), f"{round_id}.gate_graph_seal")
    if seal.get("schema_version") != SEMANTIC_GATE_SCHEMA:
        raise CleanOrchestratorError(
            f"{round_id}.gate_graph_seal.schema_version must be {SEMANTIC_GATE_SCHEMA}"
        )
    if seal.get("sealed_before_dispatch") is not True:
        raise CleanOrchestratorError(f"{round_id}.gate_graph_seal must be sealed before dispatch")
    if not allow_draft and seal.get("content_sha256") != round_gate_digest(plan):
        raise CleanOrchestratorError(f"{round_id}.gate_graph_seal digest mismatch")
    return plan


def validate_clean_runtime_contract(
    orchestration: dict[str, Any],
    *,
    allow_draft: bool,
    required: bool = False,
) -> dict[str, Any] | None:
    raw = orchestration.get("clean_orchestrator_runtime")
    if raw is None:
        if required:
            raise CleanOrchestratorError("clean_orchestrator_runtime is required")
        return None
    runtime = _object(raw, "clean_orchestrator_runtime")
    if runtime.get("schema_version") != CLEAN_RUNTIME_SCHEMA:
        raise CleanOrchestratorError(
            f"clean_orchestrator_runtime.schema_version must be {CLEAN_RUNTIME_SCHEMA}"
        )
    expected = {
        "activation": "native_default",
        "topology": "main_single_clean_orchestrator_nested_workers",
        "context_origin": "clean_packet",
        "parent_transcript": "excluded",
        "orchestrator_fork_turns": "none",
        "legacy_main_fanout": "forbidden_production",
        "completion_classification_version": COMPLETION_DENSITY_SCHEMA,
    }
    for key, value in expected.items():
        if runtime.get(key) != value:
            raise CleanOrchestratorError(f"clean_orchestrator_runtime.{key} must be {value}")
    delivery = runtime.get("delivery_level")
    if delivery not in DELIVERY_LEVELS:
        raise CleanOrchestratorError(
            f"clean_orchestrator_runtime.delivery_level must be one of {sorted(DELIVERY_LEVELS)}"
        )
    capabilities = validate_capabilities(runtime.get("capabilities"), allow_unknown=allow_draft)
    admission = _object(runtime.get("admission"), "clean_orchestrator_runtime.admission")
    for name in (
        "planned_rounds",
        "planned_lanes",
        "max_parallelism",
        "workflow_deadline_ms",
        "estimated_coordinator_completions_worst_case",
        "estimated_coordinator_tokens_worst_case",
        "max_coordinator_completions",
        "max_coordinator_tokens",
    ):
        _integer(admission.get(name), f"clean_orchestrator_runtime.admission.{name}")
    decision = admission.get("decision")
    if decision not in ADMISSION_DECISIONS:
        raise CleanOrchestratorError(
            f"clean_orchestrator_runtime.admission.decision must be one of {sorted(ADMISSION_DECISIONS)}"
        )
    _text(admission.get("reason"), "clean_orchestrator_runtime.admission.reason")
    actual_rounds = [item for item in orchestration.get("rounds", []) if isinstance(item, dict)]
    actual_lanes = [
        lane
        for round_plan in actual_rounds
        for lane in round_plan.get("lanes", [])
        if isinstance(lane, dict) and lane.get("enabled") is True
    ]
    if not allow_draft:
        if admission["planned_rounds"] != len(actual_rounds):
            raise CleanOrchestratorError("admission.planned_rounds must match orchestration rounds")
        if admission["planned_lanes"] != len(actual_lanes):
            raise CleanOrchestratorError("admission.planned_lanes must match enabled lanes")
        max_parallelism = max(
            (_integer(item.get("max_parallelism"), "round.max_parallelism") for item in actual_rounds),
            default=0,
        )
        if admission["max_parallelism"] != max_parallelism:
            raise CleanOrchestratorError(
                "admission.max_parallelism must match the largest planned round"
            )
        derived = derive_admission(str(delivery), capabilities, admission, actual_rounds)
        if decision != derived:
            raise CleanOrchestratorError(
                f"admission.decision must be derived as {derived}, not {decision}"
            )
        if derived not in {"admit_bounded_interim", "admit_target"}:
            raise CleanOrchestratorError(f"clean runtime admission rejected dispatch: {derived}")
        if delivery == "target" and derived != "admit_target":
            raise CleanOrchestratorError("target delivery requires target admission")
        if delivery == "bounded_interim" and derived != "admit_bounded_interim":
            raise CleanOrchestratorError("bounded interim delivery requires interim admission")
    boundaries = _list(
        runtime.get("host_owned_boundaries"),
        "clean_orchestrator_runtime.host_owned_boundaries",
    )
    missing_boundaries = sorted(
        (TARGET_ONLY_CAPABILITIES | {"queued_native_dispatch", "generation_rotation"})
        - set(boundaries)
    )
    if missing_boundaries:
        raise CleanOrchestratorError(
            "host_owned_boundaries missing: " + ", ".join(missing_boundaries)
        )
    for round_plan in actual_rounds:
        validate_round_contract(round_plan, allow_draft=allow_draft)
        if not allow_draft and round_plan.get("runtime_mode") != delivery:
            raise CleanOrchestratorError(
                f"{round_plan.get('round_id')}.runtime_mode must match delivery_level {delivery}"
            )
    return runtime


def validate_completion_density(
    value: Any,
    orchestration: dict[str, Any],
    *,
    final: bool,
) -> dict[str, Any]:
    ledger = _object(value, "runner-evidence.json.completion_density")
    if ledger.get("schema_version") != COMPLETION_DENSITY_SCHEMA:
        raise CleanOrchestratorError(
            f"completion_density.schema_version must be {COMPLETION_DENSITY_SCHEMA}"
        )
    if ledger.get("source") != "runtime_session_events":
        raise CleanOrchestratorError("completion_density.source must be runtime_session_events")
    entries = _list(ledger.get("entries"), "completion_density.entries")
    event_refs: set[str] = set()
    rounds = {
        item.get("round_id"): item
        for item in orchestration.get("rounds", [])
        if isinstance(item, dict) and isinstance(item.get("round_id"), str)
    }
    actual_by_round: dict[str, dict[str, int]] = {
        round_id: {name: 0 for name in COMPLETION_CLASSES} for round_id in rounds
    }
    for index, raw in enumerate(entries, start=1):
        entry = _object(raw, f"completion_density.entries[{index}]")
        event_ref = _text(entry.get("event_ref"), f"completion_density.entries[{index}].event_ref")
        if event_ref in event_refs:
            raise CleanOrchestratorError("completion_density event_ref values must be unique")
        event_refs.add(event_ref)
        round_id = entry.get("round_id")
        if round_id not in rounds:
            raise CleanOrchestratorError(f"completion entry references unknown round {round_id}")
        completion_class = entry.get("class")
        if completion_class not in COMPLETION_CLASSES:
            raise CleanOrchestratorError(f"completion entry class is invalid: {completion_class}")
        actual_by_round[str(round_id)][str(completion_class)] += 1
        gate_id = entry.get("gate_id")
        if completion_class in {
            "decision_gate",
            "repair_gate",
            "human_gate",
            "final_synthesis",
        }:
            gates = {
                gate.get("gate_id")
                for gate in rounds[str(round_id)].get("semantic_gates", [])
                if isinstance(gate, dict)
            }
            if gate_id not in gates:
                raise CleanOrchestratorError(
                    f"semantic completion {event_ref} must reference a sealed gate"
                )
        if completion_class == "repair_gate":
            _text(entry.get("trigger_evidence_ref"), f"{event_ref}.trigger_evidence_ref")
            _text(entry.get("decision_diff"), f"{event_ref}.decision_diff")
    projection = _object(ledger.get("rounds"), "completion_density.rounds")
    for round_id, plan in rounds.items():
        counts = actual_by_round[round_id]
        item = _object(projection.get(round_id), f"completion_density.rounds.{round_id}")
        if item.get("gate_graph_sha256") != plan.get("gate_graph_seal", {}).get("content_sha256"):
            raise CleanOrchestratorError(f"{round_id} completion projection gate digest mismatch")
        if item.get("actual_counts") != counts:
            raise CleanOrchestratorError(f"{round_id} completion projection counts drift")
        actual_total = sum(counts.values())
        if item.get("actual_coordinator_completions") != actual_total:
            raise CleanOrchestratorError(f"{round_id} actual completion total drift")
        budget = _object(plan.get("completion_budget"), f"{round_id}.completion_budget")
        if final and actual_total > budget["absolute_coordinator_completions_max"]:
            raise CleanOrchestratorError(f"{round_id} exceeds its absolute completion budget")
        if any(counts[name] for name in FORBIDDEN_COMPLETION_CLASSES):
            raise CleanOrchestratorError(f"{round_id} contains forbidden completion classes")
        if plan.get("runtime_mode") == "target":
            if counts["decision_gate"] != budget["round_result_reactivations"]:
                raise CleanOrchestratorError(f"{round_id} target decision-gate count mismatch")
            if counts["native_sibling_terminal_reactivation"] != 0:
                raise CleanOrchestratorError(f"{round_id} target mode forbids sibling wakes")
            if counts["deterministic_tool_result_reactivation"] != 0:
                raise CleanOrchestratorError(
                    f"{round_id} target mode forbids deterministic-result wakes"
                )
        else:
            expected_decisions = budget["round_result_reactivations"]
            if final and counts["decision_gate"] != expected_decisions:
                raise CleanOrchestratorError(
                    f"{round_id} bounded interim decision-gate count mismatch"
                )
            if counts["decision_gate"] > (
                budget["round_result_reactivations"]
                + budget["extra_semantic_repairs"]
            ):
                raise CleanOrchestratorError(
                    f"{round_id} exceeds decision-gate wake bound"
                )
            if counts["repair_gate"] > budget["extra_semantic_repairs"]:
                raise CleanOrchestratorError(
                    f"{round_id} exceeds repair-gate wake bound"
                )
            if (
                counts["native_sibling_terminal_reactivation"]
                > budget["native_sibling_terminal_reactivations_max"]
            ):
                raise CleanOrchestratorError(f"{round_id} exceeds sibling-terminal wake bound")
            if (
                counts["deterministic_tool_result_reactivation"]
                > budget["deterministic_tool_result_reactivations_max"]
            ):
                raise CleanOrchestratorError(
                    f"{round_id} exceeds deterministic-result wake bound"
                )
    return ledger


def prepare_clean_runtime_dispatch(orchestration: dict[str, Any]) -> int:
    """Refresh derived pre-dispatch projections without inventing capabilities."""

    runtime = orchestration.get("clean_orchestrator_runtime")
    if not isinstance(runtime, dict):
        return 0
    rounds = [item for item in orchestration.get("rounds", []) if isinstance(item, dict)]
    for round_plan in rounds:
        seal_round_gate_graph(round_plan)
    lanes = [
        lane
        for round_plan in rounds
        for lane in round_plan.get("lanes", [])
        if isinstance(lane, dict) and lane.get("enabled") is True
    ]
    admission = _object(runtime.get("admission"), "clean_orchestrator_runtime.admission")
    admission["planned_rounds"] = len(rounds)
    admission["planned_lanes"] = len(lanes)
    admission["max_parallelism"] = max(
        (int(item.get("max_parallelism", 0)) for item in rounds),
        default=0,
    )
    capabilities = _object(runtime.get("capabilities"), "clean_orchestrator_runtime.capabilities")
    decision = derive_admission(
        str(runtime.get("delivery_level")), capabilities, admission, rounds
    )
    admission["decision"] = decision
    admission["reason"] = {
        "reject_capability_required": "Host capability evidence and explicit delivery mode are required before dispatch.",
        "reject_unsupported": "Observed host capabilities do not satisfy the selected delivery mode.",
        "reject_bounds": "Planned deadline, capacity, completion, or token bounds exceed observed limits.",
        "admit_bounded_interim": "Observed interim capabilities and all declared worst-case bounds pass.",
        "admit_target": "Observed target capabilities and all declared worst-case bounds pass.",
    }[decision]
    return len(rounds)


def build_empty_completion_density(orchestration: dict[str, Any]) -> dict[str, Any]:
    counts = {name: 0 for name in COMPLETION_CLASSES}
    return {
        "schema_version": COMPLETION_DENSITY_SCHEMA,
        "source": "runtime_session_events",
        "entries": [],
        "rounds": {
            str(item["round_id"]): {
                "gate_graph_sha256": item.get("gate_graph_seal", {}).get(
                    "content_sha256"
                ),
                "actual_counts": dict(counts),
                "actual_coordinator_completions": 0,
                "budget_resolution": None,
            }
            for item in orchestration.get("rounds", [])
            if isinstance(item, dict) and isinstance(item.get("round_id"), str)
        },
    }
