#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Resolve a Meldra package or profile into dependency installation order."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import tomllib


class ResolutionError(Exception):
    pass


def load_repository(path: Path) -> dict[str, dict]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    result: dict[str, dict] = {}
    for package in data.get("package", []):
        name = package.get("name")
        dependencies = package.get("dependencies", [])
        if not isinstance(name, str) or not name:
            raise ResolutionError("repository package needs a name")
        if name in result:
            raise ResolutionError(f"duplicate package: {name}")
        if not isinstance(dependencies, list) or not all(isinstance(x, str) for x in dependencies):
            raise ResolutionError(f"invalid dependencies for {name}")
        result[name] = package
    return result


def resolve(repository: dict[str, dict], targets: list[str]) -> list[str]:
    order: list[str] = []
    complete: set[str] = set()
    visiting: list[str] = []

    def visit(name: str) -> None:
        if name in complete:
            return
        if name in visiting:
            cycle = visiting[visiting.index(name):] + [name]
            raise ResolutionError(f"dependency cycle: {' -> '.join(cycle)}")
        package = repository.get(name)
        if package is None:
            raise ResolutionError(f"unknown package: {name}")
        visiting.append(name)
        for dependency in package["dependencies"]:
            visit(dependency)
        visiting.pop()
        complete.add(name)
        order.append(name)

    for target in targets:
        visit(target)
    return order


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("targets", nargs="+", help="package names to resolve")
    parser.add_argument("--repository", type=Path, default=Path("repository.toml"))
    parser.add_argument("--buildable", action="store_true", help="fail if any package is only planned")
    args = parser.parse_args()
    try:
        repository = load_repository(args.repository)
        order = resolve(repository, args.targets)
        if args.buildable:
            planned = [name for name in order if repository[name].get("status") != "built"]
            if planned:
                raise ResolutionError(f"packages without build recipes: {', '.join(planned)}")
    except (OSError, tomllib.TOMLDecodeError, ResolutionError) as exc:
        print(f"resolve_profile: {exc}", file=sys.stderr)
        return 1
    for name in order:
        package = repository[name]
        print(f"{name}\t{package.get('version', 'unversioned')}\t{package.get('status', 'planned')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
