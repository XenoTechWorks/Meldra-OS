#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Build a bootable Meldra live ISO with GRUB.

With grub-mkrescue and xorriso the ISO boots on BIOS and UEFI. Without xorriso it falls back to grub-mkimage and
mkisofs, which produces a BIOS-only ISO (said so when it happens)."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile


GRUB_CFG = """set timeout=3
set default=0

menuentry 'Meldra OS GNOME preview' {
    linux /boot/bzImage console=tty0 console=ttyS0 rdinit=/init panic=1
    initrd /boot/meldra-gnome-initramfs.cpio.gz
}
"""


def build_bios_iso(tree: Path, output: Path) -> None:
    """Fallback for hosts with mkisofs but no xorriso."""
    mkimage, mkisofs = shutil.which("grub-mkimage"), shutil.which("mkisofs")
    modules = Path("/usr/lib/grub/i386-pc")
    if not mkimage or not mkisofs or not modules.is_dir():
        raise RuntimeError("need xorriso, or grub-mkimage + mkisofs + GRUB i386-pc modules")
    boot = tree / "boot/grub/i386-pc"
    boot.mkdir(parents=True)
    core = boot / "core.img"
    subprocess.run([mkimage, "-O", "i386-pc", "-p", "/boot/grub", "-o", str(core),
                    "biosdisk", "iso9660", "normal", "linux", "configfile", "search", "search_fs_file"], check=True)
    eltorito = boot / "eltorito.img"
    eltorito.write_bytes((modules / "cdboot.img").read_bytes() + core.read_bytes())
    subprocess.run([mkisofs, "-R", "-V", "MELDRA_GNOME", "-o", str(output),
                    "-b", "boot/grub/i386-pc/eltorito.img", "-no-emul-boot",
                    "-boot-load-size", "4", "-boot-info-table", str(tree)], check=True)
    hybrid = modules / "boot_hybrid.img"
    if hybrid.is_file():
        with output.open("r+b") as image:
            image.write(hybrid.read_bytes())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kernel", type=Path, default=Path("build/bzImage"))
    parser.add_argument("--initramfs", type=Path, default=Path("build/meldra-gnome-initramfs.cpio.gz"))
    parser.add_argument("--output", type=Path, default=Path("build/meldra-gnome.iso"))
    args = parser.parse_args()
    for artifact in (args.kernel, args.initramfs):
        if not artifact.is_file():
            parser.error(f"missing artifact: {artifact}")
    grub = shutil.which("grub-mkrescue")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="meldra-iso-") as temporary:
        tree = Path(temporary)
        (tree / "boot/grub").mkdir(parents=True)
        shutil.copy2(args.kernel, tree / "boot/bzImage")
        shutil.copy2(args.initramfs, tree / "boot/meldra-gnome-initramfs.cpio.gz")
        (tree / "boot/grub/grub.cfg").write_text(GRUB_CFG, encoding="utf-8")
        output = args.output.resolve()
        if grub and shutil.which("xorriso"):
            subprocess.run([grub, "-o", str(output), str(tree)], check=True)
        else:
            build_bios_iso(tree, output)
            print("note: xorriso not found; this ISO boots on BIOS only, not UEFI")
    print(f"built bootable ISO: {args.output} ({args.output.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
