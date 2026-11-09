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
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kiln.cli import build
from tools.build_graphics_packages import manifest, python_wheel, require_glibc_elf, source
import tools.build_desktop_libraries as desktop


def pin(lock_data: dict, name: str) -> dict:
    """A package's pin: the gnome section first, then the older desktop section."""
    key = name.replace("-", "_")
    return lock_data["gnome"][key] if key in lock_data["gnome"] else lock_data["desktop"][key]


class Package:
    def __init__(self, name, deps, kind="meson", options=(), pattern=None, post=None, env=None,
                 introspect=False, rust=False):
        self.introspect = introspect
        self.rust = rust
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


def introspection_cross_file(original):
    """Wrap desktop.cross_file so meson finds the host g-ir-scanner and can run target binaries."""
    def make(work: Path, sysroot: Path) -> Path:
        path = original(work, sysroot)
        text = path.read_text(encoding="utf-8")
        gi = HOST_GI.resolve()
        root = sysroot.resolve()
        ldd = work / "cross-ldd"
        loader = root / "lib64/ld-linux-x86-64.so.2"
        library_path = f"{root}/lib64:{root}/usr/lib64:{root}/usr/lib"
        ldd.write_text(f'#!/bin/sh\nexec "{loader}" --library-path "{library_path}" --list "$@"\n')
        ldd.chmod(0o755)
        text = text.replace("[binaries]\n", "[binaries]\n"
                            f"g-ir-scanner = '{gi}/bin/g-ir-scanner'\n"
                            f"g-ir-compiler = '{gi}/bin/g-ir-compiler'\n"
                            f"g-ir-generate = '{gi}/bin/g-ir-generate'\n", 1)
        # The host .pc requires the host's glib; meson only reads its variables here.
        shim = work / "gi-pc"
        shim.mkdir(exist_ok=True)
        lines = [line for line in (gi / "lib/pkgconfig/gobject-introspection-1.0.pc").read_text().splitlines()
                 if not line.startswith(("Requires", "Libs", "Cflags"))]
        (shim / "gobject-introspection-1.0.pc").write_text("\n".join(lines) + "\nLibs:\nCflags:\n")
        text = text.replace("pkg_config_libdir = [", f"pkg_config_libdir = ['{shim}', ", 1)
        path.write_text(text, encoding="utf-8")
        # Search only the target's .gir files, never the host's (they are a different glib).
        os.environ["XDG_DATA_DIRS"] = f"{root}/usr/share"
        os.environ["GI_TYPELIB_PATH"] = f"{root}/usr/lib/girepository-1.0:{root}/usr/lib64/girepository-1.0"
        os.environ["GI_GIR_PATH"] = f"{root}/usr/share/gir-1.0"
        os.environ["GI_CROSS_LAUNCHER"] = (
            f"{loader} --library-path {library_path}")
        os.environ["PATH"] = f"{work}:{os.environ['PATH']}"  # cross-ldd first
        return path
    return make


def meson_data_build(pkg: Package, lock: dict, cache: Path, work: Path, output: Path) -> None:
    """Meson package with no shared library (schemas, data files): no ELF check, `expect` must exist."""
    original = desktop.cross_file
    if pkg.introspect:
        desktop.cross_file = introspection_cross_file(original)
    try:
        unit = work / pkg.name
        unit.mkdir()
        upstream = source(lock, cache, unit / "source")
        sysroot = desktop.make_sysroot(output, unit, pkg.deps)
        cross = desktop.cross_file(unit, sysroot)
        build_dir, stage = unit / "build", unit / "package/root"
        subprocess.run([*desktop.MESON, "setup", str(build_dir), str(upstream), "--prefix=/usr",
                        "--cross-file", str(cross), *pkg.options], check=True)
        subprocess.run([*desktop.MESON, "compile", "-C", str(build_dir)], check=True)
        subprocess.run([*desktop.MESON, "install", "-C", str(build_dir), "--destdir", str(stage)], check=True)
        if not (stage / pkg.pattern).exists():
            raise RuntimeError(f"{pkg.name}: expected file not installed: {pkg.pattern}")
        build(manifest(stage, pkg.name, lock["version"], pkg.deps), output / f"{pkg.name}-{lock['version']}.kiln")
    finally:
        desktop.cross_file = original


def autotools_data_build(pkg: Package, lock: dict, cache: Path, work: Path, output: Path) -> None:
    """Autotools package that installs only data; `pattern` is a path under the stage root."""
    unit = work / pkg.name
    unit.mkdir()
    upstream = source(lock, cache, unit / "source")
    build_dir, stage = unit / "build", unit / "package/root"
    build_dir.mkdir()
    subprocess.run([str(upstream / "configure"), "--prefix=/usr", *pkg.options], cwd=build_dir, check=True)
    subprocess.run(["make"], cwd=build_dir, check=True)
    subprocess.run(["make", "install", f"DESTDIR={stage}"], cwd=build_dir, check=True)
    if not (stage / pkg.pattern).exists():
        raise RuntimeError(f"{pkg.name}: expected file not installed: {pkg.pattern}")
    build(manifest(stage, pkg.name, lock["version"], pkg.deps), output / f"{pkg.name}-{lock['version']}.kiln")


def nspr_build(pkg: Package, lock: dict, cache: Path, work: Path, output: Path) -> None:
    unit = work / pkg.name
    unit.mkdir()
    upstream = source(lock, cache, unit / "source")
    sysroot = desktop.make_sysroot(output, unit, pkg.deps)
    root = sysroot.resolve()
    build_dir, stage = unit / "build", unit / "package/root"
    build_dir.mkdir()
    env = os.environ.copy()
    env.update({"CC": f"gcc --sysroot={root}", "CFLAGS": "-O2 -pipe", "HOST_CC": "gcc"})
    subprocess.run([str(upstream / "nspr/configure"), "--prefix=/usr", "--libdir=/usr/lib",
                    "--enable-64bit", "--disable-debug", "--enable-optimize", "--with-mozilla=no"],
                   cwd=build_dir, env=env, check=True)
    subprocess.run(["make"], cwd=build_dir, env=env, check=True)
    subprocess.run(["make", "install", f"DESTDIR={stage}"], cwd=build_dir, env=env, check=True)
    library = next((stage / "usr/lib").glob("libnspr4.so"), None)
    if library is None:
        raise RuntimeError("libnspr4 not installed")
    require_glibc_elf(library)
    build(manifest(stage, pkg.name, lock["version"], pkg.deps), output / f"{pkg.name}-{lock['version']}.kiln")


