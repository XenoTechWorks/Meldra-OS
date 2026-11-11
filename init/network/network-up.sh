#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# Bring up loopback and the first wired interface, then keep a DHCP lease on it (busybox udhcpc).
# Hearth runs this as a service with respawn=yes, so a lost link or an exited client is retried.
/bin/ip link set lo up
# Let udev finish renaming interfaces (eth0 -> enp0s1) so the name chosen below is the final one.
[ -x /usr/bin/udevadm ] && /usr/bin/udevadm settle --timeout=15
interface=""
tries=0
while [ -z "$interface" ] && [ "$tries" -lt 30 ]; do
    for path in /sys/class/net/*; do
        name=${path##*/}
        case "$name" in lo|sit*|tun*|wl*) continue ;; esac
        [ -e "$path/device" ] && interface=$name && break
    done
    [ -z "$interface" ] && sleep 1
    tries=$((tries + 1))
done
if [ -z "$interface" ]; then
    echo "network-up: no wired network interface found" >&2
    sleep 30
    exit 1
fi
# Wait until udev has finished with the device (it writes /run/udev/data/n<ifindex> after its rules, including
# any rename such as eth0 -> enp0s1), then use the device's final name, found by its index.
index=$(cat "/sys/class/net/$interface/ifindex")
tries=0
while [ ! -e "/run/udev/data/n$index" ] && [ "$tries" -lt 50 ]; do
    sleep 0.2
    tries=$((tries + 1))
done
for path in /sys/class/net/*; do
    [ "$(cat "$path/ifindex" 2>/dev/null)" = "$index" ] && interface=${path##*/}
done
/bin/ip link set "$interface" up
exec /bin/udhcpc -f -i "$interface" -s /usr/libexec/meldra/udhcpc-script -x hostname:meldra
