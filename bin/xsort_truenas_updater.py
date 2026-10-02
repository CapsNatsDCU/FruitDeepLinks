#!/usr/bin/env python3
"""One-time setup and in-app updates for an existing TrueNAS Custom App.

Run on the NAS using its system Python. TrueNAS remains the deployment owner;
only a file mailbox is shared with Fruit. No NAS credentials or Docker socket
are placed in the web container.
"""

import argparse
import copy
import fcntl
import json
import os
import re
import shlex
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from update_protocol import read_json, write_json
from xsort_updater import REVISION, SERVICE, UpdateError, Updater


def local_api(method, *args, job=False):
    try:
        try:
            from truenas_api_client import Client
        except ImportError:
            from middlewared.client import Client
        with Client() as client:
            return client.call(method, *args, job=job)
    except Exception:
        # App configuration and API errors may contain deployment credentials.
        raise UpdateError("TrueNAS operation failed. Check the app's task status on the NAS.") from None


def set_mapping_value(service, key, name, value):
    current = service.get(key, {})
    if isinstance(current, list):
        current = [item for item in current if item.split("=", 1)[0] != name]
        current.append(f"{name}={value}")
    elif isinstance(current, dict):
        current[name] = value
    else:
        raise UpdateError("Unsupported app environment or labels format.")
    service[key] = current


