#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFLIGHT_VALIDATED=0

if [[ "${1:-}" == "--preflight-validated" ]]; then
  PREFLIGHT_VALIDATED=1
  shift
fi
if [[ $# -ne 0 ]]; then
  echo "usage: scripts/package-all.sh [--preflight-validated]" >&2
  exit 2
fi

# dist is generated output. Rebuild the archive set from the current registry so
# removed skills and superseded versions cannot survive into a public release.
mkdir -p "$ROOT/dist"
find "$ROOT/dist" -maxdepth 1 -type f \
  \( -name '*.tar.gz' -o -name '*.tar.gz.sha256' \) -delete

IDS="$(python3 - "$ROOT/registry.json" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as f:
    data = json.load(f)
for skill in data.get("skills", []):
    print(skill["id"])
PY
)"

for skill_id in $IDS; do
  if [[ "$PREFLIGHT_VALIDATED" -eq 1 ]]; then
    bash "$ROOT/scripts/package-skill.sh" "$skill_id" --preflight-validated
  else
    bash "$ROOT/scripts/package-skill.sh" "$skill_id"
  fi
done
