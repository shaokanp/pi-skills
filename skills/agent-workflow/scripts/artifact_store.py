#!/usr/bin/env python3
"""Create-once, crash-durable artifact writes for Agent Workflow vNext."""

from __future__ import annotations

import json
import os
import secrets
import fcntl
from contextlib import contextmanager
from functools import wraps
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator, TypeVar


class ArtifactError(ValueError):
    """Raised when an artifact path or immutable write is unsafe."""


_T = TypeVar("_T")


@contextmanager
def authority_transaction(root: Path, *, exclusive: bool = True) -> Iterator[Path]:
    """Hold a crash-released shared mutation or exclusive finalization fence."""

    descriptor, resolved = _open_or_create_root(Path(root))
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield resolved
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def serialized_authority_transaction(
    function: Callable[..., _T],
) -> Callable[..., _T]:
    """Crash-released process serialization for one root-first transaction."""

    @wraps(function)
    def wrapped(root: Path, *args: Any, **kwargs: Any) -> _T:
        with authority_transaction(Path(root)) as resolved:
            return function(resolved, *args, **kwargs)

    return wrapped


def shared_authority_transaction(
    function: Callable[..., _T],
) -> Callable[..., _T]:
    """Allow concurrent active mutations while excluding final publication."""

    @wraps(function)
    def wrapped(root: Path, *args: Any, **kwargs: Any) -> _T:
        with authority_transaction(Path(root), exclusive=False) as resolved:
            return function(resolved, *args, **kwargs)

    return wrapped


def _safe_parts(relative_path: str) -> tuple[str, ...]:
    if not isinstance(relative_path, str) or not relative_path:
        raise ArtifactError("artifact path must be a non-empty relative path")
    path = PurePosixPath(relative_path)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ArtifactError("artifact path must be a safe relative path")
    return path.parts


def _directory_flags() -> int:
    if not getattr(os, "O_DIRECTORY", 0) or not getattr(os, "O_NOFOLLOW", 0):
        raise ArtifactError("secure create-once writes require O_DIRECTORY and O_NOFOLLOW")
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _open_or_create_root(root: Path) -> tuple[int, Path]:
    absolute = root if root.is_absolute() else Path.cwd() / root
    absolute = Path(os.path.abspath(absolute))
    if absolute.is_symlink():
        raise ArtifactError("artifact root cannot be a symlink")
    # The host chooses the control root. Canonicalize only its parent so macOS
    # system aliases such as /var -> /private/var do not disable secure writes,
    # while an explicitly supplied symlink root still fails closed above.
    absolute = Path(os.path.realpath(absolute.parent)) / absolute.name
    flags = _directory_flags()
    current_fd = os.open("/", flags)
    try:
        for part in absolute.parts[1:]:
            created = False
            try:
                os.mkdir(part, mode=0o700, dir_fd=current_fd)
                created = True
                os.fsync(current_fd)
            except FileExistsError:
                pass
            next_fd = os.open(part, flags, dir_fd=current_fd)
            if created:
                os.fsync(next_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd, absolute
    except Exception:
        os.close(current_fd)
        raise


def create_once_bytes(root: Path, relative_path: str, payload: bytes) -> Path:
    """Durably publish exact bytes without replacing an existing artifact."""

    root = Path(root)
    parts = _safe_parts(relative_path)
    directory_parts, target_name = parts[:-1], parts[-1]
    if not isinstance(payload, bytes):
        raise ArtifactError("artifact payload must be bytes")

    opened_fds: list[int] = []
    temp_name: str | None = None
    parent_fd: int | None = None
    try:
        current_fd, root = _open_or_create_root(root)
        opened_fds.append(current_fd)
        for part in directory_parts:
            created = False
            try:
                os.mkdir(part, mode=0o700, dir_fd=current_fd)
                created = True
                os.fsync(current_fd)
            except FileExistsError:
                pass
            next_fd = os.open(part, _directory_flags(), dir_fd=current_fd)
            if created:
                os.fsync(next_fd)
            opened_fds.append(next_fd)
            current_fd = next_fd
        parent_fd = current_fd

        temp_name = f".{target_name}.{secrets.token_hex(8)}.tmp"
        temp_flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
        )
        temp_fd = os.open(temp_name, temp_flags, 0o600, dir_fd=parent_fd)
        try:
            view = memoryview(payload)
            while view:
                written = os.write(temp_fd, view)
                if written <= 0:
                    raise ArtifactError("artifact write made no progress")
                view = view[written:]
            os.fsync(temp_fd)
        finally:
            os.close(temp_fd)

        try:
            os.link(
                temp_name,
                target_name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise ArtifactError(f"artifact already exists: {relative_path}") from exc
        os.unlink(temp_name, dir_fd=parent_fd)
        temp_name = None
        os.fsync(parent_fd)
    except ArtifactError:
        raise
    except OSError as exc:
        raise ArtifactError(f"could not create immutable artifact {relative_path}: {exc}") from exc
    finally:
        if temp_name is not None and parent_fd is not None:
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        for file_descriptor in reversed(opened_fds):
            os.close(file_descriptor)

    return root.joinpath(*parts)


def create_once_json(root: Path, relative_path: str, value: Any) -> Path:
    """Durably publish canonical JSON without replacing an existing artifact."""

    payload = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    return create_once_bytes(root, relative_path, payload)
