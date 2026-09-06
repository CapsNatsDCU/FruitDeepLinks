import sqlite3
import sys
import unittest
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from sports_catalog import (apply_catalog_records, canonicalize_participants,
                            preview_catalog_records, recurring_event_aliases, resolve_team_alias)
from sports_metadata import ensure_schema, resolve_source_event
from update_sports_catalog import openligadb_records, thesportsdb_records, wikidata_records


START = "2026-10-11T17:00:00Z"


class SportsCatalogTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def apply(self, *records):
        result = apply_catalog_records(self.conn, records, dry_run=False)
        self.assertFalse(result["conflicts"])
        self.assertFalse(result["invalid"])
        return result

    @staticmethod
    def capitals_record():
        return {"entity_type": "team", "name": "Washington Capitals", "sport": "Ice hockey", "league": "NHL",
                "aliases": ["Washington Caps", "Capitals", "WSH"], "source": "wikidata",
                "external_ids": {"wikidata": "Q170185"}, "source_url": "https://www.wikidata.org/wiki/Q170185"}

    def test_preview_is_non_mutating_and_apply_preserves_fruit_identity(self):
        record = self.capitals_record()
        preview = preview_catalog_records(self.conn, [record])
        self.assertEqual(1, len(preview["additions"]))
        self.assertEqual(0, self.conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0])
        self.apply(record)
        first = self.conn.execute("SELECT fruit_id FROM catalog_entity_provenance WHERE source='wikidata' AND external_id='Q170185'").fetchone()[0]
        # A renamed upstream item cannot silently move the original Q-ID to a
        # new Fruit entity; it is a conflict and leaves the known ID intact.
        conflict = preview_catalog_records(self.conn, [{**record, "name": "Washington Capitals Hockey Club"}])
        self.assertEqual("conflict", conflict["conflicts"][0]["kind"])
        self.assertEqual(first, self.conn.execute("SELECT fruit_id FROM catalog_entity_provenance WHERE source='wikidata' AND external_id='Q170185'").fetchone()[0])

    def test_catalog_alias_is_scoped_and_generic_mascot_is_not_imported(self):
        self.apply(self.capitals_record())
        sport_id = self.conn.execute("SELECT id FROM sports WHERE name='Ice hockey'").fetchone()[0]
        league_id = self.conn.execute("SELECT id FROM leagues WHERE name='NHL'").fetchone()[0]
        self.assertEqual("Washington Capitals", resolve_team_alias(self.conn, "Washington Caps", sport_id=sport_id, league_id=league_id)["name"])
        self.assertIsNone(resolve_team_alias(self.conn, "Capitals", sport_id=sport_id, league_id=league_id))
        self.assertIsNone(resolve_team_alias(self.conn, "Washington Caps", sport_id=None, league_id=None))

    def test_catalog_alias_contributes_to_real_canonical_event_matching(self):
        self.apply(self.capitals_record())
        apple = resolve_source_event(self.conn, source="apple", source_event_id="apple-caps", data={
            "sport_name": "Ice hockey", "league_name": "NHL", "start_utc": START,
            "competitors": [{"name": "Washington Capitals", "homeAway": "away"}, {"name": "New York Rangers", "homeAway": "home"}],
        })
        xtream = resolve_source_event(self.conn, source="xtream", source_event_id="xtream-caps", data={
            "sport_name": "Ice hockey", "league_name": "NHL", "start_utc": START,
            "competitors": [{"name": "Washington Caps", "homeAway": "away"}, {"name": "New York Rangers", "homeAway": "home"}],
        })
        self.assertEqual(apple["canonical_event_id"], xtream["canonical_event_id"])
        participant = self.conn.execute("SELECT display_name FROM canonical_event_participants WHERE event_id=? AND role='away'", (xtream["canonical_event_id"],)).fetchone()[0]
        self.assertEqual("Washington Capitals", participant)

    def test_ambiguous_or_minor_league_mascots_cannot_force_a_catalog_match(self):
        self.apply(self.capitals_record(), {
            "entity_type": "team", "name": "Virden Oil Capitals", "sport": "Ice hockey", "league": "MJHL",
            "aliases": ["Virden Oil Caps"], "source": "wikidata", "external_ids": {"wikidata": "Q999999"},
        })
        sport_id = self.conn.execute("SELECT id FROM sports WHERE name='Ice hockey'").fetchone()[0]
        nhl_id = self.conn.execute("SELECT id FROM leagues WHERE name='NHL'").fetchone()[0]
        values = canonicalize_participants(self.conn, [{"name": "Capitals", "role": "away"}], sport_id=sport_id, league_id=nhl_id)
        self.assertEqual("Capitals", values[0]["name"])

    def test_racing_catalog_has_stable_identity_without_schedule_dates(self):
        self.apply({"entity_type": "racing_event", "name": "Italian Grand Prix", "sport": "Motorsport", "league": "Formula 1",
                    "aliases": ["Italian GP", "Monza"], "venue_aliases": ["Autodromo Nazionale Monza"],
                    "session_vocabulary": ["practice", "qualifying", "race"], "source": "wikidata", "external_ids": {"wikidata": "Q1"}})
        league_id = self.conn.execute("SELECT id FROM leagues WHERE name='Formula 1'").fetchone()[0]
        self.assertEqual("Italian Grand Prix", recurring_event_aliases(self.conn, "Monza", league_id=league_id)["name"])
        self.assertEqual(0, self.conn.execute("SELECT COUNT(*) FROM canonical_events").fetchone()[0])
        resolved = resolve_source_event(self.conn, source="apple", source_event_id="monza", data={
            "title": "Italian GP", "sport_name": "Motorsport", "league_name": "Formula 1", "event_type": "race", "start_utc": START,
        })
        metadata = json.loads(self.conn.execute("SELECT metadata_json FROM canonical_events WHERE id=?", (resolved["canonical_event_id"],)).fetchone()[0])
        self.assertEqual("Italian Grand Prix", metadata["recurring_event"]["name"])

    def test_source_normalizers_are_bounded_and_only_emit_catalog_entities(self):
        called = []
        def fetch(url):
            called.append(url)
            return {"teams": [{"idTeam": "134920", "strTeam": "Washington Capitals", "strSport": "Ice hockey", "strLeague": "NHL",
                               "strAlternate": "Washington Caps", "strTeamShort": "WSH"}]}
        records = thesportsdb_records(fetch, api_key="123", targets=(("Ice hockey", "NHL"),))
        self.assertEqual(1, len(records))
        self.assertEqual("134920", records[0]["external_ids"]["thesportsdb"])
        self.assertEqual(1, len(called))
        wd = wikidata_records({"results": {"bindings": [{
            "entity": {"value": "https://www.wikidata.org/entity/Q170185"}, "entityLabel": {"value": "Washington Capitals"},
            "entityType": {"value": "team"}, "sportLabel": {"value": "Ice hockey"}, "leagueLabel": {"value": "NHL"},
            "alias": {"value": "Washington Caps"},
        }]}})
        self.assertEqual("Q170185", wd[0]["external_ids"]["wikidata"])
        ol = openligadb_records([{"team1": {"teamId": 1, "teamName": "Example FC"}, "team2": {"teamId": 2, "teamName": "Other FC"}}], league="bl1")
        self.assertEqual({"Example FC", "Other FC"}, {record["name"] for record in ol})


if __name__ == "__main__":
    unittest.main()
