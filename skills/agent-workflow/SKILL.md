---
name: agent-workflow
description: |
  Orchestrate multi-agent work with planner-selected lanes, durable round state, independent review or challenge, verification gates, and repair when gates fail. Use when the user asks to run an agent team, subagents, or a gated multi-round loop, especially for broad or high-risk work. Use write-good-goal for paste-ready goal text; handle ordinary single-agent planning, review, and implementation directly.
---

# Agent Workflow

Use this skill as a harnessed agent-loop orchestration layer. It turns a broad goal into a planned agent team workflow with durable run state, lane contracts, independent review/challenge, verification gates, and multi-round iteration when the result is not good enough yet.

This skill does not rely on shelling out from one agent runtime to another. The first runner version supports only native subagent surfaces: Codex calls Codex's built-in multi-agent tools, and Claude Code calls Claude Code's built-in subagent/agent-team surface from inside Claude Code. Never have Codex invoke Claude CLI as a subagent runner, and never have Claude Code invoke Codex CLI as a subagent runner.

## Core Contract

- For new Codex native workflows, Main creates exactly one clean Orchestrator
  session with `fork_turns=none`; that Orchestrator is the workflow Lead and
  owns orchestration, integration, final writes, and final claims. Main does
  not fan out to worker lanes or receive nested worker events.
- The orchestrator is a plan compiler, not a normal worker lane. It decides the workflow shape before any lane runs, then revises the shape between rounds when verification fails.
- Use a planner-first approach. Templates may inform the plan, but the orchestrator must decide lanes, agent count, prompts, budgets, stop conditions, and gates from the current goal and evidence.
- Use the smallest harness that materially raises confidence. A chat-level workflow is enough for small tasks; create a `.workflow/<slug>/` run workspace for explicit agent-team workflows, multi-round work, broad ambiguity, high risk, or reusable coordination state.
- Intermediate lane outputs are JSON only. Human-readable Markdown belongs in `orchestration.md`, `integration.md`, `final-report.md`, or an explicit debug summary.
- If a lane returns invalid JSON, ask the same agent to repair the JSON once. If it is still invalid, mark the lane `invalid_output` and fail closed through integration or verification.
- Gate decisions use independent verifier/challenger confidence. Worker self-confidence may be recorded, but it cannot be the only reason to pass a round.
- Scripts scaffold, summarize, and validate run workspaces. They do not spawn subagents.
- For native subagent, agent-team, swarm, dynamic workflow, or multi-agent simulated workflows, show a compact left-rail Swarm Card before dispatch and on meaningful state transitions so the user can see the team shape without reading raw orchestration files. Label native, simulated, and lead-owned work honestly.
- Final workflow summaries must include exact, runtime-event-backed workflow token usage. New workspaces use token-usage v2 and must fail closed when native usage events are unavailable; never substitute a Lead or Goal counter estimate and label it exact. Existing token-usage v1 estimated workspaces remain readable for compatibility.

## Default Clean Orchestrator Runtime

New Codex native workspaces activate the Clean Orchestrator contract by
default. Clean orchestration and model routing remain separate contracts, but
both now activate by default for new Codex native workspaces. Main passes a
compact, digest-bound packet to one isolated Orchestrator. Nested lanes also use
isolated, digest-bound context and return compact receipts. Legacy Main-led
fan-out and wrapper polling are forbidden production fallbacks when this
contract is active.

The portable skill owns schemas, capability negotiation, admission arithmetic,
sealed semantic gates, completion-density budgets, deterministic controller
operations, exact-event accounting, validators, and honest runtime labels. It
does not implement or simulate host primitives. Atomic `run_orchestrator`, a
true all-terminal durable barrier, queued native dispatch, generation rotation,
automatic native session registration, and terminal host finalization remain
host-owned capabilities.

Before dispatch, read `Clean Orchestrator Runtime Artifacts` in
`references/workflow-artifacts.md`. A structured host snapshot must yield one
of these outcomes:

