import json
import os
import sys
import tempfile
import time
import threading
import subprocess
import signal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from server.services.xtream_quality import (
    QualityProbeDeferred, _sample_media, measure_stream_quality, quality_probe_guard,
)
from xtream_activity import mark_normal_activity, normal_activity, normal_activity_remaining
from tests.test_xtream_pool import account_rows, pool_environment
from tests.xtream_test_helpers import FakeMedia, HealthyAccountClient
from xtream_ingest import XtreamError
from xtream_pool import PoolUnavailable, XtreamPool
from xtream_quality_sample import capture_sample, MAX_SAMPLE_BYTES


class QualityProbeTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "fruit.db"
        environment = pool_environment()
        with patch.dict(os.environ, environment):
            self.pool = XtreamPool(path, environment, client_factory=HealthyAccountClient)
            self.pool.check_accounts()

    def measure_fake(self, stream_id, *, session_factory, **kwargs):
        def sample(lease, stream, extension):
            return capture_sample("http://fixture/stream.ts", "http://fixture/stream.m3u8",
                                  extension, lease.fd, deadline=time.monotonic() + 8,
                                  session_factory=session_factory)
        with patch("server.services.xtream_quality._sample_media", side_effect=sample):
            return measure_stream_quality(stream_id, **kwargs)

    def test_independent_guards_serialize_without_blocking_playback(self):
        with quality_probe_guard(self.pool.db_path, pool=self.pool):
            with self.assertRaises(QualityProbeDeferred) as deferred:
                with quality_probe_guard(self.pool.db_path, pool=self.pool):
                    self.fail("A second probe must not run")
            self.assertIn("running", str(deferred.exception))
            lease = self.pool.acquire("playback", "persistent:1")
            lease.release()
        self.assertEqual(3, self.pool.status()["available"])

    def test_guard_defers_while_an_existing_probe_media_lease_is_live(self):
        lease = self.pool.acquire("probe", "quality_probe")
        try:
            with self.assertRaises(QualityProbeDeferred):
                with quality_probe_guard(self.pool.db_path, pool=self.pool):
                    self.fail("An existing probe media lease must prevent overlap")
        finally:
            lease.release()

    def test_successful_guard_pacing_survives_a_new_guard_instance(self):
        with patch("server.services.xtream_quality.time.time", return_value=100):
            with quality_probe_guard(self.pool.db_path, pool=self.pool):
                pass
        with patch("server.services.xtream_quality.time.time", return_value=109):
            with self.assertRaises(QualityProbeDeferred) as deferred:
                with quality_probe_guard(self.pool.db_path, pool=self.pool):
                    self.fail("Probe spacing must be preserved")
            self.assertEqual(1, deferred.exception.retry_after)
        with patch("server.services.xtream_quality.time.time", return_value=110):
            with quality_probe_guard(self.pool.db_path, pool=self.pool):
                pass

    def test_failed_guard_pauses_probes_but_not_playback_accounts(self):
        before = self.pool.status()["accounts"]
        with patch("server.services.xtream_quality.time.time", return_value=100):
            with self.assertRaises(RuntimeError):
                with quality_probe_guard(self.pool.db_path, pool=self.pool):
                    raise RuntimeError("Provider failure")
        with patch("server.services.xtream_quality.time.time", return_value=101):
            with self.assertRaises(QualityProbeDeferred) as deferred:
                with quality_probe_guard(self.pool.db_path, pool=self.pool):
                    self.fail("Failed probes must pause subsequent probes")
            self.assertEqual(119, deferred.exception.retry_after)
        self.assertEqual(before, self.pool.status()["accounts"])

    def test_normal_activity_extends_quiet_period_after_completion(self):
        with normal_activity(self.pool.db_path):
            self.assertGreater(normal_activity_remaining(self.pool.db_path), 9)
            with self.assertRaises(QualityProbeDeferred):
                with quality_probe_guard(self.pool.db_path, pool=self.pool):
                    self.fail("An active account request must prevent probing")
            with patch("xtream_activity.time.time", return_value=time.time() + 5):
                mark_normal_activity(self.pool.db_path)
        self.assertGreater(normal_activity_remaining(self.pool.db_path), 9)
        with self.assertRaises(QualityProbeDeferred) as deferred:
            with quality_probe_guard(self.pool.db_path, pool=self.pool):
                self.fail("The cooldown must survive a new guard instance")
        self.assertGreaterEqual(deferred.exception.retry_after, 9)

    def test_legacy_ten_minute_marker_is_shortened_once_and_then_expires(self):
        marker = self.pool.lock_dir / 'normal-activity-until'
        marker.write_text('1100', encoding='ascii')
        with patch('xtream_activity.time.time', return_value=500):
            self.assertEqual(10, normal_activity_remaining(self.pool.db_path))
        with patch('xtream_activity.time.time', return_value=508):
            self.assertEqual(2, normal_activity_remaining(self.pool.db_path))
        with patch('xtream_activity.time.time', return_value=511):
            self.assertEqual(0, normal_activity_remaining(self.pool.db_path))

    def test_metadata_requests_pause_probes_but_probe_catalog_does_not_pause_itself(self):
        config = self.pool.accounts[0].config
        with quality_probe_guard(self.pool.db_path, pool=self.pool):
            with self.pool.gate.hold(config):
                self.assertEqual(0, normal_activity_remaining(self.pool.db_path))
        activity_file = self.pool.lock_dir / "normal-activity-until"
        activity_file.write_text("0", encoding="ascii")
        with self.pool.gate.hold(config):
            self.assertGreater(normal_activity_remaining(self.pool.db_path), 9)

    def test_new_activity_cancels_in_progress_media_sample(self):
        lease = self.pool.acquire("probe", "quality_probe")
        process = Mock(pid=12345, returncode=None)
        process.stdin.closed = True
        process.stdout.closed = True
        def interrupted_sample(**kwargs):
            mark_normal_activity(self.pool.db_path)
            raise subprocess.TimeoutExpired("sample", kwargs["timeout"])
        process.communicate.side_effect = interrupted_sample
        try:
            with (patch("server.services.xtream_quality.subprocess.Popen", return_value=process),
                  patch("server.services.xtream_quality.os.killpg") as kill):
                with self.assertRaises(QualityProbeDeferred):
                    _sample_media(lease, "probe", "ts")
                kill.assert_called_once_with(process.pid, signal.SIGKILL)
        finally:
            lease.release()

    def test_normal_tune_waits_briefly_for_probe_to_yield(self):
        lease = self.pool.acquire("probe", "quality_probe")
        released = threading.Event()
        def yield_probe():
            while normal_activity_remaining(self.pool.db_path) == 0:
                time.sleep(0.01)
            lease.release()
            released.set()
        worker = threading.Thread(target=yield_probe, daemon=True)
        worker.start()
        normal = None
        try:
            normal = self.pool.acquire("playing", "persistent:1",
                                       excluded={"account_1", "account_2"})
            self.assertEqual("account_0", normal.account.id)
            self.assertTrue(released.wait(1))
        finally:
            if normal:
                normal.release()
            lease.release()
            worker.join(timeout=1)

    def test_degraded_account_is_skipped_for_probe_but_available_for_playback(self):
        with self.pool.connection() as conn:
            conn.execute("UPDATE xtream_account_state SET health='degraded' WHERE account_id='account_0'")
        with (patch("server.services.xtream_quality._sample_media", return_value=b"\x47" * 188) as sample,
              patch("server.services.xtream_quality._probe_bytes", return_value={"height": 720})):
            measure_stream_quality("probe", pool=self.pool)
        self.assertEqual("account_1", sample.call_args.args[0].account.id)
        self.assertEqual("degraded", self.pool.status()["accounts"][0]["health"])
        lease = self.pool.acquire("playback", "persistent:1")
        self.assertEqual("account_0", lease.account.id)
        lease.release()

    def test_unverified_accounts_never_trigger_verification_or_sampling(self):
        for health in ("degraded", "unknown", "unreachable", "unhealthy"):
            with self.subTest(health=health):
                with self.pool.connection() as conn:
                    conn.execute("UPDATE xtream_account_state SET health=?", (health,))
                before = self.pool.status()
                with (patch.object(self.pool, "check_accounts") as verify,
                      patch("server.services.xtream_quality._sample_media") as sample):
                    for guarded in (True, False):
                        with self.assertRaises(QualityProbeDeferred) as deferred:
                            measure_stream_quality("probe", pool=self.pool, guarded=guarded)
                        self.assertIn("verified healthy", str(deferred.exception))
                    verify.assert_not_called()
                    sample.assert_not_called()
                self.assertEqual(before, self.pool.status())

    def test_guard_defers_while_any_account_has_playback(self):
        lease = self.pool.acquire("playback", "persistent:1")
        try:
            with self.pool.connection() as conn:
                conn.execute("UPDATE xtream_account_state SET health='degraded' WHERE account_id='account_1'")
            with self.assertRaises(QualityProbeDeferred):
                with quality_probe_guard(self.pool.db_path, pool=self.pool):
                    self.fail("Playback on another account must pause resolution checks")
        finally:
            lease.release()

    def test_occupied_healthy_capacity_defers_probe_before_sampling(self):
        leases = [self.pool.acquire(str(i), "playback") for i in range(3)]
        try:
            with patch("server.services.xtream_quality._sample_media") as sample:
                with self.assertRaises(QualityProbeDeferred):
                    measure_stream_quality("probe", pool=self.pool)
                sample.assert_not_called()
        finally:
            for lease in leases:
                lease.release()

    def test_allocator_rechecks_health_after_probe_preflight(self):
        with quality_probe_guard(self.pool.db_path, pool=self.pool):
            with self.pool.connection() as conn:
                conn.execute("UPDATE xtream_account_state SET health='degraded'")
            with self.assertRaises(PoolUnavailable):
                self.pool.acquire("probe", "quality_probe")
        self.assertEqual(0, self.pool.status()["active"])

    def test_probe_passes_only_media_to_ffprobe_and_releases_capacity(self):
        media = FakeMedia([b"\x47" * 188] * 3)
        session = Mock()
        session.get.return_value = media
        runner = Mock(return_value=Mock(returncode=0, stdout=json.dumps({"streams": [
            {"codec_type": "video", "codec_name": "h264", "width": 1920,
             "height": 1080, "avg_frame_rate": "60000/1001"},
        ]}).encode()))
        def analyze(*args, **kwargs):
            self.assertTrue(media.closed)
            self.assertEqual(0, self.pool.status()["active"])
            return Mock(returncode=0, stdout=json.dumps({"streams": [
                {"codec_type": "video", "width": 1920, "height": 1080,
                 "codec_name": "h264", "avg_frame_rate": "60000/1001"},
            ]}).encode())
        runner.side_effect = analyze
        result = self.measure_fake("437219", pool=self.pool,
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
        with patch("xtream_curl.CurlStream", return_value=curl) as fallback:
            result = self.measure_fake("437219", pool=self.pool,
                                            session_factory=lambda: session, runner=runner)
        self.assertEqual(720, result["height"])
        self.assertTrue(first.closed)
        self.assertEqual(0, self.pool.status()["active"])
        self.assertEqual("healthy", self.pool.status()["accounts"][0]["health"])
        self.assertEqual(1, session.get.call_count)
        self.assertIn(".ts", fallback.call_args.args[0])
        curl.close.assert_called_once()

    def test_quality_sample_retries_alternate_host_after_primary_failure(self):
        first = FakeMedia(status=404)
        second = FakeMedia([b"\x47" * 188])
        session = Mock()
        session.get.side_effect = [first, second]
        sample = capture_sample(
            "http://primary.example/stream.ts", "http://primary.example/stream.m3u8",
            "ts", 42, deadline=time.monotonic() + 3,
            alternate_ts_url="http://alternate.example/stream.ts",
            alternate_hls_url="http://alternate.example/stream.m3u8",
            session_factory=lambda: session,
        )
        self.assertEqual(b"\x47" * 188, sample)
        self.assertTrue(first.closed)
        self.assertEqual("http://alternate.example/stream.ts", session.get.call_args_list[1].args[0])

    def test_failed_probe_uses_one_account_without_cooling_down_playback(self):
        session = Mock()
        session.get.return_value = FakeMedia(status=403)
        curl = Mock()
        curl.chunks.side_effect = OSError("Curl media transport failed")
        with patch("xtream_curl.CurlStream", return_value=curl):
            with self.assertRaises(XtreamError):
                self.measure_fake("437219", pool=self.pool,
                                       session_factory=lambda: session)
        state = self.pool.status()
        self.assertEqual(0, state["active"])
        self.assertTrue(all(account["health"] == "healthy" for account in state["accounts"]))
        self.assertEqual(3, state["available"])
        self.assertEqual(1, session.get.call_count)
        self.assertEqual(1, curl.close.call_count)
        self.assertEqual("quality_probe", state["recent_streams"][0]["source"])

    def test_byte_limit_does_not_read_an_extra_chunk(self):
        media = FakeMedia([b"\x47" * MAX_SAMPLE_BYTES, RuntimeError("Must not read")])
        session = Mock()
        session.get.return_value = media
        sample = capture_sample("http://fixture/stream.ts", "http://fixture/stream.m3u8",
                                "ts", 0, deadline=time.monotonic() + 8,
                                session_factory=lambda: session)
        self.assertEqual(MAX_SAMPLE_BYTES, len(sample))
        self.assertEqual(1, media.reads)
        self.assertTrue(media.closed)

    def test_analysis_failure_has_already_released_media_capacity(self):
        runner = Mock(side_effect=RuntimeError("Analysis failed"))
        with patch("server.services.xtream_quality._sample_media", return_value=b"\x47" * 188):
            with self.assertRaises(RuntimeError):
                measure_stream_quality("437219", pool=self.pool, runner=runner)
        self.assertEqual(0, self.pool.status()["active"])

    def test_real_slow_trickle_cannot_extend_the_media_deadline(self):
        disconnected = threading.Event()
        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                try:
                    # Every byte arrives before the idle timeout. Only a hard
                    # wall deadline can stop this incomplete chunk/header sample.
                    while True:
                        self.wfile.write(b"\x47")
                        self.wfile.flush()
                        time.sleep(0.05)
                except (BrokenPipeError, ConnectionResetError):
                    disconnected.set()
        provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        provider.daemon_threads = True
        self.addCleanup(provider.server_close)
        self.addCleanup(provider.shutdown)
        threading.Thread(target=provider.serve_forever, daemon=True).start()
        rows = account_rows((None, None, None))
        for row in rows:
            row["server_url"] = f"http://127.0.0.1:{provider.server_port}"
        self.pool = XtreamPool(self.pool.db_path, pool_environment(rows),
                               client_factory=HealthyAccountClient)
        self.pool.check_accounts()
        before = time.monotonic()
        with patch("server.services.xtream_quality.MAX_SAMPLE_SECONDS", 1):
            with self.assertRaises(XtreamError):
                measure_stream_quality("437219", pool=self.pool)
        self.assertLess(time.monotonic() - before, 3)
        self.assertTrue(disconnected.wait(2), "The provider socket must close at the deadline")
        self.assertEqual(0, self.pool.status()["active"])

    def test_sample_watchdog_stops_curl_without_a_web_worker(self):
        connected, disconnected = threading.Event(), threading.Event()
        attempted = threading.Event()
        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                # Deny the first media request so the curl fallback is exercised
                # even when Python and curl correctly use the same player agent.
                if not attempted.is_set():
                    attempted.set()
                    self.send_response(403)
                    self.end_headers()
                    return
                self.send_response(200)
                self.end_headers()
                connected.set()
                try:
                    while True:
                        self.wfile.write(b"\x47")
                        self.wfile.flush()
                        time.sleep(0.05)
                except (BrokenPipeError, ConnectionResetError):
                    disconnected.set()
        provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        provider.daemon_threads = True
        self.addCleanup(provider.server_close)
        self.addCleanup(provider.shutdown)
        threading.Thread(target=provider.serve_forever, daemon=True).start()
        lease = self.pool.acquire("437219", "quality_probe")
        process = subprocess.Popen([
            sys.executable, str(Path(__file__).resolve().parents[1] / "bin/xtream_quality_sample.py"),
            str(time.monotonic() + 2), str(lease.fd), str(lease.gate_fd),
        ], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, pass_fds=(lease.fd, lease.gate_fd))
        try:
            # Only feed input; no communicate timeout or supervising web worker.
            url = f"http://127.0.0.1:{provider.server_port}/stream.ts"
            process.stdin.write(json.dumps({"ts_url": url, "hls_url": url,
                                            "extension": "ts"}).encode())
            process.stdin.close()
            self.assertTrue(connected.wait(1.5), "Curl fallback must actually connect")
            self.assertIn(process.wait(timeout=3), (0, -signal.SIGKILL))
            self.assertTrue(disconnected.wait(2), "Watchdog must kill the curl socket too")
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)
            lease.release()
        self.assertEqual(0, self.pool.status()["active"])



if __name__ == "__main__":
    unittest.main()
