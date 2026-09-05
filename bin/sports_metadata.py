"""Metadata-first sports identity, rules, resolution, and coverage support.

This module deliberately sits beside the legacy ``events``/``playables``
tables.  Those tables keep their existing provider-facing contract, while this
module maintains Fruit-owned identities and mappings which can join multiple
providers without making a stream title the definition of an event.

All instants accepted here are converted exactly once to aware UTC datetimes.
SQLite stores ISO-8601 UTC strings ending in ``Z``.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional


UTC = timezone.utc
POLICIES = frozenset({"IGNORE", "NORMAL", "PRIORITIZE", "ALWAYS_SCHEDULE"})
_WORDS = re.compile(r"[^a-z0-9]+")


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def utc_instant(value: Any, *, source_timezone: str | None = None) -> datetime | None:
    """Convert an epoch or ISO-8601 input to one absolute UTC instant.

    A naive value is intentionally rejected unless a source timezone was
    explicitly supplied: treating an unspecified wall clock as UTC is the
    common cause of hidden double-conversion bugs.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            if not source_timezone:
                return None
            from zoneinfo import ZoneInfo
            value = value.replace(tzinfo=ZoneInfo(source_timezone))
        return value.astimezone(UTC)
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().lstrip("-").isdigit()):
        number = float(value)
        # Apple uses milliseconds.  Seconds and milliseconds are both epochs,
        # so neither receives a presentation-timezone conversion.
        if abs(number) > 100_000_000_000:
            number /= 1000
        return datetime.fromtimestamp(number, UTC)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        if not source_timezone:
            return None
        from zoneinfo import ZoneInfo
        parsed = parsed.replace(tzinfo=ZoneInfo(source_timezone))
    return parsed.astimezone(UTC)


def utc_text(value: Any, *, source_timezone: str | None = None) -> str | None:
    parsed = utc_instant(value, source_timezone=source_timezone)
    return parsed.replace(microsecond=0).isoformat().replace("+00:00", "Z") if parsed else None


def _norm(value: Any) -> str:
    # Treat punctuation-only initialisms as their conventional compact alias:
    # D.C. United and DC United are the same identity, without adding
    # nickname-only matching for unrelated teams.
    text = re.sub(r"\b(?:[a-zA-Z]\.){2,}", lambda m: m.group(0).replace(".", ""), str(value or ""))
    return _WORDS.sub(" ", text.casefold()).strip()


