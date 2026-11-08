#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Build the first target-side Meldra graphics packages from pinned sources."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kiln.cli import build, install


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def source(lock: dict, cache: Path, work: Path) -> Path:
    archive = cache / Path(lock["url"]).name
    cache.mkdir(parents=True, exist_ok=True)
    if not archive.is_file():
        temporary = archive.with_suffix(archive.suffix + ".part")
        with urllib.request.urlopen(lock["url"], timeout=60) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output)
        temporary.replace(archive)
    if digest(archive) != lock["sha256"]:
        raise RuntimeError(f"checksum mismatch: {archive}")
    destination = work / lock["version"]
    destination.mkdir(parents=True)
    with tarfile.open(archive, "r:*") as bundle:
        members = bundle.getmembers()
        for member in members:
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise RuntimeError(f"unsafe source member: {member.name}")
        bundle.extractall(destination, members=members, filter="data")
    roots = list(destination.iterdir())
    if len(roots) != 1 or not roots[0].is_dir():
        raise RuntimeError(f"source archive has unexpected layout: {archive}")
    return roots[0]


def run(*command: str, env: dict[str, str] | None = None) -> None:
    subprocess.run(command, check=True, env=env)


def require_glibc_elf(path: Path) -> None:
    result = subprocess.run(
        ["readelf", "-d", str(path)], capture_output=True, text=True, check=True
    )
    if "Shared library: [libc.so.6]" not in result.stdout:
        raise RuntimeError(f"target is not linked to glibc: {path}")


def manifest(root: Path, name: str, version: str, dependencies: tuple[str, ...]) -> Path:
    package = root.parent
    deps = ", ".join(f'"{item}"' for item in dependencies)
    (package / "KILN.toml").write_text(
        "[package]\n"
        f'name = "{name}"\nversion = "{version}"\narchitecture = "x86_64"\n'
        f"dependencies = [{deps}]\n",
        encoding="utf-8",
    )
    return package


def target_environment(sysroot: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "CC": f"gcc --sysroot={sysroot.resolve()}",
            "CFLAGS": "-O2 -pipe",
            "PKG_CONFIG_SYSROOT_DIR": str(sysroot.resolve()),
            "PKG_CONFIG_LIBDIR": ":".join(
                (
                    f"{sysroot.resolve()}/usr/lib64/pkgconfig",
                    f"{sysroot.resolve()}/usr/lib/pkgconfig",
                    f"{sysroot.resolve()}/usr/share/pkgconfig",
                )
            ),
        }
    )
    return environment


def build_autotools(
    lock: dict,
    name: str,
    cache: Path,
    work: Path,
    output: Path,
    sysroot: Path,
    options: tuple[str, ...],
) -> None:
    upstream = source(lock, cache, work / f"{name}-source")
    build_dir = work / f"{name}-build"
    stage = work / f"{name}-package/root"
    build_dir.mkdir()
    environment = target_environment(sysroot)
    subprocess.run(
        [str(upstream / "configure"), "--build=x86_64-pc-linux-gnu",
         "--host=x86_64-unknown-linux-gnu", "--prefix=/usr", *options],
        cwd=build_dir,
        env=environment,
        check=True,
    )
    subprocess.run(["make", "-j8"], cwd=build_dir, env=environment, check=True)
    subprocess.run(
        ["make", "install", f"DESTDIR={stage}"], cwd=build_dir, env=environment, check=True
    )
    info_index = stage / "usr/share/info/dir"
    if info_index.is_file():
        info_index.unlink()
    package = manifest(stage, name, lock["version"], ("glibc",))
    build(package, output / f"{name}-{lock['version']}.kiln")


def build_protocols(lock: dict, cache: Path, work: Path, output: Path) -> None:
    upstream = source(lock, cache, work / "protocols-source")
    stage = work / "protocols-package/root"
    build_dir = work / "protocols-build"
    environment = os.environ.copy()
    # Scanner-generated convenience headers are optional. The host scanner may
    # be older than the pinned protocol XML, so package the canonical XML only.
    environment["PKG_CONFIG_LIBDIR"] = str(work / "empty-pkgconfig")
    run(
        "meson", "setup", str(build_dir), str(upstream), "--prefix=/usr", "-Dtests=false",
        env=environment,
    )
    run("meson", "install", "-C", str(build_dir), "--destdir", str(stage))
    package = manifest(stage, "wayland-protocols", lock["version"], ())
    build(package, output / f"wayland-protocols-{lock['version']}.kiln")


