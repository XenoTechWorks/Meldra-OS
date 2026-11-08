#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
set -eu

log=${1:-build/reports/desktop-boot.log}
marker=build/reports/labwc-smoke.ok

if grep -Fq "hearth: start 30-labwc" "$log" \
    && grep -Fq "Opened client 1 on seat0" "$log" \
    && ! grep -Fq "hearth: 30-labwc exited" "$log" \
    && ! grep -Fq "Kernel panic" "$log"; then
    mkdir -p "$(dirname "$marker")"
    : >"$marker"
    echo "labwc-smoke: passed ($log)"
    exit 0
fi

echo "labwc-smoke: failed ($log)" >&2
tail -n 80 "$log" >&2
exit 1
