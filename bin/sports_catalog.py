"""Offline-first sports knowledge catalog and safe source-update primitives.

This module owns neither schedules nor provider credentials.  It stores stable
Fruit identities already used by :mod:`sports_metadata`, plus imported aliases
and provenance which make the runtime resolver local, fast, and auditable.
External sources are only consulted by the explicit updater.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping


_WORDS = re.compile(r"[^a-z0-9]+")
ENTITY_TYPES = frozenset({"sport", "league", "team", "racing_event"})


def normalize(value: Any) -> str:
    """Normalize names without turning short nicknames into identities."""
    text = re.sub(r"\b(?:[a-zA-Z]\.){2,}", lambda match: match.group(0).replace(".", ""), str(value or ""))
    return _WORDS.sub(" ", text.casefold()).strip()


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _key(prefix: str, *values: Any) -> str:
    material = "|".join(normalize(value) for value in values)
    return f"{prefix}_{hashlib.sha256(material.encode('utf-8')).hexdigest()[:20]}"


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Install additive catalog tables and indexes; never delete catalog data."""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS catalog_entity_provenance (
      entity_type TEXT NOT NULL CHECK(entity_type IN ('sport','league','team','racing_event')),
      fruit_id TEXT NOT NULL,
      source TEXT NOT NULL,
      external_id TEXT NOT NULL,
      source_url TEXT,
      details_json TEXT NOT NULL DEFAULT '{}',
      operator_confirmed INTEGER NOT NULL DEFAULT 0,
      first_seen_utc TEXT NOT NULL,
      last_verified_utc TEXT NOT NULL,
      PRIMARY KEY(source, entity_type, external_id),
      UNIQUE(entity_type, fruit_id, source, external_id)
    );
    CREATE TABLE IF NOT EXISTS catalog_aliases (
      entity_type TEXT NOT NULL CHECK(entity_type IN ('sport','league','team','racing_event')),
      fruit_id TEXT NOT NULL,
      alias TEXT NOT NULL,
      normalized_alias TEXT NOT NULL,
      source TEXT NOT NULL,
      confidence REAL NOT NULL DEFAULT 1 CHECK(confidence >= 0 AND confidence <= 1),
      operator_confirmed INTEGER NOT NULL DEFAULT 0,
      last_verified_utc TEXT NOT NULL,
      PRIMARY KEY(entity_type, fruit_id, normalized_alias, source)
    );
    CREATE TABLE IF NOT EXISTS catalog_recurring_events (
      id TEXT PRIMARY KEY,
      sport_id TEXT REFERENCES sports(id),
      league_id TEXT REFERENCES leagues(id),
      name TEXT NOT NULL,
      normalized_name TEXT NOT NULL,
      venue_aliases_json TEXT NOT NULL DEFAULT '[]',
      session_vocabulary_json TEXT NOT NULL DEFAULT '[]',
      created_utc TEXT NOT NULL,
      updated_utc TEXT NOT NULL,
      UNIQUE(league_id, normalized_name)
    );
    CREATE TABLE IF NOT EXISTS catalog_import_runs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      source TEXT NOT NULL,
      dry_run INTEGER NOT NULL,
      summary_json TEXT NOT NULL,
      created_utc TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_catalog_alias_lookup
      ON catalog_aliases(entity_type, normalized_alias, fruit_id);
    CREATE INDEX IF NOT EXISTS idx_catalog_provenance_fruit
      ON catalog_entity_provenance(entity_type, fruit_id);
    """)


def _coerce_aliases(values: Any) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Iterable):
        return []
    seen: dict[str, str] = {}
    for value in values:
        text = " ".join(str(value or "").split())
        if normalize(text):
            seen.setdefault(normalize(text), text)
    return [seen[key] for key in sorted(seen)]


def _external_ids(record: Mapping[str, Any]) -> dict[str, str]:
    value = record.get("external_ids") or {}
    if not isinstance(value, Mapping):
        return {}
    return {str(source).strip().casefold(): str(identifier).strip()
            for source, identifier in value.items() if str(source).strip() and str(identifier).strip()}


def _lookup_id(conn: sqlite3.Connection, entity_type: str, name: str, *, sport_id: str | None = None,
               league_id: str | None = None) -> str | None:
    normalized = normalize(name)
    if entity_type == "sport":
        row = conn.execute("SELECT id FROM sports WHERE normalized_name=?", (normalized,)).fetchone()
    elif entity_type == "league":
        row = conn.execute("SELECT id FROM leagues WHERE sport_id IS ? AND normalized_name=?", (sport_id, normalized)).fetchone()
    elif entity_type == "team":
        row = conn.execute("SELECT id FROM teams WHERE league_id IS ? AND normalized_name=?", (league_id, normalized)).fetchone()
    else:
        row = conn.execute("SELECT id FROM catalog_recurring_events WHERE league_id IS ? AND normalized_name=?", (league_id, normalized)).fetchone()
    return str(row[0]) if row else None


def _stable_id(entity_type: str, name: str, *, sport_id: str | None = None, league_id: str | None = None) -> str:
    prefixes = {"sport": "sport", "league": "league", "team": "team", "racing_event": "racing_event"}
    scope = sport_id if entity_type == "league" else league_id if entity_type in {"team", "racing_event"} else None
    return _key(prefixes[entity_type], scope, name)


def _upsert_base(conn: sqlite3.Connection, entity_type: str, name: str, *, sport_id: str | None = None,
                 league_id: str | None = None, venue_aliases: Iterable[str] = (), session_vocabulary: Iterable[str] = ()) -> str:
    """Create a Fruit-owned entity ID once; subsequent imports do not rename it."""
    existing = _lookup_id(conn, entity_type, name, sport_id=sport_id, league_id=league_id)
    if existing:
        return existing
    now = utc_now()
    identifier = _stable_id(entity_type, name, sport_id=sport_id, league_id=league_id)
    normalized = normalize(name)
    if entity_type == "sport":
        conn.execute("INSERT INTO sports(id,name,normalized_name,created_utc,updated_utc) VALUES(?,?,?,?,?)",
                     (identifier, name, normalized, now, now))
    elif entity_type == "league":
        conn.execute("INSERT INTO leagues(id,sport_id,name,normalized_name,created_utc,updated_utc) VALUES(?,?,?,?,?,?)",
                     (identifier, sport_id, name, normalized, now, now))
    elif entity_type == "team":
        conn.execute("INSERT INTO teams(id,sport_id,league_id,name,normalized_name,aliases_json,created_utc,updated_utc) VALUES(?,?,?,?,?,?,?,?)",
                     (identifier, sport_id, league_id, name, normalized, "[]", now, now))
    else:
        conn.execute("INSERT INTO catalog_recurring_events(id,sport_id,league_id,name,normalized_name,venue_aliases_json,session_vocabulary_json,created_utc,updated_utc) VALUES(?,?,?,?,?,?,?,?,?)",
                     (identifier, sport_id, league_id, name, normalized, json.dumps(_coerce_aliases(venue_aliases)),
                      json.dumps(_coerce_aliases(session_vocabulary)), now, now))
    return identifier


def _provenance_conflict(conn: sqlite3.Connection, *, entity_type: str, source: str, external_id: str,
                         fruit_id: str) -> str | None:
    row = conn.execute("SELECT fruit_id FROM catalog_entity_provenance WHERE source=? AND entity_type=? AND external_id=?",
                       (source, entity_type, external_id)).fetchone()
    return str(row[0]) if row and str(row[0]) != fruit_id else None


def _record_plan(conn: sqlite3.Connection, record: Mapping[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    entity_type = str(record.get("entity_type") or "").strip().casefold()
    name = " ".join(str(record.get("name") or "").split())
    if entity_type not in ENTITY_TYPES or not name:
        return None, {"kind": "invalid", "record": dict(record), "reason": "entity_type_and_name_required"}
    sport_name = record.get("sport")
    league_name = record.get("league")
    sport_id = _lookup_id(conn, "sport", str(sport_name)) if sport_name else None
    league_id = _lookup_id(conn, "league", str(league_name), sport_id=sport_id) if league_name else None
    if entity_type == "league" and sport_name and not sport_id:
        sport_id = _stable_id("sport", str(sport_name))
    if entity_type in {"team", "racing_event"} and league_name and not league_id:
        league_id = _stable_id("league", str(league_name), sport_id=sport_id)
    fruit_id = _lookup_id(conn, entity_type, name, sport_id=sport_id, league_id=league_id) or _stable_id(
        entity_type, name, sport_id=sport_id, league_id=league_id)
    source = str(record.get("source") or "manual").strip().casefold()
    for external_source, external_id in _external_ids(record).items():
        conflict = _provenance_conflict(conn, entity_type=entity_type, source=external_source, external_id=external_id, fruit_id=fruit_id)
        if conflict:
            return None, {"kind": "conflict", "entity_type": entity_type, "name": name, "source": external_source,
                          "external_id": external_id, "existing_fruit_id": conflict, "requested_fruit_id": fruit_id,
                          "reason": "external_id_already_maps_to_a_different_fruit_entity"}
    return {"record": dict(record), "entity_type": entity_type, "name": name, "sport_id": sport_id, "league_id": league_id,
            "fruit_id": fruit_id, "source": source, "exists": bool(_lookup_id(conn, entity_type, name, sport_id=sport_id, league_id=league_id))}, None


def preview_catalog_records(conn: sqlite3.Connection, records: Iterable[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Compare source records without changing the database or upstream mapping."""
    ensure_schema(conn)
    result: dict[str, list[dict[str, Any]]] = {"additions": [], "updates": [], "conflicts": [], "invalid": []}
    for record in records:
        plan, problem = _record_plan(conn, record)
        if problem:
            result["conflicts" if problem["kind"] == "conflict" else "invalid"].append(problem)
        elif plan:
            result["updates" if plan["exists"] else "additions"].append({key: plan[key] for key in ("entity_type", "name", "fruit_id", "source")})
    return result


