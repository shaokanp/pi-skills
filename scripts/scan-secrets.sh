#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCAN_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/pi-skills-public.XXXXXX")"
PUBLIC_ARCHIVE="$SCAN_ROOT/pi-skills-public.tar"
trap 'rm -rf "$SCAN_ROOT"' EXIT

if ! command -v gitleaks >/dev/null 2>&1; then
  echo "publish blocked: gitleaks is required for the public publish gate" >&2
  echo "install it with: brew install gitleaks" >&2
  exit 1
fi

git -C "$ROOT" archive --format=tar --output="$PUBLIC_ARCHIVE" HEAD

gitleaks dir \
  --redact=100 \
  --no-banner \
  --max-decode-depth=2 \
  --max-archive-depth=2 \
  "$PUBLIC_ARCHIVE"

gitleaks git \
  --redact=100 \
  --no-banner \
  --max-decode-depth=2 \
  --max-archive-depth=2 \
  --log-opts="--all" \
  "$ROOT"

echo "gitleaks tracked HEAD and history scans passed"
