#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKILL_ID="${1:-}"

if [[ -z "$SKILL_ID" ]]; then
  echo "usage: scripts/validate-skill.sh <skill-id>" >&2
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
SKILL_DIR="$ROOT/$SOURCE_REL"
SKILL_MD="$SKILL_DIR/SKILL.md"

if [[ ! -d "$SKILL_DIR" ]]; then
  echo "missing skill directory: $SOURCE_REL" >&2
  exit 1
fi

if [[ ! -f "$SKILL_MD" ]]; then
  echo "missing SKILL.md: $SOURCE_REL/SKILL.md" >&2
  exit 1
fi

python3 - "$SKILL_MD" "$SKILL_ID" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
expected = sys.argv[2]
text = path.read_text(encoding="utf-8")
placeholder = re.compile(
    r"(?im)pi-skills:scaffold-incomplete|"
    r"^\s*(?:todo|tbd)(?:\s*:|\s*$)|"
    r"\[\s*(?:todo|tbd)[^\]]*\]"
)
match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
if not match:
    raise SystemExit("SKILL.md must start with YAML frontmatter")
frontmatter = match.group(1)
name = None
description_seen = False
for line in frontmatter.splitlines():
    if line.startswith("name:"):
        name = line.split(":", 1)[1].strip()
    if line.startswith("description:"):
        description_seen = True
if name != expected:
    raise SystemExit(f"frontmatter name mismatch: expected {expected!r}, got {name!r}")
if not description_seen:
    raise SystemExit("frontmatter description is required")
if placeholder.search(text):
    raise SystemExit("SKILL.md contains incomplete placeholder content")
if not re.search(r"(?m)^# \S.+$", text):
    raise SystemExit("SKILL.md must include a top-level heading")
PY

for guide in "$SKILL_DIR/README.md" "$SKILL_DIR/README.en.md"; do
  python3 - "$guide" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.is_file():
    raise SystemExit(f"missing required skill guide: {path.name}")
text = path.read_text(encoding="utf-8")
lines = text.splitlines()
first_content = next((line for line in lines if line.strip()), "")
if not re.fullmatch(r"# \S.+", first_content):
    raise SystemExit(f"{path.name} must start with a non-empty top-level heading")
if re.search(
    r"(?im)pi-skills:scaffold-incomplete|"
    r"^\s*(?:todo|tbd)(?:\s*:|\s*$)|"
    r"\[\s*(?:todo|tbd)[^\]]*\]",
    text,
):
    raise SystemExit(f"{path.name} contains incomplete placeholder content")
PY
done

LOCAL_CONFIG="${PI_SKILLS_LOCAL_CONFIG:-$ROOT/.pi-skills.local.json}"
VALIDATOR="${PI_SKILLS_VALIDATOR:-}"
if [[ -z "$VALIDATOR" && -f "$LOCAL_CONFIG" ]]; then
  VALIDATOR="$(python3 - "$LOCAL_CONFIG" <<'PY'
import json
import os
import sys

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    value = json.load(handle).get("validator")
if isinstance(value, str) and value.strip():
    print(os.path.abspath(os.path.expanduser(value)))
PY
)"
fi

if [[ -n "$VALIDATOR" ]]; then
  if [[ ! -f "$VALIDATOR" ]]; then
    echo "configured validator does not exist: $VALIDATOR" >&2
    exit 1
  fi
  python3 "$VALIDATOR" "$SKILL_DIR"
fi

python3 - "$SKILL_DIR" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
for path in sorted(root.glob("scripts/*.py")):
    compile(path.read_text(encoding="utf-8"), str(path), "exec")
PY

if [[ "$SKILL_ID" == "agent-workflow" ]]; then
  python3 "$SKILL_DIR/scripts/test_model_routing.py"
  python3 "$SKILL_DIR/scripts/test_execution_efficiency.py"
  python3 "$SKILL_DIR/scripts/test_token_accounting.py"
  python3 "$SKILL_DIR/scripts/test_swarm_card.py"
  python3 "$SKILL_DIR/scripts/test_vnext_suite.py"
  python3 "$SKILL_DIR/scripts/test_vnext_candidate.py"
  python3 "$SKILL_DIR/scripts/test_vnext_accounting.py"
  python3 "$SKILL_DIR/scripts/test_vnext_canary.py"
  python3 "$SKILL_DIR/scripts/test_inspect_legacy.py"
  python3 "$SKILL_DIR/scripts/test_vnext_runtime.py"
  python3 "$SKILL_DIR/scripts/test_process_supervisor.py"
  python3 "$SKILL_DIR/scripts/test_source_workspace.py"
  python3 "$SKILL_DIR/scripts/test_recovery_runtime.py"
fi

if git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git -C "$ROOT" diff --check -- "$SOURCE_REL" "registry.json" >/dev/null
fi

echo "validated $SKILL_ID"
