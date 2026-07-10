from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class MaintenanceHarnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "pi-skills"
        shutil.copytree(
            PROJECT_ROOT,
            self.root,
            ignore=shutil.ignore_patterns(
                ".git",
                ".claude",
                ".pi-skills.local.json",
                "dist",
                "__pycache__",
                "*.pyc",
            ),
        )
        self.run_git("init", "-q")
        self.run_git("config", "user.name", "Harness Test")
        self.run_git("config", "user.email", "harness@example.test")
        self.run_git("add", "-A")
        self.run_git("commit", "-qm", "fixture")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def run_script(
        self, *args: str, local_config: str = "missing.json", extra_env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return self.run_command("bash", *args, local_config=local_config, extra_env=extra_env)

    def run_python(
        self, *args: str, local_config: str = "missing.json", extra_env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return self.run_command("python3", *args, local_config=local_config, extra_env=extra_env)

    def run_command(
        self,
        command: str,
        *args: str,
        local_config: str = "missing.json",
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["PI_SKILLS_LOCAL_CONFIG"] = str(self.root / local_config)
        environment.update(extra_env or {})
        return subprocess.run(
            [command, *args],
            cwd=self.root,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )

    def test_doctor_is_portable_and_strict_local_fails_closed(self) -> None:
        portable = self.run_script("scripts/doctor.sh")
        self.assertEqual(portable.returncode, 0, portable.stderr)
        self.assertIn("doctor passed (portable)", portable.stdout)
        self.assertIn("local maintainer config is missing", portable.stdout)

        strict = self.run_script("scripts/doctor.sh", "--strict-local")
        self.assertEqual(strict.returncode, 1)
        self.assertIn("local maintainer config is missing", strict.stdout)
        self.assertIn("doctor failed", strict.stderr)

        fixture_root = Path(self.temp_dir.name)
        target = fixture_root / "local-target"
        shutil.copytree(
            self.root / "skills",
            target,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        runtime_cache = target / "agent-workflow" / "scripts" / "__pycache__"
        runtime_cache.mkdir()
        (runtime_cache / "runtime-noise.pyc").write_bytes(b"runtime noise")
        strict_config = fixture_root / "strict-local.json"
        strict_config.write_text(
            json.dumps(
                {"target_root": str(target), "validator": None, "private_markers": ["fixture-only"]}
            ),
            encoding="utf-8",
        )
        self.run_git("config", "core.hooksPath", ".githooks")
        verified = self.run_script(
            "scripts/doctor.sh",
            "--strict-local",
            local_config=str(strict_config),
        )
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertIn("doctor passed (strict local)", verified.stdout)

    def test_new_skill_scaffold_fails_until_completed_without_collision_changes(self) -> None:
        registry_before = (self.root / "registry.json").read_text(encoding="utf-8")
        changelog_before = (self.root / "CHANGELOG.md").read_text(encoding="utf-8")
        invalid = self.run_script("scripts/new-skill.sh", "Bad_Id")
        self.assertEqual(invalid.returncode, 2)
        self.assertEqual((self.root / "registry.json").read_text(encoding="utf-8"), registry_before)
        self.assertEqual((self.root / "CHANGELOG.md").read_text(encoding="utf-8"), changelog_before)
        self.assertFalse((self.root / "skills" / "Bad_Id").exists())

        created = self.run_script("scripts/new-skill.sh", "demo-skill")
        self.assertEqual(created.returncode, 0, created.stderr)
        incomplete = self.run_script("scripts/validate-skill.sh", "demo-skill")
        self.assertEqual(incomplete.returncode, 1)
        self.assertIn("incomplete placeholder", incomplete.stderr)

        registry_after_create = (self.root / "registry.json").read_text(encoding="utf-8")
        changelog_after_create = (self.root / "CHANGELOG.md").read_text(encoding="utf-8")
        collision = self.run_script("scripts/new-skill.sh", "demo-skill")
        self.assertEqual(collision.returncode, 1)
        self.assertEqual(
            (self.root / "registry.json").read_text(encoding="utf-8"), registry_after_create
        )
        self.assertEqual(
            (self.root / "CHANGELOG.md").read_text(encoding="utf-8"), changelog_after_create
        )

        skill_dir = self.root / "skills" / "demo-skill"
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: demo-skill\n"
            "description: Demonstrate the completed scaffold contract.\n"
            "---\n\n"
            "# Demo Skill\n\n"
            "Use this completed fixture for validation.\n",
            encoding="utf-8",
        )
        (skill_dir / "README.md").write_text(
            "# Demo Skill\n\n完整的繁體中文 guide。\n", encoding="utf-8"
        )
        (skill_dir / "README.en.md").write_text(
            "# Demo Skill\n\nCompleted English guide.\n", encoding="utf-8"
        )
        completed = self.run_script("scripts/validate-skill.sh", "demo-skill")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        registry_placeholder = self.run_script("scripts/validate-all.sh")
        self.assertEqual(registry_placeholder.returncode, 1)
        self.assertIn(
            "description contains incomplete placeholder content for demo-skill",
            registry_placeholder.stderr,
        )

        registry_path = self.root / "registry.json"
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        for skill in registry["skills"]:
            if skill["id"] == "demo-skill":
                skill["description"] = "Demonstrate a completed new-skill contract."
        registry_path.write_text(json.dumps(registry, indent=2) + "\n", encoding="utf-8")
        accepted = self.run_script("scripts/validate-all.sh")
        self.assertEqual(accepted.returncode, 0, accepted.stderr)

    def test_readme_placeholder_fails_closed(self) -> None:
        guide = self.root / "skills" / "explain" / "README.en.md"
        guide.write_text("# Explain\n\nTODO\n", encoding="utf-8")
        result = self.run_script("scripts/validate-skill.sh", "explain")
        self.assertEqual(result.returncode, 1)
        self.assertIn("README.en.md contains incomplete placeholder", result.stderr)

    def test_missing_current_changelog_marker_fails_closed(self) -> None:
        changelog = self.root / "CHANGELOG.md"
        marker = "<!-- pi-skills:unreleased id=write-good-goal version=1.0.1 -->\n"
        changelog.write_text(
            changelog.read_text(encoding="utf-8").replace(marker, ""), encoding="utf-8"
        )
        result = self.run_python("scripts/validate-registry-changelog.py")
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing changelog marker for write-good-goal 1.0.1", result.stderr)


if __name__ == "__main__":
    unittest.main()
