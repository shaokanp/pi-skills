# Changelog

## Unreleased

- Replaced the fixed-width Swarm Card phase grid with a deterministic,
  CJK-safe per-agent Markdown left rail.
- Removed executor-type display symbols, placed explicit status text beside
  every status symbol, and moved model/effort into italic parentheses.
- Added automatic card scaffolding, exact-token projection, render-hash
  deduplication for event-only updates, structural validation, and regressions.
- Replaced self-declared workflow token totals with exact token-usage v2
  accounting from native Codex and Claude session events.
- Added automatic Codex start snapshots, append-only registration for every
  spawned attempt, runtime child-session discovery, terminal and aborted agent
  totals, Lead counter deltas, source-revalidated token evidence, and
  fail-closed coverage/arithmetic validation.
- Kept legacy v1 estimates readable while rejecting v1 documents that claim
  exact runtime usage without start/end evidence.
- Added an opt-in execution-efficiency contract for native Agent Workflow runs.
- Added isolated Codex/Claude lane contexts, digest-bound dispatch preparation,
  notification-first long-wait telemetry, compact receipts and integration
  indexes, lane-admission and quality gates, per-agent budgets, bounded writer
  reuse, and independent verifier identity validation.
- Added deterministic regressions for busy polling, unresolved timeout, card
  heartbeat, dispatch/output tampering, deterministic lane admission, repair
  affinity, Codex/Claude/manual runners, and legacy v1 compatibility.
- Kept execution efficiency disabled by default so existing v1 workspaces and
  manual simulations require no migration.
- Added opt-in Codex model routing with persisted policy and capability
  snapshots, immutable per-attempt route evidence, and claim-derived verifier
  floors.
- Set `gpt-5.6-terra/high` as the minimum automatic route, reserved Sol and
  xhigh for stronger lanes, and required evidenced escalation for Sol/max.
- Added fail-closed planned and final validation for unavailable routes,
  identity independence, evidence bindings, fallback, retry, and escalation.
- Moved detailed runtime-contract procedures into the artifact reference so the
  `agent-workflow` entrypoint stays within production skill-lint limits.

## 0.3.0 - 2026-07-10

- Added `explain` as a stable, portable skill for evidence-first comprehension
  of technical systems, specs, diffs, workflows, and multi-round progress.
- Preserved its plain-language, diagram-aware explanation behavior while
  removing personal addressees and private project examples from public source.
- Registered `explain` for the same validation, packaging, local production,
  and public release pipeline used by the rest of `pi-skills`.
- Added index-aware pre-commit and ref-aware pre-push safety gates.
- Moved personal denylist values into ignored local configuration while keeping
  generic path and credential rules in portable source.
- Added full-history CI scanning and regression tests for staged secrets,
  removed-but-historical secrets, and first-push history coverage.
- Added Gitleaks as a strict public-publish dependency without coupling it to
  internal local-production releases.
- Removed the legacy `agent-loops` skill alias before public release. The
  canonical skill is `agent-workflow`; existing `agent-loops.*.v1` workflow
  schema identifiers remain supported for artifact compatibility.
- Made the Git tracked tree the exact public distribution surface. Internal
  project overlays now live outside the public repository and are materialized
  only as ignored local files.
- Added an explicit public-file allowlist and Git-file-based packaging so local
  ignored content cannot enter a public archive.
- Added `write-good-goal` as the canonical portable goal-writing skill, based
  on the proven feasibility, Follow-Up Gate, and bounded-round contract.

## 0.2.0 - 2026-07-10

- Made the repository and `agent-workflow` skill portable and public-safe.
- Moved machine-specific production paths into ignored local configuration.
- Added public-content and Git-history scanning for private paths and common
  secret formats, including commit identity metadata.
- Added portable installation, complete preflight, and GitHub Actions checks.
- Kept local production copy-based and backward-compatible with `agent-loops`.
- Declared portable `shared-context` linkage through an external local registry,
  avoiding private Ops paths in public project instructions.
- Added the MIT license and removed release-only documentation from skill
  packages.

## 0.1.0 - 2026-07-09

- Created the `pi-skills` mono repo for curated skill development.
- Projectized the former `agent-loops` skill as `agent-workflow`.
- Added `agent-loops` as a legacy alias that routes old invocations to
  `agent-workflow`.
- Added source validation, packaging, production drift diffing, and local release
  harness scripts.
- Added release documentation and safety gates for local production and future
  GitHub publishing.
