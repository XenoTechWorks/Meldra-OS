#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# Runs inside the logind session, as the unprivileged user, started by greetd after login.
# Mesa picks the GPU's driver (virgl, radeonsi, nouveau, zink) and falls back to software rendering itself.
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
export XDG_SESSION_TYPE=wayland
export XDG_CURRENT_DESKTOP=GNOME
# dconf is the shared, persistent GSettings backend.  Do not force the memory
# backend here: every process would get its own private settings store, so a
# wallpaper selected in Settings would never reach GNOME Shell.
export GIO_USE_VFS=local
export GIO_USE_VOLUME_MONITOR=unix
# GTK and GNOME use the packaged AT-SPI D-Bus bridge. org.a11y.Bus is activated
# on this session bus when the first accessible application connects.
# Keep the session's messages in the user's home (greetd's terminal is hidden behind the desktop, and
# $XDG_RUNTIME_DIR is deleted when the session ends, taking any log of a failed start with it).
log_dir="$HOME/.local/state/meldra"
if mkdir -p "$log_dir" 2>/dev/null; then
    [ -f "$log_dir/session.log" ] && mv -f "$log_dir/session.log" "$log_dir/session.log.old"
    exec >"$log_dir/session.log" 2>&1
fi
# The session bus is created here so the Shell and everything it activates share one.
exec /usr/bin/dbus-run-session -- /usr/libexec/meldra/gnome-session-inner
