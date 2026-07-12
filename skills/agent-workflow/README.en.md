# Agent Workflow

[繁體中文](./README.md) | English

Give Agent Workflow a goal that needs multiple agents, independent quality
checks, or multi-round repair. New native Codex runs do not fan workers out from
Main. Main creates one clean `fork_turns=none` Orchestrator; that Orchestrator
selects the team, lanes, sealed gates, completion budgets, and stop conditions
before coordinating nested workers, integration, and quality lanes.

When the runtime exposes native subagents and capability admission passes, the
Orchestrator creates a real agent team. Missing target primitives permit only an
explicitly bounded `bounded_interim` path, or fail closed before spawn. Without
native authority, the Lead can only run an explicitly labeled sequential
simulation and must not claim that subagents ran.

## Agent Workflow vNext candidate

vNext starts only when the user explicitly requests Agent Workflow. Main creates
one clean Orchestrator, which plans dynamic phases from a sealed brief while the
deterministic runtime coordinates external routed workers. Those workers produce
auditable routing, result, and receipt artifacts, but they are not the Codex native sub-agent tree
and therefore do not appear in the native sub-agent hierarchy.

The `.workflow/<slug>/` file `view.json` is a derived human and UI projection. Immutable,
digest-bound artifacts remain authoritative. See the
[vNext runtime reference](./references/vnext-runtime-reference.md) for mechanics.

**Legacy/native 0.x boundary**: the lanes, rounds, Swarm Card, workspace tree,
Runner Modes, and native runtime protections below describe pre-cutover 0.x. The
vNext external runner launches routed workers; legacy scripts do not spawn agents.
vNext does not use the legacy Swarm Card; its only guaranteed UI is `view.json`
rebuilt from artifacts.

## When To Use It

Use Agent Workflow when:

- the user explicitly requests an agent workflow, agent team, swarm, or agent loop;
- the task needs multi-round repair and verification;
- a specification, research, or strategy problem needs structured disagreement;
- cross-module implementation needs seam review and independent quality gates; or
- the team needs resumable and auditable collaboration artifacts.

Do not use it for:

- a small change that one agent can complete directly;
- an ordinary plan, review, explanation, or paste-ready goal-text request;
- the bare word "workflow" without multi-agent intent; or
- a background scheduler, queue, or unattended daemon.

Use the smallest harness that materially raises confidence. Agent count is not
a goal by itself.

## Get Started

