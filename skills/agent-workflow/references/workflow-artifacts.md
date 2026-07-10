# Agent Loop Run Workspace

Use this reference when `agent-workflow` creates, updates, validates, or summarizes a durable `.workflow/<slug>/` run workspace.

A run workspace is persistent coordination state for a harnessed agent loop. It lets multiple lanes and multiple rounds share contracts, evidence, findings, repair packets, and gate decisions. It is not a runner by itself.

## Directory Shape

New run workspaces use this shape:

```text
.workflow/<slug>/
|-- plan.md
|-- state.json
|-- token-usage.json      # total workflow token usage for final summary
|-- token-evidence.json   # native session usage snapshots for token-usage v2
|-- orchestration.md
|-- orchestration.json
|-- routing-policy.json   # optional immutable routing policy snapshot
|-- runtime-capabilities.json # optional lead-recorded capability snapshot
|-- swarm-card.json        # optional user-visible display state
|-- rounds/
|   `-- round-001/
|       |-- lane-runs/
|       |-- integration.json
|       `-- integration.md
`-- final-report.md
```

Optional future-runner directories may be added when useful:

```text
rounds/round-001/
|-- packets/
`-- logs/
```

Rules:

- Keep run workspaces small and auditable.
- Do not store transcripts, credentials, private customer data, bulky logs, secret-bearing command output, or unrelated files.
- Use repo-local `.workflow/` unless the current project has a stronger convention.
- Existing legacy workspaces may still have `packets/` and `results/`; do not mix legacy packet Markdown with the v1 lane JSON contract unless the final report explains the migration.

## `plan.md`

Human-readable operating contract:

```text
Goal:
Success criteria:
Current context:
Constraints:
Non-goals:
Risks:
Approval required:
Run workspace path:
Round budget:
Initial lanes:
Integration policy:
Verification:
Stop conditions:
```

Keep it short enough to guide the run. The plan is not progress by itself.

## `state.json`

Machine-readable progress state:

```json
{
  "schema_version": "agent-workflow.workflow.v2",
  "title": "Example workflow",
  "slug": "example-workflow",
  "created_at": "2026-07-08T00:00:00+00:00",
  "status": "planned",
  "current_round": "round-001",
  "round_budget": 3,
  "runner_mode": "codex_builtin_subagents",
  "runner_adapter": {
    "mode": "codex_builtin_subagents",
    "dispatch_surface": "multi_agent_v1",
    "cross_runtime_calls_allowed": false,
    "notes": "Lead agent calls Codex multi-agent tools directly.",
    "capability_evidence": {
      "source": "lead_agent",
      "summary": "Lead confirmed multi_agent_v1 tools are available in this session.",
      "verified": true
    }
  },
  "token_accounting": {
    "required_schema": "agent-workflow.token-usage.v2",
    "exact_required": true
  },
  "approval": {
    "required": false,
    "granted": null,
    "notes": ""
  },
  "gates": {
    "severity_policy": "P0/P1 block, P2 budget/risk decision, P3 record",
    "confidence_policy": "Independent verifier/challenger confidence gates the round"
  },
  "rounds": [
    {
      "round_id": "round-001",
      "status": "planned",
      "objective": "Compile the first orchestration plan.",
      "enabled_lanes": [],
      "gate_decision": "pending"
    }
  ],
  "final_status": "pending"
}
```

`agent-workflow.workflow.v2` makes exact token accounting non-downgradable: a
new workspace cannot delete this contract and replace its ledger with legacy
v1 estimates. Existing `agent-loops.workflow.v1` state remains supported for
pre-v2 workspaces.

Status values:

- workflow or round: `planned`, `orchestrated`, `running`, `integrating`, `verifying`, `passed`, `revising`, `blocked`, `complete`, `abandoned`
- gate: `pending`, `pass`, `revise`, `more_discovery`, `challenge`, `second_opinion`, `human_gate`, `blocked`
- runner mode: `codex_builtin_subagents`, `claude_code_builtin_subagents`, `manual_simulation`

## Native Runner Adapters

The first runner version supports only native subagent surfaces:

| Runner mode | Runtime | Dispatch surface | Notes |
| --- | --- | --- | --- |
| `codex_builtin_subagents` | Codex | `multi_agent_v1.spawn_agent`, `send_input`, `wait_agent`, `close_agent` | Use only when those tools are present in the current Codex session. |
| `claude_code_builtin_subagents` | Claude Code | Claude Code Agent/subagent surface; agent teams when enabled and appropriate | Use only from inside Claude Code, not by shelling out from Codex. |
| `manual_simulation` | Any | none | Use when no native subagent surface is available or subagents are not authorized. State clearly that no subagents ran. |

Rules:

- `cross_runtime_calls_allowed` must be `false`.
- Native modes require lead-recorded `runner_adapter.capability_evidence` before `executed` or `final` validation can pass. Environment variables alone are not capability proof.
- Do not use `claude -p`, `claude agents`, `codex exec`, or other CLI calls as a subagent runner from the other runtime.
- CE/MAT skills, reviewer prompts, and scripts can inform prompts or validation, but they are not runner adapters in v1.

### `runner-evidence.json`

Native runs should keep a compact lifecycle ledger at the run workspace root:

```json
{
  "schema_version": "agent-loops.runner-evidence.v1",
  "runner_mode": "codex_builtin_subagents",
  "dispatch_surface": "multi_agent_v1",
  "evidence_level": "lead_recorded",
  "cross_runtime_calls_allowed": false,
  "capability_evidence": {
    "source": "tool_search",
    "summary": "multi_agent_v1 exposed spawn_agent, wait_agent, send_input, and close_agent.",
    "verified": true
  },
  "agents": [
    {
      "round_id": "round-001",
      "lane_id": "verify-01",
      "execution_kind": "lead_recorded_native",
      "evidence_level": "lead_recorded",
      "agent_id": "019...",
      "spawn_tool": "multi_agent_v1",
      "wait_status": "completed",
      "close_status": "closed",
      "output_path": "rounds/round-001/lane-runs/verify-01.json"
    }
  ]
}
```

Execution kinds:

- `native_spawned`: a native subagent was spawned and backed by a durable tool-event record. This is reserved for a future runtime attestation path. In v1, `verify_workflow.py --mode final` rejects `tool_event_verified` because the lead cannot prove a self-authored event log came from the runtime.
- `lead_recorded_native`: the lead agent observed and recorded native subagent lifecycle fields, but no immutable tool-event log is available to the validator. Final reports must call this `lead-recorded`; do not describe it as independently verified native execution.
- `lead_owned`: the lead did the work directly, such as integration or a tightly scoped repair. Do not claim it was spawned as a native subagent.
- `manual_simulation`: no native subagent ran; final reports must say so.
- `legacy_native`: pre-hardening evidence may be accepted only as a documented compatibility path, not as the new standard.

