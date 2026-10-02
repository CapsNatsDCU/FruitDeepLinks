import copy
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from server.app import create_app
from server import refresh
from update_protocol import read_json, write_json
from xsort_updater import Updater, UpdateError


class UpdaterRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.database = self.root / "app.db"
        with sqlite3.connect(self.database) as conn:
            conn.execute("CREATE TABLE user_preferences (key TEXT PRIMARY KEY, value TEXT, updated_utc TEXT)")
        self.env = patch.dict(os.environ, {"XSORT_UPDATE_DIR": str(self.root), "FRUIT_DB_PATH": str(self.database)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client = create_app().test_client()
        self.headers = {"Origin": "http://localhost", "X-Xsort-Update": "1"}
        self.root.joinpath("heartbeat").touch()
        refresh.refresh_status["running"] = False
        self.addCleanup(refresh.refresh_status.update, running=False)
        self.state(phase="available", can_install=True, available_revision="a" * 40)

    def state(self, **state):
        write_json(self.root / "status.json", state)

    def post(self, action, payload=None, headers=None):
        return self.client.post("/api/updates/" + action, json=payload or {},
                                headers=self.headers if headers is None else headers)

    def test_get_only_reads_and_offline_is_truthful(self):
        self.assertTrue(self.client.get("/api/updates").json["online"])
        self.assertFalse(self.root.joinpath("request.json").exists())
        os.utime(self.root / "heartbeat", (0, 0))
        self.assertFalse(self.client.get("/api/updates").json["online"])
        self.assertEqual(503, self.post("check").status_code)

    def test_unconfigured_installation_shows_setup(self):
        with patch.dict(os.environ, {"XSORT_UPDATE_DIR": ""}):
            self.assertFalse(self.client.get("/api/updates").json["enabled"])
            self.assertEqual(503, self.post("check").status_code)

    def test_cross_origin_and_missing_header_cannot_queue_commands(self):
        for headers in ({}, {"X-Xsort-Update": "1", "Origin": "https://other.example"},
                        {"X-Xsort-Update": "1", "Origin": "null"}):
            self.assertEqual(403, self.post("check", headers=headers).status_code)
        self.assertFalse(self.root.joinpath("request.json").exists())

    def test_check_is_queued_once(self):
        self.assertEqual(202, self.post("check").status_code)
        self.assertEqual("check", read_json(self.root / "request.json")["action"])
        self.assertEqual(409, self.post("check").status_code)

    def test_install_requires_reviewed_revision_and_idle_refresh(self):
        for revision in ("b" * 40, "--help", "", 3):
            self.assertEqual(409, self.post("install", {"revision": revision}).status_code)
        refresh.refresh_status["running"] = True
        self.assertEqual(409, self.post("install", {"revision": "a" * 40}).status_code)
        refresh.refresh_status["running"] = False
        self.assertEqual(202, self.post("install", {"revision": "a" * 40}).status_code)
        self.assertEqual("a" * 40, read_json(self.root / "request.json")["revision"])

    def test_invalid_action_and_body_rejected(self):
        self.assertEqual(400, self.post("shell").status_code)
        self.assertEqual(400, self.client.post("/api/updates/check", json=[1], headers=self.headers).status_code)

    def test_installation_pauses_manual_writes_and_scheduled_refresh(self):
        self.state(phase="building")
        self.assertEqual(503, self.client.post("/api/settings", json={"num_lanes": 5}).status_code)
        with patch("server.refresh.subprocess.Popen") as popen:
            refresh.run_refresh(source="auto")
            refresh.run_apply_filters()
            refresh.run_ai_failure_retry()
        popen.assert_not_called()
        self.assertFalse(refresh.refresh_status["running"])
        # Stale heartbeat must not permanently lock the app after a host crash.
        os.utime(self.root / "heartbeat", (0, 0))
        self.assertEqual(200, self.client.post("/api/settings", json={"num_lanes": 5}).status_code)

    def test_settings_panel_and_script_are_served(self):
        response = self.client.get("/settings")
        self.assertEqual(200, response.status_code)
        self.assertIn(b'id="app-updates"', response.data)
        self.assertIn(b'updates.js', response.data)
        with self.client.get("/static/updates.js") as script:
            self.assertEqual(200, script.status_code)


class HostUpdaterTests(unittest.TestCase):
    """Use real Git repositories, with Docker replaced at its process boundary."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.remote = self.root / "remote"
        self.repo = self.root / "deployment"
        self.remote.mkdir()
        self.command(self.remote, "init", "-b", "updates")
        self.command(self.remote, "config", "user.name", "Updater Test")
        self.command(self.remote, "config", "user.email", "updater@example.invalid")
        (self.remote / ".gitignore").write_text(".xsort-updater/\n")
        (self.remote / "app.txt").write_text("old app")
        self.command(self.remote, "add", ".")
        self.command(self.remote, "commit", "-m", "Initial app")
        self.old = self.command(self.remote, "rev-parse", "HEAD")
        self.command(self.root, "clone", str(self.remote), str(self.repo))
        (self.remote / "app.txt").write_text("updated app")
        self.command(self.remote, "commit", "-am", "Update app")
        self.new = self.command(self.remote, "rev-parse", "HEAD")
        self.updater = Updater(self.repo, ["docker-compose.yml", "docker-compose.updater.yml"])
        self.running = self.old
        self.config = {"name": "test-app", "services": {"fruitdeeplinks": {
            "build": {"context": str(self.repo), "dockerfile": "Dockerfile"},
            "environment": {"PRIVATE_TOKEN": "do-not-publish$literal"},
            "volumes": [{"type": "bind", "source": str(self.repo / ".xsort-updater/control"), "target": "/run/xsort-updater"}]
                       + [{"type": "bind", "source": str(self.repo / name), "target": f"/app/{name}"}
                          for name in ("data", "out", "logs")],
        }}}
        self.commands = []
        self.fail_build = False
        self.fail_health = False
        self.fail_rollback = False
        self.fail_backup = False
        self.busy = False
        self.real_run = self.updater.run
        self.patcher = patch.object(self.updater, "run", side_effect=self.fake_run)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def command(self, cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, check=True, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()

    def fake_run(self, args, **kwargs):
        if args[0] == "git":
            return self.real_run(args, **kwargs)
        self.commands.append(args)
        if args[1] == "inspect":
            return json.dumps([{"Id": "container-id", "Image": "sha256:" + "1" * 64,
                                "State": {"Running": True},
                                "Mounts": [{"Destination": "/run/xsort-updater", "Source": str(self.updater.control)}],
                                "Config": {"Labels": {"org.opencontainers.image.revision": self.running}}}])
        if "config" in args:
            return json.dumps(self.config)
        if "ps" in args:
            return "container-id"
        if "exec" in args:
            if "s.backup(t)" in args[-1]:
                if self.fail_backup:
                    raise UpdateError("Database backup failed. The app has not been restarted.")
                return ""
            return json.dumps({"busy": self.busy})
        if "build" in args and self.fail_build:
            raise UpdateError("Build failed; the running app was left in place.")
        if "up" in args:
            rolling_back = any(a.endswith("rollback.json") for a in args)
            if (rolling_back and self.fail_rollback) or (not rolling_back and self.fail_health):
                raise UpdateError("Health check failed")
            self.running = self.old if rolling_back else self.new
        return ""

    def request(self, action="install", revision=None):
        self.updater.handle({"action": action, "revision": revision or self.new, "requested_at": time.time()})
        return read_json(self.updater.control / "status.json")

    def test_check_fetches_branch_without_changing_checkout(self):
        state = self.request("check")
        self.assertTrue(state["can_install"])
        self.assertEqual(self.new, state["available_revision"])
        self.assertEqual(self.old, self.command(self.repo, "rev-parse", "HEAD"))
        self.assertEqual(1, len(state["commits"]))
        self.assertNotIn("do-not-publish", json.dumps(state))

    def test_same_revision_is_current(self):
        self.running = self.new
        state = self.request("check")
        self.assertEqual("current", state["phase"])
        self.assertFalse(state["can_install"])

    def test_unknown_image_revision_is_explicit(self):
        self.running = "unknown"
        state = self.request("check")
        self.assertIn("unknown", state["message"])
        self.assertTrue(state["can_install"])

    def test_local_changes_are_never_overwritten(self):
        (self.repo / "app.txt").write_text("my local changes")
        state = self.request()
        self.assertEqual("error", state["phase"])
        self.assertIn("local changes", state["message"])
        self.assertEqual("my local changes", (self.repo / "app.txt").read_text())
        self.assertEqual([], self.commands)

    def test_diverged_checkout_is_not_reset(self):
        self.command(self.repo, "config", "user.name", "Updater Test")
        self.command(self.repo, "config", "user.email", "updater@example.invalid")
        (self.repo / "local.txt").write_text("local commit")
        self.command(self.repo, "add", ".")
        self.command(self.repo, "commit", "-m", "Local work")
        local = self.command(self.repo, "rev-parse", "HEAD")
        state = self.request()
        self.assertIn("diverged", state["message"])
        self.assertEqual(local, self.command(self.repo, "rev-parse", "HEAD"))

    def test_stale_approval_never_installs_new_commit(self):
        state = self.request(revision=self.old)
        self.assertIn("changed", state["message"])
        self.assertFalse(any("up" in cmd or "build" in cmd for cmd in self.commands))
        self.assertEqual(self.old, self.command(self.repo, "rev-parse", "HEAD"))

    def test_success_backs_up_then_restarts_only_selected_service(self):
        state = self.request()
        self.assertEqual("success", state["phase"])
        self.assertEqual(self.new, self.running)
        self.assertEqual(self.new, self.command(self.repo, "rev-parse", "HEAD"))
        up = next(c for c in self.commands if "up" in c)
        self.assertEqual("fruitdeeplinks", up[-1])
        self.assertIn("--wait", up)
        self.assertIn("--no-deps", up)
        backup_index = next(i for i, c in enumerate(self.commands) if "s.backup(t)" in c[-1])
        self.assertLess(backup_index, self.commands.index(up))
        candidate = read_json(self.updater.private / "candidate.json")
        self.assertEqual(self.new, candidate["services"]["fruitdeeplinks"]["build"]["args"]["FDL_BUILD_REVISION"])
        self.assertEqual("do-not-publish$$literal", candidate["services"]["fruitdeeplinks"]["environment"]["PRIVATE_TOKEN"])
        self.assertEqual(0o600, (self.updater.private / "candidate.json").stat().st_mode & 0o777)
        self.assertEqual(0o700, self.updater.private.stat().st_mode & 0o777)

    def test_build_failure_keeps_running_container_and_can_be_retried(self):
        self.fail_build = True
        state = self.request()
        self.assertEqual("error", state["phase"])
        self.assertEqual(self.old, self.running)
        self.assertFalse(any("up" in cmd for cmd in self.commands))
        self.fail_build = False
        self.assertEqual("success", self.request()["phase"])

    def test_backup_failure_never_restarts(self):
        self.fail_backup = True
        state = self.request()
        self.assertIn("backup failed", state["message"])
        self.assertFalse(any("up" in cmd for cmd in self.commands))

    def test_health_failure_restores_previous_image(self):
        self.fail_health = True
        state = self.request()
        self.assertEqual("error", state["phase"])
        self.assertIn("previous image is running", state["message"])
        self.assertEqual(self.old, self.running)
        self.assertEqual(2, sum("up" in cmd for cmd in self.commands))

    def test_recovery_failure_never_claims_success(self):
        self.fail_health = self.fail_rollback = True
        state = self.request()
        self.assertEqual("error", state["phase"])
        self.assertIn("recovery failed", state["message"])

    def test_busy_app_blocks_before_git_merge(self):
        self.busy = True
        self.assertIn("refresh is running", self.request()["message"])
        self.assertEqual(self.old, self.command(self.repo, "rev-parse", "HEAD"))

    def test_development_and_nonpersistent_installs_are_blocked(self):
        original = copy.deepcopy(self.config)
        for target in ("/app/bin", "/app/templates", "/app/VERSION"):
            self.config = copy.deepcopy(original)
            self.config["services"]["fruitdeeplinks"]["volumes"].append({"target": target})
            self.assertIn("development", self.request("check")["message"])
        self.config = copy.deepcopy(original)
        self.config["services"]["fruitdeeplinks"]["volumes"].pop()
        self.assertIn("persistent", self.request("check")["message"])

    def test_expired_and_arbitrary_requests_do_not_run(self):
        self.updater.handle({"action": "install", "revision": self.new, "requested_at": 1})
        self.assertIn("expired", read_json(self.updater.control / "status.json")["message"])
        self.assertEqual([], self.commands)
        self.assertIn("Invalid", self.request("shell")["message"])


if __name__ == "__main__":
    unittest.main()
