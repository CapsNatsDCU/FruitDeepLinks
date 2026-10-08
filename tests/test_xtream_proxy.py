import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from flask import Flask

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from server.app import create_app
from server.services.xtream_persistent import create_channel
from server.services.xtream_proxy import proxy_stream
from xtream_pool import XtreamPool
from xtream_hosts import host_configs
from tests.test_xtream_pool import account_rows, pool_environment
from tests.xtream_test_helpers import FakeMedia, HealthyAccountClient, mocked_provider


class StreamProxyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "fruit.db"
        self.env = {**pool_environment(), "FRUIT_DB_PATH": str(self.path)}
        self.environment = patch.dict(os.environ, self.env)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.pool = XtreamPool(self.path, self.env, client_factory=HealthyAccountClient)
        self.pool.check_accounts()
        self.app = Flask(__name__)
        self.app.add_url_rule('/stream', view_func=lambda: proxy_stream("437219", "test", pool=self.pool), methods=["GET", "HEAD"])
        self.client = self.app.test_client()

    def test_streaming_is_incremental_and_close_releases_even_without_full_consumption(self):
        upstream = FakeMedia([b"\x47" * 188] * 100)
        with mocked_provider(upstream) as session:
            response = self.client.get('/stream', buffered=False)
            self.assertEqual(200, response.status_code)
            self.assertEqual("video/mp2t", response.content_type)
            self.assertNotIn("Location", response.headers)
            self.assertEqual(1, upstream.reads)
            self.assertEqual(1, self.pool.status()["active"])
            self.assertTrue(session.get.call_args.kwargs["stream"])
            self.assertTrue(session.get.call_args.kwargs["allow_redirects"])
            self.assertEqual("http://provider.example:8080/private-user-0/private-password%2F0/437219.ts",
                             session.get.call_args.args[0])
            self.assertEqual((10, 60), session.get.call_args.kwargs["timeout"])
            response.close()
        self.assertTrue(upstream.closed)
        self.assertEqual(0, self.pool.status()["active"])
        self.assertEqual("client_closed", self.pool.status()["recent_streams"][0]["outcome"])

    def test_stream_retries_alternate_host_with_same_lease(self):
        rows = account_rows((1,))
        rows[0]["fallback_server_url"] = "http://alternate.example"
        self.pool = XtreamPool(self.path, pool_environment(rows), client_factory=HealthyAccountClient)
        self.pool.check_accounts()
        first = FakeMedia(status=403)
        second = FakeMedia([b"\x47" * 188])
        session = Mock()
        session.get.side_effect = [first, second]
        curl = Mock()
        curl.chunks.side_effect = OSError("primary failed")
        with patch("server.services.xtream_proxy.requests.Session", return_value=session), \
             patch("server.services.xtream_proxy.CurlStream", return_value=curl):
            response = self.client.get('/stream', buffered=False)
            self.assertEqual(200, response.status_code)
            self.assertEqual(1, self.pool.status()["active"])
            self.assertEqual("http://alternate.example/private-user-0/private-password%2F0/437219.ts",
                             session.get.call_args_list[1].args[0])
            response.close()
        self.assertTrue(first.closed)
        curl.close.assert_called_once()
        self.assertEqual(0, self.pool.status()["active"])
        with self.pool.gate.hold(self.pool.accounts[0].config) as fd:
            self.assertEqual(1, host_configs(self.pool.accounts[0].config, fd)[0][0])

    def test_degraded_account_tunes_without_waiting_for_account_api(self):
        with self.pool.connection() as conn:
            conn.execute("UPDATE xtream_account_state SET health='degraded',last_checked=0")
        with patch.object(self.pool, "check_accounts") as check, \
             mocked_provider(FakeMedia([b"\x47" * 376])) as upstream:
            response = self.client.get('/stream', buffered=False)
            self.assertEqual(200, response.status_code)
            self.assertEqual(b"\x47" * 376, response.get_data())
            response.close()
            self.assertEqual(1, upstream.get.call_count)
            check.assert_not_called()
        self.assertEqual(0, self.pool.status()["active"])
        self.assertTrue(all(account["health"] == "degraded" for account in self.pool.status()["accounts"]))
        account = self.pool.status()["accounts"][0]
        self.assertIsNotNone(account["last_media_success"])
        self.assertEqual("ready_degraded", account["availability_reason"])
        self.assertIsNone(self.pool.status()["accounts"][1]["last_media_success"])

    def test_failed_tune_enters_cooldown_before_releasing_account_and_retries_next(self):
        finish = self.pool.finish
        failed_accounts = []

        def checked_finish(lease, outcome, fd, gate_fd):
            if outcome == "tune_failed":
                state = next(a for a in self.pool.status()["accounts"] if a["id"] == lease.account.id)
                self.assertGreater(state["retry_after_seconds"], 0)
                self.assertTrue(state["busy"])
                failed_accounts.append(lease.account.id)
            return finish(lease, outcome, fd, gate_fd)

        session = Mock()
        session.get.side_effect = [FakeMedia(status=503), FakeMedia([b"\x47" * 376])]
        with patch.object(self.pool, "finish", side_effect=checked_finish), \
             patch("server.services.xtream_proxy.requests.Session", return_value=session):
            response = self.client.get('/stream', buffered=False)
            try:
                self.assertEqual(200, response.status_code)
                self.assertEqual(["account_0"], failed_accounts)
                self.assertEqual("account_1", self.pool.status()["leases"][0]["account_id"])
            finally:
                response.close()
        self.assertEqual(0, self.pool.status()["active"])

    def test_unknown_accounts_are_checked_before_first_tune(self):
        with self.pool.connection() as conn:
            conn.execute("UPDATE xtream_account_state SET health='unknown',last_success=NULL,last_checked=0")
        with patch.object(self.pool, "check_accounts", wraps=self.pool.check_accounts) as check, \
             mocked_provider(FakeMedia([b"\x47" * 376])):
            response = self.client.get('/stream', buffered=False)
            self.assertEqual(200, response.status_code)
            response.close()
            check.assert_called_once_with(due_only=True)

    def test_all_three_live_responses_fourth_rejected_then_reused(self):
        with mocked_provider():
            responses = [self.client.get('/stream', buffered=False) for _ in range(3)]
            try:
                self.assertEqual([200] * 3, [r.status_code for r in responses])
                fourth = self.client.get('/stream')
                self.assertEqual(503, fourth.status_code)
                self.assertEqual("5", fourth.headers["Retry-After"])
                responses[0].close()
                replacement = self.client.get('/stream', buffered=False)
                self.assertEqual(200, replacement.status_code)
                replacement.close()
            finally:
                for response in responses:
                    response.close()
        self.assertEqual(0, self.pool.status()["active"])

    def test_eof_read_exception_timeout_and_generator_never_started_cleanup(self):
        for parts, outcome in (([b"\x47"], "upstream_eof"), ([b"\x47", OSError("secret-url")], "upstream_error"),
                               ([b"\x47", requests.Timeout("secret-url")], "upstream_timeout")):
            with self.subTest(outcome=outcome), mocked_provider(FakeMedia(parts)):
                response = self.client.get('/stream', buffered=False)
                self.assertEqual(b"\x47", response.get_data())
                response.close()
                self.assertEqual(0, self.pool.status()["active"])
                self.assertEqual(outcome, self.pool.status()["recent_streams"][0]["outcome"])
        with self.app.test_request_context('/stream'), mocked_provider():
            response = proxy_stream("1", "unstarted", pool=self.pool)
            response.close()
        self.assertEqual(0, self.pool.status()["active"])

    def test_connection_error_and_bad_response_release_all_capacity(self):
        for failure in (requests.ConnectionError("http://private-user-0:private-password/0@provider"), FakeMedia(status=500), FakeMedia([]), FakeMedia([b'{"password":"bad"}'])):
            self.pool.check_accounts()
            session = Mock()
            if isinstance(failure, Exception):
                session.get.side_effect = failure
            else:
                session.get.return_value = failure
            with patch("server.services.xtream_proxy.requests.Session", return_value=session):
                response = self.client.get('/stream')
            self.assertEqual(502, response.status_code)
            self.assertEqual(0, self.pool.status()["active"])
            self.assertNotIn("private-password", response.get_data(as_text=True))

    def test_python_auth_rejection_retries_media_with_curl_without_disabling_account(self):
        responses = [FakeMedia(status=401)]
        session = Mock()
        session.get.side_effect = responses
        curl = Mock()
        curl.chunks.return_value = iter([b"\x47" * 188])
        with patch("server.services.xtream_proxy.requests.Session", return_value=session), \
             patch("server.services.xtream_proxy.CurlStream", return_value=curl) as fallback:
            response = self.client.get('/stream', buffered=False)
            state = self.pool.status()
            self.assertEqual(200, response.status_code)
            self.assertEqual(1, state["active"])
            self.assertEqual("healthy", state["accounts"][0]["health"])
            self.assertEqual("account_0", state["leases"][0]["account_id"])
            self.assertTrue(responses[0].closed)
            self.assertEqual(1, session.get.call_count)
            self.assertIn(".ts", fallback.call_args.args[0])
            response.close()
        curl.close.assert_called_once()

    def test_failed_curl_retry_cools_down_account_and_releases_lease(self):
        session = Mock()
        session.get.return_value = FakeMedia(status=403)
        curl = Mock()
        curl.chunks.side_effect = OSError("Curl media transport failed")
        with patch("server.services.xtream_proxy.requests.Session", return_value=session), \
             patch("server.services.xtream_proxy.CurlStream", return_value=curl):
            response = self.client.get('/stream')
        self.assertEqual(502, response.status_code)
        state = self.pool.status()
        self.assertEqual(0, state["active"])
        self.assertTrue(all(account["health"] == "degraded" for account in state["accounts"]))
        self.assertEqual(0, state["available"])
        self.assertEqual(3, session.get.call_count)
        curl.close.assert_called()

    def test_session_constructor_failure_releases_and_channel_error_does_not_disable_accounts(self):
        with patch("server.services.xtream_proxy.requests.Session", side_effect=OSError("setup failed")):
            self.assertEqual(502, self.client.get('/stream').status_code)
        self.assertEqual(0, self.pool.status()["active"])
        with mocked_provider(FakeMedia(status=404)):
            self.assertEqual(502, self.client.get('/stream').status_code)
        state = self.pool.status()
        self.assertEqual(0, state["active"])
        self.assertTrue(all(a["health"] == "healthy" for a in state["accounts"]))

    def test_missing_stream_retries_another_account_with_its_own_credentials(self):
        first = FakeMedia(status=404)
        second = FakeMedia([b"\x47" * 188])
        session = Mock()
        session.get.side_effect = [first, second]
        with patch("server.services.xtream_proxy.requests.Session", return_value=session):
            response = self.client.get('/stream', buffered=False)
            self.assertEqual(200, response.status_code)
            self.assertEqual(b"\x47" * 188, response.get_data())
            response.close()
        self.assertEqual([
            "http://provider.example:8080/private-user-0/private-password%2F0/437219.ts",
            "http://provider.example:8080/private-user-1/private-password%2F1/437219.ts",
        ], [call.args[0] for call in session.get.call_args_list])
        self.assertTrue(first.closed)
        self.assertEqual(0, self.pool.status()["active"])

    def test_head_and_range_probe_cannot_leak_upstream_headers(self):
        with mocked_provider() as session:
            self.assertEqual(200, self.client.head('/stream').status_code)
            session.get.assert_not_called()
            self.assertEqual(0, self.pool.status()["active"])
            response = self.client.get('/stream', headers={"Range": "bytes=0-", "Cookie": "private", "Authorization": "secret"})
            self.assertEqual({"Accept-Encoding": "identity"}, session.get.call_args.kwargs["headers"])
            self.assertNotIn("Set-Cookie", response.headers)
            response.close()

    def test_hls_body_is_remuxed_and_never_exposed(self):
        upstream = FakeMedia([b"#EXTM3U\nhttp://provider/live/private-user-0/private-password%2F0/1.ts"])
        remux = Mock()
        remux.chunks.return_value = iter([b"\x47" * 188])
        with mocked_provider(upstream), patch("server.services.xtream_proxy.HLSStream", return_value=remux) as factory:
            response = self.client.get('/stream', buffered=False)
            self.assertEqual(200, response.status_code)
            self.assertEqual(b"\x47" * 188, response.get_data())
            response.close()
            self.assertTrue(upstream.closed)
            self.assertIn(".m3u8", factory.call_args.args[0])
            self.assertGreaterEqual(factory.call_args.args[2], 0)
            remux.close.assert_called()
        self.assertEqual(0, self.pool.status()["active"])

    def test_rapid_tune_stop_and_retry_do_not_accumulate_leases(self):
        with mocked_provider():
            for _ in range(25):
                response = self.client.get('/stream', buffered=False)
                self.assertEqual(200, response.status_code)
                response.close()
        self.assertEqual(0, self.pool.status()["active"])
        self.assertFalse(any(not (path.name.startswith("account-") or
                                  path.name == "normal-activity-until")
                             for path in self.pool.lock_dir.iterdir()))

    def test_persistent_and_dynamic_routes_share_pool_and_ignore_legacy_xtream_urls(self):
        with sqlite3.connect(self.path) as conn:
            channel = create_channel(conn, {"stream_id": "500", "name": "ESPN"}, category_id="10", category_name="Sports", channel_number="10")
            conn.executescript("""
                CREATE TABLE playables(event_id TEXT,playable_id TEXT,provider TEXT,stream_id TEXT,stream_extension TEXT,stream_url TEXT);
                CREATE TABLE lane_events(lane_id INTEGER,event_id TEXT,chosen_playable_id TEXT,is_placeholder INTEGER,start_utc TEXT,end_utc TEXT);
                INSERT INTO playables VALUES('game','play','xtream','700','ts','http://provider/live/old-user/old-password/700.ts');
                INSERT INTO lane_events VALUES(7,'game','play',0,'2020-01-01','2099-01-01');
            """)
        client = create_app().test_client()
        with mocked_provider() as session:
            responses = [client.get(f'/xtream/channel/{channel["id"]}/stream', buffered=False),
                         client.get('/lane/7/stream.m3u8', buffered=False),
                         client.get('/lane/7/stream.m3u8', buffered=False)]
            try:
                self.assertEqual([200] * 3, [r.status_code for r in responses])
                self.assertEqual(3, self.pool.status()["active"])
                self.assertEqual(503, client.get('/lane/7/stream.m3u8').status_code)
                self.assertTrue(all("old-password" not in c.args[0] for c in session.get.call_args_list))
            finally:
                for response in responses:
                    response.close()
        self.assertEqual(0, self.pool.status()["active"])

    def test_non_xtream_direct_source_keeps_existing_behavior_without_lease(self):
        with sqlite3.connect(self.path) as conn:
            conn.executescript("""
                CREATE TABLE playables(event_id TEXT,playable_id TEXT,provider TEXT,stream_id TEXT,stream_extension TEXT,stream_url TEXT);
                CREATE TABLE lane_events(lane_id INTEGER,event_id TEXT,chosen_playable_id TEXT,is_placeholder INTEGER,start_utc TEXT,end_utc TEXT);
                INSERT INTO playables VALUES('game','play','public','700','ts','https://media.example/public.ts');
                INSERT INTO lane_events VALUES(7,'game','play',0,'2020-01-01','2099-01-01');
            """)
        with patch.dict(os.environ, {"XTREAM_ONLY": "false"}), mocked_provider() as session:
            response = create_app().test_client().get('/lane/7/stream.m3u8')
            self.assertEqual(302, response.status_code)
            self.assertEqual("https://media.example/public.ts", response.headers["Location"])
            session.get.assert_not_called()
        self.assertEqual(0, self.pool.status()["active"])

    def test_admin_api_and_logs_contain_no_secrets_and_cannot_write_credentials(self):
        client = create_app().test_client()
        response = client.patch('/api/xtream/pool/accounts/account_0', json={"password": "bad"})
        self.assertEqual(400, response.status_code)
        with mocked_provider():
            response = self.client.get('/stream')
            response.close()
        from server.logging_setup import get_recent_logs
        values = client.get('/api/xtream/pool').get_data(as_text=True) + repr(get_recent_logs(count=100))
        for account in account_rows():
            self.assertNotIn(account["username"], values)
            self.assertNotIn(account["password"], values)
        self.assertNotIn("/live/", values)


if __name__ == '__main__':
    unittest.main()
