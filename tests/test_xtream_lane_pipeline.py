import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

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
        cfg = XtreamConfig(
            enabled=True,
            server_url="http://provider.example:8080",
            username="demo user",
            password="secret/pass",
            category_ids=("10",),
            timezone_name="UTC",
            default_duration_minutes=120,
        )
        stream = {
            "stream_id": "500",
            "name": "NHL | Capitals @ Lightning",
            "start_timestamp": int((self.now - timedelta(minutes=5)).timestamp()),
            "end_timestamp": int((self.now + timedelta(minutes=55)).timestamp()),
            "stream_icon": "https://img.example/game.png",
            "epg_channel_id": "nhl.game",
            "container_extension": "ts",
        }
        ingest_payload(
            self.conn,
            [{"category_id": "10", "category_name": "NHL PPV"}],
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
            "XTREAM_TIMEZONE": "UTC",
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
        self.assertIn("NHL | Capitals @ Lightning", content)
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


if __name__ == "__main__":
    unittest.main()
