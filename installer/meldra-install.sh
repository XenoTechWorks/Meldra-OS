#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
#
# meldra-install: install the running Meldra live system onto a disk.
#
#     meldra-install [--yes] [--user NAME] [--password-file FILE] [--hostname NAME]
#                    [--keyboard-layout LAYOUT] [--timezone AREA/CITY] [--encrypt]
#                    [--alongside --esp PARTITION --root-partition PARTITION] DISK
#
# DISK is a whole disk such as /dev/nvme0n1, /dev/sda or /dev/vda. In the default mode everything on it is erased
# and the disk gets a GPT label with two partitions:
#
#   1  512 MiB  EFI system partition (FAT32, label MELDRA-ESP)  holds the kernel as \EFI\BOOT\BOOTX64.EFI
#   2  rest     Meldra root (ext4, partition name meldra-root)  the installed system
#
# With --alongside, the caller instead supplies existing ESP and root partitions; only the selected root is
# formatted. The kernel boots through its EFI stub and a Meldra firmware entry. Secure Boot must be off.
#
# Without --yes the disk name must be typed back to confirm. The password is read from --password-file (one
# line) or asked for on the terminal; without either, the account has no password until one is set.
# Progress lines start with "meldra-install: " and the last line is "meldra-install: done" on success, so a
# graphical front end can follow along.

set -eu
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH

say() { echo "meldra-install: $*"; }
die() { echo "meldra-install: error: $*" >&2; exit 1; }

assume_yes=no
mode=erase
encrypt=no
esp_arg=""
root_arg=""
user=meldra
password_file=""
hostname=meldra
keyboard_layout=us
timezone=UTC
disk=""
while [ $# -gt 0 ]; do
    case "$1" in
        --yes) assume_yes=yes ;;
        --user) user=${2:?--user needs a name}; shift ;;
        --password-file) password_file=${2:?--password-file needs a path}; shift ;;
        --hostname) hostname=${2:?--hostname needs a name}; shift ;;
        --keyboard-layout) keyboard_layout=${2:?--keyboard-layout needs a layout}; shift ;;
        --timezone) timezone=${2:?--timezone needs a zone}; shift ;;
        --encrypt) encrypt=yes ;;
        --alongside) mode=alongside ;;
        --esp) esp_arg=${2:?--esp needs a partition}; shift ;;
        --root-partition) root_arg=${2:?--root-partition needs a partition}; shift ;;
        -h|--help) sed -n '3,20p' "$0"; exit 0 ;;
        -*) die "unknown option: $1" ;;
        *) [ -z "$disk" ] || die "only one disk may be given"; disk=$1 ;;
    esac
    shift
done

[ "$(id -u)" = 0 ] || die "must be run as root"
[ -n "$disk" ] || die "no disk given (see --help)"
[ -b "$disk" ] || die "$disk is not a block device"
case "$user" in
    root|"") die "the account may not be root" ;;
    *[!a-z0-9_-]*|[!a-z_]*) die "user names use lower-case letters, digits, - and _, starting with a letter" ;;
esac
case "$hostname" in *[!A-Za-z0-9-]*|"") die "host names use letters, digits and -" ;; esac
case "$keyboard_layout" in *[!A-Za-z0-9_-]*|"") die "invalid keyboard layout" ;; esac
[ -f "/usr/share/xkeyboard-config-2/symbols/$keyboard_layout" ] || die "unknown keyboard layout: $keyboard_layout"
case "$timezone" in /*|*..*|*[!A-Za-z0-9_+./-]*|"") die "invalid time zone" ;; esac
[ -f "/usr/share/zoneinfo/$timezone" ] || die "unknown time zone: $timezone"

name=${disk##*/}
[ -e "/sys/block/$name" ] || die "$disk is not a whole disk (give the disk, not a partition)"
[ "$(cat "/sys/block/$name/ro")" = 0 ] || die "$disk is read-only"
if grep -q "^/dev/$name" /proc/mounts; then
    die "$disk (or one of its partitions) is mounted; unmount it first"