def nss_build(pkg: Package, lock: dict, cache: Path, work: Path, output: Path) -> None:
    unit = work / pkg.name
    unit.mkdir()
    upstream = source(lock, cache, unit / "source")
    sysroot = desktop.make_sysroot(output, unit, pkg.deps)
    root = sysroot.resolve()
    stage = unit / "package/root"
    nss = upstream / "nss"
    # nsinstall runs during the build, so it must be a host binary (the target loader is not the host's).
    host_nsinstall = unit / "nsinstall"
    subprocess.run(["gcc", "-O2", "-o", str(host_nsinstall), str(nss / "coreconf/nsinstall/nsinstall.c"),
                    str(nss / "coreconf/nsinstall/pathsub.c")], check=True)
    common = [
        "BUILD_OPT=1", "USE_64=1", "NSS_DISABLE_GTESTS=1", "NSS_ENABLE_WERROR=0", "NSS_USE_SYSTEM_SQLITE=1",
        "NSS_DISABLE_DBM=1", f"NSPR_INCLUDE_DIR={root}/usr/include/nspr", f"NSPR_LIB_DIR={root}/usr/lib",
        f"CC=gcc --sysroot={root}", f"CCC=g++ --sysroot={root}", f"NSINSTALL={host_nsinstall}",
        f"LDFLAGS=-Wl,-rpath-link,{root}/usr/lib -Wl,-rpath-link,{root}/lib64",
    ]
    # `nss_build_all` would also build a bundled NSPR; the NSPR package is already in the sysroot.
    subprocess.run(["make", "all", *common], cwd=nss, check=True)
    subprocess.run(["make", "latest", *common], cwd=nss, check=True)
    dist = next(d for d in (upstream / "dist").iterdir() if d.name.endswith("_OPT.OBJ"))
    lib, inc, pc = stage / "usr/lib", stage / "usr/include/nss", stage / "usr/lib/pkgconfig"
    for directory in (lib, inc, pc):
        directory.mkdir(parents=True)
    for so in dist.joinpath("lib").glob("*.so"):
        (lib / so.name).write_bytes(so.read_bytes())
        (lib / so.name).chmod(0o755)
    for header in (upstream / "dist/public/nss").glob("*.h"):
        (inc / header.name).write_bytes(header.read_bytes())
    version = lock["version"]
    template = (nss / "pkg/pkg-config/nss.pc.in").read_text()
    for key, value in (("%prefix%", "/usr"), ("%exec_prefix%", "/usr"), ("%libdir%", "/usr/lib"),
                       ("%includedir%", "/usr/include/nss"), ("%NSS_VERSION%", version),
                       ("%NSPR_VERSION%", "4.40"), ("%NSS_VERSION%", version)):
        template = template.replace(key, value)
    (pc / "nss.pc").write_text(template)
    library = lib / "libnss3.so"
    if not library.is_file():
        raise RuntimeError("libnss3 not installed")
    require_glibc_elf(library)
    build(manifest(stage, pkg.name, version, pkg.deps), output / f"{pkg.name}-{version}.kiln")


def icu_build(pkg: Package, lock: dict, cache: Path, work: Path, output: Path) -> None:
    """ICU runs its own data tools while building, so a native build comes first (--with-cross-build)."""
    unit = work / pkg.name
    unit.mkdir()
    upstream = source(lock, cache, unit / "source")
    configure = upstream / "source/configure"
    common = ["--disable-tests", "--disable-samples", "--disable-extras", "--disable-static",
              "--enable-shared", "--disable-layoutex"]
    native = unit / "native"
    native.mkdir()
    subprocess.run([str(configure), *common], cwd=native, check=True)
    subprocess.run(["make"], cwd=native, check=True)
    sysroot = desktop.make_sysroot(output, unit, pkg.deps)
    root = sysroot.resolve()
    build_dir, stage = unit / "build", unit / "package/root"
    build_dir.mkdir()
    env = os.environ.copy()
    # The sysroot has no C++ runtime, so embed it rather than depend on the host's libstdc++.
    env.update({"CC": f"gcc --sysroot={root}", "CXX": f"g++ --sysroot={root}", "CFLAGS": "-O2 -pipe",
                "CXXFLAGS": "-O2 -pipe",
                "LDFLAGS": f"-static-libstdc++ -static-libgcc -Wl,-rpath-link,{root}/lib64"})
    subprocess.run([str(configure), "--build=x86_64-pc-linux-gnu", "--host=x86_64-unknown-linux-gnu",
                    f"--with-cross-build={native}", "--prefix=/usr", "--libdir=/usr/lib", *common],
                   cwd=build_dir, env=env, check=True)
    subprocess.run(["make"], cwd=build_dir, env=env, check=True)
    subprocess.run(["make", "install", f"DESTDIR={stage}"], cwd=build_dir, env=env, check=True)
    library = next((stage / "usr/lib").glob("libicuuc.so.*.*"), None)
    if library is None:
        raise RuntimeError("libicuuc not installed")
    require_glibc_elf(library)
    build(manifest(stage, pkg.name, lock["version"], pkg.deps), output / f"{pkg.name}-{lock['version']}.kiln")


def native_libical_generator(upstream: Path, work: Path) -> Path:
    """libical-glib generates its sources with a tool that must run on the build machine."""
    native = work / "libical-native"
    v4 = "LIBICAL_GLIB" in (upstream / "CMakeLists.txt").read_text()
    flags = (["-DLIBICAL_GLIB=ON", "-DLIBICAL_GOBJECT_INTROSPECTION=OFF", "-DLIBICAL_GLIB_VAPI=OFF",
              "-DLIBICAL_GLIB_BUILD_DOCS=OFF", "-DLIBICAL_BUILD_DOCS=OFF", "-DLIBICAL_BUILD_TESTING=OFF",
              "-DLIBICAL_BUILD_EXAMPLES=OFF", "-DLIBICAL_JAVA_BINDINGS=OFF", "-DLIBICAL_CXX_BINDINGS=OFF"] if v4 else
             ["-DICAL_GLIB=ON", "-DGOBJECT_INTROSPECTION=OFF", "-DICAL_GLIB_VAPI=OFF", "-DICAL_BUILD_DOCS=OFF",
              "-DWITH_CXX_BINDINGS=OFF", "-DLIBICAL_BUILD_TESTING=OFF", "-DLIBICAL_BUILD_EXAMPLES=OFF"])
    subprocess.run(["cmake", "-S", str(upstream), "-B", str(native), "-G", "Ninja",
                    f"-DCMAKE_INSTALL_PREFIX={native}/prefix", "-DCMAKE_BUILD_TYPE=Release", *flags], check=True)
    subprocess.run(["cmake", "--build", str(native)], check=True)
    subprocess.run(["cmake", "--install", str(native)], check=True, stdout=subprocess.DEVNULL)
    exported = next((native / "prefix").rglob("IcalGlibSrcGenerator.cmake"))
    return exported


