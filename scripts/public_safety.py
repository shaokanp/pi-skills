#!/usr/bin/env python3
"""Fail-closed public-content scanner for pi-skills source and Git history."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Iterable


GENERIC_PRIVATE_PATTERNS = (
    (
        "absolute Unix user home path",
        re.compile(r"(?<![A-Za-z0-9_.-])(?:/Users|/home)/[A-Za-z0-9_.-]+"),
    ),
    (
        "absolute Windows user home path",
        re.compile(r"(?i)\b[A-Z]:\\Users\\[^\\\s]+"),
    ),
    (
        "local-only email",
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.local\b", re.IGNORECASE),
    ),
)

SECRET_PATTERNS = (
    (
        "private key",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    ),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b")),
    ("generic sk token", re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}\b")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("Stripe live key", re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{16,}\b")),
    ("npm token", re.compile(r"\bnpm_[A-Za-z0-9]{30,}\b")),
    ("PyPI token", re.compile(r"\bpypi-[A-Za-z0-9_-]{30,}\b")),
    ("SendGrid key", re.compile(r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b")),
    ("bearer token", re.compile(r"\bBearer [A-Za-z0-9._~+/=-]{24,}\b", re.IGNORECASE)),
    (
        "credential in URL",
        re.compile(r"https?://[^\s/:@]+:[^\s/@]+@[^\s/]+", re.IGNORECASE),
    ),
)

ASSIGNED_SECRET_PATTERN = re.compile(
    r"""(?ix)
    \b(?:
      api[_-]?key|
      access[_-]?token|
      auth[_-]?token|
      client[_-]?secret|
      private[_-]?token|
      password|passwd
    )\b
    \s*(?:=|:)\s*
    [\"']?
    (?P<value>[A-Za-z0-9_./+=:@-]{12,})
    """
)

SENSITIVE_SUFFIXES = (
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".sqlite",
    ".sqlite3",
    ".db",
    ".log",
)
SENSITIVE_BASENAMES = {
    ".ds_store",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".pi-skills.local.json",
    "credentials",
    "credentials.json",
    "id_ed25519",
    "id_rsa",
    "service-account.json",
}
SENSITIVE_PARTS = {".workflow", "logs", "state"}
PLACEHOLDER_FRAGMENTS = {
    "changeme",
    "example",
    "placeholder",
    "redacted",
    "replace_me",
    "replace-with",
    "sample",
    "your_",
    "your-",
}


class PublicSafetyScanner:
    def __init__(self, root: Path, local_markers: Iterable[str]) -> None:
        self.root = root
        self.local_markers = tuple(
            marker for marker in local_markers if isinstance(marker, str) and len(marker.strip()) >= 4
        )
        self.findings: set[tuple[str, str, int, str]] = set()
        self._blob_cache: dict[str, bytes] = {}

    def run_git(self, *args: str, check: bool = True) -> bytes:
        result = subprocess.run(
            ["git", "-C", str(self.root), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if check and result.returncode != 0:
            message = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(message or f"git {' '.join(args)} failed")
        return result.stdout

    @staticmethod
    def line_number(text: str, offset: int) -> int:
        return text.count("\n", 0, offset) + 1

    @staticmethod
    def is_placeholder(value: str) -> bool:
        lowered = value.lower()
        return any(fragment in lowered for fragment in PLACEHOLDER_FRAGMENTS)

    def add_finding(self, origin: str, path: str, line: int, label: str) -> None:
        self.findings.add((origin, path, line, label))

    def check_path(self, path: str, origin: str) -> None:
        normalized = path.replace("\\", "/")
        parts = [part.lower() for part in Path(normalized).parts]
        basename = parts[-1] if parts else ""
        if basename == ".env" or basename.startswith(".env."):
            self.add_finding(origin, path, 0, "environment file name")
        if basename in SENSITIVE_BASENAMES or basename.endswith(SENSITIVE_SUFFIXES):
            self.add_finding(origin, path, 0, "sensitive file name")
        if any(part in SENSITIVE_PARTS for part in parts):
            self.add_finding(origin, path, 0, "private runtime path")

    def check_content(self, path: str, raw: bytes, origin: str) -> None:
        self.check_path(path, origin)
        if b"\0" in raw[:8192]:
            return

        text = raw.decode("utf-8", errors="replace")
        lowered = text.casefold()

        for marker in self.local_markers:
            offset = lowered.find(marker.casefold())
            if offset >= 0:
                self.add_finding(
                    origin,
                    path,
                    self.line_number(text, offset),
                    "local private marker",
                )

        for label, pattern in GENERIC_PRIVATE_PATTERNS:
            match = pattern.search(text)
            if match:
                self.add_finding(origin, path, self.line_number(text, match.start()), label)

        for label, pattern in SECRET_PATTERNS:
            match = pattern.search(text)
            if match:
                self.add_finding(origin, path, self.line_number(text, match.start()), label)

        assigned = ASSIGNED_SECRET_PATTERN.search(text)
        if assigned and not self.is_placeholder(assigned.group("value")):
            self.add_finding(
                origin,
                path,
                self.line_number(text, assigned.start()),
                "assigned credential-like value",
            )

    def scan_worktree(self) -> None:
        paths = self.run_git(
            "ls-files", "-z", "--cached", "--others", "--exclude-standard"
        ).split(b"\0")
        for encoded in paths:
            if not encoded:
                continue
            path = encoded.decode("utf-8", errors="surrogateescape")
            full_path = self.root / path
            if full_path.is_symlink():
                raw = os.readlink(full_path).encode("utf-8", errors="replace")
            elif full_path.is_file():
                raw = full_path.read_bytes()
            else:
                continue
            self.check_content(path, raw, "worktree")

    def scan_index(self) -> None:
        entries = self.run_git("ls-files", "-s", "-z").split(b"\0")
        for entry in entries:
            if not entry:
                continue
            metadata, encoded_path = entry.split(b"\t", 1)
            _mode, object_id, stage = metadata.decode("ascii").split()
            if stage != "0":
                self.add_finding("index", encoded_path.decode(errors="replace"), 0, "unmerged index entry")
                continue
            path = encoded_path.decode("utf-8", errors="surrogateescape")
            raw = self._read_blob(object_id)
            self.check_content(path, raw, "index")

    def _read_blob(self, object_id: str) -> bytes:
        if object_id not in self._blob_cache:
            self._blob_cache[object_id] = self.run_git("cat-file", "-p", object_id)
        return self._blob_cache[object_id]

    def _scan_tree(self, treeish: str, origin: str) -> None:
        entries = self.run_git("ls-tree", "-r", "-z", "--full-tree", treeish).split(b"\0")
        for entry in entries:
            if not entry:
                continue
            metadata, encoded_path = entry.split(b"\t", 1)
            _mode, object_type, object_id = metadata.decode("ascii").split()
            if object_type != "blob":
                continue
            path = encoded_path.decode("utf-8", errors="surrogateescape")
            self.check_content(path, self._read_blob(object_id), origin)

    def scan_commits(self, commits: Iterable[str]) -> None:
        seen: set[str] = set()
        for commit in commits:
            if not commit or commit in seen:
                continue
            seen.add(commit)
            metadata = self.run_git(
                "show", "-s", "--format=%an%n%ae%n%cn%n%ce%n%B", commit
            )
            origin = commit[:12]
            self.check_content("<commit-metadata>", metadata, origin)
            self._scan_tree(commit, origin)

    def scan_history(self) -> None:
        commits = self.run_git("rev-list", "HEAD").decode().splitlines()
        self.scan_commits(commits)

    @staticmethod
    def _is_zero_oid(object_id: str) -> bool:
        return bool(object_id) and set(object_id) == {"0"}

    def _object_type(self, object_id: str) -> str | None:
        result = subprocess.run(
            ["git", "-C", str(self.root), "cat-file", "-t", object_id],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode != 0:
            return None
        return result.stdout.decode().strip()

    def _scan_ref_object(self, object_id: str, local_ref: str) -> list[str]:
        object_type = self._object_type(object_id)
        if object_type == "tag":
            self.check_content(
                f"<tag-metadata:{local_ref}>",
                self.run_git("cat-file", "-p", object_id),
                object_id[:12],
            )
            peeled = self.run_git("rev-parse", f"{object_id}^{{}}").decode().strip()
            object_id = peeled
            object_type = self._object_type(object_id)

        if object_type == "commit":
            return [object_id]
        if object_type == "tree":
            self._scan_tree(object_id, object_id[:12])
            return []
        if object_type == "blob":
            self.check_content(f"<pushed-blob:{local_ref}>", self._read_blob(object_id), object_id[:12])
            return []

        self.add_finding("push", local_ref, 0, "unsupported or missing pushed object")
        return []

    def scan_push_refs(self, refs_file: Path) -> None:
        commits: set[str] = set()
        for line_number, line in enumerate(refs_file.read_text(encoding="utf-8").splitlines(), 1):
            fields = line.split()
            if len(fields) != 4:
                self.add_finding("push", str(refs_file), line_number, "invalid pre-push ref record")
                continue
            local_ref, local_oid, _remote_ref, remote_oid = fields
            if local_ref == "(delete)" or self._is_zero_oid(local_oid):
                continue

            tips = self._scan_ref_object(local_oid, local_ref)
            for tip in tips:
                args = ["rev-list", tip]
                if not self._is_zero_oid(remote_oid) and self._object_type(remote_oid) == "commit":
                    args.append(f"^{remote_oid}")
                commits.update(self.run_git(*args).decode().splitlines())

        self.scan_commits(sorted(commits))

    def scan_artifacts(self) -> None:
        artifact_root = self.root / "dist"
        if not artifact_root.is_dir():
            raise RuntimeError("dist directory is missing; package before checking artifacts")

        with (self.root / "registry.json").open("r", encoding="utf-8") as handle:
            registry = json.load(handle)

        expected_artifacts: set[str] = set()
        for skill in registry.get("skills", []):
            archive_name = f"{skill['id']}-{skill['version']}.tar.gz"
            expected_artifacts.add(archive_name)
            expected_artifacts.add(f"{archive_name}.sha256")

        for artifact in artifact_root.iterdir():
            if not artifact.is_file():
                continue
            if not (artifact.name.endswith(".tar.gz") or artifact.name.endswith(".tar.gz.sha256")):
                continue
            if artifact.name not in expected_artifacts:
                self.add_finding(
                    "artifact",
                    str(artifact.relative_to(self.root)),
                    0,
                    "unexpected unregistered artifact",
                )

        for skill in registry.get("skills", []):
            archive = artifact_root / f"{skill['id']}-{skill['version']}.tar.gz"
            checksum = Path(str(archive) + ".sha256")
            for required in (archive, checksum):
                if not required.is_file():
                    self.add_finding(
                        "artifact",
                        str(required.relative_to(self.root)),
                        0,
                        "missing current artifact",
                    )
            if checksum.is_file():
                self.check_content(
                    str(checksum.relative_to(self.root)), checksum.read_bytes(), "artifact"
                )
            if not archive.is_file():
                continue

            with tarfile.open(archive, "r:gz") as package:
                for member in package.getmembers():
                    package_path = f"{archive.name}:{member.name}"
                    self.check_path(package_path, "artifact")
                    member_parts = Path(member.name).parts
                    if member.name.startswith("/") or ".." in member_parts:
                        self.add_finding("artifact", package_path, 0, "unsafe archive path")
                    if member.issym() or member.islnk():
                        self.add_finding("artifact", package_path, 0, "archive link is not allowed")
                        continue
                    if not member.isfile():
                        continue
                    extracted = package.extractfile(member)
                    if extracted is not None:
                        self.check_content(package_path, extracted.read(), "artifact")


def load_local_markers(root: Path) -> tuple[str, ...]:
    markers: list[str] = []
    config_path = Path(
        os.environ.get("PI_SKILLS_LOCAL_CONFIG", str(root / ".pi-skills.local.json"))
    ).expanduser()
    if config_path.is_file():
        with config_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        configured = data.get("private_markers", [])
        if not isinstance(configured, list) or not all(isinstance(item, str) for item in configured):
            raise RuntimeError("private_markers in local config must be a list of strings")
        markers.extend(configured)

    environment_markers = os.environ.get("PI_SKILLS_PRIVATE_MARKERS", "")
    markers.extend(line.strip() for line in environment_markers.splitlines() if line.strip())
    return tuple(dict.fromkeys(marker.strip() for marker in markers if marker.strip()))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument(
        "--mode", required=True, choices=("worktree", "index", "history", "push", "artifacts")
    )
    parser.add_argument("--refs-file", type=Path)
    parser.add_argument("--require-local-markers", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    try:
        markers = load_local_markers(root)
        if args.require_local_markers and not markers:
            raise RuntimeError(
                "no private_markers configured; add them to the ignored .pi-skills.local.json"
            )
        scanner = PublicSafetyScanner(root, markers)
        if args.mode == "worktree":
            scanner.scan_worktree()
        elif args.mode == "index":
            scanner.scan_index()
        elif args.mode == "history":
            scanner.scan_history()
        elif args.mode == "push":
            if args.refs_file is None or not args.refs_file.is_file():
                raise RuntimeError("push mode requires --refs-file")
            scanner.scan_push_refs(args.refs_file)
        elif args.mode == "artifacts":
            scanner.scan_artifacts()
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError, tarfile.TarError) as error:
        print(f"public safety check failed ({args.mode}): {error}", file=sys.stderr)
        return 1

    if scanner.findings:
        print(f"public safety check failed ({args.mode}):", file=sys.stderr)
        for origin, path, line, label in sorted(scanner.findings):
            location = f"{path}:{line}" if line else path
            print(f"  {origin} {location}: {label}", file=sys.stderr)
        return 1

    marker_note = f", {len(markers)} local markers" if markers else ""
    print(f"public safety check passed ({args.mode}{marker_note})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
