"""Per-account host fallback under the existing account file lock.

The lock file stores only a host index and retry time, never credentials or
authenticated URLs. After a successful fallback, prefer it for five minutes
before giving the configured primary another chance.
"""
from __future__ import annotations

import os
import time
from dataclasses import replace


PRIMARY_RETRY_SECONDS = 300


def _preference(fd: int | None) -> tuple[int, float]:
    if fd is None:
        return 0, 0
    try:
        index, until = os.pread(fd, 64, 0).decode("ascii").split()
        if index == "1":
            return 1, float(until)
    except (OSError, ValueError, UnicodeError):
        pass
    return 0, 0


def host_configs(config, gate_fd: int | None = None):
    """Yield host variants of one credential set, in current preferred order."""
    fallback = getattr(config, "fallback_server_url", None)
    if not fallback:
        return ((0, config),)
    alternate = replace(config, server_url=fallback, fallback_server_url=None)
    index, until = _preference(gate_fd)
    if index == 1 and until > time.time():
        return ((1, alternate), (0, config))
    return ((0, config), (1, alternate))


def record_host_success(gate_fd: int | None, index: int) -> None:
    if gate_fd is None:
        return
    current, until = _preference(gate_fd)
    now = time.time()
    if (index == 0 and current == 0) or (index == 1 and current == 1 and until > now):
        return
    value = f"{index} {now + PRIMARY_RETRY_SECONDS if index == 1 else 0:.3f}".encode("ascii")
    try:
        os.pwrite(gate_fd, value, 0)
        os.ftruncate(gate_fd, len(value))
    except OSError:
        # Route preference is only an optimization; a working provider result
        # must remain usable even if its lock file cannot be updated.
        pass
