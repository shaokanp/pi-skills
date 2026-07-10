#!/usr/bin/env python3
"""Summarize agent-loop lane outputs into an integration checklist."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from execution_efficiency import (
    ExecutionEfficiencyError,
    build_integration_index,
    write_json_atomic,
)

MARKERS = (
    "Accepted",
    "Rejected",
    "Conflict",
    "Decision",
    "Risk",
    "Verification",
    "TODO",
)


def heading_for(path: Path) -> str:
    return path.stem.replace("-", " ").replace("_", " ").title()


def interesting_lines(text: str) -> list[str]:
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if stripped.startswith(("-", "*", "#")) or any(
            marker.lower() in lowered for marker in MARKERS
        ):
            lines.append(stripped)
    return lines[:40]


def load_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"Invalid JSON in {path}: {exc}"
    if not isinstance(value, dict):
        return None, f"JSON root must be an object in {path}"
    return value, None


def confidence_text(value: dict[str, Any]) -> str:
    confidence = value.get("confidence")
    if not isinstance(confidence, dict):
        return "confidence: missing"
    parts: list[str] = []
    for key in ("independent", "self"):
        score = confidence.get(key)
        if score is not None:
            parts.append(f"{key}={score}")
    source = confidence.get("source")
    if source:
        parts.append(f"source={source}")
    return "confidence: " + (", ".join(parts) if parts else "not reported")


def short_evidence(value: Any) -> str:
    if not isinstance(value, list) or not value:
        return ""
    parts: list[str] = []
    for item in value[:3]:
        if isinstance(item, dict):
            path = item.get("path")
            detail = item.get("detail") or item.get("summary") or item.get("evidence")
            if path and detail:
                parts.append(f"{path}: {detail}")
            elif path:
                parts.append(str(path))
            elif detail:
                parts.append(str(detail))
        else:
            parts.append(str(item))
    return "; ".join(parts)


def finding_key(finding: dict[str, Any]) -> tuple[str, str]:
    return (str(finding.get("severity", "")), str(finding.get("claim") or finding.get("summary") or ""))


def collect_findings(value: dict[str, Any]) -> tuple[list[tuple[str, dict[str, Any]]], list[str]]:
    findings: list[tuple[str, dict[str, Any]]] = []
    warnings: list[str] = []
    seen: set[tuple[str, str]] = set()
    top = value.get("findings")
    if isinstance(top, list):
        for item in top:
            if isinstance(item, dict):
                seen.add(finding_key(item))
                findings.append(("top-level", item))
    payload = value.get("payload")
    nested = payload.get("findings") if isinstance(payload, dict) else None
    if isinstance(nested, list):
        for item in nested:
            if not isinstance(item, dict):
                continue
            key = finding_key(item)
            if key in seen:
                continue
            warnings.append(
                f"{value.get('run_id', 'unknown')} has payload-only finding: {key[0]} {key[1]}"
            )
            findings.append(("payload", item))
    return findings, warnings


def summarize_findings(findings: list[tuple[str, dict[str, Any]]]) -> list[str]:
    if not findings:
        return ["- Findings: none reported"]
    lines = ["- Findings:"]
    for index, (source, finding) in enumerate(findings, start=1):
        if not isinstance(finding, dict):
            lines.append(f"  - finding-{index}: invalid shape")
            continue
        severity = finding.get("severity", "unscored")
        claim = finding.get("claim") or finding.get("summary") or "No claim"
        recommendation = finding.get("recommendation")
        line = f"  - {severity}: {claim} [{source}]"
        if recommendation:
            line += f" -> {recommendation}"
        lines.append(line)
        evidence = short_evidence(finding.get("evidence"))
        if evidence:
            lines.append(f"    - Evidence: {evidence}")
        repair_packet = finding.get("repair_packet")
        if isinstance(repair_packet, dict):
            objective = repair_packet.get("objective")
            ownership = repair_packet.get("ownership")
            if objective:
                lines.append(f"    - Repair objective: {objective}")
            if isinstance(ownership, list) and ownership:
                lines.append(f"    - Ownership: {', '.join(map(str, ownership))}")
    return lines


def summarize_repair_packets(value: Any, prefix: str = "Repair packets") -> list[str]:
    if not isinstance(value, list) or not value:
        return []
    lines = [f"- {prefix}: {len(value)}"]
    for index, packet in enumerate(value, start=1):
        if not isinstance(packet, dict):
            lines.append(f"  - packet-{index}: invalid shape")
            continue
        objective = packet.get("objective") or packet.get("repair_objective") or "No objective"
        lines.append(f"  - packet-{index}: {objective}")
        ownership = packet.get("ownership")
        if isinstance(ownership, list) and ownership:
            lines.append(f"    - Ownership: {', '.join(map(str, ownership))}")
    return lines


def summarize_lane_output(path: Path, value: dict[str, Any]) -> tuple[list[str], list[str]]:
    lane = value.get("lane", "unknown")
    run_id = value.get("run_id", path.stem)
    status = value.get("status", "unknown")
    summary = value.get("summary", "")
    gate = value.get("gate") if isinstance(value.get("gate"), dict) else {}
    decision = gate.get("decision", "unknown") if isinstance(gate, dict) else "unknown"
    reason = gate.get("reason", "") if isinstance(gate, dict) else ""

    lines = [
        f"### {run_id}",
        "",
        f"- Lane: `{lane}`",
        f"- Status: `{status}`",
        f"- Gate: `{decision}`",
        f"- {confidence_text(value)}",
    ]
    if summary:
        lines.append(f"- Summary: {summary}")
    if reason:
        lines.append(f"- Gate reason: {reason}")
    findings, warnings = collect_findings(value)
    lines.extend(summarize_findings(findings))

    payload = value.get("payload")
    if isinstance(payload, dict):
        next_lanes = payload.get("recommended_next_lanes")
        if isinstance(next_lanes, list) and next_lanes:
            lines.append(f"- Recommended next lanes: {', '.join(map(str, next_lanes))}")
        lines.extend(summarize_repair_packets(payload.get("repair_packets"), "Payload repair packets"))
    lines.append("")
    return lines, warnings


def collect_lane_outputs(workflow_dir: Path) -> tuple[list[str], list[str]]:
    lines: list[str] = []
    warnings: list[str] = []
    lane_files = sorted((workflow_dir / "rounds").glob("*/lane-runs/*.json"))
    if not lane_files:
        lines.extend(["No v1 lane JSON outputs found.", ""])
        return lines, warnings

    current_round: str | None = None
    for file in lane_files:
        round_id = file.parent.parent.name
        if round_id != current_round:
            current_round = round_id
            lines.extend([f"## {round_id}", ""])
        value, error = load_json(file)
        if error:
            warnings.append(error)
            lines.extend([f"### {file.name}", "", f"- Error: {error}", ""])
            continue
        assert value is not None
        summary_lines, summary_warnings = summarize_lane_output(file, value)
        lines.extend(summary_lines)
        warnings.extend(summary_warnings)
    return lines, warnings


def collect_legacy_results(workflow_dir: Path) -> list[str]:
    results_dir = workflow_dir / "results"
    if not results_dir.is_dir():
        return []
    files = sorted(results_dir.glob("*.md"))
    if not files:
        return []

    lines = ["## Legacy Markdown Results", ""]
    for file in files:
        text = file.read_text(encoding="utf-8")
        lines.extend([f"### {heading_for(file)}", ""])
        snippets = interesting_lines(text)
        if snippets:
            lines.extend(snippets)
        else:
            lines.append("No checklist-like lines found; inspect this result manually.")
        lines.append("")
    return lines


def format_int(value: Any) -> str:
    return f"{value:,}" if isinstance(value, int) else "not recorded"


def collect_token_usage(workflow_dir: Path) -> tuple[list[str], list[str]]:
    path = workflow_dir / "token-usage.json"
    if not path.is_file():
        return [], [f"Missing token usage file: {path}"]
    value, error = load_json(path)
    if error:
        return [], [error]
    assert value is not None
    lines = [
        "## Token Usage",
        "",
        f"- Total tokens: {format_int(value.get('total_tokens'))}",
        f"- Source: {value.get('source', 'not recorded')}",
        f"- Confidence: {value.get('confidence', 'not recorded')}",
        f"- Status: {value.get('status', 'not recorded')}",
    ]
    method = value.get("method")
    if method:
        lines.append(f"- Method: {method}")
    accounting = value.get("accounting")
    if isinstance(accounting, dict):
        lines.append(f"- Runtime: {accounting.get('runtime', 'not recorded')}")
        participants = accounting.get("participants")
        if isinstance(participants, list):
            lines.append(f"- Registered native attempts: {len(participants)}")
    boundary = value.get("boundary")
    if isinstance(boundary, dict):
        lines.append(
            "- Final user response included: "
            + str(boundary.get("final_user_response_included", "not recorded")).lower()
        )
    lines.append("")
    return lines, []


def route_text(value: Any) -> str:
    if not isinstance(value, dict):
        return "none"
    model = value.get("model")
    effort = value.get("effort")
    if not isinstance(model, str) or not isinstance(effort, str):
        return "none"
    return f"{model}/{effort}"


def collect_routing_summary(workflow_dir: Path) -> tuple[list[str], list[str]]:
    orchestration_path = workflow_dir / "orchestration.json"
    if not orchestration_path.is_file():
        return [], []
    orchestration, error = load_json(orchestration_path)
    if error or not isinstance(orchestration, dict):
        return [], [error] if error else []
    routing = orchestration.get("model_routing")
    if not isinstance(routing, dict) or routing.get("enabled") is not True:
        return [], []
    evidence_path = workflow_dir / "runner-evidence.json"
    evidence, evidence_error = load_json(evidence_path)
    warnings = [evidence_error] if evidence_error else []
    records: dict[str, dict[str, Any]] = {}
    if isinstance(evidence, dict):
        for record in evidence.get("agents", []):
            if not isinstance(record, dict):
                continue
            round_id = record.get("round_id")
            lane_id = record.get("lane_id")
            if isinstance(round_id, str) and isinstance(lane_id, str):
                records[f"{round_id}:{lane_id}"] = record
    lines = ["## Model Routing", "", "Actual routes below are lead-recorded runner evidence."]
    for round_plan in orchestration.get("rounds", []):
        if not isinstance(round_plan, dict):
            continue
        round_id = round_plan.get("round_id")
        for lane in round_plan.get("lanes", []):
            if not isinstance(lane, dict) or not isinstance(lane.get("routing"), dict):
                continue
            decision = lane["routing"]
            lane_id = lane.get("id")
            record = records.get(f"{round_id}:{lane_id}")
            attempts = record.get("attempts", []) if isinstance(record, dict) else []
            terminal = attempts[-1] if isinstance(attempts, list) and attempts else None
            actual = terminal.get("actual_route") if isinstance(terminal, dict) else None
            terminal_outcome = (
                terminal.get("outcome") if isinstance(terminal, dict) else "not_attempted"
            )
            lines.append(
                f"- `{round_id}:{lane_id}` planned `{route_text(decision.get('selected'))}`; "
                f"actual `{route_text(actual)}`; attempts {len(attempts) if isinstance(attempts, list) else 0}; "
                f"planned status `{decision.get('status', 'unknown')}`; "
                f"terminal outcome `{terminal_outcome}`"
            )
    lines.append("")
    return lines, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workflow_dir", help="Path to .workflow/<slug>")
    parser.add_argument(
        "--output",
        help="Optional output Markdown path (default: print to stdout)",
    )
    parser.add_argument(
        "--index-output",
        help=(
            "Optional compact integration index path. Execution-efficiency workflows "
            "default to <workflow-dir>/integration-index.json."
        ),
    )
    args = parser.parse_args()

    workflow_dir = Path(args.workflow_dir)
    if not workflow_dir.is_dir():
        raise SystemExit(f"Missing workflow directory: {workflow_dir}")

    orchestration_path = workflow_dir / "orchestration.json"
    if orchestration_path.is_file():
        orchestration, orchestration_error = load_json(orchestration_path)
        if orchestration_error:
            raise SystemExit(orchestration_error)
    else:
        orchestration = None
    efficiency_enabled = (
        isinstance(orchestration, dict)
        and isinstance(orchestration.get("execution_efficiency"), dict)
        and orchestration["execution_efficiency"].get("enabled") is True
    )
    if efficiency_enabled or args.index_output:
        try:
            assert isinstance(orchestration, dict)
            index = build_integration_index(workflow_dir, orchestration)
            index_path = (
                Path(args.index_output)
                if args.index_output
                else workflow_dir / "integration-index.json"
            )
            write_json_atomic(index_path, index)
        except (OSError, json.JSONDecodeError, ExecutionEfficiencyError) as exc:
            raise SystemExit(f"Cannot build compact integration index: {exc}") from exc

    lines = [f"# Integration Checklist: {workflow_dir.name}", ""]
    if efficiency_enabled or args.index_output:
        lines.extend(
            [
                "Integration input: compact receipt index; open raw lane artifacts only on demand.",
                "",
            ]
        )
    lane_lines, warnings = collect_lane_outputs(workflow_dir)
    lines.extend(lane_lines)
    legacy_lines = collect_legacy_results(workflow_dir)
    if legacy_lines:
        lines.extend(legacy_lines)
    routing_lines, routing_warnings = collect_routing_summary(workflow_dir)
    if routing_lines:
        lines.extend(routing_lines)
    warnings.extend(routing_warnings)
    token_lines, token_warnings = collect_token_usage(workflow_dir)
    if token_lines:
        lines.extend(token_lines)
    warnings.extend(token_warnings)

    if warnings:
        lines.extend(["## Collection Warnings", ""])
        lines.extend(f"- {warning}" for warning in warnings)
        lines.append("")

    lines.extend(
        [
            "## Integration Decisions",
            "",
            "Accepted:",
            "",
            "Rejected:",
            "",
            "Conflicts:",
            "",
            "Repair packets:",
            "",
            "Verification evidence:",
            "",
            "Remaining risks:",
            "",
            "Next round or stop reason:",
            "",
        ]
    )
    output = "\n".join(lines)
    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
