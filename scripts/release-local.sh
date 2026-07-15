#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_CONFIG="${PI_SKILLS_LOCAL_CONFIG:-$ROOT/.pi-skills.local.json}"
SKILL_ID=""
TARGET_ROOT="${PI_SKILLS_TARGET_ROOT:-}"
EXECUTE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --execute)
      EXECUTE=1
      shift
      ;;
    --target-root)
      TARGET_ROOT="${2:-}"
      if [[ -z "$TARGET_ROOT" ]]; then
        echo "--target-root requires a value" >&2
        exit 2
      fi
      shift 2
      ;;
    -*)
      echo "unknown option: $1" >&2
      exit 2
      ;;
    *)
      if [[ -n "$SKILL_ID" ]]; then
        echo "unexpected argument: $1" >&2
        exit 2
      fi
      SKILL_ID="$1"
      shift
      ;;
  esac
done

if [[ -z "$SKILL_ID" ]]; then
  echo "usage: scripts/release-local.sh <skill-id|all> [--target-root path] [--execute]" >&2
  exit 2
fi

config_value() {
  local field="$1"
  python3 - "$LOCAL_CONFIG" "$field" <<'PY'
import json
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.is_file():
    raise SystemExit(0)
with path.open("r", encoding="utf-8") as handle:
    value = json.load(handle).get(sys.argv[2])
if isinstance(value, str) and value.strip():
    print(os.path.abspath(os.path.expanduser(value)))
PY
}

registry_value() {
  local id="$1"
  local field="$2"
  python3 - "$ROOT/registry.json" "$id" "$field" <<'PY'
import json
import sys

registry_path, skill_id, field = sys.argv[1:4]
with open(registry_path, "r", encoding="utf-8") as handle:
    data = json.load(handle)
for skill in data.get("skills", []):
    if skill.get("id") == skill_id:
        value = skill.get(field)
        if value is None:
            raise SystemExit(f"missing field {field!r} for {skill_id}")
        print(value)
        raise SystemExit(0)
raise SystemExit(f"unknown skill id: {skill_id}")
PY
}

release_ids() {
  local requested="$1"
  python3 - "$ROOT/registry.json" "$requested" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    data = json.load(handle)
requested = sys.argv[2]
skills = data.get("skills", [])
by_id = {skill["id"]: skill for skill in skills}
roots = list(by_id) if requested == "all" else [requested]
seen = set()

def visit(skill_id):
    if skill_id in seen:
        return
    if skill_id not in by_id:
        raise SystemExit(f"unknown skill id: {skill_id}")
    for dependency in by_id[skill_id].get("requires", []):
        visit(dependency)
    seen.add(skill_id)
    print(skill_id)

for root in roots:
    visit(root)
PY
}

if [[ -z "$TARGET_ROOT" ]]; then
  TARGET_ROOT="$(config_value target_root)"
fi

if [[ -z "$TARGET_ROOT" ]]; then
  echo "local skills target is not configured" >&2
  echo "pass --target-root, set PI_SKILLS_TARGET_ROOT, or create .pi-skills.local.json" >&2
  exit 2
fi

TARGET_ROOT="$(python3 - "$TARGET_ROOT" <<'PY'
import os
import sys

print(os.path.abspath(os.path.expanduser(sys.argv[1])))
PY
)"

if [[ "$TARGET_ROOT" == "/" || "$TARGET_ROOT" == "$ROOT" || "$TARGET_ROOT" == "$ROOT/"* ]]; then
  echo "unsafe target root: $TARGET_ROOT" >&2
  exit 2
fi

if ! command -v rsync >/dev/null 2>&1; then
  echo "rsync is required for local release" >&2
  exit 1
fi

# One exact-tree preflight authorizes both dry-run and execute. A second call
# reuses the same receipt instead of rerunning the release suite.
bash "$ROOT/scripts/preflight.sh"

release_one() {
  local id="$1"
  local source_rel target_name source_dir target_dir
  source_rel="$(registry_value "$id" source)"
  target_name="$(registry_value "$id" production_target)"
  source_dir="$ROOT/$source_rel/"
  target_dir="$TARGET_ROOT/$target_name/"

  if [[ "$EXECUTE" -eq 0 ]]; then
    echo "dry-run local release: $source_rel -> $target_dir"
    if [[ ! -d "$TARGET_ROOT/$target_name" ]]; then
      echo "target does not exist; execute would create it and install:"
      python3 - "$source_dir" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
paths = (
    item
    for item in root.rglob("*")
    if item.is_file()
    and "__pycache__" not in item.parts
    and item.name != ".DS_Store"
    and item.suffix != ".pyc"
)
for path in sorted(paths):
    print(f"  + {path.relative_to(root)}")
PY
    else
      rsync -anic --delete --omit-dir-times \
        --delete-excluded \
        --exclude ".DS_Store" \
        --exclude "__pycache__" \
        --exclude "*.pyc" \
        "$source_dir" "$target_dir"
    fi
    return
  fi

  mkdir -p "$target_dir"
  rsync -a --delete --delete-excluded --omit-dir-times \
    --exclude ".DS_Store" \
    --exclude "__pycache__" \
    --exclude "*.pyc" \
    "$source_dir" "$target_dir"

  bash "$ROOT/scripts/diff-production.sh" "$id" "$TARGET_ROOT" >/dev/null
  echo "released $id to $target_dir"
}

while IFS= read -r id; do
  release_one "$id"
done < <(release_ids "$SKILL_ID")
