#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Check an assembled GNOME root without booting it: run its own programs through its own loader.

    check_gnome_rootfs.py --root build/rootfs-gnome

Programs run with the root's glibc, and the private library directories their rpaths name (which only
resolve inside the booted system) are added to the loader's search path. Nothing here needs a display.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

# Namespaces GNOME Shell's JavaScript and C code load, with the version each is pinned to.
NAMESPACES = (
    ("GLib", "2.0"), ("GObject", "2.0"), ("Gio", "2.0"), ("GModule", "2.0"), ("GIRepository", "3.0"),
    ("cairo", "1.0"), ("Pango", "1.0"), ("PangoCairo", "1.0"), ("HarfBuzz", "0.0"), ("GdkPixbuf", "2.0"),
    ("Graphene", "1.0"), ("Gtk", "4.0"), ("Gdk", "4.0"), ("Atk", "1.0"), ("Atspi", "2.0"),
    ("Meta", "18"), ("Clutter", "18"), ("Cogl", "18"), ("Mtk", "18"),
    ("St", "18"), ("Shell", "18"), ("Gvc", "1.0"), ("Shew", "0"),
    ("Polkit", "1.0"), ("PolkitAgent", "1.0"), ("Gcr", "4"), ("Gck", "2"),
    ("GnomeDesktop", "4.0"), ("Gly", "2"), ("GDesktopEnums", "3.0"), ("GUdev", "1.0"),
    ("Json", "1.0"), ("Colord", "1.0"),
)


def environment(root: Path, home: Path) -> dict[str, str]:
    typelibs = ":".join(str(root / d) for d in
                        ("usr/lib/girepository-1.0", "usr/lib/mutter-18", "usr/lib/gnome-shell",
                         "usr/lib/gnome-shell/girepository-1.0", "usr/lib/gjs/girepository-1.0"))
    return {"GI_TYPELIB_PATH": typelibs, "GCONV_PATH": f"{root}/usr/lib64/gconv", "GSETTINGS_BACKEND": "memory",
            "GSETTINGS_SCHEMA_DIR": f"{root}/usr/share/glib-2.0/schemas", "XDG_DATA_DIRS": f"{root}/usr/share",
            "HOME": str(home), "LANG": "C", "PATH": os.environ.get("PATH", "")}


def command(root: Path, program: str, *arguments: str) -> list[str]:
    library_path = ":".join(str(root / d) for d in
                            ("lib64", "usr/lib64", "usr/lib", "usr/lib/mutter-18", "usr/lib/gnome-shell",
                             "usr/lib/pulseaudio", "usr/lib/gjs"))
    return ["nice", "-n", "19", str(root / "lib64/ld-linux-x86-64.so.2"), "--library-path", library_path,
            str(root / program.lstrip("/")), *arguments]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    failures = 0
    with tempfile.TemporaryDirectory() as home:
        env = environment(root, Path(home))
        version = subprocess.run(command(root, "/usr/bin/gnome-shell", "--version"), capture_output=True, text=True,
                                 env=env, timeout=120)
        print(f"gnome-shell --version: {(version.stdout + version.stderr).strip()[:100]!r} (exit {version.returncode})")
        failures += version.returncode != 0
        script = "const out = [];\n" + "".join(
            f'try {{ imports.gi.versions["{n}"] = "{v}"; imports.gi[{n!r}]; out.push("ok   {n}-{v}"); }} '
            f'catch (e) {{ out.push("FAIL {n}-{v}: " + String(e.message).split("\\n")[0]); }}\n'
            for n, v in NAMESPACES) + 'print(out.join("\\n"));\n'
        result = subprocess.run(command(root, "/usr/bin/gjs", "-c", script), capture_output=True, text=True, env=env,
                                timeout=600)
        print(result.stdout.strip())
        if result.stderr.strip():
            print(result.stderr.strip()[:600])
        failures += result.stdout.count("FAIL ") + (result.returncode != 0)
    print(f"{failures} problem(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
