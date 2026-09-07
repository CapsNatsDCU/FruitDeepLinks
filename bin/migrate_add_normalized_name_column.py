#!/usr/bin/env python3
"""Add the optional event programming-name override column."""

import argparse
import sqlite3
from pathlib import Path

from event_naming import ensure_normalized_name_column


def main() -> int:
    parser = argparse.ArgumentParser(description="Add normalized_name to events")
    parser.add_argument("--db", default="data/fruit_events.db")
    args = parser.parse_args()
    path = Path(args.db)
    if not path.exists():
        print(f"Database not found: {path}")
        return 1
    with sqlite3.connect(path) as conn:
        changed = ensure_normalized_name_column(conn)
    print("normalized_name column added" if changed else "normalized_name column already exists")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
