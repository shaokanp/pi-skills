#!/usr/bin/env python3
"""Canonical, no-follow repository state evidence shared by vNext validators."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any

sys.dont_write_bytecode = True


class RepositoryStateError(RuntimeError):
    pass


def _canonical(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _git(repository: Path, *args: str) -> bytes:
    completed = subprocess.run(
        ["git", *args],
        cwd=repository,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode:
        raise RepositoryStateError(completed.stderr.decode(errors="replace").strip())
    return completed.stdout


def _open_directory_no_follow(path: Path) -> int:
    path = Path(path)
    if not path.is_absolute() or any(part in {".", ".."} for part in path.parts[1:]):
        raise RepositoryStateError("repository evidence directory path is unsafe")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    current = os.open("/", flags)
    try:
        for part in path.parts[1:]:
            child = os.open(part, flags, dir_fd=current)
            os.close(current)
            current = child
        return current
    except OSError as exc:
        os.close(current)
        raise RepositoryStateError("repository evidence directory path drifted") from exc


def _untracked_file_evidence(repository: Path, relative: str) -> dict[str, Any]:
    parent = _open_directory_no_follow((repository / relative).parent)
    descriptor: int | None = None
    try:
        try:
            descriptor = os.open(
                Path(relative).name,
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=parent,
            )
        except OSError as exc:
            raise RepositoryStateError("untracked repository file is unsafe or disappeared") from exc
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink > 1:
            raise RepositoryStateError("untracked repository evidence requires private regular files")
        digest = hashlib.sha256()
        while True:
            payload = os.read(descriptor, 1024 * 1024)
            if not payload:
                break
            digest.update(payload)
        after = os.fstat(descriptor)
        stable_fields = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
            raise RepositoryStateError("untracked repository file changed during evidence capture")
        return {
            "path": relative,
            "mode": stat.S_IMODE(after.st_mode),
            "sha256": "sha256:" + digest.hexdigest(),
        }
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)


def collect_repository_state(repository: Path) -> dict[str, str]:
    repository = Path(repository).resolve(strict=True)
    head = _git(repository, "rev-parse", "HEAD").decode().strip()
    branch = _git(repository, "branch", "--show-current").decode().strip()
    tracked = _git(
        repository,
        "diff",
        "--binary",
        "HEAD",
        "--",
        ".",
        ":(exclude).workflow",
        ":(exclude).workflow/**",
    )
    untracked_paths = sorted(
        item.decode("utf-8")
        for item in _git(repository, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0")
        if item
        and ".workflow" not in {
            part.casefold() for part in PurePosixPath(item.decode("utf-8")).parts
        }
    )
    untracked = [_untracked_file_evidence(repository, relative) for relative in untracked_paths]
    evidence = {
        "head": head,
        "branch": branch,
        "tracked_diff_sha256": _digest(tracked),
        "untracked_manifest_sha256": _digest(_canonical(untracked)),
    }
    evidence["source_state_sha256"] = _digest(_canonical(evidence))
    return evidence
