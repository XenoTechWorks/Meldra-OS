#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Pin a GNOME module from download.gnome.org, verified against GNOME's published checksum.

    pin_gnome.py gtk 4.22          # newest stable 4.22.x
    pin_gnome.py mutter 50 --role "compositor"
"""
import argparse
import json
import re
import subprocess
import sys
import urllib.request

BASE = "https://download.gnome.org/sources"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("module")
    ap.add_argument("series", nargs="?", default="")
    ap.add_argument("--role", default="")
    args = ap.parse_args()
    cache = json.load(urllib.request.urlopen(f"{BASE}/{args.module}/cache.json", timeout=30))
    versions, files = cache[2][args.module], cache[1][args.module]
    # GNOME numbers its pre-releases x.y.90 and up (alpha/beta/rc); those are not stable releases.
    stable = [v for v in versions if re.fullmatch(r"[0-9.]+", v) and not re.search(r"\.9[0-9]$", v)]
    if args.series:
        stable = [v for v in stable if v == args.series or v.startswith(args.series + ".")]
    if not stable:
        print(f"no stable {args.module} {args.series}", file=sys.stderr)
        return 1
    version = max(stable, key=lambda v: [int(x) for x in v.split(".")])
    tar = files[version]["tar.xz"]
    sums = urllib.request.urlopen(f"{BASE}/{args.module}/{files[version]['sha256sum']}", timeout=30).read().decode()
    expected = next(line.split()[0] for line in sums.splitlines() if line.strip().endswith(tar.split("/")[-1]))
    name = args.module.replace("-", "_")
    return subprocess.call([sys.executable, "tools/pin_source.py", f"gnome.{name}", version,
                            f"{BASE}/{args.module}/{tar}", "--sha256", expected, "--role", args.role])


if __name__ == "__main__":
    raise SystemExit(main())
