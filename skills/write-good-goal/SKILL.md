---
name: write-good-goal
description: Use this skill when the user wants to write, refine, or audit a Codex goal, Claude Code goal, goal-mode prompt, multi-round agent task, agent loop, dynamic workflow, or long-running project objective. Produce concise goal text with feasibility checks, boundaries, auditable done criteria, progress selection, minimal human gates, and round reporting.
---

# Write Good Goal

Create goal text that another coding agent can use directly. Optimize for a
goal that can actually progress inside the goal run. Prefer shaping the goal so
it finishes cleanly over writing a goal that predictably pauses.

Hard length limit: the final paste-ready goal text, including any feasibility
warning, must be at most 4000 Unicode characters unless the user explicitly
gives a different limit. If the draft is over budget, compress it before
responding; do not ask the user to trim it later.

## Workflow

1. Extract the contract inputs: objective, source and boundary, done criteria,
   approvals, human judgment points, and any cost or compute constraints.
2. Classify each done criterion before drafting:
   - Agent-achievable: the agent can produce or verify it within normal rounds.
   - External evidence: it depends on elapsed time, future data, scheduled runs,
     third-party state, or long-running observation.
   - Human decision: deterministic verification cannot decide it, or policy or
     product judgment is required.
   - Approval: the user must explicitly allow an irreversible action or scope
     change.
3. Do a feasibility check before drafting:
   - If the main objective cannot complete inside the goal run because it needs
     wall-clock waiting, future observations, external-state changes, or a
     product or policy decision, warn the user and propose a tighter
     agent-achievable goal.
   - If the external requirement is only a follow-up acceptance gate, move it
     out of `Done` and into `Follow-Up Gate` so the goal can complete without
     pretending the future evidence exists.
   - If the external requirement is central and cannot be scoped out, add a
     `Risk / Pause Plan` and say the goal is expected to pause.
4. If a criterion is not agent-achievable in the current run, put it in
   `Follow-Up Gate`, `Human Gate`, or `Risk / Pause Plan`, or ask the user to
   rescope it. Do not bury it inside normal `Done`.
5. Ask at most three gap questions only when missing information would
   materially change the goal. Otherwise use a safe assumption and mark it.
6. Draft only the goal text by default. Add a short feasibility warning before
   the goal only when it needs a follow-up gate, human decision, or pause plan.
7. Self-check before responding:
   - Done criteria can be judged as pass, partial, paused, or blocked.
   - Main Done criteria are agent-achievable unless the user explicitly accepts
     an expected pause.
   - Progress selection compares next moves before each round.
   - Elapsed-time or external-evidence requirements are scoped out, moved to a
     follow-up gate, or given a pause and resume trigger.
   - Human gates are limited to semantic judgment or explicit approval.
   - Capability-only work has a five-round budget.
   - The output is directly pasteable into Codex or Claude Code.
   - The final goal text stays within the active character limit.

## Goal Shape

Write in the user's language. Use this structure unless the context clearly
requires a smaller version. Under the default 4000-character limit, preserve
`Goal`, `Boundary`, `Done`, `Loop`, `Human Gate`, and `Round Report`; include
optional sections only when needed.

If feasibility risk exists, put this before the goal:

```text
Feasibility Warning:
[The requested done condition depends on external evidence, elapsed time, or a human decision. I scoped the goal to the agent-achievable slice and put the external condition in Follow-Up Gate. To keep it in Done, the user must explicitly accept a pausing goal.]
```

```text
Goal:
[One-sentence objective.]

Boundary:
- Use: [...]
- Do not use: [...]
- Approval needed: [...]

Done:
- [Criterion 1; define pass, partial, or blocked when useful.]
- [Criterion 2.]
- [Criterion 3.]

Follow-Up Gate:
- [Optional. Acceptance evidence that cannot exist during the goal run, such as future observations, scheduled data, external review, or a later human decision.]

Loop:
Before each round, compare up to three next moves by expected increment, expected state change, and cost or risk. Choose the move with the best expected progress toward Done.
Valid increments:
- Outcome: the target artifact or result improves.
- Evidence: trustworthy evidence increases.
- Capability: a named blocker is removed.
Prefer Outcome or Evidence over Capability when similarly feasible. Do not run more than five consecutive Capability-only rounds.
After each round, check whether the next required state change depends on external evidence, elapsed time, or human judgment. If yes, stop at the pause point instead of inventing adjacent work.

Risk / Pause Plan:
[Optional. Include only when the goal is expected to pause or has a meaningful pause risk.]
Pause when the next required progress depends on external evidence, elapsed time, human decision, or explicit approval and no remaining agent-achievable increment would materially advance Done.
When pausing, report:
- current proven state
- missing trigger or evidence
- exact resume condition
- optional monitor or scheduled check, if supported

Human Gate:
Ask for human input only when deterministic verification cannot decide correctness or explicit approval is required.

Round Report:
For each round report:
- selected move
- increment type
- expected state change
- actual state change
- verification
- next decision
```

## Rules

- Do not turn the goal into a full project plan.
- Do not exceed the active character limit. Compress boundaries, merge related
  bullets, and omit optional sections first.
- Do not put wall-clock waiting, future observations, external-state changes,
  or later human decisions in main `Done` unless the user explicitly wants a
  pausing goal.
- Prefer completing the agent-achievable slice and recording external
  acceptance as `Follow-Up Gate`.
- A paused goal is not complete. It has a proven state, a missing trigger, and a
  concrete resume condition.
- Do not enumerate every bad direction. Select next-round candidates by
  expected progress.
- Do not count process surface as progress unless it removes a named blocker.
- Prefer observable state changes over vague quality language.
- When the blocker is human input, approval, elapsed time, or external evidence,
  state it instead of inventing more preparation.
- One narrow blocker-removal round may create a real collection, monitoring, or
  verification mechanism. After that, pause until the missing evidence exists.
