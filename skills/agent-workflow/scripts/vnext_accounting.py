#!/usr/bin/env python3
"""Version-bound vNext native accounting and completion-density adapters."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from artifact_store import ArtifactError, create_once_bytes, create_once_json, serialized_authority_transaction
from phase_protocol import ProtocolError, validate_replay, validate_sidecar

ACCOUNTING_SCHEMA = "agent-workflow.accounting-observation.vnext.v1"
SUPPORTED_CODEX_VERSION = "codex-cli 0.144.0-alpha.4"
SUPPORTED_APP_SCHEMA_SHA256 = "sha256:fb0d6bf6b9f192257f452340de3fdca6b4b2c8e1a216aafaf48837a006e14bea"
BREAKDOWN_FIELDS = (
    "cachedInputTokens",
    "inputTokens",
    "outputTokens",
    "reasoningOutputTokens",
    "totalTokens",
)
SEMANTIC_CLASSES = {"admission_planning", "planning", "phase_terminal", "human_gate", "blocked", "final"}
FORBIDDEN_CLASSES = {"wrapper_wait", "status_poll", "partial_progress"}
SPARSE_CLASS = "sparse_wait_continuation"


class AccountingError(ValueError):
    """Raised when exact accounting or completion classification is not provable."""


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _integer(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise AccountingError(f"{label} must be a non-negative integer")
    return value


def _breakdown(value: Any, label: str) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != set(BREAKDOWN_FIELDS):
        raise AccountingError(f"{label} schema drift")
    result = {field: _integer(value[field], f"{label}.{field}") for field in BREAKDOWN_FIELDS}
    if result["cachedInputTokens"] > result["inputTokens"]:
        raise AccountingError(f"{label} cached-input arithmetic is invalid")
    if result["reasoningOutputTokens"] > result["outputTokens"]:
        raise AccountingError(f"{label} reasoning-output arithmetic is invalid")
    if result["totalTokens"] != result["inputTokens"] + result["outputTokens"]:
        raise AccountingError(f"{label} arithmetic does not satisfy total=input+output")
    return result


def _snake(value: dict[str, int]) -> dict[str, int]:
    return {
        "cached_input_tokens": value["cachedInputTokens"],
        "input_tokens": value["inputTokens"],
        "output_tokens": value["outputTokens"],
        "reasoning_output_tokens": value["reasoningOutputTokens"],
        "total_tokens": value["totalTokens"],
    }


def observe_app_server(
    events: list[dict[str, Any]],
    *,
    raw_evidence: bytes,
    evidence_sha256: str,
    codex_version: str,
    protocol_schema_sha256: str,
    thread_id: str,
    turn_ids: list[str],
    prior_breakdown: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Produce exact Orchestrator usage from a complete, pinned App Server stream."""
    if codex_version != SUPPORTED_CODEX_VERSION:
        raise AccountingError("unsupported Codex version")
    if protocol_schema_sha256 != SUPPORTED_APP_SCHEMA_SHA256:
        raise AccountingError("protocol schema digest is unsupported")
    if evidence_sha256 != _sha256(raw_evidence):
        raise AccountingError("evidence digest mismatch")
    try:
        parsed = [json.loads(line) for line in raw_evidence.splitlines() if line.strip()]
    except json.JSONDecodeError as exc:
        raise AccountingError("App Server evidence is invalid JSONL") from exc
    if parsed != events:
        raise AccountingError("evidence bytes do not match parsed events")
    if not thread_id or not turn_ids or len(turn_ids) != len(set(turn_ids)):
        raise AccountingError("thread and ordered turn boundary must be non-empty and unique")

    expected = set(turn_ids)
    accumulated: dict[str, dict[str, int]] = {}
    prior_camel = {field: 0 for field in BREAKDOWN_FIELDS}
    if prior_breakdown is not None:
        expected_prior = {
            "cached_input_tokens", "input_tokens", "output_tokens",
            "reasoning_output_tokens", "total_tokens",
        }
        if not isinstance(prior_breakdown, dict) or set(prior_breakdown) != expected_prior:
            raise AccountingError("prior App Server breakdown schema drift")
        prior_camel = {
            "cachedInputTokens": _integer(prior_breakdown["cached_input_tokens"], "prior.cached_input_tokens"),
            "inputTokens": _integer(prior_breakdown["input_tokens"], "prior.input_tokens"),
            "outputTokens": _integer(prior_breakdown["output_tokens"], "prior.output_tokens"),
            "reasoningOutputTokens": _integer(prior_breakdown["reasoning_output_tokens"], "prior.reasoning_output_tokens"),
            "totalTokens": _integer(prior_breakdown["total_tokens"], "prior.total_tokens"),
        }
        _breakdown(prior_camel, "prior")
    previous_total: dict[str, int] | None = prior_camel if prior_breakdown is not None else None
    started: list[str] = []
    completed: list[str] = []
    active_turn: str | None = None
    for index, event in enumerate(events, start=1):
        if not isinstance(event, dict):
            raise AccountingError(f"unexpected App Server event at {index}")
        method = event.get("method")
        params = event.get("params")
        if not isinstance(params, dict) or params.get("threadId") != thread_id:
            raise AccountingError(f"unexpected thread at {index}")
        if method in {"turn/started", "turn/completed"}:
            if set(params) != {"threadId", "turn"} or not isinstance(params.get("turn"), dict):
                raise AccountingError(f"terminal notification schema drift at {index}")
            turn = params["turn"]
            turn_id = turn.get("id")
            if (
                not isinstance(turn_id, str)
                or turn_id not in expected
                or not isinstance(turn.get("items"), list)
            ):
                raise AccountingError(f"unexpected turn at {index}")
            if method == "turn/started":
                if turn.get("status") != "inProgress" or active_turn is not None or turn_id in started:
                    raise AccountingError(f"terminal start boundary is invalid at {index}")
                active_turn = turn_id
                started.append(turn_id)
            else:
                if turn.get("status") != "completed" or active_turn != turn_id or turn_id in completed:
                    raise AccountingError(f"terminal completion boundary is invalid at {index}")
                if turn_id not in accumulated:
                    raise AccountingError(f"terminal turn lacks token usage at {index}")
                completed.append(turn_id)
                active_turn = None
            continue
        if method != "thread/tokenUsage/updated":
            raise AccountingError(f"unexpected App Server event at {index}")
        if set(params) != {"threadId", "turnId", "tokenUsage"}:
            raise AccountingError(f"notification schema drift at {index}")
        turn_id = params["turnId"]
        if not isinstance(turn_id, str) or turn_id not in expected:
            raise AccountingError(f"unexpected turn at {index}")
        if active_turn != turn_id:
            raise AccountingError(f"token usage lacks terminal turn boundary at {index}")
        usage = params["tokenUsage"]
        if not isinstance(usage, dict) or set(usage) not in ({"last", "total"}, {"last", "total", "modelContextWindow"}):
            raise AccountingError(f"tokenUsage schema drift at {index}")
        last = _breakdown(usage["last"], f"event[{index}].last")
        total = _breakdown(usage["total"], f"event[{index}].total")
        if previous_total is not None:
            for field in BREAKDOWN_FIELDS:
                if total[field] < previous_total[field]:
                    raise AccountingError(f"cumulative {field} moved backwards at {index}")
        prior_turn = accumulated.get(turn_id, {field: 0 for field in BREAKDOWN_FIELDS})
        accumulated[turn_id] = {field: prior_turn[field] + last[field] for field in BREAKDOWN_FIELDS}
        previous_total = total

    if active_turn is not None or started != turn_ids or completed != turn_ids:
        raise AccountingError("terminal turn boundary is incomplete or out of order")
    aggregate = {field: sum(accumulated[turn][field] for turn in turn_ids) for field in BREAKDOWN_FIELDS}
    expected_total = {
        field: prior_camel[field] + aggregate[field]
        for field in BREAKDOWN_FIELDS
    }
    if previous_total != expected_total:
        raise AccountingError("cumulative total does not equal the complete turn boundary")
    return {
        "schema_version": ACCOUNTING_SCHEMA,
        "coverage": "exact",
        "source": "codex_app_server_thread_token_usage_v2",
        "confidence": "exact",
        "boundary": "through_orchestrator_terminal",
        "workflow_tokens": aggregate["totalTokens"],
        "breakdown": _snake(aggregate),
        "thread_id": thread_id,
        "turn_ids": list(turn_ids),
        "codex_version": codex_version,
        "protocol_schema_sha256": protocol_schema_sha256,
        "evidence_sha256": evidence_sha256,
        "late_seal_wake_required": False,
    }


