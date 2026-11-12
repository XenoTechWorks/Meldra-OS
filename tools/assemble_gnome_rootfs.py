#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Assemble a Meldra root filesystem that boots into GNOME Shell.

Unlike assemble_rootfs.py, which installs every package in a directory, this installs only the dependency
closure of the packages the GNOME session needs, then:

  * adds the unprivileged user the session runs as, and the system users dbus and elogind expect,
  * replaces the base Hearth services with the GNOME set (init/gnome-services),
  * compiles the GSettings schemas and regenerates the loader cache with the target's own tools.

    assemble_gnome_rootfs.py --root build/rootfs-gnome --packages build/packages
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kiln.cli import install
from tools.assemble_rootfs import dependency_order, package_manifest, write_base_configuration

# Packages the session runs directly; everything they depend on comes along.
ROOTS = (
    "hearth", "busybox", "kiln", "eudev", "dbus", "elogind", "linux-pam", "meldra-session",
    "gnome-shell", "gnome-backgrounds", "gnome-classic-favorites", "dejavu-fonts", "xkeyboard-config",
    "gsettings-desktop-schemas", "gcc-runtime", "glycin", "gi-typelibs", "accountsservice", "gdm", "dconf",
    "librsvg", "upower", "geoclue", "geocode-glib", "libgweather", "ibus", "gnome-settings-daemon",
    "bubblewrap", "adwaita-icon-theme", "hicolor-icon-theme", "foot", "rtkit", "meldra-desktop-apps",
    "linux", "linux-modules", "meldra-installer", "meldra-greeter",
    # A real shell for the terminal: bash and the GNU/procps tools (busybox remains /bin/sh for system scripts).
    "bash", "coreutils", "grep", "sed", "findutils", "gawk", "diffutils", "less", "procps-ng", "fastfetch",
    # Drivers and firmware (modules load through eudev + kmod), GPU acceleration, Wi-Fi, audio.
    "kmod", "linux-firmware", "wireless-regdb", "mesa", "vulkan-loader", "networkmanager", "wpa-supplicant", "pipewire",
    "wireplumber",
    # Real GNOME apps.
    "gnome-console", "gnome-text-editor", "nautilus", "gnome-control-center", "udisks", "meldra-backgrounds", "shared-mime-info", "nautilus",
)

PASSWD = (
    "root:x:0:0:root:/root:/bin/sh\n"
    "messagebus:x:81:81:D-Bus:/var/run/dbus:/bin/false\n"
    "geoclue:x:120:120:GeoClue:/var/lib/geoclue:/bin/false\n"
    "gdm:x:121:121:GDM:/var/lib/gdm:/bin/false\n"
    "rtkit:x:122:122:RealtimeKit:/proc:/bin/false\n"
    "greeter:x:123:123:Login screen:/var/lib/greeter:/bin/false\n"
    "meldra:x:1000:1000:Meldra:/home/meldra:/bin/bash\n"
)
GROUP = (
    "root:x:0:\nkmem:x:2:\ntty:x:5:\ndisk:x:6:\nlp:x:7:\ndialout:x:18:\n"
    "video:x:27:meldra\ninput:x:28:meldra\naudio:x:29:meldra\nrender:x:104:meldra\nclock:x:105:\n"
    "messagebus:x:81:\ngeoclue:x:120:\ngdm:x:121:\nrtkit:x:122:\ngreeter:x:123:\nmeldra:x:1000:\nwheel:x:10:meldra\ncdrom:x:11:\nkvm:x:24:\n"
)
LIVE_AUTOLOGIN = """
# meldra-live-autologin (the installer removes this section)
[initial_session]
command = "/usr/libexec/meldra/start-gnome"
user = "meldra"
"""

# Services of the base Hearth package that the GNOME image replaces: seatd (elogind is the seat manager
# here) and the getty on tty1 (the session owns that VT).
REMOVED_SERVICES = ("05-seatd.service", "90-getty.service")


def newest_packages(directory: Path) -> dict[str, tuple[Path, dict]]:
    """name -> (archive, manifest), keeping the newest version of each package."""
    found: dict[str, tuple[Path, dict]] = {}
    for archive in sorted(directory.glob("*.kiln")):
        meta = package_manifest(archive)
        key = [int(part) if part.isdigit() else 0 for part in re.split(r"[.]", meta["version"])]
        old = found.get(meta["name"])
        if old is None or key > [int(p) if p.isdigit() else 0 for p in re.split(r"[.]", old[1]["version"])]:
            found[meta["name"]] = (archive, meta)
    return found


