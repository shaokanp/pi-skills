---
name: agent-workflow
description: |
  Run a real native agent team when the user explicitly asks for Agent Workflow, an agent team, a swarm, parallel agents, multi-agent work, adversarial review, or fresh-context verification. Dynamically split independent work for speed, coordinate evidence-based challenge and review for quality, use fresh agents for verification, and repair only falsified claims. Use native collaboration tools only for agent lifecycle; never launch or manage agents through CLI runners.
compatibility: Requires native agent collaboration tools such as spawn_agent, send_message, followup_task, wait_agent, and interrupt_agent.
---

# Agent Workflow

Run a team, not a ceremony. The agent that loads this skill is the Orchestrator.

## Native boundary

Use the host's native agent tools for every agent lifecycle action:

- `spawn_agent` creates a fresh specialist.
- `send_message` delivers a compact challenge or dependency update.
- `followup_task` returns a completed owner to one bounded repair.
- `wait_agent` waits for mailbox or terminal updates.
- `interrupt_agent` stops work that is obsolete, unsafe, or outside authority.

Never use external model CLIs, App Server clients, shell background jobs, or
generated process supervisors to launch, resume, wait for, or coordinate agents.
Ordinary task commands such as tests, builds, linters, and data scripts remain
allowed when they directly prove the requested outcome.

If native agent tools are unavailable, say Agent Workflow is unsupported in the
current host. Do not simulate a team or silently fall back to single-agent work.

## 1. Frame the mission

Before dispatch, reduce the request to a compact Mission Brief:

- outcome: what must exist or be decided;
- proof: observable checks that would make the result trustworthy;
- constraints: scope, safety, time, compatibility, and user decisions;
- context: only the files, sources, facts, and paths specialists need;
- authority: what may change now and which actions still need approval.

Do not copy the conversation transcript into child prompts. Each agent gets a
self-contained packet and `fork_turns="none"` so its judgment starts fresh.

## 2. Design the smallest high-value team

Create a task only when it has a distinct question, owned outcome, or independent
error-detection value. Duplicate agents create consensus noise, not quality.

Choose roles from the work instead of using a fixed lane checklist:

- Explorer: finds facts, constraints, seams, or competing explanations.
- Builder: owns a concrete artifact or bounded code change.
- Challenger: tries to falsify claims with counterexamples and missing evidence.
- Reviewer: inspects correctness, interfaces, tests, and unintended effects.
- Verifier: starts fresh and decides whether the final outcome is actually proven.

Explicit invocation means use a real team. The minimum meaningful shape is one
owner plus one independent fresh quality lens. For source changes that lens is a
Verifier; for research or decisions it may be an Explorer, Challenger, or Judge.
Add agents only when parallel work or an additional review lens can change the
result.

## 3. Parallelize for elapsed time

Launch all ready, independent tasks without waiting between them. Parallelize
research, inspection, test design, alternatives, and review lenses aggressively.

Parallel source changes require clearly disjoint ownership. Two writers are safe
only when their paths and interfaces do not overlap and neither depends on the
other's unfinished state. If ownership is uncertain, use one writer and parallel
read-only support. Never let multiple agents edit the same file or semantic seam.

Keep the Orchestrator focused on decomposition, integration, and decisions. Do
not duplicate a worker's investigation in the parent while that worker is active.

## 4. Give each agent a complete packet

Every assignment states:

1. the owned outcome and why it is separate;
2. exact context, inputs, and relevant paths;
3. read/write ownership and forbidden overlap;
4. proof or acceptance checks;
5. the compact terminal deliverable;
6. a stop condition and material blockers;
7. that the agent must not delegate or run another Agent Workflow unless it was
   explicitly appointed as a sub-orchestrator for an independent subtree.

Ask for conclusions, evidence, changed paths, checks, uncertainties, and the next
recommended action. Do not request progress chatter or full transcripts.

## 5. Collaborate through evidence

Agents think independently before seeing sibling conclusions. After independent
work exists, use communication only when it can change a decision.

Use this compact challenge shape:

```text
CLAIM: the conclusion being challenged
EVIDENCE: source, test, diff, or reproducible observation
RISK: concrete failure mode or counterexample
REQUEST: exact question, repair, or proof needed
```

Route material findings to the original owner with `send_message` while active or
`followup_task` when idle. The owner either fixes the issue or rebuts it with new
evidence. Agreement without evidence does not close a finding.

Do not create open-ended debates. One owner response is the default for a finding;
another attempt requires materially new evidence or a changed assumption.

## 6. Prove with fresh context

Writer self-checks are useful evidence but never final approval. After integrating
a source change, spawn a fresh verifier with `fork_turns="none"`. Give it the
Mission Brief, final artifact or diff, relevant source, and test evidence—but not
the builder transcript, confidence, or preferred verdict.

For source changes, the verifier is read-only and must check:

- every success criterion;
- correctness and regression risk;
- tests or deterministic proof;
- ownership boundaries and unintended changes;
- unresolved material findings.

Capture a fingerprint of the relevant artifact and source diff immediately before
verification and compare it after the verifier returns. Any verifier mutation
invalidates the verdict and routes the changed scope back to the original owner.

For research or decisions, use an independent challenger or judge when sources
conflict, stakes are high, or the recommendation depends on uncertain assumptions.

If verification falsifies a claim, repair the smallest affected scope through the
original owner, then use a fresh re-verification. If the same issue fails again,
stop and report the blocker instead of renaming the task or looping.

## 7. Coordinate without polling

Use native direct-parent terminal callbacks as the primary completion surface.
Wait for the current wave only after all useful local integration work is
exhausted. Do not poll agent status, redraw workflow cards, narrate "still
running", or use shell waits. Children do not send progress chatter; before their
terminal deliverable, they communicate only material evidence or dependencies
that can change a decision.

Treat `wait_agent` as a blocking mailbox wait, not a durable multi-worker
all-terminal barrier. When the Orchestrator must block at a decision point, issue
one wait using the longest host-safe window that fits the relevant task deadline
and communication contract; terminal evidence may return it early. Any mailbox
update or timeout may return it, and neither proves every spawned child is
terminal.

Track terminal evidence for every spawned child. After a timeout with no new
evidence, do not immediately issue an identical re-wait. Continue useful work if
any remains; otherwise let native terminal notification deliver the result. If
the real deadline arrives without the required terminal evidence, report the
blocker instead of claiming that the wave joined or completed.

Preserve completed siblings. Cancel only work made obsolete by user steering,
shared-assumption failure, unsafe writes, or an explicit stop.

## 8. Finish at the outcome boundary

Complete only when:

- the requested outcome exists;
- relevant deterministic checks pass;
- the required fresh verification, challenge, or independent judgment passes;
- all spawned agents are terminal;
- material disagreements are resolved or explicitly reported;
- remaining risks and unperformed external actions are clear.

Commit, push, PR creation, publish, deploy, release, production mutation, and
messages to third parties remain separate approval boundaries unless the user
explicitly authorized the exact action.

Return one compact result: outcome first, agents and parallel work used, proof,
important challenge/repair decisions, changed artifacts, and remaining risks.