def _key(prefix: str, *values: Any) -> str:
    material = "|".join(_norm(v) for v in values)
    return f"{prefix}_{hashlib.sha256(material.encode()).hexdigest()[:20]}"


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value or {}


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create idempotent canonical tables without altering legacy records."""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS sports (
      id TEXT PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE,
      normalized_name TEXT NOT NULL UNIQUE, created_utc TEXT NOT NULL, updated_utc TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS leagues (
      id TEXT PRIMARY KEY, sport_id TEXT REFERENCES sports(id), name TEXT NOT NULL,
      normalized_name TEXT NOT NULL, created_utc TEXT NOT NULL, updated_utc TEXT NOT NULL,
      UNIQUE(sport_id, normalized_name));
    CREATE TABLE IF NOT EXISTS teams (
      id TEXT PRIMARY KEY, sport_id TEXT REFERENCES sports(id), league_id TEXT REFERENCES leagues(id),
      name TEXT NOT NULL, normalized_name TEXT NOT NULL, aliases_json TEXT NOT NULL DEFAULT '[]',
      created_utc TEXT NOT NULL, updated_utc TEXT NOT NULL,
      UNIQUE(league_id, normalized_name));
    CREATE TABLE IF NOT EXISTS canonical_events (
      id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, sport_id TEXT REFERENCES sports(id),
      league_id TEXT REFERENCES leagues(id), season TEXT, competition TEXT, stage TEXT, round TEXT,
      event_type TEXT, start_utc TEXT NOT NULL, end_utc TEXT, venue TEXT, status TEXT NOT NULL DEFAULT 'discovered',
      title TEXT, metadata_json TEXT NOT NULL DEFAULT '{}', created_utc TEXT NOT NULL, updated_utc TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS canonical_event_participants (
      event_id TEXT NOT NULL REFERENCES canonical_events(id) ON DELETE CASCADE,
      team_id TEXT REFERENCES teams(id), display_name TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'participant',
      source_identifier TEXT, PRIMARY KEY(event_id, display_name, role));
    CREATE TABLE IF NOT EXISTS source_entity_mappings (
      source TEXT NOT NULL, entity_type TEXT NOT NULL, source_id TEXT NOT NULL,
      canonical_id TEXT NOT NULL, confidence REAL NOT NULL DEFAULT 1, manual INTEGER NOT NULL DEFAULT 0,
      evidence_json TEXT NOT NULL DEFAULT '{}', last_seen_utc TEXT NOT NULL,
      PRIMARY KEY(source, entity_type, source_id));
    CREATE TABLE IF NOT EXISTS source_event_records (
      source TEXT NOT NULL, source_event_id TEXT NOT NULL, canonical_event_id TEXT NOT NULL REFERENCES canonical_events(id),
      confidence REAL NOT NULL, resolution_kind TEXT NOT NULL, evidence_json TEXT NOT NULL DEFAULT '{}',
      raw_json TEXT NOT NULL DEFAULT '{}', last_seen_utc TEXT NOT NULL,
      PRIMARY KEY(source, source_event_id));
    CREATE TABLE IF NOT EXISTS sports_rules (
      id INTEGER PRIMARY KEY AUTOINCREMENT, target_type TEXT NOT NULL CHECK(target_type IN ('event','team','league','sport','competition')),
      target_id TEXT NOT NULL, event_type TEXT, policy TEXT NOT NULL CHECK(policy IN ('IGNORE','NORMAL','PRIORITIZE','ALWAYS_SCHEDULE')),
      broadcast_preferences_json TEXT NOT NULL DEFAULT '[]', enabled INTEGER NOT NULL DEFAULT 1,
      created_utc TEXT NOT NULL, updated_utc TEXT NOT NULL,
      UNIQUE(target_type, target_id, event_type));
    CREATE TABLE IF NOT EXISTS scheduling_decisions (
      canonical_event_id TEXT NOT NULL, generation_utc TEXT NOT NULL, decision TEXT NOT NULL,
      priority INTEGER NOT NULL DEFAULT 0, reason_json TEXT NOT NULL DEFAULT '{}', lane_id INTEGER,
      PRIMARY KEY(canonical_event_id, generation_utc));
    CREATE TABLE IF NOT EXISTS provider_capacities (
      provider TEXT PRIMARY KEY, max_concurrent INTEGER NOT NULL CHECK(max_concurrent > 0),
      updated_utc TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS xtream_catalog_categories (
      category_id TEXT PRIMARY KEY, name TEXT NOT NULL, normalized_name TEXT NOT NULL,
      enabled INTEGER NOT NULL DEFAULT 0, ignored INTEGER NOT NULL DEFAULT 0, stream_count INTEGER,
      samples_json TEXT NOT NULL DEFAULT '[]', first_seen_utc TEXT NOT NULL, last_seen_utc TEXT NOT NULL,
      disappeared_utc TEXT);
    CREATE INDEX IF NOT EXISTS idx_source_event_canonical ON source_event_records(canonical_event_id);
    CREATE INDEX IF NOT EXISTS idx_rules_target ON sports_rules(target_type, target_id, enabled);
    CREATE INDEX IF NOT EXISTS idx_canonical_events_time ON canonical_events(start_utc);
    """)
    conn.commit()


