import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))

import daily_refresh  # noqa: E402
from fruit_build_lanes import (  # noqa: E402
    build_lanes_with_placeholders,
    create_lanes,
    ensure_lane_schema,
    load_future_events,
)
from fruit_export_lanes import build_lanes_xmltv  # noqa: E402
from fruit_import_appletv import filter_disabled_source_playables  # noqa: E402
from xtream_ingest import XtreamConfig, ingest_payload  # noqa: E402


STANDARD_SETTINGS = (
    "kayo_enabled",
    "fanatiz_enabled",
    "bein_enabled",
    "nesn_enabled",
    "victory_enabled",
    "gotham_enabled",
    "espn_enabled",
)


class XtreamOnlyRefreshTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data_dir = self.root / "data"
        self.out_dir = self.root / "out"
        self.data_dir.mkdir()
        self.out_dir.mkdir()
        self.db_path = self.data_dir / "fruit_events.db"
        self.xml_path = self.out_dir / "multisource_lanes.xml"
        self.commands = []

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE user_preferences (key TEXT PRIMARY KEY, value TEXT, updated_utc TEXT)"
        )
        settings = {key: False for key in STANDARD_SETTINGS}
        settings.update({
            "xtream_enabled": True,
            "xtream_category_ids": "597,1185,1021,1016,2303,2304,2253,1926",
            "num_lanes": 1,
            "days_ahead": 7,
            "server_url": "http://fruit.test:6655",
        })
        for key, value in settings.items():
            conn.execute(
                "INSERT INTO user_preferences(key, value) VALUES (?, ?)",
                (f"setting:{key}", json.dumps(value)),
            )
        conn.commit()
        conn.close()

        # Cached artifacts reproduce the original bug: their mere existence
        # previously caused imports/enrichment even though every toggle was off.
        for filename in (
            "kayo_raw.json",
            "fanatiz_raw.json",
            "bein_snapshot.json",
            "nesn_raw.json",
        ):
            (self.out_dir / filename).write_text('{"events": []}', encoding="utf-8")
        sqlite3.connect(self.data_dir / "apple_events.db").close()
        sqlite3.connect(self.data_dir / "espn_graph.db").close()

    def _run_step(self, _step, _total, _description, command, allow_fail=False, env=None):
        del allow_fail, env
        command = list(command)
        self.commands.append(command)
        scripts = {Path(part).name for part in command if str(part).endswith(".py")}

        if "xtream_ingest.py" in scripts:
            now = datetime.now(timezone.utc).replace(microsecond=0)
            cfg = XtreamConfig(
                enabled=True,
                server_url="http://provider.example:8080",
                username="private-user",
                password="private-password",
                category_ids=("597",),
                timezone_name="UTC",
                default_duration_minutes=180,
                event_window_days=7,
            )
            stream = {
                "stream_id": "9001",
                "name": "US (WNBA 03) | Golden State Valkyries at Portland Fire",
                "start_timestamp": int((now + timedelta(hours=1)).timestamp()),
                "end_timestamp": int((now + timedelta(hours=3)).timestamp()),
                "container_extension": "ts",
            }
            conn = sqlite3.connect(self.db_path)
            try:
                ingest_payload(
                    conn,
                    [{"category_id": "597", "category_name": "WNBA"}],
                    {"597": [stream]},
                    cfg,
                    now=now,
                )
            finally:
                conn.close()

        if "fruit_build_lanes.py" in scripts:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            try:
                ensure_lane_schema(conn)
                create_lanes(conn, 1)
                events = load_future_events(conn, 7)
                build_lanes_with_placeholders(conn, events, 1)
            finally:
                conn.close()

        if "fruit_export_lanes.py" in scripts:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            try:
                build_lanes_xmltv(conn, str(self.xml_path))
            finally:
                conn.close()
        return True

    def test_full_refresh_uses_saved_plan_and_produces_only_xtream_lanes(self):
        env = {
            # Opposite Docker/env values prove saved UI choices remain authoritative.
            "KAYO_ENABLED": "true",
            "FANATIZ_ENABLED": "true",
            "BEIN_ENABLED": "true",
            "NESN_ENABLED": "true",
            "VICTORY_ENABLED": "true",
            "GOTHAM_ENABLED": "true",
            "ESPN_ENABLED": "true",
            "XTREAM_ENABLED": "false",
            "XTREAM_USERNAME": "private-user",
            "XTREAM_PASSWORD": "private-password",
            "DB_MAINTENANCE": "false",
            "APPLE_AUTH_BOOTSTRAP": "true",
        }
        output = io.StringIO()
        with patch.dict(os.environ, env, clear=False), patch.multiple(
            daily_refresh,
            DATA_DIR=self.data_dir,
            OUT_DIR=self.out_dir,
            DB_PATH=self.db_path,
            APPLE_DB_PATH=self.data_dir / "apple_events.db",
            APPLE_AUTH_PATH=self.data_dir / "apple_uts_auth.json",
            APPLE_IMPORT_STAMP_PATH=self.data_dir / ".apple_import_stamp.json",
            run_step=self._run_step,
        ), redirect_stdout(output):
            self.assertEqual(0, daily_refresh.main([]))

        invoked = {
            Path(part).name
            for command in self.commands
            for part in command
            if str(part).endswith(".py")
        }
        self.assertIn("xtream_ingest.py", invoked)
        self.assertIn("fruit_build_lanes.py", invoked)
        self.assertIn("fruit_export_lanes.py", invoked)
        for disabled_script in (
            "multi_scraper.py",
            "apple_scraper_db.py",
            "fruit_import_appletv.py",
            "kayo_scrape.py",
            "ingest_kayo.py",
            "fanatiz_scrape.py",
            "ingest_fanatiz.py",
            "bein_scrape.py",
            "bein_import.py",
            "nesn_scrape.py",
            "ingest_nesn.py",
            "victory_scraper.py",
            "gotham_integration.py",
            "fruit_ingest_espn_graph.py",
            "fruit_enrich_espn.py",
            "fix_espn_spanish_only.py",
            "amazon2.py",
            "migrate_amazon_logical_services.py",
        ):
            self.assertNotIn(disabled_script, invoked)

        conn = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(
                [("xtream", "xtream")],
                conn.execute(
                    "SELECT DISTINCT provider, logical_service FROM playables"
                ).fetchall(),
            )
            lane = conn.execute(
                "SELECT chosen_provider FROM lane_events WHERE is_placeholder=0"
            ).fetchone()
            self.assertEqual("xtream", lane[0])
        finally:
            conn.close()

        xml = self.xml_path.read_text(encoding="utf-8")
        self.assertIn("Golden State Valkyries at Portland Fire", xml)
        for disabled_title in (
            "GT World Challenge Europe",
            "Chicago White Sox at Minnesota Twins",
            "US Open: Court 10",
            "Amazon - beIN Sports Connect",
            "Amazon - Unavailable (Regional Restriction)",
            "ESPN Unlimited",
        ):
            self.assertNotIn(disabled_title, xml)

        log = output.getvalue()
        self.assertIn("Enabled providers:\nXtream IPTV", log)
        self.assertIn("Disabled providers:\nKayo\nFanatiz\nbeIN\nNESN\nVictory+\nGotham\nESPN", log)
        self.assertNotIn("private-user", log)
        self.assertNotIn("private-password", log)


class AppleImportProviderFilterTest(unittest.TestCase):
    @staticmethod
    def playable(playable_id, provider, service_name, logical_service):
        return (
            "appletv-event",
            playable_id,
            provider,
            service_name,
            logical_service,
            None,
            None,
            None,
            None,
            None,
            0,
            None,
            "2026-08-30T00:00:00Z",
        )

    def test_disabled_espn_and_bein_are_removed_before_apple_upsert(self):
        rows = [
            self.playable("espn", "sportscenter", "ESPN Unlimited", "espn_unlimited"),
            self.playable("bein", "aiv", "beIN Sports Connect", "aiv_bein"),
            self.playable("peacock", "peacock", "Peacock", "peacock"),
        ]
        filtered = filter_disabled_source_playables(rows, {"espn", "bein"})
        self.assertEqual(["peacock"], [row[1] for row in filtered])


if __name__ == "__main__":
    unittest.main()
