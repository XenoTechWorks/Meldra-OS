#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Build Meldra's text and desktop rendering libraries from pinned sources."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import tomllib

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kiln.cli import build, install
from tools.build_graphics_packages import (
    source, python_wheel, manifest, require_glibc_elf,
)


VERSIONS = {
    "glibc": "2.44", "libffi": "3.8.0", "pcre2": "10.49",
    "expat": "2.10.0", "pixman": "0.46.4", "zlib": "1.3.2",
    "libpng": "1.6.50", "freetype": "2.14.1", "glib": "2.88.4",
    "fontconfig": "2.18.2", "harfbuzz": "15.1.0",
    "fribidi": "1.0.18", "cairo": "1.18.6", "pango": "1.58.2",
    "libdrm": "2.4.134", "wayland": "1.26.0",
    "wayland-protocols": "1.49", "mesa": "26.2.5",
    "libxkbcommon": "1.11.0", "eudev": "3.3.0",
    "libevdev": "1.13.4", "mtdev": "1.1.7", "libinput": "1.30.1",
    "seatd": "0.9.3", "libdisplay-info": "0.4.0", "wlroots": "0.19.0",
    "libxml2": "2.14.6", "dejavu-fonts": "2.37",
    "xkeyboard-config": "2.49", "labwc": "0.9.1",
}

SKIP_EXISTING = False
MESON = ("meson",)


def validate_elf_closure(root: Path) -> int:
    """Require every target ELF dependency and interpreter to exist in root."""
    # Include nested loader locations such as glibc's $ORIGIN-based gconv
    # helper libraries, not only the default top-level search directories.
    available = {path.name for path in root.rglob("*")
                 if path.is_file() or path.is_symlink()}
    checked = 0
    missing: list[str] = []
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            with path.open("rb") as stream:
                if stream.read(4) != b"\x7fELF":
                    continue
        except OSError:
            continue
        checked += 1
        dynamic = subprocess.run(
            ["readelf", "-d", str(path)], capture_output=True, text=True, check=True,
        ).stdout
        for needed in re.findall(r"Shared library: \[([^]]+)\]", dynamic):
            if needed not in available:
                missing.append(f"{path.relative_to(root)} needs {needed}")
        program = subprocess.run(
            ["readelf", "-l", str(path)], capture_output=True, text=True, check=True,
        ).stdout
        for interpreter in re.findall(r"Requesting program interpreter: ([^]]+)", program):
            target = root / interpreter.lstrip("/")
            if not target.exists() and not target.is_symlink():
                missing.append(
                    f"{path.relative_to(root)} needs interpreter {interpreter}"
                )
    if missing:
        raise RuntimeError("incomplete ELF closure:\n" + "\n".join(sorted(missing)))
    return checked


def make_sysroot(output: Path, work: Path, dependencies: tuple[str, ...]) -> Path:
    root = work / "sysroot"
    root.mkdir()
    installed: set[str] = set()

    def install_with_dependencies(name: str) -> None:
        if name in installed:
            return
        if name not in VERSIONS:
            raise RuntimeError(f"desktop library version is not pinned: {name}")
        archive = output / f"{name}-{VERSIONS[name]}.kiln"
        if not archive.is_file():
            raise RuntimeError(f"desktop library dependency missing: {archive}")
        with tarfile.open(archive, "r:*") as bundle:
            member = bundle.extractfile("KILN.toml")
            if member is None:
                raise RuntimeError(f"desktop library package has no manifest: {archive}")
            package = tomllib.loads(member.read().decode("utf-8"))["package"]
        for dependency in package.get("dependencies", []):
            install_with_dependencies(dependency)
        install(archive, root)
        installed.add(name)

    for name in dependencies:
        install_with_dependencies(name)
    return root


def cross_file(work: Path, sysroot: Path) -> Path:
    path = work / "cross.ini"
    loader = sysroot / "lib64/ld-linux-x86-64.so.2"
    library_path = f"{sysroot}/lib64:{sysroot}/usr/lib64:{sysroot}/usr/lib"
    path.write_text(
        "[binaries]\n"
        f"c = ['gcc', '--sysroot={sysroot.resolve()}']\n"
        f"cpp = ['g++', '--sysroot={sysroot.resolve()}']\n"
        "ar = 'ar'\nstrip = 'strip'\npkg-config = 'pkg-config'\n"
        f"exe_wrapper = ['{loader.resolve()}', '--library-path', '{library_path}']\n"
        "[host_machine]\nsystem = 'linux'\ncpu_family = 'x86_64'\n"
        "cpu = 'x86_64'\nendian = 'little'\n"
        "[built-in options]\n"
        f"c_link_args = ['-Wl,-rpath-link,{sysroot.resolve()}/usr/lib', "
        f"'-Wl,-rpath-link,{sysroot.resolve()}/usr/lib64', "
        f"'-Wl,-rpath-link,{sysroot.resolve()}/lib64']\n"
        f"cpp_link_args = ['-Wl,-rpath-link,{sysroot.resolve()}/usr/lib', "
        f"'-Wl,-rpath-link,{sysroot.resolve()}/usr/lib64', "
        f"'-Wl,-rpath-link,{sysroot.resolve()}/lib64']\n"
        "[properties]\n"
        # Same architecture, newer glibc: programs built for the target must run through its loader, never
        # directly (meson would otherwise run them natively against the build machine's glibc).
        "needs_exe_wrapper = true\n"
        f"sys_root = '{sysroot.resolve()}'\n"
        f"pkg_config_libdir = ['{sysroot.resolve()}/usr/lib64/pkgconfig', "
        f"'{sysroot.resolve()}/usr/lib/pkgconfig', '{sysroot.resolve()}/usr/share/pkgconfig']\n",
        encoding="utf-8",
    )
    return path


