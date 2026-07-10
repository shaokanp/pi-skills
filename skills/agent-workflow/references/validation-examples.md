# Validation Examples

Use these examples when forward-testing or improving `agent-workflow`.

## Should Not Trigger On Bare Workflow

Prompt:

```text
Can you make a workflow for publishing this blog post?
```

Expected behavior:

- Do not invoke `agent-workflow` solely because the word `workflow` appears.
- Use a normal plan or the more specific relevant skill.
- Invoke `agent-workflow` only if the user asks for agent loop workflow, agent team workflow, multi-agent orchestration, review/challenge/verify loops, or durable run state.

## Small Agent Loop

Prompt:

```text
Use agent-workflow to fix a typo in README.md, but still sanity-check it.
```

Expected behavior:

- Use a light chat-level workflow.
- Say that a run workspace is unnecessary unless the user insists.
- Make the edit directly.
- Verify the diff.
- Do not create `.workflow/`.

## Planner-First Agent Team Workflow

Prompt:

```text
Use an agent team workflow to design the spec for replacing our import pipeline. I want discovery, roundtable, challenge, and verification before we implement anything.
```

Expected behavior:

- Invoke `agent-workflow`.
- Create a planner-first orchestration contract before running lanes.
- Use `discover`, `roundtable`, `plan`, `challenge`, and `verify` lanes.
- Use JSON-only intermediate lane outputs if a run workspace is active.
- Gate the round with independent verifier/challenger confidence, not worker self-confidence.
- Do not implement until the spec gate passes or the user explicitly changes scope.

## Multi-Round Repair Loop

Prompt:

```text
Use an agent loop workflow to build this feature. If verify finds it is not good enough, send it into another round rather than stopping at the first draft.
```

Expected behavior:

- Create a run workspace when scope is broad enough.
- Compile `orchestration.md` and `orchestration.json`.
- Run or simulate lanes separately.
- If verify finds P0/P1 issues or low independent confidence with actionable fixes, create a next round with a targeted `repair` lane followed by `verify`.
- Stop only on pass, human gate, blocked, explicit deferral, or exhausted round budget.

## Roundtable Lane For Engineering Spec

Prompt:

```text
Use agent-workflow and include a roundtable lane to decide whether this should be a shared service, a local module, or a one-off script.
```

Expected behavior:

- Use `roundtable` as a reusable reasoning lane.
- Select participants using the hybrid rule.
- Prefer agent roles for technical/spec work unless a real-person frame would add real strategic tension.
- Produce participants, tension map, core disagreements, open questions, decision options, and recommended next lanes.
- Preserve unresolved tension instead of forcing consensus.

## Run Workspace Scaffold

Prompt:

```text
Use agent-workflow for this broad migration and keep durable workflow state.
```

Expected behavior:

- Create `.workflow/<slug>/` with `plan.md`, `state.json`, `token-usage.json`, `orchestration.md`, `orchestration.json`, `rounds/round-001/lane-runs/`, `rounds/round-001/integration.json`, `rounds/round-001/integration.md`, and `final-report.md`.
- Use `scripts/new_workflow.py` when available.
- Use `scripts/verify_workflow.py --mode scaffold` for a fresh scaffold.
- Use `scripts/verify_workflow.py --mode planned` after orchestration is populated and before the first dispatch. Routed scaffolds must replace every `draft` decision first.
- Use `scripts/verify_workflow.py --mode executed` after planned lane outputs exist.
- Use `scripts/verify_workflow.py --mode final` before any final claim.
- Remember that scripts do not spawn agents.

## Default Native Execution Efficiency

Prompt:

```text
Use an agent workflow, isolate every subagent context, avoid polling turns, and integrate compact receipts instead of replaying full outputs.
```

Expected behavior:

- New Codex and Claude Code native scaffolds enable the policy automatically.
  Manual simulation and existing v1 workspaces remain unchanged when the block
  is absent. Use `--execution-efficiency off` only for an explicit compatibility
  rollback; `--execution-efficiency native` may still assert native-only intent.
- Keep planning and integration lead-owned. Require an explicit exception for a
  separate plan lane, enable seam only for real boundary risk, reject duplicate
  lane questions, use one agent per efficiency lane, and execute deterministic
  checks without an LLM lane.
