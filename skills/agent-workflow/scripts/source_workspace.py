#!/usr/bin/env python3
"""Isolated source-writing transactions for Agent Workflow vNext.

Workers receive only a private checkout snapshot.  This module audits their
changes and applies a bounded, exact-base patch to the shared checkout only
after every target root still matches the launch seal.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import unicodedata
import ctypes
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from artifact_store import ArtifactError, create_once_json
from baseline_gate import BaselineError, _unpack, verify_baseline
from repository_state import RepositoryStateError, collect_repository_state


class SourceWriteError(RuntimeError):
    """Base class for a fail-closed source-writing boundary."""


class DirtyOverlap(SourceWriteError):
    """The requested writer roots overlap pre-existing user changes."""


class IntegrationConflict(SourceWriteError):
    """The shared checkout changed after the writer snapshot was sealed."""


@dataclass(frozen=True)
class TaskWorkspace:
    task_id: str
    root: Path
    write_roots: tuple[str, ...]
    initial_files: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class SourcePhase:
    phase_id: str
    repository: Path
    control_root: Path
    target_before: dict[str, str]
    tasks: dict[str, TaskWorkspace]
    seal_ref: str
    integration_anchor: str


MAX_PATCH_FILES = 1024
MAX_PATCH_BYTES = 16 * 1024 * 1024
MAX_SNAPSHOT_FILES = 50_000
MAX_SNAPSHOT_BYTES = 512 * 1024 * 1024
_RESERVED_PARTS = {".git", ".workflow"}


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _create_or_verify_json(root: Path, relative: str, value: Any) -> Path:
    expected = _canonical(value)
    path = Path(root) / relative
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != expected:
            raise SourceWriteError(f"immutable source artifact drifted: {relative}")
        return path
    try:
        return create_once_json(root, relative, value)
    except ArtifactError as exc:
        raise SourceWriteError(str(exc)) from exc


def _safe_root(value: str) -> str:
    if not isinstance(value, str):
        raise SourceWriteError("write root must be a string")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise SourceWriteError("write root must be a safe relative path")
    if any(part.casefold() in _RESERVED_PARTS for part in path.parts):
        raise SourceWriteError("write root cannot include a control or Git path")
    return path.as_posix()


def _safe_read_root(value: str) -> str:
    if value == ".":
        return "."
    return _safe_root(value)


def _fold_path(value: str) -> tuple[str, ...]:
    return tuple(unicodedata.normalize("NFC", part).casefold() for part in PurePosixPath(value).parts)


def _overlap(left: str, right: str) -> bool:
    a, b = _fold_path(left), _fold_path(right)
    return a[: len(b)] == b or b[: len(a)] == a


def validate_write_roots(repository: Path, tasks: list[dict[str, Any]]) -> dict[str, tuple[str, ...]]:
    """Canonicalize roots and reject lexical, realpath, inode, and hardlink collisions."""

    repository = Path(repository).resolve(strict=True)
    observed: list[tuple[str, str, Path | None, tuple[int, int] | None]] = []
    result: dict[str, tuple[str, ...]] = {}
    for task in tasks:
        task_id = task.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise SourceWriteError("writer task id is invalid")
        roots = tuple(_safe_root(value) for value in task.get("write_roots", []))
        if not roots:
            raise SourceWriteError(f"writer {task_id} has no write roots")
        if len({_fold_path(value) for value in roots}) != len(roots):
            raise SourceWriteError(f"writer {task_id} has case or Unicode-equivalent roots")
        for relative in roots:
            candidate = repository / relative
            resolved: Path | None = None
            identity: tuple[int, int] | None = None
            cursor = repository
            for part in PurePosixPath(relative).parts:
                cursor = cursor / part
                if cursor.is_symlink():
                    raise SourceWriteError(f"write root crosses a symlink: {relative}")
                if not cursor.exists():
                    break
                if not cursor.resolve(strict=True).is_relative_to(repository):
                    raise SourceWriteError(f"write root escapes repository: {relative}")
            if candidate.exists() or candidate.is_symlink():
                if candidate.is_symlink():
                    raise SourceWriteError(f"write root is a symlink: {relative}")
                resolved = candidate.resolve(strict=True)
                if not resolved.is_relative_to(repository):
                    raise SourceWriteError(f"write root escapes repository: {relative}")
                metadata = candidate.stat()
                identity = (metadata.st_dev, metadata.st_ino)
                if metadata.st_nlink > 1 and candidate.is_file():
                    raise SourceWriteError(f"write root is hard-linked: {relative}")
            for prior_task, prior, prior_resolved, prior_identity in observed:
                if _overlap(relative, prior):
                    raise SourceWriteError(
                        f"writer roots overlap: {prior_task}:{prior} and {task_id}:{relative}"
                    )
                if resolved is not None and prior_resolved is not None:
                    if resolved == prior_resolved or resolved.is_relative_to(prior_resolved) or prior_resolved.is_relative_to(resolved):
                        raise SourceWriteError("writer roots collide after realpath resolution")
                    if identity == prior_identity:
                        raise SourceWriteError("writer roots collide by device/inode")
            observed.append((task_id, relative, resolved, identity))
        result[task_id] = roots
    return result


def _git_paths(repository: Path, *args: str) -> list[str]:
    completed = subprocess.run(
        ["git", *args], cwd=repository, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
    )
    if completed.returncode:
        raise SourceWriteError(completed.stderr.decode(errors="replace").strip())
    return sorted(item.decode("utf-8") for item in completed.stdout.split(b"\0") if item)


def dirty_paths(repository: Path) -> list[str]:
    tracked = set(
        _git_paths(repository, "diff", "--no-renames", "--name-only", "-z", "HEAD", "--", ".")
    )
    untracked = set(_git_paths(repository, "ls-files", "--others", "--exclude-standard", "-z"))
    return sorted(tracked | untracked)


def prepare_read_only_snapshot(
    control_root: Path,
    repository: Path,
    phase_id: str,
    read_roots: tuple[str, ...],
) -> Path:
    """Materialize the live integrated source without Git or control-plane roots."""

    control_root = Path(control_root).resolve()
    repository = Path(repository).resolve(strict=True)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", phase_id) is None:
        raise SourceWriteError("read snapshot phase id is unsafe")
    normalized_roots = tuple(_safe_read_root(value) for value in read_roots)
    if not normalized_roots:
        raise SourceWriteError("read snapshot requires at least one source root")
    destination = control_root / "runtime" / "read-snapshots" / phase_id / "checkout"
    manifest_ref = f"runtime/read-snapshots/{phase_id}/manifest.json"
    manifest_path = control_root / manifest_ref
    destination_exists = destination.exists() or destination.is_symlink()
    manifest_exists = manifest_path.exists() or manifest_path.is_symlink()
    if destination_exists != manifest_exists:
        raise SourceWriteError("read snapshot transaction is incomplete")
    try:
        source_state = collect_repository_state(repository)
    except RepositoryStateError as exc:
        raise SourceWriteError(f"read snapshot repository state failed closed: {exc}") from exc

    def selected(relative: str) -> bool:
        parts = PurePosixPath(relative).parts
        if not parts or any(part.casefold() in _RESERVED_PARTS for part in parts):
            return False
        folded = _fold_path(relative)
        return any(
            root == "." or folded[: len(_fold_path(root))] == _fold_path(root)
            for root in normalized_roots
        )

    if not destination_exists:
        destination.mkdir(parents=True, mode=0o700)
        tracked_paths = set(_git_paths(repository, "ls-files", "--cached", "-z"))
        untracked_paths = set(
            _git_paths(repository, "ls-files", "--others", "--exclude-standard", "-z")
        )
        paths = sorted(tracked_paths | untracked_paths)
        file_count = 0
        byte_count = 0
        for relative in paths:
            if not selected(relative):
                continue
            source = repository / relative
            if not source.exists() and not source.is_symlink():
                if relative in tracked_paths:
                    # A tracked worktree deletion is part of the integrated source state.
                    continue
                raise SourceWriteError("untracked source disappeared during snapshot creation")
            file_count += 1
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            byte_count += _copy_read_snapshot_file_no_follow(source, target)
            if file_count > MAX_SNAPSHOT_FILES or byte_count > MAX_SNAPSHOT_BYTES:
                raise SourceWriteError("read snapshot exceeds its file or byte cap")
        try:
            if collect_repository_state(repository) != source_state:
                raise SourceWriteError("repository changed during read snapshot creation")
        except RepositoryStateError as exc:
            raise SourceWriteError(f"read snapshot terminal repository state failed closed: {exc}") from exc
    elif destination.is_symlink() or not destination.is_dir():
        raise SourceWriteError("read snapshot destination is unsafe")

    files = _regular_files(destination)
    if any(
        any(part.casefold() in _RESERVED_PARTS for part in PurePosixPath(relative).parts)
        for relative in files
    ):
        raise SourceWriteError("read snapshot contains a reserved control path")
    manifest = {
        "schema_version": "agent-workflow.read-snapshot.vnext.v1",
        "phase_id": phase_id,
        "repository": os.fspath(repository),
        "repository_state": source_state,
        "repository_state_sha256": source_state["source_state_sha256"],
        "read_roots": list(normalized_roots),
        "checkout_ref": destination.relative_to(control_root).as_posix(),
        "files": files,
        "files_sha256": _digest(_canonical(files)),
    }
    _create_or_verify_json(control_root, manifest_ref, manifest)
    return destination


def replay_read_only_snapshot(
    control_root: Path,
    repository: Path,
    phase_id: str,
    read_roots: tuple[str, ...],
    manifest_evidence: dict[str, Any],
) -> tuple[Path, dict[str, Any]]:
    """Replay one existing digest-bound snapshot without creating replacement authority."""

    control_root = Path(control_root).resolve()
    repository = Path(repository).resolve(strict=True)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", phase_id) is None:
        raise SourceWriteError("read snapshot phase id is unsafe")
    normalized_roots = tuple(_safe_read_root(value) for value in read_roots)
    expected_ref = f"runtime/read-snapshots/{phase_id}/manifest.json"
    if not isinstance(manifest_evidence, dict) or set(manifest_evidence) != {
        "evidence_ref",
        "evidence_sha256",
    }:
        raise SourceWriteError("read snapshot manifest evidence is invalid")
    if manifest_evidence["evidence_ref"] != expected_ref or not isinstance(
        manifest_evidence["evidence_sha256"], str
    ):
        raise SourceWriteError("read snapshot manifest reference drifted")

    destination = control_root / "runtime" / "read-snapshots" / phase_id / "checkout"
    manifest_path = control_root / expected_ref
    if (
        destination.is_symlink()
        or not destination.is_dir()
        or manifest_path.is_symlink()
        or not manifest_path.is_file()
    ):
        raise SourceWriteError("read snapshot is missing or unsafe")
    if not destination.resolve(strict=True).is_relative_to(control_root):
        raise SourceWriteError("read snapshot checkout escapes the control root")
    payload = manifest_path.read_bytes()
    if _digest(payload) != manifest_evidence["evidence_sha256"]:
        raise SourceWriteError("read snapshot manifest digest drifted")
    try:
        manifest = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceWriteError("read snapshot manifest is invalid JSON") from exc
    expected_keys = {
        "schema_version",
        "phase_id",
        "repository",
        "repository_state",
        "repository_state_sha256",
        "read_roots",
        "checkout_ref",
        "files",
        "files_sha256",
    }
    expected_checkout_ref = f"runtime/read-snapshots/{phase_id}/checkout"
    if not isinstance(manifest, dict) or set(manifest) != expected_keys:
        raise SourceWriteError("read snapshot manifest contract is incomplete")
    if (
        manifest["schema_version"] != "agent-workflow.read-snapshot.vnext.v1"
        or manifest["phase_id"] != phase_id
        or manifest["repository"] != os.fspath(repository)
        or manifest["read_roots"] != list(normalized_roots)
        or manifest["checkout_ref"] != expected_checkout_ref
    ):
        raise SourceWriteError("read snapshot manifest authority drifted")
    state = manifest["repository_state"]
    if not isinstance(state, dict) or state.get("source_state_sha256") != manifest[
        "repository_state_sha256"
    ]:
        raise SourceWriteError("read snapshot repository state binding is invalid")
    state_payload = {key: value for key, value in state.items() if key != "source_state_sha256"}
    if _digest(_canonical(state_payload)) != manifest["repository_state_sha256"]:
        raise SourceWriteError("read snapshot repository state digest drifted")
    files = _regular_files(destination)
    if files != manifest["files"] or _digest(_canonical(files)) != manifest["files_sha256"]:
        raise SourceWriteError("read snapshot checkout bytes or modes drifted")
    return destination, manifest


def _assert_no_dirty_overlap(repository: Path, roots: dict[str, tuple[str, ...]]) -> None:
    dirty = dirty_paths(repository)
    collisions = sorted(
        path
        for path in dirty
        if any(_overlap(path, root) for task_roots in roots.values() for root in task_roots)
    )
    if collisions:
        raise DirtyOverlap("writer roots overlap existing dirty paths: " + ", ".join(collisions))


def _regular_files(root: Path) -> dict[str, dict[str, Any]]:
    if not root.exists():
        return {}
    if root.is_symlink():
        raise SourceWriteError(f"source path is a symlink: {root}")
    paths = [root] if root.is_file() else sorted(root.rglob("*"))
    files: dict[str, dict[str, Any]] = {}
    for path in paths:
        relative = "." if path == root else path.relative_to(root).as_posix()
        if path.is_symlink():
            raise SourceWriteError(f"source tree contains a symlink: {relative}")
        metadata = path.stat()
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise SourceWriteError(f"source tree contains a non-regular file: {relative}")
        if metadata.st_nlink > 1:
            raise SourceWriteError(f"source tree contains a hardlink: {relative}")
        files[relative] = {
            "sha256": _digest(path.read_bytes()),
            "mode": stat.S_IMODE(metadata.st_mode),
        }
    return files


def _copy_read_snapshot_file_no_follow(source: Path, target: Path) -> int:
    """Copy one exact source inode without reopening its pathname."""

    source_fd = _open_regular_file_no_follow(source)
    target_fd: int | None = None
    try:
        before = os.fstat(source_fd)
        target_fd = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            stat.S_IMODE(before.st_mode) or 0o600,
        )
        copied = 0
        while True:
            payload = os.read(source_fd, 1024 * 1024)
            if not payload:
                break
            copied += len(payload)
            view = memoryview(payload)
            while view:
                written = os.write(target_fd, view)
                view = view[written:]
        after = os.fstat(source_fd)
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
            raise SourceWriteError("read snapshot source changed during descriptor copy")
        if copied != after.st_size:
            raise SourceWriteError("read snapshot source size changed during descriptor copy")
        os.fchmod(target_fd, stat.S_IMODE(after.st_mode) or 0o600)
        os.fsync(target_fd)
        return copied
    finally:
        if target_fd is not None:
            os.close(target_fd)
        os.close(source_fd)


def _tree_digest(repository: Path, relative: str) -> str:
    return _digest(_canonical(_regular_files(repository / relative)))


def _enforce_snapshot_cap(root: Path) -> None:
    count = 0
    total = 0
    for path in root.rglob("*"):
        if path.is_symlink():
            raise SourceWriteError("materialized source snapshot contains a symlink")
        if path.is_dir():
            continue
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode):
            raise SourceWriteError("materialized source snapshot contains a special file")
        count += 1
        total += metadata.st_size
        if count > MAX_SNAPSHOT_FILES or total > MAX_SNAPSHOT_BYTES:
            raise SourceWriteError("materialized source snapshot exceeds its final file or byte cap")


def _run_git(repository: Path, *args: str, input_payload: bytes | None = None) -> bytes:
    completed = subprocess.run(
        ["git", *args],
        cwd=repository,
        env={**os.environ, "GIT_CEILING_DIRECTORIES": os.fspath(repository)},
        input=input_payload,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode:
        raise SourceWriteError(completed.stderr.decode(errors="replace").strip())
    return completed.stdout


def _materialize_baseline(
    repository: Path,
    destination: Path,
    baseline: dict[str, Any],
    read_roots: tuple[str, ...],
) -> None:
    """Replay HEAD, staged, unstaged, and untracked bytes into a private snapshot."""

    try:
        verify_baseline(baseline)
    except BaselineError as exc:
        raise SourceWriteError("source-write baseline is invalid") from exc
    selection = baseline["selection"]
    if selection["tracked_excludes"] or selection["untracked_mode"] != "all":
        raise SourceWriteError(
            "source-write baseline must include all tracked and untracked repository state"
        )
    materialized = destination.parent / "_materialized-repository"
    if materialized.exists() or materialized.is_symlink():
        raise SourceWriteError("baseline materialization root already exists")
    materialized.mkdir(parents=True, mode=0o700)
    archive = subprocess.Popen(
        ["git", "archive", "--format=tar", baseline["head"]],
        cwd=repository,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert archive.stdout is not None
    file_count = 0
    byte_count = 0
    with tarfile.open(fileobj=archive.stdout, mode="r|") as stream:
        for member in stream:
            path = PurePosixPath(member.name)
            if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
                raise SourceWriteError("Git archive contains an unsafe path")
            if member.isdir():
                continue
            if not member.isfile():
                raise SourceWriteError(f"Git archive contains an unsupported path type: {member.name}")
            file_count += 1
            byte_count += member.size
            if file_count > MAX_SNAPSHOT_FILES or byte_count > MAX_SNAPSHOT_BYTES:
                archive.kill()
                archive.wait()
                archive.stdout.close()
                if archive.stderr is not None:
                    archive.stderr.close()
                raise SourceWriteError("source snapshot file or byte cap exceeded")
            source = stream.extractfile(member)
            if source is None:
                raise SourceWriteError("Git archive member payload is unavailable")
            target = materialized.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            payload = source.read(MAX_SNAPSHOT_BYTES + 1)
            if len(payload) != member.size:
                raise SourceWriteError("Git archive member size drifted")
            target.write_bytes(payload)
            target.chmod(0o755 if member.mode & 0o111 else 0o644)
    archive_stderr = archive.stderr.read() if archive.stderr is not None else b""
    if archive.wait() != 0:
        raise SourceWriteError(archive_stderr.decode(errors="replace").strip())
    archive.stdout.close()
    if archive.stderr is not None:
        archive.stderr.close()
    _run_git(materialized, "init", "-q")
    _run_git(materialized, "add", "-A")
    for field in ("staged_binary_patch", "unstaged_binary_patch"):
        patch = _unpack(baseline[field], field)
        if patch:
            if len(patch) > MAX_PATCH_BYTES:
                raise SourceWriteError("source baseline patch byte cap exceeded")
            apply_args = ["apply"]
            if field == "staged_binary_patch":
                apply_args.append("--index")
            _run_git(
                materialized,
                *apply_args,
                "--binary",
                "--whitespace=nowarn",
                "-",
                input_payload=patch,
            )
    for item in baseline["untracked"]:
        byte_count += item["content"]["bytes"]
        file_count += 1
        if file_count > MAX_SNAPSHOT_FILES or byte_count > MAX_SNAPSHOT_BYTES:
            raise SourceWriteError("source snapshot file or byte cap exceeded")
        target = materialized / item["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_unpack(item["content"], f"untracked:{item['path']}"))
    shutil.rmtree(materialized / ".git")
    _enforce_snapshot_cap(materialized)
    destination.mkdir(parents=True, mode=0o700)
    for path in sorted(materialized.rglob("*")):
        if path.is_dir():
            continue
        relative = path.relative_to(materialized).as_posix()
        if any(
            part.casefold() in _RESERVED_PARTS
            for part in PurePosixPath(relative).parts
        ):
            continue
        if not any(
            _fold_path(relative)[: len(_fold_path(root))] == _fold_path(root)
            for root in read_roots
        ):
            continue
        if path.is_symlink() or not path.is_file() or path.stat().st_nlink > 1:
            raise SourceWriteError(f"materialized baseline contains an unsafe path: {relative}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    shutil.rmtree(materialized)


def _integration_anchor(roots: list[str]) -> str:
    folded = [_fold_path(root) for root in roots]
    common: list[str] = []
    source_parts = PurePosixPath(roots[0]).parts
    for index, part in enumerate(folded[0]):
        if all(len(value) > index and value[index] == part for value in folded):
            common.append(source_parts[index])
        else:
            break
    if not common:
        raise SourceWriteError(
            "writer roots require more than one atomic integration anchor; split the phase"
        )
    return common[0]


def _tree_digest_path(path: Path) -> str:
    return _digest(_canonical(_regular_files(path)))


def _open_directory_no_follow(path: Path) -> int:
    """Open an absolute directory by walking every component with O_NOFOLLOW."""

    path = Path(path)
    if not path.is_absolute() or any(part in {".", ".."} for part in path.parts[1:]):
        raise SourceWriteError("directory-FD path must be absolute and normalized")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    current_fd = os.open("/", flags)
    try:
        for part in path.parts[1:]:
            child_fd = os.open(part, flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = child_fd
        return current_fd
    except OSError as exc:
        os.close(current_fd)
        raise SourceWriteError(f"directory-FD path is unsafe: {path}") from exc
    except Exception:
        os.close(current_fd)
        raise


def _open_regular_file_no_follow(path: Path) -> int:
    parent_fd = _open_directory_no_follow(path.parent)
    try:
        try:
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        except OSError as exc:
            raise SourceWriteError(f"sealed integration source is unsafe: {path}") from exc
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink > 1:
            os.close(descriptor)
            raise SourceWriteError("sealed integration source is not a private regular file")
        return descriptor
    finally:
        os.close(parent_fd)


def _secure_private_directory(root: Path, parts: tuple[str, ...]) -> Path:
    """Create a private control-plane directory without following descendant symlinks."""

    if any(not part or part in {".", ".."} or "/" in part for part in parts):
        raise SourceWriteError("private control directory has an unsafe component")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    current_fd = _open_directory_no_follow(root)
    try:
        for part in parts:
            try:
                os.mkdir(part, mode=0o700, dir_fd=current_fd)
            except FileExistsError:
                pass
            child_fd = os.open(part, flags, dir_fd=current_fd)
            metadata = os.fstat(child_fd)
            if metadata.st_uid != os.getuid():
                os.close(child_fd)
                raise SourceWriteError("private control directory has an unexpected owner")
            os.fchmod(child_fd, 0o700)
            os.close(current_fd)
            current_fd = child_fd
        os.fsync(current_fd)
    finally:
        os.close(current_fd)
    return root.joinpath(*parts)


def _copy_regular_tree_to_private_parent(source: Path, parent: Path, name: str) -> None:
    """Materialize a sealed file/tree below a private, no-follow directory FD."""

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent_fd = _open_directory_no_follow(parent)

    def copy_node(source_path: Path, destination_parent_fd: int, destination_name: str) -> None:
        metadata = source_path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise SourceWriteError("sealed integration anchor contains a symlink")
        if stat.S_ISDIR(metadata.st_mode):
            os.mkdir(destination_name, mode=0o700, dir_fd=destination_parent_fd)
            destination_fd = os.open(
                destination_name,
                flags,
                dir_fd=destination_parent_fd,
            )
            try:
                for child in sorted(source_path.iterdir(), key=lambda item: item.name):
                    copy_node(child, destination_fd, child.name)
                os.fchmod(destination_fd, stat.S_IMODE(metadata.st_mode) or 0o700)
                os.fsync(destination_fd)
            finally:
                os.close(destination_fd)
            return
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink > 1:
            raise SourceWriteError("sealed integration anchor contains an unsafe file")
        source_fd = _open_regular_file_no_follow(source_path)
        destination_fd = os.open(
            destination_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            stat.S_IMODE(metadata.st_mode) or 0o600,
            dir_fd=destination_parent_fd,
        )
        try:
            while True:
                payload = os.read(source_fd, 1024 * 1024)
                if not payload:
                    break
                view = memoryview(payload)
                while view:
                    written = os.write(destination_fd, view)
                    view = view[written:]
            os.fchmod(destination_fd, stat.S_IMODE(metadata.st_mode) or 0o600)
            os.fsync(destination_fd)
        finally:
            os.close(destination_fd)
            os.close(source_fd)

    try:
        copy_node(source, parent_fd, name)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _fsync_private_tree(parent: Path, name: str) -> None:
    """Durably flush one no-follow staging tree before it can be atomically installed."""

    parent_fd = _open_directory_no_follow(parent)

    def sync_node(node_parent_fd: int, node_name: str) -> None:
        metadata = os.stat(node_name, dir_fd=node_parent_fd, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            directory_fd = os.open(
                node_name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=node_parent_fd,
            )
            try:
                for child_name in sorted(os.listdir(directory_fd)):
                    sync_node(directory_fd, child_name)
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            return
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink > 1:
            raise SourceWriteError("integration staging tree contains an unsafe path")
        file_fd = os.open(node_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=node_parent_fd)
        try:
            os.fsync(file_fd)
        finally:
            os.close(file_fd)

    try:
        sync_node(parent_fd, name)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _apply_patch_to_anchor(anchor_root: Path, anchor: str, entries: list[dict[str, Any]]) -> None:
    anchor_parts = _fold_path(anchor)
    for entry in entries:
        parts = PurePosixPath(entry["path"]).parts
        if _fold_path(entry["path"])[: len(anchor_parts)] != anchor_parts:
            raise SourceWriteError("bounded patch escapes its integration anchor")
        relative_parts = parts[len(PurePosixPath(anchor).parts) :]
        if not relative_parts:
            target = anchor_root
        else:
            target = anchor_root.joinpath(*relative_parts)
        if entry["after_base64"] is None:
            if target.exists():
                if target.is_symlink() or not target.is_file():
                    raise SourceWriteError("bounded patch deletion target is unsafe")
                target.unlink()
            continue
        payload = base64.b64decode(entry["after_base64"], validate=True)
        if target.exists() and (target.is_symlink() or not target.is_file()):
            raise SourceWriteError("bounded patch replacement target is unsafe")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        target.chmod(entry["after_mode"])


def _atomic_exchange_or_install(replacement: Path, target: Path) -> bool:
    """Atomically install one integration anchor; return whether an old anchor was swapped out."""

    if replacement.name in {"", ".", ".."} or target.name in {"", ".", ".."}:
        raise SourceWriteError("atomic integration path has an unsafe basename")
    source_parent_fd = _open_directory_no_follow(replacement.parent)
    target_parent_fd = _open_directory_no_follow(target.parent)
    try:
        replacement_metadata = os.stat(
            replacement.name,
            dir_fd=source_parent_fd,
            follow_symlinks=False,
        )
        if stat.S_ISLNK(replacement_metadata.st_mode):
            raise SourceWriteError("integration replacement is a symlink")
        target_parent_metadata = os.fstat(target_parent_fd)
        if replacement_metadata.st_dev != target_parent_metadata.st_dev:
            raise SourceWriteError("integration staging and target must share a filesystem")
        try:
            target_metadata = os.stat(
                target.name,
                dir_fd=target_parent_fd,
                follow_symlinks=False,
            )
            if stat.S_ISLNK(target_metadata.st_mode):
                raise IntegrationConflict("shared integration anchor became a symlink")
            target_exists = True
        except FileNotFoundError:
            target_exists = False
        if os.uname().sysname != "Darwin":
            raise SourceWriteError("atomic integration requires the supported macOS host")
        libc = ctypes.CDLL(None, use_errno=True)
        renameatx_np = libc.renameatx_np
        renameatx_np.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameatx_np.restype = ctypes.c_int
        flags = 0x00000002 if target_exists else 0x00000004
        if renameatx_np(
            source_parent_fd,
            os.fsencode(replacement.name),
            target_parent_fd,
            os.fsencode(target.name),
            flags,
        ) != 0:
            error = ctypes.get_errno()
            if not target_exists and error == 17:
                raise IntegrationConflict("integration anchor appeared before atomic install")
            raise SourceWriteError(
                f"atomic integration anchor exchange failed: {os.strerror(error)}"
            )
        os.fsync(source_parent_fd)
        os.fsync(target_parent_fd)
        return target_exists
    finally:
        os.close(target_parent_fd)
        os.close(source_parent_fd)


def _rollback_applied_anchor(staging_anchor: Path, target: Path, old_anchor_retained: bool) -> None:
    """Restore the exact before-state after an uncommitted atomic installation."""

    if old_anchor_retained:
        if not _atomic_exchange_or_install(staging_anchor, target):
            raise SourceWriteError("integration rollback lost its displaced anchor")
        return
    if staging_anchor.exists() or staging_anchor.is_symlink():
        raise SourceWriteError("empty-anchor rollback staging unexpectedly exists")
    if _atomic_exchange_or_install(target, staging_anchor):
        _atomic_exchange_or_install(staging_anchor, target)
        raise SourceWriteError("empty-anchor rollback raced with an unexpected staging anchor")


def _seal_displaced_anchor_evidence(
    phase: SourcePhase,
    displaced_anchor: Path | None,
    *,
    reason: str,
) -> str:
    """Preserve post-swap external bytes as named human-recovery evidence."""

    if displaced_anchor is not None and (
        not displaced_anchor.exists() or displaced_anchor.is_symlink()
    ):
        raise SourceWriteError("displaced source evidence is unavailable or unsafe")
    relative = f"runtime/source-write/{phase.phase_id}/displaced-anchor.json"
    value = {
        "schema_version": "agent-workflow.displaced-anchor.vnext.v1",
        "phase_id": phase.phase_id,
        "anchor": phase.integration_anchor,
        "reason": reason,
        "displaced_state": "retained_tree" if displaced_anchor is not None else "missing",
        "staging_ref": (
            displaced_anchor.relative_to(phase.control_root).as_posix()
            if displaced_anchor is not None
            else None
        ),
        "staging_sha256": (
            _tree_digest_path(displaced_anchor)
            if displaced_anchor is not None
            else _digest(_canonical({}))
        ),
        "cleanup_allowed": False,
    }
    _create_or_verify_json(phase.control_root, relative, value)
    return relative


def prepare_isolated_phase(
    control_root: Path,
    repository: Path,
    plan: dict[str, Any],
    *,
    read_roots: tuple[str, ...] | None = None,
    admission_baseline: dict[str, Any],
) -> SourcePhase:
    """Seal exact roots and create one private snapshot per writer task."""

    control_root = Path(control_root).resolve()
    repository = Path(repository).resolve(strict=True)
    phase_id = plan.get("phase_id")
    tasks = [task for task in plan.get("tasks", []) if task.get("work_mode") == "write"]
    if not isinstance(phase_id, str) or not tasks:
        raise SourceWriteError("source-writing phase is missing its phase id or writer tasks")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", phase_id):
        raise SourceWriteError("source-writing phase id is unsafe for control-plane paths")
    roots = validate_write_roots(repository, tasks)
    requested_reads = tuple(_safe_read_root(value) for value in (read_roots or ()))
    snapshot_roots = tuple(
        sorted(
            {root for values in roots.values() for root in values} | set(requested_reads),
            key=_fold_path,
        )
    )
    _assert_no_dirty_overlap(repository, roots)
    all_roots = sorted({root for values in roots.values() for root in values}, key=_fold_path)
    anchor = _integration_anchor(all_roots)
    snapshot_roots = tuple(sorted(set(snapshot_roots) | {anchor}, key=_fold_path))
    task_workspaces: dict[str, TaskWorkspace] = {}
    seal_tasks: dict[str, Any] = {}
    template = control_root / "runtime" / "source-workspaces" / phase_id / "_sealed-base"
    if template.exists() or template.is_symlink():
        raise SourceWriteError("isolated source template already exists")
    _materialize_baseline(repository, template, admission_baseline, snapshot_roots)
    before = {anchor: _tree_digest(template, anchor)}
    if _tree_digest(repository, anchor) != before[anchor]:
        raise IntegrationConflict("writer integration anchor drifted from the admission baseline")
    for task in tasks:
        task_id = task["task_id"]
        workspace = control_root / "runtime" / "source-workspaces" / phase_id / task_id / "checkout"
        if workspace.exists() or workspace.is_symlink():
            raise SourceWriteError(f"isolated workspace already exists: {task_id}")
        shutil.copytree(template, workspace, copy_function=shutil.copy2)
        initial = _regular_files(workspace)
        task_workspaces[task_id] = TaskWorkspace(task_id, workspace, roots[task_id], initial)
        seal_tasks[task_id] = {
            "workspace_ref": workspace.relative_to(control_root).as_posix(),
            "write_roots": list(roots[task_id]),
            "initial_files": initial,
        }
    seal = {
        "schema_version": "agent-workflow.source-phase-seal.vnext.v1",
        "phase_id": phase_id,
        "repository": os.fspath(repository),
        "dirty_paths": dirty_paths(repository),
        "baseline_sha256": _digest(_canonical(admission_baseline)),
        "integration_anchor": anchor,
        "target_before": before,
        "tasks": seal_tasks,
    }
    seal_ref = f"runtime/source-write/{phase_id}/seal.json"
    try:
        create_once_json(control_root, seal_ref, seal)
    except ArtifactError as exc:
        raise SourceWriteError(str(exc)) from exc
    return SourcePhase(
        phase_id,
        repository,
        control_root,
        before,
        task_workspaces,
        seal_ref,
        anchor,
    )


def load_isolated_phase(
    control_root: Path,
    repository: Path,
    plan: dict[str, Any],
    *,
    admission_baseline: dict[str, Any],
) -> SourcePhase:
    """Reconstruct a sealed source phase without trusting mutable workspace state."""

    control_root = Path(control_root).resolve()
    repository = Path(repository).resolve(strict=True)
    seal_ref = f"runtime/source-write/{plan['phase_id']}/seal.json"
    seal_path = control_root / seal_ref
    if seal_path.is_symlink() or not seal_path.is_file():
        raise SourceWriteError("source phase seal is missing or unsafe")
    try:
        seal = json.loads(seal_path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceWriteError("source phase seal is invalid JSON") from exc
    if not isinstance(seal, dict) or set(seal) != {
        "schema_version",
        "phase_id",
        "repository",
        "dirty_paths",
        "baseline_sha256",
        "integration_anchor",
        "target_before",
        "tasks",
    }:
        raise SourceWriteError("source phase seal contract drifted")
    if (
        seal["schema_version"] != "agent-workflow.source-phase-seal.vnext.v1"
        or seal["phase_id"] != plan["phase_id"]
        or seal["repository"] != os.fspath(repository)
        or seal["baseline_sha256"] != _digest(_canonical(admission_baseline))
    ):
        raise SourceWriteError("source phase seal identity drifted")
    anchor = _safe_root(seal["integration_anchor"])
    anchor_digest = seal["target_before"].get(anchor) if isinstance(seal["target_before"], dict) else None
    if (
        seal["target_before"] != {anchor: anchor_digest}
        or not isinstance(anchor_digest, str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", anchor_digest)
    ):
        raise SourceWriteError("source phase target baseline is invalid")
    tasks_by_id = {task["task_id"]: task for task in plan["tasks"]}
    if set(seal["tasks"]) != set(tasks_by_id):
        raise SourceWriteError("source phase task seal does not match the plan")
    workspaces: dict[str, TaskWorkspace] = {}
    for task_id, item in seal["tasks"].items():
        if not isinstance(item, dict) or set(item) != {
            "workspace_ref",
            "write_roots",
            "initial_files",
        }:
            raise SourceWriteError("source task workspace seal is invalid")
        workspace = (control_root / item["workspace_ref"]).resolve()
        if not workspace.is_relative_to(control_root) or not workspace.is_dir():
            raise SourceWriteError("source task workspace is missing or escapes control root")
        roots = tuple(_safe_root(value) for value in item["write_roots"])
        if roots != tuple(tasks_by_id[task_id]["write_roots"]):
            raise SourceWriteError("source task write roots drifted")
        if not isinstance(item["initial_files"], dict):
            raise SourceWriteError("source task initial manifest is invalid")
        workspaces[task_id] = TaskWorkspace(
            task_id,
            workspace,
            roots,
            item["initial_files"],
        )
    sealed_anchor = (
        control_root
        / "runtime"
        / "source-workspaces"
        / plan["phase_id"]
        / "_sealed-base"
        / anchor
    )
    if _tree_digest_path(sealed_anchor) != seal["target_before"][anchor]:
        raise SourceWriteError("sealed source baseline tree drifted")
    return SourcePhase(
        plan["phase_id"],
        repository,
        control_root,
        seal["target_before"],
        workspaces,
        seal_ref,
        anchor,
    )


def _snapshot_workspace(task: TaskWorkspace) -> dict[str, tuple[bytes, int]]:
    observed: dict[str, tuple[bytes, int]] = {}
    for relative, state in _regular_files(task.root).items():
        observed[relative] = (
            (task.root / relative).read_bytes(),
            state["mode"],
        )
    return observed


def _initial_workspace(task: TaskWorkspace) -> dict[str, dict[str, Any]]:
    return dict(task.initial_files)


def integrate_isolated_phase(
    phase: SourcePhase,
    *,
    completed_task_ids: set[str],
    apply: bool = True,
    max_patch_files: int = MAX_PATCH_FILES,
    max_patch_bytes: int = MAX_PATCH_BYTES,
    pre_apply_fence: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Audit isolated changes, seal a patch, and apply only to the exact launch base."""

    if not completed_task_ids.issubset(phase.tasks):
        raise SourceWriteError("completed task set contains an unknown writer")
    if apply and completed_task_ids != set(phase.tasks):
        raise SourceWriteError("partial writer results cannot be integrated")
    patch_entries: list[dict[str, Any]] = []
    changed_by_task: dict[str, list[str]] = {}
    claimed: set[tuple[str, ...]] = set()
    for task_id, task in phase.tasks.items():
        initial = _initial_workspace(task)
        current = _snapshot_workspace(task)
        paths = sorted(set(initial) | set(current), key=_fold_path)
        changed: list[str] = []
        for relative in paths:
            before_state = initial.get(relative)
            after_observed = current.get(relative)
            after_payload = after_observed[0] if after_observed is not None else None
            after_state = (
                {"sha256": _digest(after_payload), "mode": after_observed[1]}
                if after_observed is not None
                else None
            )
            if before_state == after_state:
                continue
            if not any(_overlap(relative, root) and _fold_path(relative)[: len(_fold_path(root))] == _fold_path(root) for root in task.write_roots):
                raise SourceWriteError(f"worker changed a path outside its roots: {relative}")
            folded = _fold_path(relative)
            if folded in claimed:
                raise SourceWriteError(f"multiple writers changed the same path: {relative}")
            claimed.add(folded)
            changed.append(relative)
            patch_entries.append(
                {
                    "path": relative,
                    "before_sha256": before_state["sha256"] if before_state else None,
                    "before_mode": before_state["mode"] if before_state else None,
                    "after_sha256": after_state["sha256"] if after_state else None,
                    "after_mode": after_state["mode"] if after_state else None,
                    "after_base64": base64.b64encode(after_payload).decode("ascii") if after_payload is not None else None,
                }
            )
        changed_by_task[task_id] = changed
    if len(patch_entries) > max_patch_files:
        raise SourceWriteError("bounded patch file count exceeded")
    patch = {
        "schema_version": "agent-workflow.bounded-patch.vnext.v1",
        "phase_id": phase.phase_id,
        "target_before": phase.target_before,
        "entries": sorted(patch_entries, key=lambda item: _fold_path(item["path"])),
    }
    patch_payload = _canonical(patch)
    if len(patch_payload) > max_patch_bytes:
        raise SourceWriteError("bounded patch byte limit exceeded")
    patch_ref = f"phases/{phase.phase_id}/integration.patch.json"
    patch_path = _create_or_verify_json(phase.control_root, patch_ref, patch)

    if not apply:
        return {
            "mode": "isolated_exact_base",
            "status": "not_applied",
            "patch_ref": patch_ref,
            "patch_sha256": _digest(patch_path.read_bytes()),
            "target_before": phase.target_before,
            "target_after": {},
            "changed_by_task": changed_by_task,
        }

    anchor = phase.integration_anchor
    live_before = {anchor: _tree_digest(phase.repository, anchor)}
    existing_intent_path = (
        phase.control_root
        / "runtime"
        / "source-write"
        / phase.phase_id
        / "integration-intent.json"
    )
    if live_before != phase.target_before and not existing_intent_path.is_file():
        return {
            "mode": "isolated_exact_base",
            "status": "conflict",
            "patch_ref": patch_ref,
            "patch_sha256": _digest(patch_path.read_bytes()),
            "target_before": phase.target_before,
            "target_after": {},
            "changed_by_task": changed_by_task,
        }

    staging_parent = phase.control_root / "runtime" / "integration-staging" / phase.phase_id
    staging_parent = _secure_private_directory(
        phase.control_root,
        ("runtime", "integration-staging", phase.phase_id),
    )
    staging_anchor = staging_parent / "next-anchor"
    terminal_ref = f"runtime/source-write/{phase.phase_id}/integration-terminal.json"
    terminal_path = phase.control_root / terminal_ref
    if terminal_path.is_file() and not terminal_path.is_symlink():
        terminal = json.loads(terminal_path.read_bytes())
        expected_keys = {
            "schema_version",
            "phase_id",
            "status",
            "patch_ref",
            "patch_sha256",
            "anchor",
            "target_before",
            "target_after",
            "old_anchor_retained",
        }
        if not isinstance(terminal, dict) or set(terminal) != expected_keys:
            raise SourceWriteError("integration terminal evidence is invalid")
        if _tree_digest(phase.repository, anchor) != terminal["target_after"][anchor]:
            raise IntegrationConflict("integrated source drifted after terminal publication")
        return {
            "mode": "isolated_exact_base",
            "status": "applied",
            "patch_ref": patch_ref,
            "patch_sha256": _digest(patch_path.read_bytes()),
            "target_before": terminal["target_before"],
            "target_after": terminal["target_after"],
            "changed_by_task": changed_by_task,
        }
    if terminal_path.exists() or terminal_path.is_symlink():
        raise SourceWriteError("integration terminal path is unsafe")

    intent_ref = f"runtime/source-write/{phase.phase_id}/integration-intent.json"
    intent_path = phase.control_root / intent_ref
    if intent_path.is_file() and not intent_path.is_symlink():
        intent = json.loads(intent_path.read_bytes())
        if (
            not isinstance(intent, dict)
            or intent.get("schema_version") != "agent-workflow.integration-intent.vnext.v2"
            or intent.get("phase_id") != phase.phase_id
            or intent.get("patch_ref") != patch_ref
            or intent.get("patch_sha256") != _digest(patch_path.read_bytes())
            or intent.get("anchor") != anchor
            or intent.get("staging_ref") != staging_anchor.relative_to(phase.control_root).as_posix()
            or intent.get("target_before") != phase.target_before
            or set(intent.get("target_after", {})) != {anchor}
        ):
            raise SourceWriteError("integration intent evidence drifted")
        target_after = intent["target_after"]
    else:
        if intent_path.exists() or intent_path.is_symlink():
            raise SourceWriteError("integration intent path is unsafe")
        if staging_anchor.is_symlink():
            raise SourceWriteError("unclaimed integration staging anchor is unsafe")
        if not staging_anchor.exists():
            sealed_anchor = (
                phase.control_root
                / "runtime"
                / "source-workspaces"
                / phase.phase_id
                / "_sealed-base"
                / anchor
            )
            if sealed_anchor.is_dir():
                _copy_regular_tree_to_private_parent(
                    sealed_anchor,
                    staging_parent,
                    staging_anchor.name,
                )
            elif sealed_anchor.is_file():
                _copy_regular_tree_to_private_parent(
                    sealed_anchor,
                    staging_parent,
                    staging_anchor.name,
                )
            else:
                staging_anchor = _secure_private_directory(
                    staging_parent,
                    (staging_anchor.name,),
                )
        _apply_patch_to_anchor(staging_anchor, anchor, patch["entries"])
        _fsync_private_tree(staging_parent, staging_anchor.name)
        target_after = {anchor: _tree_digest_path(staging_anchor)}
        intent = {
            "schema_version": "agent-workflow.integration-intent.vnext.v2",
            "phase_id": phase.phase_id,
            "patch_ref": patch_ref,
            "patch_sha256": _digest(patch_path.read_bytes()),
            "anchor": anchor,
            "staging_ref": staging_anchor.relative_to(phase.control_root).as_posix(),
            "target_before": phase.target_before,
            "target_after": target_after,
        }
        _create_or_verify_json(phase.control_root, intent_ref, intent)

    observed = _tree_digest(phase.repository, anchor)
    old_anchor_retained = False
    if observed == target_after[anchor]:
        old_anchor_retained = staging_anchor.exists()
        if old_anchor_retained and _tree_digest_path(staging_anchor) != phase.target_before[anchor]:
            raise IntegrationConflict("recovery displaced anchor does not match the sealed before-state")
        if pre_apply_fence is not None:
            try:
                pre_apply_fence()
            except Exception:
                _rollback_applied_anchor(
                    staging_anchor,
                    phase.repository / anchor,
                    old_anchor_retained,
                )
                raise
    elif observed == phase.target_before[anchor]:
        if _tree_digest_path(staging_anchor) != target_after[anchor]:
            raise IntegrationConflict("staged integration anchor drifted")
        if pre_apply_fence is not None:
            pre_apply_fence()
        target = phase.repository / anchor
        if target.exists() and (target.is_symlink() or target.stat().st_nlink > 1 and target.is_file()):
            raise IntegrationConflict("shared integration anchor became unsafe")
        old_anchor_retained = _atomic_exchange_or_install(staging_anchor, target)
        if pre_apply_fence is not None:
            try:
                pre_apply_fence()
            except Exception:
                _rollback_applied_anchor(staging_anchor, target, old_anchor_retained)
                raise
        if (
            old_anchor_retained
            and _tree_digest_path(staging_anchor) != phase.target_before[anchor]
        ):
            displaced_digest = _tree_digest_path(staging_anchor)
            rolled_back = _atomic_exchange_or_install(staging_anchor, target)
            if not rolled_back or _tree_digest(phase.repository, anchor) != displaced_digest:
                raise SourceWriteError("integration conflict rollback could not restore the shared anchor")
            return {
                "mode": "isolated_exact_base",
                "status": "conflict",
                "patch_ref": patch_ref,
                "patch_sha256": _digest(patch_path.read_bytes()),
                "target_before": phase.target_before,
                "target_after": {},
                "changed_by_task": changed_by_task,
            }
        if _tree_digest(phase.repository, anchor) != target_after[anchor]:
            _rollback_applied_anchor(staging_anchor, target, old_anchor_retained)
            _seal_displaced_anchor_evidence(
                phase,
                staging_anchor,
                reason="post_swap_shared_edit",
            )
            return {
                "mode": "isolated_exact_base",
                "status": "conflict",
                "patch_ref": patch_ref,
                "patch_sha256": _digest(patch_path.read_bytes()),
                "target_before": phase.target_before,
                "target_after": {},
                "changed_by_task": changed_by_task,
            }
        repository_fd = _open_directory_no_follow(phase.repository)
        try:
            os.fsync(repository_fd)
        finally:
            os.close(repository_fd)
    else:
        if (
            staging_anchor.exists()
            and _tree_digest_path(staging_anchor) == phase.target_before[anchor]
        ):
            target = phase.repository / anchor
            if target.exists():
                _atomic_exchange_or_install(staging_anchor, target)
                _seal_displaced_anchor_evidence(
                    phase,
                    staging_anchor,
                    reason="recovery_observed_mixed_shared_state",
                )
            else:
                _seal_displaced_anchor_evidence(
                    phase,
                    None,
                    reason="recovery_observed_missing_shared_anchor",
                )
                _atomic_exchange_or_install(staging_anchor, target)
        return {
            "mode": "isolated_exact_base",
            "status": "conflict",
            "patch_ref": patch_ref,
            "patch_sha256": _digest(patch_path.read_bytes()),
            "target_before": phase.target_before,
            "target_after": {},
            "changed_by_task": changed_by_task,
        }
    if _tree_digest(phase.repository, anchor) != target_after[anchor]:
        raise IntegrationConflict("atomic integration anchor did not reach the sealed after state")
    terminal = {
        "schema_version": "agent-workflow.integration-terminal.vnext.v1",
        "phase_id": phase.phase_id,
        "status": "applied",
        "patch_ref": patch_ref,
        "patch_sha256": _digest(patch_path.read_bytes()),
        "anchor": anchor,
        "target_before": phase.target_before,
        "target_after": target_after,
        "old_anchor_retained": old_anchor_retained,
    }
    _create_or_verify_json(phase.control_root, terminal_ref, terminal)
    return {
        "mode": "isolated_exact_base",
        "status": "applied",
        "patch_ref": patch_ref,
        "patch_sha256": _digest(patch_path.read_bytes()),
        "target_before": phase.target_before,
        "target_after": target_after,
        "changed_by_task": changed_by_task,
    }


