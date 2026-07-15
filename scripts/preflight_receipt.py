#!/usr/bin/env python3
"""Bind one successful repository preflight to one exact source/toolchain input."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA = "pi-skills.preflight-receipt.v1"


class ReceiptError(RuntimeError):
    pass


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def file_sha256(path: Path) -> str:
    return sha256(path.read_bytes())


def git(root: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", os.fspath(root), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise ReceiptError(result.stderr.decode(errors="replace").strip() or "git command failed")
    return result.stdout


def receipt_path(root: Path) -> Path:
    raw = git(root, "rev-parse", "--git-path", "pi-skills/preflight-v1.json").decode().strip()
    path = Path(raw)
    return path if path.is_absolute() else (root / path).resolve()


def public_tree_manifest(root: Path) -> list[dict[str, Any]]:
    names = git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    manifest = []
    for raw_name in sorted(part for part in names.split(b"\0") if part):
        relative = raw_name.decode("utf-8", errors="strict")
        path = root / relative
        if not path.exists() and not path.is_symlink():
            continue
        metadata = path.lstat()
        mode = stat.S_IMODE(metadata.st_mode)
        if stat.S_ISREG(metadata.st_mode):
            kind = "file"
            digest = file_sha256(path)
        elif stat.S_ISLNK(metadata.st_mode):
            kind = "symlink"
            digest = sha256(os.readlink(path).encode())
        else:
            raise ReceiptError(f"unsupported public tree entry: {relative}")
        manifest.append({"path": relative, "kind": kind, "mode": mode, "sha256": digest})
    return manifest


def optional_file_digest(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"present": False}
    return {"present": True, "sha256": file_sha256(path)}


def validator_evidence(root: Path) -> dict[str, Any]:
    configured = os.environ.get("PI_SKILLS_VALIDATOR", "").strip()
    config_path = Path(os.environ.get("PI_SKILLS_LOCAL_CONFIG", root / ".pi-skills.local.json"))
    if not configured and config_path.is_file():
        try:
            value = json.loads(config_path.read_text(encoding="utf-8")).get("validator")
        except (OSError, json.JSONDecodeError, AttributeError):
            value = None
        if isinstance(value, str) and value.strip():
            configured = value.strip()
    if not configured:
        return {"configured": False}
    path = Path(configured).expanduser().resolve()
    return {
        "configured": True,
        "path_sha256": sha256(os.fspath(path).encode()),
        "content": optional_file_digest(path),
    }


def current_inputs(root: Path) -> dict[str, Any]:
    root = root.resolve()
    if Path(git(root, "rev-parse", "--show-toplevel").decode().strip()).resolve() != root:
        raise ReceiptError("root must be the Git worktree root")
    helper = Path(__file__).resolve()
    bash = shutil.which("bash") or ""
    bash_version = subprocess.run(
        [bash, "--version"] if bash else ["false"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    ).stdout.splitlines()
    config_path = Path(os.environ.get("PI_SKILLS_LOCAL_CONFIG", root / ".pi-skills.local.json"))
    return {
        "tree": public_tree_manifest(root),
        "toolchain": {
            "python_executable": os.fspath(Path(sys.executable).resolve()),
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "bash_executable": os.path.realpath(bash) if bash else None,
            "bash_version": bash_version[0] if bash_version else None,
            "receipt_tool_sha256": file_sha256(helper),
        },
        "local_policy": {
            "config": optional_file_digest(config_path),
            "private_markers_sha256": sha256(
                os.environ.get("PI_SKILLS_PRIVATE_MARKERS", "").encode()
            ),
            "validator": validator_evidence(root),
        },
    }


def fingerprint(root: Path) -> tuple[str, dict[str, Any]]:
    inputs = current_inputs(root)
    return sha256(canonical(inputs)), inputs


def load_stages(path: Path) -> list[dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReceiptError(f"invalid stages JSON: {exc}") from exc
    if not isinstance(value, list) or not value:
        raise ReceiptError("preflight stages must be a non-empty list")
    names = []
    for stage in value:
        if not isinstance(stage, dict) or not isinstance(stage.get("name"), str):
            raise ReceiptError("each preflight stage needs a name")
        names.append(stage["name"])
        if stage.get("status") != "pass":
            raise ReceiptError("all preflight stages must pass")
    if len(set(names)) != len(names):
        raise ReceiptError("preflight stage names must be unique")
    return value


def record(root: Path, stages_path: Path, expected_fingerprint: str) -> dict[str, Any]:
    current_fingerprint, inputs = fingerprint(root)
    if current_fingerprint != expected_fingerprint:
        raise ReceiptError("preflight inputs changed while stages were running")
    receipt = {
        "schema_version": SCHEMA,
        "status": "pass",
        "fingerprint": current_fingerprint,
        "inputs": inputs,
        "stages": load_stages(stages_path),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    path = receipt_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix="preflight-", suffix=".json", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical(receipt) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return receipt


def check(root: Path) -> dict[str, Any]:
    path = receipt_path(root)
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReceiptError("preflight receipt is missing") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ReceiptError(f"preflight receipt is invalid: {exc}") from exc
    if receipt.get("schema_version") != SCHEMA or receipt.get("status") != "pass":
        raise ReceiptError("preflight receipt is not a passing v1 receipt")
    observed, inputs = fingerprint(root)
    if receipt.get("fingerprint") != observed or receipt.get("inputs") != inputs:
        raise ReceiptError("preflight receipt fingerprint mismatch")
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("fingerprint", "check"):
        child = subparsers.add_parser(name)
        child.add_argument("--root", type=Path, required=True)
    record_parser = subparsers.add_parser("record")
    record_parser.add_argument("--root", type=Path, required=True)
    record_parser.add_argument("--stages-json", type=Path, required=True)
    record_parser.add_argument("--expected-fingerprint", required=True)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "fingerprint":
            observed, inputs = fingerprint(arguments.root)
            value = {"fingerprint": observed, "inputs": inputs}
        elif arguments.command == "check":
            value = check(arguments.root)
        else:
            value = record(arguments.root, arguments.stages_json, arguments.expected_fingerprint)
        print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0
    except ReceiptError as exc:
        print(str(exc), file=sys.stderr)
        return 1 if arguments.command == "check" else 2


if __name__ == "__main__":
    raise SystemExit(main())