- `target`: all required host primitives and declared bounds pass;
- `bounded_interim`: clean context, direct terminal-event wait, subtree
  discovery, exact cumulative token events, and deadline/capacity/token/
  completion bounds pass, while missing target primitives remain explicit;
- unsupported: fail closed before spawn.

Unknown capability fields, a deadline above the native wait ceiling, fan-out
above available slots without host-owned queued dispatch, or worst-case token/
completion estimates above the declared maxima all reject dispatch. Do not add
another Coordinator agent to mimic all-terminal await.

## Native Runner Adapters

Choose exactly one runner adapter for a run workspace:

| Runner mode | Use when | Dispatch rule |
| --- | --- | --- |
| `codex_builtin_subagents` | The skill is running inside Codex and the `multi_agent_v1` tools are available. | The lead agent uses Codex `spawn_agent`, `send_input`, `wait_agent`, and `close_agent` directly. |
| `claude_code_builtin_subagents` | The skill is running inside Claude Code. | Claude Code uses its own Agent/subagent or agent-team tools inside that same Claude Code session. |
| `manual_simulation` | No native subagent surface is available, or the user did not authorize subagents. | The lead simulates lanes sequentially and must say no subagents ran. |

Runner rules:

- Do not use CLI cross-calls as a runner adapter.
- Do not represent CE/MAT skills, shell scripts, or future SDK work as runner modes. They may inform lane prompts or review style, but they are not the subagent harness in v1.
- Prefer `discover`/`seam` lanes as read-only workers and isolate write ownership for `implement`/`repair`.
- Close or finish native subagents when their lane result has been integrated.

## Runtime Contracts

### Exact Token Accounting

New workspaces use fail-closed `agent-workflow.token-usage.v2` and leave its accounting boundary unstarted while static workspace work is prepared. Before the first dispatch, read the `token-usage.json` section of `references/workflow-artifacts.md`, finish the plan, orchestration, report template, static evidence, runner skeleton, and `accounting-start.json`, then call `workflow_controller.py start <workflow-dir>` once. A fresh scaffold intentionally has no `token-evidence.json`; the compound start creates it from the native snapshot and must not require a fake placeholder. That operation prepares digest-bound dispatch, validates planned mode, renders the card when present, captures the Lead snapshot only after those static steps, registers every declared attempt, and emits one typed receipt. Its `original_ledger_state` binds each pre-transaction ledger as `present: false` or `present: true` plus the exact SHA-256. The two-ledger commit restores that recorded state on replacement failure, including removing only evidence newly created by the failed transaction. Early Codex `token_count.info = null` events are incomplete snapshots; continue to the last complete cumulative snapshot and fail if none exists. Register every descendant attempt and reconcile the discovered runtime subtree before finalization. Reused pre-boundary Codex identities require both a raw start snapshot and membership in the current Lead's complete raw parent lineage; unrelated reuse still fails closed. During collect, accept the declared author's anchored `Message Type: FINAL_ANSWER` after the unique follow-up and before the next non-collaboration action, whether it appears immediately before or after the non-timeout wait output; stale, wrong-author, nonterminal, and wait-output-only evidence still fail closed. Perform no workflow work after the terminal controller finalization operation.
Never label Lead counters, estimates, or incomplete runtime events as exact; final reports describe runtime-session-event deltas as lead-recorded provenance, not independent billing attestation.
Clean Orchestrator final mode also requires `runtime_harness.py` to reopen the
raw Lead JSONL and derive every in-boundary completion, input context, class,
round density, and forbidden wake count. `runner-evidence.json` is only the
digest-bound projection of that replay; hand-written counts cannot pass.

### Default Native Execution Efficiency

New Codex and Claude Code native workflows enable execution efficiency automatically; manual simulation and existing workspaces remain unchanged. Read `Default Native Execution-Efficiency Artifacts` in `references/workflow-artifacts.md` and follow lane admission, isolated context, digest-bound dispatch, notification-first waits, receipt transport, budgets, identity independence, and planned/executed/final validation. Use `--execution-efficiency off` only as an explicit compatibility rollback; no artifact migration is required.

