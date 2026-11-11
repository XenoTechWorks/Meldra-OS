# SPDX-License-Identifier: GPL-2.0-only
import io
from pathlib import Path
import tarfile
import tempfile
import unittest

from kiln.cli import PackageError, build, install, list_packages, remove


class PackageManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.root = self.work / "rootfs"
        self.root.mkdir()

    def package(self, unsafe: bool = False) -> Path:
        path = self.work / "hello.kiln"
        with tarfile.open(path, "w:gz") as archive:
            manifest = b'[package]\nname="hello"\nversion="1.0"\n'
            info = tarfile.TarInfo("KILN.toml")
            info.size = len(manifest)
            archive.addfile(info, io.BytesIO(manifest))
            payload = b"hello\n"
            info = tarfile.TarInfo("../escape" if unsafe else "root/usr/bin/hello")
            info.size = len(payload)
            info.mode = 0o755
            archive.addfile(info, io.BytesIO(payload))
        return path

    def test_install_list_remove(self):
        self.assertIn("installed hello 1.0", install(self.package(), self.root))
        self.assertEqual(list_packages(self.root), ["hello 1.0"])
        self.assertTrue((self.root / "usr/bin/hello").exists())
        self.assertEqual(remove("hello", self.root), "removed hello")
        self.assertFalse((self.root / "usr/bin/hello").exists())

    def test_rejects_path_traversal(self):
        with self.assertRaises(PackageError):
            install(self.package(unsafe=True), self.root)

    def setuid_package(self, name: str, path: str) -> Path:
        package = self.work / f"{name}.kiln"
        with tarfile.open(package, "w:gz") as archive:
            manifest = f'[package]\nname="{name}"\nversion="1.0"\n'.encode()
            info = tarfile.TarInfo("KILN.toml")
            info.size = len(manifest)
            archive.addfile(info, io.BytesIO(manifest))
            info = tarfile.TarInfo(path)
            info.size = 6
            info.mode = 0o4755
            archive.addfile(info, io.BytesIO(b"hello\n"))
        return package

    def test_restores_setuid_only_for_allowlisted_helpers(self):
        install(self.setuid_package("polkit", "root/usr/lib/polkit-1/polkit-agent-helper-1"), self.root)
        helper = self.root / "usr/lib/polkit-1/polkit-agent-helper-1"
        self.assertEqual(helper.stat().st_mode & 0o7777, 0o4755)

    def test_rejects_setuid_file_not_on_allowlist(self):
        with self.assertRaises(PackageError):
            install(self.setuid_package("hello", "root/usr/bin/hello"), self.root)
        self.assertFalse((self.root / "usr/bin/hello").exists())
        # The path alone is not enough: the allowlist is keyed by the owning package too.
        with self.assertRaises(PackageError):
            install(self.setuid_package("impostor", "root/usr/lib/polkit-1/polkit-agent-helper-1"), self.root)

    def test_hard_links_are_packaged_as_copies(self):
        source = self.work / "linked"
        (source / "root/usr/bin").mkdir(parents=True)
        (source / "KILN.toml").write_text('[package]\nname="linked"\nversion="1"\n', encoding="utf-8")
        original = source / "root/usr/bin/mke2fs"
        original.write_text("tool\n", encoding="utf-8")
        (source / "root/usr/bin/mkfs.ext4").hardlink_to(original)
        package = self.work / "linked.kiln"
        build(source, package)
        with tarfile.open(package) as archive:
            self.assertFalse(any(member.islnk() for member in archive.getmembers()))
        install(package, self.root)
        self.assertEqual((self.root / "usr/bin/mkfs.ext4").read_text(), "tool\n")

    def test_builds_installable_package(self):
        source = self.work / "source"
        (source / "root/usr/share/hello").mkdir(parents=True)
        (source / "KILN.toml").write_text(
            '[package]\nname="built-hello"\nversion="2.0"\n', encoding="utf-8"
        )
        (source / "root/usr/share/hello/message").write_text("hello\n", encoding="utf-8")
        package = self.work / "built.kiln"
        self.assertIn("built", build(source, package))
        self.assertIn("installed built-hello 2.0", install(package, self.root))
        self.assertEqual((self.root / "usr/share/hello/message").read_text(), "hello\n")

    def test_dependencies_are_enforced_for_install_and_remove(self):
        def make_package(name: str, dependencies: list[str]) -> Path:
            source = self.work / name
            (source / "root/usr/share" / name).mkdir(parents=True)
            dependency_list = ", ".join(f'"{item}"' for item in dependencies)
            (source / "KILN.toml").write_text(
                f'[package]\nname = "{name}"\nversion = "1"\n'
                f'dependencies = [{dependency_list}]\n',
                encoding="utf-8",
            )
            (source / "root/usr/share" / name / "data").write_text(name, encoding="utf-8")
            archive = self.work / f"{name}.kiln"
            build(source, archive)
            return archive

        base = make_package("base", [])
        app = make_package("app", ["base"])
        with self.assertRaisesRegex(PackageError, "missing dependencies"):
            install(app, self.root)
        install(base, self.root)
        install(app, self.root)
        with self.assertRaisesRegex(PackageError, "required by"):
            remove("base", self.root)

    def test_safe_symlink_installs_and_escape_is_rejected(self):
        source = self.work / "links"
        (source / "root/usr/lib").mkdir(parents=True)
        (source / "root/usr/lib/libdemo.so.1").write_text("library", encoding="utf-8")
        (source / "root/usr/lib/libdemo.so").symlink_to("libdemo.so.1")
        (source / "KILN.toml").write_text(
            '[package]\nname="links"\nversion="1"\n', encoding="utf-8"
        )
        archive = self.work / "links.kiln"
        build(source, archive)
        install(archive, self.root)
        self.assertEqual((self.root / "usr/lib/libdemo.so").readlink(), Path("libdemo.so.1"))

        unsafe = self.work / "unsafe-link.kiln"
        with tarfile.open(unsafe, "w:gz") as bundle:
            manifest = b'[package]\nname="unsafe-link"\nversion="1"\n'
            info = tarfile.TarInfo("KILN.toml")
            info.size = len(manifest)
            bundle.addfile(info, io.BytesIO(manifest))
            info = tarfile.TarInfo("root/usr/lib/escape")
            info.type = tarfile.SYMTYPE
            info.linkname = "../../../../outside"
            bundle.addfile(info)
        with self.assertRaisesRegex(PackageError, "unsafe archive symlink"):
            install(unsafe, self.root)
