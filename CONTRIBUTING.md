# Contributing

`pi-skills` treats its Git tracked tree as the exact public distribution
surface. A file that is committed here must be safe and useful for every public
clone.

## Source Rules

- Edit canonical skill source only under `skills/<skill-id>/`.
- Register every public skill in `registry.json`.
- Every registered skill must include `SKILL.md`, `README.md`, and
  `README.en.md`. The guides must start with meaningful H1 headings and cannot
  contain scaffold placeholders.
- Do not add machine-specific paths, credentials, private workspace policy,
  runtime state, logs, or personal project metadata.
- Do not maintain a second hand-edited public copy of a skill.
- Keep generated archives under ignored `dist/`.

## New Skills And Versions

Start a new public skill with:

```bash
bash scripts/new-skill.sh <id>
```

The scaffold uses structured `registry.json` editing and adds an explicit
`<!-- pi-skills:unreleased id=<id> version=<version> -->` marker. It is
intentionally incomplete, so validation fails until all three skill documents
are completed. The command never installs, releases, commits, or pushes.

When a maintainer decides a behavior change deserves a version change, update
the registry version and keep exactly one matching marker for that skill and
version: either the current `unreleased` marker under `## Unreleased`, or a
`release` marker under a dated release heading. Validation checks that contract
but cannot determine semantic behavior changes from Git history; that decision
remains a maintainer responsibility.

## Validate

Install the repository hooks once:

```bash
brew install gitleaks
bash scripts/install-hooks.sh
```

During development, run focused tests. After the source tree is frozen, run one
complete preflight:

```bash
bash scripts/preflight.sh
```

For a non-mutating session check, run `bash scripts/doctor.sh`. Local
maintainers should also run `bash scripts/doctor.sh --strict-local` at session
start and after local release; it requires local config, hooks, and production
drift to be clean. Gitleaks remains part of the separate public publish gate.

Before publishing:

```bash
bash scripts/publish-preflight.sh
```

The preflight writes a Git-metadata receipt bound to the exact public tree,
toolchain, local policy, and validator. The pre-commit hook stays fast: staged
diff/static and public-index checks only. The pre-push and local-release paths
run or reuse the exact receipt instead of rerunning the same suite. Pre-push
still scans exact refs and runs Gitleaks. GitHub CI remains an independent remote
boundary and repeats public-tree, package, current-branch history, and secret
checks.

## Local Installation

Use `.pi-skills.local.example.json` as a portable template or pass an explicit
target root. The actual `.pi-skills.local.json` is ignored and must never be
committed.

```bash
bash scripts/release-local.sh all
bash scripts/release-local.sh all --execute
bash scripts/diff-production.sh all
```
