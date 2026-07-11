#!/usr/bin/env python3
"""Run source-owned deterministic workflow operations as one typed receipt.

The controller composes portable file/schema/validation/accounting helpers. It
never spawns agents, waits on native agents, queues dispatch, rotates sessions,
or claims host-terminal atomicity.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from clean_orchestrator import CONTROLLER_RECEIPT_SCHEMA
from token_accounting import (
    TokenAccountingError,
    default_runtime_root,
    locate_session,
    register_agent,
    start_accounting,
)
from runtime_harness import event_line, normalized_tool_name, raw_text, structured_target


MAX_OUTPUT_CHARS = 2048
TERMINAL_PROJECTIONS = (
    "token-usage.json",
    "token-evidence.json",
    "runtime-observations.json",
    "runner-evidence.json",
    "final-report.md",
)
TERMINAL_COMMIT_MANIFEST = "terminal-commit.json"
ACCOUNTING_START_SCHEMA = "agent-workflow.accounting-start.v1"
ACCOUNTING_START_MANIFEST = "accounting-start.json"
HOST_OWNED_BOUNDARIES = [
    "native_spawn",
    "native_wait_or_join",
    "queued_dispatch",
    "generation_rotation",
    "terminal_host_finalization",
]


class LedgerSnapshotError(OSError):
    def __init__(
        self,
        message: str,
        originals: dict[str, bytes | None],
        state: dict[str, dict[str, Any]],
    ) -> None:
        super().__init__(message)
        self.originals = originals
        self.state = state


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def internal_step(
    name: str,
    command: list[str],
    *,
    status: str,
    output: str,
) -> dict[str, Any]:
    return {
        "name": name,
        "command": command,
        "status": status,
        "exit_code": 0 if status == "pass" else 1,
        "output_sha256": "sha256:" + hashlib.sha256(output.encode()).hexdigest(),
        "output_excerpt": output[-MAX_OUTPUT_CHARS:],
    }


def resolve_accounting_start_manifest(
    workflow_dir: Path, manifest_path: Path | None
) -> Path:
    if manifest_path is None:
        return workflow_dir / ACCOUNTING_START_MANIFEST
    if manifest_path.is_absolute():
        return manifest_path
    return workflow_dir / manifest_path


def validate_accounting_start_manifest(
    workflow_dir: Path, manifest_path: Path | None = None
) -> tuple[Path, list[dict[str, str]]]:
    path = resolve_accounting_start_manifest(workflow_dir, manifest_path)
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
        orchestration = json.loads(
            (workflow_dir / "orchestration.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"accounting start input is unreadable: {exc}") from exc
    if not isinstance(value, dict) or set(value) != {"schema_version", "participants"}:
        raise ValueError(
            "accounting start input must contain exactly schema_version and participants"
        )
    if value.get("schema_version") != ACCOUNTING_START_SCHEMA:
        raise ValueError(
            f"accounting start input schema_version must be {ACCOUNTING_START_SCHEMA}"
        )
    participants = value.get("participants")
    if not isinstance(participants, list) or not 1 <= len(participants) <= 64:
        raise ValueError("accounting start participants must contain 1..64 attempts")
    planned: set[tuple[str, str]] = set()
    rounds = orchestration.get("rounds") if isinstance(orchestration, dict) else None
    if not isinstance(rounds, list):
        raise ValueError("orchestration rounds are required for accounting start")
    for round_plan in rounds:
        if not isinstance(round_plan, dict) or not isinstance(round_plan.get("round_id"), str):
            continue
        lanes = round_plan.get("lanes")
        if not isinstance(lanes, list):
            continue
        for lane in lanes:
            if (
                isinstance(lane, dict)
                and lane.get("enabled") is True
                and isinstance(lane.get("id"), str)
            ):
                planned.add((round_plan["round_id"], lane["id"]))
    required_keys = {
        "execution_ref",
        "agent_id",
        "round_id",
        "lane_id",
        "registration_mode",
    }
    normalized: list[dict[str, str]] = []
    execution_refs: set[str] = set()
    for index, participant in enumerate(participants):
        label = f"participants[{index}]"
        if not isinstance(participant, dict) or set(participant) != required_keys:
            raise ValueError(f"{label} must contain exactly {sorted(required_keys)}")
        if any(
            not isinstance(participant.get(key), str) or not participant[key].strip()
            for key in required_keys
        ):
            raise ValueError(f"{label} fields must be non-empty strings")
        if participant["registration_mode"] not in {
            "new_session",
            "reuse_existing_session",
        }:
            raise ValueError(
                f"{label}.registration_mode must be new_session or reuse_existing_session"
            )
        execution_ref = participant["execution_ref"]
        if execution_ref in execution_refs:
            raise ValueError(f"duplicate execution_ref: {execution_ref}")
        execution_refs.add(execution_ref)
        lane_ref = (participant["round_id"], participant["lane_id"])
        if lane_ref not in planned:
            raise ValueError(
                f"{label} must bind an enabled planned lane: {lane_ref[0]}:{lane_ref[1]}"
            )
        normalized.append({key: participant[key].strip() for key in required_keys})
    return path, normalized


def snapshot_original_ledgers(
    workflow_dir: Path,
) -> tuple[dict[str, bytes | None], dict[str, dict[str, Any]]]:
    names = ("token-usage.json", "token-evidence.json")
    originals: dict[str, bytes | None] = {name: None for name in names}
    state: dict[str, dict[str, Any]] = {
        name: {"present": None, "snapshot_error": "not_determined"}
        for name in names
    }
    for name in names:
        path = workflow_dir / name
        try:
            path.lstat()
        except FileNotFoundError:
            state[name] = {"present": False}
        except OSError as exc:
            state[name] = {
                "present": None,
                "snapshot_error": str(exc),
            }
            raise LedgerSnapshotError(
                f"cannot determine original ledger presence for {name}: {exc}",
                originals,
                state,
            ) from exc
        else:
            state[name] = {"present": True, "sha256": None}
    for name in names:
        if state[name]["present"] is not True:
            continue
        path = workflow_dir / name
        try:
            payload = path.read_bytes()
        except OSError as exc:
            state[name]["snapshot_error"] = str(exc)
            raise LedgerSnapshotError(
                f"cannot read original ledger {name}: {exc}",
                originals,
                state,
            ) from exc
        originals[name] = payload
        state[name] = {
            "present": True,
            "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
        }
    return originals, state


def bind_raw_wait_evidence(
    workflow_dir: Path, *, runtime_root: Path | None = None
) -> dict[str, Any]:
    usage = json.loads((workflow_dir / "token-usage.json").read_text(encoding="utf-8"))
    accounting = usage.get("accounting") if isinstance(usage, dict) else None
    if not isinstance(accounting, dict) or accounting.get("runtime") != "codex":
        raise ValueError("raw wait binding requires started Codex accounting")
    lead_session_id = accounting.get("lead_session_id")
    participants = accounting.get("participants")
    if not isinstance(lead_session_id, str) or not isinstance(participants, list):
        raise ValueError("raw wait binding requires one Lead and participant declarations")
    evidence = json.loads((workflow_dir / "token-evidence.json").read_text(encoding="utf-8"))
    start = evidence.get("lead", {}).get("start") if isinstance(evidence, dict) else None
    if not isinstance(start, dict):
        raise ValueError("raw wait binding requires the Lead start snapshot")
    start_line = event_line(start.get("event_ref"), lead_session_id)
    root = (runtime_root or default_runtime_root("codex")).expanduser().resolve()
    lead_path = locate_session("codex", lead_session_id, root, lead=True)
    calls: list[dict[str, Any]] = []
    non_collaboration_calls: list[dict[str, Any]] = []
    outputs: dict[str, dict[str, Any]] = {}
    agent_messages: list[dict[str, Any]] = []
    with lead_path.open("rb") as handle:
        for line_number, raw in enumerate(handle, start=1):
            if line_number <= start_line:
                continue
            item = json.loads(raw)
            payload = item.get("payload") if isinstance(item, dict) else None
            if not isinstance(payload, dict) or item.get("type") != "response_item":
                continue
            payload_type = payload.get("type")
            if payload_type == "agent_message":
                agent_messages.append(
                    {
                        "line": line_number,
                        "author": payload.get("author"),
                        "content": raw_text(payload.get("content")),
                        "timestamp": str(item.get("timestamp") or ""),
                    }
                )
                continue
            call_id = payload.get("call_id")
            if not isinstance(call_id, str):
                continue
            if payload_type in {"custom_tool_call", "function_call"}:
                tool_name = normalized_tool_name(payload.get("name"))
                if tool_name.endswith("followup_task") or tool_name.endswith("wait_agent"):
                    calls.append(
                        {
                            "call_id": call_id,
                            "tool_name": tool_name,
                            "line": line_number,
                            "input": payload.get("input") or payload.get("arguments"),
                            "timestamp": str(item.get("timestamp") or ""),
                        }
                    )
                else:
                    non_collaboration_calls.append(
                        {
                            "call_id": call_id,
                            "tool_name": tool_name,
                            "line": line_number,
                        }
                    )
            elif payload_type in {"custom_tool_call_output", "function_call_output"}:
                outputs[call_id] = {
                    "line": line_number,
                    "timestamp": str(item.get("timestamp") or ""),
                    "output": raw_text(payload.get("output")),
                }
    followups = [item for item in calls if item["tool_name"].endswith("followup_task")]
    waits = [item for item in calls if item["tool_name"].endswith("wait_agent")]
    if len(followups) != 1 or len(waits) != 1:
        raise ValueError("raw wait binding requires exactly one followup_task and one wait_agent")
    wait_call = waits[0]
    wait_output = outputs.get(wait_call["call_id"])
    if not isinstance(wait_output, dict):
        raise ValueError("raw wait binding cannot find the wait_agent result")
    try:
        wait_result = json.loads(wait_output["output"])
    except json.JSONDecodeError as exc:
        raise ValueError("raw wait binding requires structured wait output") from exc
    if not isinstance(wait_result, dict) or wait_result.get("timed_out") is not False:
        raise ValueError("raw wait binding requires one non-timeout terminal wait")
    followup_call = followups[0]
    if not (
        followup_call["line"] < wait_call["line"] < wait_output["line"]
    ):
        raise ValueError("raw wait binding requires followup, wait, and wait result ordering")
    runner_path = workflow_dir / "runner-evidence.json"
    runner = json.loads(runner_path.read_text(encoding="utf-8"))
    agents = runner.get("agents") if isinstance(runner, dict) else None
    if not isinstance(agents, list) or len(agents) != len(participants):
        raise ValueError("runner skeleton must bind every declared participant before start")
    participant_ids = {
        item.get("agent_id") for item in participants if isinstance(item, dict)
    }
    runner_ids = {item.get("agent_id") for item in agents if isinstance(item, dict)}
    if participant_ids != runner_ids or None in participant_ids:
        raise ValueError("runner skeleton identities drift from accounting participants")
    followup_target = structured_target(followup_call.get("input"))
    matched_agents = [
        item
        for item in agents
        if isinstance(item, dict)
        and followup_target in {item.get("agent_id"), item.get("native_handle")}
    ]
    if len(matched_agents) != 1:
        raise ValueError("raw followup target must bind exactly one declared runner identity")
    next_action_lines = [
        item["line"]
        for item in non_collaboration_calls
        if item["line"] > followup_call["line"]
    ]
    terminal_boundary = min(next_action_lines) if next_action_lines else float("inf")
    if terminal_boundary <= wait_output["line"]:
        raise ValueError(
            "raw wait binding encountered a non-collaboration action before wait completion"
        )
    terminal_authors = {
        item.get("author")
        for item in agent_messages
        if followup_call["line"] < item["line"] < terminal_boundary
        and re.match(
            r"\A\s*Message Type:\s*FINAL_ANSWER\s*(?:\r?\n|\Z)",
            item.get("content", ""),
        )
    }
    matched_handle = matched_agents[0].get("native_handle")
    if not isinstance(matched_handle, str) or matched_handle not in terminal_authors:
        raise ValueError(
            "raw wait result lacks a terminal agent message from the declared runner identity"
        )
    targets = sorted(str(item) for item in participant_ids)
    for agent in agents:
        agent.update(
            {
                "wait_status": "completed",
                "close_status": "closed",
                "status": "terminal",
            }
        )
    efficiency = runner.get("execution_efficiency")
    if not isinstance(efficiency, dict):
        raise ValueError("runner skeleton lacks execution_efficiency")
    round_ids = {
        item.get("round_id") for item in participants if isinstance(item, dict)
    }
    if len(round_ids) != 1 or not isinstance(next(iter(round_ids)), str):
        raise ValueError("one compound wait wave must bind exactly one round")
    round_id = str(next(iter(round_ids)))
    efficiency.update(
        {
            "lead_model_completions": 4,
            "status_only_completions": 0,
            "functions_wait_calls": 1,
            "wait_waves": [
                {
                    "round_id": round_id,
                    "wave_id": f"wave-{round_id}-terminal-001",
                    "barrier_id": f"barrier-{round_id}-terminal",
                    "trigger_ref": f"codex:{lead_session_id}:{wait_call['call_id']}",
                    "targets": targets,
                    "terminal_targets": targets,
                    "timeout_ms": 3600000,
                    "outcome": "completed",
                    "started_at": wait_call["timestamp"],
                    "completed_at": wait_output["timestamp"],
                    "trigger": "dispatch",
                }
            ],
        }
    )
    atomic_write(runner_path, runner)
    return {
        "lead_session_id": lead_session_id,
        "followup_call_id": followups[0]["call_id"],
        "wait_call_id": wait_call["call_id"],
        "participant_count": len(participants),
    }


def integrate_terminal_lane_outputs(workflow_dir: Path) -> dict[str, Any]:
    state_path = workflow_dir / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    orchestration = json.loads(
        (workflow_dir / "orchestration.json").read_text(encoding="utf-8")
    )
    current_round = state.get("current_round") if isinstance(state, dict) else None
    if not isinstance(current_round, str):
        raise ValueError("terminal integration requires state.current_round")
    round_plan = next(
        (
            item
            for item in orchestration.get("rounds", [])
            if isinstance(item, dict) and item.get("round_id") == current_round
        ),
        None,
    )
    if not isinstance(round_plan, dict):
        raise ValueError("terminal integration cannot find the current round plan")
    enabled = [
        item
        for item in round_plan.get("lanes", [])
        if isinstance(item, dict) and item.get("enabled") is True
    ]
    outputs: list[dict[str, Any]] = []
    for lane in enabled:
        lane_id = lane.get("id")
        if not isinstance(lane_id, str):
            raise ValueError("terminal integration found an invalid enabled lane")
        path = workflow_dir / "rounds" / current_round / "lane-runs" / f"{lane_id}.json"
        try:
            output = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"terminal integration cannot read {lane_id}: {exc}") from exc
        if not isinstance(output, dict) or output.get("status") != "complete":
            raise ValueError(f"terminal integration requires complete output: {lane_id}")
        outputs.append(output)
    if not outputs:
        raise ValueError("terminal integration requires at least one enabled lane output")
    pass_gate = all(
        isinstance(item.get("gate"), dict)
        and item["gate"].get("decision") == "pass"
        for item in outputs
    )
    verification_evidence: list[dict[str, Any]] = []
    remaining_risks: list[str] = []
    findings: list[dict[str, Any]] = []
    for output in outputs:
        payload = output.get("payload") if isinstance(output.get("payload"), dict) else {}
        if output.get("lane") == "verify":
            for check in payload.get("checks", []):
                if isinstance(check, dict):
                    verification_evidence.append(
                        {
                            "check": check.get("name"),
                            "status": check.get("status"),
                            "evidence": check.get("evidence"),
                        }
                    )
        for risk in payload.get("remaining_uncertainty", []):
            if isinstance(risk, str) and risk.strip():
                remaining_risks.append(risk.strip())
        for finding in output.get("findings", []):
            if isinstance(finding, dict):
                findings.append(finding)
    integration = {
        "schema_version": "agent-loops.integration.v1",
        "round_id": current_round,
        "status": "complete" if pass_gate else "blocked",
        "accepted": [
            str(item.get("summary"))
            for item in outputs
            if isinstance(item.get("gate"), dict)
            and item["gate"].get("decision") == "pass"
        ],
        "rejected": [
            str(item.get("summary"))
            for item in outputs
            if not isinstance(item.get("gate"), dict)
            or item["gate"].get("decision") != "pass"
        ],
        "conflicts": [],
        "repair_packets": [
            finding["repair_packet"]
            for finding in findings
            if isinstance(finding.get("repair_packet"), dict)
        ],
        "finding_resolutions": [],
        "verification_evidence": verification_evidence,
        "remaining_risks": remaining_risks,
        "next_round": None,
        "stop_reason": "verify_pass" if pass_gate else "blocked",
    }
    round_state = next(
        (
            item
            for item in state.get("rounds", [])
            if isinstance(item, dict) and item.get("round_id") == current_round
        ),
        None,
    )
    if not isinstance(round_state, dict):
        raise ValueError("terminal integration cannot find current round state")
    round_state["status"] = "complete" if pass_gate else "blocked"
    round_state["gate_decision"] = "pass" if pass_gate else "blocked"
    state["status"] = "complete" if pass_gate else "blocked"
    state["final_status"] = "complete" if pass_gate else "blocked"
    atomic_write(
        workflow_dir / "rounds" / current_round / "integration.json",
        integration,
    )
    atomic_write(state_path, state)
    return {
        "round_id": current_round,
        "lane_count": len(outputs),
        "gate_decision": "pass" if pass_gate else "blocked",
        "verification_evidence_count": len(verification_evidence),
    }


def write_terminal_commit_manifest(workflow_dir: Path) -> dict[str, Any]:
    projections = {
        name: "sha256:" + hashlib.sha256((workflow_dir / name).read_bytes()).hexdigest()
        for name in TERMINAL_PROJECTIONS
    }
    revision = "sha256:" + hashlib.sha256(
        json.dumps(projections, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    manifest = {
        "schema_version": "agent-workflow.terminal-commit.v1",
        "status": "committed",
        "revision": revision,
        "projections": projections,
    }
    atomic_write(workflow_dir / TERMINAL_COMMIT_MANIFEST, manifest)
    return manifest


def command_step(name: str, command: list[str], *, cwd: Path) -> dict[str, Any]:
    result = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    output = result.stdout or ""
    return {
        "name": name,
        "command": command,
        "status": "pass" if result.returncode == 0 else "fail",
        "exit_code": result.returncode,
        "output_sha256": "sha256:" + hashlib.sha256(output.encode()).hexdigest(),
        "output_excerpt": output[-MAX_OUTPUT_CHARS:],
    }


def replace_token_markers(workflow_dir: Path) -> dict[str, Any]:
    usage_path = workflow_dir / "token-usage.json"
    report_path = workflow_dir / "final-report.md"
    usage = json.loads(usage_path.read_text(encoding="utf-8"))
    report = report_path.read_text(encoding="utf-8")
    replacements = {
        "{{WORKFLOW_TOTAL_TOKENS}}": str(usage.get("total_tokens")),
        "{{WORKFLOW_TOKEN_SOURCE}}": str(usage.get("source")),
        "{{WORKFLOW_TOKEN_CONFIDENCE}}": str(usage.get("confidence")),
    }
    missing = [marker for marker in replacements if marker not in report]
    if missing:
        raise ValueError("final-report.md missing token marker(s): " + ", ".join(missing))
    for marker, value in replacements.items():
        report = report.replace(marker, value)
    report_path.write_text(report, encoding="utf-8")
    return {
        "name": "bind_exact_token_markers",
        "command": ["internal", "bind_exact_token_markers"],
        "status": "pass",
        "exit_code": 0,
        "output_sha256": "sha256:" + hashlib.sha256(report.encode()).hexdigest(),
        "output_excerpt": (
            f"Bound total={usage.get('total_tokens')} source={usage.get('source')} "
            f"confidence={usage.get('confidence')} into final-report.md"
        ),
    }


def controller_commands(operation: str, workflow_dir: Path, repo_root: Path) -> list[tuple[str, list[str]]]:
    scripts = repo_root / "skills" / "agent-workflow" / "scripts"
    python = sys.executable
    if operation in {"start", "prepare"}:
        commands = [
            (
                "prepare_dispatch",
                [python, str(scripts / "prepare_dispatch.py"), str(workflow_dir)],
            ),
            (
                "validate_planned",
                [
                    python,
                    str(scripts / "verify_workflow.py"),
                    str(workflow_dir),
                    "--mode",
                    "planned",
                ],
            ),
        ]
        if (workflow_dir / "swarm-card.json").is_file():
            commands.append(
                (
                    "render_event_card",
                    [
                        python,
                        str(scripts / "render_swarm_card.py"),
                        str(workflow_dir),
                        "--emit",
                    ],
                )
            )
        return commands
    if operation == "collect":
        orchestration = json.loads(
            (workflow_dir / "orchestration.json").read_text(encoding="utf-8")
        )
        commands: list[tuple[str, list[str]]] = []
        for round_plan in orchestration.get("rounds", []):
            if not isinstance(round_plan, dict):
                continue
            round_id = round_plan.get("round_id")
            if not isinstance(round_id, str):
                continue
            for lane in round_plan.get("lanes", []):
                if (
                    isinstance(lane, dict)
                    and lane.get("enabled") is True
                    and isinstance(lane.get("id"), str)
                ):
                    commands.append(
                        (
                            f"emit_lane_receipt_{round_id}_{lane['id']}",
                            [
                                python,
                                str(scripts / "lane_receipt.py"),
                                str(workflow_dir),
                                round_id,
                                lane["id"],
                            ],
                        )
                    )
        commands.extend([
            (
                "collect_results",
                [python, str(scripts / "collect_results.py"), str(workflow_dir)],
            ),
            (
                "validate_executed",
                [
                    python,
                    str(scripts / "verify_workflow.py"),
                    str(workflow_dir),
                    "--mode",
                    "executed",
                ],
            ),
        ])
        if (workflow_dir / "swarm-card.json").is_file():
            commands.append(
                (
                    "render_event_card",
                    [
                        python,
                        str(scripts / "render_swarm_card.py"),
                        str(workflow_dir),
                        "--emit",
                    ],
                )
            )
        return commands
    if operation == "finalize":
        return [
            (
                "replay_raw_runtime_completions",
                [
                    python,
                    str(scripts / "runtime_harness.py"),
                    "collect",
                    str(workflow_dir),
                ],
            ),
            (
                "validate_executed_before_terminal_accounting",
                [
                    python,
                    str(scripts / "verify_workflow.py"),
                    str(workflow_dir),
                    "--mode",
                    "executed",
                ],
            ),
            (
                "finalize_exact_token_accounting",
                [
                    python,
                    str(scripts / "token_accounting.py"),
                    "finalize",
                    str(workflow_dir),
                ],
            ),
            (
                "reconcile_runtime_observations",
                [
                    python,
                    str(scripts / "runtime_harness.py"),
                    "reconcile",
                    str(workflow_dir),
                ],
            ),
        ]
    raise ValueError(f"unknown operation: {operation}")


def run_start_operation(
    workflow_dir: Path,
    receipt_id: str,
    *,
    manifest_path: Path | None = None,
    runtime: str | None = None,
    lead_session_id: str | None = None,
    runtime_root: Path | None = None,
) -> dict[str, Any]:
    repo_root = Path.cwd().resolve()
    workflow_dir = workflow_dir.resolve()
    started_at = utc_now()
    steps: list[dict[str, Any]] = []
    committed = False
    resolved_manifest: Path | None = None
    participants: list[dict[str, str]] = []
    original_ledgers: dict[str, bytes | None] = {}
    original_ledger_state: dict[str, dict[str, Any]] = {
        "token-usage.json": {"present": None, "snapshot_error": "not_attempted"},
        "token-evidence.json": {"present": None, "snapshot_error": "not_attempted"},
    }
    try:
        original_ledgers, original_ledger_state = snapshot_original_ledgers(workflow_dir)
        steps.append(
            internal_step(
                "snapshot_original_ledgers",
                ["internal", "snapshot_original_ledgers"],
                status="pass",
                output=json.dumps(original_ledger_state, sort_keys=True),
            )
        )
    except LedgerSnapshotError as exc:
        original_ledgers = exc.originals
        original_ledger_state = exc.state
        steps.append(
            internal_step(
                "snapshot_original_ledgers",
                ["internal", "snapshot_original_ledgers"],
                status="fail",
                output=str(exc),
            )
        )
    if all(step["status"] == "pass" for step in steps):
        try:
            resolved_manifest, participants = validate_accounting_start_manifest(
                workflow_dir, manifest_path
            )
            manifest_digest = "sha256:" + hashlib.sha256(resolved_manifest.read_bytes()).hexdigest()
            steps.append(
                internal_step(
                    "validate_accounting_start_input",
                    ["internal", "validate_accounting_start_manifest", str(resolved_manifest)],
                    status="pass",
                    output=(
                        f"Validated {len(participants)} participant attempt(s); "
                        f"manifest={manifest_digest}"
                    ),
                )
            )
        except (OSError, ValueError) as exc:
            steps.append(
                internal_step(
                    "validate_accounting_start_input",
                    ["internal", "validate_accounting_start_manifest"],
                    status="fail",
                    output=str(exc),
                )
            )
    if all(step["status"] == "pass" for step in steps):
        for name, command in controller_commands("start", workflow_dir, repo_root):
            step = command_step(name, command, cwd=repo_root)
            steps.append(step)
            if step["status"] != "pass":
                break
    stage_root: Path | None = None
    if all(step["status"] == "pass" for step in steps):
        stage_root = Path(
            tempfile.mkdtemp(prefix=workflow_dir.name + ".accounting-start-stage.")
        )
        for name, payload in original_ledgers.items():
            if payload is not None:
                atomic_write_bytes(stage_root / name, payload)
        try:
            value = start_accounting(
                stage_root,
                runtime=runtime,
                lead_session_id=lead_session_id,
                runtime_root=runtime_root,
            )
            steps.append(
                internal_step(
                    "start_exact_token_accounting",
                    ["internal", "token_accounting.start"],
                    status="pass",
                    output=(
                        f"Started {value['accounting']['runtime']}:"
                        f"{value['accounting']['lead_session_id']} after static preparation"
                    ),
                )
            )
            for index, participant in enumerate(participants, start=1):
                register_agent(
                    stage_root,
                    execution_ref=participant["execution_ref"],
                    agent_id=participant["agent_id"],
                    round_id=participant["round_id"],
                    lane_id=participant["lane_id"],
                    runtime_root=runtime_root,
                    reuse_existing_session=(
                        participant["registration_mode"] == "reuse_existing_session"
                    ),
                )
                steps.append(
                    internal_step(
                        f"register_participant_{index:03d}",
                        ["internal", "token_accounting.register_agent"],
                        status="pass",
                        output=(
                            f"Registered {participant['execution_ref']} -> "
                            f"{participant['agent_id']} ({participant['registration_mode']})"
                        ),
                    )
                )
            try:
                for name in ("token-usage.json", "token-evidence.json"):
                    atomic_write_bytes(
                        workflow_dir / name, (stage_root / name).read_bytes()
                    )
            except OSError as commit_error:
                rollback_errors: list[str] = []
                for name, payload in original_ledgers.items():
                    path = workflow_dir / name
                    try:
                        if payload is None:
                            path.unlink(missing_ok=True)
                        else:
                            atomic_write_bytes(path, payload)
                    except OSError as rollback_error:
                        rollback_errors.append(f"{name}: {rollback_error}")
                if rollback_errors:
                    raise OSError(
                        f"ledger commit failed ({commit_error}); rollback failed: "
                        + "; ".join(rollback_errors)
                    ) from commit_error
                raise
            committed = True
            steps.append(
                internal_step(
                    "commit_accounting_boundary",
                    ["internal", "commit_staged_token_ledger"],
                    status="pass",
                    output="Committed the staged lead snapshot and all declared participants.",
                )
            )
        except (OSError, json.JSONDecodeError, TokenAccountingError) as exc:
            steps.append(
                internal_step(
                    "start_or_register_exact_accounting",
                    ["internal", "staged_token_accounting_start"],
                    status="fail",
                    output=str(exc),
                )
            )
        finally:
            shutil.rmtree(stage_root)
    status = "pass" if steps and all(step["status"] == "pass" for step in steps) else "fail"
    receipt = {
        "schema_version": CONTROLLER_RECEIPT_SCHEMA,
        "operation": "start",
        "receipt_id": receipt_id,
        "started_at": started_at,
        "completed_at": utc_now(),
        "status": status,
        "host_atomicity": "not_claimed",
        "portable_transaction": {
            "mode": "staged_accounting_ledger_commit",
            "committed": committed,
            "original_mutated_before_commit": True,
            "bundle_atomicity": "not_claimed",
        },
        "accounting_snapshot_after_static_steps": True,
        "original_ledger_state": original_ledger_state,
        "bounded_input": {
            "path": (
                str(resolved_manifest.relative_to(workflow_dir))
                if resolved_manifest is not None and resolved_manifest.is_relative_to(workflow_dir)
                else str(resolved_manifest) if resolved_manifest is not None else None
            ),
            "participant_count": len(participants),
        },
        "host_owned_boundaries": list(HOST_OWNED_BOUNDARIES),
        "steps": steps,
        "next_allowed_decisions": (
            ["dispatch_declared_participants"]
            if status == "pass"
            else ["repair_required", "blocked"]
        ),
    }
    receipt_path = workflow_dir / "controller-receipts" / f"{receipt_id}.json"
    atomic_write(receipt_path, receipt)
    receipt["receipt_path"] = str(receipt_path.relative_to(workflow_dir))
    return receipt


def run_operation(
    operation: str,
    workflow_dir: Path,
    receipt_id: str,
    *,
    manifest_path: Path | None = None,
    runtime: str | None = None,
    lead_session_id: str | None = None,
    runtime_root: Path | None = None,
) -> dict[str, Any]:
    if operation == "start":
        return run_start_operation(
            workflow_dir,
            receipt_id,
            manifest_path=manifest_path,
            runtime=runtime,
            lead_session_id=lead_session_id,
            runtime_root=runtime_root,
        )
    repo_root = Path.cwd().resolve()
    workflow_dir = workflow_dir.resolve()
    started_at = utc_now()
    stage_root: Path | None = None
    operation_dir = workflow_dir
    if operation in {"collect", "finalize"}:
        stage_root = Path(
            tempfile.mkdtemp(
                prefix=workflow_dir.name + f".{operation}-stage.",
                dir=workflow_dir.parent,
            )
        )
        operation_dir = stage_root / workflow_dir.name
        shutil.copytree(workflow_dir, operation_dir)
    steps: list[dict[str, Any]] = []
    fault = os.environ.get("AGENT_WORKFLOW_FINALIZE_FAULT", "") if operation == "finalize" else ""
    if operation == "collect":
        try:
            binding = bind_raw_wait_evidence(operation_dir, runtime_root=runtime_root)
            steps.append(
                internal_step(
                    "bind_raw_wait_evidence",
                    ["internal", "bind_raw_wait_evidence"],
                    status="pass",
                    output=json.dumps(binding, sort_keys=True),
                )
            )
        except (OSError, ValueError, json.JSONDecodeError, TokenAccountingError) as exc:
            steps.append(
                internal_step(
                    "bind_raw_wait_evidence",
                    ["internal", "bind_raw_wait_evidence"],
                    status="fail",
                    output=str(exc),
                )
            )
        if all(step["status"] == "pass" for step in steps):
            try:
                integration = integrate_terminal_lane_outputs(operation_dir)
                steps.append(
                    internal_step(
                        "integrate_terminal_lane_outputs",
                        ["internal", "integrate_terminal_lane_outputs"],
                        status="pass",
                        output=json.dumps(integration, sort_keys=True),
                    )
                )
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                steps.append(
                    internal_step(
                        "integrate_terminal_lane_outputs",
                        ["internal", "integrate_terminal_lane_outputs"],
                        status="fail",
                        output=str(exc),
                    )
                )
    commands = (
        controller_commands(operation, operation_dir, repo_root)
        if all(step["status"] == "pass" for step in steps)
        else []
    )
    for name, command in commands:
        step = command_step(name, command, cwd=repo_root)
        steps.append(step)
        if step["status"] != "pass":
            break
        if fault == "reconcile_drift" and name == "finalize_exact_token_accounting":
            steps.append({
                "name": "injected_reconcile_drift",
                "command": ["internal", "fault_injection"],
                "status": "fail",
                "exit_code": 1,
                "output_sha256": "sha256:" + hashlib.sha256(fault.encode()).hexdigest(),
                "output_excerpt": "Injected reconcile drift before staged commit.",
            })
            break
    if operation == "finalize" and all(step["status"] == "pass" for step in steps):
        if fault == "marker_missing":
            report_path = operation_dir / "final-report.md"
            report_path.write_text(
                report_path.read_text(encoding="utf-8").replace("{{WORKFLOW_TOTAL_TOKENS}}", "missing-marker"),
                encoding="utf-8",
            )
        try:
            steps.append(replace_token_markers(operation_dir))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            steps.append(
                {
                    "name": "bind_exact_token_markers",
                    "command": ["internal", "bind_exact_token_markers"],
                    "status": "fail",
                    "exit_code": 1,
                    "output_sha256": "sha256:" + hashlib.sha256(str(exc).encode()).hexdigest(),
                    "output_excerpt": str(exc),
                }
            )
        if all(step["status"] == "pass" for step in steps):
            scripts = repo_root / "skills" / "agent-workflow" / "scripts"
            manifest = write_terminal_commit_manifest(operation_dir)
            steps.append({
                "name": "stage_terminal_commit_manifest",
                "command": ["internal", "stage_terminal_commit_manifest"],
                "status": "pass",
                "exit_code": 0,
                "output_sha256": manifest["revision"],
                "output_excerpt": "Staged manifest-last revision for five terminal projections.",
            })
            if fault == "final_verifier_failure":
                steps.append({
                    "name": "validate_final",
                    "command": ["internal", "fault_injection"],
                    "status": "fail",
                    "exit_code": 1,
                    "output_sha256": "sha256:" + hashlib.sha256(fault.encode()).hexdigest(),
                    "output_excerpt": "Injected final verifier failure before staged commit.",
                })
            else:
                steps.append(
                    command_step(
                        "validate_final",
                        [
                            sys.executable,
                            str(scripts / "verify_workflow.py"),
                            str(operation_dir),
                            "--mode",
                            "final",
                        ],
                        cwd=repo_root,
                    )
                )
    status = "pass" if steps and all(step["status"] == "pass" for step in steps) else "fail"
    committed = False
    if operation == "collect" and status == "pass":
        collect_projections = [
            "state.json",
            "runner-evidence.json",
            "integration-index.json",
            "swarm-card.json",
        ]
        for name in collect_projections:
            staged = operation_dir / name
            if staged.is_file():
                atomic_write_bytes(workflow_dir / name, staged.read_bytes())
        for pattern in ("rounds/*/integration.json", "rounds/*/integration.md", "rounds/*/receipts/*.json"):
            for staged in operation_dir.glob(pattern):
                relative = staged.relative_to(operation_dir)
                atomic_write_bytes(workflow_dir / relative, staged.read_bytes())
        committed = True
    if operation == "finalize" and status == "pass":
        for name in TERMINAL_PROJECTIONS:
            atomic_write_bytes(workflow_dir / name, (operation_dir / name).read_bytes())
        atomic_write_bytes(
            workflow_dir / TERMINAL_COMMIT_MANIFEST,
            (operation_dir / TERMINAL_COMMIT_MANIFEST).read_bytes(),
        )
        committed = True
    if stage_root is not None:
        shutil.rmtree(stage_root)
    receipt = {
        "schema_version": CONTROLLER_RECEIPT_SCHEMA,
        "operation": operation,
        "receipt_id": receipt_id,
        "started_at": started_at,
        "completed_at": utc_now(),
        "status": status,
        "host_atomicity": "not_claimed",
        "portable_transaction": {
            "mode": (
                "two_phase_staged_manifest_last"
                if operation == "finalize"
                else "staged_collect_commit"
                if operation == "collect"
                else "not_applicable"
            ),
            "committed": committed,
            "original_mutated_before_commit": False,
            "bundle_atomicity": "not_claimed",
        },
        "host_owned_boundaries": list(HOST_OWNED_BOUNDARIES),
        "steps": steps,
        "next_allowed_decisions": (
            ["return_terminal_receipt"]
            if status == "pass" and operation == "finalize"
            else ["advance", "repair_required", "blocked"]
        ),
    }
    receipt_path = workflow_dir / "controller-receipts" / f"{receipt_id}.json"
    atomic_write(receipt_path, receipt)
    receipt["receipt_path"] = str(receipt_path.relative_to(workflow_dir))
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("start", "prepare", "collect", "finalize"))
    parser.add_argument("workflow_dir")
    parser.add_argument("--receipt-id")
    parser.add_argument(
        "--participants",
        type=Path,
        help=f"Start-only participant manifest (default: {ACCOUNTING_START_MANIFEST})",
    )
    parser.add_argument("--runtime", choices=("codex", "claude"))
    parser.add_argument("--lead-session-id")
    parser.add_argument("--runtime-root", type=Path)
    args = parser.parse_args()
    receipt_id = args.receipt_id or f"{args.operation}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    if args.operation != "start" and any(
        value is not None
        for value in (
            args.participants,
            args.runtime,
            args.lead_session_id,
            args.runtime_root,
        )
    ):
        parser.error("--participants/--runtime/--lead-session-id/--runtime-root are start-only")
    receipt = run_operation(
        args.operation,
        Path(args.workflow_dir),
        receipt_id,
        manifest_path=args.participants,
        runtime=args.runtime,
        lead_session_id=args.lead_session_id,
        runtime_root=args.runtime_root,
    )
    print(json.dumps(receipt, ensure_ascii=False, separators=(",", ":")))
    return 0 if receipt["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
