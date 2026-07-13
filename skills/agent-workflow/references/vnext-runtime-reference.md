# Agent Workflow vNext Runtime Reference

This reference contains executable mechanics for the thin candidate instruction. It is not a second policy layer. The Orchestrator chooses the semantic plan; the runtime enforces authority, transactions, replay, and terminal barriers.

## Workspace

Each run uses one `.workflow/<slug>/` directory. Immutable, digest-bound artifacts are the source of truth. `view.json` is a derived projection for humans and UI surfaces; never use it as authority.

The sealed Workflow Brief is the Orchestrator's only inherited context. Phase and Task artifacts hold scoped prompts, criteria, dependencies, routing roles, attempts, results, receipts, amendments, and final evidence.

The admitted `workflow.routing.top_model` and `workflow.routing.worker_model` bind the two roles to concrete models and one inherited reasoning effort. A Task selects exactly one role; the runtime attests the actual route before accepting its result.

## Materialization contracts

Use these shipped, validator-backed files as the minimum contract examples:

- `skills/agent-workflow/fixtures/vnext/protocol/valid/workflow.json`
- `skills/agent-workflow/fixtures/vnext/protocol/valid/phase-plan.json`
- `skills/agent-workflow/fixtures/vnext/protocol/valid/final.json`

Copy a template outside the workflow root, replace example-specific scenario values, preserve schema constants and enum values, create each referenced packet/evidence file, and compute its SHA-256 before calling the lifecycle command. `admit`, `run-phase`, and `seal-final` validate the source and publish authoritative copies create-once. The Orchestrator writes plans and final candidates only; the one runtime implementation writes results, receipts, claims, events, and `view.json`.

`workflow.json` seals objective, criteria, routing, limits, bundle, baseline, repository facts, capabilities, and accounting coverage. A phase plan seals generation, predecessor, authority, cause, intent, deadline, and bounded Tasks. Each Task carries exactly one role, one criterion lineage, work mode, packet digest, input digests, disjoint write roots, and deadline. The final candidate binds the exact receipts, amendments, lineage claims, verification, P2 resolutions, accounting, completion density, report, and runtime bundle.

## Lifecycle commands

Invoke `workflow_runtime.py` from `skills/agent-workflow/scripts/`:

```bash
python3 workflow_runtime.py probe-host-capabilities --root <workflow> --repo <repo> --relevant-root <relative-root> --auth-source <auth.json>
python3 workflow_runtime.py probe-source-write --root <workflow> --auth-source <auth.json>
python3 workflow_runtime.py admit --root <workflow> --repo <repo> --workflow-source <workflow.json>
python3 workflow_runtime.py pinned-runtime --root <workflow>
python3 host_validation.py --root <workflow> --repo <repo> --spec-source <validation.json>
python3 workflow_runtime.py run-phase --root <workflow> --repo <repo> --plan-source <phase.json> --auth-source <auth.json> --max-parallel <n>
python3 workflow_runtime.py cancel --root <workflow> --authority-revision <revision>
python3 workflow_runtime.py reconcile --root <workflow> --authority-revision <revision>
python3 workflow_runtime.py amend --root <workflow> --request-source <amendment.json>
python3 workflow_runtime.py resume-brief --root <workflow> --generation-id <generation-id>
python3 workflow_runtime.py seal-final --root <workflow> --candidate-source <final-candidate.json>
python3 workflow_runtime.py seal-accounting --root <workflow> --native-source <native-observation.json> --native-evidence-source <native-events.jsonl> --completion-source <orchestrator-session.jsonl>
```

