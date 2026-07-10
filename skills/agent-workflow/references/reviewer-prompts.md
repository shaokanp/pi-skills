# Agent Loop Lane Prompts

Use these fallback prompts when the current platform has no specialized runner, CE/MAT skill, or domain-specific reviewer. They are narrow lane prompts, not final answers. The lead agent must validate JSON, integrate findings, and make final decisions.

All lane prompts must return JSON only using the envelope in `workflow-artifacts.md`.

## Common Envelope Reminder

Every lane returns:

```json
{
  "schema_version": "agent-loops.lane-output.v1",
  "run_id": "round-001-review-01",
  "round_id": "round-001",
  "lane_id": "review-01",
  "lane": "review",
  "status": "complete",
  "summary": "Short summary.",
  "confidence": {
    "self": null,
    "independent": 0.7,
    "source": "reviewer",
    "rationale": "Why this confidence is justified."
  },
  "findings": [],
  "gate": {
    "decision": "pass",
    "reason": "Why this decision follows.",
    "next_lanes": []
  },
  "payload": {}
}
```

If you cannot produce valid JSON, say nothing except a valid JSON object with `status` set to `invalid_output`.

## Execution-Efficiency Dispatch Wrapper

When `orchestration.execution_efficiency.enabled` is true, do not send the
conversation transcript or a generic lane template by itself. Build one
self-contained prompt from the frozen lane contract:

```text
Lane ref: {round_id}:{lane_id}
Unique question: {unique_question}
Purpose: {purpose}
Expected state change: {expected_state_change}
Allowed input refs: {input_refs}
Output schema: {output_schema}
Output path: {output_path}
Gate: {gate}
Budget: {max_tool_turns} tool turns, {max_test_runs} test runs
Workflow contract digest: {workflow_contract_sha256}
Dispatch digest: {dispatch_sha256}

Instructions:
{lane_specific_prompt}

Read only the declared workspace references. Do not assume access to the parent
transcript or undeclared lane output. Write the complete JSON envelope to the
exact output path. Build the compact lane receipt, then return only its compact
JSON line to the lead.
```

Each allowed input ref is a `root`/`path`/`content_sha256` file binding. Treat a
digest mismatch or changed workflow target as a new dispatch, not as permission
to continue from stale context.

Codex dispatch must explicitly use `fork_context: false`; Claude Code dispatch
must use isolated context. Keep the lane-specific prompt narrow enough to answer
one admitted question. If the task is deterministic, do not dispatch this
wrapper: run a script and persist its command/result evidence instead.

## Routed Lane Context

Model routing is explicit opt-in for `codex_builtin_subagents`; it is not inferred for ordinary, legacy, manual, or Claude Code workflows. For an enabled routed workflow, the lead must record a fresh capability recheck bound to the inventory snapshot, replace every scaffold `draft` with a complete planned decision, bind required verifier and author lanes, and pass `verify_workflow.py --mode planned` before dispatch.

Pass routed lanes this compact context when it affects their work:

```text
Routing packet facts: {routing_facts}
Planned route: {planned_route}
Decision digest: {planned_decision_sha256}
Verifier floor: {verification_floor}
Evidence level: lead_recorded
```

The workflow inherits one user-selected reasoning effort and no lane may raise or lower it. Route models by responsibility: Sol decides, interprets, reviews, challenges, verifies, or handles ambiguity and high risk; Terra executes a bounded packet. Never suggest Luna, an unclassified model, a model below the planned minimum, or any lane-specific effort substitution.

The lead owns snapshots and the append-only `runner-evidence.json attempts[]` ledger. Route-changing evidence must be a safe, existing, substantive workspace reference and escalation evidence must bind to the preceding failed attempt. Lanes may report outcome and evidence, but must not claim that lead-recorded capabilities, identity, lifecycle, or terminal actual route are runtime-attested or independently verified.

## Discover Lane

Purpose: establish ground truth before planning or repair.

```text
You are the discover lane for an agent-loop workflow.

Task:
{task}

Current evidence:
{evidence}

Return JSON only using the Agent Workflow lane-output envelope.

Payload schema: discover_payload.v1
Focus on:
- sources read or still needed
- confirmed current state
- constraints and non-goals
- unknowns that affect route or risk
- risks and approval boundaries
- recommended next lanes

Do not propose implementation unless it follows directly from evidence.
```

## Plan Lane

Purpose: convert evidence into a bounded approach or spec path.

```text
You are the plan lane for an agent-loop workflow.

Task:
{task}

Discovery/context:
{context}

Return JSON only using the Agent Workflow lane-output envelope.

Payload schema: plan_payload.v1
Focus on:
- approach
- work slices with ownership and verification
- dependencies and approval gates
- likely next lanes
- assumptions that need review or challenge
```

## Roundtable Lane

Purpose: expose productive tension for ambiguous strategy, spec, design, or implementation direction.

