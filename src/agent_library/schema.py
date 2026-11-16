"""Versioned on-disk format. Never silently open an incompatible archive."""
from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path

VERSION = 1
REQUIRED = {
    "sessions": {"id", "native_id", "dataset", "provider", "title", "project", "created_at", "updated_at", "kind", "parent_id", "metadata_json"},
    "messages": {"id", "session_id", "ordinal", "native_id", "role", "kind", "text", "created_at", "source_path", "source_line", "parent_id", "media_json", "metadata_json"},
    "media": {"id", "relative_path", "mime", "name", "size"},
}


def create_database(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.executescript("""
        PRAGMA foreign_keys=ON;
        CREATE TABLE sessions (
          id TEXT PRIMARY KEY, native_id TEXT NOT NULL, dataset TEXT NOT NULL,
          provider TEXT NOT NULL, title TEXT NOT NULL, project TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '',
          kind TEXT NOT NULL DEFAULT 'main', parent_id TEXT, metadata_json TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE messages (
          id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
          ordinal INTEGER NOT NULL, native_id TEXT NOT NULL, role TEXT NOT NULL, kind TEXT NOT NULL,
          text TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT '', source_path TEXT NOT NULL DEFAULT '',
          source_line INTEGER NOT NULL DEFAULT 0, parent_id TEXT, media_json TEXT NOT NULL DEFAULT '[]',
          metadata_json TEXT NOT NULL DEFAULT '{}', UNIQUE(session_id, ordinal));
        CREATE INDEX messages_session_order ON messages(session_id,ordinal);
        CREATE INDEX sessions_provider ON sessions(provider);
        CREATE TABLE media (id TEXT PRIMARY KEY,relative_path TEXT NOT NULL,mime TEXT NOT NULL,name TEXT NOT NULL,size INTEGER NOT NULL);
        CREATE TABLE archive_info (key TEXT PRIMARY KEY,value TEXT NOT NULL);
    """)
    try:
        db.execute("CREATE VIRTUAL TABLE message_search USING fts5(text,content='messages',content_rowid='rowid',tokenize='trigram')")
        tokenizer = "trigram"
    except sqlite3.OperationalError:
        tokenizer = "like"
    db.execute("INSERT INTO archive_info VALUES ('search',?)", (tokenizer,))
    db.execute(f"PRAGMA user_version={VERSION}")
    db.commit()
    return db


def readonly(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def validate_database(path: Path) -> None:
    with contextlib.closing(readonly(path)) as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version != VERSION:
            raise ValueError(f"Unsupported archive schema {version}; this version supports {VERSION}.")
        for table, columns in REQUIRED.items():
            present = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
            if not columns <= present:
                raise ValueError(f"Archive table is missing required fields: {table}")
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Archive database failed integrity validation.")
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("Archive has broken message references.")


def rebuild_search(db: sqlite3.Connection) -> None:
    found = db.execute("SELECT name FROM sqlite_master WHERE name='message_search'").fetchone()
    if found:
        db.execute("INSERT INTO message_search(message_search) VALUES ('rebuild')")
