"""First-release acceptance: clean install commands and real local HTTP jobs."""
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.request import Request, urlopen

from agent_library.core import LibraryStore
from agent_library.importers import create_demo_source
from agent_library.server import make_server


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def cli(self, *args):
        completed = subprocess.run([sys.executable, "-m", "agent_library", "--home", str(self.root / "home"), *args], capture_output=True, text=True, check=True)
        return json.loads(completed.stdout)

    def test_cli_import_search_handoff_backup_and_relocated_restore(self):
        source = create_demo_source(self.root / "source")
        library = self.cli("create", "Synthetic acceptance shelf")
        report = self.cli("import", library["id"], str(source))
        self.assertEqual(report["provider"], "mixed")
        self.assertTrue(self.cli("import", library["id"], str(source))["unchanged"])
        found = self.cli("search", library["id"], "fictional parser")
        self.assertEqual(found["total"], 1)
        session_id = found["items"][0]["id"]
        latest = self.cli("show", library["id"], session_id, "--limit", "10")
        self.assertTrue(latest["has_more"])
        self.assertFalse(latest["has_after"])
        first = self.cli("show", library["id"], session_id, "--before", str(latest["next_before"]), "--limit", "100")
        self.assertLess(first["items"][-1]["ordinal"], latest["items"][0]["ordinal"])
        pack = self.root / "task.zip"
        self.cli("handoff", library["id"], session_id, "--target", "codex", "--output", str(pack))
        destination = self.cli("create", "Context import")
        self.cli("import", destination["id"], str(pack), "--provider", "agent_library")
        self.assertEqual(self.cli("search", destination["id"], "fictional parser")["total"], 1)
        backup = self.root / "backup.zip"
        self.assertTrue(self.cli("backup", library["id"], str(backup))["verified"])
        self.assertTrue(self.cli("verify-backup", str(backup))["verified"])
        restored = self.cli("restore", str(backup), "--name", "Restored acceptance shelf")
        self.assertNotEqual(restored["id"], library["id"])
        self.assertTrue(self.cli("doctor")["ok"])

    def test_real_http_import_backup_restore_jobs(self):
        store = LibraryStore(self.root / "home")
        source = create_demo_source(self.root / "source")
        server = make_server(store)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.application.executor.shutdown, True)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        origin = f"http://127.0.0.1:{server.server_address[1]}"
        with urlopen(origin + "/api/state") as response:
            token = json.load(response)["csrf_token"]

        def post(path, data):
            request = Request(origin + path, json.dumps(data).encode(), {"Content-Type": "application/json", "X-Agent-Library-Token": token})
            with urlopen(request) as response:
                return json.load(response)

        def complete(job):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                with urlopen(origin + "/api/jobs/" + job["job_id"]) as response:
                    state = json.load(response)
                if state["status"] == "complete":
                    return state["result"]
                self.assertNotEqual(state["status"], "failed", state)
                time.sleep(0.01)
            self.fail("Local job did not finish")

        library = post("/api/libraries", {"name": "Synthetic HTTP shelf"})
        self.assertEqual(complete(post("/api/import", {"library_id": library["id"], "path": str(source)}))["provider"], "mixed")
        result = complete(post("/api/backup", {"library_id": library["id"]}))
        self.assertIn("download_url", result)
        self.assertNotIn("download_path", result)
        with urlopen(origin + result["download_url"]) as response:
            self.assertEqual(response.headers.get_content_type(), "application/zip")
            self.assertTrue(response.read(4).startswith(b"PK"))
        restored = complete(post("/api/restore", {"path": result["path"], "name": "Restored HTTP shelf"}))
        self.assertNotEqual(restored["id"], library["id"])
        self.assertTrue(store.doctor()["ok"])


if __name__ == "__main__":
    unittest.main()
