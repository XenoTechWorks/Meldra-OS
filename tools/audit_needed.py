#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Fail if any ELF in a Kiln package records a build path, or needs libstdc++/libgcc_s without declaring
the gcc-runtime package as a dependency.

    audit_needed.py [build/packages/*.kiln]
"""
import re
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from pathlib import Path


def main() -> int:
    packages = [Path(p) for p in sys.argv[1:]] or sorted(Path("build/packages").glob("*.kiln"))
    bad = 0
    for package in packages:
        with tempfile.TemporaryDirectory() as directory, tarfile.open(package) as bundle:
            bundle.extractall(directory, filter="data")
            meta = tomllib.loads((Path(directory) / "KILN.toml").read_text())["package"]
            for member in bundle.getnames():
                parts = Path(member).parts
                if len(parts) > 1 and parts[0] == "root" and (parts[1] in ("home", "tmp", "mnt") or "sysroot" in parts):
                    print(f"{package.name}: file installed under a build path: {member}")
                    bad += 1
                    break
            # The gcc-runtime package is where libstdc++ and libgcc_s live; it may need its own pieces.
            runtime_declared = "gcc-runtime" in meta.get("dependencies", []) or meta["name"] == "gcc-runtime"
            for path in Path(directory).rglob("*"):
                if path.is_symlink() or not path.is_file():
                    continue
                with path.open("rb") as stream:
                    if stream.read(4) != b"\x7fELF":
                        continue
                out = subprocess.run(["readelf", "-d", str(path)], capture_output=True, text=True).stdout
                for needed in re.findall(r"Shared library: \[([^]]+)\]", out):
                    if needed.startswith(("libgcc_s", "libstdc++")) and not runtime_declared:
                        print(f"{package.name}: {path.relative_to(directory)} needs undeclared GCC runtime {needed}")
                        bad += 1
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
