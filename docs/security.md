# Security and vulnerability workflow

The project treats vulnerability management as continuous kernel maintenance,
not as a one-time CVE cleanup.

## Intake

1. Track the Linux stable release stream selected for the baseline.
2. Ingest Linux kernel CVE announcements and their referenced fix commits.
3. Track security notices for every admitted BSD-derived upstream.
4. Record non-CVE fixes discovered by fuzzing, review, and upstream stable work.

The Linux kernel CVE process assigns identifiers after fixes are available and
warns that applicability depends on configuration and use. Therefore each item
must be mapped to enabled Kconfig symbols, reachable interfaces, architecture,
and the exact baseline commit. Raw CVE totals are not a useful risk score.

## Triage states

- `new`: received but not evaluated
- `affected`: reachable in a supported build
- `not-affected`: evidence recorded (code absent, configuration off, or fix present)
- `mitigated`: exposure reduced while awaiting the complete upstream fix set
- `fixed`: stable update or verified backport merged and tested

## Fix policy

Prefer the complete upstream stable release containing the fix. Cherry-pick only
when release timing makes that impossible, and record dependencies plus the
planned convergence release. Security patches run normal regression, sanitizer,
fuzz, boot, and userspace compatibility suites.

## Reporting

Potential unfixed Linux vulnerabilities must follow the Linux kernel security
reporting process; they must not be posted to a public issue tracker first.
Hybrid-only reports need a private security contact before the first public
release. Embargo handling, disclosure deadlines, and supported-release lifetime
must be documented before accepting external users.

