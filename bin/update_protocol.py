"""Small file mailbox shared by the web app and the Docker-host updater.

The host alone chooses the repository, branch and Compose service. Requests
contain an action and an already-reviewed revision, never shell commands.
"""

import fcntl
import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

BUSY_PHASES = {"queued", "checking", "installing", "building", "restarting", "rolling_back"}
INSTALL_PHASES = {"installing", "building", "restarting", "rolling_back"}


def installation_active():
    directory = os.getenv("XSORT_UPDATE_DIR")
    return bool(directory and host_online(directory)
                and read_json(Path(directory) / "status.json").get("phase") in INSTALL_PHASES)


def read_json(path):
    try:
        value = json.loads(Path(path).read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path, value):
    path = Path(path)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".update-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
        os.chmod(name, 0o644)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def mailbox_lock(directory):
    path = Path(directory) / "mailbox.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o666)
    try:
        # The host user and the container user can differ. No secrets are kept
        # in this mailbox, which must only be mounted into the trusted app.
        try:
            os.fchmod(fd, 0o666)
        except PermissionError:
            pass
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def host_online(directory):
    try:
        return time.time() - (Path(directory) / "heartbeat").stat().st_mtime < 15
    except OSError:
        return False
