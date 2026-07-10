#!/usr/bin/env python3
"""Render a CJK-safe Agent Workflow Swarm Card from durable JSON state."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from model_routing import expected_swarm_projection


CARD_SCHEMA = "agent-workflow.swarm-card.v2"
LEGACY_CARD_SCHEMA = "agent-loops.swarm-card.v1"

STATUS_SYMBOLS = {
    "planned": "□",
    "running": "◐",
    "waiting": "△",
    "complete": "■",
    "skipped": "-",
    "blocked": "!",
    "failed": "×",
}

STATUS_LABELS = {
    "planned": "not started",
    "running": "running",
    "waiting": "waiting",
    "complete": "complete",
    "skipped": "skipped",
    "blocked": "blocked",
    "failed": "failed",
}

WORKFLOW_STATUS_LABELS = {
    "preview": "PREVIEW",
    "running": "RUNNING",
    "complete": "COMPLETED",
    "completed": "COMPLETED",
    "paused": "PAUSED",
    "blocked": "BLOCKED",
    "human_gate": "HUMAN GATE",
    "failed": "FAILED",
}

PHASE_GROUPS = {
    "discover": ("discover", "Discover"),
    "seam": ("discover", "Discover"),
    "plan": ("plan", "Plan & Roundtable"),
    "roundtable": ("plan", "Plan & Roundtable"),
    "implement": ("implement", "Implement & Repair"),
    "repair": ("implement", "Implement & Repair"),
    "review": ("review", "Review & Challenge"),
    "challenge": ("review", "Review & Challenge"),
    "verify": ("verify", "Verify"),
    "custom": ("custom", "Custom"),
}

FRIENDLY_MODELS = {
    "gpt-5.6-terra": "Terra",
    "gpt-5.6-sol": "Sol",
}

RUNNER_LABELS = {
    "codex_builtin_subagents": "Codex native",
    "claude_code_builtin_subagents": "Claude Code native",
    "manual_simulation": "manual simulation",
}

EXECUTOR_LEGEND_KEYS = {"native", "simulated", "lead_owned"}

LANE_STATUS_TO_CARD = {
    "pending": "planned",
    "running": "running",
    "complete": "complete",
    "skipped": "skipped",
    "blocked": "blocked",
    "invalid_output": "failed",
}

WORKFLOW_STATUS_TO_CARD = {
    "planned": "preview",
    "orchestrated": "preview",
    "running": "running",
    "integrating": "running",
    "verifying": "running",
    "revising": "running",
    "passed": "completed",
    "complete": "completed",
    "blocked": "blocked",
    "abandoned": "failed",
}


class SwarmCardError(ValueError):
    """Raised when card state cannot be validated or rendered."""


def _one_line(value: Any) -> str:
    return " ".join(str(value).split())


def _escape_markdown(value: Any) -> str:
    text = _one_line(value)
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", text)


def _require_object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SwarmCardError(f"{path} must be an object")
    return value


def _require_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SwarmCardError(f"{path} must be a non-empty string")
    return value


def validate_card(card: dict[str, Any]) -> None:
    schema = card.get("schema_version")
    if schema not in {CARD_SCHEMA, LEGACY_CARD_SCHEMA}:
        raise SwarmCardError(
            f"schema_version must be {CARD_SCHEMA!r} or legacy {LEGACY_CARD_SCHEMA!r}"
        )

    status = card.get("status")
    if status not in WORKFLOW_STATUS_LABELS:
        raise SwarmCardError(
            "status must be one of " + ", ".join(sorted(WORKFLOW_STATUS_LABELS))
        )
    _require_string(card.get("title"), "title")
    slug = _require_string(card.get("slug"), "slug")
    if schema == CARD_SCHEMA and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", slug):
        raise SwarmCardError("slug must be a lowercase kebab-case identifier")
    if schema == CARD_SCHEMA and card.get("runner_mode") not in RUNNER_LABELS:
        raise SwarmCardError(
            "runner_mode must be one of " + ", ".join(sorted(RUNNER_LABELS))
        )

    round_state = _require_object(card.get("round"), "round")
    current_round = _require_string(round_state.get("current"), "round.current")
    if schema == CARD_SCHEMA and not re.fullmatch(r"round-[0-9]{3,}", current_round):
        raise SwarmCardError("round.current must match round-NNN")
    budget = round_state.get("budget")
    if not isinstance(budget, int) or isinstance(budget, bool) or budget < 1:
        raise SwarmCardError("round.budget must be an integer >= 1")

    summary = _require_object(card.get("summary"), "summary")
    agents_planned = summary.get("agents_planned")
    phases_planned = summary.get("phases_planned")
    if not isinstance(agents_planned, int) or isinstance(agents_planned, bool) or agents_planned < 1:
        raise SwarmCardError("summary.agents_planned must be an integer >= 1")
    if schema == CARD_SCHEMA and (
        not isinstance(phases_planned, int)
        or isinstance(phases_planned, bool)
        or phases_planned < 1
    ):
        raise SwarmCardError("summary.phases_planned must be an integer >= 1")
    _require_string(summary.get("goal"), "summary.goal")

    legend = _require_object(card.get("legend"), "legend")
    for key, symbol in STATUS_SYMBOLS.items():
        if schema == CARD_SCHEMA and legend.get(key) != symbol:
            raise SwarmCardError(f"legend.{key} must be {symbol!r}")
    if schema == CARD_SCHEMA:
        stale_executor_keys = sorted(EXECUTOR_LEGEND_KEYS & set(legend))
        if stale_executor_keys:
            raise SwarmCardError(
                "executor-type legend keys are not allowed in v2: "
                + ", ".join(stale_executor_keys)
            )

    phases = card.get("phases")
    if not isinstance(phases, list) or not phases:
        raise SwarmCardError("phases must be a non-empty list")
    seen_agents: set[tuple[str, str]] = set()
    agent_count = 0
    phase_ids: set[str] = set()
    for phase_index, phase_value in enumerate(phases):
        phase = _require_object(phase_value, f"phases[{phase_index}]")
        phase_id = _require_string(phase.get("id"), f"phases[{phase_index}].id")
        if schema == CARD_SCHEMA and not re.fullmatch(r"[a-z][a-z0-9_-]*", phase_id):
            raise SwarmCardError(f"phases[{phase_index}].id must be a safe identifier")
        if phase_id in phase_ids:
            raise SwarmCardError(f"duplicate phase id: {phase_id}")
        phase_ids.add(phase_id)
        _require_string(phase.get("label"), f"phases[{phase_index}].label")
        agents = phase.get("agents")
        if not isinstance(agents, list) or not agents:
            raise SwarmCardError(f"phases[{phase_index}].agents must be a non-empty list")
        for agent_index, agent_value in enumerate(agents):
            path = f"phases[{phase_index}].agents[{agent_index}]"
            agent = _require_object(agent_value, path)
            round_id = _require_string(agent.get("round_id"), f"{path}.round_id")
            lane_id = _require_string(agent.get("lane_id"), f"{path}.lane_id")
            if schema == CARD_SCHEMA and not re.fullmatch(r"round-[0-9]{3,}", round_id):
                raise SwarmCardError(f"{path}.round_id must match round-NNN")
            if schema == CARD_SCHEMA and not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]*", lane_id
            ):
                raise SwarmCardError(f"{path}.lane_id must be a safe identifier")
            key = (round_id, lane_id)
            if key in seen_agents:
                raise SwarmCardError(f"duplicate agent projection: {round_id}:{lane_id}")
            seen_agents.add(key)
            agent_count += 1
            status_value = agent.get("status")
            if status_value not in STATUS_SYMBOLS:
                raise SwarmCardError(
                    f"{path}.status must be one of " + ", ".join(sorted(STATUS_SYMBOLS))
                )
            status_note = agent.get("status_note")
            if status_note is not None and (
                not isinstance(status_note, str) or not status_note.strip()
            ):
                raise SwarmCardError(f"{path}.status_note must be null or a non-empty string")

    if agent_count != agents_planned:
        raise SwarmCardError(
            f"summary.agents_planned is {agents_planned}, but phases contain {agent_count} agents"
        )
    if schema == CARD_SCHEMA and len(phases) != phases_planned:
        raise SwarmCardError(
            f"summary.phases_planned is {phases_planned}, but phases contains {len(phases)}"
        )

    gate = _require_object(card.get("gate"), "gate")
    _require_string(gate.get("policy"), "gate.policy")
    _require_string(gate.get("decision"), "gate.decision")
    open_p2_plus = gate.get("open_p2_plus")
    if not isinstance(open_p2_plus, int) or isinstance(open_p2_plus, bool) or open_p2_plus < 0:
        raise SwarmCardError("gate.open_p2_plus must be an integer >= 0")

    if schema == CARD_SCHEMA:
        display = _require_object(card.get("display_policy"), "display_policy")
        if display.get("format") != "markdown_left_rail":
            raise SwarmCardError("display_policy.format must be 'markdown_left_rail'")
        if display.get("status_polling") is not False:
            raise SwarmCardError("display_policy.status_polling must be false")
        if display.get("updates") != "event_only":
            raise SwarmCardError("display_policy.updates must be 'event_only'")
        if display.get("emit_only_when_rendered_card_changes") is not True:
            raise SwarmCardError(
                "display_policy.emit_only_when_rendered_card_changes must be true"
            )
        emitted_hash = card.get("last_emitted_hash")
        if emitted_hash is not None and not re.fullmatch(r"sha256:[0-9a-f]{64}", str(emitted_hash)):
            raise SwarmCardError("last_emitted_hash must be null or sha256:<64 lowercase hex>")


def _friendly_model(value: str) -> str:
    return FRIENDLY_MODELS.get(value, value)


def _model_parts(agent: dict[str, Any]) -> list[str]:
    routing = agent.get("routing") if isinstance(agent.get("routing"), dict) else {}
    route = routing.get("terminal_actual_route")
    if not isinstance(route, dict):
        route = routing.get("planned_route")
    if isinstance(route, dict) and isinstance(route.get("model"), str):
        parts = [_friendly_model(route["model"])]
        if isinstance(route.get("effort"), str) and route["effort"].strip():
            parts.append(route["effort"])
    else:
        model = agent.get("model")
        if isinstance(model, str) and model.strip():
            parts = [model]
        elif isinstance(model, dict):
            display_name = model.get("display_name") or model.get("model") or model.get("id")
            parts = [_friendly_model(display_name)] if isinstance(display_name, str) else []
            effort = model.get("effort")
            if isinstance(effort, str) and effort.strip():
                parts.append(effort)
        else:
            parts = ["model pending"]

    runner = agent.get("runner")
    if runner == "simulated" and "simulated" not in parts:
        parts.append("simulated")
    elif runner == "lead_owned" and "lead-owned" not in parts:
        parts.append("lead-owned")
    return parts


def _round_label(value: str) -> str:
    match = re.fullmatch(r"round-(\d+)", value)
    return str(int(match.group(1))) if match else value


def _token_line(card_path: Path, token_usage: dict[str, Any] | None) -> str:
    if token_usage is None:
        token_path = card_path.parent / "token-usage.json"
        if token_path.is_file():
            try:
                loaded = json.loads(token_path.read_text(encoding="utf-8"))
                token_usage = loaded if isinstance(loaded, dict) else None
            except (OSError, json.JSONDecodeError):
                token_usage = None
    if isinstance(token_usage, dict):
        total = token_usage.get("total_tokens")
        if (
            token_usage.get("status") == "complete"
            and token_usage.get("confidence") == "exact"
            and isinstance(total, int)
            and not isinstance(total, bool)
            and total >= 0
        ):
            return f"Tokens: {total:,} exact"
    return "Tokens: measuring"


def render_card(
    card: dict[str, Any],
    *,
    card_path: Path,
    token_usage: dict[str, Any] | None = None,
) -> str:
    validate_card(card)
    agents = [
        agent
        for phase in card["phases"]
        for agent in phase["agents"]
        if isinstance(agent, dict)
    ]
    complete_count = sum(agent.get("status") == "complete" for agent in agents)
    round_state = card["round"]
    summary = card["summary"]
    runner_label = RUNNER_LABELS.get(card.get("runner_mode"), card.get("runner_mode", ""))

    header_details = [
        f"Round {_round_label(round_state['current'])}/{round_state['budget']}",
        f"{complete_count}/{len(agents)} complete",
    ]
    if runner_label:
        header_details.append(str(runner_label))

    lines = [
        f"> **{_escape_markdown(card['title'])} · {WORKFLOW_STATUS_LABELS[card['status']]}**",
        f"> `{_one_line(card['slug'])}` · " + " · ".join(header_details),
        f"> {_token_line(card_path, token_usage)}",
        ">",
        f"> {_escape_markdown(summary['goal'])}",
    ]

    for phase in card["phases"]:
        lines.extend([">", f"> **{_escape_markdown(phase['label'])}**"])
        for agent in phase["agents"]:
            status = agent["status"]
            status_text = STATUS_LABELS[status]
            if agent.get("status_note"):
                status_text += f": {_escape_markdown(agent['status_note'])}"
            details = [
                f"{STATUS_SYMBOLS[status]} {status_text}",
                f"`{_one_line(agent['lane_id'])}`",
            ]
            label = agent.get("label")
            if isinstance(label, str) and label.strip() and label.strip() != agent["lane_id"]:
                details.append(_escape_markdown(label))
            model_text = " · ".join(_escape_markdown(item) for item in _model_parts(agent))
            attempt_count = (
                agent.get("routing", {}).get("attempt_count")
                if isinstance(agent.get("routing"), dict)
                else None
            )
            if isinstance(attempt_count, int) and attempt_count > 1:
                details.append(f"attempt {attempt_count}")
            lines.append("> " + " · ".join(details) + f" *({model_text})*")

    gate = card["gate"]
    lines.extend(
        [
            ">",
            "> **Gate** "
            + _escape_markdown(str(gate["decision"]).replace("_", " ").title())
            + f" · Open P2+: {gate['open_p2_plus']}",
        ]
    )
    return "\n".join(lines) + "\n"


def rendered_hash(markdown: str) -> str:
    return "sha256:" + hashlib.sha256(markdown.encode("utf-8")).hexdigest()


def resolve_card_path(value: str | Path) -> Path:
    path = Path(value)
    return path / "swarm-card.json" if path.is_dir() else path


def load_card(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SwarmCardError(f"missing card state: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise SwarmCardError(f"cannot read card state {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SwarmCardError(f"card state must be a JSON object: {path}")
    return value


def _load_optional_object(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def synchronize_card(card: dict[str, Any], card_path: Path) -> None:
    """Project current orchestration and lane artifacts into card display state."""

    workflow_dir = card_path.parent
    orchestration = _load_optional_object(workflow_dir / "orchestration.json")
    state = _load_optional_object(workflow_dir / "state.json")
    runner_evidence = _load_optional_object(workflow_dir / "runner-evidence.json")

    lane_specs: dict[tuple[str, str], dict[str, Any]] = {}
    if isinstance(orchestration, dict):
        workflow = orchestration.get("workflow")
        if isinstance(workflow, dict):
            goal = workflow.get("goal")
            if isinstance(goal, str) and goal.strip() and not goal.lstrip().startswith("TODO:"):
                card["summary"]["goal"] = goal
        for round_plan in orchestration.get("rounds", []):
            if not isinstance(round_plan, dict) or not isinstance(round_plan.get("round_id"), str):
                continue
            round_id = round_plan["round_id"]
            for lane in round_plan.get("lanes", []):
                if (
                    isinstance(lane, dict)
                    and lane.get("enabled", True)
                    and isinstance(lane.get("id"), str)
                ):
                    lane_specs[(round_id, lane["id"])] = lane

    evidence: dict[tuple[str, str], dict[str, Any]] = {}
    if isinstance(runner_evidence, dict):
        for record in runner_evidence.get("agents", []):
            if (
                isinstance(record, dict)
                and isinstance(record.get("round_id"), str)
                and isinstance(record.get("lane_id"), str)
            ):
                evidence[(record["round_id"], record["lane_id"])] = record

    if isinstance(state, dict):
        card_status = WORKFLOW_STATUS_TO_CARD.get(state.get("status"))
        if card_status:
            card["status"] = card_status
        current_round = state.get("current_round")
        if isinstance(current_round, str):
            card["round"]["current"] = current_round
        round_budget = state.get("round_budget")
        if isinstance(round_budget, int) and not isinstance(round_budget, bool):
            card["round"]["budget"] = round_budget

    for phase in card.get("phases", []):
        if not isinstance(phase, dict):
            continue
        for agent in phase.get("agents", []):
            if not isinstance(agent, dict):
                continue
            key = (agent.get("round_id"), agent.get("lane_id"))
            spec = lane_specs.get(key)
            record = evidence.get(key)
            if isinstance(spec, dict) and isinstance(spec.get("routing"), dict):
                agent["routing"] = expected_swarm_projection(spec["routing"], record)

            round_id, lane_id = key
            if not isinstance(round_id, str) or not isinstance(lane_id, str):
                continue
            output_path = workflow_dir / "rounds" / round_id / "lane-runs" / f"{lane_id}.json"
            output = _load_optional_object(output_path)
            if isinstance(output, dict):
                projected = LANE_STATUS_TO_CARD.get(output.get("status"))
                if projected:
                    agent["status"] = projected
                    if projected not in {"waiting", "blocked", "failed"}:
                        agent["status_note"] = None
            elif isinstance(record, dict):
                attempts = record.get("attempts")
                terminal = attempts[-1] if isinstance(attempts, list) and attempts else None
                if isinstance(terminal, dict) and terminal.get("outcome") in {"failed", "unavailable"}:
                    agent["status"] = "failed"
                    failure_class = terminal.get("failure_class")
                    if isinstance(failure_class, str) and failure_class.strip():
                        agent["status_note"] = failure_class


def emit_card(path: Path, *, only_if_changed: bool) -> str:
    card = load_card(path)
    synchronize_card(card, path)
    markdown = render_card(card, card_path=path)
    digest = rendered_hash(markdown)
    if only_if_changed and card.get("last_emitted_hash") == digest:
        return ""
    if only_if_changed:
        card["last_emitted_hash"] = digest
        path.write_text(
            json.dumps(card, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    return markdown


def build_initial_card(
    *,
    slug: str,
    runner_mode: str,
    round_id: str,
    round_budget: int,
    lanes: list[dict[str, Any]],
    goal: str,
) -> dict[str, Any]:
    phases: list[dict[str, Any]] = []
    phase_by_id: dict[str, dict[str, Any]] = {}
    for lane in lanes:
        lane_name = str(lane.get("lane", "custom"))
        phase_id, phase_label = PHASE_GROUPS.get(lane_name, ("custom", "Custom"))
        phase = phase_by_id.get(phase_id)
        if phase is None:
            phase = {"id": phase_id, "label": phase_label, "agents": []}
            phase_by_id[phase_id] = phase
            phases.append(phase)
        runner = lane.get("runner") if isinstance(lane.get("runner"), dict) else {}
        runner_value = (
            "simulated"
            if runner_mode == "manual_simulation"
            else "native"
        )
        agent: dict[str, Any] = {
            "round_id": round_id,
            "lane_id": lane["id"],
            "label": lane_name,
            "runner": runner_value,
            "agent_type": runner.get("agent_type", "unknown"),
            "status": "planned",
            "status_note": None,
            "model": {
                "display_name": (
                    "pending selection" if isinstance(lane.get("routing"), dict) else "inherited model"
                ),
                "effort": None,
            },
        }
        if isinstance(lane.get("routing"), dict):
            routing = lane["routing"]
            agent["routing"] = {
                "packet_id": routing.get("packet_id"),
                "decision_id": routing.get("decision_id"),
                "planned_route": routing.get("selected"),
                "terminal_actual_route": None,
                "route_status": routing.get("status", "draft"),
                "attempt_count": 0,
            }
        phase["agents"].append(agent)

    return {
        "schema_version": CARD_SCHEMA,
        "status": "preview",
        "title": "Agent Workflow",
        "slug": slug,
        "runner_mode": runner_mode,
        "round": {"current": round_id, "budget": round_budget},
        "summary": {
            "agents_planned": len(lanes),
            "phases_planned": len(phases),
            "goal": goal,
        },
        "legend": dict(STATUS_SYMBOLS),
        "phases": phases,
        "gate": {
            "policy": "P0/P1 block · P2 repair-or-defer · P3 record",
            "decision": "pending",
            "open_p2_plus": 0,
        },
        "display_policy": {
            "format": "markdown_left_rail",
            "emit": [
                "before_dispatch",
                "after_first_dispatch",
                "phase_status_change",
                "gate_decision",
                "round_transition",
                "final_stop",
            ],
            "status_polling": False,
            "updates": "event_only",
            "emit_only_when_rendered_card_changes": True,
        },
        "last_emitted_hash": None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workflow", help="Workflow directory or swarm-card.json path")
    parser.add_argument(
        "--emit",
        action="store_true",
        help="Print only a changed card and record its rendered hash.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate the card without rendering it.",
    )
    args = parser.parse_args()

    path = resolve_card_path(args.workflow)
    try:
        card = load_card(path)
        validate_card(card)
        if args.check:
            print(f"Swarm Card valid: {path}")
            return 0
        output = emit_card(path, only_if_changed=args.emit)
    except SwarmCardError as exc:
        print(f"Swarm Card invalid: {exc}")
        return 1
    if output:
        print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
