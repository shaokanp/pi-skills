#!/usr/bin/env python3
"""Create an Agent Workflow v1 run workspace."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from clean_orchestrator import (
    CLEAN_RUNTIME_SCHEMA,
    build_clean_runtime_contract,
    build_empty_completion_density,
    build_round_runtime_contract,
)
from execution_efficiency import build_execution_policy, build_lane_execution
from model_routing import (
    RoutingError,
    draft_decision,
    load_policy_template,
    prepare_capability_snapshot,
)
from render_swarm_card import build_initial_card
from runtime_harness import HARNESS_SCHEMA
from token_accounting import (
    TOKEN_USAGE_SCHEMA,
    new_token_usage,
)


ALLOWED_LANES = {
    "discover",
    "plan",
    "roundtable",
    "implement",
    "seam",
    "review",
    "challenge",
    "verify",
    "repair",
    "custom",
}

RUNNER_MODES = {
    "codex_builtin_subagents",
    "claude_code_builtin_subagents",
    "manual_simulation",
}

RUNNER_MODE_CHOICES = RUNNER_MODES | {"auto"}

OUTPUT_SCHEMAS = {
    "discover": "discover_payload.v1",
    "plan": "plan_payload.v1",
    "roundtable": "roundtable_payload.v1",
    "implement": "implement_payload.v1",
    "seam": "seam_payload.v1",
    "review": "review_payload.v1",
    "challenge": "challenge_payload.v1",
    "verify": "verify_payload.v1",
    "repair": "repair_payload.v1",
    "custom": "custom_payload.v1",
}

CODEX_AGENT_TYPES = {
    "discover": "explorer",
    "plan": "default",
    "roundtable": "default",
    "implement": "worker",
    "seam": "explorer",
    "review": "default",
    "challenge": "default",
    "verify": "default",
    "repair": "worker",
    "custom": "default",
}

CLAUDE_CODE_AGENT_TYPES = {
    "discover": "Explore",
    "plan": "Plan",
    "roundtable": "general-purpose",
    "implement": "general-purpose",
    "seam": "Explore",
    "review": "general-purpose",
    "challenge": "general-purpose",
    "verify": "general-purpose",
    "repair": "general-purpose",
    "custom": "general-purpose",
}


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:64].strip("-") or "workflow"


def write_new(path: Path, content: str) -> None:
    if path.exists():
        return
    path.write_text(content, encoding="utf-8")


def write_json_new(path: Path, value: object) -> None:
    write_new(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def resolve_runner_mode(value: str) -> str:
    if value != "auto":
        return value
    if os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_CI"):
        return "codex_builtin_subagents"
    if os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CLAUDE_CODE_ENTRYPOINT"):
        return "claude_code_builtin_subagents"
    return "manual_simulation"


def capability_record(
    summary: str,
    verified: bool,
    *,
    checked_at: str | None = None,
    snapshot_content_sha256: str | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "source": "lead_agent",
        "summary": summary,
        "verified": verified,
    }
    if snapshot_content_sha256 is not None:
        record["checked_at"] = checked_at
        record["snapshot_content_sha256"] = snapshot_content_sha256
    return record


def runner_adapter(
    mode: str,
    capability_evidence: str = "",
    *,
    capability_snapshot: dict[str, object] | None = None,
    checked_at: str | None = None,
) -> dict[str, object]:
    evidence_summary = capability_evidence.strip()
    verified = bool(evidence_summary)
    capability_kwargs = (
        {
            "checked_at": checked_at if verified else None,
            "snapshot_content_sha256": str(capability_snapshot["content_sha256"]),
        }
        if capability_snapshot is not None
        else {}
    )
    if mode == "codex_builtin_subagents":
        adapter: dict[str, object] = {
            "mode": mode,
            "dispatch_surface": "multi_agent_v1",
            "cross_runtime_calls_allowed": False,
            "notes": "Lead agent calls Codex multi-agent tools directly.",
        }
        if evidence_summary:
            adapter["capability_evidence"] = capability_record(
                evidence_summary, verified, **capability_kwargs
            )
        else:
            adapter["capability_evidence"] = capability_record(
                "Native runner mode selected by scaffold; lead must record "
                "actual multi_agent_v1 tool availability before executed/final validation.",
                False,
                **capability_kwargs,
            )
        return adapter
    if mode == "claude_code_builtin_subagents":
        adapter = {
            "mode": mode,
            "dispatch_surface": "claude_code_agent_tool",
            "cross_runtime_calls_allowed": False,
            "notes": (
                "Claude Code uses its built-in Agent/subagent surface inside "
                "the same Claude Code session."
            ),
        }
        if evidence_summary:
            adapter["capability_evidence"] = capability_record(
                evidence_summary, verified, **capability_kwargs
            )
        else:
            adapter["capability_evidence"] = capability_record(
                "Native runner mode selected by scaffold; lead must record "
                "actual Claude Code subagent availability before executed/final validation.",
                False,
            )
        return adapter
    return {
        "mode": mode,
        "dispatch_surface": "none",
        "cross_runtime_calls_allowed": False,
        "notes": "No native subagent runner selected; lanes must be simulated.",
    }


def lane_runner(mode: str, lane: str) -> dict[str, str]:
    if mode == "codex_builtin_subagents":
        return {
            "mode": mode,
            "agent_type": CODEX_AGENT_TYPES[lane],
            "dispatch_method": "spawn_agent",
        }
    if mode == "claude_code_builtin_subagents":
        return {
            "mode": mode,
            "agent_type": CLAUDE_CODE_AGENT_TYPES[lane],
            "dispatch_method": "Agent",
        }
    return {
        "mode": mode,
        "agent_type": "none",
        "dispatch_method": "simulate_in_main_thread",
    }


def parse_lanes(raw: str) -> list[str]:
    if not raw.strip():
        return []
    lanes = [part.strip().lower() for part in raw.split(",") if part.strip()]
    invalid = [lane for lane in lanes if lane not in ALLOWED_LANES]
    if invalid:
        if "integrate" in invalid:
            raise SystemExit(
                "The integrate worker lane is not scaffolded in v1. "
                "Integration is lead-owned through integration.json/integration.md."
            )
        raise SystemExit(
            "Unknown lane(s): "
            + ", ".join(invalid)
            + ". Allowed: "
            + ", ".join(sorted(ALLOWED_LANES))
        )
    return lanes


def build_lane_specs(
    lanes: list[str],
    mode: str,
    routing_context: tuple[dict[str, object], dict[str, object]] | None = None,
    execution_policy: dict[str, object] | None = None,
    round_id: str = "round-001",
) -> list[dict[str, object]]:
    specs: list[dict[str, object]] = []
    seen: dict[str, int] = {}
    for lane in lanes:
        seen[lane] = seen.get(lane, 0) + 1
        lane_id = f"{lane}-{seen[lane]:02d}"
        spec: dict[str, object] = {
            "id": lane_id,
            "lane": lane,
            "enabled": True,
            "required": lane in {"discover", "verify"},
            "agent_count": 1,
            "purpose": f"TODO: define why the {lane} lane is needed for this round.",
            "prompt": (
                f"TODO: define the bounded {lane} task. Write JSON only using "
                f"{OUTPUT_SCHEMAS[lane]} to rounds/{round_id}/lane-runs/{lane_id}.json."
            ),
            "input_refs": ["plan.md", "orchestration.md"],
            "output_schema": OUTPUT_SCHEMAS[lane],
            "gate": {
                "blocks_on": ["P0", "P1"],
                "confidence_source": (
                    "independent_verifier"
                    if lane == "verify"
                    else "lane_specific"
                ),
            },
            "runner": lane_runner(mode, lane),
        }
        if lane == "custom":
            spec["custom_name"] = "TODO"
        if routing_context is not None:
            policy, capabilities = routing_context
            spec["routing"] = draft_decision(
                policy, capabilities, lane_id=lane_id, role=lane
            )
        if execution_policy is not None:
            build_lane_execution(spec, round_id, mode, execution_policy)
        specs.append(spec)
    return specs


def load_runtime_capability_input(
    path: str,
    *,
    reasoning_effort: str,
) -> dict[str, object]:
    source = Path(path)
    if not source.is_file():
        raise SystemExit(f"--runtime-capabilities does not exist: {source}")
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
        return prepare_capability_snapshot(value, reasoning_effort=reasoning_effort)
    except (OSError, json.JSONDecodeError, RoutingError) as exc:
        raise SystemExit(f"Invalid --runtime-capabilities input: {exc}") from exc


def build_model_routing_block(
    policy: dict[str, object],
    capabilities: dict[str, object],
    *,
    activation: str,
) -> dict[str, object]:
    return {
        "enabled": True,
        "activation": activation,
        "adapter": "codex_builtin_subagents",
        "policy_snapshot": {
            "path": "routing-policy.json",
            "snapshot_id": policy["snapshot_id"],
            "content_sha256": policy["content_sha256"],
        },
        "capability_snapshot": {
            "path": "runtime-capabilities.json",
            "snapshot_id": capabilities["snapshot_id"],
            "content_sha256": capabilities["content_sha256"],
        },
        "reasoning_effort": capabilities["reasoning_effort"],
        "dispatch_gate": "python3 scripts/verify_workflow.py <workflow-dir> --mode planned",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("title", help="Workflow title or task summary")
    parser.add_argument(
        "--root",
        default=".workflow",
        help="Directory where workflow runs are stored (default: .workflow)",
    )
    parser.add_argument("--slug", help="Optional explicit workflow slug")
    parser.add_argument(
        "--round-budget",
        type=int,
        default=3,
        help="Maximum planned rounds before a human or stop gate (default: 3)",
    )
    parser.add_argument(
        "--runner-mode",
        default="auto",
        choices=sorted(RUNNER_MODE_CHOICES),
        help=(
            "Native lane runner. auto resolves to the current runtime when "
            "possible (default: auto)."
        ),
    )
    parser.add_argument(
        "--runner-capability-evidence",
        default="",
        help=(
            "Lead-recorded evidence that the selected native runner surface is "
            "available. Routed workflows bind this explicit fresh recheck to the "
            "capability snapshot; supplying the inventory file alone is not evidence."
        ),
    )
    parser.add_argument(
        "--model-routing",
        choices=("auto", "off", "codex"),
        default="auto",
        help=(
            "Control responsibility-based Codex model routing. Default auto "
            "enables routing for codex_builtin_subagents and leaves other "
            "runners unchanged; off is an explicit compatibility rollback."
        ),
    )
    parser.add_argument(
        "--execution-efficiency",
        choices=("auto", "off", "native"),
        default="auto",
        help=(
            "Control isolated native dispatch, notification-first waits, compact "
            "receipts, admission gates, and execution budgets. Default auto enables "
            "the policy for native Codex and Claude Code runners and leaves manual "
            "simulation unchanged; off is an explicit compatibility rollback."
        ),
    )
    parser.add_argument(
        "--runtime-capabilities",
        help=(
            "JSON capability inventory to snapshot when model routing is active. "
            "Required for Codex native scaffolds unless --model-routing=off."
        ),
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=("low", "medium", "high", "xhigh", "max", "ultra"),
        help=(
            "User-selected session reasoning effort inherited unchanged by every "
            "routed lane. Required when model routing is active."
        ),
    )
    parser.add_argument(
        "--reuse-existing",
        action="store_true",
        help=(
            "Allow leaving existing files in place when the workflow slug already "
            "exists. Without this, stale slug reuse fails closed."
        ),
    )
    parser.add_argument(
        "--lanes",
        default="",
        help=(
            "Optional comma-separated initial lanes, e.g. "
            "discover,roundtable,plan,challenge,verify"
        ),
    )
    parser.add_argument(
        "--swarm-card",
        choices=("auto", "off"),
        default="auto",
        help=(
            "Scaffold CJK-safe Swarm Card state for native lanes or multi-agent "
            "simulations (default: auto)."
        ),
    )
    args = parser.parse_args()

    if args.round_budget < 1:
        raise SystemExit("--round-budget must be >= 1")

    slug = slugify(args.slug or args.title)
    run_dir = Path(args.root) / slug
    if run_dir.exists() and not args.reuse_existing:
        raise SystemExit(
            f"Workflow already exists: {run_dir}. Use --reuse-existing to keep "
            "existing files, or choose a new --slug."
        )
    round_id = "round-001"
    round_dir = run_dir / "rounds" / round_id
    lane_runs_dir = round_dir / "lane-runs"
    lane_runs_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    lanes = parse_lanes(args.lanes)
    resolved_runner_mode = resolve_runner_mode(args.runner_mode)
    execution_policy: dict[str, object] | None = None
    if (
        args.execution_efficiency == "native"
        and resolved_runner_mode == "manual_simulation"
    ):
        raise SystemExit(
            "--execution-efficiency=native requires a native Codex or Claude Code runner"
        )
    if resolved_runner_mode != "manual_simulation" and args.execution_efficiency != "off":
        execution_policy = build_execution_policy(resolved_runner_mode)
        (round_dir / "receipts").mkdir(parents=True, exist_ok=True)
    routing_context: tuple[dict[str, object], dict[str, object]] | None = None
    routing_enabled = args.model_routing == "codex" or (
        args.model_routing == "auto"
        and resolved_runner_mode == "codex_builtin_subagents"
    )
    routing_activation = (
        "explicit_opt_in" if args.model_routing == "codex" else "native_default"
    )
    if args.model_routing == "codex" and resolved_runner_mode != "codex_builtin_subagents":
        raise SystemExit(
            "--model-routing=codex requires --runner-mode=codex_builtin_subagents"
        )
    if routing_enabled:
        if resolved_runner_mode != "codex_builtin_subagents":
            raise SystemExit("enabled model routing requires codex_builtin_subagents")
        if not args.runtime_capabilities:
            raise SystemExit(
                "--runtime-capabilities is required because Codex model routing "
                "is enabled by default; pass --model-routing=off only for an "
                "explicit compatibility rollback"
            )
        if not args.reasoning_effort:
            raise SystemExit(
                "--reasoning-effort is required because Codex model routing is "
                "enabled by default; "
                "the router may not infer or change the user's session effort"
            )
        policy_path = Path(__file__).resolve().parents[1] / "assets" / "model-routing-policy.v2.json"
        try:
            policy = load_policy_template(policy_path)
        except RoutingError as exc:
            raise SystemExit(f"Invalid tracked routing policy template: {exc}") from exc
        capabilities = load_runtime_capability_input(
            args.runtime_capabilities,
            reasoning_effort=args.reasoning_effort,
        )
        routing_context = (policy, capabilities)
    capability_evidence = args.runner_capability_evidence
    adapter = runner_adapter(
        resolved_runner_mode,
        capability_evidence,
        capability_snapshot=routing_context[1] if routing_context is not None else None,
        checked_at=now,
    )
    if resolved_runner_mode != "manual_simulation" and not capability_evidence:
        print(
            "Warning: native runner mode was scaffolded without verified "
            "capability evidence; executed/final validation must fail until a "
            "lead records actual runner evidence.",
            file=sys.stderr,
        )
    lane_specs = build_lane_specs(
        lanes,
        resolved_runner_mode,
        routing_context,
        execution_policy,
        round_id,
    )

    state = {
        "schema_version": "agent-workflow.workflow.v2",
        "title": args.title,
        "slug": slug,
        "created_at": now,
        "status": "planned",
        "current_round": round_id,
        "round_budget": args.round_budget,
        "runner_mode": resolved_runner_mode,
        "runner_adapter": adapter,
        "token_accounting": {
            "required_schema": TOKEN_USAGE_SCHEMA,
            "exact_required": True,
        },
        "approval": {"required": False, "granted": None, "notes": ""},
        "gates": {
            "severity_policy": "P0/P1 block, P2 budget/risk decision, P3 record",
            "confidence_policy": (
                "Independent verifier/challenger confidence gates the round"
            ),
        },
        "rounds": [
            {
                "round_id": round_id,
                "status": "planned",
                "objective": "Compile and run the first orchestration round.",
                "enabled_lanes": [spec["id"] for spec in lane_specs],
                "gate_decision": "pending",
            }
        ],
        "final_status": "pending",
    }
    if resolved_runner_mode == "codex_builtin_subagents" and execution_policy is not None:
        state["runtime_contract"] = {
            "required_schema": CLEAN_RUNTIME_SCHEMA,
            "default_topology": "main_single_clean_orchestrator_nested_workers",
            "legacy_main_fanout": "forbidden_production",
        }
    initial_round = {
        "round_id": round_id,
        "objective": "First planned round.",
        "lanes": lane_specs,
    }
    if resolved_runner_mode == "codex_builtin_subagents" and execution_policy is not None:
        runtime_round = build_round_runtime_contract(
            round_id=round_id,
            objective=str(initial_round["objective"]),
            lane_ids=[str(spec["id"]) for spec in lane_specs],
        )
        runtime_round["lanes"] = lane_specs
        initial_round = runtime_round
    orchestration = {
        "schema_version": "agent-loops.orchestration.v1",
        "workflow": {
            "title": args.title,
            "slug": slug,
            "goal": "TODO: define the user-visible goal.",
            "success_criteria": [],
            "constraints": [],
            "non_goals": [],
        },
        "orchestrator": {
            "planning_mode": "planner_first",
            "runner_mode": resolved_runner_mode,
            "runner_adapter": adapter,
            "round_budget": args.round_budget,
            "stop_conditions": [
                "verify_pass",
                "blocked",
                "human_gate",
                "round_budget_exhausted",
            ],
            "invalid_json_policy": "repair_once_then_invalid_output",
            "display_policy": {
                "swarm_card": "markdown_left_rail",
                "emit": [
                    "before_dispatch",
                    "after_first_dispatch",
                    "phase_status_change",
                    "gate_decision",
                    "round_transition",
                    "final_stop",
                ],
                "polling": "disabled_status_only_event_updates",
            },
        },
        "rounds": [initial_round],
    }
    if resolved_runner_mode == "codex_builtin_subagents" and execution_policy is not None:
        orchestration["clean_orchestrator_runtime"] = build_clean_runtime_contract(
            resolved_runner_mode
        )
    if routing_context is not None:
        orchestration["model_routing"] = build_model_routing_block(
            *routing_context,
            activation=routing_activation,
        )
    if execution_policy is not None:
        orchestration["execution_efficiency"] = execution_policy
        orchestration["workflow"]["workspace_root"] = os.path.relpath(
            Path.cwd().resolve(), run_dir.resolve()
        )

    integration = {
        "schema_version": "agent-loops.integration.v1",
        "round_id": round_id,
        "status": "pending",
        "accepted": [],
        "rejected": [],
        "conflicts": [],
        "repair_packets": [],
        "finding_resolutions": [],
        "verification_evidence": [],
        "remaining_risks": [],
        "next_round": None,
        "stop_reason": None,
    }
    runner_evidence = {
        "schema_version": "agent-loops.runner-evidence.v1",
        "runner_mode": resolved_runner_mode,
        "dispatch_surface": adapter["dispatch_surface"],
        "cross_runtime_calls_allowed": False,
        "capability_evidence": adapter.get("capability_evidence", {}),
        "agents": [],
        "notes": [
            "Scaffolded evidence ledger. The lead agent must add per-lane "
            "lifecycle evidence after native subagents actually run."
        ],
    }
    if resolved_runner_mode != "manual_simulation":
        runner_evidence["evidence_level"] = "lead_recorded"
    if routing_context is not None:
        runner_evidence["model_capability_snapshot"] = {
            "snapshot_id": routing_context[1]["snapshot_id"],
            "content_sha256": routing_context[1]["content_sha256"],
        }
    if execution_policy is not None:
        runner_evidence["execution_efficiency"] = {
            "lead_model_completions": 0,
            "status_only_completions": 0,
            "functions_wait_calls": 0,
            "wait_waves": [],
            "card_events": [],
        }
    if "clean_orchestrator_runtime" in orchestration:
        runner_evidence["completion_density"] = build_empty_completion_density(
            orchestration
        )
        runtime_harness = {
            "schema_version": HARNESS_SCHEMA,
            "default_round_id": round_id,
            "source": "raw_runtime_session_events",
            "artifact_projection_is_truth": False,
            "outer_main_post_terminal_wake": "outside_sealed_subtree_unobserved",
        }
    token_usage = new_token_usage()

    write_new(
        run_dir / "plan.md",
        f"""# {args.title}