def build_wayland(lock: dict, cache: Path, work: Path, output: Path, base_sysroot: Path) -> None:
    upstream = source(lock, cache, work / "wayland-source")
    native_prefix = work / "wayland-native"
    native_build = work / "wayland-native-build"
    run(
        "meson", "setup", str(native_build), str(upstream), f"--prefix={native_prefix}",
        "-Dlibraries=false", "-Dscanner=true", "-Dtests=false", "-Ddocumentation=false",
        "-Ddtd_validation=false",
    )
    run("meson", "compile", "-C", str(native_build))
    run("meson", "install", "-C", str(native_build))
    sysroot = work / "wayland-sysroot"
    sysroot.mkdir()
    for archive in (
        output / "glibc-2.44.kiln",
        output / "libffi-3.8.0.kiln",
        output / "expat-2.10.0.kiln",
    ):
        if not archive.is_file():
            raise RuntimeError(f"Wayland build dependency package missing: {archive}")
        install(archive, sysroot)
    build_dir = work / "wayland-build"
    stage = work / "wayland-package/root"
    cross = work / "wayland-cross.ini"
    loader = sysroot / "lib64/ld-linux-x86-64.so.2"
    library_path = f"{sysroot}/lib64:{sysroot}/usr/lib64:{sysroot}/usr/lib"
    cross.write_text(
        "[binaries]\n"
        f"c = ['gcc', '--sysroot={sysroot.resolve()}']\n"
        "ar = 'ar'\nstrip = 'strip'\npkg-config = 'pkg-config'\n"
        f"exe_wrapper = ['{loader.resolve()}', '--library-path', '{library_path}']\n"
        "[host_machine]\nsystem = 'linux'\ncpu_family = 'x86_64'\n"
        "cpu = 'x86_64'\nendian = 'little'\n"
        "[properties]\n"
        f"sys_root = '{sysroot.resolve()}'\n"
        f"pkg_config_libdir = ['{sysroot.resolve()}/usr/lib64/pkgconfig', "
        f"'{sysroot.resolve()}/usr/lib/pkgconfig', '{sysroot.resolve()}/usr/share/pkgconfig']\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["PATH"] = f"{native_prefix / 'bin'}:{environment['PATH']}"
    environment["PKG_CONFIG_PATH"] = str(native_prefix / "lib/pkgconfig")
    run(
        "meson", "setup", str(build_dir), str(upstream), "--prefix=/usr",
        "--cross-file", str(cross), "-Dtests=false", "-Ddocumentation=false",
        "-Ddtd_validation=false",
        env=environment,
    )
    run("meson", "compile", "-C", str(build_dir), env=environment)
    run("meson", "install", "-C", str(build_dir), "--destdir", str(stage), env=environment)
    require_glibc_elf(stage / "usr/lib/libwayland-client.so.0.26.0")
    require_glibc_elf(stage / "usr/lib/libwayland-server.so.0.26.0")
    scanner = stage / "usr/bin/wayland-scanner"
    interpreter = subprocess.run(
        ["readelf", "-l", str(scanner)], capture_output=True, text=True, check=True
    ).stdout
    if "/lib64/ld-linux-x86-64.so.2" not in interpreter:
        raise RuntimeError("wayland-scanner does not use the Meldra glibc loader")
    package = manifest(stage, "wayland", lock["version"], ("glibc", "libffi", "expat"))
    build(package, output / f"wayland-{lock['version']}.kiln")


def build_libdrm(lock: dict, cache: Path, work: Path, output: Path, sysroot: Path) -> None:
    libc = sysroot / "lib64/libc.so.6"
    if not libc.is_file():
        raise RuntimeError(f"Meldra glibc sysroot not found: {libc}")
    upstream = source(lock, cache, work / "libdrm-source")
    stage = work / "libdrm-package/root"
    build_dir = work / "libdrm-build"
    cross = work / "meldra-cross.ini"
    cross.write_text(
        "[binaries]\n"
        f"c = ['gcc', '--sysroot={sysroot.resolve()}']\n"
        "ar = 'ar'\nstrip = 'strip'\npkg-config = 'pkg-config'\n"
        "[host_machine]\nsystem = 'linux'\ncpu_family = 'x86_64'\n"
        "cpu = 'x86_64'\nendian = 'little'\n"
        "[properties]\nneeds_exe_wrapper = true\n"
        f"sys_root = '{sysroot.resolve()}'\n"
        f"pkg_config_libdir = ['{sysroot.resolve()}/usr/lib64/pkgconfig', "
        f"'{sysroot.resolve()}/usr/share/pkgconfig']\n",
        encoding="utf-8",
    )
    options = [
        "intel", "radeon", "amdgpu", "nouveau", "vmwgfx", "omap", "exynos",
        "freedreno", "tegra", "vc4", "etnaviv", "cairo-tests", "man-pages", "valgrind",
    ]
    run(
        "meson", "setup", str(build_dir), str(upstream), "--prefix=/usr",
        "--cross-file", str(cross), "-Dtests=false", "-Dudev=false",
        *(f"-D{option}=disabled" for option in options),
    )
    run("meson", "compile", "-C", str(build_dir))
    run("meson", "install", "-C", str(build_dir), "--destdir", str(stage))
    require_glibc_elf(stage / "usr/lib/libdrm.so.2.134.0")
    package = manifest(stage, "libdrm", lock["version"], ("glibc",))
    build(package, output / f"libdrm-{lock['version']}.kiln")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=Path("sources.lock.toml"))
    parser.add_argument("--cache", type=Path, default=Path("build/downloads"))
    parser.add_argument("--sysroot", type=Path, default=Path("build/sysroots/glibc"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    locks = tomllib.loads(args.lock.read_text(encoding="utf-8"))["desktop"]
    args.output.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="meldra-graphics-") as directory:
            work = Path(directory)
            build_autotools(
                locks["libffi"], "libffi", args.cache, work, args.output, args.sysroot,
                ("--disable-static", "--enable-shared", "--disable-multi-os-directory"),
            )
            build_autotools(
                locks["expat"], "expat", args.cache, work, args.output, args.sysroot,
                ("--disable-static", "--enable-shared", "--without-xmlwf",
                 "--without-docbook", "--without-tests", "--without-examples"),
            )
            build_wayland(locks["wayland"], args.cache, work, args.output, args.sysroot)
            build_protocols(locks["wayland_protocols"], args.cache, work, args.output)
            build_libdrm(locks["libdrm"], args.cache, work, args.output, args.sysroot)
    except (OSError, RuntimeError, subprocess.CalledProcessError, tarfile.TarError) as exc:
        print(f"build_graphics_packages: {exc}", file=sys.stderr)
        return 1
    print(f"built graphics foundation packages in {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
