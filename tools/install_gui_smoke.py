#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Drive the graphical installer from GNOME's app grid, then boot and log in.

This deliberately launches the application through GNOME rather than a serial
shell: polkit must see the installer as part of the active local session.
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
DISK = Path("build/vm/install-gui-smoke.img")
PASSWORD = "Gui-smoke-pw1"
REPORTS = Path("build/reports")


def vm(*arguments: str, check: bool = True) -> str:
    result = subprocess.run([*VM, *arguments], capture_output=True, text=True)
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or f"meldra_vm {' '.join(arguments)} failed")
    return result.stdout.strip()


def wait_for_live(timeout: float = 180) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        probe = vm(
            "serial",
            "test -b /dev/vda && "
            "grep -q 'GNOME Shell started at' /home/meldra/.local/state/meldra/session.log 2>/dev/null && "
            "echo LIVE-READY",
            "--timeout", "3", check=False,
        )
        if "LIVE-READY" in probe:
            return
        time.sleep(3)
    raise RuntimeError("live GNOME session did not become ready")


def screenshot(name: str) -> Path:
    return Path(vm("screenshot", name))


def click(x: int, y: int, pause: float = 1.0) -> None:
    meldra_vm.click(x, y)
    time.sleep(pause)


def replace_at(x: int, y: int, text: str) -> None:
    click(x, y, 0.2)
    meldra_vm.monitor("sendkey ctrl-a")
    meldra_vm.type_text(text, delay=0.04)


def choose_next(x: int, y: int, steps: int) -> None:
    """Choose a later item from a GtkDropDown using its keyboard interface."""
    click(x, y, 0.3)
    for _ in range(steps):
        meldra_vm.monitor("sendkey down", wait=0.2)
    meldra_vm.monitor("sendkey ret")
    time.sleep(0.5)


def launch_from_app_grid() -> None:
    # Search from GNOME's Overview instead of relying on app-grid coordinates:
    # hosted runners and local displays do not necessarily choose the same mode.
    meldra_vm.monitor("sendkey esc")
    time.sleep(1)
    meldra_vm.monitor("sendkey meta_l")
    time.sleep(2)
    meldra_vm.type_text("Install Meldra", delay=0.04)
    meldra_vm.monitor("sendkey ret")
    deadline = time.time() + 30
    while time.time() < deadline:
        if "GUI-RUNNING" in vm(
            "serial", "pidof meldra-installer-gui >/dev/null && echo GUI-RUNNING", check=False
        ):
            # Give GTK and Mutter time to map the first window on slower TCG runners.
            time.sleep(8)
            # GNOME can leave keyboard-launched applications behind the Overview
            # until it finishes the transition. Escape exposes the application.
            meldra_vm.monitor("sendkey esc")
            time.sleep(3)
            window_deadline = time.time() + 60
            while time.time() < window_deadline:
                running = "GUI-RUNNING" in vm(
                    "serial", "pidof meldra-installer-gui >/dev/null && echo GUI-RUNNING", check=False
                )
                frame = screenshot("install-gui-window-ready")
                # A bare GNOME Overview compresses to less than 10 KiB in this
                # software-rendered image; the light installer window is much larger.
                if running and frame.stat().st_size > 20_000:
                    return
                if not running:
                    raise RuntimeError("Install Meldra exited before its window appeared")
                meldra_vm.monitor("sendkey esc")
                time.sleep(3)
            raise RuntimeError("Install Meldra process started but its window did not appear")
        time.sleep(2)
    raise RuntimeError("Install Meldra did not launch from the app grid search")


