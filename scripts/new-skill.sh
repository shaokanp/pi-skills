#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKILL_ID=""
DISPLAY_NAME=""
VERSION="0.1.0"
STATUS="experimental"

usage() {
  cat <<'EOF'
usage: scripts/new-skill.sh <id> [options]

Create a registered, intentionally incomplete public skill scaffold. The new
skill will fail validation until SKILL.md, README.md, and README.en.md contain
completed non-placeholder content.

Options:
  --display-name <name>  Human-readable name. Defaults to title-cased <id>.
  --version <version>    Initial skill version. Defaults to 0.1.0.
  --status <status>      stable or experimental. Defaults to experimental.
  --help                 Show this help.

The command edits only the source tree, registry.json, and CHANGELOG.md. It
does not install, release, commit, push, or modify production.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --display-name)
      DISPLAY_NAME="${2:-}"
      [[ -n "$DISPLAY_NAME" ]] || { echo "--display-name requires a value" >&2; exit 2; }
      shift 2
      ;;
    --version)
      VERSION="${2:-}"
      [[ -n "$VERSION" ]] || { echo "--version requires a value" >&2; exit 2; }
      shift 2
      ;;
    --status)
      STATUS="${2:-}"
      [[ -n "$STATUS" ]] || { echo "--status requires a value" >&2; exit 2; }
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    -*)
      echo "unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
    *)
      [[ -z "$SKILL_ID" ]] || { echo "unexpected argument: $1" >&2; exit 2; }
      SKILL_ID="$1"
      shift
      ;;
  esac
done

[[ -n "$SKILL_ID" ]] || { usage >&2; exit 2; }
[[ "$SKILL_ID" =~ ^[a-z0-9]+(-[a-z0-9]+)*$ ]] || { echo "invalid skill id: $SKILL_ID" >&2; exit 2; }
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.-]+)?$ ]] || { echo "invalid version: $VERSION" >&2; exit 2; }
[[ "$STATUS" == "stable" || "$STATUS" == "experimental" ]] || { echo "status must be stable or experimental" >&2; exit 2; }

if [[ -z "$DISPLAY_NAME" ]]; then
  DISPLAY_NAME="$(python3 - "$SKILL_ID" <<'PY'
import sys

print(" ".join(part.capitalize() for part in sys.argv[1].split("-")))
PY
)"
fi
[[ "$DISPLAY_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9\ .\,\&\(\)\'-]*$ ]] || { echo "invalid display name" >&2; exit 2; }

git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1 || { echo "repository root is not a Git worktree" >&2; exit 1; }

mkdir -p "$ROOT/.cache"
STAGE="$(mktemp -d "$ROOT/.cache/new-skill.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT

python3 - "$ROOT" "$STAGE" "$SKILL_ID" "$DISPLAY_NAME" "$VERSION" "$STATUS" <<'PY'
import json
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
stage = Path(sys.argv[2])
skill_id, display_name, version, status = sys.argv[3:]
registry_path = root / "registry.json"
changelog_path = root / "CHANGELOG.md"
registry = json.loads(registry_path.read_text(encoding="utf-8"))
skills = registry.get("skills")
if not isinstance(skills, list):
    raise SystemExit("registry skills must be a list")
if any(skill.get("id") == skill_id for skill in skills):
    raise SystemExit(f"skill id already registered: {skill_id}")
if (root / "skills" / skill_id).exists():
    raise SystemExit(f"skill directory already exists: skills/{skill_id}")

changelog = changelog_path.read_text(encoding="utf-8")
marker = f"<!-- pi-skills:unreleased id={skill_id} version={version} -->"
if marker in changelog:
    raise SystemExit(f"changelog marker already exists: {skill_id} {version}")
lines = changelog.splitlines(keepends=True)
for index, line in enumerate(lines):
    if line.rstrip("\n") == "## Unreleased":
        insert_at = index + 1
        break
else:
    raise SystemExit("CHANGELOG.md is missing the Unreleased heading")

skills.append(
    {
        "id": skill_id,
        "display_name": display_name,
        "description": "TODO: describe the skill trigger, outcome, and boundaries.",
        "source": f"skills/{skill_id}",
        "production_target": skill_id,
        "version": version,
        "status": status,
        "visibility": "public",
    }
)
(stage / "registry.json").write_text(json.dumps(registry, indent=2) + "\n", encoding="utf-8")
lines.insert(insert_at, marker + "\n")
(stage / "CHANGELOG.md").write_text("".join(lines), encoding="utf-8")
PY

mkdir -p "$STAGE/skill"
cat >"$STAGE/skill/SKILL.md" <<EOF
---
name: $SKILL_ID
description: [TODO: describe the skill trigger, outcome, and boundaries.]
---

<!-- pi-skills:scaffold-incomplete -->
# [TODO: $DISPLAY_NAME]

[TODO: write the portable skill contract.]
EOF
cat >"$STAGE/skill/README.md" <<EOF
# [TODO: $DISPLAY_NAME]

<!-- pi-skills:scaffold-incomplete -->
[TODO: write the Traditional Chinese guide.]
EOF
cat >"$STAGE/skill/README.en.md" <<EOF
# [TODO: $DISPLAY_NAME]

<!-- pi-skills:scaffold-incomplete -->
[TODO: write the English guide.]
EOF

mv "$STAGE/skill" "$ROOT/skills/$SKILL_ID"
mv "$STAGE/registry.json" "$ROOT/registry.json"
mv "$STAGE/CHANGELOG.md" "$ROOT/CHANGELOG.md"

echo "created incomplete scaffold for $SKILL_ID"
echo "complete skills/$SKILL_ID/{SKILL.md,README.md,README.en.md} before validation"
