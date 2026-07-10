#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

bash "$ROOT/scripts/check-public.sh" --index --require-local-markers
git -C "$ROOT" config core.hooksPath .githooks
echo "installed git hooks from .githooks"

if ! command -v gitleaks >/dev/null 2>&1; then
  echo "warning: public pushes remain blocked until gitleaks is installed" >&2
  echo "install it with: brew install gitleaks" >&2
fi
