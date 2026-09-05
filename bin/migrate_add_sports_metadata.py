#!/usr/bin/env python3
"""Idempotently create/backfill Fruit's canonical sports metadata tables."""
import argparse
import sqlite3
from sports_metadata import ensure_schema, sync_legacy_events

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--backfill", action="store_true")
    args = parser.parse_args()
    with sqlite3.connect(args.db) as conn:
        ensure_schema(conn)
        print(sync_legacy_events(conn) if args.backfill else "canonical sports schema ready")

if __name__ == "__main__":
    main()
