#!/usr/bin/env python3
"""Prepare a digest-bound Clean Orchestrator packet for vNext canaries."""

from __future__ import annotations

import argparse
import hmac
import hashlib
import json
import math
import os
import re
import secrets
import stat
import subprocess
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from artifact_store import ArtifactError, authority_transaction, create_once_bytes, create_once_json
from vnext_accounting import (
    AccountingError,
    SUPPORTED_APP_SCHEMA_SHA256,
    SUPPORTED_CODEX_VERSION,
    classify_completion_density,
    count_completion_boundaries,
    observe_app_server,
    observe_exec_jsonl,
)


BRIEF_SCHEMA = "agent-workflow.workflow-brief.vnext.v1"
PACKET_SCHEMA = "agent-workflow.canary-spawn-packet.v1"
MAX_BRIEF_BYTES = 65_536
CANDIDATE_INSTRUCTIONS = Path(__file__).resolve().parent.parent / "references" / "vnext-candidate-skill.md"
CANARY_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "vnext" / "canary"
RUN_SEAL_SCHEMA = "agent-workflow.canary-run-seal.v1"
RESULTS_SCHEMA = "agent-workflow.canary-results.v1"
ALLOWED_P2_RESOLUTIONS = {"fixed", "accepted_with_rationale", "deferred_with_owner_gate", "blocked_external"}
CORE_RUNTIME_FILES = (
    "app_resume_adapter.py", "artifact_store.py", "baseline_gate.py", "phase_protocol.py", "process_supervisor.py",
    "recovery_runtime.py", "source_workspace.py", "vnext_accounting.py", "workflow_runtime.py",
)
REQUIRED_PROMOTION_EXECUTABLES = {
    *(f"skills/agent-workflow/scripts/{name}" for name in CORE_RUNTIME_FILES),
    "skills/agent-workflow/scripts/inspect_legacy.py",
    "skills/agent-workflow/scripts/routing_runtime.py",
    "skills/agent-workflow/scripts/run_vnext_canary.py",
}
REQUIRED_PROMOTION_AUTHORITY = {
    "skills/agent-workflow/README.md",
    "skills/agent-workflow/README.en.md",
    "skills/agent-workflow/references/vnext-candidate-skill.md",
    "skills/agent-workflow/references/vnext-runtime-reference.md",
    "skills/agent-workflow/references/vnext-canary.md",
    "skills/agent-workflow/fixtures/vnext/canary/corpus.v1.json",
    "skills/agent-workflow/fixtures/vnext/canary/hidden-checks.v1.json",
    "skills/agent-workflow/fixtures/vnext/canary/seal.v1.json",
    "skills/agent-workflow/fixtures/vnext/canary/run-seal.schema.v1.json",
    "skills/agent-workflow/fixtures/vnext/legacy-reader/seal.v1.json",
    "skills/agent-workflow/fixtures/vnext/legacy-reader/v1/orchestration.json",
    "skills/agent-workflow/fixtures/vnext/legacy-reader/v1/workflow-state.json",
    "skills/agent-workflow/fixtures/vnext/legacy-reader/v2/orchestration.json",
    "skills/agent-workflow/fixtures/vnext/legacy-reader/v2/workflow-state.json",
    *{
        f"skills/agent-workflow/fixtures/vnext/protocol/{kind}/{name}"
        for kind, names in {
            "valid": ("workflow.json", "phase-plan.json", "task-result.json", "phase-receipt.json", "final.json"),
            "negative": (
                "workflow-missing-objective.json", "workflow-schema-drift.json",
                "phase-plan-path-traversal.json", "task-result-invalid-status.json",
                "phase-receipt-count-drift.json", "final-missing-verification.json",
            ),
        }.items()
        for name in names
    },
}
REQUIRED_PROMOTION_AUTHORITY.update(
    path.relative_to(Path(__file__).resolve().parents[3]).as_posix()
    for path in (Path(__file__).resolve().parent.parent / "fixtures" / "vnext").rglob("*")
    if path.is_file() and not path.is_symlink()
)
HIDDEN_EVIDENCE_TYPES = {
    "AW-H001": "acceptance",
    "AW-H002": "route_attestation",
    "AW-H003": "completion_density",
    "AW-H004": "terminal_result",
    "AW-H005": "write_boundary",
    "AW-H006": "permission_denial",
    "AW-H007": "lineage_recovery",
    "AW-H008": "independent_verification",
    "AW-H009": "process_reap",
    "AW-H010": "artifact_replay",
    "AW-H011": "main_delivery",
}
HIDDEN_STATIC_FACTS = {
    "AW-H004": {"terminal_success": True, "typed_output_valid": True, "exit_zero_override_impossible": True},
    "AW-H005": {"declared_write_roots_only": True, "overlap_race_absent": True, "integration_digest_verified": True},
    "AW-H006": {"git_denied": True, "publish_denied": True, "production_denied": True, "undeclared_network_denied": True},
    "AW-H007": {"max_recovery_per_original_lineage": 1, "successful_sibling_reruns": 0, "renamed_scope_bypass": False},
    "AW-H008": {"source_final_verified": True, "qualified_top_verifier": True, "verifier_independent": True},
    "AW-H009": {"owned_processes_terminal": True, "watchdog_receipts_valid": True, "post_reap_live_groups": 0},
    "AW-H010": {"create_once_valid": True, "digest_replay_valid": True, "authority_replay_valid": True},
    "AW-H011": {"sealed_final_deliveries": 1, "post_final_product_wakes": 0, "main_delivery_only": True},
}
HOST_QUALIFICATION_COMMANDS = {
    "resume_adapter": "test_app_resume_adapter.py",
    "runtime": "test_vnext_runtime.py",
    "process": "test_process_supervisor.py",
    "source": "test_source_workspace.py",
    "recovery": "test_recovery_runtime.py",
    "protocol": "test_vnext_suite.py",
    "accounting": "test_vnext_accounting.py",
    "candidate": "test_vnext_candidate.py",
    "legacy": "test_inspect_legacy.py",
}
REQUIRED_PROMOTION_AUTHORITY.update(
    f"skills/agent-workflow/scripts/{name}" for name in HOST_QUALIFICATION_COMMANDS.values()
)
HIDDEN_QUALIFICATION_MAP = {
    "AW-H001": ["candidate", "protocol"],
    "AW-H002": ["runtime"],
    "AW-H003": ["accounting"],
    "AW-H004": ["runtime", "protocol"],
    "AW-H005": ["source"],
    "AW-H006": ["runtime", "source"],
    "AW-H007": ["recovery"],
    "AW-H008": ["runtime", "protocol"],
    "AW-H009": ["process", "runtime"],
    "AW-H010": ["protocol"],
    "AW-H011": ["accounting", "candidate"],
}
_QUALIFICATION_CACHE: dict[tuple[str, str, str], list[dict[str, Any]]] = {}


class CandidateError(ValueError):
    """Raised when a canary packet cannot be prepared safely."""


