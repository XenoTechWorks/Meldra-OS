# SPDX-License-Identifier: GPL-2.0-only
import tempfile
from pathlib import Path
import unittest

from tools.component_audit import audit


class ComponentAuditTests(unittest.TestCase):
    def manifest(self, body: str) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "components.toml"
        path.write_text(body, encoding="utf-8")
        return path

    def test_study_may_be_unpinned(self):
        path = self.manifest('''
schema_version = 1
[[component]]
name = "example"
origin = "https://example.com/source.git"
revision = "UNPINNED"
license = "BSD-2-Clause"
target = "docs/studies/example"
owner = "unassigned"
status = "study"
strategy = "design-study"
''')
        self.assertEqual(audit(path), [])

    def test_prototype_requires_pin_and_owner(self):
        path = self.manifest('''
schema_version = 1
[[component]]
name = "example"
origin = "http://example.com/source.git"
revision = "UNPINNED"
license = "BSD-2-Clause"
target = "../escape"
owner = "unassigned"
status = "prototype"
strategy = "clean-port"
''')
        errors = "\n".join(audit(path))
        self.assertIn("canonical HTTPS", errors)
        self.assertIn("immutable revision", errors)
        self.assertIn("require an owner", errors)
        self.assertIn("safe path", errors)


if __name__ == "__main__":
    unittest.main()

