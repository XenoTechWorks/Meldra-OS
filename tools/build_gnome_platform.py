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
import sysconfig
import tempfile
import tomllib

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kiln.cli import build
from tools.build_graphics_packages import manifest, python_wheel, require_glibc_elf, source
import tools.build_desktop_libraries as desktop


OWNED = {"meldra-session": "0.1.0", "meldra-desktop-apps": "0.1.0", "meldra-installer": "0.1.0", "meldra-greeter": "0.1.0",
         "gi-typelibs": "1.86.0"}   # built from this repository, so there is no upstream pin


def pin(lock_data: dict, name: str) -> dict:
    """A package's pin: the gnome section first, then the older desktop section."""
    if name in OWNED:
        return {"version": OWNED[name]}
    name = {"libltdl": "libtool"}.get(name, name)   # libltdl is built from the libtool tarball
    key = name.replace("-", "_")
    return lock_data["gnome"][key] if key in lock_data["gnome"] else lock_data["desktop"][key]


class Package:
    def __init__(self, name, deps, kind="meson", options=(), pattern=None, post=None, env=None,
                 introspect=False, rust=False, target_tools=None, host_python=False, patches=(),
                 host_programs=()):
        self.host_programs = tuple(host_programs)  # build-machine programs the sandbox lacks, run on the host
        self.patches = tuple(patches)            # reviewed, hash-checked edits (see apply_recorded_patch)
        self.host_python = host_python           # build scripts import gi (PyGObject): run python3 on the host
        self.target_tools = target_tools or {}   # build-time programs run from the target sysroot
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
        # usr/lib/pulseaudio holds libpulsecommon, which libpulse reaches through an absolute rpath that
        # only means something on the installed system.
        library_path = f"{root}/lib64:{root}/usr/lib64:{root}/usr/lib:{root}/usr/lib/pulseaudio"
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
        # The build machine's MIME database stays visible through a directory that holds nothing else: tools
        # such as glib-compile-resources identify images by MIME type (GTK 3's theme PNGs failed without it).
        mime_only = work / "host-mime"
        mime_only.mkdir(exist_ok=True)
        if not (mime_only / "mime").exists() and Path("/usr/share/mime").is_dir():
            (mime_only / "mime").symlink_to("/usr/share/mime")
        os.environ["XDG_DATA_DIRS"] = f"{root}/usr/share:{mime_only}"
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
        for relative, before, after, old_text, new_text, why in pkg.patches:
            apply_recorded_patch(upstream / relative, before, after, old_text, new_text, why)
        sysroot = desktop.make_sysroot(output, unit, pkg.deps)
        cross = desktop.cross_file(unit, sysroot)
        build_dir, stage = unit / "build", unit / "package/root"
        # Run setup from inside the build directory: meson writes the #include of a header outside the source
        # tree (gnome.mkenums_simple on the sysroot's gdesktop-enums.h) relative to its working directory.
        build_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run([*desktop.MESON, "setup", "--wrap-mode=nodownload", str(build_dir), str(upstream), "--prefix=/usr",
                        "--cross-file", str(cross), *pkg.options], check=True, cwd=build_dir)
        subprocess.run([*desktop.MESON, "compile", "-C", str(build_dir)], check=True)
        subprocess.run([*desktop.MESON, "install", "-C", str(build_dir), "--destdir", str(stage)], check=True)
        if pkg.post:
            pkg.post(stage, upstream, build_dir)
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


def cross_autotools_build(pkg: Package, lock: dict, cache: Path, work: Path, output: Path) -> None:
    """Autotools program package, cross-compiled against the Meldra sysroot (never the build machine's glibc).

    `pattern` is a path under the stage root that must exist afterwards; `env` adds to the configure/make
    environment; `post(stage, upstream, build_dir)` runs after `make install`.
    """
    unit = work / pkg.name
    unit.mkdir()
    upstream = source(lock, cache, unit / "source")
    sysroot = desktop.make_sysroot(output, unit, pkg.deps)
    root = sysroot.resolve()
    build_dir, stage = unit / "build", unit / "package/root"
    build_dir.mkdir()
    if not (upstream / "configure").exists():   # git tag archives ship configure.ac only
        subprocess.run(["autoreconf", "-fi"], cwd=upstream, check=True)
    env = {**os.environ, "CC": f"gcc --sysroot={root}", "CFLAGS": "-O2 -pipe",
           "LDFLAGS": f"-Wl,-rpath-link,{root}/usr/lib -Wl,-rpath-link,{root}/lib64",
           "PKG_CONFIG_SYSROOT_DIR": str(root),
           "PKG_CONFIG_LIBDIR": f"{root}/usr/lib/pkgconfig:{root}/usr/share/pkgconfig",
           # msgfmt finds ITS rules (polkit's, AppStream's) in GETTEXTDATADIRS; they come with the sysroot's packages.
           "GETTEXTDATADIRS": f"{root}/usr/share/gettext", **pkg.env}
    subprocess.run([str(upstream / "configure"), "--build=x86_64-pc-linux-gnu", "--host=x86_64-unknown-linux-gnu",
                    "--prefix=/usr", *pkg.options], cwd=build_dir, env=env, check=True)
    subprocess.run(["make", f"-j{os.environ.get('MELDRA_JOBS', '2')}"], cwd=build_dir, env=env, check=True)
    subprocess.run(["make", "install", f"DESTDIR={stage}"], cwd=build_dir, env=env, check=True)
    if pkg.post:
        pkg.post(stage, upstream, build_dir)
    if not (stage / pkg.pattern).exists():
        raise RuntimeError(f"{pkg.name}: expected file not installed: {pkg.pattern}")
    # Every dynamically linked program must link the sysroot's glibc, not the build machine's.
    for directory in ("bin", "sbin", "usr/bin", "usr/sbin"):
        for path in sorted((stage / directory).glob("*")):
            if path.is_file() and not path.is_symlink() and path.read_bytes()[:4] == b"\x7fELF":
                require_glibc_elf(path)
    build(manifest(stage, pkg.name, lock["version"], pkg.deps), output / f"{pkg.name}-{lock['version']}.kiln")


def ncurses_post(stage: Path, upstream: Path, build_dir: Path) -> None:
    """Compile the terminfo database with a native tic built from the same source (the target's tic needs
    glibc 2.44 and cannot run on the build machine), and add the usual compatibility names."""
    native = build_dir.parent / "native-tic"
    native.mkdir()
    subprocess.run([str(upstream / "configure"), "--without-shared", "--without-debug", "--without-ada",
                    "--without-cxx-binding", "--without-manpages", "--without-tests", "--enable-widec",
                    "--disable-db-install"], cwd=native, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["make", "-C", "include"], cwd=native, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["make", "-C", "progs", "tic"], cwd=native, check=True, stdout=subprocess.DEVNULL)
    terminfo = stage / "usr/share/terminfo"
    terminfo.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(native / "progs/tic"), "-x", "-o", str(terminfo), str(upstream / "misc/terminfo.src")],
                   check=True, env={**os.environ, "TERMINFO": str(terminfo)})
    lib = stage / "usr/lib"
    # Programs built elsewhere ask for the narrow names; the wide-character library serves them all.
    for alias, target in (("libncurses.so", "libncursesw.so"), ("libncurses.so.6", "libncursesw.so.6"),
                          ("libtinfo.so.6", "libncursesw.so.6"), ("libtinfo.so", "libncursesw.so"),
                          ("libcurses.so", "libncursesw.so")):
        if not (lib / alias).exists():
            (lib / alias).symlink_to(target)
    pc = lib / "pkgconfig"
    for alias in ("ncurses.pc", "tinfo.pc"):
        if (pc / "ncursesw.pc").exists() and not (pc / alias).exists():
            (pc / alias).symlink_to("ncursesw.pc")


def libgtop_post(stage: Path, upstream: Path, build_dir: Path) -> None:
    """libgtop installs libgtop_server2 setuid; on Linux the library reads /proc itself and the server is only
    for other systems, so it ships without the setuid bit (Kiln's allowlist would reject it anyway)."""
    del upstream, build_dir
    for helper in stage.rglob("libgtop_server*"):
        helper.chmod(0o755)


def nautilus_post(stage: Path, upstream: Path, build_dir: Path) -> None:
    """Nautilus reads two localsearch preferences even when the optional file indexer is not installed."""
    del upstream, build_dir
    schemas = stage / "usr/share/glib-2.0/schemas"
    schemas.mkdir(parents=True, exist_ok=True)
    (schemas / "org.meldra.Tracker3.Miner.Files.gschema.xml").write_text(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<schemalist>\n"
        "  <schema id=\"org.freedesktop.Tracker3.Miner.Files\" "
        "path=\"/org/freedesktop/tracker/miner/files/\">\n"
        "    <key name=\"index-recursive-directories\" type=\"as\"><default>[ '$HOME' ]</default></key>\n"
        "    <key name=\"index-single-directories\" type=\"as\"><default>[]</default></key>\n"
        "  </schema>\n"
        "</schemalist>\n"
    )


def cracklib_post(stage: Path, upstream: Path, build_dir: Path) -> None:
    """Compile cracklib's small word list into /usr/share/cracklib/pw_dict with the target's own packer."""
    del build_dir
    words_file = upstream / "dicts/cracklib-small"
    words = sorted({w.strip().lower() for w in words_file.read_text(errors="ignore").splitlines() if w.strip()})
    loader = Path("build/sysroots/glibc/lib64/ld-linux-x86-64.so.2").resolve()
    libs = f"{loader.parent}:{stage}/usr/lib"
    target = stage / "usr/share/cracklib"
    target.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(loader), "--library-path", libs, str(stage / "usr/sbin/cracklib-packer"),
                    str(target / "pw_dict")], input="\n".join(words) + "\n", text=True, check=True,
                   stdout=subprocess.DEVNULL)


def bash_post(stage: Path, upstream: Path, build_dir: Path) -> None:
    del upstream, build_dir
    (stage / "bin").mkdir(exist_ok=True)
    # /bin/bash is where scripts and /etc/passwd look; the binary lives in /usr/bin.
    if not (stage / "bin/bash").exists():
        (stage / "bin/bash").symlink_to("../usr/bin/bash")


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


def ltdl_build(pkg: Package, lock: dict, cache: Path, work: Path, output: Path) -> None:
    """libltdl from the libtool tarball: its own configure lives in the libltdl/ subdirectory."""
    unit = work / pkg.name
    unit.mkdir()
    upstream = source(lock, cache, unit / "source")
    sysroot = desktop.make_sysroot(output, unit, pkg.deps)
    root = sysroot.resolve()
    build_dir, stage = unit / "build", unit / "package/root"
    build_dir.mkdir()
    env = os.environ.copy()
    env.update({"CC": f"gcc --sysroot={root}", "CFLAGS": "-O2 -pipe"})
    subprocess.run([str(upstream / "libltdl/configure"), "--build=x86_64-pc-linux-gnu",
                    "--host=x86_64-unknown-linux-gnu", "--prefix=/usr", "--libdir=/usr/lib", "--disable-static",
                    "--enable-shared", "--enable-ltdl-install"], cwd=build_dir, env=env, check=True)
    subprocess.run(["make"], cwd=build_dir, env=env, check=True)
    subprocess.run(["make", "install", f"DESTDIR={stage}"], cwd=build_dir, env=env, check=True)
    library = next((stage / "usr/lib").glob(pkg.pattern), None)
    if library is None:
        raise RuntimeError(f"{pkg.name} library not installed: {pkg.pattern}")
    require_glibc_elf(library)
    build(manifest(stage, pkg.name, lock["version"], pkg.deps), output / f"{pkg.name}-{lock['version']}.kiln")


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


def strip_sysroot_from_rpaths(stage: Path, root: Path) -> int:
    """Remove the build sysroot prefix from RPATH/RUNPATH strings, in place.

    Meson resolves private library directories through pkg-config with the sysroot prefix attached, so
    an installed ELF can end up with /build/.../sysroot/usr/lib/foo in its rpath. The correct installed
    value is /usr/lib/foo. The edit only ever shortens the string inside .dynstr (padded with NULs), so
    no offsets move. The result is checked with readelf.
    """
    prefix = str(root).encode()
    fixed = 0
    for path in stage.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        data = path.read_bytes()
        if data[:4] != b"\x7fELF" or prefix not in data:
            continue
        dynamic = subprocess.run(["readelf", "-d", str(path)], capture_output=True, text=True).stdout
        values = re.findall(r"(?:RPATH|RUNPATH)[^\[]*\[([^\]]+)\]", dynamic)
        if not values:
            continue
        buffer = bytearray(data)
        for value in values:
            if prefix.decode() not in value:
                continue
            old = value.encode()
            new = b":".join(part[len(prefix):] if part.startswith(prefix) else part for part in old.split(b":"))
            # Only the exact, NUL-delimited rpath string is edited, never other text that mentions the path.
            needle = b"\x00" + old + b"\x00"
            at = buffer.find(needle)
            if at == -1 or buffer.find(needle, at + 1) != -1:
                raise RuntimeError(f"{path.name}: cannot uniquely locate its rpath string")
            buffer[at + 1:at + 1 + len(old)] = new + b"\x00" * (len(old) - len(new))
            fixed += 1
        path.write_bytes(bytes(buffer))
        check = subprocess.run(["readelf", "-d", str(path)], capture_output=True, text=True).stdout
        if str(root) in check:
            raise RuntimeError(f"{path.name}: the sysroot is still in its rpath after the edit")
    return fixed


def strip_sysroot_from_baked_paths(stage: Path, root: Path) -> list[str]:
    """Fix compiled-in path strings that pkg-config handed to the compiler with the sysroot prefix attached.

    `pkg-config --variable=...` prepends PKG_CONFIG_SYSROOT_DIR to paths, so a value a program bakes in
    (for example GNOME Shell's Mutter typelib directory) can name the build sysroot, which does not exist on
    the installed system. Only exact NUL-delimited strings that start with the sysroot are rewritten (the
    prefix is removed and the string padded with NULs), and each change is returned so it can be logged.
    """
    pattern = re.compile(rb"(?<=\x00)" + re.escape(str(root).encode()) + rb"(/[^\x00]*)(?=\x00)")
    changed = []
    for path in stage.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        data = path.read_bytes()
        if data[:4] != b"\x7fELF" or str(root).encode() not in data:
            continue
        buffer = bytearray(data)
        for match in list(pattern.finditer(data)):
            old, new = match.group(0), match.group(1)
            buffer[match.start():match.end()] = new + b"\x00" * (len(old) - len(new))
            changed.append(f"{path.relative_to(stage)}: {old.decode()} -> {new.decode()}")
        path.write_bytes(bytes(buffer))
    return changed


def strip_any_sysroot_prefix(stage: Path) -> list[str]:
    """Like strip_sysroot_from_baked_paths, for packages whose sysroot path is not known to the caller:
    any NUL-delimited string of the form <anything>/sysroot/<path> in an ELF becomes /<path>."""
    pattern = re.compile(rb"(?<=\x00)[^\x00]*?/sysroot(/[^\x00]*)(?=\x00)")
    changed = []
    for path in stage.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        data = path.read_bytes()
        if data[:4] != b"\x7fELF" or b"/sysroot/" not in data:
            continue
        buffer = bytearray(data)
        for match in list(pattern.finditer(data)):
            old, new = match.group(0), match.group(1)
            # A bare prefix counts too: IBus bakes in iso-codes' pkg-config `prefix`, which is exactly ".../usr".
            if not (new.startswith((b"/usr/", b"/etc/", b"/var/", b"/lib", b"/opt/")) or new in (b"/usr", b"/etc", b"/var", b"/opt")):
                continue
            buffer[match.start():match.end()] = new + b"\x00" * (len(old) - len(new))
            changed.append(f"{path.relative_to(stage)}: {new.decode()}")
        path.write_bytes(bytes(buffer))
    return changed


_kiln_build = build


