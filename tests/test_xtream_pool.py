import json
import multiprocessing
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import requests
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from xtream_accounts import load_accounts
from db.preferences import get_settings_schema, load_all_settings
from xtream_ingest import XtreamClient, XtreamConfig, XtreamError, build_stream_url, load_config, load_metadata_configs
from xtream_pool import PoolUnavailable, XtreamPool, scheduler_capacity
from tests.xtream_test_helpers import HealthyAccountClient


def account_rows(capacities=(1, 1, 1)):
    return [{"id": f"account_{i}", "label": f"Account {i + 1}", "server_url": "http://provider.example:8080",
             "username": f"private-user-{i}", "password": f"private-password/{i}", "capacity_override": value}
            for i, value in enumerate(capacities)]


def pool_environment(rows=None):
    return {"XTREAM_ENABLED": "true", "XTREAM_ACCOUNTS_JSON": json.dumps(rows if rows is not None else account_rows()),
            "XTREAM_TIMEZONE": "UTC"}


def hold_in_process(path, env, pipe):
    pool = XtreamPool(path, env, client_factory=HealthyAccountClient)
    try:
        lease = pool.acquire("100", "process")
        pipe.send(lease.id)
        pipe.recv()
        lease.release()
    except PoolUnavailable:
        pipe.send("full")


