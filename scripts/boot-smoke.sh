#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
set -eu

report_dir=build/reports
log=$report_dir/boot.log
marker=$report_dir/boot-smoke.ok
mkdir -p "$report_dir"

set +e
timeout 20s ./scripts/run-qemu.sh >"$log" 2>&1
qemu_status=$?
set -e

if grep -Fq "Meldra OS userspace is alive" "$log" \
    && ! grep -Fq "applet not found" "$log" \
    && ! grep -Fq "Kernel panic" "$log"; then
    : >"$marker"
    echo "boot-smoke: passed (QEMU status $qemu_status; expected timeout after successful boot)"
    exit 0
fi

echo "boot-smoke: failed; userspace marker was not observed (QEMU status $qemu_status)" >&2
tail -n 40 "$log" >&2
exit 1
