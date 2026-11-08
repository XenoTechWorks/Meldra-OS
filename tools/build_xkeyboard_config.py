#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Build Meldra's pinned XKB keyboard configuration data package."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kiln.cli import build
from tools.build_graphics_packages import manifest, source
from tools.build_labwc import pinned_meson


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=Path("sources.lock.toml"))
    parser.add_argument("--cache", type=Path, default=Path("build/downloads"))
    parser.add_argument("--output", type=Path, default=Path("build/packages"))
    args = parser.parse_args()
    lock_data = tomllib.loads(args.lock.read_text(encoding="utf-8"))
    lock = lock_data["desktop"]["xkeyboard_config"]
    args.output.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="meldra-xkeyboard-config-") as directory:
            work = Path(directory)
            meson = pinned_meson(lock_data["host_tools"]["meson"], args.cache, work)
            upstream = source(lock, args.cache, work / "source")
            build_dir = work / "build"
            stage = work / "package/root"
            subprocess.run(
                [*meson, "setup", str(build_dir), str(upstream), "--prefix=/usr",
                 "-Dnls=false", "-Dcompat-rules=true", "-Dxorg-rules-symlinks=true",
                 "-Dnon-latin-layouts-list=false"],
                check=True,
            )
            subprocess.run([*meson, "compile", "-C", str(build_dir)], check=True)
            subprocess.run(
                [*meson, "install", "-C", str(build_dir), "--destdir", str(stage)],
                check=True,
            )
            legacy_root = stage / "usr/share/X11/xkb"
            if not legacy_root.exists() and not legacy_root.is_symlink():
                raise RuntimeError("legacy /usr/share/X11/xkb data path was not installed")
            if legacy_root.is_symlink():
                legacy_root.unlink()
                legacy_root.symlink_to("../xkeyboard-config-2")
            package = manifest(stage, "xkeyboard-config", lock["version"], ())
            archive = args.output / f"xkeyboard-config-{lock['version']}.kiln"
            build(package, archive)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"build_xkeyboard_config: {exc}", file=sys.stderr)
        return 1
    print(f"built {archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
