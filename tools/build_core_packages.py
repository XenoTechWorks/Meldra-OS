#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Build the packages owned by the Meldra OS repository."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kiln.cli import build


def write_manifest(
    path: Path, name: str, version: str, description: str, dependencies: tuple[str, ...] = ()
) -> None:
    dependency_list = ", ".join(f'"{dependency}"' for dependency in dependencies)
    path.write_text(
        "[package]\n"
        f'name = "{name}"\n'
        f'version = "{version}"\n'
        f'description = "{description}"\n'
        'architecture = "x86_64"\n'
        f'dependencies = [{dependency_list}]\n',
        encoding="utf-8",
    )


def build_hearth(binary: Path, work: Path, output: Path) -> None:
    source = work / "hearth"
    target = source / "root/sbin/hearth"
    target.parent.mkdir(parents=True)
    shutil.copy2(binary, target)
    services = source / "root/etc/meldra/services"
    shutil.copytree(Path("init/services"), services)
    write_manifest(
        source / "KILN.toml", "hearth", "0.1.0",
        "Meldra service supervisor and PID 1", ("busybox", "glibc")
    )
    build(source, output / "hearth-0.1.0.kiln")


def build_kiln(work: Path, output: Path) -> None:
    source = work / "kiln"
    module = source / "root/usr/lib/meldra/kiln"
    shutil.copytree(Path("kiln"), module, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    launcher = source / "root/usr/bin/kiln"
    launcher.parent.mkdir(parents=True)
    launcher.write_text(
        "#!/bin/sh\n"
        "export PYTHONPATH=/usr/lib/meldra${PYTHONPATH:+:$PYTHONPATH}\n"
        "exec /usr/bin/python3 -m kiln \"$@\"\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    write_manifest(source / "KILN.toml", "kiln", "0.1.0", "Meldra package manager")
    build(source, output / "kiln-0.1.0.kiln")


def build_busybox(binary: Path, work: Path, output: Path) -> None:
    source = work / "busybox"
    applets = (
        "busybox", "sh", "ash", "mount", "umount", "agetty", "login", "getty",
        "poweroff", "reboot", "echo", "cat", "sleep", "uname", "ls", "dmesg",
        "ip", "udhcpc", "ping", "mkdir", "chmod", "chown", "cp", "mv", "rm",
        "ps", "kill", "tar", "gzip", "sha256sum", "switch_root",
    )
    for applet in applets:
        target = source / "root/bin" / applet
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(binary, target)
    write_manifest(
        source / "KILN.toml", "busybox", "1.36.1", "Meldra bootstrap command-line runtime"
    )
    build(source, output / "busybox-1.36.1.kiln")


def build_wallpapers(work: Path, output: Path) -> None:
    source = work / "meldra-wallpapers"
    target = source / "root/usr/share/backgrounds/meldra"
    target.mkdir(parents=True)
    for wallpaper in sorted(Path("assets/wallpapers").glob("*.png")):
        shutil.copy2(wallpaper, target / wallpaper.name)
    write_manifest(
        source / "KILN.toml", "meldra-wallpapers", "0.1.0",
        "Official Meldra OS desktop wallpapers"
    )
    build(source, output / "meldra-wallpapers-0.1.0.kiln")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hearth", required=True, type=Path)
    parser.add_argument("--busybox", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="meldra-packages-") as directory:
        work = Path(directory)
        build_hearth(args.hearth, work, args.output)
        build_kiln(work, args.output)
        build_wallpapers(work, args.output)
        if args.busybox:
            if not args.busybox.is_file():
                parser.error(f"BusyBox not found: {args.busybox}")
            build_busybox(args.busybox, work, args.output)
    print(f"built core packages in {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
