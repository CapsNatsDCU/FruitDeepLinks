#!/usr/bin/env python3
"""Run on the Docker host to service the app's fixed check/install mailbox.

Python 3.10+, Git and Docker Compose v2 are required. This process deliberately
has no HTTP listener. Only the selected Compose service is recreated.
"""

import argparse
import copy
import fcntl
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from update_protocol import BUSY_PHASES, mailbox_lock, read_json, write_json

SERVICE = "fruitdeeplinks"
REVISION = re.compile(r"[0-9a-f]{40}")


class UpdateError(Exception):
    pass


class Updater:
    def __init__(self, repo, compose_files, remote="origin", branch=None, project=None):
        self.repo = Path(repo).resolve()
        self.directory = self.repo / ".xsort-updater"
        self.control = self.directory / "control"
        self.private = self.directory / "private"
        self.control.mkdir(parents=True, exist_ok=True)
        self.private.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self.private, 0o700)
        self.remote = remote
        self.branch = branch or self.git("symbolic-ref", "--short", "HEAD")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", remote):
            raise UpdateError("Use a configured Git remote name, such as origin.")
        self.git("check-ref-format", "--branch", self.branch)
        self.compose = ["docker", "compose", "--project-directory", str(self.repo)]
        if project:
            self.compose += ["--project-name", project]
        for name in compose_files:
            self.compose += ["-f", str((self.repo / name).resolve())]

    def run(self, args, *, timeout=120, error="Host command failed.", env=None):
        # Never return command output/errors to the web UI: Compose can include
        # deployment secrets and Git errors can include authenticated URLs.
        try:
            result = subprocess.run(args, cwd=self.repo, env=env, text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise UpdateError(error) from exc
        if result.returncode:
            raise UpdateError(error)
        return result.stdout.strip()

    def git(self, *args):
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
        return self.run(["git", *args], env=env, error="Git operation failed. Check repository access on the Docker host.")

    def save(self, **changes):
        with mailbox_lock(self.control):
            state = read_json(self.control / "status.json")
            state.update(changes)
            state.update(branch=self.branch, remote=self.remote)
            write_json(self.control / "status.json", state)
        return state

    def deployment(self):
        config = json.loads(self.run(self.compose + ["config", "--format", "json"],
                                    error="Could not read Docker Compose configuration."))
        service = config.get("services", {}).get(SERVICE, {})
        if not service.get("build"):
            raise UpdateError("Branch updates require a source-build Compose installation.")
        mounts = service.get("volumes", [])
        if any(v.get("target") in {"/app", "/app/bin", "/app/templates", "/app/VERSION"} for v in mounts):
            raise UpdateError("Disable development source mounts before using the updater.")
        control_mount = next((v for v in mounts if v.get("target") == "/run/xsort-updater"), {})
        if Path(control_mount.get("source", "")).resolve() != self.control or control_mount.get("read_only"):
            raise UpdateError("Include docker-compose.updater.yml in the host helper's Compose files.")
        for target in ("/app/data", "/app/out", "/app/logs"):
            if not any(v.get("target") == target and v.get("type") in {"bind", "volume"} for v in mounts):
                raise UpdateError("Data, output and logs must use persistent mounts before updating.")
        container_id = self.run(self.compose + ["ps", "--all", "--quiet", SERVICE],
                                error="Could not find the running app container.")
        if not container_id or "\n" in container_id:
            raise UpdateError("Start exactly one app container before using the updater.")
        container = json.loads(self.run(["docker", "inspect", container_id]))[0]
        if not container.get("State", {}).get("Running"):
            raise UpdateError("The app container is not running.")
        actual_mounts = container.get("Mounts", [])
        if not any(v.get("Destination") == "/run/xsort-updater" and Path(v.get("Source", "")).resolve() == self.control
                   for v in actual_mounts):
            raise UpdateError("Recreate the app with docker-compose.updater.yml before updating.")
        labels = container.get("Config", {}).get("Labels", {}) or {}
        running = labels.get("org.opencontainers.image.revision", "unknown")
        return config, container, running

    def clean_checkout(self):
        if self.git("symbolic-ref", "--short", "HEAD") != self.branch:
            raise UpdateError("The checkout branch changed. Restart the helper for the intended branch.")
        if self.git("status", "--porcelain", "--untracked-files=normal"):
            raise UpdateError("The deployment checkout has local changes. Commit or move them before updating.")

    def check(self, for_install=False):
        self.save(phase="installing" if for_install else "checking", can_install=False,
                  message="Checking the current branch for updates.")
        self.clean_checkout()
        _, _, running = self.deployment()
        self.git("fetch", "--no-tags", self.remote,
                 f"+refs/heads/{self.branch}:refs/xsort-updater/target")
        target = self.git("rev-parse", "refs/xsort-updater/target")
        if not REVISION.fullmatch(target):
            raise UpdateError("The remote did not return a valid revision.")
        try:
            self.git("merge-base", "--is-ancestor", "HEAD", target)
            if REVISION.fullmatch(running):
                self.git("merge-base", "--is-ancestor", running, target)
        except UpdateError as exc:
            raise UpdateError("The branch has diverged or would downgrade this install. Reconcile it on the host.") from exc
        log_range = f"{running}..{target}" if REVISION.fullmatch(running) else target
        commits = self.git("log", "-12", "--format=%h %s", log_range).splitlines()
        available = running != target
        return self.save(phase="installing" if for_install else ("available" if available else "current"),
                         can_install=available and not for_install,
                         available_revision=target, installed_revision=running,
                         checked_at=time.time(), commits=commits,
                         message=("An update is available." if REVISION.fullmatch(running) else
                                  "Running revision unknown. Install the checked branch revision to establish it.")
                         if available else "You are up to date.")

    def app_idle(self, container_id):
        code = ("import json,urllib.request; "
                "s=json.load(urllib.request.urlopen('http://127.0.0.1:6655/api/updates',timeout=10)); "
                "print(json.dumps({'busy':bool(s['refresh_running'])}))")
        result = json.loads(self.run(["docker", "exec", container_id, "python3", "-c", code],
                                    error="Could not verify that the app is ready for an update."))
        if result.get("busy", True):
            raise UpdateError("A refresh is running. Wait for it to finish, then check and install again.")

    def write_config(self, path, config):
        # `compose config` has already interpolated environment values. Escape
        # literal dollars before feeding that resolved model back into Compose.
        def literal(value):
            if isinstance(value, str):
                return value.replace("$", "$$")
            if isinstance(value, list):
                return [literal(item) for item in value]
            if isinstance(value, dict):
                return {key: literal(item) for key, item in value.items()}
            return value
        write_json(path, literal(config))
        os.chmod(path, 0o600)

    def backup_database(self, container_id, revision):
        backup_name = f"before-{revision[:12]}-{time.time_ns()}.db"
        code = (
            "import os,sqlite3,pathlib; "
            "p=pathlib.Path(os.getenv('FRUIT_DB_PATH','/app/data/fruit_events.db')).resolve(); "
            "assert p.is_file(),'Database missing'; "
            "d=p.parent/'update-backups'; d.mkdir(exist_ok=True); "
            "s=sqlite3.connect(p.as_uri()+'?mode=ro',uri=True); "
            f"t=sqlite3.connect(d/'{backup_name}'); "
            "s.backup(t); t.close(); s.close()"
        )
        self.run(["docker", "exec", container_id, "python3", "-c", code], timeout=180,
                 error="Database backup failed. The app has not been restarted.")
        return backup_name

    def install(self, expected):
        # Fetch again so an old browser cannot unknowingly approve newer code.
        state = self.check(for_install=True)
        if state["installed_revision"] == expected or expected != state["available_revision"]:
            raise UpdateError("The available revision changed. Review a new check before installing.")
        config, old, _ = self.deployment()
        self.app_idle(old["Id"])
        self.clean_checkout()
        self.save(phase="installing", can_install=False, message="Preparing the update.")
        # Snapshot the resolved deployment before its Compose source can change.
        # It contains secrets and stays in a host-only directory with mode 0700.
        old_config = copy.deepcopy(config)
        project = config["name"]
        image = config["services"][SERVICE].get("image") or f"{project}-{SERVICE}"
        config["services"][SERVICE]["image"] = image
        build = config["services"][SERVICE]["build"]
        if Path(build["context"]).resolve() != self.repo or build.get("dockerfile", "Dockerfile") != "Dockerfile":
            raise UpdateError("The updater requires the repository's standard Dockerfile and build context.")
        build.setdefault("args", {})["FDL_BUILD_REVISION"] = expected
        backup_image = f"xsort-update-backup:{old['Image'].split(':')[-1][:16]}"
        self.run(["docker", "image", "tag", old["Image"], backup_image], error="Could not retain the previous app image.")
        old_config["services"][SERVICE]["image"] = backup_image
        candidate = self.private / "candidate.json"
        rollback = self.private / "rollback.json"
        self.write_config(candidate, config)
        self.write_config(rollback, old_config)
        self.save(previous_image=backup_image)
        self.git("merge", "--ff-only", expected)
        command = ["docker", "compose", "--project-directory", str(self.repo), "-p", project]
        candidate_cmd = command + ["-f", str(candidate)]
        self.save(phase="building", message="Building the update. The app is still running.")
        self.run(candidate_cmd + ["build", SERVICE], timeout=3600,
                 error="Build failed; the running app was left in place. Check the build on the host and retry.")
        self.app_idle(old["Id"])
        # SQLite's online backup also handles WAL databases without copying an
        # inconsistent DB/WAL pair. Store it alongside the persisted database.
        backup_name = self.backup_database(old["Id"], expected)
        self.save(phase="restarting", database_backup=backup_name,
                  message="Restarting the app and checking its health. Reconnecting shortly.")
        up = ["up", "-d", "--no-deps", "--no-build", "--pull", "never", "--wait", "--wait-timeout", "120", SERVICE]
        try:
            self.run(candidate_cmd + up, timeout=180, error="The updated app did not become healthy.")
            _, _, actual = self.deployment()
            if actual != expected:
                raise UpdateError("The running container did not match the approved revision.")
        except Exception:
            self.save(phase="rolling_back", message="Update did not become healthy. Restoring the previous app image.")
            try:
                self.run(command + ["-f", str(rollback)] + up, timeout=180,
                         error="Automatic image recovery failed. Use the host recovery command in the updater guide.")
                self.run(["docker", "image", "tag", old["Image"], image])
            except Exception as exc:
                raise UpdateError("Update and automatic image recovery failed. Follow the host recovery instructions.") from exc
            raise UpdateError("Update failed its health check; the previous image is running. Database backup was retained.")
        self.save(phase="success", can_install=False, installed_revision=expected,
                  message="Update installed and healthy.", finished_at=time.time())

    def handle(self, request):
        try:
            if time.time() - float(request.get("requested_at", 0)) > 300:
                raise UpdateError("The queued request expired. Check for updates again.")
            if request.get("action") == "check":
                self.check()
            elif request.get("action") == "install" and REVISION.fullmatch(str(request.get("revision", ""))):
                self.install(request["revision"])
            else:
                raise UpdateError("Invalid updater request.")
        except UpdateError as exc:
            self.save(phase="error", can_install=False, message=str(exc))
        except Exception:
            self.save(phase="error", can_install=False,
                      message="The host updater encountered an error. Check host configuration and retry.")

    def serve(self):
        lock = (self.directory / "helper.lock").open("w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock.close()
            raise UpdateError("An updater helper is already running for this checkout.") from exc
        stop = threading.Event()

        def heartbeat():
            while not stop.is_set():
                (self.control / "heartbeat").touch()
                stop.wait(2)

        try:
            with mailbox_lock(self.control):
                # Never replay an installation after a helper/host crash.
                state = read_json(self.control / "status.json")
                interrupted = state.get("phase") in BUSY_PHASES
                (self.control / "request.json").unlink(missing_ok=True)
                write_json(self.control / "status.json", {
                    **state, "phase": "error" if interrupted else "idle", "can_install": False,
                    "branch": self.branch, "remote": self.remote,
                    "message": "The previous operation was interrupted. Check the running app before retrying."
                    if interrupted else "Ready. Check for updates to compare with the current branch.",
                })
            pulse = threading.Thread(target=heartbeat, daemon=True)
            pulse.start()
            print(f"Updater ready for {self.remote}/{self.branch}", flush=True)
            while True:
                with mailbox_lock(self.control):
                    request = read_json(self.control / "request.json")
                    (self.control / "request.json").unlink(missing_ok=True)
                if request:
                    self.handle(request)
                time.sleep(1)
        finally:
            stop.set()
            (self.control / "heartbeat").unlink(missing_ok=True)
            lock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--branch", help="Defaults to the checkout's current branch; fixed until helper restarts.")
    parser.add_argument("--compose-file", action="append", help="Repeat for each deployment Compose file.")
    parser.add_argument("--project-name", help="Use the existing Compose project name if explicitly set at deployment.")
    args = parser.parse_args()
    try:
        Updater(args.repo, args.compose_file or ["docker-compose.yml", "docker-compose.updater.yml"],
                args.remote, args.branch, args.project_name).serve()
    except UpdateError as exc:
        parser.exit(1, f"{exc}\n")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