## Goal

## Success Criteria

## Current Context

## Constraints

## Non-goals

## Risks

## Approval Required

## Run Workspace Path

`{run_dir}`

## Round Budget

{args.round_budget}

## Initial Lanes

{", ".join(lanes) if lanes else "TBD by orchestrator"}

## Integration Policy

## Verification

## Stop Conditions
""",
    )
    write_new(
        run_dir / "orchestration.md",
        f"""# Orchestration: {args.title}

## Workflow Shape

## Why This Shape

## Enabled Lanes

{", ".join(lanes) if lanes else "TBD by orchestrator"}

## Disabled Lanes

## Agent / Team Model

Runner mode: `{resolved_runner_mode}`

Cross-runtime CLI calls allowed: `false`

## Round Budget

{args.round_budget}

## Risk And Approval Gates

## Verification Strategy

## Stop Conditions

- verify_pass
- blocked
- human_gate
- round_budget_exhausted
""",
    )
    write_json_new(run_dir / "state.json", state)
    write_json_new(run_dir / "token-usage.json", token_usage)
    write_json_new(run_dir / "orchestration.json", orchestration)
    write_json_new(run_dir / "runner-evidence.json", runner_evidence)
    if "clean_orchestrator_runtime" in orchestration:
        write_json_new(run_dir / "runtime-harness.json", runtime_harness)
    should_scaffold_card = (
        args.swarm_card == "auto"
        and bool(lane_specs)
        and (
            resolved_runner_mode != "manual_simulation"
            or len(lane_specs) > 1
        )
    )
    if should_scaffold_card:
        write_json_new(
            run_dir / "swarm-card.json",
            build_initial_card(
                slug=slug,
                runner_mode=resolved_runner_mode,
                round_id=round_id,
                round_budget=args.round_budget,
                lanes=lane_specs,
                goal=args.title,
            ),
        )
    if routing_context is not None:
        write_json_new(run_dir / "routing-policy.json", routing_context[0])
        write_json_new(run_dir / "runtime-capabilities.json", routing_context[1])
    write_json_new(round_dir / "integration.json", integration)
    write_new(
        round_dir / "integration.md",
        f"""# Integration: {args.title} ({round_id})

## Accepted

## Rejected

## Conflicts

## Repair Packets

## Verification Evidence

## Remaining Risks

## Next Round Or Stop Reason
""",
    )
    write_new(
        run_dir / "final-report.md",
        f"""# Final Report: {args.title}

## Outcome

## Workflow Shape

## Rounds

## Accepted Results

## Rejected Results

## Conflicts Resolved

## Verification Evidence

## Remaining Risks

## Token Usage

Workflow tokens: {{{{WORKFLOW_TOTAL_TOKENS}}}} ({{{{WORKFLOW_TOKEN_SOURCE}}}}, {{{{WORKFLOW_TOKEN_CONFIDENCE}}}}; excludes accounting finalizer and final user-facing response).

## Stop Reason
""",
    )

    print(run_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
