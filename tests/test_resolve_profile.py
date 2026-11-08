from __future__ import annotations

import unittest

from tools.resolve_profile import ResolutionError, resolve


class ResolverTests(unittest.TestCase):
    def test_dependency_order(self):
        repository = {
            "desktop": {"dependencies": ["shell", "base"]},
            "shell": {"dependencies": ["graphics"]},
            "graphics": {"dependencies": ["base"]},
            "base": {"dependencies": []},
        }
        self.assertEqual(resolve(repository, ["desktop"]), ["base", "graphics", "shell", "desktop"])

    def test_cycle_rejected(self):
        repository = {"a": {"dependencies": ["b"]}, "b": {"dependencies": ["a"]}}
        with self.assertRaisesRegex(ResolutionError, "dependency cycle"):
            resolve(repository, ["a"])

    def test_missing_package_rejected(self):
        with self.assertRaisesRegex(ResolutionError, "unknown package"):
            resolve({"a": {"dependencies": ["missing"]}}, ["a"])
