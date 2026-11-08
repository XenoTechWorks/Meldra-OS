#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Report Meldra OS milestone readiness from repository artifacts."""

from __future__ import annotations

from pathlib import Path
import shutil


def qemu_available() -> bool:
    return (
        shutil.which("qemu-system-x86_64") is not None
        or Path("build/host-tools/qemu/bin/qemu-system-x86_64").is_file()
    )


CHECKS = [
    ("Kernel baseline pinned", lambda: Path("sources.lock.toml").is_file()),
    ("Kernel source verified", lambda: any(Path("upstream").glob("linux-*/.meldra-source"))),
    ("Kernel configured", lambda: Path("build/linux/.config").is_file()),
    ("Kernel image built", lambda: Path("build/bzImage").is_file()),
    ("Hearth builds", lambda: Path("build/hearth").is_file()),
    ("Core Kiln packages", lambda: len(list(Path("build/packages").glob("*.kiln"))) >= 2),
    ("Persistent rootfs staged", lambda: Path("build/rootfs/etc/os-release").is_file()),
    ("Base runtime installed", lambda: Path("build/rootfs/bin/sh").is_file()),
    ("Initramfs", lambda: Path("build/meldra-initramfs.cpio.gz").is_file()),
    ("QEMU available", qemu_available),
    ("Boot smoke passed", lambda: Path("build/reports/boot-smoke.ok").is_file()),
    ("Graphics stack packaged", lambda: all(
        Path(path).is_file() for path in (
            "build/packages/wayland-1.26.0.kiln",
            "build/packages/wlroots-0.19.0.kiln",
            "build/packages/pango-1.58.2.kiln",
        )
    )),
    ("Labwc session passed", lambda: Path("build/reports/labwc-smoke.ok").is_file()),
    ("GNOME session passed", lambda: Path("build/reports/gnome-smoke.ok").is_file()),
]


def main() -> int:
    passed = 0
    for label, check in CHECKS:
        result = check()
        passed += result
        print(f"[{'x' if result else ' '}] {label}")
    print(f"\nReadiness gates: {passed}/{len(CHECKS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
