#!/usr/bin/env python3
"""Transient Agent Workflow vNext runtime; Slice 1 read-only tracer."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import resource
import shutil
import shlex
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

# A pinned runtime directory is immutable authority, not a Python cache root.
sys.dont_write_bytecode = True

from artifact_store import (
    ArtifactError,
    create_once_bytes,
    create_once_json,
    serialized_authority_transaction,
    shared_authority_transaction,
)
from app_resume_adapter import (
    ResumeAdapterFailure,
    _project_turn as project_app_resume_turn,
    _validate_effective_context as validate_app_resume_context,
)
from baseline_gate import (
    BaselineError,
    current_repository_evidence,
    repository_evidence,
    verify_baseline,
    verify_candidate_against_parent,
)
from phase_protocol import (
    CAPABILITY_NAMES,
    ProtocolError,
    TASK_TERMINAL_STATUSES,
    _read_artifact_bytes,
    validate_contract,
    validate_replay_candidate,
    validate_sidecar,
)
from process_supervisor import launch as launch_supervisor
from process_supervisor import load_request as load_supervisor_request
from process_supervisor import live_marker_processes
from process_supervisor import command_matches_request
from process_supervisor import process_birth
from process_supervisor import process_command
from process_supervisor import process_identity as _process_identity
from process_supervisor import reconcile as reconcile_supervisors
from process_supervisor import SupervisorFailure
from process_supervisor import validate_receipt as validate_supervisor_receipt
from recovery_runtime import (
    RecoveryError,
    _committed_plan_refs,
    build_resume_brief,
    commit_phase_authority,
    current_authority_revision,
    prepare_phase_authority,
    seal_amendment,
    seal_resume_brief,
)
from repository_state import RepositoryStateError, collect_repository_state
from source_workspace import (
    DirtyOverlap,
    SourcePhase,
    SourceWriteError,
    _secure_private_directory,
    attest_writer_permissions,
    integrate_isolated_phase,
    load_isolated_phase,
    prepare_isolated_phase,
    prepare_read_only_snapshot,
    revalidate_isolated_phase_launch,
    replay_read_only_snapshot,
    writer_profile_bytes,
)
from vnext_accounting import AccountingError, seal_accounting


class RuntimeFailure(RuntimeError):
    """Raised when the deterministic runtime cannot safely continue."""


class HumanGateRequired(RuntimeFailure):
    """Raised before writer launch when current user state needs approval."""


class PinnedBundleUnavailable(RuntimeFailure):
    """Raised when an admitted workflow cannot replay its exact runtime bundle."""


@dataclass(frozen=True)
class RawExecution:
    """Observed terminal process evidence returned by a worker adapter."""

    exit_code: int
    events: list[dict[str, Any]]
    stderr: str
    turn_context: dict[str, Any] | None
    stdout_bytes: bytes | None = None
    adapter_error: bool = False
    log_limit_exceeded: bool = False
    cancelled: bool = False
    not_started_deadline: bool = False
    interrupted_before_launch: bool = False
    observed_started_at: datetime | None = None
    observed_finished_at: datetime | None = None


@dataclass(frozen=True)
class _PreparedResult:
    task_id: str
    result: dict[str, Any]
    events_payload: bytes
    attestation_payload: bytes
    output_payload: bytes | None


TaskExecutor = Callable[[dict[str, Any], dict[str, Any]], RawExecution]
TerminalFence = Callable[[], None]

_PARSED_EVENT_LIMIT_BYTES = 4 * 1024 * 1024
_PARSED_EVENT_LIMIT_COUNT = 4096
_ISOLATED_WORKER_DEVELOPER_INSTRUCTIONS = (
    "You are an isolated Agent Workflow task actor, not the outer Main session. "
    "Treat the exact user task packet as the complete assignment. This runtime isolation "
    "contract supersedes any skill-catalog trigger rule included later in the host prompt. "
    "Do not read or invoke any SKILL.md unless the sealed task packet explicitly names that "
    "skill file as a required input or acceptance criterion. Do not otherwise invoke skills, "
    "delegate, poll, inspect unrelated files, or perform unrequested lifecycle actions. "
    "Use tools only when the packet's acceptance criteria require them. Never commit, push, "
    "publish, deploy, or modify production. A source-writer snapshot intentionally has no "
    ".git directory or broad project toolchain capability; do not run repository-level Git, "
    "test, or build commands there because the host owns diff, exact-base integration, and "
    "post-integration validation. Return only the declared output schema."
)


@dataclass(frozen=True)
class CodexExecConfig:
    run_root: Path
    repo_root: Path
    codex_home: Path
    top_model: str = "gpt-5.6-sol"
    worker_model: str = "gpt-5.6-terra"
    reasoning_effort: str = "xhigh"
    codex_binary: str = "codex"
    log_limit_bytes: int = 16 * 1024 * 1024
    terminate_grace_seconds: float = 1.0
    workflow_id: str = "unknown-workflow"
    authority_revision: int = 1
    permissions_profile: str = "vnext-read-only"


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _digest_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            hasher.update(chunk)
    return "sha256:" + hasher.hexdigest()


def _create_once_or_verify_bytes(root: Path, relative_path: str, payload: bytes) -> Path:
    """Publish derived evidence once, or replay only byte-identical crash residue."""

    path = root / relative_path
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise RuntimeFailure(f"replayed artifact drifted: {relative_path}")
        return path
    try:
        return create_once_bytes(root, relative_path, payload)
    except ArtifactError as exc:
        raise RuntimeFailure(f"artifact publication failed: {exc}") from exc


def _create_once_or_verify_json(root: Path, relative_path: str, value: Any) -> Path:
    return _create_once_or_verify_bytes(root, relative_path, _canonical(value))


_RUNTIME_BUNDLE_FILES = (
    "app_resume_adapter.py",
    "artifact_store.py",
    "baseline_gate.py",
    "host_validation.py",
    "phase_protocol.py",
    "process_supervisor.py",
    "recovery_runtime.py",
    "repository_state.py",
    "source_workspace.py",
    "test_vnext_runtime.py",
    "vnext_accounting.py",
    "workflow_runtime.py",
)


def _runtime_bundle_sha256() -> str:
    root = Path(__file__).resolve().parent
    manifest = []
    for name in _RUNTIME_BUNDLE_FILES:
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise RuntimeFailure(f"runtime bundle member is missing or unsafe: {name}")
        manifest.append({"path": name, "sha256": _digest_file(path)})
    return _digest(_canonical(manifest))


def _pinned_bundle_manifest(root: Path) -> tuple[Path, list[dict[str, str]]]:
    bundle_root = Path(root).resolve() / "runtime-bundle"
    if bundle_root.is_symlink() or not bundle_root.is_dir():
        raise PinnedBundleUnavailable("pinned runtime bundle is missing or unsafe")
    observed = {path.name for path in bundle_root.iterdir()}
    if observed != set(_RUNTIME_BUNDLE_FILES):
        raise PinnedBundleUnavailable("pinned runtime bundle file set drifted")
    manifest: list[dict[str, str]] = []
    for name in _RUNTIME_BUNDLE_FILES:
        path = bundle_root / name
        if path.is_symlink() or not path.is_file():
            raise PinnedBundleUnavailable(f"pinned runtime member is missing or unsafe: {name}")
        manifest.append({"path": name, "sha256": _digest_file(path)})
    return bundle_root, manifest


def _resolve_pinned_runtime(root: Path, expected_bundle: str) -> Path:
    bundle_root, manifest = _pinned_bundle_manifest(root)
    observed_bundle = _digest(_canonical(manifest))
    if observed_bundle != expected_bundle:
        raise PinnedBundleUnavailable("pinned runtime bundle digest drifted")
    return bundle_root / "workflow_runtime.py"


def _seal_runtime_bundle(root: Path) -> Path:
    """Crash-replayably pin the exact executable bundle before admission commits."""

    source_root = Path(__file__).resolve().parent
    expected_bundle = _runtime_bundle_sha256()
    for name in _RUNTIME_BUNDLE_FILES:
        source = source_root / name
        if source.is_symlink() or not source.is_file():
            raise RuntimeFailure(f"runtime bundle member is missing or unsafe: {name}")
        _create_or_verify_bytes(Path(root), f"runtime-bundle/{name}", source.read_bytes())
    return _resolve_pinned_runtime(root, expected_bundle)


def _resolve_executable(value: str) -> Path:
    raw = Path(value)
    if raw.is_absolute():
        candidate = raw
    else:
        discovered = shutil.which(value)
        if discovered is None:
            raise RuntimeFailure(f"executable is not available: {value}")
        candidate = Path(discovered)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise RuntimeFailure(f"executable cannot be resolved: {value}") from exc
    if not resolved.is_file():
        raise RuntimeFailure(f"executable is not a regular file: {value}")
    return resolved


def _codex_identity(value: str) -> tuple[Path, str, str]:
    binary = _resolve_executable(value)
    observed = subprocess.run(
        [os.fspath(binary), "--version"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    version = observed.stdout.strip()
    if observed.returncode != 0 or not version:
        raise RuntimeFailure("Codex binary version probe failed")
    return binary, _digest_file(binary), version


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise RuntimeFailure(f"{label} must be an RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeFailure(f"{label} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise RuntimeFailure(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _load_fixed_json(root: Path, relative_path: str, label: str) -> dict[str, Any]:
    path = root / relative_path
    if path.is_symlink() or not path.is_file():
        raise RuntimeFailure(f"{label} is missing or unsafe: {relative_path}")
    payload = path.read_bytes()
    if len(payload) > 4 * 1024 * 1024:
        raise RuntimeFailure(f"{label} is too large")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeFailure(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeFailure(f"{label} must be an object")
    return value


def _atomic_derived_json(root: Path, relative_path: str, value: Any) -> Path:
    """Replace one non-authoritative derived view with fsync durability."""

    path = root / relative_path
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise RuntimeFailure("derived view target is unsafe")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink():
        raise RuntimeFailure("derived view parent is unsafe")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        payload = _canonical(value)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise RuntimeFailure("derived view write made no progress")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    return path


def _create_or_verify_bytes(root: Path, relative_path: str, payload: bytes) -> Path:
    """Resume an interrupted publication only when existing bytes are exact."""

    path = root / relative_path
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise RuntimeFailure(f"reconcile artifact drifted: {relative_path}")
        return path
    try:
        return create_once_bytes(root, relative_path, payload)
    except ArtifactError as exc:
        raise RuntimeFailure(str(exc)) from exc


def _create_or_verify_json(root: Path, relative_path: str, value: Any) -> Path:
    return _create_or_verify_bytes(root, relative_path, _canonical(value))


def rebuild_view(root: Path) -> dict[str, Any]:
    """Deterministically project immutable artifacts into one rebuildable view."""

    root = Path(root).resolve()
    workflow = _load_fixed_json(root, "workflow.json", "workflow seal")
    phase_receipts = []
    for path in sorted(root.glob("phases/*/receipt.json")):
        value = _load_fixed_json(root, path.relative_to(root).as_posix(), "phase receipt")
        phase_receipts.append({"ref": path.relative_to(root).as_posix(), "status": value.get("status")})
    attempts = []
    process_root = root / "runtime" / "processes"
    process_records = sorted(process_root.rglob("*.json")) if process_root.is_dir() else []
    for path in process_records:
        active = _load_fixed_json(root, path.relative_to(root).as_posix(), "active process record")
        terminal_ref = active.get("terminal_ref")
        terminal_status = None
        if isinstance(terminal_ref, str) and (root / terminal_ref).is_file():
            terminal_status = _load_fixed_json(root, terminal_ref, "supervisor receipt").get("status")
        attempts.append({"task_id": active.get("task_id"), "active_ref": path.relative_to(root).as_posix(), "terminal_ref": terminal_ref, "terminal_status": terminal_status})
    view = {
        "schema_version": "agent-workflow.view.vnext.v1",
        "workflow_id": workflow["workflow_id"],
        "authority_revision": current_authority_revision(root, workflow),
        "phase_receipts": phase_receipts,
        "attempts": attempts,
    }
    _atomic_derived_json(root, "view.json", view)
    return view


def _generation_claim_ref(plan: dict[str, Any]) -> str:
    contention = {
        "predecessor_sha256": plan["predecessor_sha256"],
        "authority_revision": plan["authority_revision"],
    }
    contention_key = _digest(_canonical(contention))
    return f"generations/claims/{contention_key.removeprefix('sha256:')}.json"


def _seal_generation_claim(
    root: Path,
    workflow: dict[str, Any],
    plan: dict[str, Any],
    plan_payload: bytes,
) -> tuple[str, Path]:
    """Claim the sole predecessor+authority contention key for one generation."""

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
    try:
        validate_sidecar("generation-claim", claim)
    except ProtocolError as exc:
        raise RuntimeFailure("runtime produced an invalid generation claim") from exc
    claim_ref = _generation_claim_ref(plan)
    try:
        claim_path = create_once_json(root, claim_ref, claim)
    except ArtifactError as exc:
        existing = root / claim_ref
        winner = None
        if existing.is_file() and not existing.is_symlink():
            try:
                winner = json.loads(existing.read_bytes()).get("generation_id")
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                pass
        raise RuntimeFailure(
            f"generation contention lost for predecessor and authority; winner={winner or 'unknown'}"
        ) from exc
    return claim_ref, claim_path


def _seal_deadlines(
    root: Path,
    workflow: dict[str, Any],
    plan: dict[str, Any],
    *,
    allow_expired: bool = False,
) -> tuple[dict[str, Any], float]:
    """Seal workflow/phase wall ceilings and derive a same-boot monotonic remainder."""

    now = _now()
    workflow_started = _parse_timestamp(workflow["created_at"], "workflow.created_at")
    workflow_not_after = workflow_started + timedelta(
        seconds=workflow["limits"]["workflow_budget_seconds"]
    )
    phase_not_after = min(
        now + timedelta(seconds=plan["phase_budget_seconds"]),
        workflow_not_after,
    )
    boot_identity = _process_identity(1)
    if boot_identity is None:
        raise RuntimeFailure("host boot identity is unavailable")
    value = {
        "schema_version": "agent-workflow.deadlines.vnext.v1",
        "workflow_id": workflow["workflow_id"],
        "generation_id": plan["generation_id"],
        "phase_id": plan["phase_id"],
        "authority_revision": plan["authority_revision"],
        "workflow_started_at": _timestamp(workflow_started),
        "workflow_not_after": _timestamp(workflow_not_after),
        "phase_started_at": _timestamp(now),
        "phase_not_after": _timestamp(phase_not_after),
        "boot_identity": boot_identity,
    }
    deadline_ref = f"runtime/deadlines/{plan['phase_id']}.json"
    path = root / deadline_ref
    if path.exists():
        persisted = _load_fixed_json(root, deadline_ref, "deadline seal")
        if persisted != value:
            # Resume preserves the original ceiling. Only stable identity fields are compared.
            stable_keys = {
                "schema_version",
                "workflow_id",
                "generation_id",
                "phase_id",
                "authority_revision",
                "workflow_started_at",
                "workflow_not_after",
                "boot_identity",
            }
            if any(persisted.get(key) != value.get(key) for key in stable_keys):
                raise RuntimeFailure("deadline seal drifted across resume")
            value = persisted
    else:
        try:
            create_once_json(root, deadline_ref, value)
        except ArtifactError as exc:
            raise RuntimeFailure(str(exc)) from exc
    if value["boot_identity"] != boot_identity:
        raise RuntimeFailure("deadline remainder cannot be proven across host boot")
    phase_started = _parse_timestamp(value["phase_started_at"], "phase_started_at")
    phase_deadline = _parse_timestamp(value["phase_not_after"], "phase_not_after")
    if now < phase_started - timedelta(seconds=1):
        raise RuntimeFailure("wall clock moved backwards after deadline seal")
    remaining = (phase_deadline - now).total_seconds()
    if remaining <= 0 and not allow_expired:
        raise RuntimeFailure("workflow or phase hard deadline has expired")
    return value, time.monotonic() + max(0.0, remaining)


def _resource_admission(
    root: Path,
    workflow: dict[str, Any],
    requested_parallel: int,
    *,
    log_limit_bytes: int,
) -> dict[str, Any]:
    """Derive capacity from live host facts; unknown or unsafe facts fail closed."""

    if not isinstance(requested_parallel, int) or isinstance(requested_parallel, bool) or requested_parallel < 1:
        raise RuntimeFailure("requested parallelism must be a positive integer")
    admitted = min(
        requested_parallel,
        workflow["limits"]["max_parallel_tasks"],
        workflow["admission"]["host_capacity"]["max_parallel_tasks"],
        workflow["admission"]["host_capacity"]["max_processes"],
    )
    soft_fd, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    required_fd = 64 + admitted * 12
    if soft_fd == resource.RLIM_INFINITY:
        soft_fd_value = 2**31 - 1
    elif isinstance(soft_fd, int) and soft_fd > 0:
        soft_fd_value = soft_fd
    else:
        raise RuntimeFailure("host file-descriptor capacity is unknown")
    if soft_fd_value < required_fd:
        raise RuntimeFailure("host file-descriptor capacity is below the watchdog floor")
    try:
        open_fd_count = len(os.listdir("/dev/fd"))
    except OSError as exc:
        raise RuntimeFailure("live file-descriptor usage is unavailable") from exc
    available_fd = max(0, soft_fd_value - open_fd_count)
    if available_fd < required_fd:
        raise RuntimeFailure("live file-descriptor availability is below the watchdog floor")
    usage = shutil.disk_usage(Path(root).resolve())
    required_disk = 32 * 1024 * 1024 + admitted * log_limit_bytes * 2
    if usage.free < required_disk:
        raise RuntimeFailure("workflow disk floor is not available")
    return {
        "max_parallel_admitted": admitted,
        "fd_soft_limit": soft_fd_value,
        "fd_required": required_fd,
        "fd_open_observed": open_fd_count,
        "fd_available_observed": available_fd,
        "disk_free_bytes": usage.free,
        "disk_required_bytes": required_disk,
        "log_limit_bytes_per_stream": log_limit_bytes,
        "backend_ceiling_source": "sealed_host_capacity_not_live_backend",
    }


def _repository_root_for(path: Path) -> Path:
    current = path.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    raise RuntimeFailure("workflow root is not inside a Git repository")


def _reject_non_json_constant(value: str) -> None:
    raise ValueError(f"non-JSON numeric constant is forbidden: {value}")


def _strict_json_loads(payload: bytes | str) -> Any:
    return json.loads(payload, parse_constant=_reject_non_json_constant)


def _load_packet(root: Path, task: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        payload = _read_artifact_bytes(root, task["packet_path"], task["packet_sha256"])
        packet = _strict_json_loads(payload)
    except (ProtocolError, UnicodeDecodeError, ValueError) as exc:
        raise RuntimeFailure(f"invalid packet for {task['task_id']}: {exc}") from exc
    if not isinstance(packet, dict) or set(packet) != {
        "schema_version",
        "prompt",
        "output_schema_ref",
        "output_schema_sha256",
    }:
        raise RuntimeFailure(f"invalid packet contract for {task['task_id']}")
    if packet["schema_version"] != "agent-workflow.task-packet.vnext.v1":
        raise RuntimeFailure(f"unsupported packet schema for {task['task_id']}")
    if not isinstance(packet["prompt"], str) or not packet["prompt"].strip():
        raise RuntimeFailure(f"packet prompt is empty for {task['task_id']}")
    try:
        schema_payload = _read_artifact_bytes(
            root,
            packet["output_schema_ref"],
            packet["output_schema_sha256"],
        )
        schema = _strict_json_loads(schema_payload)
    except (ProtocolError, UnicodeDecodeError, ValueError) as exc:
        raise RuntimeFailure(f"invalid output schema for {task['task_id']}: {exc}") from exc
    if not isinstance(schema, dict):
        raise RuntimeFailure(f"output schema must be an object for {task['task_id']}")
    _validate_output_schema_contract(schema)
    for input_ref in task["input_refs"]:
        try:
            _read_artifact_bytes(root, input_ref, task["input_sha256"][input_ref])
        except ProtocolError as exc:
            raise RuntimeFailure(f"input evidence drift for {task['task_id']}: {exc}") from exc
    return packet, schema


_SCHEMA_ANNOTATIONS = {"$schema", "$id", "title", "description", "default", "examples"}
_SCHEMA_KEYWORDS = {
    "type",
    "anyOf",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "enum",
    "const",
}
_SCHEMA_TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}
_MAX_SCHEMA_OBJECT_DEPTH = 10
_MAX_SCHEMA_PROPERTIES = 5_000


def _is_json_number(value: Any) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
    ) or (
        isinstance(value, float)
        and math.isfinite(value)
    )


def _value_matches_schema_type(value: Any, expected: str) -> bool:
    checks = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": _is_json_number,
        "boolean": lambda item: isinstance(item, bool),
        "null": lambda item: item is None,
    }
    return expected in checks and checks[expected](value)


def _schema_types(value: Any, path: str) -> tuple[str, ...]:
    if isinstance(value, str):
        types = (value,)
    elif (
        isinstance(value, list)
        and len(value) == 2
        and len(set(value)) == 2
        and all(isinstance(item, str) for item in value)
        and "null" in value
    ):
        types = tuple(value)
    else:
        raise RuntimeFailure(f"output schema node requires an explicit supported type at {path}")
    if any(item not in _SCHEMA_TYPES for item in types):
        raise RuntimeFailure(f"output schema node requires an explicit supported type at {path}")
    return types


def _validate_output_schema_contract(
    schema: dict[str, Any],
    path: str = "$",
    *,
    _root: bool = True,
    _object_depth: int = 0,
    _property_count: list[int] | None = None,
) -> None:
    """Validate the strict provider schema before any routed model launch."""

    if not isinstance(schema, dict):
        raise RuntimeFailure(f"output schema node must be an object at {path}")
    if _property_count is None:
        _property_count = [0]
    unknown = set(schema) - _SCHEMA_ANNOTATIONS - _SCHEMA_KEYWORDS
    if unknown:
        raise RuntimeFailure(f"unsupported output schema keyword at {path}: {sorted(unknown)[0]}")
    if "anyOf" in schema:
        if _root:
            raise RuntimeFailure("root output schema must be an object and cannot use anyOf")
        if set(schema) - _SCHEMA_ANNOTATIONS - {"anyOf"}:
            raise RuntimeFailure(f"anyOf schema cannot mix direct validation keywords at {path}")
        choices = schema["anyOf"]
        if not isinstance(choices, list) or not choices or not all(
            isinstance(item, dict) for item in choices
        ):
            raise RuntimeFailure(f"anyOf requires non-empty schema branches at {path}")
        for index, choice in enumerate(choices):
            _validate_output_schema_contract(
                choice,
                f"{path}.anyOf[{index}]",
                _root=False,
                _object_depth=_object_depth,
                _property_count=_property_count,
            )
        return

    types = _schema_types(schema.get("type"), path)
    if _root and types != ("object",):
        raise RuntimeFailure("root output schema must be an object and cannot use anyOf")
    if "const" in schema and not any(
        _value_matches_schema_type(schema["const"], expected) for expected in types
    ):
        raise RuntimeFailure(f"output schema const does not match its type at {path}")
    if "enum" in schema:
        enum = schema["enum"]
        if (
            not isinstance(enum, list)
            or not enum
            or any(
                not any(_value_matches_schema_type(item, expected) for expected in types)
                for item in enum
            )
        ):
            raise RuntimeFailure(f"output schema enum does not match its type at {path}")
    if "object" in types:
        object_depth = _object_depth + 1
        if object_depth > _MAX_SCHEMA_OBJECT_DEPTH:
            raise RuntimeFailure(f"output schema exceeds the maximum object nesting depth at {path}")
        properties = schema.get("properties")
        required = schema.get("required")
        if (
            not isinstance(properties, dict)
            or not all(isinstance(key, str) and isinstance(value, dict) for key, value in properties.items())
            or not isinstance(required, list)
            or len(required) != len(set(required))
            or set(required) != set(properties)
            or schema.get("additionalProperties") is not False
        ):
            raise RuntimeFailure(
                f"strict object schema requires all properties and additionalProperties=false at {path}"
            )
        _property_count[0] += len(properties)
        if _property_count[0] > _MAX_SCHEMA_PROPERTIES:
            raise RuntimeFailure("output schema exceeds the maximum property count")
        for key, value in properties.items():
            _validate_output_schema_contract(
                value,
                f"{path}.{key}",
                _root=False,
                _object_depth=object_depth,
                _property_count=_property_count,
            )
        if "items" in schema:
            raise RuntimeFailure(f"object schema cannot declare items at {path}")
    elif "array" in types:
        items = schema.get("items")
        if not isinstance(items, dict):
            raise RuntimeFailure(f"strict array schema requires items at {path}")
        _validate_output_schema_contract(
            items,
            f"{path}[]",
            _root=False,
            _object_depth=_object_depth,
            _property_count=_property_count,
        )
        if any(key in schema for key in ("properties", "required", "additionalProperties")):
            raise RuntimeFailure(f"array schema cannot declare object keywords at {path}")
    elif any(key in schema for key in ("properties", "required", "additionalProperties", "items")):
        raise RuntimeFailure(f"scalar schema contains container keywords at {path}")


def _validate_source_writer_schema(schema: dict[str, Any], task_id: str) -> None:
    properties = schema.get("properties", {})
    changed_paths = properties.get("changed_paths") if isinstance(properties, dict) else None
    if (
        not isinstance(changed_paths, dict)
        or changed_paths.get("type") != "array"
        or not isinstance(changed_paths.get("items"), dict)
        or changed_paths["items"].get("type") != "string"
        or "changed_paths" not in schema.get("required", [])
    ):
        raise RuntimeFailure(f"source writer output schema must require changed_paths for {task_id}")


def _json_equal(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _json_equal(a, b) for a, b in zip(left, right, strict=True)
        )
    if isinstance(left, dict):
        return set(left) == set(right) and all(
            _json_equal(left[key], right[key]) for key in left
        )
    return left == right


def _validate_typed_output(value: Any, schema: dict[str, Any], path: str = "$") -> None:
    """Validate the deliberately small, fail-closed v1 output-schema profile."""

    unknown = set(schema) - _SCHEMA_ANNOTATIONS - _SCHEMA_KEYWORDS
    if unknown:
        raise RuntimeFailure(f"unsupported output schema keyword at {path}: {sorted(unknown)[0]}")
    if "anyOf" in schema:
        choices = schema["anyOf"]
        failures: list[str] = []
        for choice in choices:
            try:
                _validate_typed_output(value, choice, path)
                return
            except RuntimeFailure as exc:
                failures.append(str(exc))
        raise RuntimeFailure(f"output does not satisfy anyOf at {path}: {failures[0]}")
    if "enum" in schema:
        enum = schema["enum"]
        if not isinstance(enum, list) or not any(_json_equal(value, item) for item in enum):
            raise RuntimeFailure(f"output does not satisfy enum at {path}")
    if "const" in schema and not _json_equal(value, schema["const"]):
        raise RuntimeFailure(f"output does not satisfy const at {path}")

    expected = schema.get("type")
    accepted_types: tuple[str, ...] = ()
    type_checks = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": _is_json_number,
        "boolean": lambda item: isinstance(item, bool),
        "null": lambda item: item is None,
    }
    if expected is not None:
        accepted_types = _schema_types(expected, path)
        if not any(type_checks[item](value) for item in accepted_types):
            raise RuntimeFailure(f"output type mismatch at {path}: expected {list(accepted_types)}")

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        additional = schema.get("additionalProperties", True)
        if not isinstance(properties, dict) or not all(
            isinstance(key, str) and isinstance(item, dict)
            for key, item in properties.items()
        ):
            raise RuntimeFailure(f"invalid properties schema at {path}")
        if not isinstance(required, list) or not all(isinstance(key, str) for key in required):
            raise RuntimeFailure(f"invalid required schema at {path}")
        missing = [key for key in required if key not in value]
        if missing:
            raise RuntimeFailure(f"output missing required property at {path}: {missing[0]}")
        for key, item in value.items():
            if key in properties:
                _validate_typed_output(item, properties[key], f"{path}.{key}")
            elif additional is False:
                raise RuntimeFailure(f"output has additional property at {path}: {key}")
            elif isinstance(additional, dict):
                _validate_typed_output(item, additional, f"{path}.{key}")
            elif additional is not True:
                raise RuntimeFailure(f"invalid additionalProperties schema at {path}")
    elif (
        any(key in schema for key in ("properties", "required", "additionalProperties"))
        and not (value is None and "null" in accepted_types)
    ):
        raise RuntimeFailure(f"object keywords require object output at {path}")

    if isinstance(value, list):
        items = schema.get("items")
        if items is not None:
            if not isinstance(items, dict):
                raise RuntimeFailure(f"invalid items schema at {path}")
            for index, item in enumerate(value):
                _validate_typed_output(item, items, f"{path}[{index}]")
    elif "items" in schema and not (value is None and "null" in accepted_types):
        raise RuntimeFailure(f"items requires array output at {path}")


def _host_capability_worker_root(
    root: Path,
    repository_root: Path | None,
    relevant_roots: tuple[str, ...],
    snapshot_evidence: dict[str, Any],
    *,
    require_live_state: bool,
) -> Path:
    """Revalidate and return the sealed read snapshot used by host probes."""

    if repository_root is None:
        raise RuntimeFailure("host capability admission requires a source repository")
    try:
        worker_root, manifest = replay_read_only_snapshot(
            root,
            repository_root,
            "000-host-capability-probe",
            relevant_roots,
            snapshot_evidence,
        )
        if require_live_state:
            current = collect_repository_state(repository_root)
            if current != manifest["repository_state"]:
                raise SourceWriteError("live repository state drifted from the host probe snapshot")
        return worker_root
    except (RepositoryStateError, SourceWriteError) as exc:
        raise RuntimeFailure(f"host capability snapshot failed closed: {exc}") from exc


def _validate_admission_inputs(
    root: Path,
    workflow: dict[str, Any],
    *,
    repository_root: Path | None = None,
    codex_binary: str | None = None,
    require_host_snapshot_live_state: bool,
) -> None:
    """Bind dispatch to the sealed baseline, capabilities, and running bundle."""

    routing = workflow["routing"]
    if routing != {
        "policy_version": "qualified-routing.v1",
        "top_model": "gpt-5.6-sol",
        "worker_model": "gpt-5.6-terra",
        "reasoning_effort": "xhigh",
    }:
        raise RuntimeFailure("Slice 1 requires the qualified Sol/Terra xhigh routing floor")
    running_bundle = _runtime_bundle_sha256()
    if workflow["runtime_bundle"]["sha256"] != running_bundle:
        raise RuntimeFailure("admitted runtime bundle does not match the running executable")
    pinned_root = Path(root) / "runtime-bundle"
    # Before workflow.json commits, a partial bundle is an admission crash seam
    # that _seal_runtime_bundle must repair. After commit it is active authority.
    workflow_committed = (Path(root) / "workflow.json").exists() or (Path(root) / "workflow.json").is_symlink()
    if workflow_committed and (pinned_root.exists() or pinned_root.is_symlink()):
        _resolve_pinned_runtime(root, workflow["runtime_bundle"]["sha256"])

    try:
        baseline_payload = _read_artifact_bytes(
            root,
            workflow["baseline_ref"],
            workflow["baseline_sha256"],
        )
        baseline = json.loads(baseline_payload)
        verify_baseline(baseline)
    except (ProtocolError, BaselineError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeFailure("workflow baseline is not a valid replayable baseline") from exc
    if repository_evidence(baseline) != workflow["admission"]["repository"]:
        raise RuntimeFailure("workflow repository evidence does not match the sealed baseline")
    if repository_root is not None:
        try:
            live_evidence = current_repository_evidence(repository_root, baseline)
        except BaselineError as exc:
            raise RuntimeFailure("live repository evidence cannot be reconstructed") from exc
        if workflow["admission"]["profile"] == "source_write":
            expected_repository = workflow["admission"]["repository"]
            if (
                live_evidence["head"] != expected_repository["head"]
                or live_evidence["branch"] != expected_repository["branch"]
            ):
                raise RuntimeFailure("live source repository HEAD or branch drifted from admission")
        elif live_evidence != workflow["admission"]["repository"]:
            raise RuntimeFailure("live repository evidence drifted from admission")
    if baseline["baseline_kind"] == "candidate_gate":
        parent_ref = baseline["candidate_parent"]
        try:
            repository_root = _repository_root_for(root)
            parent_payload = _read_artifact_bytes(
                repository_root,
                parent_ref["path"],
                parent_ref["sha256"],
            )
            parent = json.loads(parent_payload)
            verify_candidate_against_parent(baseline, parent)
        except (ProtocolError, BaselineError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeFailure("candidate baseline parent is invalid") from exc

    codex_path: Path | None = None
    codex_sha256: str | None = None
    codex_version: str | None = None
    if codex_binary is not None:
        codex_path, codex_sha256, codex_version = _codex_identity(codex_binary)

    evidence_cache: dict[tuple[str, str], dict[str, Any]] = {}
    validated_receipts: set[tuple[str, str]] = set()
    for name, capability in workflow["admission"]["capabilities"].items():
        if workflow["admission"]["profile"] == "source_write" and name == "sandbox_isolation":
            _validate_source_write_capability(
                root,
                capability,
                running_bundle=running_bundle,
                codex_sha256=codex_sha256,
                codex_version=codex_version,
                codex_binary=codex_path,
            )
            continue
        cache_key = (capability["evidence_ref"], capability["evidence_sha256"])
        if cache_key not in evidence_cache:
            try:
                payload = _read_artifact_bytes(root, *cache_key)
                evidence = json.loads(payload)
            except (ProtocolError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeFailure(f"capability evidence is invalid for {name}") from exc
            if not isinstance(evidence, dict) or set(evidence) != {
                "schema_version",
                "observed_at",
                "codex_cli_version",
                "codex_binary_sha256",
                "capabilities",
            } or evidence.get("schema_version") not in {
                "agent-workflow.slice0b-capability-summary.v2",
                "agent-workflow.host-capability-summary.v1",
            }:
                raise RuntimeFailure(f"capability evidence uses an unsupported schema for {name}")
            if codex_sha256 is not None and evidence["codex_binary_sha256"] != codex_sha256:
                raise RuntimeFailure("capability evidence Codex binary digest drifted")
            if codex_version is not None and evidence["codex_cli_version"] != codex_version:
                raise RuntimeFailure("capability evidence Codex version drifted")
            evidence_cache[cache_key] = evidence
        summary = evidence_cache[cache_key]
        summaries = summary["capabilities"]
        if not isinstance(summaries, dict) or set(summaries) != set(
            workflow["admission"]["capabilities"]
        ):
            raise RuntimeFailure("capability summary names drifted")
        observed = summaries.get(name)
        declared = capability["status"]
        if not isinstance(observed, dict) or set(observed) != {
            "status",
            "evidence_ref",
            "evidence_sha256",
        } or observed["status"] != declared:
            raise RuntimeFailure(f"capability evidence contradicts declared status for {name}")
        try:
            receipt_payload = _read_artifact_bytes(
                root,
                observed["evidence_ref"],
                observed["evidence_sha256"],
            )
            receipt = json.loads(receipt_payload)
        except (ProtocolError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeFailure(f"capability source receipt is invalid for {name}") from exc
        if not isinstance(receipt, dict) or receipt.get("schema_version") not in {
            "agent-workflow.capability-receipt.vnext.v3",
            "agent-workflow.host-capability-receipt.v1",
        }:
            raise RuntimeFailure(f"capability source receipt uses an unsupported contract for {name}")
        statuses = receipt.get("capabilities")
        if not isinstance(statuses, dict) or statuses.get(name) != declared:
            raise RuntimeFailure(f"capability source receipt contradicts {name}")
        receipt_key = (observed["evidence_ref"], observed["evidence_sha256"])
        if receipt.get("schema_version") == "agent-workflow.host-capability-receipt.v1":
            if receipt_key not in validated_receipts:
                _validate_host_capability_provenance(
                    root,
                    receipt,
                    running_bundle=running_bundle,
                    codex_sha256=codex_sha256,
                    codex_version=codex_version,
                    worker_root=_host_capability_worker_root(
                        root,
                        repository_root,
                        tuple(workflow["admission"]["relevant_roots"]),
                        receipt.get("snapshot_manifest"),
                        require_live_state=require_host_snapshot_live_state,
                    ),
                )
                validated_receipts.add(receipt_key)
            continue
        adapter = receipt.get("blocking_transport")
        expected_adapter = {
            "host": "codex-desktop",
            "outer_tool": "functions.exec",
            "inner_tool": "exec_command",
            "outer_yield_ms": 30000,
            "inner_yield_ms": 30000,
            "maximum_blocking_window_ms": 30000,
            "early_exit_observed": True,
            "sparse_continuation_limit": 0,
        }
        if adapter != expected_adapter:
            raise RuntimeFailure("blocking transport probe contract is incomplete")
        proofs = receipt.get("proofs")
        expected_proofs = {
            "blocking_wait": {
                "terminal_json_observed": True,
                "cell_handle_rejected": True,
            },
            "read_only_containment": {
                "exact_profile_attested": True,
                "credential_denial_attested": True,
                "network_restricted": True,
            },
            "route_attestation": {
                "persisted_turn_context": True,
                "top_model": "gpt-5.6-sol",
                "worker_model": "gpt-5.6-terra",
                "reasoning_effort": "xhigh",
            },
            "sandbox_isolation": {
                "reason": "host_capability_unavailable",
            },
            "cancel_reap": {
                "owned_pgid_signal": True,
                "live_marker_required": True,
                "post_publish_cancel_fence": True,
            },
            "raw_session_audit": {
                "thread_id_bound": True,
                "terminal_event_required": True,
            },
            "accounting_evidence": {
                "terminal_usage_required": True,
                "confidence": "exact",
            },
            "generation_fence": {
                "create_once": True,
                "authority_revision_bound": True,
                "predecessor_bound": True,
                "plan_digest_bound": True,
            },
        }
        if proofs != expected_proofs:
            raise RuntimeFailure("capability-specific probe contract is incomplete")
        if receipt_key not in validated_receipts:
            _validate_capability_provenance(
                root,
                receipt,
                running_bundle=running_bundle,
                codex_sha256=codex_sha256,
                codex_version=codex_version,
                worker_root=(
                    (repository_root or Path(root).resolve())
                    / workflow["admission"]["relevant_roots"][0]
                ).resolve(strict=True),
            )
            validated_receipts.add(receipt_key)


def _validate_source_write_capability(
    root: Path,
    capability: dict[str, Any],
    *,
    running_bundle: str,
    codex_sha256: str | None,
    codex_version: str | None,
    codex_binary: Path | None = None,
) -> None:
    """Validate one actual named-profile writer probe, not a model assertion."""

    if capability.get("status") != "pass":
        raise RuntimeFailure("source_write requires a passing sandbox capability")
    try:
        payload = _read_artifact_bytes(
            root,
            capability["evidence_ref"],
            capability["evidence_sha256"],
        )
        evidence = json.loads(payload)
    except (ProtocolError, UnicodeDecodeError, json.JSONDecodeError, KeyError) as exc:
        raise RuntimeFailure("source-write capability evidence is invalid") from exc
    if not isinstance(evidence, dict) or set(evidence) != {
        "schema_version",
        "observed_at",
        "producer",
        "workspace",
        "session",
        "supervisor",
        "deterministic_probe",
        "environment",
    } or evidence.get("schema_version") != "agent-workflow.source-write-capability.vnext.v1":
        raise RuntimeFailure("source-write capability contract is unsupported")
    try:
        observed_at = datetime.fromisoformat(evidence["observed_at"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise RuntimeFailure("source-write capability observation time is invalid") from exc
    age = _now() - observed_at.astimezone(timezone.utc)
    if age.total_seconds() < -300 or age > timedelta(hours=24):
        raise RuntimeFailure("source-write capability is stale or from the future")
    producer = evidence["producer"]
    if not isinstance(producer, dict) or producer != {
        "name": "agent-workflow-slice3-writer-probe",
        "runtime_bundle_sha256": running_bundle,
        "codex_cli_version": codex_version or producer.get("codex_cli_version"),
        "codex_binary_sha256": codex_sha256 or producer.get("codex_binary_sha256"),
    }:
        raise RuntimeFailure("source-write capability producer drifted")
    workspace = evidence["workspace"]
    if not isinstance(workspace, dict) or set(workspace) != {
        "root",
        "codex_home",
        "write_roots",
        "profile_ref",
        "profile_sha256",
        "turn_context_ref",
        "turn_context_sha256",
    }:
        raise RuntimeFailure("source-write capability workspace is invalid")
    workspace_root = Path(workspace["root"])
    source_codex_home = Path(workspace["codex_home"])
    if (
        not workspace_root.is_absolute()
        or not source_codex_home.is_absolute()
        or not workspace_root.is_dir()
        or not source_codex_home.is_dir()
    ):
        raise RuntimeFailure("source-write capability roots are unavailable")
    write_roots = tuple(workspace["write_roots"]) if isinstance(workspace["write_roots"], list) else ()
    expected_profile = writer_profile_bytes(write_roots)
    try:
        profile_payload = _read_artifact_bytes(
            root,
            workspace["profile_ref"],
            workspace["profile_sha256"],
        )
        context_payload = _read_artifact_bytes(
            root,
            workspace["turn_context_ref"],
            workspace["turn_context_sha256"],
        )
        context = json.loads(context_payload)
    except (ProtocolError, UnicodeDecodeError, json.JSONDecodeError, KeyError) as exc:
        raise RuntimeFailure("source-write profile or turn context evidence is invalid") from exc
    if profile_payload != expected_profile:
        raise RuntimeFailure("source-write named profile bytes drifted")
    session = evidence["session"]
    if not isinstance(session, dict) or set(session) != {"id", "model", "reasoning_effort"}:
        raise RuntimeFailure("source-write session evidence is invalid")
    if (
        session["model"] != "gpt-5.6-terra"
        or session["reasoning_effort"] != "xhigh"
        or context.get("session_id") != session["id"]
        or context.get("model") != session["model"]
        or context.get("effort") != session["reasoning_effort"]
        or attest_writer_permissions(
            context,
            workspace_root,
            source_codex_home,
            write_roots,
            codex_binary,
        )
        is not None
    ):
        raise RuntimeFailure("source-write effective permission attestation failed")
    supervisor = evidence["supervisor"]
    if not isinstance(supervisor, dict) or set(supervisor) != {
        "request_ref",
        "request_sha256",
        "terminal_ref",
        "terminal_sha256",
        "events_ref",
        "events_sha256",
        "stderr_ref",
        "stderr_sha256",
    }:
        raise RuntimeFailure("source-write supervisor evidence is invalid")
    try:
        request_payload = _read_artifact_bytes(
            root, supervisor["request_ref"], supervisor["request_sha256"]
        )
        terminal_payload = _read_artifact_bytes(
            root, supervisor["terminal_ref"], supervisor["terminal_sha256"]
        )
        events_payload = _read_artifact_bytes(
            root, supervisor["events_ref"], supervisor["events_sha256"]
        )
        stderr_payload = _read_artifact_bytes(
            root, supervisor["stderr_ref"], supervisor["stderr_sha256"]
        )
        request = json.loads(request_payload)
        terminal = json.loads(terminal_payload)
        validate_supervisor_receipt(terminal)
    except (
        ProtocolError,
        SupervisorFailure,
        UnicodeDecodeError,
        json.JSONDecodeError,
        KeyError,
    ) as exc:
        raise RuntimeFailure("source-write supervisor evidence is unreadable") from exc
    command = request.get("command") if isinstance(request, dict) else None
    if (
        not isinstance(command, list)
        or "-p" not in command
        or command[command.index("-p") + 1] != "vnext-writer"
        or request.get("cwd") != os.fspath(workspace_root)
        or request.get("environment", {}).get("CODEX_HOME") != os.fspath(source_codex_home)
        or terminal.get("status") != "completed"
        or terminal.get("request_sha256") != supervisor["request_sha256"]
        or terminal.get("stdout_sha256") != supervisor["events_sha256"]
        or terminal.get("stderr_sha256") != supervisor["stderr_sha256"]
        or not events_payload
        or stderr_payload is None
    ):
        raise RuntimeFailure("source-write supervisor command or terminal evidence is insufficient")
    messages = [
        event.get("item", {}).get("text")
        for event in _parse_jsonl(events_payload)
        if event.get("type") == "item.completed"
        and isinstance(event.get("item"), dict)
        and event["item"].get("type") == "agent_message"
        and isinstance(event["item"].get("text"), str)
    ]
    try:
        live_output = json.loads(messages[-1]) if messages else None
    except json.JSONDecodeError:
        live_output = None
    if (
        live_output != {"answer": "ok"}
    ):
        raise RuntimeFailure("source-write live route evidence is insufficient")
    probe = evidence["deterministic_probe"]
    if not isinstance(probe, dict) or set(probe) != {"evidence_ref", "evidence_sha256"}:
        raise RuntimeFailure("source-write deterministic probe reference is invalid")
    try:
        report = json.loads(_read_artifact_bytes(root, probe["evidence_ref"], probe["evidence_sha256"]))
    except (ProtocolError, UnicodeDecodeError, json.JSONDecodeError, KeyError) as exc:
        raise RuntimeFailure("source-write deterministic probe is invalid") from exc
    if not isinstance(report, dict) or set(report) != {
        "schema_version",
        "profile_sha256",
        "workspace_root",
        "command",
        "command_sha256",
        "stdout_ref",
        "stdout_sha256",
        "stderr_ref",
        "stderr_sha256",
        "allowed_write_exit",
        "git_write_exit",
        "sibling_write_exit",
        "control_read_exit",
        "credential_read_exit",
        "network_exit",
    }:
        raise RuntimeFailure("source-write deterministic probe contract is incomplete")
    try:
        sandbox_stdout = _read_artifact_bytes(
            root, report["stdout_ref"], report["stdout_sha256"]
        )
        _read_artifact_bytes(root, report["stderr_ref"], report["stderr_sha256"])
    except (ProtocolError, KeyError) as exc:
        raise RuntimeFailure("source-write deterministic raw logs are invalid") from exc
    command = report["command"]
    denied = (
        report["git_write_exit"],
        report["sibling_write_exit"],
        report["control_read_exit"],
        report["credential_read_exit"],
        report["network_exit"],
    )
    if (
        report["schema_version"] != "agent-workflow.source-write-denial-probe.vnext.v1"
        or report["profile_sha256"] != workspace["profile_sha256"]
        or report["workspace_root"] != os.fspath(workspace_root)
        or not isinstance(command, list)
        or report["command_sha256"] != _digest(_canonical(command))
        or not any(
            command[index : index + 2] == ["-P", "vnext_writer"]
            for index in range(max(0, len(command) - 1))
        )
        or not sandbox_stdout
        or report["allowed_write_exit"] != 0
        or any(not isinstance(code, int) or isinstance(code, bool) or code == 0 for code in denied)
    ):
        raise RuntimeFailure("source-write deterministic denial evidence is insufficient")
    environment = evidence["environment"]
    if environment != {
        "inherit": [
            "AGENT_WORKFLOW_AUDIT_MARKER",
            "CODEX_HOME",
            "HOME",
            "LANG",
            "PATH",
            "TMPDIR",
        ],
        "plugins_disabled": True,
        "mcp_disabled": True,
        "network_enabled": False,
    }:
        raise RuntimeFailure("source-write sanitized environment evidence is incomplete")


def _host_probe_command(
    *,
    codex_binary: str,
    model: str,
    audit_marker: str,
    output_schema: Path,
    worker_root: Path,
) -> list[str]:
    """Return the one source-owned argv accepted for host capability probes."""

    return [
        codex_binary,
        "exec",
        "--ignore-rules",
        "--disable",
        "plugins",
        "--json",
        "-m",
        model,
        "-c",
        'model_reasoning_effort="xhigh"',
        "-c",
        'shell_environment_policy.inherit="core"',
        "-c",
        'cli_auth_credentials_store="file"',
        "-c",
        f"developer_instructions={json.dumps(_ISOLATED_WORKER_DEVELOPER_INSTRUCTIONS)}",
        "-c",
        f'agent_workflow_audit_marker="{audit_marker}"',
        "--output-schema",
        os.fspath(output_schema),
        "-p",
        "vnext-read-only",
        "-C",
        os.fspath(worker_root),
        "Return exactly the schema-compliant JSON answer probe-ok. Use no tools.",
    ]


def _validate_host_capability_provenance(
    root: Path,
    receipt: dict[str, Any],
    *,
    running_bundle: str,
    codex_sha256: str | None,
    codex_version: str | None,
    worker_root: Path,
) -> None:
    """Validate a fresh, source-owned host probe without promotion-only callbacks."""

    expected_keys = {
        "schema_version",
        "observed_at",
        "producer",
        "relevant_root",
        "snapshot_manifest",
        "execution",
        "sessions",
        "deterministic_denials",
        "focused_tests",
        "capabilities",
    }
    if set(receipt) != expected_keys:
        raise RuntimeFailure("host capability receipt contract is incomplete")
    producer = receipt["producer"]
    if not isinstance(producer, dict) or set(producer) != {
        "name",
        "runtime_bundle_sha256",
        "codex_cli_version",
        "codex_binary_sha256",
    }:
        raise RuntimeFailure("host capability producer is invalid")
    if (
        producer["name"] != "agent-workflow-host-capability-probe"
        or producer["runtime_bundle_sha256"] != running_bundle
        or (codex_sha256 is not None and producer["codex_binary_sha256"] != codex_sha256)
        or (codex_version is not None and producer["codex_cli_version"] != codex_version)
    ):
        raise RuntimeFailure("host capability producer identity drifted")
    try:
        observed_at = datetime.fromisoformat(receipt["observed_at"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise RuntimeFailure("host capability observation time is invalid") from exc
    age = _now() - observed_at.astimezone(timezone.utc)
    if age.total_seconds() < -300 or age > timedelta(hours=24):
        raise RuntimeFailure("host capability evidence is stale or from the future")
    expected_worker_root = os.fspath(worker_root.resolve(strict=True))
    if not isinstance(receipt["relevant_root"], str) or receipt["relevant_root"] != expected_worker_root:
        raise RuntimeFailure("host capability relevant root drifted")
    snapshot_evidence = receipt["snapshot_manifest"]
    if not isinstance(snapshot_evidence, dict) or set(snapshot_evidence) != {
        "evidence_ref",
        "evidence_sha256",
    }:
        raise RuntimeFailure("host capability snapshot manifest evidence is invalid")
    expected_snapshot_ref = "runtime/read-snapshots/000-host-capability-probe/manifest.json"
    if snapshot_evidence["evidence_ref"] != expected_snapshot_ref:
        raise RuntimeFailure("host capability snapshot manifest reference drifted")
    try:
        snapshot_payload = _read_artifact_bytes(
            root,
            snapshot_evidence["evidence_ref"],
            snapshot_evidence["evidence_sha256"],
        )
        snapshot_manifest = json.loads(snapshot_payload)
    except (ProtocolError, UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise RuntimeFailure("host capability snapshot manifest is invalid") from exc
    expected_checkout_ref = "runtime/read-snapshots/000-host-capability-probe/checkout"
    try:
        observed_checkout_root = os.fspath(
            (Path(root) / expected_checkout_ref).resolve(strict=True)
        )
    except OSError as exc:
        raise RuntimeFailure("host capability snapshot checkout is missing or unsafe") from exc
    if (
        not isinstance(snapshot_manifest, dict)
        or snapshot_manifest.get("schema_version") != "agent-workflow.read-snapshot.vnext.v1"
        or snapshot_manifest.get("phase_id") != "000-host-capability-probe"
        or snapshot_manifest.get("checkout_ref") != expected_checkout_ref
        or observed_checkout_root != expected_worker_root
    ):
        raise RuntimeFailure("host capability snapshot manifest authority drifted")

    execution = receipt["execution"]
    if not isinstance(execution, dict) or set(execution) != {
        "started_at",
        "finished_at",
        "role_count",
        "terminal_count",
    }:
        raise RuntimeFailure("host capability execution evidence is invalid")
    try:
        started_at = datetime.fromisoformat(execution["started_at"].replace("Z", "+00:00"))
        finished_at = datetime.fromisoformat(execution["finished_at"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise RuntimeFailure("host capability execution time is invalid") from exc
    if (
        finished_at < started_at
        or observed_at < finished_at
        or execution["role_count"] != 2
        or execution["terminal_count"] != 2
    ):
        raise RuntimeFailure("host capability blocking barrier evidence is insufficient")

    sessions = receipt["sessions"]
    if not isinstance(sessions, dict) or set(sessions) != {"top", "worker"}:
        raise RuntimeFailure("host capability routed session set drifted")
    expected_models = {"top": "gpt-5.6-sol", "worker": "gpt-5.6-terra"}
    observed_ids: set[str] = set()
    for role, expected_model in expected_models.items():
        session = sessions[role]
        if not isinstance(session, dict) or set(session) != {
            "session_id",
            "model",
            "reasoning_effort",
            "codex_home",
            "events_ref",
            "events_sha256",
            "turn_context_ref",
            "turn_context_sha256",
            "output_ref",
            "output_sha256",
            "supervisor_request_ref",
            "supervisor_request_sha256",
            "supervisor_terminal_ref",
            "supervisor_terminal_sha256",
            "rollout_path",
            "rollout_bytes",
            "rollout_sha256",
        }:
            raise RuntimeFailure("host capability routed session contract is invalid")
        if (
            session["model"] != expected_model
            or session["reasoning_effort"] != "xhigh"
            or not isinstance(session["session_id"], str)
            or not session["session_id"]
            or session["session_id"] in observed_ids
        ):
            raise RuntimeFailure("host capability route attestation drifted")
        observed_ids.add(session["session_id"])
        codex_home = Path(session["codex_home"]).resolve(strict=True)
        if not codex_home.is_relative_to(Path(root).resolve()):
            raise RuntimeFailure("host capability Codex home escaped its workflow root")
        try:
            request, request_payload = load_supervisor_request(
                root, session["supervisor_request_ref"], enforce_boot=False
            )
            terminal_payload = _read_artifact_bytes(
                root,
                session["supervisor_terminal_ref"],
                session["supervisor_terminal_sha256"],
            )
            terminal = validate_supervisor_receipt(json.loads(terminal_payload))
        except (ProtocolError, SupervisorFailure, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeFailure("host capability supervisor evidence is invalid") from exc
        command = request.get("command")
        environment = request.get("environment")
        expected_command = _host_probe_command(
            codex_binary=request.get("codex_binary"),
            model=expected_model,
            audit_marker=request.get("audit_marker"),
            output_schema=(
                root / "evidence/host-capability-probe/output-schema.json"
            ).resolve(strict=True),
            worker_root=worker_root,
        )
        if (
            _digest(request_payload) != session["supervisor_request_sha256"]
            or terminal.get("request_ref") != session["supervisor_request_ref"]
            or terminal.get("request_sha256") != session["supervisor_request_sha256"]
            or terminal.get("status") != "completed"
            or terminal.get("exit_code") != 0
            or terminal.get("group_reaped") is not True
            or terminal.get("group_gone_observed") is not True
        ):
            raise RuntimeFailure("host capability supervisor terminal evidence is insufficient")
        if command != expected_command:
            raise RuntimeFailure("host capability supervisor command is not exact")
        if request.get("command_sha256") != _digest(_canonical(expected_command)):
            raise RuntimeFailure("host capability supervisor command digest drifted")
        expected_environment_keys = {
            "AGENT_WORKFLOW_AUDIT_MARKER",
            "CODEX_HOME",
            "HOME",
            "LANG",
            "PATH",
            "TMPDIR",
        }
        if (
            not isinstance(environment, dict)
            or set(environment) != expected_environment_keys
            or Path(environment["CODEX_HOME"]).resolve() != codex_home
            or Path(environment["HOME"]).resolve() != (codex_home / "home").resolve()
            or Path(environment["TMPDIR"]).resolve() != (codex_home / "tmp").resolve()
            or environment["AGENT_WORKFLOW_AUDIT_MARKER"] != request.get("audit_marker")
            or request.get("work_mode") != "read"
            or request.get("write_roots") != []
            or request.get("runtime_bundle_sha256") != running_bundle
            or request.get("codex_binary_sha256") != producer["codex_binary_sha256"]
            or terminal.get("stdout_ref") != request.get("stdout_ref")
            or terminal.get("stderr_ref") != request.get("stderr_ref")
        ):
            raise RuntimeFailure("host capability supervisor environment evidence is insufficient")
        rollout_path = Path(session["rollout_path"]).resolve(strict=True)
        canonical_rollout = _find_session_rollout(codex_home, session["session_id"])
        rollout_size = session["rollout_bytes"]
        if (
            canonical_rollout is None
            or rollout_path != canonical_rollout
            or not isinstance(rollout_size, int)
            or isinstance(rollout_size, bool)
            or rollout_size <= 0
            or rollout_size > 4 * 1024 * 1024
            or rollout_path.stat().st_size != rollout_size
            or _digest_file(rollout_path) != session["rollout_sha256"]
        ):
            raise RuntimeFailure("host capability canonical rollout evidence drifted")
        try:
            events_payload = _read_artifact_bytes(
                root, session["events_ref"], session["events_sha256"]
            )
            terminal_events = _read_artifact_bytes(
                root, terminal["stdout_ref"], terminal["stdout_sha256"]
            )
            context = json.loads(
                _read_artifact_bytes(
                    root,
                    session["turn_context_ref"],
                    session["turn_context_sha256"],
                )
            )
            output = json.loads(
                _read_artifact_bytes(root, session["output_ref"], session["output_sha256"])
            )
        except (ProtocolError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeFailure("host capability routed evidence is invalid") from exc
        canonical_context = _project_turn_context_from_rollout(
            rollout_path, session["session_id"]
        )
        events = _parse_jsonl(events_payload)
        started = [item for item in events if item.get("type") == "thread.started"]
        completed = [item for item in events if item.get("type") == "turn.completed"]
        failed = [item for item in events if item.get("type") in {"turn.failed", "runtime.error"}]
        usage = completed[0].get("usage") if len(completed) == 1 else None
        if (
            len(started) != 1
            or events_payload != terminal_events
            or terminal["stdout_sha256"] != session["events_sha256"]
            or started[0].get("thread_id") != session["session_id"]
            or len(completed) != 1
            or failed
            or not isinstance(usage, dict)
            or not all(
                isinstance(usage.get(key), int) and usage[key] >= 0
                for key in ("input_tokens", "output_tokens")
            )
            or output != {"answer": "probe-ok"}
            or canonical_context != context
            or context.get("session_id") != session["session_id"]
            or context.get("model") != expected_model
            or context.get("effort", context.get("reasoning_effort")) != "xhigh"
            or _attest_worker_permissions(
                context,
                worker_root,
                codex_home,
                Path(request["codex_binary"]),
            )
            is not None
        ):
            raise RuntimeFailure("host capability routed evidence is insufficient")

    denial = receipt["deterministic_denials"]
    tests = receipt["focused_tests"]
    for value, label in ((denial, "denial"), (tests, "focused test")):
        if not isinstance(value, dict) or set(value) != {"evidence_ref", "evidence_sha256"}:
            raise RuntimeFailure(f"host capability {label} reference is invalid")
    try:
        denial_report = json.loads(
            _read_artifact_bytes(root, denial["evidence_ref"], denial["evidence_sha256"])
        )
        test_report = _read_artifact_bytes(
            root, tests["evidence_ref"], tests["evidence_sha256"]
        ).decode("utf-8", errors="strict").replace("\r\n", "\n")
    except (ProtocolError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeFailure("host capability deterministic evidence is invalid") from exc
    expected_denials = {
        "workspace_read_exit",
        "workspace_write_exit",
        "sibling_read_exit",
        "control_read_exit",
        "credential_read_exit",
        "network_exit",
    }
    if (
        not isinstance(denial_report, dict)
        or set(denial_report) != expected_denials
        or denial_report["workspace_read_exit"] != 0
        or any(denial_report[name] == 0 for name in expected_denials - {"workspace_read_exit"})
    ):
        raise RuntimeFailure("host capability read-only denials are insufficient")
    required_tests = {
        "test_cancel_rejects_record_without_live_unforgeable_marker",
        "test_cancel_signals_active_group_and_terminalizes_queued_task",
        "test_generation_claim_uses_one_predecessor_authority_contention_key",
        "test_log_drainer_caps_event_object_count_even_within_durable_limit",
        "test_runner_sigkill_reconcile_materializes_task_and_phase_receipt",
        "test_terminal_fence_runs_after_workers_and_before_results_or_receipt",
    }
    if (
        not required_tests.issubset(set(re.findall(r"test_[a-z0-9_]+", test_report)))
        or "\nOK\n" not in test_report
    ):
        raise RuntimeFailure("host capability focused tests are insufficient")

    statuses = receipt["capabilities"]
    expected_statuses = {
        name: "unavailable" if name == "sandbox_isolation" else "pass"
        for name in CAPABILITY_NAMES
    }
    if statuses != expected_statuses:
        raise RuntimeFailure("host capability status set drifted")


def _validate_capability_provenance(
    root: Path,
    receipt: dict[str, Any],
    *,
    running_bundle: str,
    codex_sha256: str | None,
    codex_version: str | None,
    worker_root: Path,
) -> None:
    provenance = receipt.get("provenance")
    if not isinstance(provenance, dict) or set(provenance) != {
        "producer",
        "observed_at",
        "source_run",
        "source_codex_identity",
        "proof_artifacts",
    }:
        raise RuntimeFailure("capability provenance contract is incomplete")
    producer = provenance["producer"]
    if not isinstance(producer, dict) or set(producer) != {
        "name",
        "validator_runtime_bundle_sha256",
        "source_runtime_bundle_sha256",
        "codex_cli_version",
        "codex_binary_sha256",
    }:
        raise RuntimeFailure("capability provenance producer is invalid")
    if (
        producer["name"] != "agent-workflow-slice1-probe"
        or producer["validator_runtime_bundle_sha256"] != running_bundle
        or (codex_sha256 is not None and producer["codex_binary_sha256"] != codex_sha256)
        or (codex_version is not None and producer["codex_cli_version"] != codex_version)
    ):
        raise RuntimeFailure("capability provenance producer identity drifted")
    try:
        observed_at = datetime.fromisoformat(provenance["observed_at"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise RuntimeFailure("capability provenance observation time is invalid") from exc
    age = _now() - observed_at.astimezone(timezone.utc)
    if age.total_seconds() < -300 or age > timedelta(hours=24):
        raise RuntimeFailure("capability provenance is stale or from the future")
    source_run = provenance["source_run"]
    if not isinstance(source_run, dict) or set(source_run) != {
        "workflow_id",
        "orchestrator_session_id",
        "main_session_id",
        "codex_home",
        "worker_session_ids",
        "top_session_ids",
    }:
        raise RuntimeFailure("capability provenance source run is invalid")
    if not all(
        isinstance(source_run[field], str) and source_run[field]
        for field in ("workflow_id", "orchestrator_session_id", "main_session_id", "codex_home")
    ) or not all(
        isinstance(source_run[field], list)
        and source_run[field]
        and all(isinstance(item, str) and item for item in source_run[field])
        for field in ("worker_session_ids", "top_session_ids")
    ):
        raise RuntimeFailure("capability provenance source run values are invalid")
    source_codex_identity = provenance["source_codex_identity"]
    if not isinstance(source_codex_identity, dict) or set(source_codex_identity) != {
        "evidence_ref",
        "evidence_sha256",
    }:
        raise RuntimeFailure("capability provenance source Codex identity is invalid")
    try:
        source_codex = json.loads(
            _read_artifact_bytes(
                root,
                source_codex_identity["evidence_ref"],
                source_codex_identity["evidence_sha256"],
            )
        )
    except (ProtocolError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeFailure("capability provenance source Codex evidence is invalid") from exc
    if (
        source_codex.get("schema_version")
        != "agent-workflow.slice0b-capability-summary.v2"
        or source_codex.get("codex_cli_version") != producer["codex_cli_version"]
        or source_codex.get("codex_binary_sha256") != producer["codex_binary_sha256"]
    ):
        raise RuntimeFailure("capability provenance source Codex identity drifted")
    proof_artifacts = provenance["proof_artifacts"]
    expected_kinds = {
        "blocking_wait": {"runner_evidence"},
        "read_only_containment": {"runner_evidence", "turn_context", "typed_output"},
        "route_attestation": {"runner_evidence", "turn_context"},
        "sandbox_isolation": {"runner_evidence"},
        "cancel_reap": {"focused_test_report"},
        "raw_session_audit": {"runner_evidence", "main_delivery_audit"},
        "accounting_evidence": {"runner_evidence"},
        "generation_fence": {"runner_evidence", "focused_test_report"},
    }
    if not isinstance(proof_artifacts, dict) or set(proof_artifacts) != set(expected_kinds):
        raise RuntimeFailure("capability provenance proof names drifted")
    artifact_cache: dict[tuple[str, str], tuple[str, bytes]] = {}
    observed_kinds: dict[str, set[str]] = {}
    for capability, entries in proof_artifacts.items():
        if not isinstance(entries, list) or not entries:
            raise RuntimeFailure(f"capability provenance is empty for {capability}")
        kinds: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {
                "kind",
                "evidence_ref",
                "evidence_sha256",
            }:
                raise RuntimeFailure("capability provenance artifact entry is invalid")
            key = (entry["evidence_ref"], entry["evidence_sha256"])
            if key not in artifact_cache:
                artifact_cache[key] = (
                    entry["kind"],
                    _read_artifact_bytes(root, *key),
                )
            elif artifact_cache[key][0] != entry["kind"]:
                raise RuntimeFailure("capability provenance artifact kind drifted")
            kinds.add(entry["kind"])
        observed_kinds[capability] = kinds
    if observed_kinds != expected_kinds:
        raise RuntimeFailure("capability provenance artifact coverage is incomplete")

    artifacts_by_kind: dict[str, list[bytes]] = {}
    for kind, payload in artifact_cache.values():
        artifacts_by_kind.setdefault(kind, []).append(payload)
    try:
        runner = json.loads(artifacts_by_kind["runner_evidence"][0])
        main_audit = json.loads(artifacts_by_kind["main_delivery_audit"][0])
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, IndexError) as exc:
        raise RuntimeFailure("capability provenance JSON artifact is invalid") from exc
    if (
        runner.get("schema_version") != "agent-workflow.runner-evidence.vnext.slice1.v2"
        or runner.get("execution_scope", {}).get("target_eligible") is not True
        or runner.get("orchestrator_session", {}).get("session_id")
        != source_run["orchestrator_session_id"]
        or runner.get("runtime_bundle_sha256") != producer["source_runtime_bundle_sha256"]
        or runner.get("completion_density", {}).get("actual_orchestrator_completions") != 2
        or runner.get("completion_density", {}).get("forbidden_polling_or_wrapper_wakes") != 0
        or runner.get("security", {}).get("all_permission_profiles_exact") is not True
        or runner.get("security", {}).get("all_routes_attested") is not True
        or runner.get("p1_repairs", {}).get("repository_runtime_and_codex_terminal_fence_passed")
        is not True
    ):
        raise RuntimeFailure("capability provenance runner evidence is insufficient")
    source_started_at = runner.get("parallelism", {}).get("all_tasks_started_at")
    try:
        source_observed = datetime.fromisoformat(source_started_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise RuntimeFailure("capability provenance source run time is invalid") from exc
    source_age = _now() - source_observed.astimezone(timezone.utc)
    if source_age.total_seconds() < -300 or source_age > timedelta(hours=24):
        raise RuntimeFailure("capability provenance source run is stale or from the future")
    if observed_at < source_observed:
        raise RuntimeFailure("capability provenance predates its source run")
    if (
        main_audit.get("schema_version")
        != "agent-workflow.main-delivery-audit.vnext.slice1.v1"
        or main_audit.get("main_session", {}).get("session_id") != source_run["main_session_id"]
        or main_audit.get("child_session", {}).get("session_id")
        != source_run["orchestrator_session_id"]
        or main_audit.get("workflow_id") != source_run["workflow_id"]
        or main_audit.get("delivery", {}).get("matching_child_terminal_callbacks_received") != 1
    ):
        raise RuntimeFailure("capability provenance Main delivery evidence is insufficient")
    observed_sessions: dict[str, set[str]] = {"worker": set(), "top": set()}
    source_codex_home = Path(source_run["codex_home"]).resolve()
    for payload in artifacts_by_kind.get("turn_context", []):
        try:
            context = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeFailure("capability provenance turn context is invalid") from exc
        model = context.get("model")
        session_id = context.get("session_id")
        role = "worker" if model == "gpt-5.6-terra" else "top" if model == "gpt-5.6-sol" else None
        if (
            role is None
            or context.get("effort") != "xhigh"
            or not isinstance(session_id, str)
            or _attest_worker_permissions(context, worker_root, source_codex_home) is not None
        ):
            raise RuntimeFailure("capability provenance turn context is insufficient")
        observed_sessions[role].add(session_id)
    if (
        observed_sessions["worker"] != set(source_run["worker_session_ids"])
        or observed_sessions["top"] != set(source_run["top_session_ids"])
    ):
        raise RuntimeFailure("capability provenance routed sessions drifted")
    for payload in artifacts_by_kind.get("typed_output", []):
        try:
            output = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeFailure("capability provenance typed output is invalid") from exc
        if output != {"answer": "workspace=read;transient=denied;source=denied"}:
            raise RuntimeFailure("capability provenance credential denial is insufficient")
    reports = [
        payload.decode("utf-8", errors="strict").replace("\r\n", "\n")
        for payload in artifacts_by_kind.get("focused_test_report", [])
    ]
    required_tests = {
        "test_cancel_rejects_record_without_live_unforgeable_marker",
        "test_cancel_signals_active_group_and_terminalizes_queued_task",
        "test_log_drainer_caps_event_object_count_even_within_durable_limit",
        "test_terminal_fence_runs_after_workers_and_before_results_or_receipt",
    }
    if not reports or not any(
        required_tests.issubset(set(re.findall(r"test_[a-z0-9_]+", report)))
        and "\nOK\n" in report
        for report in reports
    ):
        raise RuntimeFailure("capability provenance focused tests are insufficient")


def _drain_stream(
    stream: Any,
    path: Path,
    limit: int,
    outcome: dict[str, Any],
) -> None:
    """Drain a child pipe without backpressure while capping durable bytes."""

    seen = 0
    parsed_bytes = 0
    event_bytes = 0
    event_overflow = False
    parsed_events: list[dict[str, Any]] = []
    line_buffer = b""
    try:
        with path.open("wb") as destination:
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    break
                remaining = max(0, limit - seen)
                if remaining:
                    destination.write(chunk[:remaining])
                seen += len(chunk)
                parse_remaining = max(0, min(limit, _PARSED_EVENT_LIMIT_BYTES) - parsed_bytes)
                if parse_remaining:
                    parsed_chunk = chunk[:parse_remaining]
                    parsed_bytes += len(parsed_chunk)
                    line_buffer += parsed_chunk
                    while b"\n" in line_buffer:
                        raw_line, line_buffer = line_buffer.split(b"\n", 1)
                        if (
                            len(parsed_events) >= _PARSED_EVENT_LIMIT_COUNT
                            or event_bytes + len(raw_line) > _PARSED_EVENT_LIMIT_BYTES
                        ):
                            event_overflow = True
                            line_buffer = b""
                            continue
                        try:
                            item = json.loads(raw_line)
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            continue
                        if isinstance(item, dict):
                            parsed_events.append(item)
                            event_bytes += len(raw_line)
                elif chunk:
                    event_overflow = True
        if line_buffer and len(line_buffer) <= min(limit, _PARSED_EVENT_LIMIT_BYTES):
            if (
                len(parsed_events) >= _PARSED_EVENT_LIMIT_COUNT
                or event_bytes + len(line_buffer) > _PARSED_EVENT_LIMIT_BYTES
            ):
                event_overflow = True
                line_buffer = b""
        if line_buffer:
            try:
                item = json.loads(line_buffer)
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
            else:
                if isinstance(item, dict):
                    parsed_events.append(item)
        outcome.update(
            {
                "seen": seen,
                "overflow": seen > limit or event_overflow,
                "event_overflow": event_overflow,
                "error": None,
                "events": parsed_events,
            }
        )
    except Exception as exc:  # surfaced as a typed runner failure
        outcome.update({"seen": seen, "overflow": False, "error": str(exc)})
    finally:
        stream.close()


def _parse_jsonl(payload: bytes) -> list[dict[str, Any]]:
    if len(payload) > _PARSED_EVENT_LIMIT_BYTES:
        raise RuntimeFailure("parsed JSONL byte cap exceeded")
    events: list[dict[str, Any]] = []
    for raw_line in payload.splitlines():
        try:
            item = json.loads(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(item, dict):
            if len(events) >= _PARSED_EVENT_LIMIT_COUNT:
                raise RuntimeFailure("parsed JSONL event-count cap exceeded")
            events.append(item)
    return events


def _find_turn_context(codex_home: Path, thread_id: str) -> dict[str, Any] | None:
    candidates = sorted(
        codex_home.glob("sessions/**/*.jsonl"),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    scanned = 0
    for path in candidates[:32]:
        size = path.stat().st_size
        if size > 32 * 1024 * 1024 or scanned + size > 128 * 1024 * 1024:
            continue
        scanned += size
        with path.open("rb") as handle:
            payload_bytes = handle.read(size + 1)
        if len(payload_bytes) != size:
            continue
        projected = _project_turn_context_payload(payload_bytes, thread_id)
        if projected is not None:
            return projected
    return None


def _project_turn_context_payload(
    payload_bytes: bytes, thread_id: str
) -> dict[str, Any] | None:
    """Project one persisted turn context from a bounded rollout payload."""

    matched = False
    context: dict[str, Any] | None = None
    for line in payload_bytes.splitlines():
        try:
            event = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "session_meta":
            payload = event.get("payload")
            if isinstance(payload, dict) and thread_id in {
                payload.get("id"),
                payload.get("session_id"),
            }:
                matched = True
        elif event.get("type") == "turn_context":
            payload = event.get("payload")
            if isinstance(payload, dict):
                context = payload
    if not matched or context is None:
        return None
    return {
        "model": context.get("model"),
        "effort": context.get("effort")
        or context.get("collaboration_mode", {}).get("settings", {}).get("reasoning_effort"),
        "session_id": thread_id,
        "workspace_roots": context.get("workspace_roots"),
        "sandbox_policy": context.get("sandbox_policy"),
        "permission_profile": context.get("permission_profile"),
    }


def _project_turn_context_from_rollout(
    rollout_path: Path, thread_id: str
) -> dict[str, Any] | None:
    """Read and project one already size-capped canonical rollout."""

    size = rollout_path.stat().st_size
    if size <= 0 or size > 4 * 1024 * 1024:
        return None
    with rollout_path.open("rb") as handle:
        payload = handle.read(size + 1)
    if len(payload) != size:
        return None
    return _project_turn_context_payload(payload, thread_id)


def _find_session_rollout(codex_home: Path, thread_id: str) -> Path | None:
    """Return the one bounded rollout file carrying an exact session identity."""

    matches: list[Path] = []
    scanned = 0
    for path in sorted(codex_home.glob("sessions/**/*.jsonl"))[:128]:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
            continue
        prefix_size = min(path.stat().st_size, 64 * 1024)
        if scanned + prefix_size > 8 * 1024 * 1024:
            break
        scanned += prefix_size
        with path.open("rb") as handle:
            prefix = handle.read(prefix_size)
        for line in prefix.splitlines()[:8]:
            try:
                event = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            payload = event.get("payload") if isinstance(event, dict) else None
            if (
                event.get("type") == "session_meta"
                and isinstance(payload, dict)
                and thread_id in {payload.get("id"), payload.get("session_id")}
            ):
                matches.append(path.resolve(strict=True))
                break
    return matches[0] if len(matches) == 1 else None


def _attest_worker_permissions(
    context: dict[str, Any] | None,
    worker_root: Path,
    codex_home: Path,
    codex_binary: Path | None = None,
) -> str | None:
    """Return a failure reason unless the persisted turn proves least-privilege reads."""

    if not isinstance(context, dict):
        return "persisted turn context is missing"
    profile = context.get("permission_profile")
    if not isinstance(profile, dict) or profile.get("type") != "managed":
        return "worker permission profile is not managed"
    if profile.get("network") != "restricted":
        return "worker network is not restricted"
    if context.get("sandbox_policy") != {"type": "read-only"}:
        return "worker legacy sandbox projection is not read-only"
    file_system = profile.get("file_system")
    if not isinstance(file_system, dict) or file_system.get("type") != "restricted":
        return "worker filesystem is not restricted"
    entries = file_system.get("entries")
    if not isinstance(entries, list):
        return "worker filesystem entries are missing"

    worker_root = worker_root.resolve()
    codex_arg0_root = (codex_home.resolve() / "tmp" / "arg0")
    runtime_reads: set[Path] = set()
    if codex_binary is not None:
        candidate = (
            codex_binary.resolve(strict=True).parent.parent
            / "codex-resources/zsh/bin/zsh"
        )
        if candidate.exists() or candidate.is_symlink():
            if candidate.is_symlink() or not candidate.is_file():
                return "sealed Codex runtime read path is unsafe"
            runtime_reads.add(candidate.resolve(strict=True))
    if context.get("workspace_roots") != [os.fspath(worker_root)]:
        return "worker workspace roots are not exact"
    observed_kinds: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            return "worker filesystem entry is invalid"
        access = entry.get("access")
        if access != "read":
            return "worker filesystem contains non-read access"
        path = entry.get("path")
        if not isinstance(path, dict):
            return "worker filesystem path is invalid"
        special = path.get("value") if path.get("type") == "special" else None
        kind = special.get("kind") if isinstance(special, dict) else None
        if kind is not None:
            if kind != "minimal":
                return "worker filesystem contains an unexpected special root"
            observed_kinds.append("minimal")
            continue
        path_value = path.get("path") if path.get("type") == "path" else None
        if not isinstance(path_value, str):
            return "worker filesystem path is not concrete"
        try:
            resolved = Path(path_value).resolve()
        except OSError:
            return "worker filesystem path cannot be resolved"
        if resolved == worker_root:
            observed_kinds.append("worker_root")
        elif resolved.is_relative_to(codex_arg0_root) and resolved.name.startswith("codex-arg0"):
            observed_kinds.append("codex_arg0")
        elif resolved in runtime_reads:
            observed_kinds.append(f"codex_runtime:{resolved}")
        else:
            return "worker filesystem contains an unexpected readable path"
    # Codex materializes arg0 lazily. A no-tool task may omit it; if present it
    # must still be the single actor-owned path recognized above.
    base_kinds = {"minimal", "worker_root"}
    allowed_kinds = {*base_kinds, "codex_arg0"}
    if (
        not base_kinds.issubset(observed_kinds)
        or len(observed_kinds) != len(set(observed_kinds))
        or any(
            kind not in allowed_kinds
            and not kind.startswith("codex_runtime:")
            for kind in observed_kinds
        )
    ):
        return "worker filesystem allowlist is incomplete or duplicated"
    return None


def _cancel_request(root: Path, authority_revision: int) -> dict[str, Any] | None:
    path = root / "amendments" / "cancel.json"
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise RuntimeFailure("cancel request is unsafe")
    try:
        value = json.loads(path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeFailure("cancel request is invalid") from exc
    expected = {"schema_version", "workflow_id", "authority_revision", "requested_at"}
    if not isinstance(value, dict) or set(value) != expected:
        raise RuntimeFailure("cancel request contract is invalid")
    if value["schema_version"] != "agent-workflow.cancel-request.vnext.v1":
        raise RuntimeFailure("cancel request schema is invalid")
    if value["authority_revision"] != authority_revision:
        raise RuntimeFailure("cancel request authority revision is stale")
    return value


@shared_authority_transaction
def cancel_run(
    root: Path,
    authority_revision: int,
    *,
    grace_seconds: float = 5.0,
) -> dict[str, Any]:
    """Create one authority-bound cancel request and terminate matching owned PGIDs."""

    root = Path(root).resolve()
    if (root / "final.json").exists() or (root / "final.json").is_symlink():
        raise RuntimeFailure("final seal rejects a later cancel request")
    workflow = _load_fixed_json(root, "workflow.json", "workflow seal")
    validate_contract("workflow", workflow)
    try:
        live_authority_revision = current_authority_revision(root, workflow)
    except RecoveryError as exc:
        raise RuntimeFailure(str(exc)) from exc
    if authority_revision != live_authority_revision:
        raise RuntimeFailure("cancel authority revision does not match current amendment authority")
    request = {
        "schema_version": "agent-workflow.cancel-request.vnext.v1",
        "workflow_id": workflow["workflow_id"],
        "authority_revision": authority_revision,
        "requested_at": _timestamp(_now()),
    }
    try:
        request_path = create_once_json(root, "amendments/cancel.json", request)
    except ArtifactError as exc:
        raise RuntimeFailure(str(exc)) from exc

    signalled: list[str] = []
    records_root = root / "runtime" / "processes"
    records = sorted(records_root.rglob("*.json")) if records_root.is_dir() else []
    live: list[tuple[str, int, int, str | None, str, str | None]] = []
    for path in records:
        supervisor_request: dict[str, Any] | None = None
        if path.is_symlink() or not path.is_file():
            raise RuntimeFailure("active process record is unsafe")
        try:
            record = json.loads(path.read_bytes())
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeFailure("active process record is invalid") from exc
        legacy_keys = {
            "workflow_id",
            "authority_revision",
            "task_id",
            "pid",
            "pgid",
            "audit_marker",
            "process_identity",
            "command",
            "command_sha256",
        }
        watchdog_v2_keys = legacy_keys | {
            "schema_version",
            "generation_id",
            "phase_id",
            "supervisor_pid",
            "supervisor_identity",
            "request_ref",
            "request_sha256",
            "plan_sha256",
            "generation_claim_ref",
            "generation_claim_sha256",
            "runtime_bundle_sha256",
            "codex_binary_sha256",
            "deadline_at",
        }
        watchdog_v3_keys = (legacy_keys - {"process_identity"}) | {
            "schema_version",
            "generation_id",
            "phase_id",
            "supervisor_pid",
            "supervisor_birth",
            "supervisor_sid",
            "supervisor_command",
            "supervisor_command_sha256",
            "sid",
            "process_birth",
            "request_ref",
            "request_sha256",
            "plan_sha256",
            "generation_claim_ref",
            "generation_claim_sha256",
            "runtime_bundle_sha256",
            "codex_binary_sha256",
            "deadline_at",
            "deadline_monotonic",
            "boot_identity",
            "terminal_ref",
            "started_at",
        }
        watchdog_v4_keys = watchdog_v3_keys | {
            "codex_binary",
            "transport_executable_sha256",
            "transport_adapter_sha256",
        }
        record_keys = frozenset(record) if isinstance(record, dict) else frozenset()
        if (
            not isinstance(record, dict)
            or record_keys not in {
                frozenset(legacy_keys),
                frozenset(watchdog_v2_keys),
                frozenset(watchdog_v3_keys),
                frozenset(watchdog_v4_keys),
            }
        ):
            raise RuntimeFailure("active process record contract is invalid")
        if record_keys in {frozenset(watchdog_v2_keys), frozenset(watchdog_v3_keys), frozenset(watchdog_v4_keys)}:
            expected_schema = (
                "agent-workflow.process-record.vnext.v4"
                if record_keys == frozenset(watchdog_v4_keys)
                else "agent-workflow.process-record.vnext.v3"
                if record_keys == frozenset(watchdog_v3_keys)
                else "agent-workflow.process-record.vnext.v2"
            )
            if record["schema_version"] != expected_schema:
                raise RuntimeFailure("active process record schema is invalid")
            request_ref = record["request_ref"]
            request_sha256 = record["request_sha256"]
            if (
                not isinstance(request_ref, str)
                or not isinstance(request_sha256, str)
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", request_sha256)
            ):
                raise RuntimeFailure("active process request binding is invalid")
            supervisor_request_path = root / request_ref
            try:
                supervisor_request_path.relative_to(root)
            except ValueError as exc:
                raise RuntimeFailure("active process request path escapes the workflow") from exc
            if (
                supervisor_request_path.is_symlink()
                or not supervisor_request_path.is_file()
                or _digest(supervisor_request_path.read_bytes()) != request_sha256
            ):
                raise RuntimeFailure("active process request binding drifted")
            if record_keys in {frozenset(watchdog_v3_keys), frozenset(watchdog_v4_keys)}:
                supervisor_request, _ = load_supervisor_request(root, request_ref)
        if (
            record["workflow_id"] != workflow["workflow_id"]
            or record["authority_revision"] != authority_revision
        ):
            continue
        pid = record["pid"]
        pgid = record["pgid"]
        marker = record["audit_marker"]
        identity = record.get("process_identity")
        birth = record.get("process_birth")
        command = record["command"]
        command_sha256 = record["command_sha256"]
        if (
            not isinstance(pid, int)
            or not isinstance(pgid, int)
            or not isinstance(marker, str)
            or not isinstance(identity if identity is not None else birth, str)
            or not isinstance(command, list)
            or not all(isinstance(item, str) for item in command)
            or not isinstance(command_sha256, str)
            or not isinstance(record["task_id"], str)
        ):
            raise RuntimeFailure("active process record values are invalid")
        expected_marker_prefix = (
            f"agent-workflow:{workflow['workflow_id']}:{record['phase_id']}:{record['task_id']}:"
            if record_keys in {frozenset(watchdog_v3_keys), frozenset(watchdog_v4_keys)}
            else f"agent-workflow:{workflow['workflow_id']}:{record['task_id']}:"
        )
        if (
            pgid != pid
            or not marker.startswith(expected_marker_prefix)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", command_sha256)
            or _digest(_canonical(command)) != command_sha256
            or f'agent_workflow_audit_marker="{marker}"' not in command
        ):
            raise RuntimeFailure("active process record ownership proof is invalid")
        live_identity = _process_identity(pid)
        if identity is not None and live_identity != identity:
            continue
        if birth is not None:
            if process_birth(pid) != birth:
                continue
            if record.get("sid") != pid:
                raise RuntimeFailure("active process SID ownership proof is invalid")
        if live_identity is None or marker not in live_identity:
            raise RuntimeFailure("active process live marker ownership proof is invalid")
        if supervisor_request is not None:
            live_command = process_command(pid)
            if live_command is None or not command_matches_request(
                live_command,
                supervisor_request,
                record["request_ref"],
            ):
                raise RuntimeFailure("active process live command fence is invalid")
        try:
            if os.getpgid(pid) != pgid:
                raise RuntimeFailure("active process PGID no longer matches its owner")
        except ProcessLookupError:
            continue
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        signalled.append(record["task_id"])
        live.append((record["task_id"], pid, pgid, identity, marker, birth))

    deadline = time.monotonic() + max(0.0, grace_seconds)
    while live and time.monotonic() < deadline:
        live = [
            item
            for item in live
            if (
                (item[3] is None or _process_identity(item[1]) == item[3])
                and (item[5] is None or process_birth(item[1]) == item[5])
                and item[4] in (_process_identity(item[1]) or "")
            )
        ]
        if live:
            time.sleep(0.05)
    killed: list[str] = []
    for task_id, pid, pgid, identity, marker, birth in live:
        live_identity = _process_identity(pid)
        if (
            live_identity is None
            or marker not in live_identity
            or (identity is not None and live_identity != identity)
            or (birth is not None and process_birth(pid) != birth)
        ):
            continue
        try:
            os.killpg(pgid, signal.SIGKILL)
            killed.append(task_id)
        except ProcessLookupError:
            pass
    return {
        "status": "cancel_requested",
        "request_ref": request_path.relative_to(root).as_posix(),
        "signalled_tasks": signalled,
        "killed_tasks": killed,
    }


def _reconcile_run(
    root: Path,
    authority_revision: int,
    *,
    grace_seconds: float = 1.0,
    finalization_lock_held: bool = False,
) -> dict[str, Any]:
    """Reconcile watchdog attempts and rebuild the non-authoritative view."""

    root = Path(root).resolve()
    if (root / "final.json").exists() or (root / "final.json").is_symlink():
        raise RuntimeFailure("final seal rejects later reconciliation")
    workflow = _load_fixed_json(root, "workflow.json", "workflow seal")
    validate_contract("workflow", workflow)
    try:
        live_authority_revision = current_authority_revision(root, workflow)
    except RecoveryError as exc:
        raise RuntimeFailure(str(exc)) from exc
    if authority_revision != live_authority_revision:
        raise RuntimeFailure("reconcile authority revision does not match current amendment authority")
    summary = reconcile_supervisors(root, grace_seconds=grace_seconds)
    materialized_receipts: list[str] = []
    if not summary["active"]:
        for plan_ref in sorted(_committed_plan_refs(root, workflow)):
            plan_path = root / plan_ref
            phase_root = plan_path.parent
            if (phase_root / "receipt.json").exists():
                continue
            plan = _load_fixed_json(root, plan_path.relative_to(root).as_posix(), "phase plan")
            validate_contract("phase-plan", plan)
            executions: dict[str, RawExecution] = {}
            all_terminal = True
            for task in plan["tasks"]:
                request_ref = f"runtime/watchdogs/{plan['phase_id']}/{task['task_id']}/request.json"
                receipt_ref = f"runtime/watchdogs/{plan['phase_id']}/{task['task_id']}/terminal.json"
                request_exists = (root / request_ref).is_file() and not (root / request_ref).is_symlink()
                receipt_exists = (root / receipt_ref).is_file() and not (root / receipt_ref).is_symlink()
                if request_exists and receipt_exists:
                    executions[task["task_id"]] = _raw_from_supervisor_terminal(
                        root,
                        request_ref,
                        receipt_ref,
                    )
                elif request_exists and not receipt_exists:
                    process_ref = (
                        f"runtime/processes/{plan['phase_id']}/{task['task_id']}.json"
                    )
                    if (root / process_ref).exists() or (root / process_ref).is_symlink():
                        raise RuntimeFailure(
                            "request-only reconcile found an unterminalized process record"
                        )
                    try:
                        request, request_payload = load_supervisor_request(root, request_ref)
                    except SupervisorFailure as exc:
                        raise RuntimeFailure("request-only reconcile request is invalid") from exc
                    claim_ref = _generation_claim_ref(plan)
                    claim_path = root / claim_ref
                    if (
                        request["workflow_id"] != workflow["workflow_id"]
                        or request["authority_revision"] != plan["authority_revision"]
                        or request["generation_id"] != plan["generation_id"]
                        or request["phase_id"] != plan["phase_id"]
                        or request["task_id"] != task["task_id"]
                        or request["plan_sha256"] != _digest(plan_path.read_bytes())
                        or request["generation_claim_ref"] != claim_ref
                        or request["generation_claim_sha256"]
                        != _digest(claim_path.read_bytes())
                        or live_marker_processes(request["audit_marker"])
                    ):
                        raise RuntimeFailure(
                            "request-only reconcile cannot prove the task never launched"
                        )
                    event = {
                        "type": "runtime.runner_interrupted_before_launch",
                        "phase_id": plan["phase_id"],
                        "task_id": task["task_id"],
                        "request_sha256": _digest(request_payload),
                    }
                    now = _now()
                    executions[task["task_id"]] = RawExecution(
                        exit_code=0,
                        events=[event],
                        stderr="",
                        turn_context=None,
                        stdout_bytes=_canonical(event),
                        observed_started_at=now,
                        observed_finished_at=now,
                        interrupted_before_launch=True,
                    )
                elif not request_exists and not receipt_exists:
                    event = {
                        "type": "runtime.runner_interrupted_before_launch",
                        "phase_id": plan["phase_id"],
                        "task_id": task["task_id"],
                    }
                    now = _now()
                    executions[task["task_id"]] = RawExecution(
                        exit_code=0,
                        events=[event],
                        stderr="",
                        turn_context=None,
                        stdout_bytes=_canonical(event),
                        observed_started_at=now,
                        observed_finished_at=now,
                        interrupted_before_launch=True,
                    )
                else:
                    all_terminal = False
                    break
            if all_terminal:
                source_phase = None
                if any(task["work_mode"] == "write" for task in plan["tasks"]):
                    try:
                        repository_root = _repository_root_for(root)
                        admission_baseline = json.loads(
                            _read_artifact_bytes(
                                root,
                                workflow["baseline_ref"],
                                workflow["baseline_sha256"],
                            )
                        )
                        source_phase = load_isolated_phase(
                            root,
                            repository_root,
                            plan,
                            admission_baseline=admission_baseline,
                            predecessor_sha256=plan["predecessor_sha256"],
                        )
                    except (
                        ProtocolError,
                        UnicodeDecodeError,
                        json.JSONDecodeError,
                        SourceWriteError,
                    ) as exc:
                        raise RuntimeFailure(
                            f"source integration reconciliation failed closed: {exc}"
                        ) from exc
                phase_runner = (
                    run_read_only_phase.__wrapped__
                    if finalization_lock_held
                    else run_read_only_phase
                )
                phase_summary = phase_runner(
                    root,
                    plan,
                    lambda _task, _packet: RawExecution(1, [], "", None),
                    max_parallel=max(1, len(executions)),
                    reconciled_executions=executions,
                    source_phase=source_phase,
                )
                materialized_receipts.append(phase_summary["receipt_ref"])
    auth_scrubbed = False
    if not summary["active"]:
        auth_scrubbed = _scrub_stale_codex_auth(root)
    view = rebuild_view(root)
    return {
        **summary,
        "view_ref": "view.json",
        "view_sha256": _digest((root / "view.json").read_bytes()),
        "attempt_count": len(view["attempts"]),
        "stale_auth_scrubbed": auth_scrubbed,
        "materialized_phase_receipts": materialized_receipts,
    }


@shared_authority_transaction
def reconcile_run(
    root: Path,
    authority_revision: int,
    *,
    grace_seconds: float = 1.0,
) -> dict[str, Any]:
    return _reconcile_run(root, authority_revision, grace_seconds=grace_seconds)


@serialized_authority_transaction
def seal_final(root: Path, candidate: dict[str, Any]) -> Path:
    """Validate and create-once publish one terminal final contract."""

    root = Path(root).resolve()
    final_path = root / "final.json"
    if final_path.exists() or final_path.is_symlink():
        raise RuntimeFailure("final.json is already sealed")
    workflow_path = root / "workflow.json"
    workflow = _load_fixed_json(root, "workflow.json", "workflow seal")
    try:
        validate_contract("workflow", workflow)
        validate_contract("final", candidate)
        authority_revision = current_authority_revision(root, workflow)
    except (ProtocolError, RecoveryError) as exc:
        raise RuntimeFailure(str(exc)) from exc
    if candidate["workflow_id"] != workflow["workflow_id"]:
        raise RuntimeFailure("final candidate belongs to another workflow")
    if _cancel_request(root, authority_revision) is not None:
        raise RuntimeFailure("cancel fence rejects final publication")

    reconcile_summary = _reconcile_run(
        root,
        authority_revision,
        grace_seconds=0.0,
        finalization_lock_held=True,
    )
    if reconcile_summary.get("active"):
        raise RuntimeFailure("final publication requires all attempts terminal")
    try:
        projection = build_resume_brief(root, workflow, candidate["generation_id"])
    except RecoveryError as exc:
        raise RuntimeFailure(str(exc)) from exc
    terminal_phases = projection["terminal_phases"]
    expected_phase_refs = [item["receipt_ref"] for item in terminal_phases]
    expected_phase_sha256 = {
        item["receipt_ref"]: item["receipt_sha256"] for item in terminal_phases
    }
    if (
        candidate["phase_receipt_refs"] != expected_phase_refs
        or candidate["phase_receipt_sha256"] != expected_phase_sha256
    ):
        raise RuntimeFailure("final candidate does not cover the exact terminal phase chain")
    if (
        candidate["status"] == "complete"
        and candidate["verification_ref"] != expected_phase_refs[-1]
    ):
        raise RuntimeFailure("complete final requires the latest phase to be verification")

    amendment_paths = sorted((root / "amendments" / "criteria").glob("*.json"))
    expected_amendment_refs = [path.relative_to(root).as_posix() for path in amendment_paths]
    expected_amendment_sha256 = {
        path.relative_to(root).as_posix(): _digest(path.read_bytes())
        for path in amendment_paths
    }
    if (
        candidate["amendment_refs"] != expected_amendment_refs
        or candidate["amendment_sha256"] != expected_amendment_sha256
    ):
        raise RuntimeFailure("final candidate does not cover the exact amendment chain")

    lineage_paths = sorted((root / "lineages").glob("*/origin.json"))
    lineage_paths.extend(sorted((root / "lineages").glob("*/recovery.json")))
    lineage_paths.sort()
    expected_lineage_refs = [path.relative_to(root).as_posix() for path in lineage_paths]
    expected_lineage_sha256 = {
        path.relative_to(root).as_posix(): _digest(path.read_bytes())
        for path in lineage_paths
    }
    if (
        candidate["lineage_claim_refs"] != expected_lineage_refs
        or candidate["lineage_claim_sha256"] != expected_lineage_sha256
    ):
        raise RuntimeFailure("final candidate does not cover the exact lineage claim set")
    try:
        validate_replay_candidate(
            root,
            workflow_sha256=_digest(workflow_path.read_bytes()),
            final=candidate,
        )
    except ProtocolError as exc:
        raise RuntimeFailure(f"final candidate replay failed: {exc}") from exc
    try:
        if (
            current_authority_revision(root, workflow) != authority_revision
            or _cancel_request(root, authority_revision) is not None
        ):
            raise RuntimeFailure("final authority fence drifted before publication")
    except RecoveryError as exc:
        raise RuntimeFailure(str(exc)) from exc
    try:
        return create_once_json(root, "final.json", candidate)
    except ArtifactError as exc:
        raise RuntimeFailure(f"final publication failed: {exc}") from exc


def codex_task_executor(config: CodexExecConfig) -> TaskExecutor:
    """Build a crash-independent, watchdog-supervised Codex executor."""

    run_root = Path(config.run_root).resolve()
    repo_root = Path(config.repo_root).resolve()
    codex_home = Path(config.codex_home).resolve()

    def execute(task: dict[str, Any], packet: dict[str, Any]) -> RawExecution:
        if _cancel_request(run_root, config.authority_revision) is not None:
            event = {"type": "runtime.cancelled", "task_id": task["task_id"], "launched": False}
            return RawExecution(
                exit_code=-signal.SIGTERM,
                events=[event],
                stderr="",
                turn_context=None,
                stdout_bytes=_canonical(event),
                cancelled=True,
            )
        execution_root = Path(task.get("_runtime_worker_root", repo_root)).resolve(strict=True)
        execution_codex_home = Path(task.get("_runtime_codex_home", codex_home)).resolve()
        permissions_profile = task.get("_runtime_permissions_profile", config.permissions_profile)
        write_roots = tuple(task.get("_runtime_write_roots", ()))
        source_launch_fence = task.get("_runtime_source_launch_fence")
        if task.get("work_mode") == "write" and not callable(source_launch_fence):
            raise RuntimeFailure("source task lacks its launch-time dependency fence")
        resume_binding = task.get("_runtime_resume_binding")
        if not isinstance(permissions_profile, str) or not permissions_profile:
            raise RuntimeFailure("task permission profile is invalid")
        if resume_binding is not None:
            required_binding = {
                "failed_result_ref", "failed_result_sha256", "causal_receipt_ref",
                "causal_receipt_sha256", "session_id", "codex_home",
                "session_rollout_path", "prior_rollout_sha256", "prior_rollout_size",
            }
            if not isinstance(resume_binding, dict) or set(resume_binding) != required_binding:
                raise RuntimeFailure("recovery resume binding is invalid")
            if not re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                resume_binding["session_id"],
            ):
                raise RuntimeFailure("recovery resume session id is invalid")
        schema_path = run_root / packet["output_schema_ref"]
        model = config.worker_model if task["role"] == "worker" else config.top_model
        phase_id = task.get("_runtime_phase_id", "001-unknown")
        audit_marker = (
            f"agent-workflow:{config.workflow_id}:{phase_id}:{task['task_id']}:{uuid.uuid4().hex}"
        )
        codex_binary_path = Path(config.codex_binary).resolve(strict=True)
        codex_binary_sha256 = _digest_file(codex_binary_path)
        runtime_bundle_sha256 = task.get("_runtime_bundle_sha256", _runtime_bundle_sha256())
        transport_adapter_sha256: str | None = None
        if resume_binding is None:
            command = [config.codex_binary, "exec", "--ignore-rules", "--disable", "plugins", "--json", "-m", model, "-c", f'model_reasoning_effort="{config.reasoning_effort}"', "-c", 'shell_environment_policy.inherit="core"', "-c", 'cli_auth_credentials_store="file"', "-c", f"developer_instructions={json.dumps(_ISOLATED_WORKER_DEVELOPER_INSTRUCTIONS)}", "-c", f'agent_workflow_audit_marker="{audit_marker}"', "--output-schema", str(schema_path), "-p", permissions_profile, "-C", str(execution_root), packet["prompt"]]
            transport_executable = codex_binary_path
        else:
            try:
                pinned_runtime = _resolve_pinned_runtime(run_root, runtime_bundle_sha256)
            except PinnedBundleUnavailable as exc:
                raise RuntimeFailure("recovery resume lacks its pinned runtime bundle") from exc
            adapter_path = pinned_runtime.parent / "app_resume_adapter.py"
            rollout_path = Path(resume_binding["session_rollout_path"]).resolve(strict=True)
            if (
                _digest_file(rollout_path) != resume_binding["prior_rollout_sha256"]
                or rollout_path.stat().st_size != resume_binding["prior_rollout_size"]
            ):
                raise RuntimeFailure("recovery session changed before resume admission")
            spec_ref = f"runtime/resume/{phase_id}/{task['task_id']}/spec.json"
            resume_nonce = uuid.uuid4().hex
            task_prompt = packet["prompt"]
            resume_prompt = (
                f"{task_prompt}\n\n[agent_workflow_resume_nonce={resume_nonce}]"
            )
            spec = {
                "schema_version": "agent-workflow.app-resume-spec.vnext.v1",
                "workflow_id": config.workflow_id,
                "authority_revision": config.authority_revision,
                "generation_id": task.get("_runtime_generation_id", "generation-001"),
                "phase_id": phase_id,
                "task_id": task["task_id"],
                "lineage_id": task["lineage_id"],
                "plan_sha256": task.get("_runtime_plan_sha256", "sha256:" + "0" * 64),
                "generation_claim_ref": task.get("_runtime_generation_claim_ref", "generations/claims/unknown.json"),
                "generation_claim_sha256": task.get("_runtime_generation_claim_sha256", "sha256:" + "0" * 64),
                "runtime_bundle_sha256": runtime_bundle_sha256,
                "failed_result_ref": resume_binding["failed_result_ref"],
                "failed_result_sha256": resume_binding["failed_result_sha256"],
                "causal_receipt_ref": resume_binding["causal_receipt_ref"],
                "causal_receipt_sha256": resume_binding["causal_receipt_sha256"],
                "session_id": resume_binding["session_id"],
                "codex_home": os.fspath(execution_codex_home),
                "session_rollout_path": os.fspath(rollout_path),
                "prior_rollout_sha256": resume_binding["prior_rollout_sha256"],
                "prior_rollout_size": resume_binding["prior_rollout_size"],
                "codex_binary_sha256": codex_binary_sha256,
                "model": model,
                "reasoning_effort": config.reasoning_effort,
                "permissions_profile": permissions_profile.replace("-", "_"),
                "cwd": os.fspath(execution_root),
                "runtime_workspace_roots": [os.fspath(execution_root)],
                "prompt": resume_prompt,
                "task_prompt_sha256": _digest(task_prompt.encode("utf-8")),
                "resume_nonce": resume_nonce,
                "output_schema_path": os.fspath(schema_path.resolve(strict=True)),
                "output_schema_sha256": _digest_file(schema_path.resolve(strict=True)),
                "audit_marker": audit_marker,
                "run_root": os.fspath(run_root),
                "claim_ref": f"runtime/resume/{phase_id}/{task['task_id']}/claim.json",
                "turn_claim_ref": f"runtime/resume/{phase_id}/{task['task_id']}/turn-claim.json",
                "terminal_ref": f"runtime/resume/{phase_id}/{task['task_id']}/terminal.json",
            }
            try:
                spec_path = create_once_json(run_root, spec_ref, spec)
            except ArtifactError as exc:
                raise RuntimeFailure(f"recovery resume spec failed: {exc}") from exc
            spec_sha256 = _digest(spec_path.read_bytes())
            transport_executable = Path(sys.executable).resolve(strict=True)
            transport_adapter_sha256 = _digest_file(adapter_path)
            command = [os.fspath(transport_executable), os.fspath(adapter_path), "--spec", os.fspath(spec_path), "--spec-sha256", spec_sha256, "--codex", os.fspath(codex_binary_path), "-c", f'agent_workflow_audit_marker="{audit_marker}"']
        isolated_home = execution_codex_home / "home"
        isolated_tmp = execution_codex_home / "tmp"
        isolated_home.mkdir(parents=True, exist_ok=True)
        isolated_tmp.mkdir(parents=True, exist_ok=True)
        environment = {
            "CODEX_HOME": str(execution_codex_home),
            "HOME": str(isolated_home),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
            "TMPDIR": str(isolated_tmp),
            "AGENT_WORKFLOW_AUDIT_MARKER": audit_marker,
        }
        timeout_seconds = float(
            task.get("_runtime_timeout_seconds", task["execution_deadline_seconds"])
        )
        deadline_at = task.get("_runtime_deadline_at")
        if not isinstance(deadline_at, str):
            deadline_at = _timestamp(_now() + timedelta(seconds=max(0.0, timeout_seconds)))
        request_ref = f"runtime/watchdogs/{phase_id}/{task['task_id']}/request.json"
        stdout_ref = f"transient/{phase_id}/{task['task_id']}/stdout.jsonl"
        stderr_ref = f"transient/{phase_id}/{task['task_id']}/stderr.log"
        receipt_ref = f"runtime/watchdogs/{phase_id}/{task['task_id']}/terminal.json"
        request = {
            "schema_version": "agent-workflow.supervisor-request.vnext.v2",
            "workflow_id": config.workflow_id,
            "authority_revision": config.authority_revision,
            "generation_id": task.get("_runtime_generation_id", "generation-001"),
            "phase_id": phase_id,
            "task_id": task["task_id"],
            "plan_sha256": task.get("_runtime_plan_sha256", "sha256:" + "0" * 64),
            "generation_claim_ref": task.get("_runtime_generation_claim_ref", "generations/claims/unknown.json"),
            "generation_claim_sha256": task.get("_runtime_generation_claim_sha256", "sha256:" + "0" * 64),
            "runtime_bundle_sha256": runtime_bundle_sha256,
            "codex_binary": os.fspath(codex_binary_path),
            "codex_binary_sha256": codex_binary_sha256,
            "transport_executable_sha256": _digest_file(transport_executable),
            "transport_adapter_sha256": transport_adapter_sha256,
            "command": command,
            "command_sha256": _digest(_canonical(command)),
            "cwd": os.fspath(execution_root),
            "work_mode": task.get("work_mode", "read"),
            "write_roots": list(write_roots),
            "environment": environment,
            "audit_marker": audit_marker,
            "deadline_at": deadline_at,
            "deadline_monotonic": task.get(
                "_runtime_deadline_monotonic",
                time.monotonic() + max(0.0, timeout_seconds),
            ),
            "boot_identity": task.get("_runtime_boot_identity", _process_identity(1)),
            "terminate_grace_seconds": config.terminate_grace_seconds,
            "log_limit_bytes": config.log_limit_bytes,
            "stdout_ref": stdout_ref,
            "stderr_ref": stderr_ref,
            "receipt_ref": receipt_ref,
        }
        try:
            create_once_json(run_root, request_ref, request)
            if source_launch_fence is not None:
                source_launch_fence()
            watchdog = launch_supervisor(run_root, request_ref)
            watchdog.wait(timeout=max(1.0, timeout_seconds + config.terminate_grace_seconds + 5.0))
        except ArtifactError as exc:
            raise RuntimeFailure(str(exc)) from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeFailure("watchdog did not publish a bounded terminal receipt") from exc
        receipt_path = run_root / receipt_ref
        if watchdog.returncode != 0 or receipt_path.is_symlink() or not receipt_path.is_file():
            raise RuntimeFailure("watchdog failed without a terminal receipt")
        try:
            receipt = json.loads(receipt_path.read_bytes())
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeFailure("watchdog receipt is invalid JSON") from exc
        validate_supervisor_receipt(receipt)
        expected_request_sha = _digest((run_root / request_ref).read_bytes())
        if (
            not isinstance(receipt, dict)
            or receipt.get("schema_version") != "agent-workflow.supervisor-receipt.vnext.v1"
            or receipt.get("workflow_id") != config.workflow_id
            or receipt.get("authority_revision") != config.authority_revision
            or receipt.get("task_id") != task["task_id"]
            or receipt.get("request_ref") != request_ref
            or receipt.get("request_sha256") != expected_request_sha
        ):
            raise RuntimeFailure("watchdog receipt does not match its sealed request")
        stdout_path = run_root / stdout_ref
        stderr_path = run_root / stderr_ref
        stdout_payload = stdout_path.read_bytes()
        stderr_payload = stderr_path.read_bytes()
        if (
            _digest(stdout_payload) != receipt.get("stdout_sha256")
            or _digest(stderr_payload) != receipt.get("stderr_sha256")
        ):
            raise RuntimeFailure("watchdog log digest does not match its receipt")
        events = _parse_jsonl(stdout_payload)
        status = receipt.get("status")
        cancelled = status == "cancelled"
        timed_out = status in {"timed_out", "not_started_deadline"}
        escaped_process = status == "escaped_process_detected"
        if cancelled:
            cancel_event = {"type": "runtime.cancelled", "task_id": task["task_id"], "launched": receipt.get("exit_code") is not None}
            events.append(cancel_event)
            stdout_payload += _canonical(cancel_event)
        if timed_out:
            timeout_type = "runtime.not_started_deadline" if status == "not_started_deadline" else "runtime.timeout"
            timeout_event = {"type": timeout_type, "task_id": task["task_id"]}
            events.append(timeout_event)
            stdout_payload += _canonical(timeout_event)
        if escaped_process:
            escape_event = {"type": "runtime.escaped_process_detected", "task_id": task["task_id"]}
            events.append(escape_event)
            stdout_payload += _canonical(escape_event)
        log_limit_exceeded = bool(receipt.get("log_limit_exceeded"))
        if log_limit_exceeded:
            limit_event = {"type": "runtime.log_limit_exceeded", "task_id": task["task_id"], "limit_bytes": config.log_limit_bytes}
            events.append(limit_event)
            stdout_payload += _canonical(limit_event)
        thread_ids = [event.get("thread_id") for event in events if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str)]
        thread_id = thread_ids[0] if len(thread_ids) == 1 else None
        context = _find_turn_context(execution_codex_home, thread_id) if thread_id else None
        permission_failure = (
            attest_writer_permissions(
                context,
                execution_root,
                execution_codex_home,
                write_roots,
                codex_binary_path,
            )
            if task.get("work_mode") == "write"
            else _attest_worker_permissions(
                context,
                execution_root,
                execution_codex_home,
                codex_binary_path,
            )
        )
        return RawExecution(
            exit_code=receipt.get("exit_code") if isinstance(receipt.get("exit_code"), int) else -signal.SIGTERM,
            events=events,
            stderr=stderr_payload.decode("utf-8", errors="replace") + (f"\npermission attestation failed: {permission_failure}" if permission_failure else ""),
            turn_context=context,
            stdout_bytes=stdout_payload,
            adapter_error=permission_failure is not None,
            log_limit_exceeded=log_limit_exceeded,
            cancelled=cancelled,
            not_started_deadline=status == "not_started_deadline",
        )

    return execute


def _raw_from_supervisor_terminal(
    root: Path,
    request_ref: str,
    receipt_ref: str,
) -> RawExecution:
    """Project one digest-bound watchdog terminal into the existing result parser."""

    root = Path(root).resolve()
    request, request_payload = load_supervisor_request(root, request_ref)
    receipt = _load_fixed_json(root, receipt_ref, "supervisor receipt")
    validate_supervisor_receipt(receipt)
    if receipt["request_ref"] != request_ref or receipt["request_sha256"] != _digest(request_payload):
        raise RuntimeFailure("supervisor terminal request binding drifted")
    if request["runtime_bundle_sha256"] != _runtime_bundle_sha256():
        raise RuntimeFailure("reconcile runtime bundle drifted from the launch seal")
    binary = Path(request["codex_binary"]).resolve(strict=True)
    if _digest_file(binary) != request["codex_binary_sha256"]:
        raise RuntimeFailure("reconcile Codex binary drifted from the launch seal")
    stdout = (root / receipt["stdout_ref"]).read_bytes()
    stderr = (root / receipt["stderr_ref"]).read_bytes()
    if _digest(stdout) != receipt["stdout_sha256"] or _digest(stderr) != receipt["stderr_sha256"]:
        raise RuntimeFailure("reconcile watchdog log digest drifted")
    events = _parse_jsonl(stdout)
    adapter_recovered = False
    if receipt["producer"] == "reconciler" and request["transport_adapter_sha256"] is not None:
        command = request["command"]
        if command.count("--spec") != 1 or command.count("--spec-sha256") != 1:
            raise RuntimeFailure("reconcile App resume command lacks exact spec authority")
        spec_path = Path(command[command.index("--spec") + 1])
        spec_sha256 = command[command.index("--spec-sha256") + 1]
        try:
            spec_ref = spec_path.resolve(strict=True).relative_to(root).as_posix()
        except (OSError, ValueError) as exc:
            raise RuntimeFailure("reconcile App resume spec escapes the workflow") from exc
        spec_payload = _read_artifact_bytes(root, spec_ref, spec_sha256)
        try:
            spec = json.loads(spec_payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeFailure("reconcile App resume spec is invalid") from exc
        if (
            not isinstance(spec, dict)
            or spec.get("schema_version") != "agent-workflow.app-resume-spec.vnext.v1"
            or not isinstance(spec.get("terminal_ref"), str)
            or not isinstance(spec.get("turn_claim_ref"), str)
            or not isinstance(spec.get("session_rollout_path"), str)
        ):
            raise RuntimeFailure("reconcile App resume spec contract drifted")
        terminal = _load_fixed_json(root, spec.get("terminal_ref"), "App resume terminal")
        turn_claim = _load_fixed_json(root, spec.get("turn_claim_ref"), "App resume turn claim")
        rollout = Path(spec.get("session_rollout_path", ""))
        codex_home = Path(request["environment"]["CODEX_HOME"]).resolve()
        expected_turn_claim_keys = {
            "schema_version", "spec_sha256", "session_id", "turn_id",
            "prompt_sha256", "resume_nonce", "audit_marker",
        }
        if (
            not isinstance(terminal, dict)
            or set(terminal) != {
                "schema_version", "spec_sha256", "session_id", "turn_id",
                "rollout_sha256", "events",
            }
            or terminal.get("schema_version") != "agent-workflow.app-resume-terminal.vnext.v1"
            or terminal.get("spec_sha256") != spec_sha256
            or terminal.get("session_id") != spec.get("session_id")
            or not isinstance(terminal.get("events"), list)
            or rollout.is_symlink()
            or not rollout.is_file()
            or not rollout.resolve(strict=True).is_relative_to(codex_home)
            or _digest_file(rollout.resolve(strict=True)) != terminal.get("rollout_sha256")
            or not isinstance(turn_claim, dict)
            or set(turn_claim) != expected_turn_claim_keys
            or turn_claim.get("schema_version") != "agent-workflow.app-resume-turn-claim.vnext.v1"
            or turn_claim.get("spec_sha256") != spec_sha256
            or turn_claim.get("session_id") != terminal.get("session_id")
            or turn_claim.get("turn_id") != terminal.get("turn_id")
            or turn_claim.get("prompt_sha256") != _digest(spec.get("prompt", "").encode("utf-8"))
            or turn_claim.get("resume_nonce") != spec.get("resume_nonce")
            or turn_claim.get("audit_marker") != request["audit_marker"]
        ):
            raise RuntimeFailure("reconcile App resume terminal authority drifted")
        try:
            projected_turn, projected_events, projected_context = project_app_resume_turn(
                rollout.resolve(strict=True),
                spec["prior_rollout_size"],
                spec["session_id"],
                spec["prompt"],
            )
            validate_app_resume_context(projected_context, spec)
        except (KeyError, TypeError, ResumeAdapterFailure) as exc:
            raise RuntimeFailure("reconcile App resume raw projection drifted") from exc
        if (
            projected_turn != terminal["turn_id"]
            or projected_events != terminal["events"]
        ):
            raise RuntimeFailure("reconcile App resume terminal differs from raw projection")
        events = projected_events
        stdout = b"".join(_canonical(event) for event in events)
        adapter_recovered = True
    status = receipt["status"]
    cancelled = status == "cancelled" or _cancel_request(root, request["authority_revision"]) is not None
    not_started = status == "not_started_deadline"
    if cancelled:
        event = {"type": "runtime.cancelled", "task_id": request["task_id"], "launched": receipt["exit_code"] is not None}
        events.append(event)
        stdout += _canonical(event)
    if status == "timed_out":
        event = {"type": "runtime.timeout", "task_id": request["task_id"]}
        events.append(event)
        stdout += _canonical(event)
    if not_started:
        event = {"type": "runtime.not_started_deadline", "task_id": request["task_id"]}
        events.append(event)
        stdout += _canonical(event)
    if status == "escaped_process_detected":
        event = {"type": "runtime.escaped_process_detected", "task_id": request["task_id"]}
        events.append(event)
        stdout += _canonical(event)
    thread_ids = [event.get("thread_id") for event in events if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str)]
    thread_id = thread_ids[0] if len(thread_ids) == 1 else None
    codex_home = Path(request["environment"]["CODEX_HOME"]).resolve()
    worker_root = Path(request["cwd"]).resolve()
    context = _find_turn_context(codex_home, thread_id) if thread_id else None
    permission_failure = (
        attest_writer_permissions(
            context,
            worker_root,
            codex_home,
            tuple(request["write_roots"]),
            binary,
        )
        if request["work_mode"] == "write"
        else _attest_worker_permissions(context, worker_root, codex_home, binary)
    )
    producer_failure = (
        (receipt["producer"] == "reconciler" or status == "failed")
        and not adapter_recovered
    )
    return RawExecution(
        exit_code=0 if adapter_recovered else receipt["exit_code"] if isinstance(receipt["exit_code"], int) else 1,
        events=events,
        stderr=stderr.decode("utf-8", errors="replace") + (f"\npermission attestation failed: {permission_failure}" if permission_failure else ""),
        turn_context=context,
        stdout_bytes=stdout,
        adapter_error=producer_failure or permission_failure is not None,
        log_limit_exceeded=receipt["log_limit_exceeded"],
        cancelled=cancelled,
        not_started_deadline=not_started,
        observed_started_at=_parse_timestamp(receipt["started_at"], "supervisor receipt started_at"),
        observed_finished_at=_parse_timestamp(receipt["finished_at"], "supervisor receipt finished_at"),
    )


def _route_from_execution(
    raw: RawExecution,
    *,
    thread_id: str | None,
) -> tuple[dict[str, Any] | None, bytes]:
    context = raw.turn_context if isinstance(raw.turn_context, dict) else {}
    payload = _canonical(context)
    model = context.get("model")
    effort = context.get("effort", context.get("reasoning_effort"))
    context_session = context.get("session_id")
    if not all(isinstance(item, str) and item for item in (model, effort, context_session)):
        return None, payload
    if thread_id is None or context_session != thread_id:
        return None, payload
    return {
        "model": model,
        "reasoning_effort": effort,
        "session_id": context_session,
        "attestation_ref": "",
        "attestation_sha256": _digest(payload),
    }, payload


def _prepare_result(
    workflow: dict[str, Any],
    plan: dict[str, Any],
    task: dict[str, Any],
    packet: dict[str, Any],
    output_schema: dict[str, Any],
    execute: TaskExecutor,
    phase_deadline_monotonic: float,
) -> _PreparedResult:
    started = _now()
    remaining_seconds = phase_deadline_monotonic - time.monotonic()
    if remaining_seconds <= 0:
        deadline_event = {"type": "runtime.not_started_deadline", "task_id": task["task_id"]}
        raw = RawExecution(
            exit_code=0,
            events=[deadline_event],
            stderr="",
            turn_context=None,
            stdout_bytes=_canonical(deadline_event),
            not_started_deadline=True,
        )
    else:
        runtime_task = dict(task)
        runtime_timeout_seconds = min(
            float(task["execution_deadline_seconds"]),
            remaining_seconds,
        )
        runtime_task["_runtime_timeout_seconds"] = runtime_timeout_seconds
        runtime_task["_runtime_deadline_at"] = _timestamp(
            _now() + timedelta(seconds=runtime_timeout_seconds)
        )
        runtime_task["_runtime_deadline_monotonic"] = min(
            phase_deadline_monotonic,
            time.monotonic() + float(task["execution_deadline_seconds"]),
        )
        runtime_task["_runtime_generation_id"] = plan["generation_id"]
        runtime_task["_runtime_phase_id"] = plan["phase_id"]
        try:
            raw = execute(runtime_task, packet)
            if not isinstance(raw, RawExecution):
                raise RuntimeFailure("executor returned an invalid observation")
        except Exception as exc:  # adapter failures become typed task failures
            raw = RawExecution(
                exit_code=1,
                events=[{"type": "runtime.error", "message": str(exc)}],
                stderr=str(exc),
                turn_context=None,
                adapter_error=True,
            )
    if raw.observed_started_at is not None:
        started = raw.observed_started_at
    finished = raw.observed_finished_at or _now()
    elapsed_ms = max(0, int((finished - started).total_seconds() * 1000))
    events_payload = (
        raw.stdout_bytes
        if isinstance(raw.stdout_bytes, bytes)
        else b"".join(_canonical(event) for event in raw.events)
    )
    base = f"phases/{plan['phase_id']}/tasks/{task['task_id']}"
    events_ref = f"{base}/attempts/001/events.jsonl"
    attestation_ref = f"{base}/attempts/001/turn-context.json"
    output_ref = f"{base}/output.json"

    thread_ids = [
        event.get("thread_id")
        for event in raw.events
        if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str)
    ]
    thread_id = thread_ids[0] if len(thread_ids) == 1 else None
    route, attestation_payload = _route_from_execution(raw, thread_id=thread_id)
    if route is not None:
        route["attestation_ref"] = attestation_ref

    completed_events = [event for event in raw.events if event.get("type") == "turn.completed"]
    failed_events = [event for event in raw.events if event.get("type") == "turn.failed"]
    terminal_valid = len(completed_events) + len(failed_events) == 1
    output_payload: bytes | None = None
    status = "failed"
    terminal_reason = "runner_error"
    token_usage: dict[str, Any] = {
        "input": None,
        "output": None,
        "total": None,
        "source": "unavailable",
        "confidence": "partial",
    }

    if len(completed_events) == 1:
        usage = completed_events[0].get("usage")
        if isinstance(usage, dict):
            input_tokens = usage.get("input_tokens")
            output_tokens = usage.get("output_tokens")
            if all(isinstance(item, int) and not isinstance(item, bool) and item >= 0 for item in (input_tokens, output_tokens)):
                token_usage = {
                    "input": input_tokens,
                    "output": output_tokens,
                    "total": input_tokens + output_tokens,
                    "source": "codex_terminal_events",
                    "confidence": "exact",
                }

    expected_model = workflow["routing"][f"{task['role']}_model"]
    expected_effort = workflow["routing"]["reasoning_effort"]
    timed_out = any(event.get("type") == "runtime.timeout" for event in raw.events)
    escaped_process = any(
        event.get("type") == "runtime.escaped_process_detected" for event in raw.events
    )
    if raw.interrupted_before_launch:
        status = "not_started_interrupted"
        terminal_reason = "runner_interrupted_before_launch"
        token_usage = {
            "input": 0,
            "output": 0,
            "total": 0,
            "source": "no_session",
            "confidence": "exact",
        }
    elif raw.not_started_deadline:
        status = "not_started_deadline"
        terminal_reason = "queue_deadline"
        token_usage = {
            "input": 0,
            "output": 0,
            "total": 0,
            "source": "no_session",
            "confidence": "exact",
        }
    elif raw.cancelled:
        status = "cancelled"
        terminal_reason = "user_cancelled"
    elif timed_out:
        status = "timed_out"
        terminal_reason = "execution_deadline"
    elif escaped_process:
        status = "escaped_process_detected"
        terminal_reason = "escaped_process_detected"
    elif raw.adapter_error or raw.log_limit_exceeded:
        status = "failed"
        terminal_reason = "runner_error"
    elif not terminal_valid:
        status = "failed"
        terminal_reason = "runner_error"
    elif route is None:
        status = "route_attestation_failed"
        terminal_reason = "attestation_missing"
    elif route["model"] != expected_model or route["reasoning_effort"] != expected_effort:
        status = "route_attestation_failed"
        terminal_reason = "route_mismatch"
    elif failed_events:
        status = "failed"
        terminal_reason = "codex_turn_failed"
    elif raw.exit_code != 0 or token_usage["confidence"] != "exact":
        status = "failed"
        terminal_reason = "runner_error"
    else:
        messages = [
            event.get("item", {}).get("text")
            for event in raw.events
            if event.get("type") == "item.completed"
            and isinstance(event.get("item"), dict)
            and event["item"].get("type") == "agent_message"
            and isinstance(event["item"].get("text"), str)
        ]
        try:
            output = _strict_json_loads(messages[-1]) if messages else None
        except ValueError:
            output = None
        if not isinstance(output, dict):
            status = "failed"
            terminal_reason = "invalid_typed_output"
        else:
            try:
                _validate_typed_output(output, output_schema)
            except RuntimeFailure:
                status = "failed"
                terminal_reason = "invalid_typed_output"
            else:
                output_payload = _canonical(output)
                status = "completed"
                terminal_reason = "typed_output_validated"

    if status != "completed":
        output_ref_value: str | None = None
        output_sha256: str | None = None
    else:
        output_ref_value = output_ref
        output_sha256 = _digest(output_payload)

    result = {
        "schema_version": "agent-workflow.task-result.v1",
        "workflow_id": workflow["workflow_id"],
        "phase_id": plan["phase_id"],
        "task_id": task["task_id"],
        "lineage_id": task["lineage_id"],
        "attempt": 1,
        "status": status,
        "terminal_reason": terminal_reason,
        "actual_route": route,
        "output_ref": output_ref_value,
        "output_sha256": output_sha256,
        "evidence_refs": [events_ref],
        "evidence_sha256": {events_ref: _digest(events_payload)},
        "checks": [],
        "changed_paths": [],
        "started_at": _timestamp(started),
        "finished_at": _timestamp(finished),
        "elapsed_ms": elapsed_ms,
        "token_usage": token_usage,
    }
    try:
        validate_contract("task-result", result)
    except ProtocolError as exc:
        raise RuntimeFailure(f"runtime produced an invalid task result for {task['task_id']}: {exc}") from exc
    return _PreparedResult(task["task_id"], result, events_payload, attestation_payload, output_payload)


@shared_authority_transaction
def run_read_only_phase(
    root: Path,
    plan: dict[str, Any],
    execute: TaskExecutor,
    *,
    max_parallel: int,
    terminal_fence: TerminalFence | None = None,
    reconciled_executions: dict[str, RawExecution] | None = None,
    source_phase: SourcePhase | None = None,
    runtime_task_overrides: dict[str, dict[str, Any]] | None = None,
    read_snapshot_check: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute one causally admitted Phase and return one terminal mechanical summary."""

    root = Path(root)
    if (root / "final.json").exists() or (root / "final.json").is_symlink():
        raise RuntimeFailure("final seal rejects a later phase")
    workflow = _load_fixed_json(root, "workflow.json", "workflow seal")
    try:
        validate_contract("workflow", workflow)
        validate_contract("phase-plan", plan)
    except ProtocolError as exc:
        raise RuntimeFailure(str(exc)) from exc
    criterion_ids = {item["id"] for item in workflow["success_criteria"]}
    if any(task["criterion_id"] not in criterion_ids for task in plan["tasks"]):
        raise RuntimeFailure("phase task references an unknown success criterion")
    reconciling = reconciled_executions is not None
    current_receipt = root / "phases" / plan["phase_id"] / "receipt.json"
    if not reconciling and (current_receipt.exists() or current_receipt.is_symlink()):
        if not plan["caused_by"]:
            raise RuntimeFailure("exactly one initial phase execution is permitted")
        raise RuntimeFailure("phase is already terminal and immutable")
    try:
        phase_authority = prepare_phase_authority(
            root,
            workflow,
            plan,
            reconciling=reconciling,
        )
    except RecoveryError as exc:
        raise RuntimeFailure(str(exc)) from exc
    source_writing = source_phase is not None
    if source_writing:
        if workflow["admission"]["profile"] != "source_write":
            raise RuntimeFailure("write tasks require source_write admission")
        if any(task["work_mode"] != "write" for task in plan["tasks"]):
            raise RuntimeFailure("source-writing Phase cannot mix read and write tasks")
        if source_phase.phase_id != plan["phase_id"] or set(source_phase.tasks) != {
            task["task_id"] for task in plan["tasks"]
        }:
            raise RuntimeFailure("isolated source phase does not match the phase plan")
    elif any(task["work_mode"] != "read" for task in plan["tasks"]):
        raise RuntimeFailure("read-only tracer rejects write tasks without an isolated source phase")
    if not isinstance(max_parallel, int) or isinstance(max_parallel, bool) or max_parallel < 1:
        raise RuntimeFailure("max_parallel must be a positive integer")
    if _cancel_request(root, plan["authority_revision"]) is not None and reconciled_executions is None:
        raise RuntimeFailure("cancel fence rejects phase launch")
    admitted = min(
        max_parallel,
        workflow["limits"]["max_parallel_tasks"],
        workflow["admission"]["host_capacity"]["max_parallel_tasks"],
    )
    packets = {task["task_id"]: _load_packet(root, task) for task in plan["tasks"]}
    if source_writing:
        for task in plan["tasks"]:
            _validate_source_writer_schema(packets[task["task_id"]][1], task["task_id"])
        if not reconciling:
            try:
                revalidate_isolated_phase_launch(source_phase)
            except SourceWriteError as exc:
                raise RuntimeFailure(f"source-write launch rejected: {exc}") from exc
    plan_payload = _canonical(plan)
    plan_ref = f"phases/{plan['phase_id']}/plan.json"
    if reconciling:
        plan_path = root / plan_ref
        claim_ref = _generation_claim_ref(plan)
        claim_path = root / claim_ref
        if (
            plan_path.is_symlink()
            or not plan_path.is_file()
            or plan_path.read_bytes() != plan_payload
            or claim_path.is_symlink()
            or not claim_path.is_file()
        ):
            raise RuntimeFailure("reconcile phase plan or generation claim is missing or drifted")
        claim = json.loads(claim_path.read_bytes())
        if (
            claim.get("generation_id") != plan["generation_id"]
            or claim.get("plan_sha256") != _digest(plan_payload)
        ):
            raise RuntimeFailure("reconcile generation claim does not bind the phase plan")
        if set(reconciled_executions) != {task["task_id"] for task in plan["tasks"]}:
            raise RuntimeFailure("reconcile terminal executions do not cover the phase tasks")
    else:
        try:
            plan_path = create_once_json(root, plan_ref, plan)
        except ArtifactError as exc:
            raise RuntimeFailure(str(exc)) from exc
        claim_ref, claim_path = _seal_generation_claim(root, workflow, plan, plan_payload)
    try:
        commit_phase_authority(root, phase_authority)
    except RecoveryError as exc:
        raise RuntimeFailure(str(exc)) from exc
    if plan_path.read_bytes() != plan_payload:
        raise RuntimeFailure("persisted phase plan bytes drifted from the generation claim")
    deadline_seal, phase_deadline_monotonic = _seal_deadlines(
        root,
        workflow,
        plan,
        allow_expired=reconciling,
    )
    if reconciling:
        phase_deadline_monotonic = time.monotonic() + 60.0
    private_fence = {
        "_runtime_plan_sha256": _digest(plan_payload),
        "_runtime_generation_claim_ref": claim_ref,
        "_runtime_generation_claim_sha256": _digest(claim_path.read_bytes()),
        "_runtime_bundle_sha256": _runtime_bundle_sha256(),
        "_runtime_boot_identity": deadline_seal["boot_identity"],
    }
    overrides = runtime_task_overrides or {}
    if set(overrides) - {task["task_id"] for task in plan["tasks"]}:
        raise RuntimeFailure("runtime task overrides contain an unknown task")
    dispatched_tasks = [
        {**task, **private_fence, **overrides.get(task["task_id"], {})}
        for task in plan["tasks"]
    ]

    def source_launch_fence() -> None:
        if source_phase is None:
            return
        try:
            revalidate_isolated_phase_launch(source_phase)
        except SourceWriteError as exc:
            raise RuntimeFailure(f"source-write launch rejected: {exc}") from exc

    if source_writing and not reconciling:
        for task in dispatched_tasks:
            task["_runtime_source_launch_fence"] = source_launch_fence

    if reconciling:
        phase_execute = lambda task, _packet: reconciled_executions[task["task_id"]]
    elif source_writing:
        def phase_execute(task: dict[str, Any], packet: dict[str, Any]) -> RawExecution:
            source_launch_fence()
            return execute(task, packet)
    else:
        phase_execute = execute
    with ThreadPoolExecutor(max_workers=admitted, thread_name_prefix="vnext-read") as pool:
        futures = {
            task["task_id"]: pool.submit(
                _prepare_result,
                workflow,
                plan,
                task,
                packets[task["task_id"]][0],
                packets[task["task_id"]][1],
                phase_execute,
                phase_deadline_monotonic,
            )
            for task in dispatched_tasks
        }
        prepared = [futures[task["task_id"]].result() for task in dispatched_tasks]

    if terminal_fence is not None:
        terminal_fence()
    if _cancel_request(root, plan["authority_revision"]) is not None:
        if any(item.result["status"] != "cancelled" for item in prepared):
            raise RuntimeFailure("cancel fence rejects non-cancelled result publication")

    integration = {
        "mode": "none",
        "status": "not_applicable",
        "patch_ref": None,
        "patch_sha256": None,
        "target_before": {},
        "target_after": {},
    }
    if read_snapshot_check is not None:
        if source_phase is not None:
            raise RuntimeFailure("read snapshot evidence cannot accompany source integration")
        for item in prepared:
            if item.result["status"] != "completed":
                continue
            item.result["checks"] = [*item.result["checks"], dict(read_snapshot_check)]
            try:
                validate_contract("task-result", item.result)
            except ProtocolError as exc:
                raise RuntimeFailure(
                    f"read snapshot produced an invalid task result for {item.task_id}: {exc}"
                ) from exc
    if source_phase is not None:
        dependency_conflict = False
        if not reconciling:
            try:
                revalidate_isolated_phase_launch(source_phase)
            except SourceWriteError:
                dependency_conflict = True
        completed_task_ids = {
            item.task_id for item in prepared if item.result["status"] == "completed"
        }
        declared_changed_paths: dict[str, list[str]] = {}
        for item in prepared:
            if item.result["status"] != "completed":
                continue
            try:
                output = json.loads(item.output_payload) if item.output_payload is not None else None
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeFailure("source writer typed output is unavailable") from exc
            declared = output.get("changed_paths") if isinstance(output, dict) else None
            if (
                not isinstance(declared, list)
                or not all(isinstance(path, str) and path for path in declared)
                or len(declared) != len(set(declared))
            ):
                raise RuntimeFailure("source writer changed_paths declaration is invalid")
            declared_changed_paths[item.task_id] = declared

        source_dependency_fence_calls = 0

        def pre_apply_fence() -> None:
            nonlocal source_dependency_fence_calls
            if _cancel_request(root, plan["authority_revision"]) is not None:
                raise SourceWriteError("cancel fence rejects source integration")
            if not reconciling:
                revalidate_isolated_phase_launch(
                    source_phase,
                    allow_integrated_anchor=source_dependency_fence_calls > 0,
                )
                source_dependency_fence_calls += 1
        try:
            outcome = integrate_isolated_phase(
                source_phase,
                completed_task_ids=completed_task_ids,
                apply=(
                    len(completed_task_ids) == len(prepared)
                    and not dependency_conflict
                ),
                pre_apply_fence=pre_apply_fence,
                declared_changed_paths=declared_changed_paths,
            )
            if dependency_conflict:
                outcome["status"] = "conflict"
        except SourceWriteError as exc:
            raise RuntimeFailure(f"source integration failed closed: {exc}") from exc
        for item in prepared:
            item.result["changed_paths"] = outcome["changed_by_task"].get(item.task_id, [])
            if item.result["status"] == "completed":
                item.result["checks"] = [
                    {
                        "name": "host_changed_path_and_bounded_patch_audit",
                        "exit_code": 0,
                        "evidence_ref": outcome["patch_ref"],
                        "evidence_sha256": outcome["patch_sha256"],
                    }
                ]
            if outcome["status"] == "conflict" and item.result["status"] == "completed":
                item.result["status"] = "concurrent_edit_conflict"
                item.result["terminal_reason"] = "source_drift"
            try:
                validate_contract("task-result", item.result)
            except ProtocolError as exc:
                raise RuntimeFailure(
                    f"source integration produced an invalid task result for {item.task_id}: {exc}"
                ) from exc
        integration = {
            key: outcome[key]
            for key in (
                "mode",
                "status",
                "patch_ref",
                "patch_sha256",
                "target_before",
                "target_after",
            )
        }

    result_refs: list[str] = []
    result_digests: dict[str, str] = {}
    counts = {status: 0 for status in TASK_TERMINAL_STATUSES}
    for item in prepared:
        base = f"phases/{plan['phase_id']}/tasks/{item.task_id}"
        events_ref = f"{base}/attempts/001/events.jsonl"
        attestation_ref = f"{base}/attempts/001/turn-context.json"
        output_ref = f"{base}/output.json"
        result_ref = f"{base}/result.json"
        try:
            publish_bytes = _create_or_verify_bytes if reconciling else create_once_bytes
            publish_json = _create_or_verify_json if reconciling else create_once_json
            publish_bytes(root, events_ref, item.events_payload)
            publish_bytes(root, attestation_ref, item.attestation_payload)
            if item.output_payload is not None:
                publish_bytes(root, output_ref, item.output_payload)
            result_path = publish_json(root, result_ref, item.result)
        except ArtifactError as exc:
            raise RuntimeFailure(str(exc)) from exc
        result_refs.append(result_ref)
        result_digests[result_ref] = _digest(result_path.read_bytes())
        counts[item.result["status"]] += 1

    completed = counts["completed"]
    total = len(prepared)
    if source_phase is not None and integration["status"] == "conflict":
        phase_status = "blocked"
        terminal_reason = "integration_conflict"
    elif counts["cancelled"]:
        phase_status = "cancelled"
        terminal_reason = "phase_cancelled"
    elif source_phase is not None and completed != total:
        phase_status = "failed"
        terminal_reason = "integration_failed"
    elif completed == total:
        phase_status = "completed"
        terminal_reason = "all_tasks_terminal"
    elif completed:
        phase_status = "completed_with_failures"
        terminal_reason = "task_failures_terminal"
    else:
        phase_status = "failed"
        terminal_reason = "task_failures_terminal"
    receipt_counts = {**counts, "total": total}
    receipt = {
        "schema_version": "agent-workflow.phase-receipt.v1",
        "workflow_id": workflow["workflow_id"],
        "phase_id": plan["phase_id"],
        "generation_id": plan["generation_id"],
        "generation_claim_ref": claim_ref,
        "generation_claim_sha256": _digest(claim_path.read_bytes()),
        "plan_sha256": _digest(plan_payload),
        "predecessor_sha256": plan["predecessor_sha256"],
        "status": phase_status,
        "task_result_refs": result_refs,
        "task_result_sha256": result_digests,
        "task_counts": receipt_counts,
        "integration": integration,
        "terminal_reason": terminal_reason,
        "created_at": _timestamp(_now()),
    }
    try:
        validate_contract("phase-receipt", receipt)
        receipt_ref = f"phases/{plan['phase_id']}/receipt.json"
        receipt_path = create_once_json(root, receipt_ref, receipt)
    except (ProtocolError, ArtifactError) as exc:
        raise RuntimeFailure(str(exc)) from exc
    routes: dict[str, dict[str, str]] = {}
    exact_input = 0
    exact_output = 0
    exact_accounting = True
    for item in prepared:
        route = item.result["actual_route"]
        if isinstance(route, dict):
            routes[item.task_id] = {
                "model": route["model"],
                "reasoning_effort": route["reasoning_effort"],
                "session_id": route["session_id"],
            }
        usage = item.result["token_usage"]
        if usage["confidence"] != "exact":
            exact_accounting = False
        else:
            exact_input += usage["input"]
            exact_output += usage["output"]
    return {
        "status": phase_status,
        "receipt_ref": receipt_ref,
        "receipt_sha256": _digest(receipt_path.read_bytes()),
        "worker_count": total,
        "max_parallel_admitted": admitted,
        "receipt_count": 1,
        "task_counts": receipt_counts,
        "terminal_reason": terminal_reason,
        "generation_claim_ref": claim_ref,
        "generation_claim_sha256": _digest(claim_path.read_bytes()),
        "routes": routes,
        "external_token_subtotal": {
            "source": "codex_terminal_events" if exact_accounting else "mixed",
            "confidence": "exact" if exact_accounting else "partial",
            "input": exact_input if exact_accounting else None,
            "output": exact_output if exact_accounting else None,
            "total": exact_input + exact_output if exact_accounting else None,
        },
        "completion_density_source": "host_raw_session_audit_required",
    }


