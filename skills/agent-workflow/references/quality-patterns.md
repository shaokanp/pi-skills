# Quality And Throughput Patterns

Use this reference when the orchestrator shapes lanes and rounds for discovery-shaped goals (bug hunts, audits, research sweeps), high-stakes claims, or workflows where wall-clock and token cost matter. These patterns compose with the existing lane, gate, and execution-efficiency contracts; they do not replace them.

## Scale Calibration

Match verification depth to what the user asked for, not to a fixed template:

| Request shape | Review breadth | Verification depth |
| --- | --- | --- |
| Quick check ("sanity-check this") | 1 review lane | Lead spot-check; no verify lane unless a write happened |
| Standard ("find real bugs", "review this") | 2 review dimensions | 1 independent verify lane |
| Thorough ("audit", "be comprehensive", "must not miss") | 3+ dimensions or modalities | Per-finding verification panel plus completeness critic plus loop-until-dry rounds |

State the chosen tier in `orchestration.md` so a stricter tier is a deliberate decision, not drift.

## Dedup Before Verification

Verification is the expensive stage. When several review/discover lanes can report overlapping findings, the lead must merge and dedup findings after collection and before dispatching verification:

- Duplicate candidates: same file plus nearby line, or same claim with high token overlap.
- Merge duplicates into one finding and record every source lane id in the merged finding.
- Verify the deduped set, not the raw union.

Record the raw count and deduped count in `integration.md` so reviewers can see collection overlap.

## Verification Panels

A single verify lane has one identity and therefore one set of blind spots. Independence from the writer is necessary but not sufficient for high-stakes claims.

- For `P0`/`P1` candidate findings, externally visible actions, or claims the user will rely on without re-checking, verify with a panel: two to three verify/challenge lanes with distinct lens prompts (reproduction, impact/reality, security or data-loss), each a separate lane id per execution-efficiency v1.
- Prompt panel members to refute, not to confirm. Default-refute wording ("mark refuted unless you can reproduce the failure from the cited code") kills plausible-but-wrong findings that confirm-shaped prompts wave through.
- When the claim is executable, panel members should reproduce it by execution within the lane test budget — run the failing command against a disposable fixture instead of arguing from code reading alone — and may correct severity in either direction with the reproduction as evidence. A refute-shaped panel's severity correction is stronger evidence than the original reporter's self-assessment.
- Gate rule: a finding survives only if a majority of panel members fail to refute it. Record votes per finding in integration.
- For routine `P2`/`P3` findings, one independent verify lane remains enough; panels are a severity-triggered escalation, not a default cost.

## Loop-Until-Dry Convergence

A fixed round budget alone either stops too early (missed findings) or runs pointless rounds. For unknown-size discovery goals, add a convergence stop condition:

- Maintain a seen-findings ledger across rounds: every finding ever reported, including refuted and rejected ones, keyed by file/claim.
- Each new round dedups its findings against the full seen ledger, not against accepted findings only. Otherwise refuted findings resurface every round and the loop never converges.
- Stop when a configured number of consecutive rounds (default 2) produce zero new deduped findings, or when the round budget is exhausted, whichever comes first.
- Record the convergence reason in the final report: `dry` (consecutive empty rounds), `budget`, or an explicit gate.

## Completeness Critic

`challenge` attacks the assumptions behind what was produced. It does not ask what was never attempted. For thorough-tier workflows, add one completeness lane (a `challenge` variant) near the end of a round:

- Question: which declared inputs were never read, which success criterion has no bound evidence, which dimension or modality was never run, which finding was accepted without independent verification?
- Its findings become next-round work or explicit deferrals in integration; they cannot be silently dropped.

## No Silent Caps

Any lane that bounds its own output (top-N findings, sampled files, truncated scan) must say so in its `summary` and payload, including what was dropped and how it chose. Integration must surface every reported cap in `integration.md` and the final report. A capped lane that reports no cap is an incomplete-coverage defect, not a style issue: the final report would otherwise claim more coverage than actually happened.

## Lane-Death Quorum

A dispatched lane can die without producing output: runner error, killed subagent, exhausted budget with no checkpoint. Plan the tolerance before dispatch:

- Mark lanes `required: true` only when the round is meaningless without them (verification lanes usually are). Required-lane death blocks the round: gate `blocked` or re-dispatch.
- For parallel opinion lanes, declare a quorum in the round objective (for example, at least 2 of 3 review lanes complete). Below quorum, gate `more_discovery` instead of integrating a thin result.
- A dead optional lane must be explicitly disabled with the reason recorded in integration before final validation, never left pending. The coverage gap joins the final report's remaining risks.

## Dispatch Reuse On Unchanged Digests

Digest binding already invalidates stale work when inputs change. Use the converse to avoid paying twice when nothing changed:

- When resuming an interrupted or re-entered round, a lane whose `dispatch_sha256` is unchanged, whose bound input digests still match the current files, and whose complete output plus valid receipt already exist may be reused without re-dispatch.
- Record the reuse decision in integration (lane id, receipt path, reason `unchanged_dispatch`). Do not fabricate a new runner-evidence lifecycle record for a dispatch that did not happen; the original record remains the evidence.
- Any digest mismatch means re-dispatch. Reuse is a resume economics rule, not permission to skip re-verification after edits.

## Pinned Input Snapshots

Digest binding detects mid-round mutation of referenced files, but the workspace stores no copy of the referenced bytes, so an invalidated dispatch is unrecoverable: later lanes physically cannot read the content the digests were computed from, and the only remedy is a full re-dispatch against whatever the files say now.

For rounds whose correctness depends on reviewing a specific version — concurrent-edit environments, long rounds, externally owned files — snapshot small critical refs at preparation time:

- Copy each bound input into `rounds/<round-id>/inputs/<content-sha256-prefix>-<basename>` when it is small enough to store (a few hundred KB; never secrets, never bulky logs).
- Lanes may read the snapshot path instead of the live file; the digest in `input_refs` already names the exact content.
- On a mid-round mutation, the round can then finish against the pinned bytes and report both results: verified-as-of-snapshot plus a warning that the live file moved. Without a snapshot, gate `blocked` and re-dispatch remains the only honest continuation.

## Wall-Clock Shaping

Rounds are barriers: every enabled lane completes before integration. Keep the barrier, but do not make it more expensive than it must be:

- Dispatch all independent lanes of a round in one wave, not sequentially.
- When a lane consumes only one earlier lane's output (a verify lane bound to a single implement lane), it may dispatch as soon as that output and receipt exist, inside the same round, instead of waiting for unrelated lanes. Record it as a separate wait wave with the triggering receipt as the dispatch evidence.
- Do not add barriers between rounds that exist only for tidiness. A new round is justified by a gate decision, not by a desire to regroup.

## Claude Code Lane Cost Routing

Codex model routing defaults on for new native Codex workflows. For `claude_code_builtin_subagents`, apply the same philosophy as lightweight guidance:

- Default: omit model and effort overrides; lanes inherit the session model.
- Mechanical, low-ambiguity lanes (formatting checks, inventory sweeps, receipt summarization that somehow needs an LLM) may run on a cheaper model or lower effort; say so in the lane's `runner` notes.
- Never route `verify`, `challenge`, or any lane gating `P0`/`P1` claims below the session default.
- Record any per-lane override in `orchestration.json` under `runner` so the choice is auditable.
