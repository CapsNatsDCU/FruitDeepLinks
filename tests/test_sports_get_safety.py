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

    def test_repeated_my_sports_gets_preserve_materialized_rows(self):
        urls = ["/api/sports/catalog", "/api/sports/rules", "/api/sports/coverage",
                f"/api/sports/events/{self.event['canonical_event_id']}",
                "/api/sports/provider-capacities", "/api/sports/health"]

        def snapshot():
            with sqlite3.connect(self.db_path) as conn:
                tables = [row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )]
                return {name: conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                        for name in tables}

        before = snapshot()
        for _ in range(2):
            for url in urls:
                self.assertEqual(200, self.client.get(url).status_code, url)
        self.assertEqual(before, snapshot())

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

    def test_my_sports_xtream_recommendations_are_cached_read_only(self):
        with patch("server.routes.api.xtream.ensure_sports_schema", side_effect=AssertionError("GET wrote schema")), \
             patch("server.routes.api.xtream.XtreamClient", side_effect=AssertionError("GET contacted provider")):
            response = self.client.get("/api/xtream/discovery/recommendations")
        self.assertEqual(200, response.status_code)

    def test_catalog_browser_and_rule_labels_use_materialized_ids(self):
        teams = self.client.get("/api/sports/catalog").get_json()["teams"]
        team_id = next(team["id"] for team in teams if team["name"] == "Washington Capitals")
        created = self.client.post("/api/sports/rules", json={
            "target_type": "team", "target_id": team_id, "policy": "PRIORITIZE",
        })
        self.assertEqual(201, created.status_code)
        rules = self.client.get("/api/sports/rules").get_json()["rules"]
        self.assertEqual("Washington Capitals", rules[0]["target_name"])
        page = self.client.get("/sports-catalog")
        self.assertEqual(200, page.status_code)
        self.assertIn(b"Search sports, leagues, teams", page.data)


if __name__ == "__main__":
    unittest.main()