Fresh admission is intentionally self-bootstrapping. Before materializing a
`read_only_canary` or `source_write` workflow source, run
`probe-host-capabilities` once for that workflow root. It launches one pinned
Terra and one pinned Sol read-only probe behind a single terminal barrier, then
returns ready-to-copy workflow capability bindings backed by raw session,
route, permission, token, denial, and focused-test evidence. A `source_write`
workflow additionally runs `probe-source-write` and replaces only the returned
`sandbox_isolation` binding with that live writer-probe binding. The two
commands write only under the new workflow root; the host probe reads an
isolated source snapshot, and the writer probe uses its own synthetic workspace.
Evidence is bundle/Codex/root-bound, expires after 24 hours, and
fails closed on tampering or route drift. The host receipt binds the snapshot
manifest ref and digest. Initial `admit` replays that existing create-once
snapshot and requires its manifest to reproduce the current repository state;
it cannot create a missing replacement. Later phases continue to replay the
same immutable snapshot authority but do not compare it to source changes made
by an already applied integration. The isolated snapshot path is never compared
to or replaced by the live checkout path.
Cross-directory source workflows that need a final repository-wide read-only
verifier may use `.` as the single `admission.relevant_roots` value. This makes
an isolated integrated-source snapshot readable; the live repository, `.git`,
`.workflow`, Codex homes, credentials, and control artifacts remain outside the
worker root. It does not make `.` a valid write root, artifact path, or
control-plane path.
The snapshot manifest binds file bytes and modes into each successful read task;
the terminal fence and final replay both recheck the checkout against that
manifest. A tracked worktree deletion is represented as an absent snapshot file,
not treated as an infrastructure error. Source files are copied from no-follow
file descriptors, and canonical repository state is checked before and after
materialization so a pathname replacement cannot expose a foreign target.
When an additional Phase reaches its terminal fence or deterministic crash
reconciliation, resume projection may exclude only that Phase's exact committed
winning plan while revalidating its authority. The persisted plan's canonical
bytes and the expected claim path, workflow/generation identity, predecessor,
authority revision, contention key, and complete canonical claim bytes must all
match. A missing current claim or any other unfinished committed Phase remains a
fail-closed blocker.
The host probe binds only portable Phase-runner capabilities: the OS terminal
barrier plus each routed worker's supervisor request/terminal, exact command and
sanitized environment, canonical rollout, route, permissions, and token usage.
Exact means the complete argv equals the source-owned probe template; terminal
stdout must equal the copied events, and the copied turn context must reproject
from the canonical rollout. The only optional host runtime read is the exact
`codex-resources/zsh/bin/zsh` file derived from the sealed Codex binary; arbitrary
shell or runtime-directory reads remain denied. A request-only crash consumes the
single recovery slot, while pre-receipt derived artifacts may replay only when
their bytes are identical.
It does not self-certify which native child invoked it. Main→clean-Orchestrator
lineage, `fork_turns=none`, and final callback delivery remain a host-owned
post-terminal audit. A run missing that audit may execute isolated phases but
must not claim target Agent Workflow or benchmark/promotion success.

