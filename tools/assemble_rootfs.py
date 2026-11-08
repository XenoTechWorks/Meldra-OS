#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Assemble the persistent Meldra base filesystem from Kiln packages."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import tarfile
import tomllib

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
    (etc / "group").write_text(
        "root:x:0:\nkmem:x:2:\ntty:x:5:\ndisk:x:6:\nlp:x:7:\n"
        "dialout:x:18:\nvideo:x:27:\ninput:x:28:\naudio:x:29:\n"
        "render:x:104:\nclock:x:105:\n",
        encoding="utf-8",
    )
    for directory in ("boot", "dev", "home", "proc", "root", "run", "sys", "tmp", "var/log"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    (root / "tmp").chmod(0o1777)
    (root / "init").symlink_to("sbin/hearth")


def package_manifest(path: Path) -> dict:
    with tarfile.open(path, "r:*") as archive:
        stream = archive.extractfile("KILN.toml")
        if stream is None:
            raise RuntimeError(f"package has no manifest: {path}")
        return tomllib.loads(stream.read().decode("utf-8"))["package"]


def dependency_order(packages: list[Path]) -> list[Path]:
    by_name: dict[str, tuple[Path, dict]] = {}
    for path in packages:
        package = package_manifest(path)
        name = package["name"]
        if name in by_name:
            raise RuntimeError(f"duplicate package name: {name}")
        by_name[name] = (path, package)
    remaining = dict(by_name)
    installed: set[str] = set()
    ordered: list[Path] = []
    while remaining:
        ready = sorted(
            name for name, (_, package) in remaining.items()
            if set(package.get("dependencies", [])) <= installed
        )
        if not ready:
            details = ", ".join(
                f"{name} -> {package.get('dependencies', [])}"
                for name, (_, package) in sorted(remaining.items())
            )
            raise RuntimeError(f"unresolved package dependencies: {details}")
        for name in ready:
            path, _ = remaining.pop(name)
            ordered.append(path)
            installed.add(name)
    return ordered


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
    try:
        ordered = dependency_order(packages)
        for package in ordered:
            print(install(package, args.root))
    except RuntimeError as exc:
        parser.error(str(exc))
    print(f"assembled Meldra root at {args.root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
