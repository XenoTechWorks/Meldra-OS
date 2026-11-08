#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Build the platform layers GNOME needs, one package at a time, from pinned sources.

    build_gnome_platform.py --list
    build_gnome_platform.py dbus elogind        # build named packages (and nothing else)
    build_gnome_platform.py --all               # everything not yet built, in order

Each package is built in a throwaway sysroot assembled from already-built Kiln
packages, then packed into build/packages. A failure stops at that package so the
log of the failing step is the last thing on screen.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kiln.cli import build
from tools.build_graphics_packages import manifest, python_wheel, require_glibc_elf, source
import tools.build_desktop_libraries as desktop


class Package:
    def __init__(self, name, deps, kind="meson", options=(), pattern=None, post=None, env=None):
        self.name = name
        self.deps = tuple(deps)
        self.kind = kind
        self.options = tuple(options)
        self.pattern = pattern
        self.post = post
        self.env = env or {}


def libcap_build(pkg: Package, lock: dict, cache: Path, work: Path, output: Path) -> None:
    unit = work / pkg.name
    unit.mkdir()
    upstream = source(lock, cache, unit / "source")
    sysroot = desktop.make_sysroot(output, unit, pkg.deps)
    stage = unit / "package/root"
    root = sysroot.resolve()
    subprocess.run(
        ["make", "-C", "libcap", "-j8", "prefix=/usr", "lib=lib", "GOLANG=no", "PAM_CAP=no",
         "RAISE_SETFCAP=no", "BUILD_CC=gcc", f"CC=gcc --sysroot={root}",
         f"DESTDIR={stage}", "install"],
        cwd=upstream, check=True,
    )
    library = next((stage / "usr/lib").glob("libcap.so.*"), None)
    if library is None:
        raise RuntimeError("libcap library not installed")
    require_glibc_elf(library)
    build(manifest(stage, pkg.name, lock["version"], pkg.deps), output / f"{pkg.name}-{lock['version']}.kiln")


PACKAGES = [
    Package("libcap", ("glibc",), kind="custom"),
    Package(
        "dbus", ("glibc", "expat"),
        options=("-Dsystemd=disabled", "-Dselinux=disabled", "-Dapparmor=disabled",
                 "-Dlibaudit=disabled", "-Dx11_autolaunch=disabled", "-Dxml_docs=disabled",
                 "-Ddoxygen_docs=disabled", "-Dducktype_docs=disabled", "-Dqt_help=disabled",
                 "-Dmodular_tests=disabled", "-Dinstalled_tests=false", "--sysconfdir=/etc", "--localstatedir=/var",
                ),
        pattern="libdbus-1.so.3.*",
    ),
    Package(
        "util-linux", ("glibc",), kind="autotools",
        options=("--disable-all-programs", "--enable-libmount", "--enable-libblkid",
                 "--enable-libuuid", "--disable-nls", "--without-python", "--without-systemd",
                 "--without-udev", "--without-ncurses", "--without-readline", "--libdir=/usr/lib", "--enable-usrdir-path",
                 "--disable-static", "--enable-shared"),
        pattern="libmount.so.1.*",
    ),
    Package(
        "elogind", ("glibc", "libcap", "eudev", "dbus", "util-linux"),
        options=("-Dpam=false", "-Dacl=disabled", "-Daudit=disabled", "-Dselinux=disabled",
                 "-Dman=disabled", "-Dhtml=disabled", "-Dtests=false", "-Dpolkit=false",
                 "-Dsmack=false", "-Dcgroup-controller=elogind", "-Ddefault-hierarchy=unified",
                 "-Dutmp=false", "-Dpamlibdir=/usr/lib/security"),
        pattern="libelogind.so.0.*",
    ),
]


def meson_build(pkg: Package, lock: dict, cache: Path, work: Path, output: Path) -> None:
    desktop.build_meson(lock, pkg.name, cache, work, output, pkg.deps, pkg.options, pkg.pattern)




def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("names", nargs="*")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--lock", type=Path, default=Path("sources.lock.toml"))
    parser.add_argument("--cache", type=Path, default=Path("build/downloads"))
    parser.add_argument("--output", type=Path, default=Path("build/packages"))
    args = parser.parse_args()
    lock_data = tomllib.loads(args.lock.read_text(encoding="utf-8"))
    locks = lock_data["gnome"]
    for pkg in PACKAGES:
        desktop.VERSIONS.setdefault(pkg.name, locks[pkg.name.replace("-", "_")]["version"])
    if args.list:
        for pkg in PACKAGES:
            done = (args.output / f"{pkg.name}-{desktop.VERSIONS[pkg.name]}.kiln").is_file()
            print(f"{'built  ' if done else 'pending'} {pkg.name} {desktop.VERSIONS[pkg.name]}")
        return 0
    wanted = [p for p in PACKAGES if p.name in args.names or args.all]
    unknown = set(args.names) - {p.name for p in PACKAGES}
    if unknown:
        parser.error(f"unknown package(s): {', '.join(sorted(unknown))}")
    args.output.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="meldra-gnome-") as directory:
            work = Path(directory)
            desktop.MESON = pinned_meson(lock_data["host_tools"]["meson"], args.cache, work)
            # Build-time-only Python modules (templating), from pinned wheels.
            wheel_paths = []
            for tool in ("jinja2", "markupsafe"):
                tool_work = work / f"host-{tool}"
                tool_work.mkdir()
                wheel_paths.append(str(python_wheel(lock_data["host_tools"][tool], args.cache, tool_work)))
            os.environ["PYTHONPATH"] = os.pathsep.join(wheel_paths + [os.environ.get("PYTHONPATH", "")]).rstrip(os.pathsep)
            for pkg in wanted:
                archive = args.output / f"{pkg.name}-{desktop.VERSIONS[pkg.name]}.kiln"
                if args.all and archive.is_file():
                    print(f"using existing package {archive}")
                    continue
                print(f"==> {pkg.name} {desktop.VERSIONS[pkg.name]}", flush=True)
                lock = locks[pkg.name.replace("-", "_")]
                if pkg.kind == "autotools":
                    desktop.build_autotools(lock, pkg.name, args.cache, work, args.output,
                                            pkg.deps, pkg.options, pkg.pattern)
                elif pkg.kind == "custom":
                    globals()[f"{pkg.name.replace('-', '_')}_build"](pkg, lock, args.cache, work, args.output)
                else:
                    meson_build(pkg, lock, args.cache, work, args.output)
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print(f"build_gnome_platform: {error}", file=sys.stderr)
        return 1
    return 0


def pinned_meson(lock: dict, cache: Path, work: Path) -> tuple[str, ...]:
    wheel_work = work / "host-meson"
    wheel_work.mkdir()
    root = python_wheel(lock, cache, wheel_work)
    launcher = work / "meson"
    launcher.write_text(
        f"#!{sys.executable}\nimport sys\nsys.path.insert(0, {str(root)!r})\n"
        "from mesonbuild.mesonmain import main\nraise SystemExit(main())\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    return (str(launcher),)


if __name__ == "__main__":
    raise SystemExit(main())