def _ensure_parent_entities(conn: sqlite3.Connection, record: Mapping[str, Any]) -> tuple[str | None, str | None]:
    sport_name = " ".join(str(record.get("sport") or "").split())
    league_name = " ".join(str(record.get("league") or "").split())
    sport_id = _upsert_base(conn, "sport", sport_name) if sport_name else None
    league_id = _upsert_base(conn, "league", league_name, sport_id=sport_id) if league_name else None
    return sport_id, league_id


def _upsert_alias(conn: sqlite3.Connection, *, entity_type: str, fruit_id: str, alias: str, source: str,
                  confidence: float, operator_confirmed: bool) -> None:
    normalized = normalize(alias)
    if not normalized:
        return
    conn.execute("INSERT INTO catalog_aliases(entity_type,fruit_id,alias,normalized_alias,source,confidence,operator_confirmed,last_verified_utc) VALUES(?,?,?,?,?,?,?,?) "
                 "ON CONFLICT(entity_type,fruit_id,normalized_alias,source) DO UPDATE SET alias=excluded.alias,confidence=MAX(catalog_aliases.confidence,excluded.confidence),operator_confirmed=MAX(catalog_aliases.operator_confirmed,excluded.operator_confirmed),last_verified_utc=excluded.last_verified_utc",
                 (entity_type, fruit_id, alias, normalized, source, max(0.0, min(1.0, float(confidence))), int(operator_confirmed), utc_now()))


