#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# Generic early userspace for both plain and LUKS Meldra roots.

set -eu
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH

mount -t devtmpfs devtmpfs /dev 2>/dev/null || true
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mkdir -p /run/cryptsetup /newroot

rootdev=""
tries=0
while [ -z "$rootdev" ] && [ "$tries" -lt 100 ]; do
    for event in /sys/class/block/*/uevent; do
        if grep -qx 'PARTNAME=meldra-root' "$event" 2>/dev/null; then
            rootdev="/dev/${event%/uevent}"
            rootdev="/dev/${rootdev##*/}"
            break
        fi
    done
    [ -n "$rootdev" ] || sleep 0.1
    tries=$((tries + 1))
done
[ -n "$rootdev" ] || { echo "Meldra: root partition not found" >/dev/console; exec sh </dev/console >/dev/console 2>&1; }

if cryptsetup isLuks "$rootdev"; then
    echo "Meldra encrypted root password:" >/dev/console
    # The early initramfs has no udev daemon. Let libdevmapper create the
    # mapping node itself instead of waiting on a udev semaphore forever.
    until DM_DISABLE_UDEV=1 cryptsetup open "$rootdev" meldra-root </dev/console >/dev/console 2>&1; do
        echo "Unlock failed; try again:" >/dev/console
    done
    rootdev=/dev/mapper/meldra-root
fi

mount -t ext4 -o rw "$rootdev" /newroot
# Carry the live kernel interfaces into the installed root. Leaving devtmpfs
# attached to the disposable initramfs can make switch_root fail as busy, and
# the installed Hearth services expect /dev, /proc, and /sys to be ready.
mount --move /dev /newroot/dev
mount --move /proc /newroot/proc
mount --move /sys /newroot/sys
exec switch_root /newroot /sbin/hearth