```text
You are the roundtable lane for an agent-loop workflow.

Topic:
{topic}

Context:
{context}

Participant guidance:
{participant_guidance}

Return JSON only using the Agent Workflow lane-output envelope.

Payload schema: roundtable_payload.v1
Rules:
- Select 3-5 participants as a tension network, not a simple pro/con split.
- Use `participant_type: real_person` for strategy/product/writing/philosophy when real thinkers improve the frame.
- Use `participant_type: agent_role` for engineering/spec/ops work.
- Include core disagreements, open questions, decision options, and recommended next lanes.
- Preserve unresolved tension when it is real; do not force consensus.
```

## Seam Lane

Purpose: inspect boundaries before or after implementation.

```text
You are the seam lane for an agent-loop workflow.

Task:
{task}

Plan or diff:
{plan_or_diff}

Return JSON only using the Agent Workflow lane-output envelope.

Payload schema: seam_payload.v1
Focus on:
- interfaces and contracts
- ownership boundaries
- integration risks
- adapter or migration seams
- hidden coupling
- repair or review lanes needed next
```

## Review Lane

Purpose: find actionable correctness, quality, scope, test, or policy issues.

```text
You are the review lane for an agent-loop workflow.

Task:
{task}

Plan/diff/output to review:
{artifact}

Evidence already read:
{evidence}

Return JSON only using the Agent Workflow lane-output envelope.

Payload schema: review_payload.v1
Focus on:
- logic errors, edge cases, regressions
- missing tests or weak verification
- workspace policy mismatches
- security, privacy, data loss, or permission risks
- scope creep or unnecessary complexity when it affects delivery
- routed snapshot/decision digest drift, unplanned dispatch, attempt overwrite, route-floor violations, or display projection used as evidence

Return only actionable findings. Each finding must have a stable id plus severity, claim, evidence, recommendation, and repair_packet when repair is possible, so integration can bind it to a later repair attempt.
```

## Challenge Lane

Purpose: adversarially test assumptions and premature agreement.

```text
You are the challenge lane for an agent-loop workflow.

Task:
{task}

Current plan/output:
{artifact}

Claims to challenge:
{claims}

Return JSON only using the Agent Workflow lane-output envelope.

Payload schema: challenge_payload.v1
Focus on:
- assumptions that could be false
- missing evidence
- alternative interpretations
- high-impact failure modes
- whether confidence is warranted
- whether a route raise, fallback, escalation, or verifier-floor claim is actually supported by the immutable decision and append-only attempts

Be sharp but evidence-bound. Do not invent blockers. Keep finding ids stable so a targeted repair packet can resolve the exact claim.
```

## Verify Lane

Purpose: decide whether the round can pass.

```text
You are the independent verify lane for an agent-loop workflow.

Task:
{task}

Success criteria:
{success_criteria}

Integrated result:
{integrated_result}

Checks/evidence available:
{evidence}

Routing claim class:
{claim_class}

Author lane ids:
{author_lane_ids}

Required verifier minimum route:
{minimum_route}

Required evidence names:
{required_evidence}

Missing-evidence action:
{missing_evidence_action}

Return JSON only using the Agent Workflow lane-output envelope.

Payload schema: verify_payload.v1
Gate policy:
- P0/P1 findings block pass.
- P2 findings require repair or explicit deferral.
- P3 findings do not block by default.
- Your independent confidence gates the round.
- For a routed claim, verify every routed author and verifier has a completed terminal attempt, your terminal lead-recorded actual route is at or above `minimum_route`, your recorded identity differs from every author lane, and each required evidence name binds to a substantive passing check in your payload.
- A routed verifier must complete with `gate.decision: pass`; passing checks inside a revise/blocked output do not satisfy the floor.
- If route, identity, or required evidence is missing, use the declared `more_discovery`, `human_gate`, or `blocked` action instead of passing.

Set `gate.decision` to one of: pass, revise, more_discovery, challenge, second_opinion, human_gate, blocked.
Explain what evidence would raise confidence if you do not pass.
```

## Repair Lane

Purpose: execute one bounded repair packet.

```text
You are the repair lane for an agent-loop workflow.

Task:
{task}

Repair packet:
{repair_packet}

Current state:
{context}

Return JSON only using the Agent Workflow lane-output envelope.

Payload schema: repair_payload.v1
Focus on:
- the exact repair objective
- source findings addressed
- changes made or proposed
- checks run
- remaining risk
- recommended next lane
- route outcome evidence the lead can append without rewriting the planned decision or prior attempts

Do not broaden scope beyond the repair packet. The lead, not the repair lane, records any retry, fallback, or escalation as a new attempt and preserves lead-recorded wording.
```

## Integration Ledger

The lead agent, not a worker lane, writes integration decisions:

| Suggestion | Source lane | Decision | Reason | Follow-up |
| --- | --- | --- | --- | --- |
| ... | review/challenge/verify/etc. | adopted / modified / rejected / deferred | evidence-based reason | repair, verify, human gate, or none |

Decision rules:

- Adopt when evidence is strong and the fix fits scope.
- Modify when the concern is valid but the implementation conflicts with local patterns.
- Reject when it conflicts with evidence, repo rules, or user scope.
- Defer when it is real but outside the current promise; make it durable only when the workflow needs a durable sink.
