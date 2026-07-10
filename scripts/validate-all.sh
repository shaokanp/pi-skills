#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

python3 "$ROOT/scripts/validate_public_tree.py" "$ROOT"
python3 "$ROOT/scripts/validate-registry-changelog.py" "$ROOT"

python3 - "$ROOT/registry.json" <<'PY'
import json
import re
import sys
from pathlib import PurePosixPath

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    data = json.load(handle)

if data.get("schema_version") != "pi-skills.registry.v1":
    raise SystemExit("unsupported registry schema")

skills = data.get("skills")
if not isinstance(skills, list) or not skills:
    raise SystemExit("registry skills must be a non-empty list")

id_pattern = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
version_pattern = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
known_ids = set()
for skill in skills:
    skill_id = skill.get("id")
    if not isinstance(skill_id, str) or not id_pattern.fullmatch(skill_id):
        raise SystemExit(f"invalid skill id: {skill_id!r}")
    if skill_id in known_ids:
        raise SystemExit(f"duplicate skill id: {skill_id}")
    known_ids.add(skill_id)
    source = skill.get("source")
    expected_source = f"skills/{skill_id}"
    if source != expected_source or PurePosixPath(source).is_absolute() or ".." in PurePosixPath(source).parts:
        raise SystemExit(f"unsafe source for {skill_id}: {source!r}")
    if skill.get("production_target") != skill_id:
        raise SystemExit(f"production_target must match id for {skill_id}")
    if not isinstance(skill.get("version"), str) or not version_pattern.fullmatch(skill["version"]):
        raise SystemExit(f"invalid version for {skill_id}: {skill.get('version')!r}")
    if skill.get("status") not in {"stable", "experimental"}:
        raise SystemExit(f"invalid status for {skill_id}: {skill.get('status')!r}")
    if not isinstance(skill.get("display_name"), str) or not skill["display_name"].strip():
        raise SystemExit(f"display_name is required for {skill_id}")
    if not isinstance(skill.get("description"), str) or not skill["description"].strip():
        raise SystemExit(f"description is required for {skill_id}")
    if re.match(r"(?i)^\s*(?:todo|tbd|placeholder)(?:\s*:|\s*$)", skill["description"]):
        raise SystemExit(f"description contains incomplete placeholder content for {skill_id}")
    if skill.get("visibility") != "public":
        raise SystemExit(f"public repository cannot include non-public skill {skill_id}")

for skill in skills:
    skill_id = skill["id"]
    requirements = skill.get("requires", [])
    if not isinstance(requirements, list) or not all(isinstance(item, str) for item in requirements):
        raise SystemExit(f"requires must be a list of skill ids for {skill_id}")
    for requirement in requirements:
        if requirement == skill_id or requirement not in known_ids:
            raise SystemExit(f"invalid requirement for {skill_id}: {requirement}")
PY

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
  bash "$ROOT/scripts/validate-skill.sh" "$skill_id"
done

echo "validated registry"
