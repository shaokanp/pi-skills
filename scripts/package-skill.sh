#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKILL_ID="${1:-}"
VERSION_OVERRIDE="${2:-}"

if [[ -z "$SKILL_ID" ]]; then
  echo "usage: scripts/package-skill.sh <skill-id> [version]" >&2
  exit 2
fi

registry_value() {
  local field="$1"
  python3 - "$ROOT/registry.json" "$SKILL_ID" "$field" <<'PY'
import json
import sys

registry_path, skill_id, field = sys.argv[1:4]
with open(registry_path, "r", encoding="utf-8") as f:
    data = json.load(f)
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

SOURCE_REL="$(registry_value source)"
VERSION="${VERSION_OVERRIDE:-$(registry_value version)}"
ARCHIVE="dist/${SKILL_ID}-${VERSION}.tar.gz"

bash "$ROOT/scripts/validate-skill.sh" "$SKILL_ID"

mkdir -p "$ROOT/dist"
python3 "$ROOT/scripts/package_skill.py" "$ROOT" "$SOURCE_REL" "$ROOT/$ARCHIVE"

if command -v shasum >/dev/null 2>&1; then
  (cd "$ROOT" && shasum -a 256 "$ARCHIVE") > "$ROOT/$ARCHIVE.sha256"
elif command -v sha256sum >/dev/null 2>&1; then
  (cd "$ROOT" && sha256sum "$ARCHIVE") > "$ROOT/$ARCHIVE.sha256"
else
  echo "shasum or sha256sum is required" >&2
  exit 1
fi

echo "packaged $ARCHIVE"
echo "wrote $ARCHIVE.sha256"
