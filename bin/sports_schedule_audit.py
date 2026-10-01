#!/usr/bin/env python3
"""External schedule coverage audit for selected major sports.

External schedules are reference data only.  They are stored separately from
``canonical_events`` and can never create a lane, select a playable, or grant
scheduling eligibility.  The audit answers a narrower operator question:
which expected events are missing from Fruit's observed provider pipeline?
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping

import requests


SERPAPI_URL = "https://serpapi.com/search.json"
ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard"
PREFERENCE_KEY = "sports_schedule_audit_leagues"
DEFAULT_DUE_HOURS = 72
DEFAULT_DAYS = 14
DEFAULT_MONTHLY_BUDGET = 250
DEFAULT_BUDGET_RESERVE = 25


@dataclass(frozen=True)
class LeagueSpec:
    key: str
    name: str
    sport: str
    source: str
    sport_code: str | None = None
    kgmid: str | None = None
    espn_sport: str | None = None
    espn_league: str | None = None
    aliases: tuple[str, ...] = ()


# This is an operator-editable priority registry, not scheduling authority.
# KGMIDs are stable Google/Freebase entity identifiers exposed by Wikidata.
LEAGUES: tuple[LeagueSpec, ...] = (
    LeagueSpec("nfl", "NFL", "American football", "serpapi", "af", "/m/059yj",
               aliases=("National Football League",)),
    LeagueSpec("nhl", "NHL", "Ice hockey", "serpapi", "ih", "/m/05gwr",
               aliases=("National Hockey League",)),
    LeagueSpec("mlb", "MLB", "Baseball", "serpapi", "bb", "/m/09p14",
               aliases=("Major League Baseball",)),
    LeagueSpec("nba", "NBA", "Basketball", "serpapi", "bs", "/m/05jvx",
               aliases=("National Basketball Association",)),
    LeagueSpec("mls", "MLS", "Soccer", "serpapi", "ft", "/m/0jfpf",
               aliases=("Major League Soccer",)),
    LeagueSpec("uefa-champions-league", "UEFA Champions League", "Soccer", "serpapi", "ft", "/m/0c1q0",
               aliases=("Champions League", "UCL")),
    LeagueSpec("english-premier-league", "English Premier League", "Soccer", "serpapi", "ft", "/m/02_tc",
               aliases=("Premier League", "EPL")),
    LeagueSpec("formula-1", "Formula 1", "Motorsport", "espn", espn_sport="racing", espn_league="f1",
               aliases=("F1",)),
    LeagueSpec("nascar-cup", "NASCAR Cup Series", "Motorsport", "espn", espn_sport="racing", espn_league="nascar-premier",
               aliases=("NASCAR", "NASCAR Cup")),
    LeagueSpec("indycar", "IndyCar Series", "Motorsport", "espn", espn_sport="racing", espn_league="irl",
               aliases=("IndyCar", "IRL")),
    LeagueSpec("ncaa-fbs", "NCAA FBS", "American football", "serpapi", "af", "/m/012hfxch",
               aliases=("NCAA Division I Football Bowl Subdivision", "College Football", "NCAAF")),
    LeagueSpec("ufl", "UFL", "American football", "espn", espn_sport="football", espn_league="ufl",
               aliases=("United Football League",)),
)
LEAGUE_BY_KEY = {league.key: league for league in LEAGUES}
DEFAULT_ENABLED_KEYS = tuple(league.key for league in LEAGUES)


def utc_now(now: datetime | None = None) -> str:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _normalize(value: Any) -> str:
    return " ".join("".join(ch.casefold() if ch.isalnum() else " " for ch in str(value or "")).split())


def _safe_error(prefix: str, error: BaseException | None = None) -> str:
    # Transport exceptions may contain URLs with the private API key.  Store
    # only their class, never the exception string or request URL.
    return f"{prefix}:{type(error).__name__}" if error else prefix


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS sports_schedule_reference_events (
      source TEXT NOT NULL,
      league_key TEXT NOT NULL,
      source_event_id TEXT NOT NULL,
      title TEXT NOT NULL,
      start_utc TEXT NOT NULL,
      end_utc TEXT,
      participants_json TEXT NOT NULL DEFAULT '[]',
      event_status TEXT,
      source_metadata_json TEXT NOT NULL DEFAULT '{}',
      active INTEGER NOT NULL DEFAULT 1,
      first_seen_utc TEXT NOT NULL,
      last_seen_utc TEXT NOT NULL,
      PRIMARY KEY(source, league_key, source_event_id)
    );
    CREATE INDEX IF NOT EXISTS idx_schedule_reference_window
      ON sports_schedule_reference_events(active, start_utc, league_key);
    CREATE TABLE IF NOT EXISTS sports_schedule_league_state (
      league_key TEXT PRIMARY KEY,
      source TEXT NOT NULL,
      status TEXT NOT NULL,
      event_count INTEGER NOT NULL DEFAULT 0,
      last_attempt_utc TEXT,
      last_success_utc TEXT,
      error TEXT
    );
    CREATE TABLE IF NOT EXISTS sports_schedule_audit_runs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      started_utc TEXT NOT NULL,
      finished_utc TEXT,
      status TEXT NOT NULL,
      requested_leagues INTEGER NOT NULL DEFAULT 0,
      checked_leagues INTEGER NOT NULL DEFAULT 0,
      successful_searches INTEGER NOT NULL DEFAULT 0,
      event_count INTEGER NOT NULL DEFAULT 0,
      detail_json TEXT NOT NULL DEFAULT '{}'
    );
    CREATE TABLE IF NOT EXISTS sports_schedule_api_usage (
      period TEXT PRIMARY KEY,
      successful_searches INTEGER NOT NULL DEFAULT 0,
      updated_utc TEXT NOT NULL
    );
    """)
    conn.commit()