`admit` first copies the exact manifest-bound executable and focused-validator bundle into `runtime-bundle/` with create-once replay, then
commits `workflow.json`. Before every later lifecycle command, the host runs `pinned-runtime` and invokes the returned
absolute `runtime_path`; this preserves active-run behavior across a default-selector rollback or app restart. Missing,
extra, symlinked, or digest-drifted pinned members return `blocked_incompatible_release`. There is no current-runtime or
legacy-writer fallback.
Project-level Git, tests, and builds run as host-owned validation after isolated
integration; source-writer snapshots intentionally receive no `.git` or broad
package-manager/toolchain capability. Their immutable logs may then be supplied
to the independent read-only verifier. `host_validation.py` binds the applied
integration receipt, exact resolved argv and executable digest, cwd, sanitized
environment, stdout/stderr, exit, elapsed time, and before/after repository
evidence into one create-once receipt. Repository evidence includes untracked
file bytes and modes. Replay revalidates the full typed receipt and its exact
spec; an independent verifier may claim a command only when its text and exit
code match one passed host-validation command. General input refs cannot stand
in for command evidence. A passing receipt must cover the entire sealed command
list, and its integration ref must be the latest authoritative applied source
phase before verification. The receipt's canonical source-state digest must also
equal the final verifier snapshot state; an older pre-repair or pre-edit test run
is stale evidence.
The admission baseline is immutable, while the workflow candidate head is
cumulative. Before each writer Phase, the runtime validates the terminal receipt
chain and replays each prior applied bounded patch over the admission snapshot.
The source-head manifest binds no-follow receipt, plan, generation-claim, task-result,
typed-output, host-check, patch, and integration-terminal evidence plus continuous
before/after anchor hashes. Workflow-owned dirty paths may be revisited only when
every selected live read/write dependency equals that derived head; original user
dirt or unexplained cross-anchor edits still fail closed.
New source Phase seals include the selected dependency roots and exact file
manifest. The runtime replays that seal before Phase authority, immediately
before each executor and watchdog launch, and at integration. A pre-launch drift
starts no actor; a later drift cannot reach the shared checkout. These are host
checks and grant the worker no additional read or write permission.
Every production writer schema must require `changed_paths: string[]`, and that
set must equal the host-observed patch before publication or atomic exchange.
All routed output schemas are structurally preflighted before Phase authority or
model launch against [OpenAI's strict Structured Outputs subset](https://developers.openai.com/api/docs/guides/structured-outputs#supported-schemas): an object root,
nested `anyOf`, nullable `[type, null]` unions, complete required object properties,
`additionalProperties:false`, typed array items, type-correct `const`/`enum`, at
most ten object levels, and at most 5,000 total object properties.
Packet, schema, and model-output decoding is strict JSON: Python-only `NaN` and
positive or negative `Infinity` constants are rejected. Patch and integration-
terminal replay walks every path component with `O_NOFOLLOW`; a later safe
reread cannot legitimize an earlier pathname-following read.
Every Python entry point that may execute directly from the pinned directory
disables bytecode emission before importing another bundle member. `__pycache__`
is executable file-set drift and remains a hard failure; it is never ignored or
cleaned in place.
If admission crashes between member writes, the absence of committed `workflow.json` authorizes only exact-byte
completion of that partial pin. Once `workflow.json` exists, missing, extra, or drifted members are incompatible
authority and cannot be repaired in place.

`--auth-source` is a host-owned Codex credential input, not workflow authority and never materialized by the Orchestrator. The host passes its existing Codex auth file path opaquely; the runtime accepts the established `tokens.access_token` or `OPENAI_API_KEY` shape, creates a transient `0600` copy inside the isolated worker `CODEX_HOME`, and scrubs it after the Phase. Never put credentials in the Workflow Brief, plan, packet, fixture, artifact, log, or repository.

The numeric authority revision comes from admitted `workflow.authority.revision`; after a validated amendment, use the deterministic latest revision projected by reconcile. It is not read from `--auth-source`.

Use the CLI help as the exact option authority. Commands are blocking and return after the deterministic operation reaches its own boundary. `run-phase` exits early when every admitted Task is terminal; its timeout is a bottom line for stuck or lost processes, not a mandatory wait.

## Planning and execution

- Materialize one Phase definition and its Task requests from the current receipts.
- Keep Tasks independent within a Phase. The runtime launches routed external workers and waits at one terminal barrier.
- Each Task names exactly one pinned role. The runtime records requested and actual routing evidence.
- Initial external workers also receive a fixed model-visible isolated-worker developer contract. It identifies the
  process as a Task actor rather than Main, explicitly supersedes later host skill-catalog trigger rules unless the
  sealed packet itself names the skill file as a required input, forbids other skill invocation, delegation, polling,
  unrelated inspection, and unrequested lifecycle actions, and permits tools only when the Task acceptance criteria require them. This does not
  pretend the host skill/tool catalog disappeared; it prevents that fixed floor from turning a bounded packet into
  autonomous workflow exploration.
- After the barrier, reduce typed results. Add a later Phase only when the plan, repair, verification, or human gate requires it.
- One original lineage may receive at most one evidence-bound recovery. Successful siblings remain terminal.
- Recovery resumes the exact failed Codex session through the pinned one-shot App Server adapter. The spec binds the
  failed result, causal receipt, prior rollout prefix, route, cwd, permission profile, prompt, schema, adapter, and Codex
  binary. The prompt carries a spec-bound nonce and the returned turn ID becomes a create-once turn claim. A crash after
  raw turn append freshly reprojects output/tokens from the sealed suffix, requires byte equality with any adapter
  terminal, and never starts another recovery turn. Current Codex may persist its typed host preamble as one
  environment-only message, a combined AGENTS+environment message, or two exact `input_text` envelope parts in that
  one host message. The adapter preserves part boundaries, accepts only those complete observed shapes, and then
  requires the nonce-bound explicit prompt to be one separate message containing exactly one matching part.
- Normal amendments apply only at a terminal Phase boundary.

## Finalization

Before `seal-final`, reconcile deterministically and create the independent verification decision required by the final contract. Final sealing validates lineage claims, criteria coverage, routing independence, evidence, findings, typed P2 resolutions, authority, and absence of active attempts. Finalization is create-once and serialized against phase execution, cancellation, amendment, resume, and reconcile.

`final.json` defers both token accounting and completion density to the post-terminal sidecar; the Orchestrator cannot know its still-running terminal completion. After terminal, the host runs `seal-accounting` once with the raw session prefix. Exact App Server evidence requires ordered `turn/started`, token updates, and successful `turn/completed` for the complete turn set. The transaction replays classes from raw tool-call/result/token events, binds a derived projection, verifies the currently executing runtime bundle matches `final.json`, and is idempotent after a lost response. A version-gated Stop-hook transcript remains partial. No path creates a late Orchestrator wake or changes semantic final authority.

Commit, push, publish, deploy, release, and local production remain host-owned actions and require separate human authorization.

Legacy compatibility is read-only: `python3 inspect_legacy.py <legacy-workflow-dir> --allowed-root <legacy-root>` supports
only the frozen v1/v1 and v2/v2 orchestration/state pairs. Directory-FD confinement rejects cross-version, missing,
oversized, FIFO, symlink-ancestor, and embedded traversal inputs without writing or enabling a writer fallback.
Promotion benchmarking uses the separate on-demand `vnext-canary.md` reference; ordinary workflows do not load that harness.