def closure(packages: dict[str, tuple[Path, dict]], roots: tuple[str, ...]) -> list[Path]:
    wanted: set[str] = set()
    pending = list(roots)
    while pending:
        name = pending.pop()
        if name in wanted:
            continue
        if name not in packages:
            raise RuntimeError(f"package is not built: {name}")
        wanted.add(name)
        pending.extend(packages[name][1].get("dependencies", []))
    return [packages[name][0] for name in sorted(wanted)]


# The initramfs root cannot be pivot_root'ed, and bubblewrap (glycin's image-loader sandbox) needs pivot_root, so
# /init moves the whole root into a tmpfs and switches to it before Hearth starts. switch_root frees the initramfs
# copy as it goes, and keeps PID 1.
PREVIEW_INIT = """#!/bin/sh
/bin/mount -t proc proc /proc
/bin/mkdir -p /newroot
/bin/mount -t tmpfs -o mode=0755 tmpfs /newroot
for entry in /*; do
    case "$entry" in /newroot|/proc|/sys|/dev|/init) ;; *) /bin/cp -a "$entry" /newroot/ ;; esac
done
/bin/mkdir -p /newroot/proc /newroot/sys /newroot/dev
/bin/umount /proc
exec /bin/switch_root /newroot /sbin/hearth
"""


# Build-time files the running system never reads. Typelibs (.typelib), schemas, locales and icons stay.
PRUNE_DIRS = ("usr/include", "usr/lib/pkgconfig", "usr/lib64/pkgconfig", "usr/share/pkgconfig", "usr/lib/cmake",
              "usr/share/gir-1.0", "usr/share/doc", "usr/share/man", "usr/share/info", "usr/share/gtk-doc",
              "usr/share/aclocal", "usr/share/vala", "usr/share/gdb", "usr/share/bash-completion",
              "usr/share/i18n")


def prune_development_files(root: Path) -> int:
    removed = 0
    for relative in PRUNE_DIRS:
        target = root / relative
        if target.is_dir():
            removed += sum(f.stat().st_size for f in target.rglob("*") if f.is_file() and not f.is_symlink())
            shutil.rmtree(target)
    for archive in list(root.rglob("*.a")) + list(root.rglob("*.la")):
        if archive.is_file() and not archive.is_symlink():
            removed += archive.stat().st_size
            archive.unlink()
    return removed


def strip_debug_info(root: Path) -> tuple[int, int]:
    """Remove debug sections from every ELF file (no behaviour change; several libraries ship ~100 MB of it)."""
    count = saved = 0
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open("rb") as stream:
            if stream.read(4) != b"\x7fELF":
                continue
        before = path.stat().st_size
        subprocess.run(["strip", "--strip-debug", str(path)], check=True, stderr=subprocess.DEVNULL)
        saved += before - path.stat().st_size
        count += 1
    return count, saved