def cmake_build(pkg: Package, lock: dict, cache: Path, work: Path, output: Path) -> None:
    unit = work / pkg.name
    unit.mkdir()
    upstream = source(lock, cache, unit / "source")
    extra = ()
    glib_rules = upstream / "src/libical-glib/CMakeLists.txt"
    # libical 4 needs a native copy of its generator when cross-compiling; 3.x runs it through the emulator.
    if pkg.name == "libical" and "IMPORT_ICAL_GLIB_SRC_GENERATOR" in glib_rules.read_text():
        extra = (f"-DIMPORT_ICAL_GLIB_SRC_GENERATOR={native_libical_generator(upstream, unit)}",)
    sysroot = desktop.make_sysroot(output, unit, pkg.deps)
    root = sysroot.resolve()
    build_dir, stage = unit / "build", unit / "package/root"
    # Run target programs (try_run checks, generators) through the target's own loader.
    emulator = unit / "run-target"
    emulator.write_text(
        f'#!/bin/sh\nexport GCONV_PATH="{root}/usr/lib64/gconv"\n'
        f'exec "{root}/lib64/ld-linux-x86-64.so.2" '
        f'--library-path "{root}/lib64:{root}/usr/lib64:{root}/usr/lib" "$@"\n')
    emulator.chmod(0o755)
    extra = (*extra, f"-DCMAKE_CROSSCOMPILING_EMULATOR={emulator}")
    env = os.environ.copy()
    env.update({"PKG_CONFIG_SYSROOT_DIR": str(root),
                "PKG_CONFIG_LIBDIR": f"{root}/usr/lib/pkgconfig:{root}/usr/share/pkgconfig"})
    subprocess.run(
        ["cmake", "-S", str(upstream), "-B", str(build_dir), "-G", "Ninja",
         "-DCMAKE_SYSTEM_NAME=Linux", "-DCMAKE_SYSTEM_PROCESSOR=x86_64",
         "-DCMAKE_C_COMPILER=gcc", f"-DCMAKE_C_FLAGS=--sysroot={root} -O2",
         f"-DCMAKE_EXE_LINKER_FLAGS=--sysroot={root} -static-libstdc++ -static-libgcc -Wl,-rpath-link,{root}/usr/lib -Wl,-rpath-link,{root}/lib64",
         f"-DCMAKE_SHARED_LINKER_FLAGS=--sysroot={root} -static-libstdc++ -static-libgcc -Wl,-rpath-link,{root}/usr/lib -Wl,-rpath-link,{root}/lib64",
         f"-DCMAKE_MODULE_LINKER_FLAGS=--sysroot={root} -static-libstdc++ -static-libgcc -Wl,-rpath-link,{root}/usr/lib -Wl,-rpath-link,{root}/lib64",
         f"-DCMAKE_SYSROOT={root}", f"-DCMAKE_FIND_ROOT_PATH={root}",
         "-DCMAKE_FIND_ROOT_PATH_MODE_PROGRAM=NEVER", "-DCMAKE_FIND_ROOT_PATH_MODE_LIBRARY=ONLY",
         "-DCMAKE_FIND_ROOT_PATH_MODE_INCLUDE=ONLY", "-DCMAKE_FIND_ROOT_PATH_MODE_PACKAGE=ONLY", "-DCMAKE_INSTALL_PREFIX=/usr",
         "-DCMAKE_INSTALL_LIBDIR=lib", "-DCMAKE_BUILD_TYPE=Release", *pkg.options, *extra],
        env=env, check=True)
    subprocess.run(["cmake", "--build", str(build_dir)], env=env, check=True)
    subprocess.run(["cmake", "--install", str(build_dir)], env={**env, "DESTDIR": str(stage)}, check=True)
    library = next((stage / "usr/lib").glob(pkg.pattern), None)
    if library is None:
        raise RuntimeError(f"{pkg.name} library not installed: {pkg.pattern}")
    require_glibc_elf(library)
    build(manifest(stage, pkg.name, lock["version"], pkg.deps), output / f"{pkg.name}-{lock['version']}.kiln")


def duktape_build(pkg: Package, lock: dict, cache: Path, work: Path, output: Path) -> None:
    unit = work / pkg.name
    unit.mkdir()
    upstream = source(lock, cache, unit / "source")
    sysroot = desktop.make_sysroot(output, unit, pkg.deps)
    stage = unit / "package/root"
    root = sysroot.resolve()
    subprocess.run(["make", "-f", "Makefile.sharedlibrary", f"CC=gcc --sysroot={root}", "-j1"],
                   cwd=upstream, check=True)
    lib, inc, pc = stage / "usr/lib", stage / "usr/include", stage / "usr/lib/pkgconfig"
    for directory in (lib, inc, pc):
        directory.mkdir(parents=True)
    built = next(upstream.glob("libduktape.so.*"))
    (lib / built.name).write_bytes(built.read_bytes())
    (lib / built.name).chmod(0o755)
    soname = ".".join(built.name.split(".")[:3])  # libduktape.so.207
    (lib / soname).symlink_to(built.name)
    (lib / "libduktape.so").symlink_to(soname)
    for header in ("duktape.h", "duk_config.h"):
        (inc / header).write_bytes(next(upstream.rglob(f"src/{header}")).read_bytes())
    (pc / "duktape.pc").write_text(
        f"prefix=/usr\nlibdir=${{prefix}}/lib\nincludedir=${{prefix}}/include\n\n"
        f"Name: duktape\nDescription: Embeddable JavaScript engine\nVersion: {lock['version']}\n"
        "Libs: -L${libdir} -lduktape\nCflags: -I${includedir}\n")
    require_glibc_elf(lib / built.name)
    build(manifest(stage, pkg.name, lock["version"], pkg.deps), output / f"{pkg.name}-{lock['version']}.kiln")


