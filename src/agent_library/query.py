"""Shared, library-scoped queries for CLI and local HTTP reader."""
from __future__ import annotations

import contextlib
import json
from urllib.parse import urlencode

VISIBLE_KINDS = {"text", "image", "history", "attachment"}
VISIBLE = "role IN ('user','assistant') AND kind IN ('text','image','history','attachment')"


def media_item(db, library_id, mid, occurrence=None):
    row = db.execute("SELECT id,mime,name FROM media WHERE id=?", (mid,)).fetchone()
    if not row:
        return None
    return {**(occurrence or {}), **dict(row), "url": "/api/media/" + row["id"] + "?" + urlencode({"library_id": library_id})}


def decorate_session(db, library_id, row):
    session = dict(row)
    metadata = json.loads(session.pop("metadata_json"))
    session["metadata"] = metadata
    session["attachments"] = [item for occurrence in metadata.get("session_attachments", [])
                              if isinstance(occurrence, dict) and (item := media_item(db, library_id, occurrence.get("id"), occurrence))]
    session["missing_media"] = metadata.get("missing_media", [])
    session["task_paths"] = metadata.get("task_paths", [])
    session["execution_cwd"] = metadata.get("execution_cwd", "")
    session["legacy_artifacts"] = metadata.get("legacy_artifacts", {})
    session["artifact_links"] = {kind: "/api/artifact?" + urlencode({"library_id": library_id, "session_id": session["id"], "kind": kind})
                                 for kind in ("handoff", "requirements", "transcript") if kind in session["legacy_artifacts"]}
    return session


def _like(value):
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def sessions(store, library_id, *, q="", provider="", project="", kind="main", limit=50, offset=0):
    limit, offset = min(max(int(limit), 1), 100), max(int(offset), 0)
    q = str(q).strip()[:500]
    searchable = "1" if kind == "all" else VISIBLE
    clauses, values = [], []
    if provider:
        clauses.append("s.provider=?")
        values.append(provider)
    if project:
        clauses.append("s.project=?")
        values.append(project)
    if kind != "all":
        clauses.append("s.kind=?")
        values.append(kind or "main")
    with contextlib.closing(store.connect(library_id)) as db:
        projects = [row[0] for row in db.execute("SELECT DISTINCT project FROM sessions WHERE project!='' ORDER BY project")]
        providers = [{"id": row[0], "name": {"claude": "Claude", "claude_code": "Claude Code", "cowork": "Cowork"}.get(row[0], row[0])} for row in db.execute("SELECT DISTINCT provider FROM sessions ORDER BY provider")]
        if q:
            tokenizer = db.execute("SELECT value FROM archive_info WHERE key='search'").fetchone()
            if len(q) >= 3 and tokenizer and tokenizer[0] == "trigram":
                expression = '"' + q.replace('"', '""') + '"'
                clauses.append("(s.title LIKE ? ESCAPE '\\' OR s.project LIKE ? ESCAPE '\\' OR s.id IN (SELECT m.session_id FROM messages m WHERE m.rowid IN (SELECT rowid FROM message_search WHERE message_search MATCH ?) AND " + searchable + "))")
                values.extend([_like(q), _like(q), expression])
            else:
                clauses.append("(s.title LIKE ? ESCAPE '\\' OR s.project LIKE ? ESCAPE '\\' OR EXISTS(SELECT 1 FROM messages m WHERE m.session_id=s.id AND m.text LIKE ? ESCAPE '\\' AND " + searchable + "))")
                values.extend([_like(q)] * 3)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        total = db.execute("SELECT count(*) FROM sessions s" + where, values).fetchone()[0]
        rows = db.execute("SELECT s.*, (SELECT count(*) FROM messages m WHERE m.session_id=s.id AND " + VISIBLE + ") message_count FROM sessions s" + where + " ORDER BY s.updated_at DESC,s.id LIMIT ? OFFSET ?", [*values, limit, offset]).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json"))
            preview = db.execute("SELECT text,role,kind,ordinal FROM messages WHERE session_id=? AND " + searchable + (" AND text LIKE ? ESCAPE '\\'" if q else "") + " ORDER BY ordinal DESC LIMIT 1", [row["id"], *([_like(q)] if q else [])]).fetchone()
            item["preview"] = preview["text"][:250] if preview else ""
            item["match_ordinal"] = preview["ordinal"] if preview and q else None
            item["match_hidden"] = bool(preview and (preview["role"] not in {"user", "assistant"} or preview["kind"] not in VISIBLE_KINDS))
            items.append(item)
    return {"items": items, "total": total, "projects": projects, "providers": providers}