def _load_source(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeFailure(f"{label} is not readable JSON: {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeFailure(f"{label} must be an object")
    return value


_READ_ONLY_PROFILE = b'''default_permissions = "vnext_read_only"\n\n[permissions.vnext_read_only]\ndescription = "Agent Workflow vNext least-privilege read-only worker"\n\n[permissions.vnext_read_only.filesystem]\n":minimal" = "read"\n\n[permissions.vnext_read_only.filesystem.":workspace_roots"]\n"." = "read"\n\n[permissions.vnext_read_only.network]\nenabled = false\n'''


def _recovery_resume_binding(
    root: Path,
    plan: dict[str, Any],
    task: dict[str, Any],
) -> dict[str, Any] | None:
    """Resolve a recovery continuation from exact failed-result and watchdog evidence."""

    root = Path(root).resolve()
    candidates: list[tuple[str, dict[str, Any]]] = []
    for ref in task["input_refs"]:
        if not re.fullmatch(r"phases/[^/]+/tasks/[^/]+/result\.json", ref):
            continue
        result = _load_fixed_json(root, ref, "recovery failed result")
        try:
            validate_contract("task-result", result)
        except ProtocolError as exc:
            raise RuntimeFailure("recovery failed result contract is invalid") from exc
        if result["lineage_id"] != task["lineage_id"]:
            continue
        if task["input_sha256"].get(ref) != _digest((root / ref).read_bytes()):
            raise RuntimeFailure("recovery failed result input digest drifted")
        candidates.append((ref, result))
    if not candidates:
        return None
    if len(candidates) != 1:
        raise RuntimeFailure("recovery resume must bind exactly one failed lineage result")
    failed_ref, failed = candidates[0]
    if failed["status"] in {"completed", "cancelled"}:
        raise RuntimeFailure("recovery resume cannot continue a successful or cancelled lineage")
    if not plan["caused_by"]:
        raise RuntimeFailure("recovery resume lacks a causal phase boundary")
    causal_ref = f"phases/{plan['caused_by'][-1]}/receipt.json"
    if (
        causal_ref not in task["input_refs"]
        or task["input_sha256"].get(causal_ref) != _digest((root / causal_ref).read_bytes())
    ):
        raise RuntimeFailure("recovery resume causal receipt drifted")
    route = failed["actual_route"]
    if route is None:
        return None
    if task["role"] != "worker":
        raise RuntimeFailure("only a failed worker lineage may resume")
    session_id = route["session_id"]
    if not re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        session_id,
    ):
        raise RuntimeFailure("failed lineage session id is not resumable")
    request_ref = f"runtime/watchdogs/{failed['phase_id']}/{failed['task_id']}/request.json"
    try:
        request, _ = load_supervisor_request(root, request_ref, enforce_boot=False)
    except SupervisorFailure as exc:
        raise RuntimeFailure("failed lineage watchdog request is invalid") from exc
    codex_home_value = request["environment"].get("CODEX_HOME")
    if not isinstance(codex_home_value, str):
        raise RuntimeFailure("failed lineage watchdog lacks Codex home authority")
    codex_home = Path(codex_home_value).resolve()
    homes_root = (root / "runtime/codex-homes").resolve()
    if (
        not codex_home.is_relative_to(homes_root)
        or codex_home.parent != homes_root
        or request["phase_id"] != failed["phase_id"]
        or request["task_id"] != failed["task_id"]
    ):
        raise RuntimeFailure("failed lineage Codex home authority drifted")
    events_ref = failed["evidence_refs"][0]
    events_payload = _read_artifact_bytes(
        root,
        events_ref,
        failed["evidence_sha256"][events_ref],
    )
    thread_ids = {
        event.get("thread_id")
        for event in _parse_jsonl(events_payload)
        if event.get("type") == "thread.started"
        and isinstance(event.get("thread_id"), str)
    }
    context = _find_turn_context(codex_home, session_id)
    rollout = _find_session_rollout(codex_home, session_id)
    if (
        thread_ids != {session_id}
        or context is None
        or rollout is None
        or context["session_id"] != session_id
        or context["model"] != route["model"]
        or context["effort"] != route["reasoning_effort"]
    ):
        raise RuntimeFailure("failed lineage raw session authority drifted")
    return {
        "failed_result_ref": failed_ref,
        "failed_result_sha256": _digest((root / failed_ref).read_bytes()),
        "causal_receipt_ref": causal_ref,
        "causal_receipt_sha256": _digest((root / causal_ref).read_bytes()),
        "session_id": session_id,
        "codex_home": os.fspath(codex_home),
        "session_rollout_path": os.fspath(rollout),
        "prior_rollout_sha256": _digest_file(rollout),
        "prior_rollout_size": rollout.stat().st_size,
    }


def _prepare_codex_home(
    root: Path,
    auth_source: Path,
    *,
    owner_id: str | None = None,
    writer_roots: tuple[str, ...] | None = None,
) -> Path:
    root = Path(root).resolve()
    auth_source = Path(auth_source)
    if auth_source.is_symlink() or not auth_source.is_file():
        raise RuntimeFailure("auth source must be a regular file")
    auth_payload = auth_source.read_bytes()
    if not auth_payload or len(auth_payload) > 1024 * 1024:
        raise RuntimeFailure("auth source size is invalid")
    try:
        auth = json.loads(auth_payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeFailure("auth source is invalid JSON") from exc
    access_token = auth.get("tokens", {}).get("access_token") if isinstance(auth, dict) else None
    api_key = auth.get("OPENAI_API_KEY") if isinstance(auth, dict) else None
    has_supported_credential = (
        (isinstance(access_token, str) and bool(access_token))
        or (isinstance(api_key, str) and bool(api_key))
    )
    if not has_supported_credential:
        raise RuntimeFailure("auth source has no supported Codex credential")
    if owner_id is not None and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", owner_id):
        raise RuntimeFailure("transient Codex auth owner id is invalid")
    codex_home = (
        root / "runtime" / "codex-homes" / owner_id
        if owner_id is not None
        else root / "runtime" / "codex-home"
    )
    codex_home_ref = codex_home.relative_to(root).as_posix()
    try:
        profile = codex_home / "vnext-read-only.config.toml"
        if profile.exists():
            if profile.is_symlink() or profile.read_bytes() != _READ_ONLY_PROFILE:
                raise RuntimeFailure("read-only permission profile drifted")
        else:
            create_once_bytes(root, f"{codex_home_ref}/vnext-read-only.config.toml", _READ_ONLY_PROFILE)
        if writer_roots is not None:
            writer_profile = codex_home / "vnext-writer.config.toml"
            writer_payload = writer_profile_bytes(writer_roots)
            if writer_profile.exists():
                if writer_profile.is_symlink() or writer_profile.read_bytes() != writer_payload:
                    raise RuntimeFailure("writer permission profile drifted")
            else:
                create_once_bytes(
                    root,
                    f"{codex_home_ref}/vnext-writer.config.toml",
                    writer_payload,
                )
        base_payload = writer_payload if writer_roots is not None else _READ_ONLY_PROFILE
        base_config = codex_home / "config.toml"
        if base_config.exists():
            if base_config.is_symlink() or base_config.read_bytes() != base_payload:
                raise RuntimeFailure("recovery-capable Codex base config drifted")
        else:
            create_once_bytes(root, f"{codex_home_ref}/config.toml", base_payload)
        (codex_home / "home").mkdir(mode=0o700, exist_ok=True)
        (codex_home / "tmp").mkdir(mode=0o700, exist_ok=True)
    except ArtifactError as exc:
        raise RuntimeFailure(str(exc)) from exc
    auth_path = codex_home / "auth.json"
    if auth_path.exists() or auth_path.is_symlink():
        _scrub_stale_codex_auth(root, codex_home=codex_home)
    if auth_path.exists() or auth_path.is_symlink():
        raise RuntimeFailure("transient Codex auth path must start absent")
    try:
        created = create_once_bytes(root, f"{codex_home_ref}/auth.json", auth_payload)
        if created.stat().st_mode & 0o077:
            raise RuntimeFailure("transient Codex auth permissions are too broad")
    except ArtifactError as exc:
        raise RuntimeFailure(str(exc)) from exc
    return codex_home


def _scrub_stale_codex_auth(root: Path, *, codex_home: Path | None = None) -> bool:
    """Remove only a 0600 same-owner auth copy with no live unfinished watchdog."""

    root = Path(root).resolve()
    candidates = (
        [Path(codex_home).resolve() / "auth.json"]
        if codex_home is not None
        else [root / "runtime" / "codex-home" / "auth.json"]
        + sorted((root / "runtime" / "codex-homes").glob("*/auth.json"))
    )
    present = [path for path in candidates if path.exists() or path.is_symlink()]
    if not present:
        return False
    records_root = root / "runtime" / "processes"
    records = sorted(records_root.rglob("*.json")) if records_root.is_dir() else []
    for path in records:
        if path.is_symlink() or not path.is_file():
            raise RuntimeFailure("active process record is unsafe during auth recovery")
        record = json.loads(path.read_bytes())
        terminal_ref = record.get("terminal_ref") if isinstance(record, dict) else None
        if isinstance(terminal_ref, str) and (root / terminal_ref).is_file():
            continue
        supervisor_pid = record.get("supervisor_pid") if isinstance(record, dict) else None
        supervisor_birth = record.get("supervisor_birth") if isinstance(record, dict) else None
        worker_pid = record.get("pid") if isinstance(record, dict) else None
        worker_birth = record.get("process_birth") if isinstance(record, dict) else None
        if (
            isinstance(supervisor_pid, int)
            and isinstance(supervisor_birth, str)
            and process_birth(supervisor_pid) == supervisor_birth
        ) or (
            isinstance(worker_pid, int)
            and isinstance(worker_birth, str)
            and process_birth(worker_pid) == worker_birth
        ):
            raise RuntimeFailure("cannot scrub transient auth while an owned attempt is active")
    requests_root = root / "runtime" / "watchdogs"
    request_paths = sorted(requests_root.rglob("request.json")) if requests_root.is_dir() else []
    for request_path in request_paths:
        request_ref = request_path.relative_to(root).as_posix()
        request, _ = load_supervisor_request(root, request_ref, enforce_boot=False)
        terminal_path = root / request["receipt_ref"]
        if terminal_path.is_file() and not terminal_path.is_symlink():
            continue
        if (
            request["boot_identity"] == _process_identity(1)
            and time.monotonic() < float(request["deadline_monotonic"])
        ):
            raise RuntimeFailure(
                "cannot scrub transient auth while a sealed watchdog launch is nonterminal"
            )
    for auth_path in present:
        if auth_path.is_symlink() or not auth_path.is_file():
            raise RuntimeFailure("stale transient auth path is unsafe")
        metadata = auth_path.stat()
        if metadata.st_uid != os.getuid() or metadata.st_mode & 0o777 != 0o600:
            raise RuntimeFailure("stale transient auth ownership or permissions are unsafe")
        auth_path.unlink()
        parent_fd = os.open(auth_path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    return True


def _cleanup_codex_auth(codex_home: Path) -> None:
    auth_path = codex_home / "auth.json"
    if auth_path.exists() or auth_path.is_symlink():
        if auth_path.is_symlink() or not auth_path.is_file():
            raise RuntimeFailure("transient auth path became unsafe")
        auth_path.unlink()
        descriptor = os.open(codex_home, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _admit_command(
    root: Path,
    repository: Path,
    workflow_source: Path,
    codex_binary: str,
) -> dict[str, Any]:
    workflow = _load_source(workflow_source, "workflow source")
    validate_contract("workflow", workflow)
    repository_root = repository.resolve(strict=True)
    if repository_root != _repository_root_for(root):
        raise RuntimeFailure("admission repository does not own the workflow root")
    codex_path, _, _ = _codex_identity(codex_binary)
    _validate_admission_inputs(
        root,
        workflow,
        repository_root=repository_root,
        codex_binary=os.fspath(codex_path),
        require_host_snapshot_live_state=True,
    )
    pinned_runtime = _seal_runtime_bundle(root)
    path = _create_or_verify_json(root, "workflow.json", workflow)
    return {
        "status": "admitted",
        "workflow_ref": "workflow.json",
        "workflow_sha256": _digest(path.read_bytes()),
        "pinned_runtime_ref": pinned_runtime.relative_to(Path(root).resolve()).as_posix(),
        "runtime_bundle_sha256": workflow["runtime_bundle"]["sha256"],
    }


def _run_phase_command(
    root: Path,
    repository: Path,
    plan_source: Path,
    auth_source: Path,
    codex_binary: str,
    max_parallel: int,
) -> dict[str, Any]:
    plan = _load_source(plan_source, "phase plan source")
    workflow = _load_fixed_json(root, "workflow.json", "workflow seal")
    repository_root = repository.resolve(strict=True)
    if repository_root != _repository_root_for(root):
        raise RuntimeFailure("run repository does not own the workflow root")
    codex_path, _, _ = _codex_identity(codex_binary)
    _validate_admission_inputs(
        root,
        workflow,
        repository_root=repository_root,
        codex_binary=os.fspath(codex_path),
        require_host_snapshot_live_state=False,
    )
    source_writing = any(task["work_mode"] == "write" for task in plan["tasks"])
    if source_writing and any(task["work_mode"] != "write" for task in plan["tasks"]):
        raise RuntimeFailure("source-writing Phase cannot mix read and write tasks")
    for task in plan["tasks"]:
        _packet, schema = _load_packet(root, task)
        if task["work_mode"] == "write":
            _validate_source_writer_schema(schema, task["task_id"])
    try:
        prepare_phase_authority(root, workflow, plan, reconciling=False)
    except RecoveryError as exc:
        raise RuntimeFailure(str(exc)) from exc
    resource_admission = _resource_admission(
        root,
        workflow,
        max_parallel,
        log_limit_bytes=16 * 1024 * 1024,
    )
    resume_bindings = {
        task["task_id"]: _recovery_resume_binding(root, plan, task)
        for task in plan["tasks"]
    }
    bound_homes = [
        binding["codex_home"]
        for binding in resume_bindings.values()
        if binding is not None
    ]
    if len(bound_homes) != len(set(bound_homes)):
        raise RuntimeFailure("one failed lineage session cannot authorize two recovery tasks")
    codex_homes: list[Path] = []
    runtime_overrides: dict[str, dict[str, Any]] = {}
    source_phase: SourcePhase | None = None
    read_snapshot_check: dict[str, Any] | None = None
    try:
        if source_writing:
            try:
                admission_baseline = json.loads(
                    _read_artifact_bytes(
                        root,
                        workflow["baseline_ref"],
                        workflow["baseline_sha256"],
                    )
                )
                source_phase = prepare_isolated_phase(
                    root,
                    repository_root,
                    plan,
                    read_roots=tuple(workflow["admission"]["relevant_roots"]),
                    admission_baseline=admission_baseline,
                    predecessor_sha256=plan["predecessor_sha256"],
                )
            except DirtyOverlap as exc:
                raise HumanGateRequired(str(exc)) from exc
            except (ProtocolError, UnicodeDecodeError, json.JSONDecodeError, SourceWriteError) as exc:
                raise RuntimeFailure(f"source-write admission failed closed: {exc}") from exc
            for task in plan["tasks"]:
                task_workspace = source_phase.tasks[task["task_id"]]
                binding = resume_bindings[task["task_id"]]
                owner = (
                    Path(binding["codex_home"]).name
                    if binding is not None
                    else f"{plan['generation_id']}-{task['task_id']}-{uuid.uuid4().hex}"
                )
                home = _prepare_codex_home(
                    root,
                    auth_source,
                    owner_id=owner,
                    writer_roots=task_workspace.write_roots,
                )
                codex_homes.append(home)
                runtime_overrides[task["task_id"]] = {
                    "_runtime_worker_root": os.fspath(task_workspace.root),
                    "_runtime_codex_home": os.fspath(home),
                    "_runtime_permissions_profile": "vnext-writer",
                    "_runtime_write_roots": list(task_workspace.write_roots),
                }
                if binding is not None:
                    runtime_overrides[task["task_id"]]["_runtime_resume_binding"] = binding
            worker_root = source_phase.tasks[plan["tasks"][0]["task_id"]].root
            codex_home = codex_homes[0]
        else:
            relevant_roots = workflow["admission"]["relevant_roots"]
            try:
                worker_root = prepare_read_only_snapshot(
                    root,
                    repository_root,
                    plan["phase_id"],
                    tuple(relevant_roots),
                )
            except SourceWriteError as exc:
                raise RuntimeFailure(f"read-only snapshot failed closed: {exc}") from exc
            snapshot_manifest_ref = f"runtime/read-snapshots/{plan['phase_id']}/manifest.json"
            snapshot_manifest_path = root / snapshot_manifest_ref
            snapshot_manifest_sha256 = _digest(snapshot_manifest_path.read_bytes())
            read_snapshot_check = {
                "name": "host_read_snapshot_audit",
                "exit_code": 0,
                "evidence_ref": snapshot_manifest_ref,
                "evidence_sha256": snapshot_manifest_sha256,
            }
            for task in plan["tasks"]:
                binding = resume_bindings[task["task_id"]]
                owner = (
                    Path(binding["codex_home"]).name
                    if binding is not None
                    else f"{plan['generation_id']}-{task['task_id']}-{uuid.uuid4().hex}"
                )
                home = _prepare_codex_home(root, auth_source, owner_id=owner)
                codex_homes.append(home)
                runtime_overrides[task["task_id"]] = {
                    "_runtime_worker_root": os.fspath(worker_root),
                    "_runtime_codex_home": os.fspath(home),
                    "_runtime_permissions_profile": "vnext-read-only",
                    "_runtime_write_roots": [],
                }
                if binding is not None:
                    runtime_overrides[task["task_id"]]["_runtime_resume_binding"] = binding
            codex_home = codex_homes[0]
        config = CodexExecConfig(
            run_root=root,
            repo_root=worker_root,
            codex_home=codex_home,
            codex_binary=os.fspath(codex_path),
            top_model=workflow["routing"]["top_model"],
            worker_model=workflow["routing"]["worker_model"],
            reasoning_effort=workflow["routing"]["reasoning_effort"],
            workflow_id=workflow["workflow_id"],
            authority_revision=plan["authority_revision"],
        )
        def terminal_fence() -> None:
            _validate_admission_inputs(
                root,
                workflow,
                repository_root=repository_root,
                codex_binary=os.fspath(codex_path),
                require_host_snapshot_live_state=False,
            )
            try:
                prepare_phase_authority(
                    root,
                    workflow,
                    plan,
                    reconciling=True,
                )
            except RecoveryError as exc:
                raise RuntimeFailure(f"phase authority fence drifted: {exc}") from exc
            if read_snapshot_check is not None:
                try:
                    prepare_read_only_snapshot(
                        root,
                        repository_root,
                        plan["phase_id"],
                        tuple(workflow["admission"]["relevant_roots"]),
                    )
                except SourceWriteError as exc:
                    raise RuntimeFailure(f"read snapshot terminal fence drifted: {exc}") from exc
                manifest_path = root / read_snapshot_check["evidence_ref"]
                if _digest(manifest_path.read_bytes()) != read_snapshot_check["evidence_sha256"]:
                    raise RuntimeFailure("read snapshot terminal manifest digest drifted")

        summary = run_read_only_phase(
            root,
            plan,
            codex_task_executor(config),
            max_parallel=resource_admission["max_parallel_admitted"],
            terminal_fence=terminal_fence,
            source_phase=source_phase,
            runtime_task_overrides=runtime_overrides,
            read_snapshot_check=read_snapshot_check,
        )
        summary["resource_admission"] = resource_admission
    finally:
        for home in codex_homes:
            _cleanup_codex_auth(home)
    process_root = root / "runtime" / "processes"
    process_records = sorted(process_root.rglob("*.json")) if process_root.is_dir() else []
    active_attempts = 0
    for path in process_records:
        record = json.loads(path.read_bytes())
        terminal_ref = record.get("terminal_ref") if isinstance(record, dict) else None
        if not isinstance(terminal_ref, str) or not (root / terminal_ref).is_file():
            active_attempts += 1
    summary["cleanup"] = {
        "transient_auth_removed": all(not (home / "auth.json").exists() for home in codex_homes),
        "active_attempts": active_attempts,
        "process_records_retained": len(process_records),
    }
    rebuild_view(root)
    return summary


def _probe_runtime_refs(phase_id: str, task_id: str) -> dict[str, str]:
    if not phase_id or not task_id or "/" in phase_id or "/" in task_id:
        raise RuntimeFailure("probe runtime identity is invalid")
    base = f"evidence/source-write-probe"
    return {
        "request_ref": f"{base}/runtime/watchdogs/{phase_id}/{task_id}/request.json",
        "terminal_ref": f"{base}/runtime/watchdogs/{phase_id}/{task_id}/terminal.json",
        "events_ref": f"{base}/transient/{phase_id}/{task_id}/stdout.jsonl",
        "stderr_ref": f"{base}/transient/{phase_id}/{task_id}/stderr.log",
    }


def _host_probe_output(raw: RawExecution, role: str) -> tuple[str, dict[str, Any]]:
    if raw.adapter_error or raw.exit_code != 0 or not isinstance(raw.turn_context, dict):
        raise RuntimeFailure(f"host capability {role} probe did not terminalize cleanly")
    thread_ids = [
        event.get("thread_id")
        for event in raw.events
        if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str)
    ]
    completed = [event for event in raw.events if event.get("type") == "turn.completed"]
    messages = [
        event.get("item", {}).get("text")
        for event in raw.events
        if event.get("type") == "item.completed"
        and isinstance(event.get("item"), dict)
        and event["item"].get("type") == "agent_message"
        and isinstance(event["item"].get("text"), str)
    ]
    try:
        output = json.loads(messages[-1]) if messages else None
    except json.JSONDecodeError:
        output = None
    if (
        len(thread_ids) != 1
        or len(completed) != 1
        or output != {"answer": "probe-ok"}
        or raw.turn_context.get("session_id") != thread_ids[0]
    ):
        raise RuntimeFailure(f"host capability {role} probe evidence is incomplete")
    return thread_ids[0], output


def _select_host_probe_attempt(
    root: Path, role: str
) -> tuple[str, RawExecution | None]:
    """Reuse one terminal probe or select the only bounded crash-recovery slot."""

    original_id = f"host-capability-{role}"
    for candidate in (original_id, f"{original_id}-recovery"):
        request_ref = f"runtime/watchdogs/000-host-capability-probe/{candidate}/request.json"
        terminal_ref = f"runtime/watchdogs/000-host-capability-probe/{candidate}/terminal.json"
        request_exists = (root / request_ref).is_file()
        terminal_exists = (root / terminal_ref).is_file()
        if request_exists != terminal_exists:
            if not request_exists or candidate.endswith("-recovery"):
                raise RuntimeFailure(
                    f"host capability {role} partial attempt cannot be recovered"
                )
            continue
        if not request_exists:
            return candidate, None
        observed = _raw_from_supervisor_terminal(root, request_ref, terminal_ref)
        try:
            _host_probe_output(observed, role)
        except RuntimeFailure:
            if candidate.endswith("-recovery"):
                raise RuntimeFailure(f"host capability {role} recovery was already exhausted")
            continue
        return candidate, observed
    raise RuntimeFailure(f"host capability {role} recovery was already exhausted")


def _run_host_read_only_denials(
    root: Path,
    codex_path: Path,
    codex_home: Path,
) -> dict[str, int]:
    probe = root / "evidence" / "host-capability-probe" / "denial-workspace"
    sibling = root / "evidence" / "host-capability-probe" / "denial-sibling"
    probe.mkdir(parents=True, exist_ok=True)
    sibling.mkdir(parents=True, exist_ok=True)
    readable = probe / "readable.txt"
    readable.write_text("read-only-probe\n")
    sibling_file = sibling / "readable.txt"
    sibling_file.write_text("sibling\n")
    control = root / "evidence" / "host-capability-probe" / "control-secret.txt"
    control.write_text("control\n")
    credential = codex_home / "auth.json"
    shell = (
        f"/bin/cat {shlex.quote(os.fspath(readable))} >/dev/null 2>&1; rr=$?; "
        "printf denied > should-not-write.txt 2>/dev/null; ww=$?; "
        f"/bin/cat {shlex.quote(os.fspath(sibling_file))} >/dev/null 2>&1; sr=$?; "
        f"/bin/cat {shlex.quote(os.fspath(control))} >/dev/null 2>&1; cr=$?; "
        f"/bin/cat {shlex.quote(os.fspath(credential))} >/dev/null 2>&1; ar=$?; "
        "/usr/bin/curl -m 1 -fsS http://1.1.1.1 >/dev/null 2>&1; nr=$?; "
        "printf 'workspace_read_exit=%s workspace_write_exit=%s sibling_read_exit=%s "
        "control_read_exit=%s credential_read_exit=%s network_exit=%s\\n' "
        '"$rr" "$ww" "$sr" "$cr" "$ar" "$nr"'
    )
    completed = subprocess.run(
        [
            os.fspath(codex_path),
            "sandbox",
            "-p",
            "vnext-read-only",
            "-P",
            "vnext_read_only",
            "-C",
            os.fspath(probe),
            "/bin/sh",
            "-c",
            shell,
        ],
        env={**os.environ, "CODEX_HOME": os.fspath(codex_home)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode:
        raise RuntimeFailure("host capability read-only sandbox probe failed to execute")
    try:
        values = dict(
            item.split("=", 1)
            for item in completed.stdout.decode("utf-8").strip().splitlines()[-1].split()
        )
        report = {key: int(value) for key, value in values.items()}
    except (IndexError, ValueError) as exc:
        raise RuntimeFailure("host capability read-only sandbox output is invalid") from exc
    return report


def _run_host_contract_tests() -> bytes:
    test_path = Path(__file__).resolve().parent / "test_vnext_runtime.py"
    names = [
        "ReadOnlyTracerTests.test_cancel_rejects_record_without_live_unforgeable_marker",
        "ReadOnlyTracerTests.test_cancel_signals_active_group_and_terminalizes_queued_task",
        "ReadOnlyTracerTests.test_generation_claim_uses_one_predecessor_authority_contention_key",
        "ReadOnlyTracerTests.test_log_drainer_caps_event_object_count_even_within_durable_limit",
        "ReadOnlyTracerTests.test_runner_sigkill_reconcile_materializes_task_and_phase_receipt",
        "ReadOnlyTracerTests.test_terminal_fence_runs_after_workers_and_before_results_or_receipt",
    ]
    completed = subprocess.run(
        [sys.executable, os.fspath(test_path), *names],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    report = completed.stdout + completed.stderr
    if completed.returncode:
        raise RuntimeFailure("host capability focused contract tests failed")
    if not report:
        raise RuntimeFailure("host capability focused contract tests produced no evidence")
    return ("\n".join(f"{name} ... ok" for name in names) + "\n\nOK\n").encode()


def _host_capability_summary(
    *,
    observed_at: str,
    codex_version: str,
    codex_sha256: str,
    receipt_ref: str,
    receipt_sha256: str,
    statuses: dict[str, str],
) -> dict[str, Any]:
    return {
        "schema_version": "agent-workflow.host-capability-summary.v1",
        "observed_at": observed_at,
        "codex_cli_version": codex_version,
        "codex_binary_sha256": codex_sha256,
        "capabilities": {
            name: {
                "status": status,
                "evidence_ref": receipt_ref,
                "evidence_sha256": receipt_sha256,
            }
            for name, status in statuses.items()
        },
    }


def _host_workflow_bindings(
    summary: dict[str, Any], summary_ref: str, summary_sha256: str
) -> dict[str, dict[str, str]]:
    return {
        name: {
            "status": item["status"],
            "evidence_ref": summary_ref,
            "evidence_sha256": summary_sha256,
        }
        for name, item in summary["capabilities"].items()
    }


def _probe_host_capabilities_command(
    root: Path,
    repository: Path,
    relevant_root: str,
    auth_source: Path,
    codex_binary: str,
) -> dict[str, Any]:
    """Materialize the seven non-writer capabilities needed by fresh admission."""

    root = Path(root).resolve()
    repository = Path(repository).resolve(strict=True)
    if _repository_root_for(root) != repository:
        raise RuntimeFailure("host capability workflow root belongs to another repository")
    relative = Path(relevant_root)
    if (
        relevant_root != "."
        and (
            relative.is_absolute()
            or not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)
        )
    ):
        raise RuntimeFailure("host capability relevant root must be a safe relative path")
    live_relevant_root = (repository / relative).resolve(strict=True)
    if not live_relevant_root.is_relative_to(repository):
        raise RuntimeFailure("host capability relevant root escapes repository")
    try:
        worker_root = prepare_read_only_snapshot(
            root,
            repository,
            "000-host-capability-probe",
            (relevant_root,),
        )
    except SourceWriteError as exc:
        raise RuntimeFailure(f"host capability snapshot failed closed: {exc}") from exc
    summary_ref = "evidence/host-capability-summary.json"
    receipt_ref = "evidence/host-capability-receipt.json"
    codex_path, codex_sha256, codex_version = _codex_identity(codex_binary)
    _scrub_stale_codex_auth(root)
    receipt_path = root / receipt_ref
    summary_path = root / summary_ref
    if receipt_path.is_file() and not receipt_path.is_symlink():
        receipt = json.loads((root / receipt_ref).read_bytes())
        _validate_host_capability_provenance(
            root,
            receipt,
            running_bundle=_runtime_bundle_sha256(),
            codex_sha256=codex_sha256,
            codex_version=codex_version,
            worker_root=worker_root,
        )
        receipt_sha256 = _digest(receipt_path.read_bytes())
        expected_summary = _host_capability_summary(
            observed_at=receipt["observed_at"],
            codex_version=codex_version,
            codex_sha256=codex_sha256,
            receipt_ref=receipt_ref,
            receipt_sha256=receipt_sha256,
            statuses=receipt["capabilities"],
        )
        if summary_path.exists() or summary_path.is_symlink():
            if summary_path.is_symlink() or not summary_path.is_file():
                raise RuntimeFailure("host capability summary is unsafe")
            summary = json.loads(summary_path.read_bytes())
            if summary != expected_summary:
                raise RuntimeFailure("host capability summary drifted from its receipt")
        else:
            summary_path = create_once_json(root, summary_ref, expected_summary)
            summary = expected_summary
        summary_sha256 = _digest(summary_path.read_bytes())
        return {
            "status": "pass",
            "evidence_ref": summary_ref,
            "evidence_sha256": summary_sha256,
            "capability_bindings": _host_workflow_bindings(
                summary, summary_ref, summary_sha256
            ),
            "replayed": True,
        }

    probe_root = root / "evidence" / "host-capability-probe"
    schema_ref = "evidence/host-capability-probe/output-schema.json"
    _create_once_or_verify_json(
        root,
        schema_ref,
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["answer"],
            "properties": {"answer": {"type": "string", "const": "probe-ok"}},
        },
    )
    started_at = _now()
    homes: dict[str, Path] = {}
    executions: dict[str, RawExecution] = {}
    task_ids: dict[str, str] = {}
    try:
        for role in ("worker", "top"):
            homes[role] = _prepare_codex_home(
                root,
                auth_source,
                owner_id=f"host-capability-{role}",
            )

        incomplete_requests = [
            root / "runtime" / "watchdogs" / "000-host-capability-probe"
            / f"host-capability-{role}" / "request.json"
            for role in ("worker", "top")
            if (
                root / "runtime" / "watchdogs" / "000-host-capability-probe"
                / f"host-capability-{role}" / "request.json"
            ).is_file()
            and not (
                root / "runtime" / "watchdogs" / "000-host-capability-probe"
                / f"host-capability-{role}" / "terminal.json"
            ).is_file()
        ]
        if incomplete_requests:
            reconciled = reconcile_supervisors(root, grace_seconds=0.0)
            if reconciled.get("active"):
                raise RuntimeFailure("host capability probe still has an active prior attempt")

        def execute_role(role: str) -> tuple[str, str, RawExecution]:
            task_id, observed = _select_host_probe_attempt(root, role)
            if observed is not None:
                return role, task_id, observed
            claim_ref = f"evidence/host-capability-probe/claims/{task_id}.json"
            claim_path = _create_once_or_verify_json(
                root,
                claim_ref,
                {
                    "schema_version": "agent-workflow.probe-generation-claim.v1",
                    "generation_id": "host-capability-probe",
                    "role": role,
                },
            )
            task = {
                "task_id": task_id,
                "role": role,
                "work_mode": "read",
                "write_roots": [],
                "execution_deadline_seconds": 180,
                "_runtime_worker_root": os.fspath(worker_root),
                "_runtime_codex_home": os.fspath(homes[role]),
                "_runtime_permissions_profile": "vnext-read-only",
                "_runtime_write_roots": [],
                "_runtime_plan_sha256": "sha256:" + "1" * 64,
                "_runtime_generation_claim_ref": claim_ref,
                "_runtime_generation_claim_sha256": _digest(claim_path.read_bytes()),
                "_runtime_bundle_sha256": _runtime_bundle_sha256(),
                "_runtime_boot_identity": _process_identity(1),
                "_runtime_generation_id": "host-capability-probe",
                "_runtime_phase_id": "000-host-capability-probe",
            }
            executor = codex_task_executor(
                CodexExecConfig(
                    run_root=root,
                    repo_root=worker_root,
                    codex_home=homes[role],
                    codex_binary=os.fspath(codex_path),
                    workflow_id="host-capability-probe",
                    authority_revision=1,
                    permissions_profile="vnext-read-only",
                )
            )
            return role, task_id, executor(
                task,
                {
                    "output_schema_ref": schema_ref,
                    "prompt": "Return exactly the schema-compliant JSON answer probe-ok. Use no tools.",
                },
            )

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="host-capability") as pool:
            futures = [pool.submit(execute_role, role) for role in ("worker", "top")]
            for future in futures:
                role, task_id, raw = future.result()
                executions[role] = raw
                task_ids[role] = task_id
        finished_at = _now()
        observed_starts = [item.observed_started_at for item in executions.values()]
        observed_finishes = [item.observed_finished_at for item in executions.values()]
        if all(isinstance(item, datetime) for item in observed_starts):
            started_at = min(observed_starts)
        if all(isinstance(item, datetime) for item in observed_finishes):
            finished_at = max(observed_finishes)

        session_records: dict[str, dict[str, Any]] = {}
        for role in ("worker", "top"):
            raw = executions[role]
            session_id, output = _host_probe_output(raw, role)
            events_ref = f"evidence/host-capability-probe/{role}-events.jsonl"
            context_ref = f"evidence/host-capability-probe/{role}-turn-context.json"
            output_ref = f"evidence/host-capability-probe/{role}-output.json"
            events_path = _create_once_or_verify_bytes(
                root,
                events_ref,
                raw.stdout_bytes or b"".join(_canonical(item) for item in raw.events),
            )
            context_path = _create_once_or_verify_json(root, context_ref, raw.turn_context)
            output_path = _create_once_or_verify_json(root, output_ref, output)
            task_id = task_ids[role]
            request_ref = f"runtime/watchdogs/000-host-capability-probe/{task_id}/request.json"
            terminal_ref = f"runtime/watchdogs/000-host-capability-probe/{task_id}/terminal.json"
            rollout_path = _find_session_rollout(homes[role], session_id)
            if rollout_path is None or rollout_path.stat().st_size > 4 * 1024 * 1024:
                raise RuntimeFailure(f"host capability {role} rollout is missing or oversized")
            session_records[role] = {
                "session_id": session_id,
                "model": "gpt-5.6-terra" if role == "worker" else "gpt-5.6-sol",
                "reasoning_effort": "xhigh",
                "codex_home": os.fspath(homes[role]),
                "events_ref": events_ref,
                "events_sha256": _digest(events_path.read_bytes()),
                "turn_context_ref": context_ref,
                "turn_context_sha256": _digest(context_path.read_bytes()),
                "output_ref": output_ref,
                "output_sha256": _digest(output_path.read_bytes()),
                "supervisor_request_ref": request_ref,
                "supervisor_request_sha256": _digest((root / request_ref).read_bytes()),
                "supervisor_terminal_ref": terminal_ref,
                "supervisor_terminal_sha256": _digest((root / terminal_ref).read_bytes()),
                "rollout_path": os.fspath(rollout_path),
                "rollout_bytes": rollout_path.stat().st_size,
                "rollout_sha256": _digest_file(rollout_path),
            }

        denial_report = _run_host_read_only_denials(root, codex_path, homes["worker"])
        denial_ref = "evidence/host-capability-probe/read-only-denials.json"
        denial_path = _create_once_or_verify_json(root, denial_ref, denial_report)
        test_ref = "evidence/host-capability-probe/focused-tests.txt"
        test_path = _create_once_or_verify_bytes(root, test_ref, _run_host_contract_tests())
        statuses = {
            name: "unavailable" if name == "sandbox_isolation" else "pass"
            for name in CAPABILITY_NAMES
        }
        try:
            prepare_read_only_snapshot(
                root,
                repository,
                "000-host-capability-probe",
                (relevant_root,),
            )
        except SourceWriteError as exc:
            raise RuntimeFailure(f"host capability snapshot terminal fence drifted: {exc}") from exc
        snapshot_manifest_ref = "runtime/read-snapshots/000-host-capability-probe/manifest.json"
        snapshot_manifest_path = root / snapshot_manifest_ref
        observed_at = _now()
        receipt = {
            "schema_version": "agent-workflow.host-capability-receipt.v1",
            "observed_at": _timestamp(observed_at),
            "producer": {
                "name": "agent-workflow-host-capability-probe",
                "runtime_bundle_sha256": _runtime_bundle_sha256(),
                "codex_cli_version": codex_version,
                "codex_binary_sha256": codex_sha256,
            },
            "relevant_root": os.fspath(worker_root),
            "snapshot_manifest": {
                "evidence_ref": snapshot_manifest_ref,
                "evidence_sha256": _digest(snapshot_manifest_path.read_bytes()),
            },
            "execution": {
                "started_at": _timestamp(started_at),
                "finished_at": _timestamp(finished_at),
                "role_count": 2,
                "terminal_count": 2,
            },
            "sessions": session_records,
            "deterministic_denials": {
                "evidence_ref": denial_ref,
                "evidence_sha256": _digest(denial_path.read_bytes()),
            },
            "focused_tests": {
                "evidence_ref": test_ref,
                "evidence_sha256": _digest(test_path.read_bytes()),
            },
            "capabilities": statuses,
        }
        receipt_path = create_once_json(root, receipt_ref, receipt)
        receipt_sha256 = _digest(receipt_path.read_bytes())
        summary = _host_capability_summary(
            observed_at=_timestamp(observed_at),
            codex_version=codex_version,
            codex_sha256=codex_sha256,
            receipt_ref=receipt_ref,
            receipt_sha256=receipt_sha256,
            statuses=statuses,
        )
        summary_path = create_once_json(root, summary_ref, summary)
        _validate_host_capability_provenance(
            root,
            receipt,
            running_bundle=_runtime_bundle_sha256(),
            codex_sha256=codex_sha256,
            codex_version=codex_version,
            worker_root=worker_root,
        )
        return {
            "status": "pass",
            "evidence_ref": summary_ref,
            "evidence_sha256": _digest(summary_path.read_bytes()),
            "capability_bindings": _host_workflow_bindings(
                summary, summary_ref, _digest(summary_path.read_bytes())
            ),
            "replayed": False,
            "sessions": {role: item["session_id"] for role, item in session_records.items()},
        }
    finally:
        for home in homes.values():
            _cleanup_codex_auth(home)


def _source_write_probe_workspace_manifest(workspace: Path) -> dict[str, Any]:
    """Read a private probe tree through no-follow descriptors."""

    workspace = Path(workspace)
    if not workspace.is_absolute() or any(part in {".", ".."} for part in workspace.parts[1:]):
        raise RuntimeFailure("source-write probe workspace path is not absolute and normalized")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    seen: set[tuple[int, int]] = set()
    entries: dict[str, dict[str, Any]] = {}

    def open_chain() -> tuple[int, list[dict[str, Any]]]:
        try:
            current_fd = os.open("/", directory_flags)
        except OSError as exc:
            raise RuntimeFailure("source-write probe workspace root cannot be opened") from exc
        chain: list[dict[str, Any]] = []
        try:
            for component in workspace.parts[1:]:
                child_fd = os.open(component, directory_flags, dir_fd=current_fd)
                os.close(current_fd)
                current_fd = child_fd
                metadata = os.fstat(current_fd)
                chain.append(
                    {
                        "component": component,
                        "device": metadata.st_dev,
                        "inode": metadata.st_ino,
                        "type": "directory" if stat.S_ISDIR(metadata.st_mode) else "unsafe",
                        "owner": metadata.st_uid,
                        "mode": stat.S_IMODE(metadata.st_mode),
                    }
                )
            return current_fd, chain
        except OSError as exc:
            os.close(current_fd)
            raise RuntimeFailure("source-write probe workspace path is unsafe") from exc
        except Exception:
            os.close(current_fd)
            raise

    def identity(metadata: os.stat_result, *, kind: str) -> dict[str, Any]:
        return {
            "type": kind,
            "device": metadata.st_dev,
            "inode": metadata.st_ino,
            "mode": stat.S_IMODE(metadata.st_mode),
            "owner": metadata.st_uid,
            "links": metadata.st_nlink,
            "size": metadata.st_size,
            "modified_ns": metadata.st_mtime_ns,
            "changed_ns": metadata.st_ctime_ns,
        }

    def stable_identity(metadata: os.stat_result) -> tuple[int, ...]:
        return (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_uid,
            metadata.st_nlink,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )

    def walk(directory_fd: int, prefix: str) -> None:
        for name in sorted(os.listdir(directory_fd)):
            if not name or name in {".", ".."} or "/" in name:
                raise RuntimeFailure("source-write probe workspace has an unsafe entry name")
            relative = f"{prefix}/{name}" if prefix else name
            try:
                before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError as exc:
                raise RuntimeFailure("source-write probe workspace changed during sealing") from exc
            inode = (before.st_dev, before.st_ino)
            if inode in seen:
                raise RuntimeFailure("source-write probe workspace contains a repeated inode")
            if before.st_uid != os.getuid():
                raise RuntimeFailure("source-write probe workspace has an unexpected owner")
            if stat.S_ISDIR(before.st_mode):
                try:
                    child_fd = os.open(name, directory_flags, dir_fd=directory_fd)
                except OSError as exc:
                    raise RuntimeFailure("source-write probe workspace contains an unsafe directory") from exc
                try:
                    observed = os.fstat(child_fd)
                    if (observed.st_dev, observed.st_ino) != inode:
                        raise RuntimeFailure("source-write probe directory identity drifted")
                    seen.add(inode)
                    walk(child_fd, relative)
                    after = os.fstat(child_fd)
                    if stable_identity(after) != stable_identity(observed):
                        raise RuntimeFailure("source-write probe directory changed during sealing")
                    entries[relative] = identity(after, kind="directory")
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise RuntimeFailure("source-write probe workspace contains an unsafe node")
            try:
                file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
            except OSError as exc:
                raise RuntimeFailure("source-write probe workspace contains an unsafe file") from exc
            try:
                observed = os.fstat(file_fd)
                if (
                    (observed.st_dev, observed.st_ino) != inode
                    or not stat.S_ISREG(observed.st_mode)
                    or observed.st_nlink != 1
                ):
                    raise RuntimeFailure("source-write probe file identity drifted")
                payload = bytearray()
                while chunk := os.read(file_fd, 1024 * 1024):
                    payload.extend(chunk)
                after = os.fstat(file_fd)
                if stable_identity(after) != stable_identity(observed):
                    raise RuntimeFailure("source-write probe file changed during sealing")
                seen.add(inode)
                entries[relative] = {
                    **identity(after, kind="file"),
                    "sha256": _digest(bytes(payload)),
                }
            finally:
                os.close(file_fd)

    root_fd, path_chain = open_chain()
    try:
        root_metadata = os.fstat(root_fd)
        if not stat.S_ISDIR(root_metadata.st_mode) or root_metadata.st_uid != os.getuid():
            raise RuntimeFailure("source-write probe workspace root is unsafe")
        seen.add((root_metadata.st_dev, root_metadata.st_ino))
        walk(root_fd, "")
        root_after = os.fstat(root_fd)
        if stable_identity(root_after) != stable_identity(root_metadata):
            raise RuntimeFailure("source-write probe workspace root changed during sealing")
        return {
            "path_chain": path_chain,
            "root": identity(root_after, kind="directory"),
            "entries": entries,
        }
    except OSError as exc:
        raise RuntimeFailure("source-write probe workspace changed during sealing") from exc
    finally:
        os.close(root_fd)


def _seal_source_write_probe_launch_fence(workspace: Path) -> Callable[[], None]:
    """Seal one disposable writer-probe workspace until its actor launch."""

    workspace = Path(workspace)
    sealed_workspace = _source_write_probe_workspace_manifest(workspace)
    expected_directories = {".git", "src", "src/api"}
    if (
        set(sealed_workspace["entries"]) != expected_directories
        or any(
            sealed_workspace["entries"][relative].get("type") != "directory"
            for relative in expected_directories
        )
    ):
        raise RuntimeFailure("source-write probe workspace is not the empty synthetic layout")

    def source_launch_fence() -> None:
        try:
            observed_workspace = _source_write_probe_workspace_manifest(workspace)
        except (RuntimeFailure, SourceWriteError) as exc:
            raise RuntimeFailure(f"source-write probe workspace drifted: {exc}") from exc
        if observed_workspace != sealed_workspace:
            raise RuntimeFailure("source-write probe workspace drifted before actor launch")

    return source_launch_fence


def _probe_source_write_command(
    root: Path,
    auth_source: Path,
    codex_binary: str,
) -> dict[str, Any]:
    """Produce one live, raw-evidence-backed source-write capability receipt."""

    root = Path(root).resolve(strict=True)
    try:
        _secure_private_directory(root, ("evidence",))
        probe_root = _secure_private_directory(root, ("evidence", "source-write-probe"))
    except (OSError, SourceWriteError) as exc:
        raise RuntimeFailure(f"source-write probe control root is unsafe: {exc}") from exc
    codex_path, codex_sha256, codex_version = _codex_identity(codex_binary)
    evidence_ref = "evidence/source-write-capability.json"
    evidence_path = root / evidence_ref
    _scrub_stale_codex_auth(probe_root)
    if evidence_path.is_file() and not evidence_path.is_symlink():
        capability = {
            "status": "pass",
            "evidence_ref": evidence_ref,
            "evidence_sha256": _digest(evidence_path.read_bytes()),
        }
        _validate_source_write_capability(
            root,
            capability,
            running_bundle=_runtime_bundle_sha256(),
            codex_sha256=codex_sha256,
            codex_version=codex_version,
            codex_binary=codex_path,
        )
        evidence = json.loads(evidence_path.read_bytes())
        return {
            **capability,
            "session_id": evidence["session"]["id"],
            "token_usage": {
                "input": None,
                "output": None,
                "total": None,
                "confidence": "replayed_terminal",
            },
            "replayed": True,
        }
    try:
        workspace = _secure_private_directory(probe_root, ("workspace",))
        allowed_root = _secure_private_directory(probe_root, ("workspace", "src", "api"))
        git_root = _secure_private_directory(probe_root, ("workspace", ".git"))
        sibling = _secure_private_directory(probe_root, ("sibling",))
        _secure_private_directory(probe_root, ("schemas",))
    except (OSError, SourceWriteError) as exc:
        raise RuntimeFailure(f"source-write probe workspace is unsafe: {exc}") from exc
    source_launch_fence = _seal_source_write_probe_launch_fence(workspace)
    control_secret = probe_root / "control-secret.txt"
    _create_once_or_verify_bytes(probe_root, "control-secret.txt", b"control-probe\n")
    schema = probe_root / "schemas/output.json"
    _create_once_or_verify_bytes(
        probe_root,
        "schemas/output.json",
        (json.dumps(
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["answer"],
                "properties": {"answer": {"type": "string", "const": "ok"}},
            },
            sort_keys=True,
        )
        + "\n").encode(),
    )
    codex_home = _prepare_codex_home(
        probe_root,
        auth_source,
        owner_id="source-write-capability",
        writer_roots=("src/api",),
    )
    claim_ref = "generations/claims/source-write-capability.json"
    claim_path = create_once_json(
        probe_root,
        claim_ref,
        {
            "schema_version": "agent-workflow.probe-generation-claim.v1",
            "generation_id": "generation-probe",
        },
    )
    task = {
        "task_id": "source-write-probe",
        "role": "worker",
        "work_mode": "write",
        "write_roots": ["src/api"],
        "execution_deadline_seconds": 120,
        "_runtime_worker_root": os.fspath(workspace),
        "_runtime_codex_home": os.fspath(codex_home),
        "_runtime_permissions_profile": "vnext-writer",
        "_runtime_write_roots": ["src/api"],
        "_runtime_plan_sha256": "sha256:" + "1" * 64,
        "_runtime_generation_claim_ref": claim_ref,
        "_runtime_generation_claim_sha256": _digest(claim_path.read_bytes()),
        "_runtime_bundle_sha256": _runtime_bundle_sha256(),
        "_runtime_boot_identity": _process_identity(1),
        "_runtime_generation_id": "generation-probe",
        "_runtime_phase_id": "000-source-write-probe",
        "_runtime_source_launch_fence": source_launch_fence,
    }
    raw: RawExecution | None = None
    try:
        raw = codex_task_executor(
            CodexExecConfig(
                run_root=probe_root,
                repo_root=workspace,
                codex_home=codex_home,
                codex_binary=os.fspath(codex_path),
                workflow_id="source-write-capability",
                authority_revision=1,
                permissions_profile="vnext-writer",
            )
        )(
            task,
            {
                "output_schema_ref": "schemas/output.json",
                "prompt": (
                    "Use the shell once to write the exact UTF-8 text 'source-write-probe-ok\\n' "
                    "to src/api/live.txt. Do not inspect or modify anything else. Then return "
                    "exactly the schema-compliant JSON answer ok."
                ),
            },
        )
        if raw.adapter_error or raw.exit_code != 0 or not isinstance(raw.turn_context, dict):
            raise RuntimeFailure("live source-write Codex probe did not attest its effective profile")
        if (allowed_root / "live.txt").read_text() != "source-write-probe-ok\n":
            raise RuntimeFailure("live source-write Codex probe did not produce its allowed write")

        credential = codex_home / "auth.json"
        shell = (
            "printf allowed > src/api/sandbox.txt; a=$?; "
            "printf git > .git/index; g=$?; "
            f"printf sibling > {shlex.quote(os.fspath(sibling / 'escape.txt'))}; s=$?; "
            f"/bin/cat {shlex.quote(os.fspath(control_secret))} >/dev/null 2>&1; c=$?; "
            f"/bin/cat {shlex.quote(os.fspath(credential))} >/dev/null 2>&1; r=$?; "
            "/usr/bin/curl -m 1 -fsS http://1.1.1.1 >/dev/null 2>&1; n=$?; "
            "printf 'allowed=%s git=%s sibling=%s control=%s credential=%s network=%s\\n' "
            '"$a" "$g" "$s" "$c" "$r" "$n"'
        )
        sandbox_command = [
            os.fspath(codex_path),
            "sandbox",
            "-p",
            "vnext-writer",
            "-P",
            "vnext_writer",
            "-C",
            os.fspath(workspace),
            "/bin/sh",
            "-c",
            shell,
        ]
        sandbox = subprocess.run(
            sandbox_command,
            env={**os.environ, "CODEX_HOME": os.fspath(codex_home)},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if sandbox.returncode:
            raise RuntimeFailure("deterministic source-write sandbox probe failed to execute")
        try:
            codes = dict(
                item.split("=", 1)
                for item in sandbox.stdout.decode().strip().splitlines()[-1].split()
            )
            parsed_codes = {key: int(value) for key, value in codes.items()}
        except (IndexError, ValueError) as exc:
            raise RuntimeFailure("deterministic source-write sandbox probe output is invalid") from exc
        expected_names = {"allowed", "git", "sibling", "control", "credential", "network"}
        if (
            set(parsed_codes) != expected_names
            or parsed_codes["allowed"] != 0
            or any(parsed_codes[name] == 0 for name in expected_names - {"allowed"})
        ):
            raise RuntimeFailure("deterministic source-write sandbox denials are insufficient")

        context_ref = "evidence/source-write-probe/turn-context.json"
        context_path = create_once_json(root, context_ref, raw.turn_context)
        profile_path = codex_home / "vnext-writer.config.toml"
        profile_ref = profile_path.relative_to(root).as_posix()
        sandbox_stdout_ref = "evidence/source-write-probe/sandbox.stdout"
        sandbox_stderr_ref = "evidence/source-write-probe/sandbox.stderr"
        sandbox_stdout_path = create_once_bytes(root, sandbox_stdout_ref, sandbox.stdout)
        sandbox_stderr_path = create_once_bytes(root, sandbox_stderr_ref, sandbox.stderr)
        report = {
            "schema_version": "agent-workflow.source-write-denial-probe.vnext.v1",
            "profile_sha256": _digest(profile_path.read_bytes()),
            "workspace_root": os.fspath(workspace),
            "command": sandbox_command,
            "command_sha256": _digest(_canonical(sandbox_command)),
            "stdout_ref": sandbox_stdout_ref,
            "stdout_sha256": _digest(sandbox_stdout_path.read_bytes()),
            "stderr_ref": sandbox_stderr_ref,
            "stderr_sha256": _digest(sandbox_stderr_path.read_bytes()),
            "allowed_write_exit": parsed_codes["allowed"],
            "git_write_exit": parsed_codes["git"],
            "sibling_write_exit": parsed_codes["sibling"],
            "control_read_exit": parsed_codes["control"],
            "credential_read_exit": parsed_codes["credential"],
            "network_exit": parsed_codes["network"],
        }
        report_ref = "evidence/source-write-probe/denial-report.json"
        report_path = create_once_json(root, report_ref, report)
        refs = _probe_runtime_refs(task["_runtime_phase_id"], task["task_id"])
        request_ref = refs["request_ref"]
        terminal_ref = refs["terminal_ref"]
        events_ref = refs["events_ref"]
        stderr_ref = refs["stderr_ref"]
        terminal = json.loads((root / terminal_ref).read_bytes())
        session_id = raw.turn_context["session_id"]
        evidence = {
            "schema_version": "agent-workflow.source-write-capability.vnext.v1",
            "observed_at": _timestamp(_now()),
            "producer": {
                "name": "agent-workflow-slice3-writer-probe",
                "runtime_bundle_sha256": _runtime_bundle_sha256(),
                "codex_cli_version": codex_version,
                "codex_binary_sha256": codex_sha256,
            },
            "workspace": {
                "root": os.fspath(workspace),
                "codex_home": os.fspath(codex_home),
                "write_roots": ["src/api"],
                "profile_ref": profile_ref,
                "profile_sha256": _digest(profile_path.read_bytes()),
                "turn_context_ref": context_ref,
                "turn_context_sha256": _digest(context_path.read_bytes()),
            },
            "session": {
                "id": session_id,
                "model": raw.turn_context["model"],
                "reasoning_effort": raw.turn_context["effort"],
            },
            "supervisor": {
                "request_ref": request_ref,
                "request_sha256": _digest((root / request_ref).read_bytes()),
                "terminal_ref": terminal_ref,
                "terminal_sha256": _digest((root / terminal_ref).read_bytes()),
                "events_ref": events_ref,
                "events_sha256": terminal["stdout_sha256"],
                "stderr_ref": stderr_ref,
                "stderr_sha256": terminal["stderr_sha256"],
            },
            "deterministic_probe": {
                "evidence_ref": report_ref,
                "evidence_sha256": _digest(report_path.read_bytes()),
            },
            "environment": {
                "inherit": [
                    "AGENT_WORKFLOW_AUDIT_MARKER",
                    "CODEX_HOME",
                    "HOME",
                    "LANG",
                    "PATH",
                    "TMPDIR",
                ],
                "plugins_disabled": True,
                "mcp_disabled": True,
                "network_enabled": False,
            },
        }
        evidence_path = create_once_json(root, evidence_ref, evidence)
        usage_events = [event for event in raw.events if event.get("type") == "turn.completed"]
        usage = usage_events[0].get("usage", {}) if len(usage_events) == 1 else {}
        return {
            "status": "pass",
            "evidence_ref": evidence_ref,
            "evidence_sha256": _digest(evidence_path.read_bytes()),
            "session_id": session_id,
            "token_usage": {
                "input": usage.get("input_tokens"),
                "output": usage.get("output_tokens"),
                "total": (
                    usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
                    if isinstance(usage.get("input_tokens"), int)
                    and isinstance(usage.get("output_tokens"), int)
                    else None
                ),
                "confidence": "exact" if usage else "partial",
            },
            "replayed": False,
        }
    finally:
        _cleanup_codex_auth(codex_home)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    admit = sub.add_parser("admit")
    admit.add_argument("--root", type=Path, required=True)
    admit.add_argument("--repo", type=Path, required=True)
    admit.add_argument("--workflow-source", type=Path, required=True)
    admit.add_argument("--codex-binary", default="codex")
    run = sub.add_parser("run-phase")
    run.add_argument("--root", type=Path, required=True)
    run.add_argument("--repo", type=Path, required=True)
    run.add_argument("--plan-source", type=Path, required=True)
    run.add_argument("--auth-source", type=Path, required=True)
    run.add_argument("--codex-binary", default="codex")
    run.add_argument("--max-parallel", type=int, required=True)
    once = sub.add_parser("run-once")
    once.add_argument("--root", type=Path, required=True)
    once.add_argument("--repo", type=Path, required=True)
    once.add_argument("--workflow-source", type=Path, required=True)
    once.add_argument("--plan-source", type=Path, required=True)
    once.add_argument("--auth-source", type=Path, required=True)
    once.add_argument("--codex-binary", default="codex")
    once.add_argument("--max-parallel", type=int, required=True)
    cancel = sub.add_parser("cancel")
    cancel.add_argument("--root", type=Path, required=True)
    cancel.add_argument("--authority-revision", type=int, required=True)
    reconcile_parser = sub.add_parser("reconcile")
    reconcile_parser.add_argument("--root", type=Path, required=True)
    reconcile_parser.add_argument("--authority-revision", type=int, required=True)
    reconcile_parser.add_argument("--grace-seconds", type=float, default=1.0)
    source_probe = sub.add_parser("probe-source-write")
    source_probe.add_argument("--root", type=Path, required=True)
    source_probe.add_argument("--auth-source", type=Path, required=True)
    source_probe.add_argument("--codex-binary", default="codex")
    host_probe = sub.add_parser("probe-host-capabilities")
    host_probe.add_argument("--root", type=Path, required=True)
    host_probe.add_argument("--repo", type=Path, required=True)
    host_probe.add_argument("--relevant-root", required=True)
    host_probe.add_argument("--auth-source", type=Path, required=True)
    host_probe.add_argument("--codex-binary", default="codex")
    amend = sub.add_parser("amend")
    amend.add_argument("--root", type=Path, required=True)
    amend.add_argument("--request-source", type=Path, required=True)
    resume = sub.add_parser("resume-brief")
    resume.add_argument("--root", type=Path, required=True)
    resume.add_argument("--generation-id", required=True)
    seal_final_parser = sub.add_parser("seal-final")
    seal_final_parser.add_argument("--root", type=Path, required=True)
    seal_final_parser.add_argument("--candidate-source", type=Path, required=True)
    seal_accounting_parser = sub.add_parser("seal-accounting")
    seal_accounting_parser.add_argument("--root", type=Path, required=True)
    seal_accounting_parser.add_argument("--native-source", type=Path, required=True)
    seal_accounting_parser.add_argument("--native-evidence-source", type=Path, required=True)
    seal_accounting_parser.add_argument("--completion-source", type=Path, required=True)
    pinned_parser = sub.add_parser("pinned-runtime")
    pinned_parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "admit":
            summary = _admit_command(
                args.root,
                args.repo,
                args.workflow_source,
                args.codex_binary,
            )
        elif args.command == "run-phase":
            summary = _run_phase_command(
                args.root,
                args.repo,
                args.plan_source,
                args.auth_source,
                args.codex_binary,
                args.max_parallel,
            )
        elif args.command == "run-once":
            admission = _admit_command(
                args.root,
                args.repo,
                args.workflow_source,
                args.codex_binary,
            )
            summary = _run_phase_command(
                args.root,
                args.repo,
                args.plan_source,
                args.auth_source,
                args.codex_binary,
                args.max_parallel,
            )
            summary["admission"] = admission
        elif args.command == "cancel":
            summary = cancel_run(args.root, args.authority_revision)
        elif args.command == "reconcile":
            summary = reconcile_run(
                args.root,
                args.authority_revision,
                grace_seconds=args.grace_seconds,
            )
        elif args.command == "probe-source-write":
            summary = _probe_source_write_command(
                args.root,
                args.auth_source,
                args.codex_binary,
            )
        elif args.command == "probe-host-capabilities":
            summary = _probe_host_capabilities_command(
                args.root,
                args.repo,
                args.relevant_root,
                args.auth_source,
                args.codex_binary,
            )
        elif args.command == "amend":
            workflow = _load_fixed_json(args.root, "workflow.json", "workflow seal")
            amendment = _load_source(args.request_source, "amendment request")
            path = seal_amendment(args.root, workflow, amendment)
            summary = {
                "status": "amendment_sealed",
                "amendment_ref": path.relative_to(Path(args.root).resolve()).as_posix(),
                "amendment_sha256": _digest(path.read_bytes()),
                "authority_revision": amendment["authority_revision"],
            }
        elif args.command == "resume-brief":
            workflow = _load_fixed_json(args.root, "workflow.json", "workflow seal")
            authority_revision = current_authority_revision(args.root, workflow)
            reconcile_summary = reconcile_run(
                args.root,
                authority_revision,
                grace_seconds=0.0,
            )
            path = seal_resume_brief(args.root, workflow, args.generation_id)
            summary = {
                "status": "resume_brief_sealed",
                "resume_brief_ref": path.relative_to(Path(args.root).resolve()).as_posix(),
                "resume_brief_sha256": _digest(path.read_bytes()),
                "reconciled_attempts": reconcile_summary["attempt_count"],
            }
        elif args.command == "seal-final":
            candidate = _load_source(args.candidate_source, "final candidate")
            path = seal_final(args.root, candidate)
            summary = {
                "status": "final_sealed",
                "final_ref": "final.json",
                "final_sha256": _digest(path.read_bytes()),
            }
        elif args.command == "seal-accounting":
            path = seal_accounting(
                args.root,
                native_source=args.native_source,
                native_evidence_source=args.native_evidence_source,
                completion_source=args.completion_source,
                running_bundle=_runtime_bundle_sha256(),
            )
            summary = {
                "status": "accounting_sealed",
                "accounting_ref": "accounting/final.json",
                "accounting_sha256": _digest(path.read_bytes()),
            }
        else:
            workflow = _load_fixed_json(args.root, "workflow.json", "workflow seal")
            expected_bundle = workflow["runtime_bundle"]["sha256"]
            pinned = _resolve_pinned_runtime(args.root, expected_bundle)
            summary = {
                "status": "pinned_runtime_ready",
                "runtime_ref": pinned.relative_to(Path(args.root).resolve()).as_posix(),
                "runtime_path": os.fspath(pinned),
                "runtime_bundle_sha256": expected_bundle,
            }
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0
    except HumanGateRequired as exc:
        print(
            json.dumps(
                {
                    "status": "human_gate",
                    "gate": "dirty_write_overlap",
                    "reason": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 3
    except PinnedBundleUnavailable as exc:
        print(
            json.dumps(
                {"status": "blocked_incompatible_release", "reason": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 4
    except (RuntimeFailure, RecoveryError, ProtocolError, ArtifactError, SupervisorFailure, AccountingError) as exc:
        print(
            json.dumps(
                {"status": "runtime_failed", "reason": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
