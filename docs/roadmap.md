# Meldra OS delivery roadmap

Percentages are approximate engineering readiness, not calendar promises.

## Command-line OS — about 15% complete

1. **Foundation (in progress):** architecture, provenance, Hearth, Kiln, image tooling.
2. **Kernel baseline:** pin a supported LTS, configure, build, boot, and continuously rebase.
3. **Base toolchain:** libc, dynamic loader, compiler runtime, shell, core utilities, device manager.
4. **Bootable storage:** GPT install with EFI-stub boot, persistent ext4/LUKS2 root, wired networking, users and ReGreet work in QEMU; the new encrypted and alongside paths plus real hardware still need full coverage.
5. **Distribution:** signed package repository, dependency solver, updates, recovery, installer.

The first genuinely usable console developer preview is reached after gate 4.

## Desktop OS — about 5% complete

6. **Graphics platform:** DRM/KMS, Mesa, libinput, Wayland, Xwayland, fonts.
7. **Desktop services:** D-Bus, elogind, polkit, GeoClue, RealtimeKit, portals, PipeWire, NetworkManager and QEMU Wi-Fi work; broader hardware testing remains.
8. **Labwc session:** lightweight graphical smoke target and recovery desktop.
9. **GNOME session:** Mutter, Shell, Settings and core applications run with software rendering; login is ReGreet on greetd (GDM needs systemd).
10. **Hardware and polish:** GPU acceleration and audio work in QEMU. Suspend/resume, BlueZ, AT-SPI accessibility, localization, LUKS2, and alongside installation are integrated; hardware and end-to-end coverage remain.

A VM desktop preview is reached after gate 9. A dependable daily-driver release requires gate 10,
security maintenance, upgrades, broad hardware testing, and release engineering.

## Next work

- [x] Add **ReGreet** to the `greetd` login screen.
- [ ] Exercise Bluetooth pairing and suspend/resume on representative laptops.
- [x] Add graphical, encrypted, alongside, and encrypted-alongside cases to the UEFI installer smoke matrix.
- [ ] Add Orca and screen-reader acceptance tests on top of the active AT-SPI bridge.

Run `make status` to see repository-backed gates rather than subjective percentages.
