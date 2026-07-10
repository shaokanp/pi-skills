#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REFS_FILE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --push-refs)
      REFS_FILE="${2:-}"
      if [[ -z "$REFS_FILE" ]]; then
        echo "--push-refs requires a file" >&2
        exit 2
      fi
      shift 2
      ;;
    *)
      echo "usage: scripts/publish-preflight.sh [--push-refs file]" >&2
      exit 2
      ;;
  esac
done

if git -C "$ROOT" ls-files --error-unmatch .pi-skills.local.json >/dev/null 2>&1; then
  echo "publish blocked: .pi-skills.local.json must remain untracked" >&2
  exit 1
fi

if ! git -C "$ROOT" check-ignore -q .pi-skills.local.json; then
  echo "publish blocked: .pi-skills.local.json is not ignored" >&2
  exit 1
fi

DIRTY="$(git -C "$ROOT" status --porcelain --untracked-files=normal)"
if [[ -n "$DIRTY" ]]; then
  echo "publish blocked: worktree must be clean so checks match the pushed commit" >&2
  printf '%s\n' "$DIRTY" >&2
  exit 1
fi

bash "$ROOT/scripts/preflight.sh"
bash "$ROOT/scripts/scan-secrets.sh"

if [[ -n "$REFS_FILE" ]]; then
  bash "$ROOT/scripts/check-public.sh" \
    --push-refs "$REFS_FILE" \
    --require-local-markers
else
  bash "$ROOT/scripts/check-public.sh" \
    --history \
    --require-local-markers
fi

echo "publish preflight passed"
