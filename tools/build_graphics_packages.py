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
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kiln.cli import build, install


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def source(lock: dict, cache: Path, work: Path) -> Path:
    # "file" names the cached download when the URL's last component is ambiguous (GitHub tag archives).
    archive = cache / lock.get("file", Path(lock["url"]).name)
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
        # Some release tarballs carry symlinks to absolute paths on the maintainer's machine (kmod's
        # build-aux/compile -> /usr/share/automake-*/compile). They point at nothing here; skip them, loudly.
        skipped = [m for m in members if m.issym() and m.linkname.startswith("/")]
        for member in skipped:
            print(f"skipping absolute symlink in source: {member.name} -> {member.linkname}", flush=True)
        members = [m for m in members if m not in skipped]
        bundle.extractall(destination, members=members, filter="data")
    roots = list(destination.iterdir())
    if len(roots) != 1 or not roots[0].is_dir():
        raise RuntimeError(f"source archive has unexpected layout: {archive}")
    return roots[0]


def python_wheel(lock: dict, cache: Path, work: Path) -> Path:
    wheel = cache / Path(lock["url"]).name
    cache.mkdir(parents=True, exist_ok=True)
    if not wheel.is_file():
        temporary = wheel.with_suffix(wheel.suffix + ".part")
        with urllib.request.urlopen(lock["url"], timeout=60) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output)
        temporary.replace(wheel)
    if digest(wheel) != lock["sha256"]:
        raise RuntimeError(f"checksum mismatch: {wheel}")
    target = work / "python-build-tools"
    target.mkdir()
    with zipfile.ZipFile(wheel) as bundle:
        for member in bundle.infolist():
            path = Path(member.filename)
            if path.is_absolute() or ".." in path.parts:
                raise RuntimeError(f"unsafe wheel member: {member.filename}")
        bundle.extractall(target)
    return target


def run(*command: str, env: dict[str, str] | None = None) -> None:
    subprocess.run(command, check=True, env=env)


def require_glibc_elf(path: Path) -> None:
    result = subprocess.run(
        ["readelf", "-d", str(path)], capture_output=True, text=True, check=True
    )
    if "Shared library: [libc.so.6]" not in result.stdout:
        raise RuntimeError(f"target is not linked to glibc: {path}")


def require_embedded_gcc_runtime(path: Path) -> None:
    result = subprocess.run(
        ["readelf", "-d", str(path)], capture_output=True, text=True, check=True
    )
    for library in ("libstdc++.so", "libgcc_s.so"):
        if library in result.stdout:
            raise RuntimeError(f"undeclared GCC runtime dependency in {path}: {library}")


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


def build_zlib(
    lock: dict, cache: Path, work: Path, output: Path, sysroot: Path
) -> None:
    upstream = source(lock, cache, work / "zlib-source")
    build_dir = work / "zlib-build"
    stage = work / "zlib-package/root"
    build_dir.mkdir()
    environment = target_environment(sysroot)
    subprocess.run(
        [str(upstream / "configure"), "--prefix=/usr"], cwd=build_dir,
        env=environment, check=True,
    )
    subprocess.run(["make", "-j8"], cwd=build_dir, env=environment, check=True)
    subprocess.run(
        ["make", "install", f"DESTDIR={stage}"], cwd=build_dir,
        env=environment, check=True,
    )
    library = next((stage / "usr/lib").glob("libz.so.1.*"), None)
    if library is None:
        raise RuntimeError("zlib shared library was not installed")
    require_glibc_elf(library)
    static = stage / "usr/lib/libz.a"
    if static.is_file():
        static.unlink()
    package = manifest(stage, "zlib", lock["version"], ("glibc",))
    build(package, output / f"zlib-{lock['version']}.kiln")


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
    native_pkgconfig = ":".join(
        (str(native_prefix / "lib64/pkgconfig"), str(native_prefix / "lib/pkgconfig"))
    )
    environment["PKG_CONFIG_PATH"] = native_pkgconfig
    # Meson separates host and build-machine pkg-config lookups during a cross
    # build. wayland-scanner is a build-machine dependency, so the unqualified
    # path above is insufficient on hosts carrying an older Wayland release.
    environment["PKG_CONFIG_PATH_FOR_BUILD"] = native_pkgconfig
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