fi
for source in /source /run/meldra/live; do
    if [ -e "$source" ] && grep -q "^/dev/$name.* $source " /proc/mounts; then
        die "$disk holds the live system"
    fi
done
[ -f /boot/vmlinuz ] || die "no kernel at /boot/vmlinuz (the linux package is missing)"
[ "$encrypt" != yes ] || [ -x /usr/sbin/cryptsetup ] || die "encryption support is not installed"
[ "$encrypt" != yes ] || [ -f /usr/lib/meldra/crypt-root-initramfs.img ] || die "encrypted-root initramfs is missing"
[ "$encrypt" != yes ] || [ -d /sys/firmware/efi ] || die "encrypted installs must boot the live image in UEFI mode"

size=$(( $(cat "/sys/block/$name/size") * 512 / 1024 / 1024 / 1024 ))
[ "$size" -ge 8 ] || die "$disk is ${size} GiB; Meldra needs at least 8 GiB"

say "target: $disk (${size} GiB, $mode mode)"
if [ "$assume_yes" != yes ]; then
    if [ "$mode" = erase ]; then
        echo "ALL DATA ON $disk WILL BE ERASED. Type the disk name ($name) to continue:"
    else
        echo "The selected Meldra root partition will be erased. Type the disk name ($name) to continue:"
    fi
    read -r answer
    [ "$answer" = "$name" ] || die "not confirmed; nothing was changed"
fi

password=""
if [ -n "$password_file" ]; then
    IFS= read -r password < "$password_file" || true
elif [ -t 0 ] && [ "$assume_yes" != yes ]; then
    printf "Password for %s (leave empty for none): " "$user"
    stty -echo 2>/dev/null || true
    IFS= read -r password || true
    stty echo 2>/dev/null || true
    echo
fi
[ "$encrypt" != yes ] || [ -n "$password" ] || die "disk encryption needs a non-empty password"
install_key=$password_file
if [ "$encrypt" = yes ] && [ -z "$install_key" ]; then
    mkdir -p -m 0700 /run/meldra-install
    install_key=/run/meldra-install/disk-key
    (umask 077; printf '%s\n' "$password" > "$install_key")
fi

if [ "$mode" = erase ]; then
    case "$name" in *[0-9]) part=${disk}p ;; *) part=$disk ;; esac
    esp=${part}1
    root=${part}2
else
    [ -n "$esp_arg" ] && [ -n "$root_arg" ] || die "--alongside needs --esp and --root-partition"
    esp=$esp_arg
    root=$root_arg
    [ -b "$esp" ] || die "$esp is not a block-device partition"
    [ -b "$root" ] || die "$root is not a block-device partition"
    [ "$esp" != "$root" ] || die "the EFI and root partitions must be different"
    [ "$(lsblk -no PKNAME "$esp")" = "$name" ] || die "$esp is not on $disk"
    [ "$(lsblk -no PKNAME "$root")" = "$name" ] || die "$root is not on $disk"
    grep -q "^$esp " /proc/mounts && die "$esp is mounted; unmount it first"
    grep -q "^$root " /proc/mounts && die "$root is mounted; unmount it first"
    root_partno=$(cat "/sys/class/block/${root##*/}/partition")
fi
target=/run/meldra-install/target
mapped_root=""

cleanup() {
    umount "$target/boot/efi" 2>/dev/null || true
    umount "$target" 2>/dev/null || true
    [ -z "$mapped_root" ] || DM_DISABLE_UDEV=1 cryptsetup close "$mapped_root" 2>/dev/null || true
    [ "$install_key" = /run/meldra-install/disk-key ] && rm -f "$install_key" || true
}
trap cleanup EXIT

say "step 1/6: partitioning"
if [ "$mode" = erase ]; then
    wipefs --all --quiet "$disk"
    sfdisk --quiet --wipe always "$disk" <<EOF
