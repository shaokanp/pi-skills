#!/usr/bin/env python3
"""Create-once, replayable dirty-tree baselines for Agent Workflow vNext."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import platform
import re
import subprocess
import sys
import zlib
from pathlib import Path, PurePosixPath
from typing import Any

# A pinned runtime directory is immutable authority, not a Python cache root.
sys.dont_write_bytecode = True

from artifact_store import ArtifactError, create_once_json


class BaselineError(ValueError):
    """Raised when a baseline cannot be collected or verified exactly."""


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _seal(value: dict[str, Any]) -> str:
    payload = {key: item for key, item in value.items() if key != "seal_sha256"}
    return _digest(_canonical(payload))


def repository_evidence(value: dict[str, Any]) -> dict[str, str]:
    """Return the exact repository digests embedded by the workflow admission seal."""

    verify_baseline(value)
    return {
        "head": value["head"],
        "branch": value["branch"],
        "staged_diff_sha256": value["staged_patch"]["sha256"],
        "unstaged_diff_sha256": value["unstaged_patch"]["sha256"],
        "untracked_manifest_sha256": _digest(_canonical(value["untracked"])),
        "relevant_files_sha256": _digest(_canonical(value["relevant_files"])),
        "dirty_paths_sha256": _digest(
            _canonical([item["path"] for item in value["relevant_files"]])
        ),
    }


def current_repository_evidence(repo: Path, baseline: dict[str, Any]) -> dict[str, str]:
    """Recompute admission digests from the live checkout using the sealed selection."""

    verify_baseline(baseline)
    repo = Path(repo).resolve()
    selection = baseline["selection"]
    excludes = selection["tracked_excludes"]
    staged = _diff(repo, cached=True, excludes=excludes, binary=False)
    unstaged = _diff(repo, cached=False, excludes=excludes, binary=False)
    available = {
        item.decode("utf-8")
        for item in _git(repo, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0")
        if item
    }
    selected = selection["untracked_paths"]
    if selection["untracked_mode"] == "all" and available != set(selected):
        raise BaselineError("live untracked manifest differs from the sealed all-files selection")
    if set(selected) - available:
        raise BaselineError("sealed untracked path is missing from the live checkout")

    untracked: list[dict[str, Any]] = []
    for relative in selected:
        path = repo / relative
        if path.is_symlink() or not path.is_file():
            raise BaselineError(f"live untracked path is unsafe: {relative}")
        untracked.append({"path": relative, "content": _packed(path.read_bytes())})

    relevant_paths = sorted(
        _changed_paths(repo, cached=True, excludes=excludes)
        | _changed_paths(repo, cached=False, excludes=excludes)
        | set(selected)
    )
    relevant_files: list[dict[str, Any]] = []
    for relative in relevant_paths:
        path = repo / _safe_relative(relative, "live relevant path")
        if not path.exists():
            relevant_files.append(
                {"path": relative, "status": "deleted", "sha256": None, "bytes": 0}
            )
        elif path.is_symlink() or not path.is_file():
            raise BaselineError(f"live relevant path is unsafe: {relative}")
        else:
            payload = path.read_bytes()
            relevant_files.append(
                {
                    "path": relative,
                    "status": "present",
                    "sha256": _digest(payload),
                    "bytes": len(payload),
                }
            )

    return {
        "head": _git(repo, "rev-parse", "HEAD").decode().strip(),
        "branch": _git(repo, "branch", "--show-current").decode().strip(),
        "staged_diff_sha256": _digest(staged),
        "unstaged_diff_sha256": _digest(unstaged),
        "untracked_manifest_sha256": _digest(_canonical(untracked)),
        "relevant_files_sha256": _digest(_canonical(relevant_files)),
        "dirty_paths_sha256": _digest(
            _canonical([item["path"] for item in relevant_files])
        ),
    }


def _path_covered(path: str, intended_roots: list[str]) -> bool:
    path_parts = PurePosixPath(path).parts
    return any(
        path_parts[: len(PurePosixPath(root).parts)] == PurePosixPath(root).parts
        for root in intended_roots
    )


def verify_candidate_against_parent(
    candidate: dict[str, Any],
    parent: dict[str, Any],
) -> None:
    verify_baseline(candidate)
    verify_baseline(parent)
    if candidate["baseline_kind"] != "candidate_gate" or parent["baseline_kind"] != "pre_slice":
        raise BaselineError("candidate baseline must reference a pre_slice baseline")
    if candidate["head"] != parent["head"] or candidate["branch"] != parent["branch"]:
        raise BaselineError("candidate baseline must preserve parent HEAD and branch")
    parent_files = {item["path"]: item for item in parent["relevant_files"]}
    changed_since_parent: list[str] = []
    for item in candidate["relevant_files"]:
        prior = parent_files.get(item["path"])
        if prior != item:
            changed_since_parent.append(item["path"])
    missing_parent_paths = sorted(set(parent_files) - {item["path"] for item in candidate["relevant_files"]})
    changed_since_parent.extend(missing_parent_paths)
    uncovered = sorted(
        path for path in changed_since_parent if not _path_covered(path, candidate["intended_changes"])
    )
    if uncovered:
        raise BaselineError(
            "candidate intended changes do not cover dirty paths: " + ", ".join(uncovered)
        )


def _safe_relative(value: str, label: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise BaselineError(f"{label} must be a safe relative path")
    return path.as_posix()


def _git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        raise BaselineError(result.stderr.decode("utf-8", errors="replace").strip())
    return result.stdout


def _repository_root_for(path: Path) -> Path:
    current = path.resolve().parent
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    raise BaselineError("could not locate repository root for baseline manifest")


def _packed(payload: bytes) -> dict[str, Any]:
    return {
        "sha256": _digest(payload),
        "bytes": len(payload),
        "zlib_base64": base64.b64encode(zlib.compress(payload, level=9)).decode("ascii"),
    }


def _unpack(value: Any, label: str) -> bytes:
    if not isinstance(value, dict) or set(value) != {"sha256", "bytes", "zlib_base64"}:
        raise BaselineError(f"{label} has an invalid packed payload")
    try:
        payload = zlib.decompress(base64.b64decode(value["zlib_base64"], validate=True))
    except (ValueError, TypeError, zlib.error) as exc:
        raise BaselineError(f"{label} payload is corrupt") from exc
    if len(payload) != value["bytes"] or _digest(payload) != value["sha256"]:
        raise BaselineError(f"{label} digest or byte count mismatch")
    return payload


def _diff(repo: Path, *, cached: bool, excludes: list[str], binary: bool) -> bytes:
    args = ["diff"]
    if cached:
        args.append("--cached")
    if binary:
        args.extend(["--binary", "--full-index", "--no-ext-diff"])
    args.extend(["--", "."])
    args.extend(f":(exclude){path}" for path in excludes)
    return _git(repo, *args)


def _changed_paths(repo: Path, *, cached: bool, excludes: list[str]) -> set[str]:
    args = ["diff"]
    if cached:
        args.append("--cached")
    args.extend(["--name-only", "-z", "--", "."])
    args.extend(f":(exclude){path}" for path in excludes)
    return {
        item.decode("utf-8")
        for item in _git(repo, *args).split(b"\0")
        if item
    }


def collect_baseline(
    repo: Path,
    *,
    tracked_excludes: list[str] | None = None,
    untracked_includes: list[str] | None = None,
    untracked_content_overrides: dict[str, bytes] | None = None,
    parent_summary: dict[str, Any] | None = None,
    baseline_kind: str = "pre_slice",
    candidate_parent: dict[str, str] | None = None,
    candidate_parent_manifest: dict[str, Any] | None = None,
    intended_changes: list[str] | None = None,
    model: str = "current_session_not_attested",
    reasoning_effort: str = "current_session_not_attested",
) -> dict[str, Any]:
    repo = Path(repo).resolve()
    excludes = sorted({_safe_relative(path, "tracked exclude") for path in (tracked_excludes or [])})
    staged = _diff(repo, cached=True, excludes=excludes, binary=False)
    unstaged = _diff(repo, cached=False, excludes=excludes, binary=False)
    staged_binary = _diff(repo, cached=True, excludes=excludes, binary=True)
    unstaged_binary = _diff(repo, cached=False, excludes=excludes, binary=True)

    available = {
        item.decode("utf-8")
        for item in _git(repo, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0")
        if item
    }
    if untracked_includes is None:
        selected = sorted(available)
    else:
        selected = sorted(_safe_relative(path, "untracked include") for path in untracked_includes)
        missing = sorted(set(selected) - available)
        if missing:
            raise BaselineError(f"untracked include is not currently untracked: {', '.join(missing)}")
    untracked: list[dict[str, Any]] = []
    overrides = untracked_content_overrides or {}
    if set(overrides) - set(selected):
        raise BaselineError("untracked content override must target a selected untracked path")
    for relative in selected:
        path = repo / relative
        if not path.is_file() or path.is_symlink():
            raise BaselineError(f"untracked snapshot only accepts regular files: {relative}")
        untracked.append({"path": relative, "content": _packed(overrides.get(relative, path.read_bytes()))})

    relevant_paths = sorted(
        _changed_paths(repo, cached=True, excludes=excludes)
        | _changed_paths(repo, cached=False, excludes=excludes)
        | set(selected)
    )
    relevant_files: list[dict[str, Any]] = []
    for relative in relevant_paths:
        safe = _safe_relative(relative, "relevant file")
        path = repo / safe
        if not path.exists():
            relevant_files.append({"path": safe, "status": "deleted", "sha256": None, "bytes": 0})
            continue
        if not path.is_file() or path.is_symlink():
            raise BaselineError(f"relevant snapshot only accepts regular files: {safe}")
        payload = overrides.get(safe, path.read_bytes())
        relevant_files.append(
            {"path": safe, "status": "present", "sha256": _digest(payload), "bytes": len(payload)}
        )

    if baseline_kind not in {"pre_slice", "candidate_gate"}:
        raise BaselineError("baseline kind must be pre_slice or candidate_gate")
    intended = sorted(_safe_relative(path, "intended change") for path in (intended_changes or []))
    if len(intended) != len(set(intended)):
        raise BaselineError("intended changes must be unique")
    if baseline_kind == "pre_slice":
        if parent_summary is None or candidate_parent is not None or candidate_parent_manifest is not None or intended:
            raise BaselineError("pre_slice baseline requires parent summary and no candidate fields")
    elif (
        candidate_parent is None
        or candidate_parent_manifest is None
        or not intended
        or parent_summary is not None
    ):
        raise BaselineError("candidate_gate baseline requires candidate parent and intended changes")
    if baseline_kind == "candidate_gate" and (excludes or untracked_includes is not None):
        raise BaselineError("candidate_gate baseline requires full tracked and untracked selection")
    if baseline_kind == "candidate_gate":
        _safe_relative(candidate_parent["path"], "candidate parent path")
        if candidate_parent["sha256"] != _digest(_canonical(candidate_parent_manifest)):
            raise BaselineError("candidate parent digest does not match supplied manifest")

    manifest: dict[str, Any] = {
        "schema_version": "agent-workflow.vnext-replayable-baseline.v2",
        "baseline_kind": baseline_kind,
        "head": _git(repo, "rev-parse", "HEAD").decode().strip(),
        "branch": _git(repo, "branch", "--show-current").decode().strip(),
        "environment": {
            "codex_cli": subprocess.run(
                ["codex", "--version"], text=True, capture_output=True, check=False
            ).stdout.strip() or "unavailable",
            "platform": platform.platform(),
            "model": model,
            "reasoning_effort": reasoning_effort,
        },
        "selection": {
            "tracked_excludes": excludes,
            "untracked_mode": "all" if untracked_includes is None else "explicit",
            "untracked_paths": selected,
        },
        "staged_patch": _packed(staged),
        "unstaged_patch": _packed(unstaged),
        "staged_binary_patch": _packed(staged_binary),
        "unstaged_binary_patch": _packed(unstaged_binary),
        "untracked": untracked,
        "relevant_files": relevant_files,
        "candidate_parent": candidate_parent,
        "intended_changes": intended,
        "immutability": "create_once_do_not_rewrite",
    }
    if parent_summary is not None:
        expected = {
            "head": parent_summary["head"],
            "branch": parent_summary["branch"],
            "staged_diff_sha256": parent_summary["staged_diff_sha256"],
            "staged_diff_bytes": parent_summary["staged_diff_bytes"],
            "unstaged_diff_sha256": parent_summary["unstaged_diff_sha256"],
            "unstaged_diff_bytes": parent_summary["unstaged_diff_bytes"],
        }
        observed = {
            "head": manifest["head"],
            "branch": manifest["branch"],
            "staged_diff_sha256": _digest(staged),
            "staged_diff_bytes": len(staged),
            "unstaged_diff_sha256": _digest(unstaged),
            "unstaged_diff_bytes": len(unstaged),
        }
        if observed != expected:
            raise BaselineError("reconstructed baseline does not match the parent summary")
        expected_untracked = {
            item["path"]: (item["sha256"], item["bytes"])
            for item in parent_summary["untracked"]
        }
        observed_untracked = {
            item["path"]: (item["content"]["sha256"], item["content"]["bytes"])
            for item in untracked
        }
        if observed_untracked != expected_untracked:
            raise BaselineError("reconstructed untracked snapshot does not match the parent summary")
        manifest["parent_summary"] = {
            "schema_version": parent_summary["schema_version"],
            "summary_sha256": _digest(_canonical(parent_summary)),
            "head": parent_summary["head"],
            "branch": parent_summary["branch"],
        }
    else:
        manifest["parent_summary"] = None
    manifest["seal_sha256"] = _seal(manifest)
    if baseline_kind == "candidate_gate":
        verify_candidate_against_parent(manifest, candidate_parent_manifest)
    return manifest


def verify_baseline(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BaselineError("baseline must be an object")
    required = {
        "schema_version",
        "baseline_kind",
        "head",
        "branch",
        "environment",
        "selection",
        "staged_patch",
        "unstaged_patch",
        "staged_binary_patch",
        "unstaged_binary_patch",
        "untracked",
        "relevant_files",
        "parent_summary",
        "candidate_parent",
        "intended_changes",
        "immutability",
        "seal_sha256",
    }
    if set(value) != required:
        raise BaselineError("baseline keys are invalid")
    if value["schema_version"] != "agent-workflow.vnext-replayable-baseline.v2":
        raise BaselineError("baseline schema is invalid")
    if value["baseline_kind"] not in {"pre_slice", "candidate_gate"}:
        raise BaselineError("baseline kind is invalid")
    if not isinstance(value["head"], str) or not re.fullmatch(r"[0-9a-f]{40,64}", value["head"]):
        raise BaselineError("baseline head is invalid")
    if not isinstance(value["branch"], str) or not value["branch"].strip():
        raise BaselineError("baseline branch is invalid")
    environment = value["environment"]
    if not isinstance(environment, dict) or set(environment) != {
        "codex_cli", "platform", "model", "reasoning_effort"
    } or not all(isinstance(item, str) and item.strip() for item in environment.values()):
        raise BaselineError("baseline environment is invalid")
    selection = value["selection"]
    if not isinstance(selection, dict) or set(selection) != {
        "tracked_excludes", "untracked_mode", "untracked_paths"
    }:
        raise BaselineError("baseline selection is invalid")
    if not isinstance(selection["tracked_excludes"], list) or not isinstance(
        selection["untracked_paths"], list
    ):
        raise BaselineError("baseline selection lists are invalid")
    excludes = [_safe_relative(path, "tracked exclude") for path in selection["tracked_excludes"]]
    if excludes != sorted(set(excludes)):
        raise BaselineError("tracked excludes must be sorted and unique")
    if selection["untracked_mode"] not in {"all", "explicit"}:
        raise BaselineError("untracked mode is invalid")
    selected_paths = [_safe_relative(path, "selected untracked path") for path in selection["untracked_paths"]]
    if selected_paths != sorted(set(selected_paths)):
        raise BaselineError("selected untracked paths must be sorted and unique")
    if value["immutability"] != "create_once_do_not_rewrite":
        raise BaselineError("baseline immutability marker is invalid")
    if value["seal_sha256"] != _seal(value):
        raise BaselineError("baseline seal digest mismatch")
    for field in ("staged_patch", "unstaged_patch", "staged_binary_patch", "unstaged_binary_patch"):
        _unpack(value[field], field)
    if not isinstance(value["untracked"], list) or not isinstance(value["relevant_files"], list):
        raise BaselineError("baseline file manifests must be lists")
    paths: set[str] = set()
    for index, item in enumerate(value["untracked"]):
        if not isinstance(item, dict) or set(item) != {"path", "content"}:
            raise BaselineError(f"untracked[{index}] is invalid")
        path = _safe_relative(item["path"], f"untracked[{index}].path")
        if path in paths:
            raise BaselineError("untracked paths must be unique")
        paths.add(path)
        _unpack(item["content"], f"untracked[{index}].content")
    if sorted(paths) != selected_paths:
        raise BaselineError("selection does not match untracked snapshot")
    relevant_paths: set[str] = set()
    for index, item in enumerate(value["relevant_files"]):
        if not isinstance(item, dict) or set(item) != {"path", "status", "sha256", "bytes"}:
            raise BaselineError(f"relevant_files[{index}] is invalid")
        path = _safe_relative(item["path"], f"relevant_files[{index}].path")
        if path in relevant_paths:
            raise BaselineError("relevant file paths must be unique")
        relevant_paths.add(path)
        if item["status"] == "deleted":
            if item["sha256"] is not None or item["bytes"] != 0:
                raise BaselineError("deleted relevant file must have null digest and zero bytes")
        elif item["status"] == "present":
            if not isinstance(item["bytes"], int) or item["bytes"] < 0:
                raise BaselineError("relevant file byte count is invalid")
            if not isinstance(item["sha256"], str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", item["sha256"]):
                raise BaselineError("relevant file digest is invalid")
        else:
            raise BaselineError("relevant file status is invalid")
    if [item["path"] for item in value["relevant_files"]] != sorted(relevant_paths):
        raise BaselineError("relevant files must be sorted")
    relevant_by_path = {item["path"]: item for item in value["relevant_files"]}
    for item in value["untracked"]:
        relevant = relevant_by_path.get(item["path"])
        if relevant is None or relevant["status"] != "present":
            raise BaselineError("every untracked file must appear in relevant files")
        if (
            relevant["sha256"] != item["content"]["sha256"]
            or relevant["bytes"] != item["content"]["bytes"]
        ):
            raise BaselineError("untracked content does not match relevant file digest")
    if not isinstance(value["intended_changes"], list):
        raise BaselineError("intended changes must be a list")
    intended = [_safe_relative(path, "intended change") for path in value["intended_changes"]]
    if intended != sorted(set(intended)):
        raise BaselineError("intended changes must be sorted and unique")
    if value["baseline_kind"] == "pre_slice":
        parent = value["parent_summary"]
        if not isinstance(parent, dict) or set(parent) != {"schema_version", "summary_sha256", "head", "branch"}:
            raise BaselineError("pre_slice parent summary is invalid")
        if parent["head"] != value["head"] or parent["branch"] != value["branch"]:
            raise BaselineError("pre_slice parent identity does not match baseline")
        if value["candidate_parent"] is not None or intended:
            raise BaselineError("pre_slice baseline cannot contain candidate fields")
    else:
        parent = value["candidate_parent"]
        if not isinstance(parent, dict) or set(parent) != {"path", "sha256"}:
            raise BaselineError("candidate parent is invalid")
        _safe_relative(parent["path"], "candidate parent path")
        if not isinstance(parent["sha256"], str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", parent["sha256"]):
            raise BaselineError("candidate parent digest is invalid")
        if value["parent_summary"] is not None or not intended:
            raise BaselineError("candidate baseline requires intended changes only")
        if excludes or selection["untracked_mode"] != "all":
            raise BaselineError("candidate baseline must use full tracked and untracked selection")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    collect = sub.add_parser("collect")
    collect.add_argument("--repo", type=Path, required=True)
    collect.add_argument("--output", required=True)
    collect.add_argument("--tracked-exclude", action="append", default=[])
    collect.add_argument("--untracked-include", action="append")
    collect.add_argument(
        "--untracked-content",
        action="append",
        default=[],
        metavar="PATH=SOURCE",
        help="Use SOURCE bytes for a selected path when reconstructing a prior sealed tree.",
    )
    collect.add_argument("--parent-summary", type=Path)
    collect.add_argument("--kind", choices=("pre_slice", "candidate_gate"), default="pre_slice")
    collect.add_argument("--candidate-parent-manifest", type=Path)
    collect.add_argument("--intended-change", action="append", default=[])
    collect.add_argument("--model", default="current_session_not_attested")
    collect.add_argument("--reasoning-effort", default="current_session_not_attested")
    verify = sub.add_parser("verify")
    verify.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "collect":
            parent = json.loads(args.parent_summary.read_text()) if args.parent_summary else None
            overrides: dict[str, bytes] = {}
            for raw in args.untracked_content:
                if "=" not in raw:
                    raise BaselineError("--untracked-content must be PATH=SOURCE")
                path, source = raw.split("=", 1)
                overrides[_safe_relative(path, "untracked content path")] = Path(source).read_bytes()
            candidate_parent = None
            candidate_parent_manifest = None
            if args.candidate_parent_manifest:
                parent_path = args.candidate_parent_manifest.resolve()
                repo_root = args.repo.resolve()
                try:
                    relative_parent = parent_path.relative_to(repo_root).as_posix()
                except ValueError as exc:
                    raise BaselineError("candidate parent manifest must be inside the repository root") from exc
                parent_bytes = parent_path.read_bytes()
                candidate_parent_manifest = json.loads(parent_bytes)
                candidate_parent = {
                    "path": _safe_relative(relative_parent, "candidate parent path"),
                    "sha256": _digest(parent_bytes),
                }
            manifest = collect_baseline(
                args.repo,
                tracked_excludes=args.tracked_exclude,
                untracked_includes=args.untracked_include,
                untracked_content_overrides=overrides,
                parent_summary=parent,
                baseline_kind=args.kind,
                candidate_parent=candidate_parent,
                candidate_parent_manifest=candidate_parent_manifest,
                intended_changes=args.intended_change,
                model=args.model,
                reasoning_effort=args.reasoning_effort,
            )
            verify_baseline(manifest)
            created = create_once_json(args.repo, args.output, manifest)
            print(json.dumps({"status": "created", "path": str(created)}, separators=(",", ":")))
        else:
            manifest_bytes = args.manifest.read_bytes()
            manifest = json.loads(manifest_bytes)
            verify_baseline(manifest)
            if manifest["baseline_kind"] == "candidate_gate":
                repo_root = _repository_root_for(args.manifest)
                parent_path = repo_root / manifest["candidate_parent"]["path"]
                parent_bytes = parent_path.read_bytes()
                if _digest(parent_bytes) != manifest["candidate_parent"]["sha256"]:
                    raise BaselineError("candidate parent digest mismatch")
                verify_candidate_against_parent(manifest, json.loads(parent_bytes))
            print(json.dumps({"status": "valid", "path": str(args.manifest)}, separators=(",", ":")))
    except (ArtifactError, BaselineError, OSError, KeyError, json.JSONDecodeError) as exc:
        print(f"baseline gate failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
