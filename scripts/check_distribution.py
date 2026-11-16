#!/usr/bin/env python3
"""Inspect an actual wheel or sdist using only the Python standard library.

Checks completeness, offline assets/licenses, wheel RECORD hashes, and source
release boundaries. It does not build, publish, or extract the distribution.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile
import zipfile

from check_release import inspect_entry, validate_archive_name

VENDOR_LICENSES = ("markdown-it.LICENSE", "dompurify.LICENSE", "dompurify.LICENSE-MPL", "highlight.LICENSE", "katex.LICENSE",
                   "mdurl.LICENSE", "uc.micro.LICENSE", "entities.LICENSE", "linkify-it.LICENSE", "punycode.js.LICENSE", "katex-fonts.LICENSE-OFL")
CORE = ("__init__.py", "__main__.py", "cli.py", "core.py", "schema.py", "ingest.py", "importers.py", "query.py", "handoff.py", "backup.py", "server.py", "legacy.py")
MAX_MEMBERS = 10_000
MAX_FILE_BYTES = 32 * 1024**2
MAX_TOTAL_BYTES = 200 * 1024**2
LICENSE_EXPRESSION = "MIT AND BSD-2-Clause AND BSD-3-Clause AND Apache-2.0 AND OFL-1.1"
README_FILES = ("README.md", "README.en.md", "README.zh-CN.md")
README_SCREENSHOTS = tuple("docs/screenshots/" + view + "-" + language + ".jpg"
                          for language in ("zh-CN", "en") for view in ("library", "handoff", "import"))


def validate_readme_images(files):
    """Keep both language pages' embedded screenshots usable in source packages."""
    for name in README_FILES:
        text = files[name].decode("utf-8")
        for target in re.findall(r"!\[[^\]]*\]\(([^)\s]+)\)", text):
            validate_archive_name(target)
            if target not in files:
                raise ValueError("README image is missing from source distribution: " + target)


def validate_members(names, sizes):
    if len(names) > MAX_MEMBERS or any(size > MAX_FILE_BYTES for size in sizes) or sum(sizes) > MAX_TOTAL_BYTES:
        raise ValueError("Distribution exceeds source release size limits")
    if len(names) != len(set(name.casefold() for name in names)):
        raise ValueError("Distribution contains duplicate or ambiguous paths")
    for name in names:
        validate_archive_name(name)


def distribution_files(path):
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            infos = [info for info in archive.infolist() if not info.is_dir()]
            names = [info.filename for info in infos]
            validate_members(names, [info.file_size for info in infos])
            for info in infos:
                mode = stat.S_IFMT(info.external_attr >> 16)
                if mode not in (0, stat.S_IFREG) or info.flag_bits & 1:
                    raise ValueError("Wheel contains a link, special file or encrypted member")
            return "wheel", {info.filename: archive.read(info) for info in infos}
    with tarfile.open(path, "r:*") as archive:
        members = [member for member in archive.getmembers() if not member.isdir()]
        if any(not member.isfile() for member in members):
            raise ValueError("Source archive contains non-regular files")
        names = [member.name for member in members]
        validate_members(names, [member.size for member in members])
        tops = {PurePosixPath(name).parts[0] for name in names}
        if len(tops) != 1:
            raise ValueError("Source archive must have a single root directory")
        prefix = tops.pop() + "/"
        return "sdist", {member.name.removeprefix(prefix): archive.extractfile(member).read() for member in members}


def inspect_distribution(path):
    kind, files = distribution_files(Path(path))
    for name, data in files.items():
        parts = PurePosixPath(name).parts
        if name.startswith("/") or ".." in parts or "\\" in name:
            raise ValueError("Unsafe distribution path")
        if any(p in {".venv", "node_modules", ".git", "__pycache__"} for p in parts):
            raise ValueError("Development/runtime directory was bundled")
        failure = inspect_entry(name, data, layout=kind)
        if failure:
            raise ValueError(failure)
    prefix = "agent_library/" if kind == "wheel" else "src/agent_library/"
    required = {prefix + item for item in CORE}
    required.update(prefix + "web/" + item for item in ("index.html", "app.js", "i18n.js", "markdown.js", "style.css"))
    vendor = prefix + "web/vendor/"
    required.update(vendor + item for item in VENDOR_LICENSES)
    required.update({vendor + "VERSIONS.json", vendor + "katex.VERSION.json", vendor + "LICENSES.json"})
    if kind == "sdist":
        required.update({*README_FILES, *README_SCREENSHOTS, "LICENSE", "THIRD_PARTY_NOTICES.md", "SECURITY.md", "CONTRIBUTING.md", "pyproject.toml", "MANIFEST.in", "scripts/start.command", "scripts/start.bat", "scripts/check_distribution.py"})
    missing = required - set(files)
    if missing:
        raise ValueError("Distribution is incomplete: " + ", ".join(sorted(missing)))
    if kind == "sdist":
        validate_readme_images(files)
    components = json.loads(files[vendor + "VERSIONS.json"])
    components.append(json.loads(files[vendor + "katex.VERSION.json"]))
    components.extend(json.loads(files[vendor + "LICENSES.json"]))
    asset_count = 0
    for component in components:
        for row in component["files"]:
            data = files.get(vendor + row["file"])
            if data is None or len(data) != row["bytes"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
                raise ValueError("Bundled asset mismatch: " + row["file"])
            asset_count += 1
    if kind == "wheel":
        records = [name for name in files if name.endswith(".dist-info/RECORD")]
        if len(records) != 1 or not any(name.endswith(".dist-info/licenses/LICENSE") for name in files):
            raise ValueError("Wheel metadata/license missing")
        recorded = set()
        for name, digest, size in csv.reader(io.StringIO(files[records[0]].decode("utf-8"))):
            if name in recorded or name not in files:
                raise ValueError("Wheel RECORD does not match its files")
            recorded.add(name)
            if name == records[0]:
                continue
            data = files[name]
            expected = "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
            if digest != expected or int(size) != len(data):
                raise ValueError("Wheel RECORD checksum mismatch")
        if recorded != set(files):
            raise ValueError("Wheel contains unlisted files")
        metadata_names = [name for name in files if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            raise ValueError("Wheel requires exactly one package metadata file")
        metadata = BytesParser().parsebytes(files[metadata_names[0]])
        if metadata.get("License-Expression") != LICENSE_EXPRESSION:
            raise ValueError("Wheel must identify the source and bundled third-party licenses")
        license_prefix = metadata_names[0].rsplit("/", 1)[0] + "/licenses/"
        declarations = set(metadata.get_all("License-File", []))
        expected_licenses = {"LICENSE", "THIRD_PARTY_NOTICES.md", *["src/agent_library/web/vendor/" + item for item in VENDOR_LICENSES]}
        if not expected_licenses <= declarations:
            raise ValueError("Wheel metadata is missing source or third-party license declarations")
        if any(license_prefix + item not in files for item in declarations):
            raise ValueError("A declared wheel license file is missing")
        for item in VENDOR_LICENSES:
            if files[license_prefix + "src/agent_library/web/vendor/" + item] != files[vendor + item]:
                raise ValueError("Wheel license metadata does not match the bundled license text")
    return {"kind": kind, "members": len(files), "verified_vendor_assets": asset_count, "fonts": sum("/fonts/" in name for name in files), "source_boundary": "passed"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    print(json.dumps(inspect_distribution(args.archive), indent=2))


if __name__ == "__main__":
    main()
