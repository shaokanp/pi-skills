# Contributing

`pi-skills` treats its Git tracked tree as the exact public distribution
surface. A file that is committed here must be safe and useful for every public
clone.

## Source Rules

- Edit canonical skill source only under `skills/<skill-id>/`.
- Register every public skill in `registry.json`.
- Do not add machine-specific paths, credentials, private workspace policy,
  runtime state, logs, or personal project metadata.
- Do not maintain a second hand-edited public copy of a skill.
- Keep generated archives under ignored `dist/`.

## Validate

Install the repository hooks once:

```bash
brew install gitleaks
bash scripts/install-hooks.sh
```

Before committing:

```bash
bash scripts/preflight.sh
```

Before publishing:

```bash
bash scripts/publish-preflight.sh
```

The pre-commit hook scans the Git index. The pre-push hook requires a clean
tree, rebuilds public packages, scans the exact refs being pushed, and runs
Gitleaks. GitHub CI repeats the public-tree, package, current-branch history,
and secret checks.

## Local Installation

Use `.pi-skills.local.example.json` as a portable template or pass an explicit
target root. The actual `.pi-skills.local.json` is ignored and must never be
committed.

```bash
bash scripts/release-local.sh all
bash scripts/release-local.sh all --execute
bash scripts/diff-production.sh all
```
