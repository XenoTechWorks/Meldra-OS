#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Configure and build Meldra's pinned Linux baseline.

Produces build/bzImage and two Kiln packages in --packages:
  linux          the kernel image as /boot/vmlinuz (the installer copies it to the EFI system partition)
  linux-modules  /usr/lib/modules/<release> with modules.dep etc., for Wi-Fi, GPU and audio drivers that
                 load firmware from the root filesystem

--jobs defaults to MELDRA_JOBS or 2: the maintainer's machine has no CPU fan. CI passes $(nproc).
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tomllib


def run(command: list[str], *, cwd: Path | None = None) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, check=True)


def package_kernel(source: Path, output: Path, packages: Path, version: str) -> None:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from kiln.cli import build
    release = subprocess.run(["make", "-s", f"O={output}", "kernelrelease"], cwd=source, capture_output=True,
                             text=True, check=True).stdout.strip()
    work = output / "meldra-packages"
    shutil.rmtree(work, ignore_errors=True)
    packages.mkdir(parents=True, exist_ok=True)

    kernel = work / "linux"
    (kernel / "root/boot").mkdir(parents=True)
    shutil.copy2(output / "arch/x86/boot/bzImage", kernel / "root/boot/vmlinuz")
    (kernel / "root/boot/meldra-kernel-release").write_text(release + "\n", encoding="utf-8")
    write_manifest(kernel / "KILN.toml", "linux", version, ("linux-modules",))
    print(build(kernel, packages / f"linux-{version}.kiln"))

    modules = work / "linux-modules"
    stage = modules / "root"
    stage.mkdir(parents=True)
    run(["make", f"O={output}", f"INSTALL_MOD_PATH={stage / 'usr'}", "INSTALL_MOD_STRIP=1", "DEPMOD=true",
         "modules_install"], cwd=source)
    tree = stage / "usr/lib/modules" / release
    for link in ("build", "source"):
        (tree / link).unlink(missing_ok=True)
    # depmod from the build host writes the indexes for the target tree (module format is host-independent).
    if shutil.which("depmod") or Path("/sbin/depmod").exists():
        run([shutil.which("depmod") or "/sbin/depmod", "-b", str(stage / "usr"), release])
    else:
        raise RuntimeError("depmod (kmod) is required to index the modules")
    if not (tree / "modules.dep").is_file():
        raise RuntimeError("modules.dep was not written")
    write_manifest(modules / "KILN.toml", "linux-modules", version, ())
    print(build(modules, packages / f"linux-modules-{version}.kiln"))


def write_manifest(path: Path, name: str, version: str, dependencies: tuple[str, ...]) -> None:
    listed = ", ".join(f'"{item}"' for item in dependencies)
    path.write_text(f'[package]\nname = "{name}"\nversion = "{version}"\n'
                    f'description = "Linux kernel for Meldra"\ndependencies = [{listed}]\n', encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=Path("sources.lock.toml"))
    parser.add_argument("--output", type=Path, default=Path("build/linux"))
    parser.add_argument("--jobs", type=int, default=int(os.environ.get("MELDRA_JOBS", "2")))
    parser.add_argument("--packages", type=Path, default=Path("build/packages"))
    parser.add_argument("--configure-only", action="store_true")
    args = parser.parse_args()
    linux = tomllib.loads(args.lock.read_text(encoding="utf-8"))["linux"]
    source = (Path("upstream") / f"linux-{linux['version']}").resolve()
    if not (source / ".meldra-source").is_file():
        parser.error("verified kernel source missing; run tools/fetch_sources.py")
    if shutil.which("make") is None:
        parser.error("make is required")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    host_tools = Path("build/host-tools/bin").resolve()
    environment = os.environ.copy()
    if host_tools.is_dir():
        environment["PATH"] = f"{host_tools}:{environment['PATH']}"
    os.environ.update({"PATH": environment["PATH"]})
    run(["make", f"O={output}", "x86_64_defconfig"], cwd=source)
    merge = source / "scripts/kconfig/merge_config.sh"
    run([str(merge), "-m", "-O", str(output), str(output / ".config"),
         str(Path("kernel/config/x86_64.fragment").resolve())], cwd=source)
    run(["make", f"O={output}", "olddefconfig"], cwd=source)
    if not args.configure_only:
        run(["make", f"O={output}", f"-j{args.jobs}", "bzImage", "modules"], cwd=source)
        destination = Path("build/bzImage")
        shutil.copy2(output / "arch/x86/boot/bzImage", destination)
        print(f"built {destination}")
        package_kernel(source, output, args.packages, linux["version"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

