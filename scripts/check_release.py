#!/usr/bin/env python3
"""Check source/distribution boundaries without reading user library directories.

This supplements human review: arbitrary private prose cannot be identified
reliably. Fixtures must be fictional, and new release paths/media need review.
"""
from pathlib import Path, PurePosixPath
import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tarfile
import zipfile

TOP_FILES = {
    'README.md', 'README.en.md', 'README.zh-CN.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md',
    'SECURITY.md', 'CONTRIBUTING.md', 'CHANGELOG.md', 'pyproject.toml',
    'package.json', 'package-lock.json', '.gitignore', '.gitattributes', 'MANIFEST.in', 'AGENTS.md',
}
IGNORE_DIRS = {'.git', '.venv', 'venv', 'node_modules', 'build', 'dist', '__pycache__', '.pytest_cache'}
FORBIDDEN_PARTS = {'data', 'libraries', 'transcripts', 'handoffs', 'backups', '.agent-library',
                   'exports', 'extracted', 'local_backup', 'migration_reports', 'annotations', '.claude', '.codex'}
FORBIDDEN_SUFFIXES = {'.sqlite', '.sqlite3', '.db', '.zip', '.part', '.log', '.pyc',
                      '.jsonl', '.pem', '.key', '.p12', '.pfx', '.jks', '.keystore'}
CREDENTIAL_NAMES = {'.env', '.npmrc', '.pypirc', '.credentials.json', 'credentials.json',
                    'auth.json', 'tokens.json', 'token.json', 'session_tokens.json'}
WEB_FILES = {'index.html', 'app.js', 'i18n.js', 'markdown.js', 'style.css'}
VENDOR_METADATA = {'VERSIONS.json', 'katex.VERSION.json', 'LICENSES.json'}
VENDOR_LICENSES = {'markdown-it.LICENSE', 'dompurify.LICENSE', 'dompurify.LICENSE-MPL',
                   'highlight.LICENSE', 'katex.LICENSE', 'mdurl.LICENSE', 'uc.micro.LICENSE',
                   'entities.LICENSE', 'linkify-it.LICENSE', 'punycode.js.LICENSE', 'katex-fonts.LICENSE-OFL'}
VENDOR_RUNTIME = {'markdown-it.min.js', 'purify.min.js', 'highlight.min.js',
                  'highlight-github.min.css', 'katex.min.js', 'katex.min.css'}
DIST_METADATA = {'METADATA', 'WHEEL', 'entry_points.txt', 'top_level.txt', 'RECORD', 'INSTALLER', 'REQUESTED'}
EGG_METADATA = {'PKG-INFO', 'SOURCES.txt', 'dependency_links.txt', 'entry_points.txt', 'top_level.txt', 'requires.txt'}
# Approved fictional screenshot, reviewed independently of the user's library.
APPROVED_MEDIA = {
    'docs/preview-zh-CN.jpg': 'd924323ec4c12d918fb8b5f6df1678532751cf97c46c8bbf526298dfe1187cc2',
    'docs/screenshots/handoff-en.jpg': '1376e90a78d7da36d357afcd9c1f8dacef047ed2826a5c4f8f62b9ce3fb0ed71',
    'docs/screenshots/handoff-zh-CN.jpg': '834eed3274d9a8a5e626b3286c088f388f40e8303a2e9073f72fff756cd7e278',
    'docs/screenshots/import-en.jpg': '84719bea365535de7cf7e0b41315ad4400321f3ab0a7cc752cc90ceb6f3678b9',
    'docs/screenshots/import-zh-CN.jpg': '60fa23c77a92f025d20ef0514ede577dd855e54dd50b288fb3e7cff89c463ff5',
    'docs/screenshots/library-en.jpg': 'a376e3a2b2adf297453237cdb4034a10102c17fb4dffb618b1cdaadf849affab',
    'docs/screenshots/library-zh-CN.jpg': '0ca2b00a1ac081c25f5b27b3bef2b0d68de452c23b471de3ecfd9b8fb3c46072',
}
PERSONAL_PATH = re.compile(rb'/(?:Users|home)/[^/\s\"\'<>]+(?:/|(?=[\s\"\'<>]|$))|[A-Za-z]:\\{1,2}Users\\{1,2}[^\\\s\"\'<>]+(?:\\{1,2}|(?=[\s\"\'<>]|$))')
SENSITIVE_MARKERS = (
    ('private key', re.compile(rb'-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----')),
    ('provider credential', re.compile(rb'(?<![A-Za-z0-9_])sk-(?:ant-(?:api|sid|oat)\d+-|proj-|svcacct-)?[A-Za-z0-9_-]{32,}')),
    ('GitHub credential', re.compile(rb'(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})')),
    ('AWS access identifier', re.compile(rb'(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])')),
    ('literal authorization credential', re.compile(rb'(?i)\bauthorization[\s\"\']*[:=][\s\"\']*(?:bearer|basic)\s+[A-Za-z0-9_./+=-]{20,}')),
)
ROOT = Path(__file__).resolve().parents[1]
MAX_FILES, MAX_FILE_BYTES, MAX_ARCHIVE_BYTES = 100_000, 32 * 1024**2, 256 * 1024**2


