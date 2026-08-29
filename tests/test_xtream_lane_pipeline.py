import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from fruit_build_lanes import (  # noqa: E402
    build_lanes_with_placeholders,
    create_lanes,
    ensure_lane_schema,
    load_future_events,
)
from fruit_export_lanes import build_lanes_m3u, build_lanes_xmltv  # noqa: E402
from server.app import create_app  # noqa: E402
from server.services.lanes import get_lane_direct_stream  # noqa: E402
from xtream_ingest import XtreamConfig, ingest_payload  # noqa: E402


class XtreamLanePipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "fruit.db"
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        provider_zone = ZoneInfo("America/New_York")
        start_local = self.now.astimezone(provider_zone).replace(
            minute=0, second=0, microsecond=0
        )
        hour12 = start_local.hour % 12 or 12
        ampm = "am" if start_local.hour < 12 else "pm"
        self.provider_name = (
            f"NFL | 05 - {start_local.month}/{start_local.day} "
            f"{hour12}{ampm} Commanders at Ravens"
        )
        self.expected_start = start_local.astimezone(timezone.utc)
        cfg = XtreamConfig(
            enabled=True,
            server_url="http://provider.example:8080",
            username="demo user",
            password="secret/pass",
            category_ids=("10",),
            timezone_name="America/New_York",
            default_duration_minutes=180,
            event_window_days=2,
        )
        # Live-shaped get_live_streams entry: schedule information exists only
        # in the dynamic name, not in synthetic start/end timestamp fields.
        stream = {
            "num": 17,
            "stream_type": "live",
            "stream_id": "500",
            "name": self.provider_name,
            "stream_icon": "https://img.example/game.png",
            "epg_channel_id": "nhl.game",
            "added": "1693251000",
            "category_id": "10",
            "custom_sid": "",
            "tv_archive": 0,
            "direct_source": "",
            "tv_archive_duration": 0,
            "container_extension": "ts",
        }
        ingest_payload(
            self.conn,
            [{"category_id": "10", "category_name": "NFL PPV"}],
            {"10": [stream]},
            cfg,
            now=self.now,
        )
        ensure_lane_schema(self.conn)
        create_lanes(self.conn, 1)
        events = load_future_events(self.conn, 2)
        self.assertEqual(len(events), 1)
        build_lanes_with_placeholders(self.conn, events, 1)

    @property
    def tune_env(self):
        return {
            "XTREAM_ENABLED": "true",
            "XTREAM_SERVER_URL": "http://provider.example:8080",
            "XTREAM_USERNAME": "demo user",
            "XTREAM_PASSWORD": "secret/pass",
            "XTREAM_CATEGORY_IDS": "10",
            "XTREAM_TIMEZONE": "America/New_York",
        }

    def test_lane_selects_xtream_playable_and_resolves_direct_stream(self):
        row = self.conn.execute(
            "SELECT chosen_provider, chosen_logical_service, chosen_playable_id, chosen_deeplink "
            "FROM lane_events WHERE is_placeholder=0"
        ).fetchone()
        self.assertEqual(row["chosen_provider"], "xtream")
        self.assertEqual(row["chosen_logical_service"], "xtream")
        self.assertTrue(row["chosen_playable_id"])
        self.assertIsNone(row["chosen_deeplink"])

        with patch.dict(os.environ, self.tune_env, clear=False):
            playable = get_lane_direct_stream(
                self.conn, 1, self.now.isoformat(timespec="seconds")
            )
        self.assertEqual(playable["provider"], "xtream")
        self.assertEqual(
            playable["stream_url"],
            "http://provider.example:8080/live/demo%20user/secret%2Fpass/500.ts",
        )

    def test_m3u_uses_lane_tuning_endpoint_without_credentials(self):
        path = Path(self.tmp.name) / "lanes.m3u"
        build_lanes_m3u(self.conn, str(path), "http://fruit.local:6655")
        content = path.read_text(encoding="utf-8")
        self.assertIn("http://fruit.local:6655/lane/1/stream.m3u8", content)
        self.assertNotIn("demo user", content)
        self.assertNotIn("secret", content)

    def test_xmltv_contains_event_without_authenticated_stream_url(self):
        path = Path(self.tmp.name) / "lanes.xml"
        build_lanes_xmltv(self.conn, str(path))
        content = path.read_text(encoding="utf-8")
        self.assertIn(self.provider_name, content)
        self.assertIn("Xtream IPTV", content)
        self.assertNotIn("demo user", content)
        self.assertNotIn("secret", content)
        self.assertNotIn("/live/", content)

    def test_flask_lane_endpoint_redirects_selected_xtream_stream(self):
        env = dict(self.tune_env)
        env["FRUIT_DB_PATH"] = str(self.db_path)
        with patch.dict(os.environ, env, clear=False):
            app = create_app()
            client = app.test_client()
            response = client.get(
                "/lane/1/stream.m3u8",
                query_string={"at": self.now.isoformat(timespec="seconds")},
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.headers["Location"],
            "http://provider.example:8080/live/demo%20user/secret%2Fpass/500.ts",
        )
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_live_shaped_name_flows_through_complete_lane_pipeline(self):
        event = self.conn.execute(
            "SELECT title, start_utc FROM events WHERE title=?", (self.provider_name,)
        ).fetchone()
        self.assertIsNotNone(event)
        self.assertEqual(
            datetime.fromisoformat(event["start_utc"].replace("Z", "+00:00")),
            self.expected_start,
        )
        lane = self.conn.execute(
            "SELECT chosen_provider FROM lane_events WHERE is_placeholder=0"
        ).fetchone()
        self.assertEqual(lane["chosen_provider"], "xtream")

        xml_path = Path(self.tmp.name) / "live-shaped.xml"
        m3u_path = Path(self.tmp.name) / "live-shaped.m3u"
        build_lanes_xmltv(self.conn, str(xml_path))
        build_lanes_m3u(self.conn, str(m3u_path), "http://fruit.local:6655")
        self.assertIn(self.provider_name, xml_path.read_text(encoding="utf-8"))
        self.assertIn(
            "http://fruit.local:6655/lane/1/stream.m3u8",
            m3u_path.read_text(encoding="utf-8"),
        )

        env = dict(self.tune_env)
        env["FRUIT_DB_PATH"] = str(self.db_path)
        with patch.dict(os.environ, env, clear=False):
            response = create_app().test_client().get(
                "/lane/1/stream.m3u8",
                query_string={"at": self.now.isoformat(timespec="seconds")},
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.headers["Location"],
            "http://provider.example:8080/live/demo%20user/secret%2Fpass/500.ts",
        )


if __name__ == "__main__":
    unittest.main()
