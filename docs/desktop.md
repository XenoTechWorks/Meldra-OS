# Desktop platform

Meldra OS targets Wayland first and offers two installable sessions.

During desktop bring-up, both graphical profiles ship the pinned upstream
`gnome-backgrounds` package plus the original GNOME Curves, Aqua Foot, and
Sunset wallpapers in `gnome-classic-favorites`. Curves is the initial desktop;
Aqua Foot and Sunset are registered in GNOME's wallpaper chooser. Meldra's
experimental artwork remains optional at `/usr/share/backgrounds/meldra/`.

## GNOME (default)

GNOME is the integrated desktop: GNOME Shell and Mutter, meldra-greeter/greetd,
settings, NetworkManager, BlueZ, PipeWire/WirePlumber, AT-SPI, portals, polkit,
power management, and AccountsService. Login, audio, networking, portals, and
software rendering work in a VM; suspend/resume now has a QEMU smoke path.
Lock/unlock, screen sharing, Bluetooth hardware, and accessibility workflows
still need broader acceptance coverage.

GNOME currently assumes several service APIs often supplied by systemd projects.
Meldra will initially package the required standalone daemons and compatibility
interfaces rather than patching GNOME. Hearth supervises them. Any later
replacement must preserve the relevant D-Bus and logind behavior.

## Labwc (alternative)

Labwc provides a smaller stacking Wayland session with greetd, Waybar, Foot,
Fuzzel, and the wlroots portal. It is both a user choice and a recovery desktop
when GNOME or GPU acceleration is unavailable.

## Port order

1. Mesa, libdrm, libinput, Wayland, and Xwayland.
2. D-Bus, seat/session management, polkit, PipeWire, and portals.
3. Labwc smoke session to validate the platform beneath GNOME.
4. Mutter and GNOME Shell under software rendering.
5. Graphical login (meldra-greeter), complete GNOME session, and hardware acceleration.
6. Accessibility, input methods, Bluetooth, color management, screen sharing, and suspend.

`repository.toml` is the dependency intent and recipe-status catalog. Run
`make labwc-plan` or `make gnome-plan` to inspect dependency order. A plan is
not considered buildable until every entry has a pinned target recipe; host
libraries must never be copied into Meldra to satisfy a missing recipe.
