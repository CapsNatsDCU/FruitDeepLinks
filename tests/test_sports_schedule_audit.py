import json
import sqlite3
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from sports_metadata import ensure_schema, resolve_source_event
from sports_schedule_audit import (DEFAULT_ENABLED_KEYS, LEAGUE_BY_KEY, ensure_schema as ensure_audit_schema,
                                   enabled_keys, parse_espn_events, refresh,
                                   save_enabled_keys, snapshot)


NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


class Response:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


class SportsScheduleAuditTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        ensure_schema(self.conn)
        ensure_audit_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_selected_priority_set_is_default_and_operator_configurable(self):
        self.assertEqual(list(DEFAULT_ENABLED_KEYS), enabled_keys(self.conn))
        self.assertEqual(["nfl", "formula-1"], save_enabled_keys(self.conn, ["formula-1", "nfl"]))
        self.assertEqual(["nfl", "formula-1"], enabled_keys(self.conn))
        with self.assertRaisesRegex(ValueError, "unsupported league"):
            save_enabled_keys(self.conn, ["made-up-league"])

    def test_espn_parser_supports_racing_without_creating_participants(self):
        payload = {"events": [{"id": "race-1", "date": "2026-10-04T19:00Z",
                               "name": "NASCAR Cup Series at Talladega",
                               "status": {"type": {"state": "pre"}}}]}
        events = parse_espn_events(LEAGUE_BY_KEY["nascar-cup"], payload)
        self.assertEqual("race-1", events[0]["id"])
        self.assertEqual([], events[0]["participants"])

    def test_refresh_uses_public_feed_for_selected_leagues(self):
        save_enabled_keys(self.conn, ["formula-1", "ufl"])
        calls = []

        def getter(url, **kwargs):
            calls.append((url, kwargs))
            return Response({"events": [{"id": "one", "date": "2026-10-04T19:00Z",
                                         "name": "Expected event", "competitions": []}]})

        result = refresh(self.conn, force=True, now=NOW, getter=getter)
        self.assertEqual("complete", result["status"])
        self.assertEqual((2, 2), (result["checked_leagues"], result["event_count"]))
        self.assertTrue(all("espn.com" in call[0] for call in calls))
        self.assertEqual(2, self.conn.execute("SELECT COUNT(*) FROM sports_schedule_reference_events").fetchone()[0])

    def test_legacy_source_is_replaced_even_before_next_due_time(self):
        save_enabled_keys(self.conn, ["nfl"])
        self.conn.execute("ALTER TABLE sports_schedule_audit_runs ADD COLUMN successful_searches INTEGER NOT NULL DEFAULT 0")
        self.conn.execute(
            "INSERT INTO sports_schedule_league_state(league_key,source,status,last_attempt_utc) VALUES('nfl','serpapi','key_required',?)",
            ("2026-09-30T11:00:00Z",),
        )
        self.conn.execute(
            "INSERT INTO sports_schedule_audit_runs(started_utc,finished_utc,status) VALUES(?,?,?)",
            ("2026-09-30T11:00:00Z", "2026-09-30T11:00:00Z", "unavailable"),
        )
        self.conn.execute("""
            INSERT INTO sports_schedule_reference_events(
                source,league_key,source_event_id,title,start_utc,first_seen_utc,last_seen_utc
            ) VALUES('serpapi','nfl','old-game','Old game','2026-10-04T17:00:00Z',?,?)
        """, ("2026-09-30T11:00:00Z", "2026-09-30T11:00:00Z"))
        self.conn.commit()
        calls = []
        def getter(url, **kwargs):
            calls.append(url)
            self.assertNotIn("api_key", kwargs["params"])
            return Response({"events": [{"id": "espn-game", "date": "2026-10-04T17:00:00Z",
                                         "name": "Commanders at Eagles"}]})
        result = refresh(self.conn, now=NOW, getter=getter)
        self.assertEqual("complete", result["status"])
        self.assertTrue(calls)
        self.assertTrue(all("espn.com" in url for url in calls))
        state = self.conn.execute("SELECT source,status FROM sports_schedule_league_state WHERE league_key='nfl'").fetchone()
        self.assertEqual(("espn", "ok"), tuple(state))
        active_sources = [row[0] for row in self.conn.execute(
            "SELECT source FROM sports_schedule_reference_events WHERE league_key='nfl' AND active=1")]
        self.assertEqual(["espn"], active_sources)

    def test_default_team_feeds_need_no_key_and_fetch_whole_months(self):
        save_enabled_keys(self.conn, ['nhl','nba'])
        calls=[]
        def getter(url, **kwargs):
            params=kwargs['params']; calls.append((url,params))
            self.assertNotIn('api_key',params)
            self.assertRegex(params['dates'], r'^\d{6}$')
            return Response({'events':[{'id':'game','date':'2026-10-04T19:00Z','name':'Matchup',
                'competitions':[{'competitors':[{'team':{'displayName':'Washington Capitals'}},
                                               {'team':{'displayName':'Pittsburgh Penguins'}}]}]}]})
        result=refresh(self.conn,force=True,now=NOW,getter=getter)
        self.assertEqual(result['status'],'complete')
        self.assertEqual(result['event_count'],2)
        self.assertEqual([p['dates'] for _,p in calls],['202609','202610','202609','202610'])
        self.assertTrue(all('espn.com' in url for url,_ in calls))

    def test_failed_month_retains_whole_previous_snapshot(self):
        save_enabled_keys(self.conn,['nhl'])
        def successful(*args,**kwargs):
            return Response({'events':[{'id':'old','date':'2026-10-04T19:00Z','name':'Old'}]})
        refresh(self.conn,force=True,now=NOW,getter=successful)
        for body,status in [({},200),({'events':[{}]*1000},200),({},500)]:
            def fails_later(url,**kwargs):
                return Response({'events':[{'id':'new','date':'2026-10-05T19:00Z','name':'New'}]}) if kwargs['params']['dates']=='202609' else Response(body,status)
            result=refresh(self.conn,force=True,now=NOW,getter=fails_later)
            self.assertEqual(result['status'],'unavailable')
            self.assertEqual([r[0] for r in self.conn.execute('SELECT source_event_id FROM sports_schedule_reference_events WHERE active=1')],['old'])

    def test_college_public_feed_cap_does_not_claim_complete_schedule(self):
        save_enabled_keys(self.conn,['ncaa-fbs'])
        result=refresh(self.conn,force=True,now=NOW,getter=lambda *a,**kw:Response({'events':[{}]*25}))
        self.assertEqual(result['status'],'unavailable')
        self.assertEqual(result['details']['ncaa-fbs'],'response_limit_reached')

    def test_snapshot_reports_missing_playable_and_scheduled_without_authorizing_events(self):
        save_enabled_keys(self.conn, ["nfl"])
        spec = LEAGUE_BY_KEY["nfl"]
        now_text = "2026-09-30T12:00:00Z"
        rows = [
            ("serpapi", spec.key, "external-known", "Commanders at Eagles", "2026-10-04T17:00:00Z",
             json.dumps(["Washington Commanders", "Philadelphia Eagles"])),
            ("serpapi", spec.key, "external-missing", "Giants at Cowboys", "2026-10-05T00:00:00Z",
             json.dumps(["New York Giants", "Dallas Cowboys"])),
        ]
        self.conn.executemany("""
          INSERT INTO sports_schedule_reference_events(
            source,league_key,source_event_id,title,start_utc,participants_json,first_seen_utc,last_seen_utc
          ) VALUES(?,?,?,?,?,?,?,?)
        """, [(*row, now_text, now_text) for row in rows])
        known = resolve_source_event(self.conn, source="apple", source_event_id="apple-known", data={
            "title": "Washington Commanders at Philadelphia Eagles", "sport_name": "American football",
            "league_name": "NFL", "start_utc": "2026-10-04T17:00:00Z",
            "competitors": [{"name": "Washington Commanders"}, {"name": "Philadelphia Eagles"}],
        })
        self.conn.executescript("""
          CREATE TABLE playables(event_id TEXT, playable_id TEXT, provider TEXT, logical_service TEXT, service_name TEXT, priority INTEGER);
          CREATE TABLE lane_events(lane_id INTEGER,event_id TEXT,start_utc TEXT,end_utc TEXT,chosen_playable_id TEXT,chosen_provider TEXT,is_placeholder INTEGER DEFAULT 0);
        """)
        self.conn.execute("INSERT INTO playables(event_id,playable_id,provider) VALUES('apple-known','play','apple')")
        self.conn.execute("INSERT INTO lane_events(lane_id,event_id,is_placeholder) VALUES(1,'apple-known',0)")
        self.conn.commit()
        result = snapshot(self.conn, days=14, now=NOW)
        states = {row["source_event_id"]: row["coverage_state"] for row in result["events"]}
        self.assertEqual({"external-known": "scheduled", "external-missing": "missing_from_ingestion"}, states)
        self.assertEqual(1, self.conn.execute("SELECT COUNT(*) FROM canonical_events").fetchone()[0])
        self.assertEqual(known["canonical_event_id"], next(row["canonical_event_id"] for row in result["events"] if row["source_event_id"] == "external-known"))

    def test_snapshot_prefers_final_lane_over_unplaced_canonical_duplicate(self):
        now_text = "2026-09-30T12:00:00Z"
        self.conn.execute("""
          INSERT INTO sports_schedule_reference_events(
            source,league_key,source_event_id,title,start_utc,participants_json,first_seen_utc,last_seen_utc
          ) VALUES(?,?,?,?,?,?,?,?)
        """, ("espn", "nhl", "game", "Winnipeg Jets at Florida Panthers", "2026-10-04T23:00:00Z",
              json.dumps(["Winnipeg Jets", "Florida Panthers"]), now_text, now_text))
        unplaced = resolve_source_event(self.conn, source="xtream", source_event_id="unplaced", data={
            "title": "Winnipeg Jets at Florida Panthers", "sport_name": "Hockey", "league_name": "NHL",
            "start_utc": "2026-10-04T23:00:00Z",
            "competitors": [{"name": "Winnipeg Jets"}, {"name": "Florida Panthers"}],
        })
        unrelated = resolve_source_event(self.conn, source="xtream", source_event_id="other", data={
            "title": "Dallas Stars at Colorado Avalanche", "sport_name": "Hockey", "league_name": "NHL",
            "start_utc": "2026-10-04T22:30:00Z",
            "competitors": [{"name": "Dallas Stars"}, {"name": "Colorado Avalanche"}],
        })
        self.conn.executescript("""
          CREATE TABLE events(id TEXT PRIMARY KEY,title TEXT,start_utc TEXT,channel_name TEXT);
          CREATE TABLE playables(event_id TEXT, playable_id TEXT);
          CREATE TABLE lane_events(lane_id INTEGER,event_id TEXT,is_placeholder INTEGER DEFAULT 0);
        """)
        self.conn.execute("INSERT INTO playables VALUES('unplaced','unplaced-playable')")
        self.conn.execute("INSERT INTO events VALUES(?,?,?,?)", (
            "scheduled-source", "Winnipeg Jets vs Florida Panthers @ Oct 4 6:30 PM :NHL 01",
            "2026-10-04T22:30:00Z", "US| NHL PPV"))
        self.conn.execute("INSERT INTO playables VALUES('scheduled-source','live-playable')")
        self.conn.execute("INSERT INTO lane_events VALUES(60,'scheduled-source',0)")
        self.conn.execute(
            "INSERT INTO source_event_records(source,source_event_id,canonical_event_id,confidence,resolution_kind,last_seen_utc) "
            "VALUES('xtream','scheduled-source',?,1,'source_mapping',?)",
            (unrelated["canonical_event_id"], now_text),
        )
        self.conn.commit()

        result = snapshot(self.conn, days=14, now=NOW)
        item = result["events"][0]
        self.assertEqual("scheduled", item["coverage_state"])
        self.assertEqual("scheduled-source", item["matched_event_id"])
        self.assertEqual("import_title", item["match_kind"])
        self.assertEqual("US| NHL PPV", item["matched_category"])
        self.assertEqual(60, item["lane_id"])
        self.assertIsNone(item["canonical_event_id"])
        self.assertEqual((1, 1, 1), tuple(result["import_horizons"][0][key]
                                          for key in ("events", "with_playable", "days_3_to_7")))

    def test_snapshot_does_not_match_one_team_or_distant_stream(self):
        now_text = "2026-09-30T12:00:00Z"
        self.conn.execute("""
          INSERT INTO sports_schedule_reference_events(
            source,league_key,source_event_id,title,start_utc,participants_json,first_seen_utc,last_seen_utc
          ) VALUES(?,?,?,?,?,?,?,?)
        """, ("espn", "nba", "game", "Chicago Bulls at Boston Celtics", "2026-10-04T23:00:00Z",
              json.dumps(["Chicago Bulls", "Boston Celtics"]), now_text, now_text))
        self.conn.execute("CREATE TABLE events(id TEXT PRIMARY KEY,title TEXT,start_utc TEXT)")
        self.conn.execute("INSERT INTO events VALUES('one-team','Boston Celtics Pregame','2026-10-04T23:00:00Z')")
        self.conn.execute("INSERT INTO events VALUES('pregame','Boston Celtics vs Chicago Bulls Pregame','2026-10-04T23:00:00Z')")
        self.conn.execute("INSERT INTO events VALUES('distant','Boston Celtics vs Chicago Bulls','2026-10-05T03:01:00Z')")
        self.conn.commit()
        self.assertEqual("missing_from_ingestion", snapshot(self.conn, days=14, now=NOW)["events"][0]["coverage_state"])

    def test_snapshot_suggests_short_team_titles_without_counting_them_as_coverage(self):
        now_text = "2026-09-30T12:00:00Z"
        self.conn.execute("""
          INSERT INTO sports_schedule_reference_events(
            source,league_key,source_event_id,title,start_utc,participants_json,first_seen_utc,last_seen_utc
          ) VALUES(?,?,?,?,?,?,?,?)
        """, ("espn", "nhl", "short-names", "Chicago Blackhawks at Boston Bruins",
              "2026-10-01T17:00:00Z", json.dumps(["Chicago Blackhawks", "Boston Bruins"]),
              now_text, now_text))
        self.conn.execute("CREATE TABLE events(id TEXT PRIMARY KEY,title TEXT,start_utc TEXT,channel_name TEXT)")
        self.conn.execute("CREATE TABLE playables(event_id TEXT,playable_id TEXT)")
        self.conn.execute("INSERT INTO events VALUES(?,?,?,?)", (
            "likely", "US (ESPN+ 337) | Hockey: Blackhawks vs. Bruins (Blackhawks Broadcast)",
            "2026-10-01T16:30:00Z", "US| ESPN+ PPV"))
        self.conn.execute("INSERT INTO playables VALUES('likely','stream')")
        self.conn.execute("INSERT INTO events VALUES(?,?,?,?)", (
            "wrong-league", "(Apple) (MLS) 001 | Blackhawks vs. Bruins",
            "2026-10-01T17:00:00Z", "US| MLS PPV"))
        self.conn.commit()
        result = snapshot(self.conn, days=14, now=NOW)
        item = result["events"][0]
        self.assertEqual("missing_from_ingestion", item["coverage_state"])
        self.assertEqual(1, result["possible_import_count"])
        self.assertEqual(["likely"], [candidate["event_id"] for candidate in item["possible_imports"]])

    def test_snapshot_rejects_league_name_under_wrong_sport(self):
        now_text = "2026-09-30T12:00:00Z"
        self.conn.execute("""
          INSERT INTO sports_schedule_reference_events(
            source,league_key,source_event_id,title,start_utc,participants_json,first_seen_utc,last_seen_utc
          ) VALUES(?,?,?,?,?,?,?,?)
        """, ("espn", "nhl", "wrong-parent", "Chicago Blackhawks at Boston Bruins",
              "2026-10-01T17:00:00Z", json.dumps(["Chicago Blackhawks", "Boston Bruins"]),
              now_text, now_text))
        resolved = resolve_source_event(self.conn, source="xtream", source_event_id="wrong-sport", data={
            "title": "Chicago Blackhawks at Boston Bruins", "sport_name": "Soccer",
            "league_name": "NHL", "start_utc": "2026-10-01T17:00:00Z",
            "competitors": [{"name": "Chicago Blackhawks"}, {"name": "Boston Bruins"}],
        })
        self.assertTrue(resolved["resolved"])
        self.assertEqual("missing_from_ingestion", snapshot(self.conn, days=14, now=NOW)["events"][0]["coverage_state"])

    def test_snapshot_rejects_full_title_from_conflicting_category(self):
        now_text = "2026-09-30T12:00:00Z"
        self.conn.execute("""
          INSERT INTO sports_schedule_reference_events(
            source,league_key,source_event_id,title,start_utc,participants_json,first_seen_utc,last_seen_utc
          ) VALUES(?,?,?,?,?,?,?,?)
        """, ("espn", "nhl", "wrong-category", "Chicago Blackhawks at Boston Bruins",
              "2026-10-01T17:00:00Z", json.dumps(["Chicago Blackhawks", "Boston Bruins"]),
              now_text, now_text))
        self.conn.execute("CREATE TABLE events(id TEXT PRIMARY KEY,title TEXT,start_utc TEXT,channel_name TEXT)")
        self.conn.execute("INSERT INTO events VALUES(?,?,?,?)", (
            "wrong-category", "Chicago Blackhawks vs Boston Bruins",
            "2026-10-01T17:00:00Z", "US| MLS PPV"))
        self.assertEqual("missing_from_ingestion", snapshot(self.conn, days=14, now=NOW)["events"][0]["coverage_state"])


if __name__ == "__main__":
    unittest.main()
