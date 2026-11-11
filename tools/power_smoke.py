#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Verify elogind poweroff and reboot requests shut down the Meldra VM."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
import time

VM = [sys.executable, "tools/meldra_vm.py"]


def vm(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([*VM, *args], capture_output=True, text=True)


def exercise(action: str, initrd: Path, timeout: float) -> None:
    started = vm("start", "--initrd", str(initrd), "--memory", "4096", "--cpus", "2")
    if started.returncode:
        raise RuntimeError(started.stderr.strip() or started.stdout.strip())
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            result = vm("serial", "loginctl list-seats >/dev/null 2>&1 && echo READY", "--timeout", "3")
            if "READY" in result.stdout:
                break
            time.sleep(1)
        else:
            raise RuntimeError(f"elogind did not become ready for {action}")
        vm("serial", f"loginctl {action}", "--timeout", "3")
        deadline = time.time() + 15
        while time.time() < deadline:
            if "not running" in vm("status").stdout:
                # QEMU runs with -no-reboot, so it exits on a restart too: the kernel's last words tell them apart.
                log = vm("log", "40").stdout
                expected = "reboot: Power down" if action == "poweroff" else "reboot: Restarting system"
                if expected not in log:
                    raise RuntimeError(f"VM stopped, but the kernel did not print {expected!r}")
                print(f"PASS: elogind {action} stopped the VM ({expected})")
                return
            time.sleep(0.5)
        raise RuntimeError(f"VM did not stop after loginctl {action}")
    finally:
        vm("stop")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--initrd", type=Path, default=Path("build/meldra-gnome-initramfs.cpio.gz"))
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    reports = Path("build/reports")
    reports.mkdir(parents=True, exist_ok=True)
    for stale in ("gnome-power.ok", "gnome-power.failed"):
        (reports / stale).unlink(missing_ok=True)
    evidence = [f"initrd: {args.initrd}"]
    passed = True
    for action in ("poweroff", "reboot"):
        try:
            exercise(action, args.initrd, args.timeout)
            evidence.append(f"{action}: PASS")
        except RuntimeError as error:
            evidence.append(f"{action}: FAIL ({error})")
            passed = False
    report = reports / ("gnome-power.ok" if passed else "gnome-power.failed")
    report.write_text("\n".join(evidence) + "\n", encoding="utf-8")
    print("\n".join(evidence))
    print(f"{'PASS' if passed else 'FAIL'}: {report}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
