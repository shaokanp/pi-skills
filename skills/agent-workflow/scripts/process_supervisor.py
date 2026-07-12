#!/usr/bin/env python3
"""Crash-independent worker watchdog for Agent Workflow vNext.

The Phase Runner may wait for this short-lived process, but it does not own the
worker pipes or process group.  If the Runner exits, the watchdog still drains
bounded logs, enforces the sealed deadline, reaps its child, and publishes one
create-once exit receipt.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

from artifact_store import ArtifactError, create_once_bytes, create_once_json


class SupervisorFailure(RuntimeError):
    """Raised when a watchdog request cannot be executed safely."""


_MAX_REQUEST_BYTES = 1024 * 1024
_MAX_EVENT_BYTES = 4 * 1024 * 1024
_MAX_EVENT_COUNT = 4096


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_time(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise SupervisorFailure(f"{label} must be an RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SupervisorFailure(f"{label} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise SupervisorFailure(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def process_identity(pid: int) -> str | None:
    observed = subprocess.run(
        ["ps", "-ww", "-p", str(pid), "-o", "lstart=,command="],
        text=True,
        capture_output=True,
        check=False,
    )
    identity = observed.stdout.strip()
    return identity if observed.returncode == 0 and identity else None


def process_birth(pid: int) -> str | None:
    if sys.platform == "darwin":
        class ProcBsdInfo(ctypes.Structure):
            _fields_ = [
                ("pbi_flags", ctypes.c_uint32), ("pbi_status", ctypes.c_uint32),
                ("pbi_xstatus", ctypes.c_uint32), ("pbi_pid", ctypes.c_uint32),
                ("pbi_ppid", ctypes.c_uint32), ("pbi_uid", ctypes.c_uint32),
                ("pbi_gid", ctypes.c_uint32), ("pbi_ruid", ctypes.c_uint32),
                ("pbi_rgid", ctypes.c_uint32), ("pbi_svuid", ctypes.c_uint32),
                ("pbi_svgid", ctypes.c_uint32), ("rfu_1", ctypes.c_uint32),
                ("pbi_comm", ctypes.c_char * 16), ("pbi_name", ctypes.c_char * 32),
                ("pbi_nfiles", ctypes.c_uint32), ("pbi_pgid", ctypes.c_uint32),
                ("pbi_pjobc", ctypes.c_uint32), ("e_tdev", ctypes.c_uint32),
                ("e_tpgid", ctypes.c_uint32), ("pbi_nice", ctypes.c_int32),
                ("pbi_start_tvsec", ctypes.c_uint64),
                ("pbi_start_tvusec", ctypes.c_uint64),
            ]

        info = ProcBsdInfo()
        try:
            libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            observed = libproc.proc_pidinfo(
                int(pid),
                3,  # PROC_PIDTBSDINFO from <sys/proc_info.h>
                0,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
        except OSError:
            observed = 0
        if observed == ctypes.sizeof(info) and info.pbi_pid == pid:
            return (
                f"darwin:{info.pbi_start_tvsec}.{info.pbi_start_tvusec:06d}:"
                f"uid={info.pbi_uid}:ppid={info.pbi_ppid}:pgid={info.pbi_pgid}"
            )
    observed = subprocess.run(
        ["ps", "-ww", "-p", str(pid), "-o", "lstart="],
        text=True,
        capture_output=True,
        check=False,
    )
    value = observed.stdout.strip()
    return f"coarse:{value}" if observed.returncode == 0 and value else None


def process_command(pid: int) -> str | None:
    observed = subprocess.run(
        ["ps", "-ww", "-p", str(pid), "-o", "command="],
        text=True,
        capture_output=True,
        check=False,
    )
    value = observed.stdout.strip()
    return value if observed.returncode == 0 and value else None


def command_matches_request(command: str, request: dict[str, Any], request_ref: str) -> bool:
    """Bind a live command to either the sealed bootstrap or sealed worker executable."""

    if request["audit_marker"] not in command:
        return False
    try:
        executable = os.fspath(Path(request["command"][0]).resolve(strict=True))
    except OSError:
        return False
    bootstrap = os.fspath(Path(__file__).resolve())
    return executable in command or (bootstrap in command and request_ref in command)


def _scan_marker(marker: str) -> list[dict[str, Any]]:
    observed = subprocess.run(
        ["ps", "-ww", "-axo", "pid=,pgid=,sess=,command="],
        text=True,
        capture_output=True,
        check=False,
    )
    if observed.returncode != 0:
        raise SupervisorFailure("escaped-process scan is unavailable")
    matches = []
    for line in observed.stdout.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) != 4 or marker not in parts[3]:
            continue
        try:
            pid, pgid, sid = (int(parts[0]), int(parts[1]), int(parts[2]))
        except ValueError:
            continue
        if pid != os.getpid():
            matches.append({"pid": pid, "pgid": pgid, "sid": sid})
    return matches


def _kill_marker_matches(
    marker: str,
    matches: list[dict[str, Any]],
    *,
    grace_seconds: float,
) -> bool:
    """Kill only still-matching escaped groups and prove their marker disappeared."""

    for item in matches:
        command = process_command(item["pid"])
        if command is None or marker not in command:
            continue
        try:
            os.killpg(item["pgid"], signal.SIGKILL)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + max(0.0, grace_seconds)
    while _scan_marker(marker) and time.monotonic() < deadline:
        time.sleep(0.02)
    return not _scan_marker(marker)


def _safe_file(root: Path, relative: str, label: str) -> Path:
    if not isinstance(relative, str) or not relative or relative.startswith("/"):
        raise SupervisorFailure(f"{label} must be a relative path")
    parts = Path(relative).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise SupervisorFailure(f"{label} is unsafe")
    path = root.joinpath(*parts)
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise SupervisorFailure(f"{label} escapes the workflow root") from exc
    return path


def _load_request(
    root: Path,
    request_ref: str,
    *,
    enforce_boot: bool = True,
) -> tuple[dict[str, Any], bytes]:
    path = _safe_file(root, request_ref, "supervisor request")
    if path.is_symlink() or not path.is_file():
        raise SupervisorFailure("supervisor request is missing or unsafe")
    payload = path.read_bytes()
    if not payload or len(payload) > _MAX_REQUEST_BYTES:
        raise SupervisorFailure("supervisor request size is invalid")
    try:
        request = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SupervisorFailure("supervisor request is invalid JSON") from exc
    expected = {
        "schema_version",
        "workflow_id",
        "authority_revision",
        "generation_id",
        "phase_id",
        "task_id",
        "plan_sha256",
        "generation_claim_ref",
        "generation_claim_sha256",
        "runtime_bundle_sha256",
        "codex_binary",
        "codex_binary_sha256",
        "transport_executable_sha256",
        "transport_adapter_sha256",
        "command",
        "command_sha256",
        "cwd",
        "work_mode",
        "write_roots",
        "environment",
        "audit_marker",
        "deadline_at",
        "deadline_monotonic",
        "boot_identity",
        "terminate_grace_seconds",
        "log_limit_bytes",
        "stdout_ref",
        "stderr_ref",
        "receipt_ref",
    }
    if not isinstance(request, dict) or set(request) != expected:
        raise SupervisorFailure("supervisor request contract is invalid")
    if request["schema_version"] != "agent-workflow.supervisor-request.vnext.v2":
        raise SupervisorFailure("supervisor request schema is invalid")
    for key in ("workflow_id", "generation_id", "phase_id", "task_id", "audit_marker"):
        if not isinstance(request[key], str) or not request[key]:
            raise SupervisorFailure(f"supervisor request {key} is invalid")
    for key in (
        "plan_sha256",
        "generation_claim_sha256",
        "runtime_bundle_sha256",
        "codex_binary_sha256",
        "transport_executable_sha256",
    ):
        if not isinstance(request[key], str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", request[key]):
            raise SupervisorFailure(f"supervisor request {key} is invalid")
    claim_path = _safe_file(root, request["generation_claim_ref"], "generation claim")
    if claim_path.is_symlink() or not claim_path.is_file() or _digest(claim_path.read_bytes()) != request["generation_claim_sha256"]:
        raise SupervisorFailure("supervisor request generation claim binding drifted")
    if not isinstance(request["authority_revision"], int) or isinstance(request["authority_revision"], bool) or request["authority_revision"] < 1:
        raise SupervisorFailure("supervisor request authority revision is invalid")
    command = request["command"]
    if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
        raise SupervisorFailure("supervisor request command is invalid")
    if request["command_sha256"] != _digest(_canonical(command)):
        raise SupervisorFailure("supervisor request command digest does not match")
    codex_binary = Path(request["codex_binary"])
    transport_executable = Path(command[0])
    if (
        not codex_binary.is_absolute()
        or codex_binary.is_symlink()
        or not codex_binary.is_file()
        or _digest(codex_binary.read_bytes()) != request["codex_binary_sha256"]
    ):
        raise SupervisorFailure("supervisor request Codex binary authority drifted")
    if (
        not transport_executable.is_absolute()
        or transport_executable.is_symlink()
        or not transport_executable.is_file()
        or _digest(transport_executable.read_bytes()) != request["transport_executable_sha256"]
    ):
        raise SupervisorFailure("supervisor request transport executable authority drifted")
    adapter_sha = request["transport_adapter_sha256"]
    if adapter_sha is None:
        if codex_binary.resolve() != transport_executable.resolve():
            raise SupervisorFailure("direct Codex transport does not execute the sealed binary")
    else:
        if not isinstance(adapter_sha, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", adapter_sha):
            raise SupervisorFailure("supervisor request transport adapter digest is invalid")
        if len(command) < 2:
            raise SupervisorFailure("supervisor request transport adapter is missing")
        adapter = Path(command[1])
        if (
            not adapter.is_absolute()
            or adapter.is_symlink()
            or not adapter.is_file()
            or _digest(adapter.read_bytes()) != adapter_sha
            or command.count("--codex") != 1
            or command[command.index("--codex") + 1] != os.fspath(codex_binary)
        ):
            raise SupervisorFailure("supervisor request transport adapter authority drifted")
    if f'agent_workflow_audit_marker="{request["audit_marker"]}"' not in command:
        raise SupervisorFailure("supervisor request command does not bind the audit marker")
    environment = request["environment"]
    if not isinstance(environment, dict) or not environment or not all(
        isinstance(key, str) and key and isinstance(value, str)
        for key, value in environment.items()
    ):
        raise SupervisorFailure("supervisor request environment is invalid")
    cwd = Path(request["cwd"])
    if not cwd.is_absolute() or cwd.is_symlink() or not cwd.is_dir():
        raise SupervisorFailure("supervisor request cwd is invalid")
    if (
        request["work_mode"] not in {"read", "write"}
        or not isinstance(request["write_roots"], list)
        or not all(isinstance(item, str) and item for item in request["write_roots"])
        or (request["work_mode"] == "read" and request["write_roots"])
        or (request["work_mode"] == "write" and not request["write_roots"])
    ):
        raise SupervisorFailure("supervisor request work-mode authority is invalid")
    _parse_time(request["deadline_at"], "supervisor request deadline")
    if not isinstance(request["deadline_monotonic"], (int, float)) or isinstance(request["deadline_monotonic"], bool) or request["deadline_monotonic"] <= 0:
        raise SupervisorFailure("supervisor request monotonic deadline is invalid")
    if not isinstance(request["boot_identity"], str) or not request["boot_identity"]:
        raise SupervisorFailure("supervisor request boot identity is invalid")
    if enforce_boot and request["boot_identity"] != process_identity(1):
        raise SupervisorFailure("supervisor request monotonic deadline belongs to another host boot")
    grace = request["terminate_grace_seconds"]
    if not isinstance(grace, (int, float)) or isinstance(grace, bool) or grace < 0 or grace > 30:
        raise SupervisorFailure("supervisor request grace period is invalid")
    limit = request["log_limit_bytes"]
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1024 or limit > 64 * 1024 * 1024:
        raise SupervisorFailure("supervisor request log limit is invalid")
    for key in ("stdout_ref", "stderr_ref", "receipt_ref"):
        _safe_file(root, request[key], f"supervisor request {key}")
    return request, payload


def load_request(
    root: Path,
    request_ref: str,
    *,
    enforce_boot: bool = True,
) -> tuple[dict[str, Any], bytes]:
    """Read and validate one immutable watchdog launch request."""

    return _load_request(Path(root).resolve(), request_ref, enforce_boot=enforce_boot)


def live_marker_processes(marker: str) -> list[dict[str, Any]]:
    """Return live processes carrying one exact workflow audit marker."""

    if not isinstance(marker, str) or not marker:
        raise SupervisorFailure("audit marker is invalid")
    return _scan_marker(marker)


def _drain(stream: BinaryIO, path: Path, limit: int, outcome: dict[str, Any]) -> None:
    written = 0
    overflow = False
    flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise SupervisorFailure("watchdog log target is not a regular file")
        while chunk := stream.read(65536):
            remaining = max(0, limit - written)
            if remaining:
                view = memoryview(chunk[:remaining])
                while view:
                    count = os.write(descriptor, view)
                    if count <= 0:
                        raise SupervisorFailure("watchdog log write made no progress")
                    written += count
                    view = view[count:]
            if len(chunk) > remaining:
                overflow = True
        os.fsync(descriptor)
        outcome.update(overflow=overflow)
    except Exception as exc:  # pragma: no cover - defensive OS seam
        outcome["error"] = str(exc)
    finally:
        os.close(descriptor)
        stream.close()


def _cancel_exists(root: Path, authority_revision: int) -> bool:
    path = root / "amendments" / "cancel.json"
    if not path.exists():
        return False
    if path.is_symlink() or not path.is_file():
        raise SupervisorFailure("cancel request is unsafe")
    try:
        value = json.loads(path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SupervisorFailure("cancel request is invalid") from exc
    if not isinstance(value, dict) or value.get("authority_revision") != authority_revision:
        raise SupervisorFailure("cancel request authority revision is stale")
    return True


def validate_receipt(value: Any) -> dict[str, Any]:
    """Validate one runtime-evidence terminal receipt; this is not lifecycle state."""

    expected = {
        "schema_version", "workflow_id", "authority_revision", "generation_id",
        "phase_id", "task_id", "request_ref", "request_sha256", "active_ref",
        "active_sha256", "producer", "status", "exit_code", "term_sent",
        "kill_sent", "started_at", "finished_at", "stdout_ref", "stdout_sha256",
        "stderr_ref", "stderr_sha256", "log_limit_exceeded", "group_reaped",
        "group_gone_observed", "escape_scan", "reconcile_reason",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise SupervisorFailure("supervisor receipt contract is invalid")
    if value["schema_version"] != "agent-workflow.supervisor-receipt.vnext.v1":
        raise SupervisorFailure("supervisor receipt schema is invalid")
    if value["producer"] not in {"watchdog", "reconciler"}:
        raise SupervisorFailure("supervisor receipt producer is invalid")
    if value["status"] not in {
        "completed", "failed", "timed_out", "cancelled", "not_started_deadline",
        "escaped_process_detected",
    }:
        raise SupervisorFailure("supervisor receipt status is invalid")
    if value["exit_code"] is not None and (
        not isinstance(value["exit_code"], int) or isinstance(value["exit_code"], bool)
    ):
        raise SupervisorFailure("supervisor receipt exit code is invalid")
    for key in ("term_sent", "kill_sent", "log_limit_exceeded", "group_reaped", "group_gone_observed"):
        if not isinstance(value[key], bool):
            raise SupervisorFailure(f"supervisor receipt {key} must be boolean")
    for key in ("request_sha256", "stdout_sha256", "stderr_sha256"):
        if not isinstance(value[key], str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value[key]):
            raise SupervisorFailure(f"supervisor receipt {key} is invalid")
    if (value["active_ref"] is None) != (value["active_sha256"] is None):
        raise SupervisorFailure("supervisor receipt active evidence is incomplete")
    if value["active_sha256"] is not None and not re.fullmatch(r"sha256:[0-9a-f]{64}", value["active_sha256"]):
        raise SupervisorFailure("supervisor receipt active digest is invalid")
    started = _parse_time(value["started_at"], "supervisor receipt started_at")
    finished = _parse_time(value["finished_at"], "supervisor receipt finished_at")
    if finished < started:
        raise SupervisorFailure("supervisor receipt timestamps are inverted")
    escape = value["escape_scan"]
    if not isinstance(escape, dict) or set(escape) != {"status", "matches"} or escape["status"] not in {"clear", "detected", "not_run"} or not isinstance(escape["matches"], list):
        raise SupervisorFailure("supervisor receipt escape scan is invalid")
    if value["status"] == "escaped_process_detected" and escape["status"] != "detected":
        raise SupervisorFailure("escaped process status requires detected evidence")
    if value["producer"] == "reconciler" and value["group_reaped"]:
        raise SupervisorFailure("reconciler cannot claim waitpid reaping")
    return value


def _terminate(process: subprocess.Popen[bytes], grace_seconds: float) -> tuple[int, bool]:
    killed = False
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return process.wait(), killed
    try:
        return process.wait(timeout=grace_seconds), killed
    except subprocess.TimeoutExpired:
        killed = True
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return process.wait(), killed


def _worker_entry(root: Path, request_ref: str, audit_marker: str) -> int:
    """Wait behind the active-record handshake, then replace this process with the worker."""

    root = Path(root).resolve()
    request, _ = _load_request(root, request_ref)
    if audit_marker != request["audit_marker"]:
        raise SupervisorFailure("worker bootstrap marker does not match the launch seal")
    release_ref = f"{Path(request_ref).parent.as_posix()}/release.json"
    release_path = _safe_file(root, release_ref, "worker release fence")
    deadline = request["deadline_monotonic"]
    while not release_path.exists():
        if time.monotonic() >= deadline:
            return 124
        if _cancel_exists(root, request["authority_revision"]):
            return 125
        time.sleep(0.01)
    if release_path.is_symlink() or not release_path.is_file():
        raise SupervisorFailure("worker release fence is unsafe")
    if _cancel_exists(root, request["authority_revision"]):
        return 125
    os.chdir(request["cwd"])
    os.execvpe(request["command"][0], request["command"], request["environment"])
    return 127  # pragma: no cover - exec replaces the process


def supervise(root: Path, request_ref: str) -> dict[str, Any]:
    """Run one request to a create-once terminal receipt."""

    root = Path(root).resolve()
    request, request_payload = _load_request(root, request_ref)
    receipt_path = _safe_file(root, request["receipt_ref"], "supervisor receipt")
    if receipt_path.exists() or receipt_path.is_symlink():
        raise SupervisorFailure("supervisor receipt must start absent")
    started = datetime.now(timezone.utc)
    deadline = _parse_time(request["deadline_at"], "supervisor request deadline")
    monotonic_deadline = float(request["deadline_monotonic"])
    cancelled_before_launch = _cancel_exists(root, request["authority_revision"])
    if cancelled_before_launch or time.monotonic() >= monotonic_deadline:
        status = "cancelled" if cancelled_before_launch else "not_started_deadline"
        receipt = {
            "schema_version": "agent-workflow.supervisor-receipt.vnext.v1",
            "workflow_id": request["workflow_id"],
            "authority_revision": request["authority_revision"],
            "generation_id": request["generation_id"],
            "phase_id": request["phase_id"],
            "task_id": request["task_id"],
            "request_ref": request_ref,
            "request_sha256": _digest(request_payload),
            "active_ref": None,
            "active_sha256": None,
            "producer": "watchdog",
            "status": status,
            "exit_code": None,
            "term_sent": False,
            "kill_sent": False,
            "started_at": _timestamp(started),
            "finished_at": _timestamp(datetime.now(timezone.utc)),
            "stdout_ref": request["stdout_ref"],
            "stdout_sha256": _digest(b""),
            "stderr_ref": request["stderr_ref"],
            "stderr_sha256": _digest(b""),
            "log_limit_exceeded": False,
            "group_reaped": True,
            "group_gone_observed": True,
            "escape_scan": {"status": "not_run", "matches": []},
            "reconcile_reason": None,
        }
        create_once_bytes(root, request["stdout_ref"], b"")
        create_once_bytes(root, request["stderr_ref"], b"")
        create_once_json(root, request["receipt_ref"], validate_receipt(receipt))
        return receipt

    stdout_path = _safe_file(root, request["stdout_ref"], "watchdog stdout")
    stderr_path = _safe_file(root, request["stderr_ref"], "watchdog stderr")
    create_once_bytes(root, request["stdout_ref"], b"")
    create_once_bytes(root, request["stderr_ref"], b"")
    bootstrap_command = [
        sys.executable,
        os.fspath(Path(__file__).resolve()),
        "worker",
        "--root",
        os.fspath(root),
        "--request-ref",
        request_ref,
        "--audit-marker",
        request["audit_marker"],
    ]
    process = subprocess.Popen(
        bootstrap_command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        close_fds=True,
    )
    if process.stdout is None or process.stderr is None:  # pragma: no cover
        raise SupervisorFailure("worker pipes were not created")
    worker_birth = process_birth(process.pid)
    supervisor_birth = process_birth(os.getpid())
    supervisor_command = process_command(os.getpid())
    if worker_birth is None or supervisor_birth is None or supervisor_command is None:
        _terminate(process, float(request["terminate_grace_seconds"]))
        raise SupervisorFailure("could not establish process identities")
    record_ref = f"runtime/processes/{request['phase_id']}/{request['task_id']}.json"
    record_path = create_once_json(
        root,
        record_ref,
        {
            "schema_version": "agent-workflow.process-record.vnext.v4",
            "workflow_id": request["workflow_id"],
            "authority_revision": request["authority_revision"],
            "generation_id": request["generation_id"],
            "phase_id": request["phase_id"],
            "task_id": request["task_id"],
            "supervisor_pid": os.getpid(),
            "supervisor_birth": supervisor_birth,
            "supervisor_sid": os.getsid(os.getpid()),
            "supervisor_command": supervisor_command,
            "supervisor_command_sha256": _digest(supervisor_command.encode()),
            "pid": process.pid,
            "pgid": process.pid,
            "sid": process.pid,
            "audit_marker": request["audit_marker"],
            "process_birth": worker_birth,
            "command": request["command"],
            "command_sha256": request["command_sha256"],
            "request_ref": request_ref,
            "request_sha256": _digest(request_payload),
            "plan_sha256": request["plan_sha256"],
            "generation_claim_ref": request["generation_claim_ref"],
            "generation_claim_sha256": request["generation_claim_sha256"],
            "runtime_bundle_sha256": request["runtime_bundle_sha256"],
            "codex_binary": request["codex_binary"],
            "codex_binary_sha256": request["codex_binary_sha256"],
            "transport_executable_sha256": request["transport_executable_sha256"],
            "transport_adapter_sha256": request["transport_adapter_sha256"],
            "deadline_at": request["deadline_at"],
            "deadline_monotonic": request["deadline_monotonic"],
            "boot_identity": request["boot_identity"],
            "terminal_ref": request["receipt_ref"],
            "started_at": _timestamp(started),
        },
    )
    active_payload = record_path.read_bytes()
    release_ref = f"{Path(request_ref).parent.as_posix()}/release.json"
    create_once_json(
        root,
        release_ref,
        {
            "schema_version": "agent-workflow.worker-release.vnext.v1",
            "request_ref": request_ref,
            "request_sha256": _digest(request_payload),
            "active_ref": record_ref,
            "active_sha256": _digest(active_payload),
            "released_at": _timestamp(datetime.now(timezone.utc)),
        },
    )
    stdout_outcome: dict[str, Any] = {}
    stderr_outcome: dict[str, Any] = {}
    drainers = [
        threading.Thread(target=_drain, args=(process.stdout, stdout_path, request["log_limit_bytes"], stdout_outcome), daemon=True),
        threading.Thread(target=_drain, args=(process.stderr, stderr_path, request["log_limit_bytes"], stderr_outcome), daemon=True),
    ]
    for drainer in drainers:
        drainer.start()
    term_sent = False
    kill_sent = False
    timed_out = False
    escaped_observed: list[dict[str, Any]] = []
    try:
        remaining = max(0.0, monotonic_deadline - time.monotonic())
        try:
            exit_code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            timed_out = True
            term_sent = True
            exit_code, kill_sent = _terminate(process, float(request["terminate_grace_seconds"]))
        for drainer in drainers:
            drainer.join(timeout=float(request["terminate_grace_seconds"]) + 1.0)
        if any(drainer.is_alive() for drainer in drainers):
            escaped_observed = _scan_marker(request["audit_marker"])
            _kill_marker_matches(
                request["audit_marker"],
                escaped_observed,
                grace_seconds=float(request["terminate_grace_seconds"]) + 1.0,
            )
            for drainer in drainers:
                drainer.join(timeout=float(request["terminate_grace_seconds"]) + 1.0)
            if any(drainer.is_alive() for drainer in drainers):
                raise SupervisorFailure("worker pipe remained open after escaped-process reap")
        drain_error = stdout_outcome.get("error") or stderr_outcome.get("error")
        if drain_error:
            raise SupervisorFailure(f"worker log drain failed: {drain_error}")
        stdout_payload = stdout_path.read_bytes()
        stderr_payload = stderr_path.read_bytes()
        cancelled = _cancel_exists(root, request["authority_revision"])
        escaped = escaped_observed or _scan_marker(request["audit_marker"])
        if escaped and not _kill_marker_matches(
            request["audit_marker"],
            escaped,
            grace_seconds=float(request["terminate_grace_seconds"]) + 1.0,
        ):
            raise SupervisorFailure("escaped marker process could not be terminated")
        status = "escaped_process_detected" if escaped else "cancelled" if cancelled else "timed_out" if timed_out else "completed" if exit_code == 0 else "failed"
        receipt = {
            "schema_version": "agent-workflow.supervisor-receipt.vnext.v1",
            "workflow_id": request["workflow_id"],
            "authority_revision": request["authority_revision"],
            "generation_id": request["generation_id"],
            "phase_id": request["phase_id"],
            "task_id": request["task_id"],
            "request_ref": request_ref,
            "request_sha256": _digest(request_payload),
            "active_ref": record_ref,
            "active_sha256": _digest(active_payload),
            "producer": "watchdog",
            "status": status,
            "exit_code": exit_code,
            "term_sent": term_sent,
            "kill_sent": kill_sent,
            "started_at": _timestamp(started),
            "finished_at": _timestamp(datetime.now(timezone.utc)),
            "stdout_ref": request["stdout_ref"],
            "stdout_sha256": _digest(stdout_payload),
            "stderr_ref": request["stderr_ref"],
            "stderr_sha256": _digest(stderr_payload),
            "log_limit_exceeded": bool(stdout_outcome.get("overflow") or stderr_outcome.get("overflow")),
            "group_reaped": process.poll() is not None,
            "group_gone_observed": process.poll() is not None,
            "escape_scan": {"status": "detected" if escaped else "clear", "matches": escaped},
            "reconcile_reason": None,
        }
        create_once_json(root, request["receipt_ref"], validate_receipt(receipt))
        return receipt
    finally:
        if process.poll() is None:
            _terminate(process, float(request["terminate_grace_seconds"]))
        # active.json is immutable evidence. terminal.json, not deletion, closes it.


def launch(root: Path, request_ref: str) -> subprocess.Popen[bytes]:
    """Launch a detached watchdog without giving the caller anonymous pipes."""

    return subprocess.Popen(
        [sys.executable, os.fspath(Path(__file__).resolve()), "watch", "--root", os.fspath(Path(root).resolve()), "--request-ref", request_ref],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


def reconcile(root: Path, *, grace_seconds: float = 1.0) -> dict[str, Any]:
    """Seal terminal evidence for attempts whose watchdog no longer exists."""

    root = Path(root).resolve()
    if grace_seconds < 0 or grace_seconds > 30:
        raise SupervisorFailure("reconcile grace period is invalid")
    records_root = root / "runtime" / "processes"
    records = sorted(records_root.rglob("*.json")) if records_root.is_dir() else []
    summary = {"active": [], "already_terminal": [], "reconciled": []}
    for path in records:
        if path.is_symlink() or not path.is_file():
            raise SupervisorFailure("active process record is unsafe")
        payload = path.read_bytes()
        try:
            record = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SupervisorFailure("active process record is invalid") from exc
        required = {
            "schema_version",
            "workflow_id",
            "authority_revision",
            "generation_id",
            "phase_id",
            "task_id",
            "supervisor_pid",
            "supervisor_birth",
            "supervisor_sid",
            "supervisor_command",
            "supervisor_command_sha256",
            "pid",
            "pgid",
            "sid",
            "audit_marker",
            "process_birth",
            "command",
            "command_sha256",
            "request_ref",
            "request_sha256",
            "plan_sha256",
            "generation_claim_ref",
            "generation_claim_sha256",
            "runtime_bundle_sha256",
            "codex_binary",
            "codex_binary_sha256",
            "transport_executable_sha256",
            "transport_adapter_sha256",
            "deadline_at",
            "deadline_monotonic",
            "boot_identity",
            "terminal_ref",
            "started_at",
        }
        if (
            not isinstance(record, dict)
            or set(record) != required
            or record.get("schema_version") != "agent-workflow.process-record.vnext.v4"
        ):
            raise SupervisorFailure("active process record contract is invalid")
        request, request_payload = _load_request(root, record["request_ref"])
        if record["boot_identity"] != process_identity(1):
            raise SupervisorFailure("reconcile cannot prove monotonic deadline across host boot")
        if (
            _digest(request_payload) != record["request_sha256"]
            or request["receipt_ref"] != record["terminal_ref"]
            or request["task_id"] != record["task_id"]
            or request["command_sha256"] != record["command_sha256"]
            or request["plan_sha256"] != record["plan_sha256"]
            or request["generation_claim_ref"] != record["generation_claim_ref"]
            or request["generation_claim_sha256"] != record["generation_claim_sha256"]
            or request["runtime_bundle_sha256"] != record["runtime_bundle_sha256"]
            or request["codex_binary"] != record["codex_binary"]
            or request["codex_binary_sha256"] != record["codex_binary_sha256"]
            or request["transport_executable_sha256"] != record["transport_executable_sha256"]
            or request["transport_adapter_sha256"] != record["transport_adapter_sha256"]
        ):
            raise SupervisorFailure("active process record fence drifted")
        terminal_path = _safe_file(root, record["terminal_ref"], "terminal receipt")
        if terminal_path.exists():
            if terminal_path.is_symlink() or not terminal_path.is_file():
                raise SupervisorFailure("terminal receipt is unsafe")
            try:
                terminal = validate_receipt(json.loads(terminal_path.read_bytes()))
            except (UnicodeDecodeError, json.JSONDecodeError, SupervisorFailure) as exc:
                raise SupervisorFailure("terminal receipt is invalid") from exc
            expected_active_ref = path.relative_to(root).as_posix()
            if (
                terminal["workflow_id"] != request["workflow_id"]
                or terminal["authority_revision"] != request["authority_revision"]
                or terminal["generation_id"] != request["generation_id"]
                or terminal["phase_id"] != request["phase_id"]
                or terminal["task_id"] != request["task_id"]
                or terminal["request_ref"] != record["request_ref"]
                or terminal["request_sha256"] != record["request_sha256"]
                or terminal["active_ref"] != expected_active_ref
                or terminal["active_sha256"] != _digest(payload)
                or terminal["stdout_ref"] != request["stdout_ref"]
                or terminal["stderr_ref"] != request["stderr_ref"]
                or not terminal["group_gone_observed"]
            ):
                raise SupervisorFailure("terminal receipt fence drifted")
            stdout_path = _safe_file(root, request["stdout_ref"], "watchdog stdout")
            stderr_path = _safe_file(root, request["stderr_ref"], "watchdog stderr")
            if (
                stdout_path.is_symlink()
                or stderr_path.is_symlink()
                or not stdout_path.is_file()
                or not stderr_path.is_file()
                or _digest(stdout_path.read_bytes()) != terminal["stdout_sha256"]
                or _digest(stderr_path.read_bytes()) != terminal["stderr_sha256"]
            ):
                raise SupervisorFailure("terminal receipt log evidence drifted")
            if (
                process_birth(record["pid"]) == record["process_birth"]
                or _scan_marker(record["audit_marker"])
            ):
                raise SupervisorFailure("terminal receipt contradicts live process evidence")
            summary["already_terminal"].append(record["task_id"])
            continue
        if process_birth(record["supervisor_pid"]) == record["supervisor_birth"]:
            live_supervisor_command = process_command(record["supervisor_pid"])
            if (
                os.getsid(record["supervisor_pid"]) == record["supervisor_sid"]
                and live_supervisor_command == record["supervisor_command"]
                and _digest(live_supervisor_command.encode()) == record["supervisor_command_sha256"]
            ):
                summary["active"].append(record["task_id"])
                continue
        pid = record["pid"]
        worker_live = process_birth(pid) == record["process_birth"]
        term_sent = False
        if worker_live:
            command = process_command(pid)
            if (
                record["pgid"] != pid
                or record["sid"] != pid
                or os.getsid(pid) != pid
                or command is None
                or not command_matches_request(command, request, record["request_ref"])
            ):
                raise SupervisorFailure("reconcile worker ownership proof is invalid")
            try:
                os.killpg(pid, signal.SIGTERM)
                term_sent = True
            except ProcessLookupError:
                worker_live = False
        deadline = time.monotonic() + grace_seconds
        while worker_live and time.monotonic() < deadline:
            worker_live = process_birth(pid) == record["process_birth"]
            if worker_live:
                time.sleep(0.02)
        kill_sent = False
        if worker_live:
            command = process_command(pid)
            if command is not None and record["audit_marker"] in command:
                try:
                    os.killpg(pid, signal.SIGKILL)
                    kill_sent = True
                except ProcessLookupError:
                    pass
        disappear_deadline = time.monotonic() + grace_seconds
        while process_birth(pid) == record["process_birth"] and time.monotonic() < disappear_deadline:
            time.sleep(0.02)
        group_gone_observed = process_birth(pid) != record["process_birth"]
        stdout_path = _safe_file(root, request["stdout_ref"], "watchdog stdout")
        stderr_path = _safe_file(root, request["stderr_ref"], "watchdog stderr")
        if stdout_path.is_symlink() or stderr_path.is_symlink() or not stdout_path.is_file() or not stderr_path.is_file():
            raise SupervisorFailure("reconcile logs are missing or unsafe")
        stdout_payload = stdout_path.read_bytes()
        stderr_payload = stderr_path.read_bytes()
        escaped = _scan_marker(record["audit_marker"])
        if escaped and not _kill_marker_matches(
            record["audit_marker"],
            escaped,
            grace_seconds=grace_seconds,
        ):
            raise SupervisorFailure("reconcile could not terminate escaped marker process")
        if not group_gone_observed:
            raise SupervisorFailure("reconcile could not prove the owned process group terminal")
        now = datetime.now(timezone.utc)
        receipt = {
            "schema_version": "agent-workflow.supervisor-receipt.vnext.v1",
            "workflow_id": request["workflow_id"],
            "authority_revision": request["authority_revision"],
            "generation_id": request["generation_id"],
            "phase_id": request["phase_id"],
            "task_id": request["task_id"],
            "request_ref": record["request_ref"],
            "request_sha256": record["request_sha256"],
            "active_ref": path.relative_to(root).as_posix(),
            "active_sha256": _digest(payload),
            "producer": "reconciler",
            "status": "escaped_process_detected" if escaped else "failed",
            "exit_code": None,
            "term_sent": term_sent,
            "kill_sent": kill_sent,
            "started_at": record["started_at"],
            "finished_at": _timestamp(now),
            "stdout_ref": request["stdout_ref"],
            "stdout_sha256": _digest(stdout_payload),
            "stderr_ref": request["stderr_ref"],
            "stderr_sha256": _digest(stderr_payload),
            "log_limit_exceeded": len(stdout_payload) >= request["log_limit_bytes"] or len(stderr_payload) >= request["log_limit_bytes"],
            "group_reaped": False,
            "group_gone_observed": group_gone_observed,
            "escape_scan": {"status": "detected" if escaped else "clear", "matches": escaped},
            "reconcile_reason": "watchdog_lost",
        }
        try:
            create_once_json(root, record["terminal_ref"], validate_receipt(receipt))
        except ArtifactError:
            if not terminal_path.is_file():
                raise
        summary["reconciled"].append(record["task_id"])
    return {"status": "reconciled", **summary}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    watch = sub.add_parser("watch")
    watch.add_argument("--root", type=Path, required=True)
    watch.add_argument("--request-ref", required=True)
    worker = sub.add_parser("worker")
    worker.add_argument("--root", type=Path, required=True)
    worker.add_argument("--request-ref", required=True)
    worker.add_argument("--audit-marker", required=True)
    reconcile_parser = sub.add_parser("reconcile")
    reconcile_parser.add_argument("--root", type=Path, required=True)
    reconcile_parser.add_argument("--grace-seconds", type=float, default=1.0)
    args = parser.parse_args(argv)
    try:
        if args.command == "worker":
            return _worker_entry(args.root, args.request_ref, args.audit_marker)
        if args.command == "reconcile":
            print(json.dumps(reconcile(args.root, grace_seconds=args.grace_seconds), sort_keys=True))
            return 0
        receipt = supervise(args.root, args.request_ref)
    except (SupervisorFailure, ArtifactError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps({"status": receipt["status"], "receipt_ref": args.request_ref}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
