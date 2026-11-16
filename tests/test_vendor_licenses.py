"""Offline release regressions for actual bundled third-party attributions."""
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("verify_vendor", ROOT / "scripts/verify_vendor.py")
VERIFY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFY)


class VendorLicenseTests(unittest.TestCase):
    def fixture(self):
        temporary = tempfile.TemporaryDirectory(prefix="agent-library-vendor-test-")
        self.addCleanup(temporary.cleanup)
        target = Path(temporary.name) / "vendor"
        shutil.copytree(VERIFY.VENDOR, target)
        return target

    def test_all_runtime_assets_and_embedded_dependency_notices_are_complete(self):
        self.assertEqual(VERIFY.verify_vendor(), 77)
        metadata = json.loads((VERIFY.VENDOR / "LICENSES.json").read_text())
        embedded = {c["name"]: c for c in metadata if c.get("bundled_in") == "markdown-it.min.js"}
        self.assertEqual(set(embedded), {"mdurl", "uc.micro", "entities", "linkify-it", "punycode.js"})
        self.assertEqual(embedded["entities"]["license"], "BSD-2-Clause")
        self.assertTrue(all(c["bundle_provenance"]["matched_bundled_sources"] for c in embedded.values()))

    def test_font_notice_is_separate_from_code_mit_and_retains_all_reserved_names(self):
        metadata = json.loads((VERIFY.VENDOR / "LICENSES.json").read_text())
        fonts = next(c for c in metadata if c["name"] == "KaTeX mathematical fonts")
        self.assertEqual(fonts["license"], "OFL-1.1")
        text = (VERIFY.VENDOR / "katex-fonts.LICENSE-OFL").read_text()
        self.assertIn("SIL OPEN FONT LICENSE Version 1.1", text)
        for source in fonts["provenance"]["font_name_records"].values():
            name = source.split("with Reserved Font Name ", 1)[1].split(".", 1)[0]
            self.assertIn(name, text)

    def test_missing_transitive_notice_fails(self):
        vendor = self.fixture()
        (vendor / "entities.LICENSE").unlink()
        with self.assertRaises(OSError):
            VERIFY.verify_vendor(vendor)

    def test_unknown_file_is_not_silently_bundled(self):
        vendor = self.fixture()
        (vendor / "unlisted.txt").write_text("Fictional unreviewed asset")
        with self.assertRaisesRegex(ValueError, "Unlisted vendor asset"):
            VERIFY.verify_vendor(vendor)

    def test_font_cannot_be_incorrectly_relabelled_mit(self):
        vendor = self.fixture()
        path = vendor / "LICENSES.json"
        metadata = json.loads(path.read_text())
        next(c for c in metadata if c["name"] == "KaTeX mathematical fonts")["license"] = "MIT"
        path.write_text(json.dumps(metadata))
        with self.assertRaisesRegex(ValueError, "OFL-1.1"):
            VERIFY.verify_vendor(vendor)


if __name__ == "__main__":
    unittest.main()