Default lane agent mapping:

| Lane family | Codex agent type | Claude Code agent type |
| --- | --- | --- |
| `discover`, `seam` | `explorer` | `Explore` |
| `implement`, `repair` | `worker` | `general-purpose` |
| `plan` | `default` | `Plan` |
| `roundtable`, `review`, `challenge`, `verify`, `custom` | `default` | `general-purpose` |

The orchestrator may override agent type only when the current runtime exposes a more specific native subagent and the lane prompt still returns the required JSON envelope.

## Swarm Card Display State

`swarm-card.json` is optional user-visible display state for agent-team or swarm
workflows. New native-lane workflows and multi-agent manual simulations scaffold
`agent-workflow.swarm-card.v2` automatically. It is not runner evidence, and it
must not be used to prove that a native subagent ran. Validation still depends
on `orchestration.json`, `runner-evidence.json`, lane outputs, and integration
files. Legacy `agent-loops.swarm-card.v1` state remains readable.

Use it when the lead needs stable, re-renderable card state across rounds:

```json
{
  "schema_version": "agent-workflow.swarm-card.v2",
  "status": "preview",
  "title": "Agent Workflow",
  "slug": "example-workflow",
  "runner_mode": "codex_builtin_subagents",
  "round": {
    "current": "round-001",
    "budget": 3
  },
  "summary": {
    "agents_planned": 5,
    "phases_planned": 4,
    "goal": "Fix validator false-pass paths until no P2+ risk remains."
  },
  "legend": {
    "planned": "□",
    "running": "◐",
    "complete": "■",
    "waiting": "△",
    "skipped": "-",
    "blocked": "!",
    "failed": "×"
  },
  "phases": [
    {
      "id": "discover",
      "label": "Discover",
      "status": "running",
      "agents": [
        {
          "round_id": "round-001",
          "lane_id": "discover-01",
          "label": "current-state explorer",
          "runner": "native",
          "agent_type": "explorer",
          "status": "running",
          "status_note": null,
          "write_scope": "read-only",
          "model": {
            "display_name": "pending selection",
            "effort": null
          },
          "routing": {
            "packet_id": "packet-discover-01",
            "decision_id": "decision-discover-01",
            "planned_route": {"model": "gpt-5.6-terra", "effort": "high"},
            "terminal_actual_route": null,
            "route_status": "planned",
            "attempt_count": 0
          }
        }
      ]
    }
  ],
  "gate": {
    "policy": "P0/P1 block · P2 repair-or-defer · P3 record",
    "decision": "pending",
    "open_p2_plus": 0
  },
  "display_policy": {
    "format": "markdown_left_rail",
    "emit": [
      "before_dispatch",
      "after_first_dispatch",
      "phase_status_change",
      "gate_decision",
      "round_transition",
      "final_stop"
    ],
    "status_polling": false,
    "updates": "event_only",
    "emit_only_when_rendered_card_changes": true
  },
  "last_emitted_hash": null
}
```

Render cards with `scripts/render_swarm_card.py`. It uses Markdown blockquotes
as the left rail and one agent per line, so Chinese text, renderer font fallback,
and mixed-width status symbols never need a calculated right edge. Every symbol
is immediately followed by a status label. Executor kind remains durable JSON
metadata but has no symbol; model and inherited effort appear in italic parentheses.
Before rendering, the helper projects workflow status and round from
`state.json`, goal and planned route from `orchestration.json`, terminal route
from `runner-evidence.json`, and completed/blocked/failed status from lane
outputs. Routing projections take precedence over the fallback `model` display
field. The card remains display state; those source artifacts remain
authoritative.

```markdown
> **Agent Workflow · RUNNING**
> `validator-hardening` · Round 1/3 · 1/5 complete · Codex native
> Tokens: measuring
>
> Fix validator false-pass paths until no P2+ risk remains.
>
> **Discover**
> ■ complete · `discover-01` · current-state explorer *(Terra · xhigh · inherited)*
>
> **Implement & Repair**
> ◐ running · `implement-01` · bounded writer *(Terra · xhigh · inherited)*
> △ waiting: review findings · `repair-01` · targeted repair *(Terra · xhigh · inherited)*
>
> **Review & Challenge**
> □ not started · `review-01` · independent review *(Sol · xhigh · inherited)*
>
> **Verify**
> □ not started · `verify-01` · evidence gate *(Sol · xhigh · inherited)*
>
> **Gate** Pending · Open P2+: 0
```

Status symbols are only a scan aid; the adjacent text is authoritative:

```text
□ not started   ◐ running   △ waiting   ■ complete
- skipped       ! blocked   × failed
```

Preview and running cards render `Tokens: measuring`. When sibling
`token-usage.json` is finalized with `confidence: exact`, the final card renders
the comma-grouped exact total. Estimated or incomplete token state never becomes
a numeric card claim.

Display policy:

- Emit `PREVIEW` after orchestration is compiled and before first dispatch.
- Emit `RUNNING` after first dispatch or recorded start.
- Emit an update on phase status changes, material failures/blockers,
  integration gate decisions, round transitions, and final stop.
- Do not emit every individual subagent update unless the update changes the
  next action or risk.
- At an allowed event, run
  `python3 scripts/render_swarm_card.py <workflow-dir> --emit`. The renderer
  stores a SHA-256 of the visible Markdown in `last_emitted_hash`; an unchanged
  card produces no output.
- Emit only from dispatch, terminal wait, integration, and gate events. Do not
  spend a model completion polling or redrawing unchanged state.
- When final reports mention native lifecycle with `lead_recorded` evidence,
  preserve the lead-recorded wording rules from `runner-evidence.json`; the card
  cannot upgrade lead-recorded lifecycle fields into independent proof.
- For manual simulation, still show the card when the workflow is multi-agent in
  shape, but append `simulated` as text inside the model parentheses and state
  that no native subagent lifecycle was recorded. Do not add an executor symbol.

## `orchestration.md`

Human-readable explanation of the orchestrator's choices:

```text
Workflow shape:
Why this shape:
Enabled lanes:
Disabled lanes:
Agent/team model:
Round budget:
Risk and approval gates:
Verification strategy:
Stop conditions:
```

Use this file to help the lead agent, future agents, or the operator understand the workflow without reading raw JSON.

## `orchestration.json`

Machine-readable plan compiled by the orchestrator before the round starts:

