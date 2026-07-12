#!/usr/bin/env python3
"""Read-only, directory-fd-confined compatibility inspector for legacy artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any


SCHEMA_MATRIX = {
    "agent-loops.orchestration.v1": "agent-loops.workflow.v1",
    "agent-loops.orchestration.v2": "agent-workflow.workflow.v2",
}
MAX_FILE_BYTES = 4 * 1024 * 1024


class LegacyInspectionError(ValueError):
    """Raised when a legacy workspace cannot be inspected safely."""


def _safe_relative(value: str, label: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise LegacyInspectionError(f"{label} must be a safe relative path")
    return path


def _open_workflow_dir(workflow_dir: Path, allowed_root: Path) -> tuple[int, Path]:
    allowed_input = Path(allowed_root).absolute()
    if allowed_input.is_symlink() or not allowed_input.is_dir():
        raise LegacyInspectionError("legacy allowed root must be a real directory")
    allowed = allowed_input.resolve(strict=True)
    raw = Path(workflow_dir)
    if raw.is_absolute():
        try:
            relative = raw.absolute().relative_to(allowed_input)
        except ValueError as exc:
            raise LegacyInspectionError("legacy workflow root must be inside the allowed root") from exc
    else:
        relative = raw
    safe = _safe_relative(PurePosixPath(*relative.parts).as_posix(), "legacy workflow root")
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(allowed_input, flags)
    try:
        for part in safe.parts:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
    except OSError as exc:
        os.close(descriptor)
        raise LegacyInspectionError("legacy workflow root contains a symlink or unsafe ancestor") from exc
    return descriptor, allowed.joinpath(*safe.parts)


def _read_object(root_fd: int, name: str) -> tuple[dict[str, Any], str]:
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=root_fd)
    except OSError as exc:
        raise LegacyInspectionError(f"legacy artifact is missing or unsafe: {name}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0 or metadata.st_size > MAX_FILE_BYTES:
            raise LegacyInspectionError(f"legacy artifact must be a bounded regular file: {name}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, MAX_FILE_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_FILE_BYTES:
                raise LegacyInspectionError(f"legacy artifact size is invalid: {name}")
        payload = b"".join(chunks)
    finally:
        os.close(descriptor)
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LegacyInspectionError(f"legacy artifact is invalid JSON: {name}") from exc
    if not isinstance(value, dict):
        raise LegacyInspectionError(f"legacy artifact must be an object: {name}")
    return value, "sha256:" + hashlib.sha256(payload).hexdigest()


def inspect(workflow_dir: Path, *, allowed_root: Path) -> dict[str, Any]:
    root_fd, display_root = _open_workflow_dir(Path(workflow_dir), Path(allowed_root))
    try:
        orchestration, orchestration_sha = _read_object(root_fd, "orchestration.json")
        state, state_sha = _read_object(root_fd, "workflow-state.json")
    finally:
        os.close(root_fd)
    orchestration_schema = orchestration.get("schema_version")
    state_schema = state.get("schema_version")
    if not isinstance(orchestration_schema, str) or orchestration_schema not in SCHEMA_MATRIX:
        raise LegacyInspectionError("legacy orchestration schema is missing or unsupported")
    if state_schema != SCHEMA_MATRIX[orchestration_schema]:
        raise LegacyInspectionError("legacy orchestration/workflow-state schema versions do not match")
    workflow = orchestration.get("workflow")
    rounds = orchestration.get("rounds", [])
    if not isinstance(workflow, dict) or not isinstance(rounds, list):
        raise LegacyInspectionError("legacy orchestration structure is malformed")
    title = workflow.get("title")
    slug = workflow.get("slug")
    if not isinstance(title, str) or not title.strip() or not isinstance(slug, str) or not slug.strip():
        raise LegacyInspectionError("legacy workflow identity is malformed")
    if "workspace_root" in workflow:
        _safe_relative(workflow["workspace_root"], "legacy workflow.workspace_root")
    round_ids: list[str] = []
    for item in rounds:
        if not isinstance(item, dict) or not isinstance(item.get("round_id"), str) or not item["round_id"].strip():
            raise LegacyInspectionError("legacy round identity is malformed")
        round_ids.append(item["round_id"])
    if len(round_ids) != len(set(round_ids)):
        raise LegacyInspectionError("legacy round identities are duplicated")
    return {
        "schema_version": "agent-workflow.legacy-inspection.v1",
        "mode": "read_only",
        "workflow_root": str(display_root),
        "title": title,
        "slug": slug,
        "orchestration_schema": orchestration_schema,
        "orchestration_sha256": orchestration_sha,
        "state_schema": state_schema,
        "state_sha256": state_sha,
        "round_count": len(round_ids),
        "round_ids": round_ids,
        "compatibility": "legacy_reader_only_no_writer_fallback",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workflow_dir", type=Path)
    parser.add_argument("--allowed-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = inspect(args.workflow_dir, allowed_root=args.allowed_root)
    except (LegacyInspectionError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
