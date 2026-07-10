#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_CONFIG="${PI_SKILLS_LOCAL_CONFIG:-$ROOT/.pi-skills.local.json}"
SKILL_ID="${1:-}"
TARGET_ROOT="${2:-${PI_SKILLS_TARGET_ROOT:-}}"

if [[ -z "$SKILL_ID" ]]; then
  echo "usage: scripts/diff-production.sh <skill-id|all> [target-root]" >&2
  exit 2
fi

if [[ -z "$TARGET_ROOT" && -f "$LOCAL_CONFIG" ]]; then
  TARGET_ROOT="$(python3 - "$LOCAL_CONFIG" <<'PY'
import json
import os
import sys

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    value = json.load(handle).get("target_root")
if isinstance(value, str) and value.strip():
    print(os.path.abspath(os.path.expanduser(value)))
PY
)"
fi

if [[ -z "$TARGET_ROOT" ]]; then
  echo "local skills target is not configured" >&2
  echo "pass a target root, set PI_SKILLS_TARGET_ROOT, or create .pi-skills.local.json" >&2
  exit 2
fi

TARGET_ROOT="$(python3 - "$TARGET_ROOT" <<'PY'
import os
import sys

print(os.path.abspath(os.path.expanduser(sys.argv[1])))
PY
)"

if ! command -v rsync >/dev/null 2>&1; then
  echo "rsync is required for production diffing" >&2
  exit 1
fi

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

registry_ids() {
  python3 - "$ROOT/registry.json" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    data = json.load(handle)
for skill in data.get("skills", []):
    print(skill["id"])
PY
}

diff_one() {
  local id="$1"
  local source_rel target_name source_dir target_dir diff_output
  source_rel="$(registry_value "$id" source)"
  target_name="$(registry_value "$id" production_target)"
  source_dir="$ROOT/$source_rel/"
  target_dir="$TARGET_ROOT/$target_name/"

  if [[ ! -d "$target_dir" ]]; then
    echo "production target missing: $target_dir" >&2
    return 1
  fi

  diff_output="$(rsync -anic --delete --omit-dir-times \
    --exclude ".DS_Store" \
    --exclude "__pycache__" \
    --exclude "*.pyc" \
    "$source_dir" "$target_dir")"

  if [[ -n "$diff_output" ]]; then
    printf '%s\n' "$diff_output"
    return 1
  fi

  echo "production matches source for $id"
}

if [[ "$SKILL_ID" == "all" ]]; then
  status=0
  while IFS= read -r id; do
    diff_one "$id" || status=1
  done < <(registry_ids)
  exit "$status"
fi

diff_one "$SKILL_ID"