def observe_exec_jsonl(
    events: list[dict[str, Any]],
    *,
    raw_evidence: bytes,
    evidence_sha256: str,
    codex_version: str,
    thread_id: str,
    turn_id: str,
) -> dict[str, Any]:
    """Replay one terminal `codex exec --json` turn as exact worker usage."""
    if codex_version != SUPPORTED_CODEX_VERSION:
        raise AccountingError("unsupported Codex version")
    if evidence_sha256 != _sha256(raw_evidence):
        raise AccountingError("evidence digest mismatch")
    try:
        parsed = [json.loads(line) for line in raw_evidence.splitlines() if line.strip()]
    except json.JSONDecodeError as exc:
        raise AccountingError("Codex exec evidence is invalid JSONL") from exc
    if parsed != events:
        raise AccountingError("evidence bytes do not match parsed events")
    if not thread_id or not turn_id:
        raise AccountingError("Codex exec thread and turn boundary must be non-empty")

    observed_thread: str | None = None
    starts = 0
    terminal_usage: dict[str, Any] | None = None
    terminal_seen = False
    for index, event in enumerate(events, start=1):
        if not isinstance(event, dict) or terminal_seen:
            raise AccountingError(f"unexpected Codex exec event at {index}")
        kind = event.get("type")
        if kind == "thread.started":
            if observed_thread is not None or set(event) != {"type", "thread_id"}:
                raise AccountingError(f"Codex exec thread boundary drifted at {index}")
            observed_thread = event.get("thread_id")
        elif kind == "turn.started":
            if observed_thread is None or starts or set(event) != {"type"}:
                raise AccountingError(f"Codex exec turn start drifted at {index}")
            starts = 1
        elif kind in {"item.started", "item.completed"}:
            if observed_thread is None or starts != 1 or not isinstance(event.get("item"), dict):
                raise AccountingError(f"Codex exec item boundary drifted at {index}")
        elif kind == "turn.completed":
            if observed_thread is None or starts != 1 or set(event) != {"type", "usage"}:
                raise AccountingError(f"Codex exec terminal boundary drifted at {index}")
            terminal_usage = event.get("usage")
            terminal_seen = True
        else:
            raise AccountingError(f"unexpected Codex exec event at {index}")

    if observed_thread != thread_id or starts != 1 or not terminal_seen or not isinstance(terminal_usage, dict):
        raise AccountingError("Codex exec terminal boundary is incomplete")
    required = {"input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"}
    if set(terminal_usage) != required:
        raise AccountingError("Codex exec usage schema drift")
    usage = {field: _integer(terminal_usage[field], f"Codex exec usage.{field}") for field in required}
    if usage["cached_input_tokens"] > usage["input_tokens"] or usage["reasoning_output_tokens"] > usage["output_tokens"]:
        raise AccountingError("Codex exec usage arithmetic is invalid")
    total = usage["input_tokens"] + usage["output_tokens"]
    return {
        "schema_version": ACCOUNTING_SCHEMA,
        "coverage": "exact",
        "source": "codex_exec_jsonl_v1",
        "confidence": "exact",
        "boundary": "through_worker_terminal",
        "workflow_tokens": total,
        "breakdown": {**usage, "total_tokens": total},
        "thread_id": thread_id,
        "turn_ids": [turn_id],
        "codex_version": codex_version,
        "evidence_sha256": evidence_sha256,
        "late_seal_wake_required": False,
    }


