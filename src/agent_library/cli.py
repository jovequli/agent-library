"""One portable command-line entry point for the same local library as the UI."""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from . import __version__, query
from .backup import backup_library, restore_library, verify_backup
from .core import LibraryStore
from .handoff import context_pack, get_handoff
from .ingest import import_source


def build_parser():
    parser = argparse.ArgumentParser(prog="agent-library", description="Read, search and continue agent conversations locally.")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--home", type=Path, default=Path(os.environ.get("AGENT_LIBRARY_HOME", "~/.agent-library")).expanduser(), help="Private data directory, outside the source repository")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Open the local reading application")
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument("--open", action="store_true", help="Open the local address in a browser")
    commands.add_parser("libraries", help="List independent libraries")
    create = commands.add_parser("create", help="Create an empty library")
    create.add_argument("name")
    ingest = commands.add_parser("import", help="Snapshot and import history without changing the source")
    ingest.add_argument("library_id")
    ingest.add_argument("source", type=Path)
    ingest.add_argument("--provider", choices=["auto", "claude", "claude_code", "cowork", "agent_library"], default="auto")
    ingest.add_argument("--dataset", default="default")
    legacy = commands.add_parser("migrate-legacy", help="Copy an existing normalized archive into a new independent library")
    legacy.add_argument("archive", type=Path)
    legacy.add_argument("--source-root", type=Path, required=True, help="Explicit root containing the preserved raw sources")
    legacy.add_argument("--name", default="Claude Database")
    search = commands.add_parser("search", help="Search visible messages, titles and projects")
    search.add_argument("library_id")
    search.add_argument("query")
    search.add_argument("--provider", default="")
    search.add_argument("--all", action="store_true", help="Include subagents, partial histories and context records")
    show = commands.add_parser("show", help="Read a bounded message window")
    show.add_argument("library_id")
    show.add_argument("session_id")
    show.add_argument("--before", type=int)
    show.add_argument("--limit", type=int, default=40)
    show.add_argument("--details", action="store_true")
    handoff = commands.add_parser("handoff", help="Create a portable context pack or print a handoff prompt")
    handoff.add_argument("library_id")
    handoff.add_argument("session_id")
    handoff.add_argument("--target", choices=["generic", "codex", "claude"], default="generic")
    handoff.add_argument("--lang", choices=["en", "zh-CN"], default="en")
    handoff.add_argument("--output", type=Path)
    backup = commands.add_parser("backup", help="Create and verify a new private backup ZIP")
    backup.add_argument("library_id")
    backup.add_argument("output", type=Path)
    verify = commands.add_parser("verify-backup", help="Verify checksums and database in a backup")
    verify.add_argument("archive", type=Path)
    restore = commands.add_parser("restore", help="Restore into a new independent library")
    restore.add_argument("archive", type=Path)
    restore.add_argument("--name")
    commands.add_parser("demo", help="Create a library with fictional mixed-agent records")
    doctor = commands.add_parser("doctor", help="Check environment, schema and preserved attachments")
    doctor.add_argument("library_id", nargs="?")
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # Accept --home at either end, so the desktop and documented CLI agree.
    for index, item in enumerate(argv):
        if item == "--home" and index > 0 and index + 1 < len(argv):
            argv = argv[index:index + 2] + argv[:index] + argv[index + 2:]
            break
        if item.startswith("--home=") and index > 0:
            argv = [item] + argv[:index] + argv[index + 1:]
            break
    args = build_parser().parse_args(argv)
    try:
        if args.command == "verify-backup":
            result = verify_backup(args.archive)
        else:
            store = LibraryStore(args.home)
            if args.command == "serve":
                from .server import serve
                serve(store, args.port, args.open)
                return 0
            if args.command == "create":
                result = store.create(args.name)
            elif args.command == "libraries":
                result = store.list()
            elif args.command == "import":
                result = import_source(store, args.library_id, args.source, args.provider, args.dataset)
            elif args.command == "migrate-legacy":
                from .legacy import migrate_legacy_archive
                result = migrate_legacy_archive(store, args.archive, args.source_root, name=args.name,
                    progress=lambda phase, percent: print(json.dumps({"phase": phase, "progress": percent}), file=sys.stderr, flush=True))
            elif args.command == "search":
                result = query.sessions(store, args.library_id, q=args.query, provider=args.provider, kind="all" if args.all else "main")
            elif args.command == "show":
                result = query.session_messages(store, args.library_id, args.session_id, before=args.before, limit=args.limit, details=args.details)
            elif args.command == "handoff":
                if args.output:
                    output = args.output.expanduser()
                    content = context_pack(store, args.library_id, args.session_id, args.target, args.lang)
                    output.parent.mkdir(parents=True, exist_ok=True)
                    with output.open("xb") as stream:
                        stream.write(content)
                    result = {"path": str(output.resolve()), "format": "agent-library.conversation", "version": 1}
                else:
                    print(get_handoff(store, args.library_id, args.session_id, args.target, args.lang)["prompt"])
                    return 0
            elif args.command == "backup":
                result = backup_library(store, args.library_id, args.output)
            elif args.command == "restore":
                result = restore_library(store, args.archive, args.name)
            elif args.command == "demo":
                from .importers import create_demo_source
                library = store.create("Demo · 演示图书馆")
                with tempfile.TemporaryDirectory(prefix="agent-library-demo-") as temporary:
                    report = import_source(store, library["id"], create_demo_source(Path(temporary)))
                result = {**library, **report}
            elif args.command == "doctor":
                result = store.doctor()
                if args.library_id:
                    store.get(args.library_id)
                    result["libraries"] = [x for x in result["libraries"] if x["id"] == args.library_id]
                    result["ok"] = all(x["ok"] for x in result["libraries"])
            else:
                raise ValueError("Unknown command.")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        ok = result.get("ok", True) if isinstance(result, dict) else True
        return 0 if ok else 1
    except (OSError, ValueError, KeyError) as exc:
        print("Agent Library: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
