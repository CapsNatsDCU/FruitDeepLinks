"""Metadata-first Sports Rules, coverage, resolver, and health APIs."""
import sqlite3
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request

from db.connection import db_exists, get_conn
from sports_metadata import (applicable_rule, coverage, ensure_schema, resolve_source_event,
                             save_rule, sync_legacy_events)

bp = Blueprint("sports_api", __name__)


def _prepare(conn):
    ensure_schema(conn)
    # Backfill is idempotent and lets the UI expose metadata discovered by the
    # existing Apple/Xtream pipeline without requiring a disruptive reimport.
    sync_legacy_events(conn)


@bp.route("/api/sports/catalog")
def catalog():
    if not db_exists(): return jsonify({"ok": False, "error": "Database not found"}), 404
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row; _prepare(conn)
        sports = [dict(r) for r in conn.execute("SELECT * FROM sports ORDER BY name")]
        leagues = [dict(r) for r in conn.execute("SELECT * FROM leagues ORDER BY name")]
        teams = [dict(r) for r in conn.execute("SELECT * FROM teams ORDER BY name")]
    return jsonify({"ok": True, "sports": sports, "leagues": leagues, "teams": teams})


@bp.route("/api/sports/rules", methods=["GET", "POST", "DELETE"])
def rules():
    if not db_exists(): return jsonify({"ok": False, "error": "Database not found"}), 404
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row; _prepare(conn)
        if request.method == "POST":
            body = request.get_json(silent=True) or {}
            try:
                rule_id = save_rule(conn, target_type=str(body.get("target_type", "")), target_id=str(body.get("target_id", "")),
                                    policy=str(body.get("policy", "NORMAL")), event_type=body.get("event_type"),
                                    broadcasts=body.get("broadcast_preferences") or [])
            except ValueError as exc:
                return jsonify({"ok": False, "error": str(exc)}), 400
            return jsonify({"ok": True, "id": rule_id}), 201
        if request.method == "DELETE":
            rule_id = request.args.get("id", type=int)
            if not rule_id: return jsonify({"ok": False, "error": "id is required"}), 400
            conn.execute("UPDATE sports_rules SET enabled=0,updated_utc=datetime('now') WHERE id=?", (rule_id,)); conn.commit()
            return jsonify({"ok": True})
        rows = [dict(r) for r in conn.execute("SELECT * FROM sports_rules WHERE enabled=1 ORDER BY target_type,target_id,id")]
    return jsonify({"ok": True, "rules": rows})


@bp.route("/api/sports/coverage")
def upcoming_coverage():
    if not db_exists(): return jsonify({"ok": False, "error": "Database not found"}), 404
    days = min(max(request.args.get("days", 14, type=int), 1), 90)
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row; _prepare(conn); items = coverage(conn, days=days)
    summary = {"wanted": len(items), "ready": sum(x["coverage_state"] == "scheduled" for x in items),
               "awaiting_source": sum(x["coverage_state"] == "awaiting_source" for x in items)}
    return jsonify({"ok": True, "days": days, "items": items, "summary": summary})


@bp.route("/api/sports/events/<canonical_event_id>")
def inspect_event(canonical_event_id):
    if not db_exists(): return jsonify({"ok": False, "error": "Database not found"}), 404
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row; _prepare(conn)
        event = conn.execute("SELECT ce.*,s.name AS sport,l.name AS league FROM canonical_events ce LEFT JOIN sports s ON s.id=ce.sport_id LEFT JOIN leagues l ON l.id=ce.league_id WHERE ce.id=?", (canonical_event_id,)).fetchone()
        if not event: return jsonify({"ok": False, "error": "Not found"}), 404
        sources = [dict(r) for r in conn.execute("SELECT source,source_event_id,confidence,resolution_kind,evidence_json,last_seen_utc FROM source_event_records WHERE canonical_event_id=?", (canonical_event_id,))]
        participants = [dict(r) for r in conn.execute("SELECT p.*,t.name AS canonical_team FROM canonical_event_participants p LEFT JOIN teams t ON t.id=p.team_id WHERE p.event_id=?", (canonical_event_id,))]
        rule = applicable_rule(conn, canonical_event_id)
    return jsonify({"ok": True, "event": dict(event), "participants": participants, "source_records": sources, "applicable_rule": rule})


@bp.route("/api/sports/resolver", methods=["POST"])
def resolver_bench():
    """Resolve an inspector-supplied source record; explicit source IDs are persistent mappings."""
    if not db_exists(): return jsonify({"ok": False, "error": "Database not found"}), 404
    body = request.get_json(silent=True) or {}
    if not body.get("source") or not body.get("source_event_id"):
        return jsonify({"ok": False, "error": "source and source_event_id are required"}), 400
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row; _prepare(conn)
        result = resolve_source_event(conn, source=str(body["source"]), source_event_id=str(body["source_event_id"]), data=body.get("event") or {})
    return jsonify({"ok": True, "result": result})


@bp.route("/api/sports/health")
def health():
    if not db_exists(): return jsonify({"ok": False, "error": "Database not found"}), 404
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row; _prepare(conn)
        counts = {name: conn.execute(sql).fetchone()[0] for name, sql in {
            "sports": "SELECT COUNT(*) FROM sports", "leagues": "SELECT COUNT(*) FROM leagues", "teams": "SELECT COUNT(*) FROM teams",
            "upcoming_events": "SELECT COUNT(*) FROM canonical_events WHERE datetime(start_utc)>=datetime('now')",
            "unresolved_source_records": "SELECT COUNT(*) FROM source_event_records WHERE confidence < .85",
            "invalid_or_naive_timestamps": "SELECT COUNT(*) FROM canonical_events WHERE start_utc NOT LIKE '%Z'",
        }.items()}
        counts["wanted_without_playable"] = sum(x["coverage_state"] == "awaiting_source" for x in coverage(conn))
    return jsonify({"ok": True, "metrics": counts})
