#!/usr/bin/env python3
"""
migrate_backfill_espn_unlimited_granular_tiers.py - One-time backfill for
existing enabled_services preferences, replacing the live wildcard that used
to auto-include espn_mlb_tv/espn_mlb_network under "espn_unlimited".

That live wildcard (filter_integration.py's get_filtered_playables() and
fruit_build_adb_lanes.py's own copy) existed so a user who only checked
"ESPN Unlimited" -- before these granular tiers existed as separate,
independently-listed checkboxes -- wouldn't silently lose events that Apple's
catalog only exposes under one of those tiers. But it couldn't distinguish
that from a user who explicitly unchecked ESPN MLB.TV: both look identical
as an absence from enabled_services, so the wildcard silently re-included it
on every request, overriding the explicit uncheck (reported on the forum).

This migration backfills existing saved preferences ONCE, so anyone who
genuinely never considered these tiers keeps the same inclusive behavior
they had before, without perpetually re-inferring intent from absence on
every filter evaluation. After this runs, absence means off -- same as
every other service checkbox.

Idempotent: guarded by a marker key so it only ever applies once.
"""

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

_bin_dir = os.path.dirname(__file__)
if _bin_dir not in sys.path:
    sys.path.insert(0, _bin_dir)

from db.preferences import load, save  # noqa: E402
from filter_integration import expand_enabled_services_for_espn_unlimited  # noqa: E402

_MARKER_KEY = "_migrated_espn_unlimited_granular_backfill"


def backfill(conn: sqlite3.Connection) -> bool:
    """Returns True if a backfill write happened, False if already done / nothing to do."""
    cur = conn.cursor()
    cur.execute("SELECT value FROM user_preferences WHERE key = ?", (_MARKER_KEY,))
    if cur.fetchone():
        print("Already backfilled (marker present) -- nothing to do")
        return False

    prefs = load(conn)
    enabled = prefs.get("enabled_services") or []
    expanded = expand_enabled_services_for_espn_unlimited(enabled)

    if expanded != enabled:
        print(f"Backfilling enabled_services: adding {sorted(set(expanded) - set(enabled))}")
        save(conn, {"enabled_services": expanded})
    else:
        print("No backfill needed (enabled_services empty, or granular tier already explicit)")

    cur.execute(
        "INSERT OR REPLACE INTO user_preferences (key, value, updated_utc) VALUES (?, ?, datetime('now'))",
        (_MARKER_KEY, json.dumps(True)),
    )
    conn.commit()
    return True


def main():
    ap = argparse.ArgumentParser(description="One-time backfill for espn_unlimited granular tier preferences")
    ap.add_argument("--db", default="data/fruit_events.db", help="Path to fruit_events.db")
    ap.add_argument("--yes", action="store_true", help="Auto-confirm without prompting")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        return 1

    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_preferences (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_utc TEXT
            )
            """
        )
        conn.commit()
        backfill(conn)
    finally:
        conn.close()

    print("\nMigration complete")
    return 0


if __name__ == "__main__":
    exit(main())