class TrueNASUpdater(Updater):
    def __init__(self, repo, app, remote="origin", branch=None, api=None):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", app):
            raise UpdateError("Use the existing TrueNAS app name, such as xsort.")
        super().__init__(repo, [], remote, branch)
        self.app = app
        self.api = api or local_api
        self.setup_mode = False
        binding = read_json(self.private / "truenas-target.json")
        if binding and binding.get("app") != app:
            raise UpdateError("This updater checkout is already connected to a different TrueNAS app.")

    def container(self):
        ids = self.run(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project=ix-{self.app}",
                        "--filter", f"label=com.docker.compose.service={SERVICE}"])
        if not ids or "\n" in ids:
            raise UpdateError("Expected exactly one Fruit container in the selected TrueNAS app.")
        return json.loads(self.run(["docker", "inspect", ids]))[0]

    def deployment(self):
        app = self.api("app.get_instance", self.app)
        if not app.get("custom_app"):
            raise UpdateError("Select an existing TrueNAS Custom App.")
        config = self.api("app.config", self.app)
        if set(config.get("services", {})) != {SERVICE}:
            raise UpdateError("The TrueNAS updater requires a Custom App containing only FruitDeepLinks.")
        build = config["services"][SERVICE].get("build", {})
        if isinstance(build, dict) and (
            set(build) - {"context", "dockerfile", "args"}
            or build.get("dockerfile", "Dockerfile") != "Dockerfile"
            or set(build.get("args") or {}) - {"FDL_BUILD_REVISION"}
        ):
            raise UpdateError("The TrueNAS updater requires the standard Dockerfile without custom build options.")
        old = self.container()
        if not old.get("State", {}).get("Running"):
            raise UpdateError("Start the TrueNAS app before updating.")
        mounts = old.get("Mounts", [])
        for target in ("/app/data", "/app/out", "/app/logs"):
            if not any(v.get("Destination") == target and v.get("Type") in {"bind", "volume"} for v in mounts):
                raise UpdateError("Data, output and logs must use persistent mounts before updating.")
        for mount in mounts:
            destination = mount.get("Destination", "").rstrip("/")
            if destination in {"/", "/app", "/app/VERSION"} or any(
                destination == p or destination.startswith(p + "/") for p in ("/app/bin", "/app/templates")
            ):
                raise UpdateError("Remove development source mounts before using the updater.")
        mailbox = next((v for v in mounts if v.get("Destination") == "/run/xsort-updater"), None)
        if mailbox and (Path(mailbox.get("Source", "")).resolve() != self.control or not mailbox.get("RW")):
            raise UpdateError("The app is connected to a different or read-only updater mailbox.")
        if not self.setup_mode and not mailbox:
            raise UpdateError("Run the one-time TrueNAS updater setup on the NAS first.")
        running = (old.get("Config", {}).get("Labels") or {}).get("org.opencontainers.image.revision", "unknown")
        return config, old, running

    def app_idle(self, container_id):
        # This endpoint also exists on the old app, before the updater UI.
        code = (
            "import json,urllib.request; "
            "get=lambda p: json.load(urllib.request.urlopen('http://127.0.0.1:6655'+p,timeout=10)); "
            "s=get('/api/status'); p=get('/api/xtream/pool'); "
            "print(json.dumps({'busy':bool(s['refresh']['running'] or p['active'])}))"
        )
        result = json.loads(self.run(["docker", "exec", container_id, "python3", "-c", code],
                                    error="Could not confirm the app is idle."))
        if result.get("busy", True):
            raise UpdateError("A refresh or stream is running. Wait for it to finish before updating.")

    def private_config(self, name, config):
        path = self.private / name
        # Native app.config returns the original Compose values, not a resolved
        # Compose model. Preserve its dollar escaping and all unrelated fields.
        write_json(path, config)
        os.chmod(path, 0o600)

    def image_config(self, original, image, revision=None, connect=False):
        config = copy.deepcopy(original)
        service = config["services"][SERVICE]
        service.pop("build", None)
        service["image"] = image
        service["pull_policy"] = "never"
        if revision:
            set_mapping_value(service, "environment", "FDL_BUILD_REVISION", revision)
            set_mapping_value(service, "labels", "org.opencontainers.image.revision", revision)
        if connect:
            set_mapping_value(service, "environment", "XSORT_UPDATE_DIR", "/run/xsort-updater")
            volumes = service.setdefault("volumes", [])
            def destination(volume):
                return volume.get("target") if isinstance(volume, dict) else volume.split(":")[1] if ":" in volume else volume
            volumes[:] = [v for v in volumes if destination(v) != "/run/xsort-updater"]
            volumes.append({"type": "bind", "source": str(self.control), "target": "/run/xsort-updater"})
        return config

    def wait_healthy(self, image_id):
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            try:
                container = self.container()
                state = container.get("State", {})
                if (container.get("Image") == image_id and state.get("Running")
                        and state.get("Health", {}).get("Status") == "healthy"):
                    return container
            except UpdateError:
                pass
            time.sleep(2)
        raise UpdateError("The selected app image did not become healthy.")

    def install(self, expected):
        state = self.check(for_install=True)
        if expected != state["available_revision"] or (state["installed_revision"] == expected and not self.setup_mode):
            raise UpdateError("The available revision changed. Review a new check before installing.")
        config, old, _ = self.deployment()
        self.app_idle(old["Id"])
        self.clean_checkout()
        self.git("merge", "--ff-only", expected)
        image = f"xsort-{self.app}:{expected}"
        self.save(phase="building", can_install=False, message="Building the update. Fruit is still running.")
        self.run(["docker", "build", "--tag", image, "--build-arg", f"FDL_BUILD_REVISION={expected}", str(self.repo)],
                 timeout=3600, error="Build failed. The running TrueNAS app was left in place.")
        built = json.loads(self.run(["docker", "image", "inspect", image]))[0]
        if (built.get("Config", {}).get("Labels") or {}).get("org.opencontainers.image.revision") != expected:
            raise UpdateError("Built image does not identify the approved revision.")
        # A simultaneous edit in TrueNAS must not be replaced with an old copy.
        if self.api("app.config", self.app) != config or self.container()["Id"] != old["Id"]:
            raise UpdateError("The TrueNAS app changed during the build. Check for updates again.")
        self.app_idle(old["Id"])
        backup = self.backup_database(old["Id"], expected)
        backup_image = f"xsort-update-backup:{old['Image'].split(':')[-1][:16]}"
        self.run(["docker", "image", "tag", old["Image"], backup_image], error="Could not retain the previous app image.")
        candidate = self.image_config(config, image, expected, connect=True)
        rollback = self.image_config(config, backup_image)
        self.private_config("truenas-original.json", config)
        self.private_config("truenas-candidate.json", candidate)
        self.private_config("truenas-rollback.json", rollback)
        self.save(phase="restarting", database_backup=backup, previous_image=backup_image,
                  message="TrueNAS is restarting Fruit. Reconnecting shortly.")
        try:
            self.api("app.update", self.app, {"custom_compose_config": candidate}, job=True)
            actual = self.wait_healthy(built["Id"])
            labels = actual.get("Config", {}).get("Labels") or {}
            if labels.get("org.opencontainers.image.revision") != expected:
                raise UpdateError("The running app did not match the approved revision.")
        except Exception:
            self.save(phase="rolling_back", message="The update failed. Restoring the previous app image through TrueNAS.")
            try:
                self.api("app.update", self.app, {"custom_compose_config": rollback}, job=True)
                self.wait_healthy(old["Image"])
            except Exception:
                raise UpdateError("Update and image recovery failed. Use the saved TrueNAS recovery configuration on the host.") from None
            raise UpdateError("Update failed; the previous image is healthy. The database backup was retained.") from None
        self.save(phase="success", can_install=False, installed_revision=expected,
                  message="Update installed and healthy. Future updates use these same buttons.", finished_at=time.time())

    @contextmanager
    def setup_lock(self):
        with (self.directory / "helper.lock").open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise UpdateError("The updater is already running. Use App Updates in Fruit.") from None
            yield

    def setup(self):
        self.setup_mode = True
        try:
            with self.setup_lock():
                self.private_config("truenas-target.json", {"app": self.app})
                state = self.check()
                self.install(state["available_revision"])
        finally:
            self.setup_mode = False
        self.start_at_boot()

    def recover(self):
        with self.setup_lock():
            if read_json(self.private / "truenas-target.json").get("app") != self.app:
                raise UpdateError("No recovery configuration is registered for this TrueNAS app.")
            config = read_json(self.private / "truenas-rollback.json")
            if set(config.get("services", {})) != {SERVICE}:
                raise UpdateError("No saved TrueNAS recovery configuration exists in this checkout.")
            image = config["services"][SERVICE].get("image", "")
            if not re.fullmatch(r"xsort-update-backup:[0-9a-f]{16}", image):
                raise UpdateError("The saved recovery image is invalid.")
            self.api("app.update", self.app, {"custom_compose_config": config}, job=True)
            image_id = json.loads(self.run(["docker", "image", "inspect", image]))[0]["Id"]
            self.wait_healthy(image_id)

    def start_at_boot(self):
        command = ["systemd-run", f"--unit=xsort-updater-{self.app}", "--collect",
                   "--property=Restart=always", "--property=RestartSec=5",
                   "/usr/bin/python3", str(self.repo / "bin/xsort_truenas_updater.py"),
                   "--repo", str(self.repo), "--app", self.app, "--remote", self.remote, "--branch", self.branch]
        comment = f"Fruit updater: {self.app}"
        tasks = self.api("initshutdownscript.query", [["comment", "=", comment]])
        if len(tasks) > 1:
            raise UpdateError("More than one Fruit updater startup task exists. Review them in TrueNAS.")
        task = {"type": "COMMAND", "command": shlex.join(command), "when": "POSTINIT",
                "enabled": True, "timeout": 30, "comment": comment}
        if tasks:
            self.api("initshutdownscript.update", tasks[0]["id"], task)
        else:
            self.api("initshutdownscript.create", task)
        self.run(command, error="The app is ready, but the helper did not start. Check the NAS startup task.")

    def handle(self, request):
        super().handle(request)
        if read_json(self.control / "status.json").get("phase") == "success":
            # The service manager starts a fresh interpreter for the newly
            # installed helper code. A failed install keeps its old helper.
            raise SystemExit(0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--app", required=True, help="Existing TrueNAS Custom App name, for example xsort.")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--branch", default="codex/xtream-ingestion")
    parser.add_argument("--setup", action="store_true", help="Connect and deploy the updater once, then start its persistent helper.")
    parser.add_argument("--recover", action="store_true", help="Restore the saved previous image and app configuration; does not restore the database.")
    args = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise UpdateError("Run this helper on the TrueNAS host with sudo /usr/bin/python3.")
        updater = TrueNASUpdater(args.repo, args.app, args.remote, args.branch)
        if args.setup and args.recover:
            raise UpdateError("Choose setup or recovery, not both.")
        if args.recover:
            updater.recover()
            print("The previous app image is healthy. Database backups were retained.")
        elif args.setup:
            updater.setup()
            print("Setup complete. Open Fruit Settings > App Updates. No further YAML edits are needed.")
        else:
            updater.serve()
    except UpdateError as exc:
        parser.exit(1, f"{exc}\n")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
