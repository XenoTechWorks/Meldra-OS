#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Pack an assembled Meldra root filesystem as a reproducible initramfs."""

from __future__ import annotations

import argparse
import gzip
import os
from pathlib import Path
import subprocess
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if not (args.root / "init").exists():
        parser.error(f"root has no init: {args.root}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    paths = [args.root, *sorted(args.root.rglob("*"), key=lambda p: str(p.relative_to(args.root)))]
    names = b".\0" + b"".join(
        str(path.relative_to(args.root)).encode() + b"\0" for path in paths[1:]
    )
    result = subprocess.run(
        ["cpio", "--null", "-o", "--format=newc", "--owner=0:0",
         "--reproducible", "--quiet"],
        cwd=args.root, input=names, capture_output=True, check=False,
    )
    if result.returncode:
        print(result.stderr.decode(errors="replace") or "cpio failed", file=sys.stderr)
        return 1
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "0"))
    with args.output.open("wb") as stream:
        with gzip.GzipFile(filename="", mode="wb", fileobj=stream, mtime=epoch) as compressed:
            compressed.write(result.stdout)
    print(f"packed {args.output} ({len(result.stdout)} uncompressed bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
