import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from fruit_build_lanes import (Event, build_lanes_with_placeholders, create_lanes,
                               ensure_lane_schema, load_future_events)
from fruit_import_appletv import iso_to_ms, ms_to_iso
from sports_metadata import applicable_rule, ensure_schema, resolve_source_event, save_rule
from sports_scheduler import simulate
from xtream_ingest import parse_timestamp


UTC = timezone.utc


class SportsSchedulerHardeningTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        ensure_schema(self.conn)
        ensure_lane_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def _reset_lanes(self, count):
        self.conn.execute("DELETE FROM lane_events")
        self.conn.execute("DELETE FROM lanes")
        create_lanes(self.conn, count)

    def _event(self, identifier, start, end, *, priority=0, rule="NORMAL", canonical=None):
        return Event(identifier, identifier, None, identifier, None, start, end,
                     canonical, priority, rule, (identifier,))

    def _scheduled_ids(self):
        return {row[0] for row in self.conn.execute(
            "SELECT event_id FROM lane_events WHERE COALESCE(is_placeholder, 0)=0"
        )}

    def test_future_always_events_do_not_displace_earlier_non_overlapping_events(self):
        self._reset_lanes(50)
        today = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=2)
        events = [self._event(f"today-{i}", today + timedelta(hours=i), today + timedelta(hours=i + 1)) for i in range(4)]
        next_week = today + timedelta(days=7)
        events.extend(self._event(f"future-{i}", next_week, next_week + timedelta(hours=2),
                                  priority=10000, rule="ALWAYS_SCHEDULE") for i in range(50))
        build_lanes_with_placeholders(self.conn, events, 50)
        self.assertTrue({f"today-{i}" for i in range(4)}.issubset(self._scheduled_ids()))
        self.assertEqual(54, len(self._scheduled_ids()))

    def test_overlap_priority_and_deterministic_tie_breaking(self):
        self._reset_lanes(1)
        start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=2)
        normal = self._event("normal", start, start + timedelta(hours=2))
        prioritized = self._event("prioritized", start, start + timedelta(hours=2), priority=1000, rule="PRIORITIZE")
        always = self._event("always", start, start + timedelta(hours=2), priority=10000, rule="ALWAYS_SCHEDULE")
        build_lanes_with_placeholders(self.conn, [normal, prioritized, always], 1)
        self.assertEqual({"always"}, self._scheduled_ids())

        self._reset_lanes(1)
        alpha = self._event("alpha", start, start + timedelta(hours=2))
        zulu = self._event("zulu", start, start + timedelta(hours=2))
        build_lanes_with_placeholders(self.conn, [zulu, alpha], 1)
        self.assertEqual({"alpha"}, self._scheduled_ids())

    def test_provider_capacity_uses_ranked_alternate_before_drop(self):
        self._reset_lanes(2)
        self.conn.execute("INSERT INTO provider_capacities(provider,max_concurrent,updated_utc) VALUES('xtream',1,'2026-01-01T00:00:00Z')")
        start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=2)
        first = self._event("first", start, start + timedelta(hours=2), canonical="ce-first")
        second = self._event("second", start, start + timedelta(hours=2), canonical="ce-second")
        choices = {
            "first": [{"event_id": "first", "playable_id": "x-1", "provider": "Xtream", "logical_service": "xtream"}],
            "second": [
                {"event_id": "second", "playable_id": "x-2", "provider": "xtream", "logical_service": "xtream"},
                {"event_id": "second", "playable_id": "alt-2", "provider": "other", "logical_service": "other"},
            ],
        }
        with patch("fruit_build_lanes.get_filtered_playables", side_effect=lambda _conn, event_id, *_args, **_kwargs: choices[event_id]):
            build_lanes_with_placeholders(self.conn, [first, second], 2)
        rows = {row["event_id"]: row["chosen_provider"] for row in self.conn.execute(
            "SELECT event_id,chosen_provider FROM lane_events WHERE COALESCE(is_placeholder,0)=0"
        )}
        self.assertEqual({"first", "second"}, set(rows))
        self.assertEqual("other", rows["second"])

    def test_provider_capacity_conflict_is_recorded_distinctly(self):
        self._reset_lanes(1)
        self.conn.execute("INSERT INTO provider_capacities(provider,max_concurrent,updated_utc) VALUES('xtream',1,'2026-01-01T00:00:00Z')")
        start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=2)
        first = self._event("aaa-first", start, start + timedelta(hours=2), canonical="ce-first")
        provider_blocked = self._event("zzz-blocked", start, start + timedelta(hours=2), canonical="ce-blocked")
        choices = lambda _conn, event_id, *_args, **_kwargs: [{"event_id": event_id, "playable_id": event_id, "provider": "xtream"}]
        with patch("fruit_build_lanes.get_filtered_playables", side_effect=choices):
            build_lanes_with_placeholders(self.conn, [first, provider_blocked], 1)
        decision = self.conn.execute("SELECT decision FROM scheduling_decisions WHERE canonical_event_id='ce-blocked'").fetchone()[0]
        self.assertEqual("provider_capacity_conflict", decision)

    def test_lane_capacity_conflict_is_recorded_distinctly(self):
        self._reset_lanes(1)
        start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=2)
        first = self._event("aaa-first", start, start + timedelta(hours=2), canonical="ce-first")
        blocked = self._event("zzz-blocked", start, start + timedelta(hours=2), canonical="ce-blocked")
        build_lanes_with_placeholders(self.conn, [first, blocked], 1)
        decision = self.conn.execute("SELECT decision FROM scheduling_decisions WHERE canonical_event_id='ce-blocked'").fetchone()[0]
        self.assertEqual("lane_capacity_conflict", decision)

    def test_canonical_rows_produce_one_candidate_and_keep_legacy_ids(self):
        start = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=2)
        start_text = start.isoformat().replace("+00:00", "Z")
        end_text = (start + timedelta(hours=2)).isoformat().replace("+00:00", "Z")
        apple = resolve_source_event(self.conn, source="apple", source_event_id="apple-row", data={
            "sport_name": "Hockey", "league_name": "NHL", "start_utc": start_text,
            "competitors": [{"name": "Washington Capitals", "homeAway": "away"}, {"name": "New York Rangers", "homeAway": "home"}],
        })
        xtream = resolve_source_event(self.conn, source="xtream", source_event_id="xtream-row", data={
            "sport_name": "Hockey", "league_name": "NHL", "start_utc": (start + timedelta(minutes=2)).isoformat().replace("+00:00", "Z"),
            "competitors": [{"name": "Washington Capitals", "homeAway": "away"}, {"name": "New York Rangers", "homeAway": "home"}],
        })
        self.assertEqual(apple["canonical_event_id"], xtream["canonical_event_id"])
        self.conn.executescript("""
            CREATE TABLE events (
              id TEXT PRIMARY KEY, pvid TEXT, slug TEXT, title TEXT, channel_name TEXT,
              start_utc TEXT, end_utc TEXT, raw_attributes_json TEXT, genres_json TEXT,
              classification_json TEXT
            );
        """)
        for row_id in ("apple-row", "xtream-row"):
            self.conn.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?)", (
                row_id, row_id, None, "Capitals at Rangers", None, start_text, end_text, "{}", "[]", "[]"
            ))
        candidates = load_future_events(self.conn, 2)
        self.assertEqual(1, len(candidates))
        self.assertEqual({"apple-row", "xtream-row"}, set(candidates[0].legacy_event_ids))
        self._reset_lanes(2)
        build_lanes_with_placeholders(self.conn, candidates, 2)
        self.assertEqual(1, len(self._scheduled_ids()))
        live_count = self.conn.execute("SELECT COUNT(*) FROM lane_events").fetchone()[0]
        dry_run = simulate(self.conn, 2, 2)
        self.assertEqual("fruit_build_lanes.build_lanes_with_placeholders", dry_run["uses"])
        self.assertEqual(live_count, self.conn.execute("SELECT COUNT(*) FROM lane_events").fetchone()[0])

    def test_simultaneous_nfl_resolves_by_full_participant_identity(self):
        start = "2026-10-11T17:00:00Z"
        teams = [("Washington Commanders", "Philadelphia Eagles")] + [(f"Away {i}", f"Home {i}") for i in range(7)]
        ids = []
        for index, (away, home) in enumerate(teams):
            result = resolve_source_event(self.conn, source="apple", source_event_id=f"apple-{index}", data={
                "sport_name": "Football", "league_name": "NFL", "start_utc": start, "event_type": "regular",
                "competitors": [{"name": away, "homeAway": "away"}, {"name": home, "homeAway": "home"}],
            })
            ids.append(result["canonical_event_id"])
        source = resolve_source_event(self.conn, source="espn", source_event_id="commanders-eagles", data={
            "sport_name": "Football", "league_name": "NFL", "start_utc": "2026-10-11T17:04:00Z", "event_type": "regular",
            "competitors": [{"name": "Washington Commanders", "homeAway": "away"}, {"name": "Philadelphia Eagles", "homeAway": "home"}],
        })
        self.assertEqual(ids[0], source["canonical_event_id"])
        self.assertEqual(8, self.conn.execute("SELECT COUNT(*) FROM canonical_events").fetchone()[0])

    def test_rule_precedence_league_beats_competition_and_event_beats_team(self):
        result = resolve_source_event(self.conn, source="apple", source_event_id="rules", data={
            "sport_name": "Soccer", "league_name": "MLS", "competition": "Cup", "start_utc": "2026-10-11T17:00:00Z",
            "competitors": [{"name": "D.C. United", "homeAway": "home"}, {"name": "FC Cincinnati", "homeAway": "away"}],
        })
        event = self.conn.execute("SELECT * FROM canonical_events WHERE id=?", (result["canonical_event_id"],)).fetchone()
        team_id = self.conn.execute("SELECT team_id FROM canonical_event_participants WHERE event_id=? LIMIT 1", (event["id"],)).fetchone()[0]
        save_rule(self.conn, target_type="competition", target_id="Cup", policy="NORMAL")
        save_rule(self.conn, target_type="league", target_id=event["league_id"], policy="PRIORITIZE")
        self.assertEqual("PRIORITIZE", applicable_rule(self.conn, event["id"])["policy"])
        save_rule(self.conn, target_type="team", target_id=team_id, policy="ALWAYS_SCHEDULE")
        save_rule(self.conn, target_type="event", target_id=event["id"], policy="IGNORE")
        self.assertEqual("IGNORE", applicable_rule(self.conn, event["id"])["policy"])

    def test_apple_and_xtream_real_timestamp_parsers_preserve_absolute_instants(self):
        self.assertEqual("2026-03-08T07:30:00Z", ms_to_iso(iso_to_ms("2026-03-08T03:30:00-04:00")))
        self.assertEqual("2026-11-01T06:30:00Z", ms_to_iso(iso_to_ms("2026-11-01T01:30:00-05:00")))
        self.assertIsNone(iso_to_ms("2026-09-13T20:25:00"))
        self.assertEqual(datetime(2026, 9, 13, 20, 25, tzinfo=UTC), parse_timestamp("2026-09-13T20:25:00Z", "America/New_York"))
        self.assertEqual(datetime(2026, 9, 13, 20, 25, tzinfo=UTC), parse_timestamp("2026-09-13T16:25:00-04:00", "America/New_York"))
        epoch = int(datetime(2026, 9, 13, 20, 25, tzinfo=UTC).timestamp())
        self.assertEqual(datetime(2026, 9, 13, 20, 25, tzinfo=UTC), parse_timestamp(epoch, "America/New_York"))
        self.assertEqual(datetime(2026, 9, 13, 20, 25, tzinfo=UTC), parse_timestamp(epoch * 1000, "America/New_York"))


if __name__ == "__main__":
    unittest.main()
