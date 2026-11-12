#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Package three favorite wallpapers from verified historical GNOME releases."""

from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import tomllib
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kiln.cli import build


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def fetch(entry: dict, cache: Path) -> Path:
    archive = cache / Path(entry["url"]).name
    if not archive.is_file():
        with tempfile.NamedTemporaryFile(dir=cache, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            with urllib.request.urlopen(entry["url"], timeout=90) as response:
                shutil.copyfileobj(response, temporary)
        temporary_path.replace(archive)
    if digest(archive) != entry["sha256"]:
        raise RuntimeError(f"checksum mismatch: {archive}")
    return archive


def extract_file(archive: Path, version: str, relative: str, destination: Path) -> None:
    member_name = f"gnome-backgrounds-{version}/{relative}"
    with tarfile.open(archive, "r:*") as bundle:
        member = bundle.getmember(member_name)
        stream = bundle.extractfile(member)
        if stream is None or not member.isfile():
            raise RuntimeError(f"missing regular asset: {member_name}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("wb") as output:
            shutil.copyfileobj(stream, output)


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    lock = tomllib.loads((root / "sources.lock.toml").read_text(encoding="utf-8"))["desktop"]
    selections = (
        ("gnome_backgrounds_curves", "GNOME-Curves.png"),
        ("gnome_backgrounds_aqua", "GNOME-Aqua.jpg"),
        ("gnome_backgrounds_sunset", "Sunset.png"),
    )
    cache = root / "build/downloads"
    cache.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="meldra-gnome-classics-") as directory:
            package = Path(directory)
            backgrounds = package / "root/usr/share/backgrounds/gnome-classics"
            licenses = package / "root/usr/share/licenses/gnome-classic-favorites"
            for key, filename in selections:
                entry = lock[key]
                archive = fetch(entry, cache)
                extract_file(archive, entry["version"], entry["asset"], backgrounds / filename)
                extract_file(archive, entry["version"], "COPYING", licenses / f"COPYING-{entry['version']}")
            properties = package / "root/usr/share/gnome-background-properties/meldra-gnome-classics.xml"
            properties.parent.mkdir(parents=True, exist_ok=True)
            properties.write_text(
                "<?xml version=\"1.0\"?>\n<!DOCTYPE wallpapers SYSTEM \"gnome-wp-list.dtd\">\n"
                "<wallpapers>\n"
                "  <wallpaper><name>GNOME Curves</name><filename>/usr/share/backgrounds/gnome-classics/GNOME-Curves.png</filename><options>zoom</options></wallpaper>\n"
                "  <wallpaper><name>Aqua Foot</name><filename>/usr/share/backgrounds/gnome-classics/GNOME-Aqua.jpg</filename><options>zoom</options></wallpaper>\n"
                "  <wallpaper><name>Sunset</name><filename>/usr/share/backgrounds/gnome-classics/Sunset.png</filename><options>zoom</options></wallpaper>\n"
                "</wallpapers>\n",
                encoding="utf-8",
            )
            # The default background is Meldra's own (meldra-backgrounds); these are offered in Settings only.
            (package / "KILN.toml").write_text(
                "[package]\nname = \"gnome-classic-favorites\"\nversion = \"1.0.0\"\n"
                "architecture = \"any\"\ndependencies = [\"gnome-backgrounds\"]\n",
                encoding="utf-8",
            )
            output = root / "build/packages/gnome-classic-favorites-1.0.0.kiln"
            print(build(package, output))
    except (OSError, RuntimeError, KeyError, tarfile.TarError) as exc:
        print(f"build_gnome_classics: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
