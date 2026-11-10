.PHONY: all check test audit sources kernel-config kernel glibc init packages graphics-packages desktop-libs fonts xkeyboard-config labwc gnome-backgrounds gnome-classics rootfs desktop-rootfs desktop-initramfs desktop-preview gnome-iso gnome-power-smoke initramfs boot-smoke labwc-plan gnome-plan status clean

BUSYBOX ?= build/host-src/busybox-1.36.1/busybox

all: init

sources:
	python3 tools/fetch_sources.py

kernel-config: sources
	python3 tools/build_kernel.py --configure-only

kernel: sources
	python3 tools/build_kernel.py

glibc:
	python3 tools/build_glibc.py

check: audit init test

audit:
	python3 tools/component_audit.py components.toml

test:
	python3 -m unittest discover -s tests -v

init: build/hearth

initramfs: build/hearth
	python3 tools/build_initramfs.py --hearth build/hearth-static --busybox $(BUSYBOX) --output build/meldra-initramfs.cpio.gz

boot-smoke: initramfs
	./scripts/boot-smoke.sh

labwc-plan:
	python3 tools/resolve_profile.py meldra-labwc

gnome-plan:
	python3 tools/resolve_profile.py meldra-gnome

packages: build/hearth
	python3 tools/build_core_packages.py --hearth build/hearth --busybox $(BUSYBOX) --output build/packages

graphics-packages: glibc
	python3 tools/build_graphics_packages.py --output build/packages

desktop-libs: graphics-packages
	python3 tools/build_desktop_libraries.py

fonts:
	python3 tools/build_fonts.py

xkeyboard-config:
	python3 tools/build_xkeyboard_config.py

labwc: desktop-libs fonts xkeyboard-config
	python3 tools/build_labwc.py

gnome-backgrounds:
	python3 tools/build_gnome_backgrounds.py

gnome-classics: gnome-backgrounds
	python3 tools/build_gnome_classics.py

rootfs: packages
	python3 tools/assemble_rootfs.py --root build/rootfs --packages build/packages

desktop-rootfs: labwc packages
	python3 tools/assemble_rootfs.py --root build/rootfs-desktop --packages build/packages

desktop-initramfs: desktop-rootfs
	python3 tools/pack_rootfs.py --root build/rootfs-desktop --output build/meldra-desktop-initramfs.cpio.gz

desktop-preview: desktop-initramfs
	./scripts/run-desktop-preview.sh

gnome-iso: build/bzImage build/meldra-gnome-initramfs.cpio.gz
	python3 tools/build_iso.py

gnome-power-smoke:
	python3 tools/power_smoke.py

status:
	python3 tools/readiness.py

build/hearth: init/hearth.c
	mkdir -p build
	$(CC) $(CPPFLAGS) $(CFLAGS) -std=c11 -O2 -Wall -Wextra -Werror -pedantic -o $@ $< $(LDFLAGS)

clean:
	rm -f build/hearth
	rm -f build/meldra-initramfs.cpio.gz

