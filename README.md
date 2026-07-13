# pi-skills

`pi-skills` is a curated monorepo of portable instruction contracts for agent
runtimes. Each skill tells a compatible agent when to use a specialized process
and how to carry it out. A skill does not add permissions, tools, subagents, a
scheduler, or a background daemon that the runtime does not already provide.

## Choose A Skill

| Skill | Status | Use when | You get | Not for |
| --- | --- | --- | --- | --- |
| `explain` | Stable | You need to understand a non-trivial system, spec, diff, artifact, or multi-round history. | An evidence-backed explanation of structure, mechanism, status, and boundaries. | Correctness review or implementation. |
| `agent-workflow` | Experimental | You want the current runtime to coordinate an agent team, subagents, or a gated multi-round loop. | Planner-selected lanes and durable state. A passing run delivers the work; an early stop records the proven state, blocker, and resume condition. | Ordinary single-agent work or an unattended daemon. |
| `write-good-goal` | Stable | You want to create, refine, or audit a paste-ready coding-agent goal. | A bounded goal contract with achievable Done criteria and honest follow-up or human gates. | Executing the goal or writing a full project plan. |

`Stable` means the public contract is intended to remain backward-compatible.
`Experimental` means the contract is useful but may still change between minor
releases. Repository preflight validates structure, available regressions,
packaging, and public-safety rules; neither label nor preflight proves semantic
correctness for every task.

`agent-workflow` is the canonical skill id. The internal `.workflow` schema
keeps its existing `agent-loops.*` prefix for backward compatibility.

## Skill Guides

| Skill | 繁體中文 | English |
| --- | --- | --- |
| `explain` | [Guide](skills/explain/README.md) | [Guide](skills/explain/README.en.md) |
| `agent-workflow` | [Guide](skills/agent-workflow/README.md) | [Guide](skills/agent-workflow/README.en.md) |
| `write-good-goal` | [Guide](skills/write-good-goal/README.md) | [Guide](skills/write-good-goal/README.en.md) |

## Install

Prerequisites: Git, Bash, Python 3, and `rsync`. Gitleaks is required only for
the public-publishing checks described below.

Clone the repository and run the commands from its root:

```bash
git clone https://github.com/shaokanp/pi-skills.git
cd pi-skills
```

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
2. Use `bash scripts/new-skill.sh <id>` for a new registered scaffold. It is
   deliberately incomplete and cannot pass validation until all three skill
   documents are completed.
3. Update `registry.json` and `CHANGELOG.md` when behavior or version changes.
   Each current skill version needs exactly one deterministic changelog marker:
   an `unreleased` marker under `## Unreleased`, or a `release` marker under a
   dated release heading. The validator cannot infer behavior changes from a
   Git diff, so maintainers must make that version decision explicitly.
4. During development, run focused tests for the seam being changed. After the
   source tree is frozen, run the complete preflight once:

```bash
bash scripts/preflight.sh
```

For a quick session check that never installs, releases, commits, pushes, or
changes production, run:

```bash
bash scripts/doctor.sh
```

Local maintainers can require the ignored local configuration, active hooks,
and a passing source-to-production comparison with
`bash scripts/doctor.sh --strict-local`. Run it at session start and again after
local release; use `publish-preflight.sh` for the Gitleaks-backed publish gate.

The preflight validates every registered skill, scans the publishable tree for
private paths and common secret formats, builds release archives under `dist/`,
and scans generated artifact metadata before it can be published. A passing
receipt is stored in the worktree's Git metadata and binds the exact public
tree, toolchain, local policy, and validator. Repeating pre-push or local-release
steps for the same fingerprint reuses that receipt; any relevant byte or
environment change invalidates it.

Heavy Agent Workflow validation is executed through a lease-bound temporary
root. Local macOS runs default to `/Volumes/OWC-4TB/tmp/pi-skills` only after a
device-identity mount check; CI uses its explicit `RUNNER_TEMP`, and maintainers
may set an existing `PI_SKILLS_TMP_ROOT`. If no policy-managed root is available,
validation fails closed instead of silently filling the boot disk. See
[`docs/agent-workflow-canary-storage.md`](docs/agent-workflow-canary-storage.md).

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

The pre-commit hook runs only fast diff/static and staged-index safety checks.
The pre-push hook runs or reuses the complete preflight, then reads Git's
ref update stream and scans the commits and annotated tags actually being sent;
on the first push to an empty remote, that means the complete reachable history.
The strict publish gate also runs Gitleaks against an archive of the tracked
`HEAD` distribution and full local history. Ignored local workspaces are outside
the public surface. Gitleaks is intentionally required only for public
publishing, not for internal `release-local.sh` use.

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
