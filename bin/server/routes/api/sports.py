"""Metadata-first Sports Rules, coverage, resolver, and health APIs."""
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request

from db.connection import db_exists, get_conn
from sports_metadata import (applicable_rule, coverage, ensure_schema, resolve_source_event,
                             save_rule, normalize_provider)
from local_ai_event_parser import clear_cache as clear_local_ai_cache
from sports_catalog import apply_catalog_records
from catalog_workbench import (add_alias, apply_all as apply_all_catalog_proposals,
                               apply_proposal as apply_catalog_proposal, entity_state,
                               cancel_run, merge_entities, run_ai_review, set_archived,
                               set_entity_fields, undo_merge, edit_proposal, merge_details,
                               detach_merge_relationship, save_source_mapping)

try:
    from db.preferences import get_setting
except ImportError:
    def get_setting(conn, key, fallback=None):
        return fallback

bp = Blueprint("sports_api", __name__)


def _prepare_write(conn):
    """Schema/materialization boundary for explicit write tools only.

    My Sports GETs intentionally do not call this helper.  The refresh/import
    pipeline materializes canonical records before the UI reads them.
    """
    ensure_schema(conn)


@bp.route("/api/sports/catalog")
def catalog():
    if not db_exists(): return jsonify({"ok": False, "error": "Database not found"}), 404
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        sports = [dict(r) for r in conn.execute("SELECT * FROM sports ORDER BY name")]
        leagues = [dict(r) for r in conn.execute("SELECT * FROM leagues ORDER BY name")]
        teams = [dict(r) for r in conn.execute("SELECT * FROM teams ORDER BY name")]
        aliases = [dict(r) for r in conn.execute("SELECT entity_type,fruit_id,alias,source,confidence,operator_confirmed,last_verified_utc FROM catalog_aliases ORDER BY entity_type,alias")]
        provenance = [dict(r) for r in conn.execute("SELECT entity_type,fruit_id,source,external_id,source_url,details_json,operator_confirmed,last_verified_utc FROM catalog_entity_provenance ORDER BY entity_type,source,external_id")]
        source_mappings = [dict(r) for r in conn.execute("SELECT source,entity_type,source_id,canonical_id,confidence,manual,evidence_json,last_seen_utc FROM source_entity_mappings ORDER BY entity_type,source,source_id")]
        racing_events = [dict(r) for r in conn.execute("SELECT * FROM catalog_recurring_events ORDER BY name")]
        for entity_type, entities in (("sport", sports), ("league", leagues), ("team", teams), ("racing_event", racing_events)):
            for entity in entities: entity["state"] = entity_state(conn, entity_type, entity["id"])
        proposals = [dict(r) for r in conn.execute("SELECT id,run_id,entity_type,action,target_id,payload_json,evidence_json,confidence,status,result_json,created_utc,decided_utc FROM catalog_change_proposals ORDER BY id DESC LIMIT 250")]
        runs = [dict(r) for r in conn.execute("SELECT * FROM catalog_change_runs ORDER BY id DESC LIMIT 25")]
        merges = [dict(r) for r in conn.execute("SELECT id,entity_type,survivor_id,source_id,status,created_utc,undone_utc FROM catalog_merge_groups ORDER BY id DESC LIMIT 250")]
    return jsonify({"ok": True, "sports": sports, "leagues": leagues, "teams": teams,
                    "racing_events": racing_events, "aliases": aliases, "provenance": provenance, "source_mappings": source_mappings,
                    "proposals": proposals, "runs": runs, "merges": merges,
                    "materialization": "refresh_pipeline"})


@bp.route("/api/sports/catalog/entities", methods=["POST"])
def catalog_create_entity():
    if not db_exists(): return jsonify({"ok": False, "error": "Database not found"}), 404
    body = request.get_json(silent=True) or {}
    record = {"entity_type": body.get("entity_type"), "name": body.get("name"), "sport": body.get("sport"),
              "league": body.get("league"), "aliases": body.get("aliases") or [], "source": "manual", "operator_confirmed": True}
    with get_conn() as conn:
        _prepare_write(conn)
        result = apply_catalog_records(conn, [record], dry_run=False)
    if result["invalid"] or result["conflicts"]: return jsonify({"ok": False, "error": "catalog record rejected", "result": result}), 400
    return jsonify({"ok": True, "result": result}), 201


