import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))

from fruit_build_lanes import (  # noqa: E402
    build_lanes_with_placeholders,
    create_lanes,
    ensure_lane_schema,
    load_future_events,
)
from fruit_export_lanes import build_lanes_m3u, build_lanes_xmltv  # noqa: E402
from server.app import create_app  # noqa: E402
from xtream_ingest import (  # noqa: E402
    XtreamConfig,
    ingest_payload,
    is_placeholder_stream_name,
    normalize_motorsport_session,
    normalize_stream,
    parse_motorsport_stream,
    stable_event_id,
)


FIXTURE = json.loads(
    (ROOT / "tests" / "fixtures" / "xtream_f1_ppv.json").read_text(encoding="utf-8")
)
CATEGORY = FIXTURE["category"]
REFERENCE_NOW = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)


def config(*, window=120):
    return XtreamConfig(
        enabled=True,
        server_url="http://provider.example:8080",
        username="demo user",
        password="secret/pass",
        category_ids=("2253",),
        timezone_name="America/New_York",
        default_duration_minutes=180,
        event_window_days=window,
    )


class XtreamMotorsportParsingTest(unittest.TestCase):
    def parse(self, name, *, now=REFERENCE_NOW, window=120, timezone_name="America/New_York"):
        return parse_motorsport_stream(
            name,
            CATEGORY["category_name"],
            timezone_name,
            now=now,
            event_window_days=window,
        )

    def test_italy_race_has_clean_title_timezone_year_and_metadata(self):
        stream = FIXTURE["streams"][0]
        parsed = self.parse(stream["name"])
        self.assertEqual("F1 - Italy - Race", parsed["display_title"])
        self.assertEqual(datetime(2026, 9, 6, 11, 50, tzinfo=timezone.utc), parsed["start"])
        self.assertEqual("Formula 1", parsed["series"])
        self.assertEqual("Race", parsed["session_type"])
        self.assertEqual("EDT", parsed["timezone_abbreviation"])
        self.assertEqual("name_abbreviation", parsed["timezone_source"])

        normalized = normalize_stream(
            stream, "2253", CATEGORY["category_name"], config(), now=REFERENCE_NOW
        )
        self.assertEqual("F1 - Italy - Race", normalized["event"]["title"])
        self.assertNotIn("8K EXCLUSIVE", normalized["event"]["title"])
        self.assertEqual(4 * 60 * 60, normalized["event"]["runtime_secs"])
        metadata = json.loads(normalized["event"]["raw_attributes_json"])
        self.assertEqual("motorsports", metadata["sport"])
        self.assertEqual("Formula 1", metadata["series"])
        self.assertEqual("Italy", metadata["location"])
        self.assertEqual("Race", metadata["session_type"])
        classifications = json.loads(normalized["event"]["classification_json"])
        self.assertIn({"type": "sport", "value": "Motorsports"}, classifications)
        self.assertIn({"type": "league", "value": "Formula 1"}, classifications)

    def test_singapore_sprint_is_normalized_with_two_hour_duration(self):
        stream = FIXTURE["streams"][2]
        normalized = normalize_stream(
            stream, "2253", CATEGORY["category_name"], config(), now=REFERENCE_NOW
        )
        self.assertEqual("F1 - Singapore - Sprint", normalized["event"]["title"])
        self.assertEqual(2 * 60 * 60, normalized["event"]["runtime_secs"])
        metadata = json.loads(normalized["playable"]["stream_metadata_json"])
        self.assertEqual("Sprint", metadata["session_type"])

    def test_est_multiword_and_night_race_times(self):
        cases = {
            "NEXT | MEXICO: RACE | Sun 01 Nov 13:50 EST (US) | 8K EXCLUSIVE | US: APPLE TV F1 PPV 8": (
                "Mexico", datetime(2026, 11, 1, 18, 50, tzinfo=timezone.utc)
            ),
            "NEXT | UNITED STATES: RACE | Sun 25 Oct 14:50 EDT (US) | 8K EXCLUSIVE | US: APPLE TV F1 PPV 7": (
                "United States", datetime(2026, 10, 25, 18, 50, tzinfo=timezone.utc)
            ),
            "NEXT | LAS VEGAS: RACE | Sat 21 Nov 21:50 EST (US) | 8K EXCLUSIVE | US: APPLE TV F1 PPV 10": (
                "Las Vegas", datetime(2026, 11, 22, 2, 50, tzinfo=timezone.utc)
            ),
        }
        for name, (location, start) in cases.items():
            with self.subTest(location=location):
                parsed = self.parse(name)
                self.assertEqual(location, parsed["location"])
                self.assertEqual(start, parsed["start"])

    def test_unknown_timezone_abbreviation_falls_back_safely(self):
        parsed = self.parse(
            "NEXT | ITALY: RACE | Sun 06 Sep 07:50 XYZ (US) | US: APPLE TV F1 PPV 1"
        )
        self.assertEqual(datetime(2026, 9, 6, 11, 50, tzinfo=timezone.utc), parsed["start"])
        self.assertEqual("configured_timezone_fallback", parsed["timezone_source"])

    def test_placeholder_variations_are_exact_and_count_separately(self):
        for label in (
            "- NO EVENT STREAMING -", "NO EVENT", "NO EVENT STREAMING",
            "OFFLINE", "NO STREAM", "TBA",
        ):
            with self.subTest(label=label):
                self.assertTrue(is_placeholder_stream_name(
                    f"{label} | 8K EXCLUSIVE | US: APPLE TV F1 PPV 11"
                ))
        self.assertFalse(is_placeholder_stream_name(
            "NEXT | TBA GRAND PRIX: RACE | Sun 06 Sep 07:50 EDT (US) | F1"
        ))

        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        result = ingest_payload(
            conn, [CATEGORY], {"2253": [FIXTURE["streams"][3]]}, config(), now=REFERENCE_NOW
        )
        self.assertEqual(1, result["observed_upstream"])
        self.assertEqual(1, result["skipped_placeholder"])
        self.assertEqual(0, result["skipped_unparseable"])
        self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM playables").fetchone()[0])

    def test_lookahead_filter_and_new_year_inference(self):
        spain = FIXTURE["streams"][1]
        self.assertIsNone(normalize_stream(
            spain, "2253", CATEGORY["category_name"], config(window=7), now=REFERENCE_NOW
        ))
        new_year = self.parse(
            "NEXT | AUSTRALIA: RACE | Fri 01 Jan 01:00 EST (US) | US: APPLE TV F1 PPV 1",
            now=datetime(2026, 12, 31, 23, 30, tzinfo=timezone.utc),
            window=7,
        )
        self.assertEqual(datetime(2027, 1, 1, 6, 0, tzinfo=timezone.utc), new_year["start"])

    def test_session_aliases(self):
        expected = {
            "FP1": "Practice 1", "FP2": "Practice 2", "FP3": "Practice 3",
            "PRACTICE": "Practice", "PRACTICE 1": "Practice 1",
            "QUALIFY": "Qualifying", "QUALIFYING": "Qualifying",
            "SPRINT": "Sprint", "RACE": "Race",
            "SPRINT QUALIFYING": "Sprint Qualifying",
            "SPRINT SHOOTOUT": "Sprint Shootout",
        }
        for raw, normalized in expected.items():
            with self.subTest(session=raw):
                self.assertEqual(normalized, normalize_motorsport_session(raw))

    def test_non_f1_category_is_not_assumed_to_match_observed_format(self):
        name = "NEXT | ITALY: RACE | Sun 06 Sep 07:50 EDT (US) | FLO RACING PPV"
        self.assertIsNone(parse_motorsport_stream(
            name, "US| FLO RACING PPV", now=REFERENCE_NOW, event_window_days=120
        ))


class FixedLaneDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return REFERENCE_NOW.astimezone(tz) if tz else REFERENCE_NOW.replace(tzinfo=None)


class XtreamF1LanePipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "f1.db"
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.result = ingest_payload(
            self.conn,
            [CATEGORY],
            {"2253": FIXTURE["streams"]},
            config(window=60),
            now=REFERENCE_NOW,
        )
        ensure_lane_schema(self.conn)
        create_lanes(self.conn, 2)
        with patch("fruit_build_lanes.datetime", FixedLaneDateTime):
            events = load_future_events(self.conn, 60)
            build_lanes_with_placeholders(self.conn, events, 2)

    @property
    def tune_env(self):
        return {
            "FRUIT_DB_PATH": str(self.db_path),
            "XTREAM_ENABLED": "true",
            "XTREAM_SERVER_URL": "http://provider.example:8080",
            "XTREAM_USERNAME": "demo user",
            "XTREAM_PASSWORD": "secret/pass",
            "XTREAM_CATEGORY_IDS": "2253",
            "XTREAM_TIMEZONE": "America/New_York",
        }

    def test_real_response_flows_to_database_lanes_exports_and_tune(self):
        self.assertEqual(4, self.result["observed_upstream"])
        self.assertEqual(3, self.result["normalized"])
        self.assertEqual(1, self.result["skipped_placeholder"])
        self.assertEqual(0, self.result["skipped_unparseable"])

        titles = {
            row[0] for row in self.conn.execute("SELECT title FROM events").fetchall()
        }
        self.assertEqual(
            {"F1 - Italy - Race", "F1 - Spain - Race", "F1 - Singapore - Sprint"},
            titles,
        )
        self.assertFalse(any("NO EVENT" in title for title in titles))

        xml_path = Path(self.tmp.name) / "f1.xml"
        m3u_path = Path(self.tmp.name) / "f1.m3u"
        build_lanes_xmltv(self.conn, str(xml_path))
        build_lanes_m3u(self.conn, str(m3u_path), "http://fruit.local:6655")
        xml = xml_path.read_text(encoding="utf-8")
        m3u = m3u_path.read_text(encoding="utf-8")
        self.assertIn("F1 - Italy - Race", xml)
        self.assertIn("F1 - Singapore - Sprint", xml)
        for output in (xml, m3u):
            self.assertNotIn("NO EVENT STREAMING", output)
            self.assertNotIn("demo user", output)
            self.assertNotIn("secret/pass", output)
            self.assertNotIn("/live/", output)

        italy_id = stable_event_id("2253", "3001")
        lane = self.conn.execute(
            "SELECT lane_id FROM lane_events WHERE event_id=?", (italy_id,)
        ).fetchone()["lane_id"]
        with patch.dict(os.environ, self.tune_env, clear=False):
            response = create_app().test_client().get(
                f"/lane/{lane}/stream.m3u8", query_string={"at": "2026-09-06T12:00:00Z"}
            )
        self.assertEqual(302, response.status_code)
        self.assertEqual(
            "http://provider.example:8080/live/demo%20user/secret%2Fpass/3001.ts",
            response.headers["Location"],
        )


if __name__ == "__main__":
    unittest.main()
