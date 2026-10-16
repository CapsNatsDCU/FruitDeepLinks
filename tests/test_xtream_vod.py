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
            "get_vod_streams": [{"category_id": "10", "stream_id": "123", "name": "A Film", "container_extension": "mp4",
                                 "size_bytes": "2147483648", "resolution": "1920x1080"}],
            "get_vod_info": {"movie_data": {"stream_id": 123},
                             "info": {"audio": [{"language": "eng"}, {"tags": {"language": "spa"}}]}},
            "get_series": [{"category_id": "20", "series_id": "456", "name": "A Show"}],
            "get_series_info": {"episodes": {"1": [{"id": "789", "episode_num": 2,
                                                    "title": "Second Episode", "container_extension": "mkv",
                                                    "info": {"filesize": "734003200", "video": {"width": 1280, "height": 720},
                                                             "audio": [{"tags": {"language": "eng"}}]}}]}},
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
            self.assertEqual((page["items"][0]["size_bytes"], page["items"][0]["resolution"]),
                             (2147483648, "1920×1080"))
            self.assertEqual(vod.catalog(conn, category_id="movie:10")["total"], 1)
            self.assertEqual(vod.catalog(conn, query="%")["total"], 0)
            self.assertEqual(vod.episodes(conn, "456")[0]["episode_id"], "789")
            self.assertEqual((vod.episodes(conn, "456")[0]["size_bytes"], vod.episodes(conn, "456")[0]["resolution"]),
                             (734003200, "1280×720"))
            self.assertEqual(vod.episodes(conn, "456")[0]["audio_languages"], "English")
            movie_job = vod.queue_download(conn, "movie", "123")
            self.assertEqual(movie_job, vod.queue_download(conn, "movie", "123"))
            episode_job = vod.queue_download(conn, "episode", "789")
            self.assertNotEqual(movie_job, episode_job)
            self.assertEqual(conn.execute("SELECT folder FROM xtream_vod_jobs WHERE id=?", (movie_job,)).fetchone()[0], "movies")
            self.assertEqual(conn.execute("SELECT folder FROM xtream_vod_jobs WHERE id=?", (episode_job,)).fetchone()[0], "tv")
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

    def test_missing_or_untrusted_media_details_stay_unknown(self):
        self.assertIsNone(vod._size_bytes({"size": "1.5 GB", "duration_secs": 7200}))
        self.assertIsNone(vod._size_bytes({"size": True}))
        self.assertIsNone(vod._resolution({"resolution": "FHD"}))
        self.assertIsNone(vod._resolution({"width": 0, "height": 1080}))
        self.assertIsNone(vod._audio_languages({"audio": [{"codec_name": "aac"}], "country": "US"}))
        self.assertEqual(vod._audio_languages({"audio_languages": "en-US, es-MX, und"}), "English, Spanish")
        with self.conn() as conn:
            conn.executescript("""
                CREATE TABLE xtream_vod_items (
                    kind TEXT, item_id TEXT, category_id TEXT, title TEXT,
                    search_title TEXT, extension TEXT
                );
                CREATE TABLE xtream_vod_categories (
                    kind TEXT, category_id TEXT, name TEXT
                );
                CREATE TABLE xtream_vod_episodes (
                    series_id TEXT, episode_id TEXT, season INTEGER,
                    episode INTEGER, title TEXT, extension TEXT
                );
                CREATE TABLE xtream_vod_state (
                    id INTEGER, refreshed_at TEXT, movie_count INTEGER, series_count INTEGER
                );
                INSERT INTO xtream_vod_categories VALUES ('series','20','Shows');
                INSERT INTO xtream_vod_items VALUES ('series','456','20','A Show','a show',NULL);
                INSERT INTO xtream_vod_episodes VALUES ('456','789',1,2,'Second Episode','mkv');
            """)
            self.assertIsNone(vod.catalog(conn)["items"][0]["size_bytes"])
            self.assertIsNone(vod.episodes(conn, "456")[0]["resolution"])
            self.assertIsNone(vod.catalog(conn)["items"][0]["audio_languages"])
            vod.ensure_schema(conn)
            self.assertIn("size_bytes", {row[1] for row in conn.execute("PRAGMA table_info(xtream_vod_items)")})
            self.assertIn("resolution", {row[1] for row in conn.execute("PRAGMA table_info(xtream_vod_episodes)")})
            self.assertIn("audio_languages", {row[1] for row in conn.execute("PRAGMA table_info(xtream_vod_items)")})

    def test_cached_titles_expose_language_tags_without_provider_requests(self):
        examples = {
            "EN - Blade Runner 2049": "English",
            "DE - Queen's Blade (2009) (JP) (Ger Sub)": "German",
            "Movie [ENG + SPA] [1080p]": "English, Spanish",
            "Film | FR": "French",
            "Movie [English subtitles]": None,
            "Movie (Ger Sub)": None,
            "NF - Blade: Trinity": None,
            "The English Patient": None,
            "It": None,
        }
        for title, expected in examples.items():
            with self.subTest(title=title):
                self.assertEqual(vod._title_listing_languages(title), expected)
        self.assertEqual(vod._title_language_hints("EN - Blade Runner 2049"), ("English", None))
        self.assertEqual(vod._title_language_hints("DE - Queen's Blade (2009) (JP) (Ger Sub)"),
                         (None, "German"))
        self.assertEqual(vod._title_language_hints("DE - Queen's Blade (2009) (JA) (Ger Sub)"),
                         ("Japanese", "German"))
        self.assertEqual(vod._title_language_hints("DE - Anime (Ger Sub)"), (None, "German"))
        self.assertEqual(vod._title_language_hints("NF - Blade: Trinity"), (None, None))
        self.populate()
        with self.conn() as conn:
            conn.execute("UPDATE xtream_vod_items SET title=?,search_title=? WHERE kind='movie' AND item_id='123'",
                         ("EN - Blade Runner 2049", "en - blade runner 2049"))
            conn.execute("UPDATE xtream_vod_items SET title=?,search_title=? WHERE kind='series' AND item_id='456'",
                         ("DE - Queen's Blade (2009) (JP) (Ger Sub)", "de - queen's blade (2009) (jp) (ger sub)"))
            conn.commit()
        with patch.dict(os.environ, {"FRUIT_DB_PATH": str(self.db)}), \
             patch.object(vod.VodProvider, "fetch", side_effect=AssertionError("provider traffic")):
            client = create_app().test_client()
            items = client.get("/api/xtream/vod/catalog?q=blade").get_json()["items"]
            episode = client.get("/api/xtream/vod/series/456/episodes").get_json()["episodes"][0]
        by_kind = {item["kind"]: item for item in items}
        self.assertEqual(by_kind["movie"]["title_audio_languages"], "English")
        self.assertIsNone(by_kind["series"]["title_audio_languages"])
        self.assertEqual(by_kind["series"]["title_subtitle_languages"], "German")
        self.assertIsNone(episode["title_audio_languages"])
        self.assertEqual(episode["audio_languages"], "English")

    def test_movie_details_read_is_explicit_and_kept_on_catalog_refresh(self):
        self.populate()
        with self.conn() as conn:
            self.assertIsNone(vod.catalog(conn, query="film")["items"][0]["audio_languages"])
        with patch.dict(os.environ, {"FRUIT_DB_PATH": str(self.db)}), \
             patch.object(vod.VodProvider, "fetch", side_effect=FakeProvider().fetch) as fetch:
            client = create_app().test_client()
            details = client.post("/api/xtream/vod/movie/123/details/refresh")
            self.assertEqual(details.status_code, 200)
            self.assertEqual(details.get_json()["audio_languages"], "English, Spanish")
            self.assertEqual(client.get("/api/xtream/vod/catalog?q=film").get_json()["items"][0]["audio_languages"],
                             "English, Spanish")
            fetch.assert_called_once_with("get_vod_info", vod_id="123")
        with patch.object(vod, "load_accounts", return_value=[]):
            vod.refresh_catalog(FakeProvider(), self.db)
        with self.conn() as conn:
            self.assertEqual(vod.catalog(conn, query="film")["items"][0]["audio_languages"], "English, Spanish")
        class WrongMovie(FakeProvider):
            def fetch(self, action, **params):
                if action == "get_vod_info":
                    return {"movie_data": {"stream_id": 999}, "info": {"audio_language": "French"}}
                return super().fetch(action, **params)
        with self.assertRaisesRegex(Exception, "different movie"):
            vod.refresh_movie_details("123", WrongMovie(), self.db)
        with self.conn() as conn:
            self.assertEqual(vod.catalog(conn, query="film")["items"][0]["audio_languages"], "English, Spanish")

    def test_download_writes_complete_file_and_releases_lease(self):
        self.populate()
        with self.conn() as conn:
            job_id = vod.queue_download(conn, "movie", "123")
            job = dict(conn.execute("SELECT * FROM xtream_vod_jobs WHERE id=?", (job_id,)).fetchone())
            episode_id = vod.queue_download(conn, "episode", "789")
            episode_job = dict(conn.execute("SELECT * FROM xtream_vod_jobs WHERE id=?", (episode_id,)).fetchone())
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
        original_umask = os.umask(0o022)
        try:
            vod._run_job(self.db, job, Pool())
            vod._run_job(self.db, episode_job, Pool())
        finally:
            os.umask(original_umask)
        with self.conn() as conn:
            finished = dict(conn.execute("SELECT * FROM xtream_vod_jobs WHERE id=?", (job_id,)).fetchone())
            episode_finished = dict(conn.execute("SELECT * FROM xtream_vod_jobs WHERE id=?", (episode_id,)).fetchone())
        self.assertEqual(finished["state"], "completed")
        self.assertEqual(Path(finished["filename"]).read_bytes(), MediaHandler.body)
        self.assertEqual(Path(finished["filename"]).parent, vod.folder_dir("movies"))
        self.assertEqual(Path(finished["filename"]).name, "A Film.mp4")
        self.assertEqual(Path(finished["filename"]).stat().st_mode & 0o777, 0o644)
        self.assertEqual(episode_finished["state"], "completed")
        self.assertEqual(Path(episode_finished["filename"]).parent, vod.folder_dir("tv") / "A Show" / "Season 01")
        self.assertIn("A Show - s01e02 - Second Episode", Path(episode_finished["filename"]).name)
        self.assertEqual(Path(episode_finished["filename"]).read_bytes(), MediaHandler.body)
        self.assertEqual(Path(episode_finished["filename"]).stat().st_mode & 0o777, 0o644)
        self.assertEqual(lease.outcome, "upstream_eof")
        self.assertFalse(list(vod.folder_dir("movies").glob("*.part")))

    def test_movie_filename_uses_plex_title_and_release_year(self):
        job = {"kind": "movie", "title": "EN - Blade Runner (1982) [1080p]", "extension": "mp4",
               "item_id": "117814", "id": 13}
        self.assertEqual(vod._filename(job), "Blade Runner (1982).mp4")
        self.assertEqual(vod._filename(dict(job, title="NF - Blade: Trinity")), "Blade Trinity.mp4")
        self.assertEqual(vod._filename(dict(job, title="DE - Queen's Blade (2009) (JP) (Ger Sub)")),
                         "Queen's Blade (2009).mp4")
        self.assertEqual(vod._filename(dict(job, title="É" * 150)), "É" * 118 + ".mp4")
        long_name = vod._filename(dict(job, title="É" * 150 + " (1982)"))
        self.assertTrue(long_name.endswith(" (1982).mp4"))
        self.assertLessEqual(len(long_name.encode("utf-8")), 240)

    def test_movie_download_does_not_replace_existing_plex_named_file(self):
        self.populate()
        with self.conn() as conn:
            conn.execute("UPDATE xtream_vod_items SET title='EN - Blade Runner (1982)' WHERE kind='movie' AND item_id='123'")
            job_id = vod.queue_download(conn, "movie", "123")
            job = dict(conn.execute("SELECT * FROM xtream_vod_jobs WHERE id=?", (job_id,)).fetchone())
        destination = vod.folder_dir("movies")
        destination.mkdir(parents=True)
        existing = destination / "Blade Runner (1982).mp4"
        existing.write_bytes(b"existing movie")
        class NoProviderPool:
            def status(self):
                raise AssertionError("An existing file should stop the download before provider traffic")
        vod._run_job(self.db, job, NoProviderPool())
        with self.conn() as conn:
            result = conn.execute("SELECT state,error FROM xtream_vod_jobs WHERE id=?", (job_id,)).fetchone()
        self.assertEqual(existing.read_bytes(), b"existing movie")
        self.assertEqual(result["state"], "failed")
        self.assertIn("already exists", result["error"])

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
        self.assertEqual(catalog["items"][0]["size_bytes"], 2147483648)
        self.assertEqual(catalog["items"][0]["resolution"], "1920×1080")
        self.assertEqual(episodes["episodes"][0]["episode_id"], "789")
        self.assertEqual(episodes["episodes"][0]["size_bytes"], 734003200)
        self.assertEqual(episodes["episodes"][0]["resolution"], "1280×720")
        self.assertEqual(episodes["episodes"][0]["audio_languages"], "English")
        self.assertEqual(jobs["jobs"], [])
        self.assertEqual(jobs["destinations"]["movies"], str(vod.folder_dir("movies")))
        self.assertEqual(jobs["destinations"]["tv"], str(vod.folder_dir("tv")))

    def test_folder_choice_is_saved_and_restricted(self):
        self.populate()
        with patch.dict(os.environ, {"FRUIT_DB_PATH": str(self.db)}), patch.object(vod, "ensure_worker"):
            client = create_app().test_client()
            response = client.post("/api/xtream/vod/downloads", json={"kind": "movie", "item_id": "123", "folder": "tv"})
            self.assertEqual(response.status_code, 202)
            tv_job = response.get_json()["job_id"]
            self.assertEqual(client.post("/api/xtream/vod/downloads", json={"kind": "movie", "item_id": "123", "folder": "tv"}).get_json()["job_id"], tv_job)
            movie_job = client.post("/api/xtream/vod/downloads", json={"kind": "movie", "item_id": "123"}).get_json()["job_id"]
            self.assertNotEqual(movie_job, tv_job)
            invalid = client.post("/api/xtream/vod/downloads", json={"kind": "movie", "item_id": "123", "folder": "../elsewhere"})
            self.assertEqual(invalid.status_code, 400)
            malformed = client.post("/api/xtream/vod/downloads", json={"kind": "movie", "item_id": "123", "folder": ["tv"]})
            self.assertEqual(malformed.status_code, 400)
            jobs = client.get("/api/xtream/vod/downloads").get_json()["jobs"]
        self.assertEqual({job["folder"] for job in jobs}, {"movies", "tv"})
        self.assertEqual(vod.folder_dir("tv"), vod.download_dir() / "TV Shows")
        with patch.dict(os.environ, {"XTREAM_TV_FOLDER": "Shows/Elsewhere"}):
            with self.assertRaisesRegex(ValueError, "one folder name"):
                vod.folder_dir("tv")

    def test_staged_download_waits_for_start_and_uses_selected_folder(self):
        self.populate()
        with patch.dict(os.environ, {"FRUIT_DB_PATH": str(self.db)}), patch.object(vod, "ensure_worker") as worker:
            client = create_app().test_client()
            staged = client.post("/api/xtream/vod/downloads/stage", json={"kind": "movie", "item_id": "123"})
            self.assertEqual(staged.status_code, 201)
            job_id = staged.get_json()["job_id"]
            again = client.post("/api/xtream/vod/downloads/stage", json={"kind": "movie", "item_id": "123"})
            self.assertEqual(again.get_json()["job_id"], job_id)
            worker.assert_not_called()
            stopped = threading.Event()
            stopped.set()
            vod.run_worker(self.db, stop_event=stopped)
            with self.conn() as conn:
                self.assertEqual(conn.execute("SELECT state FROM xtream_vod_jobs WHERE id=?", (job_id,)).fetchone()[0], "staged")
            invalid = client.post(f"/api/xtream/vod/downloads/{job_id}/start", json={"folder": "../elsewhere"})
            self.assertEqual(invalid.status_code, 400)
            started = client.post(f"/api/xtream/vod/downloads/{job_id}/start", json={"folder": "tv"})
            self.assertEqual(started.status_code, 202)
            worker.assert_called_once()
            self.assertEqual(client.post(f"/api/xtream/vod/downloads/{job_id}/start", json={"folder": "movies"}).status_code, 409)
            already_started = client.post("/api/xtream/vod/downloads/stage", json={"kind": "movie", "item_id": "123"}).get_json()
            self.assertEqual((already_started["job_id"], already_started["state"]), (job_id, "pending"))
            episode = client.post("/api/xtream/vod/downloads/stage", json={"kind": "episode", "item_id": "789"})
            episode_id = episode.get_json()["job_id"]
            self.assertEqual(client.delete(f"/api/xtream/vod/downloads/{episode_id}").status_code, 200)
            jobs = client.get("/api/xtream/vod/downloads").get_json()["jobs"]
        self.assertEqual(len(jobs), 1)
        self.assertEqual((jobs[0]["state"], jobs[0]["folder"]), ("pending", "tv"))

    def test_episode_reuses_a_unique_existing_plex_show_folder(self):
        self.populate()
        existing = vod.folder_dir("tv") / "A Show (2020) {tmdb-123}"
        existing.mkdir(parents=True)
        with self.conn() as conn:
            job_id = vod.queue_download(conn, "episode", "789")
            job = dict(conn.execute("SELECT * FROM xtream_vod_jobs WHERE id=?", (job_id,)).fetchone())
        self.assertEqual(vod._job_download_dir(job), existing / "Season 01")
        self.assertFalse((vod.folder_dir("tv") / "A Show").exists())
        self.assertIn("s01e02", vod._filename(job))
        special = dict(job, season=0, episode=1)
        self.assertEqual(vod._job_download_dir(special), existing / "Season 00")
        self.assertIn("s00e01", vod._filename(special))
        long_job = dict(job, series_title="É" * 150, title="É" * 150)
        self.assertLessEqual(len(vod._filename(long_job).encode("utf-8")), 240)

    def test_staged_episode_from_older_schema_gets_plex_metadata_on_start(self):
        self.populate()
        with self.conn() as conn:
            job_id = vod.stage_download(conn, "episode", "789")
            conn.execute("UPDATE xtream_vod_jobs SET series_title=NULL,season=NULL,episode=NULL WHERE id=?", (job_id,))
            conn.commit()
            self.assertTrue(vod.start_staged_download(conn, job_id, "tv"))
            job = dict(conn.execute("SELECT * FROM xtream_vod_jobs WHERE id=?", (job_id,)).fetchone())
        self.assertEqual((job["series_title"], job["season"], job["episode"]), ("A Show", 1, 2))
        self.assertEqual(vod._job_download_dir(job), vod.folder_dir("tv") / "A Show" / "Season 01")

    def test_episode_show_folder_match_does_not_guess_between_years(self):
        self.populate()
        root = vod.folder_dir("tv")
        (root / "A Show (2018)").mkdir(parents=True)
        (root / "A Show (2020)").mkdir()
        with self.conn() as conn:
            job_id = vod.queue_download(conn, "episode", "789")
            job = dict(conn.execute("SELECT * FROM xtream_vod_jobs WHERE id=?", (job_id,)).fetchone())
        with self.assertRaisesRegex(OSError, "Multiple existing show folders"):
            vod._job_download_dir(job)
        vod._run_job(self.db, job, object())
        with self.conn() as conn:
            failed = conn.execute("SELECT state,error FROM xtream_vod_jobs WHERE id=?", (job_id,)).fetchone()
        self.assertEqual(failed["state"], "failed")
        self.assertIn("Multiple existing show folders", failed["error"])

    def test_episode_prefers_existing_title_over_a_year_guess(self):
        self.populate()
        root = vod.folder_dir("tv")
        (root / "a show").mkdir(parents=True)
        (root / "A Show (2020)").mkdir()
        with self.conn() as conn:
            job_id = vod.queue_download(conn, "episode", "789")
            job = dict(conn.execute("SELECT * FROM xtream_vod_jobs WHERE id=?", (job_id,)).fetchone())
        self.assertTrue(vod._job_download_dir(job).parent.samefile(root / "a show"))

    def test_existing_jobs_keep_original_root_destination(self):
        with self.conn() as conn:
            conn.execute("""CREATE TABLE xtream_vod_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, item_id TEXT NOT NULL,
                series_id TEXT, title TEXT NOT NULL, extension TEXT NOT NULL,
                state TEXT NOT NULL, bytes_done INTEGER NOT NULL DEFAULT 0,
                bytes_total INTEGER, filename TEXT, error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
            conn.execute("INSERT INTO xtream_vod_jobs(kind,item_id,title,extension,state) VALUES ('movie','123','Old Film','mp4','cancelling')")
            vod.ensure_schema(conn)
            job = dict(conn.execute("SELECT * FROM xtream_vod_jobs").fetchone())
        self.assertEqual(job["folder"], "root")
        self.assertEqual(vod._job_download_dir(job), vod.download_dir())
        vod.download_dir().mkdir()
        partial = vod.download_dir() / (vod._filename(job) + ".part")
        partial.write_bytes(b"partial")
        stopped = threading.Event()
        stopped.set()
        vod.run_worker(self.db, stop_event=stopped)
        self.assertFalse(partial.exists())

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
        self.assertFalse(list(vod.folder_dir("movies").iterdir()))

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
