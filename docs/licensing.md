# Licensing and provenance policy

This is an engineering policy, not legal advice.

The combined kernel distribution follows the Linux kernel's GPL-2.0-only terms.
Permissively licensed source can be incorporated only when its exact file-level
license is compatible and all copyright, attribution, and disclaimer conditions
are retained. A project's general reputation as “BSD licensed” is not enough;
each imported file must be checked.

## Admission requirements

Every imported or adapted component must have:

- canonical origin URL and immutable upstream commit
- per-file SPDX identifier and retained copyright notices
- destination path and named maintenance owner
- documented modification history
- compatibility review recorded in the change proposal
- upstream update and security-response plan

`UNPINNED` entries are studies only. They cannot enter compiled kernel code.

## Hard boundaries

- Do not relicense Linux-derived code as BSD.
- Do not remove BSD notices when incorporating compatible permissive code.
- Do not copy CDDL or otherwise questionable code into the combined kernel while
  compatibility is unresolved; study behavior and interfaces instead.
- Do not use “clean room” to describe ordinary rewriting. A clean-room process
  requires separate specification and implementation teams and legal review.

## Source annotations

New kernel-bound source defaults to `GPL-2.0-only`. A file retaining upstream
BSD terms uses the accurate expression determined during review; it must not be
changed mechanically just to pass tooling.

