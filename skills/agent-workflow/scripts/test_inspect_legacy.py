#!/usr/bin/env python3
"""Slice 8 legacy reader confinement, compatibility, and zero-write tests."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from inspect_legacy import LegacyInspectionError, MAX_FILE_BYTES, inspect  # noqa: E402


FIXTURES = SCRIPT_DIR.parent / "fixtures" / "vnext" / "legacy-reader"


def snapshot(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def write_pair(root: Path, orchestration_schema: str, state_schema: str, *, workspace_root: object | None = None) -> None:
    workflow: dict[str, object] = {"title": "fixture", "slug": "fixture"}
    if workspace_root is not None:
        workflow["workspace_root"] = workspace_root
    (root / "orchestration.json").write_text(json.dumps({
        "schema_version": orchestration_schema,
        "workflow": workflow,
        "rounds": [{"round_id": "round-1"}],
    }))
    (root / "workflow-state.json").write_text(json.dumps({"schema_version": state_schema}))


class LegacyInspectorTests(unittest.TestCase):
    def test_frozen_v1_and_v2_are_digest_sealed_and_read_without_writes(self) -> None:
        seal = json.loads((FIXTURES / "seal.v1.json").read_text())
        for version, expected_rounds in (("v1", 1), ("v2", 2)):
            with self.subTest(version=version):
                root = FIXTURES / version
                before = snapshot(root)
                for name, digest in seal["fixtures"][version].items():
                    self.assertEqual("sha256:" + hashlib.sha256((root / name).read_bytes()).hexdigest(), digest)
                result = inspect(root, allowed_root=FIXTURES)
                self.assertEqual(result["round_count"], expected_rounds)
                self.assertEqual(result["compatibility"], "legacy_reader_only_no_writer_fallback")
                self.assertEqual(snapshot(root), before)

    def test_missing_schema_state_cross_version_and_traversal_fail_closed(self) -> None:
        with TemporaryDirectory() as raw:
            allowed = Path(raw)
            root = allowed / "workflow"
            root.mkdir()
            write_pair(root, "agent-loops.orchestration.v1", "agent-loops.workflow.v1")
            (root / "workflow-state.json").unlink()
            with self.assertRaisesRegex(LegacyInspectionError, "missing or unsafe"):
                inspect(root, allowed_root=allowed)
            write_pair(root, "agent-loops.orchestration.v1", "agent-workflow.workflow.v2")
            with self.assertRaisesRegex(LegacyInspectionError, "do not match"):
                inspect(root, allowed_root=allowed)
            write_pair(root, "agent-loops.orchestration.v1", "agent-loops.workflow.v1", workspace_root="../escape")
            with self.assertRaisesRegex(LegacyInspectionError, "safe relative"):
                inspect(root, allowed_root=allowed)
            write_pair(root, "", "agent-loops.workflow.v1")
            with self.assertRaisesRegex(LegacyInspectionError, "missing or unsupported"):
                inspect(root, allowed_root=allowed)

    def test_file_and_ancestor_symlinks_fifo_oversize_and_outside_root_fail_closed(self) -> None:
        with TemporaryDirectory() as raw:
            allowed = Path(raw) / "allowed"
            allowed.mkdir()
            real = allowed / "real"
            real.mkdir()
            write_pair(real, "agent-loops.orchestration.v1", "agent-loops.workflow.v1")
            linked = allowed / "linked"
            linked.symlink_to(real, target_is_directory=True)
            with self.assertRaisesRegex(LegacyInspectionError, "symlink or unsafe ancestor"):
                inspect(linked, allowed_root=allowed)
            (real / "orchestration.json").unlink()
            (real / "orchestration.json").symlink_to(real / "workflow-state.json")
            with self.assertRaisesRegex(LegacyInspectionError, "missing or unsafe"):
                inspect(real, allowed_root=allowed)
            (real / "orchestration.json").unlink()
            os.mkfifo(real / "orchestration.json")
            with self.assertRaisesRegex(LegacyInspectionError, "bounded regular file"):
                inspect(real, allowed_root=allowed)
            (real / "orchestration.json").unlink()
            (real / "orchestration.json").write_bytes(b"x" * (MAX_FILE_BYTES + 1))
            with self.assertRaisesRegex(LegacyInspectionError, "bounded regular file"):
                inspect(real, allowed_root=allowed)
            outside = Path(raw) / "outside"
            outside.mkdir()
            write_pair(outside, "agent-loops.orchestration.v1", "agent-loops.workflow.v1")
            with self.assertRaisesRegex(LegacyInspectionError, "inside the allowed root"):
                inspect(outside, allowed_root=allowed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
