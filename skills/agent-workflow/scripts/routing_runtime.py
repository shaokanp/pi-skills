#!/usr/bin/env python3
"""Resolve immutable model-routing inputs from the current Codex session."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from model_routing import REASONING_EFFORTS
from token_accounting import (
    TokenAccountingError,
    default_runtime_root,
    locate_session,
)


SESSION_PROFILE_SCHEMA = "agent-workflow.routing-session-profile.v1"


class RoutingRuntimeError(ValueError):
    """Raised when current session routing inputs cannot be established."""


def _session_path(explicit_path: str | Path | None = None) -> tuple[Path, str | None]:
    if explicit_path is not None:
        path = Path(explicit_path).expanduser().resolve()
        if not path.is_file():
            raise RoutingRuntimeError(f"Codex runtime session log does not exist: {path}")
        session_id = os.environ.get("CODEX_THREAD_ID", "").strip()
        if not session_id:
            raise RoutingRuntimeError(
                "Current Codex session id is unavailable; an explicit log cannot "
                "replace CODEX_THREAD_ID"
            )
        try:
            canonical = locate_session(
                "codex",
                session_id,
                default_runtime_root("codex"),
                lead=True,
            ).resolve()
        except TokenAccountingError as exc:
            raise RoutingRuntimeError(str(exc)) from exc
        if path != canonical:
            raise RoutingRuntimeError(
                "Explicit Codex runtime session log must be the canonical current-thread log: "
                f"expected {canonical}, observed {path}"
            )
        return canonical, session_id

    session_id = os.environ.get("CODEX_THREAD_ID", "").strip()
    if not session_id:
        raise RoutingRuntimeError(
            "Current Codex session id is unavailable; the host must expose CODEX_THREAD_ID"
        )
    try:
        path = locate_session(
            "codex",
            session_id,
            default_runtime_root("codex"),
            lead=True,
        )
    except TokenAccountingError as exc:
        raise RoutingRuntimeError(str(exc)) from exc
    return path, session_id


def read_session_profile(
    path: Path,
    *,
    expected_session_id: str | None = None,
    through_line: int | None = None,
) -> dict[str, Any]:
    """Read and seal the last complete Codex turn_context from one session."""

    session_id: str | None = None
    last_context: dict[str, Any] | None = None
    prefix = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for line_number, raw in enumerate(handle, start=1):
                if through_line is not None and line_number > through_line:
                    break
                prefix.update(raw)
                item = json.loads(raw)
                payload = item.get("payload") if isinstance(item, dict) else None
                if item.get("type") == "session_meta" and isinstance(payload, dict):
                    observed_id = payload.get("id")
                    if not isinstance(observed_id, str) or not observed_id:
                        raise RoutingRuntimeError("Codex runtime session log lacks session_meta.id")
                    if session_id is not None and session_id != observed_id:
                        raise RoutingRuntimeError("Codex runtime session has conflicting session_meta ids")
                    session_id = observed_id
                    continue
                if item.get("type") != "turn_context" or not isinstance(payload, dict):
                    continue
                model = payload.get("model")
                effort = payload.get("effort")
                if isinstance(model, str) and model and isinstance(effort, str):
                    last_context = {
                        "model": model,
                        "effort": effort,
                        "event_line": line_number,
                        "event_sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
                        "prefix_sha256": "sha256:" + prefix.hexdigest(),
                        "observed_at": str(item.get("timestamp") or ""),
                    }
    except (OSError, json.JSONDecodeError) as exc:
        raise RoutingRuntimeError(f"Cannot parse Codex runtime session log: {path}") from exc
    if session_id is None:
        raise RoutingRuntimeError("Codex runtime session log lacks session_meta.id")
    if expected_session_id is not None and session_id != expected_session_id:
        raise RoutingRuntimeError(
            "Codex runtime session log does not match the current thread: "
            f"expected {expected_session_id}, observed {session_id}"
        )
    if last_context is None:
        raise RoutingRuntimeError("Codex runtime session has no complete turn_context")
    if last_context["effort"] not in REASONING_EFFORTS:
        raise RoutingRuntimeError(
            f"Current Codex reasoning effort is unsupported: {last_context['effort']}"
        )
    return {
        "schema_version": SESSION_PROFILE_SCHEMA,
        "runtime": "codex",
        "session_id": session_id,
        "session_path": str(path),
        "model": last_context["model"],
        "reasoning_effort": last_context["effort"],
        "source": "runtime_turn_context",
        "event_line": last_context["event_line"],
        "event_sha256": last_context["event_sha256"],
        "prefix_sha256": last_context["prefix_sha256"],
        "observed_at": last_context["observed_at"],
    }


def resolve_session_profile(
    explicit_path: str | Path | None = None,
) -> dict[str, Any]:
    path, expected_session_id = _session_path(explicit_path)
    return read_session_profile(path, expected_session_id=expected_session_id)


def resolve_reasoning_effort(
    explicit_effort: str | None,
    *,
    session_log: str | Path | None = None,
) -> tuple[str, dict[str, Any]]:
    profile = resolve_session_profile(session_log)
    observed = str(profile["reasoning_effort"])
    if explicit_effort is not None and explicit_effort != observed:
        raise RoutingRuntimeError(
            "--reasoning-effort is an assertion, not an override; "
            f"current session uses {observed}, not {explicit_effort}"
        )
    return observed, profile
