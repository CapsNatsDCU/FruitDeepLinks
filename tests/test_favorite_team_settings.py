import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


BIN_DIR = Path(__file__).resolve().parents[1] / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from server.app import create_app
from server.services.favorite_teams import (
    StoredFavoriteTeamsMalformed,
    create_team,
    delete_team,
    export_favorite_team_settings,
    import_favorite_team_settings,
    load_favorite_team_settings,
    preview_team_match,
    reset_favorite_team_settings,
    set_global_enabled,
    set_team_enabled,
    update_team,
)
from team_preferences import TeamPreferenceValidationError


CAPITALS = {
    "team": "Washington Capitals",
    "enabled": True,
    "aliases": [" Washington Capitals ", "Capitals", "capitals", "WSH"],
    "preferred_terms": [" WASHINGTON CAPITALS ", "MONUMENTAL", "monumental"],
    "avoid_terms": [" Rangers Broadcast "],
}

WASHINGTON_FIXTURE = [
    CAPITALS,
    {
        "team": "Washington Nationals", "enabled": True,
        "aliases": ["Washington Nationals", "Nationals", "Nats", "WSH Nationals"],
        "preferred_terms": ["WASHINGTON NATIONALS", "Nationals Broadcast"],
        "avoid_terms": [],
    },
    {
        "team": "Washington Commanders", "enabled": True,
        "aliases": ["Washington Commanders", "Commanders"],
        "preferred_terms": [
            "WRC", "WTTG", "WUSA", "WJLA", "NBC 4", "FOX 5", "CBS 9",
            "ABC 7", "Washington Commanders",
        ],
        "avoid_terms": [],
    },
    {
        "team": "DC United", "enabled": True,
        "aliases": ["DC United", "D.C. United"],
        "preferred_terms": ["DC United"],
        "avoid_terms": [],
    },
]


def make_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE user_preferences (key TEXT PRIMARY KEY, value TEXT, updated_utc TEXT)"
    )
    return conn