def _partial(
    reason: str,
    *,
    tokens: int | None = None,
    breakdown: dict[str, Any] | None = None,
    evidence_sha256: str | None = None,
    session_id: str | None = None,
    turn_id: str | None = None,
    codex_version: str | None = None,
) -> dict[str, Any]:
    result = {
        "schema_version": ACCOUNTING_SCHEMA,
        "coverage": "partial",
        "source": "codex_stop_hook_transcript_version_gated",
        "confidence": "partial",
        "boundary": "through_orchestrator_terminal",
        "workflow_tokens": tokens,
        "breakdown": breakdown,
        "reason": reason,
        "evidence_sha256": evidence_sha256,
        "late_seal_wake_required": False,
    }
    if session_id is not None:
        result["session_id"] = session_id
    if turn_id is not None:
        result["turn_id"] = turn_id
    if codex_version is not None:
        result["codex_version"] = codex_version
    return result


def observe_stop_hook(payload: dict[str, Any], *, transcript_path: Path, codex_version: str) -> dict[str, Any]:
    """Parse the unstable Stop transcript only as a version-gated partial fallback."""
    path = Path(transcript_path)
    transcript_digest = _sha256(path.read_bytes()) if path.is_file() and not path.is_symlink() else None
    required = {"session_id", "turn_id", "transcript_path", "hook_event_name", "permission_mode", "last_assistant_message"}
    if not isinstance(payload, dict) or set(payload) != required or payload.get("hook_event_name") != "Stop":
        return _partial("hook_input_schema_drift", evidence_sha256=transcript_digest)
    session_id = payload.get("session_id")
    turn_id = payload.get("turn_id")
    declared = payload.get("transcript_path")
    if not isinstance(session_id, str) or not session_id or not isinstance(turn_id, str) or not turn_id or not isinstance(declared, str):
        return _partial("hook_input_schema_drift", evidence_sha256=transcript_digest)
    def partial(reason: str, *, tokens: int | None = None, breakdown: dict[str, Any] | None = None) -> dict[str, Any]:
        return _partial(
            reason,
            tokens=tokens,
            breakdown=breakdown,
            evidence_sha256=transcript_digest,
            session_id=session_id,
            turn_id=turn_id,
            codex_version=codex_version,
        )
    declared_path = Path(declared)
    if not declared_path.is_absolute():
        declared_path = path.parent / declared_path
    if path.is_symlink() or not path.is_file() or declared_path.resolve() != path.resolve():
        return partial("transcript_path_mismatch")
    if codex_version != SUPPORTED_CODEX_VERSION:
        return partial("unsupported_codex_version")
    latest: dict[str, Any] | None = None
    terminal = False
    session_meta_ids: list[str] = []
    turn_context_ids: list[str] = []
    try:
        with path.open("rb") as handle:
            for raw in handle:
                item = json.loads(raw)
                item_payload = item.get("payload") if isinstance(item, dict) else None
                if not isinstance(item_payload, dict):
                    continue
                if item.get("type") == "session_meta":
                    candidate = item_payload.get("id")
                    if isinstance(candidate, str):
                        session_meta_ids.append(candidate)
                elif item.get("type") == "turn_context":
                    candidate = item_payload.get("turn_id")
                    if isinstance(candidate, str):
                        turn_context_ids.append(candidate)
                elif item.get("type") == "event_msg" and item_payload.get("type") == "token_count" and isinstance(item_payload.get("info"), dict):
                    counters = item_payload["info"].get("total_token_usage")
                    if not isinstance(counters, dict):
                        return partial("transcript_schema_drift")
                    latest = counters
                elif item.get("type") == "event_msg" and item_payload.get("type") in {"task_complete", "turn_aborted"}:
                    terminal = item_payload.get("turn_id") == turn_id
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return partial("transcript_schema_drift")
    if session_meta_ids != [session_id]:
        return partial("transcript_session_mismatch")
    if not turn_context_ids or turn_context_ids[-1] != turn_id:
        return partial("transcript_turn_mismatch")
    if not terminal:
        return partial("transcript_not_terminal")
    required_usage = {"input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens"}
    if not isinstance(latest, dict) or set(latest) != required_usage:
        return partial("transcript_schema_drift")
    try:
        usage = {key: _integer(latest[key], f"hook usage {key}") for key in required_usage}
    except AccountingError:
        return partial("transcript_schema_drift")
    if usage["cached_input_tokens"] > usage["input_tokens"] or usage["total_tokens"] != usage["input_tokens"] + usage["output_tokens"]:
        return partial("transcript_schema_drift")
    return partial("version_gated_transcript_observation", tokens=usage["total_tokens"], breakdown=usage)


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _raw_jsonl(raw_evidence: bytes, label: str) -> list[tuple[int, bytes, dict[str, Any]]]:
    rows: list[tuple[int, bytes, dict[str, Any]]] = []
    try:
        for line_number, raw in enumerate(raw_evidence.splitlines(keepends=True), start=1):
            if not raw.strip():
                continue
            item = json.loads(raw)
            if not isinstance(item, dict):
                raise AccountingError(f"{label} must contain JSON objects")
            rows.append((line_number, raw, item))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AccountingError(f"{label} is invalid JSONL") from exc
    if not rows:
        raise AccountingError(f"{label} is empty")
    return rows


