#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# Runs inside the logind session, as the unprivileged user, started by meldra-session.
# Meldra has no GPU driver in the preview image, so Mesa renders in software (kms_swrast on the DRM node).
export XDG_SESSION_TYPE=wayland
export XDG_CURRENT_DESKTOP=GNOME
export LIBGL_ALWAYS_SOFTWARE=1
export MESA_LOADER_DRIVER_OVERRIDE=kms_swrast
# No dconf or gvfs yet: keep GSettings in memory and use the plain local VFS.
export GSETTINGS_BACKEND=memory
export GIO_USE_VFS=local
export GIO_USE_VOLUME_MONITOR=unix
# The session bus is created here so the Shell and everything it activates share one.
exec /usr/bin/dbus-run-session -- /usr/bin/gnome-shell --wayland --no-x11
