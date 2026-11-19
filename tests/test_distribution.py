"""Validate built archives contain the public plugin payload only."""
from __future__ import annotations

import tarfile
import unittest
import zipfile
from pathlib import Path

DIST = Path(__file__).resolve().parents[1] / "dist"
REQUIRED_ROOT = {"plugin.py", "deai.py", "config.toml", "_manifest.json"}


class DistributionTests(unittest.TestCase):
    def test_source_archive_has_plugin_root_and_no_host_artifacts(self):
        archives = list(DIST.glob("*.tar.gz"))
        self.assertEqual(len(archives), 1, f"expected one sdist, found {archives}")
        with tarfile.open(archives[0], "r:gz") as archive:
            names = archive.getnames()
        prefix = archives[0].name.removesuffix(".tar.gz") + "/"
        root_files = {
            name[len(prefix):]
            for name in names
            if name.startswith(prefix) and "/" not in name[len(prefix):]
        }
        self.assertTrue(REQUIRED_ROOT <= root_files, f"missing plugin files: {REQUIRED_ROOT - root_files}")
        self.assertFalse(
            any("nas_" in name or "/nas-" in name for name in names),
            "host-only file leaked into sdist",
        )

    def test_wheel_has_runtime_modules_and_no_host_artifacts(self):
        wheels = list(DIST.glob("*.whl"))
        self.assertEqual(len(wheels), 1, f"expected one wheel, found {wheels}")
        with zipfile.ZipFile(wheels[0]) as archive:
            names = archive.namelist()
        self.assertIn("plugin.py", names)
        self.assertIn("deai.py", names)
        self.assertFalse(
            any("nas_" in name or "nas-" in name for name in names),
            "host-only file leaked into wheel",
        )


if __name__ == "__main__":
    unittest.main()
