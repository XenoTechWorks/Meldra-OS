#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# udhcpc event handler: apply the lease to the interface, the default route and /etc/resolv.conf.
case "$1" in
    deconfig)
        /bin/ip -4 addr flush dev "$interface"
        /bin/ip link set "$interface" up
        ;;
    bound|renew)
        /bin/ip -4 addr flush dev "$interface"
        /bin/ip addr add "$ip/${mask:-24}" ${broadcast:+broadcast "$broadcast"} dev "$interface"
        /bin/ip route del default dev "$interface" 2>/dev/null
        for gateway in $router; do
            /bin/ip route add default via "$gateway" dev "$interface"
            break
        done
        temporary=/etc/resolv.conf.udhcpc
        : > "$temporary"
        [ -n "$domain" ] && echo "search $domain" >> "$temporary"
        for server in $dns; do echo "nameserver $server" >> "$temporary"; done
        /bin/mv "$temporary" /etc/resolv.conf
        echo "network: $interface $ip/${mask:-24} via ${router%% *} dns ${dns:-none}"
        ;;
esac
exit 0