def _safe_imported_alias(alias: str, canonical_name: str, *, operator_confirmed: bool) -> bool:
    """Reject generic upstream mascot fragments unless an operator confirms them.

    A catalog source can truthfully call a club the "Capitals" while that text
    is still unsafe resolver evidence.  Full names, multi-word aliases, and
    compact all-caps abbreviations remain useful; manual mappings may elect to
    accept a shorter alias deliberately.
    """
    if operator_confirmed or normalize(alias) == normalize(canonical_name):
        return True
    words = normalize(alias).split()
    return len(words) >= 2 or (alias.strip().isupper() and 2 <= len(alias.strip()) <= 5)


def apply_catalog_records(conn: sqlite3.Connection, records: Iterable[Mapping[str, Any]], *, dry_run: bool = True) -> dict[str, list[dict[str, Any]]]:
    """Apply non-destructive catalog additions after a conflict-free preview.

    There are intentionally no automatic deletions or entity merges.  A source
    external ID which points at another Fruit ID is a conflict requiring an
    operator decision, not a data-cleanup opportunity.
    """
    materialized = [dict(record) for record in records]
    preview = preview_catalog_records(conn, materialized)
    if dry_run or preview["conflicts"] or preview["invalid"]:
        return preview
    ensure_schema(conn)
    conn.execute("SAVEPOINT catalog_import")
    try:
        for record in materialized:
            plan, problem = _record_plan(conn, record)
            if problem or not plan:
                raise ValueError("catalog plan changed while applying")
            entity_type, name = plan["entity_type"], plan["name"]
            sport_id, league_id = _ensure_parent_entities(conn, record)
            fruit_id = _upsert_base(conn, entity_type, name, sport_id=sport_id, league_id=league_id,
                                    venue_aliases=record.get("venue_aliases") or (),
                                    session_vocabulary=record.get("session_vocabulary") or ())
            source = plan["source"]
            manual = bool(record.get("operator_confirmed") or source == "manual")
            aliases = _coerce_aliases([name, *(record.get("aliases") or []),
                                       *((record.get("venue_aliases") or []) if entity_type == "racing_event" else [])])
            for alias in aliases:
                if entity_type == "team" and not _safe_imported_alias(alias, name, operator_confirmed=manual):
                    continue
                _upsert_alias(conn, entity_type=entity_type, fruit_id=fruit_id, alias=alias, source=source,
                              confidence=float(record.get("confidence", 1.0)), operator_confirmed=manual)
            for external_source, external_id in _external_ids(record).items():
                conn.execute("INSERT INTO catalog_entity_provenance(entity_type,fruit_id,source,external_id,source_url,details_json,operator_confirmed,first_seen_utc,last_verified_utc) VALUES(?,?,?,?,?,?,?,?,?) "
                             "ON CONFLICT(source,entity_type,external_id) DO UPDATE SET last_verified_utc=excluded.last_verified_utc,details_json=CASE WHEN catalog_entity_provenance.operator_confirmed=1 THEN catalog_entity_provenance.details_json ELSE excluded.details_json END,source_url=COALESCE(catalog_entity_provenance.source_url,excluded.source_url)",
                             (entity_type, fruit_id, external_source, external_id, record.get("source_url"),
                              json.dumps(record.get("provenance") or {}, sort_keys=True), int(manual), utc_now(), utc_now()))
        summary = {key: len(value) for key, value in preview.items()}
        source = ",".join(sorted({str(record.get("source") or "manual") for record in materialized})) or "manual"
        conn.execute("INSERT INTO catalog_import_runs(source,dry_run,summary_json,created_utc) VALUES(?,?,?,?)",
                     (source, 0, json.dumps(summary, sort_keys=True), utc_now()))
    except Exception:
        conn.execute("ROLLBACK TO catalog_import")
        conn.execute("RELEASE catalog_import")
        raise
    conn.execute("RELEASE catalog_import")
    conn.commit()
    return preview