def writer_profile_bytes(write_roots: tuple[str, ...]) -> bytes:
    """Return the exact named Codex profile for one isolated task workspace."""

    roots = tuple(_safe_root(value) for value in write_roots)
    lines = [
        'default_permissions = "vnext_writer"',
        "",
        "[permissions.vnext_writer]",
        'description = "Agent Workflow vNext isolated writer"',
        "",
        "[permissions.vnext_writer.filesystem]",
        '":minimal" = "read"',
        "",
        "[permissions.vnext_writer.filesystem.\":workspace_roots\"]",
        '"." = "read"',
    ]
    for root in roots:
        lines.append(f'{json.dumps(root, ensure_ascii=False)} = "write"')
        lines.append(f'{json.dumps(root + "/**", ensure_ascii=False)} = "write"')
    lines.extend(["", "[permissions.vnext_writer.network]", "enabled = false", ""])
    return "\n".join(lines).encode()


def attest_writer_permissions(
    context: dict[str, Any] | None,
    workspace_root: Path,
    codex_home: Path,
    write_roots: tuple[str, ...],
    codex_binary: Path | None = None,
) -> str | None:
    """Require the persisted effective profile to equal the task-specific allowlist."""

    if not isinstance(context, dict):
        return "persisted turn context is missing"
    profile = context.get("permission_profile")
    if not isinstance(profile, dict) or profile.get("type") != "managed":
        return "writer permission profile is not managed"
    if profile.get("network") != "restricted":
        return "writer network is not restricted"
    entries = profile.get("file_system", {}).get("entries")
    if profile.get("file_system", {}).get("type") != "restricted" or not isinstance(entries, list):
        return "writer filesystem is not restricted"
    workspace_root = workspace_root.resolve()
    if context.get("workspace_roots") != [os.fspath(workspace_root)]:
        return "writer workspace roots are not exact"
    expected = {("special:minimal", "read"), (os.fspath(workspace_root), "read")}
    expected.update((os.fspath((workspace_root / root).resolve()), "write") for root in write_roots)
    arg0 = codex_home.resolve() / "tmp" / "arg0"
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
    observed: set[tuple[str, str]] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), dict):
            return "writer filesystem entry is invalid"
        path, access = entry["path"], entry.get("access")
        if path.get("type") == "special" and path.get("value") == {"kind": "minimal"}:
            observed.add(("special:minimal", access))
            continue
        value = path.get("path") if path.get("type") == "path" else None
        if not isinstance(value, str):
            return "writer filesystem path is not concrete"
        resolved = Path(value).resolve()
        if resolved.is_relative_to(arg0) and resolved.name.startswith("codex-arg0") and access == "read":
            continue
        if resolved in runtime_reads and access == "read":
            continue
        observed.add((os.fspath(resolved), access))
    if observed != expected:
        return "writer filesystem allowlist is not exact"
    return None
