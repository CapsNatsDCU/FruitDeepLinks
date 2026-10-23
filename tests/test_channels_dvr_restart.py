import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError, URLError


BIN = Path(__file__).resolve().parents[1] / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))

from server import scheduler
from server.services import channels_dvr_sync


class _Response:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


class ChannelsDVRRefreshTest(unittest.TestCase):
    def test_http_failure_does_not_report_success_and_guide_still_runs(self):
        calls = []

        def open_request(request, timeout):
            calls.append((request.full_url, request.get_method(), timeout))
            if len(calls) == 1:
                raise HTTPError(request.full_url, 403, "Forbidden", {}, None)
            return _Response(204)

        with patch.object(channels_dvr_sync, "urlopen", side_effect=open_request), \
                patch.object(channels_dvr_sync, "sleep"):
            result = channels_dvr_sync.refresh_source("127.0.0.1", "Fruit Source")

        self.assertEqual({"playlist": "HTTP 403", "guide": None}, result)
        self.assertEqual(["POST", "PUT"], [method for _, method, _ in calls])
        self.assertIn("Fruit%20Source", calls[0][0])
        self.assertTrue(all(timeout == 10 for _, _, timeout in calls))

    def test_unreachable_dvr_reports_both_failures(self):
        with patch.object(channels_dvr_sync, "urlopen", side_effect=URLError("down")), \
                patch.object(channels_dvr_sync, "sleep"):
            self.assertEqual({"playlist": "URLError", "guide": "URLError"},
                             channels_dvr_sync.refresh_source("127.0.0.1", "Fruit"))


class StartupReloadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        db_path = Path(self.tmp.name) / "fruit_events.db"
        with closing(sqlite3.connect(db_path)) as conn:
            with conn:
                conn.execute("CREATE TABLE xtream_persistent_channels (id INTEGER PRIMARY KEY, enabled INTEGER)")
                conn.execute("CREATE TABLE user_preferences (key TEXT PRIMARY KEY, value TEXT)")
                conn.executemany("INSERT INTO user_preferences VALUES (?, ?)", (
                    ("setting:channels_dvr_ip", '"127.0.0.1"'),
                    ("setting:channels_source_name", '"Fruit"'),
                ))
        self.db_path = db_path
        self.removed = []
        self.fake_scheduler = SimpleNamespace(remove_job=self.removed.append)
        self.patches = [
            patch.dict(os.environ, {"FRUIT_DB_PATH": str(db_path)}),
            patch.object(scheduler, "_scheduler", self.fake_scheduler),
            patch.object(scheduler, "_channel_sync_job", SimpleNamespace(id="startup")),
            patch.object(scheduler, "_channel_sync_attempts", 0),
            patch.object(scheduler, "_persistent_playlist_ready", return_value=True),
            patch.object(scheduler, "log"),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def test_saved_channels_reload_after_retry(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                conn.execute("INSERT INTO xtream_persistent_channels VALUES (1,1)")
        with patch.object(channels_dvr_sync, "refresh_source", side_effect=[
            {"playlist": "HTTP 503", "guide": None},
            {"playlist": None, "guide": None},
        ]) as refresh:
            scheduler._sync_channels_after_startup()
            self.assertEqual([], self.removed)
            scheduler._sync_channels_after_startup()
            self.assertEqual(["startup"], self.removed)
            self.assertEqual(2, refresh.call_count)

    def test_waits_for_served_playlist_before_reloading_dvr(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                conn.execute("INSERT INTO xtream_persistent_channels VALUES (1,1)")
        with patch.object(scheduler, "_persistent_playlist_ready", side_effect=[False, True]), \
                patch.object(channels_dvr_sync, "refresh_source",
                             return_value={"playlist": None, "guide": None}) as refresh:
            scheduler._sync_channels_after_startup()
            refresh.assert_not_called()
            scheduler._sync_channels_after_startup()
            refresh.assert_called_once()

    def test_empty_database_never_replaces_dvr_lineup(self):
        with patch.object(channels_dvr_sync, "refresh_source") as refresh:
            scheduler._sync_channels_after_startup()
            refresh.assert_not_called()
            self.assertEqual([], self.removed)

    def test_database_restored_after_startup_is_reloaded(self):
        saved = self.db_path.with_suffix(".saved")
        self.db_path.rename(saved)
        with patch.object(channels_dvr_sync, "refresh_source",
                          return_value={"playlist": None, "guide": None}) as refresh:
            scheduler._sync_channels_after_startup()
            refresh.assert_not_called()
            self.assertEqual([], self.removed)
            saved.rename(self.db_path)
            with closing(sqlite3.connect(self.db_path)) as conn:
                with conn:
                    conn.execute("INSERT INTO xtream_persistent_channels VALUES (1,1)")
            scheduler._sync_channels_after_startup()
            refresh.assert_called_once_with("127.0.0.1", "Fruit")
            self.assertEqual(["startup"], self.removed)


if __name__ == "__main__":
    unittest.main()
