"""Loopback-only HTTP application. No accounts, telemetry, CDN or cloud service."""
from __future__ import annotations

import contextlib
import json
import mimetypes
import re
import secrets
import threading
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import __version__, query
from .core import LibraryStore, safe_relative
from .handoff import context_pack, get_handoff, save_notes
from .ingest import import_source

WEB = Path(__file__).parent / "web"
PROVIDERS = [{"id": "claude", "name": "Claude"}, {"id": "claude_code", "name": "Claude Code"}, {"id": "cowork", "name": "Cowork"}]


def local_path(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("A local source or destination path is required.")
    return Path(value.strip().strip('"').strip("'")).expanduser()


class Application:
    def __init__(self, store):
        self.store = store
        self.token = secrets.token_urlsafe(32)
        self.jobs = {}
        self.jobs_lock = threading.Lock()
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="agent-library")

    def job(self, work):
        ident = uuid.uuid4().hex
        job = {"id": ident, "status": "queued", "phase": "queued", "progress": 0}
        with self.jobs_lock:
            self.jobs[ident] = job

        def update(phase, progress):
            with self.jobs_lock:
                job.update(phase=phase, progress=progress)

        def run():
            with self.jobs_lock:
                job.update(status="running", phase="running")
            try:
                result = work(update)
                if isinstance(result, dict) and "download_path" in result:
                    result = {**result, "download_url": "/api/backup/download?job_id=" + ident}
                with self.jobs_lock:
                    job.update(status="complete", phase="complete", progress=100, result=result)
            except Exception as exc:
                # File content, search terms and credentials never enter logs.
                if isinstance(exc, (ValueError, FileNotFoundError, FileExistsError)):
                    message = str(exc)
                else:
                    message = "Operation failed (" + type(exc).__name__ + "). Check the source format and available disk space."
                with self.jobs_lock:
                    job.update(status="failed", phase="failed", error=message, code="operation_failed")
        self.executor.submit(run)
        return {"job_id": ident}


