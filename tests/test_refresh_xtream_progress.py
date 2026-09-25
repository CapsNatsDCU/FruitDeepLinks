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

    def test_tracks_event_resolution_and_catalog_review_as_distinct_phases(self):
        refresh._consume_progress_marker(self.marker(
            "event_resolution_start", ai_mode="bounded", started_at="2026-09-25T15:00:00Z",
        ))
        refresh._consume_progress_marker(self.marker(
            "event_resolution_done", status="complete", ai_mode="bounded", resolved=25,
            resolved_without_ai=21, ai_interpretations_used=2, requests=3, cache_hits=1,
            failures=1, budget_exhausted=4, finished_at="2026-09-25T15:00:07Z",
        ))
        refresh._consume_progress_marker(self.marker(
            "catalog_ai_start", started_at="2026-09-25T15:00:08Z",
        ))
        refresh._consume_progress_marker(self.marker(
            "catalog_ai_done", status="completed", proposals=3, run_id=91,
            finished_at="2026-09-25T15:00:16Z",
        ))

        resolution = refresh.refresh_status["progress"]["event_resolution"]
        self.assertEqual(resolution["resolved_without_ai"], 21)
        self.assertEqual(resolution["ai_interpretations_used"], 2)
        self.assertEqual(resolution["budget_exhausted"], 4)
        self.assertEqual(refresh.refresh_status["progress"]["catalog_ai"], {
            "status": "completed", "started_at": "2026-09-25T15:00:08Z",
            "proposals": 3, "run_id": 91, "finished_at": "2026-09-25T15:00:16Z",
        })


if __name__ == "__main__":
    unittest.main()