PACKAGES = [
    Package(
        "glib", ("glibc", "libffi", "pcre2", "zlib"), introspect=True,
        options=("-Dtests=false", "-Dinstalled_tests=false", "-Dselinux=disabled", "-Dxattr=false",
                 "-Dlibmount=disabled", "-Dman-pages=disabled", "-Ddocumentation=false",
                 "-Dgtk_doc=false", "-Dnls=disabled", "-Dsysprof=disabled", "-Ddtrace=false",
                 "-Dsystemtap=false", "-Dintrospection=enabled"),
        pattern="libglib-2.0.so.0.*",
    ),
    Package("libcap", ("glibc",), kind="custom"),
    Package(
        "libxkbcommon", ("glibc", "libxml2"),
        options=("-Denable-tools=false", "-Denable-x11=false", "-Denable-wayland=false",
                 "-Denable-xkbregistry=true", "-Denable-bash-completion=false", "-Denable-docs=false"),
        pattern="libxkbregistry.so.0.*",
    ),
    Package(
        "lcms2", ("glibc",),
        options=("-Djpeg=disabled", "-Dtiff=disabled", "-Dutils=false", "-Dfastfloat=true"),
        pattern="liblcms2.so.2.*",
    ),
    Package(
        "libei", ("glibc", "libevdev"),
        options=("-Dtests=disabled", "-Ddocumentation=[]", "-Dliboeffis=disabled"),
        pattern="libei.so.1.*",
    ),
    Package(
        "libseccomp", ("glibc",), kind="autotools",
        options=("--disable-static", "--enable-shared", "--disable-python"),
        pattern="libseccomp.so.2.*",
    ),
    Package(
        "sqlite", ("glibc", "zlib"), kind="autotools",
        options=("--disable-static", "--enable-shared", "--disable-readline", "--enable-fts5", "--soname=legacy"),
        pattern="libsqlite3.so.3.*",
    ),
    Package(
        "libusb", ("glibc", "eudev"), kind="autotools",
        options=("--disable-static", "--enable-shared", "--enable-udev", "--disable-examples-build",
                 "--disable-tests-build"),
        pattern="libusb-1.0.so.0.*",
    ),
    Package(
        "libepoxy", ("glibc", "mesa", "libdrm", "wayland", "expat", "libffi"),
        options=("-Degl=yes", "-Dglx=no", "-Dx11=false", "-Dtests=false"),
        pattern="libepoxy.so.0.*",
    ),
    Package(
        "gdk-pixbuf", ("glibc", "glib", "libffi", "pcre2", "zlib", "libpng"), introspect=True,
        options=("-Dpng=enabled", "-Dtiff=disabled", "-Djpeg=disabled", "-Dgif=disabled",
                 "-Dothers=disabled", "-Dgio_sniffing=false", "-Dman=false", "-Dgtk_doc=false",
                 "-Dintrospection=enabled", "-Dtests=false", "-Dinstalled_tests=false",
                 "-Dbuiltin_loaders=png", "-Drelocatable=false", "-Dglycin=disabled"),
        pattern="libgdk_pixbuf-2.0.so.0.*",
    ),
    Package(
        "at-spi2-core", ("glibc", "glib", "libffi", "pcre2", "zlib", "dbus", "expat", "libxml2"), introspect=True,
        options=("-Dx11=disabled", "-Dintrospection=enabled", "-Ddocs=false", "-Dsystemd_user_dir=/usr/lib/systemd/user",
                 "-Duse_systemd=false"),
        pattern="libatspi.so.0.*",
    ),
    Package(
        "gsettings-desktop-schemas", ("glibc", "glib", "libffi", "pcre2", "zlib"), kind="data",
        introspect=True, options=("-Dintrospection=true",),
        pattern="usr/share/pkgconfig/gsettings-desktop-schemas.pc",
    ),
    Package(
        "json-glib", ("glibc", "glib", "libffi", "pcre2", "zlib"), introspect=True,
        options=("-Dtests=false", "-Dinstalled_tests=false", "-Dintrospection=enabled",
                 "-Ddocumentation=disabled", "-Dman=false", "-Dnls=disabled"),
        pattern="libjson-glib-1.0.so.0.*",
    ),
    Package(
        "libgusb", ("glibc", "glib", "libffi", "pcre2", "zlib", "libusb", "json-glib"),
        introspect=True,
        options=("-Dtests=false", "-Dintrospection=true", "-Ddocs=false", "-Dvapi=false",
                 "-Dusb_ids=/usr/share/hwdata/usb.ids"),
        pattern="libgusb.so.2.*",
    ),
    Package(
        "libjpeg-turbo", ("glibc",), kind="cmake",
        options=("-DENABLE_STATIC=OFF", "-DENABLE_SHARED=ON", "-DWITH_TOOLS=OFF", "-DWITH_TESTS=OFF",
                 "-DWITH_JAVA=OFF", "-DWITH_SIMD=ON"),
        pattern="libjpeg.so.62.*",
    ),
    Package(
        "libtiff", ("glibc", "zlib", "libjpeg-turbo"), kind="autotools",
        options=("--disable-static", "--enable-shared", "--disable-docs", "--disable-tools",
                 "--disable-tests", "--disable-contrib", "--disable-lzma", "--disable-zstd",
                 "--disable-webp", "--disable-lerc", "--disable-jbig", "--disable-cxx"),
        pattern="libtiff.so.6.*",
    ),
    Package(
        "gtk", ("glibc", "glib", "libffi", "pcre2", "zlib", "pango", "harfbuzz", "fribidi", "cairo",
                "pixman", "freetype", "fontconfig", "libpng", "libtiff", "libjpeg-turbo", "gdk-pixbuf",
                "graphene", "libepoxy", "libxkbcommon", "wayland", "wayland-protocols", "mesa",
                "libdrm", "expat", "libxml2"),
        introspect=True,
        options=("-Dx11-backend=false", "-Dwayland-backend=true", "-Dbroadway-backend=false",
                 "-Dmedia-gstreamer=disabled", "-Dprint-cups=disabled", "-Dprint-cpdb=disabled",
                 "-Dvulkan=disabled", "-Dcloudproviders=disabled", "-Dsysprof=disabled",
                 "-Dtracker=disabled", "-Dcolord=disabled", "-Dintrospection=enabled",
                 "-Dbuild-demos=false", "-Dbuild-testsuite=false", "-Dbuild-examples=false",
                 "-Dbuild-tests=false", "-Ddocumentation=false", "-Dman-pages=false",
                 "-Dscreenshots=false"),
        pattern="libgtk-4.so.1.*",
    ),
    Package("duktape", ("glibc",), kind="custom"),
    Package(
        "polkit", ("glibc", "glib", "libffi", "pcre2", "zlib", "expat", "duktape", "elogind", "dbus",
                   "libcap", "util-linux", "eudev"),
        introspect=True,
        options=("-Dsession_tracking=elogind", "-Dauthfw=shadow", "-Dtests=false", "-Dman=false",
                 "-Dintrospection=true", "-Dgettext=false", "-Dexamples=false",
                 "-Dpolkitd_user=root", "--sysconfdir=/etc", "--localstatedir=/var"),
        pattern="libpolkit-agent-1.so.0.*",
    ),
    Package(
        "libpsl", ("glibc",),
        options=("-Druntime=no", "-Dbuiltin=false", "-Dtests=false", "-Ddocs=false"),
        pattern="libpsl.so.5.*",
    ),
    Package(
        "nghttp2", ("glibc",), kind="autotools",
        options=("--enable-lib-only", "--disable-static", "--enable-shared", "--disable-python-bindings",
                 "--without-libxml2", "--without-jansson", "--without-systemd"),
        pattern="libnghttp2.so.14.*",
    ),
    Package(
        "libical", ("glibc", "glibc", "glib", "libffi", "pcre2", "zlib", "libxml2", "sqlite"), kind="cmake",
        options=("-DICAL_GLIB=ON", "-DGOBJECT_INTROSPECTION=OFF", "-DICAL_BUILD_DOCS=OFF",
                 "-DICAL_GLIB_VAPI=OFF", "-DWITH_CXX_BINDINGS=OFF", "-DSHARED_ONLY=ON",
                 "-DUSE_BUILTIN_TZDATA=ON", "-DLIBICAL_BUILD_TESTING=OFF",
                 "-DLIBICAL_BUILD_EXAMPLES=OFF"),
        pattern="libical.so.3.*",
    ),
    Package(
        "libsecret", ("glibc", "glibc", "glib", "libffi", "pcre2", "zlib"), introspect=True,
        options=("-Dcrypto=disabled", "-Dmanpage=false", "-Dgtk_doc=false", "-Dvapi=false",
                 "-Dintrospection=true", "-Dbash_completion=disabled", "-Dtpm2=false"),
        pattern="libsecret-1.so.0.*",
    ),
    Package(
        "libsoup", ("glibc", "glibc", "glib", "libffi", "pcre2", "zlib", "libpsl", "nghttp2", "sqlite", "libxml2"), introspect=True,
        options=("-Dgssapi=disabled", "-Dntlm=disabled", "-Dbrotli=disabled", "-Dsysprof=disabled",
                 "-Dtls_check=false", "-Dvapi=disabled", "-Ddocs=disabled", "-Dtests=false",
                 "-Dintrospection=enabled", "-Dpkcs11_tests=disabled", "-Dautobahn=disabled"),
        pattern="libsoup-3.0.so.0.*",
    ),
    Package(
        "evolution-data-server",
        ("glibc", "glib", "libffi", "pcre2", "zlib", "libsecret", "libsoup", "libical", "libxml2", "sqlite",
         "json-glib", "util-linux", "libpsl", "nghttp2", "nss", "nspr", "icu"),
        kind="cmake",
        options=("-DWITH_LIBDB=OFF", "-DENABLE_GOA=OFF", "-DENABLE_GTK=OFF",
                 "-DENABLE_GTK4=OFF", "-DENABLE_WEATHER=OFF", "-DENABLE_CANBERRA=OFF",
                 "-DENABLE_EXAMPLES=OFF", "-DENABLE_TESTS=OFF", "-DENABLE_INSTALLED_TESTS=OFF",
                 "-DENABLE_INTROSPECTION=OFF", "-DENABLE_VALA_BINDINGS=OFF", "-DENABLE_GTK_DOC=OFF",
                 "-DWITH_SYSTEMDUSERUNITDIR=no", "-DENABLE_OAUTH2_WEBKITGTK=OFF",
                 "-DENABLE_OAUTH2_WEBKITGTK4=OFF", "-DWITH_OPENLDAP=OFF", "-DWITH_KRB5=OFF",
                 "-DENABLE_IPV6=OFF", "-DENABLE_LARGEFILE=OFF", "-DWITH_PHONENUMBER=OFF"),
        pattern="libedataserver-1.2.so.27.*",
    ),
    Package("icu", ("glibc",), kind="custom"),
    Package("nspr", ("glibc",), kind="custom"),
    Package("nss", ("glibc", "nspr", "sqlite", "zlib"), kind="custom"),
    Package(
        "libgpg-error", ("glibc",), kind="autotools",
        options=("--disable-static", "--enable-shared", "--disable-doc", "--disable-tests",
                 "--disable-languages", "--disable-nls"),
        pattern="libgpg-error.so.0.*",
    ),
    Package(
        "libgcrypt", ("glibc", "libgpg-error"), kind="autotools",
        options=("--disable-static", "--enable-shared", "--disable-doc", "--disable-asm"),
        pattern="libgcrypt.so.20.*",
    ),
    Package(
        "libtasn1", ("glibc",), kind="autotools",
        options=("--disable-static", "--enable-shared", "--disable-doc"),
        pattern="libtasn1.so.6.*",
    ),
    Package(
        "p11-kit", ("glibc", "libffi", "libtasn1"),
        options=("-Dtest=false", "-Dnls=false", "-Dman=false", "-Dgtk_doc=false", "-Dsystemd=disabled",
                 "-Dbash_completion=disabled", "-Dtrust_module=enabled", "-Dtrust_paths=/etc/ssl/certs/ca-certificates.crt",
                 "--sysconfdir=/etc"),
        pattern="libp11-kit.so.0.*",
    ),
    Package(
        "gcr", ("glibc", "glib", "libffi", "pcre2", "zlib", "p11-kit", "libgcrypt", "libgpg-error", "libsecret", "gtk", "pango", "harfbuzz", "fribidi", "cairo", "pixman", "freetype", "fontconfig", "libpng", "libtiff", "libjpeg-turbo", "gdk-pixbuf", "graphene", "libepoxy", "libxkbcommon", "wayland", "wayland-protocols", "mesa", "libdrm", "expat", "libxml2"),
        introspect=True,
        options=("-Dcrypto=libgcrypt", "-Dssh_agent=false", "-Dgtk4=true", "-Dintrospection=true",
                 "-Dvapi=false", "-Dgtk_doc=false", "-Dsystemd=disabled", "-Dgpg_path=/usr/bin/gpg"),
        pattern="libgcr-4.so.4.*",
    ),
    Package(
        "glycin", ("glibc", "glib", "libffi", "pcre2", "zlib", "libseccomp", "lcms2", "fontconfig", "freetype",
                   "expat", "libpng"),
        introspect=True, rust=True,
        options=("-Dglycin-loaders=true", "-Dloaders=glycin-image-rs", "-Dlibglycin=true",
                 "-Dlibglycin-gtk4=false", "-Dintrospection=true", "-Dvapi=false", "-Dtests=false",
                 "-Dglycin-thumbnailer=false", "-Dcapi_docs=false", "-Dlto=false"),
        pattern="libglycin-2.so.0",
    ),
    Package(
        "gjs", ("glibc", "glib", "libffi", "pcre2", "zlib", "cairo", "pixman", "freetype", "fontconfig",
                "libpng", "expat", "mozjs", "gcc-runtime"),
        introspect=True,
        options=("-Dreadline=disabled", "-Dprofiler=disabled", "-Dinstalled_tests=false",
                 "-Dskip_dbus_tests=true", "-Dskip_gtk_tests=true"),
        pattern="libgjs.so.0.*",
    ),
    Package(
        "mutter", ("glibc", "glib", "libffi", "pcre2", "zlib", "cairo", "pixman", "freetype", "fontconfig", "libpng", "expat",
                "pango", "harfbuzz", "fribidi", "graphene", "gsettings-desktop-schemas", "libxkbcommon", "libxml2",
                "at-spi2-core", "dbus", "colord", "lcms2", "sqlite", "libgusb", "libusb", "json-glib", "libei",
                "libdisplay-info", "libdrm", "gnome-desktop", "gdk-pixbuf", "gtk", "libgudev", "eudev", "wayland",
                "wayland-protocols", "libinput", "libevdev", "mtdev", "mesa", "glycin", "libseccomp", "elogind",
                "libcap", "util-linux", "libepoxy", "libtiff", "libjpeg-turbo", "iso-codes", "xkeyboard-config"),
        introspect=True,
        options=("-Dxwayland=false", "-Dlogind=true", "-Dnative_backend=true", "-Dremote_desktop=false",
                 "-Dlibwacom=false", "-Dsound_player=false", "-Dstartup_notification=false", "-Dprofiler=false",
                 "-Dintrospection=true", "-Ddocs=false", "-Dtests=disabled", "-Dcogl_tests=false",
                 "-Dclutter_tests=false", "-Dmutter_tests=false", "-Dinstalled_tests=false",
                 "-Dbash_completion=false", "-Ddevkit=disabled", "-Degl_device=false", "-Dwayland_eglstream=false",
                 "-Dverbose=false", "-Dudev=true", "-Dgles2=true", "-Degl=true", "-Dopengl=false"),
        pattern="libmutter-18.so.0.*",
    ),
    Package("iso-codes", ("glibc",), kind="data", pattern="usr/share/pkgconfig/iso-codes.pc"),
    Package(
        "gnome-desktop", ("glibc", "glib", "libffi", "pcre2", "zlib", "gdk-pixbuf", "gtk", "gsettings-desktop-schemas",
                          "fontconfig", "freetype", "libxkbcommon", "libxml2", "xkeyboard-config", "iso-codes",
                          "eudev", "libseccomp", "pango", "cairo", "harfbuzz", "fribidi", "graphene",
                          "libepoxy", "libpng", "libtiff", "libjpeg-turbo", "pixman", "wayland", "mesa",
                          "libdrm", "expat"),
        introspect=True,
        options=("-Dbuild_gtk4=true", "-Dlegacy_library=false", "-Dudev=enabled", "-Dsystemd=disabled",
                 "-Dintrospection=true", "-Ddesktop_docs=false", "-Ddebug_tools=false", "-Dgtk_doc=false",
                 "-Dinstalled_tests=false"),
        pattern="libgnome-desktop-4.so.2.*",
    ),
    Package(
        "libgudev", ("glibc", "glib", "libffi", "pcre2", "zlib", "eudev"), introspect=True,
        options=("-Dtests=disabled", "-Dintrospection=enabled", "-Dvapi=disabled", "-Dgtk_doc=false"),
        pattern="libgudev-1.0.so.0.*",
    ),
    Package(
        "colord", ("glibc", "glib", "lcms2", "pcre2", "libffi", "zlib", "sqlite", "libgusb", "libgudev"), introspect=True,
        options=("-Ddaemon=false", "-Dsession_example=false", "-Dsystemd=false",
                 "-Dlibcolordcompat=false", "-Dargyllcms_sensor=false", "-Dbash_completion=false",
                 "-Dman=false", "-Ddocs=false", "-Dtests=false", "-Dudev_rules=false",
                 "-Dvapi=false", "-Dintrospection=true", "-Dinstalled_tests=false", "-Dsane=false",
                 "-Ddaemon_user=root"),
        pattern="libcolord.so.2.*",
    ),
    Package(
        "harfbuzz", ("glibc", "freetype", "glib"), introspect=True,
        options=("-Dglib=enabled", "-Dgobject=enabled", "-Dfreetype=enabled", "-Dcairo=disabled",
                 "-Dchafa=disabled", "-Dpng=disabled", "-Dzlib=disabled", "-Dicu=disabled",
                 "-Dgraphite2=disabled", "-Dfontations=disabled", "-Dharfrust=disabled",
                 "-Dwasm=disabled", "-Draster=disabled", "-Dvector=disabled", "-Dgpu=disabled",
                 "-Dsubset=enabled", "-Dtests=disabled", "-Ddocs=disabled",
                 "-Dintrospection=enabled", "-Dutilities=disabled"),
        pattern="libharfbuzz.so.0.*",
    ),
    Package(
        "cairo", ("glibc", "pixman", "zlib", "libpng", "freetype", "fontconfig", "glib"),
        options=("-Dfontconfig=enabled", "-Dfreetype=enabled", "-Dpng=enabled", "-Dzlib=enabled",
                 "-Ddwrite=disabled", "-Dquartz=disabled", "-Dtee=disabled", "-Dxcb=disabled",
                 "-Dxlib=disabled", "-Dxlib-xcb=disabled", "-Dtests=disabled", "-Dlzo=disabled",
                 "-Dglib=enabled", "-Dspectre=disabled", "-Dsymbol-lookup=disabled",
                 "-Dgtk_doc=false"),
        pattern="libcairo.so.2.*",
    ),
    Package(
        "pango", ("glibc", "glib", "harfbuzz", "fribidi", "cairo", "fontconfig", "freetype"),
        introspect=True,
        options=("-Ddocumentation=false", "-Dman-pages=false", "-Dintrospection=enabled",
                 "-Dbuild-testsuite=false", "-Dbuild-examples=false", "-Dfontconfig=enabled",
                 "-Dsysprof=disabled", "-Dlibthai=disabled", "-Dcairo=enabled", "-Dxft=disabled",
                 "-Dfreetype=enabled"),
        pattern="libpango-1.0.so.0.*",
    ),
    Package(
        "graphene", ("glibc", "glib", "libffi", "pcre2", "zlib"), introspect=True,
        options=("-Dtests=false", "-Dinstalled_tests=false", "-Dgtk_doc=false",
                 "-Dintrospection=enabled", "-Darm_neon=false", "-Dsse2=true"),
        pattern="libgraphene-1.0.so.0.*",
    ),
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


HOST_GI = Path("build/host-tools/gobject-introspection")


def build_host_gi(lock: dict, cache: Path, work: Path) -> Path:
    """Native g-ir-scanner and g-ir-compiler, used to generate typelibs for target libraries.

    The scanner runs on the host and runs the target's dumper programs through the loader
    wrapper in the cross file, which is the usual way to introspect in a cross build.
    """
    prefix = HOST_GI.resolve()
    if (prefix / "bin/g-ir-scanner").is_file():
        return prefix
    upstream = source(lock, cache, work / "host-gi-source")
    build_dir = work / "host-gi-build"
    subprocess.run([*desktop.MESON, "setup", str(build_dir), str(upstream), f"--prefix={prefix}",
                    "--libdir=lib", "-Ddoctool=disabled", "-Dgtk_doc=false", "-Dtests=false",
                    "-Dcairo=disabled"], check=True)
    subprocess.run([*desktop.MESON, "install", "-C", str(build_dir)], check=True)
    return prefix


HOST_WAYLAND = Path("build/host-tools/wayland")


def build_host_wayland(lock: dict, cache: Path, work: Path) -> None:
    """A native wayland-scanner at the pinned version, for packages that generate protocol code."""
    prefix = HOST_WAYLAND.resolve()
    if not (prefix / "bin/wayland-scanner").is_file():
        upstream = source(lock, cache, work / "host-wayland-source")
        build_dir = work / "host-wayland-build"
        subprocess.run([*desktop.MESON, "setup", str(build_dir), str(upstream), f"--prefix={prefix}",
                        "--libdir=lib", "-Dlibraries=false", "-Dscanner=true", "-Dtests=false",
                        "-Ddocumentation=false", "-Ddtd_validation=false"], check=True)
        subprocess.run([*desktop.MESON, "install", "-C", str(build_dir)], check=True)
    os.environ["PATH"] = f"{prefix}/bin{os.pathsep}{os.environ['PATH']}"
    os.environ["PKG_CONFIG_PATH_FOR_BUILD"] = f"{prefix}/lib/pkgconfig:{prefix}/share/pkgconfig"


RUST_TARGET = "x86_64-unknown-linux-gnu"


def rust_cross_file(original):
    """Wrap a cross file for projects that build with cargo.

    Host and target are the same triple, so --target is what makes cargo treat the build as a cross
    build: build scripts and proc-macros use the host linker, and target code links with the sysroot
    through a wrapper (one executable, because cargo takes a single linker path).
    """
    def make(work: Path, sysroot: Path) -> Path:
        path = original(work, sysroot)
        root = sysroot.resolve()
        cc = work / "target-cc"
        # Rust links libgcc_s dynamically for unwinding, but the target has no GCC runtime library.
        # A linker script named libgcc_s.so, searched first, resolves -lgcc_s to the static pieces.
        static_dir = work / "gcc-static"
        static_dir.mkdir(exist_ok=True)
        pieces = [subprocess.run(["gcc", f"-print-file-name={name}"], capture_output=True, text=True,
                                 check=True).stdout.strip() for name in ("libgcc.a", "libgcc_eh.a")]
        (static_dir / "libgcc_s.so").write_text(f"GROUP ( {pieces[0]} {pieces[1]} )\n")
        cc.write_text(
            f'#!/bin/sh\nexec gcc --sysroot={root} -L{static_dir} -Wl,-rpath-link,{root}/usr/lib '
            f'-Wl,-rpath-link,{root}/lib64 "$@"\n')
        cc.chmod(0o755)
        text = path.read_text(encoding="utf-8")
        text = text.replace("[binaries]\n", f"[binaries]\nrust = ['rustc', '--target', '{RUST_TARGET}', "
                            f"'-C', 'linker={cc}']\n", 1)
        text = text.replace("[properties]\n", f"[properties]\nrust_target = '{RUST_TARGET}'\n", 1)
        path.write_text(text, encoding="utf-8")
        triple = RUST_TARGET.upper().replace("-", "_")
        os.environ[f"CARGO_TARGET_{triple}_LINKER"] = str(cc)
        os.environ[f"CC_{RUST_TARGET.replace('-', '_')}"] = str(cc)
        os.environ["CARGO_BUILD_JOBS"] = os.environ.get("MELDRA_JOBS", "2")
        os.environ["PKG_CONFIG_ALLOW_CROSS"] = "1"
        os.environ["PKG_CONFIG_SYSROOT_DIR"] = str(root)
        os.environ["PKG_CONFIG_LIBDIR"] = f"{root}/usr/lib/pkgconfig:{root}/usr/share/pkgconfig"
        return path
    return make


HOST_LIBXCVT = Path("build/host-tools/libxcvt")


def build_host_libxcvt(lock: dict, cache: Path, work: Path) -> None:
    """Mutter runs `cvt` at build time to generate its default display-mode table."""
    prefix = HOST_LIBXCVT.resolve()
    if not (prefix / "bin/cvt").is_file():
        upstream = source(lock, cache, work / "host-libxcvt-source")
        build_dir = work / "host-libxcvt-build"
        subprocess.run([*desktop.MESON, "setup", str(build_dir), str(upstream), f"--prefix={prefix}",
                        "--libdir=lib"], check=True)
        subprocess.run([*desktop.MESON, "install", "-C", str(build_dir)], check=True)
    os.environ["PATH"] = f"{prefix}/bin{os.pathsep}{os.environ['PATH']}"
    # The private-prefix cvt has no rpath for its own libxcvt.
    os.environ["LD_LIBRARY_PATH"] = f"{prefix}/lib{os.pathsep}{os.environ.get('LD_LIBRARY_PATH', '')}".rstrip(os.pathsep)


def meson_build(pkg: Package, lock: dict, cache: Path, work: Path, output: Path) -> None:
    original = desktop.cross_file
    if pkg.rust:
        desktop.cross_file = rust_cross_file(desktop.cross_file)
    if pkg.introspect:
        desktop.cross_file = introspection_cross_file(desktop.cross_file)
    try:
        desktop.build_meson(lock, pkg.name, cache, work, output, pkg.deps, pkg.options, pkg.pattern)
    finally:
        desktop.cross_file = original


def _unused(pkg, lock, cache, work, output):
    desktop.build_meson(lock, pkg.name, cache, work, output, pkg.deps, pkg.options, pkg.pattern)




def gentle_environment(work: Path, jobs: int) -> None:
    """Cap parallelism for every build tool, since the build machine has no CPU fan.

    ninja and make are found through PATH, so small wrappers force the job count and ignore the
    -jN that individual build scripts hard-code.
    """
    bin_dir = work / "gentle-bin"
    bin_dir.mkdir()
    ninja = next(p for p in os.environ["PATH"].split(os.pathsep) if (Path(p) / "ninja").is_file())
    make = next(p for p in os.environ["PATH"].split(os.pathsep) if (Path(p) / "make").is_file())
    # Drop any -jN / -j N the caller passes (cmake --build, scripts that hard-code -j8): the cap wins.
    strip = (
        "#!/bin/sh\nargs=\nskip=0\nfor a in \"$@\"; do\n"
        "  if [ \"$skip\" = 1 ]; then skip=0; case \"$a\" in [0-9]*) continue ;; esac; fi\n"
        "  case \"$a\" in -j) skip=1 ;; -j*) ;; *) args=\"$args '$a'\" ;; esac\ndone\n")
    (bin_dir / "ninja").write_text(strip + f'eval exec {ninja}/ninja -j{jobs} "$args"\n')
    (bin_dir / "make").write_text(strip + f'eval exec {make}/make -j{jobs} "$args"\n')
    for name in ("ninja", "make"):
        (bin_dir / name).chmod(0o755)
    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
    os.environ["MAKEFLAGS"] = f"-j{jobs}"
    os.environ["CMAKE_BUILD_PARALLEL_LEVEL"] = str(jobs)
    os.environ["CCACHE_NODIRECT"] = "1"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("names", nargs="*")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--keep", action="store_true",
                        help="keep the temporary build directory (build/work) for debugging a failure")
    parser.add_argument("--jobs", type=int, default=int(os.environ.get("MELDRA_JOBS", "2")),
                        help="parallel compile jobs (default 2; this machine overheats)")
    parser.add_argument("--lock", type=Path, default=Path("sources.lock.toml"))
    parser.add_argument("--cache", type=Path, default=Path("build/downloads"))
    parser.add_argument("--output", type=Path, default=Path("build/packages"))
    args = parser.parse_args()
    lock_data = tomllib.loads(args.lock.read_text(encoding="utf-8"))
    locks = lock_data["gnome"]
    for pkg in PACKAGES:
        desktop.VERSIONS.setdefault(pkg.name, pin(lock_data, pkg.name)["version"])
    # Packages built elsewhere (mozjs and gcc-runtime come from CI): use the newest of each as an input.
    import tarfile
    newest = {}
    for archive in args.output.glob("*.kiln"):
        with tarfile.open(archive) as bundle:
            meta = tomllib.loads(bundle.extractfile("KILN.toml").read().decode())["package"]
        key = [int(x) if x.isdigit() else 0 for x in re.split(r"[.]", meta["version"])]
        if meta["name"] not in newest or key > newest[meta["name"]][0]:
            newest[meta["name"]] = (key, meta["version"])
    for name in ("mozjs", "gcc-runtime"):
        if name in newest:
            desktop.VERSIONS[name] = newest[name][1]
    if args.list:
        for pkg in PACKAGES:
            done = (args.output / f"{pkg.name}-{desktop.VERSIONS[pkg.name]}.kiln").is_file()
            print(f"{'built  ' if done else 'pending'} {pkg.name} {desktop.VERSIONS[pkg.name]}")
        return 0
    wanted = [p for p in PACKAGES if p.name in args.names or args.all]
    args.names = [n for n in args.names]
    unknown = set(args.names) - {p.name for p in PACKAGES} - {"host-gi"}
    if unknown:
        parser.error(f"unknown package(s): {', '.join(sorted(unknown))}")
    args.output.mkdir(parents=True, exist_ok=True)
    try:
        import contextlib
        keep_dir = Path("build/work").resolve()
        if args.keep:
            shutil.rmtree(keep_dir, ignore_errors=True)
            keep_dir.mkdir(parents=True)
        with (contextlib.nullcontext(str(keep_dir)) if args.keep
              else tempfile.TemporaryDirectory(prefix="meldra-gnome-")) as directory:
            work = Path(directory)
            gentle_environment(work, max(1, args.jobs))
            desktop.MESON = pinned_meson(lock_data["host_tools"]["meson"], args.cache, work)
            build_host_wayland(lock_data["desktop"]["wayland"], args.cache, work)
            build_host_libxcvt(locks["libxcvt"], args.cache, work)
            # Build-time-only Python modules (templating), from pinned wheels.
            wheel_paths = []
            for tool in ("jinja2", "markupsafe", "docutils"):
                tool_work = work / f"host-{tool}"
                tool_work.mkdir()
                wheel_paths.append(str(python_wheel(lock_data["host_tools"][tool], args.cache, tool_work)))
            os.environ["PYTHONPATH"] = os.pathsep.join(wheel_paths + [os.environ.get("PYTHONPATH", "")]).rstrip(os.pathsep)
            # rst2man (docutils) renders man pages for projects that build them unconditionally.
            tools_bin = work / "tools-bin"
            tools_bin.mkdir()
            launcher = tools_bin / "rst2man"
            launcher.write_text(f"#!{sys.executable}\nimport sys\nsys.path[:0] = {wheel_paths!r}\n"
                                "from docutils.core import publish_cmdline\npublish_cmdline(writer_name='manpage')\n")
            launcher.chmod(0o755)
            os.environ["PATH"] = f"{tools_bin}{os.pathsep}{os.environ['PATH']}"
            if "host-gi" in args.names:
                build_host_gi(locks["gobject_introspection"], args.cache, work)
            if any(p.introspect for p in wanted):
                build_host_gi(locks["gobject_introspection"], args.cache, work)
            for pkg in wanted:
                archive = args.output / f"{pkg.name}-{desktop.VERSIONS[pkg.name]}.kiln"
                if args.all and archive.is_file():
                    print(f"using existing package {archive}")
                    continue
                print(f"==> {pkg.name} {desktop.VERSIONS[pkg.name]}", flush=True)
                lock = pin(lock_data, pkg.name)
                if pkg.name == "icu":
                    icu_build(pkg, lock, args.cache, work, args.output)
                elif pkg.name == "nss":
                    nss_build(pkg, lock, args.cache, work, args.output)
                elif pkg.name == "nspr":
                    nspr_build(pkg, lock, args.cache, work, args.output)
                elif pkg.name == "duktape":
                    duktape_build(pkg, lock, args.cache, work, args.output)
                elif pkg.kind == "autotools-data":
                    autotools_data_build(pkg, lock, args.cache, work, args.output)
                elif pkg.kind == "data":
                    meson_data_build(pkg, lock, args.cache, work, args.output)
                elif pkg.kind == "cmake":
                    cmake_build(pkg, lock, args.cache, work, args.output)
                elif pkg.kind == "autotools":
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
    # Lowest CPU and disk priority, so the machine stays responsive and cool.
    try:
        os.nice(19)
        subprocess.run(["ionice", "-c3", "-p", str(os.getpid())], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass
    raise SystemExit(main())
