#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
set -eu

kernel=${MELDRA_KERNEL:-build/bzImage}
initramfs=${MELDRA_INITRAMFS:-build/meldra-desktop-initramfs.cpio.gz}
local_qemu=build/host-tools/qemu/bin/qemu-system-x86_64

if command -v qemu-system-x86_64 >/dev/null 2>&1; then
    qemu=qemu-system-x86_64
elif [ -x "$local_qemu" ]; then
    qemu=$local_qemu
else
    echo "run-desktop-preview: qemu-system-x86_64 is required" >&2
    exit 1
fi

for artifact in "$kernel" "$initramfs"; do
    if [ ! -f "$artifact" ]; then
        echo "run-desktop-preview: missing $artifact" >&2
        exit 1
    fi
done

exec "$qemu" \
    -machine q35,accel=kvm:tcg \
    -m 2048 \
    -smp 2 \
    -kernel "$kernel" \
    -initrd "$initramfs" \
    -append "console=tty0 console=ttyS0 rdinit=/init panic=1" \
    -device virtio-vga \
    -device virtio-keyboard \
    -device virtio-tablet \
    -serial mon:stdio \
    -no-reboot
