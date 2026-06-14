#!/usr/bin/env python
"""
Migration: purge orphaned playables rows.

The playables table declares ``FOREIGN KEY (event_id) REFERENCES events(id)
ON DELETE CASCADE``, but SQLite ignores foreign keys unless a connection sets
``PRAGMA foreign_keys = ON``. For a long time the daily_refresh cleanup steps
opened raw connections without that pragma, so deleting stale events left their
playables behind. The leak is now fixed at the source (daily_refresh Step 5c),
but this migration sweeps up the already-accumulated orphans.

Safe to run multiple times: it only deletes playables whose event_id has no
matching row in events, and only VACUUMs when it actually freed space.
"""

import argparse
import logging
import sqlite3
from pathlib import Path
from typing import Optional


DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "fruit_events.db"


def get_logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    return logging.getLogger("migrate_purge_orphan_playables")


def purge_orphans(conn: sqlite3.Connection, log: logging.Logger) -> int:
    """Delete playables whose event_id is absent from events. Returns rows deleted."""
    cur = conn.cursor()

    # Bail out gracefully if either table is missing (early bootstrap).
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('playables','events');"
    )
    tables = {row[0] for row in cur.fetchall()}
    if {"playables", "events"} - tables:
        log.info("playables/events not both present yet; nothing to purge.")
        return 0

    cur.execute(
        """
        DELETE FROM playables
        WHERE event_id NOT IN (SELECT id FROM events)
        """
    )
    deleted = cur.rowcount
    conn.commit()
    return deleted


def migrate(db_path: Path) -> None:
    log = get_logger()
    log.info("Using database: %s", db_path)

    if not db_path.exists():
        log.error(
            "Database file does not exist at %s. Run your ingest pipeline first.",
            db_path,
        )
        raise SystemExit(1)

    conn = sqlite3.connect(str(db_path))
    try:
        deleted = purge_orphans(conn, log)
        if deleted > 0:
            log.info("Deleted %d orphaned playables row(s); reclaiming space (VACUUM)...", deleted)
            conn.execute("VACUUM")
            log.info("Migration complete: purged %d orphan(s) and vacuumed.", deleted)
        else:
            log.info("Migration complete: no orphaned playables found.")
    finally:
        conn.close()


def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Purge orphaned playables rows left by non-cascading event deletes."
    )
    parser.add_argument(
        "--db",
        dest="db_path",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"Path to SQLite DB (default: {DEFAULT_DB_PATH})",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = parse_args()
    migrate(args.db_path)
