#!/usr/bin/env python3
"""Run validation inside one lease-bound, policy-managed temporary root."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


LEASE_NAME = ".pi-skills-validation-lease.json"
DEFAULT_OWC_ROOT = Path("/Volumes/OWC-4TB")


class TemporaryPolicyError(RuntimeError):
    """Raised when temporary storage cannot be selected or reclaimed safely."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"


def regular_directory(path: Path, label: str) -> Path:
    expanded = path.expanduser()
    if expanded.is_symlink() or not expanded.is_dir():
        raise TemporaryPolicyError(f"{label} is unavailable or unsafe: {expanded}")
    return expanded.resolve(strict=True)


def resolve_policy_root() -> tuple[Path, dict[str, Any]]:
    explicit = os.environ.get("PI_SKILLS_TMP_ROOT", "").strip()
    if explicit:
        base = regular_directory(Path(explicit), "configured policy-managed temporary root")
        return base, {
            "selection": "explicit",
            "mount_root": str(base),
            "mount_device": base.stat().st_dev,
        }

    runner_temp = os.environ.get("RUNNER_TEMP", "").strip()
    if os.environ.get("CI") and runner_temp:
        base = regular_directory(Path(runner_temp), "CI policy-managed temporary root")
        return base, {
            "selection": "ci-runner",
            "mount_root": str(base),
            "mount_device": base.stat().st_dev,
        }

    mount = regular_directory(
        Path(os.environ.get("PI_SKILLS_OWC_ROOT", DEFAULT_OWC_ROOT)),
        "policy-managed temporary root",
    )
    if mount.stat().st_dev == Path("/").stat().st_dev:
        raise TemporaryPolicyError("policy-managed temporary root is unavailable: OWC mount identity is absent")
    base = mount / "tmp" / "pi-skills"
    base.mkdir(parents=True, exist_ok=True)
    if base.is_symlink() or not base.is_dir() or base.resolve(strict=True).stat().st_dev != mount.stat().st_dev:
        raise TemporaryPolicyError("policy-managed temporary root is unavailable: OWC temp path drifted")
    base = base.resolve(strict=True)
    return base, {
        "selection": "owc-default",
        "mount_root": str(mount),
        "mount_device": mount.stat().st_dev,
    }


def make_tree_removable(root: Path) -> None:
    if not root.exists():
        return
    root.chmod(0o700)
    for path in root.rglob("*"):
        if path.is_symlink():
            continue
        path.chmod(0o700 if path.is_dir() else 0o600)


def verify_lease(run_root: Path, expected: dict[str, Any]) -> None:
    lease_path = run_root / LEASE_NAME
    if lease_path.is_symlink() or not lease_path.is_file():
        raise TemporaryPolicyError("validation temporary lease is missing or unsafe")
    try:
        observed = json.loads(lease_path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TemporaryPolicyError("validation temporary lease is unreadable") from exc
    if observed != expected:
        raise TemporaryPolicyError("validation temporary lease authority drifted")
    base = Path(expected["base_root"])
    if (
        run_root.resolve(strict=True) != Path(expected["run_root"])
        or run_root.parent.resolve(strict=True) != base
        or base.stat().st_dev != expected["base_device"]
        or Path(expected["mount_root"]).stat().st_dev != expected["mount_device"]
    ):
        raise TemporaryPolicyError("validation temporary mount identity drifted")


def run_with_policy_tmp(repo: Path, command: list[str]) -> int:
    if not command:
        raise TemporaryPolicyError("validation command is required")
    repo = repo.resolve(strict=True)
    if not repo.is_dir():
        raise TemporaryPolicyError("repository root must be a directory")
    base, mount = resolve_policy_root()
    run_root = Path(tempfile.mkdtemp(prefix="pi-skills-validation-", dir=base)).resolve(strict=True)
    nonce = secrets.token_hex(32)
    lease = {
        "schema_version": "pi-skills.validation-temp-lease.v1",
        "nonce": nonce,
        "pid": os.getpid(),
        "repository_root": str(repo),
        "base_root": str(base),
        "base_device": base.stat().st_dev,
        "run_root": str(run_root),
        "mount_root": mount["mount_root"],
        "mount_device": mount["mount_device"],
        "selection": mount["selection"],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    lease_path = run_root / LEASE_NAME
    descriptor = os.open(lease_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(canonical_json(lease))

    environment = os.environ.copy()
    environment.update(
        {
            "TMPDIR": str(run_root),
            "TMP": str(run_root),
            "TEMP": str(run_root),
            "PI_SKILLS_VALIDATION_TMP_ACTIVE": "1",
            "PI_SKILLS_VALIDATION_TMP_LEASE": str(lease_path),
        }
    )
    result: subprocess.CompletedProcess[bytes] | None = None
    cleanup_error: Exception | None = None
    try:
        result = subprocess.run(command, cwd=repo, env=environment, check=False)
    finally:
        try:
            verify_lease(run_root, lease)
            make_tree_removable(run_root)
            shutil.rmtree(run_root)
        except Exception as exc:  # fail closed and preserve evidence on authority drift
            cleanup_error = exc
    if cleanup_error is not None:
        print(f"validation temporary cleanup blocked: {cleanup_error}", file=sys.stderr)
        return 74
    if result is None:
        return 70
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command_name", required=True)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--repo", type=Path, required=True)
    run_parser.add_argument("command", nargs=argparse.REMAINDER)
    arguments = parser.parse_args()
    command = list(arguments.command)
    if command and command[0] == "--":
        command.pop(0)
    try:
        return run_with_policy_tmp(arguments.repo, command)
    except (OSError, TemporaryPolicyError) as exc:
        print(f"policy-managed temporary root is unavailable: {exc}", file=sys.stderr)
        return 73


if __name__ == "__main__":
    raise SystemExit(main())
