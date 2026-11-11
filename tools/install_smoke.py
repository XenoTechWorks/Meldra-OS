#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Install the live image onto a fresh virtual disk, boot that disk through UEFI, and log in.

    install_smoke.py [--initrd build/meldra-gnome-initramfs.cpio.gz]

Steps (one VM at a time):
  1. boot the live image with a blank 16 GiB disk and run meldra-install on it over the serial shell,
  2. boot the disk alone with UEFI firmware (the kernel's EFI stub, no -kernel/-initrd),
  3. screenshot the login screen, type a wrong password (the screen must still be the login screen), then the
     right one, and require that the desktop replaces the login screen.

Writes build/reports/install-smoke.ok (or .failed); screenshots land in build/screenshots/<date>/.
"""

from __future__ import annotations

import argparse
import datetime
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import meldra_vm  # noqa: E402
from gnome_smoke import distinct_colours  # noqa: E402

VM = [sys.executable, "tools/meldra_vm.py"]
DISK = "build/vm/install-smoke.img"
PASSWORD = "Smoke-test-pw1"
REPORTS = Path("build/reports")


def vm(*arguments: str, check: bool = True) -> str:
    result = subprocess.run([*VM, *arguments], capture_output=True, text=True)
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or f"meldra_vm {' '.join(arguments)} failed")
    return result.stdout.strip()


def frames_differ(a: Path, b: Path) -> bool:
    return a.read_bytes() != b.read_bytes()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--initrd", default="build/meldra-gnome-initramfs.cpio.gz")
    args = parser.parse_args()
    REPORTS.mkdir(parents=True, exist_ok=True)
    for stale in ("install-smoke.ok", "install-smoke.failed"):
        (REPORTS / stale).unlink(missing_ok=True)
    evidence = [f"date: {datetime.datetime.now().isoformat(timespec='seconds')}", f"initrd: {args.initrd}"]
    passed = False
    try:
        Path(DISK).unlink(missing_ok=True)
        (meldra_vm.STATE / "uefi-vars.fd").unlink(missing_ok=True)
        vm("start", "--initrd", args.initrd, "--disk", DISK, "--disk-size", "16")
        for _ in range(60):
            if "DISKREADY" in vm("serial", "test -b /dev/vda && echo DISKREADY", "--timeout", "3", check=False):
                break
            time.sleep(2)
        result = vm("serial", f"printf '{PASSWORD}\\n' > /tmp/pw; meldra-install --yes --user tester --hostname "
                              "smokebox --password-file /tmp/pw /dev/vda > /tmp/install.log 2>&1; echo EXIT=$?",
                    "--timeout", "400")
        installed = "EXIT=0" in result
        evidence.append(f"meldra-install exit 0: {installed}")
        vm("stop", check=False)
        if installed:
            vm("start", "--disk", DISK, "--boot-disk")
            time.sleep(90)  # firmware, kernel, services, cage and the login screen (software rendering)
            greeter = Path(vm("screenshot", "install-smoke-greeter"))
            greeter_drawn = distinct_colours(greeter) > 8
            meldra_vm.type_text("wrong-password\n")
            time.sleep(8)
            rejected = Path(vm("screenshot", "install-smoke-rejected"))
            meldra_vm.type_text(PASSWORD + "\n")
            time.sleep(50)
            desktop = Path(vm("screenshot", "install-smoke-desktop"))
            logged_in = frames_differ(rejected, desktop) and distinct_colours(desktop) > 8
            evidence += [f"login screen drawn from the installed disk: {greeter_drawn} ({greeter})",
                         f"wrong password kept the login screen: {rejected}",
                         f"right password reached the desktop: {logged_in} ({desktop})"]
            passed = greeter_drawn and logged_in
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        evidence.append(f"error: {error}")
    finally:
        vm("stop", check=False)
        Path(DISK).unlink(missing_ok=True)
    report = REPORTS / ("install-smoke.ok" if passed else "install-smoke.failed")
    report.write_text("\n".join(evidence) + "\n", encoding="utf-8")
    print("\n".join(evidence))
    print(f"{'PASS' if passed else 'FAIL'}: {report}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