```json
{
  "schema_version": "agent-loops.orchestration.v1",
  "workflow": {
    "title": "Example workflow",
    "slug": "example-workflow",
    "goal": "Deliver the requested outcome with independent verification.",
    "success_criteria": ["The final output satisfies the user's request."],
    "constraints": [],
    "non_goals": []
  },
  "orchestrator": {
    "planning_mode": "planner_first",
    "runner_mode": "codex_builtin_subagents",
    "runner_adapter": {
      "mode": "codex_builtin_subagents",
      "dispatch_surface": "multi_agent_v1",
      "cross_runtime_calls_allowed": false,
      "notes": "Lead agent calls Codex multi-agent tools directly."
    },
    "round_budget": 3,
    "stop_conditions": ["verify_pass", "blocked", "human_gate", "round_budget_exhausted"],
    "invalid_json_policy": "repair_once_then_invalid_output",
    "display_policy": {
      "swarm_card": "markdown_left_rail",
      "emit": [
        "before_dispatch",
        "after_first_dispatch",
        "phase_status_change",
        "gate_decision",
        "round_transition",
        "final_stop"
      ],
      "polling": "disabled_status_only_event_updates"
    }
  },
  "rounds": [
    {
      "round_id": "round-001",
      "objective": "First planned round.",
      "lanes": []
    }
  ]
}
```

Each lane object:

```json
{
  "id": "review-01",
  "lane": "review",
  "enabled": true,
  "required": true,
  "agent_count": 1,
  "purpose": "Find correctness and risk issues before verify.",
  "prompt": "Return JSON only using the lane-output envelope.",
  "input_refs": ["plan.md", "rounds/round-001/lane-runs/implement-01.json"],
  "output_schema": "review_payload.v1",
  "gate": {
    "blocks_on": ["P0", "P1"],
    "confidence_source": "independent_reviewer"
  },
  "runner": {
    "mode": "codex_builtin_subagents",
    "agent_type": "default",
    "dispatch_method": "spawn_agent"
  }
}
```

Built-in lane names:

```text
discover, plan, roundtable, implement, seam, review, challenge, verify, repair, custom
```

Integration is lead-owned in v1. New run workspaces should not scaffold an
`integrate` worker lane; existing `integrate` lane outputs are advisory legacy
evidence only.

For `custom`, the lane object must define:

- `custom_name`
- `purpose`
- `input_refs`
- `output_schema`
- `gate`

## Opt-In Execution-Efficiency Artifacts

Execution efficiency extends the existing v1 workspace only when
`orchestration.execution_efficiency.enabled` is exactly `true`. Scaffold it
with a native runner:

```bash
python3 scripts/new_workflow.py "Efficient native workflow" \
  --runner-mode codex_builtin_subagents \
  --runner-capability-evidence "Observed multi_agent_v1 in this session." \
  --execution-efficiency native \
  --lanes discover,implement,review,verify
```

The policy declares the mechanism and rollback boundary:

```json
{
  "schema_version": "agent-workflow.execution-efficiency.v1",
  "enabled": true,
  "activation": "explicit_opt_in",
  "risk_class": "medium",
  "context": {
    "default_mode": "isolated",
    "lead_fork_context": false,
    "lane_fork_context": false,
    "parent_transcript": "excluded",
    "prior_lane_outputs": "references_only",
    "input_refs": "digest_bound_files"
  },
  "admission": {
    "lead_owned": ["plan", "integration"],
    "conditional_lanes": ["seam"],
    "deterministic_work": "script_only",
    "duplicate_question_policy": "reject"
  },
  "quality": {
    "write_lanes_require": ["review", "verify"],
    "high_risk_require": ["challenge", "verify"],
    "assessment_identity": "independent_from_writer"
  },
  "wait": {
    "strategy": "notification_first",
    "barrier": "multi_target",
    "max_native_wait_ms": 3600000,
    "min_repoll_ms": 300000,
    "status_polling": false,
    "card_updates": "event_only"
  },
  "budgets": {
    "default_max_tool_turns": 24,
    "default_max_test_runs": 3,
    "max_writer_reuse": 1,
    "on_exhausted": "checkpoint_then_gate"
  },
  "result_transport": {
    "mode": "artifact_receipt",
    "max_inline_chars": 2048,
    "integration_input": "compact_index"
  },
  "rollback": {
    "mode": "disable_policy",
    "artifact_migration_required": false
  }
}
```

Each enabled lane gains an immutable execution contract. Codex lanes also set
`runner.fork_context: false`; Claude Code lanes set
`runner.context_mode: "isolated"`.
Execution-efficiency v1 requires `agent_count: 1` for each lane; create distinct
lane IDs when several independent opinions are needed.

The opt-in orchestration also records a portable
`workflow.workspace_root` relative to the run directory. During dispatch
preparation, each lane `input_refs` entry becomes a file-bound object:

```json
{
  "root": "workspace",
  "path": "skills/agent-workflow/scripts/verify_workflow.py",
  "content_sha256": "sha256:..."
}
```

`root` is exactly `workflow` or `workspace`. Dot paths, parent traversal,
directories, missing files, duplicate references, and stale content digests
fail validation. Prior lane JSON may be referenced as a file only after it
exists and receives this content binding; its contents are never injected as
implicit parent context.
Dispatch preparation also computes one `workflow_contract_sha256` over title,
slug, goal, success criteria, constraints, and non-goals. Every lane execution,
receipt, and integration-index entry carries that digest. A passing verify
payload must list the exact current success criteria in order; changing the
workflow target therefore invalidates stale dispatches and receipts.

```json
{
  "schema_version": "agent-workflow.lane-execution.v1",
  "context_mode": "isolated",
  "parent_transcript": "excluded",
  "raw_prior_outputs": false,
  "output_path": "rounds/round-001/lane-runs/review-01.json",
  "workflow_contract_sha256": "sha256:...",
  "dispatch_sha256": "sha256:...",
  "admission": {
    "decision": "enabled",
    "unique_question": "Which P2+ defects remain in the bounded diff?",
    "expected_state_change": "Persist evidence-bound review findings.",
    "reason": "A writer-independent correctness pass is required.",
    "deterministic": false,
    "exception_reason": ""
  },
  "budget": {
    "max_tool_turns": 24,
    "max_test_runs": 3,
    "on_exhausted": "checkpoint_then_gate"
  },
  "repair_affinity": {
    "strategy": "not_applicable",
    "source_lane_id": null,
    "max_writer_reuse": 0,
    "verifier_must_be_independent": true
  },
  "receipt": {
    "schema_version": "agent-workflow.lane-receipt.v1",
    "max_inline_chars": 2048
  }
}
```

