#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Download a source archive and pin its version and SHA-256 in sources.lock.toml.

    pin_source.py gnome.dbus 1.16.2 https://.../dbus-1.16.2.tar.xz [--role TEXT]

The checksum is computed from what the server sent, so review the pin before
trusting it: compare against the project's published checksum when one exists.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import sys
import tomllib
import urllib.request


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", help="section.name, for example gnome.dbus")
    parser.add_argument("version")
    parser.add_argument("url")
    parser.add_argument("--role", default="")
    parser.add_argument("--file", help="cache file name, when the URL's last part is ambiguous (v1.2.tar.gz)")
    parser.add_argument("--sha256", help="expected checksum from the project; the pin fails on a mismatch")
    parser.add_argument("--lock", type=Path, default=Path("sources.lock.toml"))
    parser.add_argument("--cache", type=Path, default=Path("build/downloads"))
    args = parser.parse_args()
    section, _, key = args.name.partition(".")
    if not key:
        parser.error("name must look like section.name")
    key = key.replace("-", "_")
    lock = tomllib.loads(args.lock.read_text(encoding="utf-8"))
    if key in lock.get(section, {}):
        existing = lock[section][key]
        if existing["version"] == args.version and existing["url"] == args.url:
            print(f"{args.name} already pinned at {args.version}")
            return 0
        print(f"{args.name} is pinned at {existing['version']}; edit it by hand", file=sys.stderr)
        return 1
    args.cache.mkdir(parents=True, exist_ok=True)
    archive = args.cache / (args.file or Path(args.url).name)
    if not archive.is_file():
        part = archive.with_suffix(archive.suffix + ".part")
        with urllib.request.urlopen(args.url, timeout=120) as response, part.open("wb") as out:
            while chunk := response.read(1 << 20):
                out.write(chunk)
        part.replace(archive)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if args.sha256 and digest != args.sha256.lower():
        archive.unlink()
        print(f"checksum mismatch for {args.url}: expected {args.sha256}, got {digest}", file=sys.stderr)
        return 1
    with args.lock.open("a", encoding="utf-8") as stream:
        stream.write(f'\n[{section}.{key}]\nversion = "{args.version}"\nurl = "{args.url}"\nsha256 = "{digest}"\n')
        if args.file:
            stream.write(f'file = "{args.file}"\n')
        if args.role:
            stream.write(f'role = "{args.role}"\n')
    print(f"pinned {args.name} {args.version} sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
