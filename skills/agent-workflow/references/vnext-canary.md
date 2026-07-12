# Agent Workflow vNext Canary Contract

Use this reference only while building or running the vNext candidate benchmark. Ordinary workflow runs do not
load it.

## Frozen inputs

- Corpus: `fixtures/vnext/canary/corpus.v1.json`
- Hidden checks: `fixtures/vnext/canary/hidden-checks.v1.json`
- Immutable digest seal: `fixtures/vnext/canary/seal.v1.json`
- Pre-result run-seal schema: `fixtures/vnext/canary/run-seal.schema.v1.json`
- Each workload runs as at least five legacy/vNext pairs.
- Seal the candidate digest, repository fixture, Codex/host version, models, reasoning effort, capacity, paired order,
  blind labels, reviewer rubric, and hidden-check digests before revealing vNext results.

The release suite verifies both exact fixture byte digests against the versioned seal. Any corpus or rubric change
requires a new corpus, hidden-check manifest, and seal version together.

The fixture seal does not stand in for a run seal. Before the first vNext result, create one immutable run seal from
the schema above containing the candidate, repository, host, models, reasoning, capacity, paired order, blind labels,
and rubric. The source document omits `sealed_at`, `host_authority_id`, `results_ref`, `freeze_path`, and
`repository_root`; `seal-run` writes those fields itself after proving the workspace
contains only the six sealed authority inputs. Every raw execution/reviewer session must start at or after that time
and every receipt, review, and hidden evidence record binds the resulting run-seal digest. Seal publication holds an
exclusive authority transaction, rescans after create-once publication, and is idempotent after a lost response.
A missing or late run seal makes that benchmark ineligible for promotion.

Use the deterministic host commands:

```text
python3 scripts/run_vnext_canary.py create-freeze --repo <repo> --output <freeze.json>
python3 scripts/run_vnext_canary.py verify-freeze --repo <repo> --freeze <freeze.json>
python3 scripts/run_vnext_canary.py seal-run --workspace <canary> --repo <repo> --freeze <freeze.json> --source <run-seal-source.json> --results-ref results.json --output run-seal.json
python3 scripts/run_vnext_canary.py seal-results --workspace <canary> --run-seal <canary>/run-seal.json --draft <canary>/results-draft.json --seal-output host/evidence-seal.json --results-output results.json
python3 scripts/run_vnext_canary.py evaluate --workspace <canary> --run-seal <canary>/run-seal.json --results <canary>/results.json --output evaluation.json
```

The promotion freeze is one exact manifest over runtime executables, candidate instruction, protocol fixtures,
canary schemas/corpus/checks, and legacy-reader fixtures. It exposes distinct runtime and semantic bundle digests;
changing candidate semantics invalidates the freeze even when Python bytes are unchanged.

`seal-run` creates a 0600 per-run replay key in a derived host control root outside the worker workspace and exposes
only its digest as `host_authority_id`. The key is retained with that run until host-owned archival/cleanup so later
evaluation remains replayable; workers never receive the parent/control root. In the same transaction, `seal-run`
copies every frozen executable and authority file into an exact-byte, read-only host-side repository. Qualification
executes only from that immutable copy, never from the mutable source checkout. `seal-results` is the only final
results writer: immediately before qualification it re-verifies current repository bytes against the exact freeze
recorded by `seal-run`, then replays the unsigned draft and HMAC-seals the exact canonical results core, whose refs and
digests transitively bind raw sessions, App events, snapshots, hidden proofs, reviews, and verifier evidence. Ordinary
`evaluate` rejects unsigned or post-seal-modified results. This is a short-lived script boundary, not a runtime service.
The qualification receipt also carries its own host-authority HMAC. If the process crashes after qualification or host
seal publication, a retry verifies and reuses the exact receipt instead of rerunning timing-variable tests.

