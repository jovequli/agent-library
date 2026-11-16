"""Real loopback HTTP regression checks using fictional records only."""
from __future__ import annotations

from contextlib import closing
import hashlib
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from agent_library.core import LibraryStore
from agent_library.server import make_server


class ServerBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temporary.name)
        cls.store = LibraryStore(cls.base / "home")
        cls.first = cls.store.create("Fictional Alpha")
        cls.second = cls.store.create("Fictional Beta")
        cls.sid = hashlib.sha256(b"fictional-session").hexdigest()
        cls.mid = hashlib.sha256(b"fictional-image").hexdigest()
        generation = cls.store.generation(cls.first["id"])
        (generation / "media" / "fictional.png").write_bytes(b"\x89PNG\r\n\x1a\nfictional")
        with closing(sqlite3.connect(generation / "archive.sqlite3")) as db:
            db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?)", (cls.sid, "fictional-native", "fixture", "claude", "Fictional conversation", "", "", "", "main", None, "{}"))
            db.execute("INSERT INTO media VALUES (?,?,?,?,?)", (cls.mid, "media/fictional.png", "image/png", "fictional.png", 17))
            db.commit()
        cls.server = make_server(cls.store)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.token = cls.server.application.token
        (cls.base / "outside-secret.txt").write_text("Fictional outside sentinel")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server.application.executor.shutdown(wait=True)
        cls.thread.join(timeout=5)
        cls.temporary.cleanup()

    def request(self, path, method="GET", data=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        request_headers = {"Host": f"127.0.0.1:{self.port}"}
        if data is not None:
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        body = json.dumps(data).encode() if isinstance(data, dict) else data
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_local_state_and_security_headers(self):
        status, headers, body = self.request("/api/state")
        self.assertEqual(status, 200)
        state = json.loads(body)
        self.assertEqual(state["csrf_token"], self.token)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_wrong_host_origin_or_cross_site_is_rejected(self):
        cases = [
            {"Host": "attacker.invalid"},
            {"Origin": "https://attacker.invalid"},
            {"Origin": "null"},
            {"Sec-Fetch-Site": "cross-site"},
        ]
        for headers in cases:
            with self.subTest(headers=headers):
                status, _, body = self.request("/api/state", headers=headers)
                self.assertEqual(status, 403)
                self.assertNotIn(self.token.encode(), body)

    def test_mutation_requires_token_and_valid_origin(self):
        before = len(self.store.list())
        for headers in ({}, {"X-Agent-Library-Token": "wrong"}, {"X-Agent-Library-Token": self.token, "Origin": "https://attacker.invalid"}):
            status, _, _ = self.request("/api/libraries", "POST", {"name": "Must not create"}, headers)
            self.assertEqual(status, 403)
        self.assertEqual(len(self.store.list()), before)
        status, _, body = self.request("/api/libraries", "POST", {"name": "Fictional API collection"}, {"X-Agent-Library-Token": self.token})
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(body)["name"], "Fictional API collection")

    def test_malformed_json_and_large_body_rejected(self):
        headers = {"X-Agent-Library-Token": self.token}
        self.assertEqual(self.request("/api/libraries", "POST", b"not-json", headers)[0], 400)
        headers["Content-Length"] = str(1024**2 + 1)
        self.assertEqual(self.request("/api/libraries", "POST", b"", headers)[0], 400)

    def test_wrong_content_type_rejected(self):
        headers = {"X-Agent-Library-Token": self.token, "Content-Type": "text/plain"}
        self.assertEqual(self.request("/api/libraries", "POST", b"{}", headers)[0], 400)

    def test_static_paths_cannot_read_outside_web(self):
        for path in ("/../outside-secret.txt", "/../../core.py", "/%2e%2e/outside-secret.txt", "/%2fetc/passwd"):
            with self.subTest(path=path):
                status, _, body = self.request(path)
                self.assertIn(status, (400, 404))
                self.assertNotIn(b"Fictional outside sentinel", body)
        self.assertEqual(self.request("/")[0], 200)

    def test_session_and_media_are_library_scoped(self):
        first, second = self.first["id"], self.second["id"]
        self.assertEqual(self.request(f"/api/messages?library_id={first}&session_id={self.sid}")[0], 200)
        status, _, body = self.request(f"/api/messages?library_id={second}&session_id={self.sid}")
        self.assertEqual(status, 400)
        self.assertNotIn(b"Fictional conversation", body)
        self.assertEqual(self.request(f"/api/media/{self.mid}?library_id={first}")[0], 200)
        self.assertEqual(self.request(f"/api/media/{self.mid}?library_id={second}")[0], 400)

    def test_unknown_library_never_falls_back(self):
        status, _, body = self.request("/api/sessions?library_id=" + "0" * 32)
        self.assertEqual(status, 400)
        self.assertNotIn(b"Fictional conversation", body)
        self.assertEqual(self.request("/api/sessions?library_id=../escape")[0], 400)

    def test_backup_download_needs_completed_owned_job(self):
        status, _, body = self.request("/api/backup/download?job_id=../../outside-secret.txt")
        self.assertEqual(status, 400)
        self.assertNotIn(b"Fictional outside sentinel", body)


if __name__ == "__main__":
    unittest.main()
