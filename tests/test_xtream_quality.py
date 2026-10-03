import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from server.services.xtream_quality import measure_stream_quality
from tests.test_xtream_pool import pool_environment
from tests.xtream_test_helpers import FakeMedia, HealthyAccountClient
from xtream_pool import XtreamPool


class QualityProbeTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "fruit.db"
        environment = pool_environment()
        with patch.dict(os.environ, environment):
            self.pool = XtreamPool(path, environment, client_factory=HealthyAccountClient)
            self.pool.check_accounts()

    def test_probe_passes_only_media_to_ffprobe_and_releases_capacity(self):
        media = FakeMedia([b"\x47" * 188] * 3)
        session = Mock()
        session.get.return_value = media
        runner = Mock(return_value=Mock(returncode=0, stdout=json.dumps({"streams": [
            {"codec_type": "video", "codec_name": "h264", "width": 1920,
             "height": 1080, "avg_frame_rate": "60000/1001"},
        ]}).encode()))
        result = measure_stream_quality("437219", pool=self.pool,
                                        session_factory=lambda: session, runner=runner)
        self.assertEqual({"width": 1920, "height": 1080, "fps": 59.94, "codec": "h264"}, result)
        self.assertEqual(0, self.pool.status()["active"])
        self.assertTrue(media.closed)
        self.assertEqual(b"\x47" * 188 * 3, runner.call_args.kwargs["input"])
        self.assertNotIn("private-password", " ".join(runner.call_args.args[0]))
        self.assertNotIn("provider", " ".join(runner.call_args.args[0]))

    def test_python_auth_rejection_retries_with_curl_and_preserves_account(self):
        first = FakeMedia(status=401)
        session = Mock()
        session.get.return_value = first
        curl = Mock()
        curl.chunks.return_value = iter([b"\x47" * 188])
        runner = Mock(return_value=Mock(returncode=0, stdout=json.dumps({"streams": [
            {"codec_type": "video", "width": 1280, "height": 720},
        ]}).encode()))
        with patch("server.services.xtream_quality.CurlStream", return_value=curl) as fallback:
            result = measure_stream_quality("437219", pool=self.pool,
                                            session_factory=lambda: session, runner=runner)
        self.assertEqual(720, result["height"])
        self.assertTrue(first.closed)
        self.assertEqual(0, self.pool.status()["active"])
        self.assertEqual("healthy", self.pool.status()["accounts"][0]["health"])
        self.assertEqual(1, session.get.call_count)
        self.assertIn(".ts", fallback.call_args.args[0])
        curl.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
