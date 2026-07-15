#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FORCE=0

if [[ "${1:-}" == "--force" ]]; then
  FORCE=1
  shift
fi
if [[ $# -ne 0 ]]; then
  echo "usage: scripts/preflight.sh [--force]" >&2
  exit 2
fi

REUSED_RECEIPT=""
if [[ "$FORCE" -eq 0 ]]; then
  REUSED_RECEIPT="$(python3 "$ROOT/scripts/preflight_receipt.py" check --root "$ROOT" 2>/dev/null || true)"
fi
if [[ -n "$REUSED_RECEIPT" ]]; then
  FINGERPRINT="$(printf '%s' "$REUSED_RECEIPT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["fingerprint"])')"
  echo "preflight reused: $FINGERPRINT"
  exit 0
fi

STAGES_JSON="$(mktemp "${TMPDIR:-/tmp}/pi-skills-preflight-stages.XXXXXX")"
trap 'rm -f "$STAGES_JSON"' EXIT
printf '[]\n' >"$STAGES_JSON"
START_FINGERPRINT="$(python3 "$ROOT/scripts/preflight_receipt.py" fingerprint --root "$ROOT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["fingerprint"])')"

run_stage() {
  local name="$1"
  shift
  local started ended
  started="$(python3 -c 'import time; print(time.monotonic_ns())')"
  "$@"
  ended="$(python3 -c 'import time; print(time.monotonic_ns())')"
  python3 - "$STAGES_JSON" "$name" "$started" "$ended" <<'PY'
import json
import sys

path, name, started, ended = sys.argv[1:]
with open(path, "r", encoding="utf-8") as handle:
    stages = json.load(handle)
stages.append(
    {
        "name": name,
        "status": "pass",
        "elapsed_ms": round((int(ended) - int(started)) / 1_000_000, 3),
    }
)
with open(path, "w", encoding="utf-8") as handle:
    json.dump(stages, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    handle.write("\n")
PY
}

run_stage validate-all bash "$ROOT/scripts/validate-all.sh"
run_stage repository-tests python3 -m unittest discover -s "$ROOT/tests" -p 'test_*.py'
run_stage public-tree bash "$ROOT/scripts/check-public.sh"
run_stage package-artifacts bash "$ROOT/scripts/package-all.sh" --preflight-validated
run_stage public-artifacts bash "$ROOT/scripts/check-public.sh" --artifacts

RECEIPT="$(python3 "$ROOT/scripts/preflight_receipt.py" record --root "$ROOT" --stages-json "$STAGES_JSON" --expected-fingerprint "$START_FINGERPRINT")"
FINGERPRINT="$(printf '%s' "$RECEIPT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["fingerprint"])')"

echo "preflight passed: $FINGERPRINT"
