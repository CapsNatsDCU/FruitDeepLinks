import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from server.services.xtream_background_quality import run_background_quality
from server.services.xtream_quality import quality_probe_guard
from server.services.xtream_persistent import ensure_schema, quality_for_stream
from tests.test_xtream_pool import account_rows, pool_environment
from tests.xtream_test_helpers import HealthyAccountClient
from xtream_ingest import XtreamClient
from xtream_pool import XtreamPool


@contextmanager
def open_probe_guard(*args, **kwargs):
    yield ()


class BackgroundQualityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "fruit.db"
        environment = pool_environment(account_rows((1,)))
        with patch.dict(os.environ, environment):
            self.pool = XtreamPool(self.path, environment, client_factory=HealthyAccountClient)
            self.pool.check_accounts()
            self.pool.update("account_0", {"reserved_for_fruit": True})
        with self.pool.connection() as conn:
            ensure_schema(conn)
            conn.execute("""INSERT INTO xtream_persistent_channels
                (id,stream_id,category_id,original_name,display_name,channel_number,
                 availability_status,created_at,updated_at)
                VALUES (7,'437219','sports','Sports 1','Sports 1','7','available','2024-01-02T00:00:00Z','now')""")
            conn.execute("""INSERT INTO xtream_persistent_channels
                (id,stream_id,category_id,original_name,display_name,channel_number,
                 availability_status,created_at,updated_at)
                VALUES (8,'437220','sports','Sports 2','Sports 2','8','available','2024-01-01T00:00:00Z','now')""")
        self.patches = [
            patch("server.services.xtream_background_quality.installation_active", return_value=False),
            patch("server.services.xtream_background_quality.quality_probe_guard", side_effect=open_probe_guard),
            patch("server.services.xtream_background_quality.time.sleep"),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def test_missing_or_positive_provider_activity_never_opens_media(self):
        for reported in (None, 1):
            with self.subTest(reported=reported), patch(
                "server.services.xtream_background_quality.XtreamClient"
            ) as client_type, patch("server.services.xtream_background_quality._sample_media") as sample:
                client_type.return_value.get_probe_active_connections.return_value = reported
                self.assertEqual("provider_occupied_or_unknown",
                                 run_background_quality(self.path, pool=self.pool))
                sample.assert_not_called()
                self.assertEqual(0, self.pool.status()["active"])
                with self.pool.connection() as conn:
                    conn.execute("DELETE FROM xtream_background_quality_accounts")

    def test_unreserved_account_never_checks_provider(self):
        self.pool.update("account_0", {"reserved_for_fruit": False})
        with patch("server.services.xtream_background_quality.XtreamClient") as client_type:
            self.assertEqual("no_reserved_healthy_account", run_background_quality(self.path, pool=self.pool))
            client_type.assert_not_called()

    def test_two_zero_readings_measure_one_channel_and_enforce_account_interval(self):
        with patch("server.services.xtream_background_quality.XtreamClient") as client_type, \
             patch("server.services.xtream_background_quality._sample_media", return_value=b"\x47" * 188) as sample, \
             patch("server.services.xtream_background_quality._probe_bytes", return_value={
                 "width": 1280, "height": 720, "fps": 30, "codec": "h264"}):
            client_type.return_value.get_probe_active_connections.side_effect = [0, 0]
            self.assertEqual("measured", run_background_quality(self.path, pool=self.pool))
            self.assertEqual(2, client_type.return_value.get_probe_active_connections.call_count)
            self.assertEqual("account_0", sample.call_args.args[0].account.id)
            self.assertEqual("account_interval", run_background_quality(self.path, pool=self.pool))
        with self.pool.connection() as conn:
            self.assertEqual(720, quality_for_stream(conn, "sports", "437219")["height"])
        self.assertEqual(0, self.pool.status()["active"])

    def test_live_fruit_stream_blocks_background_provider_requests(self):
        lease = self.pool.acquire("playing", "persistent:1")
        try:
            with patch("server.services.xtream_background_quality.XtreamClient") as client_type:
                self.assertEqual("local_activity", run_background_quality(self.path, pool=self.pool))
                client_type.assert_not_called()
        finally:
            lease.release()

    def test_completed_stream_keeps_background_checks_paused(self):
        lease = self.pool.acquire("playing", "persistent:1")
        lease.release()
        with (patch("server.services.xtream_background_quality.quality_probe_guard", quality_probe_guard),
              patch("server.services.xtream_background_quality.XtreamClient") as client_type):
            self.assertEqual("deferred", run_background_quality(self.path, pool=self.pool))
            client_type.assert_not_called()

    def test_provider_failure_releases_lease_and_does_not_open_media(self):
        with patch("server.services.xtream_background_quality.XtreamClient") as client_type, \
             patch("server.services.xtream_background_quality._sample_media") as sample:
            client_type.return_value.get_probe_active_connections.side_effect = TimeoutError()
            self.assertEqual("error", run_background_quality(self.path, pool=self.pool))
            sample.assert_not_called()
        self.assertEqual(0, self.pool.status()["active"])

    def test_activity_appearing_between_provider_checks_skips_media(self):
        other = None
        rows = account_rows((1, 1))
        environment = pool_environment(rows)
        with patch.dict(os.environ, environment):
            pool = XtreamPool(self.path, environment, client_factory=HealthyAccountClient)
            pool.check_accounts()
            pool.update("account_0", {"reserved_for_fruit": True})
        def start_other_stream(*args, **kwargs):
            nonlocal other
            other = pool.acquire("playing", "persistent:1", excluded={"account_0"})
        with patch("server.services.xtream_background_quality.XtreamClient") as client_type, \
             patch("server.services.xtream_background_quality.time.sleep", side_effect=start_other_stream), \
             patch("server.services.xtream_background_quality._sample_media") as sample:
            client_type.return_value.get_probe_active_connections.return_value = 0
            try:
                self.assertEqual("local_activity", run_background_quality(self.path, pool=pool))
                sample.assert_not_called()
            finally:
                if other:
                    other.release()
        self.assertEqual(0, pool.status()["active"])

    def test_activity_count_requires_valid_authenticated_integer(self):
        for active, expected in (("0", 0), (0, 0), ("1", 1), (None, None),
                                 ("unknown", None), (True, None)):
            with self.subTest(active=active):
                maximum, check = XtreamClient._account_check_result({
                    "user_info": {"auth": 1, "status": "Active", "max_connections": "1",
                                  "active_cons": active}})
                self.assertEqual(1, maximum)
                self.assertEqual(expected, check.get("active_connections"))

    def test_scheduler_registers_a_single_slow_job(self):
        from server import scheduler
        fake = Mock()
        with patch.object(scheduler, "_AVAILABLE", True), \
             patch.object(scheduler, "BackgroundScheduler", return_value=fake), \
             patch.dict(os.environ, {"XTREAM_BACKGROUND_QUALITY_ENABLED": "true"}):
            try:
                scheduler.start()
                quality = [call for call in fake.add_job.call_args_list
                           if call.kwargs.get("id") == "xtream_background_quality"]
                self.assertEqual(1, len(quality))
                self.assertEqual(60, quality[0].kwargs["seconds"])
                self.assertEqual(1, quality[0].kwargs["max_instances"])
            finally:
                scheduler.stop()


if __name__ == "__main__":
    unittest.main()
