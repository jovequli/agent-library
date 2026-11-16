"""Negative release-package checks with source-only, fictional fixtures."""
from __future__ import annotations

import base64
import csv
import io
import json
from pathlib import Path
import hashlib
import stat
import sys
import tarfile
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import check_distribution as distribution


class DistributionChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent-library-dist-test-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.metadata = "agent_history_library-0.1.0.dist-info/"
        self.files = {}
        for name in distribution.CORE:
            self.files["agent_library/" + name] = (ROOT / "src/agent_library" / name).read_bytes()
        for name in ("index.html", "app.js", "i18n.js", "markdown.js", "style.css"):
            self.files["agent_library/web/" + name] = (ROOT / "src/agent_library/web" / name).read_bytes()
        vendor = ROOT / "src/agent_library/web/vendor"
        manifests = ["VERSIONS.json", "katex.VERSION.json", "LICENSES.json"]
        for name in manifests:
            self.files["agent_library/web/vendor/" + name] = (vendor / name).read_bytes()
            components = json.loads((vendor / name).read_bytes())
            if isinstance(components, dict):
                components = [components]
            for component in components:
                for entry in component["files"]:
                    self.files["agent_library/web/vendor/" + entry["file"]] = (vendor / entry["file"]).read_bytes()
        for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
            self.files[self.metadata + "licenses/" + name] = (ROOT / name).read_bytes()
        declarations = ["LICENSE", "THIRD_PARTY_NOTICES.md"]
        for name in distribution.VENDOR_LICENSES:
            relative = "src/agent_library/web/vendor/" + name
            self.files[self.metadata + "licenses/" + relative] = (vendor / name).read_bytes()
            declarations.append(relative)
        self.files[self.metadata + "METADATA"] = ("Metadata-Version: 2.4\nName: agent-history-library\nVersion: 0.1.0\nLicense-Expression: " + distribution.LICENSE_EXPRESSION + "\n" + "".join("License-File: " + path + "\n" for path in declarations) + "\n").encode()
        self.files[self.metadata + "WHEEL"] = b"Wheel-Version: 1.0\nGenerator: synthetic-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"

    def wheel(self, changes=None):
        files = {**self.files, **(changes or {})}
        files = {name: value for name, value in files.items() if value is not None}
        record = io.StringIO(newline="")
        writer = csv.writer(record)
        for name, data in sorted(files.items()):
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            writer.writerow((name, "sha256=" + digest, len(data)))
        writer.writerow((self.metadata + "RECORD", "", ""))
        files[self.metadata + "RECORD"] = record.getvalue().encode()
        target = self.directory / "synthetic.whl"
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in files.items():
                archive.writestr(name, data)
        return target

    def test_complete_wheel_contains_all_notices_and_declared_licenses(self):
        result = distribution.inspect_distribution(self.wheel())
        self.assertEqual(result["kind"], "wheel")
        self.assertEqual(result["verified_vendor_assets"], 77)
        self.assertEqual(result["fonts"], 60)

    def test_missing_font_permission_is_rejected_even_with_updated_record(self):
        path = "agent_library/web/vendor/katex-fonts.LICENSE-OFL"
        with self.assertRaisesRegex(ValueError, "incomplete"):
            distribution.inspect_distribution(self.wheel({path: None}))

    def test_missing_license_metadata_copy_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "declared wheel license"):
            distribution.inspect_distribution(self.wheel({self.metadata + "licenses/THIRD_PARTY_NOTICES.md": None}))

    def test_incorrect_bundle_license_expression_is_rejected(self):
        metadata = self.files[self.metadata + "METADATA"].replace(distribution.LICENSE_EXPRESSION.encode(), b"MIT")
        with self.assertRaisesRegex(ValueError, "third-party licenses"):
            distribution.inspect_distribution(self.wheel({self.metadata + "METADATA": metadata}))

    def test_mismatching_notice_copy_is_rejected(self):
        key = self.metadata + "licenses/src/agent_library/web/vendor/entities.LICENSE"
        with self.assertRaisesRegex(ValueError, "does not match"):
            distribution.inspect_distribution(self.wheel({key: b"A replacement is not the preserved license."}))

    def test_required_migration_module_cannot_be_dropped(self):
        with self.assertRaisesRegex(ValueError, "incomplete"):
            distribution.inspect_distribution(self.wheel({"agent_library/legacy.py": None}))

    def test_private_payload_is_rejected_inside_complete_wheel(self):
        with self.assertRaises(ValueError):
            distribution.inspect_distribution(self.wheel({"agent_library/credentials.json": b'{"fictional":true}'}))

    def test_zip_symlink_is_rejected(self):
        target = self.directory / "link.whl"
        with zipfile.ZipFile(target, "w") as archive:
            item = zipfile.ZipInfo("agent_library/link.py")
            item.create_system = 3
            item.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(item, "elsewhere")
        with self.assertRaisesRegex(ValueError, "link"):
            distribution.distribution_files(target)

    def test_tar_root_traversal_is_rejected_before_stripping(self):
        target = self.directory / "traversal.tar.gz"
        with tarfile.open(target, "w:gz") as archive:
            item = tarfile.TarInfo("../LICENSE")
            item.size = 1
            archive.addfile(item, io.BytesIO(b"x"))
        with self.assertRaises(ValueError):
            distribution.distribution_files(target)

    def test_archive_limits_reject_oversized_source_member(self):
        with self.assertRaisesRegex(ValueError, "size limits"):
            distribution.validate_members(["src/agent_library/core.py"], [distribution.MAX_FILE_BYTES + 1])

    def test_bilingual_readme_images_resolve_inside_source_package(self):
        files = {"README.md": b"![Fictional demo](docs/screenshots/library-zh-CN.jpg)",
                 "README.en.md": b"![Fictional demo](docs/screenshots/library-en.jpg)",
                 "README.zh-CN.md": b"[Default](README.md)",
                 "docs/screenshots/library-zh-CN.jpg": b"fictional image fixture",
                 "docs/screenshots/library-en.jpg": b"fictional image fixture"}
        distribution.validate_readme_images(files)
        del files["docs/screenshots/library-en.jpg"]
        with self.assertRaisesRegex(ValueError, "README image is missing"):
            distribution.validate_readme_images(files)


if __name__ == "__main__":
    unittest.main()
