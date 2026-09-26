import os
import json
import sqlite3
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from server.app import create_app
from sports_metadata import ensure_schema, resolve_source_event, save_rule, coverage
from catalog_workbench import set_visibility_override


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

    def test_coverage_exposes_the_configured_display_timezone(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS user_preferences (key TEXT PRIMARY KEY, value TEXT, updated_utc TEXT)")
            conn.execute("INSERT OR REPLACE INTO user_preferences(key,value) VALUES(?,?)",
                         ("setting:timezone", json.dumps("America/Chicago")))
        coverage = self.client.get("/api/sports/coverage").get_json()
        self.assertEqual("America/Chicago", coverage["display_timezone"])
        self.assertEqual("absolute_utc", coverage["timestamp_contract"])
        page = self.client.get("/my-sports")
        self.assertIn(b"formatUtc", page.data)

    def test_quiet_league_stays_materialized_but_has_no_coverage_status(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            league_id = conn.execute("SELECT league_id FROM canonical_events WHERE id=?", (self.event["canonical_event_id"],)).fetchone()[0]
            save_rule(conn, target_type="league", target_id=league_id, policy="PRIORITIZE")
            self.assertEqual(1, len(coverage(conn, days=90)))
            set_visibility_override(conn, entity_type="league", fruit_id=league_id, visibility="quiet")
            conn.commit()
            self.assertEqual([], coverage(conn, days=90))
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM canonical_events").fetchone()[0])

    def test_identity_filters_hide_hidden_by_default_but_direct_search_reveals_it(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            league_id = conn.execute("SELECT league_id FROM canonical_events WHERE id=?", (self.event["canonical_event_id"],)).fetchone()[0]
            set_visibility_override(conn, entity_type="league", fruit_id=league_id, visibility="hidden")
            conn.commit()
        default_items = self.client.get("/api/sports/catalog/identities?types=league").get_json()["items"]
        self.assertFalse(any(item["id"] == league_id for item in default_items))
        searched = self.client.get("/api/sports/catalog/identities?q=NHL&types=league").get_json()["items"]
        self.assertTrue(any(item["id"] == league_id and item["effective_visibility"]["visibility"] == "hidden" for item in searched))

    def test_identity_filters_read_pre_visibility_database_without_writing_schema(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DROP TABLE catalog_identity_attention")
            conn.execute("DROP TABLE catalog_saved_views")
        with patch("server.routes.api.sports.ensure_schema", side_effect=AssertionError("GET wrote schema")):
            response = self.client.get("/api/sports/catalog/identities")
        self.assertEqual(200, response.status_code)
        self.assertTrue(response.get_json()["items"])
        self.assertEqual([], response.get_json()["saved_views"])

    def test_catalog_pages_expose_entries_beyond_first_fifty(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                "INSERT INTO sports(id,name,normalized_name,created_utc,updated_utc) VALUES(?,?,?,?,?)",
                [(f"paging-{index:03d}", f"Paging Sport {index:03d}", f"paging sport {index:03d}", "now", "now")
                 for index in range(53)],
            )
        first = self.client.get("/api/sports/catalog/identities?q=Paging&types=sport&page=1&per_page=50").get_json()
        second = self.client.get("/api/sports/catalog/identities?q=Paging&types=sport&page=2&per_page=50").get_json()
        past_end = self.client.get("/api/sports/catalog/identities?q=Paging&types=sport&page=99&per_page=50").get_json()
        self.assertEqual((53, 50, 1), (first["total"], len(first["items"]), first["page"]))
        self.assertEqual((53, 3, 2), (second["total"], len(second["items"]), second["page"]))
        self.assertEqual([item["id"] for item in second["items"]], [item["id"] for item in past_end["items"]])


if __name__ == "__main__":
    unittest.main()
