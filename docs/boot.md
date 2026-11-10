# Booting Meldra OS

The first boot target is an x86_64 serial-console initramfs. It proves the chain
from a Linux baseline kernel into Hearth before storage, installation, or a
graphical session is introduced.

## Requirements

- a statically linked BusyBox binary
- a statically linked Hearth binary
- `cpio`, `gzip`, and Python 3 on the build host
- QEMU and a Linux `bzImage` for the smoke test

Build Hearth statically using a musl cross compiler or an equivalent static
toolchain, then create the image:

```sh
CC=musl-gcc LDFLAGS=-static make init
BUSYBOX=/path/to/static/busybox make initramfs
MELDRA_KERNEL=/path/to/bzImage scripts/run-qemu.sh
```

Successful boot reaches a serial login prompt and prints:

```text
Meldra OS userspace is alive
```

The tiny initramfs deliberately excludes Kiln and Python. Kiln belongs in the
persistent base root filesystem built during the next image stage.

## Bootable GNOME ISO

Once the GNOME initramfs exists, build a hybrid BIOS/UEFI live image with:

```sh
make gnome-iso
```

This writes `build/meldra-gnome.iso`. With `xorriso`, GRUB produces a hybrid
BIOS/UEFI image; on a host with only `mkisofs`, the builder produces a hybrid
BIOS image. GRUB loads the same Meldra kernel and GNOME initramfs used by the
direct QEMU workflow; it is a live preview image, not yet a persistent installer.

Boot it in QEMU through its bootloader (add `--display gtk` for a window):

```sh
python3 tools/meldra_vm.py start --iso build/meldra-gnome.iso
```

The release checks are `python3 tools/gnome_smoke.py` (Shell starts, draws, reacts to mouse and keyboard) and
`make gnome-power-smoke` (elogind poweroff and reboot stop the VM); they write `build/reports/gnome-smoke.ok` and
`build/reports/gnome-power.ok`. The live image keeps everything in RAM; nothing is saved across boots.
