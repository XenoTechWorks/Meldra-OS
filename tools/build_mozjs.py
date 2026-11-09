#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Build SpiderMonkey (libmozjs-140) from the pinned Firefox ESR source, natively, as Kiln packages.

    build_mozjs.py [--jobs N] [--work DIR] [--output DIR]

Why native: Mozilla's Rust build links and *runs* host-side tools (build scripts, proc-macros). In a
custom-glibc sysroot those tools would be linked against a newer glibc than the build machine has and
fail to start. Building against the build machine's own (older) glibc avoids that, and the result runs
unchanged on Meldra's newer glibc, because glibc is backward compatible.

The C++ runtime (libstdc++, libgcc_s) is shipped as its own declared package, "gcc-runtime", which the
mozjs package lists as a dependency. Nothing is patched in the Mozilla tree.

Intended for CI (see .github/workflows/build-mozjs.yml); --jobs defaults to 2 for anyone running it on
a machine that must stay cool.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kiln.cli import build
from tools.build_graphics_packages import manifest, source


def run(*command: str, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)


def gcc_runtime_files() -> list[Path]:
    """The compiler's own libstdc++ and libgcc_s, resolved to real files."""
    found = []
    for name in ("libstdc++.so.6", "libgcc_s.so.1"):
        path = subprocess.run(["gcc", f"-print-file-name={name}"], capture_output=True, text=True,
                              check=True).stdout.strip()
        if not os.path.isabs(path):
            raise RuntimeError(f"gcc cannot find {name}")
        found.append(Path(path).resolve())
    return found


def package_gcc_runtime(output: Path, work: Path) -> tuple[str, str]:
    version = subprocess.run(["gcc", "-dumpfullversion"], capture_output=True, text=True,
                             check=True).stdout.strip()
    stage = work / "gcc-runtime/package/root"
    lib = stage / "usr/lib"
    lib.mkdir(parents=True)
    for real in gcc_runtime_files():
        target = lib / real.name
        shutil.copy2(real, target)
        soname = {"libstdc++": "libstdc++.so.6", "libgcc_s": "libgcc_s.so.1"}[real.name.split(".so")[0]]
        if target.name != soname:
            (lib / soname).symlink_to(target.name)
    notes = stage / "usr/share/doc/gcc-runtime"
    notes.mkdir(parents=True)
    (notes / "README").write_text(
        f"libstdc++ and libgcc_s from GCC {version}, distributed under the GNU GPL v3 with the\n"
        "GCC Runtime Library Exception. Declared as a dependency by packages built with GCC's C++.\n")
    archive = output / f"gcc-runtime-{version}.kiln"
    build(manifest(stage, "gcc-runtime", version, ("glibc",)), archive)
    return version, str(archive)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=Path("sources.lock.toml"))
    parser.add_argument("--cache", type=Path, default=Path("build/downloads"))
    parser.add_argument("--output", type=Path, default=Path("build/packages"))
    parser.add_argument("--work", type=Path, default=Path("build/mozjs-work"))
    parser.add_argument("--jobs", type=int, default=int(os.environ.get("MELDRA_JOBS", "2")))
    args = parser.parse_args()

    lock = tomllib.loads(args.lock.read_text(encoding="utf-8"))["gnome"]["mozjs"]
    version = lock["version"]
    work = args.work.resolve()
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    args.output.mkdir(parents=True, exist_ok=True)

    print(f"gcc: {subprocess.run(['gcc', '--version'], capture_output=True, text=True).stdout.splitlines()[0]}")
    print(f"rustc: {subprocess.run(['rustc', '--version'], capture_output=True, text=True).stdout.strip()}")

    top = source(lock, args.cache, work / "source")   # verifies the pinned SHA-256
    obj = work / "obj"
    stage = work / "package/root"
    obj.mkdir()
    mozbuild_state = work / "mozbuild-state"
    mozbuild_state.mkdir()

    env = os.environ.copy()
    env.update({
        "MOZBUILD_STATE_PATH": str(mozbuild_state),
        "MACH_NO_UV": "1",
        "PYTHON3": "python3",
        # Keep build directories out of the binaries (the package audit rejects them).
        "CFLAGS": f"-O2 -ffile-prefix-map={top}=. -ffile-prefix-map={obj}=. -fdebug-prefix-map={top}=.",
        "CXXFLAGS": f"-O2 -ffile-prefix-map={top}=. -ffile-prefix-map={obj}=. -fdebug-prefix-map={top}=.",
        "RUSTFLAGS": f"--remap-path-prefix={top}=. --remap-path-prefix={obj}=.",
        "CARGO_BUILD_JOBS": str(args.jobs),
    })
    # js/src/configure (not the top-level one) forces the standalone JS project. Run from an empty
    # object directory outside the source tree.
    run(str(top / "js/src/configure"), "--prefix=/usr", "--enable-optimize", "--disable-debug",
        # Do NOT pass --disable-js-shell: js/src/moz.build only adds the "rust" directory (the jsrust
        # library the engine needs) when the shell is enabled. The shell is dropped after install.
        "--disable-tests", "--enable-shared-js", "--disable-jemalloc",
        "--without-system-icu", "--without-system-nspr", "--without-system-zlib",
        cwd=obj, env=env)
    run("make", f"-j{args.jobs}", cwd=obj, env=env)
    run("make", "install", f"DESTDIR={stage}", cwd=obj, env=env)

    libs = sorted((stage / "usr/lib").glob("libmozjs-*.so*"))
    if not libs:
        raise RuntimeError("libmozjs was not installed")
    for path in libs:
        if path.is_file() and not path.is_symlink():
            run("strip", "--strip-unneeded", str(path))
    # The static archive, the shell and the -config script are not needed at runtime or by gjs.
    for extra in list((stage / "usr/lib").glob("*.a")) + list((stage / "usr/bin").glob("js*")):
        extra.unlink()

    runtime_version, _ = package_gcc_runtime(args.output, work)
    series = re.match(r"(\d+)", version).group(1)
    build(manifest(stage, "mozjs", version, ("glibc", "gcc-runtime")), args.output / f"mozjs-{version}.kiln")
    print(f"built mozjs {version} (SpiderMonkey {series}) and gcc-runtime {runtime_version}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print(f"build_mozjs: {error}", file=sys.stderr)
        raise SystemExit(1)
