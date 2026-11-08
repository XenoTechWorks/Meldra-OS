# Meldra OS

**Meldra OS** is an experimental operating system built around a kernel
that combines Linux's hardware support and userspace ABI with carefully selected
BSD designs and permissively licensed BSD components.

It is **not** a textual splice of two kernels. The initial architecture is a
Linux kernel baseline with reviewed ports behind Linux subsystem interfaces.
That gives the project a bootable base, retains Linux application and driver
compatibility, and creates an auditable boundary around every imported component.

## Status

Phase 0 (architecture, base userspace, and admission tooling) is underway.

- component provenance manifest and validator
- licensing and security policies
- initial subsystem scorecard
- reproducible upstream pinning plan
- **Hearth**, a small service-supervising PID 1
- **Kiln**, a safe package-format and package-manager prototype
- GNOME and Labwc desktop profiles

No upstream kernel source is vendored yet. Baseline versions must be selected
and pinned before the first source import.

## Quick start

```sh
make check
python3 tools/component_audit.py components.toml
make init
python3 -m kiln --help
```

## Project shape

```text
components.toml              proposed BSD-derived components
docs/architecture.md         technical direction and subsystem boundaries
docs/boot.md                 initramfs and QEMU smoke-boot instructions
docs/desktop.md              GNOME and Labwc port plan
docs/licensing.md            import and redistribution rules
docs/roadmap.md              delivery gates and current readiness
docs/security.md             vulnerability intake and response process
init/                        Hearth source and service definitions
kiln/                        Kiln package manager
profiles/                    installable OS profiles
tools/component_audit.py     provenance/admission validator
tools/build_initramfs.py     deterministic early-userspace image builder
tests/                       tooling tests
```

Run `make packages`, `make rootfs`, and `make status` to build and inspect the
current base-system artifacts.

The Linux baseline is pinned in `sources.lock.toml`. Use `make sources`,
`make kernel-config`, and `make kernel` for the verified kernel pipeline.

## Guiding principles

1. Boot and run a normal Linux userspace throughout development.
2. Port one subsystem at a time through stable internal adapters.
3. Preserve original copyright and license notices.
4. Treat upstream stable updates as the default security unit; do not build a
   fragile pile of isolated CVE patches.
5. Require tests and a rollback path for every imported component.

See [docs/architecture.md](docs/architecture.md) for the first milestones.

# Meldra-OS