def build_meson(
    lock: dict, name: str, cache: Path, work: Path, output: Path,
    dependencies: tuple[str, ...], options: tuple[str, ...], pattern: str,
) -> None:
    archive = output / f"{name}-{lock['version']}.kiln"
    if SKIP_EXISTING and archive.is_file():
        print(f"using existing package {archive}")
        return
    unit = work / name
    unit.mkdir()
    upstream = source(lock, cache, unit / "source")
    sysroot = make_sysroot(output, unit, dependencies)
    cross = cross_file(unit, sysroot)
    build_dir = unit / "build"
    stage = unit / "package/root"
    subprocess.run(
        [*MESON, "setup", "--wrap-mode=nodownload", str(build_dir), str(upstream), "--prefix=/usr",
         "--cross-file", str(cross), *options], check=True,
    )
    subprocess.run([*MESON, "compile", "-C", str(build_dir)], check=True)
    subprocess.run(
        [*MESON, "install", "-C", str(build_dir), "--destdir", str(stage)],
        check=True,
    )
    library = next((stage / "usr/lib").glob(pattern), None)
    if library is None:
        raise RuntimeError(f"{name} library not installed: {pattern}")
    require_glibc_elf(library)
    package = manifest(stage, name, lock["version"], dependencies)
    build(package, archive)


def build_autotools(
    lock: dict, name: str, cache: Path, work: Path, output: Path,
    dependencies: tuple[str, ...], options: tuple[str, ...], pattern: str,
) -> None:
    archive = output / f"{name}-{lock['version']}.kiln"
    if SKIP_EXISTING and archive.is_file():
        print(f"using existing package {archive}")
        return
    unit = work / name
    unit.mkdir()
    upstream = source(lock, cache, unit / "source")
    sysroot = make_sysroot(output, unit, dependencies)
    build_dir = unit / "build"
    build_dir.mkdir()
    stage = unit / "package/root"
    environment = os.environ.copy()
    environment.update({
        "CC": f"gcc --sysroot={sysroot.resolve()}",
        "CFLAGS": "-O2 -pipe",
        "PKG_CONFIG_SYSROOT_DIR": str(sysroot.resolve()),
        "PKG_CONFIG_LIBDIR": ":".join((
            f"{sysroot.resolve()}/usr/lib/pkgconfig",
            f"{sysroot.resolve()}/usr/share/pkgconfig",
        )),
    })
    subprocess.run(
        [str(upstream / "configure"), "--build=x86_64-pc-linux-gnu",
         "--host=x86_64-unknown-linux-gnu", "--prefix=/usr", *options],
        cwd=build_dir, env=environment, check=True,
    )
    subprocess.run(["make", "-j8"], cwd=build_dir, env=environment, check=True)
    subprocess.run(
        ["make", "install", f"DESTDIR={stage}"], cwd=build_dir,
        env=environment, check=True,
    )
    library = next((stage / "usr/lib").glob(pattern), None)
    if library is None:
        raise RuntimeError(f"{name} library not installed: {pattern}")
    require_glibc_elf(library)
    package = manifest(stage, name, lock["version"], dependencies)
    build(package, archive)