def validate_archive_name(name):
    """Reject platform-dependent, escaping or ambiguous paths before reading."""
    if not isinstance(name, str) or not name or '\\' in name or ':' in name or any(ord(c) < 32 for c in name):
        raise ValueError('Unsafe release path')
    body = name[:-1] if name.endswith('/') else name
    parts = body.split('/')
    reserved = {'CON', 'PRN', 'AUX', 'NUL', *('COM' + str(i) for i in range(1, 10)), *('LPT' + str(i) for i in range(1, 10))}
    if any(part in ('', '.', '..') or part.endswith(('.', ' ')) or part.split('.')[0].upper() in reserved for part in parts):
        raise ValueError('Unsafe release path')


def _license_name(name):
    return ('LICENSE' in name.upper() or 'LICENCE' in name.upper() or name.upper().startswith(('NOTICE', 'COPYING'))
            or name == 'THIRD_PARTY_NOTICES.md')


def _approved_font(name):
    try:
        metadata = json.loads((ROOT / 'src/agent_library/web/vendor/katex.VERSION.json').read_text(encoding='utf-8'))
        return 'fonts/' + name in {row['file'] for row in metadata['files']}
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _package_path(parts):
    if len(parts) == 1:
        return parts[0].endswith('.py') and not parts[0].startswith('.')
    if not parts or parts[0] != 'web':
        return False
    if len(parts) == 2:
        return parts[1] in WEB_FILES
    if parts[1] != 'vendor':
        return False
    if len(parts) == 3:
        return parts[2] in VENDOR_METADATA | VENDOR_LICENSES | VENDOR_RUNTIME
    return len(parts) == 4 and parts[2] == 'fonts' and _approved_font(parts[3])


def approved_release_path(name, layout='auto'):
    """Allow reviewed source shapes and known generated packaging metadata."""
    try:
        validate_archive_name(name)
    except ValueError:
        return False
    parts = PurePosixPath(name).parts
    source = (len(parts) == 1 and name in TOP_FILES)
    source |= len(parts) == 3 and parts[:2] == ('src', 'agent_library') and _package_path(parts[2:])
    source |= len(parts) > 3 and parts[:2] == ('src', 'agent_library') and _package_path(parts[2:])
    source |= len(parts) == 2 and parts[0] == 'tests' and parts[1].startswith('test_') and Path(parts[1]).suffix in {'.py', '.cjs'}
    source |= len(parts) == 2 and parts[0] == 'scripts' and (Path(parts[1]).suffix == '.py' or parts[1] in {'start.command', 'start.bat'})
    source |= len(parts) == 2 and parts[0] == 'examples' and Path(parts[1]).suffix in {'.py', '.md'}
    source |= len(parts) == 2 and parts[0] == 'docs' and (Path(parts[1]).suffix == '.md' or name in APPROVED_MEDIA)
    source |= len(parts) > 2 and parts[0] == 'docs' and name in APPROVED_MEDIA
    source |= len(parts) == 3 and parts[:2] == ('.github', 'workflows') and Path(parts[2]).suffix in {'.yml', '.yaml'}
    generated = name in {'PKG-INFO', 'setup.cfg'} or (len(parts) == 3 and parts[:2] == ('src', 'agent_history_library.egg-info') and parts[2] in EGG_METADATA)
    wheel = parts[0] == 'agent_library' and _package_path(parts[1:])
    if re.fullmatch(r'agent_history_library-[A-Za-z0-9_.+-]+\.dist-info', parts[0]):
        wheel |= len(parts) == 2 and parts[1] in DIST_METADATA
        wheel |= (len(parts) == 3 and parts[1] == 'licenses' and parts[2] in {'LICENSE', 'THIRD_PARTY_NOTICES.md'})
        wheel |= (len(parts) == 7 and parts[1:6] == ('licenses', 'src', 'agent_library', 'web', 'vendor') and parts[6] in VENDOR_LICENSES)
    if layout == 'source':
        return bool(source)
    if layout == 'sdist':
        return bool(source or generated)
    if layout == 'wheel':
        return bool(wheel)
    return bool(source or generated or wheel)


