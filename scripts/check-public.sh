#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="worktree"
REFS_FILE=""
REQUIRE_LOCAL_MARKERS=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --history)
      MODE="history"
      shift
      ;;
    --artifacts)
      MODE="artifacts"
      shift
      ;;
    --index)
      MODE="index"
      shift
      ;;
    --push-refs)
      MODE="push"
      REFS_FILE="${2:-}"
      if [[ -z "$REFS_FILE" ]]; then
        echo "--push-refs requires a file" >&2
        exit 2
      fi
      shift 2
      ;;
    --require-local-markers)
      REQUIRE_LOCAL_MARKERS=1
      shift
      ;;
    *)
      echo "usage: scripts/check-public.sh [--history|--artifacts|--index|--push-refs file] [--require-local-markers]" >&2
      exit 2
      ;;
  esac
done

ARGS=(
  --root "$ROOT"
  --mode "$MODE"
)

if [[ -n "$REFS_FILE" ]]; then
  ARGS+=(--refs-file "$REFS_FILE")
fi

if [[ "$REQUIRE_LOCAL_MARKERS" -eq 1 ]]; then
  ARGS+=(--require-local-markers)
fi

python3 "$ROOT/scripts/public_safety.py" "${ARGS[@]}"
