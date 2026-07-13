#!/usr/bin/env python3
"""Create-once host validation receipts for an integrated source workflow."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

sys.dont_write_bytecode = True

from artifact_store import ArtifactError, create_once_bytes, create_once_json
from phase_protocol import (
    ProtocolError,
    _read_artifact_bytes,
    validate_contract,
    validate_host_validation_receipt,
)
from repository_state import RepositoryStateError, collect_repository_state
from recovery_runtime import RecoveryError, current_authority_revision


class HostValidationError(RuntimeError):
    pass


def _canonical(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_json_file(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise HostValidationError(f"{label} must be one bounded regular JSON file")
    try:
        value = json.loads(path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HostValidationError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise HostValidationError(f"{label} must be an object")
    return value


def _git(repository: Path, *args: str) -> bytes:
    completed = subprocess.run(
        ["git", *args], cwd=repository, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
    )
    if completed.returncode:
        raise HostValidationError(completed.stderr.decode(errors="replace").strip())
    return completed.stdout


def _repository_evidence(repository: Path) -> dict[str, str]:
    try:
        return collect_repository_state(repository)
    except RepositoryStateError as exc:
        raise HostValidationError(f"repository evidence failed closed: {exc}") from exc


def _safe_cwd(repository: Path, value: str) -> Path:
    if not isinstance(value, str) or not value:
        raise HostValidationError("host validation cwd is unsafe")
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(part in {"", ".."} for part in relative.parts):
        raise HostValidationError("host validation cwd is unsafe")
    cwd = (repository / relative).resolve(strict=True)
    if not cwd.is_relative_to(repository) or not cwd.is_dir():
        raise HostValidationError("host validation cwd escapes the repository")
    return cwd


def _sanitized_environment(sandbox_root: Path, overrides: dict[str, Any]) -> dict[str, str]:
    if not isinstance(overrides, dict):
        raise HostValidationError("host validation environment must be an object")
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "HOME": os.fspath(sandbox_root / "home"),
        "TMPDIR": os.fspath(sandbox_root / "tmp"),
        "CI": "1",
    }
    allowed_overrides = {
        "NODE_ENV",
        "JIFG_DATA_DIR",
        "JIFG_STATE_DIR",
        "JIFG_CANONICAL_PROD_SAVE_PATH",
        "JIFG_CODEX_MODE",
    }
    for key, value in overrides.items():
        if (
            key not in allowed_overrides
            or not isinstance(value, str)
            or len(value) > 4096
        ):
            raise HostValidationError("host validation environment override is unsafe")
        environment[key] = value
    return environment


def run_validation(root: Path, repository: Path, spec_source: Path) -> dict[str, Any]:
    root = Path(root).resolve(strict=True)
    repository = Path(repository).resolve(strict=True)
    if root != repository / ".workflow" / root.name:
        raise HostValidationError("host validation root is not owned by the repository")
    workflow = _load_json_file(root / "workflow.json", "workflow seal")
    spec_path = Path(spec_source)
    spec = _load_json_file(spec_path, "host validation spec")
    spec_sha256 = _digest(spec_path.read_bytes())
    expected_keys = {
        "schema_version",
        "workflow_id",
        "authority_revision",
        "validation_id",
        "integration_receipt_ref",
        "integration_receipt_sha256",
        "cwd",
        "environment",
        "commands",
    }
    if set(spec) != expected_keys or spec.get("schema_version") != "agent-workflow.host-validation-spec.v1":
        raise HostValidationError("host validation spec contract is invalid")
    validation_id = spec.get("validation_id")
    try:
        live_authority_revision = current_authority_revision(root, workflow)
    except RecoveryError as exc:
        raise HostValidationError(
            f"host validation authority could not be reconstructed: {exc}"
        ) from exc
    if (
        spec.get("workflow_id") != workflow.get("workflow_id")
        or spec.get("authority_revision") != live_authority_revision
        or not isinstance(validation_id, str)
        or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", validation_id) is None
    ):
        raise HostValidationError("host validation identity or authority drifted")
    integration_ref = spec.get("integration_receipt_ref")
    integration_sha256 = spec.get("integration_receipt_sha256")
    if not isinstance(integration_ref, str) or re.fullmatch(r"phases/[^/]+/receipt\.json", integration_ref) is None:
        raise HostValidationError("host validation integration receipt ref is invalid")
    try:
        integration_payload = _read_artifact_bytes(root, integration_ref, integration_sha256)
        integration = json.loads(integration_payload)
        validate_contract("phase-receipt", integration)
    except (ProtocolError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HostValidationError("host validation integration receipt is invalid") from exc
    integration_phase = integration_ref.split("/")[1]
    if (
        integration.get("workflow_id") != workflow.get("workflow_id")
        or integration.get("phase_id") != integration_phase
        or integration.get("integration", {}).get("mode") != "isolated_exact_base"
        or integration.get("integration", {}).get("status") != "applied"
    ):
        raise HostValidationError("host validation requires an applied integration receipt")

    cwd = _safe_cwd(repository, spec.get("cwd"))
    sandbox_root = root / "runtime" / "host-validations" / validation_id
    for directory in (sandbox_root / "home", sandbox_root / "tmp"):
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        if directory.is_symlink() or not directory.is_dir():
            raise HostValidationError("host validation sandbox directory is unsafe")
    environment = _sanitized_environment(sandbox_root, spec.get("environment"))
    commands = spec.get("commands")
    if not isinstance(commands, list) or not commands or len(commands) > 16:
        raise HostValidationError("host validation commands must be a bounded non-empty list")

    receipt_ref = f"evidence/host-validations/{validation_id}/receipt.json"
    if (root / receipt_ref).exists() or (root / receipt_ref).is_symlink():
        receipt_path = root / receipt_ref
        receipt_sha256 = _digest(receipt_path.read_bytes())
        try:
            receipt = validate_host_validation_receipt(
                root,
                receipt_ref,
                receipt_sha256,
                workflow_id=workflow["workflow_id"],
                authority_revision=spec["authority_revision"],
            )
        except ProtocolError as exc:
            raise HostValidationError(f"host validation receipt replay failed: {exc}") from exc
        if (
            receipt["spec_sha256"] != spec_sha256
            or receipt["integration_receipt_ref"] != integration_ref
            or receipt["integration_receipt_sha256"] != integration_sha256
            or receipt["cwd"] != os.fspath(cwd)
            or receipt["environment"] != environment
            or receipt["repository_after"] != _repository_evidence(repository)
        ):
            raise HostValidationError("host validation receipt drifted from its sealed inputs")
        expected_commands = []
        for item in commands:
            executable_value = shutil.which(item["argv"][0], path=environment["PATH"])
            if executable_value is None:
                raise HostValidationError(f"host validation executable is unavailable: {item['argv'][0]}")
            executable = Path(executable_value).resolve(strict=True)
            expected_commands.append({
                "id": item["id"],
                "argv": [os.fspath(executable), *item["argv"][1:]],
                "timeout_seconds": item["timeout_seconds"],
                "executable_sha256": _digest_file(executable),
            })
        observed_commands = [
            {
                "id": item["id"],
                "argv": item["argv"],
                "timeout_seconds": item["timeout_seconds"],
                "executable_sha256": item["executable_sha256"],
            }
            for item in receipt["commands"]
        ]
        if observed_commands != expected_commands[: len(observed_commands)]:
            raise HostValidationError("host validation receipt command authority drifted")
        if receipt["status"] == "pass" and observed_commands != expected_commands:
            raise HostValidationError("passing host validation receipt omitted sealed commands")
        if (
            receipt["status"] == "fail"
            and len(observed_commands) < len(expected_commands)
            and receipt["commands"][-1]["exit_code"] == 0
            and receipt["commands"][-1]["timed_out"] is False
        ):
            raise HostValidationError("failed host validation receipt stopped before a failing command")
        return {"status": receipt["status"], "receipt_ref": receipt_ref, "receipt_sha256": receipt_sha256, "replayed": True}

    before = _repository_evidence(repository)
    command_receipts: list[dict[str, Any]] = []
    overall_pass = True
    validation_started = _timestamp()
    for index, item in enumerate(commands):
        if not isinstance(item, dict) or set(item) != {"id", "argv", "timeout_seconds"}:
            raise HostValidationError("host validation command contract is invalid")
        command_id = item.get("id")
        argv = item.get("argv")
        timeout = item.get("timeout_seconds")
        if (
            not isinstance(command_id, str)
            or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", command_id) is None
            or not isinstance(argv, list)
            or not argv
            or not all(isinstance(value, str) and value and len(value) <= 8192 for value in argv)
            or not isinstance(timeout, int)
            or isinstance(timeout, bool)
            or not 1 <= timeout <= 3600
        ):
            raise HostValidationError("host validation command values are invalid")
        executable_value = shutil.which(argv[0], path=environment["PATH"])
        if executable_value is None:
            raise HostValidationError(f"host validation executable is unavailable: {argv[0]}")
        executable = Path(executable_value).resolve(strict=True)
        if executable.is_symlink() or not executable.is_file():
            raise HostValidationError("host validation executable is unsafe")
        command = [os.fspath(executable), *argv[1:]]
        started_at = _timestamp()
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
            exit_code = completed.returncode
            timed_out = False
            stdout = completed.stdout
            stderr = completed.stderr
        except subprocess.TimeoutExpired as exc:
            exit_code = 124
            timed_out = True
            stdout = exc.stdout or b""
            stderr = exc.stderr or b""
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if len(stdout) > 32 * 1024 * 1024 or len(stderr) > 32 * 1024 * 1024:
            raise HostValidationError("host validation log exceeded its bounded size")
        log_base = f"evidence/host-validations/{validation_id}/{index:02d}-{command_id}"
        stdout_path = create_once_bytes(root, f"{log_base}.stdout", stdout)
        stderr_path = create_once_bytes(root, f"{log_base}.stderr", stderr)
        command_receipts.append({
            "id": command_id,
            "argv": command,
            "argv_sha256": _digest(_canonical(command)),
            "executable_sha256": _digest_file(executable),
            "timeout_seconds": timeout,
            "started_at": started_at,
            "finished_at": _timestamp(),
            "elapsed_ms": elapsed_ms,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "stdout_ref": stdout_path.relative_to(root).as_posix(),
            "stdout_sha256": _digest(stdout),
            "stderr_ref": stderr_path.relative_to(root).as_posix(),
            "stderr_sha256": _digest(stderr),
        })
        if exit_code != 0:
            overall_pass = False
            break

    after = _repository_evidence(repository)
    repository_unchanged = before == after
    overall_pass = overall_pass and repository_unchanged
    receipt = {
        "schema_version": "agent-workflow.host-validation-receipt.v1",
        "workflow_id": workflow["workflow_id"],
        "authority_revision": spec["authority_revision"],
        "validation_id": validation_id,
        "status": "pass" if overall_pass else "fail",
        "spec_sha256": spec_sha256,
        "integration_receipt_ref": integration_ref,
        "integration_receipt_sha256": integration_sha256,
        "cwd": os.fspath(cwd),
        "environment": environment,
        "started_at": validation_started,
        "finished_at": _timestamp(),
        "repository_before": before,
        "repository_after": after,
        "repository_unchanged": repository_unchanged,
        "commands": command_receipts,
    }
    try:
        receipt_path = create_once_json(root, receipt_ref, receipt)
    except ArtifactError as exc:
        raise HostValidationError(str(exc)) from exc
    return {"status": receipt["status"], "receipt_ref": receipt_ref, "receipt_sha256": _digest(receipt_path.read_bytes()), "replayed": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--spec-source", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = run_validation(args.root, args.repo, args.spec_source)
    except HostValidationError as exc:
        print(json.dumps({"status": "runtime_failed", "reason": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
