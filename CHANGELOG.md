# Changelog

## Unreleased

<!-- pi-skills:unreleased id=explain version=1.0.1 -->
<!-- pi-skills:unreleased id=write-good-goal version=1.0.1 -->

### Repository maintenance

- Bound one complete repository preflight to the exact public-tree, toolchain,
  local-policy, and validator digest. Pre-push and local release reuse that receipt,
  while pre-commit remains a fast index/static gate.
- Added a portable/strict-local session doctor, fail-closed new-skill scaffold,
  bilingual guide checks, and deterministic registry/changelog version markers.
- Kept Gitleaks in the public publish gate and ignored runtime cache noise when
  checking source-to-production parity.
- Scoped the Gitleaks directory scan to an archive of tracked `HEAD`, preventing
  ignored local worktrees from blocking an otherwise clean public release while
  retaining tracked-tree and Git-history coverage.
- Reworked all three skill descriptions around distinct trigger branches and
  routing boundaries between explanation, goal drafting, multi-agent execution,
  and ordinary direct work.
- Reordered the bilingual guides so first-time readers see when to use each skill,
  what it produces, what it does not do, and how to start before deep mechanics.
- Added a repository-level skill chooser, defined maturity and preflight claims,
  and documented the clone-and-run-from-root installation prerequisite.
- Added Traditional Chinese and English guides for `explain` and `write-good-goal`.
- Replaced the event-range-dependent Gitleaks Action wrapper with the official
  versioned container and an explicit full-`HEAD` history scan.

## 1.0.0 - 2026-07-15

<!-- pi-skills:release id=agent-workflow version=1.0.0 -->

- Introduced Agent Workflow as a native thin-team design. Explicit invocation
  makes the current agent the Orchestrator, launches fresh specialists through
  native collaboration tools, and parallelizes independent work.
- Defined dynamic team design, disjoint writer ownership, evidence-shaped
  challenge, original-owner bounded repair, and mandatory fresh read-only
  verification for source changes. Model routing remains an optional host
  capability rather than an external lifecycle dependency.
- The public 1.0 package is intentionally small: its native instruction
  contract, host metadata, bilingual guides, eval corpus, one repository
  contract test, and license.
- Added the normative architecture and implementation documents for one-prompt
  native operation, plus a focused native-team behavior/evaluation corpus.

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
- Declared portable `shared-context` linkage through an external local registry,
  avoiding private Ops paths in public project instructions.
- Added the MIT license and removed release-only documentation from skill
  packages.

## 0.1.0 - 2026-07-09

- Created the `pi-skills` mono repo for curated skill development.
- Projectized the former `agent-loops` skill as `agent-workflow`.
- Added source validation, packaging, production drift diffing, and local release
  harness scripts.
- Added release documentation and safety gates for local production and future
  GitHub publishing.
