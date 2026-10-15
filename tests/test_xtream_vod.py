import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from subprocess import CompletedProcess

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from server.services import xtream_vod as vod
from server.app import create_app
from xtream_ingest import XtreamClient, XtreamConfig


class FakeProvider:
    def fetch(self, action, **params):
        values = {
            "get_vod_categories": [{"category_id": "10", "category_name": "Films"}],
            "get_series_categories": [{"category_id": "20", "category_name": "Shows"}],
            "get_vod_streams": [{"category_id": "10", "stream_id": "123", "name": "A Film", "container_extension": "mp4"}],
            "get_series": [{"category_id": "20", "series_id": "456", "name": "A Show"}],
            "get_series_info": {"episodes": {"1": [{"id": "789", "episode_num": 2,
                                                    "title": "Second Episode", "container_extension": "mkv"}]}},
        }
        return values[action]


class MediaHandler(BaseHTTPRequestHandler):
    body = b"\x00\x00\x00\x18ftyp" + b"x" * 8192

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):
        pass


class VodTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "test.db"
        sqlite3.connect(self.db).close()
        self.env = patch.dict(os.environ, {"XTREAM_DOWNLOAD_DIR": str(Path(self.tmp.name) / "downloads")})
        self.env.start()
        self.addCleanup(self.env.stop)

    def conn(self):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        return conn

    def populate(self):
        with patch.object(vod, "load_accounts", return_value=[]):
            vod.refresh_catalog(FakeProvider(), self.db)
            vod.refresh_episodes("456", FakeProvider(), self.db)

    def test_catalog_episode_and_queue_are_saved_without_credentials(self):
        self.populate()
        with self.conn() as conn:
            page = vod.catalog(conn, query="film")
            self.assertEqual(page["total"], 1)
            self.assertEqual(page["items"][0]["item_id"], "123")
            self.assertEqual(vod.catalog(conn, category_id="movie:10")["total"], 1)
            self.assertEqual(vod.catalog(conn, query="%")["total"], 0)
            self.assertEqual(vod.episodes(conn, "456")[0]["episode_id"], "789")
            movie_job = vod.queue_download(conn, "movie", "123")
            self.assertEqual(movie_job, vod.queue_download(conn, "movie", "123"))
            episode_job = vod.queue_download(conn, "episode", "789")
            self.assertNotEqual(movie_job, episode_job)
            self.assertTrue(vod.cancel_job(conn, episode_job))
            self.assertEqual(vod.list_jobs(conn)[0]["state"], "cancelled")
        self.assertNotIn("password", self.db.read_bytes().decode("utf-8", errors="ignore"))

    def test_catalog_refresh_keeps_snapshot_on_provider_failure(self):
        self.populate()
        class BrokenProvider(FakeProvider):
            def fetch(self, action, **params):
                raise RuntimeError("provider offline")
        with self.assertRaises(RuntimeError):
            vod.refresh_catalog(BrokenProvider(), self.db)
        class PartialProvider(FakeProvider):
            def fetch(self, action, **params):
                return [] if action == "get_vod_streams" else super().fetch(action, **params)
        with self.assertRaisesRegex(Exception, "incomplete VOD catalog"):
            vod.refresh_catalog(PartialProvider(), self.db)
        with self.conn() as conn:
            self.assertEqual(vod.catalog(conn)["total"], 2)

    def test_download_writes_complete_file_and_releases_lease(self):
        self.populate()
        with self.conn() as conn:
            job_id = vod.queue_download(conn, "movie", "123")
            job = dict(conn.execute("SELECT * FROM xtream_vod_jobs WHERE id=?", (job_id,)).fetchone())
        server = ThreadingHTTPServer(("127.0.0.1", 0), MediaHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        config = SimpleNamespace(server_url=f"http://127.0.0.1:{server.server_port}", username="secretuser",
                                 password="secretpass", fallback_server_url=None)
        class Lease:
            account = SimpleNamespace(config=config)
            gate_fd = None
            fd = -1
            outcome = None
            def release(self, outcome):
                self.outcome = outcome
        lease = Lease()
        class Pool:
            def status(self): return {"accounts": [{"available": 1}]}
            def acquire(self, *args):
                return lease
        vod._run_job(self.db, job, Pool())
        with self.conn() as conn:
            finished = dict(conn.execute("SELECT * FROM xtream_vod_jobs WHERE id=?", (job_id,)).fetchone())
        self.assertEqual(finished["state"], "completed")
        self.assertEqual(Path(finished["filename"]).read_bytes(), MediaHandler.body)
        self.assertEqual(lease.outcome, "upstream_eof")
        self.assertFalse(list(vod.download_dir().glob("*.part")))

    def test_cached_api_reads_do_not_contact_provider(self):
        self.populate()
        with patch.dict(os.environ, {"FRUIT_DB_PATH": str(self.db)}), \
             patch.object(vod.VodProvider, "fetch", side_effect=AssertionError("provider traffic")):
            client = create_app().test_client()
            page = client.get("/movies-tv")
            catalog = client.get("/api/xtream/vod/catalog?q=film").get_json()
            episodes = client.get("/api/xtream/vod/series/456/episodes").get_json()
            jobs = client.get("/api/xtream/vod/downloads").get_json()
        self.assertEqual(page.status_code, 200)
        self.assertEqual(catalog["total"], 1)
        self.assertEqual(episodes["episodes"][0]["episode_id"], "789")
        self.assertEqual(jobs["jobs"], [])

    def test_cancelled_transfer_removes_partial_file(self):
        self.populate()
        with self.conn() as conn:
            job_id = vod.queue_download(conn, "movie", "123")
            job = dict(conn.execute("SELECT * FROM xtream_vod_jobs WHERE id=?", (job_id,)).fetchone())
        config = SimpleNamespace(server_url="http://provider.invalid", username="u", password="p", fallback_server_url=None)
        lease = SimpleNamespace(account=SimpleNamespace(config=config), gate_fd=None, fd=-1,
                                release=lambda outcome: None)
        class Pool:
            def status(self): return {"accounts": [{"available": 1}]}
            def acquire(self, *args):
                return lease
        class Response:
            status_code = 200
            headers = {}
            def close(self): pass
            def iter_content(self, chunk_size):
                yield b"\x00\x00\x00\x18ftyp" + b"x" * 100
                with self_test.conn() as conn:
                    vod.cancel_job(conn, job_id)
                yield b"x" * 100
        class Session:
            headers = {}
            proxies = {}
            trust_env = True
            def get(self, *args, **kwargs): return Response()
            def close(self): pass
        self_test = self
        with patch.object(vod.requests, "Session", Session):
            vod._run_job(self.db, job, Pool())
        with self.conn() as conn:
            state = conn.execute("SELECT state FROM xtream_vod_jobs WHERE id=?", (job_id,)).fetchone()[0]
        self.assertEqual(state, "cancelled")
        self.assertFalse(list(vod.download_dir().iterdir()))

    def test_series_metadata_keeps_credentials_out_of_process_arguments(self):
        calls = []
        def run(command, **kwargs):
            calls.append((command, kwargs["input"]))
            return CompletedProcess(command, 0, stdout='{"episodes":{"1":[]}}', stderr="")
        config = XtreamConfig(True, "http://provider.invalid", "user-secret", "pass-secret", ())
        client = XtreamClient(config, subprocess_runner=run)
        try:
            result = client._curl_payload("get_series_info", extra_params={"series_id": "456"})
        finally:
            client.session.close()
        self.assertIn("episodes", result)
        command, stdin = calls[0]
        self.assertIn("series_id=456", command)
        self.assertNotIn("user-secret", " ".join(command))
        self.assertNotIn("pass-secret", " ".join(command))
        self.assertIn("user-secret", stdin)

    def test_python_auth_rejection_uses_curl_media_fallback(self):
        self.populate()
        with self.conn() as conn:
            job_id = vod.queue_download(conn, "movie", "123")
            job = dict(conn.execute("SELECT * FROM xtream_vod_jobs WHERE id=?", (job_id,)).fetchone())
        config = SimpleNamespace(server_url="http://provider.invalid", username="u", password="p", fallback_server_url=None)
        lease = SimpleNamespace(account=SimpleNamespace(config=config), gate_fd=None, fd=-1,
                                release=lambda outcome: None)
        class Pool:
            def status(self): return {"accounts": [{"available": 1}]}
            def acquire(self, *args): return lease
        class Response:
            status_code = 403
            headers = {}
            def close(self): pass
        class Session:
            headers = {}
            proxies = {}
            trust_env = True
            def get(self, *args, **kwargs): return Response()
            def close(self): pass
        class Curl:
            def __init__(self, url, idle, lease_fd, gate_fd):
                self.url = url
            def chunks(self):
                yield MediaHandler.body
            def close(self): pass
        with patch.object(vod.requests, "Session", Session), patch.object(vod, "CurlStream", Curl):
            vod._run_job(self.db, job, Pool())
        with self.conn() as conn:
            finished = dict(conn.execute("SELECT * FROM xtream_vod_jobs WHERE id=?", (job_id,)).fetchone())
        self.assertEqual(finished["state"], "completed")
        self.assertEqual(Path(finished["filename"]).read_bytes(), MediaHandler.body)

    def test_worker_requeues_interrupted_job_on_restart(self):
        self.populate()
        with self.conn() as conn:
            job_id = vod.queue_download(conn, "movie", "123")
            conn.execute("UPDATE xtream_vod_jobs SET state='running',bytes_done=400 WHERE id=?", (job_id,))
            conn.commit()
        stopped = threading.Event()
        stopped.set()
        vod.run_worker(self.db, stop_event=stopped)
        with self.conn() as conn:
            row = conn.execute("SELECT state,bytes_done FROM xtream_vod_jobs WHERE id=?", (job_id,)).fetchone()
        self.assertEqual((row["state"], row["bytes_done"]), ("pending", 0))


if __name__ == "__main__":
    unittest.main()