- For every Codex lane set `fork_context: false`; for Claude Code set isolated
  context. Do not include the parent transcript or undeclared raw prior outputs.
- Populate a specific bounded prompt, workspace-relative input refs, output
  schema/path, admission rationale, budget, and gate. Dispatch preparation binds
  the workflow target, each input file, and every lane dispatch to canonical
  digests. Verify pass must cover the exact current success criteria. Run
  `prepare_dispatch.py`, then require planned validation before dispatch.
- Spawn a concurrent wave and call one multi-target native long wait. Do not
  create lead completions for status checks. A 30-second polling interval,
  unresolved final timeout/error, or `status_only_completions > 0` must fail.
  A partial terminal event may continue the wait immediately; a timeout-based
  re-wait requires the configured interval and recorded causal timestamps.
  Keep continuation inside one Lead-side barrier operation, remove every
  terminal target, preserve the exact active set, and reject decoy or duplicate
  aliases. A timeout must consume its declared duration before continuation.
- Write full lane JSON to the declared output path, build a receipt, and return
  only receipt-sized information to the lead. Build `integration-index.json`
  before executed/final validation; receipt, output, dispatch, or index digest
  drift must fail.
- Require review+verify for write lanes and challenge+verify for high-risk work.
  A repair may reuse its source writer once; assessment identities remain
  independent from writer identities.
- Record model completion, tool-turn, test-run, repair, wait, and card-event
  metrics. Work checkpoints, rotates, or gates before exceeding a hard budget;
  a canonical writer identity can be reused for only one repair across the
  workflow. It never lowers model quality or skips verification.

Scoped regression commands:

```bash
python3 skills/agent-workflow/scripts/test_execution_efficiency.py
bash scripts/validate-skill.sh agent-workflow
```

The standard-library suite covers Codex, Claude Code, manual simulation, legacy
v1, scaffold/planned/executed validators, digest and receipt tampering, 30-second
polling, timeout, heartbeat cards, deterministic lane admission, budget
exhaustion, repair affinity, and verifier independence without invoking an LLM.

## Opt-In Codex Model Routing

Prompt:

```text
Use an agent team workflow and explicitly route Codex subagents with the portable responsibility-routing policy.
```

Expected behavior:

- Enable routing only with `--model-routing codex`, `--runner-mode codex_builtin_subagents`, a lead-provided `--runtime-capabilities` JSON inventory, explicit `--reasoning-effort`, and fresh `--runner-capability-evidence`. The inventory file alone does not certify availability. Default scaffolds contain no routing block or routing files.
- Persist `routing-policy.json` and `runtime-capabilities.json` with canonical digests, and copy both snapshot IDs/digests into every planned lane decision.
- Classify all packet facts, compute the ordered first-match decision, bind every required verifier and author lane, confirm planned verifier routes meet their floors, replace `draft`, and pass `--mode planned` before dispatch.
- Snapshot one user-session reasoning effort and require every selected, dispatched, and actual route to preserve it. The router never chooses effort.
- Route Sol for thinking roles, material ambiguity, cross-boundary work, production or hard-to-reverse risk, weak verifiability, novelty, and judgment claims. Route Terra only for the bounded default execution case.
- A reasoned lead/user request may raise Terra to Sol at the inherited effort. A request to lower Sol or change effort becomes a human gate or validation failure.
- Keep one runner record per round/lane and append attempts. Retry keeps model and effort after context/tool failure; fallback or escalation may only change Terra to Sol while preserving effort. Route-changing evidence refs must be safe, existing, substantive workspace artifacts. Never rewrite the planned selection, digest, or earlier attempts.
- In final mode, every routed lane ends with a completed terminal attempt. Every required verifier completes with a pass gate, meets the minimum terminal actual route, has a different recorded identity from every named author, and binds every required evidence name to a substantive passing check.
- Label capability, identity, lifecycle, attempt, and actual-route evidence as lead-recorded. Do not call it runtime-attested or independently verified.
- Project routing into the optional Swarm Card from orchestration plus runner evidence. The card cannot become routing truth or execution evidence.

Scoped regression commands:

