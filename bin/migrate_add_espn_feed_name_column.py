#!/usr/bin/env python3
"""
migrate_add_espn_feed_name_column.py - Add feed_name column to playables table

ESPN Watch Graph's own feed_name/feed_type fields (e.g. feed_type=HOME with
feed_name="Cubs Broadcast") distinguish same-service duplicate playables that
Apple's own scraped title can't -- e.g. two MLB Unlimited playables for the
same game, one the home-market broadcast and one the away-market broadcast,
with identical Apple titles ("Cincinnati Reds vs. Chicago Cubs" for both).
fruit_enrich_espn.py stores the matched candidate's feed_name here so
xmltv_helpers.get_service_label_for_playable() can use it as a fallback
qualifier when title-based extraction (extract_feed_qualifier) finds nothing
to distinguish them.

This migration:
1. Adds feed_name column to playables table (if missing)
2. No-op if column already exists
"""

import argparse
import sqlite3
from pathlib import Path


def ensure_feed_name_column(conn: sqlite3.Connection) -> bool:
    """Add feed_name column to playables table if it doesn't exist"""
    cur = conn.cursor()

    cur.execute("PRAGMA table_info(playables)")
    columns = [row[1] for row in cur.fetchall()]

    if "feed_name" in columns:
        print("feed_name column already exists")
        return False

    print("Adding feed_name column to playables table...")
    cur.execute("ALTER TABLE playables ADD COLUMN feed_name TEXT")
    conn.commit()
    print("feed_name column added successfully")
    return True


def main():
    ap = argparse.ArgumentParser(description="Add feed_name column to playables table")
    ap.add_argument("--db", default="data/fruit_events.db", help="Path to fruit_events.db")
    ap.add_argument("--yes", action="store_true", help="Auto-confirm without prompting")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        return 1

    conn = sqlite3.connect(db_path)
    column_added = ensure_feed_name_column(conn)
    conn.close()

    print("\nMigration complete" if column_added else "\nNo changes needed (already migrated)")
    return 0


if __name__ == "__main__":
    exit(main())
