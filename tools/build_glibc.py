#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Build Meldra's pinned glibc target sysroot and Kiln package."""

from __future__ import annotations

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
from kiln.cli import build

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "sources.lock.toml"
CACHE = ROOT / "build/downloads"
SOURCE = ROOT / "build/host-src"
BUILD = ROOT / "build/glibc-build-2.44-uapi-v2"
SYSROOT = ROOT / "build/sysroots/glibc"
PACKAGE = ROOT / "build/packages"
LINUX_HEADERS = ROOT / "build/host-tools/musl/include"


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def fetch(lock: dict) -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    archive = CACHE / Path(lock["url"]).name
    if not archive.is_file():
        with tempfile.NamedTemporaryFile(dir=CACHE, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            with urllib.request.urlopen(lock["url"], timeout=60) as response:
                shutil.copyfileobj(response, temporary)
        temporary_path.replace(archive)
    if digest(archive) != lock["sha256"]:
        raise RuntimeError(f"checksum mismatch: {archive}")
    return archive


def extract(archive: Path, version: str) -> Path:
    target = SOURCE / f"glibc-{version}"
    marker = target / ".meldra-source"
    checksum = digest(archive)
    if marker.is_file() and marker.read_text(encoding="utf-8").strip() == checksum:
        return target
    if target.exists():
        raise RuntimeError(f"refusing unverified source directory: {target}")
    SOURCE.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:xz") as bundle:
        members = bundle.getmembers()
        for member in members:
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise RuntimeError(f"unsafe source member: {member.name}")
        bundle.extractall(SOURCE, members=members, filter="data")
    marker.write_text(checksum + "\n", encoding="utf-8")
    return target


def main() -> int:
    lock = tomllib.loads(LOCK.read_text(encoding="utf-8"))["runtime"]["glibc"]
    try:
        source = extract(fetch(lock), lock["version"])
        installed = SYSROOT / "lib64/libc.so.6"
        if not installed.is_file():
            BUILD.mkdir(parents=True, exist_ok=True)
            if not (BUILD / "config.status").is_file():
                subprocess.run(
                    [
                        str(source / "configure"), "--prefix=/usr", "--disable-werror",
                        "--enable-kernel=5.15", "--without-selinux",
                        f"--with-headers={LINUX_HEADERS}",
                    ],
                    cwd=BUILD,
                    check=True,
                )
            jobs = min(8, max(1, os.cpu_count() or 2))
            subprocess.run(["make", f"-j{jobs}"], cwd=BUILD, check=True)
            subprocess.run(["make", "install", f"DESTDIR={SYSROOT}"], cwd=BUILD, check=True)
        for header_tree in ("linux", "asm", "asm-generic"):
            source_headers = LINUX_HEADERS / header_tree
            if not source_headers.is_dir():
                raise RuntimeError(f"Linux UAPI headers missing: {source_headers}")
            shutil.copytree(
                source_headers,
                SYSROOT / "usr/include" / header_tree,
                dirs_exist_ok=True,
                symlinks=True,
            )
        with tempfile.TemporaryDirectory(prefix="meldra-glibc-package-") as temporary:
            package = Path(temporary)
            shutil.copytree(SYSROOT, package / "root", symlinks=True)
            (package / "KILN.toml").write_text(
                "[package]\nname = \"glibc\"\n"
                f'version = "{lock["version"]}"\narchitecture = "x86_64"\n'
                "dependencies = []\n",
                encoding="utf-8",
            )
            PACKAGE.mkdir(parents=True, exist_ok=True)
            print(build(package, PACKAGE / f'glibc-{lock["version"]}.kiln'))
    except (OSError, RuntimeError, subprocess.CalledProcessError, tarfile.TarError) as exc:
        print(f"build_glibc: {exc}", file=sys.stderr)
        return 1
    print(f"glibc {lock['version']} sysroot ready at {SYSROOT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