```bash
python3 -m py_compile skills/agent-workflow/scripts/*.py
python3 skills/agent-workflow/scripts/test_model_routing.py
bash scripts/validate-skill.sh agent-workflow
```

`test_model_routing.py` uses only the Python standard library. It executes every tracked positive and negative case under `fixtures/model-routing/`, creates disposable workspaces outside the repo, and covers helper validation plus legacy, routing-off, scaffold, planned, executed, and final routing paths.

## Native Runner Adapter

Prompt:

```text
Use an agent team workflow and actually run the lanes with native subagents if this environment supports it.
```

Expected behavior:

- In Codex, choose `codex_builtin_subagents` only when the `multi_agent_v1` tools are available, then use `spawn_agent` / `send_input` / `wait_agent` / `close_agent` directly.
- In Claude Code, choose `claude_code_builtin_subagents` and use Claude Code's own Agent/subagent or agent-team surface inside that same Claude Code session.
- Do not have Codex call `claude -p`, `claude agents`, or Claude Code workflows as a runner.
- Do not have Claude Code call `codex exec` as a runner.
- If neither native surface is available, use `manual_simulation` and clearly say no subagents ran.

## Swarm Card Display

Prompt:

```text
Use an agent team workflow for this change, and show me the swarm before you launch it.
```

Expected behavior:

- Invoke `agent-workflow` when the work genuinely needs an agent team, native
  subagents, a swarm, dynamic workflow, multi-agent simulation, or durable
  multi-round state.
- Compile the orchestration contract before dispatch.
- Display a CJK-safe left-rail Swarm Card before the first dispatch. Do not use
  closed right-border ASCII boxes for cards that may contain Chinese text.
- Show model/runner posture, phases, planned agent slots, gate policy, and
  whether work is native, simulated, or lead-owned.
- If the workflow is a manual simulation of multiple lanes, still show the card
  but label simulated lanes as simulated.
- Do not depend on exact column widths; shorten or wrap long CJK labels, and
  fall back to one field per line when alignment would be fragile.
- Update the card on meaningful events: first dispatch, phase status change,
  material agent failure/blocker, integration gate, round transition, and final
  stop.
- Do not poll or reprint the same card. Update it only from a dispatch, terminal
  wait, material failure, gate, round transition, or final-stop event.
- Treat any `swarm-card.json` file as user-visible display state, not runner
  evidence.
- For routed cards, project `packet_id`, `decision_id`, planned route, terminal
  actual route, route status, and attempt count. Final mode must reject drift
  from orchestration or runner evidence.

## Invalid Lane JSON

Prompt:

```text
Use an agent team workflow and make the lanes machine-readable so you can continue automatically.
```

Expected behavior:

- Require JSON-only intermediate lane outputs.
- If a lane output is invalid JSON or fails the envelope, ask the same lane to repair JSON once.
- If repair fails, mark the lane `invalid_output`, integrate that failure, and gate accordingly.

## No Subagent Runner

Prompt:

```text
Use an agent team workflow to review this feature for security and reliability risks.
```

Expected behavior:

- Use actual subagents only if the current platform exposes a real runner and the workflow justifies it.
- If no runner is available, simulate lanes sequentially in the main thread.
- Keep lane outputs separate as JSON before integration.
- State honestly what ran and what was simulated.

## Hardened Verifier Regressions

Expected behavior:

- Routing checks activate only when `orchestration.model_routing.enabled` is exactly `true`; missing/disabled routing preserves legacy, manual, Claude Code, and prior v1 runner records without attempts.
- Routed `scaffold` accepts draft decisions, `planned` rejects drafts/human gates before dispatch, and `executed`/`final` require attempts for every dispatched routed lane.
- Routed snapshots, references, planned decisions, and attempt decision references reject canonical digest drift.
- Routed v2 rejects semantically weakened policies even when an attacker recomputes a valid content digest.
- Capability observations require valid RFC3339 timestamps, and planned mode requires an explicit fresh recheck bound to the capability snapshot digest.
- Responsibility routing rejects prose predicates, duplicate priorities, missing defaults, Luna, effort-bearing policy effects, unknown facts, aliases such as `novelty: mixed`, and missing packet facts.
- The capability snapshot requires one locked `user_session` effort. Attempt, override, orchestration, and actual-route drift from that effort fail closed.
- Attempt validation rejects changed-model retries, excess retries, fallback after the wrong failure/outcome, changed effort, unsafe or missing model-change evidence, unbound escalation evidence, excess model changes, silent actual-route substitution, terminal pointer drift, post-completion attempts, and planned digest drift.
- Planned verifier-floor validation rejects missing bindings or below-floor verifier plans. Final validation also rejects non-completed terminal attempts, a missing/non-pass verifier, identity overlap with any named author, or missing substantive named checks, and applies the decision's missing-evidence action.
- Final Swarm projection validation rejects stale planned/actual routes, status, or attempt counts; removing the optional card does not weaken routing validation.

