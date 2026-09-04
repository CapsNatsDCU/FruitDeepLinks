import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from fruit_export_lanes import build_lanes_xmltv  # noqa: E402
from server.app import create_app  # noqa: E402


class GuideApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "fruit.db"
        self.start = datetime(2026, 9, 3, 18, tzinfo=timezone.utc)
        self.end = self.start + timedelta(hours=2)
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
            CREATE TABLE lanes (lane_id INTEGER PRIMARY KEY, name TEXT, logical_number INTEGER);
            CREATE TABLE lane_events (lane_id INTEGER, event_id TEXT, is_placeholder INTEGER, start_utc TEXT, end_utc TEXT, title TEXT, chosen_playable_id TEXT, chosen_provider TEXT, chosen_logical_service TEXT, chosen_deeplink TEXT);
            CREATE TABLE events (id TEXT PRIMARY KEY, title TEXT, synopsis TEXT, channel_name TEXT, genres_json TEXT, classification_json TEXT, pvid TEXT, hero_image_url TEXT, raw_attributes_json TEXT);
            CREATE TABLE playables (event_id TEXT, playable_id TEXT, provider TEXT, stream_id TEXT, stream_metadata_json TEXT);
            CREATE TABLE event_images (event_id TEXT, img_type TEXT, url TEXT);
            CREATE TABLE user_preferences (key TEXT PRIMARY KEY, value TEXT, updated_utc TEXT);
        """)
        for lane in range(1, 51):
            conn.execute("INSERT INTO lanes VALUES (?, ?, ?)", (lane, f"Fruit Lane {lane}", 8999 + lane))
            conn.execute("INSERT INTO lane_events VALUES (?, ?, 1, ?, ?, ?, NULL, NULL, NULL, NULL)",
                         (lane, f"placeholder-{lane}", self.start.isoformat(), self.end.isoformat(), f"Fruit Lane {lane}"))
        metadata = '{"category_id":"10","category_name":"NFL PPV","stream_id":"500","username":"never-return"}'
        conn.execute("INSERT INTO events VALUES (?, ?, '', 'NFL PPV', '[]', '[]', '', '', ?)",
                     ("xtream-event", "Commanders at Ravens", metadata))
        conn.execute("INSERT INTO playables VALUES ('xtream-event', 'xtream-playable', 'xtream', '500', ?)", (metadata,))
        conn.execute("INSERT INTO playables VALUES ('xtream-event', 'alternate-playable', 'other', NULL, '{}')")
        conn.execute("INSERT INTO user_preferences VALUES ('setting:favorite_teams', ?, ?)",
                     ('[{"team":"Washington Commanders","aliases":["Commanders"],"preferred_terms":["NFL PPV"],"avoid_terms":[],"enabled":true}]', self.start.isoformat()))
        conn.execute("INSERT INTO lane_events VALUES (1, 'xtream-event', 0, ?, ?, 'Commanders at Ravens', 'xtream-playable', 'xtream', 'xtream', NULL)",
                     ((self.start + timedelta(minutes=30)).isoformat(), (self.start + timedelta(minutes=90)).isoformat()))
        conn.commit()
        conn.close()
        self.env = {"FRUIT_DB_PATH": str(self.db_path), "TZ": "America/New_York"}

    def guide(self, **params):
        with patch.dict(os.environ, self.env, clear=False):
            return create_app().test_client().get("/api/guide", query_string={
                "start": self.start.isoformat(), "end": self.end.isoformat(), **params})

    def test_all_configured_lanes_and_placeholders_are_returned(self):
        response = self.guide()
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(len(payload["lanes"]), 50)
        self.assertEqual(payload["lanes"][0]["channel_number"], 9000)
        self.assertEqual(payload["lanes"][-1]["channel_number"], 9049)
        self.assertEqual(payload["summary"]["placeholders"], 50)
        self.assertEqual(payload["summary"]["real_events"], 1)
        with patch.dict(os.environ, self.env, clear=False):
            page = create_app().test_client().get("/guide")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Lane Guide", page.data)
        self.assertIn(b"/api/guide", page.data)

    def test_final_lane_record_and_xtream_details_are_safe(self):
        payload = self.guide().get_json()
        event = next(row for row in payload["programmes"] if not row["is_placeholder"])
        self.assertEqual(event["lane_id"], 1)
        self.assertEqual(event["title"], "Commanders at Ravens")
        self.assertEqual(event["start_utc"], (self.start + timedelta(minutes=30)).isoformat())
        self.assertEqual(event["xtream_category_name"], "NFL PPV")
        self.assertEqual(event["xtream_category_id"], "10")
        self.assertEqual(event["xtream_stream_id"], "500")
        self.assertTrue(event["favorite"])
        self.assertEqual(event["favorite_teams"], ["Washington Commanders"])
        self.assertEqual(event["selected_playable"]["playable_id"], "xtream-playable")
        self.assertEqual(event["playable_count"], 2)
        self.assertNotIn("username", event)
        self.assertNotIn("never-return", response_text := self.guide().get_data(as_text=True))

    def test_requested_window_and_placeholder_filter_are_honored(self):
        response = self.guide(start=(self.end + timedelta(hours=1)).isoformat(), end=(self.end + timedelta(hours=2)).isoformat())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.get_json()["lanes"]), 50)
        self.assertEqual(response.get_json()["programmes"], [])
        response = self.guide(include_placeholders="false")
        self.assertEqual(len(response.get_json()["programmes"]), 1)

    def test_api_programmes_correspond_to_xmltv_export(self):
        xml_path = Path(self.tmp.name) / "multisource_lanes.xml"
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            build_lanes_xmltv(conn, str(xml_path))
        xml = xml_path.read_text(encoding="utf-8")
        payload = self.guide().get_json()
        for programme in payload["programmes"]:
            self.assertIn(f'channel="lane.{programme["lane_id"]}"', xml)
            self.assertIn(f"<title>{programme['title']}</title>", xml)
            start = datetime.fromisoformat(programme["start_utc"])
            end = datetime.fromisoformat(programme["end_utc"])
            self.assertIn(start.strftime('start="%Y%m%d%H%M%S +0000"'), xml)
            self.assertIn(end.strftime('stop="%Y%m%d%H%M%S +0000"'), xml)


if __name__ == "__main__":
    unittest.main()
