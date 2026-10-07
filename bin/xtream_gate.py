"""Cross-process account lock shared by provider API requests and media.

The lock filename contains only a credential fingerprint. Media workers inherit
the descriptor, so a surviving curl/FFmpeg child keeps the account occupied.
"""
from __future__ import annotations

import fcntl
import os
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path

from xtream_accounts import config_fingerprint


class AccountBusy(Exception):
    """No provider request may start while this account is occupied."""


class AccountDisabled(AccountBusy):
    """The account was disabled after it was chosen for a request."""


class AccountGate:
    def __init__(self, db_path):
        path = Path(db_path)
        self.db_path = path
        self.directory = path.parent / (path.name + ".xtream-locks")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    def acquire(self, config, *, wait_seconds=0):
        path = self.directory / ("account-" + config_fingerprint(config))
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        deadline = time.monotonic() + wait_seconds
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return fd
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise AccountBusy("Xtream account is occupied") from None
                    time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        except BaseException:
            os.close(fd)
            raise

    @contextmanager
    def hold(self, config, *, wait_seconds=2):
        fd = self.acquire(config, wait_seconds=wait_seconds)
        try:
            # Metadata clients can retain a config across a long refresh. A
            # saved disable must still prevent their next provider request.
            try:
                with closing(sqlite3.connect(self.db_path, timeout=2)) as conn:
                    disabled = conn.execute(
                        "SELECT 1 FROM xtream_account_state WHERE fingerprint=? AND enabled_override=0 LIMIT 1",
                        (config_fingerprint(config),),
                    ).fetchone()
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc).lower():
                    raise AccountBusy("Xtream account state is unavailable") from None
                disabled = None
            if disabled:
                raise AccountDisabled("Xtream account is disabled")
            yield fd
        finally:
            os.close(fd)
