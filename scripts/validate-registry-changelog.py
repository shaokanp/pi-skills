#!/usr/bin/env python3
"""Validate deterministic per-skill version markers in CHANGELOG.md."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


ID_PATTERN = r"[a-z0-9]+(?:-[a-z0-9]+)*"
VERSION_PATTERN = r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?"
HEADER = re.compile(r"^## (?P<title>.+?)\s*$")
MARKER = re.compile(
    rf"^<!-- pi-skills:(?P<state>unreleased|release) "
    rf"id=(?P<id>{ID_PATTERN}) version=(?P<version>{VERSION_PATTERN}) -->$"
)
RELEASE_HEADING = re.compile(rf"^{VERSION_PATTERN} - \d{{4}}-\d{{2}}-\d{{2}}$")


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    registry = json.loads((root / "registry.json").read_text(encoding="utf-8"))
    registered = {
        (skill["id"], skill["version"]): skill for skill in registry.get("skills", [])
    }
    known_ids = {skill_id for skill_id, _ in registered}
    section: str | None = None
    markers: dict[tuple[str, str], list[tuple[str, int]]] = {}
    failures: list[str] = []

    for line_number, line in enumerate(
        (root / "CHANGELOG.md").read_text(encoding="utf-8").splitlines(), start=1
    ):
        header = HEADER.fullmatch(line)
        if header:
            section = header.group("title")
            continue
        if not line.startswith("<!-- pi-skills:"):
            continue
        marker = MARKER.fullmatch(line)
        if not marker:
            failures.append(f"malformed changelog marker at line {line_number}")
            continue
        state = marker.group("state")
        skill_id = marker.group("id")
        version = marker.group("version")
        if skill_id not in known_ids:
            failures.append(f"changelog marker names unknown skill {skill_id} at line {line_number}")
        if state == "unreleased" and section != "Unreleased":
            failures.append(f"unreleased marker outside Unreleased at line {line_number}")
        if state == "release" and (section is None or not RELEASE_HEADING.fullmatch(section)):
            failures.append(f"release marker must sit under a dated release heading at line {line_number}")
        markers.setdefault((skill_id, version), []).append((state, line_number))

    for key in sorted(registered):
        matches = markers.get(key, [])
        if not matches:
            failures.append(f"missing changelog marker for {key[0]} {key[1]}")
        elif len(matches) != 1:
            lines = ", ".join(str(line) for _, line in matches)
            failures.append(f"ambiguous changelog markers for {key[0]} {key[1]} at lines {lines}")

    if failures:
        print("registry/changelog validation failed:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1

    print(f"validated changelog markers for {len(registered)} registered skills")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
