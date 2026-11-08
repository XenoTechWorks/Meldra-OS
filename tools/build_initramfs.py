#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Build Meldra's minimal bootable initramfs."""

from __future__ import annotations

import argparse
import gzip
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile


class BuildError(Exception):
    pass


def require_static(path: Path) -> None:
    result = subprocess.run(["file", str(path)], capture_output=True, text=True, check=False)
    if result.returncode or "statically linked" not in result.stdout:
        raise BuildError(f"{path} must be a statically linked executable")


def install_busybox(busybox: Path, root: Path) -> None:
    require_static(busybox)
    destination = root / "bin/busybox"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(busybox, destination)
    for applet in (
        "sh", "mount", "umount", "getty", "poweroff", "reboot", "echo", "cat",
        "sleep", "uname", "ls", "dmesg",
    ):
        (root / "bin" / applet).symlink_to("busybox")


def build(hearth: Path, busybox: Path, services: Path, output: Path) -> None:
    require_static(hearth)
    with tempfile.TemporaryDirectory(prefix="meldra-rootfs-") as temporary:
        root = Path(temporary)
        for directory in ("dev", "proc", "sys", "run", "tmp", "etc/meldra/services", "sbin"):
            (root / directory).mkdir(parents=True, exist_ok=True)
        install_busybox(busybox, root)
        shutil.copy2(hearth, root / "sbin/hearth")
        (root / "init").symlink_to("sbin/hearth")
        for service in sorted(services.glob("*.service")):
            shutil.copy2(service, root / "etc/meldra/services" / service.name)
        os.chmod(root / "sbin/hearth", stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)

        output.parent.mkdir(parents=True, exist_ok=True)
        epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "0"))
        paths = [root, *sorted(root.rglob("*"), key=lambda item: str(item.relative_to(root)))]
        for path in paths:
            if not path.is_symlink():
                os.utime(path, (epoch, epoch))
        names = b".\0" + b"".join(
            (str(path.relative_to(root)).encode() + b"\0") for path in paths[1:]
        )
        cpio = subprocess.run(
            [
                "cpio", "--null", "-o", "--format=newc", "--owner=0:0",
                "--reproducible", "--quiet",
            ],
            cwd=root,
            input=names,
            capture_output=True,
            check=False,
        )
        if cpio.returncode:
            raise BuildError(cpio.stderr.decode(errors="replace") or "cpio failed")
        with output.open("wb") as stream:
            with gzip.GzipFile(filename="", mode="wb", fileobj=stream, mtime=0) as compressed:
                compressed.write(cpio.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hearth", type=Path, required=True)
    parser.add_argument("--busybox", type=Path, default=Path(os.environ.get("BUSYBOX", "busybox")))
    parser.add_argument("--services", type=Path, default=Path("init/initramfs-services"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    busybox = args.busybox
    if not busybox.is_file():
        resolved = shutil.which(str(busybox))
        if not resolved:
            parser.error("static BusyBox not found; pass --busybox PATH or set BUSYBOX")
        busybox = Path(resolved)
    try:
        build(args.hearth, busybox, args.services, args.output)
    except (BuildError, OSError) as exc:
        print(f"build_initramfs: {exc}", file=sys.stderr)
        return 1
    print(f"built {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