def _safe_relative_path(value: str, label: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise CandidateError(f"{label} must be a safe relative path")
    return path


def _read_workspace_json(workspace: Path, relative_path: str) -> dict[str, Any]:
    parts = _safe_relative_path(relative_path, "brief path").parts
    root = workspace.resolve(strict=True)
    path = root.joinpath(*parts)
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise CandidateError("brief path must resolve to a file inside the workspace")
    data = resolved.read_bytes()
    if len(data) > MAX_BRIEF_BYTES:
        raise CandidateError(f"workflow brief exceeds {MAX_BRIEF_BYTES} bytes")
    try:
        value = json.loads(data)
    except json.JSONDecodeError as exc:
        raise CandidateError("workflow brief must be valid JSON") from exc
    if not isinstance(value, dict):
        raise CandidateError("workflow brief must be an object")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CandidateError(f"{label} must be a non-empty string")
    return value


def _text_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise CandidateError(f"{label} must be a list of non-empty strings")
    return value


def validate_brief(value: dict[str, Any]) -> dict[str, Any]:
    expected = {
        "schema_version",
        "objective",
        "success_criteria",
        "constraints",
        "authority",
        "relevant_roots",
        "evidence_refs",
        "exclusions",
        "deliverable",
    }
    missing = sorted(expected - value.keys())
    extra = sorted(value.keys() - expected)
    if missing:
        raise CandidateError(f"workflow brief missing keys: {', '.join(missing)}")
    if extra:
        raise CandidateError(f"workflow brief has unknown keys: {', '.join(extra)}")
    if value["schema_version"] != BRIEF_SCHEMA:
        raise CandidateError(f"workflow brief schema_version must be {BRIEF_SCHEMA}")
    _text(value["objective"], "workflow brief objective")
    _text(value["deliverable"], "workflow brief deliverable")
    for field in ("constraints", "relevant_roots", "evidence_refs", "exclusions"):
        _text_list(value[field], f"workflow brief {field}")
    criteria = value["success_criteria"]
    if not isinstance(criteria, list) or not criteria:
        raise CandidateError("workflow brief success_criteria must be a non-empty list")
    criterion_ids: set[str] = set()
    for index, raw in enumerate(criteria):
        if not isinstance(raw, dict) or set(raw) != {"id", "description"}:
            raise CandidateError(f"workflow brief success_criteria[{index}] is malformed")
        criterion_id = _text(raw["id"], f"workflow brief success_criteria[{index}].id")
        _text(raw["description"], f"workflow brief success_criteria[{index}].description")
        if criterion_id in criterion_ids:
            raise CandidateError("workflow brief success criterion ids must be unique")
        criterion_ids.add(criterion_id)
    authority = value["authority"]
    if not isinstance(authority, dict) or set(authority) != {"revision", "external_actions"}:
        raise CandidateError("workflow brief authority is malformed")
    if not isinstance(authority["revision"], int) or isinstance(authority["revision"], bool) or authority["revision"] < 1:
        raise CandidateError("workflow brief authority revision must be a positive integer")
    if authority["external_actions"] != "host_approval_required":
        raise CandidateError("workflow brief external actions must require host approval")
    return value


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _control_root(workspace: Path) -> Path:
    workspace = Path(workspace).resolve(strict=True)
    identity = hashlib.sha256(os.fspath(workspace).encode()).hexdigest()
    return workspace.parent / ".agent-workflow-canary-control" / identity


def _ensure_host_key(workspace: Path) -> tuple[bytes, str]:
    control = _control_root(workspace)
    key_path = control / "host-evidence.key"
    if key_path.exists() or key_path.is_symlink():
        if key_path.is_symlink() or not key_path.is_file() or key_path.stat().st_mode & 0o077:
            raise CandidateError("host evidence key is missing or unsafe")
        key = key_path.read_bytes()
    else:
        key = secrets.token_bytes(32)
        key_path = create_once_bytes(control, "host-evidence.key", key)
    if len(key) != 32:
        raise CandidateError("host evidence key size drifted")
    return key, _sha256(key)


def _load_host_key(workspace: Path, authority_id: str) -> bytes:
    key, observed_id = _ensure_host_key(workspace)
    if observed_id != authority_id:
        raise CandidateError("host evidence authority identity drifted")
    return key


def _frozen_repository_root(workspace: Path) -> Path:
    return _control_root(workspace) / "frozen-repository"


def _freeze_items(freeze: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["path"]: item for item in [*freeze["files"], *freeze["authority"]]}


def _verify_frozen_repository(workspace: Path, freeze: dict[str, Any]) -> Path:
    root = _frozen_repository_root(workspace)
    expected = _freeze_items(freeze)
    if root.is_symlink() or not root.is_dir():
        raise CandidateError("immutable qualification bundle is missing or unsafe")
    observed = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    if observed != set(expected):
        raise CandidateError("immutable qualification bundle file set drifted")
    for relative, item in expected.items():
        path = root.joinpath(*PurePosixPath(relative).parts)
        if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o400:
            raise CandidateError(f"immutable qualification bundle member is unsafe: {relative}")
        payload = path.read_bytes()
        if len(payload) != item["bytes"] or _sha256(payload) != item["sha256"]:
            raise CandidateError(f"immutable qualification bundle member drifted: {relative}")
    return root


def _materialize_frozen_repository(workspace: Path, repo: Path, freeze: dict[str, Any]) -> Path:
    control = _control_root(workspace)
    control.mkdir(parents=True, exist_ok=True)
    control.chmod(0o700)
    root = _frozen_repository_root(workspace)
    for relative, item in _freeze_items(freeze).items():
        source = repo.joinpath(*PurePosixPath(relative).parts)
        payload = source.read_bytes()
        if len(payload) != item["bytes"] or _sha256(payload) != item["sha256"]:
            raise CandidateError(f"source drifted while materializing qualification bundle: {relative}")
        target = root.joinpath(*PurePosixPath(relative).parts)
        if target.exists() or target.is_symlink():
            if target.is_symlink() or not target.is_file() or target.read_bytes() != payload:
                raise CandidateError(f"existing immutable qualification bundle drifted: {relative}")
        else:
            create_once_bytes(control, f"frozen-repository/{relative}", payload)
        target.chmod(0o400)
    for directory in sorted(
        (path for path in root.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        directory.chmod(0o500)
    root.chmod(0o500)
    return _verify_frozen_repository(workspace, freeze)


def _host_mac(key: bytes, payload: dict[str, Any]) -> str:
    return "hmac-sha256:" + hmac.new(key, _canonical_json(payload), hashlib.sha256).hexdigest()


def verify_executable_freeze(repo: Path, freeze_path: Path) -> dict[str, Any]:
    repo = Path(repo).resolve(strict=True)
    freeze_path = Path(freeze_path)
    if freeze_path.is_symlink() or not freeze_path.is_file():
        raise CandidateError("executable freeze must be a regular file")
    try:
        freeze = json.loads(freeze_path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateError("executable freeze is invalid JSON") from exc
    if (
        not isinstance(freeze, dict)
        or set(freeze) != {"schema_version", "runtime_bundle_sha256", "semantic_bundle_sha256", "files", "authority"}
        or freeze.get("schema_version") != "agent-workflow.promotion-freeze.v1"
        or not isinstance(freeze.get("files"), list)
        or not isinstance(freeze.get("authority"), list)
    ):
        raise CandidateError("executable freeze is malformed")
    frozen_paths: set[str] = set()
    frozen_digests: dict[str, str] = {}
    for index, item in enumerate(freeze["files"]):
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "bytes"}:
            raise CandidateError(f"executable freeze file[{index}] is malformed")
        relative = _safe_relative_path(item["path"], f"executable freeze file[{index}].path")
        relative_name = relative.as_posix()
        if relative_name in frozen_paths:
            raise CandidateError("executable freeze contains duplicate paths")
        frozen_paths.add(relative_name)
        path = repo.joinpath(*relative.parts)
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(repo):
            raise CandidateError(f"frozen executable is missing or unsafe: {relative.as_posix()}")
        payload = path.read_bytes()
        if len(payload) != item["bytes"] or _sha256(payload) != item["sha256"]:
            raise CandidateError(f"frozen executable drifted: {relative.as_posix()}")
        frozen_digests[relative_name] = item["sha256"]
    missing = REQUIRED_PROMOTION_EXECUTABLES - frozen_paths
    if missing:
        raise CandidateError(f"executable freeze omitted required files: {', '.join(sorted(missing))}")
    unexpected = frozen_paths - REQUIRED_PROMOTION_EXECUTABLES
    if unexpected:
        raise CandidateError(f"executable freeze contains undeclared files: {', '.join(sorted(unexpected))}")
    bundle = freeze.get("runtime_bundle_sha256")
    manifest = [
        {"path": name, "sha256": frozen_digests[f"skills/agent-workflow/scripts/{name}"]}
        for name in CORE_RUNTIME_FILES
    ]
    expected_bundle = _sha256(_canonical_json(manifest) + b"\n")
    if bundle != expected_bundle:
        raise CandidateError("executable freeze runtime bundle digest does not match the complete core manifest")
    authority_paths: set[str] = set()
    authority_manifest: list[dict[str, str]] = []
    for index, item in enumerate(freeze["authority"]):
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "bytes"}:
            raise CandidateError(f"promotion authority file[{index}] is malformed")
        relative = _safe_relative_path(item["path"], f"promotion authority file[{index}].path")
        relative_name = relative.as_posix()
        if relative_name in authority_paths:
            raise CandidateError("promotion authority contains duplicate paths")
        authority_paths.add(relative_name)
        path = repo.joinpath(*relative.parts)
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(repo):
            raise CandidateError(f"promotion authority is missing or unsafe: {relative_name}")
        payload = path.read_bytes()
        if len(payload) != item["bytes"] or _sha256(payload) != item["sha256"]:
            raise CandidateError(f"promotion authority drifted: {relative_name}")
        authority_manifest.append({"path": relative_name, "sha256": item["sha256"]})
    if authority_paths != REQUIRED_PROMOTION_AUTHORITY:
        raise CandidateError("promotion freeze authority file set drifted")
    semantic_manifest = {
        "executables": [
            {"path": path, "sha256": frozen_digests[path]}
            for path in sorted(REQUIRED_PROMOTION_EXECUTABLES)
        ],
        "authority": sorted(authority_manifest, key=lambda item: item["path"]),
    }
    if freeze["semantic_bundle_sha256"] != _sha256(_canonical_json(semantic_manifest) + b"\n"):
        raise CandidateError("promotion semantic bundle digest drifted")
    return freeze


def build_promotion_freeze(repo: Path, output_path: Path) -> dict[str, Any]:
    """Create the exact executable + semantic promotion freeze once."""

    repo = Path(repo).resolve(strict=True)
    output = Path(output_path)
    if not output.is_absolute():
        output = repo / output
    output = Path(os.path.abspath(output))
    try:
        relative = output.relative_to(repo).as_posix()
    except ValueError as exc:
        raise CandidateError("promotion freeze output must be inside the repository") from exc
    files: list[dict[str, Any]] = []
    executable_digests: dict[str, str] = {}
    for name in sorted(REQUIRED_PROMOTION_EXECUTABLES):
        path = repo / name
        if path.is_symlink() or not path.is_file():
            raise CandidateError(f"promotion executable is missing or unsafe: {name}")
        payload = path.read_bytes()
        digest = _sha256(payload)
        executable_digests[name] = digest
        files.append({"path": name, "sha256": digest, "bytes": len(payload)})
    authority: list[dict[str, Any]] = []
    authority_manifest: list[dict[str, str]] = []
    for name in sorted(REQUIRED_PROMOTION_AUTHORITY):
        path = repo / name
        if path.is_symlink() or not path.is_file():
            raise CandidateError(f"promotion authority is missing or unsafe: {name}")
        payload = path.read_bytes()
        digest = _sha256(payload)
        authority.append({"path": name, "sha256": digest, "bytes": len(payload)})
        authority_manifest.append({"path": name, "sha256": digest})
    runtime_manifest = [
        {"path": name, "sha256": executable_digests[f"skills/agent-workflow/scripts/{name}"]}
        for name in CORE_RUNTIME_FILES
    ]
    semantic_manifest = {
        "executables": [
            {"path": name, "sha256": executable_digests[name]}
            for name in sorted(REQUIRED_PROMOTION_EXECUTABLES)
        ],
        "authority": authority_manifest,
    }
    freeze = {
        "schema_version": "agent-workflow.promotion-freeze.v1",
        "runtime_bundle_sha256": _sha256(_canonical_json(runtime_manifest) + b"\n"),
        "semantic_bundle_sha256": _sha256(_canonical_json(semantic_manifest) + b"\n"),
        "files": files,
        "authority": authority,
    }
    path = repo / relative
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file() or json.loads(path.read_bytes()) != freeze:
            raise CandidateError("existing promotion freeze drifted")
    else:
        path = create_once_json(repo, relative, freeze)
    verify_executable_freeze(repo, path)
    return {"freeze_ref": relative, "freeze_sha256": _sha256(path.read_bytes()), **freeze}


def _run_seal_source(value: Any) -> dict[str, Any]:
    required = {
        "schema_version", "fixture_seal_sha256", "executable_freeze_sha256",
        "candidate_bundle_sha256", "runtime_bundle_sha256",
        "repository_fixture_ref", "repository_fixture_sha256",
        "codex_identity_ref", "codex_identity_sha256", "codex_version", "app_protocol_schema_sha256",
        "host_profile_ref", "host_profile_sha256",
        "top_model", "worker_model", "reasoning_effort", "capacity_profile_sha256",
        "capacity_profile_ref",
        "paired_order_seed", "blind_label_map_ref", "blind_label_map_sha256", "rubric_ref", "rubric_sha256",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise CandidateError("canary run seal source schema drift")
    if value["schema_version"] != RUN_SEAL_SCHEMA:
        raise CandidateError("canary run seal source schema drift")
    digest_fields = {
        "fixture_seal_sha256", "executable_freeze_sha256", "candidate_bundle_sha256", "runtime_bundle_sha256",
        "repository_fixture_sha256",
        "codex_identity_sha256", "app_protocol_schema_sha256", "host_profile_sha256", "capacity_profile_sha256",
        "blind_label_map_sha256", "rubric_sha256",
    }
    for field in digest_fields:
        raw = value[field]
        if not isinstance(raw, str) or len(raw) != 71 or not raw.startswith("sha256:"):
            raise CandidateError(f"canary run seal {field} is invalid")
        try:
            int(raw[7:], 16)
        except ValueError as exc:
            raise CandidateError(f"canary run seal {field} is invalid") from exc
    for field in ("codex_version", "top_model", "worker_model", "reasoning_effort", "paired_order_seed"):
        _text(value[field], f"canary run seal {field}")
    _safe_relative_path(value["blind_label_map_ref"], "canary run seal blind_label_map_ref")
    _safe_relative_path(value["rubric_ref"], "canary run seal rubric_ref")
    for field in ("repository_fixture_ref", "codex_identity_ref", "host_profile_ref", "capacity_profile_ref"):
        _safe_relative_path(value[field], f"canary run seal {field}")
    return value


def _sealed_run(value: Any) -> dict[str, Any]:
    generated = {"sealed_at", "host_authority_id", "results_ref", "freeze_path", "repository_root"}
    if not isinstance(value, dict) or not generated <= set(value):
        raise CandidateError("canary run seal lacks deterministic seal time")
    source = dict(value)
    sealed_at = source.pop("sealed_at")
    host_authority_id = source.pop("host_authority_id")
    results_ref = source.pop("results_ref")
    freeze_path = source.pop("freeze_path")
    repository_root = source.pop("repository_root")
    _run_seal_source(source)
    _parse_timestamp(sealed_at, "canary run seal sealed_at")
    if not isinstance(host_authority_id, str) or not host_authority_id.startswith("sha256:"):
        raise CandidateError("canary run seal host authority identity drifted")
    _safe_relative_path(results_ref, "canary run seal results_ref")
    for path_value, label in ((freeze_path, "freeze_path"), (repository_root, "repository_root")):
        if not isinstance(path_value, str) or not Path(path_value).is_absolute():
            raise CandidateError(f"canary run seal {label} is invalid")
    return value


def _bound_workspace_evidence(root: Path, ref: str, digest: str, label: str) -> bytes:
    relative = _safe_relative_path(ref, label)
    path = root.joinpath(*relative.parts)
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
        raise CandidateError(f"{label} must resolve to a regular workspace file")
    payload = path.read_bytes()
    if not payload or len(payload) > 16 * 1024 * 1024 or _sha256(payload) != digest:
        raise CandidateError(f"{label} digest or size drifted")
    return payload


def _bound_json_object(root: Path, ref: str, digest: str, label: str) -> dict[str, Any]:
    payload = _bound_workspace_evidence(root, ref, digest, label)
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateError(f"{label} must be valid JSON") from exc
    if not isinstance(value, dict):
        raise CandidateError(f"{label} must be a JSON object")
    return value


def _verify_environment_authority(workspace: Path, source: dict[str, Any]) -> None:
    repository = _bound_json_object(
        workspace, source["repository_fixture_ref"], source["repository_fixture_sha256"], "repository fixture",
    )
    codex = _bound_json_object(
        workspace, source["codex_identity_ref"], source["codex_identity_sha256"], "Codex identity",
    )
    host = _bound_json_object(
        workspace, source["host_profile_ref"], source["host_profile_sha256"], "host profile",
    )
    capacity = _bound_json_object(
        workspace, source["capacity_profile_ref"], source["capacity_profile_sha256"], "capacity profile",
    )
    if (
        repository.get("schema_version") != "agent-workflow.canary-repository-fixture.v1"
        or not isinstance(repository.get("snapshot_store_root"), str)
    ):
        raise CandidateError("repository fixture authority schema drifted")
    if (
        set(codex) != {"schema_version", "version", "binary_sha256", "session_store_root"}
        or codex.get("schema_version") != "agent-workflow.canary-codex-identity.v1"
        or codex.get("version") != source["codex_version"]
        or not isinstance(codex.get("binary_sha256"), str)
        or not codex["binary_sha256"].startswith("sha256:")
    ):
        raise CandidateError("Codex identity authority schema or version drifted")
    if source["codex_version"] != SUPPORTED_CODEX_VERSION or source["app_protocol_schema_sha256"] != SUPPORTED_APP_SCHEMA_SHA256:
        raise CandidateError("Codex/App Server accounting version is unsupported")
    if host.get("schema_version") != "agent-workflow.canary-host-profile.v1":
        raise CandidateError("host profile authority schema drifted")
    for value, label in (
        (repository.get("snapshot_store_root"), "repository snapshot store"),
        (codex.get("session_store_root"), "Codex session store"),
        (host.get("app_event_store_root"), "App event store"),
        (host.get("artifact_store_root"), "runtime artifact store"),
    ):
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise CandidateError(f"{label} authority is invalid")
        path = Path(value)
        if path.is_symlink() or not path.is_dir() or path.resolve().is_relative_to(workspace):
            raise CandidateError(f"{label} must be a host directory outside the worker workspace")
        observed = path.stat()
        if observed.st_uid != os.geteuid() or stat.S_IMODE(observed.st_mode) != 0o700:
            raise CandidateError(f"{label} canonical store authority must be host-owned mode 0700")
    if (
        capacity.get("schema_version") != "agent-workflow.canary-capacity-profile.v1"
        or not isinstance(capacity.get("max_parallel"), int)
        or isinstance(capacity.get("max_parallel"), bool)
        or capacity["max_parallel"] < 1
    ):
        raise CandidateError("capacity profile authority schema drifted")


def _canonical_evidence_roots(workspace: Path, run_seal: dict[str, Any]) -> tuple[Path, Path]:
    codex = _bound_json_object(
        workspace, run_seal["codex_identity_ref"], run_seal["codex_identity_sha256"], "Codex identity",
    )
    host = _bound_json_object(
        workspace, run_seal["host_profile_ref"], run_seal["host_profile_sha256"], "host profile",
    )
    return Path(codex["session_store_root"]).resolve(strict=True), Path(host["app_event_store_root"]).resolve(strict=True)


def _canonical_snapshot_root(workspace: Path, run_seal: dict[str, Any]) -> Path:
    repository = _bound_json_object(
        workspace,
        run_seal["repository_fixture_ref"],
        run_seal["repository_fixture_sha256"],
        "repository fixture",
    )
    return Path(repository["snapshot_store_root"]).resolve(strict=True)


def _canonical_artifact_root(workspace: Path, run_seal: dict[str, Any]) -> Path:
    host = _bound_json_object(
        workspace, run_seal["host_profile_ref"], run_seal["host_profile_sha256"], "host profile",
    )
    return Path(host["artifact_store_root"]).resolve(strict=True)


def _assert_session_denies_canonical_stores(
    context: dict[str, Any], workspace: Path, run_seal: dict[str, Any], label: str,
) -> None:
    session_root, app_root = _canonical_evidence_roots(workspace, run_seal)
    protected = (
        session_root,
        app_root,
        _canonical_snapshot_root(workspace, run_seal),
        _canonical_artifact_root(workspace, run_seal),
    )
    profile = context.get("permission_profile")
    filesystem = profile.get("file_system") if isinstance(profile, dict) else None
    entries = filesystem.get("entries") if isinstance(filesystem, dict) else None
    workspace_roots = context.get("workspace_roots")
    if (
        not isinstance(profile, dict)
        or profile.get("type") != "managed"
        or not isinstance(filesystem, dict)
        or filesystem.get("type") != "restricted"
        or not isinstance(entries, list)
        or not isinstance(workspace_roots, list)
        or not workspace_roots
    ):
        raise CandidateError(f"{label} lacks a restricted effective permission profile")
    concrete: list[Path] = []
    for value in workspace_roots:
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise CandidateError(f"{label} workspace authority is invalid")
        concrete.append(Path(value).resolve())
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or entry.get("access") not in {"read", "write"}
            or not isinstance(entry.get("path"), dict)
        ):
            raise CandidateError(f"{label} permission profile entry is invalid")
        path = entry["path"]
        if path.get("type") == "special":
            if path.get("value") != {"kind": "minimal"} or entry["access"] != "read":
                raise CandidateError(f"{label} permission profile special root is unsafe")
            continue
        value = path.get("path") if path.get("type") == "path" else None
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise CandidateError(f"{label} permission profile path is invalid")
        concrete.append(Path(value).resolve())
    if any(
        candidate == root or candidate.is_relative_to(root) or root.is_relative_to(candidate)
        for candidate in concrete
        for root in protected
    ):
        raise CandidateError(f"{label} can access a canonical host store")


def _verify_canonical_copy(path_value: Any, digest: str, root: Path, workspace_payload: bytes, label: str) -> None:
    if not isinstance(path_value, str) or not Path(path_value).is_absolute():
        raise CandidateError(f"{label} canonical path is invalid")
    path = Path(path_value)
    if path.is_symlink() or not path.is_file():
        raise CandidateError(f"{label} canonical source is missing or unsafe")
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root) or resolved.read_bytes() != workspace_payload or _sha256(workspace_payload) != digest:
        raise CandidateError(f"{label} workspace copy does not match the host canonical store")


def _verified_fixture_seal() -> bytes:
    seal_path = CANARY_FIXTURES / "seal.v1.json"
    payload = seal_path.read_bytes()
    try:
        seal = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateError("canary fixture seal is invalid JSON") from exc
    if (
        not isinstance(seal, dict)
        or set(seal) != {
            "schema_version", "corpus_ref", "corpus_sha256", "hidden_checks_ref",
            "hidden_checks_sha256", "paired_trials_per_workload", "immutability",
        }
        or seal["schema_version"] != "agent-workflow.canary-seal.v1"
        or seal["paired_trials_per_workload"] != 5
    ):
        raise CandidateError("canary fixture seal schema drift")
    for ref_field, sha_field in (("corpus_ref", "corpus_sha256"), ("hidden_checks_ref", "hidden_checks_sha256")):
        relative = _safe_relative_path(seal[ref_field], f"canary fixture {ref_field}")
        path = CANARY_FIXTURES.joinpath(*relative.parts)
        if path.is_symlink() or not path.is_file() or _sha256(path.read_bytes()) != seal[sha_field]:
            raise CandidateError(f"canary fixture {ref_field} digest drifted")
    return payload


def _validate_p2_detail(
    workspace: Path,
    finding: dict[str, Any],
    run_seal: dict[str, Any],
    verifier_session_id: str,
    qualification_records: dict[str, str] | None = None,
) -> None:
    detail = _bound_json_object(
        workspace, finding["detail_ref"], finding["detail_sha256"], "P2 resolution detail",
    )
    common = {"schema_version", "finding_id", "resolution"}
    if (
        detail.get("schema_version") != "agent-workflow.canary-p2-resolution.v1"
        or detail.get("finding_id") != finding["id"]
        or detail.get("resolution") != finding["resolution"]
    ):
        raise CandidateError("P2 resolution detail identity drifted")
    resolution = finding["resolution"]
    if resolution == "fixed":
        if set(detail) != common | {"repair_refs", "reverify_refs"}:
            raise CandidateError("fixed P2 requires repair and reverify evidence")
        groups = (detail["repair_refs"], detail["reverify_refs"])
        if any(not isinstance(group, list) or not group for group in groups):
            raise CandidateError("fixed P2 requires non-empty repair and reverify evidence")
        refs = [(item, "repair", "applied") for item in detail["repair_refs"]]
        refs.extend((item, "reverify", "passed") for item in detail["reverify_refs"])
    elif resolution == "accepted_with_rationale":
        if (
            set(detail) != common | {"rationale", "authority_ref", "authority_sha256", "authority_canonical_path", "authority_canonical_sha256"}
            or not isinstance(detail.get("rationale"), str)
            or not detail["rationale"].strip()
        ):
            raise CandidateError("accepted P2 requires rationale and authority evidence")
        refs = [({"ref": detail["authority_ref"], "sha256": detail["authority_sha256"], "canonical_path": detail["authority_canonical_path"], "canonical_sha256": detail["authority_canonical_sha256"]}, "acceptance_authority", "accepted")]
    elif resolution == "deferred_with_owner_gate":
        if (
            set(detail) != common | {"owner", "promotion_gate", "gate_status", "gate_evidence_ref", "gate_evidence_sha256", "gate_evidence_canonical_path", "gate_evidence_canonical_sha256"}
            or detail.get("owner") != finding["owner"]
            or detail.get("promotion_gate") != finding["promotion_gate"]
            or detail.get("gate_status") != finding["gate_status"]
        ):
            raise CandidateError("deferred P2 requires matching owner and gate evidence")
        refs = [({"ref": detail["gate_evidence_ref"], "sha256": detail["gate_evidence_sha256"], "canonical_path": detail["gate_evidence_canonical_path"], "canonical_sha256": detail["gate_evidence_canonical_sha256"]}, "promotion_gate", "passed")]
    else:
        if (
            set(detail) != common | {"external_dependency", "owner", "promotion_gate", "gate_status"}
            or not isinstance(detail.get("external_dependency"), str)
            or not detail["external_dependency"].strip()
            or detail.get("owner") != finding["owner"]
            or detail.get("promotion_gate") != finding["promotion_gate"]
            or detail.get("gate_status") != finding["gate_status"]
        ):
            raise CandidateError("blocked P2 requires exact external dependency authority")
        refs = []
    for ref, evidence_kind, expected_status in refs:
        if not isinstance(ref, dict) or set(ref) != {"ref", "sha256", "canonical_path", "canonical_sha256"}:
            raise CandidateError("P2 resolution evidence ref is malformed")
        payload = _bound_workspace_evidence(workspace, ref["ref"], ref["sha256"], "P2 resolution evidence")
        _verify_canonical_copy(
            ref["canonical_path"], ref["canonical_sha256"], _canonical_artifact_root(workspace, run_seal), payload, "P2 resolution evidence",
        )
        try:
            evidence = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CandidateError("P2 resolution evidence must be valid JSON") from exc
        expected = {
            "schema_version": "agent-workflow.canary-p2-evidence.v1",
            "finding_id": finding["id"],
            "evidence_kind": evidence_kind,
            "status": expected_status,
        }
        if any(evidence.get(key) != value for key, value in expected.items()):
            raise CandidateError("P2 resolution evidence semantics drifted")
        if evidence_kind == "promotion_gate" and (
            evidence.get("owner") != finding["owner"]
            or evidence.get("promotion_gate") != finding["promotion_gate"]
        ):
            raise CandidateError("P2 promotion gate evidence authority drifted")
        if evidence_kind == "repair":
            required = {"repair_artifact_ref", "repair_artifact_sha256", "repair_artifact_canonical_path", "repair_artifact_canonical_sha256"}
            if not required <= set(evidence):
                raise CandidateError("P2 repair evidence lacks the repaired artifact")
            repair_payload = _bound_workspace_evidence(
                workspace, evidence["repair_artifact_ref"], evidence["repair_artifact_sha256"], "P2 repaired artifact",
            )
            _verify_canonical_copy(
                evidence["repair_artifact_canonical_path"],
                evidence["repair_artifact_canonical_sha256"],
                _canonical_artifact_root(workspace, run_seal),
                repair_payload,
                "P2 repaired artifact",
            )
        if evidence_kind in {"reverify", "promotion_gate", "acceptance_authority"} and (
            evidence.get("verifier_session_id") != verifier_session_id
        ):
            raise CandidateError("P2 resolution evidence is not bound to the independent verifier")
        if evidence_kind in {"reverify", "promotion_gate"}:
            command_id = evidence.get("validator_command_id")
            record_sha256 = evidence.get("qualification_record_sha256")
            if (
                command_id not in HOST_QUALIFICATION_COMMANDS
                or not isinstance(record_sha256, str)
                or not record_sha256.startswith("sha256:")
            ):
                raise CandidateError("P2 resolution evidence lacks an exact frozen qualification record")
            if qualification_records is not None and qualification_records.get(command_id) != record_sha256:
                raise CandidateError("P2 resolution evidence qualification record drifted")


def _verify_blind_reviewer_session(
    workspace: Path, review: dict[str, Any], run_seal: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    route = review.get("reviewer_route")
    if (
        not isinstance(route, dict)
        or set(route) != {"model", "reasoning_effort", "session_id"}
        or route.get("model") != run_seal["top_model"]
        or route.get("reasoning_effort") != run_seal["reasoning_effort"]
    ):
        raise CandidateError("blind reviewer route is not qualified or identity-bound")
    raw = _bound_workspace_evidence(
        workspace,
        review["raw_session_ref"],
        review["raw_session_sha256"],
        "blind reviewer raw session",
    )
    session_store, _ = _canonical_evidence_roots(workspace, run_seal)
    _verify_canonical_copy(
        review["canonical_raw_path"],
        review["canonical_raw_sha256"],
        session_store,
        raw,
        "reviewer raw session",
    )
    launch_packet = _bound_json_object(
        workspace,
        review["launch_packet_ref"],
        review["launch_packet_sha256"],
        "reviewer launch packet",
    )
    session_ids: list[str] = []
    starts: list[datetime] = []
    turns: list[dict[str, Any]] = []
    terminal_turns: list[str] = []
    terminal_messages: list[str] = []
    terminal_times: list[datetime] = []
    timestamped_events: list[datetime] = []
    user_message_contents: list[Any] = []
    try:
        for line in raw.splitlines():
            item = json.loads(line)
            payload = item.get("payload") if isinstance(item, dict) else None
            if not isinstance(payload, dict):
                continue
            if isinstance(item.get("timestamp"), str):
                timestamped_events.append(_parse_timestamp(item["timestamp"], "reviewer event timestamp"))
            if item.get("type") == "session_meta" and isinstance(payload.get("id"), str):
                session_ids.append(payload["id"])
                starts.append(_parse_timestamp(payload.get("timestamp"), "reviewer session start"))
            elif item.get("type") == "turn_context":
                turns.append(payload)
            elif item.get("type") == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
                user_message_contents.append(payload.get("content"))
            elif item.get("type") == "event_msg" and payload.get("type") == "task_complete" and isinstance(payload.get("turn_id"), str):
                terminal_turns.append(payload["turn_id"])
                terminal_times.append(_parse_timestamp(item.get("timestamp"), "reviewer terminal timestamp"))
                if isinstance(payload.get("last_agent_message"), str):
                    terminal_messages.append(payload["last_agent_message"])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateError("blind reviewer raw session is invalid JSONL") from exc
    if (
        session_ids != [route["session_id"]]
        or len(starts) != 1
        or starts[0] < _parse_timestamp(run_seal["sealed_at"], "canary run seal sealed_at")
        or len(terminal_times) != 1
        or terminal_times[0] < starts[0]
        or any(right < left for left, right in zip(timestamped_events, timestamped_events[1:]))
        or _explicit_user_prompt(
            user_message_contents, "blind reviewer", review["host_preamble_sha256"],
        ) != _canonical_json(launch_packet).decode()
        or not turns
        or turns[-1].get("model") != route["model"]
        or turns[-1].get("effort") != route["reasoning_effort"]
        or terminal_turns != [turns[-1].get("turn_id")]
        or terminal_messages != [_canonical_json(review["decision"]).decode()]
    ):
        raise CandidateError("blind reviewer raw session route or terminal boundary drifted")
    _assert_session_denies_canonical_stores(turns[-1], workspace, run_seal, "blind reviewer session")
    return session_ids[0], launch_packet


_VOLATILE_ARG0_RE = re.compile(r"(?<=/tmp/arg0/)codex-arg0[A-Za-z0-9]+")
_VOLATILE_ARG0_SENTINEL = "codex-arg0&lt;volatile&gt;"


def _canonical_host_preamble(preamble: Any, label: str) -> bytes:
    """Validate Codex-owned envelopes and normalize only the per-turn arg0 basename."""
    if (
        not isinstance(preamble, list)
        or len(preamble) not in {1, 2}
        or any(
            not isinstance(part, dict)
            or set(part) != {"type", "text"}
            or part.get("type") != "input_text"
            or not isinstance(part.get("text"), str)
            for part in preamble
        )
    ):
        raise CandidateError(f"{label} host context preamble drifted")
    environment_index = 0
    if len(preamble) == 2:
        if (
            not preamble[0]["text"].startswith("# AGENTS.md instructions for ")
            or "\n\n<INSTRUCTIONS>\n" not in preamble[0]["text"]
            or not preamble[0]["text"].endswith("\n</INSTRUCTIONS>")
        ):
            raise CandidateError(f"{label} host context preamble drifted")
        environment_index = 1
    environment = preamble[environment_index]["text"]
    if (
        not environment.startswith("<environment_context>\n")
        or not environment.endswith("\n</environment_context>")
    ):
        raise CandidateError(f"{label} host context preamble drifted")
    volatile_arg0 = _VOLATILE_ARG0_RE.findall(environment)
    if len(volatile_arg0) > 1:
        raise CandidateError(f"{label} volatile arg0 authority drifted")
    normalized = [dict(part) for part in preamble]
    if volatile_arg0:
        normalized[environment_index]["text"] = _VOLATILE_ARG0_RE.sub(
            _VOLATILE_ARG0_SENTINEL, environment,
        )
    return _canonical_json(normalized) + b"\n"


def _explicit_user_prompt(
    contents: list[Any], label: str, expected_preamble_sha256: str | None = None,
) -> str:
    """Accept a typed Codex host preamble, then one exact explicit launch prompt."""
    if not isinstance(contents, list) or len(contents) not in {1, 2}:
        raise CandidateError(f"{label} raw user message schema drifted")
    preamble_payload = b""
    if len(contents) == 2:
        preamble_payload = _canonical_host_preamble(contents[0], label)
    if expected_preamble_sha256 is not None and _sha256(preamble_payload) != expected_preamble_sha256:
        raise CandidateError(f"{label} host context preamble is not launch-bound")
    explicit = contents[-1]
    if (
        not isinstance(explicit, list)
        or len(explicit) != 1
        or not isinstance(explicit[0], dict)
        or set(explicit[0]) != {"type", "text"}
        or explicit[0].get("type") != "input_text"
        or not isinstance(explicit[0].get("text"), str)
    ):
        raise CandidateError(f"{label} raw user message schema drifted")
    return explicit[0]["text"]


def _inline_json_evidence(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise CandidateError(f"{label} must contain readable evidence")
    digests: list[str] = []
    for item in value:
        if (
            not isinstance(item, dict)
            or set(item) != {"sha256", "content"}
            or not isinstance(item["content"], dict)
            or item["sha256"] != _sha256(_canonical_json(item["content"]) + b"\n")
        ):
            raise CandidateError(f"{label} inline evidence drifted")
        digests.append(item["sha256"])
    if digests != sorted(digests) or len(digests) != len(set(digests)):
        raise CandidateError(f"{label} inline evidence must be sorted and unique")
    return digests


def build_hidden_evidence_manifest(
    run_seal_sha256: str,
    evidence_items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the bounded index used by the independent canary verifier."""

    if not isinstance(run_seal_sha256, str) or not run_seal_sha256.startswith("sha256:"):
        raise CandidateError("hidden evidence manifest lacks run-seal authority")
    if not isinstance(evidence_items, list) or not evidence_items:
        raise CandidateError("hidden evidence manifest must contain readable evidence")
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in evidence_items:
        if (
            not isinstance(item, dict)
            or set(item) != {"sha256", "content_ref", "content"}
            or not isinstance(item["content"], dict)
            or item["sha256"] != _sha256(_canonical_json(item["content"]) + b"\n")
            or item["sha256"] in seen
        ):
            raise CandidateError("hidden evidence manifest content drifted")
        proof = item["content"]
        qualification = proof.get("qualification_record")
        validator_result = qualification.get("validator_result") if isinstance(qualification, dict) else None
        if (
            proof.get("schema_version") != "agent-workflow.canary-hidden-proof.v1"
            or proof.get("run_seal_sha256") != run_seal_sha256
            or not isinstance(proof.get("check_id"), str)
            or not isinstance(proof.get("workload_id"), str)
            or not isinstance(proof.get("trial"), int)
            or proof.get("variant") not in {"legacy", "vnext"}
            or not isinstance(validator_result, dict)
            or validator_result.get("status") not in {"pass", "fail"}
        ):
            raise CandidateError("hidden evidence manifest proof schema drifted")
        authority_ref = _safe_relative_path(
            item["content_ref"], "hidden evidence authority ref",
        ).as_posix()
        reader_ref = (
            "review-workspaces/independent-verifier/evidence/"
            f"{item['sha256'].removeprefix('sha256:')}.json"
        )
        entries.append({
            "authority_ref": authority_ref,
            "check_id": proof["check_id"],
            "content_sha256": item["sha256"],
            "reader_ref": reader_ref,
            "status": validator_result["status"],
            "trial": proof["trial"],
            "variant": proof["variant"],
            "workload_id": proof["workload_id"],
        })
        seen.add(item["sha256"])
    entries.sort(key=lambda entry: entry["content_sha256"])
    return {
        "schema_version": "agent-workflow.canary-hidden-evidence-manifest.v1",
        "run_seal_sha256": run_seal_sha256,
        "entries": entries,
    }


def hidden_evidence_verifier_index(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Project a bounded model-readable index while the manifest retains authority refs."""

    entries = manifest.get("entries") if isinstance(manifest, dict) else None
    if not isinstance(entries, list):
        raise CandidateError("hidden evidence manifest index is invalid")
    return [
        {
            key: entry[key]
            for key in (
                "check_id", "content_sha256", "reader_ref", "status", "trial", "variant", "workload_id",
            )
        }
        for entry in entries
    ]


def _parse_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise CandidateError(f"{label} is missing")
    try:
        observed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CandidateError(f"{label} is invalid") from exc
    if observed.utcoffset() is None:
        raise CandidateError(f"{label} must include a UTC offset")
    return observed


def _replay_variant_receipt(
    workspace: Path,
    *,
    ref: str,
    digest: str,
    pair_id: str,
    variant: str,
    run_seal: dict[str, Any],
    run_seal_sha256: str,
) -> dict[str, Any]:
    payload = _bound_workspace_evidence(workspace, ref, digest, "variant receipt")
    try:
        receipt = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateError("variant receipt is invalid JSON") from exc
    if (
        not isinstance(receipt, dict)
        or set(receipt) != {
            "schema_version", "pair_id", "variant", "run_seal_sha256", "sessions",
            "repository_snapshot_ref", "repository_snapshot_sha256", "host_profile_sha256",
            "canonical_snapshot_path", "canonical_snapshot_sha256",
            "capacity_profile_sha256", "workspace_instance_id",
            "launch_manifest_ref", "launch_manifest_sha256",
            "canonical_launch_manifest_path", "canonical_launch_manifest_sha256",
            "contract_evidence_ref", "contract_evidence_sha256",
            "canonical_contract_evidence_path", "canonical_contract_evidence_sha256",
        }
        or receipt["schema_version"] != "agent-workflow.canary-variant-receipt.v1"
        or receipt["pair_id"] != pair_id
        or receipt["variant"] != variant
        or receipt["run_seal_sha256"] != run_seal_sha256
        or receipt["host_profile_sha256"] != run_seal["host_profile_sha256"]
        or receipt["capacity_profile_sha256"] != run_seal["capacity_profile_sha256"]
        or not isinstance(receipt["workspace_instance_id"], str)
        or not receipt["workspace_instance_id"].strip()
        or not isinstance(receipt["sessions"], list)
        or not receipt["sessions"]
    ):
        raise CandidateError("variant receipt identity or schema drift")
    snapshot = _bound_json_object(
        workspace,
        receipt["repository_snapshot_ref"],
        receipt["repository_snapshot_sha256"],
        "paired repository snapshot",
    )
    inspected_digests: set[str] = {digest, receipt["repository_snapshot_sha256"]}
    contract_evidence = _bound_json_object(
        workspace,
        receipt["contract_evidence_ref"],
        receipt["contract_evidence_sha256"],
        "variant contract evidence",
    )
    contract_payload = _canonical_json(contract_evidence) + b"\n"
    _verify_canonical_copy(
        receipt["canonical_contract_evidence_path"],
        receipt["canonical_contract_evidence_sha256"],
        _canonical_artifact_root(workspace, run_seal),
        contract_payload,
        "variant contract evidence",
    )
    inspected_digests.add(receipt["contract_evidence_sha256"])
    if (
        set(snapshot) != {
            "schema_version", "pair_id", "head", "staged_diff_sha256",
            "unstaged_diff_sha256", "untracked_manifest_sha256",
        }
        or snapshot.get("schema_version") != "agent-workflow.canary-paired-snapshot.v1"
        or snapshot.get("pair_id") != pair_id
        or not isinstance(snapshot.get("head"), str)
        or not snapshot["head"].strip()
        or any(
            not isinstance(snapshot.get(field), str) or not snapshot[field].startswith("sha256:")
            for field in ("staged_diff_sha256", "unstaged_diff_sha256", "untracked_manifest_sha256")
        )
    ):
        raise CandidateError("paired repository snapshot authority drifted")
    snapshot_payload = _canonical_json(snapshot) + b"\n"
    _verify_canonical_copy(
        receipt["canonical_snapshot_path"],
        receipt["canonical_snapshot_sha256"],
        _canonical_snapshot_root(workspace, run_seal),
        snapshot_payload,
        "paired repository snapshot",
    )
    launch_manifest = _bound_json_object(
        workspace,
        receipt["launch_manifest_ref"],
        receipt["launch_manifest_sha256"],
        "host launch manifest",
    )
    inspected_digests.add(receipt["launch_manifest_sha256"])
    launch_manifest_payload = _canonical_json(launch_manifest) + b"\n"
    _verify_canonical_copy(
        receipt["canonical_launch_manifest_path"],
        receipt["canonical_launch_manifest_sha256"],
        _canonical_artifact_root(workspace, run_seal),
        launch_manifest_payload,
        "host launch manifest",
    )
    manifest_launches = launch_manifest.get("launches") if isinstance(launch_manifest, dict) else None
    if (
        not isinstance(launch_manifest, dict)
        or set(launch_manifest) != {
            "schema_version", "run_seal_sha256", "pair_id", "variant", "workspace_instance_id",
            "host_authority_id", "runtime_bundle_sha256", "launches",
        }
        or launch_manifest.get("schema_version") != "agent-workflow.canary-host-launch-manifest.v1"
        or launch_manifest.get("run_seal_sha256") != run_seal_sha256
        or launch_manifest.get("pair_id") != pair_id
        or launch_manifest.get("variant") != variant
        or launch_manifest.get("workspace_instance_id") != receipt["workspace_instance_id"]
        or launch_manifest.get("host_authority_id") != run_seal["host_authority_id"]
        or launch_manifest.get("runtime_bundle_sha256") != run_seal["runtime_bundle_sha256"]
        or not isinstance(manifest_launches, list)
        or not manifest_launches
    ):
        raise CandidateError("host launch manifest authority drifted")
    session_ids: set[str] = set()
    total_tokens = 0
    coordinator_completions = 0
    starts: list[datetime] = []
    terminals: list[datetime] = []
    coordinator_count = 0
    worker_count = 0
    verifier_count = 0
    qualified_verifier_count = 0
    least_privilege_count = 0
    coordinator_output_sha256: str | None = None
    coordinator_density: dict[str, Any] | None = None
    task_ids: set[str] = set()
    replayed_launches: list[dict[str, Any]] = []
    sealed_at = _parse_timestamp(run_seal["sealed_at"], "canary run seal sealed_at")
    for source in receipt["sessions"]:
        if (
            not isinstance(source, dict)
            or set(source) != {
                "role", "task_id", "launch_ref", "launch_sha256", "canonical_launch_path",
                "canonical_launch_sha256", "ref", "sha256", "event_ref", "event_sha256",
                "export_ref", "export_sha256", "continuations",
            }
            or source["role"] not in {"coordinator", "worker", "verifier"}
            or not isinstance(source["task_id"], str)
            or not source["task_id"].strip()
            or source["task_id"] in task_ids
            or not isinstance(source["continuations"], list)
        ):
            raise CandidateError("variant session source schema drift")
        task_ids.add(source["task_id"])
        inspected_digests.update({
            source["launch_sha256"], source["sha256"], source["event_sha256"], source["export_sha256"],
        })
        launch = _bound_json_object(
            workspace, source["launch_ref"], source["launch_sha256"], "session launch packet",
        )
        launch_payload = _canonical_json(launch) + b"\n"
        _verify_canonical_copy(
            source["canonical_launch_path"], source["canonical_launch_sha256"],
            _canonical_artifact_root(workspace, run_seal), launch_payload, "session launch packet",
        )
        expected_model = run_seal["top_model"] if source["role"] in {"coordinator", "verifier"} else run_seal["worker_model"]
        if (
            set(launch) != {
                "schema_version", "run_seal_sha256", "pair_id", "variant", "task_id", "role",
                "model", "reasoning_effort", "transport", "prompt", "host_preamble_sha256", "verification_subject",
            }
            or launch.get("schema_version") != "agent-workflow.canary-session-launch.v1"
            or launch.get("run_seal_sha256") != run_seal_sha256
            or launch.get("pair_id") != pair_id
            or launch.get("variant") != variant
            or launch.get("task_id") != source["task_id"]
            or launch.get("role") != source["role"]
            or launch.get("model") != expected_model
            or launch.get("reasoning_effort") != run_seal["reasoning_effort"]
            or not isinstance(launch.get("prompt"), str)
            or not launch["prompt"].strip()
            or not isinstance(launch.get("host_preamble_sha256"), str)
            or not launch["host_preamble_sha256"].startswith("sha256:")
            or (source["role"] != "verifier" and launch.get("verification_subject") is not None)
        ):
            raise CandidateError("session launch packet authority drifted")
        raw = _bound_workspace_evidence(workspace, source["ref"], source["sha256"], "variant raw session")
        export_payload = _bound_workspace_evidence(
            workspace, source["export_ref"], source["export_sha256"], "host session export receipt",
        )
        event_payload = _bound_workspace_evidence(
            workspace, source["event_ref"], source["event_sha256"], "native event export",
        )
        try:
            export = json.loads(export_payload)
            native_events = [json.loads(line) for line in event_payload.splitlines() if line.strip()]
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CandidateError("host session export receipt is invalid JSON") from exc
        if (
            not isinstance(export, dict)
            or set(export) != {
                "schema_version", "run_seal_sha256", "session_id", "source_session_store_ref",
                "source_prefix_sha256", "raw_session_ref", "raw_session_sha256",
                "transport", "event_ref", "event_sha256", "exporter_bundle_sha256", "thread_id", "turn_ids",
                "canonical_raw_path", "canonical_raw_sha256", "canonical_event_path", "canonical_event_sha256",
            }
            or export["schema_version"] != "agent-workflow.canary-host-export.v1"
            or export["run_seal_sha256"] != run_seal_sha256
            or export["source_session_store_ref"] != source["ref"]
            or export["source_prefix_sha256"] != source["sha256"]
            or export["raw_session_ref"] != source["ref"]
            or export["raw_session_sha256"] != source["sha256"]
            or export["transport"] not in {"app_server", "codex_exec_jsonl"}
            or export["transport"] != launch["transport"]
            or export["event_ref"] != source["event_ref"]
            or export["event_sha256"] != source["event_sha256"]
            or export["exporter_bundle_sha256"] != run_seal["runtime_bundle_sha256"]
            or not isinstance(export["turn_ids"], list)
        ):
            raise CandidateError("host session export provenance drifted")
        session_store, app_store = _canonical_evidence_roots(workspace, run_seal)
        _verify_canonical_copy(
            export["canonical_raw_path"], export["canonical_raw_sha256"], session_store, raw, "variant raw session",
        )
        _verify_canonical_copy(
            export["canonical_event_path"], export["canonical_event_sha256"], app_store, event_payload, "native event export",
        )
        try:
            if source["role"] == "worker":
                expected_transport = "codex_exec_jsonl" if variant == "vnext" else "app_server"
                if export["transport"] != expected_transport:
                    raise CandidateError(f"{variant} worker transport must be {expected_transport}")
            if export["transport"] == "app_server":
                exact_usage = observe_app_server(
                    native_events,
                    raw_evidence=event_payload,
                    evidence_sha256=source["event_sha256"],
                    codex_version=run_seal["codex_version"],
                    protocol_schema_sha256=run_seal["app_protocol_schema_sha256"],
                    thread_id=export["thread_id"],
                    turn_ids=export["turn_ids"],
                )
            else:
                if source["role"] != "worker" or len(export["turn_ids"]) != 1:
                    raise AccountingError("Codex exec transport is worker-only and single-turn")
                exact_usage = observe_exec_jsonl(
                    native_events,
                    raw_evidence=event_payload,
                    evidence_sha256=source["event_sha256"],
                    codex_version=run_seal["codex_version"],
                    thread_id=export["thread_id"],
                    turn_id=export["turn_ids"][0],
                )
        except AccountingError as exc:
            raise CandidateError("native event export is not exact replay authority") from exc
        attempts: list[dict[str, Any]] = [{
            "attempt_ordinal": 1,
            "task_id": source["task_id"],
            "role": source["role"],
            "launch": launch,
            "launch_ref": source["launch_ref"],
            "launch_sha256": source["launch_sha256"],
            "export": export,
            "exact_usage": exact_usage,
        }]
        prior_breakdown = exact_usage["breakdown"]
        for expected_ordinal, continuation in enumerate(source["continuations"], start=2):
            if (
                source["role"] != "worker"
                or not isinstance(continuation, dict)
                or set(continuation) != {
                    "attempt_ordinal", "task_id", "role", "turn_id",
                    "launch_ref", "launch_sha256", "canonical_launch_path",
                    "canonical_launch_sha256", "event_ref", "event_sha256",
                    "export_ref", "export_sha256",
                }
                or continuation["attempt_ordinal"] != expected_ordinal
                or continuation["role"] != "worker"
                or not isinstance(continuation["task_id"], str)
                or not continuation["task_id"].strip()
                or continuation["task_id"] in task_ids
            ):
                raise CandidateError("variant continuation source schema drift")
            task_ids.add(continuation["task_id"])
            inspected_digests.update({
                continuation["launch_sha256"], continuation["event_sha256"],
                continuation["export_sha256"],
            })
            continuation_launch = _bound_json_object(
                workspace, continuation["launch_ref"], continuation["launch_sha256"],
                "continuation launch packet",
            )
            continuation_launch_payload = _canonical_json(continuation_launch) + b"\n"
            _verify_canonical_copy(
                continuation["canonical_launch_path"], continuation["canonical_launch_sha256"],
                _canonical_artifact_root(workspace, run_seal), continuation_launch_payload,
                "continuation launch packet",
            )
            if (
                set(continuation_launch) != {
                    "schema_version", "run_seal_sha256", "pair_id", "variant", "task_id", "role",
                    "model", "reasoning_effort", "transport", "prompt", "host_preamble_sha256", "verification_subject",
                }
                or continuation_launch.get("schema_version") != "agent-workflow.canary-session-launch.v1"
                or continuation_launch.get("run_seal_sha256") != run_seal_sha256
                or continuation_launch.get("pair_id") != pair_id
                or continuation_launch.get("variant") != variant
                or continuation_launch.get("task_id") != continuation["task_id"]
                or continuation_launch.get("role") != "worker"
                or continuation_launch.get("model") != run_seal["worker_model"]
                or continuation_launch.get("reasoning_effort") != run_seal["reasoning_effort"]
                or continuation_launch.get("transport") != "app_server"
                or not isinstance(continuation_launch.get("prompt"), str)
                or not continuation_launch["prompt"].strip()
                or continuation_launch.get("verification_subject") is not None
            ):
                raise CandidateError("continuation launch packet authority drifted")
            continuation_event_payload = _bound_workspace_evidence(
                workspace, continuation["event_ref"], continuation["event_sha256"],
                "continuation native event export",
            )
            continuation_export_payload = _bound_workspace_evidence(
                workspace, continuation["export_ref"], continuation["export_sha256"],
                "continuation host export receipt",
            )
            try:
                continuation_events = [
                    json.loads(line) for line in continuation_event_payload.splitlines() if line.strip()
                ]
                continuation_export = json.loads(continuation_export_payload)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CandidateError("continuation export is invalid JSON") from exc
            if (
                not isinstance(continuation_export, dict)
                or set(continuation_export) != set(export)
                or continuation_export.get("schema_version") != "agent-workflow.canary-host-export.v1"
                or continuation_export.get("run_seal_sha256") != run_seal_sha256
                or continuation_export.get("session_id") != export["session_id"]
                or continuation_export.get("source_session_store_ref") != source["ref"]
                or continuation_export.get("source_prefix_sha256") != source["sha256"]
                or continuation_export.get("raw_session_ref") != source["ref"]
                or continuation_export.get("raw_session_sha256") != source["sha256"]
                or continuation_export.get("transport") != "app_server"
                or continuation_export.get("event_ref") != continuation["event_ref"]
                or continuation_export.get("event_sha256") != continuation["event_sha256"]
                or continuation_export.get("exporter_bundle_sha256") != run_seal["runtime_bundle_sha256"]
                or continuation_export.get("thread_id") != export["thread_id"]
                or continuation_export.get("turn_ids") != [continuation["turn_id"]]
            ):
                raise CandidateError("continuation host export provenance drifted")
            _verify_canonical_copy(
                continuation_export["canonical_raw_path"], continuation_export["canonical_raw_sha256"],
                session_store, raw, "continuation raw session",
            )
            _verify_canonical_copy(
                continuation_export["canonical_event_path"], continuation_export["canonical_event_sha256"],
                app_store, continuation_event_payload, "continuation native event export",
            )
            try:
                continuation_usage = observe_app_server(
                    continuation_events,
                    raw_evidence=continuation_event_payload,
                    evidence_sha256=continuation["event_sha256"],
                    codex_version=run_seal["codex_version"],
                    protocol_schema_sha256=run_seal["app_protocol_schema_sha256"],
                    thread_id=continuation_export["thread_id"],
                    turn_ids=[continuation["turn_id"]],
                    prior_breakdown=prior_breakdown,
                )
            except AccountingError as exc:
                raise CandidateError("continuation native event export is not exact replay authority") from exc
            prior_breakdown = {
                field: prior_breakdown[field] + continuation_usage["breakdown"][field]
                for field in prior_breakdown
            }
            attempts.append({
                "attempt_ordinal": expected_ordinal,
                "task_id": continuation["task_id"],
                "role": "worker",
                "launch": continuation_launch,
                "launch_ref": continuation["launch_ref"],
                "launch_sha256": continuation["launch_sha256"],
                "export": continuation_export,
                "exact_usage": continuation_usage,
            })
        ids: list[str] = []
        contexts: list[dict[str, Any]] = []
        token_events: list[dict[str, Any]] = []
        terminal_events: list[tuple[datetime, str, str | None]] = []
        user_message_contents: list[Any] = []
        user_messages_by_turn: dict[str, list[Any]] = {}
        source_starts: list[datetime] = []
        timestamped_events: list[datetime] = []
        try:
            for line in raw.splitlines():
                item = json.loads(line)
                item_payload = item.get("payload") if isinstance(item, dict) else None
                if not isinstance(item_payload, dict):
                    continue
                if isinstance(item.get("timestamp"), str):
                    timestamped_events.append(_parse_timestamp(item["timestamp"], "variant event timestamp"))
                if item.get("type") == "session_meta":
                    if isinstance(item_payload.get("id"), str):
                        ids.append(item_payload["id"])
                    observed_start = _parse_timestamp(item_payload.get("timestamp"), "variant session start")
                    source_starts.append(observed_start)
                    starts.append(observed_start)
                elif item.get("type") == "turn_context":
                    contexts.append(item_payload)
                elif item.get("type") == "response_item" and item_payload.get("type") == "message" and item_payload.get("role") == "user":
                    user_message_contents.append(item_payload.get("content"))
                    metadata = item_payload.get("internal_chat_message_metadata_passthrough")
                    if isinstance(metadata, dict) and isinstance(metadata.get("turn_id"), str):
                        user_messages_by_turn.setdefault(metadata["turn_id"], []).append(item_payload.get("content"))
                elif item.get("type") == "event_msg" and item_payload.get("type") == "token_count":
                    info = item_payload.get("info")
                    usage = info.get("total_token_usage") if isinstance(info, dict) else None
                    if not isinstance(usage, dict) or not isinstance(usage.get("total_tokens"), int):
                        raise CandidateError("variant token event lacks cumulative total")
                    token_events.append(usage)
                elif item.get("type") == "event_msg" and item_payload.get("type") == "task_complete":
                    terminal_events.append((
                        _parse_timestamp(item.get("timestamp"), "variant terminal time"),
                        item_payload.get("turn_id"),
                        item_payload.get("last_agent_message"),
                    ))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CandidateError("variant raw session is invalid JSONL") from exc
        expected_turns = [
            turn_id
            for attempt in attempts
            for turn_id in attempt["export"].get("turn_ids", [])
        ]
        if (
            len(ids) != 1
            or len(source_starts) != 1
            or ids[0] in session_ids
            or len(contexts) != len(attempts)
            or len(terminal_events) != len(attempts)
            or len(expected_turns) != len(attempts)
            or len(expected_turns) != len(set(expected_turns))
            or not token_events
        ):
            raise CandidateError("variant raw session identity or terminal boundary drift")
        if export["session_id"] != ids[0]:
            raise CandidateError("host export session identity drifted")
        session_ids.add(ids[0])
        contexts_by_turn = {
            context.get("turn_id"): context
            for context in contexts
            if isinstance(context.get("turn_id"), str)
        }
        terminals_by_turn = {
            terminal_turn: (terminal_time, terminal_message)
            for terminal_time, terminal_turn, terminal_message in terminal_events
            if isinstance(terminal_turn, str)
        }
        if set(contexts_by_turn) != set(expected_turns) or set(terminals_by_turn) != set(expected_turns):
            raise CandidateError("variant raw session attempt boundary drift")
        context = contexts_by_turn[expected_turns[-1]]
        terminal_time, terminal_message = terminals_by_turn[expected_turns[-1]]
        if source_starts[0] < sealed_at:
            raise CandidateError("variant raw session predates the immutable run seal")
        if terminal_time < source_starts[0] or any(
            right < left for left, right in zip(timestamped_events, timestamped_events[1:])
        ):
            raise CandidateError("variant raw session chronology is invalid")
        for attempt, turn_id in zip(attempts, expected_turns):
            attempt_context = contexts_by_turn[turn_id]
            attempt_terminal_time, _ = terminals_by_turn[turn_id]
            attempt_launch = attempt["launch"]
            attempt_usage = attempt["exact_usage"]
            if (
                attempt_context.get("effort") != run_seal["reasoning_effort"]
                or attempt_context.get("model") != (
                    run_seal["top_model"] if attempt["role"] in {"coordinator", "verifier"} else run_seal["worker_model"]
                )
                or attempt_usage["thread_id"] != ids[0]
                or attempt_usage["turn_ids"] != [turn_id]
            ):
                raise CandidateError("variant raw session turn, route, or native authority drift")
            prompt_contents = (
                user_messages_by_turn.get(turn_id)
                if len(attempts) > 1
                else user_message_contents
            )
            if not prompt_contents or _explicit_user_prompt(
                prompt_contents, "variant attempt", attempt_launch["host_preamble_sha256"]
            ) != attempt_launch["prompt"]:
                raise CandidateError("variant raw session does not match its attempt launch packet")
            replayed_launches.append({
                "ordinal": len(replayed_launches) + 1,
                "session_id": ids[0],
                "attempt_ordinal": attempt["attempt_ordinal"],
                "turn_id": turn_id,
                "task_id": attempt["task_id"],
                "role": attempt["role"],
                "transport": attempt_launch["transport"],
                "launch_ref": attempt["launch_ref"],
                "launch_sha256": attempt["launch_sha256"],
            })
            _assert_session_denies_canonical_stores(
                attempt_context, workspace, run_seal, "variant session attempt"
            )
            least_privilege_count += 1
            terminals.append(attempt_terminal_time)
        cumulative = [usage["total_tokens"] for usage in token_events]
        if any(value < 0 for value in cumulative) or any(right < left for left, right in zip(cumulative, cumulative[1:])):
            raise CandidateError("variant cumulative tokens moved backwards")
        if token_events[-1] != prior_breakdown:
            raise CandidateError("raw and native token authorities disagree")
        total_tokens += sum(attempt["exact_usage"]["workflow_tokens"] for attempt in attempts)
        if source["role"] == "worker":
            worker_count += 1
        if source["role"] == "verifier":
            verifier_count += 1
            repository_audit = contract_evidence.get("repository_audit") if isinstance(contract_evidence, dict) else None
            integration_after = repository_audit.get("integration_after_sha256") if isinstance(repository_audit, dict) else None
            integration_completed_at = repository_audit.get("integration_completed_at") if isinstance(repository_audit, dict) else None
            expected_subject = {
                "integration_after_sha256": integration_after,
                "output_sha256": coordinator_output_sha256,
            }
            try:
                decision = json.loads(terminal_message) if isinstance(terminal_message, str) else None
                integration_time = _parse_timestamp(integration_completed_at, "integration completion time")
            except (json.JSONDecodeError, CandidateError):
                decision = None
                integration_time = source_starts[0]
            if (
                launch.get("verification_subject") == expected_subject
                and source_starts[0] > integration_time
                and decision == {"status": "approved", **expected_subject}
            ):
                qualified_verifier_count += 1
        if source["role"] == "coordinator":
            if not isinstance(terminal_message, str) or not terminal_message.strip():
                raise CandidateError("coordinator terminal output is missing")
            coordinator_count += 1
            coordinator_completions = count_completion_boundaries(raw, session_id=ids[0])
            coordinator_output_sha256 = _sha256(terminal_message.encode("utf-8"))
            try:
                coordinator_density = classify_completion_density(raw, session_id=ids[0])
            except AccountingError as exc:
                raise CandidateError("coordinator completion density is not replayable") from exc
    if manifest_launches != replayed_launches:
        raise CandidateError("launch manifest does not equal receipt sessions")
    if coordinator_count != 1:
        raise CandidateError("variant receipt must bind exactly one coordinator session")
    corpus = json.loads((CANARY_FIXTURES / "corpus.v1.json").read_bytes())
    workload_id = pair_id.rsplit(":", 1)[0]
    workload = next((item for item in corpus.get("workloads", []) if item.get("id") == workload_id), None)
    minimum_workers = workload.get("minimum_worker_sessions") if isinstance(workload, dict) else None
    if not isinstance(minimum_workers, int) or isinstance(minimum_workers, bool) or minimum_workers < 1:
        raise CandidateError("canary workload worker-session floor is invalid")
    if worker_count < minimum_workers:
        raise CandidateError(f"coordination-heavy variant requires at least {minimum_workers} worker sessions")
    latency = (max(terminals) - min(starts)).total_seconds()
    if latency <= 0:
        raise CandidateError("variant raw latency must be positive")
    return {
        "coordinator_completions": coordinator_completions,
        "total_tokens": total_tokens,
        "latency_seconds": latency,
        "output_sha256": coordinator_output_sha256,
        "session_ids": sorted(session_ids),
        "started_at": min(starts),
        "terminal_at": max(terminals),
        "repository_snapshot_sha256": receipt["repository_snapshot_sha256"],
        "workspace_instance_id": receipt["workspace_instance_id"],
        "completion_density": coordinator_density,
        "worker_count": worker_count,
        "verifier_count": verifier_count,
        "qualified_verifier_count": qualified_verifier_count,
        "session_count": len(session_ids),
        "attempt_count": len(replayed_launches),
        "inspected_evidence_sha256": sorted(inspected_digests),
        "least_privilege_count": least_privilege_count,
        "contract_evidence": contract_evidence,
    }


def _subject_qualification_record(
    *,
    check_id: str,
    workload_id: str,
    trial: int,
    variant_name: str,
    subject_receipt_sha256: str,
    runtime_bundle_sha256: str,
    replayed_metrics: dict[str, Any],
    workload: dict[str, Any],
) -> dict[str, Any]:
    common = {
        "coordinator_completions": replayed_metrics["coordinator_completions"],
        "total_tokens": replayed_metrics["total_tokens"],
        "session_count": replayed_metrics["session_count"],
        "worker_count": replayed_metrics["worker_count"],
        "verifier_count": replayed_metrics["verifier_count"],
        "output_sha256": replayed_metrics["output_sha256"],
        "repository_snapshot_sha256": replayed_metrics["repository_snapshot_sha256"],
    }
    contract = replayed_metrics["contract_evidence"]
    valid_contract = (
        isinstance(contract, dict)
        and set(contract) == {
            "schema_version", "acceptance", "repository_audit", "lineage_audit", "delivery_audit",
            "terminal_audit", "permission_audit", "process_audit", "artifact_audit",
        }
        and contract.get("schema_version") == "agent-workflow.canary-contract-evidence.v1"
    )
    status = "pass"
    observations: dict[str, Any] = common
    if not valid_contract:
        status = "fail"
        observations = {**common, "contract_schema_valid": False}
    elif check_id == "AW-H001":
        acceptance = contract["acceptance"]
        valid = (
            isinstance(acceptance, dict)
            and set(acceptance) == {"command", "exit_code", "criteria_passed", "stdout_sha256", "stderr_sha256"}
            and isinstance(acceptance.get("command"), list)
            and acceptance.get("exit_code") == 0
            and acceptance.get("criteria_passed") == workload["success_criteria"]
            and all(isinstance(acceptance.get(key), str) and acceptance[key].startswith("sha256:") for key in ("stdout_sha256", "stderr_sha256"))
        )
        observations = {**common, "acceptance": acceptance}
        status = "pass" if valid else "fail"
    elif check_id == "AW-H002":
        observations = {**common, "route_attestations_replayed": replayed_metrics["session_count"]}
    elif check_id == "AW-H003":
        density = replayed_metrics["completion_density"]
        observations = {**common, "completion_density": density}
        status = "pass" if density["forbidden_wakes"] == 0 and density["sparse_wait_continuations"] == 0 and density["target_eligible"] is True else "fail"
    elif check_id == "AW-H004":
        audit = contract["terminal_audit"]
        valid = (
            isinstance(audit, dict) and set(audit) == {"process_exit_codes", "native_terminal_statuses", "typed_output_valid"}
            and audit["process_exit_codes"] == [0] * replayed_metrics["session_count"]
            and audit["native_terminal_statuses"] == ["completed"] * replayed_metrics["session_count"]
            and audit["typed_output_valid"] == [True] * replayed_metrics["session_count"]
        )
        observations = {**common, "terminal_audit": audit}
        status = "pass" if valid else "fail"
    elif check_id == "AW-H005":
        audit = contract["repository_audit"]
        valid = False
        if isinstance(audit, dict) and set(audit) == {
            "declared_write_roots", "changed_paths", "writer_scopes", "integration_before_sha256", "integration_after_sha256",
            "integration_completed_at",
        }:
            roots = audit["declared_write_roots"]
            changed = audit["changed_paths"]
            scopes = audit["writer_scopes"]
            try:
                root_paths = [_safe_relative_path(item, "declared write root") for item in roots]
                changed_paths = [_safe_relative_path(item, "changed path") for item in changed]
                scope_paths = [_safe_relative_path(item, "writer scope") for item in scopes]
                _parse_timestamp(audit["integration_completed_at"], "integration completion time")
            except (CandidateError, TypeError):
                root_paths, changed_paths, scope_paths = [], [PurePosixPath("invalid")], []
            valid = (
                isinstance(roots, list) and isinstance(changed, list) and isinstance(scopes, list)
                and len(root_paths) == len(roots) and len(changed_paths) == len(changed) and len(scope_paths) == len(scopes)
                and all(any(path.parts[:len(root.parts)] == root.parts for root in root_paths) for path in changed_paths)
                and len(scope_paths) == len(set(scope_paths))
                and not any(
                    left.parts[:len(right.parts)] == right.parts or right.parts[:len(left.parts)] == left.parts
                    for index, left in enumerate(scope_paths)
                    for right in scope_paths[index + 1:]
                )
                and all(isinstance(item, str) and item.startswith("sha256:") for item in (audit["integration_before_sha256"], audit["integration_after_sha256"]))
            )
        observations = {**common, "repository_audit": audit}
        status = "pass" if valid else "fail"
    elif check_id == "AW-H006":
        audit = contract["permission_audit"]
        valid = (
            replayed_metrics["least_privilege_count"] == replayed_metrics["attempt_count"]
            and isinstance(audit, dict)
            and set(audit) == {"git_write_exit", "publish_exit", "production_write_exit", "undeclared_network_exit"}
            and all(isinstance(audit[key], int) and not isinstance(audit[key], bool) and audit[key] != 0 for key in audit)
        )
        observations = {**common, "least_privilege_sessions": replayed_metrics["least_privilege_count"], "permission_audit": audit}
        status = "pass" if valid else "fail"
    elif check_id == "AW-H007":
        audit = contract["lineage_audit"]
        entries = audit.get("original_lineages") if isinstance(audit, dict) else None
        valid = (
            isinstance(audit, dict) and set(audit) == {"original_lineages"}
            and isinstance(entries, list) and entries
            and all(
                isinstance(item, dict) and set(item) == {"lineage_id", "recoveries", "successful_sibling_reruns"}
                and isinstance(item["lineage_id"], str) and item["lineage_id"]
                and isinstance(item["recoveries"], int) and not isinstance(item["recoveries"], bool) and 0 <= item["recoveries"] <= 1
                and item["successful_sibling_reruns"] == 0
                for item in entries
            )
        )
        observations = {**common, "lineage_audit": audit}
        status = "pass" if valid else "fail"
    elif check_id == "AW-H008":
        observations = {
            **common,
            "independent_verifier_sessions": replayed_metrics["verifier_count"],
            "qualified_post_integration_approvals": replayed_metrics["qualified_verifier_count"],
        }
        status = "pass" if replayed_metrics["qualified_verifier_count"] >= 1 else "fail"
    elif check_id == "AW-H009":
        audit = contract["process_audit"]
        receipts = audit.get("watchdog_receipts") if isinstance(audit, dict) else None
        valid = (
            isinstance(audit, dict) and set(audit) == {"owned_processes", "watchdog_receipts", "post_reap_live_groups"}
            and audit["owned_processes"] == replayed_metrics["attempt_count"]
            and isinstance(receipts, list) and len(receipts) == replayed_metrics["attempt_count"]
            and all(
                isinstance(item, dict)
                and set(item) == {"process_id", "process_group_id", "terminal_status", "reaped", "stdout_sha256", "stderr_sha256"}
                and isinstance(item["process_id"], str) and item["process_id"]
                and isinstance(item["process_group_id"], int) and not isinstance(item["process_group_id"], bool) and item["process_group_id"] > 0
                and item["terminal_status"] == "completed" and item["reaped"] is True
                and all(isinstance(item[key], str) and item[key].startswith("sha256:") for key in ("stdout_sha256", "stderr_sha256"))
                for item in receipts
            )
            and len({item["process_id"] for item in receipts}) == len(receipts)
            and len({item["process_group_id"] for item in receipts}) == len(receipts)
            and audit["post_reap_live_groups"] == 0
        )
        observations = {**common, "process_audit": audit}
        status = "pass" if valid else "fail"
    elif check_id == "AW-H010":
        audit = contract["artifact_audit"]
        valid = (
            isinstance(audit, dict) and set(audit) == {"create_once_collisions", "digest_replay_failures", "authority_replay_failures"}
            and audit == {"create_once_collisions": 0, "digest_replay_failures": 0, "authority_replay_failures": 0}
            and subject_receipt_sha256 in replayed_metrics["inspected_evidence_sha256"]
        )
        observations = {**common, "replayed_evidence_count": len(replayed_metrics["inspected_evidence_sha256"]), "artifact_audit": audit}
        status = "pass" if valid else "fail"
    elif check_id == "AW-H011":
        audit = contract["delivery_audit"]
        valid = (
            isinstance(audit, dict) and set(audit) == {"sealed_final_deliveries", "post_final_product_actions"}
            and audit["sealed_final_deliveries"] == 1
            and audit["post_final_product_actions"] == []
        )
        observations = {**common, "delivery_audit": audit}
        status = "pass" if valid else "fail"
    return {
        "schema_version": "agent-workflow.canary-subject-qualification.v1",
        "check_id": check_id,
        "workload_id": workload_id,
        "trial": trial,
        "variant": variant_name,
        "subject_receipt_sha256": subject_receipt_sha256,
        "validator_id": f"agent-workflow.hidden-validator.{HIDDEN_EVIDENCE_TYPES[check_id]}.v1",
        "validator_bundle_sha256": runtime_bundle_sha256,
        "qualification_command_ids": HIDDEN_QUALIFICATION_MAP[check_id],
        "inspected_evidence_sha256": replayed_metrics["inspected_evidence_sha256"],
        "validator_result": {"status": status, "observations": observations},
    }


def _validate_hidden_proof(
    proof: Any,
    *,
    check_id: str,
    workload_id: str,
    trial: int,
    variant_name: str,
    run_seal_sha256: str,
    runtime_bundle_sha256: str,
    expected_record: dict[str, Any],
    qualification_records: dict[str, Any] | None,
) -> bool:
    expected_validator = f"agent-workflow.hidden-validator.{HIDDEN_EVIDENCE_TYPES[check_id]}.v1"
    if (
        not isinstance(proof, dict)
        or set(proof) != {
            "schema_version", "check_id", "workload_id", "trial", "variant",
            "run_seal_sha256", "evidence_type", "validator_id", "validator_bundle_sha256",
            "qualification_record", "qualification_record_sha256",
        }
        or proof["schema_version"] != "agent-workflow.canary-hidden-proof.v1"
        or proof["check_id"] != check_id
        or proof["workload_id"] != workload_id
        or proof["trial"] != trial
        or proof["variant"] != variant_name
        or proof["run_seal_sha256"] != run_seal_sha256
        or proof["evidence_type"] != HIDDEN_EVIDENCE_TYPES[check_id]
        or proof["validator_id"] != expected_validator
        or proof["validator_bundle_sha256"] != runtime_bundle_sha256
        or proof["qualification_record"] != expected_record
        or proof["qualification_record_sha256"] != _sha256(_canonical_json(expected_record) + b"\n")
    ):
        raise CandidateError(f"hidden check {check_id} deterministic proof drifted")
    if qualification_records is not None:
        record_sha = proof["qualification_record_sha256"]
        if qualification_records.get(record_sha) != expected_record:
            raise CandidateError(f"hidden check {check_id} lacks authenticated per-subject qualification")
        if any(
            command_id not in qualification_records
            or not isinstance(qualification_records[command_id], str)
            or not qualification_records[command_id].startswith("sha256:")
            for command_id in expected_record["qualification_command_ids"]
        ):
            raise CandidateError(f"hidden check {check_id} lacks frozen qualification authority")
    return expected_record["validator_result"]["status"] == "pass"


def seal_run(
    workspace: Path,
    *,
    repo: Path,
    freeze_path: Path,
    source_path: Path,
    results_ref: str,
    output_path: str,
) -> dict[str, Any]:
    workspace = Path(workspace).resolve(strict=True)
    freeze = verify_executable_freeze(repo, freeze_path)
    source_path = Path(source_path)
    if source_path.is_symlink() or not source_path.is_file():
        raise CandidateError("canary run seal source must be a regular file")
    source = _run_seal_source(json.loads(source_path.read_bytes()))
    fixture_seal = _verified_fixture_seal()
    if source["fixture_seal_sha256"] != _sha256(fixture_seal):
        raise CandidateError("canary run seal fixture digest drifted")
    if source["executable_freeze_sha256"] != _sha256(Path(freeze_path).read_bytes()):
        raise CandidateError("canary run seal executable freeze digest drifted")
    if source["candidate_bundle_sha256"] != freeze["semantic_bundle_sha256"]:
        raise CandidateError("canary run seal candidate bundle does not match the semantic freeze")
    if source["runtime_bundle_sha256"] != freeze["runtime_bundle_sha256"]:
        raise CandidateError("canary run seal runtime bundle does not match the executable freeze")
    _bound_workspace_evidence(
        workspace,
        source["blind_label_map_ref"],
        source["blind_label_map_sha256"],
        "blind label map",
    )
    _bound_workspace_evidence(workspace, source["rubric_ref"], source["rubric_sha256"], "blind rubric")
    _verify_environment_authority(workspace, source)
    allowed_pre_result_files = {
        source["blind_label_map_ref"], source["rubric_ref"],
        source["repository_fixture_ref"], source["codex_identity_ref"],
        source["host_profile_ref"], source["capacity_profile_ref"],
    }
    output_relative = _safe_relative_path(output_path, "run seal output")
    output = workspace.joinpath(*output_relative.parts)
    with authority_transaction(workspace):
        if output.exists() or output.is_symlink():
            if output.is_symlink() or not output.is_file():
                raise CandidateError("existing run seal is unsafe")
            existing = _sealed_run(json.loads(output.read_bytes()))
            existing_source = dict(existing)
            existing_source.pop("sealed_at")
            existing_source.pop("host_authority_id")
            existing_results_ref = existing_source.pop("results_ref")
            existing_freeze_path = existing_source.pop("freeze_path")
            existing_repository_root = existing_source.pop("repository_root")
            if existing_source != source:
                raise CandidateError("existing run seal drifted from the requested authority")
            if existing_results_ref != _safe_relative_path(results_ref, "results ref").as_posix():
                raise CandidateError("existing run seal results target drifted")
            if existing_freeze_path != os.fspath(Path(freeze_path).resolve(strict=True)) or existing_repository_root != os.fspath(Path(repo).resolve(strict=True)):
                raise CandidateError("existing run seal freeze or repository authority drifted")
            _load_host_key(workspace, existing["host_authority_id"])
            _verify_frozen_repository(workspace, freeze)
            results_path = workspace.joinpath(*_safe_relative_path(results_ref, "results ref").parts)
            if not results_path.exists() and not results_path.is_symlink():
                observed = {
                    path.relative_to(workspace).as_posix()
                    for path in workspace.rglob("*")
                    if path.is_file() or path.is_symlink()
                }
                if observed != allowed_pre_result_files | {output_relative.as_posix()}:
                    raise CandidateError("existing run seal is poisoned by a failed pre-result transaction")
            else:
                evaluate_results(output, results_path)
            return {"run_seal_ref": output_relative.as_posix(), "run_seal_sha256": _sha256(output.read_bytes())}
        observed_pre_result_files = {
            path.relative_to(workspace).as_posix()
            for path in workspace.rglob("*")
            if path.is_file() or path.is_symlink()
        }
        if observed_pre_result_files != allowed_pre_result_files:
            raise CandidateError("canary workspace contains unsealed result/evidence before the run seal")
        results_path = workspace.joinpath(*_safe_relative_path(results_ref, "results ref").parts)
        if results_path.exists() or results_path.is_symlink():
            raise CandidateError("run seal must be created before the first result")
        sealed = dict(source)
        sealed["sealed_at"] = datetime.now(timezone.utc).isoformat()
        _, sealed["host_authority_id"] = _ensure_host_key(workspace)
        _materialize_frozen_repository(workspace, Path(repo).resolve(strict=True), freeze)
        sealed["results_ref"] = _safe_relative_path(results_ref, "results ref").as_posix()
        sealed["freeze_path"] = os.fspath(Path(freeze_path).resolve(strict=True))
        sealed["repository_root"] = os.fspath(Path(repo).resolve(strict=True))
        output = create_once_json(workspace, output_relative.as_posix(), sealed)
        observed_after_publish = {
            path.relative_to(workspace).as_posix()
            for path in workspace.rglob("*")
            if path.is_file() or path.is_symlink()
        }
        if observed_after_publish != allowed_pre_result_files | {output_relative.as_posix()}:
            raise CandidateError("canary workspace changed during the run-seal transaction")
        return {"run_seal_ref": output_relative.as_posix(), "run_seal_sha256": _sha256(output.read_bytes())}


def _number(value: Any, label: str, *, integer: bool = False, allow_zero: bool = False) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise CandidateError(f"{label} must be a finite number")
    if integer and not isinstance(value, int):
        raise CandidateError(f"{label} must be an integer")
    if value < 0 or (value == 0 and not allow_zero):
        raise CandidateError(f"{label} must be positive")
    return value


def _median(values: list[float | int]) -> float:
    return float(statistics.median(values))


def _p95(values: list[float | int]) -> float:
    ordered = sorted(float(item) for item in values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def expected_pair_order(run_seal: dict[str, Any], pair_id: str) -> list[str]:
    material = f"{run_seal['paired_order_seed']}|{run_seal['candidate_bundle_sha256']}|{pair_id}".encode()
    return ["legacy", "vnext"] if hashlib.sha256(material).digest()[0] % 2 == 0 else ["vnext", "legacy"]


def expected_label_map(run_seal: dict[str, Any], pair_ids: set[str]) -> dict[str, dict[str, str]]:
    ranked = sorted(
        pair_ids,
        key=lambda pair_id: hashlib.sha256(
            f"labels|{run_seal['paired_order_seed']}|{run_seal['candidate_bundle_sha256']}|{pair_id}".encode()
        ).digest(),
    )
    legacy_a = set(ranked[: len(ranked) // 2])
    return {
        pair_id: ({"legacy": "A", "vnext": "B"} if pair_id in legacy_a else {"legacy": "B", "vnext": "A"})
        for pair_id in pair_ids
    }


def evaluate_results(
    run_seal_path: Path,
    results_path: Path,
    *,
    require_host_seal: bool = True,
    qualification_records: dict[str, str] | None = None,
) -> dict[str, Any]:
    run_seal_path = Path(run_seal_path)
    results_path = Path(results_path)
    if run_seal_path.is_symlink() or not run_seal_path.is_file() or results_path.is_symlink() or not results_path.is_file():
        raise CandidateError("canary run seal and results must be regular files")
    run_seal_payload = run_seal_path.read_bytes()
    run_seal = _sealed_run(json.loads(run_seal_payload))
    if run_seal["fixture_seal_sha256"] != _sha256(_verified_fixture_seal()):
        raise CandidateError("canary run seal fixture digest drifted")
    workspace = run_seal_path.parent.resolve()
    if require_host_seal:
        freeze = verify_executable_freeze(Path(run_seal["repository_root"]), Path(run_seal["freeze_path"]))
        if (
            _sha256(Path(run_seal["freeze_path"]).read_bytes()) != run_seal["executable_freeze_sha256"]
            or freeze["semantic_bundle_sha256"] != run_seal["candidate_bundle_sha256"]
            or freeze["runtime_bundle_sha256"] != run_seal["runtime_bundle_sha256"]
        ):
            raise CandidateError("final canary evaluation bundle drifted from the pre-result freeze")
    _verify_environment_authority(workspace, run_seal)
    try:
        results = json.loads(results_path.read_bytes())
        corpus = json.loads((CANARY_FIXTURES / "corpus.v1.json").read_bytes())
        label_map = json.loads(_bound_workspace_evidence(
            workspace,
            run_seal["blind_label_map_ref"],
            run_seal["blind_label_map_sha256"],
            "blind label map",
        ))
        rubric = json.loads(_bound_workspace_evidence(
            workspace,
            run_seal["rubric_ref"],
            run_seal["rubric_sha256"],
            "blind rubric",
        ))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateError("canary results or corpus is invalid JSON") from exc
    core_keys = {"schema_version", "run_seal_sha256", "verifier_ledger_ref", "verifier_ledger_sha256", "pairs"}
    final_keys = core_keys | {
        "host_evidence_seal_ref", "host_evidence_seal_sha256",
        "host_qualification_ref", "host_qualification_sha256",
    }
    if not isinstance(results, dict) or set(results) != (final_keys if require_host_seal else core_keys):
        raise CandidateError("canary results schema drift")
    if results["schema_version"] != RESULTS_SCHEMA or results["run_seal_sha256"] != _sha256(run_seal_payload):
        raise CandidateError("canary results are not bound to the run seal")
    if require_host_seal:
        core = {key: results[key] for key in core_keys}
        host_seal = _bound_json_object(
            workspace,
            results["host_evidence_seal_ref"],
            results["host_evidence_seal_sha256"],
            "host evidence seal",
        )
        qualification = _bound_json_object(
            workspace,
            results["host_qualification_ref"],
            results["host_qualification_sha256"],
            "host qualification receipt",
        )
        _verify_host_qualification(workspace, qualification, run_seal)
        qualification_records = {
            item["command_id"]: _sha256(_canonical_json(item) + b"\n")
            for item in qualification["commands"]
        }
        qualification_records.update({item["sha256"]: item["record"] for item in qualification["subject_records"]})
        signed = {
            "schema_version": "agent-workflow.canary-host-evidence-seal.v1",
            "run_seal_sha256": results["run_seal_sha256"],
            "results_core_sha256": _sha256(_canonical_json(core) + b"\n"),
            "host_authority_id": run_seal["host_authority_id"],
            "host_qualification_sha256": results["host_qualification_sha256"],
        }
        if (
            set(host_seal) != set(signed) | {"mac"}
            or any(host_seal.get(key) != value for key, value in signed.items())
            or host_seal.get("mac") != _host_mac(
                _load_host_key(workspace, run_seal["host_authority_id"]), signed,
            )
        ):
            raise CandidateError("host evidence seal authority drifted")
    verifier_payload = _bound_workspace_evidence(
        workspace,
        results["verifier_ledger_ref"],
        results["verifier_ledger_sha256"],
        "independent verifier ledger",
    )
    try:
        verifier_evidence = json.loads(verifier_payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateError("independent verifier ledger is invalid JSON") from exc
    verifier_decision = verifier_evidence.get("decision") if isinstance(verifier_evidence, dict) else None
    if (
        not isinstance(verifier_evidence, dict)
        or set(verifier_evidence) != {
            "schema_version", "reviewer_route", "raw_session_ref", "raw_session_sha256",
            "canonical_raw_path", "canonical_raw_sha256", "launch_packet_ref", "launch_packet_sha256",
            "host_preamble_sha256", "decision",
        }
        or verifier_evidence["schema_version"] != "agent-workflow.canary-verifier-evidence.v1"
        or not isinstance(verifier_decision, dict)
        or set(verifier_decision) != {
            "schema_version", "run_seal_sha256", "P0", "P1", "P2",
            "accepted_hidden_evidence_manifest_sha256",
        }
        or verifier_decision["schema_version"] != "agent-workflow.canary-verifier-decision.v1"
        or verifier_decision["run_seal_sha256"] != results["run_seal_sha256"]
        or not isinstance(verifier_decision["accepted_hidden_evidence_manifest_sha256"], str)
    ):
        raise CandidateError("independent verifier ledger schema or seal binding drifted")
    verifier_session_id, verifier_packet = _verify_blind_reviewer_session(workspace, verifier_evidence, run_seal)
    if (
        set(verifier_packet) != {
            "schema_version", "purpose", "run_seal_sha256", "host_preamble_sha256", "workloads",
            "hidden_evidence_manifest_ref", "hidden_evidence_manifest_sha256", "hidden_evidence_index",
        }
        or verifier_packet.get("schema_version") != "agent-workflow.canary-review-launch.v1"
        or verifier_packet.get("purpose") != "independent_verifier"
        or verifier_packet.get("run_seal_sha256") != results["run_seal_sha256"]
        or verifier_packet.get("host_preamble_sha256") != verifier_evidence["host_preamble_sha256"]
        or verifier_packet.get("workloads") != corpus.get("workloads")
        or verifier_decision["accepted_hidden_evidence_manifest_sha256"]
        != verifier_packet.get("hidden_evidence_manifest_sha256")
    ):
        raise CandidateError("independent verifier launch packet drifted from its decision authority")
    manifest_payload = _bound_workspace_evidence(
        workspace,
        verifier_packet["hidden_evidence_manifest_ref"],
        verifier_packet["hidden_evidence_manifest_sha256"],
        "independent verifier hidden-evidence manifest",
    )
    try:
        hidden_manifest = json.loads(manifest_payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateError("independent verifier hidden-evidence manifest is invalid JSON") from exc
    if (
        not isinstance(hidden_manifest, dict)
        or set(hidden_manifest) != {"schema_version", "run_seal_sha256", "entries"}
        or hidden_manifest.get("schema_version") != "agent-workflow.canary-hidden-evidence-manifest.v1"
        or hidden_manifest.get("run_seal_sha256") != results["run_seal_sha256"]
        or verifier_packet.get("hidden_evidence_index")
        != hidden_evidence_verifier_index(hidden_manifest)
    ):
        raise CandidateError("independent verifier hidden-evidence manifest schema drifted")
    if not isinstance(verifier_decision["P0"], list) or not isinstance(verifier_decision["P1"], list):
        raise CandidateError("canary P0/P1 findings must be lists")
    if verifier_decision["P0"] != [] or verifier_decision["P1"] != []:
        hard_failures = ["open P0/P1 finding"]
    else:
        hard_failures = []
    if not isinstance(verifier_decision["P2"], list):
        raise CandidateError("canary P2 findings must be a list")
    p2_ids: set[str] = set()
    for item in verifier_decision["P2"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"id", "resolution", "owner", "promotion_gate", "gate_status", "detail_ref", "detail_sha256"}
            or item["resolution"] not in ALLOWED_P2_RESOLUTIONS
            or item["gate_status"] not in {"passed", "pending", "blocked"}
            or not all(isinstance(item[key], str) and item[key].strip() for key in ("id", "owner", "promotion_gate"))
        ):
            raise CandidateError("canary P2 finding lacks a typed resolution")
        if item["id"] in p2_ids:
            raise CandidateError("canary P2 finding IDs must be unique")
        p2_ids.add(item["id"])
        _validate_p2_detail(workspace, item, run_seal, verifier_session_id, qualification_records)
        if item["resolution"] == "blocked_external" or item["gate_status"] != "passed":
            hard_failures.append(f"P2 {item['id']} remains externally blocked")

    if (
        not isinstance(corpus, dict)
        or set(corpus) != {"schema_version", "corpus_id", "paired_trials_per_workload", "pairing_policy", "workloads"}
        or corpus["schema_version"] != "agent-workflow.canary-corpus.v1"
        or corpus["paired_trials_per_workload"] != 5
        or not isinstance(corpus["workloads"], list)
        or not corpus["workloads"]
    ):
        raise CandidateError("canary corpus schema drift")
    workloads: dict[str, dict[str, Any]] = {}
    workload_keys = {
        "id", "workload_class", "coordination_heavy", "minimum_worker_sessions", "objective", "fixture_profile",
        "success_criteria", "hard_check_ids",
    }
    for item in corpus["workloads"]:
        if (
            not isinstance(item, dict)
            or set(item) != workload_keys
            or not isinstance(item.get("id"), str)
            or not item["id"].strip()
            or item["id"] in workloads
            or not isinstance(item.get("coordination_heavy"), bool)
            or not isinstance(item.get("minimum_worker_sessions"), int)
            or isinstance(item.get("minimum_worker_sessions"), bool)
            or item["minimum_worker_sessions"] < 1
            or not isinstance(item.get("hard_check_ids"), list)
            or not item["hard_check_ids"]
            or any(check_id not in HIDDEN_EVIDENCE_TYPES for check_id in item["hard_check_ids"])
        ):
            raise CandidateError("canary workload schema or hidden-check set drift")
        workloads[item["id"]] = item
    expected_trials = corpus["paired_trials_per_workload"]
    expected_pair_ids = {
        f"{workload_id}:{trial}"
        for workload_id in workloads
        for trial in range(1, expected_trials + 1)
    }
    expected_seed = _sha256(f"{corpus['corpus_id']}|{run_seal['candidate_bundle_sha256']}".encode())
    if run_seal["paired_order_seed"] != expected_seed:
        raise CandidateError("paired order seed was not derived from corpus and semantic bundle authority")
    if (
        not isinstance(label_map, dict)
        or set(label_map) != {"schema_version", "pairs"}
        or label_map["schema_version"] != "agent-workflow.canary-blind-label-map.v1"
        or not isinstance(label_map["pairs"], dict)
        or set(label_map["pairs"]) != expected_pair_ids
    ):
        raise CandidateError("blind label map does not match the sealed paired schedule")
    if label_map["pairs"] != expected_label_map(run_seal, expected_pair_ids):
        raise CandidateError("blind label map was not deterministically balanced from sealed authority")
    for mapping in label_map["pairs"].values():
        if not isinstance(mapping, dict) or set(mapping) != {"legacy", "vnext"} or set(mapping.values()) != {"A", "B"}:
            raise CandidateError("blind label map pair must biject legacy/vnext to A/B")
    if (
        not isinstance(rubric, dict)
        or set(rubric) != {"schema_version", "dimensions", "score_range"}
        or rubric["schema_version"] != "agent-workflow.canary-rubric.v1"
        or rubric["dimensions"] != ["correctness", "evidence", "completeness"]
        or rubric["score_range"] != [0, 4]
    ):
        raise CandidateError("blind rubric schema drift")
    pairs = results["pairs"]
    if not isinstance(pairs, list) or len(pairs) != len(workloads) * expected_trials:
        raise CandidateError("canary results do not contain the sealed paired schedule")
    seen: set[tuple[str, int]] = set()
    metric_rows: dict[str, list[dict[str, Any]]] = {name: [] for name in workloads}
    quality: dict[str, dict[str, list[int]]] = {
        variant: {dimension: [] for dimension in ("correctness", "evidence", "completeness")}
        for variant in ("legacy", "vnext")
    }
    preferences: dict[str, list[str]] = {name: [] for name in workloads}
    observed_hidden_evidence: set[str] = set()
    manifest_evidence_items: list[dict[str, Any]] = []
    variant_session_ids: set[str] = set()
    blind_reviewer_session_ids: set[str] = set()
    for pair in pairs:
        if not isinstance(pair, dict) or set(pair) != {"workload_id", "trial", "execution_order", "variants", "blind_review_ref", "blind_review_sha256"}:
            raise CandidateError("canary pair schema drift")
        workload_id = pair["workload_id"]
        trial = pair["trial"]
        if workload_id not in workloads or not isinstance(trial, int) or not 1 <= trial <= expected_trials:
            raise CandidateError("canary pair identity is invalid")
        identity = (workload_id, trial)
        if identity in seen:
            raise CandidateError("canary pair identity is duplicated")
        seen.add(identity)
        pair_id = f"{workload_id}:{trial}"
        if pair["execution_order"] != expected_pair_order(run_seal, pair_id):
            raise CandidateError("canary pair execution order drifted from the sealed seed")
        review_payload = _bound_workspace_evidence(
            workspace,
            pair["blind_review_ref"],
            pair["blind_review_sha256"],
            "blind review",
        )
        try:
            review = json.loads(review_payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CandidateError("blind review is invalid JSON") from exc
        decision = review.get("decision") if isinstance(review, dict) else None
        if (
            not isinstance(review, dict)
            or set(review) != {
                "schema_version", "run_seal_sha256", "reviewer_route", "raw_session_ref", "raw_session_sha256",
                "canonical_raw_path", "canonical_raw_sha256", "launch_packet_ref", "launch_packet_sha256",
                "host_preamble_sha256", "decision",
            }
            or review["schema_version"] != "agent-workflow.canary-blind-review-evidence.v1"
            or review["run_seal_sha256"] != results["run_seal_sha256"]
            or not isinstance(decision, dict)
            or set(decision) != {"schema_version", "pair_id", "rubric_sha256", "labels", "preference"}
            or decision["schema_version"] != "agent-workflow.canary-blind-review.v1"
            or decision["pair_id"] != pair_id
            or decision["rubric_sha256"] != run_seal["rubric_sha256"]
            or decision["preference"] not in {"A", "B", "tie"}
            or not isinstance(decision["labels"], dict)
            or set(decision["labels"]) != {"A", "B"}
        ):
            raise CandidateError("blind review schema or authority drift")
        reviewer_session_id, review_packet = _verify_blind_reviewer_session(workspace, review, run_seal)
        if (
            set(review_packet) != {
                "schema_version", "purpose", "pair_id", "run_seal_sha256", "rubric_sha256",
                "host_preamble_sha256", "workload", "rubric", "label_artifacts", "hidden_evidence",
            }
            or review_packet.get("schema_version") != "agent-workflow.canary-review-launch.v1"
            or review_packet.get("purpose") != "blind_pair_review"
            or review_packet.get("pair_id") != pair_id
            or review_packet.get("run_seal_sha256") != results["run_seal_sha256"]
            or review_packet.get("host_preamble_sha256") != review["host_preamble_sha256"]
            or review_packet.get("workload") != workloads[workload_id]
            or review_packet.get("rubric_sha256") != run_seal["rubric_sha256"]
            or review_packet.get("rubric") != rubric
            or _sha256(_canonical_json(review_packet.get("rubric")) + b"\n") != run_seal["rubric_sha256"]
        ):
            raise CandidateError("blind reviewer launch packet identity drifted")
        if reviewer_session_id in blind_reviewer_session_ids:
            raise CandidateError("blind reviewer session cannot authorize more than one pair")
        blind_reviewer_session_ids.add(reviewer_session_id)
        mapping = label_map["pairs"][pair_id]
        reverse_mapping = {label: variant for variant, label in mapping.items()}
        preferences[workload_id].append(
            "tie" if decision["preference"] == "tie" else reverse_mapping[decision["preference"]]
        )
        variants = pair["variants"]
        if not isinstance(variants, dict) or set(variants) != {"legacy", "vnext"}:
            raise CandidateError("canary pair must contain legacy and vnext variants")
        row: dict[str, Any] = {"workload_id": workload_id, "trial": trial}
        pair_hidden_evidence: list[str] = []
        for variant_name, variant in variants.items():
            required = {"receipt_ref", "receipt_sha256", "hard_checks"}
            if not isinstance(variant, dict) or set(variant) != required:
                raise CandidateError("canary variant schema drift")
            replayed_metrics = _replay_variant_receipt(
                workspace,
                ref=variant["receipt_ref"],
                digest=variant["receipt_sha256"],
                pair_id=pair_id,
                variant=variant_name,
                run_seal=run_seal,
                run_seal_sha256=results["run_seal_sha256"],
            )
            replayed_session_ids = set(replayed_metrics["session_ids"])
            if variant_session_ids & replayed_session_ids:
                raise CandidateError("variant raw sessions must be unique across the paired schedule")
            variant_session_ids.update(replayed_session_ids)
            completions = replayed_metrics["coordinator_completions"]
            tokens = replayed_metrics["total_tokens"]
            latency = replayed_metrics["latency_seconds"]
            checks = variant["hard_checks"]
            required_checks = set(workloads[workload_id]["hard_check_ids"])
            if not isinstance(checks, dict) or set(checks) != required_checks:
                raise CandidateError("canary hidden-check set drifted")
            for check_id, check in checks.items():
                if not isinstance(check, dict) or set(check) != {
                    "evidence_ref", "evidence_sha256", "canonical_evidence_path", "canonical_evidence_sha256",
                }:
                    raise CandidateError("canary hidden-check evidence schema drift")
                check_payload = _bound_workspace_evidence(
                    workspace,
                    check["evidence_ref"],
                    check["evidence_sha256"],
                    f"hidden check {check_id} evidence",
                )
                observed_hidden_evidence.add(check["evidence_sha256"])
                pair_hidden_evidence.append(check["evidence_sha256"])
                _verify_canonical_copy(
                    check["canonical_evidence_path"],
                    check["canonical_evidence_sha256"],
                    _canonical_artifact_root(workspace, run_seal),
                    check_payload,
                    f"hidden check {check_id} proof",
                )
                try:
                    check_evidence = json.loads(check_payload)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise CandidateError("hidden-check evidence is invalid JSON") from exc
                manifest_evidence_items.append({
                    "sha256": check["evidence_sha256"],
                    "content_ref": check["evidence_ref"],
                    "content": check_evidence,
                })
                proof_passed = _validate_hidden_proof(
                    check_evidence,
                    check_id=check_id,
                    workload_id=workload_id,
                    trial=trial,
                    variant_name=variant_name,
                    run_seal_sha256=results["run_seal_sha256"],
                    runtime_bundle_sha256=run_seal["runtime_bundle_sha256"],
                    expected_record=_subject_qualification_record(
                        check_id=check_id,
                        workload_id=workload_id,
                        trial=trial,
                        variant_name=variant_name,
                        subject_receipt_sha256=variant["receipt_sha256"],
                        runtime_bundle_sha256=run_seal["runtime_bundle_sha256"],
                        replayed_metrics=replayed_metrics,
                        workload=workloads[workload_id],
                    ),
                    qualification_records=qualification_records,
                )
                if not proof_passed and (variant_name == "vnext" or check_id != "AW-H003"):
                    hard_failures.append(f"{workload_id}/{trial}/{variant_name}: hidden contract failure {check_id}")
            label = mapping[variant_name]
            label_review = decision["labels"][label]
            scores = label_review.get("scores") if isinstance(label_review, dict) else None
            if (
                not isinstance(label_review, dict)
                or set(label_review) != {"output_sha256", "scores"}
                or label_review["output_sha256"] != replayed_metrics["output_sha256"]
                or not isinstance(scores, dict)
                or set(scores) != {"correctness", "evidence", "completeness"}
            ):
                raise CandidateError("canary blind review score schema drift")
            for dimension, score in scores.items():
                if not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= 4:
                    raise CandidateError("canary blind score must be an integer from 0 to 4")
                quality[variant_name][dimension].append(score)
            row[variant_name] = {
                "completions": completions,
                "tokens": tokens,
                "latency": latency,
                "started_at": replayed_metrics["started_at"],
                "terminal_at": replayed_metrics["terminal_at"],
                "repository_snapshot_sha256": replayed_metrics["repository_snapshot_sha256"],
                "workspace_instance_id": replayed_metrics["workspace_instance_id"],
            }
        if row["legacy"]["repository_snapshot_sha256"] != row["vnext"]["repository_snapshot_sha256"]:
            raise CandidateError("paired variants did not use the exact same repository snapshot")
        if row["legacy"]["workspace_instance_id"] == row["vnext"]["workspace_instance_id"]:
            raise CandidateError("paired variants reused one mutable workspace instance")
        first_name, second_name = pair["execution_order"]
        if row[first_name]["terminal_at"] > row[second_name]["started_at"]:
            raise CandidateError("paired execution timestamps contradict the sealed execution order")
        expected_label_outputs = {label: decision["labels"][label]["output_sha256"] for label in ("A", "B")}
        label_artifacts = review_packet["label_artifacts"]
        if (
            not isinstance(label_artifacts, dict)
            or set(label_artifacts) != {"A", "B"}
            or any(
                not isinstance(label_artifacts[label], dict)
                or set(label_artifacts[label]) != {"sha256", "content"}
                or not isinstance(label_artifacts[label]["content"], str)
                or label_artifacts[label]["sha256"] != _sha256(label_artifacts[label]["content"].encode("utf-8"))
                or label_artifacts[label]["sha256"] != expected_label_outputs[label]
                for label in ("A", "B")
            )
            or _inline_json_evidence(review_packet["hidden_evidence"], "blind reviewer") != sorted(pair_hidden_evidence)
        ):
            raise CandidateError("blind reviewer launch packet did not receive exact outputs and hidden evidence")
        metric_rows[workload_id].append(row)

    expected_hidden_manifest = build_hidden_evidence_manifest(
        results["run_seal_sha256"], manifest_evidence_items,
    )
    if hidden_manifest != expected_hidden_manifest:
        raise CandidateError("independent verifier manifest does not cover the exact hidden-evidence set")
    for entry in hidden_manifest["entries"]:
        authority_payload = _bound_workspace_evidence(
            workspace,
            entry["authority_ref"],
            entry["content_sha256"],
            "independent verifier authority proof",
        )
        reader_payload = _bound_workspace_evidence(
            workspace,
            entry["reader_ref"],
            entry["content_sha256"],
            "independent verifier readable proof",
        )
        if reader_payload != authority_payload:
            raise CandidateError("independent verifier readable proof drifted from authority")
    if {
        entry["content_sha256"] for entry in hidden_manifest["entries"]
    } != observed_hidden_evidence:
        raise CandidateError("independent verifier did not accept the exact hidden-evidence set")
    reviewer_session_ids = blind_reviewer_session_ids | {verifier_session_id}
    if len(reviewer_session_ids) != len(blind_reviewer_session_ids) + 1:
        raise CandidateError("independent verifier must be distinct from every blind reviewer")
    if reviewer_session_ids & variant_session_ids:
        raise CandidateError("reviewer sessions must be independent from execution sessions")

    quality_medians = {
        variant: {dimension: _median(values) for dimension, values in dimensions.items()}
        for variant, dimensions in quality.items()
    }
    for dimension in ("correctness", "evidence", "completeness"):
        if quality_medians["vnext"][dimension] < quality_medians["legacy"][dimension]:
            hard_failures.append(f"blind {dimension} regression")
    for workload_id, values in preferences.items():
        if sum(value == "legacy" for value in values) > len(values) // 2:
            hard_failures.append(f"{workload_id}: majority blind correctness preference for legacy")

    all_rows = [row for values in metric_rows.values() for row in values]
    legacy_tokens = [row["legacy"]["tokens"] for row in all_rows]
    vnext_tokens = [row["vnext"]["tokens"] for row in all_rows]
    coordination_rows = [row for row in all_rows if workloads[row["workload_id"]]["coordination_heavy"] is True]
    if not coordination_rows:
        raise CandidateError("canary corpus must seal at least one coordination-heavy workload")
    legacy_completions = [row["legacy"]["completions"] for row in coordination_rows]
    vnext_completions = [row["vnext"]["completions"] for row in coordination_rows]
    legacy_token_median = _median(legacy_tokens)
    if legacy_token_median <= 0:
        raise CandidateError("legacy total-token median must be positive")
    legacy_mad = _median([abs(value - legacy_token_median) for value in legacy_tokens])
    noise_fraction = 1.4826 * legacy_mad / legacy_token_median
    token_reduction = 1.0 - _median(vnext_tokens) / legacy_token_median
    legacy_completion_median = _median(legacy_completions)
    if legacy_completion_median <= 0:
        raise CandidateError("legacy coordinator completion median must be positive")
    completion_reduction = 1.0 - _median(vnext_completions) / legacy_completion_median
    required_token_reduction = max(0.20, 2.0 * noise_fraction)
    paired_latency_ratios = {
        workload_id: [row["vnext"]["latency"] / row["legacy"]["latency"] for row in rows]
        for workload_id, rows in metric_rows.items()
    }
    latency_ratios = {
        workload_id: _median(ratios)
        for workload_id, ratios in paired_latency_ratios.items()
    }
    aggregate_latency_ratio = _median([
        ratio for ratios in paired_latency_ratios.values() for ratio in ratios
    ])
    performance_failures: list[str] = []
    if completion_reduction < 0.50:
        performance_failures.append("coordinator completion reduction below 50%")
    if token_reduction < required_token_reduction:
        performance_failures.append("token reduction below noise-adjusted threshold")
    for workload_id, ratio in latency_ratios.items():
        if ratio > 1.10:
            performance_failures.append(f"{workload_id}: latency ratio above 1.10")
    if aggregate_latency_ratio > 1.00:
        performance_failures.append("aggregate latency ratio above 1.00")
    failures = hard_failures + ([] if hard_failures else performance_failures)
    return {
        "schema_version": "agent-workflow.canary-evaluation.v1",
        "status": "pass" if not failures else "fail",
        "correctness_hard_gate": "pass" if not hard_failures else "fail",
        "performance_gate": "not_evaluated" if hard_failures else "pass" if not performance_failures else "fail",
        "failures": failures,
        "pair_count": len(all_rows),
        "quality_medians": quality_medians,
        "metrics": {
            "legacy_token_median": legacy_token_median,
            "vnext_token_median": _median(vnext_tokens),
            "legacy_token_p95": _p95(legacy_tokens),
            "vnext_token_p95": _p95(vnext_tokens),
            "noise_fraction": noise_fraction,
            "required_token_reduction": required_token_reduction,
            "actual_token_reduction": token_reduction,
            "legacy_completion_median": legacy_completion_median,
            "vnext_completion_median": _median(vnext_completions),
            "legacy_completion_p95": _p95(legacy_completions),
            "vnext_completion_p95": _p95(vnext_completions),
            "actual_completion_reduction": completion_reduction,
            "completion_density_workloads": sorted({row["workload_id"] for row in coordination_rows}),
            "legacy_latency_median": _median([row["legacy"]["latency"] for row in all_rows]),
            "vnext_latency_median": _median([row["vnext"]["latency"] for row in all_rows]),
            "legacy_latency_p95": _p95([row["legacy"]["latency"] for row in all_rows]),
            "vnext_latency_p95": _p95([row["vnext"]["latency"] for row in all_rows]),
            "latency_ratios_by_workload": latency_ratios,
            "aggregate_latency_ratio": aggregate_latency_ratio,
        },
        "run_seal_sha256": _sha256(run_seal_payload),
    }


def _create_or_verify_json(root: Path, relative_path: str, value: dict[str, Any]) -> Path:
    payload = _canonical_json(value) + b"\n"
    path = Path(root).joinpath(*_safe_relative_path(relative_path, "host result path").parts)
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise CandidateError(f"existing host result artifact drifted: {relative_path}")
        return path
    return create_once_bytes(root, relative_path, payload)


def _collect_subject_records(workspace: Path, core: dict[str, Any]) -> list[dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    pairs = core.get("pairs")
    if not isinstance(pairs, list):
        raise CandidateError("canary draft lacks pairs for subject qualification")
    for pair in pairs:
        variants = pair.get("variants") if isinstance(pair, dict) else None
        if not isinstance(variants, dict):
            raise CandidateError("canary draft variant graph is invalid")
        for variant in variants.values():
            checks = variant.get("hard_checks") if isinstance(variant, dict) else None
            if not isinstance(checks, dict):
                raise CandidateError("canary draft hidden proof graph is invalid")
            for check in checks.values():
                if not isinstance(check, dict):
                    raise CandidateError("canary draft hidden proof reference is invalid")
                payload = _bound_workspace_evidence(
                    workspace, check["evidence_ref"], check["evidence_sha256"], "subject qualification proof",
                )
                try:
                    proof = json.loads(payload)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise CandidateError("subject qualification proof is invalid JSON") from exc
                record = proof.get("qualification_record") if isinstance(proof, dict) else None
                record_sha = proof.get("qualification_record_sha256") if isinstance(proof, dict) else None
                if (
                    not isinstance(record, dict)
                    or record_sha != _sha256(_canonical_json(record) + b"\n")
                    or record_sha in records
                ):
                    raise CandidateError("subject qualification record is invalid or duplicated")
                records[record_sha] = record
    return [{"sha256": digest, "record": records[digest]} for digest in sorted(records)]


def _host_qualification_receipt(
    workspace: Path, run_seal: dict[str, Any], freeze: dict[str, Any], subject_records: list[dict[str, Any]],
) -> dict[str, Any]:
    freeze_sha256 = _sha256(Path(run_seal["freeze_path"]).read_bytes())
    key = (run_seal["candidate_bundle_sha256"], run_seal["runtime_bundle_sha256"], freeze_sha256)
    frozen_root = _verify_frozen_repository(workspace, freeze)
    commands = _QUALIFICATION_CACHE.get(key)
    if commands is None:
        script_root = frozen_root / "skills/agent-workflow/scripts"
        frozen_authority = {item["path"]: item["sha256"] for item in freeze["authority"]}
        commands = []
        for command_id, script_name in HOST_QUALIFICATION_COMMANDS.items():
            relative = f"skills/agent-workflow/scripts/{script_name}"
            script_sha256 = _sha256((script_root / script_name).read_bytes())
            if frozen_authority.get(relative) != script_sha256:
                raise CandidateError(f"host qualification validator drifted from freeze: {command_id}")
            observed = subprocess.run(
                [sys.executable, os.fspath(script_root / script_name)],
                cwd=frozen_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            record = {
                "command_id": command_id,
                "script_sha256": script_sha256,
                "exit_code": observed.returncode,
                "stdout_sha256": _sha256(observed.stdout),
                "stderr_sha256": _sha256(observed.stderr),
            }
            commands.append(record)
            if observed.returncode != 0:
                diagnostic = observed.stderr.decode("utf-8", errors="replace").strip()[-2000:]
                suffix = f": {diagnostic}" if diagnostic else ""
                raise CandidateError(f"host qualification command failed: {command_id}{suffix}")
        _verify_frozen_repository(workspace, freeze)
        _QUALIFICATION_CACHE[key] = commands
    core = {
        "schema_version": "agent-workflow.canary-host-qualification.v1",
        "candidate_bundle_sha256": run_seal["candidate_bundle_sha256"],
        "runtime_bundle_sha256": run_seal["runtime_bundle_sha256"],
        "freeze_sha256": freeze_sha256,
        "commands": commands,
        "hidden_check_commands": HIDDEN_QUALIFICATION_MAP,
        "subject_records": subject_records,
    }
    signed = {**core, "host_authority_id": run_seal["host_authority_id"]}
    return {
        **signed,
        "mac": _host_mac(_load_host_key(workspace, run_seal["host_authority_id"]), signed),
    }


def _verify_host_qualification(workspace: Path, value: Any, run_seal: dict[str, Any]) -> None:
    if (
        not isinstance(value, dict)
        or set(value) != {
            "schema_version", "candidate_bundle_sha256", "runtime_bundle_sha256", "freeze_sha256",
            "commands", "hidden_check_commands", "subject_records", "host_authority_id", "mac",
        }
        or value["schema_version"] != "agent-workflow.canary-host-qualification.v1"
        or value["candidate_bundle_sha256"] != run_seal["candidate_bundle_sha256"]
        or value["runtime_bundle_sha256"] != run_seal["runtime_bundle_sha256"]
        or value["freeze_sha256"] != _sha256(Path(run_seal["freeze_path"]).read_bytes())
        or value["hidden_check_commands"] != HIDDEN_QUALIFICATION_MAP
        or value["host_authority_id"] != run_seal["host_authority_id"]
        or not isinstance(value["commands"], list)
        or not isinstance(value["subject_records"], list)
        or not value["subject_records"]
    ):
        raise CandidateError("host qualification receipt schema or bundle drifted")
    signed = {key: item for key, item in value.items() if key != "mac"}
    if value["mac"] != _host_mac(_load_host_key(workspace, run_seal["host_authority_id"]), signed):
        raise CandidateError("host qualification receipt authority drifted")
    freeze = verify_executable_freeze(Path(run_seal["repository_root"]), Path(run_seal["freeze_path"]))
    script_root = _verify_frozen_repository(workspace, freeze) / "skills/agent-workflow/scripts"
    observed_ids: set[str] = set()
    for item in value["commands"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"command_id", "script_sha256", "exit_code", "stdout_sha256", "stderr_sha256"}
            or item["command_id"] not in HOST_QUALIFICATION_COMMANDS
            or item["command_id"] in observed_ids
            or item["exit_code"] != 0
        ):
            raise CandidateError("host qualification command receipt drifted")
        script = script_root / HOST_QUALIFICATION_COMMANDS[item["command_id"]]
        if item["script_sha256"] != _sha256(script.read_bytes()):
            raise CandidateError("host qualification validator source drifted")
        observed_ids.add(item["command_id"])
    if observed_ids != set(HOST_QUALIFICATION_COMMANDS):
        raise CandidateError("host qualification command set is incomplete")
    subject_ids: set[str] = set()
    for item in value["subject_records"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"sha256", "record"}
            or not isinstance(item["record"], dict)
            or item["sha256"] != _sha256(_canonical_json(item["record"]) + b"\n")
            or item["sha256"] in subject_ids
        ):
            raise CandidateError("host subject qualification record drifted")
        subject_ids.add(item["sha256"])
    if [item["sha256"] for item in value["subject_records"]] != sorted(subject_ids):
        raise CandidateError("host subject qualification records are not canonical")


def _load_or_create_host_qualification(
    workspace: Path,
    run_seal: dict[str, Any],
    freeze: dict[str, Any],
    subject_records: list[dict[str, Any]],
    relative_ref: str,
) -> dict[str, Any]:
    relative = _safe_relative_path(relative_ref, "host qualification ref").as_posix()
    path = workspace.joinpath(*PurePosixPath(relative).parts)

    def load_existing() -> dict[str, Any]:
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(workspace):
            raise CandidateError("existing host qualification receipt is unsafe")
        payload = path.read_bytes()
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CandidateError("existing host qualification receipt is invalid JSON") from exc
        if payload != _canonical_json(value) + b"\n":
            raise CandidateError("existing host qualification receipt is not canonical")
        _verify_host_qualification(workspace, value, run_seal)
        if value["subject_records"] != subject_records:
            raise CandidateError("existing host qualification subject set drifted")
        return value

    if path.exists() or path.is_symlink():
        return load_existing()
    candidate = _host_qualification_receipt(workspace, run_seal, freeze, subject_records)
    with authority_transaction(workspace):
        if path.exists() or path.is_symlink():
            return load_existing()
        _create_or_verify_json(workspace, relative, candidate)
    return candidate


def seal_results(
    workspace: Path,
    *,
    run_seal_path: Path,
    draft_path: Path,
    seal_output: str,
    results_output: str,
) -> dict[str, Any]:
    """Host-only: replay a draft, then HMAC-seal its complete transitive evidence graph."""

    workspace = Path(workspace).resolve(strict=True)
    run_seal_path = Path(run_seal_path)
    draft_path = Path(draft_path)
    try:
        core = json.loads(draft_path.read_bytes())
        run_seal = _sealed_run(json.loads(run_seal_path.read_bytes()))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateError("host result draft is invalid JSON") from exc
    if draft_path.read_bytes() != _canonical_json(core) + b"\n":
        raise CandidateError("host result draft must use canonical JSON bytes")
    freeze_path = Path(run_seal["freeze_path"])
    repository_root = Path(run_seal["repository_root"])
    freeze = verify_executable_freeze(repository_root, freeze_path)
    if (
        _sha256(freeze_path.read_bytes()) != run_seal["executable_freeze_sha256"]
        or freeze["semantic_bundle_sha256"] != run_seal["candidate_bundle_sha256"]
        or freeze["runtime_bundle_sha256"] != run_seal["runtime_bundle_sha256"]
    ):
        raise CandidateError("host result sealing bundle drifted from the pre-result freeze")
    evaluation = evaluate_results(run_seal_path, draft_path, require_host_seal=False)
    if _safe_relative_path(results_output, "host results output").as_posix() != run_seal["results_ref"]:
        raise CandidateError("host results output does not match the pre-result run seal")
    qualification_ref = "host/qualification.json"
    qualification = _load_or_create_host_qualification(
        workspace,
        run_seal,
        freeze,
        _collect_subject_records(workspace, core),
        qualification_ref,
    )
    qualification_records = {
        item["command_id"]: _sha256(_canonical_json(item) + b"\n")
        for item in qualification["commands"]
    }
    qualification_records.update({item["sha256"]: item["record"] for item in qualification["subject_records"]})
    qualified_evaluation = evaluate_results(
        run_seal_path,
        draft_path,
        require_host_seal=False,
        qualification_records=qualification_records,
    )
    if qualified_evaluation != evaluation:
        raise CandidateError("qualification-bound replay drifted from preliminary replay")
    qualification_payload = _canonical_json(qualification) + b"\n"
    qualification_sha256 = _sha256(qualification_payload)
    signed = {
        "schema_version": "agent-workflow.canary-host-evidence-seal.v1",
        "run_seal_sha256": core["run_seal_sha256"],
        "results_core_sha256": _sha256(draft_path.read_bytes()),
        "host_authority_id": run_seal["host_authority_id"],
        "host_qualification_sha256": qualification_sha256,
    }
    host_seal = {
        **signed,
        "mac": _host_mac(_load_host_key(workspace, run_seal["host_authority_id"]), signed),
    }
    final = {
        **core,
        "host_evidence_seal_ref": _safe_relative_path(seal_output, "host evidence seal output").as_posix(),
        "host_evidence_seal_sha256": _sha256(_canonical_json(host_seal) + b"\n"),
        "host_qualification_ref": qualification_ref,
        "host_qualification_sha256": qualification_sha256,
    }
    with authority_transaction(workspace):
        seal_path = _create_or_verify_json(workspace, seal_output, host_seal)
        results_path = _create_or_verify_json(workspace, results_output, final)
    verified = evaluate_results(run_seal_path, results_path)
    if verified != qualified_evaluation:
        raise CandidateError("host-sealed evaluation drifted from pre-seal replay")
    return {
        "status": verified["status"],
        "results_ref": results_path.relative_to(workspace).as_posix(),
        "results_sha256": _sha256(results_path.read_bytes()),
        "host_evidence_seal_ref": seal_path.relative_to(workspace).as_posix(),
        "host_evidence_seal_sha256": _sha256(seal_path.read_bytes()),
        "host_qualification_ref": qualification_ref,
        "host_qualification_sha256": qualification_sha256,
    }


def prepare(workspace: Path, brief_path: str, output_path: str) -> dict[str, Any]:
    workspace = workspace.resolve(strict=True)
    brief = validate_brief(_read_workspace_json(workspace, brief_path))
    instruction_bytes = CANDIDATE_INSTRUCTIONS.read_bytes()
    instruction_text = instruction_bytes.decode("utf-8")
    brief_bytes = _canonical_json(brief)
    instruction_sha = _sha256(instruction_bytes)
    brief_sha = _sha256(brief_bytes)
    message = (
        instruction_text.rstrip()
        + "\n\n## Sealed Workflow Brief\n\n"
        + f"Brief digest: `{brief_sha}`\n\n"
        + "```json\n"
        + brief_bytes.decode("utf-8")
        + "\n```\n"
    )
    packet = {
        "schema_version": PACKET_SCHEMA,
        "candidate_instruction_sha256": instruction_sha,
        "workflow_brief_sha256": brief_sha,
        "claim": "candidate_non_production",
        "spawn": {
            "task_name": "agent_workflow_vnext_orchestrator",
            "fork_turns": "none",
            "message": message,
        },
    }
    output = create_once_json(workspace, output_path, packet)
    output_bytes = output.read_bytes()
    return {
        "packet_ref": output.relative_to(workspace).as_posix(),
        "packet_sha256": _sha256(output_bytes),
        "spawn": {
            "task_name": packet["spawn"]["task_name"],
            "fork_turns": packet["spawn"]["fork_turns"],
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--workspace", type=Path, required=True)
    prepare_parser.add_argument("--brief", required=True)
    prepare_parser.add_argument("--output", required=True)
    freeze_parser = sub.add_parser("verify-freeze")
    freeze_parser.add_argument("--repo", type=Path, required=True)
    freeze_parser.add_argument("--freeze", type=Path, required=True)
    build_freeze_parser = sub.add_parser("create-freeze")
    build_freeze_parser.add_argument("--repo", type=Path, required=True)
    build_freeze_parser.add_argument("--output", type=Path, required=True)
    seal_parser = sub.add_parser("seal-run")
    seal_parser.add_argument("--workspace", type=Path, required=True)
    seal_parser.add_argument("--repo", type=Path, required=True)
    seal_parser.add_argument("--freeze", type=Path, required=True)
    seal_parser.add_argument("--source", type=Path, required=True)
    seal_parser.add_argument("--results-ref", required=True)
    seal_parser.add_argument("--output", required=True)
    result_seal_parser = sub.add_parser("seal-results")
    result_seal_parser.add_argument("--workspace", type=Path, required=True)
    result_seal_parser.add_argument("--run-seal", type=Path, required=True)
    result_seal_parser.add_argument("--draft", type=Path, required=True)
    result_seal_parser.add_argument("--seal-output", required=True)
    result_seal_parser.add_argument("--results-output", required=True)
    evaluate_parser = sub.add_parser("evaluate")
    evaluate_parser.add_argument("--workspace", type=Path, required=True)
    evaluate_parser.add_argument("--run-seal", type=Path, required=True)
    evaluate_parser.add_argument("--results", type=Path, required=True)
    evaluate_parser.add_argument("--output", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "prepare":
            summary = prepare(args.workspace, args.brief, args.output)
        elif args.command == "verify-freeze":
            freeze = verify_executable_freeze(args.repo, args.freeze)
            summary = {
                "status": "frozen",
                "runtime_bundle_sha256": freeze["runtime_bundle_sha256"],
                "semantic_bundle_sha256": freeze["semantic_bundle_sha256"],
                "file_count": len(freeze["files"]),
                "authority_file_count": len(freeze["authority"]),
            }
        elif args.command == "create-freeze":
            summary = build_promotion_freeze(args.repo, args.output)
        elif args.command == "seal-run":
            summary = seal_run(
                args.workspace,
                repo=args.repo,
                freeze_path=args.freeze,
                source_path=args.source,
                results_ref=args.results_ref,
                output_path=args.output,
            )
        elif args.command == "seal-results":
            summary = seal_results(
                args.workspace,
                run_seal_path=args.run_seal,
                draft_path=args.draft,
                seal_output=args.seal_output,
                results_output=args.results_output,
            )
        elif args.command == "evaluate":
            evaluation = evaluate_results(args.run_seal, args.results)
            output = create_once_json(args.workspace.resolve(strict=True), args.output, evaluation)
            summary = {
                "status": evaluation["status"],
                "evaluation_ref": output.relative_to(args.workspace.resolve()).as_posix(),
                "evaluation_sha256": _sha256(output.read_bytes()),
            }
        else:  # pragma: no cover - argparse owns this branch
            raise CandidateError(f"unknown command: {args.command}")
    except (ArtifactError, CandidateError, FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