Before dispatch, populate every `purpose`, specific prompt, admission field,
input reference, and any `plan`/`seam` exception. The prompt must name its JSON
payload schema and exact output path. Freeze the contract with:

```bash
python3 scripts/prepare_dispatch.py .workflow/<slug>
python3 scripts/verify_workflow.py .workflow/<slug> --mode planned
```

After writing a complete lane envelope, the lane or lead produces a compact
receipt:

```bash
python3 scripts/lane_receipt.py .workflow/<slug> round-001 review-01
```

```json
{
  "schema_version": "agent-workflow.lane-receipt.v1",
  "round_id": "round-001",
  "lane_id": "review-01",
  "status": "complete",
  "output_path": "rounds/round-001/lane-runs/review-01.json",
  "output_sha256": "sha256:...",
  "workflow_contract_sha256": "sha256:...",
  "dispatch_sha256": "sha256:...",
  "gate": "pass",
  "finding_count": 0,
  "summary": "No P2+ defect remains in the bounded diff."
}
```

The lane returns only this receipt-sized information to the lead. The full
JSON remains in the workspace. `collect_results.py` validates every receipt and
writes `integration-index.json`, whose lane entries contain only identifiers,
status, gate, summary, finding IDs/count, confidence, paths, and digests.
Integration reads that index first and opens raw output only for an accepted
finding, conflict, repair packet, or evidence check.

`runner-evidence.json.execution_efficiency` records the Lead-side wait surface:

```json
{
  "lead_model_completions": 4,
  "status_only_completions": 0,
  "functions_wait_calls": 1,
  "wait_waves": [
    {
      "wave_id": "round-001-assessment",
      "barrier_id": "round-001-quality-gate",
      "targets": ["agent-review", "agent-verify"],
      "timeout_ms": 900000,
      "outcome": "completed",
      "started_at": "2026-07-10T00:00:00Z",
      "completed_at": "2026-07-10T00:05:00Z",
      "trigger": "dispatch",
      "trigger_ref": "orchestration.json round-001 quality dispatch",
      "terminal_targets": ["agent-review"]
    }
  ],
  "card_events": [
    {
      "reason": "dispatch",
      "state_changed": true,
      "rendered_sha256": "sha256:..."
    }
  ]
}
```

Each `agents[]` record also carries `execution_metrics`:

```json
{
  "model_completions": 1,
  "tool_turns": 7,
  "test_runs": 2,
  "repair_reuse_count": 0,
  "budget_outcome": "within_budget",
  "context_forked": false,
  "received_parent_transcript": false,
  "dispatch_sha256": "sha256:...",
  "receipt_path": "rounds/round-001/receipts/review-01.json"
}
```

Tool and test budgets are hard limits: checkpoint, rotate, or gate before a
counter exceeds its maximum. A checkpoint/rotation records `budget_note` and a
distinct `successor_lane_ref`; rotation also proves a new identity. A canonical
writer identity may be reused by only one repair across all implement sources,
and a repair cannot use another repair as its source. Review, challenge, and
verify compare both `agent_id` and `native_handle` against writer identifiers.
Final validation also requires every agent identity to appear in terminal wait
coverage and requires zero status-only completions.
Every subsequent wait records `prior_terminal_event`, `prior_timeout`,
`material_failure`, or `user_interruption`; failure/interruption triggers need
an evidence ref, and a timeout-triggered re-wait must respect
`min_repoll_ms`. This distinguishes event-driven continuation from status
polling while supporting native wait APIs that return after the first target
finishes.
Each wait names a `barrier_id`. A new barrier starts from an evidenced
`dispatch`; continuation targets must equal the prior active set minus terminal
targets. Timeout outcomes are valid only after `timeout_ms` actually elapsed,
and their continuation must respect `min_repoll_ms`. Wait aliases must map to
registered runner identities, and one wave cannot include two aliases for the
same record. Final runtime evidence must cover every recorded agent identity as
terminal, not merely as a wait target.

Disable or remove this opt-in block to return to prior v1 behavior. No artifact
migration is required, and the extra lane fields remain harmless optional data.

## Opt-In Model Routing Artifacts

Routing is feature-gated inside the existing workspace. It is enabled only when `orchestration.model_routing.enabled` is exactly `true`, and only for `codex_builtin_subagents`. Missing or disabled routing preserves ordinary workspaces, manual simulation, Claude Code runners, and runner records without attempts. Responsibility routing uses policy, capability, and decision schema v2.
Already-dispatched v1 routed workspaces are never reinterpreted as v2. Resume them with their pinned skill snapshot or create a new v2 round plan before dispatching new attempts. A v1 capability inventory may be supplied to the new scaffold; its obsolete `automatic_efforts` fields are discarded because effort now comes from the user session.

Scaffold with explicit capability input:

```bash
python3 scripts/new_workflow.py "Routed workflow" \
  --runner-mode codex_builtin_subagents \
  --model-routing codex \
  --runtime-capabilities /path/to/capabilities.json \
  --reasoning-effort xhigh \
  --lanes discover,implement,verify
```

The scaffold writes `routing-policy.json` and `runtime-capabilities.json`, then references both from `orchestration.model_routing`:

```json
{
  "enabled": true,
  "activation": "explicit_opt_in",
  "adapter": "codex_builtin_subagents",
  "policy_snapshot": {
    "path": "routing-policy.json",
    "snapshot_id": "responsibility-routing-codex-v2-policy-v1",
    "content_sha256": "sha256:..."
  },
  "capability_snapshot": {
    "path": "runtime-capabilities.json",
    "snapshot_id": "runtime-capabilities-...",
    "content_sha256": "sha256:..."
  },
  "reasoning_effort": {
    "source": "user_session",
    "value": "xhigh",
    "locked": true
  },
  "dispatch_gate": "python3 scripts/verify_workflow.py <workflow-dir> --mode planned"
}
```

Snapshot digests use canonical UTF-8 JSON with sorted keys, compact separators, `ensure_ascii=false`, and the root `content_sha256` field omitted. Snapshot IDs and digests are copied into every decision. A digest mismatch fails closed. The registered v2 policy also has a pinned semantic fingerprint: changing `policy_id`, `policy_version`, or deleting required decision/verifier protections fails even when the edited snapshot has a freshly recomputed content digest.

`runtime-capabilities.json` must use a valid timezone-aware RFC3339 `observed_at`. The scaffold adds one locked `reasoning_effort` with `source: user_session`; every model must advertise that effort in `supported_efforts` before it can run. Supplying the inventory does not certify current availability or the native runner surface. Before `--mode planned`, `runner_adapter.capability_evidence` must be an explicit lead-recorded recheck with `source: lead_agent`, `verified: true`, an RFC3339 `checked_at`, a substantive summary, and the exact capability snapshot digest. The recheck cannot predate the observation, be materially future-dated, or exceed the freshness window enforced by the validator.

