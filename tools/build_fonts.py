#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Package Meldra's pinned bootstrap desktop fonts."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import sys
import tempfile
import tomllib

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kiln.cli import build
from tools.build_graphics_packages import manifest, source


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=Path("sources.lock.toml"))
    parser.add_argument("--cache", type=Path, default=Path("build/downloads"))
    parser.add_argument("--output", type=Path, default=Path("build/packages"))
    args = parser.parse_args()
    lock = tomllib.loads(args.lock.read_text(encoding="utf-8"))["desktop"]["dejavu_fonts"]
    args.output.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="meldra-fonts-") as directory:
            work = Path(directory)
            upstream = source(lock, args.cache, work / "source")
            stage = work / "package/root"
            font_dir = stage / "usr/share/fonts/truetype/dejavu"
            font_dir.mkdir(parents=True)
            fonts = sorted((upstream / "ttf").glob("*.ttf"))
            if not fonts:
                raise RuntimeError("DejaVu archive contains no TrueType fonts")
            for font in fonts:
                shutil.copy2(font, font_dir / font.name)
            license_dir = stage / "usr/share/licenses/dejavu-fonts"
            license_dir.mkdir(parents=True)
            for name in ("LICENSE", "AUTHORS"):
                source_file = upstream / name
                if source_file.is_file():
                    shutil.copy2(source_file, license_dir / name)
            package = manifest(stage, "dejavu-fonts", lock["version"], ())
            archive = args.output / f"dejavu-fonts-{lock['version']}.kiln"
            build(package, archive)
    except (OSError, RuntimeError) as exc:
        print(f"build_fonts: {exc}", file=sys.stderr)
        return 1
    print(f"built {archive} with {len(fonts)} fonts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
