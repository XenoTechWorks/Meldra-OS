#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Exercise encrypted-root boot and non-destructive alongside installation in QEMU.

The regular install smoke owns the graphical login test. This companion keeps
the two storage-risk cases focused: an encrypted UEFI install must unlock into
ReGreet, while an alongside install must preserve a sentinel filesystem and
place Meldra's loader alongside it on the existing ESP.
"""

from __future__ import annotations

import argparse
import datetime
from pathlib import Path
import re
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import meldra_vm  # noqa: E402
from gnome_smoke import distinct_colours  # noqa: E402

VM = [sys.executable, "tools/meldra_vm.py"]
PASSWORD = "Storage-smoke-pw1"
KEYBOARD_LAYOUT = "de"
TIMEZONE = "America/New_York"
REPORTS = Path("build/reports")
CASE_DISKS = {
    "encrypted": Path("build/vm/install-encrypted.img"),
    "alongside": Path("build/vm/install-alongside.img"),
    "encrypted-alongside": Path("build/vm/install-encrypted-alongside.img"),
}


def vm(*arguments: str, check: bool = True) -> str:
    result = subprocess.run([*VM, *arguments], capture_output=True, text=True)
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or f"meldra_vm {' '.join(arguments)} failed")
    return result.stdout.strip()


def wait_for_disk(timeout: float = 120) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if "DISKREADY" in vm("serial", "test -b /dev/vda && echo DISKREADY", "--timeout", "3", check=False):
            return
        time.sleep(2)
    raise RuntimeError("live system did not expose /dev/vda")


def run_installer(extra: str, timeout: float = 900, *, test_pbkdf: bool = False) -> tuple[bool, str]:
    environment = "MELDRA_INSTALL_TEST_PBKDF=1 " if test_pbkdf else ""
    command = ("rm -f /tmp/install.exit /tmp/install.log; "
               f"printf '{PASSWORD}\\n' > /tmp/storage-pw; "
               f"({environment}/usr/sbin/meldra-install --yes --user tester --hostname storagebox "
               f"--password-file /tmp/storage-pw {extra} /dev/vda > /tmp/install.log 2>&1; "
               "echo $? > /tmp/install.exit) & echo INSTALLER-STARTED")
    if "INSTALLER-STARTED" not in vm("serial", command, "--timeout", "10"):
        return False, "installer did not start"
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        last = vm("serial", "if [ -f /tmp/install.exit ]; then "
                  "read -r code < /tmp/install.exit; printf 'RESULT-%s-READY\\n' \"$code\"; "
                  "else tail -n 2 /tmp/install.log 2>/dev/null; echo RUNNING; fi",
                  "--timeout", "10", check=False)
        # Be tolerant of CR/LF inserted by a serial console around the marker.
        result = re.search(r"RESULT-\s*([0-9]+)\s*-READY", last)
        if result:
            return result.group(1) == "0", last
        time.sleep(5)
    return False, last + "\nTIMEOUT"


def encrypted(initrd: str, disk: Path, evidence: list[str]) -> bool:
    disk.unlink(missing_ok=True)
    (meldra_vm.STATE / "uefi-vars.fd").unlink(missing_ok=True)
    vm("start", "--uefi", "--initrd", initrd, "--disk", str(disk), "--disk-size", "16")
    wait_for_disk()
    installed, status = run_installer(
        f"--encrypt --keyboard-layout {KEYBOARD_LAYOUT} --timezone {TIMEZONE}", test_pbkdf=True
    )
    evidence.append(f"encrypted installer exit 0: {installed}")
    if not installed:
        evidence.append("encrypted installer status:\n" + status)
        evidence.append("encrypted installer log:\n" + vm("serial", "cat /tmp/install.log", "--timeout", "10", check=False))
        vm("stop", check=False)
        return False
    encrypted_layout = "ENCRYPTED-LAYOUT-OK" in vm(
        "serial",
        "blkid -p -s TYPE -o value /dev/vda2 | grep -qx crypto_LUKS && "
        "mkdir -p /tmp/esp-check && mount -o ro /dev/vda1 /tmp/esp-check && "
        "test -f /tmp/esp-check/EFI/Meldra/meldra.efi && "
        "test -f /tmp/esp-check/EFI/Meldra/initrd.img && echo ENCRYPTED-LAYOUT-OK",
        "--timeout", "30", check=False,
    )
    evidence.append(f"encrypted disk and EFI boot artifacts present: {encrypted_layout}")
    vm("stop", check=False)

    vm("start", "--disk", str(disk), "--boot-disk")
    # The EFI stub and early userspace draw the password prompt on the firmware console.
    time.sleep(20)
    prompt = Path(vm("screenshot", "encrypted-install-unlock-prompt"))
    meldra_vm.type_text("definitely-wrong\n")
    time.sleep(8)
    rejected = Path(vm("screenshot", "encrypted-install-wrong-password"))
    wrong_password_rejected = distinct_colours(rejected) > 1
    evidence.append(
        f"wrong encrypted-root password stayed at unlock prompt: {wrong_password_rejected} "
        f"({prompt}, {rejected})"
    )
    meldra_vm.type_text(PASSWORD + "\n")
    time.sleep(90)
    screen = Path(vm("screenshot", "encrypted-install-regreet"))
    reached_greeter = distinct_colours(screen) > 8
    evidence.append(f"encrypted root unlocked into ReGreet: {reached_greeter} ({screen})")
    vm("stop", check=False)
    return encrypted_layout and wrong_password_rejected and reached_greeter


def alongside(initrd: str, disk: Path, evidence: list[str]) -> bool:
    disk.unlink(missing_ok=True)
    (meldra_vm.STATE / "uefi-vars.fd").unlink(missing_ok=True)
    vm("start", "--uefi", "--initrd", initrd, "--disk", str(disk), "--disk-size", "16")
    wait_for_disk()
    prepare = r"""
