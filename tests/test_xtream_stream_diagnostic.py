"""A manual lane test must use the lane's selected stream and report early EOF."""
from __future__ import annotations

import sys
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from server.services.xtream_stream_diagnostic import diagnose_lane_stream
from xtream_ingest import XtreamError


class FakePool:
    def connection(self):
        return nullcontext(object())

    def status(self):
        return {"accounts": [
            {"id": "busy", "enabled": True, "available": 0},
            {"id": "free", "enabled": True, "available": 1},
        ]}


class LaneDiagnosticTests(unittest.TestCase):
    def test_reports_short_decodable_media_without_calling_it_sustained(self):
        rows = [
            {"method": "python_ts", "status": "passed", "video": {"width": 1920},
             "first_bytes_format": "mpeg_ts", "delivery": "upstream_eof", "media_span_seconds": 2.4},
            {"method": "curl_ts", "status": "passed", "video": {"width": 1920},
             "first_bytes_format": "mpeg_ts", "delivery": "window_complete", "media_span_seconds": 6.0},
            {"method": "ffmpeg_hls", "status": "failed", "delivery": "not_started"},
        ]
        with (patch("server.services.xtream_stream_diagnostic.get_lane_direct_stream",
                    return_value={"provider": "xtream", "stream_id": "2153009"}),
              patch("server.services.xtream_stream_diagnostic.compare",
                    return_value={"status": "complete", "results": rows}) as compare):
            result = diagnose_lane_stream(FakePool(), 2)
        self.assertEqual("short_media_then_eof", result["finding"])
        self.assertEqual("mpeg_ts", result["source_first_bytes_format"])
        self.assertEqual("MPEG-TS", result["channels_stream_format"])
        compare.assert_called_once_with(unittest.mock.ANY, "free", "2153009",
                                        host="fruit_preferred", seconds=10)

    def test_missing_or_non_xtream_lane_does_not_contact_provider(self):
        with (patch("server.services.xtream_stream_diagnostic.get_lane_direct_stream",
                    return_value={"provider": "other", "stream_id": "2153009"}),
              patch("server.services.xtream_stream_diagnostic.compare") as compare):
            with self.assertRaisesRegex(XtreamError, "no selected Xtream stream"):
                diagnose_lane_stream(FakePool(), 2)
        compare.assert_not_called()

    def test_no_available_account_skips_without_provider_request(self):
        pool = FakePool()
        pool.status = lambda: {"accounts": [{"id": "busy", "enabled": True, "available": 0}]}
        with (patch("server.services.xtream_stream_diagnostic.get_lane_direct_stream",
                    return_value={"provider": "xtream", "stream_id": "2153009"}),
              patch("server.services.xtream_stream_diagnostic.compare") as compare):
            result = diagnose_lane_stream(pool, 2)
        self.assertEqual("no_available_account", result["reason"])
        compare.assert_not_called()


if __name__ == "__main__":
    unittest.main()