### Route Policy

The router chooses only between two models:

```text
Sol   decides, interprets, plans, reviews, challenges, verifies,
      or handles ambiguity, cross-boundary work, novelty, and high risk.
Terra executes a bounded packet with clear inputs, scope, and checks.
```

Reasoning effort is not part of that decision. Use a runtime-provided current effort when available or the user's explicit statement; if neither is available, ask instead of guessing. The session effort is snapshotted once and copied unchanged into every minimum, selected, dispatched, and actual route. A lead or user may raise Terra to Sol with a reason and evidence references, but may not lower a Sol minimum or alter effort per lane. Terra unavailability may fall forward to Sol at the same effort; Sol unavailability becomes a human gate. An evidenced `insufficient_reasoning` transition may likewise change only Terra to Sol at the same effort. Route-changing evidence references must be safe POSIX workspace-relative paths, may not traverse parents or point to ephemeral/absolute locations, and must resolve to existing substantive artifacts. Escalation evidence must bind to the immediately preceding failed attempt.

### Planned Lane Decision

Every enabled routed lane has exactly one agent and one `routing` decision under `orchestration.json rounds[].lanes[]`:

```json
{
  "schema_version": "agent-workflow.routing-decision.v2",
  "packet_id": "packet-implement-01",
  "decision_id": "decision-implement-01",
  "decision_sha256": "sha256:...",
  "policy_snapshot": {"snapshot_id": "...", "content_sha256": "sha256:..."},
  "capability_snapshot": {"snapshot_id": "...", "content_sha256": "sha256:..."},
  "facts": {
    "ambiguity": "bounded",
    "coupling": "local",
    "blast_radius": "local",
    "reversibility": "easy",
    "verifiability": "deterministic",
    "novelty": "established",
    "role": "implement",
    "claim_class": "routine",
    "approval_required": false
  },
  "request": {
    "source": "automatic",
    "requested_route": null,
    "reason": "",
    "evidence_refs": []
  },
  "matched_rule_id": "route.terra.bounded_execution",
  "minimum": {"model": "gpt-5.6-terra", "effort": "xhigh"},
  "selected": {"model": "gpt-5.6-terra", "effort": "xhigh"},
  "override": {"applied": false, "direction": "none"},
  "verification_floor": {
    "rule_id": "verify.terra.routine",
    "required": false,
    "minimum_route": {"model": "gpt-5.6-terra", "effort": "xhigh"},
    "verifier_lane_ids": [],
    "independent_of_lane_ids": [],
    "required_evidence": [],
    "missing_evidence_action": "blocked"
  },
  "status": "planned"
}
```

The complete decision digest omits only `decision_sha256`. Run `verify_workflow.py --mode planned` after replacing every scaffold `draft`; no routed dispatch is allowed before it passes. When verification is required, planned mode requires non-empty verifier lanes, named author-independence bindings, required evidence names, and planned verifier routes at or above the floor. Once an attempt references the digest, do not rewrite facts, selected route, snapshot references, verifier bindings, or the decision digest.

### Append-Only Attempts

Keep one `runner-evidence.json agents[]` record per round/lane. Add routing identity fields plus an append-only attempt ledger to that existing lifecycle record:

```json
{
  "round_id": "round-001",
  "lane_id": "implement-01",
  "decision_id": "decision-implement-01",
  "planned_decision_sha256": "sha256:...",
  "terminal_attempt_id": "attempt-01",
  "attempts": [
    {
      "attempt_id": "attempt-01",
      "ordinal": 1,
      "transition": "initial",
      "parent_attempt_id": null,
      "decision_id": "decision-implement-01",
      "planned_decision_sha256": "sha256:...",
      "route": {"model": "gpt-5.6-terra", "effort": "xhigh"},
      "actual_route": {"model": "gpt-5.6-terra", "effort": "xhigh"},
      "outcome": "completed",
      "failure_class": null,
      "evidence_refs": [],
      "lifecycle": {
        "execution_kind": "lead_recorded_native",
        "evidence_level": "lead_recorded",
        "agent_id": "recorded-agent-id",
        "native_handle": null,
        "spawn_tool": "multi_agent_v1",
        "wait_status": "completed",
        "close_status": "closed",
        "output_path": "rounds/round-001/lane-runs/implement-01.json"
      }
    }
  ]
}
```

Ordinals are contiguous, IDs are unique, and every child points to the immediately preceding attempt. `retry` keeps model and effort after context/tool failure; `fallback` follows an unavailable Terra attempt and may use Sol at the same effort; `escalation` follows evidenced insufficient reasoning and may likewise change only Terra to Sol without changing effort. One retry and one model change are allowed. The top-level lifecycle fields and `terminal_attempt_id` project the last attempt. A completed attempt is terminal, and its actual route must equal its dispatched route. Final mode requires every routed lane's terminal attempt to be completed; executed mode may retain a failed/unavailable attempt only while the lane/workflow gate remains non-pass.

### Verifier Floor And Display Projection

Claim facts determine whether verification is required and its minimum route. Planned mode validates verifier membership, author bindings, and route floors before dispatch. Final mode requires every named verifier to be a planned `verify` lane that completes with a pass gate, has a completed terminal actual route at or above the floor, uses a different recorded `agent_id` and `native_handle` from every lane named in `independent_of_lane_ids`, and exposes every `required_evidence` name as a substantive passing check. Missing evidence follows the decision's `more_discovery`, `human_gate`, or `blocked` action; it never silently passes.

Capability data, identities, attempts, and actual routes are lead-recorded evidence in v2, not runtime attestation. Reports must say so. `swarm-card.json phases[].agents[].routing` is only a projection of the planned decision and terminal attempt:

```text
packet_id, decision_id, planned_route, terminal_actual_route, effort_source, route_status, attempt_count
```

When a card exists, final mode recomputes these fields and rejects drift. The card is optional and cannot replace policy snapshots, decisions, runner attempts, lifecycle evidence, or verifier output.

## `token-usage.json`

New workflow final summaries report an exact total computed from native runtime
session events. `new_workflow.py` scaffolds
`agent-workflow.token-usage.v2` and attempts to capture a Codex start snapshot
automatically. If no supported runtime log is available, exact accounting fails
closed; a Lead estimate cannot satisfy a v2 final pass.

Required lifecycle:

