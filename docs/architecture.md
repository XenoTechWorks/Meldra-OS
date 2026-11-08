# Architecture

## C library boundary

Meldra's installed userspace ABI is GNU libc. Desktop libraries, services, and
applications must be built against the pinned glibc sysroot. musl is retained
only for static early-boot and recovery binaries in the initramfs; it is not a
second installed application ABI. GLib is GNOME's utility framework and is
independent of this libc decision.

## Base decision

The first bootable line will be based on a supported Linux LTS branch. Linux is
the execution substrate: scheduler, driver model, Linux syscall ABI, namespaces,
cgroups, and the existing architecture ports remain intact. BSD technology is
introduced as ports or new implementations at explicit subsystem boundaries.

A symmetric merge is not a maintainable design. Linux and the BSDs have
different locking, memory, device, VFS, network, build, and release interfaces.
Trying to keep both internal APIs alive would multiply the test matrix and make
upstream security fixes difficult to apply.

## Integration boundary

```text
applications / containers
          |
      Linux UAPI
          |
 Linux kernel baseline
   |      |       |
 native  adapters  experimental BSD-inspired subsystems
   |      |       |
 drivers  provenance + compatibility tests
```

An adapter must be small, independently testable, and removable. No port may
change the Linux UAPI by default. Experimental behavior is gated by Kconfig and,
where practical, a boot parameter or namespace-level opt-in.

## Candidate scorecard

| Area | Candidate value | First action | Initial risk |
|---|---|---|---|
| Capability security | Capsicum semantics | design study and threat model | medium |
| Storage | ZFS operational model | interface study only | high: licensing and maintenance |
| Packet filtering | PF rule model | compare with nftables | high: duplicated network policy |
| Jails | ergonomic confinement | map onto namespaces/cgroups/LSM | medium |
| Observability | DTrace-style workflows | compare with eBPF/ftrace | medium |

The scorecard names investigations, not promises to import code.

## Milestones

### M0 — reproducible baseline

- Use Linux 6.18 LTS, currently pinned to 6.18.62 with its kernel.org checksum.
- Build x86_64 `defconfig` with LLVM and GCC in CI.
- Boot under QEMU and run kselftest smoke tests.
- Capture SBOM, `.config`, compiler versions, and source commit.

### M1 — security baseline

- Enable KASAN, UBSAN, lockdep, and fault-injection test kernels.
- Run syzkaller against the baseline before adding hybrid code.
- Subscribe ingestion automation to Linux CVE announcements and stable releases.
- Define supported configurations and hardware; applicability depends on them.

### M2 — first hybrid experiment

- Select one medium-risk candidate after a written design review.
- Add its exact source provenance to `components.toml`.
- Implement behind Kconfig with baseline-vs-feature differential tests.
- Fuzz its syscall/ioctl/parser surface and document rollback.

### M3 — sustained maintenance

- Rebase on every selected stable release.
- Measure conflict rate, regression rate, and time-to-fix.
- Drop experiments that cannot keep pace with upstream stable updates.

## Non-goals for the first release

- binary compatibility with FreeBSD applications
- running Linux and FreeBSD kernel drivers in one address space
- replacing several core subsystems at once
- claiming vulnerability immunity from either parent project