def make_server(store: LibraryStore, port=0):
    app = Application(store)

    class Handler(BaseHTTPRequestHandler):
        server_version = "AgentLibrary/" + __version__
        sys_version = ""

        def log_message(self, format, *args):
            pass

        def headers_common(self):
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self'; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")

        def respond(self, content, mime="application/json; charset=utf-8", status=200, filename=None):
            if not isinstance(content, bytes):
                content = json.dumps(content, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.headers_common()
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(content)))
            if filename:
                self.send_header("Content-Disposition", 'attachment; filename="' + filename + '"')
            self.end_headers()
            self.wfile.write(content)

        def file(self, path, mime=None, filename=None):
            self.send_response(200)
            self.headers_common()
            self.send_header("Content-Type", mime or mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(path.stat().st_size))
            if filename:
                self.send_header("Content-Disposition", 'attachment; filename="' + filename + '"')
            self.end_headers()
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    self.wfile.write(chunk)

        def guard(self, mutation=False):
            port = self.server.server_address[1]
            allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
            host = self.headers.get("Host", "")
            if host not in allowed:
                raise PermissionError("Invalid local host.")
            origin = self.headers.get("Origin")
            if origin and origin not in {"http://" + h for h in allowed}:
                raise PermissionError("Cross-origin requests are blocked.")
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                raise PermissionError("Cross-site requests are blocked.")
            if mutation and not secrets.compare_digest(self.headers.get("X-Agent-Library-Token", ""), app.token):
                raise PermissionError("Reload this page before changing the library.")

        def dispatch(self, mutation=False):
            try:
                self.guard(mutation)
                url = urlsplit(self.path)
                args = {k: v[-1] for k, v in parse_qs(url.query, keep_blank_values=True).items()}
                if mutation:
                    if self.headers.get_content_type() != "application/json":
                        raise ValueError("Send JSON when changing the library.")
                    size = int(self.headers.get("Content-Length", "0"))
                    if size < 0 or size > 1024**2:
                        raise ValueError("Request is too large.")
                    data = json.loads(self.rfile.read(size))
                    if not isinstance(data, dict):
                        raise ValueError("Expected a JSON object.")
                    return self.mutate(url.path, args, data)
                return self.read(url.path, args)
            except (BrokenPipeError, ConnectionResetError):
                return
            except PermissionError as exc:
                self.respond({"error": str(exc), "code": "request_blocked"}, status=403)
            except (ValueError, KeyError, TypeError) as exc:
                self.respond({"error": str(exc), "code": "invalid_request"}, status=400)
            except FileNotFoundError:
                self.respond({"error": "File not found in this library.", "code": "not_found"}, status=404)
            except Exception as exc:
                self.respond({"error": "Unable to complete request (" + type(exc).__name__ + ").", "code": "internal_error"}, status=500)

        def do_GET(self):
            self.dispatch()

        def do_POST(self):
            self.dispatch(True)

        def do_PUT(self):
            self.dispatch(True)

        def read(self, path, args):
            if path == "/api/state":
                return self.respond({"version": __version__, "csrf_token": app.token, "libraries": store.list(), "providers": PROVIDERS, "languages": ["zh-CN", "en"]})
            if path.startswith("/api/jobs/"):
                with app.jobs_lock:
                    job = dict(app.jobs.get(path.rsplit("/", 1)[-1], {}))
                if not job:
                    raise ValueError("Unknown job.")
                if "result" in job and isinstance(job["result"], dict):
                    job["result"] = {k: v for k, v in job["result"].items() if k != "download_path"}
                return self.respond(job)
            if path == "/api/sessions":
                return self.respond(query.sessions(store, args["library_id"], q=args.get("q", ""), provider=args.get("provider", ""), project=args.get("project", ""), kind=args.get("kind", "main"), limit=args.get("limit", 50), offset=args.get("offset", 0)))
            if path == "/api/messages":
                return self.respond(query.session_messages(store, args["library_id"], args["session_id"], before=args.get("before"), around=args.get("around"), limit=args.get("limit", 40), details=args.get("details") == "1"))
            if path == "/api/preview":
                return self.respond(query.preview(store, args["library_id"], args["session_id"], limit=args.get("limit", 30)))
            if path == "/api/handoff":
                return self.respond(get_handoff(store, args["library_id"], args["session_id"], args.get("target", "generic"), args.get("lang", "en")))
            if path == "/api/handoff/download":
                result = context_pack(store, args["library_id"], args["session_id"], args.get("target", "generic"), args.get("lang", "en"))
                return self.respond(result, "application/zip", filename="agent-library-context.zip")
            if path == "/api/artifact":
                kind = args.get("kind")
                if kind not in {"handoff", "requirements", "transcript"}:
                    raise ValueError("Unknown preserved conversation document.")
                with contextlib.closing(store.connect(args["library_id"])) as db:
                    row = db.execute("SELECT metadata_json FROM sessions WHERE id=?", (args["session_id"],)).fetchone()
                if row is None:
                    raise ValueError("Conversation was not found in this library.")
                relative = json.loads(row["metadata_json"]).get("legacy_artifacts", {}).get(kind)
                if not isinstance(relative, str) or not relative.startswith("sources/"):
                    raise ValueError("Preserved document is unavailable.")
                return self.file(safe_relative(store.generation(args["library_id"]), relative), "text/markdown; charset=utf-8", "original-" + kind + ".md")
            if path.startswith("/api/media/"):
                mid = path.rsplit("/", 1)[-1]
                if not re.fullmatch(r"[a-f0-9]{64}", mid):
                    raise ValueError("Unknown media.")
                with contextlib.closing(store.connect(args["library_id"])) as db:
                    row = db.execute("SELECT relative_path,mime FROM media WHERE id=?", (mid,)).fetchone()
                if row is None or row["mime"] not in {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp", "image/avif"}:
                    raise ValueError("Media is unavailable.")
                return self.file(safe_relative(store.generation(args["library_id"]), row["relative_path"]), row["mime"])
            if path == "/api/backup/download":
                with app.jobs_lock:
                    job = app.jobs.get(args.get("job_id"), {})
                    result = job.get("result", {})
                if job.get("status") != "complete" or not result.get("download_path"):
                    raise ValueError("Backup download is not available.")
                return self.file(Path(result["download_path"]), "application/zip", "agent-library-backup.zip")
            if path.startswith("/api/"):
                return self.respond({"error": "Unknown endpoint.", "code": "not_found"}, status=404)
            relative = "index.html" if path in {"/", "/index.html"} else path.lstrip("/")
            file = safe_relative(WEB, relative)
            if not file.is_file():
                raise FileNotFoundError()
            return self.file(file)

        def mutate(self, path, args, data):
            if path == "/api/libraries" and self.command == "POST":
                return self.respond(store.create(data.get("name", "")), status=201)
            if path == "/api/import" and self.command == "POST":
                library_id = data["library_id"]
                store.get(library_id)
                source = local_path(data["path"])
                return self.respond(app.job(lambda update: import_source(store, library_id, source, data.get("provider", "auto"), data.get("dataset", "default"), update)), status=202)
            if path == "/api/handoff" and self.command == "PUT":
                library_id = args.get("library_id") or data.get("library_id")
                session_id = args.get("session_id") or data.get("session_id")
                notes = save_notes(store, library_id, session_id, data.get("notes", {}))
                return self.respond({"notes": notes, "saved": True})
            if path == "/api/backup" and self.command == "POST":
                from .backup import backup_library
                library_id = data["library_id"]
                store.get(library_id)
                destination = local_path(data["path"]) if data.get("path") else store.home / "backups" / (library_id + "-" + uuid.uuid4().hex[:8] + ".zip")

                def work(update):
                    update("backup", 10)
                    result = backup_library(store, library_id, destination)
                    return {**result, "path": str(destination), "download_path": str(destination)}
                return self.respond(app.job(work), status=202)
            if path == "/api/restore" and self.command == "POST":
                from .backup import restore_library
                source = local_path(data["path"])
                return self.respond(app.job(lambda update: restore_library(store, source, data.get("name"))), status=202)
            if path == "/api/demo" and self.command == "POST":
                from .importers import create_demo_source
                import tempfile

                def work(update):
                    library = store.create("Demo · 演示图书馆")
                    try:
                        with tempfile.TemporaryDirectory(prefix="agent-library-demo-") as temp:
                            source = create_demo_source(Path(temp))
                            report = import_source(store, library["id"], source, progress=update)
                        return {**library, **report, "library_id": library["id"]}
                    except BaseException:
                        import shutil
                        shutil.rmtree(store.root(library["id"]), ignore_errors=True)
                        raise
                return self.respond(app.job(work), status=202)
            return self.respond({"error": "Unknown action.", "code": "not_found"}, status=404)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.application = app
    return server


def serve(store, port=0, open_browser=False):
    server = make_server(store, port)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print("Agent Library " + __version__ + " · " + url, flush=True)
    print("Local only. Press Ctrl+C to stop.", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        server.application.executor.shutdown(wait=True)