1. Let `new_workflow.py` auto-start from `CODEX_THREAD_ID`, or run `python3 scripts/token_accounting.py start <workflow-dir> --runtime codex|claude --lead-session-id <id>` before dispatch.
2. Immediately after every native spawn, including retry, fallback, escalation, or repair, run `python3 scripts/token_accounting.py register-agent <workflow-dir> --execution-ref <attempt-ref> --agent-id <session-id> --round-id <round-id> --lane-id <lane-id>`.
3. Finish release checks, integration, final gates, and every registered subagent lifecycle before accounting.
4. In the next accounting-only Lead completion, run `python3 scripts/token_accounting.py finalize <workflow-dir>`, then do no more workflow work. Deliver the already-prepared response only after final validation passes; if accounting fails, gate or block instead of estimating.

```json
{
  "schema_version": "agent-workflow.token-usage.v2",
  "status": "pending",
  "source": "runtime_session_events",
  "confidence": "pending",
  "unit": "tokens",
  "strategy": "actor_deltas",
  "total_tokens": null,
  "input_tokens": null,
  "cached_input_tokens": null,
  "cache_creation_input_tokens": null,
  "cache_read_input_tokens": null,
  "output_tokens": null,
  "reasoning_tokens": null,
  "method": "Pending native runtime session accounting.",
  "boundary": {
    "start": "latest completed runtime usage event before accounting start",
    "end": "latest completed runtime usage event before accounting finalizer",
    "includes": ["Lead workflow completions", "every registered native attempt"],
    "excludes": ["accounting finalizer completion", "final user-facing response"],
    "final_user_response_included": false,
    "exclusive_to_workflow": true
  },
  "accounting": {
    "runtime": null,
    "lead_session_id": null,
    "started_at": null,
    "finalized_at": null,
    "participants": []
  },
  "measurements": [],
  "coverage": {
    "expected_execution_refs": [],
    "covered_execution_refs": [],
    "uncovered_execution_refs": [],
    "overlapping_execution_refs": []
  },
  "evidence_ref": "token-evidence.json",
  "evidence_sha256": null,
  "round_breakdown": [],
  "agent_breakdown": [],
  "notes": []
}
```

The helper uses `actor_deltas` rather than lane estimates:

- Lead tokens are the end cumulative runtime counter minus the start counter.
- A Codex native agent contributes its terminal cumulative `token_count` total.
- Claude finalized message usage is deduplicated by message id; input, cache
  creation, cache reads, and output are each counted once.
- Reusing one native session for a bounded repair creates another execution ref
  on the same session measurement; its cumulative total is not double-counted.
- Every spawned attempt must be registered immediately. Final validation
  compares participants against visible `runner-evidence.json` records, routed
  attempts, and the runtime-discovered Codex descendant tree or Claude
  subagent directory. An attempt omitted from both Lead-authored ledgers still
  fails when its native child session exists.

`token-evidence.json` stores start/end event references, event hashes, runtime
session ids, agent terminal snapshots, and the exact usage fields used by the
calculation. `token-usage.json.evidence_sha256` binds that evidence file. This
is runtime-event-derived but lead-recorded provenance, not an independently
signed provider invoice. Final validation also reopens the source JSONL and
checks the recorded session identity, event/message digest, and usage payload.

Final v2 mode requires complete/exact state, monotonic counters, correct delta
and aggregate arithmetic, one-to-one execution coverage, terminal agent logs,
a matching evidence digest, and a final report that repeats the total, source,
confidence, and excluded post-snapshot boundary. The finalizer and final
response are deliberately excluded because their tokens are produced after the
end snapshot.

Existing `agent-loops.token-usage.v1` workspaces remain readable. A v1
`lead_estimated` or `manual_estimate` final is accepted only as legacy estimated
accounting. A v1 document that claims `runtime_reported`, `runner_reported`, or
`confidence: exact` fails final mode and must migrate to v2 evidence.

## Lane Output Envelope

Every intermediate lane output must be JSON and must use this common envelope:

```json
{
  "schema_version": "agent-loops.lane-output.v1",
  "run_id": "round-001-review-01",
  "round_id": "round-001",
  "lane_id": "review-01",
  "lane": "review",
  "status": "complete",
  "summary": "Found one actionable issue.",
  "confidence": {
    "self": 0.74,
    "independent": 0.68,
    "source": "reviewer",
    "rationale": "Evidence is concrete but one affected path was not tested."
  },
  "findings": [
    {
      "severity": "P1",
      "claim": "Reset can leave stale undo state.",
      "evidence": [{"path": "server/game.ts", "line": 123}],
      "recommendation": "Clear undo state when reset completes.",
      "repair_packet": {
        "objective": "Clear undo state during reset",
        "ownership": ["server/game.ts", "tests/game.test.ts"]
      }
    }
  ],
  "gate": {
    "decision": "revise",
    "reason": "P1 correctness issue with a bounded repair.",
    "next_lanes": ["repair", "verify"]
  },
  "payload": {}
}
```

Required envelope keys:

- `schema_version`
- `run_id`
- `round_id`
- `lane_id`
- `lane`
- `status`
- `summary`
- `confidence`
- `findings`
- `gate`
- `payload`

Lane status values:

```text
pending, running, complete, skipped, blocked, invalid_output
```

Severity values:

```text
P0, P1, P2, P3
```

Gate decisions:

```text
pass, revise, more_discovery, challenge, second_opinion, human_gate, blocked
```

Confidence values are numbers from `0.0` to `1.0`, or `null` when not applicable. Gate decisions should use independent verifier/challenger confidence when available.

## Lane Payloads

Use lane-specific `payload` objects. Keep payloads compact and evidence-backed.

### `discover_payload.v1`

```json
{
  "sources_read": [{"path": "AGENTS.md", "why": "workspace policy"}],
  "current_state": ["Known fact"],
  "constraints": ["Constraint"],
  "unknowns": ["Unknown"],
  "risks": ["Risk"],
  "recommended_next_lanes": ["plan", "challenge"]
}
```

### `plan_payload.v1`

```json
{
  "approach": ["Step"],
  "work_slices": [
    {
      "id": "slice-01",
      "objective": "Bounded objective",
      "ownership": ["path/or/domain"],
      "verification": ["Check"]
    }
  ],
  "dependencies": [],
  "approval_gates": [],
  "recommended_next_lanes": ["implement", "review"]
}
```

### `roundtable_payload.v1`

