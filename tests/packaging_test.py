"""Tests for the standalone Blender extension layout and package builder."""

from __future__ import annotations

import os
import sys
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

import build_addon


class PackagingTests(unittest.TestCase):
    def test_source_layout_matches_manifest(self) -> None:
        build_addon.validate_source_layout()
        with (build_addon.SOURCE / "blender_manifest.toml").open("rb") as manifest_file:
            manifest = tomllib.load(manifest_file)

        self.assertEqual(build_addon.PROJECT_ROOT, PROJECT_ROOT)
        self.assertEqual(build_addon.SOURCE, PROJECT_ROOT / manifest["id"])
        self.assertEqual(build_addon.PACKAGE_ID, manifest["id"])
        self.assertEqual(build_addon.PACKAGE_VERSION, manifest["version"])

    def test_staged_zip_has_extension_files_at_archive_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            executable = temporary / ("bfrass.exe" if os.name == "nt" else "bfrass")
            library = temporary / build_addon.native_library_name()
            bfrass_license = temporary / "BfrAss-LICENSE.txt"
            assimp_license = temporary / "Assimp-LICENSE.txt"
            executable.write_bytes(b"test converter")
            library.write_bytes(b"test native bridge")
            bfrass_license.write_text("BfrAss test license", encoding="utf-8")
            assimp_license.write_text("Assimp test license", encoding="utf-8")

            stage = temporary / "package"
            build_addon.copy_extension(
                stage,
                executable,
                library,
                bfrass_license,
                assimp_license,
            )
            package = temporary / "extension.zip"
            build_addon.make_zip(stage, package)

            with zipfile.ZipFile(package) as archive:
                names = set(archive.namelist())

        converter_name = "bin/bfrass.exe" if os.name == "nt" else "bin/bfrass"
        self.assertIn("__init__.py", names)
        self.assertIn("blender_manifest.toml", names)
        self.assertIn("data/material_semantics.json", names)
        self.assertIn("core/terrain.py", names)
        self.assertIn("terrain_import.py", names)
        self.assertIn(converter_name, names)
        self.assertIn(f"bin/{build_addon.native_library_name()}", names)
        self.assertIn("LICENSE", names)
        self.assertIn("licenses/THIRD_PARTY_NOTICES.md", names)
        self.assertIn("licenses/BfrAss-LICENSE.txt", names)
        self.assertIn("licenses/Assimp-LICENSE.txt", names)
        self.assertFalse(any("__pycache__" in name or name.endswith(".pyc") for name in names))
        self.assertFalse(any(name.startswith(f"{build_addon.PACKAGE_ID}/") for name in names))


if __name__ == "__main__":
    unittest.main()
