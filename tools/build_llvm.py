#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Build LLVM and clang from the pinned llvm-project release, natively, as a Kiln package.

    build_llvm.py [--jobs N] [--work DIR] [--cache DIR] [--output DIR]

What it is for: Mesa's llvmpipe (fast software OpenGL/GLES, for machines without a supported GPU and for plain
VMs) and lavapipe (software Vulkan) need libLLVM; Intel's drivers (iris, ANV) later also need clang.

Why native, like mozjs: built against the build machine's own (older) glibc the result runs unchanged on Meldra's
newer glibc, and llvm-config, which Mesa's build runs, executes on the build machine as well. The C++ runtime
comes from the declared gcc-runtime package (built in the same CI run, from the same compiler).

Only what Meldra needs: the X86 and AMDGPU targets, one shared libLLVM (and libclang-cpp) that every tool links,
no tests, examples, benchmarks or docs, and no static archives in the package.

Intended for CI (see .github/workflows/build-llvm.yml); --jobs defaults to 2 for a machine that must stay cool.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kiln.cli import build
from tools.build_graphics_packages import manifest, source
from tools.build_mozjs import package_gcc_runtime, run


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lock", type=Path, default=Path("sources.lock.toml"))
    parser.add_argument("--cache", type=Path, default=Path("build/downloads"))
    parser.add_argument("--output", type=Path, default=Path("build/packages"))
    parser.add_argument("--work", type=Path, default=Path("build/llvm-work"))
    parser.add_argument("--jobs", type=int, default=int(os.environ.get("MELDRA_JOBS", "2")))
    args = parser.parse_args()

    lock = tomllib.loads(args.lock.read_text(encoding="utf-8"))["gnome"]["llvm"]
    version = lock["version"]
    major = version.split(".")[0]
    work = args.work.resolve()
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    args.output.mkdir(parents=True, exist_ok=True)
    print(f"gcc: {subprocess.run(['gcc', '--version'], capture_output=True, text=True).stdout.splitlines()[0]}")

    top = source(lock, args.cache, work / "source")   # verifies the pinned SHA-256
    obj = work / "obj"
    stage = work / "package/root"
    obj.mkdir()
    # Keep build directories out of the binaries (the package audit rejects them).
    prefix_map = f"-ffile-prefix-map={top}=. -ffile-prefix-map={obj}=."
    run("cmake", "-G", "Ninja", "-S", str(top / "llvm"), "-B", str(obj),
        "-DCMAKE_BUILD_TYPE=Release", "-DCMAKE_INSTALL_PREFIX=/usr", "-DCMAKE_INSTALL_LIBDIR=lib",
        f"-DCMAKE_C_FLAGS={prefix_map}", f"-DCMAKE_CXX_FLAGS={prefix_map}",
        "-DLLVM_ENABLE_PROJECTS=clang",
        "-DLLVM_TARGETS_TO_BUILD=X86;AMDGPU",
        "-DLLVM_BUILD_LLVM_DYLIB=ON", "-DLLVM_LINK_LLVM_DYLIB=ON", "-DCLANG_LINK_CLANG_DYLIB=ON",
        "-DLLVM_ENABLE_RTTI=ON",            # Mesa's clover/intel paths and its LLVM checks expect RTTI
        "-DLLVM_INCLUDE_TESTS=OFF", "-DLLVM_INCLUDE_EXAMPLES=OFF", "-DLLVM_INCLUDE_BENCHMARKS=OFF",
        "-DLLVM_INCLUDE_DOCS=OFF", "-DCLANG_INCLUDE_TESTS=OFF", "-DCLANG_INCLUDE_DOCS=OFF",
        "-DLLVM_BUILD_TOOLS=ON", "-DLLVM_INSTALL_UTILS=ON",
        # Optional host libraries Meldra would have to match: off, so the package needs only glibc and the
        # C++ runtime.
        "-DLLVM_ENABLE_ZLIB=OFF", "-DLLVM_ENABLE_ZSTD=OFF", "-DLLVM_ENABLE_LIBXML2=OFF",
        "-DLLVM_ENABLE_LIBEDIT=OFF", "-DLLVM_ENABLE_LIBPFM=OFF", "-DLLVM_ENABLE_FFI=OFF",
        "-DLLVM_ENABLE_BINDINGS=OFF", "-DLLVM_ENABLE_OCAMLDOC=OFF",
        "-DLLVM_DEFAULT_TARGET_TRIPLE=x86_64-unknown-linux-gnu",
        f"-DLLVM_PARALLEL_LINK_JOBS={max(1, min(args.jobs, 2))}")   # links take several GB each
    run("cmake", "--build", str(obj), "--parallel", str(args.jobs))
    env = {**os.environ, "DESTDIR": str(stage)}
    run("cmake", "--install", str(obj), "--strip", env=env)

    lib = stage / "usr/lib"
    for archive in sorted(lib.glob("*.a")):
        archive.unlink()   # static libraries: gigabytes, and nothing on Meldra links them
    shutil.rmtree(stage / "usr/share/opt-viewer", ignore_errors=True)
    dylib = next(lib.glob(f"libLLVM.so.{major}*"), None) or next(lib.glob("libLLVM-*.so"), None)
    if dylib is None:
        raise RuntimeError("libLLVM was not installed as a shared library")
    for required in ("usr/bin/llvm-config", "usr/bin/clang", f"usr/lib/libclang-cpp.so.{major}"):
        if not any(stage.glob(required + "*")):
            raise RuntimeError(f"expected file not installed: {required}")
    for path in sorted(stage.rglob("*")):
        if path.is_file() and not path.is_symlink() and path.stat().st_size < 64 << 20:
            data = path.read_bytes()
            # DESTDIR (stage) must never leak; the source and object trees are not installed anywhere.
            for leaked in (str(top), str(obj), str(stage)):
                if leaked.encode() in data:
                    if path.name == "llvm-config":
                        # It records its configure-time source/object roots for `--src-root`/`--obj-root`;
                        # every path it prints for linking is computed from where the binary sits.
                        print(f"note: llvm-config records build root {leaked} (informational only)")
                        continue
                    raise RuntimeError(f"build path {leaked} baked into {path.relative_to(stage)}")

    runtime_version, _ = package_gcc_runtime(args.output, work)
    build(manifest(stage, "llvm", version, ("glibc", "gcc-runtime")), args.output / f"llvm-{version}.kiln")
    print(f"built llvm {version} (with clang) and gcc-runtime {runtime_version}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print(f"build_llvm: {error}", file=sys.stderr)
        raise SystemExit(1)