For active Clean Orchestrator workspaces, execution efficiency is subordinate
to the sealed round contract. Every round declares its purpose, lanes,
`semantic_return_gate`, compound operation, deterministic steps, and absolute
completion budget before dispatch. Target rounds plan one result reactivation
and zero housekeeping, status-only, wrapper-wait, partial-terminal,
deterministic-result, or sibling-terminal wakes. Bounded interim records every
native sibling-terminal and deterministic-result reactivation separately and
cannot present those exceptions as target behavior.

### Default Codex Model Routing

New `codex_builtin_subagents` workflows enable responsibility-based model routing automatically. Supply `--runtime-capabilities <path>` and `--reasoning-effort <value>` from current host/session evidence; if either is unavailable, fail closed before scaffold or ask once for the session effort instead of silently disabling routing or inferring it from the model or task. Use `--model-routing off` only as an explicit compatibility rollback. Other runners remain unchanged. The effort is the user's session-wide choice, is locked once per workflow, and must be inherited unchanged by every routed lane. The router chooses only the model: Sol for planning, judgment, ambiguity, cross-boundary or high-risk work; Terra for bounded execution and repair packets. Read `Default Model Routing Artifacts` in `references/workflow-artifacts.md` before planning. Planned decisions become immutable after attempts start, and required verifiers must satisfy claim-derived model, evidence, and identity gates.
Availability, identities, attempts, and actual routes are lead-recorded evidence in v1, not runtime attestation. Do not dispatch until `verify_workflow.py --mode planned` passes.

### Swarm Card Display

For native or multi-agent-shaped workflows, emit the CJK-safe Markdown left-rail card before dispatch and only on meaningful state transitions. Keep explicit status text beside every symbol, show only the model in parentheses, and keep simulated or Lead ownership honest. Reasoning effort remains in durable routing evidence but is intentionally hidden from the card. Read `Swarm Card Display State` in `references/workflow-artifacts.md`; the card is display state, never runner evidence.

## Phase 0 - Sync Context

Before orchestrating, gather enough ground truth to avoid routing fantasy:

1. Read active workspace instructions and any project-local context they require.
2. Check `git status --short --untracked-files=all` for the relevant path and keep unrelated dirty files out of the work.
3. Search local plans, architecture docs, prior decisions, tests, and existing patterns with `rg`.
4. For code, syntax, command, API, or tool behavior, check official docs, local docs, man pages, or source before changing it.
5. Respect workspace policy over generic agent-loop advice, including its rules for branches, worktrees, sensitive data, tools, and external actions.

Ask one focused question only when the missing answer would change the workflow shape, touched files, risk boundary, or external action. Otherwise proceed with explicit assumptions.

## Phase 1 - Orchestrate First

Start by compiling an orchestration plan before launching workers.

The orchestrator decides:

- goal, success criteria, constraints, and non-goals
- whether to create a run workspace
- round budget and stop conditions
- enabled lanes and why each lane is needed
- agent count per lane and whether runs are parallel, sequential, or simulated
- each lane's prompt, input references, expected JSON payload, and gate
- approval gates for risky, external, destructive, or expensive actions
- verification strategy and what evidence is enough to stop

When a run workspace is active, write both:

- `orchestration.md`: concise human-readable explanation of the shape and reasoning
- `orchestration.json`: machine-readable contract for scripts and future runners

For native subagent or multi-agent workflows, emit the `PREVIEW` Swarm Card after these files are written and before the first dispatch.

Read `references/workflow-artifacts.md` before writing or validating run workspace files.

For a new Codex native run, first compile the compact Main-to-Orchestrator
packet and the structured capability/admission contract. Do not dispatch any
lane until the round's semantic gate graph and completion budget are
digest-sealed and `verify_workflow.py --mode planned` passes.

Use the other references only when their branch is active:

