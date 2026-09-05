"""Dry-run the existing lane scheduler on an in-memory SQLite copy."""
from __future__ import annotations

import sqlite3
from typing import Any


def simulate(conn: sqlite3.Connection, lane_count: int, days_ahead: int) -> dict[str, Any]:
    """Run the production lane-builder without modifying live lanes/exports."""
    from fruit_build_lanes import (build_lanes_with_placeholders, create_lanes,
                                   ensure_lane_schema, load_future_events, reset_lanes)

    copy = sqlite3.connect(":memory:")
    copy.row_factory = sqlite3.Row
    try:
        conn.backup(copy)
        events = load_future_events(copy, days_ahead)
        ensure_lane_schema(copy); reset_lanes(copy); create_lanes(copy, lane_count)
        build_lanes_with_placeholders(copy, events, lane_count)
        scheduled = [dict(row) for row in copy.execute(
            "SELECT le.lane_id,le.event_id,le.start_utc,le.end_utc,le.chosen_provider,le.chosen_playable_id FROM lane_events le WHERE COALESCE(le.is_placeholder,0)=0 ORDER BY le.start_utc,le.lane_id")]
        scheduled_ids = {row["event_id"] for row in scheduled}
        dropped = [{"event_id": event.event_id, "canonical_event_id": event.canonical_event_id,
                    "rule": event.sports_rule, "reason": "lane_capacity"}
                   for event in events if event.event_id not in scheduled_ids]
        capacities = {row[0]: row[1] for row in copy.execute("SELECT provider,max_concurrent FROM provider_capacities")}
        conflicts = []
        for provider, maximum in capacities.items():
            provider_rows = [row for row in scheduled if row.get("chosen_provider") == provider]
            for row in provider_rows:
                overlaps = [other for other in provider_rows if other["start_utc"] < row["end_utc"] and other["end_utc"] > row["start_utc"]]
                if len(overlaps) > maximum:
                    conflicts.append({"event_id": row["event_id"], "provider": provider,
                                      "capacity": maximum, "overlap_count": len(overlaps), "reason": "provider_concurrency"})
        return {"scheduled": scheduled, "dropped": dropped, "provider_conflicts": conflicts,
                "uses": "fruit_build_lanes.build_lanes_with_placeholders"}
    finally:
        copy.close()
