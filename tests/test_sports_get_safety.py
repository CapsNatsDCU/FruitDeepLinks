import os
import sqlite3
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from server.app import create_app
from sports_metadata import ensure_schema, resolve_source_event


class SportsGetSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "fruit.db"
        self.old_path = os.environ.get("FRUIT_DB_PATH")
        os.environ["FRUIT_DB_PATH"] = str(self.db_path)
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            ensure_schema(conn)
            self.event = resolve_source_event(conn, source="apple", source_event_id="apple-caps", data={
                "title": "Washington Capitals at Philadelphia Flyers", "sport_name": "Ice hockey",
                "league_name": "NHL", "start_utc": "2026-10-11T17:00:00Z",
                "competitors": [{"name": "Washington Capitals", "homeAway": "away"},
                                {"name": "Philadelphia Flyers", "homeAway": "home"}],
            })
        self.client = create_app().test_client()

    def tearDown(self):
        if self.old_path is None:
            os.environ.pop("FRUIT_DB_PATH", None)
        else:
            os.environ["FRUIT_DB_PATH"] = self.old_path
        self.temp.cleanup()

    def test_my_sports_gets_are_schema_and_sync_free(self):
        # A call to either helper would write/mutate, so make it an immediate
        # failure.  The catalog must be served from refresh-materialized rows.
        urls = ["/api/sports/catalog", "/api/sports/rules", "/api/sports/coverage",
                f"/api/sports/events/{self.event['canonical_event_id']}",
                "/api/sports/provider-capacities", "/api/sports/health"]
        with patch("server.routes.api.sports.ensure_schema", side_effect=AssertionError("GET wrote schema")):
            for url in urls:
                response = self.client.get(url)
                self.assertEqual(200, response.status_code, url)

    def test_catalog_read_succeeds_while_a_refresh_writer_has_lock(self):
        # WAL plus a separate read connection gives the UI a prior committed
        # snapshot rather than competing for refresh write ownership.
        writer = sqlite3.connect(self.db_path, timeout=1)
        try:
            writer.execute("BEGIN IMMEDIATE")
            response = self.client.get("/api/sports/catalog")
            self.assertEqual(200, response.status_code)
            self.assertTrue(response.get_json()["teams"])
        finally:
            writer.rollback()
            writer.close()


if __name__ == "__main__":
    unittest.main()
