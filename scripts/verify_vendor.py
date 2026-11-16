#!/usr/bin/env python3
"""Check bundled offline assets against their recorded upstream SHA-256."""
from pathlib import Path
import hashlib
import json

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "src" / "agent_library" / "web" / "vendor"
MANIFESTS = {"VERSIONS.json", "katex.VERSION.json", "LICENSES.json"}
REQUIRED_LICENSES = {
    "markdown-it.LICENSE", "dompurify.LICENSE", "dompurify.LICENSE-MPL",
    "highlight.LICENSE", "katex.LICENSE", "katex-fonts.LICENSE-OFL",
    "mdurl.LICENSE", "uc.micro.LICENSE", "entities.LICENSE",
    "linkify-it.LICENSE", "punycode.js.LICENSE",
}


def verify_vendor(vendor=VENDOR):
    vendor = Path(vendor)
    components = []
    for name in sorted(MANIFESTS):
        manifest = vendor / name
        if manifest.is_symlink():
            raise ValueError("Vendor manifests must be regular files")
        value = json.loads(manifest.read_text("utf-8"))
        components.extend(value if isinstance(value, list) else [value])
    known = set(MANIFESTS)
    assets = set()
    checked = 0
    for component in components:
        for entry in component["files"]:
            name = entry["file"]
            path = vendor / name
            if path.is_symlink() or not path.resolve().is_relative_to(vendor.resolve()):
                raise ValueError("Unsafe vendor asset path")
            if name in known:
                raise ValueError("Duplicate vendor asset path")
            known.add(name)
            assets.add(name)
            data = path.read_bytes()
            if len(data) != entry["bytes"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
                raise ValueError(f"Vendor asset mismatch: {name}")
            checked += 1
    if not REQUIRED_LICENSES <= assets:
        raise ValueError("Required bundled component license text is missing")
    font_components = [c for c in components if c.get("name") == "KaTeX mathematical fonts"]
    if len(font_components) != 1 or font_components[0].get("license") != "OFL-1.1":
        raise ValueError("Bundled mathematical fonts require a separate OFL-1.1 notice")
    records = font_components[0].get("provenance", {}).get("font_name_records", {})
    if set(records) != {p.name for p in (vendor / "fonts").glob("*.ttf")}:
        raise ValueError("Font license provenance does not cover every bundled font family")
    for path in vendor.rglob("*"):
        if path.is_symlink():
            raise ValueError("Vendor assets must not contain symlinks")
        if path.is_file() and path.name != ".DS_Store" and path.relative_to(vendor).as_posix() not in known:
            raise ValueError("Unlisted vendor asset: " + path.relative_to(vendor).as_posix())
    return checked


def main():
    try:
        checked = verify_vendor()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Verified {checked} offline vendor assets")


if __name__ == "__main__":
    main()
