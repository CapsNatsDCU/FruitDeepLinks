#!/usr/bin/env python3
"""Run the queued, compatible-endpoint catalog AI review after materialization."""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone

from db.connection import get_conn
from sports_metadata import ensure_schema
from catalog_workbench import run_ai_review

PROGRESS_PREFIX = "__FDL_PROGRESS__"


def emit_progress(event: str, **fields) -> None:
    if os.getenv("FDL_REFRESH_PROGRESS") != "1":
        return
    print(f"{PROGRESS_PREFIX}{json.dumps({'event': event, **fields}, separators=(',', ':'))}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="scheduled")
    parser.add_argument("--db")  # Compatibility with other refresh subcommands; connection config owns the path.
    args = parser.parse_args()
    with get_conn() as conn:
        ensure_schema(conn)
        emit_progress("catalog_ai_start", started_at=datetime.now(timezone.utc).isoformat())
        result = run_ai_review(conn, source=args.source)
    emit_progress("catalog_ai_done", status=result["status"], proposals=result.get("proposals", 0),
                  run_id=result.get("run_id"), finished_at=datetime.now(timezone.utc).isoformat())
    print("catalog-ai-review", result["status"], "proposals", result.get("proposals", 0))
    return 0 if result["status"] in {"completed", "disabled"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
