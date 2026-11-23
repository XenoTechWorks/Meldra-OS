<div align="center">

# Meldra OS

### Linux compatibility. BSD discipline. One carefully engineered system.

![Developer Preview](https://img.shields.io/badge/status-developer_preview-f4a261)
![Kernel](https://img.shields.io/badge/kernel-6.18.62--meldra-5c7cfa)
![libc](https://img.shields.io/badge/userspace-glibc_2.44-7b61ff)
![Desktop](https://img.shields.io/badge/desktop-Labwc_and_GNOME_Shell_boot-38b000)

</div>

Meldra OS is an experimental operating system that keeps Linux's hardware and
application compatibility while introducing selected BSD designs through
reviewed, testable subsystem boundaries.

This is not a textual splice of two kernel trees. Meldra starts from a pinned
Linux LTS baseline, then evaluates BSD-derived components one subsystem at a
time. Every import needs clear provenance, compatible licensing, measurable
benefit, tests, and a rollback path.

## First graphical milestone

Meldra now boots its own kernel and userspace in QEMU, starts **Hearth** as PID
1, brings up udev and seatd, and launches a native **Labwc** Wayland session.

<p align="center">
  <img src="docs/screenshots/labwc-first-frame.png" width="720" alt="Meldra OS running Labwc in QEMU with its root menu open">
</p>

<p align="center"><em>The first real compositor frame from Meldra OS. The next visual milestone adds the wallpaper, terminal, panel, and launcher.</em></p>

## GNOME Shell boots

Meldra also boots **GNOME Shell 50.6** as a Wayland display server in QEMU: Hearth starts elogind and D-Bus, a
PAM session is opened for an unprivileged user, and Mutter drives the virtual GPU (software rendering).

<p align="center">
  <img src="docs/screenshots/gnome-overview.png" width="720" alt="Meldra OS running GNOME Shell: the Activities Overview with the blue GNOME wallpaper in the workspace thumbnail, the search field and the app grid button">
</p>

<p align="center"><em>GNOME Shell's Activities Overview on Meldra OS, with the GNOME wallpaper (decoded by glycin
inside its bubblewrap sandbox) and the Adwaita icons.</em></p>

<p align="center">
  <img src="docs/screenshots/gnome-desktop.png" width="720" alt="Meldra OS running GNOME Shell: the desktop with the wallpaper and the top bar, after pressing the Super key">
</p>

<p align="center"><em>The same session after a Super key press sent through QEMU: the Shell left the Overview for the
desktop. Both frames come from <code>tools/gnome_smoke.py</code>, which boots the image, waits for the Shell, and checks
that it draws and reacts to the keyboard.</em></p>

<p align="center">
  <img src="docs/screenshots/gnome-app-grid.png" width="720" alt="Meldra OS running GNOME Shell: the app grid after a mouse click on the app grid button">
</p>

<p align="center"><em>The app grid is driven through QEMU's QMP input path. The current image includes Foot and
three minimal Meldra apps: Files, Text Editor and Settings.</em></p>

<p align="center">
  <img src="docs/screenshots/gnome-terminal.png" width="720" alt="Meldra OS running GNOME Shell with the foot terminal open, showing uname and ls output">
</p>

<p align="center"><em>The foot terminal, launched from the app grid by mouse and driven from the keyboard.</em></p>

Working, checked in QEMU:

- **Apps:** GNOME Settings, GNOME Console (bash with GNU coreutils, grep, sed, findutils, gawk, less, procps, and
  fastfetch with the Meldra logo), GNOME Files (Nautilus) and GNOME Text Editor. Settings' Printers panel is left
  out until printing (CUPS server, Samba) lands; its About page cannot rename the computer yet.
- **GPU acceleration:** Mesa 26 with virgl (QEMU 3D), radeonsi and RADV for AMD (compiled with ACO, no LLVM),
  nouveau and zink. Checked with virgl: `virgl (AMD Radeon ...)`. Intel (iris) needs LLVM/clang at build time and
  comes later. GPU, Wi-Fi and network firmware ships xz-compressed (275 MB).
- **Networking:** NetworkManager with wpa_supplicant (WPA2/WPA3); GNOME shows the network icon with Wired and Wi-Fi
  tiles. Drivers load automatically (eudev + kmod). Wi-Fi was tested with simulated radios (mac80211_hwsim): scan
  and a WPA2 connection work; real Wi-Fi cards are untested.
- **Audio:** PipeWire, WirePlumber and pipewire-pulse; GNOME's volume control works, and a test sound played
  through an emulated Intel HDA card was recorded with real signal.
- **Bluetooth:** BlueZ 5.88 and GNOME's Bluetooth panel are integrated, with common USB/UART controller drivers;
  pairing on physical adapters still needs hardware coverage.
- **Accessibility and power:** AT-SPI is active for GTK/GNOME applications. elogind power off, reboot, and an
  ACPI suspend/resume cycle are covered by the QEMU power smoke test.
- polkit, UPower, AccountsService, GeoClue, and RealtimeKit.

<p align="center">
  <img src="docs/screenshots/gnome-settings.png" width="720" alt="GNOME Settings on Meldra showing the Network panel with a connected wired connection">
</p>

<p align="center">
  <img src="docs/screenshots/gnome-network.png" width="720" alt="GNOME quick settings on Meldra with Wired and Wi-Fi tiles, volume slider and network icon">
</p>

## Install to disk and log in

The live image has an **Install Meldra** app. It asks for a disk, account, computer name, keyboard layout, time
zone, and optional full-root encryption, then installs Meldra as a UEFI system. Its graphical flow erases the
chosen disk. The `meldra-install` command also has an expert `--alongside` mode that uses an existing FAT EFI
partition and a caller-selected root partition without deleting or moving other partitions. Secure Boot must be
off; encrypted systems ask for their disk password in Meldra's early userspace.

<p align="center">
  <img src="docs/screenshots/meldra-installer.png" width="720" alt="The Install Meldra window asking for a user name, password and computer name">
</p>

Installed systems start at ReGreet: greetd runs it in the cage kiosk compositor and starts GNOME after a
successful login. (GDM is not used: since GNOME 49 it needs systemd, which Meldra does not use.)
The live image still logs in automatically.

<p align="center">
  <img src="docs/screenshots/meldra-login.png" width="720" alt="Meldra's login screen with user name and password fields and Restart and Power off buttons">
</p>

Checked in QEMU by `tools/install_smoke.py`: install onto a blank disk, boot it with UEFI firmware, reject a wrong
password, log in with the right one. Files, the host name and the account survive reboots. Keyboard/time-zone
selection, encryption, and non-destructive alongside installation are covered by the end-to-end VM matrix but
still need testing on real hardware.

`tools/install_gui_smoke.py` also launches **Install Meldra** from GNOME's app grid, exercises form validation and
the erase acknowledgement, completes the graphical installation through polkit, then boots and logs into the
account created by the UI. `tools/install_storage_smoke.py` covers encrypted, alongside, and encrypted-alongside
storage layouts while preserving an unrelated sentinel filesystem.

The manual **Installer smoke tests** GitHub Actions workflow runs both matrices against a supplied HTTPS initramfs
whose SHA-256 digest is verified before QEMU starts. Reports, screenshots, and VM logs are retained as workflow
artifacts.

## What works today

| Area | State | Implementation |
| --- | --- | --- |
| Kernel | Boots | Linux 6.18.62-meldra with a reproducible config fragment |
| Early userspace | Boots | musl-static recovery/initramfs path |
| Main userspace ABI | Built | glibc 2.44 |
| Init system | Running | Hearth service supervisor as PID 1 |
| Package manager | Tested | Kiln packages with dependency and path-safety checks |
| Graphics | Accelerated | Mesa 26: virgl, radeonsi/RADV (ACO), nouveau, zink; Intel later |
| Text and UI | Built | FreeType, Fontconfig, HarfBuzz, FriBidi, Cairo, Pango |
| Window manager | Running | Labwc 0.9.1 on virtio-vga in QEMU |
| Desktop assets | Packaged | Current GNOME backgrounds plus Aqua, Curves, and Sunset |
| GNOME desktop | Usable preview | Shell 50.6, Settings, Console, Files, Text Editor, NetworkManager, PipeWire, desktop portals |
| Install and login | Working in QEMU | Installer (UEFI, ext4, optional LUKS2/alongside), ReGreet, persistent home |

Meldra is an installable developer preview, not yet a daily-driver OS. Broader
hardware testing, recovery, signed updates, and security/release engineering
remain before that claim would be responsible.

## Architecture

```text
                         Meldra OS
                            │
          ┌─────────────────┼─────────────────┐
          │                 │                 │
   Linux 6.18 LTS      Meldra userspace   Desktop profiles
   drivers + ABI       Hearth + Kiln      Labwc / GNOME
          │                 │                 │
          └──────── reviewed subsystem adapters ────────┘
                            │
                  selected BSD designs
```

- **Linux remains the compatibility spine.** Drivers, the syscall ABI, and
  mainstream Linux applications keep working throughout development.
- **BSD work is admitted, not assumed.** `components.toml` records provenance,
  ownership, licensing, integration boundaries, and maturity.
- **glibc is the desktop ABI.** musl remains intentionally scoped to static
  recovery and early-boot tools.
- **Upstream updates are the security unit.** Meldra avoids an unauditable pile
  of permanent one-off patches.

## Build and test

The build expects a Linux host with Python 3.11+, a C/C++ toolchain, Meson,
Ninja, CPIO, and QEMU. Downloaded sources are checksum-pinned in
`sources.lock.toml`.

```sh
# Validate component policy, build Hearth, and run the test suite
make check

# Build the kernel and base userspace
make kernel
make packages

# Build the Wayland and desktop dependency stacks
make graphics-packages
make desktop-libs
make fonts
make xkeyboard-config
make labwc

# Assemble and pack the graphical preview from a fresh build tree
make desktop-initramfs

# Boot the real Meldra graphical image
./scripts/run-desktop-preview.sh
```

For the smaller serial-only smoke test:

```sh
make boot-smoke
```

Installer checks require the assembled GNOME initramfs. The aggregate target runs the storage matrix first and
then drives the graphical installer from GNOME's app grid:

```sh
make installer-smoke

# Focused runs
make install-storage-smoke
make install-gui-smoke
python3 tools/install_storage_smoke.py --case encrypted-alongside
```

Generated test disks are removed after each run. Pass `--keep-disks` directly to either Python smoke runner when
the installed image is needed for debugging; reports remain in `build/reports/` and screenshots in
`build/screenshots/`.

## Meldra-owned components

### Hearth

Hearth is Meldra's compact PID 1 and service supervisor. It mounts the early
filesystems, launches declarative services, reaps child processes, respawns
long-running services, and performs orderly shutdown.

### Kiln

Kiln is Meldra's package format and package manager. Packages carry explicit
dependencies and are installed through traversal-safe, conflict-aware archive
handling. The current implementation is intentionally small enough to audit.

## Repository map

```text
components.toml                 BSD component admission and provenance
sources.lock.toml               pinned upstream versions and checksums
repository.toml                 package dependency and readiness catalog
kernel/config/                  Meldra kernel configuration fragments
init/                           Hearth and service definitions
kiln/                           Kiln package manager
desktop/labwc/                  Meldra Labwc session configuration
profiles/                       base, Labwc, and GNOME installation profiles
assets/wallpapers/              experimental Meldra artwork
docs/                           architecture, boot, desktop, security, roadmap
scripts/                        QEMU smoke and graphical preview launchers
tools/                          reproducible build and assembly pipelines
tests/                          policy, resolver, and package-manager tests
```

## Roadmap

1. Add the wallpaper client, Foot terminal, Fuzzel launcher, and Waybar.
2. Add greetd and a graphical login flow.
3. Move the preview from a large initramfs to an installable disk image.
4. Bring up networking, audio, portals, and Polkit.
5. Port Mutter, GNOME Shell, GDM, and the remaining GNOME platform stack.
6. Begin isolated BSD-subsystem experiments behind stable Linux interfaces.

The detailed delivery gates live in [docs/roadmap.md](docs/roadmap.md), while
[docs/architecture.md](docs/architecture.md) explains the kernel-integration
model and [docs/security.md](docs/security.md) defines vulnerability handling.

## Project rules

1. Keep the system bootable after every integration step.
2. Port one subsystem at a time behind an explicit boundary.
3. Preserve upstream copyright and license notices.
4. Require repeatable builds, tests, and a rollback path.
5. Report limitations honestly—screenshots and status must come from the real
   Meldra image, not a host-side mockup.

---

<div align="center">
  <strong>Meldra OS is under active construction.</strong><br>
  The first desktop frame exists; the usable desktop comes next.
</div>