@bp.route("/api/sports/catalog/entities/<entity_type>/<fruit_id>", methods=["PATCH", "DELETE"])
def catalog_entity(entity_type, fruit_id):
    if not db_exists(): return jsonify({"ok": False, "error": "Database not found"}), 404
    with get_conn() as conn:
        _prepare_write(conn)
        try:
            if request.method == "DELETE": set_archived(conn, entity_type=entity_type, fruit_id=fruit_id, archived=True)
            else: set_entity_fields(conn, entity_type=entity_type, fruit_id=fruit_id, fields=request.get_json(silent=True) or {})
            conn.commit()
        except ValueError as exc: return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True})


@bp.route("/api/sports/catalog/entities/<entity_type>/<fruit_id>/restore", methods=["POST"])
def catalog_restore(entity_type, fruit_id):
    if not db_exists(): return jsonify({"ok": False, "error": "Database not found"}), 404
    with get_conn() as conn:
        _prepare_write(conn)
        try: set_archived(conn, entity_type=entity_type, fruit_id=fruit_id, archived=False); conn.commit()
        except ValueError as exc: return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True})


@bp.route("/api/sports/catalog/entities/<entity_type>/<fruit_id>/aliases", methods=["POST"])
def catalog_alias(entity_type, fruit_id):
    if not db_exists(): return jsonify({"ok": False, "error": "Database not found"}), 404
    with get_conn() as conn:
        _prepare_write(conn)
        try: add_alias(conn, entity_type=entity_type, fruit_id=fruit_id, alias=(request.get_json(silent=True) or {}).get("alias")); conn.commit()
        except ValueError as exc: return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True})


@bp.route("/api/sports/catalog/source-mappings", methods=["PUT"])
def catalog_source_mapping():
    if not db_exists(): return jsonify({"ok": False, "error": "Database not found"}), 404
    body = request.get_json(silent=True) or {}
    with get_conn() as conn:
        _prepare_write(conn)
        try:
            save_source_mapping(conn, entity_type=str(body.get("entity_type", "")), source=body.get("source"), source_id=body.get("source_id"), canonical_id=body.get("canonical_id"), confidence=body.get("confidence", 1)); conn.commit()
        except ValueError as exc: return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True})


@bp.route("/api/sports/catalog/merges", methods=["POST"])
def catalog_merge():
    if not db_exists(): return jsonify({"ok": False, "error": "Database not found"}), 404
    body = request.get_json(silent=True) or {}
    with get_conn() as conn:
        _prepare_write(conn)
        try:
            merge_id = merge_entities(conn, entity_type=str(body.get("entity_type", "")), survivor_id=str(body.get("survivor_id", "")), source_id=str(body.get("source_id", ""))); conn.commit()
        except ValueError as exc: return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "merge_id": merge_id}), 201


@bp.route("/api/sports/catalog/merges/<int:merge_id>/undo", methods=["POST"])
def catalog_undo_merge(merge_id):
    if not db_exists(): return jsonify({"ok": False, "error": "Database not found"}), 404
    with get_conn() as conn:
        _prepare_write(conn)
        try: undo_merge(conn, merge_id); conn.commit()
        except ValueError as exc: return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True})


@bp.route("/api/sports/catalog/merges/<int:merge_id>")
def catalog_merge_details(merge_id):
    if not db_exists(): return jsonify({"ok": False, "error": "Database not found"}), 404
    with get_conn() as conn:
        _prepare_write(conn)
        try: result = merge_details(conn, merge_id)
        except ValueError as exc: return jsonify({"ok": False, "error": str(exc)}), 404
    return jsonify({"ok": True, "merge": result})


@bp.route("/api/sports/catalog/merges/<int:merge_id>/detach", methods=["POST"])
def catalog_detach_merge_relationship(merge_id):
    if not db_exists(): return jsonify({"ok": False, "error": "Database not found"}), 404
    body = request.get_json(silent=True) or {}
    with get_conn() as conn:
        _prepare_write(conn)
        try: detached = detach_merge_relationship(conn, merge_id=merge_id, relationship=str(body.get("relationship", "")), rowids=body.get("rowids") or []); conn.commit()
        except ValueError as exc: return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "detached": detached})


@bp.route("/api/sports/catalog/proposals/apply-all", methods=["POST"])
def catalog_apply_all():
    if not db_exists(): return jsonify({"ok": False, "error": "Database not found"}), 404
    with get_conn() as conn:
        _prepare_write(conn); result = apply_all_catalog_proposals(conn); conn.commit()
    return jsonify({"ok": True, **result})