label: gpt
size=512MiB, type=uefi, name="meldra-esp"
type=linux, name="meldra-root"
EOF
    [ -x /usr/bin/udevadm ] && udevadm settle --timeout=10 || sleep 2
    [ -b "$esp" ] && [ -b "$root" ] || die "partitions $esp and $root did not appear"
else
    # Give only the selected root partition Meldra's discovery label. No existing partition is deleted or moved.
    sfdisk --part-label "$disk" "$root_partno" meldra-root
fi

say "step 2/6: creating file systems"
if [ "$mode" = erase ]; then
    mkfs.vfat -F 32 -n MELDRA-ESP "$esp" >/dev/null
else
    [ "$(blkid -s TYPE -o value "$esp")" = vfat ] || die "$esp is not a FAT EFI system partition"
fi
filesystem_root=$root
if [ "$encrypt" = yes ]; then
    # Keep production installs memory-hard. Pure-emulation smoke tests may opt into a deliberately weak PBKDF
    # so they can exercise the LUKS format/unlock path without spending many minutes deriving each test key.
    pbkdf_args="--pbkdf argon2id --pbkdf-memory 65536 --pbkdf-parallel 4 --pbkdf-force-iterations 4"
    if [ "${MELDRA_INSTALL_TEST_PBKDF:-0}" = 1 ]; then
        pbkdf_args="--pbkdf pbkdf2 --pbkdf-force-iterations 1000"
    fi
    # shellcheck disable=SC2086  # fixed argument strings selected above, intentionally split for cryptsetup
    cryptsetup luksFormat --batch-mode --type luks2 $pbkdf_args --key-file "$install_key" "$root"
    DM_DISABLE_UDEV=1 cryptsetup open --key-file "$install_key" "$root" meldra-root
    mapped_root=meldra-root
    filesystem_root=/dev/mapper/meldra-root
fi
mkfs.ext4 -q -F -L meldra-root "$filesystem_root"

