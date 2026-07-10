#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

bash "$ROOT/scripts/validate-all.sh"
python3 -m unittest discover -s "$ROOT/tests" -p 'test_*.py'
bash "$ROOT/scripts/check-public.sh"
bash "$ROOT/scripts/package-all.sh"
bash "$ROOT/scripts/check-public.sh" --artifacts

echo "preflight passed"
