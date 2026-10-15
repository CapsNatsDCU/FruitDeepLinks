"""Manual diagnosis of the provider stream selected by a Fruit lane."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from server.services.lanes import get_lane_direct_stream
from xtream_compare_playback import compare
from xtream_ingest import XtreamError


def diagnose_lane_stream(pool, lane_number: int, account_id: str | None = None) -> dict:
    """Use one available account for bounded, credential-safe transport checks."""
    if lane_number < 1:
        raise XtreamError("Enter a positive Fruit lane number")
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    with pool.connection() as conn:
        playable = get_lane_direct_stream(conn, lane_number, now)
    if not playable or str(playable.get("provider") or "").lower() != "xtream":
        raise XtreamError("This lane has no selected Xtream stream right now")
    stream_id = str(playable.get("stream_id") or "")
    if not re.fullmatch(r"[0-9]+", stream_id):
        raise XtreamError("The selected lane has no valid Xtream stream ID")

    accounts = pool.status()["accounts"]
    if account_id is None:
        chosen = next((row for row in accounts if row["enabled"] and row["available"] > 0), None)
        if chosen is None:
            return {"status": "skipped", "reason": "no_available_account", "lane": lane_number}
        account_id = chosen["id"]
    elif not isinstance(account_id, str) or account_id not in {row["id"] for row in accounts}:
        raise XtreamError("Select a configured Xtream account")

    diagnostic = compare(pool, account_id, stream_id, host="fruit_preferred", seconds=10)
    if diagnostic["status"] != "complete":
        return {"lane": lane_number, **diagnostic}

    results = diagnostic["results"]
    direct = next((row for row in results if row["method"] == "python_ts"), {})
    def continued(row):
        return (row.get("video") and row.get("delivery") == "window_complete" and
                (row.get("media_span_seconds") or 0) >= 4)

    def ended_early(row):
        return (row.get("video") and row.get("delivery") == "upstream_eof" and
                (row.get("media_span_seconds") or 0) < 4)

    # Fruit tries the root TS path first and keeps it after valid initial TS
    # bytes. A working alternate method must not hide that path's early EOF.
    if continued(direct):
        finding = "media_continued"
    elif ended_early(direct):
        finding = "short_media_then_eof"
    elif any(continued(row) for row in results):
        finding = "alternate_method_continued"
    elif any(ended_early(row) for row in results):
        finding = "short_media_then_eof"
    elif any(row.get("video") for row in results):
        finding = "video_detected_duration_unverified"
    else:
        finding = "no_decodable_video"
    return {
        **diagnostic,
        "lane": lane_number,
        "source_first_bytes_format": direct.get("first_bytes_format"),
        "fruit_output_format": "mpeg_ts",
        "channels_stream_format": "MPEG-TS",
        "finding": finding,
        "note": "This tests Fruit's currently preferred account host. A video sample cannot confirm the expected programme or a full game.",
    }
