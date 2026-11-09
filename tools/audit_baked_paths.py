#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Find build-directory paths compiled into installed files (outside debug info).

    audit_baked_paths.py [build/packages/*.kiln]

A path like /tmp/meldra-gnome-x/.../sysroot/usr/lib/foo inside a binary or a text file is a runtime bug:
it names a directory that only existed during the build.
"""
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

# The leak that breaks a booted system is a path *through the build sysroot* (pkg-config prepends it).
# Plain source-file names from __FILE__ in assert and log macros also mention build directories but
# are harmless, so they are not reported.
MARKERS = re.compile(rb"[A-Za-z0-9_./-]*/sysroot(?:/[A-Za-z0-9_.+-]+)+")


def main() -> int:
    packages = [Path(p) for p in sys.argv[1:]] or sorted(Path("build/packages").glob("*.kiln"))
    total = 0
    for package in packages:
        findings = []
        with tempfile.TemporaryDirectory() as directory, tarfile.open(package) as bundle:
            bundle.extractall(directory, filter="data")
            for path in Path(directory).rglob("*"):
                if path.is_symlink() or not path.is_file():
                    continue
                with path.open("rb") as stream:
                    head = stream.read(4)
                target = path
                if head == b"\x7fELF":
                    stripped = Path(directory) / "_stripped"
                    shutil.copy2(path, stripped)
                    subprocess.run(["strip", "--strip-debug", str(stripped)], capture_output=True)
                    target = stripped
                data = target.read_bytes()
                for match in MARKERS.finditer(data):
                    start = data.rfind(b"\x00", 0, match.start()) + 1
                    end = data.find(b"\x00", match.end())
                    findings.append(f"{path.relative_to(Path(directory) / 'root')}: {data[start:end][:140].decode(errors='replace')}")
                    break
                if target is not path:
                    target.unlink()
        if findings:
            print(f"{package.name}: {len(findings)} file(s)")
            for line in findings[:6]:
                print("   ", line)
            total += len(findings)
    print(f"{len(packages)} packages scanned, {total} file(s) with build paths")
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
