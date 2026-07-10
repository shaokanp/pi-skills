# pi-skills

`pi-skills` is a curated monorepo of portable agent skills. The repository is
the single source of truth: the same validated skill directories are installed
locally and published for other people to use.

## Skills

| Skill | Status | Purpose |
| --- | --- | --- |
| `explain` | Stable | Evidence-first explanations of technical systems, specs, diffs, workflows, and multi-round progress. |
| `agent-workflow` | Experimental | Planner-first multi-agent workflow harness with durable state, review, challenge, verification, and iteration. |
| `write-good-goal` | Stable | Concise, feasible agent goals with auditable completion and follow-up gates. |

`agent-workflow` is the canonical skill id. The internal `.workflow` schema
keeps its existing `agent-loops.*` prefix for backward compatibility.

## Install

Choose the skills directory used by your agent runtime and run a dry-run first:

```bash
bash scripts/install-skill.sh explain \
  --target-root "${CODEX_HOME:-$HOME/.codex}/skills"

bash scripts/install-skill.sh agent-workflow \
  --target-root "${CODEX_HOME:-$HOME/.codex}/skills"

bash scripts/install-skill.sh write-good-goal \
  --target-root "${CODEX_HOME:-$HOME/.codex}/skills"
```

Apply the installation after reviewing the diff:

```bash
bash scripts/install-skill.sh agent-workflow \
  --target-root "${CODEX_HOME:-$HOME/.codex}/skills" \
  --execute
```

For a Claude Code skills directory, pass that directory explicitly as
`--target-root`. Install `all` to install every registered canonical skill.

## Develop

1. Edit `skills/<skill-id>/`.
2. Update `registry.json` and `CHANGELOG.md` when behavior or version changes.
3. Follow `CONTRIBUTING.md` and run the complete preflight:

```bash
bash scripts/preflight.sh
```

The preflight validates every registered skill, scans the publishable tree for
private paths and common secret formats, builds release archives under `dist/`,
and scans generated artifact metadata before it can be published.

## Local Production

Local production is an explicit copy, not a symlink. Create an ignored local
configuration from the example:

```bash
cp .pi-skills.local.example.json .pi-skills.local.json
```

Set `target_root` to the skills directory used on the current machine, then run:

```bash
bash scripts/release-local.sh all
bash scripts/release-local.sh all --execute
bash scripts/diff-production.sh all
```

CLI `--target-root` overrides `PI_SKILLS_TARGET_ROOT`, which overrides the local
configuration file.

## Publish

Install the repository hooks and configure `private_markers` in the ignored
`.pi-skills.local.json` before committing:

```bash
brew install gitleaks
bash scripts/install-hooks.sh
```

Run the strict publish gate before the first public push:

```bash
bash scripts/publish-preflight.sh
```

The pre-commit hook scans the staged Git index. The pre-push hook reads Git's
ref update stream and scans the commits and annotated tags actually being sent;
on the first push to an empty remote, that means the complete reachable history.
The strict publish gate also runs Gitleaks against the worktree and full local
history. Gitleaks is intentionally required only for public publishing, not for
internal `release-local.sh` use.

History failures must be resolved before publishing. Adding a cleanup commit
does not remove sensitive content from older commits. The local denylist remains
ignored, while the same portable skill source can still be copied into an
internal production skills root through `release-local.sh`.

Public repositories should enable GitHub secret scanning and push protection in
addition to the repository checks. The server-side CI fetches full history and
runs the history scanner again because local Git hooks can be bypassed with
`--no-verify`.

## Repository Layout

```text
skills/                 Portable skill source
scripts/                Validation, packaging, install, and release harness
registry.json           Skill ids, versions, status, and release targets
public-files.json       Explicit public tracked-tree allowlist
.github/workflows/      Public CI validation
```

## License

MIT. See `LICENSE`.