def drive_installer(evidence: list[str]) -> bool:
    launch_from_app_grid()
    welcome = screenshot("install-gui-welcome")
    evidence.append(f"installer launched from GNOME app grid: True ({welcome})")

    click(905, 727, 1.5)  # Welcome: Continue
    click(905, 727, 1.5)  # Disk (the only suitable disk is selected): Continue

    replace_at(500, 243, "tester")
    replace_at(500, 323, PASSWORD)
    replace_at(500, 371, "not-the-same")
    replace_at(500, 451, "guibox")
    choose_next(500, 531, 2)  # us -> de
    choose_next(500, 611, 1)  # UTC -> America/New_York
    click(905, 727, 2)
    validation = screenshot("install-gui-password-validation")
    mismatch_rejected = "NO-PARTITIONS" in vm(
        "serial", "test ! -b /dev/vda1 && echo NO-PARTITIONS", "--timeout", "5", check=False
    )
    evidence.append(f"mismatched passwords rejected before disk changes: {mismatch_rejected} ({validation})")

    replace_at(500, 371, PASSWORD)
    click(905, 727, 2)    # Account: Continue
    confirmation = screenshot("install-gui-confirmation")

    click(897, 727, 1)    # Disabled Erase and install button
    erase_guard = "NO-PARTITIONS" in vm(
        "serial", "test ! -b /dev/vda1 && echo NO-PARTITIONS", "--timeout", "5", check=False
    )
    evidence.append(f"erase acknowledgement gates installation: {erase_guard} ({confirmation})")
    if not mismatch_rejected or not erase_guard:
        return False

    click(340, 315, 0.5)  # I understand that everything will be erased
    click(897, 727, 1)    # Erase and install
    progress = screenshot("install-gui-progress")

    deadline = time.time() + 600
    installed = False
    while time.time() < deadline:
        probe = vm(
            "serial",
            "if [ -b /dev/vda2 ] && ! pgrep -f '^/usr/sbin/meldra-install ' >/dev/null; then "
            "mkdir -p /tmp/gui-installed; mount -o ro /dev/vda2 /tmp/gui-installed 2>/dev/null || true; "
            "test \"$(cat /tmp/gui-installed/etc/hostname 2>/dev/null)\" = guibox && "
            "grep -q '^tester:' /tmp/gui-installed/etc/passwd && "
            "test \"$(cat /tmp/gui-installed/etc/timezone 2>/dev/null)\" = America/New_York && "
            "grep -qx 'KEYMAP=de' /tmp/gui-installed/etc/vconsole.conf && "
            "grep -qx 'XKB_DEFAULT_LAYOUT DEFAULT=de' /tmp/gui-installed/etc/meldra/session-env.conf && "
            "echo GUI-INSTALLED; "
            "umount /tmp/gui-installed 2>/dev/null || true; fi",
            "--timeout", "10", check=False,
        )
        if "GUI-INSTALLED" in probe:
            installed = True
            break
        time.sleep(5)
    time.sleep(2)
    done = screenshot("install-gui-done")
    evidence.append(
        "graphical installer wrote the requested account, hostname, keyboard, and timezone: "
        f"{installed}"
    )
    evidence.append(f"installer progress and completion frames: {progress}, {done}")
    return installed and progress.read_bytes() != done.read_bytes()


def verify_installed_boot(evidence: list[str]) -> bool:
    vm("stop", check=False)
    vm("start", "--disk", str(DISK), "--boot-disk")
    time.sleep(90)
    greeter = screenshot("install-gui-greeter")
    greeter_drawn = distinct_colours(greeter) > 8
    meldra_vm.type_text("wrong-password\n")
    time.sleep(8)
    rejected = screenshot("install-gui-login-rejected")
    meldra_vm.type_text(PASSWORD + "\n")
    time.sleep(50)
    desktop = screenshot("install-gui-desktop")
    logged_in = rejected.read_bytes() != desktop.read_bytes() and distinct_colours(desktop) > 8
    evidence.append(f"GUI-installed disk booted to ReGreet: {greeter_drawn} ({greeter})")
    evidence.append(f"wrong login password remained at ReGreet: {rejected}")
    evidence.append(f"GUI-created account logged into GNOME: {logged_in} ({desktop})")
    return greeter_drawn and logged_in


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--initrd", default="build/meldra-gnome-initramfs.cpio.gz")
    parser.add_argument("--keep-disk", action="store_true", help="keep the installed disk image for debugging")
    args = parser.parse_args()
    REPORTS.mkdir(parents=True, exist_ok=True)
    for stale in ("install-gui-smoke.ok", "install-gui-smoke.failed"):
        (REPORTS / stale).unlink(missing_ok=True)
    evidence = [f"date: {datetime.datetime.now().isoformat(timespec='seconds')}", f"initrd: {args.initrd}"]
    passed = False
    try:
        DISK.unlink(missing_ok=True)
        (meldra_vm.STATE / "uefi-vars.fd").unlink(missing_ok=True)
        vm("start", "--uefi", "--initrd", args.initrd, "--disk", str(DISK), "--disk-size", "16")
        wait_for_live()
        passed = drive_installer(evidence)
        if passed:
            passed = verify_installed_boot(evidence)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        evidence.append(f"error: {error}")
        passed = False
    finally:
        vm("stop", check=False)
        if not args.keep_disk:
            DISK.unlink(missing_ok=True)
    report = REPORTS / ("install-gui-smoke.ok" if passed else "install-gui-smoke.failed")
    report.write_text("\n".join(evidence) + "\n", encoding="utf-8")
    print("\n".join(evidence))
    print(f"{'PASS' if passed else 'FAIL'}: {report}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
