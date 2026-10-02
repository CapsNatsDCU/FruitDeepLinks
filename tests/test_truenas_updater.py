import copy
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from update_protocol import read_json
from xsort_truenas_updater import TrueNASUpdater, local_api
from xsort_updater import UpdateError


class TrueNASUpdaterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.remote, self.repo = self.root / "remote", self.root / "checkout"
        self.remote.mkdir()
        self.git(self.remote, "init", "-b", "updates")
        self.git(self.remote, "config", "user.name", "Updater Test")
        self.git(self.remote, "config", "user.email", "updater@example.invalid")
        (self.remote / ".gitignore").write_text(".xsort-updater/\n")
        (self.remote / "app.txt").write_text("old")
        self.git(self.remote, "add", ".")
        self.git(self.remote, "commit", "-m", "Old app")
        self.old = self.git(self.remote, "rev-parse", "HEAD")
        self.git(self.root, "clone", str(self.remote), str(self.repo))
        (self.remote / "app.txt").write_text("new")
        self.git(self.remote, "commit", "-am", "New app")
        self.new = self.git(self.remote, "rev-parse", "HEAD")
        self.updater = TrueNASUpdater(self.repo, "xsort", branch="updates", api=self.api)
        self.config = {"services": {"fruitdeeplinks": {
            "build": {"context": "https://github.com/CapsNatsDCU/FruitDeepLinks.git#old"},
            "ports": ["6655:6655"], "restart": "unless-stopped",
            "environment": ["XTREAM_ACCOUNTS_FILE=/run/secrets/accounts.json", "SECRET=literal$$secret"],
            "volumes": ["/mnt/Apps/fruit/data:/app/data", "/mnt/Apps/fruit/out:/app/out",
                        "/mnt/Apps/fruit/logs:/app/logs", "/mnt/Apps/fruit/secrets:/run/secrets:ro"],
        }}, "networks": {"default": {"driver": "bridge"}}}
        self.original = copy.deepcopy(self.config)
        self.running = self.old
        self.image_id = "sha256:" + "1" * 64
        self.new_image_id = "sha256:" + "2" * 64
        self.initial_image_id = self.image_id
        self.mailbox = False
        self.busy = False
        self.custom = True
        self.fail_build = self.fail_backup = self.fail_health = self.fail_rollback = False
        self.change_during_build = False
        self.calls = []
        self.tasks = []
        self.real_run = self.updater.run
        self.patch_run = patch.object(self.updater, "run", side_effect=self.fake_run)
        self.patch_run.start()
        self.addCleanup(self.patch_run.stop)
        self.patch_health = patch.object(self.updater, "wait_healthy", side_effect=self.health)
        self.patch_health.start()
        self.addCleanup(self.patch_health.stop)

    def git(self, cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, check=True, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()

    def container(self):
        mounts = [{"Destination": f"/app/{part}", "Source": f"/mnt/Apps/fruit/{part}", "Type": "bind", "RW": True}
                  for part in ("data", "out", "logs")]
        mounts.append({"Destination": "/run/secrets", "Source": "/mnt/Apps/fruit/secrets", "Type": "bind", "RW": False})
        if self.mailbox:
            mounts.append({"Destination": "/run/xsort-updater", "Source": str(self.updater.control), "Type": "bind", "RW": True})
        return {"Id": self.image_id, "Image": self.image_id, "State": {"Running": True, "Health": {"Status": "healthy"}},
                "Mounts": mounts, "Config": {"Labels": {"org.opencontainers.image.revision": self.running}}}

    def fake_run(self, args, **kwargs):
        if args[0] == "git":
            return self.real_run(args, **kwargs)
        self.calls.append(copy.deepcopy(args))
        if args[:2] == ["docker", "ps"]:
            return "container-id"
        if args[:2] == ["docker", "inspect"]:
            return json.dumps([self.container()])
        if args[:3] == ["docker", "image", "inspect"]:
            return json.dumps([{"Id": self.new_image_id, "Config": {"Labels": {"org.opencontainers.image.revision": self.new}}}])
        if args[:2] == ["docker", "exec"]:
            if "s.backup(t)" in args[-1]:
                if self.fail_backup:
                    raise UpdateError("Database backup failed.")
                return ""
            return json.dumps({"busy": self.busy})
        if args[:2] == ["docker", "build"]:
            if self.fail_build:
                raise UpdateError("Build failed.")
            if self.change_during_build:
                self.config["services"]["fruitdeeplinks"]["ports"] = ["7777:6655"]
        return ""

    def api(self, method, *args, **kwargs):
        self.calls.append([method, *copy.deepcopy(args)])
        if method == "app.get_instance":
            return {"custom_app": self.custom}
        if method == "app.config":
            return copy.deepcopy(self.config)
        if method == "app.update":
            self.assertTrue(kwargs["job"])
            self.assertEqual("xsort", args[0])
            self.config = copy.deepcopy(args[1]["custom_compose_config"])
            rollback = self.config["services"]["fruitdeeplinks"]["image"].startswith("xsort-update-backup:")
            if rollback and self.fail_rollback:
                raise UpdateError("Rollback failed.")
            self.running = self.old if rollback else self.new
            self.image_id = self.initial_image_id if rollback else self.new_image_id
            self.mailbox = not rollback
            return {}
        if method == "initshutdownscript.query":
            return copy.deepcopy(self.tasks)
        if method == "initshutdownscript.create":
            self.tasks = [{"id": 7, **args[0]}]
        if method == "initshutdownscript.update":
            self.tasks = [{"id": args[0], **args[1]}]

    def health(self, image_id):
        if self.fail_health and image_id == self.new_image_id:
            raise UpdateError("Health failed.")
        self.assertEqual(image_id, self.image_id)
        return self.container()

    def test_one_time_setup_preserves_config_and_registers_persistent_helper(self):
        self.updater.setup()
        service = self.config["services"]["fruitdeeplinks"]
        self.assertNotIn("build", service)
        self.assertEqual("never", service["pull_policy"])
        self.assertEqual(f"xsort-xsort:{self.new}", service["image"])
        self.assertEqual(self.original["networks"], self.config["networks"])
        for name in ("ports", "restart"):
            self.assertEqual(self.original["services"]["fruitdeeplinks"][name], service[name])
        for mount in self.original["services"]["fruitdeeplinks"]["volumes"]:
            self.assertIn(mount, service["volumes"])
        self.assertIn("SECRET=literal$$secret", service["environment"])
        self.assertIn("XSORT_UPDATE_DIR=/run/xsort-updater", service["environment"])
        self.assertEqual("POSTINIT", self.tasks[0]["when"])
        self.assertIn("--property=Restart=always", self.tasks[0]["command"])
        self.assertTrue(any(c[0] == "systemd-run" for c in self.calls))
        backup_index = next(i for i, c in enumerate(self.calls) if c[0] == "docker" and "s.backup(t)" in str(c[-1]))
        update_index = next(i for i, c in enumerate(self.calls) if c[0] == "app.update")
        self.assertLess(backup_index, update_index)
        self.assertEqual(0o600, (self.updater.private / "truenas-original.json").stat().st_mode & 0o777)
        self.assertEqual(self.original, read_json(self.updater.private / "truenas-original.json"))
        self.assertNotIn("secret", json.dumps(read_json(self.updater.control / "status.json")).lower())

    def test_second_update_uses_buttons_without_manual_yaml_changes(self):
        self.updater.setup()
        (self.remote / "app.txt").write_text("third")
        self.git(self.remote, "commit", "-am", "Third app")
        self.new = self.git(self.remote, "rev-parse", "HEAD")
        self.new_image_id = "sha256:" + "3" * 64
        state = self.updater.check()
        self.assertTrue(state["can_install"])
        self.updater.install(state["available_revision"])
        self.assertEqual(self.new, self.running)
        self.assertEqual(1, len(self.tasks))
        self.assertEqual(2, sum(c[0] == "app.update" for c in self.calls))

    def test_unconfigured_normal_check_requires_setup(self):
        with self.assertRaisesRegex(UpdateError, "one-time"):
            self.updater.check()
        self.assertFalse(any(c[0] == "app.update" for c in self.calls))

    def test_failures_before_restart_do_not_write_truenas_configuration(self):
        self.updater.setup_mode = True
        for flag in ("busy", "fail_build", "fail_backup", "change_during_build"):
            self.calls.clear()
            setattr(self, flag, True)
            with self.assertRaises(UpdateError):
                self.updater.install(self.new)
            self.assertFalse(any(c[0] == "app.update" for c in self.calls), flag)
            self.assertEqual(self.old, self.running)
            setattr(self, flag, False)

    def test_health_failure_restores_previous_image_through_truenas(self):
        self.updater.setup_mode = True
        self.fail_health = True
        with self.assertRaisesRegex(UpdateError, "previous image is healthy"):
            self.updater.install(self.new)
        self.assertEqual(self.old, self.running)
        self.assertEqual(2, sum(c[0] == "app.update" for c in self.calls))
        self.assertEqual(self.original["services"]["fruitdeeplinks"]["volumes"], self.config["services"]["fruitdeeplinks"]["volumes"])

    def test_failed_recovery_does_not_claim_success(self):
        self.updater.setup_mode = True
        self.fail_health = self.fail_rollback = True
        with self.assertRaisesRegex(UpdateError, "recovery failed"):
            self.updater.install(self.new)

    def test_multi_service_and_catalog_apps_are_rejected(self):
        self.updater.setup_mode = True
        self.custom = False
        with self.assertRaisesRegex(UpdateError, "Custom App"):
            self.updater.check()
        self.custom = True
        self.config["services"]["other"] = {"image": "other"}
        with self.assertRaisesRegex(UpdateError, "only FruitDeepLinks"):
            self.updater.check()

    def test_stale_revision_and_dirty_checkout_do_not_install(self):
        self.updater.setup_mode = True
        with self.assertRaisesRegex(UpdateError, "revision changed"):
            self.updater.install(self.old)
        (self.repo / "app.txt").write_text("local work")
        with self.assertRaisesRegex(UpdateError, "local changes"):
            self.updater.install(self.new)
        self.assertFalse(any(c[0] == "app.update" for c in self.calls))

    def test_mapping_environment_and_labels_preserve_existing_values(self):
        service = self.config["services"]["fruitdeeplinks"]
        service["environment"] = {"SECRET": "literal$$value"}
        service["labels"] = ["custom=value", "org.opencontainers.image.revision=old"]
        candidate = self.updater.image_config(self.config, "image", self.new, connect=True)
        updated = candidate["services"]["fruitdeeplinks"]
        self.assertEqual("literal$$value", updated["environment"]["SECRET"])
        self.assertEqual(["custom=value", f"org.opencontainers.image.revision={self.new}"], updated["labels"])
        self.assertNotIn("XSORT_UPDATE_DIR", service["environment"])

    def test_startup_task_is_updated_in_place(self):
        self.updater.start_at_boot()
        self.updater.start_at_boot()
        self.assertEqual(1, sum(c[0] == "initshutdownscript.create" for c in self.calls))
        self.assertEqual(1, sum(c[0] == "initshutdownscript.update" for c in self.calls))

    def test_setup_does_not_race_an_existing_helper(self):
        with self.updater.setup_lock():
            with self.assertRaisesRegex(UpdateError, "already running"):
                self.updater.setup()
        self.assertFalse(any(c[0] == "app.update" for c in self.calls))

    def test_successful_install_exits_for_service_manager_to_reload_helper(self):
        self.mailbox = True
        with self.assertRaises(SystemExit) as result:
            self.updater.handle({"action": "install", "revision": self.new, "requested_at": time.time()})
        self.assertEqual(0, result.exception.code)

    def test_checkout_cannot_be_reused_for_another_truenas_app(self):
        self.updater.private_config("truenas-target.json", {"app": "xsort"})
        with self.assertRaisesRegex(UpdateError, "different TrueNAS app"):
            TrueNASUpdater(self.repo, "other-app", branch="updates", api=self.api)

    def test_custom_build_options_are_not_silently_discarded(self):
        self.updater.setup_mode = True
        for extra in ({"dockerfile": "Customfile"}, {"target": "alternate"}, {"args": {"PRIVATE_TOKEN": "secret"}}):
            self.config["services"]["fruitdeeplinks"]["build"] = {"context": ".", **extra}
            with self.assertRaisesRegex(UpdateError, "custom build options"):
                self.updater.check()

    def test_read_only_or_wrong_mailbox_and_code_mount_are_rejected(self):
        self.updater.setup_mode = True
        container = self.container()
        invalid_mounts = (
            {"Destination": "/run/xsort-updater", "Source": str(self.updater.control), "RW": False},
            {"Destination": "/run/xsort-updater", "Source": "/another-helper", "RW": True},
            {"Destination": "/app/bin/server/app.py", "Source": "/code", "RW": False},
        )
        for mount in invalid_mounts:
            with patch.object(self.updater, "container", return_value={**container, "Mounts": [*container["Mounts"], mount]}):
                with self.assertRaises(UpdateError):
                    self.updater.check()

    def test_health_wait_checks_exact_image_and_health_state(self):
        self.patch_health.stop()
        self.assertEqual(self.container(), self.updater.wait_healthy(self.image_id))
        with patch("xsort_truenas_updater.time.monotonic", side_effect=[0, 0, 151]), patch("xsort_truenas_updater.time.sleep"):
            with self.assertRaisesRegex(UpdateError, "did not become healthy"):
                self.updater.wait_healthy("sha256:wrong-image")

    def test_native_api_failure_does_not_expose_configuration(self):
        client_class = Mock()
        client_class.return_value.__enter__ = Mock(side_effect=RuntimeError("private token from config"))
        with patch.dict(sys.modules, {"truenas_api_client": Mock(Client=client_class)}):
            with self.assertRaises(UpdateError) as error:
                local_api("app.config", "xsort")
        self.assertNotIn("private token", str(error.exception))

    def test_manual_recovery_is_bound_to_app_and_restores_native_config(self):
        self.updater.private_config("truenas-target.json", {"app": "xsort"})
        rollback = self.updater.image_config(self.config, "xsort-update-backup:" + "1" * 16)
        self.updater.private_config("truenas-rollback.json", rollback)
        # The retained image must be inspected, not assumed to match a label.
        with patch.object(self.updater, "wait_healthy") as health:
            self.updater.recover()
            health.assert_called_once_with(self.new_image_id)
        self.assertEqual(rollback, self.config)
        self.assertEqual(1, sum(c[0] == "app.update" for c in self.calls))


if __name__ == "__main__":
    unittest.main()