def inspect_entry(name, data, layout='auto'):
    """Inspect one logical source/wheel member; never echo matched secret data."""
    try:
        validate_archive_name(name)
    except ValueError:
        return f'Unsafe release path: {name}'
    parts = PurePosixPath(name).parts
    lowered = [part.lower() for part in parts]
    suffix = Path(name).suffix.lower()
    if (any(part in FORBIDDEN_PARTS or part in IGNORE_DIRS for part in lowered)
            or suffix in FORBIDDEN_SUFFIXES or any(part in CREDENTIAL_NAMES or part.startswith('.env.') for part in lowered)
            or re.search(r'\.(?:sqlite3?|db)-(?:wal|shm|journal)$', name, re.I)):
        return f'Private/runtime artifact is not allowed: {name}'
    if not approved_release_path(name, layout):
        return f'Unapproved release path; review and add intentionally: {name}'
    if len(data) > MAX_FILE_BYTES:
        return f'Release file exceeds the inspection limit: {name}'
    if data.startswith(b'SQLite format 3\x00'):
        return f'Database is not allowed in source: {name}'
    if PERSONAL_PATH.search(data):
        return f'Machine-specific home path needs review: {name}'
    for label, marker in SENSITIVE_MARKERS:
        if marker.search(data):
            return f'Possible {label} needs review: {name}'
    if name in APPROVED_MEDIA and hashlib.sha256(data).hexdigest() != APPROVED_MEDIA[name]:
        return f'Reviewed synthetic media changed; inspect before approving: {name}'
    return None


def source_entries(root=None):
    root = ROOT if root is None else Path(root)
    for current, directories, names in os.walk(root, followlinks=False):
        base = Path(current)
        kept = []
        for name in sorted(directories):
            path = base / name
            if name in IGNORE_DIRS or name.endswith('.egg-info'):
                continue
            if path.is_symlink():
                yield path.relative_to(root).as_posix(), None, 'Source release cannot contain symlinks'
            else:
                kept.append(name)
        directories[:] = kept
        for name in sorted(names):
            path = base / name
            relative = path.relative_to(root).as_posix()
            if name in {'.DS_Store', 'Thumbs.db'}:
                continue
            if path.is_symlink():
                yield relative, None, 'Source release cannot contain symlinks'
            elif not path.is_file():
                yield relative, None, 'Source release requires regular files'
            elif not approved_release_path(relative, 'source'):
                yield relative, None, 'Unapproved source file; review and add intentionally'
            elif path.stat().st_size > MAX_FILE_BYTES:
                yield relative, None, 'Source file exceeds the inspection limit'
            else:
                yield relative, path.read_bytes(), None


def _archive_rows(path):
    """Yield verified physical entries without extraction, with bounded reads."""
    seen, total, count = set(), 0, 0
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                count += 1
                validate_archive_name(info.filename)
                folded = info.filename.rstrip('/').casefold()
                if folded in seen:
                    raise ValueError('Duplicate or ambiguous release archive paths')
                seen.add(folded)
                mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                    raise ValueError('Non-regular ZIP member')
                if info.flag_bits & 1:
                    raise ValueError('Encrypted release members cannot be inspected')
                total += info.file_size
                if count > MAX_FILES or info.file_size > MAX_FILE_BYTES or total > MAX_ARCHIVE_BYTES:
                    raise ValueError('Release archive exceeds inspection limits')
                if not info.is_dir():
                    yield info.filename, archive.read(info)
    elif tarfile.is_tarfile(path):
        with tarfile.open(path, 'r:*') as archive:
            for member in archive:
                count += 1
                validate_archive_name(member.name)
                folded = member.name.rstrip('/').casefold()
                if folded in seen:
                    raise ValueError('Duplicate or ambiguous release archive paths')
                seen.add(folded)
                if not member.isdir() and not member.isfile():
                    raise ValueError('Non-regular source archive member')
                total += member.size
                if count > MAX_FILES or member.size > MAX_FILE_BYTES or total > MAX_ARCHIVE_BYTES:
                    raise ValueError('Release archive exceeds inspection limits')
                if member.isfile():
                    yield member.name, archive.extractfile(member).read()
    else:
        raise ValueError('Expected a source archive or wheel ZIP')


def archive_entries(path):
    rows = list(_archive_rows(path))
    names = [name for name, _ in rows]
    layout = 'wheel' if Path(path).suffix == '.whl' else 'sdist'
    prefix = ''
    if names and all('/' in name for name in names):
        top = names[0].split('/', 1)[0]
        if re.fullmatch(r'agent[-_]history[-_]library-[A-Za-z0-9_.+-]+|agent-library', top) and all(name.startswith(top + '/') for name in names):
            prefix = top + '/'
    for name, data in rows:
        logical = name.removeprefix(prefix)
        error = None if approved_release_path(logical, layout) else 'Unapproved distribution member'
        yield logical, data, error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', nargs='?', type=Path, help='Optional built wheel/source archive to inspect')
    args = parser.parse_args()
    failures, checked = [], 0
    try:
        for name, data, error in archive_entries(args.archive) if args.archive else source_entries():
            checked += 1
            failure = error or inspect_entry(name, data)
            if failure:
                failures.append(f'{name}: {failure}')
    except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as exc:
        failures.append(str(exc))
    if failures:
        print('\n'.join(failures), file=sys.stderr)
        return 1
    print(f'Checked {checked} source files; approved paths and privacy markers passed')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