def build_libxkbcommon(lock: dict, cache: Path, work: Path, output: Path, sysroot: Path) -> None:
    upstream = source(lock, cache, work / "libxkbcommon-source")
    stage = work / "libxkbcommon-package/root"
    build_dir = work / "libxkbcommon-build"
    cross = work / "libxkbcommon-cross.ini"
    cross.write_text(
        "[binaries]\n"
        f"c = ['gcc', '--sysroot={sysroot.resolve()}']\n"
        "ar = 'ar'\nstrip = 'strip'\npkg-config = 'pkg-config'\n"
        "[host_machine]\nsystem = 'linux'\ncpu_family = 'x86_64'\n"
        "cpu = 'x86_64'\nendian = 'little'\n"
        "[properties]\nneeds_exe_wrapper = true\n"
        f"sys_root = '{sysroot.resolve()}'\n"
        f"pkg_config_libdir = ['{sysroot.resolve()}/usr/lib64/pkgconfig', "
        f"'{sysroot.resolve()}/usr/lib/pkgconfig', '{sysroot.resolve()}/usr/share/pkgconfig']\n",
        encoding="utf-8",
    )
    run(
        "meson", "setup", str(build_dir), str(upstream), "--prefix=/usr",
        "--cross-file", str(cross), "-Denable-tools=false", "-Denable-x11=false",
        "-Denable-wayland=false", "-Denable-xkbregistry=false",
        "-Denable-bash-completion=false", "-Denable-docs=false",
    )
    run("meson", "compile", "-C", str(build_dir))
    run("meson", "install", "-C", str(build_dir), "--destdir", str(stage))
    library = next((stage / "usr/lib").glob("libxkbcommon.so.0.*"), None)
    if library is None:
        raise RuntimeError("libxkbcommon shared library was not installed")
    require_glibc_elf(library)
    package = manifest(stage, "libxkbcommon", lock["version"], ("glibc",))
    build(package, output / f"libxkbcommon-{lock['version']}.kiln")


def build_libinput(lock: dict, cache: Path, work: Path, output: Path) -> None:
    upstream = source(lock, cache, work / "libinput-source")
    sysroot = work / "libinput-sysroot"
    sysroot.mkdir()
    dependencies = (
        ("glibc", "2.44"),
        ("eudev", "3.3.0"),
        ("libevdev", "1.13.4"),
        ("mtdev", "1.1.7"),
    )
    for name, version in dependencies:
        archive = output / f"{name}-{version}.kiln"
        if not archive.is_file():
            raise RuntimeError(f"libinput build dependency package missing: {archive}")
        install(archive, sysroot)
    stage = work / "libinput-package/root"
    build_dir = work / "libinput-build"
    cross = work / "libinput-cross.ini"
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
    run(
        "meson", "setup", str(build_dir), str(upstream), "--prefix=/usr",
        "--cross-file", str(cross), "-Dtests=false", "-Ddocumentation=false",
        "-Ddebug-gui=false", "-Dlibwacom=false", "-Dlua-plugins=disabled",
        "-Dzshcompletiondir=no",
    )
    run("meson", "compile", "-C", str(build_dir))
    run("meson", "install", "-C", str(build_dir), "--destdir", str(stage))
    library = next((stage / "usr/lib").glob("libinput.so.10.*"), None)
    if library is None:
        raise RuntimeError("libinput shared library was not installed")
    require_glibc_elf(library)
    package = manifest(
        stage, "libinput", lock["version"], ("glibc", "eudev", "libevdev", "mtdev")
    )
    build(package, output / f"libinput-{lock['version']}.kiln")


