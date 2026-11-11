#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# Runs inside the logind session, as the unprivileged user, started by greetd after login.
# Meldra has no GPU driver in the preview image, so Mesa renders in software (kms_swrast on the DRM node).
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
export XDG_SESSION_TYPE=wayland
export XDG_CURRENT_DESKTOP=GNOME
export LIBGL_ALWAYS_SOFTWARE=1
export MESA_LOADER_DRIVER_OVERRIDE=kms_swrast
# No dconf or gvfs yet: keep GSettings in memory and use the plain local VFS.
export GSETTINGS_BACKEND=memory
export GIO_USE_VFS=local
export GIO_USE_VOLUME_MONITOR=unix
# No accessibility bus yet: without this, waiting for org.a11y.Bus stalls startup for 25 seconds.
export NO_AT_BRIDGE=1
export GTK_A11Y=none
# Keep the session's messages in a per-user log (greetd's terminal is not visible behind the desktop).
if [ -n "${XDG_RUNTIME_DIR:-}" ] && [ -d "$XDG_RUNTIME_DIR" ]; then
    exec >>"$XDG_RUNTIME_DIR/meldra-session.log" 2>&1
fi
# The session bus is created here so the Shell and everything it activates share one.
exec /usr/bin/dbus-run-session -- /usr/bin/gnome-shell --wayland --display-server