def _upsert_named(conn: sqlite3.Connection, table: str, name: str, *, sport_id: str | None = None,
                  league_id: str | None = None, aliases: Iterable[str] = ()) -> str | None:
    name = " ".join(str(name or "").split())
    if not name:
        return None
    normalized = _norm(name)
    if table == "sports":
        row = conn.execute("SELECT id FROM sports WHERE normalized_name=?", (normalized,)).fetchone()
        identifier = row[0] if row else _key("sport", normalized)
        conn.execute("INSERT INTO sports(id,name,normalized_name,created_utc,updated_utc) VALUES(?,?,?,?,?) "
                     "ON CONFLICT(normalized_name) DO UPDATE SET name=excluded.name,updated_utc=excluded.updated_utc",
                     (identifier, name, normalized, utc_now(), utc_now()))
    elif table == "leagues":
        row = conn.execute("SELECT id FROM leagues WHERE sport_id IS ? AND normalized_name=?", (sport_id, normalized)).fetchone()
        identifier = row[0] if row else _key("league", sport_id, normalized)
        conn.execute("INSERT INTO leagues(id,sport_id,name,normalized_name,created_utc,updated_utc) VALUES(?,?,?,?,?,?) "
                     "ON CONFLICT(sport_id,normalized_name) DO UPDATE SET name=excluded.name,updated_utc=excluded.updated_utc",
                     (identifier, sport_id, name, normalized, utc_now(), utc_now()))
    else:
        row = conn.execute("SELECT id FROM teams WHERE league_id IS ? AND normalized_name=?", (league_id, normalized)).fetchone()
        identifier = row[0] if row else _key("team", league_id, normalized)
        prior = conn.execute("SELECT aliases_json FROM teams WHERE id=?", (identifier,)).fetchone()
        merged = sorted({_norm(x): x for x in [*(_json(prior[0]) if prior else []), *aliases] if _norm(x)}.values(), key=str.casefold)
        conn.execute("INSERT INTO teams(id,sport_id,league_id,name,normalized_name,aliases_json,created_utc,updated_utc) VALUES(?,?,?,?,?,?,?,?) "
                     "ON CONFLICT(league_id,normalized_name) DO UPDATE SET name=excluded.name, aliases_json=excluded.aliases_json, updated_utc=excluded.updated_utc",
                     (identifier, sport_id, league_id, name, normalized, json.dumps(merged), utc_now(), utc_now()))
    return identifier


