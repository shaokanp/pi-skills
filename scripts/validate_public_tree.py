#!/usr/bin/env python3
"""Validate that the worktree's public file set is explicit and complete."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


NOISE_PARTS = {"__pycache__"}
NOISE_NAMES = {".DS_Store"}
NOISE_SUFFIXES = {".pyc"}


def git_paths(root: Path, *args: str) -> list[str]:
    raw = subprocess.check_output(["git", "-C", str(root), *args])
    return [item.decode("utf-8", errors="surrogateescape") for item in raw.split(b"\0") if item]


def is_noise(path: str) -> bool:
    candidate = Path(path)
    return (
        candidate.name in NOISE_NAMES
        or candidate.suffix in NOISE_SUFFIXES
        or any(part in NOISE_PARTS for part in candidate.parts)
    )


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    manifest_path = root / "public-files.json"
    registry_path = root / "registry.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    registry = json.loads(registry_path.read_text(encoding="utf-8"))

    if manifest.get("schema_version") != "pi-skills.public-files.v1":
        raise SystemExit("unsupported public-files schema")

    allowed_files = set(manifest.get("allowed_files", []))
    allowed_prefixes = tuple(manifest.get("allowed_prefixes", []))
    forbidden_files = set(manifest.get("forbidden_files", []))
    public_paths = {
        path
        for path in git_paths(
            root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"
        )
        if (root / path).is_file() or (root / path).is_symlink()
    }

    failures: list[str] = []
    for path in sorted(public_paths):
        if path in forbidden_files:
            failures.append(f"forbidden local-only file is public: {path}")
        elif path not in allowed_files and not path.startswith(allowed_prefixes):
            failures.append(f"public path is not allowlisted: {path}")

    for required in sorted(allowed_files):
        if required not in public_paths:
            failures.append(f"allowlisted public file is missing: {required}")

    for local_only in sorted(forbidden_files):
        tracked = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--error-unmatch", "--", local_only],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if tracked.returncode == 0:
            failures.append(f"local-only file is tracked: {local_only}")
        ignored = subprocess.run(
            ["git", "-C", str(root), "check-ignore", "-q", "--", local_only],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if ignored.returncode != 0:
            failures.append(f"local-only file is not ignored: {local_only}")

    registered_sources = {item["source"] for item in registry.get("skills", [])}
    actual_sources = {
        str(path.relative_to(root))
        for path in (root / "skills").iterdir()
        if path.is_dir() and (path / "SKILL.md").is_file()
    }
    for source in sorted(actual_sources - registered_sources):
        failures.append(f"unregistered skill source: {source}")
    for source in sorted(registered_sources - actual_sources):
        failures.append(f"registered skill source is missing: {source}")

    ignored_skill_paths = git_paths(
        root,
        "ls-files",
        "-z",
        "--others",
        "--ignored",
        "--exclude-standard",
        "--",
        "skills",
    )
    for path in sorted(item for item in ignored_skill_paths if not is_noise(item)):
        failures.append(f"ignored local file is forbidden inside public skill source: {path}")

    if failures:
        print("public tree validation failed:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1

    print(f"validated public tree ({len(public_paths)} files, {len(actual_sources)} skills)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