def checked_build(package: Path, archive: Path):
    """Every package passes through here: sysroot-prefixed strings baked into binaries are corrected first."""
    root = Path(package) / "root"
    # pkg-config hands meson directories with the build sysroot prepended, so a package can install files to
    # <build>/sysroot/usr/share/... inside its stage. The part after "/sysroot" is exactly the directory it
    # meant, so move the file there (logged), and fail for anything else installed under a build path.
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        parts = path.relative_to(root).parts
        if "sysroot" in parts[:-1]:
            intended = root.joinpath(*parts[parts.index("sysroot") + 1:])
            intended.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(intended))
            print(f"relocated: {'/'.join(parts[parts.index('sysroot') + 1:])} (was under the build sysroot)", flush=True)
    for top in ("home", "tmp", "mnt"):
        leftover = root / top
        if leftover.is_dir() and not any(f.is_file() or f.is_symlink() for f in leftover.rglob("*")):
            shutil.rmtree(leftover)
    misplaced = [str(path.relative_to(root)) for path in root.rglob("*")
                 if path.parts[len(root.parts):len(root.parts) + 1] in (("home",), ("tmp",), ("mnt",))
                 or "sysroot" in path.relative_to(root).parts]
    if misplaced:
        raise RuntimeError("files were installed under a build path: " + ", ".join(misplaced[:6]))
    for line in strip_any_sysroot_prefix(root):
        print(f"baked path fixed: {line}", flush=True)
    # The info directory index is generated on the target by install-info; every GNU package ships its own copy,
    # which would conflict between packages.
    (root / "usr/share/info/dir").unlink(missing_ok=True)
    # libtool archives record absolute install paths that do not exist inside another package's sysroot, which
    # breaks anything linking through them. Nothing at runtime reads them, so none are shipped.
    for archive_file in list(root.rglob("*.la")):
        if archive_file.is_file() or archive_file.is_symlink():
            archive_file.unlink()
    # Setuid bits last: the kernel clears them whenever a non-root user writes to a setuid file, and the
    # baked-path fix-up above rewrites ELF files.
    if archive.name.startswith("polkit-"):
        helper = root / "usr/lib/polkit-1/polkit-agent-helper-1"
        if not helper.is_file():
            raise RuntimeError("polkit: authentication helper was not installed")
        helper.chmod(0o4755)
        # pkexec runs one program as root after polkit authorises it (the graphical installer uses it).
        pkexec = root / "usr/bin/pkexec"
        if not pkexec.is_file():
            raise RuntimeError("polkit: pkexec was not installed")
        pkexec.chmod(0o4755)
    if archive.name.startswith("dbus-"):
        helper = root / "usr/libexec/dbus-daemon-launch-helper"
        if not helper.is_file():
            raise RuntimeError("dbus: service activation helper was not installed")
        # The final initramfs normalizes ownership to root:root, so use 4755.
        # The helper itself still accepts calls only from the messagebus UID.
        helper.chmod(0o4755)
    return _kiln_build(package, archive)


build = checked_build
desktop.build = checked_build


def apply_recorded_patch(path: Path, before: str, after: str, old: str, new: str, why: str) -> None:
    """The only way a pinned source is modified: an exact replacement, refused unless the file's hash
    before and after matches what was reviewed. Every use is printed so it shows up in build logs."""
    import hashlib
    text = path.read_text()
    if hashlib.sha256(text.encode()).hexdigest() != before:
        raise RuntimeError(f"{path.name} is not the reviewed file; refusing to patch it")
    if text.count(old) != 1:
        raise RuntimeError(f"{path.name}: expected exactly one occurrence of the text to replace")
    patched = text.replace(old, new, 1)
    if hashlib.sha256(patched.encode()).hexdigest() != after:
        raise RuntimeError(f"{path.name}: the patch does not produce the reviewed result")
    path.write_text(patched)
    print(f"PATCH {path.name}: {why}", flush=True)


def gnome_shell_build(pkg: Package, lock: dict, cache: Path, work: Path, output: Path) -> None:
    """GNOME Shell runs `gjs` while building (its path goes into D-Bus service files), so a wrapper runs
    the target's gjs through the target loader; the installed files are then rewritten to /usr/bin/gjs."""
    unit = work / pkg.name
    unit.mkdir()
    upstream = source(lock, cache, unit / "source")
    apply_recorded_patch(
        upstream / "src/shell-app-usage.c",
        before="72e10b91aa6245d460275da5a4da8809a3597dd6ebb5f81237a915ebad62de55",
        after="b13c5746836355b7a70e6d8813002e442c685ad822a59ebdded0e42373cba3ea",
        old="#include <X11/Xlib.h>\n#include <X11/Xatom.h>\n", new="",
        why="GNOME Shell 50.6 includes X11 headers here without using any X11 symbol; the tray code that does "
            "is guarded by have_xwayland. Meldra builds Mutter without Xwayland and ships no X11 libraries.")
    sysroot = desktop.make_sysroot(output, unit, pkg.deps)
    root = sysroot.resolve()
    tools = unit / "gjs-wrapper"
    tools.mkdir()
    wrapper = tools / "gjs"
    wrapper.write_text(
        f'#!/bin/sh\nexport GI_TYPELIB_PATH="{root}/usr/lib/girepository-1.0:{root}/usr/lib/mutter-18"\n'
        f'export GCONV_PATH="{root}/usr/lib64/gconv"\n'
        f'exec "{root}/lib64/ld-linux-x86-64.so.2" --library-path "{root}/lib64:{root}/usr/lib64:{root}/usr/lib:{root}/usr/lib/pulseaudio" '
        f'"{root}/usr/bin/gjs" "$@"\n')
    wrapper.chmod(0o755)
    os.environ["PATH"] = f"{tools}{os.pathsep}{os.environ['PATH']}"
    original = desktop.cross_file
    desktop.cross_file = introspection_cross_file(original)
    try:
        cross = desktop.cross_file(unit, sysroot)
        build_dir, stage = unit / "build", unit / "package/root"
        subprocess.run([*desktop.MESON, "setup", "--wrap-mode=nodownload", str(build_dir), str(upstream), "--prefix=/usr",
                        "--sysconfdir=/etc", "--cross-file", str(cross), *pkg.options], check=True)
        subprocess.run([*desktop.MESON, "compile", "-C", str(build_dir)], check=True)
        subprocess.run([*desktop.MESON, "install", "-C", str(build_dir), "--destdir", str(stage)], check=True)
    finally:
        desktop.cross_file = original
    count = strip_sysroot_from_rpaths(stage, root)
    print(f"rpaths: removed the sysroot prefix from {count} string(s)", flush=True)
    for line in strip_sysroot_from_baked_paths(stage, root):
        print(f"baked path: {line}", flush=True)
    # Debug info records the build directory; strip it from the installed binaries.
    stripped = 0
    for path in stage.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open("rb") as stream:
            if stream.read(4) != b"\x7fELF":
                continue
        subprocess.run(["strip", "--strip-debug", str(path)], check=True)
        stripped += 1
    print(f"stripped debug info from {stripped} ELF file(s)", flush=True)
    # The build-machine gjs wrapper path must not survive into installed text files.
    for path in stage.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        if str(wrapper) in text:
            path.write_text(text.replace(str(wrapper), "/usr/bin/gjs"))
    leaks = []
    for path in stage.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        if str(work) in text or str(root) in text:
            leaks.append(str(path.relative_to(stage)))
    if leaks:
        raise RuntimeError("build directory paths are in installed text files: " + ", ".join(leaks[:8]))
    if not (stage / pkg.pattern).exists():
        raise RuntimeError(f"{pkg.name}: expected file not installed: {pkg.pattern}")
    build(manifest(stage, pkg.name, lock["version"], pkg.deps), output / f"{pkg.name}-{lock['version']}.kiln")


def gi_typelibs_build(pkg: Package, lock: dict, cache: Path, work: Path, output: Path) -> None:
    """The generic typelibs that ship with gobject-introspection (GL, DBus, cairo, freetype2, fontconfig, libxml2).

    Mutter's and Pango's introspection data depend on them, and the target builds of those libraries
    do not install them. They are compiled by the native g-ir-compiler from GI's own .gir files."""
    unit = work / pkg.name
    stage = unit / "package/root"
    target = stage / "usr/lib/girepository-1.0"
    target.mkdir(parents=True)
    source_dir = HOST_GI.resolve() / "lib/girepository-1.0"
    wanted = ("GL-1.0", "DBus-1.0", "cairo-1.0", "freetype2-2.0", "fontconfig-2.0", "libxml2-2.0")
    for name in wanted:
        shutil.copy2(source_dir / f"{name}.typelib", target / f"{name}.typelib")
    build(manifest(stage, pkg.name, lock["version"], pkg.deps), output / f"{pkg.name}-{lock['version']}.kiln")


def session_build(pkg: Package, lock: dict, cache: Path, work: Path, output: Path) -> None:
    """meldra-session (init/meldra-session.c): compiled against the target sysroot with Hearth's warning level."""
    unit = work / pkg.name
    unit.mkdir()
    sysroot = desktop.make_sysroot(output, unit, pkg.deps)
    root = sysroot.resolve()
    stage = unit / "package/root"
    (stage / "usr/bin").mkdir(parents=True)
    (stage / "etc/pam.d").mkdir(parents=True)
    binary = stage / "usr/bin/meldra-session"
    subprocess.run(
        ["gcc", f"--sysroot={root}", "-std=gnu11", "-O2", "-Wall", "-Wextra", "-Werror", "-pedantic",
         "-o", str(binary), "init/meldra-session.c", f"-Wl,-rpath-link,{root}/usr/lib", f"-Wl,-rpath-link,{root}/lib64",
         "-lpam"], check=True)
    require_glibc_elf(binary)
    shutil.copy2("init/pam.d/meldra-session", stage / "etc/pam.d/meldra-session")
    (stage / "usr/libexec/meldra").mkdir(parents=True)
    start = stage / "usr/libexec/meldra/start-gnome"
    shutil.copy2("init/start-gnome.sh", start)
    start.chmod(0o755)
    inner = stage / "usr/libexec/meldra/gnome-session-inner"
    shutil.copy2("init/gnome-session-inner.sh", inner)
    inner.chmod(0o755)
    for script in ("network-up", "udhcpc-script"):
        target = stage / "usr/libexec/meldra" / script
        shutil.copy2(f"init/network/{script}.sh", target)
        target.chmod(0o755)
    build(manifest(stage, pkg.name, lock["version"], pkg.deps), output / f"{pkg.name}-{lock['version']}.kiln")


def greetd_build(pkg: Package, lock: dict, cache: Path, work: Path, output: Path) -> None:
    """greetd (Rust): cargo build --locked, so every crate is checked against the checksums in Cargo.lock."""
    unit = work / pkg.name
    unit.mkdir()
    upstream = source(lock, cache, unit / "source")
    sysroot = desktop.make_sysroot(output, unit, pkg.deps)
    # Reuse the Rust cross set-up (sysroot linker wrapper, static libgcc) through a minimal cross file.
    plain = unit / "plain-cross.ini"
    plain.write_text("[binaries]\n[properties]\n", encoding="utf-8")
    rust_cross_file(lambda _work, _sysroot: plain)(unit, sysroot)
    env = {**os.environ, "CARGO_TARGET_DIR": str(unit / "target"),
           "RUSTFLAGS": f"--remap-path-prefix={upstream}=. --remap-path-prefix={Path.home()}=~"}
    subprocess.run(["cargo", "build", "--release", "--locked", "--target", RUST_TARGET,
                    "-j", os.environ.get("MELDRA_JOBS", "2"), "-p", "greetd", "-p", "agreety"],
                   cwd=upstream, env=env, check=True)
    built = unit / "target" / RUST_TARGET / "release"
    stage = unit / "package/root"
    (stage / "usr/sbin").mkdir(parents=True)
    (stage / "usr/bin").mkdir(parents=True)
    for binary, target in (("greetd", "usr/sbin/greetd"), ("agreety", "usr/bin/agreety")):
        shutil.copy2(built / binary, stage / target)
        subprocess.run(["strip", "--strip-unneeded", str(stage / target)], check=True)
        require_glibc_elf(stage / target)
    build(manifest(stage, pkg.name, lock["version"], pkg.deps), output / f"{pkg.name}-{lock['version']}.kiln")


def lz4_build(pkg: Package, lock: dict, cache: Path, work: Path, output: Path) -> None:
    """lz4: plain Makefiles; only the library is built and shipped."""
    unit = work / pkg.name
    unit.mkdir()
    upstream = source(lock, cache, unit / "source")
    sysroot = desktop.make_sysroot(output, unit, pkg.deps)
    stage = unit / "package/root"
    common = [f"CC=gcc --sysroot={sysroot.resolve()}", "CFLAGS=-O2 -pipe", "PREFIX=/usr", "LIBDIR=/usr/lib"]
    subprocess.run(["make", "-C", "lib", *common, "lib"], cwd=upstream, check=True)
    subprocess.run(["make", "-C", "lib", *common, f"DESTDIR={stage}", "install"], cwd=upstream, check=True)
    for archive in (stage / "usr/lib").glob("*.a"):
        archive.unlink()
    require_glibc_elf(next((stage / "usr/lib").glob("liblz4.so.1.*")))
    build(manifest(stage, pkg.name, lock["version"], pkg.deps), output / f"{pkg.name}-{lock['version']}.kiln")


FIRMWARE_SELECTION = (
    # GPUs
    "amdgpu/", "radeon/", "i915/", "xe/",
    # Wi-Fi and Bluetooth-combo chips
    "iwlwifi-", "intel/iwlwifi/", "rtw88/", "rtw89/", "mediatek/", "ath9k_htc/", "ath10k/", "ath11k/", "ath12k/",
    "brcm/", "cypress/", "rtlwifi/",
    # Wired network chips
    "rtl_nic/",
    # Wi-Fi regulatory database (cfg80211 reads it to know which channels are allowed)
    "regulatory.db", "regulatory.db.p7s",
)


def linux_firmware_build(pkg: Package, lock: dict, cache: Path, work: Path, output: Path) -> None:
    """A selection of linux-firmware (GPU, Wi-Fi, network) in /lib/firmware, where the kernel looks.

    Symlinks the upstream install script would create ("Link:" lines in WHENCE) are made too. NVIDIA GSP
    firmware is left out for now (large; only needed by nouveau on RTX 20 and newer)."""
    import tarfile
    archive = cache / lock.get("file", Path(lock["url"]).name)
    import hashlib
    if hashlib.sha256(archive.read_bytes()).hexdigest() != lock["sha256"]:
        raise RuntimeError("linux-firmware archive does not match its pin")
    unit_root = work / pkg.name
    stage = unit_root / "package/root"
    firmware = stage / "lib/firmware"
    firmware.mkdir(parents=True)
    top = f"linux-firmware-{lock['version']}/"
    whence = ""
    with tarfile.open(archive) as bundle:
        for member in bundle:
            if not member.name.startswith(top):
                continue
            relative = member.name[len(top):]
            if relative in ("WHENCE",) or relative.startswith("LICEN"):
                data = bundle.extractfile(member).read() if member.isfile() else b""
                if relative == "WHENCE":
                    whence = data.decode(errors="replace")
                if relative.startswith("LICEN") and member.isfile():
                    (firmware / relative).parent.mkdir(parents=True, exist_ok=True)
                    (firmware / relative).write_bytes(data)
                continue
            if not relative.startswith(FIRMWARE_SELECTION) or not member.isfile():
                continue
            target = firmware / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(bundle.extractfile(member).read())
    links = 0
    for line in whence.splitlines():
        if line.startswith("Link:"):
            link, _, points_to = line[5:].strip().partition(" -> ")
            if link.startswith(FIRMWARE_SELECTION) and (firmware / Path(link).parent / points_to).exists():
                (firmware / link).parent.mkdir(parents=True, exist_ok=True)
                (firmware / link).unlink(missing_ok=True)
                (firmware / link).symlink_to(points_to)
                links += 1
    (firmware / "WHENCE").write_text(whence)
    # Intel Wi-Fi ships every firmware API version for every chip; the driver uses the newest one it supports,
    # so the newest three per chip cover this kernel and the next few.
    import re as _re
    versions: dict[tuple[Path, str], list[tuple[int, Path]]] = {}
    for path in firmware.rglob("iwlwifi-*.ucode"):
        match = _re.fullmatch(r"(iwlwifi-.+)-(\d+)\.ucode", path.name)
        if match and not path.is_symlink():
            versions.setdefault((path.parent, match.group(1)), []).append((int(match.group(2)), path))
    dropped = 0
    for found in versions.values():
        for _, path in sorted(found, reverse=True)[3:]:
            path.unlink()
            dropped += 1
    for path in list(firmware.rglob("*")):           # links whose target was dropped
        if path.is_symlink() and not path.exists():
            path.unlink()
    # The kernel loads <name>.xz (CRC32-checked) when <name> is absent: compress everything but the notices.
    compressed = 0
    for path in sorted(firmware.rglob("*")):
        if path.is_file() and not path.is_symlink() and path.name not in ("WHENCE",) and \
                not path.relative_to(firmware).parts[0].startswith("LICEN"):
            subprocess.run(["xz", "--check=crc32", "-9", "--threads=1", str(path)], check=True)
            compressed += 1
    for path in sorted(firmware.rglob("*")):
        if path.is_symlink():
            target = os.readlink(path)
            path.unlink()
            path.with_name(path.name + ".xz").symlink_to(target + ".xz")
    size = sum(f.stat().st_size for f in firmware.rglob("*") if f.is_file())
    print(f"linux-firmware: {size / 1e6:.0f} MB after trimming {dropped} old Intel Wi-Fi versions and "
          f"compressing {compressed} files; {links} links")
    build(manifest(stage, pkg.name, lock["version"], pkg.deps), output / f"{pkg.name}-{lock['version']}.kiln")


