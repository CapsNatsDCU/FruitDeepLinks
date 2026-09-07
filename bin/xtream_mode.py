"""Shared Xtream-only mode policy.

The mode is deliberately resolved from Settings first, with the deployment
environment as the initial default.  It controls discovery and lane playback;
it never changes the separate persistent-channel configuration.
"""

from __future__ import annotations

import sqlite3

from db.preferences import get_setting


def is_xtream_only(conn: sqlite3.Connection) -> bool:
    """Return whether dynamic FruitLanes may use only Xtream playables."""
    return bool(get_setting(conn, "xtream_only", False))