- Read `references/reviewer-prompts.md` when native subagents or main-thread simulations need lane prompts.
- Read `references/risk-gates.md` before risky, external, destructive, expensive, or permission-sensitive steps.
- Read `references/validation-examples.md` when testing or revising this skill.
- Read `references/quality-patterns.md` when shaping discovery-shaped goals (bug hunts, audits, research sweeps), high-stakes claims, resume-after-interruption economics, or wall-clock/cost-sensitive rounds.

## Phase 2 - Choose Lanes

Built-in lanes:

| Lane | Use |
| --- | --- |
| `discover` | Map current state, constraints, sources, risks, and unknowns. |
| `plan` | Produce a concrete approach, decomposition, or spec path. |
| `roundtable` | Run multi-perspective reasoning when the problem has real tension, low confidence, or competing frames. |
| `implement` | Make bounded changes or produce the requested artifact. |
| `seam` | Inspect interfaces, ownership boundaries, integration points, adapters, and hidden coupling. |
| `review` | Find correctness, quality, scope, test, or policy issues. |
| `challenge` | Adversarially attack assumptions, missing evidence, and premature consensus. |
| `verify` | Check outputs against success criteria with tests, evidence, source checks, or expert judgment. |
| `repair` | Apply a targeted follow-up packet from verify/review/challenge failures. |
| `custom` | A user- or task-specific lane with explicit purpose, input, output schema, and gate. |

Only enable lanes that matter for this run. A good small implementation workflow may use `discover -> implement -> review -> verify`; a spec workflow may use `discover -> roundtable -> plan -> challenge -> verify`; a failed round may open only `repair -> verify`.

Calibrate breadth and verification depth to the request tier, dedup overlapping findings before dispatching verification, and escalate `P0`/`P1`-class claims to a multi-lane verification panel with default-refute prompts instead of a single verify identity. Lanes that cap or sample their own output must say so, and integration must surface every reported cap. See `references/quality-patterns.md`.

### Roundtable Lane

Use `roundtable` as a reusable reasoning lane before planning, after review or verify failure, or whenever structured disagreement matters. Build a tension network, require each participant to declare a type, and return disagreements, options, open questions, and recommended next lanes. Read the roundtable contract in `references/reviewer-prompts.md`.

## Phase 3 - Run Rounds

A round is one orchestration cycle:

1. The clean Orchestrator writes or updates the round plan and seals its
   semantic gate graph before dispatch.
2. Enabled lanes run with self-contained, digest-bound prompts and JSON output contracts. When execution efficiency is enabled, native lanes use isolated context and write their full result to the declared artifact path.
3. The lead validates lane JSON and requests one JSON repair if needed.
4. Integration summarizes findings, conflicts, accepted work, rejected work, and repair packets.
5. Independent `verify` and/or `challenge` decides whether the round passes, needs repair, needs more discovery/roundtable, is blocked, or requires a human gate.
6. Compare planned completion density with the runtime-event ledger. Target
   rounds fail on any forbidden or excess wake. Bounded interim rounds type
   every native exception and still enforce their absolute admission bounds.

For routed lanes, the lead appends each dispatch result to the lane's attempt ledger before deciding on retry, fallback, escalation, repair, or a human gate. The terminal actual route comes from the final completed attempt and must equal the dispatched route; silent model substitution or any lane-specific effort change fails validation.

Use actual subagents only through the selected native runner adapter. If the run is in Codex, use Codex's multi-agent tools directly. If the run is in Claude Code, use Claude Code's built-in subagent/agent-team surface directly. If neither native surface is available or authorized, simulate lanes sequentially in the main thread and keep their JSON outputs separate before integrating.

Prefer read/review/challenge agents over parallel writers. If multiple agents write, isolate ownership and have the lead inspect, integrate, and verify before accepting the changes.

When card display is active, update the Swarm Card only on meaningful transitions: first dispatch, phase status change, material agent failure/blocker, integration gate, round transition, and final stop. Avoid periodic status completions and heartbeat redraws; a native long wait is not a reason to spend another Orchestrator turn. Target mode returns once at all-terminal; bounded interim uses direct event-driven terminal waits only, records each sibling return, and never uses wrappers or status polling.

