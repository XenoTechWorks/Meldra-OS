# SPDX-License-Identifier: GPL-2.0-only
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import posixpath
import shutil
import tarfile
import tempfile
import tomllib

DB_PATH = Path("var/lib/kiln/installed")


class PackageError(Exception):
    pass


def _safe_members(archive: tarfile.TarFile):
    for member in archive.getmembers():
        path = Path(member.name)
        if path.is_absolute() or ".." in path.parts or member.islnk():
            raise PackageError(f"unsafe archive member: {member.name}")
        if member.issym():
            target = Path(member.linkname)
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(member.name), member.linkname))
            if target.is_absolute() or resolved == ".." or resolved.startswith("../"):
                raise PackageError(f"unsafe archive symlink: {member.name} -> {member.linkname}")
        yield member


def _digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def build(source: Path, output: Path | None = None) -> str:
    """Build a .kiln archive from KILN.toml and a root/ payload."""
    manifest_path = source / "KILN.toml"
    payload = source / "root"
    if not manifest_path.is_file() or not payload.is_dir():
        raise PackageError("source needs KILN.toml and a root directory")
    manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    package = manifest.get("package", {})
    name, version = package.get("name"), package.get("version")
    if not name or not version:
        raise PackageError("manifest needs package.name and package.version")
    output = output or Path(f"{name}-{version}.kiln")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        archive.add(manifest_path, arcname="KILN.toml", recursive=False)
        for path in sorted(payload.rglob("*")):
            # Installs refuse hard links, so never write any: forgetting the inodes seen so far makes tarfile
            # store every hard-linked file as a full copy (build systems such as e2fsprogs' create them).
            archive.inodes = {}
            archive.add(path, arcname=str(Path("root") / path.relative_to(payload)), recursive=False)
    return f"built {output} ({_digest(output)})"


# The only files a package may install setuid or setgid, keyed by package name. Each is a small audited helper
# that must run privileged; any other privileged file makes the install fail instead of silently gaining root.
SETUID_ALLOWLIST = frozenset({
    ("polkit", "root/usr/lib/polkit-1/polkit-agent-helper-1"),   # authenticates the user for polkit agents
    ("dbus", "root/usr/libexec/dbus-daemon-launch-helper"),       # starts system bus services on demand
    ("polkit", "root/usr/bin/pkexec"),                            # runs one program as root once polkit agrees
})


def install(package: Path, root: Path) -> str:
    with tempfile.TemporaryDirectory(prefix="kiln-") as temporary:
        stage = Path(temporary)
        with tarfile.open(package, "r:*") as archive:
            members = list(_safe_members(archive))
            archive.extractall(stage, members=members, filter="data")
            # Python's data filter clears setuid/setgid bits; they are restored below only for allowlisted files.
            privileged = [m for m in members if m.isfile() and m.mode & 0o6000]
        manifest_path = stage / "KILN.toml"
        if not manifest_path.is_file():
            raise PackageError("package has no KILN.toml")
        manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
        name = manifest.get("package", {}).get("name")
        version = manifest.get("package", {}).get("version")
        if not name or not version or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-+._" for c in name):
            raise PackageError("invalid package name or version")
        payload = stage / "root"
        if not payload.is_dir():
            raise PackageError("package has no root payload")
        for member in privileged:
            if (name, member.name) not in SETUID_ALLOWLIST:
                raise PackageError(f"{name}: setuid/setgid file is not allowlisted: {member.name}")
            (stage / member.name).chmod(member.mode & 0o7777)
        database = root / DB_PATH
        database.mkdir(parents=True, exist_ok=True)
        dependencies = manifest.get("package", {}).get("dependencies", [])
        if not isinstance(dependencies, list) or not all(
            isinstance(item, str) and item for item in dependencies
        ):
            raise PackageError("package.dependencies must be an array of package names")
        missing = [item for item in dependencies if not (database / f"{item}.json").is_file()]
        if missing:
            raise PackageError(f"missing dependencies for {name}: {', '.join(missing)}")
        record_path = database / f"{name}.json"
        if record_path.exists():
            raise PackageError(f"{name} is already installed")
        files = []
        for source in sorted(p for p in payload.rglob("*") if p.is_file() or p.is_symlink()):
            relative = source.relative_to(payload)
            destination = root / relative
            if destination.exists() or destination.is_symlink():
                raise PackageError(f"file conflict: /{relative}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source.is_symlink():
                target = os.readlink(source)
                destination.symlink_to(target)
                files.append({"path": str(relative), "type": "symlink", "target": target})
                continue
            temporary_destination = destination.with_name(destination.name + ".kiln-new")
            shutil.copy2(source, temporary_destination)
            temporary_destination.replace(destination)
            files.append({"path": str(relative), "type": "file", "sha256": _digest(destination)})
        record = {
            "name": name,
            "version": str(version),
            "dependencies": dependencies,
            "files": files,
        }
        record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        return f"installed {name} {version} ({len(files)} files)"


def list_packages(root: Path) -> list[str]:
    database = root / DB_PATH
    if not database.exists():
        return []
    result = []
    for record_path in sorted(database.glob("*.json")):
        record = json.loads(record_path.read_text(encoding="utf-8"))
        result.append(f"{record['name']} {record['version']}")
    return result


def remove(name: str, root: Path) -> str:
    record_path = root / DB_PATH / f"{name}.json"
    if not record_path.exists():
        raise PackageError(f"{name} is not installed")
    users = []
    for candidate in (root / DB_PATH).glob("*.json"):
        candidate_record = json.loads(candidate.read_text(encoding="utf-8"))
        if name in candidate_record.get("dependencies", []):
            users.append(candidate_record["name"])
    if users:
        raise PackageError(f"cannot remove {name}; required by: {', '.join(sorted(users))}")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    for item in record["files"]:
        path = root / item["path"]
        if item.get("type") == "symlink":
            if path.is_symlink() and os.readlink(path) == item["target"]:
                path.unlink()
        elif path.is_file() and _digest(path) == item["sha256"]:
            path.unlink()
    record_path.unlink()
    return f"removed {name}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kiln", description="Kiln package manager for Meldra OS")
    parser.add_argument("--root", type=Path, default=Path("/"), help="alternate system root")
    commands = parser.add_subparsers(dest="command", required=True)
    install_parser = commands.add_parser("install")
    install_parser.add_argument("package", type=Path)
    build_parser = commands.add_parser("build")
    build_parser.add_argument("source", type=Path)
    build_parser.add_argument("--output", type=Path)
    commands.add_parser("list")
    remove_parser = commands.add_parser("remove")
    remove_parser.add_argument("name")
    args = parser.parse_args(argv)
    try:
        if args.command == "install": print(install(args.package, args.root))
        elif args.command == "build": print(build(args.source, args.output))
        elif args.command == "list": print("\n".join(list_packages(args.root)))
        elif args.command == "remove": print(remove(args.name, args.root))
    except (PackageError, OSError, tarfile.TarError, tomllib.TOMLDecodeError) as exc:
        parser.error(str(exc))
    return 0
