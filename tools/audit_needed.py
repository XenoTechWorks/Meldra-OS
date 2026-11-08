#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Fail if any ELF in a Kiln package records an absolute path (a build directory) as a needed library.

    audit_needed.py [build/packages/*.kiln]
"""
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


def main() -> int:
    packages = [Path(p) for p in sys.argv[1:]] or sorted(Path("build/packages").glob("*.kiln"))
    bad = 0
    for package in packages:
        with tempfile.TemporaryDirectory() as directory, tarfile.open(package) as bundle:
            bundle.extractall(directory, filter="data")
            for path in Path(directory).rglob("*"):
                if path.is_symlink() or not path.is_file():
                    continue
                with path.open("rb") as stream:
                    if stream.read(4) != b"\x7fELF":
                        continue
                out = subprocess.run(["readelf", "-d", str(path)], capture_output=True, text=True).stdout
                for needed in re.findall(r"Shared library: \[([^]]+)\]", out):
                    if needed.startswith("/"):
                        print(f"{package.name}: {path.relative_to(directory)} needs absolute path {needed}")
                        bad += 1
                for rpath in re.findall(r"(?:RPATH|RUNPATH)[^\[]*\[([^]]+)\]", out):
                    if "/tmp/" in rpath or "/build/work" in rpath or "meldra-" in rpath:
                        print(f"{package.name}: {path.relative_to(directory)} has build-dir rpath {rpath}")
                        bad += 1
    print(f"{len(packages)} packages audited, {bad} problem(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