Integration is not a worker lane in v1. The lead agent writes the authoritative
`integration.json` and `integration.md` ledgers after reading lane outputs.
Legacy workspaces may contain advisory `integrate` lane outputs, but new
workspaces should not scaffold or rely on an `integrate` worker lane.

## Phase 4 - Gate And Iterate

Gate on severity plus independent confidence:

- `P0` / `P1`: must repair or escalate before passing.
- `P2`: repair when budget, scope, or risk justifies it; otherwise explicitly defer.
- `P3`: record without blocking.
- Low verifier/challenger confidence: open `more_discovery`, `challenge`, `roundtable`, `second_opinion`, or `human_gate`.

Useful gate decisions:

- `pass`: success criteria met with enough evidence.
- `revise`: actionable failures exist; open a targeted `repair` lane in the next round.
- `more_discovery`: missing facts block a responsible decision.
- `challenge`: assumptions need adversarial review before work continues.
- `second_opinion`: independent judgment is needed for fuzzy spec, strategy, or design quality.
- `human_gate`: the decision is subjective, risky, external, or outside agent authority.
- `blocked`: no meaningful progress is possible without new input or external state.

Open a new round only when the previous round produced actionable repair work, missing evidence, low-confidence judgment, or unresolved high-severity findings. Stop when verify passes, the remaining findings are explicitly deferred, a human gate is reached, the round budget is exhausted, or the workflow is blocked. For discovery-shaped goals, also stop on convergence: dedup each round's findings against the full seen-findings ledger (including refuted ones) and stop after consecutive dry rounds per `references/quality-patterns.md`.

## Phase 5 - Integrate And Report

After lanes complete, synthesize decisions instead of dumping transcripts:

Record accepted and rejected work, conflicts, repair packets, verification evidence, remaining risks, and the next-round or stop reason.

In a run workspace, write round decisions to
`rounds/<round-id>/integration.json` and
`rounds/<round-id>/integration.md`, final results to `final-report.md`, workflow
token accounting to `token-usage.json`, and native event snapshot evidence to
`token-evidence.json`.
The final report and final user response must include `total_tokens`, source, and confidence.

If card display was active, the final response should include the final Swarm Card status or a concise equivalent summary: completed, paused, blocked, or human-gated; round count; gate result; unresolved P2+ count; and any P3 follow-up.
Every final response for a run workspace should include `Workflow tokens: <total> (runtime_session_events, exact; excludes accounting finalizer and final response)`.

Before the final answer:

1. Re-check the diff and confirm only intended files changed.
2. Run the strongest practical validation for the actual change.
3. Use `scripts/verify_workflow.py --mode scaffold|executed|final` for run workspace validation when run-workspace mode is active. Final claims require `--mode final`; a bare verifier call is not final evidence. Final mode requires terminal state consistency, verification evidence, substantive final report content, and typed `P2+` finding resolutions.
4. Say what actually ran, what was simulated, which gates passed or remained open, and what risk remains.

Prepare the complete terminal report and response before exact accounting.
For Clean Orchestrator runs, also prepare every static workspace artifact before
the accounting boundary and use `workflow_controller.py start <workflow-dir>`
as the single pre-dispatch tool operation. Its internal subprocesses are
deterministic program work, not model completions, and it does not spawn, wait,
join, queue, rotate, or finalize native agents.
When the portable controller is available, make
`workflow_controller.py finalize <workflow-dir>` the last tool operation: it
runs raw completion replay, the final executed gate, exact runtime-session
accounting, runtime-observation reconciliation, binds the exact token markers
into the prepared report, and performs final workflow validation in one compound
staging transaction, writes the five projections with per-file atomic replacement,
then commits a digest-bound revision manifest last. Final validation rejects a
missing or mixed manifest revision. This is source-owned transaction ordering,
not bundle or host-terminal atomicity; the finalizer completion and final
user-facing response remain outside the sealed token boundary.

Stop when the user's requested work is handled, high-severity findings are resolved or explicitly deferred, and the final answer reflects verified evidence rather than orchestration theater.
