#!/usr/bin/env python3
"""Exact, fail-closed token accounting from native runtime session events."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TOKEN_USAGE_SCHEMA = "agent-workflow.token-usage.v2"
TOKEN_EVIDENCE_SCHEMA = "agent-workflow.token-evidence.v1"
RUNTIMES = {"codex", "claude"}
USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_tokens",
)


class TokenAccountingError(ValueError):
    """Raised when exact accounting cannot be established."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def canonical_sha256(value: Any) -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TokenAccountingError(f"Cannot read JSON object {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise TokenAccountingError(f"Expected JSON object in {path}")
    return value


def write_object(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def zero_usage(*, reasoning_available: bool = True) -> dict[str, int | None]:
    return {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0 if reasoning_available else None,
        "total_tokens": 0,
    }


def add_usage(items: list[dict[str, int | None]]) -> dict[str, int | None]:
    result = zero_usage()
    for field in USAGE_FIELDS:
        values = [item.get(field) for item in items]
        result[field] = sum(value for value in values if isinstance(value, int))
        if field == "reasoning_tokens" and any(value is None for value in values):
            result[field] = None
    return result


def subtract_usage(
    end: dict[str, int | None], start: dict[str, int | None]
) -> dict[str, int | None]:
    result: dict[str, int | None] = {}
    for field in USAGE_FIELDS:
        end_value = end.get(field)
        start_value = start.get(field)
        if end_value is None or start_value is None:
            result[field] = None
            continue
        if not isinstance(end_value, int) or not isinstance(start_value, int):
            raise TokenAccountingError(f"Usage field {field} must be an integer or null")
        if end_value < start_value:
            raise TokenAccountingError(f"Usage counter {field} moved backwards")
        result[field] = end_value - start_value
    return result


def new_token_usage() -> dict[str, Any]:
    return {
        "schema_version": TOKEN_USAGE_SCHEMA,
        "status": "pending",
        "source": "runtime_session_events",
        "confidence": "pending",
        "unit": "tokens",
        "strategy": "actor_deltas",
        "total_tokens": None,
        "input_tokens": None,
        "cached_input_tokens": None,
        "cache_creation_input_tokens": None,
        "cache_read_input_tokens": None,
        "output_tokens": None,
        "reasoning_tokens": None,
        "method": (
            "Pending native runtime session accounting. Run token_accounting.py start "
            "before dispatch, register every spawned attempt, and finalize after the final gate."
        ),
        "boundary": {
            "start": "latest completed runtime usage event before accounting start",
            "end": "latest completed runtime usage event before accounting finalizer",
            "includes": [
                "lead workflow completions after the start snapshot",
                "every registered native subagent attempt",
                "failed attempts, retries, fallbacks, reviews, challenges, repairs, and verification",
            ],
            "excludes": [
                "the accounting finalizer completion",
                "the final user-facing response",
            ],
            "final_user_response_included": False,
            "exclusive_to_workflow": True,
        },
        "accounting": {
            "runtime": None,
            "lead_session_id": None,
            "started_at": None,
            "finalized_at": None,
            "participants": [],
        },
        "measurements": [],
        "coverage": {
            "expected_execution_refs": [],
            "covered_execution_refs": [],
            "uncovered_execution_refs": [],
            "overlapping_execution_refs": [],
        },
        "evidence_ref": "token-evidence.json",
        "evidence_sha256": None,
        "round_breakdown": [],
        "agent_breakdown": [],
        "notes": [],
    }


def detect_runtime() -> tuple[str, str]:
    codex_id = os.environ.get("CODEX_THREAD_ID")
    if codex_id:
        return "codex", codex_id
    claude_id = os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CLAUDE_SESSION_ID")
    if claude_id:
        return "claude", claude_id
    raise TokenAccountingError(
        "Cannot detect a native runtime session; pass --runtime and --lead-session-id"
    )


def default_runtime_root(runtime: str) -> Path:
    if runtime == "codex":
        return Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
    return Path(os.environ.get("CLAUDE_HOME", "~/.claude")).expanduser()


def locate_session(runtime: str, session_id: str, root: Path, *, lead: bool) -> Path:
    if not session_id or "/" in session_id or session_id in {".", ".."}:
        raise TokenAccountingError("Session id must be a non-path identifier")
    candidates: list[Path] = []
    if runtime == "codex":
        search_roots = [root / "sessions", root / "archived_sessions"]
        for search_root in search_roots:
            if search_root.is_dir():
                candidates.extend(search_root.rglob(f"*{session_id}*.jsonl"))
    else:
        search_root = root / "projects" if (root / "projects").is_dir() else root
        candidates.extend(search_root.rglob(f"*{session_id}*.jsonl"))
        if lead:
            candidates = [path for path in candidates if "subagents" not in path.parts]
        else:
            candidates = [path for path in candidates if "subagents" in path.parts]
    unique = sorted({path.resolve() for path in candidates if path.is_file()})
    if len(unique) != 1:
        raise TokenAccountingError(
            f"Expected exactly one {runtime} session log for {session_id}, found {len(unique)}"
        )
    return unique[0]


def normalize_agent_id(value: str) -> str:
    return value[6:] if value.startswith("agent-") else value


def canonical_agent_id(runtime: str, value: str) -> str:
    return normalize_agent_id(value) if runtime == "claude" else value


def codex_session_meta(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            first = json.loads(handle.readline())
    except (OSError, json.JSONDecodeError) as exc:
        raise TokenAccountingError(f"Cannot read Codex session metadata from {path}") from exc
    payload = first.get("payload") if isinstance(first, dict) else None
    if first.get("type") != "session_meta" or not isinstance(payload, dict):
        raise TokenAccountingError(f"Codex session log lacks session_meta: {path}")
    session_id = payload.get("id")
    if not isinstance(session_id, str) or not session_id:
        raise TokenAccountingError(f"Codex session_meta lacks id: {path}")
    return {
        "id": session_id,
        "parent_id": payload.get("parent_thread_id"),
        "timestamp": str(payload.get("timestamp") or first.get("timestamp") or ""),
        "thread_source": payload.get("thread_source"),
    }


def claude_session_meta(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if line_number > 50:
                    break
                item = json.loads(line)
                if not isinstance(item, dict) or not isinstance(item.get("sessionId"), str):
                    continue
                return {
                    "id": item.get("agentId") or item["sessionId"],
                    "parent_id": item["sessionId"] if item.get("agentId") else None,
                    "timestamp": str(item.get("timestamp") or ""),
                    "agent_id": item.get("agentId"),
                }
    except (OSError, json.JSONDecodeError) as exc:
        raise TokenAccountingError(f"Cannot read Claude session metadata from {path}") from exc
    raise TokenAccountingError(f"Claude session log lacks sessionId metadata: {path}")


def validate_session_identity(runtime: str, path: Path, session_id: str, *, lead: bool) -> None:
    meta = codex_session_meta(path) if runtime == "codex" else claude_session_meta(path)
    actual = str(meta["id"])
    if runtime == "claude" and not lead:
        if normalize_agent_id(actual) != normalize_agent_id(session_id):
            raise TokenAccountingError(
                f"Claude agent metadata {actual} does not match requested session {session_id}"
            )
    elif actual != session_id:
        raise TokenAccountingError(
            f"{runtime} session metadata {actual} does not match requested session {session_id}"
        )


def discover_runtime_agents(
    runtime: str,
    root: Path,
    lead_session_id: str,
    lead_path: Path,
    started_at: str,
) -> set[str]:
    boundary = _timestamp(started_at)
    if runtime == "codex":
        metas: dict[str, dict[str, Any]] = {}
        for search_root in (root / "sessions", root / "archived_sessions"):
            if not search_root.is_dir():
                continue
            for path in search_root.rglob("*.jsonl"):
                try:
                    meta = codex_session_meta(path)
                except TokenAccountingError:
                    continue
                metas[str(meta["id"])] = meta
        descendants = {lead_session_id}
        changed = True
        while changed:
            changed = False
            for session_id, meta in metas.items():
                if session_id in descendants or meta.get("parent_id") not in descendants:
                    continue
                if not _is_timestamp(meta.get("timestamp")):
                    continue
                if _timestamp(meta["timestamp"]) < boundary:
                    continue
                descendants.add(session_id)
                changed = True
        return descendants - {lead_session_id}
    subagents_root = lead_path.with_suffix("") / "subagents"
    discovered: set[str] = set()
    if not subagents_root.is_dir():
        return discovered
    for path in subagents_root.rglob("*.jsonl"):
        try:
            meta = claude_session_meta(path)
        except TokenAccountingError:
            continue
        agent_id = meta.get("agent_id")
        if not isinstance(agent_id, str) or not _is_timestamp(meta.get("timestamp")):
            continue
        if _timestamp(meta["timestamp"]) >= boundary:
            discovered.add(normalize_agent_id(agent_id))
    return discovered


def _usage_snapshot(
    *,
    runtime: str,
    session_id: str,
    usage: dict[str, int | None],
    event_ref: str,
    event_sha256: str,
    captured_at: str,
    terminal: bool,
    source_path: Path | None = None,
) -> dict[str, Any]:
    snapshot = {
        "runtime": runtime,
        "session_id": session_id,
        "captured_at": captured_at,
        "event_ref": event_ref,
        "event_sha256": event_sha256,
        "terminal": terminal,
        "usage": usage,
    }
    if source_path is not None:
        snapshot["source_path"] = str(source_path.resolve())
    return snapshot


def parse_codex_session(path: Path, session_id: str) -> dict[str, Any]:
    latest: dict[str, Any] | None = None
    latest_line = 0
    terminal_line = 0
    terminal_reason: str | None = None
    last_json_line = 0
    with path.open("rb") as handle:
        for line_number, raw in enumerate(handle, start=1):
            try:
                item = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise TokenAccountingError(f"Invalid Codex JSONL at {path}:{line_number}") from exc
            last_json_line = line_number
            payload = item.get("payload") if isinstance(item, dict) else None
            if not isinstance(payload, dict):
                continue
            if item.get("type") == "event_msg" and payload.get("type") in {
                "task_complete",
                "turn_aborted",
            }:
                terminal_line = line_number
                terminal_reason = str(payload.get("type"))
            if item.get("type") != "event_msg" or payload.get("type") != "token_count":
                continue
            info = payload.get("info")
            counters = info.get("total_token_usage") if isinstance(info, dict) else None
            if not isinstance(counters, dict):
                raise TokenAccountingError(f"Codex token_count missing total_token_usage at {path}:{line_number}")
            usage = {
                "input_tokens": counters.get("input_tokens"),
                "cached_input_tokens": counters.get("cached_input_tokens"),
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": counters.get("cached_input_tokens"),
                "output_tokens": counters.get("output_tokens"),
                "reasoning_tokens": counters.get("reasoning_output_tokens"),
                "total_tokens": counters.get("total_tokens"),
            }
            for field, value in usage.items():
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    raise TokenAccountingError(
                        f"Codex usage field {field} is invalid at {path}:{line_number}"
                    )
            latest_line = line_number
            latest = _usage_snapshot(
                runtime="codex",
                session_id=session_id,
                usage=usage,
                event_ref=f"codex:{session_id}:token_count:{line_number}",
                event_sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
                captured_at=str(item.get("timestamp") or ""),
                terminal=False,
                source_path=path,
            )
    if latest is None:
        raise TokenAccountingError(f"No Codex token_count event found for {session_id}")
    latest["terminal"] = terminal_line > latest_line and terminal_line == last_json_line
    latest["terminal_reason"] = terminal_reason if latest["terminal"] else None
    return latest


def _claude_usage(message: dict[str, Any]) -> dict[str, int | None]:
    usage = message.get("usage")
    if not isinstance(usage, dict):
        raise TokenAccountingError("Claude assistant message is missing usage")
    input_tokens = usage.get("input_tokens", 0)
    cache_creation = usage.get("cache_creation_input_tokens", 0)
    cache_read = usage.get("cache_read_input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    values = (input_tokens, cache_creation, cache_read, output_tokens)
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in values):
        raise TokenAccountingError("Claude usage contains an invalid token count")
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cache_creation + cache_read,
        "cache_creation_input_tokens": cache_creation,
        "cache_read_input_tokens": cache_read,
        "output_tokens": output_tokens,
        "reasoning_tokens": None,
        "total_tokens": input_tokens + cache_creation + cache_read + output_tokens,
    }


def parse_claude_session(
    path: Path,
    session_id: str,
    *,
    exclude_trailing_tool_use: bool = False,
) -> dict[str, Any]:
    completed: dict[str, tuple[dict[str, int | None], str, bytes, int, str]] = {}
    seen_partial: set[str] = set()
    latest_timestamp = ""
    last_assistant_id: str | None = None
    last_assistant_stop: str | None = None
    with path.open("rb") as handle:
        for line_number, raw in enumerate(handle, start=1):
            try:
                item = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise TokenAccountingError(f"Invalid Claude JSONL at {path}:{line_number}") from exc
            if not isinstance(item, dict) or item.get("type") != "assistant":
                continue
            message = item.get("message")
            if not isinstance(message, dict) or not isinstance(message.get("id"), str):
                continue
            message_id = message["id"]
            seen_partial.add(message_id)
            last_assistant_id = message_id
            last_assistant_stop = (
                str(message.get("stop_reason"))
                if message.get("stop_reason") is not None
                else None
            )
            if message.get("stop_reason") is None:
                continue
            normalized = _claude_usage(message)
            previous = completed.get(message_id)
            if previous is not None and previous[0] != normalized:
                raise TokenAccountingError(f"Claude finalized usage drift for message {message_id}")
            timestamp = str(item.get("timestamp") or "")
            completed[message_id] = (
                normalized,
                timestamp,
                raw,
                line_number,
                str(message.get("stop_reason")),
            )
            latest_timestamp = max(latest_timestamp, timestamp)
    if (
        exclude_trailing_tool_use
        and last_assistant_id is not None
        and last_assistant_stop == "tool_use"
    ):
        completed.pop(last_assistant_id, None)
        seen_partial.discard(last_assistant_id)
    unfinished = seen_partial - set(completed)
    usage = add_usage([record[0] for record in completed.values()])
    digest_rows = [
        {
            "message_id": key,
            "usage": value[0],
            "timestamp": value[1],
            "stop_reason": value[4],
        }
        for key, value in sorted(completed.items())
    ]
    snapshot = _usage_snapshot(
        runtime="claude",
        session_id=session_id,
        usage=usage,
        event_ref=f"claude:{session_id}:finalized-messages:{len(completed)}",
        event_sha256=canonical_sha256(digest_rows),
        captured_at=latest_timestamp,
        terminal=(
            bool(completed)
            and not unfinished
            and last_assistant_id in completed
            and last_assistant_stop in {"end_turn", "stop_sequence"}
        ),
        source_path=path,
    )
    snapshot["message_ids"] = sorted(completed)
    snapshot["terminal_reason"] = last_assistant_stop if snapshot["terminal"] else None
    return snapshot


def parse_session(
    runtime: str,
    path: Path,
    session_id: str,
    *,
    exclude_trailing_tool_use: bool = False,
) -> dict[str, Any]:
    if runtime == "codex":
        return parse_codex_session(path, session_id)
    return parse_claude_session(
        path,
        session_id,
        exclude_trailing_tool_use=exclude_trailing_tool_use,
    )


def start_accounting(
    workflow_dir: Path,
    *,
    runtime: str | None = None,
    lead_session_id: str | None = None,
    runtime_root: Path | None = None,
) -> dict[str, Any]:
    usage_path = workflow_dir / "token-usage.json"
    value = load_object(usage_path)
    if value.get("schema_version") != TOKEN_USAGE_SCHEMA:
        raise TokenAccountingError("Exact accounting start requires token-usage v2")
    accounting = value.get("accounting")
    if not isinstance(accounting, dict):
        raise TokenAccountingError("token-usage v2 is missing accounting")
    if accounting.get("started_at") is not None:
        raise TokenAccountingError("Token accounting has already started")
    if runtime is None or lead_session_id is None:
        detected_runtime, detected_id = detect_runtime()
        runtime = runtime or detected_runtime
        lead_session_id = lead_session_id or detected_id
    if runtime not in RUNTIMES:
        raise TokenAccountingError(f"Unsupported runtime: {runtime}")
    root = (runtime_root or default_runtime_root(runtime)).expanduser().resolve()
    lead_path = locate_session(runtime, lead_session_id, root, lead=True)
    validate_session_identity(runtime, lead_path, lead_session_id, lead=True)
    start = parse_session(runtime, lead_path, lead_session_id)
    start["terminal"] = False
    started_at = utc_now()
    accounting.update(
        {
            "runtime": runtime,
            "lead_session_id": lead_session_id,
            "started_at": started_at,
            "finalized_at": None,
            "participants": [],
        }
    )
    evidence = {
        "schema_version": TOKEN_EVIDENCE_SCHEMA,
        "runtime": runtime,
        "lead_session_id": lead_session_id,
        "started_at": started_at,
        "finalized_at": None,
        "lead": {"start": start, "end": None},
        "agents": [],
    }
    value["notes"] = [
        "Exact accounting starts at the latest completed runtime usage event before this command.",
        "The finalizer completion and final user response are outside the accounting boundary.",
    ]
    write_object(workflow_dir / "token-evidence.json", evidence)
    write_object(usage_path, value)
    return value


def register_agent(
    workflow_dir: Path,
    *,
    execution_ref: str,
    agent_id: str,
    round_id: str,
    lane_id: str,
) -> dict[str, Any]:
    value = load_object(workflow_dir / "token-usage.json")
    accounting = value.get("accounting")
    if value.get("schema_version") != TOKEN_USAGE_SCHEMA or not isinstance(accounting, dict):
        raise TokenAccountingError("Agent registration requires token-usage v2")
    if not accounting.get("started_at") or accounting.get("finalized_at"):
        raise TokenAccountingError("Register agents after start and before finalization")
    participants = accounting.get("participants")
    if not isinstance(participants, list):
        raise TokenAccountingError("accounting.participants must be a list")
    if not all(isinstance(item, dict) for item in participants):
        raise TokenAccountingError("accounting.participants contains a non-object")
    if any(item.get("execution_ref") == execution_ref for item in participants):
        raise TokenAccountingError(f"Duplicate execution_ref: {execution_ref}")
    participants.append(
        {
            "execution_ref": execution_ref,
            "agent_id": agent_id,
            "round_id": round_id,
            "lane_id": lane_id,
        }
    )
    write_object(workflow_dir / "token-usage.json", value)
    return value


def _measurement(
    subject_kind: str,
    subject_id: str,
    execution_refs: list[str],
    start: dict[str, Any],
    end: dict[str, Any],
) -> dict[str, Any]:
    delta = subtract_usage(end["usage"], start["usage"])
    return {
        "subject_kind": subject_kind,
        "subject_id": subject_id,
        "execution_refs": execution_refs,
        "start": start,
        "end": end,
        "delta": delta,
        "delta_tokens": delta["total_tokens"],
    }


def finalize_accounting(
    workflow_dir: Path,
    *,
    runtime_root: Path | None = None,
) -> dict[str, Any]:
    usage_path = workflow_dir / "token-usage.json"
    value = load_object(usage_path)
    evidence_path = workflow_dir / "token-evidence.json"
    evidence = load_object(evidence_path)
    accounting = value.get("accounting")
    if value.get("schema_version") != TOKEN_USAGE_SCHEMA or not isinstance(accounting, dict):
        raise TokenAccountingError("Exact finalization requires token-usage v2")
    runtime = accounting.get("runtime")
    lead_session_id = accounting.get("lead_session_id")
    if runtime not in RUNTIMES or not isinstance(lead_session_id, str):
        raise TokenAccountingError("Token accounting was not started with a native runtime")
    if accounting.get("finalized_at") is not None:
        raise TokenAccountingError("Token accounting has already been finalized")
    root = (runtime_root or default_runtime_root(runtime)).expanduser().resolve()
    lead_path = locate_session(runtime, lead_session_id, root, lead=True)
    validate_session_identity(runtime, lead_path, lead_session_id, lead=True)
    lead_end = parse_session(
        runtime,
        lead_path,
        lead_session_id,
        exclude_trailing_tool_use=runtime == "claude",
    )
    lead_start = evidence.get("lead", {}).get("start")
    if not isinstance(lead_start, dict):
        raise TokenAccountingError("Token evidence is missing the lead start snapshot")
    measurements = [
        _measurement("lead", lead_session_id, ["lead"], lead_start, lead_end)
    ]
    participants = accounting.get("participants")
    if not isinstance(participants, list) or not all(isinstance(item, dict) for item in participants):
        raise TokenAccountingError("accounting.participants must be a list of objects")
    by_agent: dict[str, list[dict[str, Any]]] = {}
    for participant in participants:
        agent_id = participant.get("agent_id")
        execution_ref = participant.get("execution_ref")
        if not isinstance(agent_id, str) or not isinstance(execution_ref, str):
            raise TokenAccountingError("Every participant needs agent_id and execution_ref")
        by_agent.setdefault(agent_id, []).append(participant)
    discovered = discover_runtime_agents(
        runtime,
        root,
        lead_session_id,
        lead_path,
        str(accounting.get("started_at")),
    )
    registered = {canonical_agent_id(runtime, agent_id) for agent_id in by_agent}
    if discovered != registered:
        missing = sorted(discovered - registered)
        unknown = sorted(registered - discovered)
        details = []
        if missing:
            details.append("unregistered runtime agents: " + ", ".join(missing))
        if unknown:
            details.append("registered agents outside workflow tree: " + ", ".join(unknown))
        raise TokenAccountingError("; ".join(details))
    agent_evidence: list[dict[str, Any]] = []
    for agent_id, records in sorted(by_agent.items()):
        agent_path = locate_session(runtime, agent_id, root, lead=False)
        validate_session_identity(runtime, agent_path, agent_id, lead=False)
        end = parse_session(runtime, agent_path, agent_id)
        if not end.get("terminal"):
            raise TokenAccountingError(f"Agent session is not terminal: {agent_id}")
        start = _usage_snapshot(
            runtime=runtime,
            session_id=agent_id,
            usage=zero_usage(reasoning_available=runtime == "codex"),
            event_ref=f"{runtime}:{agent_id}:session-origin",
            event_sha256=canonical_sha256({"runtime": runtime, "session_id": agent_id, "origin": 0}),
            captured_at=str(accounting.get("started_at")),
            terminal=False,
            source_path=agent_path,
        )
        if runtime == "claude":
            start["message_ids"] = []
        execution_refs = sorted(str(record["execution_ref"]) for record in records)
        measurements.append(_measurement("agent_session", agent_id, execution_refs, start, end))
        agent_evidence.append(
            {
                "agent_id": agent_id,
                "execution_refs": execution_refs,
                "round_ids": sorted({str(record.get("round_id")) for record in records}),
                "lane_ids": sorted({str(record.get("lane_id")) for record in records}),
                "end": end,
            }
        )
    aggregate = add_usage([measurement["delta"] for measurement in measurements])
    expected = ["lead"] + sorted(
        str(participant["execution_ref"]) for participant in participants
    )
    finalized_at = utc_now()
    evidence.update(
        {
            "finalized_at": finalized_at,
            "lead": {"start": lead_start, "end": lead_end},
            "agents": agent_evidence,
        }
    )
    write_object(evidence_path, evidence)
    accounting["finalized_at"] = finalized_at
    value.update(
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
            "method": (
                "Exact actor-delta sum from native runtime session events: lead end minus "
                "start, plus every registered terminal subagent session total."
            ),
            "measurements": measurements,
            "coverage": {
                "expected_execution_refs": expected,
                "covered_execution_refs": list(expected),
                "uncovered_execution_refs": [],
                "overlapping_execution_refs": [],
            },
            "evidence_sha256": file_sha256(evidence_path),
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
    write_object(usage_path, value)
    return value


def _is_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _validate_usage_semantics(
    usage: Any, runtime: str, label: str, failures: list[str]
) -> None:
    if not isinstance(usage, dict):
        failures.append(f"{label} must be an object")
        return
    for field in USAGE_FIELDS:
        value = usage.get(field)
        if field == "reasoning_tokens" and runtime == "claude" and value is None:
            continue
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            failures.append(f"{label}.{field} must be a non-negative integer")
    values = {field: usage.get(field) for field in USAGE_FIELDS}
    if runtime == "codex" and all(isinstance(values[field], int) for field in USAGE_FIELDS):
        if values["total_tokens"] != values["input_tokens"] + values["output_tokens"]:
            failures.append(f"{label}.total_tokens must equal Codex input plus output")
        if values["cached_input_tokens"] > values["input_tokens"]:
            failures.append(f"{label}.cached_input_tokens cannot exceed Codex input")
        if values["reasoning_tokens"] > values["output_tokens"]:
            failures.append(f"{label}.reasoning_tokens cannot exceed Codex output")
        if values["cache_creation_input_tokens"] != 0:
            failures.append(f"{label}.cache_creation_input_tokens must be zero for Codex")
        if values["cache_read_input_tokens"] != values["cached_input_tokens"]:
            failures.append(f"{label}.cache_read_input_tokens must equal Codex cached input")
    if runtime == "claude":
        numeric_fields = (
            "input_tokens",
            "cached_input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "output_tokens",
            "total_tokens",
        )
        if all(isinstance(values[field], int) for field in numeric_fields):
            if values["cached_input_tokens"] != (
                values["cache_creation_input_tokens"] + values["cache_read_input_tokens"]
            ):
                failures.append(f"{label}.cached_input_tokens must equal Claude cache creation plus reads")
            expected_total = (
                values["input_tokens"]
                + values["cache_creation_input_tokens"]
                + values["cache_read_input_tokens"]
                + values["output_tokens"]
            )
            if values["total_tokens"] != expected_total:
                failures.append(
                    f"{label}.total_tokens must equal Claude input, cache creation, cache reads, and output"
                )
        if values["reasoning_tokens"] is not None:
            failures.append(f"{label}.reasoning_tokens must be null when Claude does not expose it")


def _validate_snapshot_source(
    snapshot: dict[str, Any], runtime: str, label: str, failures: list[str]
) -> None:
    source_value = snapshot.get("source_path")
    if not isinstance(source_value, str) or not source_value:
        failures.append(f"{label}.source_path is required")
        return
    source_path = Path(source_value).expanduser()
    if not source_path.is_file():
        failures.append(f"{label}.source_path does not exist")
        return
    session_id = snapshot.get("session_id")
    if not isinstance(session_id, str):
        failures.append(f"{label}.session_id is required")
        return
    try:
        if runtime == "codex":
            source_meta = codex_session_meta(source_path)
            is_lead = source_meta.get("parent_id") is None
        else:
            source_meta = claude_session_meta(source_path)
            is_lead = source_meta.get("agent_id") is None
        validate_session_identity(
            runtime,
            source_path,
            session_id,
            lead=is_lead,
        )
    except TokenAccountingError as exc:
        failures.append(f"{label}: {exc}")
        return
    event_ref = snapshot.get("event_ref")
    if isinstance(event_ref, str) and event_ref.endswith(":session-origin"):
        return
    if runtime == "codex":
        if not isinstance(event_ref, str):
            failures.append(f"{label}.event_ref is required")
            return
        try:
            line_number = int(event_ref.rsplit(":", 1)[1])
            raw = source_path.read_bytes().splitlines(keepends=True)[line_number - 1]
            item = json.loads(raw)
            payload = item.get("payload") if isinstance(item, dict) else None
            info = payload.get("info") if isinstance(payload, dict) else None
            counters = info.get("total_token_usage") if isinstance(info, dict) else None
        except (ValueError, IndexError, OSError, json.JSONDecodeError):
            failures.append(f"{label}.event_ref cannot be resolved")
            return
        if not isinstance(counters, dict):
            failures.append(f"{label}.event_ref is not a Codex token_count event")
            return
        usage = {
            "input_tokens": counters.get("input_tokens"),
            "cached_input_tokens": counters.get("cached_input_tokens"),
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": counters.get("cached_input_tokens"),
            "output_tokens": counters.get("output_tokens"),
            "reasoning_tokens": counters.get("reasoning_output_tokens"),
            "total_tokens": counters.get("total_tokens"),
        }
        if snapshot.get("event_sha256") != "sha256:" + hashlib.sha256(raw).hexdigest():
            failures.append(f"{label}.event_sha256 does not match the Codex source event")
        if snapshot.get("usage") != usage:
            failures.append(f"{label}.usage does not match the Codex source event")
        return
    message_ids = snapshot.get("message_ids")
    if not isinstance(message_ids, list) or not all(isinstance(item, str) for item in message_ids):
        failures.append(f"{label}.message_ids must bind Claude usage records")
        return
    completed: dict[str, dict[str, Any]] = {}
    try:
        with source_path.open("rb") as handle:
            for raw in handle:
                item = json.loads(raw)
                if not isinstance(item, dict) or item.get("type") != "assistant":
                    continue
                message = item.get("message")
                if (
                    not isinstance(message, dict)
                    or not isinstance(message.get("id"), str)
                    or message.get("stop_reason") is None
                ):
                    continue
                usage = _claude_usage(message)
                row = {
                    "message_id": message["id"],
                    "usage": usage,
                    "timestamp": str(item.get("timestamp") or ""),
                    "stop_reason": str(message.get("stop_reason")),
                }
                previous = completed.get(message["id"])
                if previous is not None and previous != row:
                    failures.append(f"{label} Claude finalized message usage drift")
                    return
                completed[message["id"]] = row
    except (OSError, json.JSONDecodeError, TokenAccountingError) as exc:
        failures.append(f"{label} cannot read Claude source usage: {exc}")
        return
    if any(message_id not in completed for message_id in message_ids):
        failures.append(f"{label}.message_ids are missing from the Claude source log")
        return
    rows = [completed[message_id] for message_id in sorted(message_ids)]
    usage = add_usage([row["usage"] for row in rows])
    if snapshot.get("event_sha256") != canonical_sha256(rows):
        failures.append(f"{label}.event_sha256 does not match Claude source messages")
    if snapshot.get("usage") != usage:
        failures.append(f"{label}.usage does not match Claude source messages")


def validate_v2(value: Any, workflow_dir: Path, *, final: bool) -> list[str]:
    failures: list[str] = []
    label = str(workflow_dir / "token-usage.json")
    if not isinstance(value, dict):
        return [f"{label} must be an object"]
    if value.get("schema_version") != TOKEN_USAGE_SCHEMA:
        return [f"{label}.schema_version must be {TOKEN_USAGE_SCHEMA}"]
    if value.get("source") != "runtime_session_events":
        failures.append(f"{label}.source must be runtime_session_events")
    if value.get("strategy") != "actor_deltas":
        failures.append(f"{label}.strategy must be actor_deltas")
    boundary = value.get("boundary")
    if (
        not isinstance(boundary, dict)
        or boundary.get("final_user_response_included") is not False
        or boundary.get("exclusive_to_workflow") is not True
    ):
        failures.append(f"{label}.boundary must explicitly exclude the final user response")
    accounting = value.get("accounting")
    if not isinstance(accounting, dict):
        failures.append(f"{label}.accounting must be an object")
        return failures
    participants = accounting.get("participants")
    if not isinstance(participants, list) or not all(isinstance(item, dict) for item in participants):
        failures.append(f"{label}.accounting.participants must be a list of objects")
        participants = []
    execution_refs = [item.get("execution_ref") for item in participants]
    if any(not isinstance(item, str) or not item for item in execution_refs):
        failures.append(f"{label} participant execution_ref values must be non-empty strings")
    if len(execution_refs) != len(set(execution_refs)):
        failures.append(f"{label} participant execution_ref values must be unique")
    if not final:
        return failures
    if value.get("status") != "complete" or value.get("confidence") != "exact":
        failures.append(f"{label} final v2 accounting must be complete and exact")
    if accounting.get("runtime") not in RUNTIMES:
        failures.append(f"{label}.accounting.runtime must be codex or claude")
    if not isinstance(accounting.get("lead_session_id"), str):
        failures.append(f"{label}.accounting.lead_session_id is required")
    if not _is_timestamp(accounting.get("started_at")) or not _is_timestamp(
        accounting.get("finalized_at")
    ):
        failures.append(f"{label} needs valid started_at and finalized_at timestamps")
    elif _timestamp(accounting["started_at"]) >= _timestamp(accounting["finalized_at"]):
        failures.append(f"{label}.accounting.started_at must precede finalized_at")
    measurements = value.get("measurements")
    if not isinstance(measurements, list) or not measurements:
        failures.append(f"{label}.measurements must be a non-empty list")
        measurements = []
    subject_ids: set[str] = set()
    measured_refs: list[str] = []
    deltas: list[dict[str, int | None]] = []
    for index, measurement in enumerate(measurements):
        item_label = f"{label}.measurements[{index}]"
        if not isinstance(measurement, dict):
            failures.append(f"{item_label} must be an object")
            continue
        subject_id = measurement.get("subject_id")
        if not isinstance(subject_id, str) or not subject_id or subject_id in subject_ids:
            failures.append(f"{item_label}.subject_id must be a unique non-empty string")
        else:
            subject_ids.add(subject_id)
        refs = measurement.get("execution_refs")
        if not isinstance(refs, list) or not refs or not all(isinstance(ref, str) for ref in refs):
            failures.append(f"{item_label}.execution_refs must be non-empty strings")
            refs = []
        measured_refs.extend(refs)
        start = measurement.get("start")
        end = measurement.get("end")
        delta = measurement.get("delta")
        if not isinstance(start, dict) or not isinstance(end, dict) or not isinstance(delta, dict):
            failures.append(f"{item_label} needs start, end, and delta objects")
            continue
        runtime = accounting.get("runtime")
        if start.get("runtime") != runtime or end.get("runtime") != runtime:
            failures.append(f"{item_label} snapshot runtime must match accounting runtime")
        if start.get("session_id") != subject_id or end.get("session_id") != subject_id:
            failures.append(f"{item_label} snapshot session_id must match subject_id")
        if start.get("source_path") != end.get("source_path"):
            failures.append(f"{item_label} start and end must use the same source session")
        if start.get("event_ref") == end.get("event_ref"):
            failures.append(f"{item_label} start and end event refs must differ")
        if not _is_timestamp(start.get("captured_at")) or not _is_timestamp(end.get("captured_at")):
            failures.append(f"{item_label} snapshots need valid timestamps")
        elif _timestamp(start["captured_at"]) >= _timestamp(end["captured_at"]):
            failures.append(f"{item_label} start snapshot must precede end snapshot")
        if runtime in RUNTIMES:
            _validate_usage_semantics(start.get("usage"), runtime, f"{item_label}.start.usage", failures)
            _validate_usage_semantics(end.get("usage"), runtime, f"{item_label}.end.usage", failures)
            _validate_snapshot_source(start, runtime, f"{item_label}.start", failures)
            _validate_snapshot_source(end, runtime, f"{item_label}.end", failures)
        if measurement.get("subject_kind") == "agent_session" and end.get("terminal") is not True:
            failures.append(f"{item_label} agent end snapshot must be terminal")
        if measurement.get("subject_kind") == "agent_session" and runtime in RUNTIMES:
            source_value = end.get("source_path")
            if isinstance(source_value, str) and Path(source_value).is_file():
                try:
                    current = parse_session(runtime, Path(source_value), str(subject_id))
                except TokenAccountingError as exc:
                    failures.append(f"{item_label} cannot recheck terminal session: {exc}")
                else:
                    if current.get("terminal") is not True:
                        failures.append(f"{item_label} source session is no longer terminal")
                    if (
                        current.get("event_sha256") != end.get("event_sha256")
                        or current.get("usage") != end.get("usage")
                    ):
                        failures.append(f"{item_label} end snapshot is not the current terminal usage")
        try:
            expected_delta = subtract_usage(end.get("usage", {}), start.get("usage", {}))
        except TokenAccountingError as exc:
            failures.append(f"{item_label}: {exc}")
            continue
        if delta != expected_delta or measurement.get("delta_tokens") != expected_delta["total_tokens"]:
            failures.append(f"{item_label} delta must equal end minus start")
        deltas.append(expected_delta)
    aggregate = add_usage(deltas)
    for field in USAGE_FIELDS:
        if value.get(field) != aggregate.get(field):
            failures.append(f"{label}.{field} must equal the measurement aggregate")
    if not isinstance(value.get("total_tokens"), int) or value["total_tokens"] <= 0:
        failures.append(f"{label}.total_tokens must be greater than zero")
    coverage = value.get("coverage")
    if not isinstance(coverage, dict):
        failures.append(f"{label}.coverage must be an object")
    else:
        expected = ["lead"] + sorted(ref for ref in execution_refs if isinstance(ref, str))
        if coverage.get("expected_execution_refs") != expected:
            failures.append(f"{label}.coverage.expected_execution_refs must match registered attempts")
        if coverage.get("covered_execution_refs") != expected:
            failures.append(f"{label}.coverage.covered_execution_refs must match expected attempts")
        if coverage.get("uncovered_execution_refs") != [] or coverage.get("overlapping_execution_refs") != []:
            failures.append(f"{label} final coverage cannot contain gaps or overlaps")
        if sorted(measured_refs) != sorted(expected) or len(measured_refs) != len(set(measured_refs)):
            failures.append(f"{label} every execution ref must be measured exactly once")
    evidence_ref = value.get("evidence_ref")
    if evidence_ref != "token-evidence.json":
        failures.append(f"{label}.evidence_ref must be token-evidence.json")
    else:
        evidence_path = workflow_dir / evidence_ref
        if not evidence_path.is_file():
            failures.append(f"Missing token evidence: {evidence_path}")
        elif value.get("evidence_sha256") != file_sha256(evidence_path):
            failures.append(f"{label}.evidence_sha256 does not match token evidence")
        else:
            try:
                evidence = load_object(evidence_path)
            except TokenAccountingError as exc:
                failures.append(str(exc))
            else:
                if evidence.get("schema_version") != TOKEN_EVIDENCE_SCHEMA:
                    failures.append(f"{evidence_path}.schema_version is invalid")
                lead = evidence.get("lead")
                if not isinstance(lead, dict) or not lead.get("start") or not lead.get("end"):
                    failures.append(f"{evidence_path} needs lead start and end snapshots")
                agents = evidence.get("agents")
                expected_agents = {item.get("agent_id") for item in participants}
                actual_agents = {
                    item.get("agent_id") for item in agents if isinstance(item, dict)
                } if isinstance(agents, list) else set()
                if actual_agents != expected_agents:
                    failures.append(f"{evidence_path} agent coverage must match registered participants")
                measurement_by_subject = {
                    item.get("subject_id"): item
                    for item in measurements
                    if isinstance(item, dict) and isinstance(item.get("subject_id"), str)
                }
                lead_measurement = measurement_by_subject.get(accounting.get("lead_session_id"))
                if (
                    isinstance(lead, dict)
                    and isinstance(lead_measurement, dict)
                    and (
                        lead_measurement.get("start") != lead.get("start")
                        or lead_measurement.get("end") != lead.get("end")
                    )
                ):
                    failures.append(f"{evidence_path} lead snapshots must match lead measurement")
                if isinstance(agents, list):
                    for agent in agents:
                        if not isinstance(agent, dict):
                            continue
                        measurement = measurement_by_subject.get(agent.get("agent_id"))
                        if not isinstance(measurement, dict) or measurement.get("end") != agent.get("end"):
                            failures.append(
                                f"{evidence_path} agent {agent.get('agent_id')} must match its measurement"
                            )
                        elif sorted(measurement.get("execution_refs", [])) != sorted(
                            agent.get("execution_refs", [])
                        ):
                            failures.append(
                                f"{evidence_path} agent {agent.get('agent_id')} execution refs drift"
                            )
    return failures


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    start = subparsers.add_parser("start", help="Capture the lead runtime start snapshot")
    start.add_argument("workflow_dir", type=Path)
    start.add_argument("--runtime", choices=sorted(RUNTIMES))
    start.add_argument("--lead-session-id")
    start.add_argument("--runtime-root", type=Path)
    register = subparsers.add_parser("register-agent", help="Register one spawned execution")
    register.add_argument("workflow_dir", type=Path)
    register.add_argument("--execution-ref", required=True)
    register.add_argument("--agent-id", required=True)
    register.add_argument("--round-id", required=True)
    register.add_argument("--lane-id", required=True)
    finalize = subparsers.add_parser("finalize", help="Compute the exact runtime event delta")
    finalize.add_argument("workflow_dir", type=Path)
    finalize.add_argument("--runtime-root", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "start":
            value = start_accounting(
                args.workflow_dir,
                runtime=args.runtime,
                lead_session_id=args.lead_session_id,
                runtime_root=args.runtime_root,
            )
            print(
                f"Token accounting started: {value['accounting']['runtime']}:"
                f"{value['accounting']['lead_session_id']}"
            )
        elif args.command == "register-agent":
            register_agent(
                args.workflow_dir,
                execution_ref=args.execution_ref,
                agent_id=args.agent_id,
                round_id=args.round_id,
                lane_id=args.lane_id,
            )
            print(f"Registered token participant: {args.execution_ref} -> {args.agent_id}")
        else:
            value = finalize_accounting(args.workflow_dir, runtime_root=args.runtime_root)
            print(f"Exact workflow tokens: {value['total_tokens']:,}")
    except TokenAccountingError as exc:
        print(f"Token accounting failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
