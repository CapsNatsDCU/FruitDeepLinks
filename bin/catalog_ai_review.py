#!/usr/bin/env python3
"""Run the queued, compatible-endpoint catalog AI review after materialization."""
from __future__ import annotations

import argparse

from db.connection import get_conn
from sports_metadata import ensure_schema
from catalog_workbench import run_ai_review


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="scheduled")
    parser.add_argument("--db")  # Compatibility with other refresh subcommands; connection config owns the path.
    args = parser.parse_args()
    with get_conn() as conn:
        ensure_schema(conn)
        result = run_ai_review(conn, source=args.source)
    print("catalog-ai-review", result["status"], "proposals", result.get("proposals", 0))
    return 0 if result["status"] in {"completed", "disabled"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