def resolve_team_alias(conn: sqlite3.Connection, name: Any, *, sport_id: str | None = None,
                       league_id: str | None = None) -> dict[str, str] | None:
    """Return a team only for an unambiguous exact catalog alias in its scope."""
    normalized = normalize(name)
    if not normalized:
        return None
    rows = conn.execute("SELECT DISTINCT t.id,t.name FROM catalog_aliases a JOIN teams t ON t.id=a.fruit_id "
                        "WHERE a.entity_type='team' AND a.normalized_alias=? AND t.sport_id IS ? AND t.league_id IS ? "
                        "ORDER BY t.id", (normalized, sport_id, league_id)).fetchall()
    if len(rows) != 1:
        return None
    return {"id": str(rows[0][0]), "name": str(rows[0][1])}


def canonicalize_participants(conn: sqlite3.Connection, participants: Iterable[Mapping[str, Any]], *,
                              sport_id: str | None, league_id: str | None) -> list[dict[str, Any]]:
    """Normalize only catalog-proven, uniquely scoped team aliases.

    The original provider text is retained as evidence.  Unknown or ambiguous
    values deliberately survive unchanged, which prevents a shared mascot from
    merging unrelated minor-league teams.
    """
    result: list[dict[str, Any]] = []
    for input_value in participants:
        value = dict(input_value)
        matched = resolve_team_alias(conn, value.get("name"), sport_id=sport_id, league_id=league_id)
        if matched:
            original = str(value["name"])
            value["name"] = matched["name"]
            if normalize(original) != normalize(matched["name"]):
                value["catalog_alias"] = original
                value["catalog_team_id"] = matched["id"]
        result.append(value)
    return result


def recurring_event_aliases(conn: sqlite3.Connection, name: Any, *, league_id: str | None = None) -> dict[str, str] | None:
    """Resolve stable racing event identity locally; this does not create dates."""
    normalized = normalize(name)
    rows = conn.execute("SELECT DISTINCT e.id,e.name FROM catalog_aliases a JOIN catalog_recurring_events e ON e.id=a.fruit_id "
                        "WHERE a.entity_type='racing_event' AND a.normalized_alias=? AND e.league_id IS ? ORDER BY e.id",
                        (normalized, league_id)).fetchall()
    return {"id": str(rows[0][0]), "name": str(rows[0][1])} if len(rows) == 1 else None