@bp.route("/api/sports/catalog/proposals/<int:proposal_id>", methods=["POST", "PATCH"])
def catalog_decide_proposal(proposal_id):
    if not db_exists(): return jsonify({"ok": False, "error": "Database not found"}), 404
    reject = bool((request.get_json(silent=True) or {}).get("reject"))
    with get_conn() as conn:
        _prepare_write(conn)
        try:
            result = (edit_proposal(conn, proposal_id, (request.get_json(silent=True) or {}).get("payload"))
                      if request.method == "PATCH" else apply_catalog_proposal(conn, proposal_id, reject=reject)); conn.commit()
        except ValueError as exc: return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "proposal": result})


@bp.route("/api/sports/catalog/review", methods=["POST"])
def catalog_review():
    """Explicit compatible-AI review.  It queues recommendations only."""
    if not db_exists(): return jsonify({"ok": False, "error": "Database not found"}), 404
    def worker():
        with get_conn() as conn:
            _prepare_write(conn); run_ai_review(conn, source="manual"); conn.commit()
    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True, "status": "started"}), 202


@bp.route("/api/sports/catalog/runs/<int:run_id>/cancel", methods=["POST"])
def catalog_cancel_run(run_id):
    if not db_exists(): return jsonify({"ok": False, "error": "Database not found"}), 404
    with get_conn() as conn:
        _prepare_write(conn); cancelled = cancel_run(conn, run_id); conn.commit()
    return jsonify({"ok": True, "cancelled": cancelled})


@bp.route("/api/sports/catalog/runs/<int:run_id>/resume", methods=["POST"])
def catalog_resume_run(run_id):
    if not db_exists(): return jsonify({"ok": False, "error": "Database not found"}), 404
    def worker():
        with get_conn() as conn:
            _prepare_write(conn); run_ai_review(conn, source="manual", resume_run_id=run_id); conn.commit()
    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True, "status": "started"}), 202


@bp.route("/api/sports/rules", methods=["GET", "POST", "DELETE"])
def rules():
    if not db_exists(): return jsonify({"ok": False, "error": "Database not found"}), 404
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        if request.method == "POST":
            _prepare_write(conn)
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
        # Rules store canonical IDs; supply their friendly materialized labels
        # without turning the browser into a write/synchronization boundary.
        labels = {}
        for target_type, table in (("sport", "sports"), ("league", "leagues"),
                                   ("team", "teams"), ("competition", "catalog_recurring_events")):
            labels[target_type] = {str(row[0]): str(row[1]) for row in conn.execute(f"SELECT id,name FROM {table}")}
        labels["event"] = {str(row[0]): str(row[1] or row[0]) for row in conn.execute("SELECT id,title FROM canonical_events")}
        for row in rows:
            row["target_name"] = labels.get(row["target_type"], {}).get(str(row["target_id"]), row["target_id"])
    return jsonify({"ok": True, "rules": rows})


@bp.route("/api/sports/coverage")
def upcoming_coverage():
    if not db_exists(): return jsonify({"ok": False, "error": "Database not found"}), 404
    days = min(max(request.args.get("days", 14, type=int), 1), 90)
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        items = coverage(conn, days=days)
        display_timezone = get_setting(conn, "timezone") or os.getenv("FRUIT_TIMEZONE") or os.getenv("TZ") or "UTC"
    summary = {"wanted": len(items), "ready": sum(x["coverage_state"] == "scheduled" for x in items),
               "awaiting_source": sum(x["coverage_state"] == "awaiting_source" for x in items)}
    return jsonify({"ok": True, "days": days, "items": items, "summary": summary,
                    "display_timezone": display_timezone, "timestamp_contract": "absolute_utc"})


