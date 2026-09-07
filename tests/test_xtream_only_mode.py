import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))

import daily_refresh  # noqa: E402
from db.preferences import save_settings  # noqa: E402
from fruit_build_lanes import (  # noqa: E402
    build_lanes_with_placeholders,
    create_lanes,
    ensure_lane_schema,
    load_future_events,
)
from filter_integration import get_filtered_playables  # noqa: E402
from fruit_export_lanes import build_lanes_m3u, build_lanes_xmltv  # noqa: E402
from server.app import create_app  # noqa: E402
from server.services.filters import _build_filters  # noqa: E402
from server.services.lanes import (  # noqa: E402
    get_event_link_info,
    get_lane_direct_stream,
    get_provider_lane_stats,
    get_provider_playable_link,
)


class XtreamOnlyModeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "fruit.db"
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript("""
            CREATE TABLE events (
                id TEXT PRIMARY KEY, pvid TEXT, slug TEXT, title TEXT,
                channel_name TEXT, start_utc TEXT, end_utc TEXT,
                raw_attributes_json TEXT, genres_json TEXT,
                classification_json TEXT, synopsis TEXT, hero_image_url TEXT
            );
            CREATE TABLE playables (
                event_id TEXT, playable_id TEXT, provider TEXT,
                service_name TEXT, logical_service TEXT, deeplink_play TEXT,
                deeplink_open TEXT, http_deeplink_url TEXT, playable_url TEXT,
                stream_url TEXT, stream_id TEXT, stream_extension TEXT,
                stream_metadata_json TEXT, title TEXT, content_id TEXT,
                priority INTEGER, created_utc TEXT,
                PRIMARY KEY(event_id, playable_id)
            );
            CREATE TABLE user_preferences (
                key TEXT PRIMARY KEY, value TEXT, updated_utc TEXT
            );
            CREATE TABLE provider_lanes (
                provider_code TEXT PRIMARY KEY, adb_enabled INTEGER,
                adb_lane_count INTEGER, created_at TEXT, updated_at TEXT,
                logo_url TEXT
            );
            CREATE TABLE event_images (
                event_id TEXT, url TEXT, img_type TEXT
            );
        """)
        self.conn.execute("ALTER TABLE events ADD COLUMN last_seen_utc TEXT")
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self._add_event("xtream-event", "Xtream game", "xtream", "701", 1)
        self._add_event("peacock-event", "Peacock game", "peacock", None, 5)
        ensure_lane_schema(self.conn)
        create_lanes(self.conn, 1)

    def _add_event(self, event_id, title, provider, stream_id, hours_from_now):
        start = self.now + timedelta(hours=hours_from_now)
        end = start + timedelta(hours=2)
        self.conn.execute(
            """INSERT INTO events (
                id, pvid, slug, title, channel_name, start_utc, end_utc,
                raw_attributes_json, genres_json, classification_json,
                synopsis, hero_image_url
            ) VALUES (?, ?, NULL, ?, ?, ?, ?, '[]', '[\"Sports\"]', '[]', NULL, ?)
            """,
            (event_id, event_id, title, provider, start.isoformat(), end.isoformat(),
             "https://images.example/event.png"),
        )
        self.conn.execute(
            """INSERT INTO playables (
                event_id, playable_id, provider, service_name, logical_service,
                deeplink_play, playable_url, stream_id, stream_extension, priority
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
            (event_id, f"{provider}-playable", provider, provider, provider,
             f"{provider}://private", f"https://{provider}.example/play", stream_id, "ts"),
        )
        self.conn.commit()

    def _plan(self):
        self.conn.execute("DELETE FROM lane_events")
        events = load_future_events(self.conn, 2)
        build_lanes_with_placeholders(self.conn, events, 1)

    def test_xtream_only_excludes_non_xtream_and_keeps_placeholder_coverage(self):
        self.assertTrue(save_settings(self.conn, {"xtream_only": True}))
        self._plan()
        selected = self.conn.execute(
            "SELECT event_id, chosen_provider FROM lane_events WHERE is_placeholder = 0"
        ).fetchall()
        self.assertEqual([("xtream-event", "xtream")], [tuple(row) for row in selected])
        self.assertGreater(
            self.conn.execute("SELECT COUNT(*) FROM lane_events WHERE is_placeholder = 1").fetchone()[0], 0
        )

    def test_tune_time_uses_xtream_stream_id_and_exports_never_contain_credentials(self):
        self.assertTrue(save_settings(self.conn, {"xtream_only": True, "xtream_enabled": True, "xtream_category_ids": "1"}))
        self._plan()
        env = {
            "XTREAM_SERVER_URL": "http://provider.example:8080",
            "XTREAM_USERNAME": "private user",
            "XTREAM_PASSWORD": "private/password",
        }
        with patch.dict(os.environ, env, clear=False):
            playable = get_lane_direct_stream(
                self.conn, 1, (self.now + timedelta(hours=1, minutes=15)).isoformat()
            )
        self.assertEqual("701", playable["stream_id"])
        self.assertTrue(playable["stream_url"].endswith("/701.ts"))

        m3u = Path(self.tmp.name) / "lanes.m3u"
        xml = Path(self.tmp.name) / "lanes.xml"
        build_lanes_m3u(self.conn, str(m3u), "http://fruit.test:6655")
        build_lanes_xmltv(self.conn, str(xml))
        combined = m3u.read_text() + xml.read_text()
        self.assertIn("/lane/1/stream.m3u8", combined)
        self.assertIn("Xtream game", combined)
        self.assertNotIn("private user", combined)
        self.assertNotIn("private/password", combined)

    def test_http_status_and_provider_views_hide_non_xtream_when_enabled(self):
        self.assertTrue(save_settings(self.conn, {"xtream_only": True, "xtream_enabled": True}))
        self.assertEqual(["xtream"], [p["provider_code"] for p in get_provider_lane_stats(self.conn)])
        self.assertEqual(["xtream"], [p["scheme"] for p in _build_filters(self.conn)["providers"]])
        env = {
            "FRUIT_DB_PATH": str(self.db_path),
            "XTREAM_USERNAME": "private user",
            "XTREAM_PASSWORD": "private/password",
            "KAYO_ENABLED": "true",
        }
        with patch.dict(os.environ, env, clear=False):
            response = create_app().test_client().get("/api/status")
        body = response.get_data(as_text=True)
        self.assertEqual(200, response.status_code)
        self.assertIn('"xtream_only":true', body)
        self.assertNotIn("private user", body)
        self.assertNotIn("private/password", body)
        self.assertNotIn("KAYO_ENABLED", body)

    def test_shared_selection_boundary_and_event_views_fail_closed(self):
        self.assertTrue(save_settings(self.conn, {"xtream_only": True}))

        filtered = get_filtered_playables(self.conn, "peacock-event", [])
        self.assertEqual([], filtered)
        self.assertIsNone(get_provider_playable_link(self.conn, "peacock-event", "peacock")["deeplink"])
        self.assertIsNone(get_event_link_info(self.conn, "peacock-event", "id", None, None)["deeplink_url"])

        env = {"FRUIT_DB_PATH": str(self.db_path)}
        with patch.dict(os.environ, env, clear=False):
            client = create_app().test_client()
            listed = client.get("/api/events?days_back=0&days_forward=2").get_json()
            self.assertEqual(["xtream-event"], [item["id"] for item in listed["items"]])
            self.assertEqual(404, client.get("/api/events/peacock-event").status_code)
            detail = client.get("/api/events/xtream-event").get_json()
            self.assertEqual(["xtream"], [item["provider"] for item in detail["playables"]])

    def test_disabling_xtream_only_restores_multisource_lane_selection(self):
        self.assertTrue(save_settings(self.conn, {"xtream_only": False}))
        self._plan()
        selected = self.conn.execute(
            "SELECT event_id FROM lane_events WHERE is_placeholder = 0 ORDER BY event_id"
        ).fetchall()
        self.assertEqual(["peacock-event", "xtream-event"], [row[0] for row in selected])


class XtreamOnlyProviderPlanTest(unittest.TestCase):
    def test_provider_plan_disables_every_non_xtream_source(self):
        with patch.object(daily_refresh, "_get_db_setting", side_effect=lambda key, fallback=None: key == "xtream_only"):
            plan = daily_refresh._build_provider_plan([])
        self.assertTrue(plan["xtream"])
        self.assertFalse(plan["apple_base"])
        self.assertFalse(plan["amazon_enrichment"])
        self.assertTrue(all(not enabled for key, enabled in plan.items() if key not in {"xtream"}))


if __name__ == "__main__":
    unittest.main()
