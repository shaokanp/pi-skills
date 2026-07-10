# Release Guide

## 1. Validate And Package

Run the full public preflight:

```bash
bash scripts/preflight.sh
```

This runs structural skill validation, scans the current publishable tree,
packages every registered skill under `dist/` with a SHA-256 checksum, and
checks generated artifact metadata for private paths.

## 2. Configure Local Production

Copy the example configuration and set the skills root used by the local agent
runtime:

```bash
cp .pi-skills.local.example.json .pi-skills.local.json
```

The local file is ignored by Git. A one-off target may instead be supplied with
`PI_SKILLS_TARGET_ROOT` or `--target-root`.

## 3. Release Locally

Review the dry-run before writing:

```bash
bash scripts/release-local.sh all
bash scripts/release-local.sh all --execute
bash scripts/diff-production.sh all
```

The release is copy-based and uses deletion within each selected target skill
directory so installed content exactly matches source. It does not remove other
skills from the target root.

## 4. Verify Local Loading

Confirm that each installed `SKILL.md` exists, production diffing passes, and
bundled scripts run from the installed directory. Agent runtimes may require a
new session before refreshed skill metadata appears in their skill catalog.

## 5. Publish

### Configure private markers

Keep machine-specific denylist values beside `target_root` in the ignored local
configuration:

```json
{
  "target_root": "/absolute/path/to/your/skills",
  "validator": null,
  "private_markers": [
    "YOUR_PRIVATE_WORKSPACE_NAME",
    "YOUR_PERSONAL_HANDLE",
    "YOUR_PRIVATE_HOME_PATH"
  ]
}
```

These values are loaded by local validation but are never packaged or committed.
CI may receive the same newline-separated values through the optional
`PI_SKILLS_PRIVATE_MARKERS` repository secret.

### Install the local hooks

```bash
brew install gitleaks
bash scripts/install-hooks.sh
```

- `pre-commit` scans the staged Git index and blocks staged content that a clean
  working-tree scan could miss.
- `pre-push` requires a clean tree, runs the complete package preflight, and
  scans the exact commits and tags from Git's pre-push ref stream.
- `publish-preflight` also runs Gitleaks over the worktree and local Git refs.
  The dependency is isolated to public publishing; local production release
  does not require it.

### Run the publish gate manually

```bash
bash scripts/publish-preflight.sh
```

Without `--push-refs`, the public-content scanner checks every commit reachable
from the current `HEAD`; unrelated unpublished local branches are excluded. This
is the required check before creating the first public remote. A cleanup commit
is not sufficient when an ancestor contains private content; sanitize or
replace the unpublished branch history, then rerun the gate.

Review the exact commit and tag intended for publication. Pushing a branch,
tag, or GitHub Release remains a separate external approval gate. Git hooks are
defense in depth, not a remote security boundary; Git supports bypassing local
hooks, so public CI fetches full history and reruns the scanner.

## Scanner Coverage

The public-safety scanner checks:

- portable source files, including untracked but non-ignored files;
- the staged Git index;
- complete current-`HEAD` ancestry or only the refs supplied by `pre-push`;
- commit and annotated-tag metadata;
- generated release archives and unsafe archive paths or links;
- absolute user-home paths, configured local private markers, common credential
  formats, assigned credential-like values, and sensitive file/path names.
- Gitleaks provider rules, entropy checks, decoded content, and nested archives
  during the strict publish gate.

No pattern scanner proves that arbitrary prose is non-sensitive. Keep GitHub
secret scanning and push protection enabled, review the first public commit
manually, and add newly discovered private markers to the ignored local config.

## Rollback

Local rollback is source-driven: check out the previously accepted source
commit, run the same preflight, and release that version with `--execute`.
Installed skill directories are not edited manually.