@bp.route("/api/sports/events/<canonical_event_id>")
def inspect_event(canonical_event_id):
    if not db_exists(): return jsonify({"ok": False, "error": "Database not found"}), 404
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        event = conn.execute("SELECT ce.*,s.name AS sport,l.name AS league FROM canonical_events ce LEFT JOIN sports s ON s.id=ce.sport_id LEFT JOIN leagues l ON l.id=ce.league_id WHERE ce.id=?", (canonical_event_id,)).fetchone()
        if not event: return jsonify({"ok": False, "error": "Not found"}), 404
        sources = [dict(r) for r in conn.execute("SELECT source,source_event_id,confidence,resolution_kind,evidence_json,last_seen_utc FROM source_event_records WHERE canonical_event_id=?", (canonical_event_id,))]
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        # Keep the untrusted AI interpretation visibly separate from Fruit's
        # canonical resolution fields.  Cache rows intentionally contain no
        # provider credentials or transport URLs.  Fetch this event's cache
        # rows in one query: a merged event commonly has several providers.
        ai_by_source = {}
        if sources and "local_ai_event_cache" in tables:
            conditions = " OR ".join("(provider=? AND source_event_id=?)" for _ in sources)
            values = [value for source_row in sources
                      for value in (source_row["source"], source_row["source_event_id"])]
            for ai_row in conn.execute(
                "SELECT model,result_json,confidence,validation_status,failure_kind,parsed_utc,provider,source_event_id "
                f"FROM local_ai_event_cache WHERE {conditions} ORDER BY updated_utc DESC",
                values,
            ):
                item = dict(ai_row)
                ai_by_source.setdefault((item.pop("provider"), item.pop("source_event_id")), item)
        for source_row in sources:
            ai_row = ai_by_source.get((source_row["source"], source_row["source_event_id"]))
            if ai_row:
                item = dict(ai_row)
                item["interpretation"] = _safe_metadata(item.pop("result_json"))
                source_row["local_ai_interpretation"] = item
            source_row["resolver_evidence"] = _safe_metadata(source_row.pop("evidence_json", "{}"))
        participants = [dict(r) for r in conn.execute("SELECT p.*,t.name AS canonical_team FROM canonical_event_participants p LEFT JOIN teams t ON t.id=p.team_id WHERE p.event_id=?", (canonical_event_id,))]
        rule = applicable_rule(conn, canonical_event_id)
        source_ids = [row["source_event_id"] for row in sources]
        playables = []; lanes = []
        if source_ids:
            marks = ",".join("?" for _ in source_ids)
            if "playables" in tables: playables = [dict(r) for r in conn.execute(f"SELECT event_id,playable_id,provider,logical_service,service_name,priority FROM playables WHERE event_id IN ({marks})", source_ids)]
            if "lane_events" in tables: lanes = [dict(r) for r in conn.execute(f"SELECT lane_id,event_id,start_utc,end_utc,chosen_playable_id,chosen_provider FROM lane_events WHERE event_id IN ({marks}) AND COALESCE(is_placeholder,0)=0", source_ids)]
        decisions = [dict(r) for r in conn.execute("SELECT * FROM scheduling_decisions WHERE canonical_event_id=? ORDER BY generation_utc DESC LIMIT 20", (canonical_event_id,))]
        capacities = [dict(r) for r in conn.execute("SELECT provider,max_concurrent,updated_utc FROM provider_capacities ORDER BY provider")] if "provider_capacities" in tables else []
        display_timezone = get_setting(conn, "timezone") or os.getenv("FRUIT_TIMEZONE") or os.getenv("TZ") or "UTC"
    event_data = dict(event)
    event_data["metadata"] = _safe_metadata(event_data.pop("metadata_json", "{}"))
    return jsonify({"ok": True, "event": event_data, "participants": participants, "source_records": sources, "applicable_rule": rule, "lanes": lanes, "playables": playables, "scheduling_decisions": decisions, "provider_capacities": capacities, "diagnostics": {"pipeline": ["provider observation", "deterministic/catalog resolution", "canonical event", "attached playables", "sports rule", "scheduler decision"], "source_resolution_count": len(sources), "selected_playable_count": len(playables)}, "time_resolution": {"raw": event_data["metadata"].get("raw_start"), "canonical_utc": event_data["start_utc"], "display_timezone": display_timezone, "contract": "absolute_utc"}})


@bp.route("/api/sports/resolver", methods=["POST"])
def resolver_bench():
    """Resolve an inspector-supplied source record; explicit source IDs are persistent mappings."""
    if not db_exists(): return jsonify({"ok": False, "error": "Database not found"}), 404
    body = request.get_json(silent=True) or {}
    if not body.get("source") or not body.get("source_event_id"):
        return jsonify({"ok": False, "error": "source and source_event_id are required"}), 400
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row; _prepare_write(conn)
        result = resolve_source_event(conn, source=str(body["source"]), source_event_id=str(body["source_event_id"]), data=body.get("event") or {})
    return jsonify({"ok": True, "result": result})


@bp.route("/api/sports/local-ai/cache/clear", methods=["POST"])
def clear_local_ai_interpretations():
    """Explicitly clear local-AI parser cache so a later sync reparses data."""
    if not db_exists(): return jsonify({"ok": False, "error": "Database not found"}), 404
    body = request.get_json(silent=True) or {}
    provider = str(body.get("provider") or "").strip().casefold() or None
    source_event_id = str(body.get("source_event_id") or "").strip() or None
    with get_conn() as conn:
        if request.method == "POST":
            _prepare_write(conn)
        cleared = clear_local_ai_cache(conn, provider=provider, source_event_id=source_event_id)
        conn.commit()
    return jsonify({"ok": True, "cleared": cleared, "reparse": "Run the next refresh or resolver request to reparse eligible records."})


