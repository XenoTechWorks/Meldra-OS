#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# Runs inside the session's D-Bus (started by start-gnome): the per-user audio services, then GNOME Shell.
# PipeWire is the audio server, WirePlumber manages devices and routing, and pipewire-pulse serves
# PulseAudio clients (GNOME's volume controls among them).
/usr/bin/pipewire &
/usr/bin/wireplumber &
/usr/bin/pipewire-pulse &
exec /usr/bin/gnome-shell --wayland --display-server
