#!/usr/bin/env python3
"""Replay Clean Orchestrator completion density from raw Codex runtime events.

The raw session log is authoritative. ``runner-evidence.json`` is only a
projection written from this replay and is rejected when it drifts. Native
spawn/join/queue/rotation/finalization remain host-owned capabilities.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
import os
from pathlib import Path
from typing import Any

from clean_orchestrator import (
    COMPLETION_CLASSES,
    COMPLETION_DENSITY_SCHEMA,
    canonical_sha256,
    validate_completion_density,
)
from routing_runtime import RoutingRuntimeError, read_session_profile
from token_accounting import (
    TokenAccountingError,
    default_runtime_root,
    locate_session,
)


LEGACY_OBSERVATION_SCHEMA = "agent-workflow.runtime-observations.v1"
OBSERVATION_SCHEMA = "agent-workflow.runtime-observations.v2"
HARNESS_SCHEMA = "agent-workflow.runtime-harness.v1"


class RuntimeHarnessError(ValueError):
    """Raised when raw runtime observations cannot support an exact claim."""


def load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeHarnessError(f"Cannot read JSON object: {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeHarnessError(f"Expected JSON object: {path}")
    return value


def atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def event_line(event_ref: Any, session_id: str) -> int:
    prefix = f"codex:{session_id}:token_count:"
    if not isinstance(event_ref, str) or not event_ref.startswith(prefix):
        raise RuntimeHarnessError("Accounting start must reference a Codex token_count event")
    try:
        return int(event_ref.removeprefix(prefix))
    except ValueError as exc:
        raise RuntimeHarnessError("Accounting start event line is malformed") from exc


def prefix_sha256(path: Path, through_line: int) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for line_number, raw in enumerate(handle, start=1):
            if line_number > through_line:
                break
            digest.update(raw)
    return "sha256:" + digest.hexdigest()


def raw_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(raw_text(item) for item in value)
    if isinstance(value, dict):
        preferred = [value.get(key) for key in ("text", "output", "message", "summary")]
        text = "\n".join(raw_text(item) for item in preferred if item is not None)
        return text or json.dumps(value, ensure_ascii=False, sort_keys=True)
    return "" if value is None else str(value)


def parse_call_input(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {"raw": value}
    return decoded if isinstance(decoded, dict) else {"raw": value}


def call_input_text(value: Any) -> str:
    """Preserve structured routing fields even when message text is encrypted."""

    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return raw_text(value)


def structured_target(value: Any) -> str | None:
    decoded = parse_call_input(value)
    for key in ("target", "task_name"):
        target = decoded.get(key)
        if isinstance(target, str) and target:
            return target
    return None


def normalized_tool_name(name: Any) -> str:
    return str(name or "").lower().replace("-", "_").replace(".", "_")


def routed_dispatch_events(
    session_path: Path,
    session_id: str,
    *,
    start_line: int,
    through_line: int,
    orchestration: dict[str, Any],
    runner_evidence: dict[str, Any] | None = None,
    participants: list[dict[str, Any]] | None = None,
    runtime_root: Path | None = None,
    session_paths: dict[str, Path] | None = None,
) -> list[dict[str, Any]]:
    """Bind each native spawn request and child runtime to one routed attempt."""

    requirement = orchestration.get("model_routing_requirement")
    if requirement is None:
        return []
    block = orchestration.get("model_routing")
    if not isinstance(block, dict) or block.get("enabled") is not True:
        raise RuntimeHarnessError("mandatory model routing lacks an enabled routing block")
    effort = block.get("reasoning_effort", {}).get("value")
    if not isinstance(effort, str):
        raise RuntimeHarnessError("mandatory model routing lacks a locked reasoning effort")
    records = {
        f"{item.get('round_id')}:{item.get('lane_id')}": item
        for item in (runner_evidence or {}).get("agents", [])
        if isinstance(item, dict)
    }
    expected: dict[str, list[dict[str, Any]]] = {}
    for round_plan in orchestration.get("rounds", []):
        if not isinstance(round_plan, dict):
            continue
        for lane in round_plan.get("lanes", []):
            if not isinstance(lane, dict) or lane.get("enabled") is not True:
                continue
            lane_id = lane.get("id")
            routing = lane.get("routing")
            selected = routing.get("selected") if isinstance(routing, dict) else None
            if (
                not isinstance(lane_id, str)
                or not isinstance(selected, dict)
                or not isinstance(selected.get("model"), str)
            ):
                raise RuntimeHarnessError(
                    "mandatory model routing requires a planned selected route for every lane"
                )
            selected_effort = selected.get("effort")
            if selected_effort != effort:
                raise RuntimeHarnessError(
                    f"{lane_id} selected effort drifts from the workflow session effort"
                )
            round_id = round_plan.get("round_id")
            task_name = lane_id.replace("-", "_")
            record = records.get(f"{round_id}:{lane_id}")
            attempts = record.get("attempts") if isinstance(record, dict) else None
            if runner_evidence is not None and not isinstance(attempts, list):
                raise RuntimeHarnessError(f"{round_id}:{lane_id} lacks a routed attempt ledger")
            route_attempts = attempts if isinstance(attempts, list) else [
                {
                    "attempt_id": "planned-initial",
                    "route": selected,
                    "lifecycle": {},
                }
            ]
            for attempt in route_attempts:
                route = attempt.get("route") if isinstance(attempt, dict) else None
                lifecycle = attempt.get("lifecycle") if isinstance(attempt, dict) else None
                if (
                    not isinstance(attempt, dict)
                    or not isinstance(attempt.get("attempt_id"), str)
                    or not isinstance(route, dict)
                    or not isinstance(route.get("model"), str)
                    or route.get("effort") != effort
                    or not isinstance(lifecycle, dict)
                ):
                    raise RuntimeHarnessError(
                        f"{round_id}:{lane_id} has an invalid routed attempt for raw replay"
                    )
                expected.setdefault(task_name, []).append(
                    {
                        "round_id": round_id,
                        "lane_id": lane_id,
                        "attempt_id": attempt["attempt_id"],
                        "model": route["model"],
                        "effort": route["effort"],
                        "agent_id": lifecycle.get("agent_id") or lifecycle.get("native_handle"),
                    }
                )
    observed: list[dict[str, Any]] = []
    output_by_call: dict[str, dict[str, Any]] = {}
    activity_by_call: dict[str, dict[str, Any]] = {}
    spawn_calls: list[dict[str, Any]] = []
    with session_path.open("rb") as handle:
        for line_number, raw in enumerate(handle, start=1):
            if line_number <= start_line or line_number > through_line:
                continue
            try:
                item = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RuntimeHarnessError(
                    f"Invalid Codex runtime JSONL at {session_path}:{line_number}"
                ) from exc
            payload = item.get("payload") if isinstance(item, dict) else None
            if (
                item.get("type") == "event_msg"
                and isinstance(payload, dict)
                and payload.get("type") == "sub_agent_activity"
                and payload.get("kind") == "started"
                and isinstance(payload.get("event_id"), str)
                and isinstance(payload.get("agent_thread_id"), str)
            ):
                activity_by_call[payload["event_id"]] = {
                    "line": line_number,
                    "agent_thread_id": payload["agent_thread_id"],
                    "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
                }
                continue
            if (
                item.get("type") == "response_item"
                and isinstance(payload, dict)
                and payload.get("type") in {"custom_tool_call_output", "function_call_output"}
                and isinstance(payload.get("call_id"), str)
            ):
                output_by_call[payload["call_id"]] = {
                    "line": line_number,
                    "payload": payload,
                    "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
                }
                continue
            if (
                item.get("type") != "response_item"
                or not isinstance(payload, dict)
                or payload.get("type") not in {"custom_tool_call", "function_call"}
                or not normalized_tool_name(payload.get("name")).endswith("spawn_agent")
            ):
                continue
            spawn_calls.append(
                {
                    "line": line_number,
                    "raw": raw,
                    "payload": payload,
                }
            )
    offsets = {name: 0 for name in expected}
    participants = participants or []
    session_paths = session_paths or {}
    for call in spawn_calls:
        line_number = call["line"]
        raw = call["raw"]
        payload = call["payload"]
        arguments = parse_call_input(payload.get("input") or payload.get("arguments"))
        task_name = arguments.get("task_name")
        if not isinstance(task_name, str) or task_name not in expected:
            raise RuntimeHarnessError(
                f"raw spawn at line {line_number} must use the normalized routed lane id as task_name"
            )
        ordinal = offsets[task_name]
        if ordinal >= len(expected[task_name]):
            raise RuntimeHarnessError(f"raw spawn count exceeds routed attempts for {task_name}")
        attempt = expected[task_name][ordinal]
        offsets[task_name] += 1
        lane_id = attempt["lane_id"]
        planned_model = attempt["model"]
        actual_model = arguments.get("model")
        actual_effort = arguments.get("thinking")
        if actual_model != planned_model or actual_effort != effort:
            raise RuntimeHarnessError(
                f"raw spawn for {lane_id} must dispatch {planned_model}/{effort}; "
                f"observed {actual_model}/{actual_effort}"
            )
        call_id = payload.get("call_id")
        output = output_by_call.get(call_id) if isinstance(call_id, str) else None
        if output is None:
            raise RuntimeHarnessError(f"raw spawn for {lane_id} lacks a successful tool result")
        activity = activity_by_call.get(call_id) if isinstance(call_id, str) else None
        if activity is None:
            raise RuntimeHarnessError(
                f"raw spawn for {lane_id} lacks a started child-runtime event"
            )
        agent_id = activity["agent_thread_id"]
        expected_agent_id = attempt.get("agent_id")
        output_text = raw_text(output["payload"].get("output"))
        if task_name not in output_text.replace("-", "_"):
            raise RuntimeHarnessError(
                f"raw spawn result for {lane_id} does not acknowledge task {task_name}"
            )
        if isinstance(expected_agent_id, str) and expected_agent_id != agent_id:
            raise RuntimeHarnessError(
                f"raw spawn activity for {lane_id} does not bind attempt agent "
                f"{expected_agent_id}"
            )
        child_profile: dict[str, Any] | None = None
        if runner_evidence is not None and participants:
            matched_participants = [
                item
                for item in participants
                if isinstance(item, dict)
                and item.get("agent_id") == agent_id
                and str(item.get("execution_ref") or "").endswith(
                    ":" + str(attempt["attempt_id"])
                )
            ]
            if len(matched_participants) != 1:
                raise RuntimeHarnessError(
                    f"{attempt['attempt_id']} must bind one accounting participant"
                )
            child_session_id = str(matched_participants[0]["agent_id"])
            child_path = session_paths.get(child_session_id)
            if child_path is None:
                try:
                    child_path = locate_session(
                        "codex",
                        child_session_id,
                        runtime_root or default_runtime_root("codex"),
                        lead=False,
                    )
                except TokenAccountingError as exc:
                    raise RuntimeHarnessError(str(exc)) from exc
            try:
                child_profile = read_session_profile(
                    child_path,
                    expected_session_id=child_session_id,
                )
            except RoutingRuntimeError as exc:
                raise RuntimeHarnessError(str(exc)) from exc
            if (
                child_profile.get("model") != planned_model
                or child_profile.get("reasoning_effort") != effort
            ):
                raise RuntimeHarnessError(
                    f"child runtime for {attempt['attempt_id']} must execute "
                    f"{planned_model}/{effort}; observed "
                    f"{child_profile.get('model')}/{child_profile.get('reasoning_effort')}"
                )
        observed.append(
            {
                "event_ref": f"codex:{session_id}:spawn_agent:{line_number}",
                "event_sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
                "lane_id": lane_id,
                "round_id": attempt["round_id"],
                "attempt_id": attempt["attempt_id"],
                "task_name": task_name,
                "model": actual_model,
                "thinking": actual_effort,
                "result_ref": f"codex:{session_id}:tool_output:{output['line']}",
                "result_sha256": output["sha256"],
                "activity_ref": f"codex:{session_id}:sub_agent_activity:{activity['line']}",
                "activity_sha256": activity["sha256"],
                "agent_id": agent_id,
                **({"child_session": child_profile} if child_profile is not None else {}),
            }
        )
    missing = [
        attempt["attempt_id"]
        for task_name, attempts in expected.items()
        for attempt in attempts[offsets[task_name]:]
    ]
    if missing:
        raise RuntimeHarnessError(
            "mandatory routed attempts lack raw model-selectable spawn evidence: "
            + ", ".join(missing)
        )
    return observed


def validate_lead_routing_profile(
    session_path: Path,
    session_id: str,
    orchestration: dict[str, Any],
) -> dict[str, Any] | None:
    requirement = orchestration.get("model_routing_requirement")
    if requirement is None:
        return None
    block = orchestration.get("model_routing")
    expected = block.get("session_profile") if isinstance(block, dict) else None
    if not isinstance(expected, dict) or not isinstance(expected.get("event_line"), int):
        raise RuntimeHarnessError("mandatory routing lacks a sealed Lead session profile")
    try:
        observed = read_session_profile(
            session_path,
            expected_session_id=session_id,
            through_line=expected["event_line"],
        )
    except RoutingRuntimeError as exc:
        raise RuntimeHarnessError(str(exc)) from exc
    projected = {key: observed.get(key) for key in expected}
    if projected != expected:
        raise RuntimeHarnessError("Lead routing session profile drifted from raw turn_context")
    return observed


def round_from_text(text: str, known: set[str]) -> str | None:
    for candidate in sorted(known, key=len, reverse=True):
        variants = {candidate, candidate.replace("-", "_"), candidate.replace("-", " ")}
        if any(variant in text for variant in variants):
            return candidate
    match = re.search(r"round[-_ ]?0*(\d+)", text, flags=re.IGNORECASE)
    if match:
        candidate = f"round-{int(match.group(1)):03d}"
        if candidate in known:
            return candidate
    return None


def participant_round(call_input: str, output: str, participants: list[dict[str, Any]]) -> str | None:
    haystack = f"{call_input}\n{output}"
    matches = {
        str(item.get("round_id"))
        for item in participants
        if isinstance(item, dict)
        and isinstance(item.get("agent_id"), str)
        and item["agent_id"] in haystack
        and isinstance(item.get("round_id"), str)
    }
    return next(iter(matches)) if len(matches) == 1 else None


def runner_round(
    call_id: str,
    call_input: str,
    participants: list[dict[str, Any]],
    runner_evidence: dict[str, Any],
) -> str | None:
    waves = runner_evidence.get("execution_efficiency", {}).get("wait_waves", [])
    wave_rounds = {
        item.get("round_id")
        for item in waves
        if isinstance(item, dict)
        and isinstance(item.get("trigger_ref"), str)
        and call_id in item["trigger_ref"]
        and isinstance(item.get("round_id"), str)
    }
    if len(wave_rounds) == 1:
        return next(iter(wave_rounds))
    records = runner_evidence.get("agents", [])
    targets = parse_call_input(call_input)
    target_text = raw_text(targets)
    record_rounds = {
        item.get("round_id")
        for item in records
        if isinstance(item, dict)
        and isinstance(item.get("round_id"), str)
        and any(
            isinstance(item.get(key), str) and item[key] in target_text
            for key in ("agent_id", "native_handle")
        )
    }
    return next(iter(record_rounds)) if len(record_rounds) == 1 else None


def validate_barrier_resume(value: Any) -> dict[str, Any]:
    resume = value if isinstance(value, dict) else {}
    required = (
        "attempt_id_before",
        "attempt_id_after",
        "deadline_before",
        "deadline_after",
        "terminal_before",
        "active_before",
        "terminal_after",
        "active_after",
        "duplicate_spawn",
    )
    if any(key not in resume for key in required):
        raise RuntimeHarnessError("barrier resume fixture is incomplete")
    if resume["attempt_id_before"] != resume["attempt_id_after"]:
        raise RuntimeHarnessError("barrier resume changed attempt identity")
    if resume["deadline_before"] != resume["deadline_after"]:
        raise RuntimeHarnessError("barrier resume reset the wall-clock deadline")
    if resume["duplicate_spawn"] is not False:
        raise RuntimeHarnessError("barrier resume duplicated native dispatch")
    if set(resume["terminal_before"]) != set(resume["terminal_after"]):
        raise RuntimeHarnessError("timeout changed terminal set without a terminal event")
    if set(resume["active_before"]) != set(resume["active_after"]):
        raise RuntimeHarnessError("timeout changed active set before a decision")
    return resume


def unique_gate(round_plan: dict[str, Any], completion_class: str) -> str | None:
    wanted = {
        "decision_gate": "decision_gate",
        "repair_gate": "repair_gate",
        "human_gate": "human_gate",
        "final_synthesis": "final_gate",
    }.get(completion_class)
    if wanted is None:
        return None
    matches = [
        gate.get("gate_id")
        for gate in round_plan.get("semantic_gates", [])
        if isinstance(gate, dict) and gate.get("gate_class") == wanted
    ]
    if len(matches) != 1 or not isinstance(matches[0], str):
        raise RuntimeHarnessError(
            f"{round_plan.get('round_id')} needs exactly one sealed {wanted} for raw classification"
        )
    return matches[0]


def classify_completion(
    *,
    trigger_tool_name: str,
    trigger_call_id: str,
    trigger_input: str,
    trigger_output: str,
    action_tool_name: str,
    action_input: str,
    round_plan: dict[str, Any],
    expected_agents: int,
    runner_evidence: dict[str, Any],
) -> tuple[str, str, str | None, dict[str, Any] | None]:
    trigger_name = normalized_tool_name(trigger_tool_name)
    action_name = normalized_tool_name(action_tool_name)
    lowered = trigger_output.lower()
    outcome = "tool_result"
    semantic_evidence: dict[str, Any] | None = None
    records = [
        item
        for item in runner_evidence.get("agents", [])
        if isinstance(item, dict) and item.get("round_id") == round_plan.get("round_id")
    ]
    if action_name.endswith("followup_task"):
        action_target = structured_target(action_input)
        matched = [
            item for item in records
            if action_target is not None
            and action_target in {item.get("native_handle"), item.get("agent_id")}
        ]
        if len(matched) != 1:
            raise RuntimeHarnessError("follow-up action lacks one runner-bound attempt identity")
        if matched[0].get("attempt_kind") == "repair":
            completion_class = "repair_gate"
            outcome = "bounded_repair_dispatch"
            semantic_evidence = {"attempt_agent_id": matched[0].get("agent_id")}
        else:
            completion_class = "deterministic_tool_result_reactivation"
            outcome = "planned_reuse_dispatch_requested"
            semantic_evidence = {"attempt_agent_id": matched[0].get("agent_id")}
    elif action_name.endswith("request_user_input"):
        completion_class = "human_gate"
        outcome = "human_authority_required"
    elif trigger_name.endswith("list_agents") or "status polling" in lowered:
        completion_class = "status_only"
        outcome = "status_observation"
    elif trigger_name in {"wait", "functions_wait"} or trigger_name.endswith("functions_wait"):
        completion_class = "wrapper_wait"
        outcome = "wrapper_return"
    elif trigger_name.endswith("spawn_agent") or trigger_name.endswith("followup_task"):
        structured = structured_target(trigger_input)
        trigger_target = (structured or "") + "\n" + trigger_output
        matched = [
            item for item in records
            if any(
                isinstance(item.get(key), str) and item[key] in trigger_target
                for key in ("agent_id", "native_handle")
            )
        ]
        if len(matched) != 1:
            raise RuntimeHarnessError("dispatch acknowledgement lacks one runner-bound attempt")
        attempt_kind = matched[0].get("attempt_kind", "initial")
        completion_class = (
            "deterministic_tool_result_reactivation"
            if trigger_name.endswith("followup_task") and attempt_kind == "repair"
            else "initial_dispatch"
        )
        outcome = (
            "bounded_repair_acknowledgement"
            if trigger_name.endswith("followup_task") and attempt_kind == "repair"
            else "native_spawn_acknowledgement"
        )
        semantic_evidence = {"attempt_agent_id": matched[0].get("agent_id")}
    elif trigger_name.endswith("wait_agent"):
        try:
            wait_result = json.loads(trigger_output)
        except json.JSONDecodeError as exc:
            raise RuntimeHarnessError("wait result must be structured JSON") from exc
        if not isinstance(wait_result, dict) or not isinstance(wait_result.get("timed_out"), bool):
            raise RuntimeHarnessError("wait result lacks a boolean timed_out field")
        waves = [
            item
            for item in runner_evidence.get("execution_efficiency", {}).get("wait_waves", [])
            if isinstance(item, dict)
            and isinstance(item.get("trigger_ref"), str)
            and trigger_call_id in item["trigger_ref"]
            and item.get("round_id") == round_plan.get("round_id")
        ]
        if len(waves) != 1:
            raise RuntimeHarnessError("wait completion lacks one barrier-bound wave")
        wave = waves[0]
        targets = wave.get("targets")
        terminal_targets = wave.get("terminal_targets")
        if not isinstance(targets, list) or not isinstance(terminal_targets, list):
            raise RuntimeHarnessError("wait wave lacks target and terminal sets")
        if expected_agents and len(targets) != expected_agents:
            raise RuntimeHarnessError("wait wave target count drifts from registered attempts")
        timed_out = wait_result["timed_out"]
        if not timed_out and set(terminal_targets) != set(targets):
            completion_class = "partial_terminal"
            outcome = "partial_terminal"
        else:
            completion_class = "decision_gate"
            outcome = "timeout_terminal_gate" if timed_out else "all_expected_terminal"
        semantic_evidence = {
            "barrier_id": wave.get("barrier_id"),
            "wave_id": wave.get("wave_id"),
            "targets": targets,
            "terminal_targets": terminal_targets,
        }
    elif trigger_name.startswith("collaboration_"):
        raise RuntimeHarnessError(f"unknown collaboration event cannot be classified: {trigger_tool_name}")
    else:
        completion_class = "deterministic_tool_result_reactivation"
    gate_id = unique_gate(round_plan, completion_class)
    return completion_class, outcome, gate_id, semantic_evidence


def codex_completion_events(
    session_path: Path,
    session_id: str,
    *,
    start_line: int,
    orchestration: dict[str, Any],
    harness: dict[str, Any],
    participants: list[dict[str, Any]],
    runner_evidence: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    rounds = {
        item.get("round_id"): item
        for item in orchestration.get("rounds", [])
        if isinstance(item, dict) and isinstance(item.get("round_id"), str)
    }
    default_round = harness.get("default_round_id")
    runner_evidence = runner_evidence or {"agents": [], "execution_efficiency": {"wait_waves": []}}
    if default_round not in rounds:
        raise RuntimeHarnessError("runtime-harness.default_round_id must name a declared round")
    calls: dict[str, dict[str, Any]] = {}
    outputs: list[dict[str, Any]] = []
    completions: list[dict[str, Any]] = []
    previous_token_line = start_line
    prior_pair: tuple[dict[str, Any], dict[str, Any]] | None = None
    with session_path.open("rb") as handle:
        for line_number, raw in enumerate(handle, start=1):
            try:
                item = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RuntimeHarnessError(
                    f"Invalid Codex runtime JSONL at {session_path}:{line_number}"
                ) from exc
            if line_number <= start_line:
                continue
            payload = item.get("payload") if isinstance(item, dict) else None
            if not isinstance(payload, dict):
                continue
            if item.get("type") == "response_item" and payload.get("type") in {
                "custom_tool_call",
                "function_call",
            }:
                call_id = payload.get("call_id")
                if isinstance(call_id, str):
                    calls[call_id] = {
                        "line": line_number,
                        "payload": payload,
                        "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
                    }
                continue
            if item.get("type") == "response_item" and payload.get("type") in {
                "custom_tool_call_output",
                "function_call_output",
            }:
                call_id = payload.get("call_id")
                if isinstance(call_id, str):
                    outputs.append(
                        {
                            "line": line_number,
                            "call_id": call_id,
                            "payload": payload,
                            "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
                        }
                    )
                continue
            if item.get("type") != "event_msg" or payload.get("type") != "token_count":
                continue
            info = payload.get("info")
            if info is None:
                continue
            if not isinstance(info, dict) or not isinstance(info.get("last_token_usage"), dict):
                raise RuntimeHarnessError(
                    f"Codex completion lacks last_token_usage at {session_path}:{line_number}"
                )
            candidates = [
                output
                for output in outputs
                if previous_token_line < output["line"] < line_number
            ]
            if len(candidates) != 1:
                raise RuntimeHarnessError(
                    f"Completion {session_id}:{line_number} must bind exactly one raw tool output; "
                    f"found {len(candidates)}"
                )
            action_output = candidates[0]
            action_call = calls.get(action_output["call_id"])
            if action_call is None or not previous_token_line < action_call["line"] < line_number:
                raise RuntimeHarnessError(
                    f"Completion {session_id}:{line_number} lacks its raw tool call"
                )
            if prior_pair is None:
                prior_pair = (action_call, action_output)
                previous_token_line = line_number
                continue
            trigger_call, trigger_output_event = prior_pair
            trigger_payload = trigger_call["payload"]
            action_payload = action_call["payload"]
            trigger_input = call_input_text(
                trigger_payload.get("input") or trigger_payload.get("arguments")
            )
            trigger_output = raw_text(trigger_output_event["payload"].get("output"))
            action_input = call_input_text(
                action_payload.get("input") or action_payload.get("arguments")
            )
            trigger_context = f"{trigger_input}\n{trigger_output}"
            derived_round = (
                round_from_text(trigger_context, set(rounds))
                or participant_round(trigger_input, trigger_output, participants)
                or runner_round(
                    str(trigger_output_event["call_id"]),
                    trigger_input,
                    participants,
                    runner_evidence,
                )
            )
            trigger_name = normalized_tool_name(trigger_payload.get("name"))
            if derived_round is None and any(
                trigger_name.endswith(name)
                for name in ("spawn_agent", "followup_task", "wait_agent")
            ):
                raise RuntimeHarnessError("semantic completion lacks an unambiguous round binding")
            round_id = str(
                derived_round
                or round_from_text(action_input, set(rounds))
                or default_round
            )
            expected_agents = sum(
                1 for item in participants if item.get("round_id") == round_id
            )
            completion_class, outcome, gate_id, semantic_evidence = classify_completion(
                trigger_tool_name=str(trigger_payload.get("name") or ""),
                trigger_call_id=str(trigger_output_event["call_id"]),
                trigger_input=trigger_input,
                trigger_output=trigger_output,
                action_tool_name=str(action_payload.get("name") or ""),
                action_input=action_input,
                round_plan=rounds[round_id],
                expected_agents=expected_agents,
                runner_evidence=runner_evidence,
            )
            event_ref = f"codex:{session_id}:token_count:{line_number}"
            completions.append(
                {
                    "event_ref": event_ref,
                    "event_sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
                    "timestamp": str(item.get("timestamp") or ""),
                    "session_id": session_id,
                    "round_id": round_id,
                    "class": completion_class,
                    "gate_id": gate_id,
                    "outcome": outcome,
                    "tool": {
                        "trigger_name": str(trigger_payload.get("name") or ""),
                        "trigger_call_id": trigger_output_event["call_id"],
                        "trigger_call_line": trigger_call["line"],
                        "trigger_call_sha256": trigger_call["sha256"],
                        "trigger_output_line": trigger_output_event["line"],
                        "trigger_output_sha256": trigger_output_event["sha256"],
                        "action_name": str(action_payload.get("name") or ""),
                        "action_call_id": action_output["call_id"],
                        "action_call_line": action_call["line"],
                        "action_call_sha256": action_call["sha256"],
                    },
                    "input_context": dict(info["last_token_usage"]),
                    "semantic_evidence": semantic_evidence,
                    **(
                        {
                            "trigger_evidence_ref": (
                                f"codex:{session_id}:tool_output:{trigger_output_event['line']}"
                            ),
                            "decision_diff": (
                                f"raw action {action_payload.get('name')} call "
                                f"{action_output['call_id']} sha256={action_call['sha256']}"
                            ),
                        }
                        if completion_class == "repair_gate"
                        else {}
                    ),
                }
            )
            prior_pair = (action_call, action_output)
            previous_token_line = line_number
    return completions


def completion_projection(
    orchestration: dict[str, Any], events: list[dict[str, Any]]
) -> dict[str, Any]:
    rounds = {
        str(item["round_id"]): item
        for item in orchestration.get("rounds", [])
        if isinstance(item, dict) and isinstance(item.get("round_id"), str)
    }
    projection: dict[str, Any] = {}
    for round_id, plan in rounds.items():
        counts = {name: 0 for name in COMPLETION_CLASSES}
        for event in events:
            if event.get("round_id") == round_id:
                counts[str(event["class"])] += 1
        projection[round_id] = {
            "gate_graph_sha256": plan.get("gate_graph_seal", {}).get("content_sha256"),
            "actual_counts": counts,
            "actual_coordinator_completions": sum(counts.values()),
            "budget_resolution": None,
        }
    ledger = {
        "schema_version": COMPLETION_DENSITY_SCHEMA,
        "source": "runtime_session_events",
        "entries": [
            {
                key: event.get(key)
                for key in (
                    "event_ref",
                    "round_id",
                    "class",
                    "gate_id",
                    "outcome",
                    "trigger_evidence_ref",
                    "decision_diff",
                )
                if event.get(key) is not None
            }
            for event in events
        ],
        "rounds": projection,
    }
    return ledger


def density_metrics(
    orchestration: dict[str, Any], ledger: dict[str, Any]
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    planned_total = 0
    actual_total = 0
    aggregate_counts = {name: 0 for name in COMPLETION_CLASSES}
    for plan in orchestration.get("rounds", []):
        if not isinstance(plan, dict) or not isinstance(plan.get("round_id"), str):
            continue
        round_id = plan["round_id"]
        budget = plan.get("completion_budget", {})
        planned = int(budget.get("absolute_coordinator_completions_max", 0))
        actual = int(ledger["rounds"][round_id]["actual_coordinator_completions"])
        planned_total += planned
        actual_total += actual
        counts = ledger["rounds"][round_id]["actual_counts"]
        for name in aggregate_counts:
            aggregate_counts[name] += int(counts.get(name, 0))
        rows.append(
            {
                "round_id": round_id,
                "planned_absolute_max": planned,
                "actual": actual,
                "actual_over_planned_max": actual / planned if planned else None,
                "counts": counts,
            }
        )
    return {
        "rounds": rows,
        "planned_absolute_max_total": planned_total,
        "actual_coordinator_completions": actual_total,
        "actual_over_planned_max": actual_total / planned_total if planned_total else None,
        "completion_counts": aggregate_counts,
        "status_only_wakes": aggregate_counts["status_only"],
        "wrapper_wait_wakes": aggregate_counts["wrapper_wait"],
        "partial_terminal_wakes": aggregate_counts["partial_terminal"],
    }


def collect(workflow_dir: Path, *, runtime_root: Path | None = None) -> dict[str, Any]:
    workflow_dir = workflow_dir.resolve()
    orchestration = load_object(workflow_dir / "orchestration.json")
    harness = load_object(workflow_dir / "runtime-harness.json")
    if harness.get("schema_version") != HARNESS_SCHEMA:
        raise RuntimeHarnessError(f"runtime-harness.schema_version must be {HARNESS_SCHEMA}")
    usage = load_object(workflow_dir / "token-usage.json")
    accounting = usage.get("accounting")
    if not isinstance(accounting, dict) or accounting.get("runtime") != "codex":
        raise RuntimeHarnessError("Raw completion replay currently requires started Codex accounting")
    lead_session_id = accounting.get("lead_session_id")
    if not isinstance(lead_session_id, str):
        raise RuntimeHarnessError("token-usage accounting lacks lead_session_id")
    evidence = load_object(workflow_dir / "token-evidence.json")
    start = evidence.get("lead", {}).get("start")
    if not isinstance(start, dict):
        raise RuntimeHarnessError("token-evidence lacks lead start snapshot")
    start_line = event_line(start.get("event_ref"), lead_session_id)
    root = (runtime_root or default_runtime_root("codex")).expanduser().resolve()
    try:
        lead_path = locate_session("codex", lead_session_id, root, lead=True)
    except TokenAccountingError as exc:
        raise RuntimeHarnessError(str(exc)) from exc
    participants = accounting.get("participants")
    if not isinstance(participants, list) or not all(isinstance(item, dict) for item in participants):
        raise RuntimeHarnessError("token-usage accounting participants must be objects")
    runner_path = workflow_dir / "runner-evidence.json"
    runner = load_object(runner_path)
    validate_lead_routing_profile(lead_path, lead_session_id, orchestration)
    events = codex_completion_events(
        lead_path,
        lead_session_id,
        start_line=start_line,
        orchestration=orchestration,
        harness=harness,
        participants=participants,
        runner_evidence=runner,
    )
    sealed_through_line = event_line(
        events[-1]["event_ref"] if events else start.get("event_ref"),
        lead_session_id,
    )
    routed_dispatches = routed_dispatch_events(
        lead_path,
        lead_session_id,
        start_line=start_line,
        through_line=sealed_through_line,
        orchestration=orchestration,
        runner_evidence=runner,
        participants=participants,
        runtime_root=root,
    )
    ledger = completion_projection(orchestration, events)
    validate_completion_density(ledger, orchestration, final=True)
    runner["completion_density"] = ledger
    atomic_write(runner_path, runner)
    observation = {
        "schema_version": OBSERVATION_SCHEMA,
        "status": "observed_pending_token_finalization",
        "source": "raw_runtime_session_events",
        "runtime": "codex",
        "lead_session_id": lead_session_id,
        "boundary": {
            "start_event_ref": start.get("event_ref"),
            "end_event_ref": events[-1]["event_ref"] if events else start.get("event_ref"),
            "final_user_response_included": False,
        },
        "session_sources": [
            {
                "role": "lead",
                "session_id": lead_session_id,
                "path": str(lead_path),
                "sealed_through_line": sealed_through_line,
                "sealed_prefix_sha256": prefix_sha256(
                    lead_path,
                    sealed_through_line,
                ),
            },
            *[
                {
                    "role": "routed_attempt",
                    "attempt_id": item["attempt_id"],
                    "session_id": item["child_session"]["session_id"],
                    "path": item["child_session"]["session_path"],
                    "sealed_through_line": item["child_session"]["event_line"],
                    "sealed_prefix_sha256": item["child_session"]["prefix_sha256"],
                }
                for item in routed_dispatches
                if isinstance(item.get("child_session"), dict)
            ],
        ],
        "routed_dispatches": routed_dispatches,
        "routed_dispatches_sha256": canonical_sha256(routed_dispatches),
        "completion_events": events,
        "completion_projection_sha256": canonical_sha256(ledger),
        "metrics": density_metrics(orchestration, ledger),
        "exact_token_total": None,
        "actor_breakdown": [],
        "token_evidence_sha256": None,
        "host_boundaries": {
            "portable_controller_native_spawn_join_queue_rotation": False,
            "terminal_host_atomicity_claimed": False,
            "outer_main_post_terminal_wake": "outside_sealed_subtree_unobserved",
        },
    }
    atomic_write(workflow_dir / "runtime-observations.json", observation)
    return observation


def reconcile(workflow_dir: Path) -> dict[str, Any]:
    workflow_dir = workflow_dir.resolve()
    observation = load_object(workflow_dir / "runtime-observations.json")
    usage = load_object(workflow_dir / "token-usage.json")
    if usage.get("status") != "complete" or usage.get("confidence") != "exact":
        raise RuntimeHarnessError("Runtime observations require complete exact token accounting")
    measurements = usage.get("measurements")
    if not isinstance(measurements, list) or not measurements:
        raise RuntimeHarnessError("Exact token accounting has no actor measurements")
    actor_breakdown = [
        {
            "actor": item.get("subject_kind"),
            "session_id": item.get("subject_id"),
            "execution_refs": item.get("execution_refs"),
            "tokens": item.get("delta_tokens"),
            "input_tokens": item.get("delta", {}).get("input_tokens"),
            "cached_input_tokens": item.get("delta", {}).get("cached_input_tokens"),
            "output_tokens": item.get("delta", {}).get("output_tokens"),
            "reasoning_tokens": item.get("delta", {}).get("reasoning_tokens"),
        }
        for item in measurements
        if isinstance(item, dict)
    ]
    if sum(int(item["tokens"]) for item in actor_breakdown) != usage.get("total_tokens"):
        raise RuntimeHarnessError("Actor breakdown does not sum to exact workflow total")
    observation.update(
        {
            "status": "complete",
            "exact_token_total": usage.get("total_tokens"),
            "actor_breakdown": actor_breakdown,
            "token_evidence_sha256": usage.get("evidence_sha256"),
        }
    )
    atomic_write(workflow_dir / "runtime-observations.json", observation)
    return observation


def validate_artifact(workflow_dir: Path, *, final: bool) -> dict[str, Any]:
    workflow_dir = workflow_dir.resolve()
    observation = load_object(workflow_dir / "runtime-observations.json")
    schema = observation.get("schema_version")
    if schema not in {LEGACY_OBSERVATION_SCHEMA, OBSERVATION_SCHEMA}:
        raise RuntimeHarnessError(
            "runtime-observations.schema_version must be "
            f"{LEGACY_OBSERVATION_SCHEMA} or {OBSERVATION_SCHEMA}"
        )
    if observation.get("source") != "raw_runtime_session_events":
        raise RuntimeHarnessError("runtime-observations source must be raw runtime events")
    runner = load_object(workflow_dir / "runner-evidence.json")
    ledger = runner.get("completion_density")
    if canonical_sha256(ledger) != observation.get("completion_projection_sha256"):
        raise RuntimeHarnessError("runner completion projection drifted from raw replay")
    source_rows = observation.get("session_sources")
    if not isinstance(source_rows, list) or not source_rows:
        raise RuntimeHarnessError("runtime observations must bind raw session sources")
    if schema == LEGACY_OBSERVATION_SCHEMA and len(source_rows) != 1:
        raise RuntimeHarnessError("legacy runtime observations must bind one Lead source")
    for source_row in source_rows:
        if not isinstance(source_row, dict) or not isinstance(source_row.get("path"), str):
            raise RuntimeHarnessError("runtime observation source row is malformed")
        source_path = Path(source_row["path"])
        source_line = source_row.get("sealed_through_line")
        if (
            not source_path.is_file()
            or not isinstance(source_line, int)
            or prefix_sha256(source_path, source_line)
            != source_row.get("sealed_prefix_sha256")
        ):
            raise RuntimeHarnessError("raw runtime source changed after replay")
    orchestration = load_object(workflow_dir / "orchestration.json")
    lead_session_id = observation.get("lead_session_id")
    boundary = observation.get("boundary")
    if not isinstance(lead_session_id, str) or not isinstance(boundary, dict):
        raise RuntimeHarnessError("runtime observation routing boundary is malformed")
    if schema == OBSERVATION_SCHEMA:
        lead_rows = [
            item
            for item in source_rows
            if isinstance(item, dict)
            and item.get("role") == "lead"
            and item.get("session_id") == lead_session_id
        ]
        if len(lead_rows) != 1:
            raise RuntimeHarnessError("runtime observations must bind one typed Lead source")
        lead_row = lead_rows[0]
        path = Path(lead_row["path"])
        through_line = int(lead_row["sealed_through_line"])
        validate_lead_routing_profile(path, lead_session_id, orchestration)
        usage = load_object(workflow_dir / "token-usage.json")
        accounting = usage.get("accounting")
        participants = accounting.get("participants") if isinstance(accounting, dict) else None
        if not isinstance(participants, list):
            raise RuntimeHarnessError("runtime observations lack accounting participants")
        session_paths = {
            str(item["session_id"]): Path(item["path"])
            for item in source_rows
            if isinstance(item, dict)
            and isinstance(item.get("session_id"), str)
            and isinstance(item.get("path"), str)
        }
        replayed_dispatches = routed_dispatch_events(
            path,
            lead_session_id,
            start_line=event_line(boundary.get("start_event_ref"), lead_session_id),
            through_line=through_line,
            orchestration=orchestration,
            runner_evidence=runner,
            participants=participants,
            session_paths=session_paths,
        )
        if canonical_sha256(replayed_dispatches) != observation.get("routed_dispatches_sha256"):
            raise RuntimeHarnessError("raw routed dispatch projection drifted after replay")
        if replayed_dispatches != observation.get("routed_dispatches"):
            raise RuntimeHarnessError("runtime routed dispatch evidence does not match raw replay")
    if final:
        usage = load_object(workflow_dir / "token-usage.json")
        if observation.get("status") != "complete":
            raise RuntimeHarnessError("final runtime observations must be complete")
        if observation.get("exact_token_total") != usage.get("total_tokens"):
            raise RuntimeHarnessError("runtime observation total drifted from exact accounting")
        if observation.get("token_evidence_sha256") != usage.get("evidence_sha256"):
            raise RuntimeHarnessError("runtime observations are not bound to token evidence")
    return observation


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("collect", "reconcile", "validate"):
        command = sub.add_parser(name)
        command.add_argument("workflow_dir", type=Path)
        if name == "collect":
            command.add_argument("--runtime-root", type=Path)
        if name == "validate":
            command.add_argument("--final", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "collect":
            value = collect(args.workflow_dir, runtime_root=args.runtime_root)
        elif args.command == "reconcile":
            value = reconcile(args.workflow_dir)
        else:
            value = validate_artifact(args.workflow_dir, final=args.final)
    except RuntimeHarnessError as exc:
        print(f"Runtime harness failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