sfdisk --quiet /dev/vda <<'EOF'
label: gpt
size=512MiB, type=uefi, name="shared-esp"
size=128MiB, type=linux, name="keep-me"
type=linux, name="meldra-space"
EOF
udevadm settle --timeout=10
mkfs.vfat -F 32 -n SHARED-ESP /dev/vda1 >/dev/null
mkfs.ext4 -q -F -L KEEP-ME /dev/vda2
mkdir -p /tmp/keep
mount /dev/vda2 /tmp/keep
echo MELDRA-MUST-PRESERVE-THIS > /tmp/keep/sentinel
umount /tmp/keep
echo PREPARED
""".strip()
    if "PREPARED" not in vm("serial", prepare, "--timeout", "60"):
        raise RuntimeError("could not prepare the alongside partition layout")
    rejected, rejection_status = run_installer(
        "--alongside --esp /dev/vda1 --root-partition /dev/vda1"
    )
    unsafe_selection_rejected = not rejected and "RESULT-" in rejection_status
    sentinel_survived_rejection = "SENTINEL-OK" in vm(
        "serial",
        "mkdir -p /tmp/keep-rejection; mount -o ro /dev/vda2 /tmp/keep-rejection; "
        "test \"$(cat /tmp/keep-rejection/sentinel)\" = MELDRA-MUST-PRESERVE-THIS && "
        "echo SENTINEL-OK; umount /tmp/keep-rejection",
        "--timeout", "15", check=False,
    )
    evidence.append(
        "unsafe alongside selection rejected without touching existing data: "
        f"{unsafe_selection_rejected and sentinel_survived_rejection}"
    )
    installed, status = run_installer(
        "--alongside --esp /dev/vda1 --root-partition /dev/vda3 "
        f"--keyboard-layout {KEYBOARD_LAYOUT} --timezone {TIMEZONE}"
    )
    evidence.append(f"alongside installer exit 0: {installed}")
    if not installed:
        evidence.append("alongside installer status:\n" + status)
        evidence.append("alongside installer log:\n" + vm("serial", "cat /tmp/install.log", "--timeout", "10", check=False))
        vm("stop", check=False)
        return False
    verify = r"""
mkdir -p /tmp/keep /tmp/esp /tmp/meldra
mount -o ro /dev/vda2 /tmp/keep
mount -o ro /dev/vda1 /tmp/esp
mount -o ro /dev/vda3 /tmp/meldra
test "$(cat /tmp/keep/sentinel)" = MELDRA-MUST-PRESERVE-THIS &&
test "$(blkid -s LABEL -o value /dev/vda2)" = KEEP-ME &&
test -f /tmp/esp/EFI/Meldra/meldra.efi &&
test -f /tmp/meldra/etc/hostname &&
test "$(cat /tmp/meldra/etc/timezone)" = America/New_York &&
test "$(readlink /tmp/meldra/etc/localtime)" = ../usr/share/zoneinfo/America/New_York &&
grep -qx 'KEYMAP=de' /tmp/meldra/etc/vconsole.conf &&
grep -qx 'XKB_DEFAULT_LAYOUT DEFAULT=de' /tmp/meldra/etc/meldra/session-env.conf &&
grep -Fqx "sources=[('xkb', 'de')]" /tmp/meldra/etc/dconf/db/local.d/00-meldra-input &&
echo PRESERVED
""".strip()
    preserved = "PRESERVED" in vm("serial", verify, "--timeout", "30")
    evidence.append(
        "alongside preserved existing filesystem and installed Meldra with locale settings: "
        f"{preserved}"
    )
    vm("stop", check=False)
    if not preserved:
        return False

    vm("start", "--disk", str(disk), "--boot-disk")
    time.sleep(90)
    screen = Path(vm("screenshot", "alongside-install-regreet"))
    reached_greeter = distinct_colours(screen) > 8
    evidence.append(f"alongside installation booted into ReGreet: {reached_greeter} ({screen})")
    vm("stop", check=False)
    return unsafe_selection_rejected and sentinel_survived_rejection and reached_greeter


def encrypted_alongside(initrd: str, disk: Path, evidence: list[str]) -> bool:
    """Install an encrypted root into free partitions without changing an existing filesystem."""
    disk.unlink(missing_ok=True)
    (meldra_vm.STATE / "uefi-vars.fd").unlink(missing_ok=True)
    vm("start", "--uefi", "--initrd", initrd, "--disk", str(disk), "--disk-size", "16")
    wait_for_disk()
    prepare = r"""
