#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Build Meldra's minimal Labwc compositor package from pinned sources."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kiln.cli import build
from tools.build_graphics_packages import (
    manifest, python_wheel, require_glibc_elf, source,
)
import tools.build_desktop_libraries as desktop


DEPENDENCIES = (
    "wlroots", "libxml2", "libpng", "glib", "cairo", "pango", "dejavu-fonts",
    "xkeyboard-config",
)


def pinned_meson(lock: dict, cache: Path, work: Path) -> tuple[str, ...]:
    wheel_work = work / "host-meson"
    wheel_work.mkdir()
    root = python_wheel(lock, cache, wheel_work)
    launcher = work / "meson"
    launcher.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"sys.path.insert(0, {str(root)!r})\n"
        "from mesonbuild.mesonmain import main\n"
        "raise SystemExit(main())\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    return (str(launcher),)


def install_meldra_session(stage: Path) -> None:
    config = stage / "etc/xdg/labwc"
    shutil.copytree(Path("desktop/labwc"), config)
    launcher = stage / "usr/bin/meldra-labwc-session"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "export HOME=/root\n"
        "export XDG_RUNTIME_DIR=/run/user/0\n"
        "export XDG_CONFIG_DIRS=/etc/xdg\n"
        "export XDG_SESSION_TYPE=wayland\n"
        "export XDG_CURRENT_DESKTOP=labwc:Meldra\n"
        "export LIBSEAT_BACKEND=seatd\n"
        "export WLR_RENDERER=pixman\n"
        "export WLR_RENDERER_ALLOW_SOFTWARE=1\n"
        "mkdir -p \"$XDG_RUNTIME_DIR\"\n"
        "chmod 0700 \"$XDG_RUNTIME_DIR\"\n"
        "attempt=0\n"
        "while [ ! -S /run/seatd.sock ] && [ \"$attempt\" -lt 100 ]; do\n"
        "    sleep 0.1\n"
        "    attempt=$((attempt + 1))\n"
        "done\n"
        "exec /usr/bin/labwc\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    service = stage / "etc/meldra/services/30-labwc.service"
    service.parent.mkdir(parents=True, exist_ok=True)
    service.write_text(
        "command=/usr/bin/meldra-labwc-session\nrespawn=yes\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=Path("sources.lock.toml"))
    parser.add_argument("--cache", type=Path, default=Path("build/downloads"))
    parser.add_argument("--output", type=Path, default=Path("build/packages"))
    args = parser.parse_args()
    lock_data = tomllib.loads(args.lock.read_text(encoding="utf-8"))
    labwc_lock = lock_data["desktop"]["labwc"]
    args.output.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="meldra-labwc-") as directory:
            work = Path(directory)
            desktop.MESON = pinned_meson(
                lock_data["host_tools"]["meson"], args.cache, work,
            )
            upstream = source(labwc_lock, args.cache, work / "source")
            target_work = work / "target"
            target_work.mkdir()
            sysroot = desktop.make_sysroot(args.output, target_work, DEPENDENCIES)
            cross = desktop.cross_file(work, sysroot)
            build_dir = work / "build"
            stage = work / "package/root"
            subprocess.run(
                [*desktop.MESON, "setup", str(build_dir), str(upstream),
                 "--prefix=/usr", "--wrap-mode=nodownload", "--cross-file", str(cross),
                 "-Dxwayland=disabled", "-Dsvg=disabled", "-Dicon=disabled",
                 "-Dnls=disabled", "-Dman-pages=disabled", "-Dtest=disabled"],
                check=True,
            )
            subprocess.run(
                [*desktop.MESON, "compile", "-C", str(build_dir)], check=True,
            )
            subprocess.run(
                [*desktop.MESON, "install", "-C", str(build_dir),
                 "--destdir", str(stage)], check=True,
            )
            executable = stage / "usr/bin/labwc"
            if not executable.is_file():
                raise RuntimeError("Labwc executable was not installed")
            require_glibc_elf(executable)
            install_meldra_session(stage)
            package = manifest(
                stage, "labwc", labwc_lock["version"], DEPENDENCIES,
            )
            archive = args.output / f"labwc-{labwc_lock['version']}.kiln"
            build(package, archive)

            validation_work = work / "validation"
            validation_work.mkdir()
            validation_root = desktop.make_sysroot(
                args.output, validation_work, ("labwc",),
            )
            checked = desktop.validate_elf_closure(validation_root)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"build_labwc: {exc}", file=sys.stderr)
        return 1
    print(f"built {archive}; validated {checked} target ELF files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
