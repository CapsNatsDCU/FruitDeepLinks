import json
import sqlite3
import sys
import unittest
from dataclasses import replace
from unittest.mock import patch
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from sports_metadata import ensure_schema, resolve_source_event
from sports_schedule_audit import (DEFAULT_ENABLED_KEYS, LEAGUE_BY_KEY, ensure_schema as ensure_audit_schema,
                                   enabled_keys, parse_espn_events, parse_serpapi_events, refresh,
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

    def test_structured_serpapi_parser_requires_absolute_time(self):
        payload = {"league_results": {"game_groups": [{"title": "Week 4", "games": [
            {"kgmid": "/g/nfl-game", "start_time": "2026-10-04T17:00:00Z", "status": "scheduled",
             "teams": [{"name": "Washington Commanders"}, {"name": "Philadelphia Eagles"}],
             "league": {"name": "NFL"}},
            {"kgmid": "/g/no-time", "teams": [{"name": "Unsafe"}]},
        ]}]}}
        events = parse_serpapi_events(LEAGUE_BY_KEY["nfl"], payload)
        self.assertEqual(1, len(events))
        self.assertEqual("2026-10-04T17:00:00Z", events[0]["start_utc"])
        self.assertEqual(["Washington Commanders", "Philadelphia Eagles"], events[0]["participants"])

    def test_espn_parser_supports_racing_without_creating_participants(self):
        payload = {"events": [{"id": "race-1", "date": "2026-10-04T19:00Z",
                               "name": "NASCAR Cup Series at Talladega",
                               "status": {"type": {"state": "pre"}}}]}
        events = parse_espn_events(LEAGUE_BY_KEY["nascar-cup"], payload)
        self.assertEqual("race-1", events[0]["id"])
        self.assertEqual([], events[0]["participants"])

    def test_refresh_uses_free_feed_without_serpapi_key_and_tracks_no_search(self):
        save_enabled_keys(self.conn, ["formula-1", "ufl"])
        calls = []

        def getter(url, **kwargs):
            calls.append((url, kwargs))
            return Response({"events": [{"id": "one", "date": "2026-10-04T19:00Z",
                                         "name": "Expected event", "competitions": []}]})

        result = refresh(self.conn, api_key=None, force=True, now=NOW, getter=getter)
        self.assertEqual("complete", result["status"])
        self.assertEqual((2, 0, 2), (result["checked_leagues"], result["successful_searches"], result["event_count"]))
        self.assertTrue(all("espn.com" in call[0] for call in calls))
        self.assertEqual(2, self.conn.execute("SELECT COUNT(*) FROM sports_schedule_reference_events").fetchone()[0])

    def test_serpapi_key_never_enters_materialized_reference_rows(self):
        save_enabled_keys(self.conn, ["nfl"])
        secret = "private-test-key"

        def getter(_url, **kwargs):
            self.assertEqual(secret, kwargs["params"]["api_key"])
            return Response({"league_results": {"game_groups": [{"games": [{
                "kgmid": "/g/game", "start_time": "2026-10-04T17:00:00Z",
                "teams": [{"name": "Washington Commanders"}, {"name": "Philadelphia Eagles"}],
            }]}]}})

        with patch.dict(LEAGUE_BY_KEY, nfl=replace(LEAGUE_BY_KEY["nfl"], source="serpapi")):
            result = refresh(self.conn, api_key=secret, force=True, now=NOW, getter=getter)
        self.assertEqual(1, result["successful_searches"])
        dump = "\n".join(str(value) for row in self.conn.iterdump() for value in [row])
        self.assertNotIn(secret, dump)

    def test_free_plan_reserve_stops_searches_before_250(self):
        save_enabled_keys(self.conn, ["nfl"])
        self.conn.execute(
            "INSERT INTO sports_schedule_api_usage(period,successful_searches,updated_utc) VALUES('2026-09',225,?)",
            ("2026-09-30T00:00:00Z",),
        )
        self.conn.commit()

        def unexpected_request(*_args, **_kwargs):
            raise AssertionError("budget guard contacted SerpApi")

        with patch.dict(LEAGUE_BY_KEY, nfl=replace(LEAGUE_BY_KEY["nfl"], source="serpapi")):
            result = refresh(self.conn, api_key="configured", force=True, now=NOW, getter=unexpected_request)
        self.assertEqual("budget_reserved", result["details"]["nfl"])
        self.assertEqual(0, result["successful_searches"])

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
        result=refresh(self.conn,api_key=None,force=True,now=NOW,getter=getter)
        self.assertEqual(result['status'],'complete')
        self.assertEqual(result['successful_searches'],0)
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


if __name__ == "__main__":
    unittest.main()