The run seal binds real label-map, rubric, repository fixture, Codex identity, host profile, and capacity evidence by
safe relative ref and digest. Each pair's execution order is derived from the sealed seed, candidate bundle, workload,
and trial. Scores and preference come from a digest-bound blind-review artifact whose qualified top-model raw session
records the exact launch packet and terminates with the canonical decision. That packet binds the A/B output digests,
rubric, and exact hidden-evidence set, so result authors cannot self-label it. Completion count, total tokens, and
latency are replayed from raw Codex sessions plus a host-export receipt. The run seal pins the supported Codex version
and App Server protocol-schema digest. Coordinator token accounting sums each App Server `last` response usage and
requires the cumulative `total` to equal that sum at the exact terminal turn. Coordinator completion count comes from
canonical raw-session boundaries whose cumulative delta exactly equals `last_token_usage`; repeated or equal counters
do not create completions. Each workload seals a minimum worker-session floor. Every session binds a canonical
create-once launch packet (pair, variant, task, role, route, transport, and full prompt). Pinned Codex may persist one
environment-owned preamble containing either the complete `<environment_context>...</environment_context>` envelope
alone, or a complete `# AGENTS.md instructions for ... <INSTRUCTIONS>...</INSTRUCTIONS>` envelope followed by that
environment envelope. Only the final explicit
user message is launch-authoritative; it must consist of exactly one `input_text` part matching the sealed prompt.
Unterminated envelopes, trailing text outside either envelope, additional messages, and extra explicit content parts
fail closed. The host creates a random per-turn `CODEX_HOME/tmp/arg0/codex-arg0<token>` basename only at launch time.
Every session launch packet therefore pre-binds the canonical preamble digest (or the empty digest), normalizing only
that basename to `codex-arg0&lt;volatile&gt;`; its exact stable path prefix and every other preamble byte remain bound.
Reviewer and verifier launch packets use the same rule. Exact unnormalized raw/native copies remain canonical
post-launch evidence and must byte-match their export receipts. The host launcher must additionally create a
canonical `agent-workflow.canary-host-launch-manifest.v1` for each variant. It binds the run-seal digest, pair, variant,
workspace instance, `host_authority_id`, runtime bundle digest, and an ordered `launches` array. Each launch records
`ordinal`, `session_id`, `attempt_ordinal`, `turn_id`, `task_id`, `role`, `transport`, `launch_ref`, and `launch_sha256`.
The receipt stores each unique session once and records an exact-session recovery as an ordered `continuations` entry.
The manifest's ordered set must equal every coordinator/worker/verifier attempt in the variant receipt exactly; a worker floor is not permission
to omit extra launched sessions or selectively add a verifier. The raw terminal token breakdown for every listed
session must equal the sum of its native attempt breakdowns field-for-field. vNext initial workers use pinned-version
terminal `codex exec --json`; an exact-session recovery uses App Server and proves its cumulative total equals the prior
digest-bound breakdown plus only the recovery turn's `last` usage. Legacy workers use App Server. Tokens count once per
attempt and latency spans first session start through the last continuation terminal.
Token-update count, App turns, and model completions cannot stand in for one another. Every AW-H
check uses its own deterministic validator ID and a per-subject record binding check/workload/trial/variant, exact
variant receipt, every inspected evidence digest, check-specific replay observations, and derived pass/fail. Variant
receipts bind canonical typed contract evidence for acceptance commands, repository/write scopes, terminal
reconciliation, capability denials, lineage recovery, watchdog/post-reap state, artifact replay, and Main delivery.
`AW-H003` is deliberately comparative: a legacy-only completion-density failure records the defect the candidate is
meant to remove and does not count as a candidate correctness regression. Any vNext `AW-H003` failure, any other vNext
hidden failure, or any non-`AW-H003` legacy failure still fails correctness before performance is evaluated.
Write roots, scopes, and changed paths are component-safe relative paths; traversal and prefix overlap fail. Source
verification launch pre-binds the exact integration/output digests, starts strictly after typed integration completion,
and its terminal decision must approve both. Watchdog evidence contains unique process/PGID receipts with terminal,
reaped, and log-digest authority; missing, duplicated, or non-reaped receipts fail.
Caller-authored generic `facts` or pass status are forbidden. Preliminary replay recomputes the record; qualified
replay additionally requires the exact sorted record in the host-HMAC qualification receipt plus its frozen command
records. Host qualification executes the frozen validator scripts themselves
and records their script, stdout, and stderr digests. Blind review packets inline the readable, digest-bound A/B output
strings, rubric JSON, and pair-local hidden-proof JSON. The independent verifier receives a bounded digest index plus
reader refs to all full proofs in its read-only workspace; deterministic replay requires exact 320-proof coverage and
byte equality with the authority refs. Each P2 reverify or promotion-gate record binds the exact digest
of the corresponding qualification command record. Workspace copies of Codex raw sessions, native events, repository
snapshots, and hidden proofs must byte-match their run-specific canonical host stores outside worker roots. Those
stores must be host-owned mode 0700, and every persisted worker/reviewer permission profile must be managed,
restricted, and non-overlapping with every canonical store. A distinct
qualified top-model verifier must accept the exact
hidden-evidence digest set in its own terminal session. The evaluator requires the exact 25-pair schedule; P0/P1 must
be empty and each P2 must satisfy its discriminated repair/rationale/gate evidence schema. `blocked_external` cannot
promote.

## Pairing

Run each pair from the same disposable repository snapshot and host configuration. Randomize legacy/vNext order
using the only accepted seed, `sha256(corpus_id || semantic_bundle_digest)`. Blind labels are separately derived and
balanced from that authority rather than caller-chosen. Variant receipts bind the same per-pair repository snapshot,
distinct workspace instances, host/capacity digests, and non-overlapping timestamps that prove actual order. Never
reuse a mutated workspace between trials.

## Correctness hard gate

Stop promotion on any hard-invariant or authority failure, any hidden-contract regression, any open P0/P1, or a
blind correctness regression. Score blind outputs from 0–4 for correctness, evidence, and completeness. vNext's
aggregate median may not be below legacy on any dimension, and no workload may have a majority of paired reviews
prefer legacy correctness.

## Performance and token gates

Only compare efficiency after correctness passes.

- Baseline noise fraction: `1.4826 × MAD(legacy trials) / median(legacy trials)`.
- Median coordinator completions: at least 50% lower across workloads sealed as `coordination_heavy: true`.
- Median total tokens: reduce by at least `max(20%, 2 × noise fraction)`.
- Wall latency: compute each pair's vNext/legacy ratio; each workload median must be at most 1.10 and the median of all
  paired ratios must be at most 1.00. Never divide two unpaired medians.
- Report P95 for tokens, coordinator completions, and latency, but do not use it as a hard gate with this sample size.

Keep raw runtime evidence, task/phase artifacts, blinded outputs, grades, timing, token accounting, and environment
receipts. Do not move thresholds after results are visible.
