#!/usr/bin/env bash
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_CONFIG="${PI_SKILLS_LOCAL_CONFIG:-$ROOT/.pi-skills.local.json}"
STRICT_LOCAL=0
FAILURES=0

usage() {
  cat <<'EOF'
usage: scripts/doctor.sh [--strict-local] [--help]

Check a portable clone without changing it. The default verifies baseline tools,
repository location, hook state, and registry/skill contracts. When a valid
local configuration and target are available, it also compares local production
against source.

--strict-local  Require maintainer-only prerequisites: configured and complete
                local config, active repository hooks, and a passing local
                production drift comparison.
--help          Show this help.
EOF
}

note() {
  printf '%-5s %s\n' "$1" "$2"
}

fail() {
  note "FAIL" "$1"
  FAILURES=$((FAILURES + 1))
}

warn_or_fail() {
  if [[ "$STRICT_LOCAL" -eq 1 ]]; then
    fail "$1"
  else
    note "WARN" "$1"
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --strict-local)
      STRICT_LOCAL=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

for tool in bash git python3 rsync; do
  if command -v "$tool" >/dev/null 2>&1; then
    note "PASS" "baseline tool available: $tool"
  else
    fail "missing baseline tool: $tool"
  fi
done

if git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  GIT_ROOT="$(git -C "$ROOT" rev-parse --show-toplevel)"
  if [[ "$GIT_ROOT" == "$ROOT" ]]; then
    note "PASS" "script root is the Git worktree root"
  else
    fail "script root does not match the Git worktree root"
  fi
else
  fail "script root is not inside a Git worktree"
fi

HOOK_PATH="$(git -C "$ROOT" config --get core.hooksPath 2>/dev/null || true)"
if [[ "$HOOK_PATH" == ".githooks" && -x "$ROOT/.githooks/pre-commit" && -x "$ROOT/.githooks/pre-push" ]]; then
  note "PASS" "repository hooks are active"
else
  warn_or_fail "repository hooks are not active; run scripts/install-hooks.sh when appropriate"
fi

VALIDATION_LOG="$(mktemp "${TMPDIR:-/tmp}/pi-skills-doctor.XXXXXX")"
trap 'rm -f "$VALIDATION_LOG"' EXIT
if bash "$ROOT/scripts/validate-all.sh" >"$VALIDATION_LOG" 2>&1; then
  note "PASS" "registry and skill contracts validate"
else
  cat "$VALIDATION_LOG" >&2
  fail "registry or skill contract validation failed"
fi

LOCAL_STATE="$(python3 - "$LOCAL_CONFIG" <<'PY'
import json
import os
import sys
from pathlib import Path

config_path = Path(sys.argv[1]).expanduser()
if not config_path.is_file():
    print("missing")
    raise SystemExit(0)
try:
    data = json.loads(config_path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    print("invalid")
    raise SystemExit(0)
if not isinstance(data, dict):
    print("invalid")
    raise SystemExit(0)
target = data.get("target_root")
markers = data.get("private_markers")
if not isinstance(target, str) or not target.strip():
    print("incomplete")
    raise SystemExit(0)
if not isinstance(markers, list) or not all(isinstance(item, str) for item in markers) or not any(item.strip() for item in markers):
    print("incomplete")
    raise SystemExit(0)
print(os.path.abspath(os.path.expanduser(target)))
PY
)"

case "$LOCAL_STATE" in
  missing)
    warn_or_fail "local maintainer config is missing"
    ;;
  invalid|incomplete)
    warn_or_fail "local maintainer config is invalid or incomplete"
    ;;
  *)
    if [[ -d "$LOCAL_STATE" ]]; then
      if bash "$ROOT/scripts/diff-production.sh" all "$LOCAL_STATE"; then
        note "PASS" "local production matches source"
      else
        fail "local production differs from source"
      fi
    else
      warn_or_fail "configured local production target is unavailable"
    fi
    ;;
esac

if [[ "$FAILURES" -gt 0 ]]; then
  echo "doctor failed ($FAILURES issue(s))" >&2
  exit 1
fi

if [[ "$STRICT_LOCAL" -eq 1 ]]; then
  echo "doctor passed (strict local)"
else
  echo "doctor passed (portable)"
fi
