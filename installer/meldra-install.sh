#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
#
# meldra-install: install the running Meldra live system onto a disk.
#
#     meldra-install [--yes] [--user NAME] [--password-file FILE] [--hostname NAME] DISK
#
# DISK is a whole disk such as /dev/nvme0n1, /dev/sda or /dev/vda. EVERYTHING ON IT IS ERASED. The disk gets a
# GPT label with two partitions:
#
#   1  512 MiB  EFI system partition (FAT32, label MELDRA-ESP)  holds the kernel as \EFI\BOOT\BOOTX64.EFI
#   2  rest     Meldra root (ext4, partition name meldra-root)  the installed system
#
# The kernel boots through its EFI stub; its built-in command line mounts root=PARTLABEL=meldra-root, so no
# bootloader or boot menu is needed. Secure Boot must be off (the kernel is not signed yet).
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
user=meldra
password_file=""
hostname=meldra
disk=""
while [ $# -gt 0 ]; do
    case "$1" in
        --yes) assume_yes=yes ;;
        --user) user=${2:?--user needs a name}; shift ;;
        --password-file) password_file=${2:?--password-file needs a path}; shift ;;
        --hostname) hostname=${2:?--hostname needs a name}; shift ;;
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

size=$(( $(cat "/sys/block/$name/size") * 512 / 1024 / 1024 / 1024 ))
[ "$size" -ge 8 ] || die "$disk is ${size} GiB; Meldra needs at least 8 GiB"

say "target: $disk (${size} GiB)"
if [ "$assume_yes" != yes ]; then
    echo "ALL DATA ON $disk WILL BE ERASED. Type the disk name ($name) to continue:"
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

case "$name" in *[0-9]) part=${disk}p ;; *) part=$disk ;; esac
esp=${part}1
root=${part}2
target=/run/meldra-install/target

cleanup() {
    umount "$target/boot/efi" 2>/dev/null || true
    umount "$target" 2>/dev/null || true
}
trap cleanup EXIT

say "step 1/6: partitioning"
wipefs --all --quiet "$disk"
sfdisk --quiet --wipe always "$disk" <<EOF
label: gpt
size=512MiB, type=uefi, name="meldra-esp"
type=linux, name="meldra-root"
EOF
[ -x /usr/bin/udevadm ] && udevadm settle --timeout=10 || sleep 2
[ -b "$esp" ] && [ -b "$root" ] || die "partitions $esp and $root did not appear"

say "step 2/6: creating file systems"
mkfs.vfat -F 32 -n MELDRA-ESP "$esp" >/dev/null
mkfs.ext4 -q -F -L meldra-root "$root"

say "step 3/6: copying the system"
mkdir -p "$target"
mount -t ext4 "$root" "$target"
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
mkdir -p "$target/boot/efi/EFI/BOOT"
cp /boot/vmlinuz "$target/boot/efi/EFI/BOOT/BOOTX64.EFI"

say "step 6/6: writing everything to disk"
sync
cleanup
trap - EXIT
say "done"
