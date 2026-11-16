# Architecture and supported formats

Agent Library is a local conversation library with a buildless browser reader.
The Python runtime uses only the standard library. The web renderer, fonts and
licenses are vendored; ordinary use has no network dependency.

## Private storage

The source checkout and installed package contain no user libraries. The data
home defaults to `~/.agent-library`; `--home` or `AGENT_LIBRARY_HOME` overrides it.
Each independent library has its own identifier, metadata, generation pointer,
immutable generations and edited task notes. A generation contains:

```
archive.sqlite3     normalized sessions, message blocks, media index and search
imports.json        source fingerprints and import receipts
report.json         last import counts and warnings
sources/            managed copies of supported original history files
media/              deduplicated preserved raster images
```

SQLite `user_version=1` identifies the schema. Unsupported versions are rejected
without rewriting data. A future schema change must define and test a migration.
FTS5 trigram is used when available; a literal LIKE search handles short queries
and builds without trigram. All queries explicitly select a library.

## Importing

`importers.parse_source` adapts Claude exports, Claude Code JSONL and Cowork
snapshots into provider-neutral session/message dictionaries. Each record keeps
its native identifier, role, block kind, historical project, timestamps, parent
relationships and relative source file/line. Missing conversation bytes are
reported honestly; input history is not represented as a complete conversation.

The import coordinator snapshots the selected input, rejects unsafe ZIP paths,
skips symlinks and unrelated/credential files, and works in a temporary generation.
It merges conversations by `(dataset, provider, native_id)` and message blocks by
stable native identifiers. Missing old blocks and expanded response snapshots
are retained; stale imports cannot replace newer titles or truncate replies.
Imports are additive rather than a mirror of source deletions.

Only after database validation, source receipt creation and media indexing does
an atomic `CURRENT` pointer activate the complete result. A failed import leaves
the previous generation readable. Writes to one library use an OS-backed SQLite
lock; readers use immutable snapshots. Historical generations are retained in
v0.1, so repeated updates require additional disk space. Backups include only the
active generation, preserving all source snapshots referenced by that generation.

## Reading and search

Existing normalized archives can be copied with the explicit `migrate-legacy`
adapter. This path retains every original row, sequence, UUID, classification,
source file, generated document and image binding instead of running raw
adapters again. Unique native block identifiers preserve duplicate historical
UUIDs through context-pack reimport. Unbound images remain session attachments;
they never become invented user/assistant turns. A private source manifest and
exact content comparison are validated before the new library is activated.
Both source roots are explicit; no live project paths from history are followed.

The server binds only to `127.0.0.1`. Host/Origin checks protect loopback requests;
mutations also require a session token obtained from the same-origin application.
There is no public hosting mode. Static, source and media paths cannot escape
their permitted roots, and media is served only through the active library index.

By default only user/assistant text, image and input-history blocks appear in the
reader. Technical details are explicit. Opting into other record types extends
search to tools, thinking and context; opening a hidden match enables its details.
Message windows use stable ordinals, open at the newest end, and preserve the
scroll anchor when preceding messages are added. Previews require an explicit
action to navigate.

Historical HTML is disabled and rendered Markdown is sanitized. Math uses
untrusted KaTeX settings. Images resolve only to managed local media; remote
resources are never automatically fetched.

## Task handoff and interchange

Context ZIPs contain `conversation.md`, `requirements.md`, `handoff.md`,
`handoff.json`, `conversation.json`, preserved media and checksums. The full
historical requirements remain inspectable. Editable goals, constraints,
decisions, completed/pending work and related files accompany the original
evidence. Initial goal/next-step fields are explicitly source excerpts, not a
verified AI summary. Copied prompts include a bounded recent excerpt; the ZIP
retains the full conversation.

The canonical JSON format is `{format:"agent-library.conversation",version:1,
sessions:[...],media:{key:"relative/path"}}`. Each session carries its own
provider label, enabling future source adapters without changing the reader.
Re-import rewrites provenance to the preserved `conversation.json`, keeps the
original provenance in metadata, and retains reviewed notes and images.

Handoffs are attachments and instructions for a receiving LLM. The application
does not write to another application's private database or inherit permissions
from historical records.

## Backup and restore

Backups contain the active generation, library metadata and edited notes. A
versioned manifest lists every payload file's size and SHA-256. Verification
checks paths, file types, ZIP CRC, checksums, database schema/integrity and source
and media completeness. Restore verifies first, then creates a new independent
library. Existing libraries and backup files are never overwritten.

Backups and context packs are private, unencrypted user content. Source release
artifacts use a separate allowlist and contain only code, licenses, documentation
and fictional fixtures.