class AccountConfigTests(unittest.TestCase):
    def test_account_urls_replace_removed_global_server_setting(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE user_preferences(key TEXT PRIMARY KEY,value TEXT)")
        conn.execute("INSERT INTO user_preferences VALUES(?,?)",
                     ("setting:xtream_server_url", json.dumps("http://stale.example")))
        rows = account_rows((1, 1))
        rows[1]["server_url"] = "http://second-provider.example:8080"
        configs = load_metadata_configs(conn, pool_environment(rows))
        self.assertEqual([row["server_url"] for row in rows],
                         [config.server_url for config in configs])
        self.assertNotIn("xtream_server_url", load_all_settings(conn))
        self.assertNotIn("xtream_server_url", {item["key"] for item in get_settings_schema()})
        legacy = {"XTREAM_ENABLED": "true", "XTREAM_SERVER_URL": "http://legacy-env.example",
                  "XTREAM_USERNAME": "legacy-user", "XTREAM_PASSWORD": "legacy-secret"}
        self.assertEqual("http://legacy-env.example", load_config(conn, legacy).server_url)

    def test_metadata_candidates_include_degraded_and_try_unhealthy_last(self):
        env = pool_environment()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fruit.db"
            pool = XtreamPool(path, env, client_factory=HealthyAccountClient)
            with pool.connection() as conn:
                conn.execute("UPDATE xtream_account_state SET health='unhealthy' WHERE account_id='account_0'")
                conn.execute("UPDATE xtream_account_state SET health='degraded' WHERE account_id='account_1'")
                conn.execute("UPDATE xtream_account_state SET health='healthy' WHERE account_id='account_2'")
                configs = load_metadata_configs(conn, env)
            self.assertEqual(["private-user-2", "private-user-1", "private-user-0"],
                             [config.username for config in configs])

    def test_legacy_and_encoding(self):
        env = {"XTREAM_ENABLED": "true", "XTREAM_SERVER_URL": "http://provider.example",
               "XTREAM_USERNAME": "private user?#", "XTREAM_PASSWORD": "secret/pass&"}
        accounts = load_accounts(environ=env)
        self.assertEqual(["legacy"], [a.id for a in accounts])
        self.assertEqual("http://provider.example/live/private%20user%3F%23/secret%2Fpass%26/55%2F6.ts",
                         build_stream_url(accounts[0].config, "55/6"))
        self.assertNotIn("secret", repr(accounts))
        self.assertNotIn("private user", repr(accounts[0].config))

    def test_multiple_and_file_and_disabled(self):
        rows = account_rows((2, 1, 4))
        rows[0]["enabled"] = False
        env = pool_environment(rows)
        self.assertEqual(3, len(load_accounts(environ=env)))
        self.assertEqual(rows[1]["username"], load_config(environ=env).username)
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / "accounts.json"
            secret.write_text(json.dumps({"accounts": rows}))
            env.pop("XTREAM_ACCOUNTS_JSON")
            env["XTREAM_ACCOUNTS_FILE"] = str(secret)
            self.assertEqual(3, len(load_accounts(environ=env)))

    def test_mounted_account_json_is_used_without_legacy_compose_credentials(self):
        rows = account_rows((2, 1))
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / "xtream-accounts.json"
            secret.write_text(json.dumps({"accounts": rows}))
            env = {"XTREAM_ENABLED": "true", "XTREAM_TIMEZONE": "UTC",
                   "XTREAM_SERVER_URL": "http://legacy.example",
                   "XTREAM_USERNAME": "legacy-user", "XTREAM_PASSWORD": "legacy-password"}
            with patch("xtream_accounts.DEFAULT_ACCOUNTS_FILE", secret):
                accounts = load_accounts(environ=env)
                self.assertEqual(["account_0", "account_1"], [account.id for account in accounts])
                self.assertEqual(rows[0]["username"], load_config(environ=env).username)
                explicit = pool_environment(account_rows((1,)))
                self.assertEqual(1, len(load_accounts(environ=explicit)))

    def test_invalid_configuration_fails_closed_without_legacy_fallback(self):
        for value in ("bad json private-password", "{}", '[{"id":"dup"}]', json.dumps(account_rows() * 2)):
            with self.subTest(value=value), self.assertRaises(XtreamError) as error:
                load_accounts(environ={**pool_environment(), "XTREAM_ACCOUNTS_JSON": value})
            self.assertNotIn("private-password", str(error.exception))

    def test_duplicate_credentials_cannot_inflate_capacity(self):
        rows = account_rows((1, 1))
        rows[1].update(username=rows[0]["username"], password=rows[0]["password"])
        with self.assertRaises(XtreamError):
            load_accounts(environ=pool_environment(rows))

    def test_explicit_empty_pool_never_reactivates_legacy_credentials(self):
        env = {**pool_environment([]), 'XTREAM_SERVER_URL':'http://provider.example',
               'XTREAM_USERNAME':'legacy-user', 'XTREAM_PASSWORD':'legacy-password'}
        self.assertEqual([], load_accounts(environ=env))
        with self.assertRaises(XtreamError):
            load_config(environ=env)

    def test_invalid_capacity_and_embedded_server_credentials(self):
        for value in (0, -1, True, "1.2", "unlimited", 10001):
            rows = account_rows((value,))
            with self.subTest(value=value), self.assertRaises(XtreamError):
                load_accounts(environ=pool_environment(rows))
        rows = account_rows((1,))
        rows[0]["server_url"] = "http://user:pass@provider.example/"
        with self.assertRaises(XtreamError):
            load_accounts(environ=pool_environment(rows))

    def test_account_api_authentication_capacity_and_malformed_payloads(self):
        config = load_accounts(environ=pool_environment())[0].config
        for payload, maximum, health in [
            ({"user_info": {"auth": 1, "status": "Active", "max_connections": "4"}}, 4, "healthy"),
            ({"user_info": {"auth": 1, "max_connections": "0"}}, None, "healthy"),
            ({"user_info": {"auth": 1, "max_connections": "bad"}}, None, "healthy"),
            ({"user_info": {"auth": 0, "max_connections": "4"}}, None, "unhealthy"),
            ({"user_info": {"status": "Expired", "max_connections": "4"}}, None, "unhealthy"),
            ([], None, "unreachable"), ({"user_info": []}, None, "unreachable"),
        ]:
            with self.subTest(payload=payload):
                session = Mock()
                session.get.return_value.json.return_value = payload
                runner = Mock(return_value=Mock(returncode=0, stdout=json.dumps(payload)))
                client = XtreamClient(config, session=session, subprocess_runner=runner)
                self.assertEqual(maximum, client.get_account_max_connections())
                self.assertEqual(health, client.last_account_check["health"])

    def test_account_discovery_curl_fallback_keeps_secrets_off_process_arguments(self):
        config = load_accounts(environ=pool_environment())[0].config
        session = Mock()
        session.get.side_effect = OSError("provider unavailable")
        runner = Mock(return_value=Mock(returncode=0, stdout='{"user_info":{"auth":1,"max_connections":"4"}}'))
        client = XtreamClient(config, session=session, subprocess_runner=runner)
        self.assertEqual(4, client.get_account_max_connections())
        self.assertEqual("healthy", client.last_account_check["health"])
        arguments = " ".join(runner.call_args.args[0])
        self.assertNotIn(config.username, arguments)
        self.assertNotIn(config.password, arguments)
        self.assertIn(config.password, runner.call_args.kwargs["input"])

    def test_account_discovery_curl_fallback_after_requests_403(self):
        config = load_accounts(environ=pool_environment())[0].config
        session = Mock()
        response = session.get.return_value
        response.status_code = 403
        response.raise_for_status.side_effect = RuntimeError("403")
        runner = Mock(return_value=Mock(
            returncode=0,
            stdout='{"user_info":{"auth":1,"status":"Active","max_connections":"3"}}',
        ))
        client = XtreamClient(config, session=session, subprocess_runner=runner)

        self.assertEqual(3, client.get_account_max_connections())
        self.assertEqual("healthy", client.last_account_check["health"])
        self.assertEqual(1, runner.call_count)

    def test_account_discovery_retries_json_rejection_and_unconfirmed_auth(self):
        config = load_accounts(environ=pool_environment())[0].config
        for info in ({"auth": 0}, {"auth": "0", "status": "Active"},
                     {"status": "Expired"}, {"max_connections": "4"}):
            with self.subTest(info=info):
                session = Mock()
                session.get.return_value.json.return_value = {"user_info": info}
                runner = Mock(return_value=Mock(returncode=0, stdout=json.dumps({
                    "user_info": {"auth": 1, "status": "Active", "max_connections": "3"},
                })))
                client = XtreamClient(config, session=session, subprocess_runner=runner)
                self.assertEqual(3, client.get_account_max_connections())
                self.assertEqual("healthy", client.last_account_check["health"])
                self.assertEqual(1, runner.call_count)

    def test_account_discovery_preserves_rejection_if_curl_cannot_authorize(self):
        config = load_accounts(environ=pool_environment())[0].config
        for completed in (
            Mock(returncode=0, stdout='{"user_info":{"auth":0}}'),
            Mock(returncode=0, stdout='{"user_info":{"max_connections":"4"}}'),
            Mock(returncode=0, stdout='[]'),
            Mock(returncode=0, stdout='invalid JSON private-password'),
            Mock(returncode=28, stderr='private-password'),
        ):
            with self.subTest(completed=completed):
                session = Mock()
                session.get.return_value.json.return_value = {"user_info": {"auth": 0}}
                runner = Mock(return_value=completed)
                client = XtreamClient(config, session=session, subprocess_runner=runner)
                self.assertIsNone(client.get_account_max_connections())
                self.assertEqual("unhealthy", client.last_account_check["health"])
                self.assertEqual(1, runner.call_count)
                self.assertNotIn("private-password", json.dumps(client.last_account_check))

    def test_account_discovery_success_does_not_retry_even_with_unknown_limit(self):
        config = load_accounts(environ=pool_environment())[0].config
        session = Mock()
        session.get.return_value.json.return_value = {"user_info": {"auth": 1}}
        runner = Mock()
        client = XtreamClient(config, session=session, subprocess_runner=runner)
        self.assertIsNone(client.get_account_max_connections())
        self.assertEqual("healthy", client.last_account_check["health"])
        runner.assert_not_called()

    def test_account_check_reports_safe_http_and_curl_failures(self):
        config = load_accounts(environ=pool_environment())[0].config
        session = Mock()
        session.get.side_effect = requests.Timeout("http://private-user:private-password@provider.example")
        runner = Mock(return_value=Mock(returncode=28, stdout="", stderr="private-password"))
        client = XtreamClient(config, session=session, subprocess_runner=runner)
        self.assertIsNone(client.get_account_max_connections())
        check = client.last_account_check
        self.assertEqual("unreachable", check["health"])
        self.assertEqual("Provider HTTP request timed out; Provider curl request timed out", check["error"])
        self.assertNotIn("private", check["error"])


class PoolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "fruit.db"
        self.env = pool_environment()
        self.pool = XtreamPool(self.path, self.env, client_factory=HealthyAccountClient)
        self.pool.check_accounts()

    def test_three_accounts_fourth_rejected_release_reuse_and_safe_status(self):
        leases = [self.pool.acquire(str(i), "persistent") for i in range(3)]
        try:
            self.assertEqual(3, len({lease.account.id for lease in leases}))
            with self.assertRaises(PoolUnavailable):
                self.pool.acquire("4", "lane")
            status = self.pool.status()
            self.assertEqual((3, 3, 0), (status["capacity"], status["active"], status["available"]))
            serialized = json.dumps(status)
            self.assertNotIn("private-user", serialized)
            self.assertNotIn("private-password", serialized)
            self.assertNotIn("server_url", serialized)
            leases[0].release()
            leases[0].release()
            new = self.pool.acquire("4", "lane")
            new.release()
        finally:
            for lease in leases:
                lease.release()
        self.assertEqual(0, self.pool.status()["active"])
        self.assertEqual([], list(self.pool.lock_dir.iterdir()))

    def test_sequential_probes_rotate_least_recently_used_accounts(self):
        selected = []
        for stream_id in range(6):
            lease = self.pool.acquire(str(stream_id), "quality_probe")
            selected.append(lease.account.id)
            lease.release("client_closed")
        self.assertEqual(
            ["account_0", "account_1", "account_2", "account_0", "account_1", "account_2"],
            selected,
        )

    def test_race_across_independent_pool_instances(self):
        barrier = threading.Barrier(20)
        def allocate(index):
            pool = XtreamPool(self.path, self.env, client_factory=HealthyAccountClient)
            barrier.wait()
            try:
                return pool.acquire(str(index), "race")
            except PoolUnavailable:
                return None
        with ThreadPoolExecutor(max_workers=20) as executor:
            leases = [lease for lease in executor.map(allocate, range(20)) if lease]
        try:
            self.assertEqual(3, len(leases))
            self.assertEqual(3, len({lease.account.id for lease in leases}))
        finally:
            for lease in leases:
                lease.release()

    def test_different_capacities_disabled_and_override_survive_checks(self):
        env = pool_environment(account_rows((2, 1, 4)))
        pool = XtreamPool(self.path, env, client_factory=HealthyAccountClient)
        self.assertEqual(7, pool.check_accounts()["capacity"])
        pool.update("account_1", {"enabled": False})
        self.assertEqual(6, pool.status()["capacity"])
        pool.update("account_0", {"capacity_override": 3, "label": "Recorder"})
        status = pool.check_accounts()
        self.assertEqual(7, status["capacity"])
        self.assertEqual(3, status["accounts"][0]["configured_override"])
        self.assertEqual(1, status["accounts"][0]["discovered_capacity"])

    def test_discovered_limit_and_conservative_unknown_limit(self):
        rows = account_rows((None,))
        pool = XtreamPool(self.path, pool_environment(rows), client_factory=HealthyAccountClient)
        self.assertEqual("discovered", pool.check_accounts()["accounts"][0]["capacity_source"])
        class UnknownLimit(HealthyAccountClient):
            def get_account_max_connections(self):
                return None
        other = XtreamPool(Path(self.temp.name) / "unknown.db", pool_environment(rows), client_factory=UnknownLimit)
        self.assertEqual((1, "conservative_default"), (other.check_accounts()["capacity"], other.status()["accounts"][0]["capacity_source"]))

    def test_bad_account_skipped_healthy_accounts_remain(self):
        class MixedClient(HealthyAccountClient):
            def get_account_max_connections(self):
                if self.config.username.endswith("-0"):
                    self.last_account_check = {"health": "unhealthy", "error": "password must not escape"}
                    return None
                return 1
        self.pool.client_factory = MixedClient
        self.assertEqual(2, self.pool.check_accounts()["capacity"])
        lease = self.pool.acquire("1", "test")
        self.assertNotEqual("account_0", lease.account.id)
        lease.release()

    def test_transient_failure_recovers_and_does_not_permanently_disable(self):
        self.pool.fail_account("account_0")
        self.assertEqual("degraded", self.pool.status()["accounts"][0]["health"])
        self.assertEqual(2, self.pool.status()["available"])
        self.assertEqual(3, self.pool.check_accounts()["available"])

    def test_disabling_or_lowering_capacity_does_not_kill_existing_stream(self):
        lease = self.pool.acquire("1", "test")
        self.pool.update(lease.account.id, {"enabled": False})
        self.assertEqual(1, self.pool.status()["active"])
        lease.release()
        self.assertEqual(0, self.pool.status()["active"])

    def test_restart_reclaims_crashed_process_but_keeps_live_worker(self):
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        process = context.Process(target=hold_in_process, args=(str(self.path), self.env, child))
        process.start()
        self.addCleanup(parent.close)
        self.addCleanup(child.close)
        try:
            self.assertTrue(parent.poll(15))
            self.assertNotEqual("full", parent.recv())
            self.assertEqual(1, XtreamPool(self.path, self.env).status()["active"])
            process.kill()
            process.join(10)
            self.assertEqual(0, XtreamPool(self.path, self.env).status()["active"])
            self.assertEqual("worker_stopped", self.pool.status()["recent_streams"][0]["outcome"])
        finally:
            if process.is_alive():
                process.kill()
            process.join(10)

    def test_scheduler_reads_derived_capacity_without_overwriting_legacy_configuration(self):
        with sqlite3.connect(self.path) as conn, patch.dict(os.environ, self.env):
            conn.execute("CREATE TABLE provider_capacities(provider TEXT,max_concurrent INTEGER)")
            conn.execute("INSERT INTO provider_capacities VALUES('xtream',1)")
            self.assertEqual(3, scheduler_capacity(conn))
            self.assertEqual(1, conn.execute("SELECT max_concurrent FROM provider_capacities").fetchone()[0])

    def test_changed_credentials_invalidate_health_but_do_not_erase_controls(self):
        self.pool.update("account_0", {"capacity_override": 2})
        rows = account_rows()
        rows[0]["password"] = "replacement-secret"
        state = XtreamPool(self.path, pool_environment(rows)).status()["accounts"][0]
        self.assertEqual("unknown", state["health"])
        self.assertEqual(2, state["configured_override"])

    def test_long_recording_never_expires_from_age_alone(self):
        lease = self.pool.acquire("1", "recording")
        try:
            with sqlite3.connect(self.path) as conn:
                conn.execute("UPDATE xtream_leases SET started=started-86400")
            state = XtreamPool(self.path, self.env).status()
            self.assertEqual(1, state["active"])
            self.assertGreaterEqual(state["leases"][0]["age_seconds"], 86400)
        finally:
            lease.release()

    def test_release_keeps_capacity_reserved_until_media_child_exits(self):
        lease = self.pool.acquire("1", "recording")
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            pass_fds=(lease.fd,),
        )
        try:
            lease.release()
            self.assertEqual(1, self.pool.status()["active"])
            other_accounts = {account.id for account in self.pool.accounts if account.id != lease.account.id}
            with self.assertRaises(PoolUnavailable):
                self.pool.acquire("2", "retry", excluded=other_accounts)
            child.terminate()
            child.wait(timeout=5)
            self.assertEqual(0, self.pool.status()["active"])
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)

    def test_renaming_account_id_during_live_stream_does_not_duplicate_capacity(self):
        lease = self.pool.acquire("1", "recording")
        rows = account_rows((1,))
        rows[0]["id"] = "renamed_account"
        other = XtreamPool(self.path, pool_environment(rows), client_factory=HealthyAccountClient)
        try:
            state = other.check_accounts()
            self.assertEqual((1, 1, 0), (state["capacity"], state["active"], state["available"]))
            with self.assertRaises(PoolUnavailable):
                other.acquire("2", "retry")
        finally:
            lease.release()
        other.acquire("2", "retry").release()


if __name__ == "__main__":
    unittest.main()
