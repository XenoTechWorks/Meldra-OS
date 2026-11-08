#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Configure and build Meldra's pinned Linux baseline."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tomllib


def run(command: list[str], *, cwd: Path | None = None) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=Path("sources.lock.toml"))
    parser.add_argument("--output", type=Path, default=Path("build/linux"))
    parser.add_argument("--jobs", type=int, default=max(1, os.cpu_count() or 1))
    parser.add_argument("--configure-only", action="store_true")
    args = parser.parse_args()
    linux = tomllib.loads(args.lock.read_text(encoding="utf-8"))["linux"]
    source = (Path("upstream") / f"linux-{linux['version']}").resolve()
    if not (source / ".meldra-source").is_file():
        parser.error("verified kernel source missing; run tools/fetch_sources.py")
    if shutil.which("make") is None:
        parser.error("make is required")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    host_tools = Path("build/host-tools/bin").resolve()
    environment = os.environ.copy()
    if host_tools.is_dir():
        environment["PATH"] = f"{host_tools}:{environment['PATH']}"
    os.environ.update({"PATH": environment["PATH"]})
    run(["make", f"O={output}", "x86_64_defconfig"], cwd=source)
    merge = source / "scripts/kconfig/merge_config.sh"
    run([str(merge), "-m", "-O", str(output), str(output / ".config"),
         str(Path("kernel/config/x86_64.fragment").resolve())], cwd=source)
    run(["make", f"O={output}", "olddefconfig"], cwd=source)
    if not args.configure_only:
        run(["make", f"O={output}", f"-j{args.jobs}", "bzImage", "modules"], cwd=source)
        destination = Path("build/bzImage")
        shutil.copy2(output / "arch/x86/boot/bzImage", destination)
        print(f"built {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

