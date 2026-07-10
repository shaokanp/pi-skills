#!/usr/bin/env python3
"""Build a skill archive from the public Git file set, never arbitrary local files."""

from __future__ import annotations

import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath


def public_source_files(root: Path, source: str) -> list[Path]:
    raw = subprocess.check_output(
        [
            "git",
            "-C",
            str(root),
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            source,
        ]
    )
    paths = [
        Path(item.decode("utf-8", errors="surrogateescape"))
        for item in raw.split(b"\0")
        if item
    ]
    return sorted(path for path in paths if (root / path).is_file())


def add_file(package: tarfile.TarFile, source: Path, archive_name: str) -> None:
    if source.is_symlink():
        raise RuntimeError(f"public skill packages do not allow symlinks: {source}")
    info = package.gettarinfo(str(source), arcname=archive_name)
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    with source.open("rb") as handle:
        package.addfile(info, handle)


def main() -> int:
    if len(sys.argv) != 4:
        raise SystemExit("usage: package_skill.py <repo-root> <source-rel> <archive>")
    root = Path(sys.argv[1]).resolve()
    source_rel = PurePosixPath(sys.argv[2])
    archive = Path(sys.argv[3]).resolve()
    files = public_source_files(root, source_rel.as_posix())
    if not files:
        raise SystemExit(f"no public files found for {source_rel}")

    archive.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "w:gz", compresslevel=9) as package:
        for relative in files:
            archive_name = relative.relative_to(Path(source_rel)).as_posix()
            add_file(package, root / relative, archive_name)
        add_file(package, root / "LICENSE", "LICENSE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