def _call_class(payload: dict[str, Any]) -> str:
    name = str(payload.get("name") or "").lower().replace("__", ".")
    raw_input = payload.get("input", payload.get("arguments", ""))
    if not isinstance(raw_input, str):
        raw_input = json.dumps(raw_input, ensure_ascii=False, sort_keys=True)
    if name.endswith("list_agents"):
        return "status_poll"
    if name.endswith("wait_agent") or name in {"wait", "functions.wait"} or name.endswith("functions.wait"):
        return "wrapper_wait"
    if name.endswith("write_stdin"):
        return SPARSE_CLASS
    if name.endswith("request_user_input"):
        return "human_gate"
    if name.endswith("apply_patch"):
        return "planning"
    if name.endswith("exec"):
        if re.search(r"\btools\.apply_patch\s*\(", raw_input):
            return "planning"
        command_match = re.search(r"(?:\"?cmd\"?)\s*:\s*(\"(?:\\.|[^\"\\])*\")", raw_input)
        command = None
        if command_match:
            try:
                command = json.loads(command_match.group(1))
            except json.JSONDecodeError:
                command = None
        if isinstance(command, str) and not re.search(r"(?:&&|\|\||[;|`]|\$\()", command):
            try:
                argv = shlex.split(command)
            except ValueError:
                argv = []
            runtime_indexes = [index for index, value in enumerate(argv) if Path(value).name == "workflow_runtime.py"]
            if len(runtime_indexes) == 1:
                index = runtime_indexes[0]
                allowed_prefix = index == 0 or (index == 1 and Path(argv[0]).name.startswith("python"))
                command_name = argv[index + 1] if allowed_prefix and len(argv) > index + 1 else None
            else:
                command_name = None
        else:
            command_name = None
        if command_name in {
            "admit", "run-phase", "run-once", "cancel", "reconcile", "probe-source-write",
            "probe-host-capabilities",
            "amend", "resume-brief", "seal-final", "seal-accounting",
        }:
            return {
                "admit": "admission_planning",
                "run-phase": "phase_terminal",
                "run-once": "phase_terminal",
                "reconcile": "phase_terminal",
                "cancel": "blocked",
                "probe-source-write": "planning",
                "probe-host-capabilities": "planning",
                "amend": "human_gate",
                "resume-brief": "planning",
                "seal-final": "final",
                "seal-accounting": "partial_progress",
            }[command_name]
    return "partial_progress"


