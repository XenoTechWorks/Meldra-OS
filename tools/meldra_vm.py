#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Drive one Meldra QEMU virtual machine: start, screenshot, serial, log, stop.

    meldra_vm.py start [--initrd build/meldra-gnome-initramfs.cpio.gz] [--memory 4096]
    meldra_vm.py screenshot NAME            # build/screenshots/<date>/NN-NAME.png
    meldra_vm.py serial "ps; ls /run"       # run a command in the root shell on ttyS0
    meldra_vm.py log [LINES]                # the console (serial) log so far
    meldra_vm.py status | stop

Only one VM runs at a time: `start` refuses while any qemu-system process exists. The VM is headless
(`-display none`); screenshots come from QEMU's `screendump`, so they show exactly what the guest draws.
QEMU runs on the host (the editor may be sandboxed), so it is launched through flatpak-spawn when present.
"""

from __future__ import annotations

import argparse
import datetime
import os
from pathlib import Path
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import time
import zlib

STATE = Path("build/vm")
MONITOR = STATE / "monitor.sock"
SERIAL = STATE / "serial.sock"
LOG = STATE / "serial.log"
PIDFILE = STATE / "qemu.pid"


def on_host(command: str, check: bool = False) -> subprocess.CompletedProcess:
    argv = ["flatpak-spawn", "--host", "sh", "-c", command] if shutil.which("flatpak-spawn") else ["sh", "-c", command]
    return subprocess.run(argv, capture_output=True, text=True, check=check)


def qemu_running() -> bool:
    return on_host("pgrep -x qemu-system-x86_64 >/dev/null 2>&1").returncode == 0


def monitor(command: str, wait: float = 0.5) -> str:
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(5)
        sock.connect(str(MONITOR))
        time.sleep(0.2)
        sock.recv(65536)  # greeting
        sock.sendall(command.encode() + b"\n")
        time.sleep(wait)
        sock.settimeout(1)
        try:
            return sock.recv(65536).decode(errors="replace")
        except socket.timeout:
            return ""


def start(args: argparse.Namespace) -> int:
    if qemu_running():
        print("a qemu-system process is already running; stop it first (one VM at a time)", file=sys.stderr)
        return 1
    for needed in (Path(args.kernel), Path(args.initrd)):
        if not needed.is_file():
            print(f"missing {needed}", file=sys.stderr)
            return 1
    STATE.mkdir(parents=True, exist_ok=True)
    for stale in (MONITOR, SERIAL, PIDFILE):
        stale.unlink(missing_ok=True)
    LOG.write_text("")
    cwd = Path.cwd()
    qemu = [
        "qemu-system-x86_64", "-machine", "q35,accel=kvm:tcg", "-cpu", "max",
        "-m", str(args.memory), "-smp", str(args.cpus),
        "-kernel", args.kernel, "-initrd", args.initrd,
        "-append", args.append,
        "-device", "virtio-vga", "-device", "virtio-keyboard-pci", "-device", "virtio-tablet-pci",
        "-display", "none",
        "-monitor", f"unix:{MONITOR},server=on,wait=off",
        "-chardev", f"socket,id=serial0,path={SERIAL},server=on,wait=off,logfile={LOG}",
        "-serial", "chardev:serial0",
        "-no-reboot", "-pidfile", str(PIDFILE), "-daemonize",
    ]
    result = on_host(f"cd {shlex.quote(str(cwd))} && nice -n 10 " + " ".join(shlex.quote(a) for a in qemu))
    if result.returncode:
        print(result.stderr.strip() or "qemu failed to start", file=sys.stderr)
        return 1
    for _ in range(50):
        if MONITOR.exists():
            break
        time.sleep(0.1)
    print(f"started: {args.memory} MB, {args.cpus} CPUs, initrd {args.initrd}")
    return 0


def png_from_ppm(ppm: bytes) -> bytes:
    if ppm[:2] != b"P6":
        raise RuntimeError("screendump did not produce a P6 PPM")
    fields, position = [], 2
    while len(fields) < 3:
        while ppm[position:position + 1].isspace():
            position += 1
        end = position
        while not ppm[end:end + 1].isspace():
            end += 1
        fields.append(int(ppm[position:end]))
        position = end
    width, height, _ = fields
    pixels = ppm[position + 1:]
    stride = width * 3
    raw = b"".join(b"\0" + pixels[row * stride:(row + 1) * stride] for row in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def screenshot(args: argparse.Namespace) -> int:
    if not MONITOR.exists():
        print("no VM is running", file=sys.stderr)
        return 1
    ppm_path = STATE / "shot.ppm"
    ppm_path.unlink(missing_ok=True)
    monitor(f"screendump {ppm_path.resolve()}", wait=1.0)
    for _ in range(40):
        if ppm_path.exists() and ppm_path.stat().st_size > 100:
            time.sleep(0.2)
            break
        time.sleep(0.1)
    folder = Path("build/screenshots") / datetime.date.today().isoformat()
    folder.mkdir(parents=True, exist_ok=True)
    number = len(list(folder.glob("*.png"))) + 1
    target = folder / f"{number:02d}-{args.name}.png"
    target.write_bytes(png_from_ppm(ppm_path.read_bytes()))
    print(target)
    return 0


def serial(args: argparse.Namespace) -> int:
    if not SERIAL.exists():
        print("no VM is running", file=sys.stderr)
        return 1
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(2)
        sock.connect(str(SERIAL))
        sock.sendall(b"\n")
        time.sleep(0.3)
        try:
            sock.recv(65536)
        except socket.timeout:
            pass
        marker = f"__done_{int(time.time())}__"
        sock.sendall((args.command + f"; echo {marker}\n").encode())
        output, deadline = b"", time.time() + args.timeout
        while time.time() < deadline and marker.encode() not in output.split(b"echo " + marker.encode())[-1]:
            try:
                data = sock.recv(65536)
            except socket.timeout:
                continue
            if not data:
                break
            output += data
    text = output.decode(errors="replace")
    print(text.split(f"echo {marker}")[-1].replace(marker, "").strip() if f"echo {marker}" in text else text.strip())
    return 0


def log(args: argparse.Namespace) -> int:
    lines = LOG.read_text(errors="replace").splitlines() if LOG.exists() else []
    print("\n".join(lines[-args.lines:]))
    return 0


def stop(_: argparse.Namespace) -> int:
    if MONITOR.exists():
        try:
            monitor("quit", wait=0.3)
        except OSError:
            pass
    for _ in range(30):
        if not qemu_running():
            break
        time.sleep(0.2)
    if qemu_running() and PIDFILE.exists():
        on_host(f"kill $(cat {shlex.quote(str(PIDFILE.resolve()))})")
        time.sleep(1)
    print("stopped" if not qemu_running() else "still running", file=sys.stderr if qemu_running() else sys.stdout)
    return 0 if not qemu_running() else 1


def status(_: argparse.Namespace) -> int:
    print("running" if qemu_running() else "not running")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("start")
    s.add_argument("--kernel", default="build/bzImage")
    s.add_argument("--initrd", default="build/meldra-gnome-initramfs.cpio.gz")
    s.add_argument("--memory", type=int, default=4096)
    s.add_argument("--cpus", type=int, default=2)
    s.add_argument("--append", default="console=ttyS0 rdinit=/init panic=1 loglevel=6")
    s.set_defaults(run=start)
    s = sub.add_parser("screenshot")
    s.add_argument("name")
    s.set_defaults(run=screenshot)
    s = sub.add_parser("serial")
    s.add_argument("command")
    s.add_argument("--timeout", type=float, default=15)
    s.set_defaults(run=serial)
    s = sub.add_parser("log")
    s.add_argument("lines", nargs="?", type=int, default=60)
    s.set_defaults(run=log)
    sub.add_parser("stop").set_defaults(run=stop)
    sub.add_parser("status").set_defaults(run=status)
    args = parser.parse_args()
    return args.run(args)


if __name__ == "__main__":
    raise SystemExit(main())
