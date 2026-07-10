from __future__ import annotations

import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGER = PROJECT_ROOT / "scripts" / "package_skill.py"


class PublicPackageTests(unittest.TestCase):
    def test_package_uses_public_git_files_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
            (root / ".gitignore").write_text("skills/demo/private.txt\n", encoding="utf-8")
            (root / "LICENSE").write_text("test license\n", encoding="utf-8")
            skill = root / "skills" / "demo"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("# Demo\n", encoding="utf-8")
            (skill / "new.txt").write_text("untracked but public\n", encoding="utf-8")
            (skill / "private.txt").write_text("ignored local file\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(root), "add", ".gitignore", "LICENSE", "skills/demo/SKILL.md"],
                check=True,
            )
            archive = root / "demo.tar.gz"
            result = subprocess.run(
                [sys.executable, str(PACKAGER), str(root), "skills/demo", str(archive)],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            with tarfile.open(archive, "r:gz") as package:
                names = set(package.getnames())
            self.assertEqual(names, {"LICENSE", "SKILL.md", "new.txt"})


if __name__ == "__main__":
    unittest.main()
