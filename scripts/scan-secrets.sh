#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! command -v gitleaks >/dev/null 2>&1; then
  echo "publish blocked: gitleaks is required for the public publish gate" >&2
  echo "install it with: brew install gitleaks" >&2
  exit 1
fi

gitleaks dir \
  --redact=100 \
  --no-banner \
  --max-decode-depth=2 \
  --max-archive-depth=2 \
  "$ROOT"

gitleaks git \
  --redact=100 \
  --no-banner \
  --max-decode-depth=2 \
  --max-archive-depth=2 \
  --log-opts="--all" \
  "$ROOT"

echo "gitleaks worktree and history scans passed"
