#!/usr/bin/env python3
"""
migrate_add_espn_graph_id_column.py - Add espn_graph_id column to playables table

Adds the column fruit_enrich_espn.py uses to store ESPN Watch Graph's own
playback ID for a matched playable. Idempotent, no-op if the column already
exists.

Previously this was inlined directly in daily_refresh.py (the one schema
change that didn't follow the project's own migrate_*.py convention, and
consequently ran outside run_step() -- invisible to the refresh progress
feed). Pulled out to match every other schema migration (e.g.
migrate_add_locale.py, migrate_add_espn_locale_fallback.py).
"""

import argparse
import sqlite3
from pathlib import Path


def ensure_espn_graph_id_column(conn: sqlite3.Connection) -> bool:
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(playables)")
    columns = [row[1] for row in cur.fetchall()]

    if "espn_graph_id" in columns:
        print("espn_graph_id column already exists")
        return False

    print("Adding espn_graph_id column to playables table...")
    cur.execute("ALTER TABLE playables ADD COLUMN espn_graph_id TEXT")
    conn.commit()
    print("espn_graph_id column added successfully")
    return True


def main():
    ap = argparse.ArgumentParser(description="Add espn_graph_id column to playables table")
    ap.add_argument("--db", default="data/fruit_events.db", help="Path to fruit_events.db")
    ap.add_argument("--yes", action="store_true", help="Auto-confirm without prompting")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        return 1

    conn = sqlite3.connect(db_path)
    column_added = ensure_espn_graph_id_column(conn)
    conn.close()

    print("\nMigration complete" if column_added else "\nNo changes needed (already migrated)")
    return 0


if __name__ == "__main__":
    exit(main())
