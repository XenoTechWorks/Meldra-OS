#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# Runs inside the session's D-Bus (started by start-gnome): the per-user audio services, then GNOME Shell.
# PipeWire is the audio server, WirePlumber manages devices and routing, and pipewire-pulse serves
# PulseAudio clients (GNOME's volume controls among them).
/usr/bin/pipewire &
/usr/bin/wireplumber &
/usr/bin/pipewire-pulse &

# Screen reader. On other distributions gnome-session starts Orca from its autostart entry, whose
# AutostartCondition follows org.gnome.desktop.a11y.applications screen-reader-enabled (the Settings switch and
# Super+Alt+S). Meldra has no gnome-session, so this watcher plays that part: it starts Orca when the key turns
# on (or is already on at login) and stops it when the key turns off.
screen_reader_watch() {
    schema=org.gnome.desktop.a11y.applications
    key=screen-reader-enabled
    # Orca needs the Shell's accessibility tree; wait until the Shell owns its bus name.
    attempt=0
    until /usr/bin/dbus-send --session --print-reply --dest=org.freedesktop.DBus / \
            org.freedesktop.DBus.NameHasOwner string:org.gnome.Shell 2>/dev/null | grep -q 'boolean true'; do
        attempt=$((attempt + 1))
        [ "$attempt" -gt 120 ] && break
        sleep 1
    done
    orca_pid=
    { echo initial; exec /usr/bin/gsettings monitor "$schema" "$key"; } | while read -r _; do
        if [ "$(/usr/bin/gsettings get "$schema" "$key")" = true ]; then
            if [ -z "$orca_pid" ] || ! kill -0 "$orca_pid" 2>/dev/null; then
                echo "meldra-session: screen reader on, starting Orca"
                /usr/bin/orca --replace &
                orca_pid=$!
            fi
        elif [ -n "$orca_pid" ]; then
            echo "meldra-session: screen reader off, stopping Orca"
            kill "$orca_pid" 2>/dev/null
            orca_pid=
        fi
    done
}
[ -x /usr/bin/orca ] && screen_reader_watch &

exec /usr/bin/gnome-shell --wayland --display-server
