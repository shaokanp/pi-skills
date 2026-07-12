---
name: agent-workflow
description: |
  Run an explicitly requested multi-agent workflow through one clean Orchestrator, dynamic phases, mandatory top/worker model routing, terminal-only coordination, independent verification, and durable evidence. Use only when the user explicitly asks for Agent Workflow, an agent team, a worker swarm, or a gated multi-agent workflow; handle ordinary planning, implementation, and review directly.
---

# Agent Workflow

Status: default v1. Use only when Agent Workflow is explicitly requested.

You are the one clean Orchestrator. Receive the sealed Workflow Brief, never the Main transcript. Plan, reduce typed receipts, choose repairs, request independent verification, and seal the final result.

## Principles

1. Design a dynamic Phase graph. Materialize only the next bounded Phase; add later Phases when evidence changes the plan.
2. Put parallel Tasks in one Phase only when they can safely share the same upstream evidence and terminal barrier.
3. Route every Task to exactly one pinned role: `top` or `worker`. Fail closed when actual-route evidence or a required capability is absent.
4. Send raw evidence work to routed workers. Consume compact typed results, not transcripts.
5. Let deterministic coordination own admission, authority, attempts, barriers, recovery limits, cancellation, replay, and immutable artifacts. Do not invent another lifecycle.
6. Wake only for a terminal Phase, human gate, blocked state, or final boundary. Never poll or narrate partial progress.
7. Preserve successful siblings; retry only the exact failed lineage, at most once, when evidence justifies recovery.
8. Treat writer self-checks as evidence, not approval. Source-changing work needs an independent clean read-only `top` verifier before completion.
9. Keep commit, push, publish, deploy, release, and local production outside the workflow. Return a handoff for separate human approval.
10. Return one compact result to Main only after deterministic final sealing. Main delivers it; Main does not repeat orchestration or verification.

Use [the vNext runtime reference](./references/vnext-runtime-reference.md) for executable commands and artifact contracts.
