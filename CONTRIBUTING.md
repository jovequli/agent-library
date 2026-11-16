# Contributing

Agent Library is a local-first library of readable agent conversations and
portable task handoffs. The first importers cover Claude exports, Claude Code,
and Cowork. Keep the common library model independent of any provider.

## Development

Use Python 3.11 or newer. From the project root:

```sh
python3 -m venv .venv
# Windows: use py -3 -m venv .venv instead.
# Activate .venv using the command appropriate to your shell.
python -m pip install -e .
python -m unittest discover -s tests -v
python scripts/verify_vendor.py
python scripts/check_release.py
```

For renderer tests, install Node.js 26+ (or 22.22.2+/24.15+ as supported by jsdom), then run:

```sh
npm ci
npm test
```

Node.js is a development dependency. Reading and searching a library require
Python only; frontend libraries and fonts are already bundled for offline use.

## Changes

- Use synthetic fixtures. Never commit actual exports, private paths, library
  databases, screenshots of real conversations, backups, or handoff packs.
- Keep raw sources read-only, preserve provenance, and distinguish user content
  from system, tools, partial history, and generated context.
- Add a fixture and a focused regression test for a new export variant.
- Localize primary interface text in both `zh-CN` and `en`.
- Do not add remote requests, telemetry, or LLM calls to core workflows.
- Schema or backup format changes need an explicit version and migration or
  refusal behavior. Failed imports must leave the active generation usable.
- Keep license and integrity records for vendored dependencies.

Before a release, run the test suites on clean environments, inspect the built
wheel/source archive using the release check, and review the publication contents
and Git history. Report vulnerabilities through the private channel in
[SECURITY.md](SECURITY.md), without uploading private conversation archives.