```json
{
  "topic": "Spec direction",
  "participants": [
    {
      "id": "operator",
      "participant_type": "agent_role",
      "stance": "Prioritizes deployability and rollback.",
      "selection_reason": "The task touches runtime behavior."
    }
  ],
  "tension_map": [
    {
      "axis": "speed_vs_rigor",
      "positions": [{"participant_id": "operator", "position": "favor_rigor"}]
    }
  ],
  "rounds": [
    {
      "round": 1,
      "guiding_question": "What can fail if we rush?",
      "core_disagreement": "Scope safety vs delivery speed",
      "summary": "The deepest disagreement is whether verification can be deferred."
    }
  ],
  "open_questions": [],
  "decision_options": [],
  "recommended_next_lanes": ["challenge", "verify"]
}
```

`participant_type` values:

```text
real_person, agent_role
```

### `implement_payload.v1`

```json
{
  "changes": [{"path": "file.ts", "summary": "What changed"}],
  "assumptions": [],
  "tests_or_checks_run": [],
  "needs_review": ["Concern"],
  "recommended_next_lanes": ["review", "verify"]
}
```

### `seam_payload.v1`

```json
{
  "interfaces": [{"name": "API", "risk": "Mismatch risk"}],
  "ownership_boundaries": [],
  "integration_risks": [],
  "adapter_or_contract_changes": [],
  "recommended_next_lanes": ["implement", "review"]
}
```

### `review_payload.v1` and `challenge_payload.v1`

```json
{
  "findings": [],
  "assumptions_attacked": [],
  "missing_evidence": [],
  "repair_packets": [],
  "recommended_next_lanes": ["repair", "verify"]
}
```

### `verify_payload.v1`

```json
{
  "checks": [
    {
      "name": "unit tests",
      "kind": "command",
      "command": "npm test",
      "status": "pass",
      "evidence": "All tests passed."
    }
  ],
  "success_criteria_status": [
    {
      "criterion": "Final output satisfies request",
      "status": "pass",
      "evidence": "Verified by scoped checks."
    }
  ],
  "confidence_drivers": ["Direct test evidence"],
  "remaining_uncertainty": [],
  "recommended_gate": "pass"
}
```

### `repair_payload.v1`

```json
{
  "repair_objective": "Fix the P1 reset issue.",
  "source_findings": ["round-001-review-01:finding-01"],
  "changes": [],
  "checks_run": [],
  "remaining_risk": [],
  "recommended_next_lanes": ["verify"]
}
```

## Integration Files

`rounds/<round-id>/integration.json` records machine-readable synthesis:

```json
{
  "schema_version": "agent-loops.integration.v1",
  "round_id": "round-001",
  "status": "pending",
  "accepted": [],
  "rejected": [],
  "conflicts": [],
  "repair_packets": [],
  "finding_resolutions": [],
  "verification_evidence": [],
  "remaining_risks": [],
  "next_round": null,
  "stop_reason": null
}
```

`rounds/<round-id>/integration.md` is the human-readable decision ledger:

```text
Accepted:
Rejected:
Conflicts:
Repair packets:
Verification evidence:
Remaining risks:
Next round or stop reason:
```

## Gate Rules

Severity gate:

- `P0` or `P1`: block pass unless explicitly accepted by a human gate.
- `P2`: repair or defer based on scope, budget, and risk.
- `P3`: record; do not block by default.

Every `P2` or higher finding needs a stable id, normally
`<round-id>:<lane-id>:finding-N`, and a final integration-level resolution:
`repaired_by`, `deferred_with_reason`, `human_gate`, `rejected_with_reason`, or
`blocked`. A lane-local deferral is not enough for final pass unless integration
accepts it in `finding_resolutions`. `P2+` finding ids must be unique across the
workflow, because final validation requires one auditable resolution per
finding occurrence. Resolution records must be typed and
auditable: `repaired_by` needs repair/check evidence, `deferred_with_reason`
needs owner/scope/reason/non-blocking rationale, `human_gate` needs decision
evidence, `rejected_with_reason` needs a reason, and `blocked` needs a blocker
reason.

Confidence gate:

- Use independent verifier/challenger confidence for pass/revise decisions.
- If the work is subjective, strategic, or spec-heavy, treat low confidence as a signal for `roundtable`, `challenge`, `second_opinion`, or `human_gate`.
- Low confidence alone should explain what evidence is missing and which next lane would raise confidence.

Iteration:

- Start a new round when integration has actionable repair packets, missing evidence, unresolved `P0/P1`, low independent confidence, or a gate decision other than `pass`.
- Do not open a new round just to produce more activity.
- Stop on `pass`, `blocked`, `human_gate`, explicit deferral of remaining risks, or exhausted round budget.

## Verification

Match checks to blast radius:

- code: unit tests, scoped tests, lint, typecheck, build
- UI: browser smoke, screenshot review, responsive checks
- data: dry run, sample comparison, migration rollback check
- research: source citation check, date/version caveats, contradiction pass
- strategy/spec/design: independent challenge, roundtable tension check, unresolved-question ledger, confidence rationale
- operations: manual checklist against success criteria and approval boundaries

`scripts/verify_workflow.py` has explicit modes:

