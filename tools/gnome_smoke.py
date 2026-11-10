#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Boot the GNOME image once and check that GNOME Shell starts, draws, and reacts to the mouse and the keyboard.

    gnome_smoke.py [--initrd build/meldra-gnome-initramfs.cpio.gz] [--timeout 240]

Steps: start one VM, wait for "GNOME Shell started" in the console log, screenshot, press Super through the QEMU
monitor (which toggles the Activities Overview), screenshot again, stop the VM. Passes only if the Shell started,
the first frame is not a single flat colour, and the two frames differ. Writes build/reports/gnome-smoke.ok (or
.failed) with the evidence. Screenshots land in build/screenshots/<date>/.
"""

from __future__ import annotations

import argparse
import datetime
from pathlib import Path
import subprocess
import sys
import time
import zlib

VM = [sys.executable, "tools/meldra_vm.py"]
REPORTS = Path("build/reports")


def vm(*arguments: str, check: bool = True) -> str:
    result = subprocess.run([*VM, *arguments], capture_output=True, text=True)
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or f"meldra_vm {' '.join(arguments)} failed")
    return result.stdout.strip()


def monitor_call(command: str) -> None:
    subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0,'tools'); import meldra_vm; "
                    f"meldra_vm.monitor({command!r}, wait=0.5)"], check=True)


def distinct_colours(png: Path) -> int:
    """Count distinct RGB values in a PNG written by meldra_vm (8-bit RGB, filter byte 0 on every row)."""
    data = png.read_bytes()
    position, idat, width = 8, b"", 0
    while position < len(data):
        length = int.from_bytes(data[position:position + 4], "big")
        kind = data[position + 4:position + 8]
        body = data[position + 8:position + 8 + length]
        if kind == b"IHDR":
            width = int.from_bytes(body[:4], "big")
        elif kind == b"IDAT":
            idat += body
        position += 12 + length
    raw = zlib.decompress(idat)
    stride = width * 3 + 1
    colours = set()
    for row in range(0, len(raw), stride * 8):          # every 8th row is plenty for this check
        line = raw[row + 1:row + stride]
        for x in range(0, len(line) - 2, 3 * 8):
            colours.add(line[x:x + 3])
    return len(colours)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--initrd", default="build/meldra-gnome-initramfs.cpio.gz")
    parser.add_argument("--timeout", type=float, default=240)
    args = parser.parse_args()
    REPORTS.mkdir(parents=True, exist_ok=True)
    for stale in ("gnome-smoke.ok", "gnome-smoke.failed"):
        (REPORTS / stale).unlink(missing_ok=True)
    evidence: list[str] = [f"date: {datetime.datetime.now().isoformat(timespec='seconds')}", f"initrd: {args.initrd}"]
    passed = False
    try:
        evidence.append(vm("start", "--initrd", args.initrd))
        deadline = time.time() + args.timeout
        started = False
        while time.time() < deadline:
            log = vm("log", "4000")
            if "GNOME Shell started at" in log:
                started = True
                break
            if "95-gnome exited" in log:
                break
            time.sleep(3)
        evidence.append(f"shell started: {started}")
        if started:
            time.sleep(12)  # let the first frames settle (software rendering is slow)
            first = Path(vm("screenshot", "smoke-started"))
            colours = distinct_colours(first)
            vm("serial", "true")
            # Mouse: the tablet is absolute (0..32767 on each axis). Click the app-grid button at the bottom of the
            # Overview (640,738 on the 1280x800 screen); the Shell should switch to the app grid.
            subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0,'tools'); import meldra_vm; "
                            "meldra_vm.click(640, 738)"], check=True)
            time.sleep(5)
            clicked = Path(vm("screenshot", "smoke-after-click"))
            mouse_worked = first.read_bytes() != clicked.read_bytes()
            monitor_call("sendkey meta_l")
            time.sleep(4)
            second = Path(vm("screenshot", "smoke-after-super"))
            changed = clicked.read_bytes() != second.read_bytes()
            evidence += [f"first frame: {first} ({colours} distinct colours sampled)",
                         f"after mouse click: {clicked} (frame changed: {mouse_worked})",
                         f"after Super: {second} (frame changed: {changed})"]
            passed = colours > 8 and changed and mouse_worked
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        evidence.append(f"error: {error}")
    finally:
        log_tail = vm("log", "60", check=False)
        vm("stop", check=False)
    report = REPORTS / ("gnome-smoke.ok" if passed else "gnome-smoke.failed")
    report.write_text("\n".join(evidence) + "\n\n--- console (last lines) ---\n" + log_tail + "\n")
    print("\n".join(evidence))
    print(f"{'PASS' if passed else 'FAIL'}: {report}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