- A v1 workspace checked without `--mode` fails; bare structural validation is not final evidence.
- A fresh scaffold with TODO placeholders passes `--mode scaffold` and fails `--mode executed` / `--mode final`.
- A planned `discover-01` output whose JSON says `"lane": "verify"` fails in executed/final mode.
- An unplanned extra lane output under `lane-runs/` fails in executed/final mode.
- Native runner mode without verified `capability_evidence` fails executed/final mode.
- Final native mode requires lifecycle evidence for native spawned lanes.
- Final native mode rejects runner-evidence root `runner_mode` / `dispatch_surface` mismatches and native lifecycle records missing `agent_id`/`native_handle` or `spawn_tool`.
- Final native mode rejects lifecycle records missing `round_id`, and `round_id` must match the lane output.
- Required lane outputs marked `pending`, `running`, `skipped`, `blocked`, or `invalid_output` fail in executed/final mode.
- Final mode rejects scaffold-only final reports, empty pass verification evidence, null pass stop reasons, and `integration.status: "pending"` when the workflow claims completion.
- A `P1` or unresolved `P2` finding with `gate.decision: "pass"` fails.
- A `P2` finding with only a `repair_packet` is actionable but not resolved; pass gates require an explicit resolution.
- Bare `finding_resolutions` enums fail final mode unless the required typed fields for that resolution kind are present.
- Duplicate explicit `P2+` finding ids fail final mode so one resolution cannot clear multiple distinct findings.
- A verify/challenge pass with independent confidence below the documented threshold fails.
- Payload-only actionable findings are surfaced by collection and fail final mode unless mirrored or resolved.
- Repair packets plus `stop_reason: "verify_pass"` fail unless repairs are resolved, deferred, rejected, or human-gated.
- `next_round` pointing to a missing round directory fails.
- Rerunning `new_workflow.py` on an existing slug fails unless `--reuse-existing` is explicit.
- State/orchestration/lane/runner-evidence runner mode mismatches fail.
- State/orchestration progress drift fails: `round_budget` must match, declared round sets must match, and each `state.rounds[].enabled_lanes` list must match enabled orchestration lane ids.
- Duplicate `round_id`, duplicate orchestration lane ids within a round, and duplicate `state.rounds[].enabled_lanes` entries fail before final pass so dict/set projections cannot hide required work.
- Final mode rejects any declared non-current round that is still planned, pending, running, or missing a terminal integration disposition.
- Final mode rejects enabled lane outputs whose status is not `complete`; optional lanes must be explicitly disabled before final pass rather than left invalid or pending.
- Final mode rejects terminal-round enabled lanes whose gate decision is not `pass`; one verify pass cannot override an enabled challenge/review/verify lane asking for revise.
- Non-current rounds must have consistent terminal graph fields: state status, gate decision, integration status, stop reason, and next round for revised rounds.
- Native final mode rejects `lead_owned` dispatch for normal worker lanes such as discover, seam, review, challenge, and verify; lead-owned bypasses are limited to implementation/repair lanes.
- Final mode rejects non-current round graph tuple mismatches, undeclared directories under `rounds/`, failed `verify_payload.checks`, and failed `finding_resolutions[].checks` used as `repaired_by` evidence.
- Final mode rejects low-information check evidence such as `ok`, `pass`, `done`, `true`, or longer content-free phrases like "all checks passed"; passing verify checks and success criteria need substantive evidence with a persisted command/result artifact, existing path whose claimed fact is present in the artifact, or fixture result persisted under the workflow workspace. Bare fake path-like or command-like tokens, standalone `exit=0`, ghost fixture names, mismatched exit codes, mismatched command claims, path-existence-only claims, and ephemeral `/tmp` paths do not count. Exact persisted fixture name + command + matching `exit_code` should pass even when the result name contains the word `command`; the same fixture with a wrong named command should fail; a named result with the wrong local exit code must still fail even if another result in the same evidence text mentions the correct exit code, including local `exit_code: 1` and `exit_code=1` punctuation variants.
- Final mode rejects non-fixture workflow artifact ghost claims: a lane output, integration file, or final report path only counts when the claimed fact is actually present in the referenced artifact. Short absent-content claims such as `bug fixed`, `P0 done`, `P1 ok`, `P2 fixed`, or arbitrary non-stopword short tokens such as `ux fixed` must fail instead of passing as empty claim-token evidence. Artifact matching is word-token based, so `unfixed` does not prove `fixed`, `unsatisfied` does not prove `satisfied`, and explicit negation such as `not fixed` or `not satisfied` cannot prove the positive claim.
- Final mode rejects runner lifecycle overclaims: `lead_recorded` native evidence may pass only when recorded as `lead_recorded_native` and the final report names it as lead-recorded lifecycle fields/evidence/entries/ledger/records. It must not certify, certified, certification, confirm, confirmed, verify, verified, validate, validated, prove, proven, complete, close successfully, ran, execute, or otherwise imply independently verified native execution, including hyphen/punctuation variants such as `closed-successfully`; `native lifecycle` without `native subagent` still triggers this guard.
- Final mode accepts correct JSON-like fixture result evidence such as `"exit_code": 0`, rejects wrong JSON-like local exits, and uses longest-name matching when one fixture result name prefixes another.
- Final mode accepts short but auditable workflow artifact claims when the referenced artifact actually contains the claimed fact, such as `rounds/round-023/lane-runs/repair-01.json records source_findings`; absent facts against the same artifact must still fail.
- Final mode rejects positive artifact claims negated after the token, including JSON-style `fixed: false`, `resolved: false`, `satisfied: false`, and `pass: false`, while allowing affirmative values for the same claim.
- Final mode rejects `lead_recorded` wording that calls native lifecycle evidence verified/verifies/verification without external runtime attestation, but allows scoped lead-recorded lifecycle records such as recorded run ids or named agent id fields.
- Final mode strips `:line` and `#Lline` citation suffixes from workflow artifact refs before claim-token matching; line numbers must not prove facts, while valid path-with-line claims such as `repair-01.json:26 records source_findings` should pass if the artifact contains the fact.
- Final mode rejects duplicate-ish fixture names (`alpha_bad` when only `alpha` exists), duplicate result names, and shared-exit claims like `alpha and beta exit_code: 0` unless each named result has an unambiguous matching local exit or command.
- Final mode treats JSON falsey values such as `0` and `null` as negative for positive artifact claims, but does not let an earlier `fixed: false` negate a later unnegated affirmative `fixed: true` claim.
- Final mode rejects `lead_recorded` subagent/agent execution overclaims even when the sentence omits `native`, for example lifecycle records that say subagents ran.
- Final mode applies the same content-token check to source refs as workflow refs: a missing source token must fail, a present source token such as `tool_event_verified` must pass, and source line/anchor suffixes must not become proof tokens.
- Final mode rejects punctuation-suffixed fixture result-name spoofing such as `alpha.bad`, `alpha/bad`, or `alpha:bad` when only `alpha` exists.
- Final mode rejects shorthand source-ref path-existence bypasses for `scripts/...`, `references/...`, and `agents/...`; these refs must also prove the claimed content token.
- Final mode rejects multi-ref content laundering: if an evidence line cites two workflow/source artifacts, every cited artifact must contain the claimed fact.
- Final mode rejects comma, semicolon, and whitespace fixture result-name spoofing such as `alpha,bad`, `alpha;bad`, or `alpha bad` when only `alpha` exists, and duplicate fixture result names cannot pass through aggregate `expectations_match true` evidence.
- Final mode rejects fixture connector ambiguity: a known result name followed by unsupported connector/suffix text cannot be silently ignored, so `alpha and beta exit_code: 0` fails unless each named result has local evidence.
- Final mode rejects pure aggregate `expectations_match true` as proof for command-kind checks; command checks need bound command/result evidence or an inspected artifact claim, and aggregate fixture evidence requires expectations/results consistency.
- Final mode rejects command-kind checks backed only by unrelated artifact content, for example a `python syntax validation` check citing `repair-01.json records source_findings`.
- Final mode rejects command-kind checks backed only by source/docs/changelog text that mentions the check name, for example `references/validation-examples.md records python syntax validation`.
- Final mode binds falsey JSON values to the relevant positive key: `fixed:true` next to `resolved:false` may still prove `fixed`, but `fixed:false` plus `resolved:true` must not prove the compound `fixed resolved` claim.
- Final mode treats short positive claims such as `fix` and `ok` as falsey-sensitive: `fix:false`, `fix:0`, `ok:false`, and `ok:null` reject the positive claim.
- Final mode rejects singular and auxiliary `lead_recorded` run overclaims, including `agent ran`, `subagent ran`, `agent did run`, and `subagent was run`, while preserving `run_id` field mentions.
- Final mode rejects broader `lead_recorded` run-state overclaims such as `native subagent has run`, `native subagent is running`, `native subagent finished`, `native subagent returned`, `native subagent succeeded`, and `native subagent produced output`.
- Final mode rejects `lead_recorded` output/result/state synonyms such as `native subagent generated output`, `emitted output`, `yielded output`, `produced a result`, `generated a result`, and `ended`, while preserving field names such as `output_path`.
- Final mode rejects passive/object-first `lead_recorded` output/result variants such as `output was generated`, `output was emitted`, `output was yielded`, and `result was produced`, while preserving field names such as `output_path`.
- Final mode rejects additional passive/object-first `lead_recorded` creation/writing/response variants such as `output was created`, `result was written`, and `response was generated`.
- Final mode rejects fake command/result vocabulary added only to the evidence sentence, such as source/docs/changelog refs plus `command status <check name>`, and command-kind artifact fallback must bind to a same-record workflow JSON command/check/result tuple.
- Final mode rejects command-kind artifact fallback when the same-record tuple has a failed result for a pass check, or when its `command`/`commands` value does not match `check.command`. A nonzero/fail-like command artifact can pass only for an explicit expected failure/rejection check, and writing `rejected` only in the evidence sentence must not create that exception.
- Final mode rejects fixture-bound command evidence that cites a real passing fixture result for the wrong current `check.command`; fixture command evidence must bind the named fixture result's command to the check command.
- Final mode rejects workflow JSON command artifacts with nested failed result objects such as `result.exit_code: 1` under a pass-like parent record, and rejects mixed unbound batched exits such as `exit_code: [1, 0]` for a command array unless the check/record is explicitly expected-failure.
- Final mode rejects nonzero command evidence when `rejected`/`rejection` appears only in the check or result name. Expected process failure must be explicit through `expected_failure: true` or a dedicated `expected`/`expectation` field.
- Final mode treats `expected`/`expectation` expected-failure fields as affirmative-only token phrases: `expected failure` and `expected to fail` may allow nonzero command evidence, while `unexpected failure`, `not expected to fail`, `no expected failure`, and `expected failure: false` must fail.
- Final mode rejects standalone or alternate-auxiliary passive output/result/response overclaims anywhere in a lead-recorded final report, such as `output had been created` or `response would be generated`, while preserving `output_path` field references.
- Final mode rejects plural and adverb-split passive output/result/response overclaims anywhere in a lead-recorded final report, such as `outputs were created`, `output has already been created`, and `response did actually get generated`, while preserving `output_path` field references.
- Final mode rejects `tool_event_verified` runner evidence in v1 unless a future external runtime attestation verifier is implemented. A self-consistent lead-authored event log with `trusted_capture`, `capture_source`, `transcript_hash`, `event_hash_chain`, or matching spawn/wait/close records must still fail; use `lead_recorded` for current native subagent lifecycle ledgers.
- Swarm Card v2 renders one agent per phase-grouped Markdown line, keeps every status symbol adjacent to its text label, displays only the model in italic parentheses, keeps effort in routing evidence, and rejects executor-type legend symbols.
- `render_swarm_card.py --emit` records the visible render hash, suppresses unchanged redraws, and emits again after a material status or gate transition.
- Running cards report `Tokens: measuring`; only a completed exact token-usage v2 ledger may render a numeric total.
- Final mode rejects pass-like non-current rounds whose lane outputs still have non-pass gate decisions.
- Strict modes reject nested entries under `lane-runs/` so hidden lane-output JSON cannot bypass validation.
- Final mode rejects duplicate `finding_resolutions` for the same `finding_id`, and rejects `blocked` resolutions when the workflow claims a pass-like stop reason.
- Final mode rejects `finding_resolutions` entries whose `finding_id` does not match a collected `P2+` finding.
- Final mode rejects keyword- or anchor-stuffed `final-report.md` stubs; final reports need substantive outcome, verification, risk, stop/gate, and runner/workflow execution sections, and the verification section needs concrete command/result plus runner or lane evidence and real check/evidence pairs or criteria names from the current passing verify lane. Check/evidence pairs must be locally bound in the same bullet, table row, or structured evidence line; blank-line-separated nearby paragraphs and separate evidence caches do not count.
- Final mode rejects missing, pending, zero, or unknown `token-usage.json`; final reports must include a token usage section that repeats the total workflow token count and labels the source/confidence.
- New workspaces auto-start token-usage v2 when `CODEX_THREAD_ID` resolves to a native session log; otherwise they remain pending until an explicit `token_accounting.py start` succeeds.
- Exact accounting sums the Lead session counter delta with every registered terminal agent session, including failed/retry/repair attempts, and does not double-count cached or reasoning subsets.
- Claude accounting deduplicates streaming copies by message id and rejects unfinished message ids rather than treating partial usage as exact.
- Final mode rejects v2 arithmetic tampering, evidence digest or source-event drift, missing or duplicate execution coverage, nonterminal agent logs, token participants absent from runner evidence, and runtime-discovered child sessions absent from the participant registry.
- Claude `tool_use` and later partial messages remain nonterminal; the trailing Lead `tool_use` that invokes finalization is excluded. Codex accepts terminal `task_complete` and `turn_aborted`, but a later reopened turn makes the session nonterminal again.
- Final mode rejects v1 `runtime_reported`, `runner_reported`, or `confidence: exact`; v1 estimates remain legacy-compatible but cannot claim exactness.
- New `agent-workflow.workflow.v2` state requires its token contract and token-usage v2 ledger; deleting the contract or replacing the ledger with legacy v1 fails final validation.
- Run `python3 scripts/test_token_accounting.py` to exercise Codex and Claude runtime fixtures, terminal-state rejection, coverage checks, auto-start, and v1 exactness regression.

