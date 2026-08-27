#!/usr/bin/env python3
"""
cleanup_stale_rows.py - Generic stale-row cleanup for the daily pipeline.

Deletes rows matching --where from --table in --db. Replaces three
hand-copied inline blocks that used to live directly in daily_refresh.py
(fruit_events.db events, apple_events.db apple_events, espn_graph.db
events) -- same shape every time (count, delete, log, non-fatal on error),
now one script instead of three, and run through run_step() like every
other pipeline step so it shows up in the refresh progress feed instead of
being invisible to it.

Usage:
  python cleanup_stale_rows.py --db data/fruit_events.db --table events \
      --where "end_utc < datetime('now', '-1 day') OR (end_utc IS NULL AND start_utc < datetime('now', '-2 days'))" \
      --label "fruit events" --cascade
"""

import argparse
import sqlite3
import sys


def cleanup(db_path: str, table: str, where: str, label: str, cascade: bool) -> int:
    conn = sqlite3.connect(db_path)
    if cascade:
        # SQLite defaults foreign_keys OFF per-connection, so ON DELETE
        # CASCADE never fires unless enabled here -- without this, deleting
        # a parent row (e.g. an event) strands its children (playables/
        # feeds) instead of cascading, silently bloating the DB.
        conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()

    cur.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}")
    stale_count = cur.fetchone()[0]

    if stale_count > 0:
        cur.execute(f"DELETE FROM {table} WHERE {where}")
        deleted = cur.rowcount
        conn.commit()
        print(f"[OK] Deleted {deleted} old {label} row(s) from {table}")
    else:
        print(f"[OK] No old {label} rows to clean up")

    conn.close()
    return stale_count


def main():
    ap = argparse.ArgumentParser(description="Delete stale rows from a SQLite table")
    ap.add_argument("--db", required=True, help="Path to the SQLite database")
    ap.add_argument("--table", required=True, help="Table to delete from")
    ap.add_argument("--where", required=True, help="SQL WHERE clause fragment identifying stale rows")
    ap.add_argument("--label", required=True, help="Human-readable label for log output")
    ap.add_argument(
        "--cascade", action="store_true",
        help="Enable PRAGMA foreign_keys=ON so ON DELETE CASCADE fires for this table's children",
    )
    args = ap.parse_args()

    cleanup(args.db, args.table, args.where, args.label, args.cascade)
    return 0


if __name__ == "__main__":
    sys.exit(main())