def wireless_regdb_build(pkg: Package, lock: dict, cache: Path, work: Path, output: Path) -> None:
    """Install cfg80211's signed regulatory database, which is released separately from linux-firmware."""
    unit = work / pkg.name
    unit.mkdir()
    upstream = source(lock, cache, unit / "source")
    stage = unit / "package/root"
    firmware = stage / "lib/firmware"
    firmware.mkdir(parents=True)
    for name in ("regulatory.db", "regulatory.db.p7s"):
        shutil.copy2(upstream / name, firmware / name)
    build(manifest(stage, pkg.name, lock["version"], pkg.deps), output / f"{pkg.name}-{lock['version']}.kiln")


def openssl_build(pkg: Package, lock: dict, cache: Path, work: Path, output: Path) -> None:
    unit = work / pkg.name
    unit.mkdir()
    upstream = source(lock, cache, unit / "source")
    sysroot = desktop.make_sysroot(output, unit, pkg.deps)
    stage = unit / "package/root"
    env = {**os.environ, "CC": f"gcc --sysroot={sysroot.resolve()}", "CFLAGS": "-O2 -pipe"}
    subprocess.run(["./Configure", "linux-x86_64", "--prefix=/usr", "--libdir=lib", "--openssldir=/etc/ssl",
                    "shared", "no-tests", "no-docs", "no-static-engine"], cwd=upstream, env=env, check=True)
    subprocess.run(["make", f"-j{os.environ.get('MELDRA_JOBS', '2')}"], cwd=upstream, env=env, check=True)
    subprocess.run(["make", f"DESTDIR={stage}", "install_sw", "install_ssldirs"], cwd=upstream, env=env, check=True)
    for archive in (stage / "usr/lib").glob("*.a"):
        archive.unlink()
    require_glibc_elf(stage / "usr/lib/libssl.so.3")
    build(manifest(stage, pkg.name, lock["version"], pkg.deps), output / f"{pkg.name}-{lock['version']}.kiln")


WPA_SUPPLICANT_CONFIG = """
CONFIG_DRIVER_NL80211=y
CONFIG_LIBNL32=y
CONFIG_CTRL_IFACE=y
CONFIG_CTRL_IFACE_DBUS_NEW=y
CONFIG_CTRL_IFACE_DBUS_INTRO=y
CONFIG_TLS=openssl
CONFIG_IEEE8021X_EAPOL=y
CONFIG_EAP_MD5=y
CONFIG_EAP_MSCHAPV2=y
CONFIG_EAP_TLS=y
CONFIG_EAP_PEAP=y
CONFIG_EAP_TTLS=y
CONFIG_EAP_GTC=y
CONFIG_PKCS12=y
CONFIG_SAE=y
CONFIG_OWE=y
CONFIG_IEEE80211W=y
CONFIG_IEEE80211R=y
CONFIG_IEEE80211N=y
CONFIG_IEEE80211AC=y
CONFIG_IEEE80211AX=y
CONFIG_AP=y
CONFIG_BGSCAN_SIMPLE=y
CONFIG_BACKEND=file
CONFIG_ELOOP=eloop
CONFIG_L2_PACKET=linux
CONFIG_DEBUG_SYSLOG=y
"""


def wpa_supplicant_build(pkg: Package, lock: dict, cache: Path, work: Path, output: Path) -> None:
    unit = work / pkg.name
    unit.mkdir()
    upstream = source(lock, cache, unit / "source")
    sysroot = desktop.make_sysroot(output, unit, pkg.deps)
    root = sysroot.resolve()
    stage = unit / "package/root"
    tree = upstream / "wpa_supplicant"
    (tree / ".config").write_text(WPA_SUPPLICANT_CONFIG)
    env = {**os.environ, "PKG_CONFIG_SYSROOT_DIR": str(root),
           "PKG_CONFIG_LIBDIR": f"{root}/usr/lib/pkgconfig:{root}/usr/share/pkgconfig"}
    common = [f"CC=gcc --sysroot={root}", "BINDIR=/usr/sbin", "LIBDIR=/usr/lib"]
    subprocess.run(["make", *common, f"-j{os.environ.get('MELDRA_JOBS', '2')}"], cwd=tree, env=env, check=True)
    subprocess.run(["make", *common, f"DESTDIR={stage}", "install"], cwd=tree, env=env, check=True)
    dbus = stage / "usr/share/dbus-1"
    (dbus / "system.d").mkdir(parents=True)
    (dbus / "system-services").mkdir(parents=True)
    shutil.copy2(tree / "dbus/dbus-wpa_supplicant.conf", dbus / "system.d/wpa_supplicant.conf")
    service = (tree / "dbus/fi.w1.wpa_supplicant1.service.in").read_text().replace("@BINDIR@", "/usr/sbin")
    # Hearth has no systemd: drop SystemdService= so D-Bus starts the daemon itself.
    service = "\n".join(l for l in service.splitlines() if not l.startswith("SystemdService")) + "\n"
    (dbus / "system-services/fi.w1.wpa_supplicant1.service").write_text(service)
    require_glibc_elf(stage / "usr/sbin/wpa_supplicant")
    build(manifest(stage, pkg.name, lock["version"], pkg.deps), output / f"{pkg.name}-{lock['version']}.kiln")


def lua_build(pkg: Package, lock: dict, cache: Path, work: Path, output: Path) -> None:
    """Lua 5.4 as a static library plus headers and a pkg-config file (WirePlumber links it in)."""
    unit = work / pkg.name
    unit.mkdir()
    upstream = source(lock, cache, unit / "source")
    sysroot = desktop.make_sysroot(output, unit, pkg.deps)
    stage = unit / "package/root"
    subprocess.run(["make", "-C", "src", f"CC=gcc --sysroot={sysroot.resolve()} -std=gnu99",
                    "MYCFLAGS=-fPIC -DLUA_USE_LINUX", "MYLIBS=", "liblua.a"], cwd=upstream, check=True)
    include = stage / "usr/include/lua5.4"
    include.mkdir(parents=True)
    for header in ("lua.h", "luaconf.h", "lualib.h", "lauxlib.h", "lua.hpp"):
        shutil.copy2(upstream / "src" / header, include / header)
    lib = stage / "usr/lib"
    lib.mkdir(parents=True)
    shutil.copy2(upstream / "src/liblua.a", lib / "liblua5.4.a")
    pc = lib / "pkgconfig"
    pc.mkdir()
    (pc / "lua5.4.pc").write_text(f"prefix=/usr\nincludedir=${{prefix}}/include/lua5.4\nlibdir=${{prefix}}/lib\n\n"
                                  f"Name: Lua\nDescription: Lua language engine\nVersion: {lock['version']}\n"
                                  "Libs: -L${libdir} -llua5.4 -lm\nCflags: -I${includedir}\n")
    (pc / "lua.pc").symlink_to("lua5.4.pc")
    build(manifest(stage, pkg.name, lock["version"], pkg.deps), output / f"{pkg.name}-{lock['version']}.kiln")


def cups_build(pkg: Package, lock: dict, cache: Path, work: Path, output: Path) -> None:
    """libcups and its headers only: the client library Settings' Printers panel links. No print server yet."""
    unit = work / pkg.name
    unit.mkdir()
    upstream = source(lock, cache, unit / "source")
    for patch in pkg.patches:
        relative, before, after, old_text, new_text, why = patch
        print(f"PATCH {relative}: {why}", flush=True)
        apply_recorded_patch(upstream / relative, before, after, old_text, new_text, why)
    sysroot = desktop.make_sysroot(output, unit, pkg.deps)
    root = sysroot.resolve()
    stage = unit / "package/root"
    env = {**os.environ, "CC": f"gcc --sysroot={root}", "CXX": f"g++ --sysroot={root}", "CFLAGS": "-O2 -pipe",
           "PKG_CONFIG_SYSROOT_DIR": str(root),
           "PKG_CONFIG_LIBDIR": f"{root}/usr/lib/pkgconfig:{root}/usr/share/pkgconfig"}
    subprocess.run(["./configure", "--build=x86_64-pc-linux-gnu", "--host=x86_64-unknown-linux-gnu",
                    "--prefix=/usr", "--libdir=/usr/lib", "--sysconfdir=/etc", "--localstatedir=/var",
                    "--with-tls=openssl", "--disable-gssapi", "--disable-avahi", "--disable-dbus",
                    "--disable-systemd", "--without-rcdir", "--disable-pam", "--disable-static"],
                   cwd=upstream, env=env, check=True)
    subprocess.run(["make", f"-j{os.environ.get('MELDRA_JOBS', '2')}", "libs"], cwd=upstream, env=env, check=True)
    subprocess.run(["make", f"BUILDROOT={stage}", "install-headers", "install-libs"], cwd=upstream, env=env,
                   check=True)
    for archive in (stage / "usr/lib").glob("*.a"):
        archive.unlink()
    # cups.pc is installed by the server's data target; ship the generated one with the library.
    (stage / "usr/lib/pkgconfig").mkdir(parents=True, exist_ok=True)
    shutil.copy2(upstream / "cups.pc", stage / "usr/lib/pkgconfig/cups.pc")
    require_glibc_elf(stage / "usr/lib/libcups.so.2")
    build(manifest(stage, pkg.name, lock["version"], pkg.deps), output / f"{pkg.name}-{lock['version']}.kiln")


def lvm2_build(pkg: Package, lock: dict, cache: Path, work: Path, output: Path) -> None:
    """Only libdevmapper (and dmsetup) from LVM2: what cryptsetup and libblockdev link. No LVM tools yet."""
    unit = work / pkg.name
    unit.mkdir()
    upstream = source(lock, cache, unit / "source")
    sysroot = desktop.make_sysroot(output, unit, pkg.deps)
    root = sysroot.resolve()
    stage = unit / "package/root"
    env = {**os.environ, "CC": f"gcc --sysroot={root}", "CFLAGS": "-O2 -pipe",
           "PKG_CONFIG_SYSROOT_DIR": str(root),
           "PKG_CONFIG_LIBDIR": f"{root}/usr/lib/pkgconfig:{root}/usr/share/pkgconfig"}
    subprocess.run(["./configure", "--build=x86_64-pc-linux-gnu", "--host=x86_64-unknown-linux-gnu",
                    "--prefix=/usr", "--libdir=/usr/lib", "--sbindir=/usr/sbin", "--with-usrlibdir=/usr/lib",
                    "--enable-pkgconfig", "--disable-selinux", "--disable-readline", "--disable-systemd-journal",
                    "--disable-app-machineid", "--disable-sd-notify", "--with-udevdir=/usr/lib/udev/rules.d",
                    "--enable-udev_sync", "--disable-static_link", "--with-thin=none", "--with-cache=none",
                    "--with-vdo=none", "--with-writecache=none", "--with-integrity=none"],
                   cwd=upstream, env=env, check=True)
    subprocess.run(["make", f"-j{os.environ.get('MELDRA_JOBS', '2')}", "device-mapper"], cwd=upstream, env=env,
                   check=True)
    # Library, header and pkg-config file only: dmsetup's VDO statistics do not link in a libdm-only build.
    subprocess.run(["make", "-C", "libdm", f"DESTDIR={stage}", "install"], cwd=upstream, env=env, check=True)
    pc = upstream / "libdm/libdevmapper.pc"
    if pc.exists() and not (stage / "usr/lib/pkgconfig/devmapper.pc").exists():
        (stage / "usr/lib/pkgconfig").mkdir(parents=True, exist_ok=True)
        shutil.copy2(pc, stage / "usr/lib/pkgconfig/devmapper.pc")
    for archive in stage.rglob("*.a"):
        archive.unlink()
    require_glibc_elf(next((stage / "usr/lib").glob("libdevmapper.so.1.*")))
    build(manifest(stage, pkg.name, lock["version"], pkg.deps), output / f"{pkg.name}-{lock['version']}.kiln")


def krb5_build(pkg: Package, lock: dict, cache: Path, work: Path, output: Path) -> None:
    """MIT Kerberos libraries. configure lives in src/ and cannot run its test programs when cross-compiling,
    so the answers for x86_64 glibc are given."""
    unit = work / pkg.name
    unit.mkdir()
    upstream = source(lock, cache, unit / "source")
    sysroot = desktop.make_sysroot(output, unit, pkg.deps)
    root = sysroot.resolve()
    stage = unit / "package/root"
    env = {**os.environ, "CC": f"gcc --sysroot={root}", "CFLAGS": "-O2 -pipe",
           "krb5_cv_attr_constructor_destructor": "yes,yes", "ac_cv_func_regcomp": "yes",
           "ac_cv_printf_positional": "yes", "krb5_cv_ints_twos_compl": "yes", "krb5_cv_char_bit_8": "yes",
           "PKG_CONFIG_SYSROOT_DIR": str(root),
           "PKG_CONFIG_LIBDIR": f"{root}/usr/lib/pkgconfig:{root}/usr/share/pkgconfig"}
    # e2fsprogs' compile_et and mk_cmds are shell/awk scripts; run the sysroot's copies with their data there.
    tools = unit / "et-tools"
    tools.mkdir()
    for tool, variable, data in (("compile_et", "_ET_DIR_OVERRIDE", "et"), ("mk_cmds", "_SS_DIR_OVERRIDE", "ss")):
        (tools / tool).write_text(f'#!/bin/sh\nexport {variable}="{root}/usr/share/{data}"\n'
                                  f'exec sh "{root}/usr/bin/{tool}" "$@"\n')
        (tools / tool).chmod(0o755)
    env["PATH"] = f"{tools}{os.pathsep}{env['PATH']}"
    src = upstream / "src"
    subprocess.run(["./configure", "--build=x86_64-pc-linux-gnu", "--host=x86_64-unknown-linux-gnu",
                    "--prefix=/usr", "--libdir=/usr/lib", "--sysconfdir=/etc", "--localstatedir=/var/lib",
                    "--runstatedir=/run", "--without-system-verto", "--without-keyutils", "--without-ldap",
                    "--without-tcl", "--disable-rpath", "--with-crypto-impl=openssl", "--with-tls-impl=openssl",
                    # com_err and ss come from e2fsprogs (both projects ship them; one copy per system).
                    "--with-system-et", "--with-system-ss", "--enable-dns-for-realm"], cwd=src, env=env, check=True)
    subprocess.run(["make", f"-j{os.environ.get('MELDRA_JOBS', '2')}"], cwd=src, env=env, check=True)
    subprocess.run(["make", f"DESTDIR={stage}", "install"], cwd=src, env=env, check=True)
    for archive in stage.rglob("*.a"):
        archive.unlink()
    require_glibc_elf(stage / "usr/lib/libkrb5.so.3")
    build(manifest(stage, pkg.name, lock["version"], pkg.deps), output / f"{pkg.name}-{lock['version']}.kiln")


def keyutils_build(pkg: Package, lock: dict, cache: Path, work: Path, output: Path) -> None:
    """libkeyutils (plain Makefile); the keyctl tool is not needed."""
    unit = work / pkg.name
    unit.mkdir()
    upstream = source(lock, cache, unit / "source")
    sysroot = desktop.make_sysroot(output, unit, pkg.deps)
    stage = unit / "package/root"
    common = [f"CC=gcc --sysroot={sysroot.resolve()}", "CFLAGS=-O2 -pipe", "LIBDIR=/usr/lib", "USRLIBDIR=/usr/lib",
              "INCLUDEDIR=/usr/include", "NO_ARLIB=1", "SHAREDIR=/usr/share/keyutils"]
    subprocess.run(["make", *common, "all"], cwd=upstream, check=True)
    subprocess.run(["make", *common, f"DESTDIR={stage}", "install"], cwd=upstream, check=True)
    for extra in ("bin", "sbin", "usr/bin", "usr/sbin", "usr/share/man", "etc"):
        shutil.rmtree(stage / extra, ignore_errors=True)
    # Its Makefile links libkeyutils.so with an absolute target; Kiln only installs relative links.
    for link in (stage / "usr/lib").glob("libkeyutils.so*"):
        if link.is_symlink() and os.readlink(link).startswith("/"):
            target = Path(os.readlink(link)).name
            link.unlink()
            link.symlink_to(target)
    require_glibc_elf(next((stage / "usr/lib").glob("libkeyutils.so.1*")))
    build(manifest(stage, pkg.name, lock["version"], pkg.deps), output / f"{pkg.name}-{lock['version']}.kiln")