def _replay_completion_density(raw_evidence: bytes, *, session_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Replay completion classes from a terminal raw Codex session prefix."""
    if not isinstance(session_id, str) or not session_id:
        raise AccountingError("raw session id is invalid")
    rows = _raw_jsonl(raw_evidence, "raw session evidence")
    session_ids: list[str] = []
    turn_ids: list[str] = []
    calls: dict[str, dict[str, Any]] = {}
    interval_call_ids: list[str] = []
    completions: list[dict[str, Any]] = []
    terminal_turn_id: str | None = None
    terminal_line: int | None = None
    terminal_kind: str | None = None
    previous_total_usage = {field: 0 for field in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")}
    for line_number, raw, item in rows:
        payload = item.get("payload")
        if not isinstance(payload, dict):
            continue
        item_type = item.get("type")
        if terminal_line is not None and item_type in {"turn_context", "response_item", "event_msg"}:
            raise AccountingError("raw session contains events after terminal boundary")
        if item_type == "session_meta":
            candidate = payload.get("id")
            if isinstance(candidate, str):
                session_ids.append(candidate)
            continue
        if item_type == "turn_context":
            candidate = payload.get("turn_id")
            if isinstance(candidate, str):
                turn_ids.append(candidate)
            continue
        if item_type == "response_item" and payload.get("type") in {"custom_tool_call", "function_call"}:
            call_id = payload.get("call_id")
            if not isinstance(call_id, str) or not call_id or call_id in calls:
                raise AccountingError("raw session tool call identity is invalid")
            calls[call_id] = {
                "line": line_number,
                "sha256": _sha256(raw),
                "payload": payload,
                "output_line": None,
                "output_sha256": None,
            }
            interval_call_ids.append(call_id)
            continue
        if item_type == "response_item" and payload.get("type") in {"custom_tool_call_output", "function_call_output"}:
            call_id = payload.get("call_id")
            call = calls.get(call_id) if isinstance(call_id, str) else None
            if call is None or call["output_line"] is not None:
                raise AccountingError("raw session tool output is orphaned or duplicated")
            call["output_line"] = line_number
            call["output_sha256"] = _sha256(raw)
            continue
        if item_type == "event_msg" and payload.get("type") == "token_count":
            if any(calls[call_id]["output_line"] is None for call_id in interval_call_ids):
                raise AccountingError("raw session completion contains an unterminated tool call")
            info = payload.get("info")
            total_usage = info.get("total_token_usage") if isinstance(info, dict) else None
            last_usage = info.get("last_token_usage") if isinstance(info, dict) else None
            usage_fields = set(previous_total_usage)
            if not isinstance(total_usage, dict) or not isinstance(last_usage, dict) or set(total_usage) != usage_fields or set(last_usage) != usage_fields:
                raise AccountingError("raw session completion token usage schema drifted")
            if any(not isinstance(total_usage[field], int) or isinstance(total_usage[field], bool) or total_usage[field] < 0 for field in usage_fields):
                raise AccountingError("raw session cumulative token usage is invalid")
            if any(not isinstance(last_usage[field], int) or isinstance(last_usage[field], bool) or last_usage[field] < 0 for field in usage_fields):
                raise AccountingError("raw session last token usage is invalid")
            if total_usage["total_tokens"] != total_usage["input_tokens"] + total_usage["output_tokens"] or last_usage["total_tokens"] != last_usage["input_tokens"] + last_usage["output_tokens"]:
                raise AccountingError("raw session completion token arithmetic is invalid")
            if total_usage["cached_input_tokens"] > total_usage["input_tokens"] or last_usage["cached_input_tokens"] > last_usage["input_tokens"]:
                raise AccountingError("raw session completion cached input is invalid")
            if total_usage["reasoning_output_tokens"] > total_usage["output_tokens"] or last_usage["reasoning_output_tokens"] > last_usage["output_tokens"]:
                raise AccountingError("raw session completion reasoning output is invalid")
            delta = {field: total_usage[field] - previous_total_usage[field] for field in usage_fields}
            if total_usage["total_tokens"] <= previous_total_usage["total_tokens"] or any(value < 0 for value in delta.values()) or delta != last_usage:
                raise AccountingError("raw session cumulative token usage does not prove a new completion")
            previous_total_usage = dict(total_usage)
            classes = [_call_class(calls[call_id]["payload"]) for call_id in interval_call_ids]
            if len(classes) == 1:
                kind: str | None = classes[0]
            elif not classes:
                kind = None
            else:
                forbidden_classes = [value for value in classes if value in FORBIDDEN_CLASSES]
                sparse_classes = [value for value in classes if value == SPARSE_CLASS]
                kind = forbidden_classes[0] if forbidden_classes else sparse_classes[0] if sparse_classes else "partial_progress"
            completions.append({
                "completion_id": f"{session_id}:token_count:{line_number}",
                "event_line": line_number,
                "event_sha256": _sha256(raw),
                "class": kind,
                "tool_calls": [
                    {
                        "call_id": call_id,
                        "name": str(calls[call_id]["payload"].get("name") or ""),
                        "call_line": calls[call_id]["line"],
                        "call_sha256": calls[call_id]["sha256"],
                        "output_line": calls[call_id]["output_line"],
                        "output_sha256": calls[call_id]["output_sha256"],
                    }
                    for call_id in interval_call_ids
                ],
            })
            interval_call_ids = []
            continue
        if item_type == "event_msg" and payload.get("type") in {"task_complete", "turn_aborted"}:
            if terminal_line is not None:
                raise AccountingError("raw session has duplicate terminal boundary")
            terminal_turn_id = payload.get("turn_id")
            terminal_line = line_number
            terminal_kind = payload.get("type")

    if session_ids != [session_id]:
        raise AccountingError("raw session identity does not match the sealed session")
    if not turn_ids or terminal_turn_id != turn_ids[-1] or terminal_line is None:
        raise AccountingError("raw session lacks a matching terminal turn boundary")
    if terminal_kind != "task_complete":
        raise AccountingError("raw session lacks a successful terminal boundary")
    if interval_call_ids:
        raise AccountingError("raw session ended with an unaccounted tool call")
    if not completions:
        raise AccountingError("raw session has no completion events")
    for index, completion in enumerate(completions):
        if completion["class"] is None:
            completion["class"] = "final" if index == len(completions) - 1 else "partial_progress"

    semantic = sum(item["class"] in SEMANTIC_CLASSES for item in completions)
    forbidden = sum(item["class"] in FORBIDDEN_CLASSES for item in completions)
    sparse = sum(item["class"] == SPARSE_CLASS for item in completions)
    density = {
        "source": "raw_session_replay_v1",
        "session_id": session_id,
        "terminal_turn_id": terminal_turn_id,
        "forbidden_wakes": forbidden,
        "semantic_wakes": semantic,
        "sparse_wait_continuations": sparse,
        "target_eligible": forbidden == 0 and sparse == 0,
    }
    projection = {
        "schema_version": "agent-workflow.completion-projection.vnext.v1",
        "session_id": session_id,
        "terminal_turn_id": terminal_turn_id,
        "terminal_line": terminal_line,
        "raw_evidence_sha256": _sha256(raw_evidence),
        "completions": completions,
        "density": density,
    }
    return density, projection


def classify_completion_density(raw_evidence: bytes, *, session_id: str) -> dict[str, Any]:
    """Classify a terminal raw Codex session; caller-authored labels are never accepted."""
    density, _ = _replay_completion_density(raw_evidence, session_id=session_id)
    return density


def count_completion_boundaries(raw_evidence: bytes, *, session_id: str) -> int:
    """Count replayed model completions without confusing them with host turns."""
    _, projection = _replay_completion_density(raw_evidence, session_id=session_id)
    return len(projection["completions"])


def _source_bytes(path: Path, label: str) -> bytes:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise AccountingError(f"{label} must be a regular file")
    payload = path.read_bytes()
    if not payload or len(payload) > 32 * 1024 * 1024:
        raise AccountingError(f"{label} size is invalid")
    return payload


def _ensure_evidence(root: Path, relative_path: str, payload: bytes) -> Path:
    target = Path(root) / relative_path
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_file() or target.read_bytes() != payload:
            raise AccountingError(f"accounting evidence drifted: {relative_path}")
        return target
    try:
        return create_once_bytes(root, relative_path, payload)
    except ArtifactError as exc:
        raise AccountingError(str(exc)) from exc


def _native_summary(
    observation: dict[str, Any], *, evidence_ref: str, evidence_sha256: str,
    raw_evidence_ref: str, raw_evidence_sha256: str,
) -> dict[str, Any]:
    if observation.get("schema_version") != ACCOUNTING_SCHEMA:
        raise AccountingError("native observation schema drift")
    coverage = observation.get("coverage")
    confidence = observation.get("confidence")
    if coverage not in {"exact", "partial"} or confidence != coverage:
        raise AccountingError("native observation coverage is invalid")
    if observation.get("boundary") != "through_orchestrator_terminal":
        raise AccountingError("native observation boundary is invalid")
    if observation.get("late_seal_wake_required") is not False:
        raise AccountingError("native observation requested a late-seal wake")
    source = observation.get("source")
    tokens = observation.get("workflow_tokens")
    if not isinstance(source, str) or not source:
        raise AccountingError("native observation source is invalid")
    if coverage == "exact":
        tokens = _integer(tokens, "native observation workflow_tokens")
        reason = None
    else:
        if tokens is not None:
            tokens = _integer(tokens, "native observation workflow_tokens")
        reason = observation.get("reason")
        if not isinstance(reason, str) or not reason:
            raise AccountingError("partial native observation requires a reason")
    return {
        "coverage": coverage,
        "source": source,
        "confidence": confidence,
        "tokens": tokens,
        "evidence_ref": evidence_ref,
        "evidence_sha256": evidence_sha256,
        "raw_evidence_ref": raw_evidence_ref,
        "raw_evidence_sha256": raw_evidence_sha256,
        "reason": reason,
        "late_seal_wake_required": False,
    }


def _replay_native_observation(
    observation: dict[str, Any], *, raw_payload: bytes, raw_path: Path,
) -> dict[str, Any]:
    if observation.get("evidence_sha256") != _sha256(raw_payload):
        raise AccountingError("native observation does not bind raw evidence")
    if observation.get("coverage") == "exact":
        events = [item for _, _, item in _raw_jsonl(raw_payload, "native App Server evidence")]
        regenerated = observe_app_server(
            events,
            raw_evidence=raw_payload,
            evidence_sha256=_sha256(raw_payload),
            codex_version=observation.get("codex_version"),
            protocol_schema_sha256=observation.get("protocol_schema_sha256"),
            thread_id=observation.get("thread_id"),
            turn_ids=observation.get("turn_ids"),
        )
    elif observation.get("source") == "codex_stop_hook_transcript_version_gated":
        session_id = observation.get("session_id")
        turn_id = observation.get("turn_id")
        codex_version = observation.get("codex_version")
        if not all(isinstance(item, str) and item for item in (session_id, turn_id, codex_version)):
            raise AccountingError("partial native observation lacks replay identity")
        regenerated = observe_stop_hook(
            {
                "session_id": session_id,
                "turn_id": turn_id,
                "transcript_path": str(raw_path.resolve()),
                "hook_event_name": "Stop",
                "permission_mode": "never",
                "last_assistant_message": "sealed-post-terminal-replay",
            },
            transcript_path=raw_path,
            codex_version=codex_version,
        )
    else:
        raise AccountingError("native observation source cannot be replayed")
    if regenerated != observation:
        raise AccountingError("native observation does not replay from raw evidence")
    return regenerated


def _session_id(raw_payload: bytes) -> str:
    ids = [
        item.get("payload", {}).get("id")
        for _, _, item in _raw_jsonl(raw_payload, "raw session evidence")
        if item.get("type") == "session_meta" and isinstance(item.get("payload"), dict)
    ]
    if len(ids) != 1 or not isinstance(ids[0], str) or not ids[0]:
        raise AccountingError("raw session must bind exactly one session identity")
    return ids[0]


def _native_terminal_turn(observation: dict[str, Any]) -> str:
    if observation.get("coverage") == "exact":
        turn_ids = observation.get("turn_ids")
        terminal_turn = turn_ids[-1] if isinstance(turn_ids, list) and turn_ids else None
    else:
        terminal_turn = observation.get("turn_id")
    if not isinstance(terminal_turn, str) or not terminal_turn:
        raise AccountingError("native observation lacks a terminal turn identity")
    return terminal_turn


def _external_summary(metrics: dict[str, Any]) -> dict[str, Any]:
    exact = metrics.get("external_accounting_exact") is True
    return {
        "source": "codex_terminal_events" if exact else "unavailable",
        "confidence": "exact" if exact else "partial",
        "input": metrics.get("external_input_tokens") if exact else None,
        "output": metrics.get("external_output_tokens") if exact else None,
        "total": metrics.get("external_total_tokens") if exact else None,
    }


def verify_accounting(root: Path, *, running_bundle: str) -> dict[str, Any]:
    """Replay a sealed accounting sidecar from its create-once raw evidence."""
    root = Path(root)
    if not isinstance(running_bundle, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", running_bundle):
        raise AccountingError("running runtime bundle digest is invalid")
    final_payload = _source_bytes(root / "final.json", "sealed final")
    workflow_payload = _source_bytes(root / "workflow.json", "sealed workflow")
    sidecar_payload = _source_bytes(root / "accounting/final.json", "sealed accounting sidecar")
    try:
        final = json.loads(final_payload)
        workflow = json.loads(workflow_payload)
        sidecar = json.loads(sidecar_payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AccountingError("sealed accounting artifacts are invalid JSON") from exc
    if not all(isinstance(item, dict) for item in (final, workflow, sidecar)):
        raise AccountingError("sealed accounting artifacts must be objects")
    if final.get("runtime_bundle_sha256") != running_bundle:
        raise AccountingError("running runtime bundle does not match sealed final")
    if sidecar.get("runtime_bundle_sha256") != running_bundle:
        raise AccountingError("accounting sidecar runtime bundle drifted")
    if sidecar.get("final_sha256") != _sha256(final_payload):
        raise AccountingError("accounting sidecar final digest drifted")
    if sidecar.get("workflow_id") != workflow.get("workflow_id"):
        raise AccountingError("accounting sidecar workflow identity drifted")
    try:
        validate_sidecar("accounting", sidecar)
    except ProtocolError as exc:
        raise AccountingError(str(exc)) from exc
    metrics: dict[str, Any] = {}
    try:
        validate_replay(
            root,
            workflow_sha256=_sha256(workflow_payload),
            final_sha256=_sha256(final_payload),
            metrics_out=metrics,
        )
    except ProtocolError as exc:
        raise AccountingError(f"final replay failed during accounting verification: {exc}") from exc
    if sidecar["external_task_usage"] != _external_summary(metrics):
        raise AccountingError("external task accounting drifted from final replay")

    native = sidecar["native_orchestrator"]
    native_payload = _source_bytes(root / native["evidence_ref"], "sealed native observation")
    native_raw = _source_bytes(root / native["raw_evidence_ref"], "sealed native raw evidence")
    if _sha256(native_payload) != native["evidence_sha256"] or _sha256(native_raw) != native["raw_evidence_sha256"]:
        raise AccountingError("sealed native accounting evidence digest drifted")
    try:
        native_observation = json.loads(native_payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AccountingError("sealed native observation is invalid JSON") from exc
    if not isinstance(native_observation, dict):
        raise AccountingError("sealed native observation must be an object")
    _replay_native_observation(native_observation, raw_payload=native_raw, raw_path=root / native["raw_evidence_ref"])
    if _native_summary(
        native_observation,
        evidence_ref=native["evidence_ref"],
        evidence_sha256=native["evidence_sha256"],
        raw_evidence_ref=native["raw_evidence_ref"],
        raw_evidence_sha256=native["raw_evidence_sha256"],
    ) != native:
        raise AccountingError("native accounting summary drifted from sealed observation")

    density = sidecar["completion_density"]
    completion_raw = _source_bytes(root / density["evidence_ref"], "sealed completion raw session")
    projection_payload = _source_bytes(root / density["projection_ref"], "sealed completion projection")
    if _sha256(completion_raw) != density["evidence_sha256"] or _sha256(projection_payload) != density["projection_sha256"]:
        raise AccountingError("sealed completion evidence digest drifted")
    replayed_density, projection = _replay_completion_density(completion_raw, session_id=density["session_id"])
    if _native_terminal_turn(native_observation) != replayed_density["terminal_turn_id"]:
        raise AccountingError("native accounting does not reach the raw session terminal turn")
    if projection_payload != _canonical(projection):
        raise AccountingError("sealed completion projection drifted from raw replay")
    if {key: density[key] for key in replayed_density} != replayed_density:
        raise AccountingError("completion-density summary drifted from raw replay")
    return sidecar


@serialized_authority_transaction
def seal_accounting(
    root: Path,
    *,
    native_source: Path,
    native_evidence_source: Path,
    completion_source: Path,
    running_bundle: str,
) -> Path:
    """Seal one replayable post-terminal accounting sidecar without a model wake."""
    root = Path(root)
    target = root / "accounting/final.json"
    if target.exists() or target.is_symlink():
        verify_accounting(root, running_bundle=running_bundle)
        return target
    final_payload = _source_bytes(root / "final.json", "sealed final")
    workflow_payload = _source_bytes(root / "workflow.json", "sealed workflow")
    try:
        final = json.loads(final_payload)
        workflow = json.loads(workflow_payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AccountingError("sealed workflow or final artifact is unreadable") from exc
    if not isinstance(final, dict) or not isinstance(workflow, dict):
        raise AccountingError("sealed workflow and final must be objects")
    if final.get("runtime_bundle_sha256") != running_bundle:
        raise AccountingError("running runtime bundle does not match sealed final")
    metrics: dict[str, Any] = {}
    try:
        validate_replay(root, workflow_sha256=_sha256(workflow_payload), final_sha256=_sha256(final_payload), metrics_out=metrics)
    except ProtocolError as exc:
        raise AccountingError(f"final replay failed before accounting: {exc}") from exc

    native_payload = _source_bytes(native_source, "native accounting source")
    native_raw_payload = _source_bytes(native_evidence_source, "native raw evidence source")
    completion_payload = _source_bytes(completion_source, "completion raw session source")
    try:
        native_observation = json.loads(native_payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AccountingError("native accounting source is invalid JSON") from exc
    if not isinstance(native_observation, dict):
        raise AccountingError("native accounting source must be an object")
    _replay_native_observation(native_observation, raw_payload=native_raw_payload, raw_path=Path(native_evidence_source))
    completion_session_id = _session_id(completion_payload)
    native_session_id = native_observation.get("thread_id", native_observation.get("session_id"))
    if isinstance(native_session_id, str) and native_session_id != completion_session_id:
        raise AccountingError("native and completion evidence session identities differ")
    density, projection = _replay_completion_density(completion_payload, session_id=completion_session_id)
    if _native_terminal_turn(native_observation) != density["terminal_turn_id"]:
        raise AccountingError("native accounting does not reach the raw session terminal turn")
    projection_payload = _canonical(projection)
    native_ref = "accounting/evidence/native-observation.json"
    native_raw_ref = "accounting/evidence/native-raw.jsonl"
    completion_ref = "accounting/evidence/completion-session.jsonl"
    projection_ref = "accounting/evidence/completion-projection.json"
    native = _native_summary(
        native_observation,
        evidence_ref=native_ref,
        evidence_sha256=_sha256(native_payload),
        raw_evidence_ref=native_raw_ref,
        raw_evidence_sha256=_sha256(native_raw_payload),
    )
    external = _external_summary(metrics)
    exact = external["confidence"] == "exact" and native["coverage"] == "exact"
    workflow_tokens = external["total"] + native["tokens"] if exact else None
    sidecar = {
        "schema_version": "agent-workflow.accounting.vnext.v1",
        "workflow_id": workflow.get("workflow_id"),
        "final_ref": "final.json",
        "final_sha256": _sha256(final_payload),
        "runtime_bundle_sha256": running_bundle,
        "boundary": "through_orchestrator_terminal",
        "coverage": "exact" if exact else "partial",
        "confidence": "exact" if exact else "partial",
        "workflow_tokens": workflow_tokens,
        "external_task_usage": external,
        "native_orchestrator": native,
        "completion_density": {
            **density,
            "evidence_ref": completion_ref,
            "evidence_sha256": _sha256(completion_payload),
            "projection_ref": projection_ref,
            "projection_sha256": _sha256(projection_payload),
        },
        "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
    }
    try:
        validate_sidecar("accounting", sidecar)
        _ensure_evidence(root, native_ref, native_payload)
        _ensure_evidence(root, native_raw_ref, native_raw_payload)
        _ensure_evidence(root, completion_ref, completion_payload)
        _ensure_evidence(root, projection_ref, projection_payload)
        return create_once_json(root, "accounting/final.json", sidecar)
    except (ArtifactError, ProtocolError) as exc:
        raise AccountingError(str(exc)) from exc