- `--mode scaffold`: validate fresh workspace shape; placeholders and missing lane outputs are allowed.
- `--mode planned`: require populated orchestration, verified native capability evidence, immutable routed snapshots and decisions, and a dispatchable `planned` status for every enabled routed lane; lane outputs and attempts are not required yet.
- `--mode executed`: require populated orchestration, planned lane outputs, lane contract matching, native capability evidence, and semantic gate checks.
- `--mode final`: add final status, terminal state consistency, state/orchestration progress consistency, substantive final report content, integration/finding resolution, native lifecycle evidence, verification evidence, required-lane completion, populated token usage accounting, and no non-deferred `P2+` findings.
- Progress consistency is object-aware: `round_id` values, lane ids within an orchestration round, and `state.rounds[].enabled_lanes` entries must be unique so duplicate identifiers cannot shadow required work.
- Final terminal consistency covers every declared round. Earlier rounds must have terminal state/integration disposition, and all enabled lane outputs must be complete before final pass.
- A final pass aggregates pass-like lane gates: every enabled lane in the terminal round must pass, and any prior round that claims `passed`/`complete` must not retain lane outputs whose gate still asks for `revise`, `blocked`, or other non-pass decisions. Prior rounds that need repair should remain `revising` with `next_round` until a later pass round resolves them.
- Native `lead_owned` dispatch is reserved for lead-owned implementation/repair work, not normal spawned lanes.
- Final mode also scans the workspace filesystem: `rounds/*` directories must be declared in state/orchestration, and strict modes reject nested entries under `lane-runs/` so hidden JSON lane outputs cannot escape validation.
- Check evidence is positive evidence only when its status is `pass` and the evidence text is substantive. Low-information evidence such as `ok`, `pass`, `done`, `true`, or longer content-free phrases like "all checks passed" cannot satisfy verify pass, success-criteria proof, final-report evidence binding, or `repaired_by` resolution proof. Evidence should include auditable specifics such as a persisted command/fixture result under the workflow workspace, an existing file, source file, or lane-output path plus an inspected fact actually present in that artifact, or a fixture result whose name, exit code, and claimed command match the referenced results JSON in the same local result claim. A result name containing the word `command` is not itself a command claim; wrong-command claims still fail when a command is explicitly named. Fixture result matching is longest-name and boundary aware, so overlapping names such as `alpha` and `alpha bad` bind to the intended named result; duplicate names, duplicate-ish names such as `alpha_bad`, `alpha.bad`, `alpha,bad`, `alpha;bad`, or `alpha bad` when only `alpha` exists, and shared exit-code wording across multiple named results are treated as ambiguous or unsupported rather than proof. A fixture evidence unit fails if any named result has a wrong local exit code, including local `exit_code: N`, `exit_code=N`, or JSON-like `"exit_code": N` punctuation variants, even if another result has a correct exit. Known result-name occurrences that are followed by unsupported connector or suffix text fail the whole fixture evidence unit instead of being silently ignored. Fixture logs with duplicate result names are rejected before aggregate `expectations_match true` evidence is accepted, and aggregate fixture evidence is accepted only when expectations/results are internally consistent. Command-kind verify checks cannot use pure aggregate fixture evidence, source/docs/changelog mention-only evidence, fake `command`/`status` words added only to the evidence sentence, or unrelated artifact facts; they need fixture-bound command/result evidence or a cited workflow JSON artifact containing a same-record command/check/result tuple. Fixture-bound command evidence must name a fixture result whose command exactly matches the current `check.command` when `check.command` is present, and that named result must have a local positive exit/result claim. For command artifact fallback, every cited workflow JSON artifact must include a dict record with `command`/`commands`, a positive result such as `exit_code: 0` or pass-like `status`, and the claimed check-name tokens; if `check.command` is present, that same record must contain the exact normalized command. Nested failed result objects such as `result.exit_code: 1` and mixed unbound batched exit codes such as `exit_code: [1, 0]` fail closed unless the check or record explicitly marks expected failure/rejection. Nonzero exit codes or fail-like statuses are accepted only when the check/record has `expected_failure: true` or a dedicated `expected`/`expectation` field with an affirmative expected-failure phrase. Matching is token/phrase based, not substring based, so `unexpected failure`, `not expected to fail`, `no expected failure`, or `expected failure: false` do not create an expected-failure exception. Ordinary names or summaries containing `rejected`/`rejection` do not create that exception, and the evidence sentence alone is not enough either. Source/docs/changelog refs are never command-kind artifact fallback evidence. Existing workflow and source artifact content claims redact path references and optional citation suffixes such as `:23` or `#L23` before token matching; every referenced artifact must contain the claimed fact, so one cited file cannot launder a missing fact for another cited file. Path fragments such as `lane-runs` and line numbers do not become claim tokens. Source refs use the same skill-root mapping for shorthand `scripts/...`, `references/...`, and `agents/...` paths that auditable-ref existence checks use. Artifact content claims use word-token matching, not substring matching; meaningful two-character claim tokens such as `ux` are retained, `unfixed` does not prove `fixed`, `unsatisfied` does not prove `satisfied`, and explicit negation near the relevant positive claim token such as `not fixed`, `not satisfied`, `fix: false`, `ok: false`, `fixed: false`, `fixed: 0`, `fixed: null`, or `resolved: false` rejects that positive claim while unrelated falsey values do not negate another key and later unnegated affirmative occurrences may still satisfy affirmative claims. Bare filename extensions, command names, `--mode` tokens, standalone `exit=0`, ghost fixture names, mismatched exit codes, mismatched command claims, path-existence-only claims, short absent-content claims such as `bug fixed`, arbitrary absent short-token claims, or ephemeral `/tmp` paths are not enough. Finding resolutions are one-to-one, and a `blocked` resolution cannot be combined with a pass-like final stop reason.
- `final-report.md` must contain substantive sections for outcome, verification, risk, stop/gate, runner/workflow execution, and token usage. Its verification section must include concrete evidence lines, such as a `verify_workflow.py --mode final` result, command results, and runner or lane evidence like `runner-evidence.json` or `rounds/<round>/lane-runs/<lane>.json`. It must also reference the current passing verify lane and summarize real passing check names or criteria from that lane output. For current verify checks, mention both the check name and its corresponding evidence in the same bullet, table row, or structured evidence line; a separate check-name list plus an evidence cache is not enough. The token usage section must repeat `token-usage.json.total_tokens` and label the source/confidence. Blank lines break evidence units. Keyword-, anchor-, or vacuous-evidence stubs are not final evidence.
- If `runner-evidence.json` uses `evidence_level: "lead_recorded"`, final reports must say `lead-recorded` in every sentence that discusses native lifecycle, lifecycle records, `multi_agent_v1`, native agents, native subagents, subagent execution, agent execution, runner execution, or whether an agent/subagent ran. Those sentences may only claim recorded lifecycle fields/evidence/entries/ledger/records with verbs such as records, lists, stores, includes, contains, names, describes, or treats. They may mention scoped lifecycle metadata such as a run id only as recorded fields, not as proof that native execution ran. They must not imply the native lifecycle was independently or tool-event verified, ran, did run, has run, was run, is running, finished, returned, succeeded, produced output, generated output, emitted output, yielded output, produced a result, generated a result, output was generated, output was emitted, output was yielded, output was created, output was written, result was produced, result was generated, result was created, result was written, response was generated, ended, executed, completed, closed successfully, certified, confirmed, verified, validated, established, or proven. In addition, passive output/result/response overclaims are scanned across the whole final report, not only sentences with native lifecycle trigger words, and include plural objects plus alternate or adverb-split auxiliaries such as `outputs were created`, `output had been created`, `output has already been created`, `response would be generated`, and `response did actually get generated`; the `output_path` field name remains allowed. Hyphen and punctuation variants count as the same phrase, and `native lifecycle` is itself a runner-claim trigger. `tool_event_verified` remains reserved for a future durable runner-produced event ledger plus external runtime attestation verifier. In v1, a self-consistent event log with `trusted_capture`, `capture_source`, `transcript_hash`, or matching spawn/wait/close JSON is still not enough for final validation.

For v1 workspaces, a bare verifier call is not final evidence. Use an explicit
mode; `--require-lane-runs` is a compatibility alias for executed-style lane
output requirements.