def _participants(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = data.get("participants") or data.get("competitors") or []
    result = []
    for value in raw:
        if isinstance(value, str):
            result.append({"name": value, "role": "participant"})
        elif isinstance(value, Mapping):
            name = value.get("name") or value.get("displayName") or value.get("teamName") or value.get("shortName")
            if name:
                role = str(value.get("homeAway") or value.get("role") or "participant").lower()
                if role in ("host",): role = "home"
                if role in ("visitor", "road"): role = "away"
                result.append({"name": str(name), "role": role if role in ("home", "away") else "participant",
                               "source_id": value.get("id") or value.get("teamId"), "aliases": value.get("abbreviations") or []})
    return result


def event_fingerprint(*, sport: str | None, league: str | None, participants: Iterable[Mapping[str, Any]],
                      event_type: str | None, start_utc: str) -> str:
    # Roles are material when authoritative; non-team events simply have an
    # ordered participant list or no participants at all.
    members = sorted(f"{_norm(p.get('role'))}:{_norm(p.get('name'))}" for p in participants)
    return _key("event", sport, league, event_type, start_utc, *members)


def _event_candidates(conn: sqlite3.Connection, *, sport_id: str | None, league_id: str | None,
                      start: datetime, participants: list[dict[str, Any]], event_type: str | None) -> tuple[str | None, float, dict]:
    """Cautious deterministic match: 0.85 auto; ambiguity is unresolved."""
    # SQLite compares ISO text; use UTC ISO bounds rather than epoch integers.
    lower = datetime.fromtimestamp(start.timestamp() - 600, UTC).isoformat().replace("+00:00", "Z")
    upper = datetime.fromtimestamp(start.timestamp() + 600, UTC).isoformat().replace("+00:00", "Z")
    rows = conn.execute("SELECT id,start_utc,event_type FROM canonical_events WHERE sport_id IS ? AND league_id IS ? "
                        "AND start_utc BETWEEN ? AND ?", (sport_id, league_id, lower, upper)).fetchall()
    wanted = {_norm(p["name"]) for p in participants}
    scored = []
    for row in rows:
        names = {_norm(r[0]) for r in conn.execute("SELECT display_name FROM canonical_event_participants WHERE event_id=?", (row[0],))}
        overlap = len(wanted & names)
        score = 0.45 + 0.25 * min(overlap, 2) + (0.1 if _norm(row[2]) == _norm(event_type) else 0)
        if wanted and wanted == names: score += 0.1
        scored.append((score, row[0], {"participant_overlap": overlap, "time_tolerance_minutes": 10}))
    scored.sort(reverse=True)
    if len(scored) == 1 and scored[0][0] >= .85:
        return scored[0][1], scored[0][0], scored[0][2]
    return None, 0.0, {"candidate_count": len(scored), "reason": "ambiguous_or_insufficient_evidence"}


def resolve_source_event(conn: sqlite3.Connection, *, source: str, source_event_id: str, data: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve one structured source record to Fruit-owned canonical identity."""
    ensure_schema(conn)
    source = _norm(source) or "unknown"
    prior = conn.execute("SELECT canonical_event_id FROM source_event_records WHERE source=? AND source_event_id=?", (source, str(source_event_id))).fetchone()
    raw = _json(data.get("raw_attributes_json"))
    sport = data.get("sport") or data.get("sport_name") or raw.get("sport_name")
    league = data.get("league") or data.get("league_name") or raw.get("league_name")
    participants = _participants(data) or _participants(raw)
    start_value = data.get("start_utc") or data.get("start_ms") or data.get("start_time") or raw.get("start_utc")
    start = utc_instant(start_value, source_timezone=data.get("source_timezone"))
    if not start:
        return {"resolved": False, "reason": "invalid_or_naive_start_timestamp"}
    start_text = utc_text(start)
    end_text = utc_text(data.get("end_utc") or data.get("end_ms") or data.get("end_time"), source_timezone=data.get("source_timezone"))
    sport_id = _upsert_named(conn, "sports", str(sport)) if sport else None
    league_id = _upsert_named(conn, "leagues", str(league), sport_id=sport_id) if league else None
    team_rows = []
    for participant in participants:
        team_id = _upsert_named(conn, "teams", participant["name"], sport_id=sport_id, league_id=league_id,
                                aliases=participant.get("aliases") or [])
        team_rows.append((participant, team_id))
        if participant.get("source_id") and team_id:
            conn.execute("INSERT INTO source_entity_mappings(source,entity_type,source_id,canonical_id,confidence,manual,evidence_json,last_seen_utc) VALUES(?,?,?,?,?,?,?,?) "
                         "ON CONFLICT(source,entity_type,source_id) DO UPDATE SET canonical_id=excluded.canonical_id,confidence=excluded.confidence,last_seen_utc=excluded.last_seen_utc",
                         (source, "team", str(participant["source_id"]), team_id, 1.0, 0, '{"kind":"structured_id"}', utc_now()))
    event_type = data.get("event_type") or raw.get("event_type") or raw.get("eventType") or "event"
    if prior:
        canonical_id, confidence, kind, evidence = prior[0], 1.0, "source_mapping", {"source_mapping": True}
    else:
        canonical_id, confidence, evidence = _event_candidates(conn, sport_id=sport_id, league_id=league_id,
                                                                  start=start, participants=participants, event_type=str(event_type))
        kind = "confidence_match" if canonical_id else "new_fingerprint"
        if not canonical_id:
            fingerprint = event_fingerprint(sport=sport, league=league, participants=participants, event_type=str(event_type), start_utc=start_text)
            known = conn.execute("SELECT id FROM canonical_events WHERE fingerprint=?", (fingerprint,)).fetchone()
            canonical_id = known[0] if known else _key("ce", fingerprint)
            confidence = 1.0 if known else .9
    metadata = {"title": data.get("title"), "source": source, "raw_start": start_value,
                "canonical_start_utc": start_text, "time_contract": "absolute_utc"}
    fingerprint = event_fingerprint(sport=sport, league=league, participants=participants, event_type=str(event_type), start_utc=start_text)
    conn.execute("INSERT INTO canonical_events(id,fingerprint,sport_id,league_id,season,competition,stage,round,event_type,start_utc,end_utc,venue,status,title,metadata_json,created_utc,updated_utc) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET end_utc=COALESCE(excluded.end_utc,canonical_events.end_utc), status=excluded.status,title=COALESCE(excluded.title,canonical_events.title),metadata_json=excluded.metadata_json,updated_utc=excluded.updated_utc",
                 (canonical_id, fingerprint, sport_id, league_id, data.get("season") or raw.get("season"), data.get("competition") or raw.get("competition"), data.get("stage") or raw.get("stage"), data.get("round") or raw.get("round"), str(event_type), start_text, end_text, data.get("venue") or raw.get("venue"), data.get("status") or raw.get("status") or "discovered", data.get("title"), json.dumps(metadata), utc_now(), utc_now()))
    conn.execute("DELETE FROM canonical_event_participants WHERE event_id=?", (canonical_id,))
    for participant, team_id in team_rows:
        conn.execute("INSERT INTO canonical_event_participants(event_id,team_id,display_name,role,source_identifier) VALUES(?,?,?,?,?)",
                     (canonical_id, team_id, participant["name"], participant["role"], str(participant.get("source_id") or "") or None))
    conn.execute("INSERT INTO source_event_records(source,source_event_id,canonical_event_id,confidence,resolution_kind,evidence_json,raw_json,last_seen_utc) VALUES(?,?,?,?,?,?,?,?) "
                 "ON CONFLICT(source,source_event_id) DO UPDATE SET canonical_event_id=excluded.canonical_event_id,confidence=excluded.confidence,resolution_kind=excluded.resolution_kind,evidence_json=excluded.evidence_json,raw_json=excluded.raw_json,last_seen_utc=excluded.last_seen_utc",
                 (source, str(source_event_id), canonical_id, confidence, kind, json.dumps(evidence), json.dumps(raw), utc_now()))
    conn.commit()
    return {"resolved": True, "canonical_event_id": canonical_id, "confidence": confidence, "resolution_kind": kind, "start_utc": start_text}


def sync_legacy_events(conn: sqlite3.Connection) -> dict[str, int]:
    """Backfill canonical records from existing imported events, idempotently."""
    ensure_schema(conn)
    columns = {r[1] for r in conn.execute("PRAGMA table_info(events)")}
    if not {"id", "start_utc"}.issubset(columns): return {"resolved": 0, "skipped": 0}
    rows = conn.execute("SELECT * FROM events WHERE start_utc IS NOT NULL").fetchall()
    resolved = skipped = 0
    for row in rows:
        data = dict(row)
        raw = _json(data.get("raw_attributes_json"))
        data.update({"sport_name": raw.get("sport_name"), "league_name": raw.get("league_name"), "competitors": raw.get("competitors", [])})
        result = resolve_source_event(conn, source="apple" if str(data.get("id", "")).startswith("appletv-") else str(data.get("channel_provider_id") or "legacy"), source_event_id=str(data["id"]), data=data)
        resolved += int(bool(result.get("resolved"))); skipped += int(not result.get("resolved"))
    return {"resolved": resolved, "skipped": skipped}


def save_rule(conn: sqlite3.Connection, *, target_type: str, target_id: str, policy: str,
              event_type: str | None = None, broadcasts: Iterable[str] = ()) -> int:
    ensure_schema(conn)
    if target_type not in {"event", "team", "league", "sport", "competition"}: raise ValueError("invalid target type")
    policy = policy.upper()
    if policy not in POLICIES: raise ValueError("invalid policy")
    now = utc_now()
    conn.execute("INSERT INTO sports_rules(target_type,target_id,event_type,policy,broadcast_preferences_json,enabled,created_utc,updated_utc) VALUES(?,?,?,?,?,?,?,?) "
                 "ON CONFLICT(target_type,target_id,event_type) DO UPDATE SET policy=excluded.policy,broadcast_preferences_json=excluded.broadcast_preferences_json,enabled=1,updated_utc=excluded.updated_utc",
                 (target_type, target_id, event_type or "", policy, json.dumps(list(broadcasts)), 1, now, now))
    conn.commit()
    row = conn.execute("SELECT id FROM sports_rules WHERE target_type=? AND target_id=? AND event_type=?", (target_type, target_id, event_type or "")).fetchone()
    return int(row[0])


def applicable_rule(conn: sqlite3.Connection, canonical_event_id: str) -> dict[str, Any]:
    """Return the most-specific rule; event-type-specific beats generic."""
    event = conn.execute("SELECT * FROM canonical_events WHERE id=?", (canonical_event_id,)).fetchone()
    if not event: return {"policy": "NORMAL", "priority": 0, "reason": "no_canonical_event"}
    event = dict(event)
    team_ids = [r[0] for r in conn.execute("SELECT team_id FROM canonical_event_participants WHERE event_id=? AND team_id IS NOT NULL", (canonical_event_id,))]
    candidates = [("event", canonical_event_id, 5), *[("team", t, 4) for t in team_ids],
                  ("competition", event.get("competition") or "", 3), ("league", event.get("league_id") or "", 2), ("sport", event.get("sport_id") or "", 1)]
    for target_type, target_id, specificity in candidates:
        if not target_id: continue
        row = conn.execute("SELECT * FROM sports_rules WHERE enabled=1 AND target_type=? AND target_id=? AND (event_type='' OR event_type=?) ORDER BY CASE WHEN event_type='' THEN 0 ELSE 1 END DESC, id DESC LIMIT 1", (target_type, target_id, event.get("event_type") or "")).fetchone()
        if row:
            item = dict(row); item.update({"priority": {"IGNORE": -10000, "NORMAL": 0, "PRIORITIZE": 1000, "ALWAYS_SCHEDULE": 10000}[item["policy"]], "specificity": specificity})
            return item
    return {"policy": "NORMAL", "priority": 0, "reason": "default"}


def coverage(conn: sqlite3.Connection, *, days: int = 14) -> list[dict[str, Any]]:
    """Starts with wanted canonical events, then reports source/lane coverage."""
    ensure_schema(conn)
    rows = conn.execute("SELECT ce.* FROM canonical_events ce WHERE datetime(ce.start_utc) BETWEEN datetime('now','-1 day') AND datetime('now', ?)", (f"+{max(1, min(days, 90))} days",)).fetchall()
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    result = []
    for row in rows:
        event = dict(row); rule = applicable_rule(conn, event["id"])
        if rule["policy"] not in {"ALWAYS_SCHEDULE", "PRIORITIZE"}: continue
        source_rows = conn.execute("SELECT source,source_event_id,confidence FROM source_event_records WHERE canonical_event_id=?", (event["id"],)).fetchall()
        legacy_ids = [r[1] for r in source_rows]
        playable_count = 0; lane = None
        if legacy_ids and {"playables", "lane_events"}.issubset(tables):
            marks = ",".join("?" for _ in legacy_ids)
            playable_count = conn.execute(f"SELECT COUNT(*) FROM playables WHERE event_id IN ({marks})", legacy_ids).fetchone()[0]
            lane = conn.execute(f"SELECT lane_id FROM lane_events WHERE event_id IN ({marks}) AND COALESCE(is_placeholder,0)=0 ORDER BY start_utc LIMIT 1", legacy_ids).fetchone()
        decision = conn.execute("SELECT decision,reason_json FROM scheduling_decisions WHERE canonical_event_id=? ORDER BY generation_utc DESC LIMIT 1", (event["id"],)).fetchone()
        status = "scheduled" if lane else ("playable_found" if playable_count else "awaiting_source")
        if decision and decision[0] in {"provider_concurrency", "lane_capacity"}:
            status = "resource_conflict"
        participants = [dict(r) for r in conn.execute("SELECT display_name,role FROM canonical_event_participants WHERE event_id=?", (event["id"],))]
        result.append({"canonical_event_id": event["id"], "title": event.get("title"), "start_utc": event["start_utc"], "participants": participants, "rule": rule["policy"], "coverage_state": status, "playable_count": playable_count, "lane_id": lane[0] if lane else None, "decision": decision[0] if decision else None})
    return result
