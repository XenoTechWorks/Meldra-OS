#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Fetch and verify Meldra's pinned upstream source archives."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import shutil
import tarfile
import tempfile
import tomllib
import urllib.request


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def safe_extract(archive: tarfile.TarFile, destination: Path) -> None:
    destination = destination.resolve()
    for member in archive.getmembers():
        target = (destination / member.name).resolve()
        if target != destination and destination not in target.parents:
            raise RuntimeError(f"unsafe archive member: {member.name}")
    archive.extractall(destination, filter="data")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=Path("sources.lock.toml"))
    parser.add_argument("--cache", type=Path, default=Path("build/downloads"))
    parser.add_argument("--destination", type=Path, default=Path("upstream"))
    args = parser.parse_args()
    lock = tomllib.loads(args.lock.read_text(encoding="utf-8"))["linux"]
    filename = Path(lock["url"]).name
    archive_path = args.cache / filename
    args.cache.mkdir(parents=True, exist_ok=True)
    if not archive_path.exists():
        print(f"downloading {lock['url']}")
        with tempfile.NamedTemporaryFile(dir=args.cache, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            with urllib.request.urlopen(lock["url"], timeout=60) as response:
                shutil.copyfileobj(response, temporary)
        if digest(temporary_path) != lock["sha256"]:
            temporary_path.unlink()
            raise SystemExit("download checksum mismatch")
        temporary_path.replace(archive_path)
    if digest(archive_path) != lock["sha256"]:
        raise SystemExit(f"cached checksum mismatch: {archive_path}")
    target = args.destination / f"linux-{lock['version']}"
    marker = target / ".meldra-source"
    if marker.is_file() and marker.read_text(encoding="utf-8").strip() == lock["sha256"]:
        print(f"verified {target}")
        return 0
    if target.exists():
        raise SystemExit(f"refusing to replace existing unverified source: {target}")
    args.destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "r:xz") as archive:
        safe_extract(archive, args.destination)
    marker.write_text(lock["sha256"] + "\n", encoding="utf-8")
    print(f"extracted and verified {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