class FavoriteTeamSettingsServiceTest(unittest.TestCase):
    def setUp(self):
        self.conn = make_conn()

    def tearDown(self):
        self.conn.close()

    def test_default_load_and_global_toggle(self):
        self.assertEqual([], load_favorite_team_settings(self.conn)["teams"])
        state = set_global_enabled(self.conn, True)
        self.assertTrue(state["enabled"])

    def test_create_normalizes_whitespace_dedupes_and_adds_display_alias(self):
        result = create_team(self.conn, CAPITALS)
        team = result["team"]
        self.assertEqual("Washington Capitals", team["team"])
        self.assertEqual(
            ["Washington Capitals", "Capitals", "WSH"], team["aliases"]
        )
        self.assertEqual(
            ["WASHINGTON CAPITALS", "MONUMENTAL"], team["preferred_terms"]
        )
        self.assertEqual(["Rangers Broadcast"], team["avoid_terms"])

    def test_create_rejects_blank_name_blank_values_and_duplicate_team(self):
        with self.assertRaises(TeamPreferenceValidationError):
            create_team(self.conn, {"team": "  ", "aliases": []})
        with self.assertRaises(TeamPreferenceValidationError):
            create_team(self.conn, {"team": "Capitals", "aliases": [" "]})
        create_team(self.conn, CAPITALS)
        with self.assertRaises(TeamPreferenceValidationError):
            create_team(self.conn, dict(CAPITALS, team=" washington capitals "))

    def test_edit_enable_disable_and_delete(self):
        create_team(self.conn, CAPITALS)
        updated = update_team(self.conn, 0, {
            "team": "Washington Caps",
            "aliases": ["Caps"],
            "preferred_terms": ["Monumental Sports"],
            "avoid_terms": [],
            "enabled": True,
        })
        self.assertEqual("Washington Caps", updated["team"]["team"])
        disabled = set_team_enabled(self.conn, 0, False)
        self.assertFalse(disabled["team"]["enabled"])
        deleted = delete_team(self.conn, 0)
        self.assertEqual("Washington Caps", deleted["deleted"]["team"])
        self.assertEqual([], deleted["settings"]["teams"])

    def test_malformed_storage_is_reported_and_not_destroyed_on_read(self):
        malformed = "{definitely-not-json"
        self.conn.execute(
            "INSERT INTO user_preferences (key, value) VALUES ('setting:favorite_teams', ?)",
            (malformed,),
        )
        self.conn.commit()
        state = load_favorite_team_settings(self.conn)
        self.assertTrue(state["malformed"])
        self.assertEqual([], state["teams"])
        stored = self.conn.execute(
            "SELECT value FROM user_preferences WHERE key='setting:favorite_teams'"
        ).fetchone()[0]
        self.assertEqual(malformed, stored)
        with self.assertRaises(StoredFavoriteTeamsMalformed):
            create_team(self.conn, CAPITALS)
        self.conn.execute(
            "INSERT INTO user_preferences (key, value) VALUES ('setting:server_url', ?)",
            (json.dumps("http://example.test:6655"),),
        )
        recovered = reset_favorite_team_settings(self.conn)
        self.assertFalse(recovered["malformed"])
        unrelated = self.conn.execute(
            "SELECT value FROM user_preferences WHERE key='setting:server_url'"
        ).fetchone()[0]
        self.assertEqual("http://example.test:6655", json.loads(unrelated))

    def test_import_validation_and_export_output(self):
        with self.assertRaises(TeamPreferenceValidationError):
            import_favorite_team_settings(self.conn, {"enabled": True, "teams": "bad"})
        with self.assertRaises(TeamPreferenceValidationError):
            import_favorite_team_settings(self.conn, {
                "schema_version": 2, "enabled": True, "teams": [],
            })
        with self.assertRaises(TeamPreferenceValidationError):
            import_favorite_team_settings(self.conn, {
                "enabled": True,
                "teams": [{"team": "Capitals", "aliases": [""]}],
            })
        state = import_favorite_team_settings(self.conn, {
            "enabled": True,
            "teams": [CAPITALS],
        })
        self.assertTrue(state["enabled"])
        exported = export_favorite_team_settings(self.conn)
        self.assertEqual(1, exported["schema_version"])
        self.assertEqual("Washington Capitals", exported["teams"][0]["team"])

    def test_preview_uses_shared_match_and_scoring_explanations(self):
        create_team(self.conn, CAPITALS)
        set_global_enabled(self.conn, True)
        preview = preview_team_match(self.conn, {
            "team_index": 0,
            "event_title": "Washington Capitals at Rangers",
            "feed_name": "US: WASHINGTON CAPITALS on MONUMENTAL",
        })
        preference = preview["team_preference"]
        self.assertEqual("Washington Capitals", preference["event_matches"][0]["matched_term"])
        self.assertEqual(170, preference["score"])
        self.assertEqual([100, 70], [reason["score"] for reason in preference["reasons"]])

    def test_realistic_four_team_gui_fixture_imports_without_hardcoded_logic(self):
        state = import_favorite_team_settings(self.conn, {
            "enabled": True,
            "teams": WASHINGTON_FIXTURE,
        })
        self.assertEqual(
            [
                "Washington Capitals", "Washington Nationals",
                "Washington Commanders", "DC United",
            ],
            [team["team"] for team in state["teams"]],
        )
        commanders = state["teams"][2]
        self.assertIn("NBC 4", commanders["preferred_terms"])
        self.assertIn("ABC 7", commanders["preferred_terms"])


class FavoriteTeamSettingsApiTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "settings.db"
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE user_preferences (key TEXT PRIMARY KEY, value TEXT, updated_utc TEXT);
            CREATE TABLE events (
                id TEXT PRIMARY KEY, title TEXT, start_utc TEXT, end_utc TEXT,
                channel_name TEXT, last_seen_utc TEXT, classification_json TEXT,
                genres_json TEXT, content_segments_json TEXT, raw_attributes_json TEXT
            );
            CREATE TABLE playables (
                event_id TEXT, playable_id TEXT, provider TEXT, deeplink_play TEXT,
                deeplink_open TEXT, playable_url TEXT, title TEXT, content_id TEXT,
                priority INTEGER, service_name TEXT, espn_graph_id TEXT,
                logical_service TEXT, locale TEXT, feed_name TEXT, feed_type TEXT,
                http_deeplink_url TEXT, stream_metadata_json TEXT
            );
            """
        )
        conn.close()
        self.env = patch.dict(os.environ, {"FRUIT_DB_PATH": str(self.db_path)})
        self.env.start()
        self.client = create_app().test_client()

    def tearDown(self):
        self.env.stop()
        self.tempdir.cleanup()

    def test_complete_crud_toggle_preview_export_import_and_reset_workflow(self):
        empty = self.client.get("/api/settings/favorite-teams")
        self.assertEqual(200, empty.status_code)
        self.assertEqual([], empty.get_json()["teams"])

        created = self.client.post("/api/settings/favorite-teams", json=CAPITALS)
        self.assertEqual(201, created.status_code)
        self.assertEqual(1, len(created.get_json()["settings"]["teams"]))

        enabled = self.client.patch(
            "/api/settings/favorite-teams", json={"enabled": True}
        )
        self.assertTrue(enabled.get_json()["enabled"])

        disabled_team = self.client.patch(
            "/api/settings/favorite-teams/0", json={"enabled": False}
        )
        self.assertFalse(disabled_team.get_json()["team"]["enabled"])
        self.client.patch("/api/settings/favorite-teams/0", json={"enabled": True})

        edited = self.client.put("/api/settings/favorite-teams/0", json={
            "team": "Washington Capitals Hockey",
            "aliases": ["Capitals"],
            "preferred_terms": ["Monumental"],
            "avoid_terms": [],
            "enabled": True,
        })
        self.assertEqual("Washington Capitals Hockey", edited.get_json()["team"]["team"])

        preview = self.client.post("/api/settings/favorite-teams/preview", json={
            "team_index": 0,
            "event_title": "Washington Capitals Hockey at Rangers",
            "feed_name": "Monumental Washington Capitals Hockey Broadcast",
        })
        self.assertGreater(preview.get_json()["team_preference"]["score"], 0)

        exported = self.client.get("/api/settings/favorite-teams/export")
        self.assertEqual(200, exported.status_code)
        self.assertIn("attachment", exported.headers["Content-Disposition"])
        exported_json = json.loads(exported.get_data(as_text=True))
        self.assertEqual(1, exported_json["schema_version"])

        invalid_import = self.client.post(
            "/api/settings/favorite-teams/import",
            json={"enabled": True, "teams": [{"team": "", "aliases": []}]},
        )
        self.assertEqual(400, invalid_import.status_code)
        malformed_request = self.client.post(
            "/api/settings/favorite-teams",
            data="not json",
            content_type="application/json",
        )
        self.assertEqual(400, malformed_request.status_code)

        deleted = self.client.delete("/api/settings/favorite-teams/0")
        self.assertEqual([], deleted.get_json()["settings"]["teams"])

        imported = self.client.post(
            "/api/settings/favorite-teams/import", json=exported_json
        )
        self.assertEqual(1, len(imported.get_json()["teams"]))

        reset = self.client.delete("/api/settings/favorite-teams")
        self.assertFalse(reset.get_json()["enabled"])
        self.assertEqual([], reset.get_json()["teams"])

    def test_settings_page_renders_no_json_editor_and_backend_errors_are_returned(self):
        page = self.client.get("/settings")
        self.assertEqual(200, page.status_code)
        html = page.get_data(as_text=True)
        self.assertIn("Favorite Teams &amp; Broadcasters", html)
        self.assertIn("Add Favorite Team", html)
        self.assertIn("Test Match", html)
        self.assertNotIn("FAVORITE_TEAMS_JSON", html)
        blank = self.client.post(
            "/api/settings/favorite-teams",
            json={"team": " ", "aliases": [], "preferred_terms": [], "avoid_terms": []},
        )
        self.assertEqual(400, blank.status_code)
        self.assertIn("cannot be blank", blank.get_json()["message"])

    def test_event_inspector_exposes_shared_score_reasons_and_final_rank(self):
        self.client.post("/api/settings/favorite-teams/import", json={
            "enabled": True,
            "teams": [CAPITALS],
        })
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, '[]', '[]', '[]', '{}')",
            (
                "capitals-event", "Washington Capitals at Penguins",
                "2026-08-29T18:00:00Z", "2026-08-29T21:00:00Z",
                "NHL", "2026-08-29T17:00:00Z",
            ),
        )
        rows = [
            (
                "capitals-event", "a-espn", "sportscenter", "sportscenter://espn",
                None, None, "ESPN+", "espn", 10, "ESPN+", None,
                "espn_plus", "en_US", None, "NATIONAL", None, None,
            ),
            (
                "capitals-event", "z-monumental", "xtream", "xtream://monumental",
                None, None, "Monumental Sports Network", "monumental", 10,
                "Xtream IPTV", None, "xtream", "en_US", None, None, None,
                json.dumps({"original_stream_name": "MONUMENTAL"}),
            ),
        ]
        conn.executemany(
            "INSERT INTO playables VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
        )
        conn.commit()
        conn.close()

        detail = self.client.get("/api/events/capitals-event")
        self.assertEqual(200, detail.status_code)
        data = detail.get_json()
        self.assertEqual("z-monumental", data["best"]["playable_id"])
        self.assertEqual(70, data["best"]["team_preference"]["score"])
        self.assertEqual(1, data["best"]["ranking_position"])
        by_id = {item["playable_id"]: item for item in data["playables"]}
        self.assertTrue(by_id["z-monumental"]["selected"])
        self.assertEqual(2, by_id["a-espn"]["ranking_position"])


if __name__ == "__main__":
    unittest.main()