def run_target(root: Path, program: str, *arguments: str) -> None:
    """Run a program from the new root with the root's own glibc, never the host's."""
    loader = root / "lib64/ld-linux-x86-64.so.2"
    library_path = f"{root}/lib64:{root}/usr/lib64:{root}/usr/lib"
    subprocess.run([str(loader), "--library-path", library_path, str(root / program.lstrip("/")), *arguments], check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--packages", required=True, type=Path)
    args = parser.parse_args()
    if args.root.exists() and any(args.root.iterdir()):
        parser.error(f"root must be empty: {args.root}")
    args.root.mkdir(parents=True, exist_ok=True)
    root = args.root.resolve()

    try:
        archives = closure(newest_packages(args.packages), ROOTS)
        write_base_configuration(root)
        for archive in dependency_order(archives):
            print(install(archive, root))
    except RuntimeError as error:
        parser.error(str(error))

    polkit_helper = root / "usr/lib/polkit-1/polkit-agent-helper-1"
    if polkit_helper.stat().st_mode & 0o7777 != 0o4755:
        raise RuntimeError("polkit authentication helper must be installed mode 4755")
    if (root / "usr/bin/pkexec").stat().st_mode & 0o7777 != 0o4755:
        raise RuntimeError("pkexec must be installed mode 4755")
    dbus_helper = root / "usr/libexec/dbus-daemon-launch-helper"
    if dbus_helper.stat().st_mode & 0o7777 != 0o4755:
        raise RuntimeError("D-Bus service activation helper must be installed mode 4755")

    init = root / "init"
    init.unlink()
    init.write_text(PREVIEW_INIT, encoding="utf-8")
    init.chmod(0o755)
    etc = root / "etc"
    (etc / "passwd").write_text(PASSWD, encoding="utf-8")
    (etc / "group").write_text(GROUP, encoding="utf-8")
    (etc / "machine-id").write_text(uuid.uuid4().hex + "\n", encoding="utf-8")
    (etc / "hostname").write_text("meldra\n", encoding="utf-8")
    # Login shells; pkexec refuses to run for a user whose shell is not listed here.
    (etc / "shells").write_text("/bin/sh\n/bin/ash\n/bin/bash\n/usr/bin/bash\n", encoding="utf-8")
    shutil.copy2("init/shell/profile", etc / "profile")
    shutil.copy2("init/shell/bash.bashrc", etc / "bash.bashrc")
    (etc / "ld.so.conf").write_text("/usr/lib64\n/usr/lib\n", encoding="utf-8")
    for directory in ("home/meldra", "var/lib/dbus", "run"):
        (root / directory).mkdir(parents=True, exist_ok=True)

    # foot (the terminal) has no terminfo entry in this image, so ask for the widely understood xterm one.
    (etc / "xdg/foot").mkdir(parents=True, exist_ok=True)
    (etc / "xdg/foot/foot.ini").write_text("[main]\nterm=xterm-256color\nfont=DejaVu Sans Mono:size=11\nlogin-shell=yes\ninitial-window-size-chars=110x32\n", encoding="utf-8")

    # Only the main Foot launcher belongs in the app grid; its server and client entries are for scripting.
    # The real GNOME apps replace Meldra's lightweight preview implementations.
    (root / "usr/share/applications/org.meldra.Files.desktop").unlink(missing_ok=True)
    (root / "usr/share/applications/org.meldra.TextEditor.desktop").unlink(missing_ok=True)
    # GNOME Files replaces the placeholder file browser; VTE's demo terminal is not an app.
    for launcher in ("org.meldra.Settings.desktop", "org.meldra.Files.desktop", "org.gnome.Vte.App.Gtk4.desktop", "org.gnome.Vte.App.Gtk3.desktop"):
        (root / "usr/share/applications" / launcher).unlink(missing_ok=True)
    for hidden in ("foot-server.desktop", "footclient.desktop"):
        (root / "usr/share/applications" / hidden).unlink(missing_ok=True)

    for name in ("poweroff", "reboot"):
        link = root / "sbin" / name
        link.unlink(missing_ok=True)
        link.symlink_to(f"../bin/{name}")

    # Hearth owns elogind's lifecycle. Leaving its D-Bus activation file in
    # place races Hearth against loginctl during startup and can create a
    # duplicate-daemon respawn loop.
    (root / "usr/share/dbus-1/system-services/org.freedesktop.login1.service").unlink(missing_ok=True)
    # colord is built as a client library only. Its package still installs an activation file, which would make
    # GNOME Shell repeatedly try to start the deliberately omitted daemon.
    (root / "usr/share/dbus-1/system-services/org.freedesktop.ColorManager.service").unlink(missing_ok=True)
    # No D-Bus activation for GDM yet: the session starts directly (not through GDM's login screen), so an
    # activated GDM only runs as root to refuse the Shell's registration ("No display available"). The Shell
    # treats a missing GDM as normal. Add activation back together with the real GDM login flow.
    (root / "usr/share/dbus-1/system-services/org.gnome.DisplayManager.service").unlink(missing_ok=True)

    # Passwords: root is locked; the live account has an empty password (PAM's nullok lets it log in at the
    # login screen), and system accounts cannot log in. The installer rewrites this with the user's password.
    shadow_lines = []
    for line in PASSWD.splitlines():
        login, _, uid = line.split(":")[:3]
        password = "" if uid == "1000" else "!"
        shadow_lines.append(f"{login}:{password}:20000:0:99999:7:::")
    (etc / "shadow").write_text("\n".join(shadow_lines) + "\n", encoding="utf-8")
    (etc / "shadow").chmod(0o600)
    # The live image logs straight into GNOME; logging out shows the login screen. Installed systems drop this.
    greetd_config = etc / "greetd/config.toml"
    greetd_config.write_text(greetd_config.read_text(encoding="utf-8") + LIVE_AUTOLOGIN, encoding="utf-8")

    # Live only: the passwordless live account may run the installer (the installer deletes this rule).
    rules = etc / "polkit-1/rules.d"
    rules.mkdir(parents=True, exist_ok=True)
    shutil.copy2("installer/50-meldra-live-installer.rules", rules / "50-meldra-live-installer.rules")

    # Modules live in /usr/lib/modules (kmod is built for that); /lib/modules points there for anything that
    # still looks in the traditional place.
    (root / "lib").mkdir(exist_ok=True)
    if not (root / "lib/modules").exists():
        (root / "lib/modules").symlink_to("../usr/lib/modules")
    (etc / "NetworkManager").mkdir(parents=True, exist_ok=True)
    shutil.copy2("init/networkmanager/NetworkManager.conf", etc / "NetworkManager/NetworkManager.conf")

    services = etc / "meldra/services"
    for name in REMOVED_SERVICES:
        (services / name).unlink(missing_ok=True)
    for service in sorted(Path("init/gnome-services").glob("*.service")):
        shutil.copy2(service, services / service.name)

    # Merged /usr for lib64: sandboxes such as glycin's bubblewrap bind only /usr and recreate /lib64 only when it
    # is a symlink into it, so a real /lib64 leaves sandboxed programs without their dynamic loader.
    lib64, usr_lib64 = root / "lib64", root / "usr/lib64"
    if lib64.is_dir() and not lib64.is_symlink():
        usr_lib64.mkdir(parents=True, exist_ok=True)
        for entry in sorted(lib64.iterdir()):
            target = usr_lib64 / entry.name
            if target.exists() or target.is_symlink():
                raise RuntimeError(f"both /lib64 and /usr/lib64 contain {entry.name}")
            shutil.move(str(entry), str(target))
        lib64.rmdir()
        lib64.symlink_to("usr/lib64")
    # The glibc package ships locale sources but no compiled locale; programs such as foot refuse to run without
    # a UTF-8 one. Compile C.UTF-8 with the target's own localedef (before usr/share/i18n is pruned).
    if (root / "usr/bin/localedef").is_file():
        (root / "usr/lib/locale").mkdir(parents=True, exist_ok=True)
        run_target(root, "/usr/bin/localedef", "--no-archive", f"--prefix={root}", "-i", "C", "-f", "UTF-8", "C.utf8")
    pruned = prune_development_files(root)
    print(f"pruned {pruned / 1e6:.0f} MB of development files")
    count, saved = strip_debug_info(root)
    print(f"stripped debug info from {count} ELF files, saving {saved / 1e6:.0f} MB")
    # Post-install steps, done with the target's own tools.
    schemas = root / "usr/share/glib-2.0/schemas"
    if schemas.is_dir():
        run_target(root, "/usr/bin/glib-compile-schemas", str(schemas))
    # The MIME database: what file types exist (thumbnailers, Files and "Open With" are chosen by type).
    if (root / "usr/bin/update-mime-database").is_file():
        run_target(root, "/usr/bin/update-mime-database", str(root / "usr/share/mime"))
    query = root / "usr/bin/gdk-pixbuf-query-loaders"
    cache = root / "usr/lib/gdk-pixbuf-2.0/2.10.0/loaders.cache"
    if query.is_file():
        result = subprocess.run([str(root / "lib64/ld-linux-x86-64.so.2"), "--library-path",
                                 f"{root}/lib64:{root}/usr/lib64:{root}/usr/lib", str(query)],
                                capture_output=True, text=True, check=True,
                                env={**os.environ, "GDK_PIXBUF_MODULEDIR": str(root / "usr/lib/gdk-pixbuf-2.0/2.10.0/loaders"),
                                     "GCONV_PATH": f"{root}/usr/lib64/gconv"})
        # The cache must name the modules by their installed path, not the build tree.
        cache.write_text(result.stdout.replace(str(root), ""))
    icon_cache = root / "usr/bin/gtk4-update-icon-cache"
    if icon_cache.is_file():
        for theme in sorted((root / "usr/share/icons").glob("*/index.theme")):
            run_target(root, "/usr/bin/gtk4-update-icon-cache", "--quiet", "--force", str(theme.parent))
    ldconfig = root / "sbin/ldconfig"
    if ldconfig.is_file():
        (etc / "ld.so.cache").unlink(missing_ok=True)
        subprocess.run([str(ldconfig), "-r", str(root)], check=True)

    size = sum(path.stat().st_size for path in root.rglob("*") if path.is_file() and not path.is_symlink())
    print(f"assembled GNOME root at {root}: {len(archives)} packages, {size / 1e6:.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