def enabled_keys(conn: sqlite3.Connection) -> list[str]:
    try:
        row = conn.execute(
            "SELECT value FROM user_preferences WHERE key IN (?,?) "
            "ORDER BY CASE key WHEN ? THEN 0 ELSE 1 END LIMIT 1",
            (f"setting:{PREFERENCE_KEY}", PREFERENCE_KEY, f"setting:{PREFERENCE_KEY}"),
        ).fetchone()
    except sqlite3.DatabaseError:
        return list(DEFAULT_ENABLED_KEYS)
    if not row:
        return list(DEFAULT_ENABLED_KEYS)
    try:
        values = json.loads(row[0])
    except (TypeError, ValueError):
        return list(DEFAULT_ENABLED_KEYS)
    if not isinstance(values, list):
        return list(DEFAULT_ENABLED_KEYS)
    return [key for key in DEFAULT_ENABLED_KEYS if key in {str(value) for value in values}]


def save_enabled_keys(conn: sqlite3.Connection, keys: Iterable[str]) -> list[str]:
    requested = {str(key) for key in keys}
    unknown = sorted(requested - set(LEAGUE_BY_KEY))
    if unknown:
        raise ValueError(f"unsupported league keys: {', '.join(unknown)}")
    selected = [key for key in DEFAULT_ENABLED_KEYS if key in requested]
    conn.execute("""
      CREATE TABLE IF NOT EXISTS user_preferences (
        key TEXT PRIMARY KEY, value TEXT, updated_utc TEXT
      )
    """)
    conn.execute(
        "INSERT INTO user_preferences(key,value,updated_utc) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_utc=excluded.updated_utc",
        (f"setting:{PREFERENCE_KEY}", json.dumps(selected), utc_now()),
    )
    conn.commit()
    return selected


