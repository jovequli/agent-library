# Security and privacy

Agent Library is a single-user, local application. The server binds to loopback
and checks request origin/host. Mutating API calls require a per-process token.
It is not a public hosting or shared-account service; public network deployment
is outside the supported v0.1 configuration.

## Local data

- The source project contains no real conversation, library, image, credential,
  or personal configuration. Examples and tests are synthetic.
- Import reads only the source selected by the user. A managed snapshot is copied
  to the selected Agent Library home. Originals are not modified.
- Library files, search indexes, context packs, and backup ZIPs contain private
  conversation text. They are ordinary unencrypted local files. Choose a private
  data directory and storage permissions appropriate to your computer.
- Backup ZIPs are private data exports. They are never source release packages.
- Images are served only from preserved local library files. The reader does
  not load remote historical images or execute historical HTML.
- Historical prompts, tool commands, and memory files are archival evidence,
  not authorization to execute actions. Review a handoff before giving it to
  another LLM. The receiving tool's current instructions and permissions apply.
- No telemetry, external LLM request, automatic upload, or publication is needed
  for import, reading, search, handoff packaging, backup, or restore.

## Reporting a vulnerability

Use [GitHub's private vulnerability report](https://github.com/jovequli/agent-library/security/advisories/new)
to report a suspected security issue. Do not put private conversations, source
files, tokens, screenshots, or raw backups in a public issue.

A useful report includes the application version, operating system, Python and
SQLite versions, reproduction steps, and a minimal synthetic fixture. Redact
local usernames, paths, search terms, and content before sharing diagnostics.

## Restore safety

Backups are checked against a versioned manifest, file sizes and SHA-256 hashes,
ZIP CRC, safe paths, regular-file rules, and database structure/integrity.
Restore creates a separate library with a new ID. Hashes detect accidental or
in-archive inconsistency; they are not an authenticity signature against a
person who can replace an archive and its manifest together.