def fastfetch_build(pkg: Package, lock: dict, cache: Path, work: Path, output: Path) -> None:
    """fastfetch (CMake), with Meldra's square logo and config as the system-wide default."""
    unit = work / pkg.name
    unit.mkdir()
    upstream = source(lock, cache, unit / "source")
    sysroot = desktop.make_sysroot(output, unit, pkg.deps)
    root = sysroot.resolve()
    build_dir, stage = unit / "build", unit / "package/root"
    env = {**os.environ, "PKG_CONFIG_SYSROOT_DIR": str(root),
           "PKG_CONFIG_LIBDIR": f"{root}/usr/lib/pkgconfig:{root}/usr/share/pkgconfig"}
    flags = f"--sysroot={root} -O2"
    subprocess.run(["cmake", "-S", str(upstream), "-B", str(build_dir), "-G", "Ninja",
                    "-DCMAKE_BUILD_TYPE=Release", "-DCMAKE_INSTALL_PREFIX=/usr", "-DCMAKE_SYSTEM_NAME=Linux",
                    "-DCMAKE_C_COMPILER=gcc", f"-DCMAKE_C_FLAGS={flags}", f"-DCMAKE_EXE_LINKER_FLAGS={flags}",
                    f"-DCMAKE_FIND_ROOT_PATH={root}", "-DCMAKE_FIND_ROOT_PATH_MODE_PROGRAM=NEVER",
                    "-DCMAKE_FIND_ROOT_PATH_MODE_LIBRARY=ONLY", "-DCMAKE_FIND_ROOT_PATH_MODE_INCLUDE=ONLY",
                    "-DBUILD_TESTS=OFF", "-DBUILD_FLASHFETCH=OFF", "-DENABLE_SYSTEM_YYJSON=OFF"],
                   env=env, check=True)
    subprocess.run(["cmake", "--build", str(build_dir)], env=env, check=True)
    subprocess.run(["cmake", "--install", str(build_dir), "--prefix", str(stage / "usr")], env=env, check=True)
    binary = stage / "usr/bin/fastfetch"
    require_glibc_elf(binary)
    subprocess.run(["strip", "--strip-unneeded", str(binary)], check=True)
    config = stage / "etc/xdg/fastfetch"
    config.mkdir(parents=True)
    shutil.copy2("assets/fastfetch/config.jsonc", config / "config.jsonc")
    shutil.copy2("assets/fastfetch/meldra-logo.ansi", config / "meldra-logo.ansi")
    build(manifest(stage, pkg.name, lock["version"], pkg.deps), output / f"{pkg.name}-{lock['version']}.kiln")


def meldra_greeter_build(pkg: Package, lock: dict, cache: Path, work: Path, output: Path) -> None:
    """meldra-greeter: the GTK login screen, plus greetd's configuration and PAM stacks."""
    del cache
    unit = work / pkg.name
    unit.mkdir()
    sysroot = desktop.make_sysroot(output, unit, pkg.deps)
    root = sysroot.resolve()
    stage = unit / "package/root"
    env = {**os.environ, "PKG_CONFIG_SYSROOT_DIR": str(root),
           "PKG_CONFIG_LIBDIR": f"{root}/usr/lib/pkgconfig:{root}/usr/share/pkgconfig"}
    flags = subprocess.run(["pkg-config", "--cflags", "--libs", "gtk4", "json-glib-1.0"], env=env,
                           capture_output=True, text=True, check=True).stdout.split()
    binary = stage / "usr/libexec/meldra/meldra-greeter"
    binary.parent.mkdir(parents=True)
    subprocess.run(["gcc", f"--sysroot={root}", "-std=gnu11", "-O2", "-Wall", "-Wextra", "-Werror",
                    "-o", str(binary), "desktop/meldra-greeter.c", *flags, "-Wl,--allow-shlib-undefined",
                    f"-Wl,-rpath-link,{root}/usr/lib", f"-Wl,-rpath-link,{root}/lib64"], check=True)
    require_glibc_elf(binary)
    for source_file, target in (("init/greetd/config.toml", "etc/greetd/config.toml"),
                                ("init/greetd/session-env.conf", "etc/meldra/session-env.conf"),
                                ("init/pam.d/greetd", "etc/pam.d/greetd"),
                                ("init/pam.d/greetd-greeter", "etc/pam.d/greetd-greeter")):
        (stage / target).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, stage / target)
    build(manifest(stage, pkg.name, lock["version"], pkg.deps), output / f"{pkg.name}-{lock['version']}.kiln")


def meldra_installer_build(pkg: Package, lock: dict, cache: Path, work: Path, output: Path) -> None:
    """meldra-installer: the meldra-install back end, the Install Meldra window, and its polkit action."""
    del cache
    unit = work / pkg.name
    unit.mkdir()
    # Compile against the libraries only; busybox, the disk tools and the kernel are runtime dependencies.
    sysroot = desktop.make_sysroot(output, unit, ("glibc", "glib", "gtk", "json-glib"))
    root = sysroot.resolve()
    stage = unit / "package/root"
    (stage / "usr/sbin").mkdir(parents=True)
    (stage / "usr/bin").mkdir(parents=True)
    target = stage / "usr/sbin/meldra-install"
    shutil.copy2("installer/meldra-install.sh", target)
    target.chmod(0o755)
    env = {**os.environ, "PKG_CONFIG_SYSROOT_DIR": str(root),
           "PKG_CONFIG_LIBDIR": f"{root}/usr/lib/pkgconfig:{root}/usr/share/pkgconfig"}
    flags = subprocess.run(["pkg-config", "--cflags", "--libs", "gtk4", "json-glib-1.0"], env=env,
                           capture_output=True, text=True, check=True).stdout.split()
    binary = stage / "usr/bin/meldra-installer-gui"
    subprocess.run(["gcc", f"--sysroot={root}", "-std=gnu11", "-O2", "-Wall", "-Wextra", "-Werror",
                    "-o", str(binary), "installer/meldra-installer-gui.c", *flags, "-Wl,--allow-shlib-undefined",
                    f"-Wl,-rpath-link,{root}/usr/lib", f"-Wl,-rpath-link,{root}/lib64"], check=True)
    require_glibc_elf(binary)
    for source_file, destination in (
            ("installer/org.meldra.installer.policy", "usr/share/polkit-1/actions/org.meldra.installer.policy"),
            ("installer/org.meldra.Installer.desktop", "usr/share/applications/org.meldra.Installer.desktop")):
        (stage / destination).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, stage / destination)
    build(manifest(stage, pkg.name, lock["version"], pkg.deps), output / f"{pkg.name}-{lock['version']}.kiln")


def meldra_desktop_apps_build(pkg: Package, lock: dict, cache: Path, work: Path, output: Path) -> None:
    """Build Meldra's compact GTK core applications."""
    del cache
    unit = work / pkg.name
    unit.mkdir()
    sysroot = desktop.make_sysroot(output, unit, pkg.deps)
    root = sysroot.resolve()
    stage = unit / "package/root"
    (stage / "usr/bin").mkdir(parents=True)
    (stage / "usr/share/applications").mkdir(parents=True)
    env = {**os.environ, "PKG_CONFIG_SYSROOT_DIR": str(root),
           "PKG_CONFIG_LIBDIR": f"{root}/usr/lib/pkgconfig:{root}/usr/share/pkgconfig"}
    flags = subprocess.run(["pkg-config", "--cflags", "--libs", "gtk4"], env=env,
                           capture_output=True, text=True, check=True).stdout.split()
    binary = stage / "usr/bin/meldra-desktop-app"
    subprocess.run(["gcc", f"--sysroot={root}", "-std=gnu11", "-O2", "-Wall", "-Wextra", "-Werror",
                    "-Wno-deprecated-declarations",
                    "-o", str(binary), "desktop/meldra-apps.c", *flags,
                    "-Wl,--allow-shlib-undefined",
                    f"-Wl,-rpath-link,{root}/usr/lib", f"-Wl,-rpath-link,{root}/lib64"], check=True)
    require_glibc_elf(binary)
    for name in ("files", "text-editor", "settings"):
        (stage / "usr/bin" / f"meldra-{name}").symlink_to("meldra-desktop-app")
    for launcher in Path("desktop/applications").glob("*.desktop"):
        shutil.copy2(launcher, stage / "usr/share/applications" / launcher.name)
    build(manifest(stage, pkg.name, lock["version"], pkg.deps), output / f"{pkg.name}-{lock['version']}.kiln")


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
    if "/" in pkg.pattern:   # a path under the stage (header-only or data packages)
        if not (stage / pkg.pattern).exists():
            raise RuntimeError(f"{pkg.name}: expected file not installed: {pkg.pattern}")
    else:
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


def rtkit_build(pkg: Package, lock: dict, cache: Path, work: Path, output: Path) -> None:
    """Build rtkit without relying on the host-only xxd utility."""
    unit = work / pkg.name
    unit.mkdir()
    upstream = source(lock, cache, unit / "source")
    xml = (upstream / "org.freedesktop.RealtimeKit1.xml").read_bytes()
    (upstream / "xml-introspection.h").write_text(
        ",".join(f"0x{byte:02x}" for byte in xml) + "\n", encoding="ascii"
    )
    sysroot = desktop.make_sysroot(output, unit, pkg.deps)
    cross = desktop.cross_file(unit, sysroot)
    build_dir, stage = unit / "build", unit / "package/root"
    subprocess.run([*desktop.MESON, "setup", "--wrap-mode=nodownload", str(build_dir), str(upstream), "--prefix=/usr",
                    "--cross-file", str(cross), *pkg.options], check=True)
    subprocess.run([*desktop.MESON, "compile", "-C", str(build_dir)], check=True)
    subprocess.run([*desktop.MESON, "install", "-C", str(build_dir), "--destdir", str(stage)], check=True)
    daemon = stage / pkg.pattern
    if not daemon.is_file():
        raise RuntimeError(f"rtkit daemon not installed: {daemon}")
    require_glibc_elf(daemon)
    build(manifest(stage, pkg.name, lock["version"], pkg.deps), output / f"{pkg.name}-{lock['version']}.kiln")


