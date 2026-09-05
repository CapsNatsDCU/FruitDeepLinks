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
import logging
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional


UTC = timezone.utc
LOG = logging.getLogger(__name__)
POLICIES = frozenset({"IGNORE", "NORMAL", "PRIORITIZE", "ALWAYS_SCHEDULE"})
_WORDS = re.compile(r"[^a-z0-9]+")


def normalize_provider(value: Any) -> str:
    """Return the stable provider identity used by capacities and diagnostics.

    Provider strings come from several legacy importers.  A capacity must not
    be bypassed merely because one importer spells ``X-Tream`` differently
    from another; equally, an unknown provider remains unconstrained.
    """
    raw = _norm(value).replace(" ", "")
    aliases = {"xtreamcodes": "xtream", "xtreamcode": "xtream", "xtreamiptv": "xtream"}
    return aliases.get(raw, raw)


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


def _source_input_fingerprint(data: Mapping[str, Any], raw: Mapping[str, Any]) -> str:
    """Fingerprint resolution-relevant source data to avoid stale mappings.

    A manual mapping remains authoritative, but an automatically inferred
    source ID must be reconsidered when its title/timing/structured identity
    changes (common for re-used IPTV stream identifiers).
    """
    material = {
        "title": data.get("title"), "sport": data.get("sport") or data.get("sport_name"),
        "league": data.get("league") or data.get("league_name"),
        "participants": data.get("participants") or data.get("competitors"),
        "start": data.get("start_utc") or data.get("start_ms") or data.get("start_time"),
        "end": data.get("end_utc") or data.get("end_ms") or data.get("end_time"),
        "raw": raw,
    }
    encoded = json.dumps(material, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


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
      provider TEXT PRIMARY KEY COLLATE NOCASE, max_concurrent INTEGER NOT NULL CHECK(max_concurrent > 0),
      updated_utc TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS sports_metadata_state (
      key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_utc TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS xtream_catalog_categories (
      category_id TEXT PRIMARY KEY, name TEXT NOT NULL, normalized_name TEXT NOT NULL,
      enabled INTEGER NOT NULL DEFAULT 0, ignored INTEGER NOT NULL DEFAULT 0, stream_count INTEGER,
      samples_json TEXT NOT NULL DEFAULT '[]', first_seen_utc TEXT NOT NULL, last_seen_utc TEXT NOT NULL,
      disappeared_utc TEXT);
    CREATE INDEX IF NOT EXISTS idx_source_event_canonical ON source_event_records(canonical_event_id);
    CREATE INDEX IF NOT EXISTS idx_rules_target ON sports_rules(target_type, target_id, enabled);
    CREATE INDEX IF NOT EXISTS idx_canonical_events_time ON canonical_events(start_utc);
    """)
    # Local AI is an optional parser/cache only.  Keeping its table here makes
    # the migration idempotent without coupling callers to an AI runtime.
    from local_ai_event_parser import ensure_schema as ensure_local_ai_schema
    ensure_local_ai_schema(conn)
    # Existing databases predate the normalized, NOCASE declaration above.
    # Coalesce aliases transactionally so ``Xtream`` and ``xtream-codes`` do
    # not become independent capacity buckets.  The smaller limit is the only
    # safe deterministic choice when old duplicate rows disagree.
    capacity_rows = conn.execute("SELECT provider,max_concurrent,updated_utc FROM provider_capacities").fetchall()
    normalized_capacities: dict[str, tuple[int, str]] = {}
    for provider, maximum, updated in capacity_rows:
        key = normalize_provider(provider)
        if not key:
            continue
        prior = normalized_capacities.get(key)
        normalized_capacities[key] = (min(int(maximum), prior[0]) if prior else int(maximum),
                                      max(str(updated or ""), prior[1]) if prior else str(updated or utc_now()))
    if normalized_capacities and any(str(provider) != normalize_provider(provider) for provider, _, _ in capacity_rows):
        # Keep the destructive rewrite atomic.  A malformed legacy row or a
        # process interruption must not leave the production database with no
        # capacity configuration at all.
        conn.execute("SAVEPOINT normalize_provider_capacities")
        try:
            conn.execute("DELETE FROM provider_capacities")
            conn.executemany("INSERT INTO provider_capacities(provider,max_concurrent,updated_utc) VALUES(?,?,?)",
                             [(provider, maximum, updated) for provider, (maximum, updated) in normalized_capacities.items()])
        except Exception:
            conn.execute("ROLLBACK TO normalize_provider_capacities")
            conn.execute("RELEASE normalize_provider_capacities")
            raise
        conn.execute("RELEASE normalize_provider_capacities")
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
        if row:
            conn.execute("UPDATE leagues SET name=?,updated_utc=? WHERE id=?", (name, utc_now(), identifier))
        else:
            conn.execute("INSERT INTO leagues(id,sport_id,name,normalized_name,created_utc,updated_utc) VALUES(?,?,?,?,?,?)",
                         (identifier, sport_id, name, normalized, utc_now(), utc_now()))
    else:
        row = conn.execute("SELECT id FROM teams WHERE league_id IS ? AND normalized_name=?", (league_id, normalized)).fetchone()
        identifier = row[0] if row else _key("team", league_id, normalized)
        prior = conn.execute("SELECT aliases_json FROM teams WHERE id=?", (identifier,)).fetchone()
        merged = sorted({_norm(x): x for x in [*(_json(prior[0]) if prior else []), *aliases] if _norm(x)}.values(), key=str.casefold)
        if row:
            conn.execute("UPDATE teams SET name=?,aliases_json=?,updated_utc=? WHERE id=?",
                         (name, json.dumps(merged), utc_now(), identifier))
        else:
            conn.execute("INSERT INTO teams(id,sport_id,league_id,name,normalized_name,aliases_json,created_utc,updated_utc) VALUES(?,?,?,?,?,?,?,?)",
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


def _title_participants(title: Any) -> list[dict[str, Any]]:
    """Conservative fallback for provider names that omit competitor objects."""
    text = str(title or "").strip()
    # A provider prefix/date may precede the actual fixture; use the final
    # pipe segment to avoid treating a category label as a team.
    text = text.rsplit("|", 1)[-1].strip()
    match = re.search(r"(?P<away>.+?)\s+(?:at|vs\.?|v\.?|x)\s+(?P<home>.+)$", text, re.IGNORECASE)
    if not match:
        return []
    away, home = (" ".join(match.group(key).split(" - ")[-1].split()) for key in ("away", "home"))
    if not away or not home or _norm(away) == _norm(home):
        return []
    return [{"name": away, "role": "away"}, {"name": home, "role": "home"}]


def event_fingerprint(*, sport: str | None, league: str | None, participants: Iterable[Mapping[str, Any]],
                      event_type: str | None, start_utc: str) -> str:
    # Roles are material when authoritative; non-team events simply have an
    # ordered participant list or no participants at all.
    members = sorted(f"{_norm(p.get('role'))}:{_norm(p.get('name'))}" for p in participants)
    return _key("event", sport, league, event_type, start_utc, *members)


def _event_candidates(conn: sqlite3.Connection, *, sport_id: str | None, league_id: str | None,
                      start: datetime, participants: list[dict[str, Any]], event_type: str | None) -> tuple[str | None, float, dict]:
    """Match structured event identity without merging concurrent fixtures.

    League/time are a candidate *scope*, never enough evidence to merge.  An
    exact participant set (and, where supplied, home/away roles) wins even
    during an NFL Sunday slate.  Incomplete or mascot-only input stays
    unresolved rather than risking a false merge.
    """
    # SQLite compares ISO text; use UTC ISO bounds rather than epoch integers.
    lower = datetime.fromtimestamp(start.timestamp() - 600, UTC).isoformat().replace("+00:00", "Z")
    upper = datetime.fromtimestamp(start.timestamp() + 600, UTC).isoformat().replace("+00:00", "Z")
    rows = conn.execute("SELECT id,start_utc,event_type FROM canonical_events WHERE sport_id IS ? AND league_id IS ? "
                        "AND start_utc BETWEEN ? AND ?", (sport_id, league_id, lower, upper)).fetchall()
    wanted = {_norm(p["name"]) for p in participants if _norm(p.get("name"))}
    wanted_roles = {(_norm(p["name"]), _norm(p.get("role"))) for p in participants if _norm(p.get("name")) and p.get("role") in {"home", "away"}}
    scored = []
    for row in rows:
        participant_rows = conn.execute("SELECT display_name,role FROM canonical_event_participants WHERE event_id=?", (row[0],)).fetchall()
        names = {_norm(r[0]) for r in participant_rows}
        roles = {(_norm(r[0]), _norm(r[1])) for r in participant_rows if r[1] in {"home", "away"}}
        overlap = len(wanted & names)
        exact_set = bool(wanted) and wanted == names
        role_match = bool(wanted_roles) and wanted_roles == roles
        # A full two-team identity is strong enough to distinguish concurrent
        # league fixtures.  One shared token is deliberately not.
        score = 0.35 + 0.25 * min(overlap, 2)
        if exact_set:
            score += 0.35
        if role_match:
            score += 0.05
        if _norm(row[2]) == _norm(event_type):
            score += 0.03
        score = min(score, 1.0)
        scored.append((score, row[0], {"participant_overlap": overlap, "exact_participant_set": exact_set,
                                       "home_away_match": role_match, "time_tolerance_minutes": 10}))
    scored.sort(key=lambda item: (-item[0], item[1]))
    if scored and scored[0][0] >= .9 and (len(scored) == 1 or scored[0][0] > scored[1][0]):
        return scored[0][1], scored[0][0], scored[0][2]
    return None, 0.0, {"candidate_count": len(scored), "reason": "ambiguous_or_insufficient_evidence"}


def resolve_source_event(conn: sqlite3.Connection, *, source: str, source_event_id: str,
                         data: Mapping[str, Any], commit: bool = True,
                         schema_ready: bool = False, ai_config=None,
                         ai_requester=None, ai_budget: list[int] | None = None,
                         recheck_inferred_mapping: bool = True) -> dict[str, Any]:
    """Resolve one structured source record to Fruit-owned canonical identity."""
    if not schema_ready:
        ensure_schema(conn)
    source = _norm(source) or "unknown"
    prior = conn.execute("SELECT canonical_event_id,resolution_kind,evidence_json FROM source_event_records WHERE source=? AND source_event_id=?", (source, str(source_event_id))).fetchone()
    raw = _json(data.get("raw_attributes_json"))
    source_fingerprint = _source_input_fingerprint(data, raw)
    prior_evidence = _json(prior[2]) if prior else {}
    prior_is_current = bool(prior and (not recheck_inferred_mapping or prior[1] == "manual_override" or not prior_evidence.get("source_input_fingerprint")
                                       or prior_evidence.get("source_input_fingerprint") == source_fingerprint))
    sport = data.get("sport") or data.get("sport_name") or raw.get("sport_name")
    league = data.get("league") or data.get("league_name") or raw.get("league_name")
    structured_participants = _participants(data) or _participants(raw)
    # A title match is useful deterministic input, but is not authoritative
    # provider metadata.  A valid local-AI interpretation may clarify a
    # provider-prefixed title before the normal resolver sees it.
    participants = structured_participants or _title_participants(data.get("title") or raw.get("title") or raw.get("original_stream_name"))
    start_value = data.get("start_utc") or data.get("start_ms") or data.get("start_time") or raw.get("start_utc")
    start = utc_instant(start_value, source_timezone=data.get("source_timezone"))
    if not start:
        return {"resolved": False, "reason": "invalid_or_naive_start_timestamp"}
    start_text = utc_text(start)
    end_text = utc_text(data.get("end_utc") or data.get("end_ms") or data.get("end_time"), source_timezone=data.get("source_timezone"))
    event_type = data.get("event_type") or raw.get("event_type") or raw.get("eventType") or "event"
    competition = data.get("competition") or raw.get("competition")
    ai_result = {"status": "not_needed", "interpretation": None}
    # Never let an AI reinterpret complete structured metadata or an explicit
    # manual mapping.  It is only an optional parser for incomplete/weak text.
    strong_structured_identity = bool(sport and league and len(structured_participants) >= 2)
    title_text = data.get("title") or raw.get("title") or raw.get("original_stream_name")
    sports_words = ("nfl", "nhl", "mlb", "mls", "nba", "ncaa", "football", "hockey", "baseball", "soccer", "basketball", "formula 1", "grand prix")
    potential_sports_record = source == "xtream" or bool(sport or league or participants) or any(word in _norm(title_text) for word in sports_words)
    if potential_sports_record and not strong_structured_identity and not (prior and prior[1] == "manual_override"):
        try:
            from local_ai_event_parser import enrich, request_openai_compatible
            kwargs = {
                # Use Fruit's normalized source label, never a provider-supplied
                # transport/configuration field, as the parser/cache provider.
                "provider": source,
                "source_event_id": str(source_event_id),
                "title": title_text,
                "category": raw.get("category_name") or raw.get("category"),
                "sport_hint": sport,
                "league_hint": league,
                "start_time": start_text,
                "config": ai_config,
                "budget": ai_budget,
            }
            if ai_requester is not None:
                kwargs["requester"] = ai_requester
            ai_result = enrich(conn, **kwargs)
            interpretation = ai_result.get("interpretation")
            if interpretation:
                # Explicit structured fields always win.  Title-derived names
                # are weak parsing and may be replaced by a validated parser
                # candidate, which still must pass _event_candidates below.
                sport = sport or interpretation.get("sport")
                league = league or interpretation.get("league")
                event_type = data.get("event_type") or raw.get("event_type") or raw.get("eventType") or interpretation.get("event_type") or event_type
                competition = competition or interpretation.get("competition")
                if not structured_participants and interpretation.get("participants"):
                    participants = interpretation["participants"]
        except Exception as exc:
            # This boundary is deliberately fail-open: the optional parser may
            # not delay or remove a legacy event.  Do not log raw provider text.
            LOG.warning("Optional local AI event parser failed (%s); using deterministic metadata", type(exc).__name__)
            ai_result = {"status": "parser_error", "interpretation": None, "error": type(exc).__name__}
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
    if prior_is_current:
        canonical_id, confidence = prior[0], 1.0
        if prior[1] == "manual_override":
            kind, evidence = "manual_override", {"operator_confirmed": True}
        else:
            kind, evidence = "source_mapping", {"source_mapping": True}
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
    if ai_result.get("status") not in {"not_needed", "disabled"}:
        metadata["local_ai"] = {"status": ai_result.get("status"), "model": getattr(ai_config, "model", None),
                                "used": bool(ai_result.get("interpretation"))}
    fingerprint = event_fingerprint(sport=sport, league=league, participants=participants, event_type=str(event_type), start_utc=start_text)
    conn.execute("INSERT INTO canonical_events(id,fingerprint,sport_id,league_id,season,competition,stage,round,event_type,start_utc,end_utc,venue,status,title,metadata_json,created_utc,updated_utc) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET end_utc=COALESCE(excluded.end_utc,canonical_events.end_utc), status=excluded.status,title=COALESCE(excluded.title,canonical_events.title),metadata_json=excluded.metadata_json,updated_utc=excluded.updated_utc",
                 (canonical_id, fingerprint, sport_id, league_id, data.get("season") or raw.get("season"), competition, data.get("stage") or raw.get("stage"), data.get("round") or raw.get("round"), str(event_type), start_text, end_text, data.get("venue") or raw.get("venue"), data.get("status") or raw.get("status") or "discovered", data.get("title"), json.dumps(metadata), utc_now(), utc_now()))
    conn.execute("DELETE FROM canonical_event_participants WHERE event_id=?", (canonical_id,))
    for participant, team_id in team_rows:
        conn.execute("INSERT INTO canonical_event_participants(event_id,team_id,display_name,role,source_identifier) VALUES(?,?,?,?,?)",
                     (canonical_id, team_id, participant["name"], participant["role"], str(participant.get("source_id") or "") or None))
    evidence = {**evidence, "source_input_fingerprint": source_fingerprint}
    conn.execute("INSERT INTO source_event_records(source,source_event_id,canonical_event_id,confidence,resolution_kind,evidence_json,raw_json,last_seen_utc) VALUES(?,?,?,?,?,?,?,?) "
                 "ON CONFLICT(source,source_event_id) DO UPDATE SET canonical_event_id=excluded.canonical_event_id,confidence=excluded.confidence,resolution_kind=excluded.resolution_kind,evidence_json=excluded.evidence_json,raw_json=excluded.raw_json,last_seen_utc=excluded.last_seen_utc",
                 (source, str(source_event_id), canonical_id, confidence, kind, json.dumps(evidence), json.dumps(raw), utc_now()))
    if commit:
        conn.commit()
    return {"resolved": True, "canonical_event_id": canonical_id, "confidence": confidence, "resolution_kind": kind, "start_utc": start_text,
            "local_ai": {"status": ai_result.get("status"), "used": bool(ai_result.get("interpretation"))}}


def _legacy_sync_fingerprint(conn: sqlite3.Connection) -> str:
    """Cheap change detector; avoids a full canonical backfill on every API hit."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
    last_seen = ", COALESCE(MAX(last_seen_utc), '')" if "last_seen_utc" in columns else ""
    row = conn.execute(f"SELECT COUNT(*), COALESCE(MAX(start_utc), ''), COALESCE(MAX(end_utc), ''){last_seen} FROM events WHERE start_utc IS NOT NULL").fetchone()
    return "|".join(map(str, row))


def sync_legacy_events(conn: sqlite3.Connection) -> dict[str, int]:
    """Incrementally backfill canonical records from existing imported events."""
    ensure_schema(conn)
    columns = {r[1] for r in conn.execute("PRAGMA table_info(events)")}
    if not {"id", "start_utc"}.issubset(columns): return {"resolved": 0, "skipped": 0}
    try:
        from local_ai_event_parser import PARSER_VERSION, load_config
        ai_config = load_config(conn)
        ai_state = f"{PARSER_VERSION}:{ai_config.usable}:{ai_config.model}:{ai_config.minimum_confidence}"
    except Exception:
        ai_config = None
        ai_state = "local-ai-unavailable"
    fingerprint = f"{_legacy_sync_fingerprint(conn)}|{ai_state}"
    state = conn.execute("SELECT value FROM sports_metadata_state WHERE key='legacy_events_fingerprint'").fetchone()
    if state and state[0] == fingerprint:
        return {"resolved": 0, "skipped": 0, "unchanged": 1}
    rows = conn.execute("SELECT * FROM events WHERE start_utc IS NOT NULL").fetchall()
    # One bounded budget covers this incremental sync.  Cache hits are free,
    # while a large provider catalog cannot cause an unbounded model walk.
    ai_budget = [ai_config.max_requests_per_refresh] if ai_config else [0]
    resolved = skipped = pending_ai = 0
    for row in rows:
        data = dict(row)
        raw = _json(data.get("raw_attributes_json"))
        classifications = _json(data.get("classification_json"))
        classified = {str(item.get("type")): item.get("value") for item in classifications if isinstance(item, Mapping)} if isinstance(classifications, list) else {}
        sport = raw.get("sport_name") or raw.get("sport") or classified.get("sport")
        league = raw.get("league_name") or raw.get("league") or raw.get("series") or classified.get("league")
        competitors = raw.get("competitors") or raw.get("participants") or _title_participants(data.get("title") or raw.get("original_stream_name"))
        data.update({"sport_name": sport, "league_name": league, "competitors": competitors})
        source = ("apple" if str(data.get("id", "")).startswith("appletv-")
                  else "xtream" if raw.get("provider") == "xtream" or str(data.get("id", "")).startswith("xtream:")
                  else str(data.get("channel_provider_id") or "legacy"))
        result = resolve_source_event(conn, source=source, source_event_id=str(data["id"]), data=data, commit=False, schema_ready=True,
                                      ai_budget=ai_budget, recheck_inferred_mapping=False)
        resolved += int(bool(result.get("resolved"))); skipped += int(not result.get("resolved"))
        pending_ai += int(bool(ai_config and ai_config.max_requests_per_refresh > 0)
                          and result.get("local_ai", {}).get("status") == "budget_exhausted")
    # A capped run is intentionally not marked complete.  On the next refresh
    # cache hits cost nothing and the next bounded slice can be interpreted.
    if not pending_ai:
        conn.execute("INSERT INTO sports_metadata_state(key,value,updated_utc) VALUES('legacy_events_fingerprint',?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_utc=excluded.updated_utc", (fingerprint, utc_now()))
    conn.commit()
    return {"resolved": resolved, "skipped": skipped, "pending_local_ai": pending_ai, "unchanged": 0}


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
                  ("league", event.get("league_id") or "", 3), ("competition", event.get("competition") or "", 2),
                  ("sport", event.get("sport_id") or "", 1)]
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
        if decision and decision[0] == "provider_capacity_conflict":
            status = "provider_capacity_conflict"
        elif decision and decision[0] == "lane_capacity_conflict":
            status = "lane_capacity_conflict"
        participants = [dict(r) for r in conn.execute("SELECT display_name,role FROM canonical_event_participants WHERE event_id=?", (event["id"],))]
        result.append({"canonical_event_id": event["id"], "title": event.get("title"), "start_utc": event["start_utc"], "participants": participants, "rule": rule["policy"], "coverage_state": status, "playable_count": playable_count, "lane_id": lane[0] if lane else None, "decision": decision[0] if decision else None})
    return result
