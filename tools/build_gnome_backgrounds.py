#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Build the pinned upstream GNOME backgrounds Kiln package."""

from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kiln.cli import build


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    lock = tomllib.loads((root / "sources.lock.toml").read_text(encoding="utf-8"))
    source_lock = lock["desktop"]["gnome_backgrounds"]
    cache = root / "build/downloads"
    archive = cache / Path(source_lock["url"]).name
    try:
        cache.mkdir(parents=True, exist_ok=True)
        if not archive.is_file():
            with tempfile.NamedTemporaryFile(dir=cache, delete=False) as temporary:
                temporary_path = Path(temporary.name)
                with urllib.request.urlopen(source_lock["url"], timeout=90) as response:
                    shutil.copyfileobj(response, temporary)
            temporary_path.replace(archive)
        if digest(archive) != source_lock["sha256"]:
            raise RuntimeError(f"checksum mismatch: {archive}")
        with tempfile.TemporaryDirectory(prefix="meldra-gnome-backgrounds-") as directory:
            work = Path(directory)
            source_parent = work / "source"
            source_parent.mkdir()
            with tarfile.open(archive, "r:xz") as bundle:
                members = bundle.getmembers()
                for member in members:
                    path = Path(member.name)
                    if path.is_absolute() or ".." in path.parts:
                        raise RuntimeError(f"unsafe source member: {member.name}")
                bundle.extractall(source_parent, members=members, filter="data")
            source = source_parent / f"gnome-backgrounds-{source_lock['version']}"
            build_dir = work / "build"
            stage = work / "package/root"
            subprocess.run(
                ["meson", "setup", str(build_dir), str(source), "--prefix=/usr"], check=True
            )
            subprocess.run(
                ["meson", "install", "-C", str(build_dir), "--destdir", str(stage)], check=True
            )
            package = stage.parent
            (package / "KILN.toml").write_text(
                "[package]\nname = \"gnome-backgrounds\"\n"
                f'version = "{source_lock["version"]}"\narchitecture = "any"\n'
                "dependencies = []\n",
                encoding="utf-8",
            )
            output = root / "build/packages" / f"gnome-backgrounds-{source_lock['version']}.kiln"
            print(build(package, output))
    except (OSError, RuntimeError, subprocess.CalledProcessError, tarfile.TarError) as exc:
        print(f"build_gnome_backgrounds: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
