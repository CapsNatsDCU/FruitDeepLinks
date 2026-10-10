"""Offline, opt-in XMLTV schedules for streams dedicated to a sports team."""
from datetime import datetime, timedelta, timezone
import json
import unicodedata
from xml.etree import ElementTree as ET

FRESH_HOURS = 72
GUIDE_DAYS = 7
DEFAULTS = {
    "guide_mode": "standard",
    "team_schedule_key": "",
    "team_pre_minutes": 30,
    "team_post_minutes": 30,
    "team_duration_minutes": 180,
}


def _normal(value):
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def _tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _time(value):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc) if dt.tzinfo else None
    except (ValueError, TypeError):
        return None


def _members(value):
    try:
        data = json.loads(value)
        if not isinstance(data, list):
            return []
        return [x for x in data if isinstance(x, str) and x.strip()]
    except (TypeError, ValueError):
        return []


def team_options(conn):
    """Use the supported league catalog even before a schedule has downloaded."""
    from sports_schedule_audit import LEAGUES, LEAGUE_BY_KEY
    options = {}
    tables = _tables(conn)
    league_keys = {_normal(alias): spec.key for spec in LEAGUES
                   for alias in (spec.key, spec.name, *spec.aliases)}

    def add(league, team, aliases=()):
        if league not in LEAGUE_BY_KEY or not isinstance(team, str) or not team.strip():
            return
        name = " ".join(team.split())
        key = league + "|" + _normal(name)
        item = options.setdefault(key, {"key": key, "team": name,
                                       "league": LEAGUE_BY_KEY[league].name, "aliases": []})
        item["aliases"] = sorted(set(item["aliases"]) | {a for a in aliases if isinstance(a, str) and a.strip()})

    aliases = {}
    league_aliases = {}
    if "catalog_aliases" in tables:
        for row in conn.execute("SELECT entity_type,fruit_id,alias FROM catalog_aliases WHERE entity_type IN ('team','league')"):
            target = aliases if row[0] == "team" else league_aliases
            target.setdefault(row[1], []).append(row[2])
    if {"teams", "leagues"}.issubset(tables):
        join = ""
        condition = ""
        if "catalog_entity_state" in tables:
            join = " LEFT JOIN catalog_entity_state s ON s.entity_type='team' AND s.fruit_id=t.id"
            condition = " WHERE COALESCE(s.archived,0)=0 AND s.merged_into_id IS NULL"
        for row in conn.execute("SELECT t.id,t.name,t.aliases_json,l.id,l.name FROM teams t JOIN leagues l ON l.id=t.league_id" + join + condition):
            league = next((league_keys[_normal(name)] for name in [row[4], *league_aliases.get(row[3], [])]
                           if _normal(name) in league_keys), None)
            add(league, row[1], [*_members(row[2]), *aliases.get(row[0], [])])
    # Only uniquely owned aliases are usable for schedule identity matching.
    owners = {}
    for item in options.values():
        league = item["key"].split("|", 1)[0]
        for name in [item["team"], *item["aliases"]]:
            owners.setdefault((league, _normal(name)), set()).add(item["key"])
    for item in options.values():
        league = item["key"].split("|", 1)[0]
        item["aliases"] = [name for name in item["aliases"] if len(owners[(league, _normal(name))]) == 1]
    if "sports_schedule_reference_events" in tables:
        for row in conn.execute("SELECT league_key,participants_json FROM sports_schedule_reference_events"):
            for team in _members(row[1]):
                owner = owners.get((row[0], _normal(team)), set())
                if len(owner) != 1:
                    add(row[0], team)
    return sorted(options.values(), key=lambda item: (item["league"].casefold(), item["team"].casefold()))


def validate_config(conn, values, *, current=None):
    from server.services.xtream_persistent import PersistentChannelError
    config = {key: values.get(key, (current or {}).get(key, default))
              for key, default in DEFAULTS.items()}
    if not isinstance(config["guide_mode"], str) or config["guide_mode"] not in {"standard", "team"}:
        raise PersistentChannelError("Choose the standard guide or Team schedule")
    for key, maximum, minimum in (
        ("team_pre_minutes", 240, 0), ("team_post_minutes", 240, 0),
        ("team_duration_minutes", 720, 15),
    ):
        value = config[key]
        if type(value) is not int or not minimum <= value <= maximum:
            raise PersistentChannelError("Use whole minutes: pre/post coverage 0–240; game duration 15–720")
    key = config["team_schedule_key"] or ""
    if not isinstance(key, str) or len(key) > 512:
        raise PersistentChannelError("Select a team from the sports catalog")
    config["team_schedule_key"] = key
    if config["guide_mode"] == "team":
        options = team_options(conn)
        known = {item["key"] for item in options}
        known.update(item["key"].split("|", 1)[0] + "|" + _normal(alias)
                     for item in options for alias in item["aliases"])
        # Retain an existing selection if its schedule source disappears. The
        # generated guide reports unavailable, while unrelated edits still work.
        retained = key and current and key == current.get("team_schedule_key")
        if key not in known and not retained:
            raise PersistentChannelError("Select a team from the sports catalog")
    return config