Requires Git, Bash, Python 3, and `rsync`. After cloning this repository, run
from its root; see [repository Install](../../README.md#install) for the complete
installation path:

```bash
bash scripts/install-skill.sh agent-workflow \
  --target-root "${CODEX_HOME:-$HOME/.codex}/skills" \
  --execute
```

Then make the workflow intent explicit in a task:

```text
Use $agent-workflow to review this change, repair any P2+ findings,
and iterate until independent verification passes.
```

## What It Solves

Basic subagent dispatch often stops at "produce several answers in parallel and
let the main agent stitch them together." Agent Workflow adds several important
constraints:

- **Orchestrate before dispatch**: decide lanes, agent count, prompts, budgets,
  dependencies, gates, and stop conditions first.
- **Persist workflow state**: agents and rounds share contracts, outputs,
  evidence, and decisions under `.workflow/<slug>/`.
- **Prevent self-approval**: a writer cannot pass its own work using
  self-confidence alone. Native runs use independent agent identities;
  simulations must label role separation and execution limits clearly.
- **Feed failures into the next round**: verification can create a bounded repair
  packet and open a new `repair -> verify` round.
- **Make completion auditable**: the final gate requires evidence, finding
  resolution, and terminal agent lifecycle. Exact token accounting accepts only
  complete runtime event evidence and is labeled Lead-recorded provenance.

## How It Works

```mermaid
flowchart TD
    Goal["Goal and done criteria"] --> Main["Main Agent"]
    Main --> Clean["Single Clean Orchestrator"]
    Clean --> Lanes["Select nested lanes dynamically"]
    Lanes --> Compile["Compile sealed workflow plan"]
    Compile --> Controller["Validate / collect / render / accounting"]
    Controller --> Work["Discover / Plan / Implement / Roundtable / Seam"]
    Work --> Integrate["Lead integration"]
    Integrate --> Assess["Review / Challenge / Verify"]
    Assess -->|"Pass"| Report["Final report + exact tokens"]
    Assess -->|"Repairable"| Repair["Bounded repair packet"]
    Repair --> Integrate
    Assess -->|"Missing evidence or judgment"| Gate["More discovery / Human gate / Blocked"]
```

The clean Orchestrator is the workflow Lead and owns orchestration, integration,
final writes, and final claims. Main does not manage workers or receive nested
worker events.
Integration is not a separate worker lane in v1, so the final responsibility
does not drift to another agent without the full picture.

## Dynamic Team Composition

The orchestrator does not enable every lane by default. It selects the smallest
team that is sufficient for the task's risk, ambiguity, and verification needs.

| Lane | Primary responsibility |
| --- | --- |
| `discover` | Map current state, constraints, evidence, risks, and unknowns |
| `plan` | Produce an executable decomposition, spec, or implementation path |
| `roundtable` | Build a tension network across competing perspectives |
| `implement` | Make changes within explicit ownership and write scope |
| `seam` | Inspect cross-module interfaces, ownership boundaries, and hidden coupling |
| `review` | Find correctness, scope, quality, and test problems |
| `challenge` | Adversarially attack assumptions, evidence gaps, and premature consensus |
| `verify` | Decide pass or fail using tests, sources, evidence, or expert judgment |
| `repair` | Execute a bounded repair packet produced by an earlier round |

Common workflow shapes:

```text
Small implementation: discover -> implement -> review -> verify
Specification:        discover -> roundtable -> plan -> challenge -> verify
Repair round:         repair -> verify
```

## Swarm Card

This section applies only to legacy/native 0.x; vNext does not use the legacy Swarm Card.
The Swarm Card is an event-driven status surface shown by the Lead Agent. It
uses a Markdown left rail instead of a fixed-width ASCII box, so mixed-width
English, Chinese, symbols, and font fallbacks do not break the layout.

### Preview

> **Agent Workflow · PREVIEW**
> `api-contract-hardening` · Round 1/3 · 0/5 complete · Codex native
> Tokens: measuring
>
> Fix API contract false-passes until no unresolved P2+ finding remains.
>
> **Discover**
> □ not started · `discover-01` · current-state explorer *(Terra)*
>
> **Implement & Repair**
> □ not started · `implement-01` · bounded writer *(Terra)*
>
> **Review & Challenge**
> □ not started · `review-01` · independent reviewer *(Sol)*
> □ not started · `challenge-01` · adversarial challenger *(Sol)*
>
> **Verify**
> □ not started · `verify-01` · evidence gate *(Sol)*
>
> **Gate** Pending · Open P2+: 0

### Verification Opens A Second Round

> **Agent Workflow · RUNNING**
> `api-contract-hardening` · Round 2/3 · 5/7 complete · Codex native
> Tokens: measuring
>
> Round 1 found a validator false-pass and opened a targeted repair packet.
>
> **Discover**
> ■ complete · `discover-01` · current-state explorer *(Terra)*
>
> **Implement & Repair**
> ■ complete · `implement-01` · bounded writer *(Terra)*
> ◐ running · `repair-01` · validator repair *(Terra)*
>
> **Review & Challenge**
> ■ complete · `review-01` · independent reviewer *(Sol)*
> ■ complete · `challenge-01` · adversarial challenger *(Sol)*
>
> **Verify**
> △ waiting: repair output · `verify-02` · regression gate *(Sol)*
>
> **Gate** Revise · Open P2+: 1

Symbols are scanning aids; the adjacent text is the authoritative label:

```text
□ not started   ◐ running   △ waiting   ■ complete
- skipped       ! blocked   × failed
```

The card displays the model only. The user-selected reasoning effort remains in
durable routing evidence but is intentionally hidden from the card. The card is
also display state, not runner evidence; it cannot prove that a native subagent
actually ran.

## Persistent Workflow Workspace

The following tree is the legacy/native 0.x format; vNext uses immutable Phase/Task
artifacts and derived `view.json`. For multi-round work, collaboration, or resumable
state, the legacy Lead Agent creates:

```text
.workflow/<slug>/
├── plan.md
├── state.json
├── orchestration.md
├── orchestration.json
├── runner-evidence.json
├── swarm-card.json
├── token-usage.json
├── token-evidence.json
├── rounds/
│   └── round-001/
│       ├── lane-runs/
│       ├── receipts/          # optional efficiency artifacts
│       ├── integration.json
│       └── integration.md
└── final-report.md
```

Lane outputs use JSON contracts so later agents, rounds, and validators can read
the same durable state. Human-readable reasoning and outcomes live in the
orchestration, integration, and final report documents.

## Runner Modes

This section describes legacy/native 0.x only. The vNext external runner launches routed workers;
legacy scripts do not spawn agents.

| Mode | Behavior |
| --- | --- |
| `codex_builtin_subagents` | A Codex Lead uses the native multi-agent tools |
| `claude_code_builtin_subagents` | A Claude Code Lead uses the native subagent or agent-team surface |
| `manual_simulation` | The Lead simulates lanes sequentially and explicitly states that no subagent ran |

Codex does not shell out to Claude Code, and Claude Code does not shell out to
Codex. Legacy scripts handle scaffolding, digests, receipts, rendering, and validation;
they do not spawn agents.

## Runtime Protections and Optional Hardening

The protections and modes below describe legacy/native 0.x, not the vNext external runner contract.

- **Execution efficiency (native default)**: Codex and Claude Code native workflows
  automatically use isolated lane context, digest-bound dispatch, notification-first
  waits, compact receipts, budgets, and independent identities; `off` is only an
  explicit rollback.
- **Clean Orchestrator (native Codex default)**: Main has one clean child, and
  every round seals its semantic gate graph, compound operation, and absolute
  completion budget before dispatch. `target` requires the atomic outer
  primitive, a true all-terminal barrier, and terminal host finalization;
  missing capabilities may use only an admission-bounded `bounded_interim`,
  never Main-led fan-out or wrapper polling.
- **Portable controller**: `workflow_controller.py` compounds prepare, collect,
  render, validate, and exact-accounting housekeeping into typed receipts. It
  does not spawn, join, queue, or rotate native agents and does not claim host
  atomicity.
- **Codex model routing v2 (native mandatory)**: every new Codex native workflow enables
  routing automatically. Sol handles planning, judgment, review, challenge,
  verification, and high-risk work; Terra handles bounded execution. Missing
  host `model`/`thinking` control or runtime session evidence fails closed before
  dispatch and cannot be bypassed with `off`. The Lead derives capabilities from
  the host tool schema, the current Codex effort is inherited automatically, and
  raw replay binds each spawn to its ordered attempt and verifies the child
  session's actual model and effort.
- **Exact token accounting**: computes Lead and registered-attempt usage from
  native runtime session events and stores Lead-recorded provenance bound to the
  event evidence. Missing evidence fails closed instead of being replaced by an
  estimate labeled exact.
- **Raw completion replay**: `runtime_harness.py` reopens the sealed runtime
  JSONL prefix and derives each input context, completion class, round density,
  and forbidden wake count. `runner-evidence.json` is only a digest-bound
  projection and cannot override raw truth.

## Detailed Specifications

- [Skill contract](./SKILL.md)
- [Workflow artifacts](./references/workflow-artifacts.md)
- [Lane prompts](./references/reviewer-prompts.md)
- [Risk gates](./references/risk-gates.md)
- [Quality patterns](./references/quality-patterns.md)
- [Validation examples](./references/validation-examples.md)

## Boundary

Agent Workflow is not an unattended runner daemon. Portable source does not
provide atomic `run_orchestrator`, a durable all-terminal barrier, a native
queue, generation rotation, terminal host finalization, a background scheduler,
a database, a cross-runtime CLI bridge, or independent provider attestation.
Those capabilities require host evidence; otherwise the run is explicitly
`bounded_interim` or rejected.