def main() -> int:
    global MESON, SKIP_EXISTING
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=Path("sources.lock.toml"))
    parser.add_argument("--cache", type=Path, default=Path("build/downloads"))
    parser.add_argument("--output", type=Path, default=Path("build/packages"))
    parser.add_argument("--skip-existing", action="store_true",
                        help="reuse package archives that have already been built")
    args = parser.parse_args()
    SKIP_EXISTING = args.skip_existing
    lock_data = tomllib.loads(args.lock.read_text(encoding="utf-8"))
    locks = lock_data["desktop"]
    try:
        with tempfile.TemporaryDirectory(prefix="meldra-desktop-libs-") as directory:
            work = Path(directory)
            meson_work = work / "host-meson"
            meson_work.mkdir()
            meson_root = python_wheel(
                lock_data["host_tools"]["meson"], args.cache, meson_work,
            )
            meson_launcher = work / "meson"
            meson_launcher.write_text(
                f"#!{sys.executable}\n"
                "import sys\n"
                f"sys.path.insert(0, {str(meson_root)!r})\n"
                "from mesonbuild.mesonmain import main\n"
                "raise SystemExit(main())\n",
                encoding="utf-8",
            )
            meson_launcher.chmod(0o755)
            MESON = (str(meson_launcher),)
            build_autotools(
                locks["libpng"], "libpng", args.cache, work, args.output,
                ("glibc", "zlib"), ("--disable-static", "--enable-shared"),
                "libpng16.so.16.*",
            )
            build_meson(
                locks["libxml2"], "libxml2", args.cache, work, args.output,
                ("glibc", "zlib"),
                ("-Dpython=disabled", "-Dreadline=disabled", "-Dicu=disabled",
                 "-Dlzma=disabled", "-Dzlib=enabled"), "libxml2.so.16.*",
            )
            build_meson(
                locks["freetype"], "freetype", args.cache, work, args.output,
                ("glibc", "zlib", "libpng"),
                ("-Dzlib=enabled", "-Dpng=enabled", "-Dbzip2=disabled",
                 "-Dbrotli=disabled", "-Dharfbuzz=disabled", "-Dtests=disabled"),
                "libfreetype.so.6.*",
            )
            build_meson(
                locks["glib"], "glib", args.cache, work, args.output,
                ("glibc", "libffi", "pcre2", "zlib"),
                ("-Dtests=false", "-Dinstalled_tests=false", "-Dselinux=disabled",
                 "-Dxattr=false", "-Dlibmount=disabled", "-Dman-pages=disabled",
                 "-Ddocumentation=false", "-Dgtk_doc=false", "-Dnls=disabled",
                 "-Dsysprof=disabled", "-Ddtrace=false", "-Dsystemtap=false",
                 "-Dintrospection=disabled"), "libglib-2.0.so.0.*",
            )
            build_meson(
                locks["fontconfig"], "fontconfig", args.cache, work, args.output,
                ("glibc", "expat", "freetype"),
                ("-Ddoc=disabled", "-Ddoc-txt=disabled", "-Ddoc-man=disabled",
                 "-Ddoc-pdf=disabled", "-Ddoc-html=disabled", "-Dnls=disabled",
                 "-Dtests=disabled", "-Dtests-external-fonts=disabled",
                 "-Dtools=enabled", "-Dcache-build=disabled",
                 "-Dxml-backend=expat"), "libfontconfig.so.1.*",
            )
            build_meson(
                locks["harfbuzz"], "harfbuzz", args.cache, work, args.output,
                ("glibc", "freetype", "glib"),
                ("-Dglib=enabled", "-Dgobject=enabled", "-Dfreetype=enabled",
                 "-Dcairo=disabled", "-Dchafa=disabled", "-Dpng=disabled",
                 "-Dzlib=disabled", "-Dicu=disabled", "-Dgraphite2=disabled",
                 "-Dfontations=disabled", "-Dharfrust=disabled", "-Dwasm=disabled",
                 "-Draster=disabled", "-Dvector=disabled", "-Dgpu=disabled",
                 "-Dsubset=disabled", "-Dtests=disabled", "-Ddocs=disabled",
                 "-Dintrospection=disabled", "-Dutilities=disabled"),
                "libharfbuzz.so.0.*",
            )
            build_meson(
                locks["fribidi"], "fribidi", args.cache, work, args.output,
                ("glibc",),
                ("-Ddocs=false", "-Dtests=false", "-Dbin=true"),
                "libfribidi.so.0.*",
            )
            build_meson(
                locks["cairo"], "cairo", args.cache, work, args.output,
                ("glibc", "pixman", "zlib", "libpng", "freetype", "fontconfig"),
                ("-Dfontconfig=enabled", "-Dfreetype=enabled", "-Dpng=enabled",
                 "-Dzlib=enabled", "-Ddwrite=disabled", "-Dquartz=disabled",
                 "-Dtee=disabled", "-Dxcb=disabled", "-Dxlib=disabled",
                 "-Dxlib-xcb=disabled", "-Dtests=disabled", "-Dlzo=disabled",
                 "-Dglib=disabled", "-Dspectre=disabled",
                 "-Dsymbol-lookup=disabled", "-Dgtk_doc=false"),
                "libcairo.so.2.*",
            )
            build_meson(
                locks["pango"], "pango", args.cache, work, args.output,
                ("glibc", "glib", "harfbuzz", "fribidi", "cairo", "fontconfig",
                 "freetype"),
                ("-Ddocumentation=false", "-Dman-pages=false",
                 "-Dintrospection=disabled", "-Dbuild-testsuite=false",
                 "-Dbuild-examples=false", "-Dfontconfig=enabled",
                 "-Dsysprof=disabled", "-Dlibthai=disabled", "-Dcairo=enabled",
                 "-Dxft=disabled", "-Dfreetype=enabled"),
                "libpango-1.0.so.0.*",
            )
            validation_work = work / "validation"
            validation_work.mkdir()
            validation_root = make_sysroot(
                args.output, validation_work,
                ("fontconfig", "harfbuzz", "fribidi", "cairo", "pango"),
            )
            checked = validate_elf_closure(validation_root)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"build_desktop_libraries: {exc}", file=sys.stderr)
        return 1
    print(f"built Meldra desktop libraries; validated {checked} target ELF files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
