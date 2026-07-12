#!/usr/bin/env python3
"""Slice 8 pre-result seal and replay-authoritative paired canary tests."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from run_vnext_canary import (  # noqa: E402
    CANARY_FIXTURES,
    CORE_RUNTIME_FILES,
    CandidateError,
    HIDDEN_EVIDENCE_TYPES,
    HIDDEN_STATIC_FACTS,
    HIDDEN_QUALIFICATION_MAP,
    HOST_QUALIFICATION_COMMANDS,
    REQUIRED_PROMOTION_EXECUTABLES,
    REQUIRED_PROMOTION_AUTHORITY,
    _canonical_json,
    _control_root,
    _collect_subject_records,
    _ensure_host_key,
    _explicit_user_prompt,
    _frozen_repository_root,
    _host_mac,
    _materialize_frozen_repository,
    _assert_session_denies_canonical_stores,
    _replay_variant_receipt,
    _validate_hidden_proof,
    _validate_p2_detail,
    _sha256,
    _subject_qualification_record,
    build_promotion_freeze,
    evaluate_results,
    expected_pair_order,
    expected_label_map,
    seal_run,
    seal_results,
    verify_executable_freeze,
)
import run_vnext_canary  # noqa: E402
from vnext_accounting import SUPPORTED_APP_SCHEMA_SHA256, SUPPORTED_CODEX_VERSION  # noqa: E402


def write_json(path: Path, value: object) -> tuple[str, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_canonical_json(value) + b"\n")
    return path.as_posix(), _sha256(path.read_bytes())


def relative_evidence(root: Path, path: Path) -> tuple[str, str]:
    return path.relative_to(root).as_posix(), _sha256(path.read_bytes())


def canonical_session_root(root: Path) -> Path:
    path = root.resolve().parent / f".canary-session-store-{hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:12]}"
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def canonical_app_root(root: Path) -> Path:
    path = root.resolve().parent / f".canary-app-store-{hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:12]}"
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def canonical_snapshot_root(root: Path) -> Path:
    path = root.resolve().parent / f".canary-snapshot-store-{hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:12]}"
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def canonical_artifact_root(root: Path) -> Path:
    path = root.resolve().parent / f".canary-artifact-store-{hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:12]}"
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def current_promotion_freeze(root: Path) -> tuple[Path, dict[str, Any]]:
    repo = SCRIPT_DIR.parents[2]
    files = []
    executable_digests: dict[str, str] = {}
    for relative in sorted(REQUIRED_PROMOTION_EXECUTABLES):
        payload = (repo / relative).read_bytes()
        executable_digests[relative] = _sha256(payload)
        files.append({"path": relative, "sha256": executable_digests[relative], "bytes": len(payload)})
    authority = []
    authority_manifest = []
    for relative in sorted(REQUIRED_PROMOTION_AUTHORITY):
        payload = (repo / relative).read_bytes()
        digest = _sha256(payload)
        authority.append({"path": relative, "sha256": digest, "bytes": len(payload)})
        authority_manifest.append({"path": relative, "sha256": digest})
    freeze = {
        "schema_version": "agent-workflow.promotion-freeze.v1",
        "runtime_bundle_sha256": _sha256(_canonical_json([
            {"path": name, "sha256": executable_digests[f"skills/agent-workflow/scripts/{name}"]}
            for name in CORE_RUNTIME_FILES
        ]) + b"\n"),
        "semantic_bundle_sha256": _sha256(_canonical_json({
            "executables": [{"path": relative, "sha256": executable_digests[relative]} for relative in sorted(REQUIRED_PROMOTION_EXECUTABLES)],
            "authority": authority_manifest,
        }) + b"\n"),
        "files": files,
        "authority": authority,
    }
    path = root.resolve().parent / f".canary-freeze-{hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:12]}.json"
    path.write_bytes(_canonical_json(freeze) + b"\n")
    return path, freeze


def run_seal(
    bundle: str, *, label_map_sha: str, rubric_sha: str, evidence_sha: dict[str, str],
    runtime_bundle: str | None = None,
    freeze_sha: str = "sha256:" + "f" * 64,
) -> dict[str, object]:
    corpus = json.loads((CANARY_FIXTURES / "corpus.v1.json").read_text())
    return {
        "schema_version": "agent-workflow.canary-run-seal.v1",
        "fixture_seal_sha256": _sha256((CANARY_FIXTURES / "seal.v1.json").read_bytes()),
        "executable_freeze_sha256": freeze_sha,
        "candidate_bundle_sha256": bundle,
        "runtime_bundle_sha256": runtime_bundle or bundle,
        "repository_fixture_ref": "authority/repository-fixture.json",
        "repository_fixture_sha256": evidence_sha["repository"],
        "codex_identity_ref": "authority/codex-identity.json",
        "codex_identity_sha256": evidence_sha["codex"],
        "codex_version": SUPPORTED_CODEX_VERSION,
        "app_protocol_schema_sha256": SUPPORTED_APP_SCHEMA_SHA256,
        "host_profile_ref": "authority/host-profile.json",
        "host_profile_sha256": evidence_sha["host"],
        "top_model": "gpt-5.6-sol",
        "worker_model": "gpt-5.6-terra",
        "reasoning_effort": "xhigh",
        "capacity_profile_ref": "authority/capacity-profile.json",
        "capacity_profile_sha256": evidence_sha["capacity"],
        "paired_order_seed": _sha256(f"{corpus['corpus_id']}|{bundle}".encode()),
        "blind_label_map_ref": "blind-label-map.json",
        "blind_label_map_sha256": label_map_sha,
        "rubric_ref": "rubric.json",
        "rubric_sha256": rubric_sha,
        "sealed_at": "2026-07-11T23:00:00+00:00",
        "results_ref": "results.json",
    }


def write_authority(
    root: Path,
    bundle: str,
    *,
    runtime_bundle: str | None = None,
    use_current_freeze: bool = True,
) -> dict[str, object]:
    if use_current_freeze:
        freeze_path, freeze = current_promotion_freeze(root)
        bundle = freeze["semantic_bundle_sha256"]
        runtime_bundle = freeze["runtime_bundle_sha256"]
        freeze_sha = _sha256(freeze_path.read_bytes())
    else:
        freeze_path = root.resolve().parent / "fixture-freeze.json"
        freeze_sha = "sha256:" + "f" * 64
    corpus = json.loads((CANARY_FIXTURES / "corpus.v1.json").read_text())
    pair_ids = {
        f"{workload['id']}:{trial}"
        for workload in corpus["workloads"]
        for trial in range(1, 6)
    }
    derived = {"paired_order_seed": _sha256(f"{corpus['corpus_id']}|{bundle}".encode()), "candidate_bundle_sha256": bundle}
    label_map = {
        "schema_version": "agent-workflow.canary-blind-label-map.v1",
        "pairs": expected_label_map(derived, pair_ids),
    }
    rubric = {
        "schema_version": "agent-workflow.canary-rubric.v1",
        "dimensions": ["correctness", "evidence", "completeness"],
        "score_range": [0, 4],
    }
    write_json(root / "blind-label-map.json", label_map)
    write_json(root / "rubric.json", rubric)
    authority = {
        "repository": {"schema_version": "agent-workflow.canary-repository-fixture.v1", "head": "fixture-head", "dirty": False, "snapshot_store_root": str(canonical_snapshot_root(root))},
        "codex": {"schema_version": "agent-workflow.canary-codex-identity.v1", "version": SUPPORTED_CODEX_VERSION, "binary_sha256": "sha256:" + "4" * 64, "session_store_root": str(canonical_session_root(root))},
        "host": {"schema_version": "agent-workflow.canary-host-profile.v1", "os": "fixture", "arch": "fixture", "app_event_store_root": str(canonical_app_root(root)), "artifact_store_root": str(canonical_artifact_root(root))},
        "capacity": {"schema_version": "agent-workflow.canary-capacity-profile.v1", "max_parallel": 4},
    }
    evidence_sha: dict[str, str] = {}
    for name, value in authority.items():
        path = root / "authority" / f"{name.replace('repository', 'repository-fixture').replace('codex', 'codex-identity').replace('host', 'host-profile').replace('capacity', 'capacity-profile')}.json"
        write_json(path, value)
        evidence_sha[name] = _sha256(path.read_bytes())
    seal = run_seal(
        bundle,
        label_map_sha=_sha256((root / "blind-label-map.json").read_bytes()),
        rubric_sha=_sha256((root / "rubric.json").read_bytes()),
        evidence_sha=evidence_sha,
        runtime_bundle=runtime_bundle,
        freeze_sha=freeze_sha,
    )
    _, seal["host_authority_id"] = _ensure_host_key(root.resolve())
    if use_current_freeze:
        _materialize_frozen_repository(root.resolve(), SCRIPT_DIR.parents[2], freeze)
    seal["freeze_path"] = str(freeze_path)
    seal["repository_root"] = str(SCRIPT_DIR.parents[2] if use_current_freeze else root.resolve().parent)
    return seal


def write_raw_session(
    root: Path,
    ref: str,
    *,
    session_id: str,
    model: str,
    effort: str,
    duration: float,
    start_offset: float = 0.0,
    completions: int = 1,
    total_tokens: int = 1,
    terminal_message: str = "done",
    input_message: str | None = None,
    semantic_completions: bool = False,
    semantic_command: str = "python3 workflow_runtime.py run-phase",
    base_time: datetime | None = None,
    host_preamble: list[dict[str, str]] | None = None,
) -> tuple[str, str]:
    start = (base_time or datetime(2026, 7, 12, tzinfo=timezone.utc)) + timedelta(seconds=start_offset)
    turn_id = f"turn-{session_id}"
    rows: list[dict[str, Any]] = [
        {"type": "session_meta", "payload": {"id": session_id, "timestamp": start.isoformat()}},
        {"type": "turn_context", "payload": {
            "turn_id": turn_id,
            "model": model,
            "effort": effort,
            "workspace_roots": [str(root.resolve())],
            "sandbox_policy": {"type": "read-only"},
            "permission_profile": {
                "type": "managed",
                "network": "restricted",
                "file_system": {
                    "type": "restricted",
                    "entries": [
                        {"path": {"type": "special", "value": {"kind": "minimal"}}, "access": "read"},
                        {"path": {"type": "path", "path": str(root.resolve())}, "access": "read"},
                    ],
                },
            },
        }},
    ]
    if host_preamble is not None:
        rows.append({
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": host_preamble},
        })
    if input_message is not None:
        rows.append({"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": input_message}]}})
    for index in range(completions):
        if semantic_completions:
            call_id = f"call-{session_id}-{index + 1}"
            rows.extend([
                {"type": "response_item", "payload": {"type": "custom_tool_call", "call_id": call_id, "name": "functions.exec", "input": json.dumps({"cmd": semantic_command})}},
                {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": call_id, "output": "terminal"}},
            ])
        cumulative = total_tokens if index == completions - 1 else max(1, total_tokens * (index + 1) // completions)
        prior = 0 if index == 0 else (total_tokens * index // completions)
        amount = cumulative - prior
        rows.append({
            "type": "event_msg",
            "payload": {"type": "token_count", "info": {
                "total_token_usage": {
                    "input_tokens": cumulative, "cached_input_tokens": 0,
                    "output_tokens": 0, "reasoning_output_tokens": 0, "total_tokens": cumulative,
                },
                "last_token_usage": {
                    "input_tokens": amount, "cached_input_tokens": 0,
                    "output_tokens": 0, "reasoning_output_tokens": 0, "total_tokens": amount,
                },
            }},
        })
    rows.append({
        "type": "event_msg",
        "timestamp": (start + timedelta(seconds=duration)).isoformat(),
        "payload": {"type": "task_complete", "turn_id": turn_id, "last_agent_message": terminal_message},
    })
    path = root / ref
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows))
    canonical = canonical_session_root(root) / ref
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_bytes(path.read_bytes())
    return relative_evidence(root, path)


def write_app_export(
    root: Path,
    prefix: str,
    *,
    session_id: str,
    run_seal_sha: str,
    runtime_bundle_sha: str,
    raw_ref: str,
    raw_sha: str,
    completions: int,
    total_tokens: int,
    turn_id: str | None = None,
    prior_tokens: int = 0,
) -> tuple[str, str, str, str]:
    thread_id = f"thread-{session_id}"
    thread_id = session_id
    turn_ids = [turn_id or f"turn-{session_id}"]
    turn_id = turn_ids[0]
    started = {"id": turn_id, "status": "inProgress", "items": []}
    completed = {"id": turn_id, "status": "completed", "items": []}
    events: list[dict[str, Any]] = [
        {"method": "turn/started", "params": {"threadId": thread_id, "turn": started}},
    ]
    for index in range(completions):
        cumulative = total_tokens if index == completions - 1 else max(1, total_tokens * (index + 1) // completions)
        prior = 0 if index == 0 else total_tokens * index // completions
        amount = cumulative - prior
        total_usage = {
            "cachedInputTokens": 0, "inputTokens": prior_tokens + cumulative, "outputTokens": 0,
            "reasoningOutputTokens": 0, "totalTokens": prior_tokens + cumulative,
        }
        last_usage = {
            "cachedInputTokens": 0, "inputTokens": amount, "outputTokens": 0,
            "reasoningOutputTokens": 0, "totalTokens": amount,
        }
        events.append({
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": thread_id, "turnId": turn_id,
                "tokenUsage": {"last": last_usage, "total": total_usage},
            },
        })
    events.append({"method": "turn/completed", "params": {"threadId": thread_id, "turn": completed}})
    app_path = root / f"{prefix}/app-events.jsonl"
    app_path.parent.mkdir(parents=True, exist_ok=True)
    app_path.write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in events))
    canonical_app = canonical_app_root(root) / f"{prefix}/app-events.jsonl"
    canonical_app.parent.mkdir(parents=True, exist_ok=True)
    canonical_app.write_bytes(app_path.read_bytes())
    app_ref, app_sha = relative_evidence(root, app_path)
    export = {
        "schema_version": "agent-workflow.canary-host-export.v1",
        "run_seal_sha256": run_seal_sha,
        "session_id": session_id,
        "source_session_store_ref": raw_ref,
        "source_prefix_sha256": raw_sha,
        "raw_session_ref": raw_ref,
        "raw_session_sha256": raw_sha,
        "transport": "app_server",
        "event_ref": app_ref,
        "event_sha256": app_sha,
        "exporter_bundle_sha256": runtime_bundle_sha,
        "thread_id": thread_id,
        "turn_ids": turn_ids,
        "canonical_raw_path": str(canonical_session_root(root) / raw_ref),
        "canonical_raw_sha256": raw_sha,
        "canonical_event_path": str(canonical_app),
        "canonical_event_sha256": app_sha,
    }
    export_path = root / f"{prefix}/host-export.json"
    write_json(export_path, export)
    export_ref, export_sha = relative_evidence(root, export_path)
    return app_ref, app_sha, export_ref, export_sha


def write_exec_export(
    root: Path,
    prefix: str,
    *,
    session_id: str,
    turn_id: str,
    run_seal_sha: str,
    runtime_bundle_sha: str,
    raw_ref: str,
    raw_sha: str,
    total_tokens: int,
) -> tuple[str, str, str, str]:
    events = [
        {"type": "thread.started", "thread_id": session_id},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
        {"type": "turn.completed", "usage": {
            "input_tokens": total_tokens,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_output_tokens": 0,
        }},
    ]
    event_path = root / f"{prefix}/exec-events.jsonl"
    event_path.parent.mkdir(parents=True, exist_ok=True)
    event_path.write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in events))
    canonical_event = canonical_app_root(root) / f"{prefix}/exec-events.jsonl"
    canonical_event.parent.mkdir(parents=True, exist_ok=True)
    canonical_event.write_bytes(event_path.read_bytes())
    event_ref, event_sha = relative_evidence(root, event_path)
    export = {
        "schema_version": "agent-workflow.canary-host-export.v1",
        "run_seal_sha256": run_seal_sha,
        "session_id": session_id,
        "source_session_store_ref": raw_ref,
        "source_prefix_sha256": raw_sha,
        "raw_session_ref": raw_ref,
        "raw_session_sha256": raw_sha,
        "transport": "codex_exec_jsonl",
        "event_ref": event_ref,
        "event_sha256": event_sha,
        "exporter_bundle_sha256": runtime_bundle_sha,
        "thread_id": session_id,
        "turn_ids": [turn_id],
        "canonical_raw_path": str(canonical_session_root(root) / raw_ref),
        "canonical_raw_sha256": raw_sha,
        "canonical_event_path": str(canonical_event),
        "canonical_event_sha256": event_sha,
    }
    export_path = root / f"{prefix}/host-export.json"
    write_json(export_path, export)
    export_ref, export_sha = relative_evidence(root, export_path)
    return event_ref, event_sha, export_ref, export_sha


def write_session_launch(
    root: Path,
    ref: str,
    *,
    run_seal_sha: str,
    pair_id: str,
    variant: str,
    task_id: str,
    role: str,
    model: str,
    reasoning_effort: str,
    transport: str,
    prompt: str,
    host_preamble_sha256: str = "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    verification_subject: dict[str, str] | None = None,
) -> tuple[str, str, str, str]:
    packet = {
        "schema_version": "agent-workflow.canary-session-launch.v1",
        "run_seal_sha256": run_seal_sha,
        "pair_id": pair_id,
        "variant": variant,
        "task_id": task_id,
        "role": role,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "transport": transport,
        "prompt": prompt,
        "host_preamble_sha256": host_preamble_sha256,
        "verification_subject": verification_subject,
    }
    path = root / ref
    write_json(path, packet)
    launch_ref, launch_sha = relative_evidence(root, path)
    canonical = canonical_artifact_root(root) / launch_ref
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_bytes(path.read_bytes())
    return launch_ref, launch_sha, str(canonical), launch_sha


def write_variant(
    root: Path,
    seal: dict[str, object],
    run_seal_sha: str,
    workload: dict[str, Any],
    trial: int,
    variant: str,
    *,
    completions: int,
    tokens: int,
    latency: float,
    start_offset: float,
    repository_snapshot_ref: str,
    repository_snapshot_sha256: str,
    raw_completion_events: int | None = None,
    failing_check: str | None = None,
    worker_transport: str = "app_server",
    host_preamble: list[dict[str, str]] | None = None,
) -> tuple[dict[str, Any], list[str], str]:
    pair_id = f"{workload['id']}:{trial}"
    prefix = f"evidence/{workload['id']}/{trial}/{variant}"
    worker_count = int(workload["minimum_worker_sessions"])
    worker_tokens = 100
    verifier_tokens = 50 if "AW-H008" in workload["hard_check_ids"] and failing_check != "AW-H008" else 0
    coordinator_tokens = tokens - worker_count * worker_tokens - verifier_tokens
    if coordinator_tokens <= 0:
        raise AssertionError("fixture token budget cannot cover required workers")
    terminal_output = _canonical_json({"pair_id": pair_id, "variant": variant, "answer": "verified"}).decode()
    sealed_at = datetime.fromisoformat(str(seal["sealed_at"]).replace("Z", "+00:00"))
    base_time = max(datetime(2026, 7, 12, tzinfo=timezone.utc), sealed_at + timedelta(seconds=1))
    host_preamble_sha = _sha256(_canonical_json(host_preamble) + b"\n") if host_preamble is not None else _sha256(b"")
    integration_after_sha = _sha256(f"{pair_id}:{variant}:integrated".encode())
    integration_completed_at = (base_time + timedelta(seconds=start_offset + latency)).isoformat()
    coordinator_prompt = _canonical_json({"pair_id": pair_id, "variant": variant, "task": "coordinate"}).decode()
    coordinator_launch = write_session_launch(
        root, f"{prefix}/coordinator-launch.json",
        run_seal_sha=run_seal_sha, pair_id=pair_id, variant=variant, task_id="coordinator",
        role="coordinator", model=str(seal["top_model"]), reasoning_effort=str(seal["reasoning_effort"]),
        transport="app_server", prompt=coordinator_prompt, host_preamble_sha256=host_preamble_sha,
    )
    coordinator_ref, coordinator_sha = write_raw_session(
        root,
        f"{prefix}/coordinator.jsonl",
        session_id=f"{workload['id']}-{trial}-{variant}-coordinator",
        model=str(seal["top_model"]),
        effort=str(seal["reasoning_effort"]),
        duration=latency,
        start_offset=start_offset,
        completions=raw_completion_events or completions,
        total_tokens=coordinator_tokens,
        terminal_message=terminal_output,
        input_message=coordinator_prompt,
        semantic_completions=True,
        semantic_command=(
            "python3 workflow_runtime.py seal-accounting"
            if failing_check == "AW-H003"
            else "python3 workflow_runtime.py run-phase"
        ),
        base_time=base_time,
        host_preamble=host_preamble,
    )
    coordinator_app_ref, coordinator_app_sha, coordinator_export_ref, coordinator_export_sha = write_app_export(
        root, f"{prefix}/coordinator-export",
        session_id=f"{workload['id']}-{trial}-{variant}-coordinator",
        run_seal_sha=run_seal_sha,
        runtime_bundle_sha=str(seal["runtime_bundle_sha256"]),
        raw_ref=coordinator_ref,
        raw_sha=coordinator_sha,
        completions=completions,
        total_tokens=coordinator_tokens,
    )
    worker_sessions: list[dict[str, str]] = []
    for worker_index in range(1, worker_count + 1):
        worker_session_id = f"{workload['id']}-{trial}-{variant}-worker-{worker_index}"
        worker_task_id = f"worker-{worker_index}"
        worker_prompt = _canonical_json({"pair_id": pair_id, "variant": variant, "task": worker_task_id}).decode()
        worker_launch = write_session_launch(
            root, f"{prefix}/{worker_task_id}-launch.json",
            run_seal_sha=run_seal_sha, pair_id=pair_id, variant=variant, task_id=worker_task_id,
            role="worker", model=str(seal["worker_model"]), reasoning_effort=str(seal["reasoning_effort"]),
            transport=worker_transport, prompt=worker_prompt, host_preamble_sha256=host_preamble_sha,
        )
        worker_ref, worker_sha = write_raw_session(
            root,
            f"{prefix}/worker-{worker_index}.jsonl",
            session_id=worker_session_id,
            model=str(seal["worker_model"]),
            effort=str(seal["reasoning_effort"]),
            duration=max(0.1, latency - 0.1),
            start_offset=start_offset,
            total_tokens=worker_tokens,
            input_message=worker_prompt,
            base_time=base_time,
            host_preamble=host_preamble,
        )
        if worker_transport == "app_server":
            event_ref, event_sha, export_ref, export_sha = write_app_export(
                root, f"{prefix}/worker-{worker_index}-export",
                session_id=worker_session_id,
                run_seal_sha=run_seal_sha,
                runtime_bundle_sha=str(seal["runtime_bundle_sha256"]),
                raw_ref=worker_ref,
                raw_sha=worker_sha,
                completions=1,
                total_tokens=worker_tokens,
            )
        elif worker_transport == "codex_exec_jsonl":
            event_ref, event_sha, export_ref, export_sha = write_exec_export(
                root, f"{prefix}/worker-{worker_index}-export",
                session_id=worker_session_id,
                turn_id=f"turn-{worker_session_id}",
                run_seal_sha=run_seal_sha,
                runtime_bundle_sha=str(seal["runtime_bundle_sha256"]),
                raw_ref=worker_ref,
                raw_sha=worker_sha,
                total_tokens=worker_tokens,
            )
        else:
            raise AssertionError("unsupported fixture worker transport")
        worker_sessions.append({
            "role": "worker", "task_id": worker_task_id,
            "launch_ref": worker_launch[0], "launch_sha256": worker_launch[1],
            "canonical_launch_path": worker_launch[2], "canonical_launch_sha256": worker_launch[3],
            "ref": worker_ref, "sha256": worker_sha,
            "event_ref": event_ref, "event_sha256": event_sha,
            "export_ref": export_ref, "export_sha256": export_sha,
            "continuations": [],
        })
    verifier_sessions: list[dict[str, str]] = []
    if verifier_tokens:
        verifier_session_id = f"{workload['id']}-{trial}-{variant}-verifier"
        verifier_prompt = _canonical_json({"pair_id": pair_id, "variant": variant, "task": "verify"}).decode()
        verification_subject = {
            "integration_after_sha256": integration_after_sha,
            "output_sha256": _sha256(terminal_output.encode()),
        }
        verifier_start_offset = start_offset + latency * (0.999 if failing_check == "AW-H008-early" else 1.001)
        verifier_launch = write_session_launch(
            root, f"{prefix}/verifier-launch.json", run_seal_sha=run_seal_sha,
            pair_id=pair_id, variant=variant, task_id="verifier", role="verifier",
            model=str(seal["top_model"]), reasoning_effort=str(seal["reasoning_effort"]),
            transport="app_server", prompt=verifier_prompt, host_preamble_sha256=host_preamble_sha,
            verification_subject=verification_subject,
        )
        verifier_ref, verifier_sha = write_raw_session(
            root, f"{prefix}/verifier.jsonl", session_id=verifier_session_id,
            model=str(seal["top_model"]), effort=str(seal["reasoning_effort"]),
            duration=max(0.0001, latency * 0.001), start_offset=verifier_start_offset,
            total_tokens=verifier_tokens, input_message=verifier_prompt,
            terminal_message=_canonical_json({"status": "approved", **verification_subject}).decode(), base_time=base_time,
            host_preamble=host_preamble,
        )
        verifier_event_ref, verifier_event_sha, verifier_export_ref, verifier_export_sha = write_app_export(
            root, f"{prefix}/verifier-export", session_id=verifier_session_id,
            run_seal_sha=run_seal_sha, runtime_bundle_sha=str(seal["runtime_bundle_sha256"]),
            raw_ref=verifier_ref, raw_sha=verifier_sha, completions=1, total_tokens=verifier_tokens,
        )
        verifier_sessions.append({
            "role": "verifier", "task_id": "verifier",
            "launch_ref": verifier_launch[0], "launch_sha256": verifier_launch[1],
            "canonical_launch_path": verifier_launch[2], "canonical_launch_sha256": verifier_launch[3],
            "ref": verifier_ref, "sha256": verifier_sha,
            "event_ref": verifier_event_ref, "event_sha256": verifier_event_sha,
            "export_ref": verifier_export_ref, "export_sha256": verifier_export_sha,
            "continuations": [],
        })
    sessions = [
        {
            "role": "coordinator", "task_id": "coordinator",
            "launch_ref": coordinator_launch[0], "launch_sha256": coordinator_launch[1],
            "canonical_launch_path": coordinator_launch[2], "canonical_launch_sha256": coordinator_launch[3],
            "ref": coordinator_ref, "sha256": coordinator_sha,
            "event_ref": coordinator_app_ref, "event_sha256": coordinator_app_sha,
            "export_ref": coordinator_export_ref, "export_sha256": coordinator_export_sha,
            "continuations": [],
        },
        *worker_sessions,
        *verifier_sessions,
    ]
    launches: list[dict[str, Any]] = []
    for source in sessions:
        for attempt_ordinal, attempt in enumerate([source, *source["continuations"]], start=1):
            export = json.loads((root / attempt["export_ref"]).read_text())
            launch = json.loads((root / attempt["launch_ref"]).read_text())
            launches.append({
                "ordinal": len(launches) + 1,
                "session_id": export["session_id"],
                "attempt_ordinal": attempt_ordinal,
                "turn_id": export["turn_ids"][0],
                "task_id": attempt["task_id"],
                "role": attempt["role"],
                "transport": launch["transport"],
                "launch_ref": attempt["launch_ref"],
                "launch_sha256": attempt["launch_sha256"],
            })
    workspace_instance_id = f"workspace-{workload['id']}-{trial}-{variant}"
    launch_manifest = {
        "schema_version": "agent-workflow.canary-host-launch-manifest.v1",
        "run_seal_sha256": run_seal_sha,
        "pair_id": pair_id,
        "variant": variant,
        "workspace_instance_id": workspace_instance_id,
        "host_authority_id": seal["host_authority_id"],
        "runtime_bundle_sha256": seal["runtime_bundle_sha256"],
        "launches": launches,
    }
    launch_manifest_path = root / f"{prefix}/host-launch-manifest.json"
    write_json(launch_manifest_path, launch_manifest)
    launch_manifest_ref, launch_manifest_sha = relative_evidence(root, launch_manifest_path)
    canonical_launch_manifest = canonical_artifact_root(root) / launch_manifest_ref
    canonical_launch_manifest.parent.mkdir(parents=True, exist_ok=True)
    canonical_launch_manifest.write_bytes(launch_manifest_path.read_bytes())
    if workload["workload_class"] in {"read_research", "long_verification"}:
        declared_roots: list[str] = []
        changed_paths: list[str] = []
    else:
        declared_roots = ["src/a", "src/b"]
        changed_paths = ["src/a/result.txt", "src/b/result.txt"]
    acceptance_stdout = _canonical_json({"criteria_passed": workload["success_criteria"]}) + b"\n"
    if failing_check == "AW-H005":
        changed_paths = ["undeclared/escape.txt"]
    if failing_check == "AW-H005-traversal":
        declared_roots = ["src/a"]
        changed_paths = ["src/a/../../.git/config"]
    watchdog_receipts = [{
        "process_id": f"{pair_id}:{variant}:process:{index}",
        "process_group_id": 1000 + index,
        "terminal_status": "completed", "reaped": True,
        "stdout_sha256": _sha256(f"{pair_id}:{variant}:stdout:{index}".encode()),
        "stderr_sha256": _sha256(b""),
    } for index in range(len(sessions))]
    if failing_check == "AW-H009":
        watchdog_receipts[0]["reaped"] = False
    elif failing_check == "AW-H009-missing":
        watchdog_receipts.pop()
    elif failing_check == "AW-H009-duplicate" and len(watchdog_receipts) > 1:
        watchdog_receipts[1]["process_id"] = watchdog_receipts[0]["process_id"]
    contract_evidence = {
        "schema_version": "agent-workflow.canary-contract-evidence.v1",
        "acceptance": {
            "command": ["python3", "acceptance.py", workload["id"]], "exit_code": 1 if failing_check == "AW-H001" else 0,
            "criteria_passed": workload["success_criteria"],
            "stdout_sha256": _sha256(acceptance_stdout), "stderr_sha256": _sha256(b""),
        },
        "repository_audit": {
            "declared_write_roots": declared_roots, "changed_paths": changed_paths,
            "writer_scopes": ["src", "src/a"] if failing_check == "AW-H005-overlap" else declared_roots,
            "integration_before_sha256": repository_snapshot_sha256,
            "integration_after_sha256": integration_after_sha,
            "integration_completed_at": integration_completed_at,
        },
        "lineage_audit": {"original_lineages": [{
            "lineage_id": f"{pair_id}:origin", "recoveries": 2 if failing_check == "AW-H007" else 1 if workload["workload_class"] == "failure_recovery" else 0,
            "successful_sibling_reruns": 0,
        }]},
        "delivery_audit": {
            "sealed_final_deliveries": 1,
            "post_final_product_actions": ["reopened_product_work"] if failing_check == "AW-H011" else [],
        },
        "terminal_audit": {
            "process_exit_codes": ([1] + [0] * (len(sessions) - 1)) if failing_check == "AW-H004" else [0] * len(sessions),
            "native_terminal_statuses": ["completed"] * len(sessions),
            "typed_output_valid": [True] * len(sessions),
        },
        "permission_audit": {
            "git_write_exit": 0 if failing_check == "AW-H006" else 1,
            "publish_exit": 1, "production_write_exit": 1, "undeclared_network_exit": 1,
        },
        "process_audit": {
            "owned_processes": len(sessions),
            "watchdog_receipts": watchdog_receipts,
            "post_reap_live_groups": 0,
        },
        "artifact_audit": {
            "create_once_collisions": 0,
            "digest_replay_failures": 1 if failing_check == "AW-H010" else 0,
            "authority_replay_failures": 0,
        },
    }
    contract_path = root / f"{prefix}/contract-evidence.json"
    write_json(contract_path, contract_evidence)
    contract_ref, contract_sha = relative_evidence(root, contract_path)
    canonical_contract = canonical_artifact_root(root) / contract_ref
    canonical_contract.parent.mkdir(parents=True, exist_ok=True)
    canonical_contract.write_bytes(contract_path.read_bytes())
    receipt = {
        "schema_version": "agent-workflow.canary-variant-receipt.v1",
        "pair_id": pair_id,
        "variant": variant,
        "run_seal_sha256": run_seal_sha,
        "repository_snapshot_ref": repository_snapshot_ref,
        "repository_snapshot_sha256": repository_snapshot_sha256,
        "canonical_snapshot_path": str(canonical_snapshot_root(root) / repository_snapshot_ref),
        "canonical_snapshot_sha256": repository_snapshot_sha256,
        "host_profile_sha256": seal["host_profile_sha256"],
        "capacity_profile_sha256": seal["capacity_profile_sha256"],
        "workspace_instance_id": workspace_instance_id,
        "launch_manifest_ref": launch_manifest_ref,
        "launch_manifest_sha256": launch_manifest_sha,
        "canonical_launch_manifest_path": str(canonical_launch_manifest),
        "canonical_launch_manifest_sha256": launch_manifest_sha,
        "contract_evidence_ref": contract_ref,
        "contract_evidence_sha256": contract_sha,
        "canonical_contract_evidence_path": str(canonical_contract),
        "canonical_contract_evidence_sha256": contract_sha,
        "sessions": sessions,
    }
    receipt_path = root / f"{prefix}/receipt.json"
    write_json(receipt_path, receipt)
    receipt_ref, receipt_sha = relative_evidence(root, receipt_path)
    replayed_metrics = _replay_variant_receipt(
        root.resolve(), ref=receipt_ref, digest=receipt_sha, pair_id=pair_id, variant=variant,
        run_seal=seal, run_seal_sha256=run_seal_sha,
    )
    checks: dict[str, Any] = {}
    evidence_digests: list[str] = []
    for check_id in workload["hard_check_ids"]:
        qualification_record = _subject_qualification_record(
            check_id=check_id, workload_id=workload["id"], trial=trial, variant_name=variant,
            subject_receipt_sha256=receipt_sha,
            runtime_bundle_sha256=str(seal["runtime_bundle_sha256"]),
            replayed_metrics=replayed_metrics,
            workload=workload,
        )
        evidence = {
            "schema_version": "agent-workflow.canary-hidden-proof.v1",
            "check_id": check_id,
            "workload_id": workload["id"],
            "trial": trial,
            "variant": variant,
            "run_seal_sha256": run_seal_sha,
            "evidence_type": HIDDEN_EVIDENCE_TYPES[check_id],
            "validator_id": f"agent-workflow.hidden-validator.{HIDDEN_EVIDENCE_TYPES[check_id]}.v1",
            "validator_bundle_sha256": seal["runtime_bundle_sha256"],
            "qualification_record": qualification_record,
            "qualification_record_sha256": _sha256(_canonical_json(qualification_record) + b"\n"),
        }
        evidence_path = root / f"{prefix}/{check_id}.json"
        write_json(evidence_path, evidence)
        evidence_ref, evidence_sha = relative_evidence(root, evidence_path)
        canonical_evidence = canonical_artifact_root(root) / evidence_ref
        canonical_evidence.parent.mkdir(parents=True, exist_ok=True)
        canonical_evidence.write_bytes(evidence_path.read_bytes())
        evidence_digests.append(evidence_sha)
        checks[check_id] = {
            "evidence_ref": evidence_ref,
            "evidence_sha256": evidence_sha,
            "canonical_evidence_path": str(canonical_evidence),
            "canonical_evidence_sha256": evidence_sha,
        }
    return (
        {"receipt_ref": receipt_ref, "receipt_sha256": receipt_sha, "hard_checks": checks},
        evidence_digests,
        _sha256(terminal_output.encode("utf-8")),
    )


def passing_results(
    root: Path,
    run_seal_sha: str,
    seal: dict[str, object],
    *,
    legacy_latency: float | list[float] = 10.0,
    vnext_latency: float | list[float] = 9.0,
    failing_check: tuple[str, int, str, str] | None = None,
    failing_checks: tuple[tuple[str, int, str, str], ...] = (),
) -> dict[str, object]:
    corpus = json.loads((CANARY_FIXTURES / "corpus.v1.json").read_text())
    pairs: list[dict[str, Any]] = []
    hidden_digests: list[str] = []
    hidden_artifacts: list[dict[str, Any]] = []
    hidden_manifest_items: list[dict[str, Any]] = []
    pair_index = 0
    label_map = json.loads((root / str(seal["blind_label_map_ref"])).read_text())["pairs"]
    rubric_value = json.loads((root / str(seal["rubric_ref"])).read_text())
    sealed_at = datetime.fromisoformat(str(seal["sealed_at"]).replace("Z", "+00:00"))
    base_time = max(datetime(2026, 7, 12, tzinfo=timezone.utc), sealed_at + timedelta(seconds=1))
    for workload in corpus["workloads"]:
        for trial in range(1, 6):
            pair_id = f"{workload['id']}:{trial}"
            variants: dict[str, Any] = {}
            output_digests: dict[str, str] = {}
            pair_hidden_digests: list[str] = []
            snapshot_path = root / f"snapshots/{workload['id']}/{trial}.json"
            write_json(snapshot_path, {
                "schema_version": "agent-workflow.canary-paired-snapshot.v1",
                "pair_id": pair_id,
                "head": "fixture-head",
                "staged_diff_sha256": "sha256:" + "1" * 64,
                "unstaged_diff_sha256": "sha256:" + "2" * 64,
                "untracked_manifest_sha256": "sha256:" + "3" * 64,
            })
            snapshot_ref, snapshot_sha = relative_evidence(root, snapshot_path)
            canonical_snapshot = canonical_snapshot_root(root) / snapshot_ref
            canonical_snapshot.parent.mkdir(parents=True, exist_ok=True)
            canonical_snapshot.write_bytes(snapshot_path.read_bytes())
            l_latency = legacy_latency[pair_index] if isinstance(legacy_latency, list) else legacy_latency
            v_latency = vnext_latency[pair_index] if isinstance(vnext_latency, list) else vnext_latency
            execution_order = expected_pair_order(seal, pair_id)
            first_latency = l_latency if execution_order[0] == "legacy" else v_latency
            offsets = {execution_order[0]: 0.0, execution_order[1]: first_latency + 1.0}
            for variant in ("legacy", "vnext"):
                failure = None
                requested_failures = ((failing_check,) if failing_check else ()) + failing_checks
                matching_failures = [
                    item[3]
                    for item in requested_failures
                    if item[:3] == (workload["id"], trial, variant)
                ]
                if len(matching_failures) > 1:
                    raise AssertionError("fixture declares more than one hidden failure for one variant")
                if matching_failures:
                    failure = matching_failures[0]
                variants[variant], digests, output_digests[variant] = write_variant(
                    root, seal, run_seal_sha, workload, trial, variant,
                    completions=10 if variant == "legacy" else 4,
                    tokens=1000 if variant == "legacy" else 700,
                    latency=l_latency if variant == "legacy" else v_latency,
                    start_offset=offsets[variant],
                    repository_snapshot_ref=snapshot_ref,
                    repository_snapshot_sha256=snapshot_sha,
                    failing_check=failure,
                    worker_transport="codex_exec_jsonl" if variant == "vnext" else "app_server",
                )
                hidden_digests.extend(digests)
                pair_hidden_digests.extend(digests)
            pair_hidden_artifacts = [
                {
                    "sha256": check["evidence_sha256"],
                    "content": json.loads((root / check["evidence_ref"]).read_text()),
                }
                for variant in ("legacy", "vnext")
                for check in variants[variant]["hard_checks"].values()
            ]
            hidden_artifacts.extend(pair_hidden_artifacts)
            hidden_manifest_items.extend([
                {
                    "sha256": check["evidence_sha256"],
                    "content_ref": check["evidence_ref"],
                    "content": json.loads((root / check["evidence_ref"]).read_text()),
                }
                for variant in ("legacy", "vnext")
                for check in variants[variant]["hard_checks"].values()
            ])
            label_artifacts = {
                label_map[pair_id][variant]: {
                    "sha256": output_digests[variant],
                    "content": _canonical_json({"pair_id": pair_id, "variant": variant, "answer": "verified"}).decode(),
                }
                for variant in ("legacy", "vnext")
            }
            review_packet = {
                "schema_version": "agent-workflow.canary-review-launch.v1",
                "purpose": "blind_pair_review",
                "pair_id": pair_id,
                "run_seal_sha256": run_seal_sha,
                "host_preamble_sha256": _sha256(b""),
                "workload": workload,
                "rubric_sha256": seal["rubric_sha256"],
                "rubric": rubric_value,
                "label_artifacts": label_artifacts,
                "hidden_evidence": sorted(pair_hidden_artifacts, key=lambda item: item["sha256"]),
            }
            review_packet_path = root / f"reviews/{workload['id']}/{trial}-launch.json"
            write_json(review_packet_path, review_packet)
            review_packet_ref, review_packet_sha = relative_evidence(root, review_packet_path)
            decision = {
                "schema_version": "agent-workflow.canary-blind-review.v1",
                "pair_id": pair_id,
                "rubric_sha256": seal["rubric_sha256"],
                "labels": {
                    label_map[pair_id][variant]: {"output_sha256": output_digests[variant], "scores": {"correctness": 4, "evidence": 4, "completeness": 4}}
                    for variant in ("legacy", "vnext")
                },
                "preference": "tie",
            }
            raw_ref, raw_sha = write_raw_session(
                root,
                f"reviews/{workload['id']}/{trial}.jsonl",
                session_id=f"reviewer-{workload['id']}-{trial}",
                model=str(seal["top_model"]),
                effort=str(seal["reasoning_effort"]),
                duration=1.0,
                start_offset=max(offsets["legacy"] + l_latency, offsets["vnext"] + v_latency) + 1.0,
                input_message=_canonical_json(review_packet).decode(),
                terminal_message=_canonical_json(decision).decode(),
                base_time=base_time,
            )
            review = {
                "schema_version": "agent-workflow.canary-blind-review-evidence.v1",
                "run_seal_sha256": run_seal_sha,
                "reviewer_route": {
                    "model": seal["top_model"],
                    "reasoning_effort": seal["reasoning_effort"],
                    "session_id": f"reviewer-{workload['id']}-{trial}",
                },
                "raw_session_ref": raw_ref,
                "raw_session_sha256": raw_sha,
                "canonical_raw_path": str(canonical_session_root(root) / raw_ref),
                "canonical_raw_sha256": raw_sha,
                "launch_packet_ref": review_packet_ref,
                "launch_packet_sha256": review_packet_sha,
                "host_preamble_sha256": _sha256(b""),
                "decision": decision,
            }
            review_path = root / f"reviews/{workload['id']}/{trial}.json"
            write_json(review_path, review)
            review_ref, review_sha = relative_evidence(root, review_path)
            pairs.append({
                "workload_id": workload["id"],
                "trial": trial,
                "execution_order": execution_order,
                "blind_review_ref": review_ref,
                "blind_review_sha256": review_sha,
                "variants": variants,
            })
            pair_index += 1
    hidden_manifest = run_vnext_canary.build_hidden_evidence_manifest(
        run_seal_sha, hidden_manifest_items,
    )
    for entry in hidden_manifest["entries"]:
        reader_path = root / entry["reader_ref"]
        reader_path.parent.mkdir(parents=True, exist_ok=True)
        reader_path.write_bytes((root / entry["authority_ref"]).read_bytes())
    hidden_manifest_path = root / "verifier/hidden-evidence-manifest.json"
    write_json(hidden_manifest_path, hidden_manifest)
    hidden_manifest_ref, hidden_manifest_sha = relative_evidence(root, hidden_manifest_path)
    verifier_decision = {
        "schema_version": "agent-workflow.canary-verifier-decision.v1",
        "run_seal_sha256": run_seal_sha,
        "P0": [],
        "P1": [],
        "P2": [],
        "accepted_hidden_evidence_manifest_sha256": hidden_manifest_sha,
    }
    verifier_packet = {
        "schema_version": "agent-workflow.canary-review-launch.v1",
        "purpose": "independent_verifier",
        "run_seal_sha256": run_seal_sha,
        "host_preamble_sha256": _sha256(b""),
        "workloads": corpus["workloads"],
        "hidden_evidence_manifest_ref": hidden_manifest_ref,
        "hidden_evidence_manifest_sha256": hidden_manifest_sha,
        "hidden_evidence_index": run_vnext_canary.hidden_evidence_verifier_index(hidden_manifest),
    }
    verifier_packet_path = root / "verifier/launch.json"
    write_json(verifier_packet_path, verifier_packet)
    verifier_packet_ref, verifier_packet_sha = relative_evidence(root, verifier_packet_path)
    verifier_raw_ref, verifier_raw_sha = write_raw_session(
        root,
        "verifier/session.jsonl",
        session_id="independent-verifier",
        model=str(seal["top_model"]),
        effort=str(seal["reasoning_effort"]),
        duration=2.0,
        start_offset=1000.0,
        input_message=_canonical_json(verifier_packet).decode(),
        terminal_message=_canonical_json(verifier_decision).decode(),
        base_time=base_time,
    )
    verifier = {
        "schema_version": "agent-workflow.canary-verifier-evidence.v1",
        "reviewer_route": {
            "model": seal["top_model"],
            "reasoning_effort": seal["reasoning_effort"],
            "session_id": "independent-verifier",
        },
        "raw_session_ref": verifier_raw_ref,
        "raw_session_sha256": verifier_raw_sha,
        "canonical_raw_path": str(canonical_session_root(root) / verifier_raw_ref),
        "canonical_raw_sha256": verifier_raw_sha,
        "launch_packet_ref": verifier_packet_ref,
        "launch_packet_sha256": verifier_packet_sha,
        "host_preamble_sha256": _sha256(b""),
        "decision": verifier_decision,
    }
    verifier_path = root / "verifier/evidence.json"
    write_json(verifier_path, verifier)
    verifier_ref, verifier_sha = relative_evidence(root, verifier_path)
    return {
        "schema_version": "agent-workflow.canary-results.v1",
        "run_seal_sha256": run_seal_sha,
        "verifier_ledger_ref": verifier_ref,
        "verifier_ledger_sha256": verifier_sha,
        "pairs": pairs,
    }


def write_host_results(root: Path, seal_path: Path, core: dict[str, object], stem: str = "results") -> Path:
    seal = json.loads(seal_path.read_text())
    frozen_scripts = _frozen_repository_root(root) / "skills/agent-workflow/scripts"
    commands = [
        {
            "command_id": command_id,
            "script_sha256": _sha256((frozen_scripts / script_name).read_bytes()),
            "exit_code": 0,
            "stdout_sha256": _sha256(f"fixture:{command_id}:stdout".encode()),
            "stderr_sha256": _sha256(b""),
        }
        for command_id, script_name in HOST_QUALIFICATION_COMMANDS.items()
    ]
    qualification_core = {
        "schema_version": "agent-workflow.canary-host-qualification.v1",
        "candidate_bundle_sha256": seal["candidate_bundle_sha256"],
        "runtime_bundle_sha256": seal["runtime_bundle_sha256"],
        "freeze_sha256": _sha256(Path(seal["freeze_path"]).read_bytes()),
        "commands": commands,
        "hidden_check_commands": HIDDEN_QUALIFICATION_MAP,
        "subject_records": _collect_subject_records(root.resolve(), core),
    }
    signed = {**qualification_core, "host_authority_id": seal["host_authority_id"]}
    key, authority_id = _ensure_host_key(root.resolve())
    if authority_id != seal["host_authority_id"]:
        raise AssertionError("fixture host authority drifted")
    write_json(root / "host/qualification.json", {**signed, "mac": _host_mac(key, signed)})
    draft_path = root / f"{stem}-draft.json"
    write_json(draft_path, core)
    summary = seal_results(
        root,
        run_seal_path=seal_path,
        draft_path=draft_path,
        seal_output=f"host/{stem}-seal.json",
        results_output=f"{stem}.json",
    )
    return root / summary["results_ref"]


class VNextCanaryTests(unittest.TestCase):
    def test_effective_worker_profile_cannot_cover_a_canonical_store(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            seal = write_authority(root, "sha256:" + "a" * 64)
            context = {
                "workspace_roots": [str(root)],
                "permission_profile": {
                    "type": "managed",
                    "file_system": {
                        "type": "restricted",
                        "entries": [{
                            "path": {"type": "path", "path": str(canonical_session_root(root))},
                            "access": "write",
                        }],
                    },
                },
            }
            with self.assertRaisesRegex(CandidateError, "can access a canonical host store"):
                _assert_session_denies_canonical_stores(context, root, seal, "fixture worker")

    def test_external_but_world_writable_canonical_store_is_rejected(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            seal = write_authority(root, "sha256:" + "a" * 64)
            seal_path = root / "run-seal.json"
            write_json(seal_path, seal)
            core = passing_results(root, _sha256(seal_path.read_bytes()), seal)
            canonical_session_root(root).chmod(0o777)
            with self.assertRaisesRegex(CandidateError, "canonical store authority"):
                write_host_results(
                    root,
                    seal_path,
                    core,
                )

    def test_qualification_executes_only_from_the_frozen_host_bundle(self) -> None:
        with TemporaryDirectory() as raw:
            base = Path(raw)
            repo = base / "repo"
            workspace = base / "workspace"
            repo.mkdir()
            workspace.mkdir()
            source_repo = SCRIPT_DIR.parents[2]
            for relative in sorted(REQUIRED_PROMOTION_EXECUTABLES | REQUIRED_PROMOTION_AUTHORITY):
                source = source_repo / relative
                target = repo / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
            freeze_summary = build_promotion_freeze(repo, Path("promotion-freeze.json"))
            freeze_path = repo / freeze_summary["freeze_ref"]
            source = write_authority(
                workspace,
                freeze_summary["semantic_bundle_sha256"],
                runtime_bundle=freeze_summary["runtime_bundle_sha256"],
                use_current_freeze=False,
            )
            source["executable_freeze_sha256"] = freeze_summary["freeze_sha256"]
            for generated in ("sealed_at", "host_authority_id", "results_ref", "freeze_path", "repository_root"):
                source.pop(generated, None)
            source_path = base / "run-seal-source.json"
            write_json(source_path, source)
            class FrozenDatetime(datetime):
                @classmethod
                def now(cls, tz: timezone | None = None) -> datetime:
                    value = datetime(2026, 7, 11, 23, 0, tzinfo=timezone.utc)
                    return value if tz is not None else value.replace(tzinfo=None)

            with mock.patch.object(run_vnext_canary, "datetime", FrozenDatetime):
                summary = seal_run(
                    workspace,
                    repo=repo,
                    freeze_path=freeze_path,
                    source_path=source_path,
                    results_ref="results.json",
                    output_path="run-seal.json",
                )
            sealed = json.loads((workspace / "run-seal.json").read_text())
            draft_path = workspace / "results-draft.json"
            write_json(draft_path, passing_results(workspace, summary["run_seal_sha256"], sealed))
            observed_scripts: list[Path] = []
            run_vnext_canary._QUALIFICATION_CACHE.clear()

            def qualification_stub(command: list[str], **_: Any) -> Any:
                observed_scripts.append(Path(command[1]).resolve())
                mutable = repo / "skills/agent-workflow/scripts/test_vnext_runtime.py"
                original = mutable.read_bytes()
                mutable.write_bytes(b"# transient mutable checkout drift\n")
                mutable.write_bytes(original)
                return mock.Mock(returncode=0, stdout=b"ok\n", stderr=b"")

            with mock.patch.object(run_vnext_canary.subprocess, "run", side_effect=qualification_stub):
                seal_results(
                    workspace,
                    run_seal_path=workspace / "run-seal.json",
                    draft_path=draft_path,
                    seal_output="host/evidence-seal.json",
                    results_output="results.json",
                )
            frozen_root = (_control_root(workspace) / "frozen-repository").resolve()
            self.assertTrue(observed_scripts)
            self.assertTrue(all(path.is_relative_to(frozen_root) for path in observed_scripts))

    def test_seal_results_reuses_host_signed_qualification_after_process_loss(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            seal = write_authority(root, "sha256:" + "a" * 64)
            seal_path = root / "run-seal.json"
            write_json(seal_path, seal)
            draft_path = root / "results-draft.json"
            write_json(draft_path, passing_results(root, _sha256(seal_path.read_bytes()), seal))
            original_create = run_vnext_canary._create_or_verify_json

            def crash_after_qualification(workspace: Path, relative: str, value: dict[str, Any]) -> Path:
                path = original_create(workspace, relative, value)
                if relative == "host/qualification.json":
                    raise RuntimeError("simulated process loss after qualification publish")
                return path

            run_vnext_canary._QUALIFICATION_CACHE.clear()
            qualification_result = mock.Mock(returncode=0, stdout=b"fixture qualification\n", stderr=b"")
            with mock.patch.object(run_vnext_canary.subprocess, "run", return_value=qualification_result):
                with mock.patch.object(
                    run_vnext_canary,
                    "_create_or_verify_json",
                    side_effect=crash_after_qualification,
                ):
                    with self.assertRaisesRegex(RuntimeError, "simulated process loss"):
                        seal_results(
                            root,
                            run_seal_path=seal_path,
                            draft_path=draft_path,
                            seal_output="host/evidence-seal.json",
                            results_output="results.json",
                        )
            qualification_path = root / "host/qualification.json"
            first_bytes = qualification_path.read_bytes()
            run_vnext_canary._QUALIFICATION_CACHE.clear()
            with mock.patch.object(run_vnext_canary.subprocess, "run") as rerun:
                summary = seal_results(
                    root,
                    run_seal_path=seal_path,
                    draft_path=draft_path,
                    seal_output="host/evidence-seal.json",
                    results_output="results.json",
                )
            self.assertEqual(rerun.call_count, 0)
            self.assertEqual(qualification_path.read_bytes(), first_bytes)
            self.assertEqual(summary["status"], "pass")
            self.assertTrue((root / "results.json").is_file())

    def test_seal_results_replays_after_host_seal_publish_response_loss(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            seal = write_authority(root, "sha256:" + "a" * 64)
            seal_path = root / "run-seal.json"
            write_json(seal_path, seal)
            draft_path = root / "results-draft.json"
            write_json(draft_path, passing_results(root, _sha256(seal_path.read_bytes()), seal))
            original_create = run_vnext_canary._create_or_verify_json

            def lose_response_after_host_seal(workspace: Path, relative: str, value: dict[str, Any]) -> Path:
                path = original_create(workspace, relative, value)
                if relative == "host/evidence-seal.json":
                    raise RuntimeError("simulated response loss after host seal")
                return path

            run_vnext_canary._QUALIFICATION_CACHE.clear()
            qualification_result = mock.Mock(returncode=0, stdout=b"fixture qualification\n", stderr=b"")
            with mock.patch.object(run_vnext_canary.subprocess, "run", return_value=qualification_result):
                with mock.patch.object(
                    run_vnext_canary,
                    "_create_or_verify_json",
                    side_effect=lose_response_after_host_seal,
                ):
                    with self.assertRaisesRegex(RuntimeError, "simulated response loss"):
                        seal_results(
                            root,
                            run_seal_path=seal_path,
                            draft_path=draft_path,
                            seal_output="host/evidence-seal.json",
                            results_output="results.json",
                        )
            qualification_bytes = (root / "host/qualification.json").read_bytes()
            host_seal_bytes = (root / "host/evidence-seal.json").read_bytes()
            run_vnext_canary._QUALIFICATION_CACHE.clear()
            with mock.patch.object(run_vnext_canary.subprocess, "run") as rerun:
                summary = seal_results(
                    root,
                    run_seal_path=seal_path,
                    draft_path=draft_path,
                    seal_output="host/evidence-seal.json",
                    results_output="results.json",
                )
            self.assertEqual(rerun.call_count, 0)
            self.assertEqual((root / "host/qualification.json").read_bytes(), qualification_bytes)
            self.assertEqual((root / "host/evidence-seal.json").read_bytes(), host_seal_bytes)
            self.assertEqual(summary["status"], "pass")

    def test_freeze_and_run_seal_are_verified_before_results_exist(self) -> None:
        with TemporaryDirectory() as raw:
            repo = Path(raw) / "repo"
            workspace = Path(raw) / "workspace"
            repo.mkdir()
            workspace.mkdir()
            frozen_files = []
            frozen_digests = {}
            for relative in sorted(REQUIRED_PROMOTION_EXECUTABLES):
                executable = repo / relative
                executable.parent.mkdir(parents=True, exist_ok=True)
                executable.write_text(f"# frozen {relative}\n")
                digest = _sha256(executable.read_bytes())
                frozen_digests[relative] = digest
                frozen_files.append({"path": relative, "sha256": digest, "bytes": executable.stat().st_size})
            manifest = [
                {"path": name, "sha256": frozen_digests[f"skills/agent-workflow/scripts/{name}"]}
                for name in CORE_RUNTIME_FILES
            ]
            bundle = _sha256(_canonical_json(manifest) + b"\n")
            authority_files = []
            authority_manifest = []
            for relative in sorted(REQUIRED_PROMOTION_AUTHORITY):
                path = repo / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"authority {relative}\n")
                digest = _sha256(path.read_bytes())
                authority_files.append({"path": relative, "sha256": digest, "bytes": path.stat().st_size})
                authority_manifest.append({"path": relative, "sha256": digest})
            semantic = _sha256(_canonical_json({
                "executables": [
                    {"path": path, "sha256": frozen_digests[path]}
                    for path in sorted(REQUIRED_PROMOTION_EXECUTABLES)
                ],
                "authority": authority_manifest,
            }) + b"\n")
            freeze = {
                "schema_version": "agent-workflow.promotion-freeze.v1",
                "runtime_bundle_sha256": bundle,
                "semantic_bundle_sha256": semantic,
                "files": frozen_files,
                "authority": authority_files,
            }
            freeze_path = Path(raw) / "freeze.json"
            source_path = Path(raw) / "run-seal-source.json"
            write_json(freeze_path, freeze)
            seal = write_authority(workspace, semantic, runtime_bundle=bundle, use_current_freeze=False)
            seal["executable_freeze_sha256"] = _sha256(freeze_path.read_bytes())
            source = dict(seal)
            source.pop("sealed_at")
            source.pop("host_authority_id")
            source.pop("results_ref")
            source.pop("freeze_path")
            source.pop("repository_root")
            write_json(source_path, source)
            self.assertEqual(verify_executable_freeze(repo, freeze_path), freeze)
            summary = seal_run(workspace, repo=repo, freeze_path=freeze_path, source_path=source_path, results_ref="results.json", output_path="run-seal.json")
            self.assertEqual(summary["run_seal_ref"], "run-seal.json")
            self.assertEqual(
                seal_run(workspace, repo=repo, freeze_path=freeze_path, source_path=source_path, results_ref="results.json", output_path="run-seal.json"),
                summary,
            )
            semantic_relative = "skills/agent-workflow/references/vnext-candidate-skill.md"
            semantic_path = repo / semantic_relative
            semantic_path.write_text("semantic drift\n")
            with self.assertRaisesRegex(CandidateError, "promotion authority drifted"):
                verify_executable_freeze(repo, freeze_path)
            semantic_path.write_text(f"authority {semantic_relative}\n")
            (workspace / "decoy-result.json").write_text("{}")
            with self.assertRaisesRegex(CandidateError, "unsealed result/evidence"):
                seal_run(workspace, repo=repo, freeze_path=freeze_path, source_path=source_path, results_ref="results.json", output_path="another-seal.json")
            drifted_relative = sorted(REQUIRED_PROMOTION_EXECUTABLES)[0]
            (repo / drifted_relative).write_text("print('drift')\n")
            with self.assertRaisesRegex(CandidateError, "drifted"):
                verify_executable_freeze(repo, freeze_path)
            sealed_run = json.loads((workspace / "run-seal.json").read_text())
            draft_path = workspace / "post-freeze-drift-draft.json"
            write_json(
                draft_path,
                passing_results(
                    workspace,
                    summary["run_seal_sha256"],
                    sealed_run,
                ),
            )
            with self.assertRaisesRegex(CandidateError, "drifted"):
                seal_results(
                    workspace,
                    run_seal_path=workspace / "run-seal.json",
                    draft_path=draft_path,
                    seal_output="host/post-freeze-drift-seal.json",
                    results_output="results.json",
                )
            self.assertFalse((workspace / "host/post-freeze-drift-seal.json").exists())
            self.assertFalse((workspace / "results.json").exists())
            (repo / drifted_relative).write_text(f"# frozen {drifted_relative}\n")
            validator_relative = "skills/agent-workflow/scripts/test_vnext_runtime.py"
            (repo / validator_relative).write_text("# qualification validator drift\n")
            with self.assertRaisesRegex(CandidateError, "promotion authority drifted"):
                seal_results(
                    workspace,
                    run_seal_path=workspace / "run-seal.json",
                    draft_path=draft_path,
                    seal_output="host/post-validator-drift-seal.json",
                    results_output="results.json",
                )
            self.assertFalse((workspace / "host/post-validator-drift-seal.json").exists())
            self.assertFalse((workspace / "results.json").exists())
            (repo / validator_relative).write_text(f"authority {validator_relative}\n")
            incomplete = copy.deepcopy(freeze)
            incomplete["files"].pop()
            write_json(freeze_path, incomplete)
            with self.assertRaisesRegex(CandidateError, "omitted required"):
                verify_executable_freeze(repo, freeze_path)

    def test_run_seal_detects_a_concurrent_prepublication_workspace_write(self) -> None:
        with TemporaryDirectory() as raw:
            repo = Path(raw) / "repo"
            workspace = Path(raw) / "workspace"
            repo.mkdir()
            workspace.mkdir()
            frozen_files = []
            frozen_digests = {}
            for relative in sorted(REQUIRED_PROMOTION_EXECUTABLES):
                path = repo / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(relative)
                frozen_digests[relative] = _sha256(path.read_bytes())
                frozen_files.append({"path": relative, "sha256": frozen_digests[relative], "bytes": path.stat().st_size})
            authority_files = []
            authority_manifest = []
            for relative in sorted(REQUIRED_PROMOTION_AUTHORITY):
                path = repo / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(relative)
                digest = _sha256(path.read_bytes())
                authority_files.append({"path": relative, "sha256": digest, "bytes": path.stat().st_size})
                authority_manifest.append({"path": relative, "sha256": digest})
            runtime_bundle = _sha256(_canonical_json([
                {"path": name, "sha256": frozen_digests[f"skills/agent-workflow/scripts/{name}"]}
                for name in CORE_RUNTIME_FILES
            ]) + b"\n")
            semantic_bundle = _sha256(_canonical_json({
                "executables": [{"path": path, "sha256": frozen_digests[path]} for path in sorted(REQUIRED_PROMOTION_EXECUTABLES)],
                "authority": authority_manifest,
            }) + b"\n")
            freeze = {"schema_version": "agent-workflow.promotion-freeze.v1", "runtime_bundle_sha256": runtime_bundle, "semantic_bundle_sha256": semantic_bundle, "files": frozen_files, "authority": authority_files}
            freeze_path = Path(raw) / "freeze.json"
            source_path = Path(raw) / "source.json"
            write_json(freeze_path, freeze)
            source = write_authority(workspace, semantic_bundle, runtime_bundle=runtime_bundle, use_current_freeze=False)
            source["executable_freeze_sha256"] = _sha256(freeze_path.read_bytes())
            source.pop("sealed_at")
            source.pop("host_authority_id")
            source.pop("results_ref")
            source.pop("freeze_path")
            source.pop("repository_root")
            write_json(source_path, source)
            real_create = run_vnext_canary.create_once_json

            def raced_create(root: Path, relative: str, value: object) -> Path:
                (Path(root) / "results.json").write_text("{}")
                return real_create(root, relative, value)

            with mock.patch("run_vnext_canary.create_once_json", side_effect=raced_create):
                with self.assertRaisesRegex(CandidateError, "changed during"):
                    seal_run(workspace, repo=repo, freeze_path=freeze_path, source_path=source_path, results_ref="results.json", output_path="run-seal.json")
            with self.assertRaisesRegex(CandidateError, "results schema drift"):
                seal_run(workspace, repo=repo, freeze_path=freeze_path, source_path=source_path, results_ref="results.json", output_path="run-seal.json")

    def test_paired_evaluator_replays_raw_authority_and_reports_p95(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            seal = write_authority(root, "sha256:" + "a" * 64)
            seal_path = root / "run-seal.json"
            write_json(seal_path, seal)
            results_path = write_host_results(root, seal_path, passing_results(root, _sha256(seal_path.read_bytes()), seal))
            result = evaluate_results(seal_path, results_path)
            self.assertEqual(result["status"], "pass")
            self.assertEqual(result["pair_count"], 25)
            for metric in ("legacy_token_p95", "vnext_token_p95", "legacy_completion_p95", "vnext_completion_p95", "legacy_latency_p95", "vnext_latency_p95"):
                self.assertIn(metric, result["metrics"])

    def test_unsigned_or_post_seal_modified_results_are_rejected(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            seal = write_authority(root, "sha256:" + "a" * 64)
            seal_path = root / "run-seal.json"
            write_json(seal_path, seal)
            core = passing_results(root, _sha256(seal_path.read_bytes()), seal)
            unsigned = root / "unsigned.json"
            write_json(unsigned, core)
            with self.assertRaisesRegex(CandidateError, "results schema drift"):
                evaluate_results(seal_path, unsigned)
            final_path = write_host_results(root, seal_path, core)
            final = json.loads(final_path.read_text())
            final["pairs"] = list(reversed(final["pairs"]))
            tampered = root / "tampered-final.json"
            write_json(tampered, final)
            with self.assertRaisesRegex(CandidateError, "host evidence seal authority drifted"):
                evaluate_results(seal_path, tampered)

    def test_hard_failure_preempts_performance(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            seal = write_authority(root, "sha256:" + "a" * 64)
            seal_path = root / "run-seal.json"
            write_json(seal_path, seal)
            corpus = json.loads((CANARY_FIXTURES / "corpus.v1.json").read_text())
            workload = next(item for item in corpus["workloads"] if "AW-H005" in item["hard_check_ids"])
            results = passing_results(
                root, _sha256(seal_path.read_bytes()), seal,
                failing_check=(workload["id"], 1, "vnext", "AW-H005"),
            )
            results_path = write_host_results(root, seal_path, results)
            evaluated = evaluate_results(seal_path, results_path)
            self.assertEqual(evaluated["performance_gate"], "not_evaluated")
            self.assertIn("AW-H005", " ".join(evaluated["failures"]))

    def test_expected_legacy_completion_density_failure_is_a_baseline_not_a_candidate_regression(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            seal = write_authority(root, "sha256:" + "a" * 64)
            seal_path = root / "run-seal.json"
            write_json(seal_path, seal)
            corpus = json.loads((CANARY_FIXTURES / "corpus.v1.json").read_text())
            workload = next(item for item in corpus["workloads"] if "AW-H003" in item["hard_check_ids"])
            results = passing_results(
                root, _sha256(seal_path.read_bytes()), seal,
                failing_check=(workload["id"], 1, "legacy", "AW-H003"),
            )
            results_path = write_host_results(root, seal_path, results)
            evaluated = evaluate_results(seal_path, results_path)
            self.assertEqual(evaluated["correctness_hard_gate"], "pass")
            self.assertEqual(evaluated["performance_gate"], "pass")
            self.assertNotIn("hidden contract failure AW-H003", " ".join(evaluated["failures"]))

    def test_non_density_legacy_hidden_failure_still_invalidates_the_baseline(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            seal = write_authority(root, "sha256:" + "a" * 64)
            seal_path = root / "run-seal.json"
            write_json(seal_path, seal)
            corpus = json.loads((CANARY_FIXTURES / "corpus.v1.json").read_text())
            workload = next(item for item in corpus["workloads"] if "AW-H001" in item["hard_check_ids"])
            results = passing_results(
                root, _sha256(seal_path.read_bytes()), seal,
                failing_check=(workload["id"], 1, "legacy", "AW-H001"),
            )
            results_path = write_host_results(root, seal_path, results)
            evaluated = evaluate_results(seal_path, results_path)
            self.assertEqual(evaluated["correctness_hard_gate"], "fail")
            self.assertIn("hidden contract failure AW-H001", " ".join(evaluated["failures"]))

    def test_vnext_completion_density_failure_is_always_a_candidate_regression(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            seal = write_authority(root, "sha256:" + "a" * 64)
            seal_path = root / "run-seal.json"
            write_json(seal_path, seal)
            corpus = json.loads((CANARY_FIXTURES / "corpus.v1.json").read_text())
            workload = next(item for item in corpus["workloads"] if "AW-H003" in item["hard_check_ids"])
            results = passing_results(
                root, _sha256(seal_path.read_bytes()), seal,
                failing_check=(workload["id"], 1, "vnext", "AW-H003"),
            )
            results_path = write_host_results(root, seal_path, results)
            evaluated = evaluate_results(seal_path, results_path)
            self.assertEqual(evaluated["correctness_hard_gate"], "fail")
            self.assertEqual(evaluated["performance_gate"], "not_evaluated")
            self.assertIn("hidden contract failure AW-H003", " ".join(evaluated["failures"]))

    def test_legacy_density_exception_cannot_mask_a_vnext_density_failure(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            seal = write_authority(root, "sha256:" + "a" * 64)
            seal_path = root / "run-seal.json"
            write_json(seal_path, seal)
            corpus = json.loads((CANARY_FIXTURES / "corpus.v1.json").read_text())
            workload = next(item for item in corpus["workloads"] if "AW-H003" in item["hard_check_ids"])
            results = passing_results(
                root, _sha256(seal_path.read_bytes()), seal,
                failing_checks=(
                    (workload["id"], 1, "legacy", "AW-H003"),
                    (workload["id"], 1, "vnext", "AW-H003"),
                ),
            )
            results_path = write_host_results(root, seal_path, results)
            evaluated = evaluate_results(seal_path, results_path)
            self.assertEqual(evaluated["correctness_hard_gate"], "fail")
            self.assertEqual(evaluated["performance_gate"], "not_evaluated")
            self.assertIn("hidden contract failure AW-H003", " ".join(evaluated["failures"]))

    def test_contract_specific_hidden_violations_hard_fail_before_performance(self) -> None:
        corpus = json.loads((CANARY_FIXTURES / "corpus.v1.json").read_text())
        for check_id, injected in (
            ("AW-H001", "AW-H001"), ("AW-H004", "AW-H004"), ("AW-H005", "AW-H005-overlap"),
            ("AW-H005", "AW-H005-traversal"),
            ("AW-H006", "AW-H006"), ("AW-H007", "AW-H007"), ("AW-H008", "AW-H008"),
            ("AW-H008", "AW-H008-early"),
            ("AW-H009", "AW-H009"), ("AW-H009", "AW-H009-missing"), ("AW-H009", "AW-H009-duplicate"),
            ("AW-H010", "AW-H010"), ("AW-H011", "AW-H011"),
        ):
            with self.subTest(check_id=check_id), TemporaryDirectory() as raw:
                root = Path(raw)
                seal = write_authority(root, "sha256:" + "a" * 64)
                seal_path = root / "run-seal.json"
                write_json(seal_path, seal)
                workload = next(item for item in corpus["workloads"] if check_id in item["hard_check_ids"])
                results = passing_results(
                    root, _sha256(seal_path.read_bytes()), seal,
                    failing_check=(workload["id"], 1, "vnext", injected),
                )
                results_path = write_host_results(root, seal_path, results)
                evaluated = evaluate_results(seal_path, results_path)
                self.assertEqual(evaluated["correctness_hard_gate"], "fail")
                self.assertIn(check_id, " ".join(evaluated["failures"]))

    def test_raw_completion_boundaries_not_app_turn_count_drive_completion_count(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            seal = write_authority(root, "sha256:" + "a" * 64)
            seal_path = root / "run-seal.json"
            write_json(seal_path, seal)
            run_sha = _sha256(seal_path.read_bytes())
            workload = json.loads((CANARY_FIXTURES / "corpus.v1.json").read_text())["workloads"][0]
            snapshot_path = root / "snapshot.json"
            write_json(snapshot_path, {
                "schema_version": "agent-workflow.canary-paired-snapshot.v1",
                "pair_id": "read-research-v1:1",
                "head": "fixture-head",
                "staged_diff_sha256": "sha256:" + "1" * 64,
                "unstaged_diff_sha256": "sha256:" + "2" * 64,
                "untracked_manifest_sha256": "sha256:" + "3" * 64,
            })
            snapshot_ref, snapshot_sha = relative_evidence(root, snapshot_path)
            canonical_snapshot = canonical_snapshot_root(root) / snapshot_ref
            canonical_snapshot.parent.mkdir(parents=True, exist_ok=True)
            canonical_snapshot.write_bytes(snapshot_path.read_bytes())
            variant, _, _ = write_variant(
                root, seal, run_sha, workload, 1, "legacy",
                completions=1,
                raw_completion_events=10,
                tokens=1000,
                latency=10.0,
                start_offset=0.0,
                repository_snapshot_ref=snapshot_ref,
                repository_snapshot_sha256=snapshot_sha,
            )
            replay = _replay_variant_receipt(
                root.resolve(),
                ref=variant["receipt_ref"],
                digest=variant["receipt_sha256"],
                pair_id="read-research-v1:1",
                variant="legacy",
                run_seal=seal,
                run_seal_sha256=run_sha,
            )
            self.assertEqual(replay["coordinator_completions"], 10)

    def test_coordination_heavy_variant_cannot_omit_a_required_worker_session(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            seal = write_authority(root, "sha256:" + "a" * 64)
            seal_path = root / "run-seal.json"
            write_json(seal_path, seal)
            run_sha = _sha256(seal_path.read_bytes())
            workload = json.loads((CANARY_FIXTURES / "corpus.v1.json").read_text())["workloads"][0]
            snapshot_path = root / "snapshot.json"
            write_json(snapshot_path, {
                "schema_version": "agent-workflow.canary-paired-snapshot.v1",
                "pair_id": "read-research-v1:1",
                "head": "fixture-head",
                "staged_diff_sha256": "sha256:" + "1" * 64,
                "unstaged_diff_sha256": "sha256:" + "2" * 64,
                "untracked_manifest_sha256": "sha256:" + "3" * 64,
            })
            snapshot_ref, snapshot_sha = relative_evidence(root, snapshot_path)
            canonical_snapshot = canonical_snapshot_root(root) / snapshot_ref
            canonical_snapshot.parent.mkdir(parents=True, exist_ok=True)
            canonical_snapshot.write_bytes(snapshot_path.read_bytes())
            variant, _, _ = write_variant(
                root, seal, run_sha, workload, 1, "vnext",
                completions=4,
                tokens=700,
                latency=9.0,
                start_offset=0.0,
                repository_snapshot_ref=snapshot_ref,
                repository_snapshot_sha256=snapshot_sha,
                worker_transport="codex_exec_jsonl",
            )
            receipt_path = root / variant["receipt_ref"]
            receipt = json.loads(receipt_path.read_text())
            receipt["sessions"] = receipt["sessions"][:-1]
            write_json(receipt_path, receipt)
            with self.assertRaisesRegex(CandidateError, "launch manifest does not equal receipt sessions"):
                _replay_variant_receipt(
                    root.resolve(),
                    ref=variant["receipt_ref"],
                    digest=_sha256(receipt_path.read_bytes()),
                    pair_id="read-research-v1:1",
                    variant="vnext",
                    run_seal=seal,
                    run_seal_sha256=run_sha,
                )

    def test_host_launch_manifest_must_equal_every_reported_session(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            seal = write_authority(root, "sha256:" + "a" * 64)
            seal_path = root / "run-seal.json"
            write_json(seal_path, seal)
            run_sha = _sha256(seal_path.read_bytes())
            workload = json.loads((CANARY_FIXTURES / "corpus.v1.json").read_text())["workloads"][0]
            snapshot_path = root / "snapshot.json"
            write_json(snapshot_path, {
                "schema_version": "agent-workflow.canary-paired-snapshot.v1",
                "pair_id": "read-research-v1:1", "head": "fixture-head",
                "staged_diff_sha256": "sha256:" + "1" * 64,
                "unstaged_diff_sha256": "sha256:" + "2" * 64,
                "untracked_manifest_sha256": "sha256:" + "3" * 64,
            })
            snapshot_ref, snapshot_sha = relative_evidence(root, snapshot_path)
            canonical_snapshot = canonical_snapshot_root(root) / snapshot_ref
            canonical_snapshot.parent.mkdir(parents=True, exist_ok=True)
            canonical_snapshot.write_bytes(snapshot_path.read_bytes())
            variant, _, _ = write_variant(
                root, seal, run_sha, workload, 1, "vnext", completions=4, tokens=700,
                latency=9.0, start_offset=0.0, repository_snapshot_ref=snapshot_ref,
                repository_snapshot_sha256=snapshot_sha, worker_transport="codex_exec_jsonl",
            )
            receipt_path = root / variant["receipt_ref"]
            receipt = json.loads(receipt_path.read_text())
            launches = []
            for ordinal, source in enumerate(receipt["sessions"], start=1):
                export = json.loads((root / source["export_ref"]).read_text())
                launch = json.loads((root / source["launch_ref"]).read_text())
                launches.append({
                    "ordinal": ordinal, "session_id": export["session_id"],
                    "task_id": source["task_id"], "role": source["role"],
                    "transport": launch["transport"], "launch_ref": source["launch_ref"],
                    "launch_sha256": source["launch_sha256"],
                })
            launches.append({
                "ordinal": len(launches) + 1, "session_id": "unreported-started-worker",
                "task_id": "worker-3", "role": "worker", "transport": "codex_exec_jsonl",
                "launch_ref": "evidence/unreported-launch.json",
                "launch_sha256": "sha256:" + "9" * 64,
            })
            manifest = {
                "schema_version": "agent-workflow.canary-host-launch-manifest.v1",
                "run_seal_sha256": run_sha, "pair_id": "read-research-v1:1", "variant": "vnext",
                "workspace_instance_id": receipt["workspace_instance_id"],
                "host_authority_id": seal["host_authority_id"],
                "runtime_bundle_sha256": seal["runtime_bundle_sha256"],
                "launches": launches,
            }
            manifest_path = root / "evidence/read-research-v1/1/vnext/host-launch-manifest.json"
            write_json(manifest_path, manifest)
            manifest_ref, manifest_sha = relative_evidence(root, manifest_path)
            canonical_manifest = canonical_artifact_root(root) / manifest_ref
            canonical_manifest.parent.mkdir(parents=True, exist_ok=True)
            canonical_manifest.write_bytes(manifest_path.read_bytes())
            receipt.update({
                "launch_manifest_ref": manifest_ref, "launch_manifest_sha256": manifest_sha,
                "canonical_launch_manifest_path": str(canonical_manifest),
                "canonical_launch_manifest_sha256": manifest_sha,
            })
            write_json(receipt_path, receipt)
            with self.assertRaisesRegex(CandidateError, "launch manifest does not equal receipt sessions"):
                _replay_variant_receipt(
                    root.resolve(), ref=variant["receipt_ref"], digest=_sha256(receipt_path.read_bytes()),
                    pair_id="read-research-v1:1", variant="vnext", run_seal=seal, run_seal_sha256=run_sha,
                )

    def test_raw_terminal_tokens_must_equal_native_exact_tokens(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            seal = write_authority(root, "sha256:" + "a" * 64)
            seal_path = root / "run-seal.json"
            write_json(seal_path, seal)
            run_sha = _sha256(seal_path.read_bytes())
            workload = json.loads((CANARY_FIXTURES / "corpus.v1.json").read_text())["workloads"][-1]
            snapshot_path = root / "snapshot.json"
            write_json(snapshot_path, {
                "schema_version": "agent-workflow.canary-paired-snapshot.v1", "pair_id": f"{workload['id']}:1",
                "head": "fixture-head", "staged_diff_sha256": "sha256:" + "1" * 64,
                "unstaged_diff_sha256": "sha256:" + "2" * 64,
                "untracked_manifest_sha256": "sha256:" + "3" * 64,
            })
            snapshot_ref, snapshot_sha = relative_evidence(root, snapshot_path)
            canonical_snapshot = canonical_snapshot_root(root) / snapshot_ref
            canonical_snapshot.parent.mkdir(parents=True, exist_ok=True)
            canonical_snapshot.write_bytes(snapshot_path.read_bytes())
            variant, _, _ = write_variant(
                root, seal, run_sha, workload, 1, "legacy", completions=10, tokens=1000,
                latency=10.0, start_offset=0.0, repository_snapshot_ref=snapshot_ref,
                repository_snapshot_sha256=snapshot_sha,
            )
            receipt_path = root / variant["receipt_ref"]
            receipt = json.loads(receipt_path.read_text())
            source = receipt["sessions"][0]
            raw_path = root / source["ref"]
            rows = [json.loads(line) for line in raw_path.read_text().splitlines()]
            for row in rows:
                payload = row.get("payload", {})
                if row.get("type") == "event_msg" and payload.get("type") == "token_count":
                    for field in ("total_token_usage", "last_token_usage"):
                        usage = payload["info"][field]
                        usage["input_tokens"] *= 2
                        usage["total_tokens"] *= 2
            raw_path.write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows))
            raw_sha = _sha256(raw_path.read_bytes())
            export_path = root / source["export_ref"]
            export = json.loads(export_path.read_text())
            Path(export["canonical_raw_path"]).write_bytes(raw_path.read_bytes())
            export["source_prefix_sha256"] = raw_sha
            export["raw_session_sha256"] = raw_sha
            export["canonical_raw_sha256"] = raw_sha
            write_json(export_path, export)
            source["sha256"] = raw_sha
            source["export_sha256"] = _sha256(export_path.read_bytes())
            write_json(receipt_path, receipt)
            with self.assertRaisesRegex(CandidateError, "raw and native token authorities disagree"):
                _replay_variant_receipt(
                    root.resolve(), ref=variant["receipt_ref"], digest=_sha256(receipt_path.read_bytes()),
                    pair_id=f"{workload['id']}:1", variant="legacy", run_seal=seal, run_seal_sha256=run_sha,
                )

    def test_launch_prompt_binding_rejects_additional_message_parts(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            seal = write_authority(root, "sha256:" + "a" * 64)
            seal_path = root / "run-seal.json"
            write_json(seal_path, seal)
            run_sha = _sha256(seal_path.read_bytes())
            workload = json.loads((CANARY_FIXTURES / "corpus.v1.json").read_text())["workloads"][-1]
            snapshot_path = root / "snapshot.json"
            write_json(snapshot_path, {
                "schema_version": "agent-workflow.canary-paired-snapshot.v1", "pair_id": f"{workload['id']}:1",
                "head": "fixture-head", "staged_diff_sha256": "sha256:" + "1" * 64,
                "unstaged_diff_sha256": "sha256:" + "2" * 64,
                "untracked_manifest_sha256": "sha256:" + "3" * 64,
            })
            snapshot_ref, snapshot_sha = relative_evidence(root, snapshot_path)
            canonical_snapshot = canonical_snapshot_root(root) / snapshot_ref
            canonical_snapshot.parent.mkdir(parents=True, exist_ok=True)
            canonical_snapshot.write_bytes(snapshot_path.read_bytes())
            variant, _, _ = write_variant(
                root, seal, run_sha, workload, 1, "legacy", completions=10, tokens=1000,
                latency=10.0, start_offset=0.0, repository_snapshot_ref=snapshot_ref,
                repository_snapshot_sha256=snapshot_sha,
            )
            receipt_path = root / variant["receipt_ref"]
            receipt = json.loads(receipt_path.read_text())
            source = receipt["sessions"][0]
            raw_path = root / source["ref"]
            rows = [json.loads(line) for line in raw_path.read_text().splitlines()]
            message = next(
                row for row in rows
                if row.get("type") == "response_item" and row.get("payload", {}).get("role") == "user"
            )
            message["payload"]["content"].append({"type": "input_image", "image_url": "data:image/png;base64,AA=="})
            raw_path.write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows))
            raw_sha = _sha256(raw_path.read_bytes())
            export_path = root / source["export_ref"]
            export = json.loads(export_path.read_text())
            Path(export["canonical_raw_path"]).write_bytes(raw_path.read_bytes())
            export["source_prefix_sha256"] = raw_sha
            export["raw_session_sha256"] = raw_sha
            export["canonical_raw_sha256"] = raw_sha
            write_json(export_path, export)
            source["sha256"] = raw_sha
            source["export_sha256"] = _sha256(export_path.read_bytes())
            write_json(receipt_path, receipt)
            with self.assertRaisesRegex(CandidateError, "raw user message schema drifted"):
                _replay_variant_receipt(
                    root.resolve(), ref=variant["receipt_ref"], digest=_sha256(receipt_path.read_bytes()),
                    pair_id=f"{workload['id']}:1", variant="legacy", run_seal=seal, run_seal_sha256=run_sha,
                )

    def test_environment_owned_context_may_precede_the_exact_launch_prompt(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            seal = write_authority(root, "sha256:" + "a" * 64)
            seal_path = root / "run-seal.json"
            write_json(seal_path, seal)
            run_sha = _sha256(seal_path.read_bytes())
            workload = json.loads((CANARY_FIXTURES / "corpus.v1.json").read_text())["workloads"][-1]
            snapshot_path = root / "snapshot.json"
            write_json(snapshot_path, {
                "schema_version": "agent-workflow.canary-paired-snapshot.v1", "pair_id": f"{workload['id']}:1",
                "head": "fixture-head", "staged_diff_sha256": "sha256:" + "1" * 64,
                "unstaged_diff_sha256": "sha256:" + "2" * 64,
                "untracked_manifest_sha256": "sha256:" + "3" * 64,
            })
            snapshot_ref, snapshot_sha = relative_evidence(root, snapshot_path)
            canonical_snapshot = canonical_snapshot_root(root) / snapshot_ref
            canonical_snapshot.parent.mkdir(parents=True, exist_ok=True)
            canonical_snapshot.write_bytes(snapshot_path.read_bytes())
            preamble = [
                {"type": "input_text", "text": "# AGENTS.md instructions for /fixture\n\n<INSTRUCTIONS>\nsealed fixture context\n</INSTRUCTIONS>"},
                {"type": "input_text", "text": "<environment_context>\n<cwd>/fixture</cwd>\n</environment_context>"},
            ]
            variant, _, _ = write_variant(
                root, seal, run_sha, workload, 1, "legacy", completions=10, tokens=1000,
                latency=10.0, start_offset=0.0, repository_snapshot_ref=snapshot_ref,
                repository_snapshot_sha256=snapshot_sha, host_preamble=preamble,
            )
            replay = _replay_variant_receipt(
                root.resolve(), ref=variant["receipt_ref"], digest=variant["receipt_sha256"],
                pair_id=f"{workload['id']}:1", variant="legacy", run_seal=seal, run_seal_sha256=run_sha,
            )
            self.assertEqual(replay["total_tokens"], 1000)

    def test_environment_context_preamble_requires_complete_pinned_envelopes(self) -> None:
        with self.assertRaisesRegex(CandidateError, "host context preamble drifted"):
            _explicit_user_prompt([
                [
                    {"type": "input_text", "text": "# AGENTS.md instructions for /fixture\n\n<INSTRUCTIONS>\nunterminated"},
                    {"type": "input_text", "text": "<environment_context>\n<cwd>/fixture</cwd>\n</environment_context> trailing"},
                ],
                [{"type": "input_text", "text": "sealed prompt"}],
            ], "variant")
        valid = [
            {"type": "input_text", "text": "# AGENTS.md instructions for /fixture\n\n<INSTRUCTIONS>\nsealed\n</INSTRUCTIONS>"},
            {"type": "input_text", "text": "<environment_context>\n<cwd>/fixture</cwd>\n</environment_context>"},
        ]
        with self.assertRaisesRegex(CandidateError, "not launch-bound"):
            _explicit_user_prompt([valid, [{"type": "input_text", "text": "sealed prompt"}]], "variant", _sha256(b""))

    def test_real_host_preamble_normalizes_only_the_volatile_arg0_basename(self) -> None:
        normalized_environment = (
            "<environment_context>\n"
            "  <cwd>/fixture</cwd>\n"
            "  <filesystem><entry><path>/host/codex-home/tmp/arg0/codex-arg0&lt;volatile&gt;</path></entry></filesystem>\n"
            "</environment_context>"
        )
        expected = _sha256(_canonical_json([
            {"type": "input_text", "text": normalized_environment},
        ]) + b"\n")
        for token in ("woMvAZ", "occo1l"):
            actual_environment = normalized_environment.replace(
                "codex-arg0&lt;volatile&gt;", f"codex-arg0{token}",
            )
            self.assertEqual(
                _explicit_user_prompt([
                    [{"type": "input_text", "text": actual_environment}],
                    [{"type": "input_text", "text": "sealed prompt"}],
                ], "variant", expected),
                "sealed prompt",
            )
        wrong_prefix = normalized_environment.replace(
            "/host/codex-home/tmp/arg0/codex-arg0&lt;volatile&gt;",
            "/other/codex-home/tmp/arg0/codex-arg0ABC123",
        )
        with self.assertRaisesRegex(CandidateError, "not launch-bound"):
            _explicit_user_prompt([
                [{"type": "input_text", "text": wrong_prefix}],
                [{"type": "input_text", "text": "sealed prompt"}],
            ], "variant", expected)

        duplicated = normalized_environment.replace(
            "</environment_context>",
            "<path>/host/codex-home/tmp/arg0/codex-arg0DEF456</path>\n</environment_context>",
        ).replace("codex-arg0&lt;volatile&gt;", "codex-arg0ABC123")
        with self.assertRaisesRegex(CandidateError, "volatile arg0 authority drifted"):
            _explicit_user_prompt([
                [{"type": "input_text", "text": duplicated}],
                [{"type": "input_text", "text": "sealed prompt"}],
            ], "variant", expected)

    def test_vnext_worker_cannot_use_app_server_transport(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            seal = write_authority(root, "sha256:" + "a" * 64)
            seal_path = root / "run-seal.json"
            write_json(seal_path, seal)
            run_sha = _sha256(seal_path.read_bytes())
            workload = json.loads((CANARY_FIXTURES / "corpus.v1.json").read_text())["workloads"][-1]
            snapshot_path = root / "snapshot.json"
            write_json(snapshot_path, {
                "schema_version": "agent-workflow.canary-paired-snapshot.v1",
                "pair_id": f"{workload['id']}:1",
                "head": "fixture-head",
                "staged_diff_sha256": "sha256:" + "1" * 64,
                "unstaged_diff_sha256": "sha256:" + "2" * 64,
                "untracked_manifest_sha256": "sha256:" + "3" * 64,
            })
            snapshot_ref, snapshot_sha = relative_evidence(root, snapshot_path)
            canonical_snapshot = canonical_snapshot_root(root) / snapshot_ref
            canonical_snapshot.parent.mkdir(parents=True, exist_ok=True)
            canonical_snapshot.write_bytes(snapshot_path.read_bytes())
            with self.assertRaisesRegex(CandidateError, "vnext worker transport"):
                write_variant(
                    root, seal, run_sha, workload, 1, "vnext",
                    completions=4, tokens=700, latency=9.0, start_offset=0.0,
                    repository_snapshot_ref=snapshot_ref,
                    repository_snapshot_sha256=snapshot_sha,
                    worker_transport="app_server",
                )

    def test_unsupported_run_seal_codex_version_cannot_replay_exact(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            seal = write_authority(root, "sha256:" + "a" * 64)
            seal_path = root / "run-seal.json"
            write_json(seal_path, seal)
            run_sha = _sha256(seal_path.read_bytes())
            workload = json.loads((CANARY_FIXTURES / "corpus.v1.json").read_text())["workloads"][-1]
            snapshot_path = root / "snapshot.json"
            write_json(snapshot_path, {
                "schema_version": "agent-workflow.canary-paired-snapshot.v1",
                "pair_id": f"{workload['id']}:1",
                "head": "fixture-head",
                "staged_diff_sha256": "sha256:" + "1" * 64,
                "unstaged_diff_sha256": "sha256:" + "2" * 64,
                "untracked_manifest_sha256": "sha256:" + "3" * 64,
            })
            snapshot_ref, snapshot_sha = relative_evidence(root, snapshot_path)
            canonical_snapshot = canonical_snapshot_root(root) / snapshot_ref
            canonical_snapshot.parent.mkdir(parents=True, exist_ok=True)
            canonical_snapshot.write_bytes(snapshot_path.read_bytes())
            variant, _, _ = write_variant(
                root, seal, run_sha, workload, 1, "legacy",
                completions=10, tokens=1000, latency=10.0, start_offset=0.0,
                repository_snapshot_ref=snapshot_ref,
                repository_snapshot_sha256=snapshot_sha,
            )
            seal["codex_version"] = "codex-cli unsupported"
            with self.assertRaisesRegex(CandidateError, "native event export is not exact replay authority"):
                _replay_variant_receipt(
                    root.resolve(), ref=variant["receipt_ref"], digest=variant["receipt_sha256"],
                    pair_id=f"{workload['id']}:1", variant="legacy", run_seal=seal, run_seal_sha256=run_sha,
                )

    def test_variant_rejects_multiple_turn_contexts(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            seal = write_authority(root, "sha256:" + "a" * 64)
            seal_path = root / "run-seal.json"
            write_json(seal_path, seal)
            run_sha = _sha256(seal_path.read_bytes())
            workload = json.loads((CANARY_FIXTURES / "corpus.v1.json").read_text())["workloads"][-1]
            snapshot_path = root / "snapshot.json"
            write_json(snapshot_path, {
                "schema_version": "agent-workflow.canary-paired-snapshot.v1", "pair_id": f"{workload['id']}:1",
                "head": "fixture-head", "staged_diff_sha256": "sha256:" + "1" * 64,
                "unstaged_diff_sha256": "sha256:" + "2" * 64, "untracked_manifest_sha256": "sha256:" + "3" * 64,
            })
            snapshot_ref, snapshot_sha = relative_evidence(root, snapshot_path)
            canonical_snapshot = canonical_snapshot_root(root) / snapshot_ref
            canonical_snapshot.parent.mkdir(parents=True, exist_ok=True)
            canonical_snapshot.write_bytes(snapshot_path.read_bytes())
            variant, _, _ = write_variant(
                root, seal, run_sha, workload, 1, "legacy", completions=10, tokens=1000,
                latency=10.0, start_offset=0.0, repository_snapshot_ref=snapshot_ref,
                repository_snapshot_sha256=snapshot_sha,
            )
            receipt_path = root / variant["receipt_ref"]
            receipt = json.loads(receipt_path.read_text())
            source = receipt["sessions"][0]
            raw_path = root / source["ref"]
            rows = [json.loads(line) for line in raw_path.read_text().splitlines()]
            turn_context = next(item for item in rows if item.get("type") == "turn_context")
            rows.insert(2, copy.deepcopy(turn_context))
            raw_path.write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows))
            raw_sha = _sha256(raw_path.read_bytes())
            export_path = root / source["export_ref"]
            export = json.loads(export_path.read_text())
            Path(export["canonical_raw_path"]).write_bytes(raw_path.read_bytes())
            export["source_prefix_sha256"] = raw_sha
            export["raw_session_sha256"] = raw_sha
            export["canonical_raw_sha256"] = raw_sha
            write_json(export_path, export)
            source["sha256"] = raw_sha
            source["export_sha256"] = _sha256(export_path.read_bytes())
            write_json(receipt_path, receipt)
            with self.assertRaisesRegex(CandidateError, "terminal boundary drift"):
                _replay_variant_receipt(
                    root.resolve(), ref=variant["receipt_ref"], digest=_sha256(receipt_path.read_bytes()),
                    pair_id=f"{workload['id']}:1", variant="legacy", run_seal=seal, run_seal_sha256=run_sha,
                )

    def test_variant_replays_one_exec_turn_plus_one_app_server_recovery_exactly_once(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            seal = write_authority(root, "sha256:" + "a" * 64)
            seal_path = root / "run-seal.json"
            write_json(seal_path, seal)
            run_sha = _sha256(seal_path.read_bytes())
            workload = json.loads((CANARY_FIXTURES / "corpus.v1.json").read_text())["workloads"][-1]
            pair_id = f"{workload['id']}:1"
            snapshot_path = root / "snapshot.json"
            write_json(snapshot_path, {
                "schema_version": "agent-workflow.canary-paired-snapshot.v1", "pair_id": pair_id,
                "head": "fixture-head", "staged_diff_sha256": "sha256:" + "1" * 64,
                "unstaged_diff_sha256": "sha256:" + "2" * 64,
                "untracked_manifest_sha256": "sha256:" + "3" * 64,
            })
            snapshot_ref, snapshot_sha = relative_evidence(root, snapshot_path)
            canonical_snapshot = canonical_snapshot_root(root) / snapshot_ref
            canonical_snapshot.parent.mkdir(parents=True, exist_ok=True)
            canonical_snapshot.write_bytes(snapshot_path.read_bytes())
            variant, _, _ = write_variant(
                root, seal, run_sha, workload, 1, "vnext", completions=4, tokens=700,
                latency=10.0, start_offset=0.0, repository_snapshot_ref=snapshot_ref,
                repository_snapshot_sha256=snapshot_sha, worker_transport="codex_exec_jsonl",
            )
            receipt_path = root / variant["receipt_ref"]
            receipt = json.loads(receipt_path.read_text())
            source = next(item for item in receipt["sessions"] if item["task_id"] == "worker-1")
            raw_path = root / source["ref"]
            rows = [json.loads(line) for line in raw_path.read_text().splitlines()]
            first_context = next(item for item in rows if item.get("type") == "turn_context")["payload"]
            first_turn = first_context["turn_id"]
            for item in rows:
                payload = item.get("payload") if isinstance(item, dict) else None
                if isinstance(payload, dict) and item.get("type") == "response_item" and payload.get("role") == "user":
                    payload["internal_chat_message_metadata_passthrough"] = {"turn_id": first_turn}
            second_turn = f"turn-{workload['id']}-1-vnext-worker-1-recovery"
            second_prompt = _canonical_json({"pair_id": pair_id, "variant": "vnext", "task": "worker-1-recovery"}).decode()
            second_context = copy.deepcopy(first_context)
            second_context["turn_id"] = second_turn
            last_time = datetime.fromisoformat(rows[-1]["timestamp"])
            rows.extend([
                {"type": "turn_context", "payload": second_context},
                {"type": "response_item", "payload": {
                    "type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": second_prompt}],
                    "internal_chat_message_metadata_passthrough": {"turn_id": second_turn},
                }},
                {"type": "event_msg", "payload": {"type": "token_count", "info": {
                    "total_token_usage": {"input_tokens": 135, "cached_input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0, "total_tokens": 135},
                    "last_token_usage": {"input_tokens": 35, "cached_input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0, "total_tokens": 35},
                }}},
                {"type": "event_msg", "timestamp": (last_time + timedelta(seconds=1)).isoformat(), "payload": {
                    "type": "task_complete", "turn_id": second_turn,
                    "last_agent_message": '{"answer":"recovered"}',
                }},
            ])
            raw_path.write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows))
            raw_sha = _sha256(raw_path.read_bytes())
            Path(json.loads((root / source["export_ref"]).read_text())["canonical_raw_path"]).write_bytes(raw_path.read_bytes())
            initial_export_path = root / source["export_ref"]
            initial_export = json.loads(initial_export_path.read_text())
            for key in ("source_prefix_sha256", "raw_session_sha256", "canonical_raw_sha256"):
                initial_export[key] = raw_sha
            write_json(initial_export_path, initial_export)
            source["sha256"] = raw_sha
            source["export_sha256"] = _sha256(initial_export_path.read_bytes())
            continuation_launch = write_session_launch(
                root, f"evidence/{workload['id']}/1/vnext/worker-1-recovery-launch.json",
                run_seal_sha=run_sha, pair_id=pair_id, variant="vnext", task_id="worker-1-recovery",
                role="worker", model=str(seal["worker_model"]), reasoning_effort=str(seal["reasoning_effort"]),
                transport="app_server", prompt=second_prompt, host_preamble_sha256=_sha256(b""),
            )
            event_ref, event_sha, export_ref, export_sha = write_app_export(
                root, f"evidence/{workload['id']}/1/vnext/worker-1-recovery-export",
                session_id=initial_export["session_id"], run_seal_sha=run_sha,
                runtime_bundle_sha=str(seal["runtime_bundle_sha256"]), raw_ref=source["ref"],
                raw_sha=raw_sha, completions=1, total_tokens=35, turn_id=second_turn,
                prior_tokens=100,
            )
            source["continuations"] = [{
                "attempt_ordinal": 2, "task_id": "worker-1-recovery", "role": "worker",
                "turn_id": second_turn,
                "launch_ref": continuation_launch[0], "launch_sha256": continuation_launch[1],
                "canonical_launch_path": continuation_launch[2], "canonical_launch_sha256": continuation_launch[3],
                "event_ref": event_ref, "event_sha256": event_sha,
                "export_ref": export_ref, "export_sha256": export_sha,
            }]
            launches: list[dict[str, Any]] = []
            for session_source in receipt["sessions"]:
                for attempt_ordinal, attempt in enumerate([session_source, *session_source["continuations"]], start=1):
                    attempt_export = json.loads((root / attempt["export_ref"]).read_text())
                    attempt_launch = json.loads((root / attempt["launch_ref"]).read_text())
                    launches.append({
                        "ordinal": len(launches) + 1, "session_id": attempt_export["session_id"],
                        "attempt_ordinal": attempt_ordinal, "turn_id": attempt_export["turn_ids"][0],
                        "task_id": attempt["task_id"], "role": attempt["role"],
                        "transport": attempt_launch["transport"], "launch_ref": attempt["launch_ref"],
                        "launch_sha256": attempt["launch_sha256"],
                    })
            manifest_path = root / receipt["launch_manifest_ref"]
            manifest = json.loads(manifest_path.read_text())
            manifest["launches"] = launches
            write_json(manifest_path, manifest)
            Path(receipt["canonical_launch_manifest_path"]).write_bytes(manifest_path.read_bytes())
            receipt["launch_manifest_sha256"] = _sha256(manifest_path.read_bytes())
            receipt["canonical_launch_manifest_sha256"] = receipt["launch_manifest_sha256"]
            contract_path = root / receipt["contract_evidence_ref"]
            contract = json.loads(contract_path.read_text())
            contract["terminal_audit"]["process_exit_codes"].append(0)
            contract["terminal_audit"]["native_terminal_statuses"].append("completed")
            contract["terminal_audit"]["typed_output_valid"].append(True)
            process_audit = contract["process_audit"]
            process_audit["owned_processes"] += 1
            process_audit["watchdog_receipts"].append({
                "process_id": f"{pair_id}:vnext:process:recovery",
                "process_group_id": 9999, "terminal_status": "completed", "reaped": True,
                "stdout_sha256": _sha256(b"recovery"), "stderr_sha256": _sha256(b""),
            })
            write_json(contract_path, contract)
            Path(receipt["canonical_contract_evidence_path"]).write_bytes(contract_path.read_bytes())
            receipt["contract_evidence_sha256"] = _sha256(contract_path.read_bytes())
            receipt["canonical_contract_evidence_sha256"] = receipt["contract_evidence_sha256"]
            write_json(receipt_path, receipt)
            metrics = _replay_variant_receipt(
                root, ref=variant["receipt_ref"], digest=_sha256(receipt_path.read_bytes()),
                pair_id=pair_id, variant="vnext", run_seal=seal, run_seal_sha256=run_sha,
            )
            self.assertEqual(metrics["total_tokens"], 735)
            self.assertEqual(metrics["attempt_count"], metrics["session_count"] + 1)
            self.assertEqual(metrics["worker_count"], workload["minimum_worker_sessions"])

    def test_session_launch_role_and_task_are_bound(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            seal = write_authority(root, "sha256:" + "a" * 64)
            seal_path = root / "run-seal.json"
            write_json(seal_path, seal)
            run_sha = _sha256(seal_path.read_bytes())
            workload = json.loads((CANARY_FIXTURES / "corpus.v1.json").read_text())["workloads"][-1]
            snapshot_path = root / "snapshot.json"
            write_json(snapshot_path, {
                "schema_version": "agent-workflow.canary-paired-snapshot.v1", "pair_id": f"{workload['id']}:1",
                "head": "fixture-head", "staged_diff_sha256": "sha256:" + "1" * 64,
                "unstaged_diff_sha256": "sha256:" + "2" * 64, "untracked_manifest_sha256": "sha256:" + "3" * 64,
            })
            snapshot_ref, snapshot_sha = relative_evidence(root, snapshot_path)
            canonical_snapshot = canonical_snapshot_root(root) / snapshot_ref
            canonical_snapshot.parent.mkdir(parents=True, exist_ok=True)
            canonical_snapshot.write_bytes(snapshot_path.read_bytes())
            variant, _, _ = write_variant(
                root, seal, run_sha, workload, 1, "vnext", completions=4, tokens=700,
                latency=9.0, start_offset=0.0, repository_snapshot_ref=snapshot_ref,
                repository_snapshot_sha256=snapshot_sha, worker_transport="codex_exec_jsonl",
            )
            receipt_path = root / variant["receipt_ref"]
            receipt = json.loads(receipt_path.read_text())
            source = receipt["sessions"][1]
            launch_path = root / source["launch_ref"]
            launch = json.loads(launch_path.read_text())
            launch["task_id"] = "different-task"
            write_json(launch_path, launch)
            launch_sha = _sha256(launch_path.read_bytes())
            Path(source["canonical_launch_path"]).write_bytes(launch_path.read_bytes())
            source["launch_sha256"] = launch_sha
            source["canonical_launch_sha256"] = launch_sha
            write_json(receipt_path, receipt)
            with self.assertRaisesRegex(CandidateError, "launch packet authority drifted"):
                _replay_variant_receipt(
                    root.resolve(), ref=variant["receipt_ref"], digest=_sha256(receipt_path.read_bytes()),
                    pair_id=f"{workload['id']}:1", variant="vnext", run_seal=seal, run_seal_sha256=run_sha,
                )

    def test_codex_exec_worker_transport_replays_exact_tokens(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            seal = write_authority(root, "sha256:" + "a" * 64)
            seal_path = root / "run-seal.json"
            write_json(seal_path, seal)
            run_sha = _sha256(seal_path.read_bytes())
            workload = json.loads((CANARY_FIXTURES / "corpus.v1.json").read_text())["workloads"][0]
            snapshot_path = root / "snapshot.json"
            write_json(snapshot_path, {
                "schema_version": "agent-workflow.canary-paired-snapshot.v1",
                "pair_id": "read-research-v1:1",
                "head": "fixture-head",
                "staged_diff_sha256": "sha256:" + "1" * 64,
                "unstaged_diff_sha256": "sha256:" + "2" * 64,
                "untracked_manifest_sha256": "sha256:" + "3" * 64,
            })
            snapshot_ref, snapshot_sha = relative_evidence(root, snapshot_path)
            canonical_snapshot = canonical_snapshot_root(root) / snapshot_ref
            canonical_snapshot.parent.mkdir(parents=True, exist_ok=True)
            canonical_snapshot.write_bytes(snapshot_path.read_bytes())
            variant, _, _ = write_variant(
                root, seal, run_sha, workload, 1, "vnext",
                completions=4,
                tokens=700,
                latency=9.0,
                start_offset=0.0,
                repository_snapshot_ref=snapshot_ref,
                repository_snapshot_sha256=snapshot_sha,
                worker_transport="codex_exec_jsonl",
            )
            replay = _replay_variant_receipt(
                root.resolve(),
                ref=variant["receipt_ref"],
                digest=variant["receipt_sha256"],
                pair_id="read-research-v1:1",
                variant="vnext",
                run_seal=seal,
                run_seal_sha256=run_sha,
            )
            self.assertEqual(replay["total_tokens"], 700)

    def test_coordinator_cannot_claim_codex_exec_worker_transport(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            seal = write_authority(root, "sha256:" + "a" * 64)
            seal_path = root / "run-seal.json"
            write_json(seal_path, seal)
            run_sha = _sha256(seal_path.read_bytes())
            workload = json.loads((CANARY_FIXTURES / "corpus.v1.json").read_text())["workloads"][0]
            snapshot_path = root / "snapshot.json"
            write_json(snapshot_path, {
                "schema_version": "agent-workflow.canary-paired-snapshot.v1",
                "pair_id": "read-research-v1:1",
                "head": "fixture-head",
                "staged_diff_sha256": "sha256:" + "1" * 64,
                "unstaged_diff_sha256": "sha256:" + "2" * 64,
                "untracked_manifest_sha256": "sha256:" + "3" * 64,
            })
            snapshot_ref, snapshot_sha = relative_evidence(root, snapshot_path)
            canonical_snapshot = canonical_snapshot_root(root) / snapshot_ref
            canonical_snapshot.parent.mkdir(parents=True, exist_ok=True)
            canonical_snapshot.write_bytes(snapshot_path.read_bytes())
            variant, _, _ = write_variant(
                root, seal, run_sha, workload, 1, "vnext",
                completions=4,
                tokens=700,
                latency=9.0,
                start_offset=0.0,
                repository_snapshot_ref=snapshot_ref,
                repository_snapshot_sha256=snapshot_sha,
                worker_transport="codex_exec_jsonl",
            )
            receipt_path = root / variant["receipt_ref"]
            receipt = json.loads(receipt_path.read_text())
            coordinator = receipt["sessions"][0]
            coordinator_session = f"{workload['id']}-1-vnext-coordinator"
            event_ref, event_sha, export_ref, export_sha = write_exec_export(
                root,
                "evidence/coordinator-forged-exec",
                session_id=coordinator_session,
                turn_id=f"turn-{coordinator_session}",
                run_seal_sha=run_sha,
                runtime_bundle_sha=str(seal["runtime_bundle_sha256"]),
                raw_ref=coordinator["ref"],
                raw_sha=coordinator["sha256"],
                total_tokens=600,
            )
            coordinator.update({
                "event_ref": event_ref,
                "event_sha256": event_sha,
                "export_ref": export_ref,
                "export_sha256": export_sha,
            })
            write_json(receipt_path, receipt)
            with self.assertRaisesRegex(CandidateError, "host session export provenance drifted"):
                _replay_variant_receipt(
                    root.resolve(),
                    ref=variant["receipt_ref"],
                    digest=_sha256(receipt_path.read_bytes()),
                    pair_id="read-research-v1:1",
                    variant="vnext",
                    run_seal=seal,
                    run_seal_sha256=run_sha,
                )

    def test_one_generic_hidden_source_cannot_satisfy_different_contract_types(self) -> None:
        record = {
            "schema_version": "agent-workflow.canary-subject-qualification.v1",
            "check_id": "AW-H001", "workload_id": "read-research-v1", "trial": 1,
            "variant": "vnext", "subject_receipt_sha256": "sha256:" + "c" * 64,
            "validator_id": f"agent-workflow.hidden-validator.{HIDDEN_EVIDENCE_TYPES['AW-H001']}.v1",
            "validator_bundle_sha256": "sha256:" + "b" * 64,
            "qualification_command_ids": HIDDEN_QUALIFICATION_MAP["AW-H001"],
            "inspected_evidence_sha256": ["sha256:" + "c" * 64],
            "validator_result": {"status": "pass", "observations": {}},
        }
        proof = {
            "schema_version": "agent-workflow.canary-hidden-proof.v1",
            "check_id": "AW-H001",
            "workload_id": "read-research-v1",
            "trial": 1,
            "variant": "vnext",
            "run_seal_sha256": "sha256:" + "a" * 64,
            "evidence_type": HIDDEN_EVIDENCE_TYPES["AW-H001"],
            "validator_id": f"agent-workflow.hidden-validator.{HIDDEN_EVIDENCE_TYPES['AW-H001']}.v1",
            "validator_bundle_sha256": "sha256:" + "b" * 64,
            "qualification_record": record,
            "qualification_record_sha256": _sha256(_canonical_json(record) + b"\n"),
        }
        self.assertTrue(_validate_hidden_proof(
            proof,
            check_id="AW-H001",
            workload_id="read-research-v1",
            trial=1,
            variant_name="vnext",
            run_seal_sha256="sha256:" + "a" * 64,
            runtime_bundle_sha256="sha256:" + "b" * 64,
            expected_record=record,
            qualification_records=None,
        ))
        with self.assertRaisesRegex(CandidateError, "deterministic proof drifted"):
            _validate_hidden_proof(
                proof,
                check_id="AW-H002",
                workload_id="read-research-v1",
                trial=1,
                variant_name="vnext",
                run_seal_sha256="sha256:" + "a" * 64,
                runtime_bundle_sha256="sha256:" + "b" * 64,
                expected_record=record,
                qualification_records=None,
            )

    def test_hidden_proof_cannot_pass_from_caller_authored_facts(self) -> None:
        proof = {
            "schema_version": "agent-workflow.canary-hidden-proof.v1",
            "check_id": "AW-H006", "workload_id": "disjoint-writers-v1", "trial": 1,
            "variant": "vnext", "run_seal_sha256": "sha256:" + "a" * 64,
            "evidence_type": HIDDEN_EVIDENCE_TYPES["AW-H006"],
            "validator_id": f"agent-workflow.hidden-validator.{HIDDEN_EVIDENCE_TYPES['AW-H006']}.v1",
            "validator_bundle_sha256": "sha256:" + "b" * 64,
            "facts": dict(HIDDEN_STATIC_FACTS["AW-H006"]),
        }
        with self.assertRaisesRegex(CandidateError, "deterministic proof drifted"):
            _validate_hidden_proof(
                proof, check_id="AW-H006", workload_id="disjoint-writers-v1", trial=1,
                variant_name="vnext", run_seal_sha256="sha256:" + "a" * 64,
                runtime_bundle_sha256="sha256:" + "b" * 64,
                expected_record={},
                qualification_records=None,
            )

    def test_blind_review_launch_contains_readable_digest_bound_artifacts(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            seal = write_authority(root, "sha256:" + "a" * 64)
            seal_path = root / "run-seal.json"
            write_json(seal_path, seal)
            results = passing_results(root, _sha256(seal_path.read_bytes()), seal)
            first = results["pairs"][0]
            review = json.loads((root / first["blind_review_ref"]).read_text())
            packet = json.loads((root / review["launch_packet_ref"]).read_text())
            corpus = json.loads((CANARY_FIXTURES / "corpus.v1.json").read_text())
            self.assertEqual(packet["workload"], corpus["workloads"][0])
            self.assertEqual(set(packet["label_artifacts"]), {"A", "B"})
            for artifact in packet["label_artifacts"].values():
                self.assertEqual(_sha256(artifact["content"].encode()), artifact["sha256"])
            self.assertEqual(
                _sha256(_canonical_json(packet["rubric"]) + b"\n"),
                packet["rubric_sha256"],
            )
            self.assertTrue(packet["hidden_evidence"])
            for evidence in packet["hidden_evidence"]:
                self.assertEqual(_sha256(_canonical_json(evidence["content"]) + b"\n"), evidence["sha256"])

    def test_independent_verifier_uses_bounded_digest_bound_manifest(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            seal = write_authority(root, "sha256:" + "a" * 64)
            seal_path = root / "run-seal.json"
            write_json(seal_path, seal)
            results = passing_results(root, _sha256(seal_path.read_bytes()), seal)
            verifier = json.loads((root / str(results["verifier_ledger_ref"])).read_text())
            packet = json.loads((root / verifier["launch_packet_ref"]).read_text())
            decision = verifier["decision"]

            self.assertNotIn("hidden_evidence", packet)
            self.assertIn("hidden_evidence_manifest_ref", packet)
            self.assertIn("hidden_evidence_manifest_sha256", packet)
            self.assertLess(len(_canonical_json(packet)), 128_000)
            manifest_path = root / packet["hidden_evidence_manifest_ref"]
            self.assertEqual(_sha256(manifest_path.read_bytes()), packet["hidden_evidence_manifest_sha256"])
            manifest = json.loads(manifest_path.read_text())
            self.assertEqual(manifest["run_seal_sha256"], _sha256(seal_path.read_bytes()))
            self.assertEqual(len(manifest["entries"]), 320)
            for entry in manifest["entries"]:
                self.assertEqual(
                    _sha256((root / entry["reader_ref"]).read_bytes()),
                    entry["content_sha256"],
                )
            self.assertEqual(
                decision["accepted_hidden_evidence_manifest_sha256"],
                packet["hidden_evidence_manifest_sha256"],
            )
            self.assertNotIn("accepted_hidden_evidence_sha256", decision)

    def test_independent_verifier_manifest_tampering_fails_closed(self) -> None:
        for mutation in ("missing", "duplicate", "ref_substitution"):
            with self.subTest(mutation=mutation), TemporaryDirectory() as raw:
                root = Path(raw)
                seal = write_authority(root, "sha256:" + "a" * 64)
                seal_path = root / "run-seal.json"
                write_json(seal_path, seal)
                results = passing_results(root, _sha256(seal_path.read_bytes()), seal)
                verifier_path = root / str(results["verifier_ledger_ref"])
                verifier = json.loads(verifier_path.read_text())
                packet_path = root / verifier["launch_packet_ref"]
                packet = json.loads(packet_path.read_text())
                manifest_path = root / packet["hidden_evidence_manifest_ref"]
                manifest = json.loads(manifest_path.read_text())
                if mutation == "missing":
                    manifest["entries"].pop()
                elif mutation == "duplicate":
                    manifest["entries"].append(copy.deepcopy(manifest["entries"][-1]))
                else:
                    manifest["entries"][0]["authority_ref"] = manifest["entries"][1]["authority_ref"]
                write_json(manifest_path, manifest)
                manifest_sha = _sha256(manifest_path.read_bytes())
                packet["hidden_evidence_manifest_sha256"] = manifest_sha
                packet["hidden_evidence_index"] = run_vnext_canary.hidden_evidence_verifier_index(manifest)
                write_json(packet_path, packet)
                verifier["launch_packet_sha256"] = _sha256(packet_path.read_bytes())
                verifier["decision"]["accepted_hidden_evidence_manifest_sha256"] = manifest_sha
                raw_ref, raw_sha = write_raw_session(
                    root,
                    str(verifier["raw_session_ref"]),
                    session_id="independent-verifier",
                    model=str(seal["top_model"]),
                    effort=str(seal["reasoning_effort"]),
                    duration=2.0,
                    start_offset=1000.0,
                    input_message=_canonical_json(packet).decode(),
                    terminal_message=_canonical_json(verifier["decision"]).decode(),
                )
                verifier["raw_session_ref"] = raw_ref
                verifier["raw_session_sha256"] = raw_sha
                verifier["canonical_raw_path"] = str(canonical_session_root(root) / raw_ref)
                verifier["canonical_raw_sha256"] = raw_sha
                write_json(verifier_path, verifier)
                results["verifier_ledger_sha256"] = _sha256(verifier_path.read_bytes())
                with self.assertRaisesRegex(CandidateError, "manifest does not cover the exact hidden-evidence set"):
                    write_host_results(root, seal_path, results)

    def test_latency_gate_uses_median_of_paired_ratios(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            seal = write_authority(root, "sha256:" + "a" * 64)
            seal_path = root / "run-seal.json"
            write_json(seal_path, seal)
            legacy = [1.0, 1.0, 100.0, 100.0, 100.0] * 5
            vnext = [1.05, 1.05, 105.0, 105.0, 105.0] * 5
            results_path = write_host_results(root, seal_path, passing_results(root, _sha256(seal_path.read_bytes()), seal, legacy_latency=legacy, vnext_latency=vnext))
            result = evaluate_results(seal_path, results_path)
            self.assertAlmostEqual(result["metrics"]["aggregate_latency_ratio"], 1.05)

    def test_review_raw_session_and_hidden_evidence_are_digest_bound(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            seal = write_authority(root, "sha256:" + "a" * 64)
            seal_path = root / "run-seal.json"
            write_json(seal_path, seal)
            results = passing_results(root, _sha256(seal_path.read_bytes()), seal)
            results_path = write_host_results(root, seal_path, results)
            first = results["pairs"][0]
            review_path = root / first["blind_review_ref"]
            review = json.loads(review_path.read_text())
            raw_review_path = root / review["raw_session_ref"]
            raw_review_path.write_text(raw_review_path.read_text() + "{}\n")
            with self.assertRaisesRegex(CandidateError, "raw session.*drifted"):
                evaluate_results(seal_path, results_path)
            raw_review_path.write_text(raw_review_path.read_text()[:-3])
            check = next(iter(first["variants"]["vnext"]["hard_checks"].values()))
            (root / check["evidence_ref"]).write_text("tampered")
            with self.assertRaisesRegex(CandidateError, "hidden check.*drifted"):
                evaluate_results(seal_path, results_path)

    def test_canonical_raw_copy_drift_is_rejected_independently(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            seal = write_authority(root, "sha256:" + "a" * 64)
            seal_path = root / "run-seal.json"
            write_json(seal_path, seal)
            core = passing_results(root, _sha256(seal_path.read_bytes()), seal)
            results_path = write_host_results(root, seal_path, core)
            first = core["pairs"][0]["variants"]["vnext"]
            receipt = json.loads((root / first["receipt_ref"]).read_text())
            export = json.loads((root / receipt["sessions"][0]["export_ref"]).read_text())
            canonical = Path(export["canonical_raw_path"])
            canonical.write_bytes(canonical.read_bytes() + b"{}\n")
            with self.assertRaisesRegex(CandidateError, "canonical store"):
                evaluate_results(seal_path, results_path)

    def test_post_seal_environment_drift_fails_closed(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            seal = write_authority(root, "sha256:" + "a" * 64)
            seal_path = root / "run-seal.json"
            write_json(seal_path, seal)
            results_path = write_host_results(root, seal_path, passing_results(root, _sha256(seal_path.read_bytes()), seal))
            (root / str(seal["host_profile_ref"])).write_text('{"schema_version":"drift"}\n')
            with self.assertRaisesRegex(CandidateError, "host profile.*drifted"):
                evaluate_results(seal_path, results_path)

    def test_raw_session_that_predates_run_seal_is_rejected(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            seal = write_authority(root, "sha256:" + "a" * 64)
            seal_path = root / "run-seal.json"
            write_json(seal_path, seal)
            results = passing_results(root, _sha256(seal_path.read_bytes()), seal)
            first = results["pairs"][0]
            review_path = root / first["blind_review_ref"]
            review = json.loads(review_path.read_text())
            raw_path = root / review["raw_session_ref"]
            rows = [json.loads(line) for line in raw_path.read_text().splitlines()]
            rows[0]["payload"]["timestamp"] = "2026-07-11T22:00:00+00:00"
            raw_path.write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows))
            Path(review["canonical_raw_path"]).write_bytes(raw_path.read_bytes())
            review["raw_session_sha256"] = _sha256(raw_path.read_bytes())
            review["canonical_raw_sha256"] = review["raw_session_sha256"]
            write_json(review_path, review)
            first["blind_review_sha256"] = _sha256(review_path.read_bytes())
            with self.assertRaisesRegex(CandidateError, "reviewer raw session.*drifted"):
                write_host_results(root, seal_path, results)

    def test_reviewer_terminal_before_session_start_is_rejected(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            seal = write_authority(root, "sha256:" + "a" * 64)
            seal_path = root / "run-seal.json"
            write_json(seal_path, seal)
            results = passing_results(root, _sha256(seal_path.read_bytes()), seal)
            first = results["pairs"][0]
            review_path = root / first["blind_review_ref"]
            review = json.loads(review_path.read_text())
            raw_path = root / review["raw_session_ref"]
            rows = [json.loads(line) for line in raw_path.read_text().splitlines()]
            rows[-1]["timestamp"] = "2026-07-11T23:30:00+00:00"
            raw_path.write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows))
            Path(review["canonical_raw_path"]).write_bytes(raw_path.read_bytes())
            review["raw_session_sha256"] = _sha256(raw_path.read_bytes())
            review["canonical_raw_sha256"] = review["raw_session_sha256"]
            write_json(review_path, review)
            first["blind_review_sha256"] = _sha256(review_path.read_bytes())
            with self.assertRaisesRegex(CandidateError, "reviewer raw session.*drifted"):
                write_host_results(root, seal_path, results)

    def test_blocked_external_p2_cannot_pass_promotion(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            seal = write_authority(root, "sha256:" + "a" * 64)
            seal_path = root / "run-seal.json"
            write_json(seal_path, seal)
            results = passing_results(root, _sha256(seal_path.read_bytes()), seal)
            verifier_path = root / str(results["verifier_ledger_ref"])
            verifier = json.loads(verifier_path.read_text())
            detail_path = root / "verifier/p2-detail.json"
            write_json(detail_path, {
                "schema_version": "agent-workflow.canary-p2-resolution.v1",
                "finding_id": "P2-host-primitive",
                "resolution": "blocked_external",
                "external_dependency": "host primitive is unavailable",
                "owner": "host-runtime",
                "promotion_gate": "host primitive exists",
                "gate_status": "passed",
            })
            detail_ref, detail_sha = relative_evidence(root, detail_path)
            verifier["decision"]["P2"] = [{
                "id": "P2-host-primitive",
                "resolution": "blocked_external",
                "owner": "host-runtime",
                "promotion_gate": "host primitive exists",
                "gate_status": "passed",
                "detail_ref": detail_ref,
                "detail_sha256": detail_sha,
            }]
            raw_ref, raw_sha = write_raw_session(
                root,
                str(verifier["raw_session_ref"]),
                session_id="independent-verifier",
                model=str(seal["top_model"]),
                effort=str(seal["reasoning_effort"]),
                duration=2.0,
                start_offset=1000.0,
                input_message=_canonical_json(json.loads((root / verifier["launch_packet_ref"]).read_text())).decode(),
                terminal_message=_canonical_json(verifier["decision"]).decode(),
            )
            verifier["raw_session_ref"] = raw_ref
            verifier["raw_session_sha256"] = raw_sha
            verifier["canonical_raw_path"] = str(canonical_session_root(root) / raw_ref)
            verifier["canonical_raw_sha256"] = raw_sha
            write_json(verifier_path, verifier)
            results["verifier_ledger_sha256"] = _sha256(verifier_path.read_bytes())
            results_path = write_host_results(root, seal_path, results)
            evaluated = evaluate_results(seal_path, results_path)
            self.assertEqual(evaluated["status"], "fail")
            self.assertIn("externally blocked", " ".join(evaluated["failures"]))

    def test_fixed_p2_rejects_arbitrary_repair_and_reverify_bytes(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            repair = root / "repair.txt"
            reverify = root / "reverify.txt"
            repair.write_text("arbitrary")
            reverify.write_text("arbitrary")
            canonical_repair = canonical_artifact_root(root) / "repair.txt"
            canonical_reverify = canonical_artifact_root(root) / "reverify.txt"
            canonical_repair.write_bytes(repair.read_bytes())
            canonical_reverify.write_bytes(reverify.read_bytes())
            detail = {
                "schema_version": "agent-workflow.canary-p2-resolution.v1",
                "finding_id": "P2-x",
                "resolution": "fixed",
                "repair_refs": [{"ref": "repair.txt", "sha256": _sha256(repair.read_bytes()), "canonical_path": str(canonical_repair), "canonical_sha256": _sha256(repair.read_bytes())}],
                "reverify_refs": [{"ref": "reverify.txt", "sha256": _sha256(reverify.read_bytes()), "canonical_path": str(canonical_reverify), "canonical_sha256": _sha256(reverify.read_bytes())}],
            }
            detail_path = root / "detail.json"
            write_json(detail_path, detail)
            finding = {
                "id": "P2-x",
                "resolution": "fixed",
                "owner": "runtime",
                "promotion_gate": "reverify",
                "gate_status": "passed",
                "detail_ref": "detail.json",
                "detail_sha256": _sha256(detail_path.read_bytes()),
            }
            with self.assertRaisesRegex(CandidateError, "must be valid JSON"):
                _validate_p2_detail(root, finding, write_authority(root, "sha256:" + "a" * 64), "independent-verifier")

    def test_p2_gate_evidence_binds_the_exact_qualification_record(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            seal = write_authority(root, "sha256:" + "a" * 64)
            evidence = {
                "schema_version": "agent-workflow.canary-p2-evidence.v1",
                "finding_id": "P2-gate",
                "evidence_kind": "promotion_gate",
                "status": "passed",
                "owner": "runtime",
                "promotion_gate": "runtime qualification",
                "verifier_session_id": "independent-verifier",
                "validator_command_id": "runtime",
                "qualification_record_sha256": "sha256:" + "1" * 64,
            }
            evidence_path = root / "gate-evidence.json"
            write_json(evidence_path, evidence)
            canonical = canonical_artifact_root(root) / "gate-evidence.json"
            canonical.write_bytes(evidence_path.read_bytes())
            detail = {
                "schema_version": "agent-workflow.canary-p2-resolution.v1",
                "finding_id": "P2-gate",
                "resolution": "deferred_with_owner_gate",
                "owner": "runtime",
                "promotion_gate": "runtime qualification",
                "gate_status": "passed",
                "gate_evidence_ref": "gate-evidence.json",
                "gate_evidence_sha256": _sha256(evidence_path.read_bytes()),
                "gate_evidence_canonical_path": str(canonical),
                "gate_evidence_canonical_sha256": _sha256(canonical.read_bytes()),
            }
            detail_path = root / "gate-detail.json"
            write_json(detail_path, detail)
            finding = {
                "id": "P2-gate",
                "resolution": "deferred_with_owner_gate",
                "owner": "runtime",
                "promotion_gate": "runtime qualification",
                "gate_status": "passed",
                "detail_ref": "gate-detail.json",
                "detail_sha256": _sha256(detail_path.read_bytes()),
            }
            with self.assertRaisesRegex(CandidateError, "qualification record drifted"):
                _validate_p2_detail(
                    root,
                    finding,
                    seal,
                    "independent-verifier",
                    {"runtime": "sha256:" + "2" * 64},
                )
            _validate_p2_detail(
                root,
                finding,
                seal,
                "independent-verifier",
                {"runtime": "sha256:" + "1" * 64},
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
