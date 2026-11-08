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
