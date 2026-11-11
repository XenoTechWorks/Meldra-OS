# Meldra OS delivery roadmap

Percentages are approximate engineering readiness, not calendar promises.

## Command-line OS — about 15% complete

1. **Foundation (in progress):** architecture, provenance, Hearth, Kiln, image tooling.
2. **Kernel baseline:** pin a supported LTS, configure, build, boot, and continuously rebase.
3. **Base toolchain:** libc, dynamic loader, compiler runtime, shell, core utilities, device manager.
4. **Bootable storage:** GPT install with EFI-stub boot, persistent ext4 root, wired networking, users and a login screen work in QEMU; real-hardware testing remains.
5. **Distribution:** signed package repository, dependency solver, updates, recovery, installer.

The first genuinely usable console developer preview is reached after gate 4.

## Desktop OS — about 5% complete

6. **Graphics platform:** DRM/KMS, Mesa, libinput, Wayland, Xwayland, fonts.
7. **Desktop services:** D-Bus, elogind sessions, polkit, GeoClue and RealtimeKit work; wired DHCP networking works; portals, PipeWire, NetworkManager and Wi-Fi remain.
8. **Labwc session:** lightweight graphical smoke target and recovery desktop.
9. **GNOME session:** Mutter, Shell, Settings and core applications run with software rendering; login is greetd with a Meldra greeter (GDM needs systemd).
10. **Hardware and polish:** GPU acceleration, suspend, audio, Bluetooth, accessibility, installer UX.

A VM desktop preview is reached after gate 9. A dependable daily-driver release requires gate 10,
security maintenance, upgrades, broad hardware testing, and release engineering.

Run `make status` to see repository-backed gates rather than subjective percentages.
