#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Validate hybrid-kernel component provenance records."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import tomllib
from urllib.parse import urlparse

ALLOWED_STATUS = {"study", "prototype", "admitted", "retired"}
REQUIRED = {"name", "origin", "revision", "license", "target", "owner", "status", "strategy"}


def audit(path: Path) -> list[str]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return [f"cannot read manifest: {exc}"]

    errors: list[str] = []
    if data.get("schema_version") != 1:
        errors.append("schema_version must be 1")

    components = data.get("component")
    if not isinstance(components, list) or not components:
        return errors + ["manifest must contain at least one [[component]]"]

    names: set[str] = set()
    targets: set[str] = set()
    for index, component in enumerate(components, 1):
        label = component.get("name", f"component #{index}")
        missing = sorted(REQUIRED - component.keys())
        if missing:
            errors.append(f"{label}: missing {', '.join(missing)}")
            continue

        if label in names:
            errors.append(f"{label}: duplicate name")
        names.add(label)

        parsed = urlparse(component["origin"])
        if parsed.scheme != "https" or not parsed.netloc:
            errors.append(f"{label}: origin must be a canonical HTTPS URL")

        status = component["status"]
        if status not in ALLOWED_STATUS:
            errors.append(f"{label}: invalid status {status!r}")

        revision = component["revision"]
        if status != "study" and revision == "UNPINNED":
            errors.append(f"{label}: {status} components require an immutable revision")

        if status in {"prototype", "admitted"} and component["owner"] == "unassigned":
            errors.append(f"{label}: {status} components require an owner")

        target = component["target"]
        if target.startswith("/") or ".." in Path(target).parts:
            errors.append(f"{label}: target must be a repository-relative safe path")
        if target in targets:
            errors.append(f"{label}: duplicate target {target!r}")
        targets.add(target)

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", nargs="?", type=Path, default=Path("components.toml"))
    args = parser.parse_args()
    errors = audit(args.manifest)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"OK: {args.manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

