import sqlite3
import sys
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from fruit_export_lanes import build_lanes_xmltv
from sports_metadata import (applicable_rule, coverage, ensure_schema, resolve_source_event,
                             save_rule, utc_instant, utc_text)


class SportsMetadataTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def event(self, source, source_id, *, team_a="Washington Capitals", team_b="New York Rangers", start="2026-09-13T20:25:00Z", league="NHL", sport="Hockey", event_type="regular"):
        return resolve_source_event(self.conn, source=source, source_event_id=source_id, data={
            "title": f"{team_a} at {team_b}", "sport_name": sport, "league_name": league,
            "start_utc": start, "event_type": event_type,
            "competitors": [{"name": team_a, "homeAway": "away", "id": "away"}, {"name": team_b, "homeAway": "home", "id": "home"}],
        })

    def test_structured_cross_source_association_and_false_positive_safety(self):
        apple = self.event("apple", "apple-1")
        espn = self.event("espn", "espn-42", start="2026-09-13T20:30:00Z")
        self.assertTrue(apple["resolved"])
        self.assertEqual(apple["canonical_event_id"], espn["canonical_event_id"])
        other = self.event("apple", "apple-2", team_a="Virden Oil Capitals", team_b="Yorkton Terriers")
        self.assertNotEqual(apple["canonical_event_id"], other["canonical_event_id"])
        dc_a = self.event("apple", "dc-a", team_a="D.C. United", team_b="FC Cincinnati", league="MLS", sport="Soccer")
        dc_b = self.event("espn", "dc-b", team_a="DC United", team_b="FC Cincinnati", league="MLS", sport="Soccer", start="2026-09-13T20:30:00Z")
        self.assertEqual(dc_a["canonical_event_id"], dc_b["canonical_event_id"])

    def test_formula_one_has_no_forced_home_away(self):
        result = resolve_source_event(self.conn, source="apple", source_event_id="f1-race", data={
            "title": "Italian Grand Prix", "sport_name": "Motorsports", "league_name": "Formula 1",
            "event_type": "race", "start_utc": "2026-09-06T13:00:00Z", "competitors": [],
        })
        self.assertTrue(result["resolved"])
        participants = self.conn.execute("SELECT * FROM canonical_event_participants WHERE event_id=?", (result["canonical_event_id"],)).fetchall()
        self.assertEqual([], participants)

    def test_rules_are_specific_and_coverage_starts_from_canonical_events(self):
        result = self.event("apple", "wanted")
        event = self.conn.execute("SELECT * FROM canonical_events WHERE id=?", (result["canonical_event_id"],)).fetchone()
        team_id = self.conn.execute("SELECT team_id FROM canonical_event_participants WHERE event_id=? LIMIT 1", (event["id"],)).fetchone()[0]
        save_rule(self.conn, target_type="league", target_id=event["league_id"], policy="PRIORITIZE")
        save_rule(self.conn, target_type="team", target_id=team_id, policy="ALWAYS_SCHEDULE")
        rule = applicable_rule(self.conn, event["id"])
        self.assertEqual("ALWAYS_SCHEDULE", rule["policy"])
        self.assertEqual(10000, rule["priority"])
        self.assertEqual("awaiting_source", coverage(self.conn, days=90)[0]["coverage_state"])
        save_rule(self.conn, target_type="event", target_id=event["id"], policy="IGNORE")
        self.assertEqual("IGNORE", applicable_rule(self.conn, event["id"])["policy"])

    def test_utc_contract_handles_epoch_offsets_dst_and_rejects_naive(self):
        self.assertEqual("2026-09-13T20:25:00Z", utc_text("2026-09-13T20:25:00Z"))
        self.assertEqual("2026-09-13T20:25:00Z", utc_text("2026-09-13T16:25:00-04:00"))
        self.assertEqual("2026-03-08T07:30:00Z", utc_text("2026-03-08T03:30:00", source_timezone="America/New_York"))
        self.assertEqual("2026-11-01T05:30:00Z", utc_text("2026-11-01T01:30:00-04:00"))
        self.assertIsNone(utc_instant("2026-09-13T20:25:00"))
        instant = datetime(2026, 9, 13, 20, 25, tzinfo=timezone.utc)
        self.assertEqual(instant, utc_instant(int(instant.timestamp())))
        self.assertEqual(instant, utc_instant(int(instant.timestamp() * 1000)))

    def test_xmltv_round_trip_preserves_absolute_utc_instant(self):
        self.conn.executescript("""
          CREATE TABLE lanes (lane_id INTEGER PRIMARY KEY, name TEXT, logical_number INTEGER);
          CREATE TABLE lane_events (lane_id INTEGER, event_id TEXT, is_placeholder INTEGER, start_utc TEXT, end_utc TEXT, title TEXT);
          CREATE TABLE events (id TEXT PRIMARY KEY, title TEXT, synopsis TEXT, channel_name TEXT, genres_json TEXT, classification_json TEXT, pvid TEXT, hero_image_url TEXT);
          CREATE TABLE event_images (event_id TEXT, img_type TEXT, url TEXT);
        """)
        self.conn.execute("INSERT INTO lanes VALUES(1,'Fruit Lane 1',9000)")
        self.conn.execute("INSERT INTO events VALUES('e','Race','', '', '[]', '[]', '', '')")
        self.conn.execute("INSERT INTO lane_events VALUES(1,'e',0,'2026-11-01T05:30:00Z','2026-11-01T07:30:00Z','Race')")
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "guide.xml"
            build_lanes_xmltv(self.conn, str(path))
            programme = ET.parse(path).find("programme")
            parsed = datetime.strptime(programme.attrib["start"], "%Y%m%d%H%M%S %z").astimezone(timezone.utc)
        self.assertEqual(datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc), parsed)


if __name__ == "__main__":
    unittest.main()
