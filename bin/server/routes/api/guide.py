"""Authoritative, post-scheduler virtual-lane guide API.

The lane XMLTV exporter reads ``lanes`` and ``lane_events``.  This endpoint
deliberately reads those same final records; it must never reassign raw events.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Blueprint, jsonify, request

from db.connection import db_exists, get_conn
from db.preferences import get_setting
from server.config import cfg

try:
    from fruit_export_lanes import build_enhanced_title
except ImportError:  # pragma: no cover - keeps the API useful in minimal installs
    def build_enhanced_title(event):
        return event.get("title") or "Untitled"


bp = Blueprint("guide_api", __name__)


def _parse_utc(value, name):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{name} must be an ISO-8601 timestamp")
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone offset")
    return parsed.astimezone(timezone.utc)


def _as_utc(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def _safe_json(value):
    try:
        return json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}


def _output_timestamp():
    path = Path(cfg.OUT_DIR) / "multisource_lanes.xml"
    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()


@bp.route("/api/guide")
def api_guide():
    """Return the exact final lane records used by multisource_lanes.xml."""
    if not db_exists():
        return jsonify({"error": "Database not found"}), 500

    try:
        start = _parse_utc(request.args.get("start"), "start")
        end = _parse_utc(request.args.get("end"), "end")
        if end and start and end <= start:
            return jsonify({"error": "end must be after start"}), 400
        lane_filter = request.args.get("lane", type=int)
        include_placeholders = request.args.get("include_placeholders", "true").lower() not in ("0", "false", "no")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    # A bounded default mirrors the product's useful "Now + next 12 hours" view.
    now = datetime.now(timezone.utc)
    start = start or now
    end = end or (start + timedelta(hours=12))

    with get_conn() as conn:
        cur = conn.cursor()
        table_names = {row[0] for row in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        has_playables = "playables" in table_names
        playable_columns = set()
        if has_playables:
            playable_columns = {row[1] for row in cur.execute("PRAGMA table_info(playables)")}
        playable_select = "NULL AS xtream_metadata, NULL AS xtream_stream_id"
        playable_join = ""
        if {"playable_id", "stream_id"}.issubset(playable_columns):
            metadata_column = "p.stream_metadata_json" if "stream_metadata_json" in playable_columns else "NULL"
            playable_select = f"{metadata_column} AS xtream_metadata, p.stream_id AS xtream_stream_id"
            playable_join = "LEFT JOIN playables p ON p.playable_id = le.chosen_playable_id"

        lane_where = "WHERE (? IS NULL OR l.lane_id = ?)"
        lane_rows = cur.execute(
            f"SELECT l.lane_id, l.name, l.logical_number FROM lanes l {lane_where} ORDER BY l.lane_id",
            (lane_filter, lane_filter),
        ).fetchall()
        rows = cur.execute(
            f"""
            SELECT le.*, e.title AS event_title, e.channel_name, e.raw_attributes_json,
                   {playable_select}
              FROM lane_events le
              LEFT JOIN events e ON e.id = le.event_id
              {playable_join}
             WHERE datetime(le.start_utc) < datetime(?) AND datetime(le.end_utc) > datetime(?)
               AND (? OR COALESCE(le.is_placeholder, 0) = 0)
               AND (? IS NULL OR le.lane_id = ?)
             ORDER BY le.lane_id, le.start_utc
            """,
            (end.isoformat(), start.isoformat(), include_placeholders, lane_filter, lane_filter),
        ).fetchall()
        try:
            zone_name = get_setting(conn, "timezone") or "America/New_York"
            ZoneInfo(zone_name)
        except Exception:
            zone_name = "America/New_York"

    programmes = []
    for row in rows:
        item = dict(row)
        is_placeholder = bool(item.get("is_placeholder"))
        metadata = _safe_json(item.get("raw_attributes_json"))
        playable_metadata = _safe_json(item.get("xtream_metadata"))
        # XMLTV uses the enhanced title builder, so expose that exact title.
        title = build_enhanced_title(item)
        start_utc = _as_utc(item["start_utc"])
        end_utc = _as_utc(item["end_utc"])
        if end_utc <= start_utc:
            end_utc = start_utc + timedelta(hours=1)
        provider = item.get("chosen_logical_service") or item.get("chosen_provider")
        programmes.append({
            "lane_id": item["lane_id"], "event_id": item.get("event_id"),
            "title": title, "is_placeholder": is_placeholder,
            "type": "placeholder" if is_placeholder else "real_event",
            "start_utc": start_utc.isoformat(), "end_utc": end_utc.isoformat(),
            "duration_seconds": int((end_utc - start_utc).total_seconds()),
            "provider": provider, "channel_name": item.get("channel_name"),
            "padding_minutes": cfg.PADDING_MINUTES if not is_placeholder else 0,
            "xtream_category_name": playable_metadata.get("category_name") or metadata.get("category_name"),
            "xtream_category_id": playable_metadata.get("category_id") or metadata.get("category_id"),
            "xtream_stream_id": item.get("xtream_stream_id") or playable_metadata.get("stream_id") or metadata.get("stream_id"),
        })

    lanes = [{"lane_id": row["lane_id"], "name": row["name"] or f"Fruit Lane {row['lane_id']}",
              "channel_number": row["logical_number"]} for row in lane_rows]
    return jsonify({
        "source": "lanes + lane_events (same final records exported to multisource_lanes.xml)",
        "timezone": zone_name, "window": {"start_utc": start.isoformat(), "end_utc": end.isoformat()},
        "lanes": lanes, "programmes": programmes,
        "summary": {"lane_count": len(lanes), "real_events": sum(not p["is_placeholder"] for p in programmes),
                    "placeholders": sum(p["is_placeholder"] for p in programmes),
                    "last_xmltv_generation": _output_timestamp(),
                    "last_lane_schedule_generation": _output_timestamp()},
    })