def build_mesa(
    lock: dict, pyyaml_lock: dict, cache: Path, work: Path, output: Path
) -> None:
    upstream = source(lock, cache, work / "mesa-source")
    build_python = python_wheel(pyyaml_lock, cache, work)
    sysroot = work / "mesa-sysroot"
    sysroot.mkdir()
    dependencies = (
        ("glibc", "2.44"),
        ("libdrm", "2.4.134"),
        ("libffi", "3.8.0"),
        ("expat", "2.10.0"),
        ("wayland", "1.26.0"),
        ("wayland-protocols", "1.49"),
    )
    for name, version in dependencies:
        archive = output / f"{name}-{version}.kiln"
        if not archive.is_file():
            raise RuntimeError(f"Mesa build dependency package missing: {archive}")
        install(archive, sysroot)
    stage = work / "mesa-package/root"
    build_dir = work / "mesa-build"
    cross = work / "mesa-cross.ini"
    loader = sysroot / "lib64/ld-linux-x86-64.so.2"
    library_path = f"{sysroot}/lib64:{sysroot}/usr/lib64:{sysroot}/usr/lib"
    cross.write_text(
        "[binaries]\n"
        f"c = ['gcc', '--sysroot={sysroot.resolve()}']\n"
        f"cpp = ['g++', '--sysroot={sysroot.resolve()}']\n"
        "ar = 'ar'\nstrip = 'strip'\npkg-config = 'pkg-config'\n"
        f"exe_wrapper = ['{loader.resolve()}', '--library-path', '{library_path}']\n"
        "[host_machine]\nsystem = 'linux'\ncpu_family = 'x86_64'\n"
        "cpu = 'x86_64'\nendian = 'little'\n"
        "[built-in options]\n"
        "c_link_args = ['-static-libgcc']\n"
        "cpp_link_args = ['-static-libgcc', '-static-libstdc++']\n"
        "[properties]\n"
        f"sys_root = '{sysroot.resolve()}'\n"
        f"pkg_config_libdir = ['{sysroot.resolve()}/usr/lib64/pkgconfig', "
        f"'{sysroot.resolve()}/usr/lib/pkgconfig', '{sysroot.resolve()}/usr/share/pkgconfig']\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(build_python)
    native_tools = work / "mesa-native-tools"
    native_tools.mkdir()
    scanner_wrapper = native_tools / "wayland-scanner"
    scanner_wrapper.write_text(
        "#!/bin/sh\n"
        f'exec "{loader.resolve()}" --library-path "{library_path}" '
        f'"{(sysroot / "usr/bin/wayland-scanner").resolve()}" "$@"\n',
        encoding="utf-8",
    )
    scanner_wrapper.chmod(0o755)
    environment["PATH"] = f"{native_tools}:{environment['PATH']}"
    run(
        "meson", "setup", str(build_dir), str(upstream), "--prefix=/usr",
        "--cross-file", str(cross), "-Dplatforms=wayland",
        "-Dgallium-drivers=softpipe", "-Dvulkan-drivers=[]", "-Dglx=disabled",
        "-Degl=enabled", "-Dgbm=enabled", "-Dgles1=enabled", "-Dgles2=enabled",
        "-Dopengl=true", "-Dllvm=disabled", "-Dshared-llvm=disabled",
        "-Ddraw-use-llvm=false", "-Dlibunwind=disabled", "-Dzlib=disabled",
        "-Dshader-cache=disabled",
        "-Dvalgrind=disabled", "-Dbuild-tests=false", "-Dgallium-va=disabled",
        "-Dvideo-codecs=[]",
        env=environment,
    )
    run("meson", "compile", "-C", str(build_dir), env=environment)
    run(
        "meson", "install", "-C", str(build_dir), "--destdir", str(stage),
        env=environment,
    )
    egl = next((stage / "usr/lib").glob("libEGL.so.1.*"), None)
    gallium = next((stage / "usr/lib").glob("libgallium-*.so"), None)
    gbm_backend = stage / "usr/lib/gbm/dri_gbm.so"
    if egl is None or gallium is None or not gbm_backend.is_file():
        raise RuntimeError("Mesa EGL, Gallium, or GBM driver was not installed")
    require_glibc_elf(egl)
    require_glibc_elf(gallium)
    require_glibc_elf(gbm_backend)
    require_embedded_gcc_runtime(egl)
    require_embedded_gcc_runtime(gallium)
    require_embedded_gcc_runtime(gbm_backend)
    package = manifest(
        stage, "mesa", lock["version"],
        ("glibc", "libdrm", "libffi", "expat", "wayland", "wayland-protocols"),
    )
    build(package, output / f"mesa-{lock['version']}.kiln")


def build_small_meson_library(
    lock: dict, name: str, cache: Path, work: Path, output: Path,
    sysroot: Path, options: tuple[str, ...], library_pattern: str,
) -> None:
    upstream = source(lock, cache, work / f"{name}-source")
    stage = work / f"{name}-package/root"
    build_dir = work / f"{name}-build"
    cross = work / f"{name}-cross.ini"
    cross.write_text(
        "[binaries]\n"
        f"c = ['gcc', '--sysroot={sysroot.resolve()}']\n"
        "ar = 'ar'\nstrip = 'strip'\npkg-config = 'pkg-config'\n"
        "[host_machine]\nsystem = 'linux'\ncpu_family = 'x86_64'\n"
        "cpu = 'x86_64'\nendian = 'little'\n"
        "[properties]\n"
        f"sys_root = '{sysroot.resolve()}'\n"
        f"pkg_config_libdir = ['{sysroot.resolve()}/usr/lib64/pkgconfig', "
        f"'{sysroot.resolve()}/usr/lib/pkgconfig', '{sysroot.resolve()}/usr/share/pkgconfig']\n",
        encoding="utf-8",
    )
    run(
        "meson", "setup", str(build_dir), str(upstream), "--prefix=/usr",
        "--cross-file", str(cross), *options,
    )
    run("meson", "compile", "-C", str(build_dir))
    run("meson", "install", "-C", str(build_dir), "--destdir", str(stage))
    library = next((stage / "usr/lib").glob(library_pattern), None)
    if library is None:
        raise RuntimeError(f"{name} shared library was not installed")
    require_glibc_elf(library)
    package = manifest(stage, name, lock["version"], ("glibc",))
    build(package, output / f"{name}-{lock['version']}.kiln")


def build_wlroots(lock: dict, cache: Path, work: Path, output: Path) -> None:
    upstream = source(lock, cache, work / "wlroots-source")
    sysroot = work / "wlroots-sysroot"
    sysroot.mkdir()
    dependencies = (
        ("glibc", "2.44"), ("libdrm", "2.4.134"), ("libffi", "3.8.0"),
        ("expat", "2.10.0"), ("wayland", "1.26.0"),
        ("wayland-protocols", "1.49"), ("mesa", "26.2.5"),
        ("libxkbcommon", "1.11.0"), ("eudev", "3.3.0"),
        ("libevdev", "1.13.4"), ("mtdev", "1.1.7"),
        ("libinput", "1.30.1"), ("pixman", "0.46.4"), ("seatd", "0.9.3"),
        ("libdisplay-info", "0.4.0"),
    )
    for name, version in dependencies:
        archive = output / f"{name}-{version}.kiln"
        if not archive.is_file():
            raise RuntimeError(f"wlroots build dependency package missing: {archive}")
        install(archive, sysroot)
    stage = work / "wlroots-package/root"
    build_dir = work / "wlroots-build"
    cross = work / "wlroots-cross.ini"
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
    native_tools = work / "wlroots-native-tools"
    native_tools.mkdir()
    scanner_wrapper = native_tools / "wayland-scanner"
    scanner_wrapper.write_text(
        "#!/bin/sh\n"
        f'exec "{loader.resolve()}" --library-path "{library_path}" '
        f'"{(sysroot / "usr/bin/wayland-scanner").resolve()}" "$@"\n',
        encoding="utf-8",
    )
    scanner_wrapper.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{native_tools}:{environment['PATH']}"
    run(
        "meson", "setup", str(build_dir), str(upstream), "--prefix=/usr",
        "--cross-file", str(cross), "-Dexamples=false", "-Dxwayland=disabled",
        "-Drenderers=gles2", "-Dbackends=drm,libinput", "-Dallocators=gbm",
        "-Dsession=enabled", "-Dcolor-management=disabled",
        "-Dlibliftoff=disabled", "-Dc_args=-Wno-error=discarded-qualifiers",
        env=environment,
    )
    run("meson", "compile", "-C", str(build_dir), env=environment)
    run(
        "meson", "install", "-C", str(build_dir), "--destdir", str(stage),
        env=environment,
    )
    library = next((stage / "usr/lib").glob("libwlroots-0.19.so*"), None)
    if library is None:
        raise RuntimeError("wlroots shared library was not installed")
    require_glibc_elf(library)
    package = manifest(
        stage, "wlroots", lock["version"],
        tuple(name for name, _ in dependencies),
    )
    build(package, output / f"wlroots-{lock['version']}.kiln")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=Path("sources.lock.toml"))
    parser.add_argument("--cache", type=Path, default=Path("build/downloads"))
    parser.add_argument("--sysroot", type=Path, default=Path("build/sysroots/glibc"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    lock_data = tomllib.loads(args.lock.read_text(encoding="utf-8"))
    locks = lock_data["desktop"]
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
            build_zlib(locks["zlib"], args.cache, work, args.output, args.sysroot)
            build_autotools(
                locks["pcre2"], "pcre2", args.cache, work, args.output,
                args.sysroot, ("--disable-static", "--enable-shared",
                               "--enable-pcre2-8", "--disable-pcre2-16",
                               "--disable-pcre2-32", "--disable-pcre2grep-jit"),
            )
            build_autotools(
                locks["libevdev"], "libevdev", args.cache, work, args.output,
                args.sysroot, ("--disable-static", "--enable-shared"),
            )
            build_autotools(
                locks["mtdev"], "mtdev", args.cache, work, args.output,
                args.sysroot, ("--disable-static", "--enable-shared"),
            )
            build_autotools(
                locks["eudev"], "eudev", args.cache, work, args.output,
                args.sysroot,
                ("--disable-static", "--enable-shared", "--disable-blkid",
                 "--disable-selinux", "--disable-kmod", "--disable-manpages",
                 "--disable-systemd-hwdb", "--disable-mtd_probe", "--disable-tpm2",
                 "--with-rootprefix=/usr", "--with-rootlibdir=/usr/lib",
                 "--with-rootlibexecdir=/usr/lib/udev"),
            )
            build_wayland(locks["wayland"], args.cache, work, args.output, args.sysroot)
            build_protocols(locks["wayland_protocols"], args.cache, work, args.output)
            build_libdrm(locks["libdrm"], args.cache, work, args.output, args.sysroot)
            build_libxkbcommon(
                locks["libxkbcommon"], args.cache, work, args.output, args.sysroot
            )
            build_libinput(locks["libinput"], args.cache, work, args.output)
            build_mesa(
                locks["mesa"], lock_data["host_tools"]["pyyaml"],
                args.cache, work, args.output,
            )
            build_small_meson_library(
                locks["pixman"], "pixman", args.cache, work, args.output,
                args.sysroot, ("-Dtests=disabled", "-Ddemos=disabled", "-Dgtk=disabled",
                               "-Dlibpng=disabled", "-Dopenmp=disabled"),
                "libpixman-1.so.0.*",
            )
            build_small_meson_library(
                locks["seatd"], "seatd", args.cache, work, args.output,
                args.sysroot, ("-Dlibseat-logind=disabled", "-Dlibseat-seatd=enabled",
                               "-Dserver=enabled", "-Dexamples=disabled",
                               "-Dman-pages=disabled"),
                "libseat.so.1",
            )
            build_small_meson_library(
                locks["libdisplay_info"], "libdisplay-info", args.cache, work,
                args.output, args.sysroot, (), "libdisplay-info.so.0.*",
            )
            build_wlroots(locks["wlroots"], args.cache, work, args.output)
    except (OSError, RuntimeError, subprocess.CalledProcessError, tarfile.TarError) as exc:
        print(f"build_graphics_packages: {exc}", file=sys.stderr)
        return 1
    print(f"built graphics foundation packages in {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
