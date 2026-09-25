import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from server import refresh  # noqa: E402


class RefreshXtreamProgressTest(unittest.TestCase):
    def setUp(self):
        self.original_progress = refresh.refresh_status["progress"]
        refresh.refresh_status["progress"] = refresh._new_progress()

    def tearDown(self):
        refresh.refresh_status["progress"] = self.original_progress

    def marker(self, event, **fields):
        return refresh.REFRESH_PROGRESS_PREFIX + json.dumps({"event": event, **fields})

    def test_tracks_category_updates_without_exposing_transport_details(self):
        self.assertTrue(refresh._consume_progress_marker(self.marker(
            "xtream_categories_start",
            categories=[
                {"category_id": "597", "category_name": "Sports", "status": "queued"},
                {"category_id": "606", "category_name": "Events", "status": "queued"},
            ],
        )))
        refresh._consume_progress_marker(self.marker(
            "xtream_category_update", category_id="606", status="complete",
            streams_fetched=123, events_recognized=7, skipped_placeholder=2,
            detail="Imported",
        ))

        categories = refresh.refresh_status["progress"]["xtream_categories"]
        self.assertEqual(categories[0]["status"], "queued")
        self.assertEqual(categories[1], {
            "category_id": "606", "category_name": "Events", "status": "complete",
            "streams_fetched": 123, "events_recognized": 7,
            "skipped_placeholder": 2, "detail": "Imported",
        })


if __name__ == "__main__":
    unittest.main()