say "step 3/6: copying the system"
mkdir -p "$target"
mount -t ext4 "$filesystem_root" "$target"
for entry in /*; do
    case "$entry" in
        /proc|/sys|/dev|/run|/tmp|/mnt|/media|/home|/init|/newroot|/lost+found) ;;
        *) cp -a "$entry" "$target/" ;;
    esac
done
for directory in proc sys dev run mnt media home; do mkdir -p "$target/$directory"; done
mkdir -p -m 1777 "$target/tmp"

say "step 4/6: configuring the installed system"
services=$target/etc/meldra/services
# The live image keeps /home on a tmpfs and offers an unauthenticated root shell on the serial port; an
# installed system does neither.
rm -f "$services/02-home.service" "$services/80-serial-shell.service"
printf 'command=/bin/mount -t tmpfs -o mode=1777,nosuid,nodev tmpfs /tmp\nrespawn=no\n' > "$services/02-tmp.service"
tr -d '-' < /proc/sys/kernel/random/uuid > "$target/etc/machine-id"
echo "$hostname" > "$target/etc/hostname"
printf '127.0.0.1 localhost\n127.0.1.1 %s\n::1 localhost\n' "$hostname" > "$target/etc/hosts"
ln -snf "../usr/share/zoneinfo/$timezone" "$target/etc/localtime"
echo "$timezone" > "$target/etc/timezone"
printf 'KEYMAP=%s\n' "$keyboard_layout" > "$target/etc/vconsole.conf"
sed -i '/^XKB_DEFAULT_LAYOUT[[:space:]]/d' "$target/etc/meldra/session-env.conf"
printf 'XKB_DEFAULT_LAYOUT DEFAULT=%s\n' "$keyboard_layout" >> "$target/etc/meldra/session-env.conf"

# Mutter stores the desktop layout in dconf; the environment setting above also covers ReGreet/Cage and is the
# fallback before the user's settings database exists.
mkdir -p "$target/etc/dconf/profile" "$target/etc/dconf/db/local.d"
printf 'user-db:user\nsystem-db:local\n' > "$target/etc/dconf/profile/user"
printf "[org/gnome/desktop/input-sources]\nsources=[('xkb', '%s')]\n" "$keyboard_layout" \
    > "$target/etc/dconf/db/local.d/00-meldra-input"
/usr/bin/chroot "$target" /usr/bin/dconf update

# The live account is "meldra" (uid 1000); rename it if another name was asked for.
if [ "$user" != meldra ]; then
    sed -i "s/^meldra:/$user:/; s|/home/meldra:|/home/$user:|" "$target/etc/passwd"
    sed -i "s/^meldra:/$user:/; s/\([:,]\)meldra\(,\|$\)/\1$user\2/g" "$target/etc/group"
fi
# Installed systems start at the login screen: drop the live image's automatic login (the section runs from its
# marker comment to the end of the file).
sed -i '/^# meldra-live-autologin/,$d' "$target/etc/greetd/config.toml"
# The live image's installer permission and its "Install Meldra" launcher have no place on an installed system.
rm -f "$target/etc/polkit-1/rules.d/50-meldra-live-installer.rules" \
      "$target/usr/share/applications/org.meldra.Installer.desktop"
mkdir -p "$target/home/$user"
chown 1000:1000 "$target/home/$user"
chmod 700 "$target/home/$user"

# Passwords live in /etc/shadow (root only). Root is locked: administration goes through the user's account.
if [ -n "$password" ]; then
    hash=$(printf '%s\n' "$password" | cryptpw -m sha512 -P 0)
else
    hash=""
fi
days=$(( $(date +%s) / 86400 ))
{
    echo "root:!:$days:0:99999:7:::"
    while IFS=: read -r login _ uid _; do
        [ "$login" = root ] && continue
        if [ "$uid" = 1000 ]; then echo "$login:$hash:$days:0:99999:7:::"; else echo "$login:!:$days::::::"; fi
    done < "$target/etc/passwd"
} > "$target/etc/shadow"
chmod 600 "$target/etc/shadow"
password=""

say "step 5/6: installing the kernel to the EFI system partition"
mkdir -p "$target/boot/efi"
mount -t vfat "$esp" "$target/boot/efi"
mkdir -p "$target/boot/efi/EFI/Meldra"
cp /boot/vmlinuz "$target/boot/efi/EFI/Meldra/meldra.efi"
cp /usr/lib/meldra/crypt-root-initramfs.img "$target/boot/efi/EFI/Meldra/initrd.img"
# A fresh unencrypted disk remains bootable through the standard removable-media path even on firmware that
# refuses variable updates. Alongside installs never overwrite another OS's fallback loader.
if [ "$mode" = erase ] && [ "$encrypt" != yes ]; then
    mkdir -p "$target/boot/efi/EFI/BOOT"
    cp /boot/vmlinuz "$target/boot/efi/EFI/BOOT/BOOTX64.EFI"
fi

boot_options='initrd=\EFI\Meldra\initrd.img rdinit=/init root=PARTLABEL=meldra-root rootfstype=ext4 rootwait rw init=/sbin/hearth console=tty0 console=ttyS0,115200'
if [ -d /sys/firmware/efi ]; then
    if ! grep -q ' /sys/firmware/efi/efivars ' /proc/mounts; then
        mount -t efivarfs efivarfs /sys/firmware/efi/efivars
    fi
    esp_partno=$(cat "/sys/class/block/${esp##*/}/partition")
    efibootmgr --create --disk "$disk" --part "$esp_partno" --label "Meldra OS" \
        --loader '\EFI\Meldra\meldra.efi' --unicode "$boot_options" || \
        [ "$encrypt" != yes ] || die "could not register the encrypted Meldra boot entry"
elif [ "$encrypt" = yes ]; then
    die "cannot register the encrypted Meldra boot entry outside UEFI mode"
fi

say "step 6/6: writing everything to disk"
sync
cleanup
trap - EXIT
say "done"