PRINTERS_WHY = "The Printers panel needs Samba's libsmbclient (to browse Windows printers); Meldra has no Samba and no print server yet, so the panel is left out until printing lands."

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
        "rtkit", ("glibc", "dbus", "expat", "libcap", "polkit", "glib", "libffi", "pcre2", "zlib",
                  "duktape", "elogind", "util-linux", "eudev"), kind="custom",
        options=("-Dlibsystemd=disabled", "-Dinstalled_tests=false", "-Dlibexecdir=/usr/libexec",
                 "-Ddbus_systemservicedir=/usr/share/dbus-1/system-services",
                 "-Ddbus_interfacedir=/usr/share/dbus-1/interfaces",
                 "-Ddbus_rulesdir=/usr/share/dbus-1/system.d",
                 "-Dpolkit_actiondir=/usr/share/polkit-1/actions",
                 "-Dsystemd_systemunitdir=/usr/lib/systemd/system"),
        pattern="usr/libexec/rtkit-daemon",
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
                   "expat", "libpng", "gtk"),
        introspect=True, rust=True,
        options=("-Dglycin-loaders=true", "-Dloaders=glycin-image-rs", "-Dlibglycin=true",
                 "-Dlibglycin-gtk4=true", "-Dintrospection=true", "-Dvapi=false", "-Dtests=false",
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
                 "-Dverbose=false", "-Dudev=true", "-Dudev_dir=/usr/lib/udev", "-Dgles2=true", "-Degl=true", "-Dopengl=false"),
        pattern="libmutter-18.so.0.*",
    ),
    Package(
        "gnome-shell", ("glibc", "glib", "libffi", "pcre2", "zlib", "cairo", "pixman", "freetype", "fontconfig", "libpng", "expat",
                "pango", "harfbuzz", "fribidi", "graphene", "gsettings-desktop-schemas", "libxkbcommon", "libxml2",
                "at-spi2-core", "dbus", "colord", "lcms2", "sqlite", "libgusb", "libusb", "json-glib", "libei",
                "libdisplay-info", "libdrm", "gnome-desktop", "gdk-pixbuf", "gtk", "libgudev", "eudev", "wayland",
                "wayland-protocols", "libinput", "libevdev", "mtdev", "mesa", "glycin", "libseccomp", "elogind",
                "libcap", "util-linux", "libepoxy", "libtiff", "libjpeg-turbo", "iso-codes", "xkeyboard-config", "mutter", "gjs", "mozjs", "gcc-runtime", "polkit", "gcr", "evolution-data-server", "icu", "nss", "nspr", "libical", "libsecret", "libsoup", "libpsl", "nghttp2", "p11-kit", "libgcrypt", "libgpg-error", "libtasn1", "duktape", "pulseaudio", "libsndfile", "networkmanager", "libnma"),
        introspect=True, kind="custom",
        options=("-Dnetworkmanager=true", "-Dsystemd=false", "-Dcamera_monitor=false", "-Dportal_helper=false",
                 "-Dextensions_tool=false", "-Dextensions_app=false", "-Dgtk_doc=false", "-Dman=false",
                 "-Dtests=false"),
        pattern="usr/bin/gnome-shell",
    ),
    Package(
        "libsndfile", ("glibc",), kind="cmake",
        options=("-DENABLE_EXTERNAL_LIBS=OFF", "-DENABLE_MPEG=OFF", "-DBUILD_PROGRAMS=OFF",
                 "-DBUILD_EXAMPLES=OFF", "-DBUILD_TESTING=OFF", "-DBUILD_REGTEST=OFF",
                 "-DENABLE_CPU_CLIP=OFF", "-DINSTALL_MANPAGES=OFF", "-DBUILD_SHARED_LIBS=ON",
                 "-DCMAKE_POLICY_VERSION_MINIMUM=3.5"),
        pattern="libsndfile.so.1.*",
    ),
    Package(
        "pulseaudio", ("glibc", "glib", "libffi", "pcre2", "zlib", "libcap", "libsndfile"),
        options=("-Ddaemon=false", "-Dclient=true", "-Ddoxygen=false", "-Dman=false", "-Dtests=false",
                 "-Dglib=enabled", "-Dalsa=disabled", "-Dasyncns=disabled", "-Davahi=disabled",
                 "-Dbluez5=disabled", "-Ddbus=disabled", "-Delogind=disabled", "-Dgsettings=disabled",
                 "-Dgstreamer=disabled", "-Dgtk=disabled", "-Dopenssl=disabled", "-Dorc=disabled",
                 "-Dsystemd=disabled", "-Dudev=disabled", "-Dx11=disabled", "-Dtcpwrap=disabled",
                 "-Dsoxr=disabled", "-Dspeex=disabled", "-Dfftw=disabled", "-Dsamplerate=disabled",
                 "-Dwebrtc-aec=disabled", "-Dlirc=disabled", "-Djack=disabled", "-Dconsolekit=disabled",
                 "-Dhal-compat=false", "-Dadrian-aec=false", "-Doss-output=disabled",
                 "--sysconfdir=/etc", "--localstatedir=/var"),
        pattern="libpulse.so.0.*",
    ),
    Package(
        "libxcrypt", ("glibc",), kind="autotools",
        options=("--disable-static", "--enable-shared", "--disable-obsolete-api", "--disable-failure-tokens", "--disable-werror"),
        pattern="libcrypt.so.2.*",
    ),
    Package(
        "linux-pam", ("glibc", "libxcrypt"),
        options=("-Di18n=disabled", "-Ddocs=disabled", "-Daudit=disabled", "-Deconf=disabled",
                 "-Dlogind=disabled", "-Delogind=disabled", "-Dselinux=disabled", "-Dnis=disabled",
                 "-Dpwaccess=disabled", "-Dpam_userdb=disabled", "-Dexamples=false", "-Dxtests=false",
                 "-Dsecuredir=/usr/lib/security", "-Dsconfigdir=/etc/security", "--sysconfdir=/etc"),
        pattern="libpam.so.0.*",
    ),
    Package("meldra-session", ("glibc", "linux-pam", "libxcrypt", "elogind"), kind="custom"),
    Package(
        # Rebuilt here (the graphics stack's copy predates elogind) so libseat can use elogind: cage, the login
        # screen's compositor, then gets its devices from the same seat manager as GNOME, with no seatd daemon.
        "seatd", ("glibc", "elogind"),
        options=("-Dlibseat-logind=elogind", "-Dlibseat-seatd=enabled", "-Dlibseat-builtin=disabled",
                 "-Dserver=enabled", "-Dexamples=disabled", "-Dman-pages=disabled"),
        pattern="libseat.so.1*",
    ),
    Package("greetd", ("glibc", "linux-pam"), kind="custom"),
    Package(
        "cage", ("glibc", "wlroots", "libdrm", "libffi", "expat", "wayland", "wayland-protocols", "mesa",
                 "libxkbcommon", "eudev", "libevdev", "mtdev", "libinput", "pixman", "seatd", "libdisplay-info"),
        kind="data", options=("-Dman-pages=disabled",), pattern="usr/bin/cage",
    ),
    Package("meldra-greeter", ("glibc", "glib", "gtk", "json-glib", "greetd", "cage", "seatd", "linux-pam",
                               "elogind"), kind="custom"),
    Package("meldra-installer", ("busybox", "util-linux", "e2fsprogs", "dosfstools", "linux", "polkit", "glibc",
                                 "glib", "gtk", "json-glib"), kind="custom"),
    Package("meldra-desktop-apps", ("glibc", "glib", "gtk"), kind="custom"),
    Package("gi-typelibs", ("glibc", "glib"), kind="custom"),
    Package(
        "json-c", ("glibc",), kind="cmake",
        options=("-DBUILD_SHARED_LIBS=ON", "-DBUILD_STATIC_LIBS=OFF", "-DBUILD_TESTING=OFF",
                 "-DDISABLE_WERROR=ON", "-DCMAKE_POLICY_VERSION_MINIMUM=3.5"),
        pattern="libjson-c.so.5.*",
    ),
    Package(
        "accountsservice", ("glibc", "glib", "libffi", "pcre2", "zlib", "polkit", "json-c", "elogind", "dbus",
                            "libxcrypt", "expat", "libcap", "util-linux", "eudev", "duktape"),
        introspect=True,
        options=("-Delogind=true", "-Dintrospection=true", "-Dvapi=false", "-Ddocbook=false", "-Dgtk_doc=false",
                 "-Dtests=false", "-Dadmin_group=wheel", "-Dminimum_uid=1000", "-Dlibxcrypt=true",
                 "-Dsystemdsystemunitdir=/usr/lib/systemd/system", "--sysconfdir=/etc", "--localstatedir=/var"),
        pattern="libaccountsservice.so.1.*",
    ),
    Package(
        "dconf", ("glibc", "glib", "libffi", "pcre2", "zlib", "dbus", "expat"),
        options=("-Dbash_completion=false", "-Dman=false", "-Dgtk_doc=false", "-Dvapi=false",
                 "-Dsystemduserunitdir=/usr/lib/systemd/user"),
        pattern="libdconf.so.1.*",
    ),
    Package(
        "gdm", ("glibc", "glib", "libffi", "pcre2", "zlib", "libgudev", "eudev", "json-glib", "accountsservice",
                "polkit", "elogind", "linux-pam", "libxcrypt", "dbus", "expat", "libcap", "util-linux", "duktape",
                "json-c", "dconf"),
        introspect=True, target_tools={"dconf": "/usr/bin/dconf"},
        patches=(("meson.build", "c9d3891e334e8205c689b11d86b0d94e56e5257304924def0ce9e83b2913a20e",
                  "50a58226259e23e83172efc86712dd17c9e21d4fd1f26927e716eec5a0b1d923",
                  "  dconf_prefix = dconf_dep.get_variable(pkgconfig: 'prefix')\n",
                  "  dconf_prefix = get_option('prefix')\n",
                  "In a cross build dconf's pkg-config prefix carries the sysroot, so GDM's `== '/usr'` test fails and "
                  "its dconf profile lands in <sysroot>/usr/etc with that path compiled into gdm-config. dconf and GDM "
                  "share the /usr prefix here, so use it directly."),),
        options=("-Dx11-support=false", "-Dlogind-provider=elogind", "-Dplymouth=disabled", "-Dselinux=disabled",
                 "-Dlibaudit=disabled", "-Dsystemd-journal=false", "-Dgdm-xsession=false",
                 "-Dsystemdsystemunitdir=/usr/lib/systemd/system", "-Dsystemduserunitdir=/usr/lib/systemd/user",
                 "-Dpam-mod-dir=/usr/lib/security", "-Ddbus-sys=/usr/share/dbus-1/system.d",
                 "--sysconfdir=/etc", "--localstatedir=/var"),
        pattern="libgdm.so.1.*",
    ),
    Package(
        "upower", ("glibc", "glib", "libffi", "pcre2", "zlib", "libgudev", "eudev"), introspect=True,
        options=("-Dman=false", "-Dgtk-doc=false", "-Dintrospection=enabled", "-Didevice=disabled",
                 "-Dpolkit=disabled", "-Dos_backend=linux", "-Dinstalled_tests=false",
                 "-Dudevrulesdir=/usr/lib/udev/rules.d", "-Dudevhwdbdir=/usr/lib/udev/hwdb.d",
                 "-Dhistorydir=/var/lib/upower", "-Dstatedir=/var/lib/upower", "-Dzshcompletiondir=no",
                 "-Dsystemdsystemunitdir=no", "--sysconfdir=/etc", "--localstatedir=/var"),
        pattern="libupower-glib.so.3.*",
    ),
    Package(
        "geoclue", ("glibc", "glib", "libffi", "pcre2", "zlib", "libsoup", "libpsl", "nghttp2", "sqlite", "libxml2", "json-glib", "dbus"), introspect=True,
        options=("-Dlibgeoclue=true", "-Dintrospection=true", "-Dvapi=false", "-Dgtk-doc=false",
                 "-Dwifi-source=false", "-D3g-source=false", "-Dcdma-source=false", "-Dmodem-gps-source=false",
                 "-Dnmea-source=false", "-Dstatic-source=false", "-Dip-source=true", "-Dcompass=false",
                 "-Denable-backend=true", "-Ddemo-agent=false", "-Ddbus-sys-dir=/usr/share/dbus-1/system.d",
                 "--sysconfdir=/etc", "--localstatedir=/var"),
        pattern="libgeoclue-2.so.0.*",
    ),
    Package(
        "geocode-glib", ("glibc", "glib", "libffi", "pcre2", "zlib", "libsoup", "libpsl", "nghttp2", "sqlite", "libxml2", "json-glib"), introspect=True,
        options=("-Denable-introspection=true", "-Denable-gtk-doc=false", "-Denable-installed-tests=false",
                 "-Dsoup2=false"),
        pattern="libgeocode-glib-2.so.0.*",
    ),
    Package(
        "libgweather", ("glibc", "glib", "libffi", "pcre2", "zlib", "libsoup", "libpsl", "nghttp2", "sqlite", "libxml2", "json-glib", "geocode-glib", "gdk-pixbuf", "libpng"), introspect=True, host_python=True,
        patches=(("meson.build", "39905bf86281688c88d175c3d25af1200fa44df0edec782815e5bbf453ad1151",
                  "e2e885bdabe63236b96219e0c1f995b102bad7906a124dab1132004206f0fa6d",
                  "build_gir = get_option('introspection') and g_ir_scanner.found() and not meson.is_cross_build()\n",
                  "build_gir = get_option('introspection') and g_ir_scanner.found()\n",
                  "libgweather skips GWeather-4.0.typelib in any cross build. This build is cross in name only "
                  "(same architecture, run through the target loader) and GNOME Shell requires the typelib."),),
        options=("-Dintrospection=true", "-Dtests=false", "-Dgtk_doc=false", "-Denable_vala=false",
                 "-Dsoup2=false", "-Dzoneinfo_dir=/usr/share/zoneinfo"),
        pattern="libgweather-4.so.0.*",
    ),
    Package(
        "ibus", ("glibc", "glib", "libffi", "pcre2", "zlib", "dbus", "json-glib", "iso-codes", "xkeyboard-config", "libxkbcommon", "libxml2"),
        introspect=True, host_programs=("valac", "vapigen"),
        patches=(("meson.build", "4c83485de6e14c703aadc553c860ded38e2ec7e12ed5f9a444eb4294b94ba975",
                  "586259113ff4f14b4bbfbd4a9860830a07bae92afc48508a289e0b101d4cf55d",
                  "enable_pygobject = pygobject_dep.found()\nif enable_pygobject\n",
                  "enable_pygobject = pygobject_dep.found()\npyoverridesdir = ''\nif enable_pygobject\n",
                  "IBus's build summary reads pyoverridesdir, which is only defined when PyGObject is found; "
                  "define it so a build without PyGObject configures. It changes no build output."),),
        options=("-Dtests=false", "-Dxim=disabled", "-Dwayland=disabled", "-Dappindicator=false", "-Dengine=false",
                 "-Dui=false", "-Dsetup=false", "-Dsystemd-services=disabled", "-Dlibnotify=false", "-Ddconf=disabled",
                 "-Dgtk3=false", "-Dgtk4=false", "-Dunicode-dict=false", "-Demoji-dict=false", "-Dvapi=enabled", "-Dgtk-doc=false",
                 "--sysconfdir=/etc", "--localstatedir=/var"),
        pattern="libibus-1.0.so.5.*",
    ),
    Package(
        "librsvg", ("glibc", "glib", "libffi", "pcre2", "zlib", "cairo", "pixman", "freetype", "fontconfig", "libpng", "expat", "harfbuzz", "fribidi",
                    "pango", "gdk-pixbuf", "libxml2", "libtiff", "libjpeg-turbo", "gcc-runtime"),
        introspect=True, rust=True,
        options=("-Dintrospection=enabled", "-Dpixbuf=enabled", "-Dpixbuf-loader=enabled", "-Dvala=disabled",
                 "-Ddocs=disabled", "-Dtests=false", "-Davif=disabled"),
        pattern="librsvg-2.so.2.*",
    ),
    Package("libltdl", ("glibc",), kind="custom", pattern="libltdl.so.7.*"),
    Package(
        "libogg", ("glibc",), kind="autotools",
        options=("--disable-static", "--enable-shared"),
        pattern="libogg.so.0.*",
    ),
    Package(
        "libvorbis", ("glibc", "libogg"), kind="autotools",
        options=("--disable-static", "--enable-shared", "--disable-docs", "--disable-examples", "--disable-oggtest"),
        pattern="libvorbisfile.so.3.*",
    ),
    Package(
        "libcanberra", ("glibc", "libltdl", "libvorbis", "libogg"), kind="autotools",
        options=("--disable-static", "--enable-shared", "--disable-alsa", "--disable-oss", "--disable-pulse",
                 "--disable-udev", "--disable-gstreamer", "--disable-gtk", "--disable-gtk3", "--disable-tdb",
                 "--disable-lynx"),
        pattern="libcanberra.so.0.*",
    ),
    Package(
        "e2fsprogs", ("glibc", "util-linux"), kind="autotools",
        options=("--disable-nls", "--disable-fuse2fs", "--disable-fsck", "--disable-uuidd", "--disable-libuuid",
                 "--disable-libblkid", "--disable-e2initrd-helper", "--without-udev-rules-dir",
                 "--without-systemd-unit-dir", "--without-crond-dir", "--disable-defrag", "--enable-elf-shlibs",
                 "--libdir=/usr/lib", "--sbindir=/usr/sbin", "--sysconfdir=/etc"),
        pattern="libext2fs.so.2.*",
    ),
    Package(
        "dosfstools", ("glibc",), kind="cross-autotools", options=("--enable-compat-symlinks", "--sbindir=/usr/sbin"),
        pattern="usr/sbin/mkfs.fat",
    ),
    # --- GPU acceleration ---
    Package(
        # Rebuilt with the AMD, Radeon and Nouveau helper libraries Mesa's hardware drivers need.
        "libdrm", ("glibc",),
        options=("-Damdgpu=enabled", "-Dradeon=enabled", "-Dnouveau=enabled", "-Dintel=disabled",
                 "-Dvmwgfx=enabled", "-Dtests=false", "-Dman-pages=disabled", "-Dvalgrind=disabled",
                 "-Dcairo-tests=disabled"),
        pattern="libdrm.so.2*",
    ),
    Package(
        "elfutils", ("glibc", "zlib", "gcc-runtime"), kind="cross-autotools",
        options=("--disable-debuginfod", "--disable-libdebuginfod", "--disable-nls", "--program-prefix=eu-",
                 "--libdir=/usr/lib", "--without-bzlib", "--without-lzma", "--without-zstd"),
        pattern="usr/lib/libelf.so.1",
    ),
    Package("vulkan-headers", ("glibc",), kind="cmake", options=("-DVULKAN_HEADERS_ENABLE_TESTS=OFF",),
            pattern="usr/include/vulkan/vulkan.h"),
    Package(
        "vulkan-loader", ("glibc", "vulkan-headers", "wayland", "libffi"), kind="cmake",
        options=("-DBUILD_TESTS=OFF", "-DBUILD_WSI_XCB_SUPPORT=OFF", "-DBUILD_WSI_XLIB_SUPPORT=OFF",
                 "-DBUILD_WSI_WAYLAND_SUPPORT=ON", "-DBUILD_WSI_DIRECTFB_SUPPORT=OFF", "-DSYSCONFDIR=/etc"),
        pattern="libvulkan.so.1*",
    ),
    Package(
        "mesa", ("glibc", "libdrm", "libffi", "expat", "wayland", "wayland-protocols", "zlib", "elfutils",
                 "vulkan-loader", "vulkan-headers", "gcc-runtime"),
        options=("-Dplatforms=wayland", "-Dgallium-drivers=softpipe,virgl,radeonsi,zink,nouveau",
                 # Intel (iris, anv) needs mesa-clc, i.e. LLVM and clang at build time: a later step.
                 "-Dvulkan-drivers=amd,virtio", "-Dglx=disabled", "-Degl=enabled", "-Dgbm=enabled",
                 "-Dgles1=disabled", "-Dgles2=enabled", "-Dopengl=true", "-Dglvnd=disabled",
                 # No LLVM: AMD shaders compile with Mesa's own ACO.
                 "-Dllvm=disabled", "-Dshared-llvm=disabled", "-Damd-use-llvm=false", "-Ddraw-use-llvm=false",
                 "-Dlibunwind=disabled", "-Dzstd=disabled", "-Dshader-cache=enabled", "-Dvalgrind=disabled",
                 "-Dbuild-tests=false", "-Dgallium-va=disabled", "-Dvideo-codecs=[]", "-Dintel-rt=disabled",
                 "-Dgallium-rusticl=false", "-Dteflon=false", "-Dmicrosoft-clc=disabled", "-Dvulkan-layers=[]",
                 "-Dtools=[]", "-Dsysconfdir=/etc"),
        pattern="libgallium-*.so",
    ),
    Package(
        "kmod", ("glibc",),
        options=("-Dmanpages=false", "-Ddocs=false", "-Dzstd=disabled", "-Dxz=disabled", "-Dzlib=disabled",
                 "-Dopenssl=disabled", "-Dtools=true", "-Dbuild-tests=false", "-Dbashcompletiondir=no",
                 "-Dfishcompletiondir=no", "-Dzshcompletiondir=no", "--sbindir=/usr/sbin",
                 # The linux-modules package installs to /usr/lib/modules.
                 "-Dmoduledir=/usr/lib/modules"),
        pattern="libkmod.so.2*",
    ),
    # --- Drivers, firmware, Wi-Fi, audio ---
    Package("linux-firmware", (), kind="custom"),
    Package("wireless-regdb", (), kind="custom"),
    Package(
        # Rebuilt with kmod so it loads drivers (Wi-Fi, AMD GPU, audio) for the hardware it finds, and with blkid.
        "eudev", ("glibc", "kmod", "util-linux"), kind="cross-autotools",
        options=("--disable-static", "--enable-shared", "--enable-blkid", "--disable-selinux", "--enable-kmod",
                 "--disable-manpages", "--disable-systemd-hwdb", "--disable-mtd_probe", "--disable-tpm2",
                 "--with-rootprefix=/usr", "--with-rootlibdir=/usr/lib", "--with-rootlibexecdir=/usr/lib/udev",
                 "--sysconfdir=/etc"),
        pattern="usr/bin/udevadm",
    ),
    Package("openssl", ("glibc",), kind="custom"),
    Package("libnl", ("glibc",), kind="cross-autotools",
            options=("--disable-static", "--sysconfdir=/etc", "--libdir=/usr/lib", "--disable-cli"),
            pattern="usr/lib/libnl-3.so.200"),
    Package("wpa-supplicant", ("glibc", "libnl", "openssl", "dbus"), kind="custom"),
    Package("libndp", ("glibc",), kind="cross-autotools", options=("--disable-static", "--libdir=/usr/lib"),
            pattern="usr/lib/libndp.so.0"),
    Package(
        "networkmanager", ("glibc", "glib", "dbus", "eudev", "util-linux", "libndp", "elogind", "polkit", "nss",
                           "readline", "wpa-supplicant"),
        introspect=True,
        options=("-Dsystemd_journal=false", "-Dsession_tracking=elogind", "-Dsession_tracking_consolekit=false",
                 "-Dsuspend_resume=elogind", "-Dpolkit=true", "-Dselinux=false", "-Dlibaudit=no", "-Dwext=false",
                 "-Dwifi=true", "-Diwd=false", "-Dppp=false", "-Dmodem_manager=false", "-Dofono=false",
                 "-Dconcheck=false", "-Dteamdctl=false", "-Dovs=false", "-Dnmcli=true", "-Dnmtui=false",
                 "-Dnm_cloud_setup=false", "-Debpf=false", "-Dnbft=false", "-Dclat=false", "-Dintrospection=true",
                 "-Dvapi=false", "-Ddocs=false", "-Dman=false", "-Dtests=no", "-Dfirewalld_zone=false",
                 "-Dlibpsl=false", "-Dcrypto=nss", "-Dqt=false", "-Dreadline=libreadline",
                 "-Dudev_dir=/usr/lib/udev", "-Ddbus_conf_dir=/usr/share/dbus-1/system.d",
                 "-Dsystemdsystemunitdir=no", "-Dsystemdsystemgeneratordir=no", "-Dconfig_dns_rc_manager_default=file", "-Dconfig_dhcp_default=internal",
                 "-Dconfig_wifi_backend_default=wpa_supplicant", "-Dmodprobe=/usr/sbin/modprobe",
                 "-Dkernel_firmware_dir=/lib/firmware", "--sysconfdir=/etc", "--localstatedir=/var"),
        pattern="libnm.so.0*",
    ),
    Package(
        "gtk3", ("glibc", "glib", "pango", "cairo", "harfbuzz", "fribidi", "fontconfig", "freetype", "gdk-pixbuf",
                 "libepoxy", "libxkbcommon", "wayland", "wayland-protocols", "at-spi2-core", "mesa", "libdrm"),
        introspect=True,
        options=("-Dx11_backend=false", "-Dwayland_backend=true", "-Dbroadway_backend=false", "-Dprint_backends=file",
                 "-Dcolord=no", "-Dgtk_doc=false", "-Dman=false", "-Dintrospection=true", "-Ddemos=false",
                 "-Dexamples=false", "-Dtests=false", "-Dinstalled_tests=false", "-Dcloudproviders=false",
                 "-Dtracker3=false", "-Dprofiler=false"),
        pattern="libgtk-3.so.0*",
    ),
    Package(
        "libnma", ("glibc", "glib", "gtk", "gtk3", "networkmanager"), introspect=True,
        options=("-Dlibnma_gtk4=true", "-Dgcr=false", "-Diso_codes=false", "-Dmobile_broadband_provider_info=false",
                 "-Dgtk_doc=false", "-Dintrospection=true", "-Dvapi=false"),
        pattern="libnma-gtk4.so.0*",
    ),
    # --- GNOME Settings and what it needs ---
    Package("cracklib", ("glibc", "zlib"), kind="cross-autotools", post=cracklib_post,
            options=("--disable-static", "--without-python", "--disable-nls", "--libdir=/usr/lib",
                     "--sbindir=/usr/sbin", "--with-default-dict=/usr/share/cracklib/pw_dict"),
            pattern="usr/share/cracklib/pw_dict.pwd"),
    Package("libpwquality", ("glibc", "cracklib", "linux-pam"), kind="cross-autotools",
            options=("--disable-static", "--disable-python-bindings", "--disable-nls", "--libdir=/usr/lib",
                     "--with-securedir=/usr/lib/security"),
            pattern="usr/lib/libpwquality.so.1"),
    Package("libwacom", ("glibc", "glib", "libgudev", "eudev", "libevdev", "libxml2"),
            options=("-Ddocumentation=disabled", "-Dtests=disabled", "-Dudev-dir=/usr/lib/udev"),
            pattern="libwacom.so.9*"),
    Package("modemmanager", ("glibc", "glib", "eudev", "libgudev", "polkit", "elogind", "dbus"), introspect=True,
            options=("-Dsystemdsystemunitdir=no", "-Dsystemd_suspend_resume=false", "-Dsystemd_journal=false",
                     "-Dpolkit=permissive", "-Dmbim=false", "-Dqmi=false", "-Dqrtr=false", "-Dintrospection=true",
                     "-Dvapi=false", "-Dman=false", "-Dgtk_doc=false", "-Dbash_completion=false", "-Dtests=false",
                     "-Dexamples=false", "-Dudevdir=/usr/lib/udev", "-Ddbus_policy_dir=/usr/share/dbus-1/system.d"),
            pattern="libmm-glib.so.0*"),
    Package("gsound", ("glibc", "glib", "libcanberra"), pattern="libgsound.so.0*",
            options=("-Dintrospection=false", "-Denable_vala=false", "-Dgtk_doc=false")),
    Package("gnome-bluetooth", ("glibc", "glib", "gtk", "libadwaita", "gsound", "libnotify", "eudev", "upower"),
            introspect=True, options=("-Dsendto=false", "-Dgtk_doc=false", "-Dintrospection=true"),
            pattern="libgnome-bluetooth-ui-3.0.so.13*"),
    Package("librest", ("glibc", "glib", "libsoup", "libxml2", "json-glib"), introspect=True,
            options=("-Dsoup2=false", "-Dexamples=false", "-Dtests=false", "-Dgtk_doc=false", "-Dvapi=false",
                     "-Dintrospection=true", "-Dca_certificates=true", "-Dca_certificates_path=/etc/ssl/certs/ca-certificates.crt"),
            pattern="librest-1.0.so.0*"),
    Package("gnome-online-accounts", ("glibc", "glib", "json-glib", "libxml2", "dbus", "gcr", "libsoup", "libsecret",
                                      "gtk", "libadwaita", "librest"),
            introspect=True,
            # The backend Settings' Online Accounts panel needs; Kerberos and the Fedora account are left out.
            options=("-Dgoabackend=true", "-Dkerberos=false", "-Dfedora=false", "-Ddocumentation=false", "-Dman=false", "-Dgtk_doc=false",
                     "-Dintrospection=true", "-Dvapi=false"),
            pattern="libgoa-1.0.so.0*"),
    Package("colord-gtk", ("glibc", "glib", "gtk", "colord", "lcms2"),
            options=("-Dgtk4=true", "-Dgtk3=false", "-Dgtk2=false", "-Dintrospection=false", "-Dvapi=false",
                     "-Dtests=false", "-Dman=false", "-Ddocs=false"),
            pattern="libcolord-gtk4.so.1*"),
    # GMP's configure tests are pre-C23 C; GCC 15 defaults to C23 and rejects them ("long long reliability").
    Package("gmp", ("glibc",), kind="cross-autotools", env={"CFLAGS": "-O2 -pipe -std=gnu17"},
            options=("--disable-static", "--enable-shared", "--libdir=/usr/lib"), pattern="usr/lib/libgmp.so.10"),
    Package("nettle", ("glibc", "gmp"), kind="cross-autotools",
            options=("--disable-static", "--disable-documentation", "--libdir=/usr/lib", "--disable-openssl"),
            pattern="usr/lib/libnettle.so.8"),
    Package("gnutls", ("glibc", "nettle", "gmp", "libtasn1", "p11-kit", "zlib"), kind="cross-autotools",
            options=("--disable-static", "--disable-doc", "--disable-tests", "--disable-nls", "--disable-cxx",
                     "--with-included-unistring", "--without-idn", "--without-tpm", "--without-tpm2",
                     "--disable-tools", "--with-default-trust-store-file=/etc/ssl/certs/ca-certificates.crt",
                     "--libdir=/usr/lib"),
            pattern="usr/lib/libgnutls.so.30"),
    Package("krb5", ("glibc", "openssl", "e2fsprogs"), kind="custom"),
    Package("mpfr", ("glibc", "gmp"), kind="cross-autotools",
            options=("--disable-static", "--libdir=/usr/lib"), pattern="usr/lib/libmpfr.so.6"),
    Package("libbytesize", ("glibc", "glib", "gmp", "mpfr", "pcre2"), kind="cross-autotools",
            options=("--disable-static", "--without-python3", "--without-gtk-doc", "--without-tools",
                     "--disable-nls", "--libdir=/usr/lib"),
            pattern="usr/lib/libbytesize.so.1"),
    Package("lvm2", ("glibc", "eudev", "util-linux"), kind="custom"),
    Package("libnvme", ("glibc", "json-c", "openssl"),
            options=("-Dpython=disabled", "-Dopenssl=enabled", "-Dlibdbus=disabled", "-Dkeyutils=disabled",
                     "-Djson-c=enabled", "-Ddocs=false", "-Dtests=false", "-Dexamples=false", "-Dliburing=disabled"),
            pattern="libnvme.so.1*"),
    Package("popt", ("glibc",), kind="cross-autotools", options=("--disable-static", "--disable-nls",
            "--libdir=/usr/lib"), pattern="usr/lib/libpopt.so.0"),
    Package("cryptsetup", ("glibc", "lvm2", "json-c", "openssl", "util-linux", "popt"), kind="cross-autotools",
            options=("--disable-static", "--disable-asciidoc", "--disable-ssh-token", "--disable-external-tokens",
                     "--disable-cryptsetup", "--disable-veritysetup", "--disable-integritysetup",
                     "--disable-selinux", "--disable-nls", "--with-crypto_backend=openssl", "--libdir=/usr/lib",
                     "--with-tmpfilesdir=no"),
            pattern="usr/lib/libcryptsetup.so.12"),
    Package("keyutils", ("glibc",), kind="custom"),
    Package("libblockdev", ("glibc", "glib", "eudev", "kmod", "cryptsetup", "lvm2", "util-linux", "e2fsprogs",
                            "libbytesize", "libnvme", "keyutils"), introspect=True, kind="cross-autotools",
            options=("--disable-static", "--without-btrfs", "--without-lvm", "--without-lvm_dbus", "--without-mpath",
                     "--without-nvdimm", "--without-smart", "--without-smartmontools", "--without-escrow",
                     "--without-python3", "--without-tools", "--without-gtk-doc", "--disable-tests", "--without-s390",
                     "--with-crypto", "--with-dm", "--with-loop", "--with-mdraid", "--with-part", "--with-fs",
                     "--with-swap", "--with-nvme", "--disable-introspection", "--libdir=/usr/lib", "--sysconfdir=/etc"),
            pattern="usr/lib/libblockdev.so.3"),
    Package("udisks", ("glibc", "glib", "polkit", "libgudev", "eudev", "libblockdev", "util-linux", "elogind",
                       "libbytesize", "cryptsetup", "libnvme", "lvm2", "e2fsprogs"), introspect=True,
            kind="cross-autotools",
            options=("--disable-static", "--disable-man", "--disable-gtk-doc", "--disable-acl", "--disable-lvm2",
                     "--disable-iscsi", "--disable-btrfs", "--disable-smart", "--disable-lsm", "--enable-fhs-media",
                     "--with-systemdsystemunitdir=no", "--with-udevdir=/usr/lib/udev", "--disable-introspection",
                     "--disable-nls", "--libdir=/usr/lib", "--sysconfdir=/etc", "--localstatedir=/var"),
            pattern="usr/lib/libudisks2.so.0"),
    Package("cups", ("glibc", "openssl", "zlib"), kind="custom",
            patches=(("cups/dest.c", "d6fd6b113c0425ec86819ad6ff35108da423969c0082206a3e2d9761d7c5e597",
                      "16cd58d964fc0c106fd137351b5467b13665a8b66e3de1f3aa1eb5383415b38f",
                      "  cups_dest_t\t*dests;\t\t\t/* Destinations */\n  char\t\tdef_name[1024],",
                      "  cups_dest_t\t*dests;\t\t\t/* Destinations */\n"
                      "  int\t\tnum_local;\t\t/* Number of local cupsd queues */\n"
                      "  cups_dest_t\t*local_dests;\t\t/* Local cupsd queues */\n"
                      "  char\t\tdef_name[1024],",
                      "Without DNS-SD, CUPS 2.4.20 uses num_local/local_dests that only the DNS-SD variant of "
                      "_cups_getdata_t declares; add them to the plain variant (the code initialises them)."),)),
    Package("tecla", ("glibc", "gtk", "libadwaita", "wayland", "libxkbcommon"), kind="data",
            pattern="usr/bin/tecla"),
    Package("gnome-control-center",
            ("glibc", "glib", "gtk", "libadwaita", "accountsservice", "colord", "gcr", "gnome-desktop",
             "gnome-bluetooth", "gnome-settings-daemon", "gnome-online-accounts", "gsettings-desktop-schemas",
             "libgudev", "ibus", "json-glib", "networkmanager", "libnma", "pulseaudio", "libsoup", "libwacom",
             "libxml2", "modemmanager", "polkit", "libpwquality", "upower", "libepoxy", "libgtop", "geoclue",
             "tecla", "cups", "colord-gtk", "gnutls", "krb5", "udisks", "libsecret"),
            # introspect: the build looks up polkit's gettext ITS files in XDG_DATA_DIRS, which the introspection
            # set-up points at the sysroot.
            kind="data", introspect=True,
            patches=(
                ("panels/meson.build", "bffe064524ca0e845c5bddf4d64dad0077e23a0772d6dc9daf7647ac666fb709",
                 "2b23328968e5666bec1b353dc18aaf3aa53679b826fe4731c95203069c7725d2",
                 "  'printers',\n", "", PRINTERS_WHY),
                ("shell/cc-panel-loader.c", "98051b16395074463fbec5cf56a981272d5222b9f8c420a6c3fb10bbeea44722",
                 "27af4180cb1780bb3da07f7dd1463189a3e0ffc12da4af666628012c032558cd",
                 "extern GType cc_printers_panel_get_type (void);\n", "", PRINTERS_WHY),
                ("shell/cc-panel-loader.c", "27af4180cb1780bb3da07f7dd1463189a3e0ffc12da4af666628012c032558cd",
                 "b967bf510e9d777b72a1cb0cae125c376db07019061267b5266e46aaba8200d5",
                 '  PANEL_TYPE("printers",         cc_printers_panel_get_type,             NULL),\n', "",
                 PRINTERS_WHY),
            ),
            options=("-Ddocumentation=false", "-Dtests=false", "-Dsnap=false", "-Dmalcontent=false",
                     "-Dibus=true", "-Dlocation-services=enabled", "-Dprivileged_group=wheel"),
            pattern="usr/bin/gnome-control-center"),
    Package("lua", ("glibc",), kind="custom"),
    Package(
        "pipewire", ("glibc", "glib", "dbus", "alsa-lib", "eudev", "elogind", "libsndfile", "readline", "ncurses"),
        options=("-Dsession-managers=[]", "-Dlibsystemd=disabled", "-Dsystemd-system-service=disabled",
                 "-Dsystemd-user-service=disabled", "-Dlogind=enabled", "-Dlogind-provider=libelogind",
                 "-Dpipewire-alsa=enabled", "-Dpipewire-jack=disabled", "-Dpipewire-v4l2=disabled", "-Djack=disabled",
                 "-Dbluez5=disabled", "-Dlibcamera=disabled", "-Dvulkan=disabled", "-Dx11=disabled",
                 "-Dx11-xfixes=disabled", "-Dlibpulse=disabled", "-Droc=disabled", "-Davahi=disabled",
                 "-Decho-cancel-webrtc=disabled", "-Dlibusb=disabled", "-Draop=disabled", "-Dlv2=disabled",
                 "-Dgstreamer=disabled", "-Dexamples=disabled", "-Dtests=disabled", "-Dman=disabled",
                 "-Ddocs=disabled", "-Dsdl2=disabled", "-Dlibffado=disabled", "-Dopus=disabled",
                 "-Dflatpak=disabled", "-Dselinux=disabled", "-Dlibmysofa=disabled", "-Dsnap=disabled",
                 "-Dlibcanberra=disabled", "-Dsndfile=enabled", "-Dreadline=enabled", "-Ddbus=enabled",
                 "-Dalsa=enabled", "-Dudev=enabled", "-Dgsettings=disabled", "-Debur128=disabled",
                 "-Dbluez5-codec-lc3=disabled", "-Dcompress-offload=disabled", "-Dlegacy-rtkit=false",
                 "--sysconfdir=/etc"),
        pattern="libpipewire-0.3.so.0*",
    ),
    Package(
        "wireplumber", ("glibc", "glib", "pipewire", "lua", "elogind"),
        options=("-Dsystem-lua=true", "-Dsystem-lua-version=5.4", "-Dsystemd=disabled",
                 "-Dsystemd-system-service=false", "-Dsystemd-user-service=false", "-Ddoc=disabled",
                 "-Dintrospection=disabled", "-Dtests=false", "-Delogind=enabled", "-Dmodules=true",
                 "-Ddaemon=true", "-Dtools=true", "--sysconfdir=/etc"),
        pattern="libwireplumber-0.5.so.0*",
    ),
    # --- Real apps: GNOME Console, Text Editor ---
    Package("lz4", ("glibc",), kind="custom"),
    Package("libfyaml", ("glibc",), kind="cross-autotools", options=("--disable-static", "--libdir=/usr/lib"),
            pattern="usr/lib/libfyaml.so.0"),
    Package("curl", ("glibc", "openssl", "zlib"), kind="cross-autotools",
            options=("--disable-static", "--with-openssl", "--with-ca-path=/etc/ssl/certs", "--without-libpsl",
                     "--without-brotli", "--without-zstd", "--without-libidn2", "--without-nghttp2",
                     "--disable-ldap", "--disable-manual", "--libdir=/usr/lib"),
            pattern="usr/lib/libcurl.so.4"),
    Package("libxmlb", ("glibc", "glib", "xz"),
            options=("-Dgtkdoc=false", "-Dintrospection=false", "-Dtests=false", "-Dstemmer=false", "-Dcli=false",
                     "-Dlzma=enabled", "-Dzstd=disabled"),
            pattern="libxmlb.so.2*"),
    Package("appstream", ("glibc", "glib", "libxml2", "libfyaml", "curl", "wayland", "libxmlb"),
            patches=(("data/meson.build", "5f853447bb084500e0d50ef2de7a4f0f2b56c54890ff77fce2731e6ad8d68fea",
                      "cc220abf8375ed19b5f47a2374f9a2c4eeb4bf30c7a84337c448dfc4a61ce2f8",
                      "if meson.is_cross_build()\n    dependency('appstream'",
                      "if false  # Meldra: run the just-built appstreamcli through the cross exe_wrapper\n"
                      "    dependency('appstream'",
                      "Cross builds ask for a native appstreamcli only to add release notes to AppStream's own "
                      "metainfo; the cross file's exe_wrapper runs the appstreamcli this build produces instead."),),
            options=("-Dsystemd=false", "-Dstemming=false", "-Dvapi=false", "-Dqt=false", "-Dcompose=false",
                     "-Dapt-support=false", "-Dgir=false", "-Ddocs=false", "-Dapidocs=false", "-Dinstall-docs=false",
                     "-Dman=false", "-Dzstd-support=false", "-Dbash-completion=false", "-Dtools=true"),
            pattern="libappstream.so.5*"),
    Package("libadwaita", ("glibc", "glib", "gtk", "fribidi", "appstream"),
            introspect=True,
            options=("-Dexamples=false", "-Dtests=false", "-Dvapi=false", "-Dintrospection=enabled",
                     "-Ddocumentation=false"),
            pattern="libadwaita-1.so.0*"),
    Package("gtksourceview", ("glibc", "glib", "gtk", "fribidi", "libxml2", "pcre2", "fontconfig", "pango"),
            introspect=True,
            options=("-Dvapi=false", "-Dbuild-testsuite=false", "-Dsysprof=false", "-Ddocumentation=false",
                     "-Dintrospection=enabled"),
            pattern="libgtksourceview-5.so.0*"),
    Package("vte", ("glibc", "glib", "gtk", "pango", "cairo", "fribidi", "icu", "pcre2", "lz4", "gcc-runtime"),
            introspect=True,
            options=("-Dgtk3=false", "-Dgtk4=true", "-Dgnutls=false", "-D_systemd=false", "-Dvapi=false",
                     "-Dgir=true", "-Ddocs=false", "-Dicu=true", "-Dglade=false"),
            pattern="libvte-2.91-gtk4.so.0*"),
    Package("libspelling", ("glibc", "glib", "gtk", "gtksourceview"), introspect=True,
            # No enchant yet (spelling dictionaries come later); Text Editor works without it.
            options=("-Denchant=disabled", "-Ddocs=false", "-Dsysprof=false", "-Dvapi=false",
                     "-Dintrospection=enabled"),
            pattern="libspelling-1.so.2*"),
    Package("libgtop", ("glibc", "glib"), kind="cross-autotools", post=libgtop_post,
            options=("--disable-static", "--disable-introspection", "--without-x", "--disable-gtk-doc",
                     "--disable-nls", "--libdir=/usr/lib"),
            pattern="usr/lib/libgtop-2.0.so.11"),
    Package("editorconfig", ("glibc", "pcre2"), kind="cmake",
            options=("-DBUILD_DOCUMENTATION=OFF", "-DBUILD_TESTING=OFF"),
            pattern="libeditorconfig.so.0*"),
    Package("gnome-console", ("glibc", "glib", "gtk", "vte", "libadwaita", "gsettings-desktop-schemas", "libgtop"),
            kind="data", options=("-Dtests=false",), pattern="usr/bin/kgx"),
    Package("gnome-text-editor", ("glibc", "glib", "gtk", "gtksourceview", "libadwaita", "libspelling",
                                  "gsettings-desktop-schemas", "editorconfig"),
            kind="data", options=("-Ddevelopment=false",), pattern="usr/bin/gnome-text-editor"),
    # --- Real apps: GNOME Files (Nautilus) ---
    Package("xz", ("glibc",), kind="cross-autotools",
            options=("--disable-static", "--disable-nls", "--disable-doc", "--libdir=/usr/lib"),
            pattern="usr/lib/liblzma.so.5"),
    Package("libarchive", ("glibc", "zlib", "xz", "expat"), kind="cross-autotools",
            options=("--disable-static", "--without-openssl", "--without-nettle", "--without-xml2",
                     "--without-lz4", "--without-zstd", "--without-bz2lib", "--without-libb2", "--with-lzma",
                     "--with-expat", "--disable-acl", "--disable-xattr", "--libdir=/usr/lib"),
            pattern="usr/lib/libarchive.so.13"),
    Package("exiv2", ("glibc", "expat", "zlib", "gcc-runtime"), kind="cmake",
            options=("-DEXIV2_ENABLE_INIH=OFF", "-DEXIV2_ENABLE_BROTLI=OFF", "-DEXIV2_BUILD_SAMPLES=OFF",
                     "-DEXIV2_BUILD_EXIV2_COMMAND=OFF", "-DEXIV2_ENABLE_NLS=OFF", "-DEXIV2_ENABLE_BMFF=ON",
                     "-DBUILD_TESTING=OFF", "-DEXIV2_BUILD_UNIT_TESTS=OFF", "-DEXIV2_ENABLE_WEBREADY=OFF"),
            pattern="libexiv2.so.28*"),
    Package("gexiv2", ("glibc", "glib", "exiv2", "gcc-runtime"), introspect=True,
            options=("-Dintrospection=true", "-Dvapi=false", "-Dpython3=false", "-Dtests=false", "-Dtools=false",
                     "-Dgtk_doc=false"),
            pattern="libgexiv2-0.16.so*"),
    Package("gnome-autoar", ("glibc", "glib", "gtk", "libarchive"),
            # The optional widget library is GTK 3-only; Nautilus uses the toolkit-independent archive API.
            options=("-Dgtk=false", "-Dintrospection=disabled", "-Dvapi=false", "-Dtests=false", "-Dgtk_doc=false"),
            pattern="libgnome-autoar-0.so.0*"),
    Package("tinysparql", ("glibc", "glib", "json-glib", "sqlite", "libxml2", "icu", "dbus", "libsoup"), introspect=True,
            options=("-Davahi=disabled", "-Dstemmer=disabled", "-Dunicode_support=icu",
                     "-Ddocs=false", "-Dman=false", "-Dtests=false", "-Dintrospection=enabled", "-Dvapi=disabled",
                     "-Dsystemd_user_services=false", "-Dbash_completion=false"),
            pattern="libtinysparql-3.0.so.0*"),
    Package("libportal", ("glibc", "glib", "gtk"),
            options=("-Dbackend-gtk4=enabled", "-Dbackend-gtk3=disabled", "-Dbackend-qt5=disabled",
                     "-Dbackend-qt6=disabled", "-Dintrospection=false", "-Dvapi=false", "-Ddocs=false",
                     "-Dtests=false"),
            pattern="libportal.so.1*"),
    Package("gstreamer", ("glibc", "glib"),
            options=("-Dintrospection=disabled", "-Dtests=disabled", "-Dexamples=disabled", "-Ddoc=disabled",
                     "-Dbenchmarks=disabled", "-Dlibunwind=disabled", "-Dlibdw=disabled", "-Dbash-completion=disabled",
                     "-Dnls=disabled", "-Dptp-helper=disabled", "-Dcheck=enabled", "-Dtools=enabled"),
            pattern="libgstreamer-1.0.so.0*"),
    Package("gst-plugins-base", ("glibc", "glib", "gstreamer", "zlib"),
            # Only the libraries Files needs (pbutils, tag, ...); optional plugins and their dependencies are off.
            options=("-Dauto_features=disabled", "-Dintrospection=disabled", "-Dexamples=disabled",
                     "-Dtests=disabled", "-Ddoc=disabled", "-Dnls=disabled", "-Dtools=disabled", "-Dorc=disabled"),
            pattern="libgstpbutils-1.0.so.0*"),
    Package("nautilus", ("glibc", "glib", "gtk", "libadwaita", "gnome-desktop", "gnome-autoar", "tinysparql",
                         "gexiv2", "glycin", "libportal", "gst-plugins-base", "icu", "wayland",
                         "gsettings-desktop-schemas"),
            kind="data",
            post=nautilus_post,
            options=("-Ddocs=false", "-Dextensions=false", "-Dintrospection=false", "-Dpackagekit=false",
                     "-Dselinux=disabled", "-Dcloudproviders=disabled", "-Dtests=none"),
            pattern="usr/bin/nautilus"),
    # --- A real shell: bash with the GNU and procps tools (busybox stays as /bin/sh for system scripts) ---
    Package(
        "ncurses", ("glibc",), kind="cross-autotools", post=ncurses_post,
        options=("--with-shared", "--without-normal", "--without-debug", "--without-ada", "--without-cxx-binding",
                 "--without-manpages", "--without-tests", "--enable-widec", "--enable-pc-files",
                 "--with-pkg-config-libdir=/usr/lib/pkgconfig", "--disable-db-install", "--disable-stripping",
                 "--with-build-cc=gcc", "--libdir=/usr/lib"),
        pattern="usr/lib/libncursesw.so.6",
    ),
    Package(
        "readline", ("glibc", "ncurses"), kind="cross-autotools",
        options=("--disable-static", "--with-curses", "--with-shared-termcap-library", "--libdir=/usr/lib"),
        pattern="usr/lib/libreadline.so.8",
    ),
    Package(
        "bash", ("glibc", "ncurses", "readline"), kind="cross-autotools", post=bash_post,
        options=("--with-installed-readline", "--without-bash-malloc", "--disable-nls", "--bindir=/usr/bin"),
        pattern="usr/bin/bash",
    ),
    Package(
        "coreutils", ("glibc",), kind="cross-autotools",
        # kill and uptime come from procps-ng.
        options=("--disable-nls", "--enable-no-install-program=kill,uptime", "--without-selinux", "--disable-acl",
                 "--disable-xattr", "--without-openssl", "--libexecdir=/usr/lib"),
        pattern="usr/bin/ls",
    ),
    Package("grep", ("glibc", "pcre2"), kind="cross-autotools", options=("--disable-nls",), pattern="usr/bin/grep"),
    Package("sed", ("glibc",), kind="cross-autotools", options=("--disable-nls", "--disable-acl"), pattern="usr/bin/sed"),
    Package("findutils", ("glibc",), kind="cross-autotools", options=("--disable-nls", "--localstatedir=/var/lib/locate"),
            pattern="usr/bin/find"),
    Package("gawk", ("glibc", "readline", "ncurses"), kind="cross-autotools",
            options=("--disable-nls", "--without-mpfr", "--libexecdir=/usr/lib"), pattern="usr/bin/gawk"),
    # configure cannot run its strcasecmp test when cross-compiling; glibc's strcasecmp is correct.
    Package("diffutils", ("glibc",), kind="cross-autotools", options=("--disable-nls",), pattern="usr/bin/diff",
            env={"gl_cv_func_strcasecmp_works": "yes"}),
    Package("less", ("glibc", "ncurses"), kind="cross-autotools", options=("--sysconfdir=/etc",), pattern="usr/bin/less"),
    Package("procps-ng", ("glibc", "ncurses"), kind="cross-autotools",
            options=("--disable-nls", "--without-systemd", "--disable-static", "--disable-kill", "--libdir=/usr/lib"),
            pattern="usr/bin/ps"),
    Package("fastfetch", ("glibc", "wayland", "libdrm", "dbus", "glib", "pcre2", "zlib", "libffi"), kind="custom"),
    Package("tllist", ("glibc",), kind="data", pattern="usr/include/tllist.h"),
    Package(
        "fcft", ("glibc", "tllist", "freetype", "fontconfig", "harfbuzz", "pixman"),
        options=("-Dgrapheme-shaping=disabled", "-Drun-shaping=disabled", "-Dsvg-backend=none", "-Ddocs=disabled",
                 "-Dtest-text-shaping=false"),
        pattern="libfcft.so.4.*",
    ),
    Package(
        "foot", ("glibc", "tllist", "fcft", "freetype", "fontconfig", "harfbuzz", "pixman", "wayland",
                 "wayland-protocols", "libxkbcommon", "dejavu-fonts"),
        kind="data", options=("-Ddocs=disabled", "-Dtests=false", "-Dterminfo=disabled", "-Dgrapheme-clustering=disabled",
                 "-Dime=false", "-Dutmp-backend=none", "-Dthemes=false"),
        pattern="usr/bin/foot",
    ),
    Package(
        "libnotify", ("glibc", "glib", "libffi", "pcre2", "zlib", "gdk-pixbuf", "libpng", "libtiff", "libjpeg-turbo"), introspect=True,
        options=("-Dtests=false", "-Dman=false", "-Dgtk_doc=false", "-Ddocbook_docs=disabled",
                 "-Dintrospection=enabled"),
        pattern="libnotify.so.4.*",
    ),
    Package(
        "gnome-settings-daemon", ("glibc", "glib", "libffi", "pcre2", "zlib", "gtk", "pango", "harfbuzz", "fribidi", "cairo", "pixman", "freetype", "fontconfig", "libpng", "libtiff", "libjpeg-turbo", "gdk-pixbuf", "graphene", "libepoxy", "libxkbcommon", "wayland", "wayland-protocols", "mesa", "libdrm", "expat", "libxml2", "libsoup", "libpsl", "nghttp2", "sqlite", "json-glib", "gnome-desktop", "gsettings-desktop-schemas", "libgweather",
                                 "geocode-glib", "geoclue", "libcanberra", "libltdl", "libvorbis", "libogg",
                                 "libnotify", "pulseaudio", "libsndfile", "polkit", "upower", "elogind", "libgudev",
                                 "eudev", "dbus", "libcap", "util-linux", "linux-pam", "libxcrypt", "iso-codes",
                                 "xkeyboard-config", "duktape", "libseccomp", "alsa-lib"),
        kind="data", introspect=True,
        patches=(("meson.build", "09634448049c63ab5b4760d286129d9e824d8627bc28c1ef61986ee33c967162",
                  "4ea740539cb5062c9e18f377cd0575ce56dec0c0aca8ee14cfc2f00ac0854fab",
                  "assert(enable_network_manager or not host_is_linux, 'NetworkManager support is not optional on Linux platforms')\n",
                  "",
                  "gnome-settings-daemon forces NetworkManager on Linux for its Sharing plugin. The code supports "
                  "HAVE_NETWORK_MANAGER=false (it is what other systems use); Meldra has no NetworkManager yet."),),
        options=("-Dsystemd=false", "-Delogind=true", "-Dalsa=true", "-Dgudev=true", "-Dcups=false",
                 "-Dnetwork_manager=false", "-Drfkill=true", "-Dsmartcard=false", "-Dusb-protection=false",
                 "-Dwwan=false", "-Dgcr3=false", "-Dcolord=false", "-Dxwayland=false", "-Dsystemd-units=false",
                 "-Dudev_dir=/usr/lib/udev", "--sysconfdir=/etc", "--localstatedir=/var"),
        pattern="usr/libexec/gsd-power",
    ),
    Package(
        "alsa-lib", ("glibc",), kind="autotools",
        options=("--disable-static", "--enable-shared", "--disable-python", "--disable-topology", "--disable-alisp",
                 "--without-debug", "--disable-aload", "--disable-old-symbols"),
        pattern="libasound.so.2.*",
    ),
    Package(
        "bubblewrap", ("glibc", "libcap"), kind="data",
        options=("-Dman=disabled", "-Dselinux=disabled", "-Dtests=false", "-Dbash_completion=disabled",
                 "-Dzsh_completion=disabled"),
        pattern="usr/bin/bwrap",
    ),
    Package("adwaita-icon-theme", ("glibc",), kind="data", pattern="usr/share/icons/Adwaita/index.theme"),
    Package("hicolor-icon-theme", ("glibc",), kind="data", pattern="usr/share/icons/hicolor/index.theme"),
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
                 "--enable-libuuid", "--enable-libfdisk", "--enable-libsmartcols",
                 # Disk tools the installer uses: partitioning, discovery, and wiping old signatures.
                 "--enable-fdisks=check", "--enable-lsblk", "--enable-blkid", "--enable-wipefs", "--disable-nls", "--without-python", "--without-systemd",
                 "--without-udev", "--without-ncurses", "--without-tinfo", "--without-readline", "--libdir=/usr/lib", "--enable-usrdir-path",
                 "--disable-static", "--enable-shared"),
        pattern="libmount.so.1.*",
    ),
    Package(
        "elogind", ("glibc", "libcap", "eudev", "dbus", "util-linux", "linux-pam", "libxcrypt"),
        options=("-Dpam=true", "-Dacl=disabled", "-Daudit=disabled", "-Dselinux=disabled",
                 "-Dman=disabled", "-Dhtml=disabled", "-Dtests=false",
                 # Without polkit support elogind refuses every privileged call from non-root users (power off,
                 # reboot, inhibitors); with it, polkit decides (active local sessions may power off).
                 "-Dpolkit=true",
                 "-Dsmack=false", "-Dcgroup-controller=elogind", "-Ddefault-hierarchy=unified",
                 "-Dutmp=false", "-Dpamlibdir=/usr/lib/security", "-Ddbuspolicydir=/usr/share/dbus-1/system.d",
                 "-Ddbussystemservicedir=/usr/share/dbus-1/system-services", "-Dudevrulesdir=/usr/lib/udev/rules.d"),
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
    scanner = prefix / "bin/g-ir-scanner"
    extension = prefix / "lib/gobject-introspection/giscanner" / ("_giscanner" + sysconfig.get_config_var("EXT_SUFFIX"))
    if scanner.is_file():
        if extension.is_file():
            return prefix
        raise RuntimeError(
            f"cached gobject-introspection does not match {sys.version.split()[0]}; "
            "rebuild it with development headers for the current Python"
        )
    upstream = source(lock, cache, work / "host-gi-source")
    build_dir = work / "host-gi-build"
    subprocess.run([*desktop.MESON, "setup", "--wrap-mode=nodownload", str(build_dir), str(upstream), f"--prefix={prefix}",
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
        subprocess.run([*desktop.MESON, "setup", "--wrap-mode=nodownload", str(build_dir), str(upstream), f"--prefix={prefix}",
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


HOST_CARGO_C = Path("build/host-tools/cargo-c")


def ensure_host_cargo_c(lock: dict, cache: Path) -> None:
    """cargo-cbuild builds C-ABI libraries from Rust crates (librsvg). Upstream's static musl binary, verified
    against the pinned SHA-256, is a build-time host tool only; nothing from it is shipped."""
    import hashlib
    import tarfile
    prefix = HOST_CARGO_C.resolve()
    if not (prefix / "bin/cargo-cbuild").is_file():
        archive = cache / Path(lock["url"]).name
        if not archive.is_file():
            raise RuntimeError(f"cargo-c archive missing: {archive} (run tools/pin_source.py)")
        if hashlib.sha256(archive.read_bytes()).hexdigest() != lock["sha256"]:
            raise RuntimeError("cargo-c archive does not match its pinned checksum")
        (prefix / "bin").mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive) as bundle:
            for member in bundle.getmembers():
                if member.isfile() and "/" not in member.name:
                    bundle.extract(member, prefix / "bin", filter="data")
    os.environ["PATH"] = f"{prefix}/bin{os.pathsep}{os.environ['PATH']}"


HOST_SASSC = Path("build/host-tools/sassc")


def build_host_sassc(locks: dict, cache: Path, work: Path) -> None:
    """sassc compiles the Shell's SCSS theme at build time; it must run on the build machine."""
    prefix = HOST_SASSC.resolve()
    if not (prefix / "bin/sassc").is_file():
        libsass = source(locks["libsass"], cache, work / "host-libsass-source")
        sassc = source(locks["sassc"], cache, work / "host-sassc-source")
        subprocess.run(["make", "BUILD=static"], cwd=libsass, check=True)
        subprocess.run(["make", "BUILD=static", f"SASS_LIBSASS_PATH={libsass}"], cwd=sassc, check=True)
        (prefix / "bin").mkdir(parents=True, exist_ok=True)
        shutil.copy2(sassc / "bin/sassc", prefix / "bin/sassc")
    os.environ["PATH"] = f"{prefix}/bin{os.pathsep}{os.environ['PATH']}"


HOST_BLUEPRINT = Path("build/host-tools/blueprint-compiler")


def build_host_blueprint(lock: dict, cache: Path, work: Path) -> None:
    """Install the pinned native Blueprint compiler used to turn GNOME app .blp files into GtkBuilder XML."""
    prefix = HOST_BLUEPRINT.resolve()
    if not (prefix / "bin/blueprint-compiler").is_file():
        upstream = source(lock, cache, work / "host-blueprint-source")
        build_dir = work / "host-blueprint-build"
        subprocess.run([*desktop.MESON, "setup", "--wrap-mode=nodownload", str(build_dir), str(upstream),
                        f"--prefix={prefix}", "-Ddocs=false"], check=True)
        subprocess.run([*desktop.MESON, "install", "-C", str(build_dir)], check=True)
    site_packages = next((prefix / "lib").glob("python*/site-packages"))
    os.environ["PYTHONPATH"] = os.pathsep.join(
        (str(site_packages), os.environ.get("PYTHONPATH", ""))
    ).rstrip(os.pathsep)
    # The editor sandbox's Python lacks PyGObject; run this pure-Python host tool with the host interpreter,
    # which also has the Gtk/Adwaita typelibs Blueprint consults while validating UI definitions.
    wrapper_dir = work / "host-blueprint-bin"
    wrapper_dir.mkdir(exist_ok=True)
    wrapper = wrapper_dir / "blueprint-compiler"
    wrapper.write_text(
        "#!/bin/sh\n"
        f'exec flatpak-spawn --host env PYTHONPATH="{site_packages}" python3 '
        f'"{prefix}/bin/blueprint-compiler" "$@"\n'
    )
    wrapper.chmod(0o755)
    os.environ["PATH"] = f"{wrapper_dir}{os.pathsep}{os.environ['PATH']}"


HOST_LIBXCVT = Path("build/host-tools/libxcvt")


def build_host_libxcvt(lock: dict, cache: Path, work: Path) -> None:
    """Mutter runs `cvt` at build time to generate its default display-mode table."""
    prefix = HOST_LIBXCVT.resolve()
    if not (prefix / "bin/cvt").is_file():
        upstream = source(lock, cache, work / "host-libxcvt-source")
        build_dir = work / "host-libxcvt-build"
        subprocess.run([*desktop.MESON, "setup", "--wrap-mode=nodownload", str(build_dir), str(upstream), f"--prefix={prefix}",
                        "--libdir=lib"], check=True)
        subprocess.run([*desktop.MESON, "install", "-C", str(build_dir)], check=True)
    os.environ["PATH"] = f"{prefix}/bin{os.pathsep}{os.environ['PATH']}"
    # The private-prefix cvt has no rpath for its own libxcvt.
    os.environ["LD_LIBRARY_PATH"] = f"{prefix}/lib{os.pathsep}{os.environ.get('LD_LIBRARY_PATH', '')}".rstrip(os.pathsep)


def target_tool_wrappers(pkg: Package):
    """Wrap programs that a build runs (for example `dconf compile`) so they execute from the target sysroot
    through the target loader. Returns a cross-file hook that writes the wrappers and puts them on PATH."""
    def hook(original):
        def make(work: Path, sysroot: Path) -> Path:
            path = original(work, sysroot)
            root = sysroot.resolve()
            tools = work / "target-tools"
            tools.mkdir(exist_ok=True)
            for name, program in pkg.target_tools.items():
                wrapper = tools / name
                wrapper.write_text(
                    f'#!/bin/sh\nexport GCONV_PATH="{root}/usr/lib64/gconv" GSETTINGS_BACKEND=memory\n'
                    f'exec "{root}/lib64/ld-linux-x86-64.so.2" --library-path '
                    f'"{root}/lib64:{root}/usr/lib64:{root}/usr/lib:{root}/usr/lib/pulseaudio" '
                    f'"{root}/{program.lstrip("/")}" "$@"\n')
                wrapper.chmod(0o755)
            os.environ["PATH"] = f"{tools}{os.pathsep}{os.environ['PATH']}"
            return path
        return make
    return hook


def host_python_wrapper(work: Path) -> None:
    """A python3 on PATH that sends only scripts needing PyGObject (the build sandbox lacks it) to the host.

    Every other script, including g-ir-scanner whose compiled extension matches this Python, keeps running here."""
    directory = work / "host-python"
    directory.mkdir(exist_ok=True)
    wrapper = directory / "python3"
    wrapper.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        '  *gen_locations_variant.py) exec flatpak-spawn --host python3 "$@" ;;\n'
        "esac\n"
        f'exec {sys.executable} "$@"\n')
    wrapper.chmod(0o755)
    os.environ["PATH"] = f"{directory}{os.pathsep}{os.environ['PATH']}"


def build_host_blueprint(lock: dict, cache: Path, work: Path) -> None:
    """blueprint-compiler (pinned source) as a build-time tool. It imports PyGObject, which this build sandbox
    lacks, so it runs on the host; GI_TYPELIB_PATH (the target sysroot's typelibs) is passed through."""
    root = source(lock, cache, work / "host-blueprint")
    # Its own `meson install` stamps the version in; this uninstalled copy gets the same stamp.
    main_py = root / "blueprintcompiler/main.py"
    main_py.write_text(main_py.read_text().replace('VERSION = "uninstalled"', f'VERSION = "{lock["version"]}"', 1))
    tools = work / "host-blueprint-bin"
    tools.mkdir()
    wrapper = tools / "blueprint-compiler"
    wrapper.write_text('#!/bin/sh\n'
                       'exec flatpak-spawn --host --env=GI_TYPELIB_PATH="$GI_TYPELIB_PATH" '
                       f'python3 "{root}/blueprint-compiler.py" "$@"\n')
    wrapper.chmod(0o755)
    os.environ["PATH"] = f"{tools}{os.pathsep}{os.environ['PATH']}"


def host_program_wrappers(work: Path, names: tuple[str, ...]) -> None:
    directory = work / "host-programs"
    directory.mkdir(exist_ok=True)
    for name in names:
        wrapper = directory / name
        wrapper.write_text(f'#!/bin/sh\nexec flatpak-spawn --host {name} "$@"\n')
        wrapper.chmod(0o755)
    os.environ["PATH"] = f"{directory}{os.pathsep}{os.environ['PATH']}"
    # Meson writes its probe files to the temp directory; the sandbox's /tmp is invisible to the host, so
    # put it in the shared build tree for programs that run on the host.
    shared_tmp = work / "tmp"
    shared_tmp.mkdir(exist_ok=True)
    os.environ["TMPDIR"] = str(shared_tmp)


def meson_build(pkg: Package, lock: dict, cache: Path, work: Path, output: Path) -> None:
    if pkg.host_python:
        host_python_wrapper(work)
    if pkg.host_programs:
        host_program_wrappers(work, pkg.host_programs)
    original_source = desktop.source
    if pkg.patches:
        def patched_source(lock_entry: dict, cache_dir: Path, destination: Path) -> Path:
            top = original_source(lock_entry, cache_dir, destination)
            for relative, before, after, old, new, why in pkg.patches:
                apply_recorded_patch(top / relative, before, after, old, new, why)
            return top
        desktop.source = patched_source
    try:
        _meson_build_inner(pkg, lock, cache, work, output)
    finally:
        desktop.source = original_source


def _meson_build_inner(pkg: Package, lock: dict, cache: Path, work: Path, output: Path) -> None:
    original = desktop.cross_file
    if pkg.target_tools:
        desktop.cross_file = target_tool_wrappers(pkg)(desktop.cross_file)
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




def scratch_dir() -> str:
    """build/tmp, also exported as TMPDIR so Kiln's own extraction stays off the RAM disk."""
    path = Path("build/tmp").resolve()
    path.mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = str(path)
    return str(path)


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
    # This build machine's gcc passes its own /usr/lib/x86_64-linux-gnu (the build machine's glibc and every
    # library installed there) to the linker as a library directory, so target links silently picked up the
    # host's libc.so, crt files, and optional libraries Meldra does not ship (libselinux, libtinfow). For any
    # compile with --sysroot=, a specs file replaces gcc's library directory list with: gcc's own directory
    # (libgcc), a directory holding only the GCC runtime libraries (libstdc++ and friends, shipped as
    # gcc-runtime), and the sysroot. -B makes the start files (crt1.o, crti.o) come from the sysroot too.
    # Native compiles (no --sysroot) are untouched.
    gcc_dir = Path(subprocess.run(["gcc", "-print-libgcc-file-name"], capture_output=True, text=True,
                                  check=True).stdout.strip()).parent
    runtime_dir = bin_dir / "gcc-runtime"
    runtime_dir.mkdir()
    stdcxx = Path(subprocess.run(["gcc", "-print-file-name=libstdc++.so"], capture_output=True, text=True,
                                 check=True).stdout.strip()).resolve().parent
    for pattern in ("libgcc_s.*", "libstdc++.*", "libsupc++.a", "libstdc++fs.a", "libstdc++exp.a", "libatomic.*", "libgomp.*",
                    "libitm.*", "libquadmath.*"):
        for library in sorted(stdcxx.glob(pattern)):
            (runtime_dir / library.name).symlink_to(library)
    specs = bin_dir / "meldra-target.specs"
    specs.write_text(f"*link_libgcc:\n-L{gcc_dir} -L{runtime_dir} -L%R/usr/lib64 -L%R/lib64 -L%R/usr/lib\n\n")
    for compiler in ("gcc", "g++"):
        real = next(p for p in os.environ["PATH"].split(os.pathsep) if (Path(p) / compiler).is_file())
        (bin_dir / compiler).write_text(
            "#!/bin/sh\nroot=\nfor a in \"$@\"; do case \"$a\" in --sysroot=*) root=${a#--sysroot=} ;; esac; done\n"
            "if [ -n \"$root\" ]; then\n"
            f"  exec {real}/{compiler} -specs={specs} -Wl,-nostdlib -B\"$root/usr/lib64/\" -B\"$root/usr/lib/\" \"$@\"\nfi\n"
            f"exec {real}/{compiler} \"$@\"\n")
        (bin_dir / compiler).chmod(0o755)
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
        # Programs run on the host (valac, vapigen, the host Python) cannot see the sandbox's /tmp, so a build
        # that uses them must work inside the project tree.
        selected = [p for p in PACKAGES if p.name in args.names] if args.names else []
        if any(p.host_programs or p.host_python for p in selected) and not args.keep:
            print("using build/work: a selected package runs programs on the host")
            args.keep = True
        if args.keep:
            shutil.rmtree(keep_dir, ignore_errors=True)
            keep_dir.mkdir(parents=True)
        with (contextlib.nullcontext(str(keep_dir)) if args.keep
              # /tmp is a small RAM disk here; sysroots with Mesa outgrow it, so work on the real disk.
              else tempfile.TemporaryDirectory(prefix="meldra-gnome-", dir=scratch_dir())) as directory:
            work = Path(directory)
            gentle_environment(work, max(1, args.jobs))
            desktop.MESON = pinned_meson(lock_data["host_tools"]["meson"], args.cache, work)
            build_host_wayland(lock_data["desktop"]["wayland"], args.cache, work)
            build_host_libxcvt(locks["libxcvt"], args.cache, work)
            if "gnome-control-center" in args.names:
                build_host_blueprint(pin(lock_data, "blueprint-compiler"), args.cache, work)
            if {"gnome-shell", "libadwaita"} & set(args.names):   # both compile their stylesheets with sassc
                build_host_sassc(locks, args.cache, work)
            if args.all or "nautilus" in args.names:
                build_host_blueprint(lock_data["host_tools"]["blueprint_compiler"], args.cache, work)
            if "librsvg" in args.names:
                ensure_host_cargo_c(lock_data["host_tools"]["cargo_c"], args.cache)
            # Build-time-only Python modules (templating), from pinned wheels.
            wheel_paths = []
            for tool in ("jinja2", "markupsafe", "docutils", "pyyaml"):
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
                if pkg.name == "gi-typelibs":
                    gi_typelibs_build(pkg, lock, args.cache, work, args.output)
                elif pkg.name == "libltdl":
                    ltdl_build(pkg, lock, args.cache, work, args.output)
                elif pkg.name == "meldra-session":
                    session_build(pkg, lock, args.cache, work, args.output)
                elif pkg.name == "gnome-shell":
                    gnome_shell_build(pkg, lock, args.cache, work, args.output)
                elif pkg.name == "icu":
                    icu_build(pkg, lock, args.cache, work, args.output)
                elif pkg.name == "nss":
                    nss_build(pkg, lock, args.cache, work, args.output)
                elif pkg.name == "nspr":
                    nspr_build(pkg, lock, args.cache, work, args.output)
                elif pkg.name == "duktape":
                    duktape_build(pkg, lock, args.cache, work, args.output)
                elif pkg.kind == "cross-autotools":
                    cross_autotools_build(pkg, lock, args.cache, work, args.output)
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