## Quality Patterns

Prompt:

```text
Use an agent workflow to audit this module thoroughly; do not miss real bugs, and only report what survives adversarial verification.
```

Expected behavior:

- Read `references/quality-patterns.md` and state the chosen calibration tier in `orchestration.md`: thorough-tier requests get 3+ review dimensions, a per-finding verification panel, a completeness critic, and loop-until-dry rounds; quick checks stay small.
- Dedup overlapping findings across review lanes before dispatching verification, and record raw vs deduped counts in `integration.md`.
- For `P0`/`P1` candidates, verify with two to three distinct-lens lanes prompted to refute (default-refute wording); a finding survives only when a majority fails to refute it. Routine `P2`/`P3` findings keep a single independent verify lane.
- For discovery-shaped goals, maintain a seen-findings ledger across rounds (including refuted findings), dedup each round against it, and stop after the configured number of consecutive dry rounds; record the convergence reason in the final report.
- Surface every lane-reported output cap in integration and the final report; a capped lane that reports no cap is an incomplete-coverage defect.
- Apply the lane-death quorum: required-lane death blocks the round; below-quorum opinion waves gate `more_discovery`; a dead optional lane is explicitly disabled with its reason recorded before final validation.
- When resuming an interrupted round, reuse a lane output only when its dispatch digest, bound input digests, complete output, and valid receipt all still match; record the reuse decision in integration without fabricating a new lifecycle record.
- Keep verify/challenge lanes and `P0`/`P1` gates at or above the session-default model quality when applying per-lane cost routing on Claude Code.