def guide(conn, channel, *, now=None):
    """Generate a continuous seven-day guide without changing persisted schedules."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    begin = current.replace(minute=0, second=0, microsecond=0)
    finish = begin + timedelta(days=GUIDE_DAYS)
    league, _, member = str(channel.get("team_schedule_key") or "").partition("|")
    option = next((t for t in team_options(conn) if t["key"] == channel.get("team_schedule_key")), None)
    team = option["team"] if option else member
    identities = {member, *(_normal(a) for a in (option or {}).get("aliases", []))}
    tables = _tables(conn)
    state = None
    if "sports_schedule_league_state" in tables:
        row = conn.execute(
            "SELECT status,last_success_utc,source FROM sports_schedule_league_state WHERE league_key=?",
            (league,),
        ).fetchone()
        if row:
            state = {"status": row[0], "last_success": _time(row[1]), "source": row[2]}
    healthy = bool(
        state and state["status"] == "ok" and state["last_success"]
        and timedelta(0) <= current - state["last_success"] <= timedelta(hours=FRESH_HOURS)
        and "sports_schedule_reference_events" in tables
    )
    identity_known = False
    if healthy:
        identity_known = any(identities.intersection(_normal(n) for n in _members(row[0]))
            for row in conn.execute("SELECT participants_json FROM sports_schedule_reference_events WHERE league_key=? AND source=?",
                                    (league, state["source"])))
    healthy = healthy and identity_known
    windows = []
    if healthy:
        rows = conn.execute(
            "SELECT title,start_utc,end_utc,participants_json,event_status FROM sports_schedule_reference_events "
            "WHERE active=1 AND league_key=? AND source=? "
            "AND datetime(start_utc) BETWEEN datetime(?) AND datetime(?) ORDER BY start_utc,source_event_id",
            (league, state["source"], (begin - timedelta(days=1)).isoformat(), finish.isoformat()),
        )
        for row in rows:
            names = _members(row[3])
            if not identities.intersection(_normal(name) for name in names):
                continue
            status = _normal(row[4])
            if any(marker in status for marker in ("cancel", "postpon", "suspend", "tbd")):
                continue
            start = _time(row[1])
            if start is None:
                continue
            stop = _time(row[2])
            estimated = stop is None or stop <= start
            if estimated:
                stop = start + timedelta(minutes=channel.get("team_duration_minutes", 180))
            padded_start = max(begin, start - timedelta(minutes=channel.get("team_pre_minutes", 30)))
            padded_stop = min(finish, stop + timedelta(minutes=channel.get("team_post_minutes", 30)))
            if padded_stop <= padded_start:
                continue
            title = " vs. ".join(names) if len(names) == 2 else row[0]
            description = (
                f"Scheduled game for {team}. Game starts {start.isoformat()}. "
                "Coverage includes your pre/post-game padding."
            )
            if estimated:
                description += " Game end time is estimated using your configured duration."
            windows.append([padded_start, padded_stop, title, description])

    # Padding around consecutive games can overlap. Merge those windows so the
    # guide remains continuous and non-overlapping without dropping a matchup.
    merged = []
    for window in sorted(windows):
        if merged and window[0] < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], window[1])
            merged[-1][2] += " / " + window[2]
            merged[-1][3] += "\n" + window[3]
        else:
            merged.append(window)
    filler = "No game scheduled" if healthy else "Schedule unavailable"
    explanation = (
        f"No game for {team} is listed in the saved league schedule during this period. "
        "This does not indicate whether the stream is online."
        if healthy else
        "The saved league schedule is missing, failed, more than three days old, or has no verified identity for this team. "
        "Refresh it in My Sports before relying on this guide."
    )
    blocks = []

    def gap(start, stop):
        while start < stop:
            midnight = start.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            end = min(stop, midnight)
            blocks.append([start, end, filler, explanation])
            start = end

    cursor = begin
    for window in merged:
        gap(cursor, window[0])
        blocks.append(window)
        cursor = window[1]
    gap(cursor, finish)
    programmes = []
    for start, stop, title, description in blocks:
        element = ET.Element(
            "programme", channel=channel.get("effective_guide_id", "preview"),
            start=start.strftime("%Y%m%d%H%M%S +0000"), stop=stop.strftime("%Y%m%d%H%M%S +0000"),
        )
        ET.SubElement(element, "title", lang="en").text = title
        ET.SubElement(element, "desc", lang="en").text = description
        if title != filler:
            ET.SubElement(element, "category").text = "Sports"
        programmes.append(element)
    return {
        "programmes": programmes, "schedule_status": "ready" if healthy else "unavailable", "team": team,
        "last_success": state["last_success"].isoformat() if state and state["last_success"] else None,
    }
