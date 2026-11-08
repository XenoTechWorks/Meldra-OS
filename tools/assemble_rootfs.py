#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Assemble the persistent Meldra base filesystem from Kiln packages."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kiln.cli import install


OS_RELEASE = """NAME="Meldra OS"
PRETTY_NAME="Meldra OS Developer Preview"
ID=meldra
ID_LIKE=linux
VERSION_ID=0.1
VERSION_CODENAME=confluence
HOME_URL="https://example.invalid/meldra"
"""


def write_base_configuration(root: Path) -> None:
    etc = root / "etc"
    etc.mkdir(parents=True, exist_ok=True)
    (etc / "os-release").write_text(OS_RELEASE, encoding="utf-8")
    (etc / "hostname").write_text("meldra\n", encoding="utf-8")
    (etc / "fstab").write_text(
        "proc /proc proc nosuid,nodev,noexec 0 0\n"
        "sysfs /sys sysfs nosuid,nodev,noexec 0 0\n"
        "devtmpfs /dev devtmpfs mode=0755,nosuid 0 0\n"
        "tmpfs /run tmpfs mode=0755,nosuid,nodev 0 0\n",
        encoding="utf-8",
    )
    (etc / "passwd").write_text("root:x:0:0:root:/root:/bin/sh\n", encoding="utf-8")
    (etc / "group").write_text("root:x:0:\n", encoding="utf-8")
    for directory in ("boot", "dev", "home", "proc", "root", "run", "sys", "tmp", "var/log"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    (root / "tmp").chmod(0o1777)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--packages", required=True, type=Path)
    args = parser.parse_args()
    if args.root.exists() and any(args.root.iterdir()):
        parser.error(f"root must be empty: {args.root}")
    args.root.mkdir(parents=True, exist_ok=True)
    write_base_configuration(args.root)
    packages = sorted(args.packages.glob("*.kiln"))
    if not packages:
        parser.error(f"no .kiln packages in {args.packages}")
    for package in packages:
        print(install(package, args.root))
    print(f"assembled Meldra root at {args.root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