def registry(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    selected = set(enabled_keys(conn))
    states: dict[str, Mapping[str, Any]] = {}
    try:
        states = {str(row["league_key"]): dict(row) for row in conn.execute(
            "SELECT league_key,source,status,event_count,last_attempt_utc,last_success_utc,error "
            "FROM sports_schedule_league_state"
        )}
    except sqlite3.DatabaseError:
        pass
    result = []
    for spec in LEAGUES:
        item = asdict(spec)
        item.pop("kgmid", None)
        item["aliases"] = list(spec.aliases)
        item["enabled"] = spec.key in selected
        item["state"] = dict(states.get(spec.key) or {"status": "never_checked", "event_count": 0})
        result.append(item)
    return result


def _stable_event_id(league_key: str, event: Mapping[str, Any]) -> str:
    explicit = event.get("kgmid") or event.get("id") or event.get("uid")
    if explicit:
        return str(explicit)
    material = "|".join((league_key, str(event.get("start_utc") or ""), str(event.get("title") or "")))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def parse_serpapi_events(spec: LeagueSpec, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    groups = ((payload.get("league_results") or {}).get("game_groups") or [])
    for group in groups:
        for game in group.get("games") or []:
            start = _parse_utc(game.get("start_time"))
            if not start:
                continue
            teams = [str(team.get("name") or team.get("short_name") or "").strip()
                     for team in game.get("teams") or []]
            teams = [team for team in teams if team]
            title = str(game.get("name") or "").strip() or " at ".join(reversed(teams)) or spec.name
            end = _parse_utc(game.get("end_time"))
            row = {
                "id": game.get("kgmid") or game.get("id"),
                "title": title,
                "start_utc": utc_now(start),
                "end_utc": utc_now(end) if end else None,
                "participants": teams,
                "status": game.get("status") or game.get("status_original"),
                "metadata": {"group": group.get("title"), "league": (game.get("league") or {}).get("name")},
            }
            row["id"] = _stable_event_id(spec.key, row)
            events.append(row)
    return events


def parse_espn_events(spec: LeagueSpec, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for event in payload.get("events") or []:
        start = _parse_utc(event.get("date"))
        if not start:
            continue
        competition = (event.get("competitions") or [{}])[0] or {}
        participants = []
        for competitor in competition.get("competitors") or []:
            team = competitor.get("team") or {}
            name = team.get("displayName") or team.get("shortDisplayName") or competitor.get("displayName")
            if name:
                participants.append(str(name))
        end = _parse_utc(event.get("endDate") or competition.get("endDate"))
        status = ((event.get("status") or {}).get("type") or {})
        row = {
            "id": event.get("id") or event.get("uid"),
            "title": str(event.get("name") or event.get("shortName") or spec.name),
            "start_utc": utc_now(start),
            "end_utc": utc_now(end) if end else None,
            "participants": participants,
            "status": status.get("state") or status.get("name"),
            "metadata": {"season": (event.get("season") or {}).get("year")},
        }
        row["id"] = _stable_event_id(spec.key, row)
        events.append(row)
    return events


def _upsert_events(conn: sqlite3.Connection, spec: LeagueSpec, events: Iterable[Mapping[str, Any]], now_text: str) -> int:
    conn.execute("UPDATE sports_schedule_reference_events SET active=0 WHERE source=? AND league_key=?",
                 (spec.source, spec.key))
    count = 0
    for event in events:
        conn.execute("""
          INSERT INTO sports_schedule_reference_events(
            source,league_key,source_event_id,title,start_utc,end_utc,participants_json,event_status,
            source_metadata_json,active,first_seen_utc,last_seen_utc
          ) VALUES(?,?,?,?,?,?,?,?,?,1,?,?)
          ON CONFLICT(source,league_key,source_event_id) DO UPDATE SET
            title=excluded.title,start_utc=excluded.start_utc,end_utc=excluded.end_utc,
            participants_json=excluded.participants_json,event_status=excluded.event_status,
            source_metadata_json=excluded.source_metadata_json,active=1,last_seen_utc=excluded.last_seen_utc
        """, (spec.source, spec.key, str(event["id"]), str(event["title"]), str(event["start_utc"]),
              event.get("end_utc"), json.dumps(event.get("participants") or []), event.get("status"),
              json.dumps(event.get("metadata") or {}, sort_keys=True), now_text, now_text))
        count += 1
    return count


def _set_league_state(conn: sqlite3.Connection, spec: LeagueSpec, *, status: str, now_text: str,
                      event_count: int = 0, error: str | None = None, success: bool = False) -> None:
    conn.execute("""
      INSERT INTO sports_schedule_league_state(
        league_key,source,status,event_count,last_attempt_utc,last_success_utc,error
      ) VALUES(?,?,?,?,?,?,?)
      ON CONFLICT(league_key) DO UPDATE SET
        source=excluded.source,status=excluded.status,event_count=excluded.event_count,
        last_attempt_utc=excluded.last_attempt_utc,
        last_success_utc=COALESCE(excluded.last_success_utc,sports_schedule_league_state.last_success_utc),
        error=excluded.error
    """, (spec.key, spec.source, status, event_count, now_text, now_text if success else None, error))


def _usage_period(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m")


def _usage(conn: sqlite3.Connection, now: datetime) -> int:
    row = conn.execute("SELECT successful_searches FROM sports_schedule_api_usage WHERE period=?",
                       (_usage_period(now),)).fetchone()
    return int(row[0]) if row else 0


def _increment_usage(conn: sqlite3.Connection, now: datetime) -> None:
    period = _usage_period(now)
    conn.execute("""
      INSERT INTO sports_schedule_api_usage(period,successful_searches,updated_utc) VALUES(?,1,?)
      ON CONFLICT(period) DO UPDATE SET successful_searches=successful_searches+1,updated_utc=excluded.updated_utc
    """, (period, utc_now(now)))


def _get_json(getter: Callable[..., Any], url: str, *, params: Mapping[str, Any], timeout: float,
              user_agent: str = "FruitDeepLinks-schedule-audit/1.0") -> tuple[int, Mapping[str, Any]]:
    response = getter(url, params=dict(params), timeout=timeout,
                      headers={"Accept": "application/json", "User-Agent": user_agent})
    status = int(getattr(response, "status_code", 200))
    if status >= 400:
        return status, {}
    payload = response.json()
    return status, payload if isinstance(payload, Mapping) else {}


def _last_finished(conn: sqlite3.Connection) -> datetime | None:
    row = conn.execute("SELECT finished_utc FROM sports_schedule_audit_runs WHERE finished_utc IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
    return _parse_utc(row[0]) if row else None


def refresh(conn: sqlite3.Connection, *, api_key: str | None = None, days: int = DEFAULT_DAYS,
            due_hours: int = DEFAULT_DUE_HOURS, force: bool = False,
            monthly_budget: int = DEFAULT_MONTHLY_BUDGET, budget_reserve: int = DEFAULT_BUDGET_RESERVE,
            now: datetime | None = None, getter: Callable[..., Any] = requests.get,
            timeout: float = 20.0) -> dict[str, Any]:
    """Refresh materialized audit references without touching canonical events."""
    ensure_schema(conn)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    last = _last_finished(conn)
    if not force and last and current - last < timedelta(hours=max(1, due_hours)):
        return {"status": "not_due", "last_finished_utc": utc_now(last), "checked_leagues": 0,
                "successful_searches": 0, "event_count": 0}

    keys = enabled_keys(conn)
    specs = [LEAGUE_BY_KEY[key] for key in keys]
    started = utc_now(current)
    cursor = conn.execute(
        "INSERT INTO sports_schedule_audit_runs(started_utc,status,requested_leagues) VALUES(?,?,?)",
        (started, "running", len(specs)),
    )
    run_id = int(cursor.lastrowid)
    conn.commit()
    checked = searches = event_count = 0
    details: dict[str, Any] = {}
    window_start = current - timedelta(days=1)
    window_end = current + timedelta(days=max(1, min(int(days), 31)))
    usable_budget = max(0, int(monthly_budget) - max(0, int(budget_reserve)))

    for spec in specs:
        now_text = utc_now(current)
        try:
            if spec.source == "serpapi":
                if not api_key:
                    _set_league_state(conn, spec, status="key_required", now_text=now_text,
                                      error="SERPAPI_API_KEY is not configured")
                    details[spec.key] = "key_required"
                    continue
                if _usage(conn, current) >= usable_budget:
                    _set_league_state(conn, spec, status="budget_reserved", now_text=now_text,
                                      error="Monthly safety reserve reached")
                    details[spec.key] = "budget_reserved"
                    continue
                params = {
                    "engine": "google_sports", "kgmid": spec.kgmid, "sp": spec.sport_code,
                    "type": "league", "tab": "gm", "gl": "us", "hl": "en",
                    "moa": utc_now(window_start), "mob": utc_now(window_end), "api_key": api_key,
                }
                status_code, payload = _get_json(getter, SERPAPI_URL, params=params, timeout=timeout)
                if status_code >= 400 or payload.get("error"):
                    error = f"http_{status_code}" if status_code >= 400 else "provider_error"
                    _set_league_state(conn, spec, status="failed", now_text=now_text, error=error)
                    details[spec.key] = error
                    continue
                _increment_usage(conn, current)
                searches += 1
                events = parse_serpapi_events(spec, payload)
            else:
                url = ESPN_SCOREBOARD_URL.format(sport=spec.espn_sport, league=spec.espn_league)
                status_code, payload = _get_json(
                    getter, url, params={"dates": str(current.year), "limit": 1000}, timeout=timeout,
                    # ESPN currently rejects application-specific agents while
                    # serving its public JSON feed to curl's standard agent.
                    user_agent="curl/8.7.1",
                )
                if status_code >= 400:
                    error = f"http_{status_code}"
                    _set_league_state(conn, spec, status="failed", now_text=now_text, error=error)
                    details[spec.key] = error
                    continue
                events = parse_espn_events(spec, payload)
            count = _upsert_events(conn, spec, events, now_text)
            _set_league_state(conn, spec, status="ok", now_text=now_text, event_count=count, success=True)
            details[spec.key] = {"status": "ok", "events": count}
            event_count += count
            checked += 1
            conn.commit()
        except Exception as exc:
            conn.rollback()
            ensure_schema(conn)
            error = _safe_error("transport_error", exc)
            _set_league_state(conn, spec, status="failed", now_text=now_text, error=error)
            conn.commit()
            details[spec.key] = error

    finished = utc_now(current)
    status = "complete" if checked == len(specs) else ("partial" if checked else "unavailable")
    conn.execute("""
      UPDATE sports_schedule_audit_runs SET finished_utc=?,status=?,checked_leagues=?,successful_searches=?,
        event_count=?,detail_json=? WHERE id=?
    """, (finished, status, checked, searches, event_count, json.dumps(details, sort_keys=True), run_id))
    conn.commit()
    return {"status": status, "run_id": run_id, "requested_leagues": len(specs),
            "checked_leagues": checked, "successful_searches": searches, "event_count": event_count,
            "details": details, "finished_utc": finished}


def _league_aliases(spec: LeagueSpec) -> set[str]:
    return {_normalize(spec.name), *(_normalize(alias) for alias in spec.aliases)}


def _participant_match(reference: list[str], canonical: list[str]) -> bool:
    if not reference:
        return True
    ref = {_normalize(value) for value in reference if _normalize(value)}
    can = {_normalize(value) for value in canonical if _normalize(value)}
    matches = 0
    for expected in ref:
        if any(expected == observed or expected in observed or observed in expected for observed in can):
            matches += 1
    return matches >= min(2, len(ref))


def snapshot(conn: sqlite3.Connection, *, days: int = DEFAULT_DAYS, now: datetime | None = None) -> dict[str, Any]:
    """Read-only external-vs-canonical coverage snapshot for the UI/API."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    leagues = registry(conn)
    usage = 0
    last_run = None
    if "sports_schedule_api_usage" in tables:
        usage = _usage(conn, current)
    if "sports_schedule_audit_runs" in tables:
        row = conn.execute("SELECT * FROM sports_schedule_audit_runs ORDER BY id DESC LIMIT 1").fetchone()
        last_run = dict(row) if row else None
    if "sports_schedule_reference_events" not in tables:
        return {"leagues": leagues, "events": [], "summary": {"expected": 0, "scheduled": 0,
                "playable_found": 0, "no_playable": 0, "missing_from_ingestion": 0},
                "usage": {"period": _usage_period(current), "successful_searches": usage,
                          "monthly_budget": DEFAULT_MONTHLY_BUDGET}, "last_run": last_run}

    end = current + timedelta(days=max(1, min(int(days), 90)))
    references = [dict(row) for row in conn.execute("""
      SELECT * FROM sports_schedule_reference_events
      WHERE active=1 AND datetime(start_utc) BETWEEN datetime(?) AND datetime(?)
      ORDER BY start_utc,league_key,title
    """, (utc_now(current - timedelta(days=1)), utc_now(end)))]
    canonical: list[dict[str, Any]] = []
    if "canonical_events" in tables:
        canonical = [dict(row) for row in conn.execute("""
          SELECT ce.id,ce.title,ce.start_utc,ce.league_id,l.name AS league_name
          FROM canonical_events ce LEFT JOIN leagues l ON l.id=ce.league_id
          WHERE datetime(ce.start_utc) BETWEEN datetime(?) AND datetime(?)
        """, (utc_now(current - timedelta(days=1)), utc_now(end)))]
    participants: dict[str, list[str]] = {}
    if canonical and "canonical_event_participants" in tables:
        ids = [row["id"] for row in canonical]
        marks = ",".join("?" for _ in ids)
        for row in conn.execute(
            f"SELECT event_id,display_name FROM canonical_event_participants WHERE event_id IN ({marks})", ids
        ):
            participants.setdefault(str(row[0]), []).append(str(row[1]))
    playable_counts: dict[str, int] = {}
    lane_ids: dict[str, int] = {}
    if canonical and "source_event_records" in tables and "playables" in tables:
        ids = [row["id"] for row in canonical]
        marks = ",".join("?" for _ in ids)
        for row in conn.execute(
            f"SELECT ser.canonical_event_id,COUNT(p.playable_id) FROM source_event_records ser "
            f"JOIN playables p ON p.event_id=ser.source_event_id WHERE ser.canonical_event_id IN ({marks}) "
            "GROUP BY ser.canonical_event_id", ids,
        ):
            playable_counts[str(row[0])] = int(row[1])
    if canonical and "source_event_records" in tables and "lane_events" in tables:
        ids = [row["id"] for row in canonical]
        marks = ",".join("?" for _ in ids)
        for row in conn.execute(
            f"SELECT ser.canonical_event_id,MIN(le.lane_id) FROM source_event_records ser "
            f"JOIN lane_events le ON le.event_id=ser.source_event_id "
            f"WHERE ser.canonical_event_id IN ({marks}) AND COALESCE(le.is_placeholder,0)=0 "
            "GROUP BY ser.canonical_event_id", ids,
        ):
            lane_ids[str(row[0])] = int(row[1])

    items = []
    for reference in references:
        spec = LEAGUE_BY_KEY.get(reference["league_key"])
        if not spec:
            continue
        reference_start = _parse_utc(reference["start_utc"])
        reference_participants = json.loads(reference.get("participants_json") or "[]")
        match = None
        for event in canonical:
            if _normalize(event.get("league_name")) not in _league_aliases(spec):
                continue
            canonical_start = _parse_utc(event.get("start_utc"))
            if not reference_start or not canonical_start or abs((canonical_start - reference_start).total_seconds()) > 7200:
                continue
            if spec.sport == "Motorsport":
                expected = _normalize(reference.get("title"))
                observed = _normalize(event.get("title"))
                if expected and observed and expected not in observed and observed not in expected:
                    continue
            elif not _participant_match(reference_participants, participants.get(event["id"], [])):
                continue
            match = event
            break
        canonical_id = str(match["id"]) if match else None
        if not canonical_id:
            state = "missing_from_ingestion"
        elif canonical_id in lane_ids:
            state = "scheduled"
        elif playable_counts.get(canonical_id, 0):
            state = "playable_found"
        else:
            state = "no_playable"
        items.append({
            "source": reference["source"], "league_key": spec.key, "league": spec.name,
            "source_event_id": reference["source_event_id"], "title": reference["title"],
            "start_utc": reference["start_utc"], "participants": reference_participants,
            "canonical_event_id": canonical_id, "coverage_state": state,
            "playable_count": playable_counts.get(canonical_id, 0) if canonical_id else 0,
            "lane_id": lane_ids.get(canonical_id) if canonical_id else None,
        })
    summary = {"expected": len(items), "scheduled": 0, "playable_found": 0,
               "no_playable": 0, "missing_from_ingestion": 0}
    for item in items:
        summary[item["coverage_state"]] += 1
    return {"leagues": leagues, "events": items, "summary": summary,
            "usage": {"period": _usage_period(current), "successful_searches": usage,
                      "monthly_budget": DEFAULT_MONTHLY_BUDGET}, "last_run": last_run}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh advisory external sports schedule coverage")
    parser.add_argument("--db", required=True)
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--due-hours", type=int, default=DEFAULT_DUE_HOURS)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    with connect(args.db) as conn:
        result = refresh(conn, api_key=os.getenv("SERPAPI_API_KEY"), days=args.days,
                         due_hours=args.due_hours, force=args.force)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
