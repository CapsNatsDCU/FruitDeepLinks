import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from team_preferences import match_favorite_teams, rejected_favorite_matches


def team(name, sport, league, aliases=()):
    return {"team": name, "sport": sport, "league": league, "aliases": list(aliases), "enabled": True,
            "preferred_terms": [], "avoid_terms": []}


class CanonicalFavoriteTeamMatchingTests(unittest.TestCase):
    def assert_matches(self, favorite, titles):
        for title in titles:
            self.assertEqual([favorite["team"]], [x["team"]["team"] for x in match_favorite_teams({"title": title}, [favorite])], title)

    def assert_rejects(self, favorite, titles):
        for title in titles:
            event = title if isinstance(title, dict) else {"title": title}
            self.assertEqual([], match_favorite_teams(event, [favorite]), title)

    def test_capitals_canonical_identity_beats_mascot_alias(self):
        favorite = team("Washington Capitals", "hockey", "NHL", ["Capitals", "Washington Caps"])
        self.assert_matches(favorite, ["Washington Capitals vs New York Rangers", "New York Rangers at Washington Capitals", "NHL: Washington Capitals at Philadelphia Flyers", "Washington Caps at Rangers"])
        self.assert_rejects(favorite, ["Summerside Western Capitals vs West Kent Steamers", "Yorkton Terriers vs Virden Oil Capitals", "Cowichan Valley Capitals vs Alberni Valley Bulldogs", "(FLSP 333) | hockey: Summerside Western Capitals vs West Kent Steamers (Home)"])
        rejected = rejected_favorite_matches({"title": "Virden Oil Capitals vs Yorkton Terriers"}, [favorite])
        self.assertEqual("Capitals", rejected[0]["matched_term"])
        self.assertIn("ambiguous", rejected[0]["reason"])

    def test_nationals_canonical_identity_beats_mascot_alias(self):
        favorite = team("Washington Nationals", "baseball", "MLB", ["Nationals", "Nats"])
        self.assert_matches(favorite, ["MLB: Washington Nationals at Los Angeles Dodgers", "Philadelphia Phillies vs Washington Nationals"])
        self.assert_rejects(favorite, ["Fredericksburg Nationals vs Wilson Warbirds", "Rockland Nationals vs Hawkesbury Hawks", "London Nationals vs St. Thomas Stars", "HLR Skagit Nationals at Skagit Speedway"])

    def test_other_teams_and_dc_normalization(self):
        commanders = team("Washington Commanders", "football", "NFL", ["Commanders"])
        united = team("D.C. United", "soccer", "MLS", ["DC United"])
        self.assert_matches(commanders, ["NFL: Washington Commanders at Philadelphia Eagles", "Dallas Cowboys vs Washington Commanders"])
        self.assert_matches(united, ["MLS: FC Cincinnati vs. D.C. United", "MLS: D.C. United vs. Columbus Crew", "DC United vs Inter Miami"])

    def test_explicit_context_is_a_veto(self):
        favorite = team("Washington Nationals", "baseball", "MLB")
        self.assert_rejects(favorite, [{"title": "Washington Nationals at Dodgers", "league": "NHL"}])


if __name__ == "__main__": unittest.main()