def session_messages(store, library_id, session_id, *, before=None, around=None, limit=40, details=False):
    limit = min(max(int(limit), 1), 100)
    with contextlib.closing(store.connect(library_id)) as db:
        session = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if session is None:
            raise ValueError("Conversation was not found in this library.")
        session = decorate_session(db, library_id, session)
        clauses, values = ["session_id=?"], [session_id]
        if not details:
            clauses.append(VISIBLE)
        if before is not None:
            clauses.append("ordinal<?")
            values.append(int(before))
        if around is not None:
            prior = db.execute("SELECT ordinal FROM messages WHERE session_id=?" + (" AND " + VISIBLE if not details else "") + " AND ordinal<=? ORDER BY ordinal DESC LIMIT ?", (session_id, int(around), limit // 2)).fetchall()
            start = min((r[0] for r in prior), default=int(around))
            clauses.append("ordinal>=?")
            values.append(start)
            order = "ASC"
        else:
            order = "DESC"
        rows = db.execute("SELECT * FROM messages WHERE " + " AND ".join(clauses) + " ORDER BY ordinal " + order + " LIMIT ?", [*values, limit]).fetchall()
        if order == "DESC":
            rows = list(reversed(rows))
        items = []
        for row in rows:
            message = dict(row)
            mids = json.loads(message.pop("media_json"))
            message["metadata"] = json.loads(message.pop("metadata_json"))
            message["media"] = []
            for mid in mids:
                media = media_item(db, library_id, mid)
                if media:
                    message["media"].append(media)
            items.append(message)
        earliest = items[0]["ordinal"] if items else None
        last = items[-1]["ordinal"] if items else None
        visible_clause = " AND " + VISIBLE if not details else ""
        more = earliest is not None and bool(db.execute("SELECT 1 FROM messages WHERE session_id=? AND ordinal<?" + visible_clause + " LIMIT 1", (session_id, earliest)).fetchone())
        after = last is not None and bool(db.execute("SELECT 1 FROM messages WHERE session_id=? AND ordinal>?" + visible_clause + " LIMIT 1", (session_id, last)).fetchone())
        return {"session": session, "items": items, "has_more": more, "has_after": after, "next_before": earliest}


def preview(store, library_id, session_id, limit=30):
    with contextlib.closing(store.connect(library_id)) as db:
        if not db.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone():
            raise ValueError("Conversation was not found in this library.")
        count = db.execute("SELECT count(*) FROM messages WHERE session_id=? AND " + VISIBLE, (session_id,)).fetchone()[0]
        limit = min(max(int(limit), 1), 100)
        # A bounded evenly spaced outline gives old and new messages a place.
        if count <= limit:
            rows = db.execute("SELECT id,ordinal,role,text,created_at FROM messages WHERE session_id=? AND " + VISIBLE + " ORDER BY ordinal", (session_id,)).fetchall()
        else:
            rows = []
            for i in range(limit):
                offset = round(i * (count - 1) / max(limit - 1, 1))
                rows.append(db.execute("SELECT id,ordinal,role,text,created_at FROM messages WHERE session_id=? AND " + VISIBLE + " ORDER BY ordinal LIMIT 1 OFFSET ?", (session_id, offset)).fetchone())
        return {"items": [{**dict(row), "text": row["text"][:350]} for row in rows], "total": count}
