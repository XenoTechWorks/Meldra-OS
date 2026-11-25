#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Screen-reader acceptance test: boot the GNOME image, turn Orca on the way a user would, and check it speaks.

    orca_smoke.py [--initrd build/meldra-gnome-initramfs.cpio.gz] [--timeout 300]

One VM, with an emulated Intel HDA card whose output QEMU records to build/reports/orca-audio.wav. Checks, in order:

  1. packages     orca, speech-dispatcher and espeak-ng are installed, and Python can import gi and pyatspi;
  2. synthesis    espeak-ng renders a phrase to audio samples;
  3. at-spi       the AT-SPI registry lists GNOME Shell as an accessible application;
  4. enable       setting org.gnome.desktop.a11y.applications screen-reader-enabled (Settings' switch, also
                  Super+Alt+S) makes GNOME Shell start Orca, and Orca starts speech-dispatcher;
  5. speech       after Orca starts, the sound card plays real signal (not silence);
  6. focus        pressing Super (Activities) makes Orca speak again.

Writes build/reports/orca-smoke.ok (or .failed) with the evidence; screenshots go to build/screenshots/<date>/.
"""

from __future__ import annotations

import argparse
import datetime
from pathlib import Path
import shlex
import struct
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gnome_smoke import monitor_call, vm  # noqa: E402

REPORTS = Path("build/reports")
WAV = REPORTS / "orca-audio.wav"
USER = "meldra"

# Runs a command as the logged-in user with the GNOME session's environment (bus, runtime dir), taken from the
# running gnome-shell. The image has no su/runuser, so Python drops the privileges.
DROP_TO_USER = (
    "import os, sys, pwd; u = pwd.getpwnam(sys.argv[1]); env = dict(l.split('=', 1) for l in "
    "open('/proc/%s/environ' % sys.argv[2]).read().split('\\0') if '=' in l); "
    "env.update(HOME=u.pw_dir, USER=u.pw_name, LOGNAME=u.pw_name); os.chdir(u.pw_dir); "
    "os.initgroups(u.pw_name, u.pw_gid); os.setgid(u.pw_gid); os.setuid(u.pw_uid); "
    "os.execve('/bin/sh', ['sh', '-c', sys.argv[3]], env)"
)
AS_USER = (
    "as_user() { python3 -c " + shlex.quote(DROP_TO_USER) + f" {USER} "
    f"$(pgrep -u {USER} -x gnome-shell | head -n1) \"$1\"; }}; "
)

ATSPI_PROBE = (
    "import pyatspi; d = pyatspi.Registry.getDesktop(0); "
    "print('ATSPI-APPS:', ','.join(sorted({(a.name or '?') for a in d if a is not None})))"
)


def guest(command: str, timeout: float = 20) -> str:
    return vm("serial", command, "--timeout", str(timeout), check=False)


def user(command: str, timeout: float = 20) -> str:
    return guest(AS_USER + "as_user " + shlex.quote(command), timeout)


def wav_peak(path: Path, start: int) -> tuple[int, int]:
    """Peak absolute 16-bit sample and count of loud samples in the WAV data after byte `start`.

    QEMU's wav backend writes 16-bit little-endian PCM and only fixes the header when it closes, so the data is
    read raw; `start` is a file size taken earlier, which separates silence-before from speech-after."""
    data = path.read_bytes()[max(start, 44):]
    data = data[: len(data) - len(data) % 2]
    if not data:
        return 0, 0
    samples = struct.unpack(f"<{len(data) // 2}h", data)
    peak = max(abs(s) for s in samples)
    loud = sum(1 for s in samples if abs(s) > 1000)
    return peak, loud


def wait_for(predicate, timeout: float, interval: float = 3) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--initrd", default="build/meldra-gnome-initramfs.cpio.gz")
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args()
    REPORTS.mkdir(parents=True, exist_ok=True)
    for stale in ("orca-smoke.ok", "orca-smoke.failed"):
        (REPORTS / stale).unlink(missing_ok=True)
    WAV.unlink(missing_ok=True)
    evidence = [f"date: {datetime.datetime.now().isoformat(timespec='seconds')}", f"initrd: {args.initrd}"]
    results: dict[str, bool] = {}
    log_tail = ""
    try:
        evidence.append(vm("start", "--initrd", args.initrd, "--audio-wav", str(WAV.resolve())))
        shell_up = wait_for(lambda: "GNOME-UP" in guest(
            f"grep -q 'GNOME Shell started at' /home/{USER}/.local/state/meldra/session.log && echo GNOME-UP"),
            args.timeout)
        evidence.append(f"GNOME Shell started: {shell_up}")
        if not shell_up:
            raise RuntimeError("GNOME Shell did not start")
        time.sleep(10)

        out = guest("for p in orca speech-dispatcher espeak-ng; do command -v $p || echo MISSING-$p; done; "
                    "python3 -c 'import gi, pyatspi, speechd; print(\"PY-OK\")'")
        lines = [line.strip() for line in out.splitlines()]
        results["packages"] = (not any(line.startswith("MISSING-") for line in lines)
                               and "PY-OK" in lines)
        evidence.append(f"1. packages: {results['packages']}\n{out}")

        out = guest("espeak-ng --stdout 'Meldra screen reader test' | wc -c")
        size = max((int(t) for t in out.split() if t.isdigit()), default=0)
        results["synthesis"] = size > 10000
        evidence.append(f"2. espeak-ng rendered {size} bytes of audio: {results['synthesis']}")

        out = user(f"python3 -c {shlex.quote(ATSPI_PROBE)}", 30)
        apps = next((line for line in out.splitlines() if line.startswith("ATSPI-APPS:")), "")
        results["at-spi"] = "gnome-shell" in apps
        evidence.append(f"3. AT-SPI desktop: {results['at-spi']} ({apps or out[-300:]})")

        before = WAV.stat().st_size if WAV.exists() else 0
        user("gsettings set org.gnome.desktop.a11y.applications screen-reader-enabled true")
        running = wait_for(lambda: "ORCA-RUNNING" in guest(
            "pgrep -f 'bin/orca' >/dev/null && pgrep -f 'bin/speech-dispatcher' >/dev/null && echo ORCA-RUNNING"),
            90)
        processes = guest("ps -o pid,user,args | grep -E 'orca|speech-dispatch|sd_espeak' | grep -v grep")
        results["enable"] = running
        evidence.append(f"4. screen reader enabled, Orca and speech-dispatcher running: {running}\n{processes}")

        time.sleep(8)   # Orca says "Screen reader on" and reads the focused item
        vm("screenshot", "orca-enabled")
        peak, loud = wav_peak(WAV, before) if WAV.exists() else (0, 0)
        results["speech"] = peak > 3000 and loud > 2000
        evidence.append(f"5. speech after enabling: peak {peak}, {loud} loud samples: {results['speech']}")

        before = WAV.stat().st_size if WAV.exists() else 0
        monitor_call("sendkey meta_l")
        time.sleep(6)
        vm("screenshot", "orca-activities")
        peak, loud = wav_peak(WAV, before) if WAV.exists() else (0, 0)
        results["focus"] = peak > 3000 and loud > 1000
        evidence.append(f"6. speech after Super (Activities): peak {peak}, {loud} loud samples: {results['focus']}")

        orca_log = user("tail -n 30 ~/.local/share/orca/debug.out 2>/dev/null; true")
        if orca_log.strip():
            evidence.append("orca debug tail:\n" + orca_log)
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        evidence.append(f"error: {error}")
    finally:
        log_tail = vm("log", "80", check=False)
        vm("stop", check=False)
    checks = ("packages", "synthesis", "at-spi", "enable", "speech", "focus")
    passed = all(results.get(name, False) for name in checks)
    evidence.append("summary: " + ", ".join(f"{name}={'pass' if results.get(name) else 'FAIL'}" for name in checks))
    report = REPORTS / ("orca-smoke.ok" if passed else "orca-smoke.failed")
    report.write_text("\n".join(evidence) + "\n\n--- console (last lines) ---\n" + log_tail + "\n")
    print("\n".join(evidence))
    print(f"{'PASS' if passed else 'FAIL'}: {report}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
