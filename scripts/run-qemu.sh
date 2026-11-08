#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
set -eu

kernel=${MELDRA_KERNEL:-build/bzImage}
initramfs=${MELDRA_INITRAMFS:-build/meldra-initramfs.cpio.gz}
local_qemu=build/host-tools/qemu/bin/qemu-system-x86_64

if command -v qemu-system-x86_64 >/dev/null 2>&1; then
    qemu=qemu-system-x86_64
elif [ -x "$local_qemu" ]; then
    qemu=$local_qemu
else
    echo "run-qemu: qemu-system-x86_64 is required (system or $local_qemu)" >&2
    exit 1
fi
if [ ! -f "$kernel" ]; then
    echo "run-qemu: kernel not found at $kernel (set MELDRA_KERNEL)" >&2
    exit 1
fi
if [ ! -f "$initramfs" ]; then
    echo "run-qemu: initramfs not found at $initramfs; run 'make initramfs'" >&2
    exit 1
fi

exec "$qemu" \
    -machine q35,accel=kvm:tcg \
    -m 1024 \
    -smp 2 \
    -kernel "$kernel" \
    -initrd "$initramfs" \
    -append "console=ttyS0 rdinit=/init panic=1" \
    -nographic \
    -no-reboot