@bp.route("/api/sports/mappings", methods=["POST"])
def manual_mapping():
    """Store an operator-confirmed source-event association above inference."""
    if not db_exists(): return jsonify({"ok": False, "error": "Database not found"}), 404
    body = request.get_json(silent=True) or {}
    source, source_event_id, canonical_event_id = (str(body.get(key, "")).strip() for key in ("source", "source_event_id", "canonical_event_id"))
    if not all((source, source_event_id, canonical_event_id)):
        return jsonify({"ok": False, "error": "source, source_event_id, and canonical_event_id are required"}), 400
    with get_conn() as conn:
        _prepare_write(conn)
        if not conn.execute("SELECT 1 FROM canonical_events WHERE id=?", (canonical_event_id,)).fetchone():
            return jsonify({"ok": False, "error": "Canonical event not found"}), 404
        conn.execute("INSERT INTO source_event_records(source,source_event_id,canonical_event_id,confidence,resolution_kind,evidence_json,raw_json,last_seen_utc) VALUES(?,?,?,?,?,?,?,datetime('now')) "
                     "ON CONFLICT(source,source_event_id) DO UPDATE SET canonical_event_id=excluded.canonical_event_id,confidence=1,resolution_kind='manual_override',evidence_json=excluded.evidence_json,last_seen_utc=excluded.last_seen_utc",
                     (source.casefold(), source_event_id, canonical_event_id, 1.0, "manual_override", '{"operator_confirmed":true}', '{}'))
        conn.commit()
    return jsonify({"ok": True, "resolution_kind": "manual_override"})


@bp.route("/api/sports/schedule-simulation", methods=["POST"])
def schedule_simulation():
    """Execute the actual allocator on a temporary database, never live lanes."""
    if not db_exists(): return jsonify({"ok": False, "error": "Database not found"}), 404
    body = request.get_json(silent=True) or {}
    lanes = min(max(int(body.get("lanes", 50)), 1), 500)
    days = min(max(int(body.get("days", 14)), 1), 90)
    with get_conn() as conn:
        _prepare_write(conn)
        from sports_scheduler import simulate
        result = simulate(conn, lanes, days)
    return jsonify({"ok": True, "lanes": lanes, "days": days, **result})


def _safe_metadata(value):
    import json
    try: return json.loads(value or "{}")
    except (TypeError, ValueError): return {}


@bp.route("/api/sports/provider-capacities", methods=["GET", "POST"])
def provider_capacities():
    if not db_exists(): return jsonify({"ok": False, "error": "Database not found"}), 404
    with get_conn() as conn:
        if request.method == "POST":
            _prepare_write(conn)
            body = request.get_json(silent=True) or {}
            provider = normalize_provider(body.get("provider"))
            try: maximum = int(body.get("max_concurrent"))
            except (TypeError, ValueError): maximum = 0
            if not provider or maximum < 1: return jsonify({"ok": False, "error": "provider and positive max_concurrent are required"}), 400
            conn.execute("INSERT INTO provider_capacities(provider,max_concurrent,updated_utc) VALUES(?,?,datetime('now')) ON CONFLICT(provider) DO UPDATE SET max_concurrent=excluded.max_concurrent,updated_utc=excluded.updated_utc", (provider, maximum)); conn.commit()
        rows = [dict(r) for r in conn.execute("SELECT * FROM provider_capacities ORDER BY provider")]
    return jsonify({"ok": True, "capacities": rows})


@bp.route("/api/sports/health")
def health():
    if not db_exists(): return jsonify({"ok": False, "error": "Database not found"}), 404
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        counts = {name: conn.execute(sql).fetchone()[0] for name, sql in {
            "sports": "SELECT COUNT(*) FROM sports", "leagues": "SELECT COUNT(*) FROM leagues", "teams": "SELECT COUNT(*) FROM teams",
            "upcoming_events": "SELECT COUNT(*) FROM canonical_events WHERE datetime(start_utc)>=datetime('now')",
            "unresolved_source_records": "SELECT COUNT(*) FROM source_event_records WHERE confidence < .85",
            "invalid_or_naive_timestamps": "SELECT COUNT(*) FROM canonical_events WHERE start_utc NOT LIKE '%Z'",
        }.items()}
        counts["wanted_without_playable"] = sum(x["coverage_state"] == "awaiting_source" for x in coverage(conn))
    return jsonify({"ok": True, "metrics": counts})