sfdisk --quiet /dev/vda <<'EOF'
label: gpt
size=512MiB, type=uefi, name="shared-esp"
size=128MiB, type=linux, name="keep-me"
type=linux, name="meldra-space"
EOF
udevadm settle --timeout=10
mkfs.vfat -F 32 -n SHARED-ESP /dev/vda1 >/dev/null
mkfs.ext4 -q -F -L KEEP-ME /dev/vda2
mkdir -p /tmp/keep
mount /dev/vda2 /tmp/keep
echo ENCRYPTED-ALONGSIDE-SENTINEL > /tmp/keep/sentinel
umount /tmp/keep
echo PREPARED
""".strip()
    if "PREPARED" not in vm("serial", prepare, "--timeout", "60"):
        raise RuntimeError("could not prepare the encrypted-alongside partition layout")
    installed, status = run_installer(
        "--encrypt --alongside --esp /dev/vda1 --root-partition /dev/vda3", test_pbkdf=True
    )
    evidence.append(f"encrypted alongside installer exit 0: {installed}")
    if not installed:
        evidence.append("encrypted alongside installer status:\n" + status)
        evidence.append(
            "encrypted alongside installer log:\n"
            + vm("serial", "cat /tmp/install.log", "--timeout", "10", check=False)
        )
        vm("stop", check=False)
        return False
    preserved_and_encrypted = "ENCRYPTED-ALONGSIDE-OK" in vm(
        "serial",
        "mkdir -p /tmp/keep-check; mount -o ro /dev/vda2 /tmp/keep-check; "
        "test \"$(cat /tmp/keep-check/sentinel)\" = ENCRYPTED-ALONGSIDE-SENTINEL && "
        "blkid -p -s TYPE -o value /dev/vda3 | grep -qx crypto_LUKS && "
        "echo ENCRYPTED-ALONGSIDE-OK",
        "--timeout", "30", check=False,
    )
    evidence.append(f"encrypted alongside install preserved existing data: {preserved_and_encrypted}")
    vm("stop", check=False)
    if not preserved_and_encrypted:
        return False

    vm("start", "--disk", str(disk), "--boot-disk")
    time.sleep(20)
    meldra_vm.type_text(PASSWORD + "\n")
    time.sleep(90)
    screen = Path(vm("screenshot", "encrypted-alongside-regreet"))
    reached_greeter = distinct_colours(screen) > 8
    evidence.append(f"encrypted alongside installation unlocked into ReGreet: {reached_greeter} ({screen})")
    vm("stop", check=False)
    return reached_greeter


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--initrd", default="build/meldra-gnome-initramfs.cpio.gz")
    parser.add_argument(
        "--case", choices=("encrypted", "alongside", "encrypted-alongside", "all"), default="all"
    )
    parser.add_argument("--keep-disks", action="store_true", help="keep generated disk images for debugging")
    args = parser.parse_args()
    selected = tuple(CASE_DISKS) if args.case == "all" else (args.case,)
    REPORTS.mkdir(parents=True, exist_ok=True)
    for stale in ("install-storage.ok", "install-storage.failed"):
        (REPORTS / stale).unlink(missing_ok=True)
    evidence = [f"date: {datetime.datetime.now().isoformat(timespec='seconds')}", f"initrd: {args.initrd}"]
    passed = True
    try:
        if args.case in ("encrypted", "all"):
            passed &= encrypted(args.initrd, CASE_DISKS["encrypted"], evidence)
        if args.case in ("alongside", "all"):
            passed &= alongside(args.initrd, CASE_DISKS["alongside"], evidence)
        if args.case in ("encrypted-alongside", "all"):
            passed &= encrypted_alongside(
                args.initrd, CASE_DISKS["encrypted-alongside"], evidence
            )
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        evidence.append(f"error: {error}")
        passed = False
    finally:
        vm("stop", check=False)
        if not args.keep_disks:
            for name in selected:
                CASE_DISKS[name].unlink(missing_ok=True)
    report = REPORTS / ("install-storage.ok" if passed else "install-storage.failed")
    report.write_text("\n".join(evidence) + "\n", encoding="utf-8")
    print("\n".join(evidence))
    print(f"{'PASS' if passed else 'FAIL'}: {report}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
