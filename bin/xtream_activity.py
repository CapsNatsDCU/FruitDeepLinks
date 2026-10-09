"""Shared, durable quiet period for optional Xtream resolution checks."""
from __future__ import annotations

import contextvars
import fcntl
import math
import os
import time
from contextlib import contextmanager
from functools import wraps
from pathlib import Path


QUIET_SECONDS = 10
_probe_context = contextvars.ContextVar("xtream_quality_probe", default=None)


def _activity_file(db_path):
    path = Path(db_path)
    directory = path.parent / (path.name + ".xtream-locks")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory / "normal-activity-until"


def mark_normal_activity(db_path):
    """Call at start and end, extending the quiet period after completion."""
    if _probe_context.get() is not None:
        return
    fd = os.open(_activity_file(db_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        deadline = time.time() + QUIET_SECONDS
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, str(deadline).encode("ascii"))
        os.fsync(fd)
    finally:
        os.close(fd)


def normal_activity_remaining(db_path):
    fd = os.open(_activity_file(db_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            deadline = float(os.read(fd, 128) or b"0")
        except ValueError:
            return QUIET_SECONDS
        if not math.isfinite(deadline):
            return QUIET_SECONDS
        now = time.time()
        # Adopt the shorter policy once when an older worker left a ten-minute
        # deadline. Persist the new deadline so repeated reads do not reset it.
        if deadline > now + QUIET_SECONDS:
            deadline = now + QUIET_SECONDS
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, str(deadline).encode("ascii"))
            os.fsync(fd)
        return max(0, deadline - now)
    finally:
        os.close(fd)


@contextmanager
def normal_activity(db_path):
    mark_normal_activity(db_path)
    try:
        yield
    finally:
        mark_normal_activity(db_path)


@contextmanager
def quality_probe_context(db_path):
    token = _probe_context.set(Path(db_path))
    try:
        yield
    finally:
        _probe_context.reset(token)


def quality_probe_db_path():
    return _probe_context.get()


def normal_activity_request(fn):
    """Mark the full lifetime of a user-initiated provider request."""
    @wraps(fn)
    def wrapped(*args, **kwargs):
        from db.connection import resolve_db_path
        with normal_activity(resolve_db_path()):
            return fn(*args, **kwargs)
    return wrapped
