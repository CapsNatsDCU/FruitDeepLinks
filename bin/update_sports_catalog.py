#!/usr/bin/env python3
"""Preview or safely apply an offline sports-catalog refresh.

The resolver never calls this program.  It is an operator-run update boundary:
all source responses are cached, changes are previewed by default, and an
external-ID conflict stops an apply rather than merging Fruit identities.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from sports_catalog import apply_catalog_records


# This is deliberately a compact requested-scope bootstrap, not a hand-made
# roster.  Teams and aliases arrive through source importers with provenance.
BOOTSTRAP = (
    ("American football", "NFL"), ("Ice hockey", "NHL"), ("Baseball", "MLB"),
    ("Basketball", "NBA"), ("Basketball", "WNBA"), ("Soccer", "MLS"),
    ("Motorsport", "Formula 1"), ("Motorsport", "NASCAR Cup Series"),
    ("Motorsport", "IndyCar Series"), ("Motorsport", "IMSA SportsCar Championship"),
    ("Motorsport", "FIA World Endurance Championship"),
)
RACING_EVENTS = (
    ("Formula 1", "Italian Grand Prix", ("Italian GP", "Monza")),
    ("Formula 1", "British Grand Prix", ("British GP", "Silverstone")),
    ("IndyCar Series", "Indianapolis 500", ("Indy 500",)),
    ("NASCAR Cup Series", "Daytona 500", ()),
)


def bootstrap_records() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for sport, league in BOOTSTRAP:
        records.extend((
            {"entity_type": "sport", "name": sport, "source": "fruit_bootstrap",
             "provenance": {"scope": "initial_major_sports_seed"}},
            {"entity_type": "league", "name": league, "sport": sport, "source": "fruit_bootstrap",
             "provenance": {"scope": "initial_major_sports_seed"}},
        ))
    sport_by_league = {league: sport for sport, league in BOOTSTRAP}
    for league, name, aliases in RACING_EVENTS:
        records.append({"entity_type": "racing_event", "name": name, "sport": sport_by_league[league], "league": league,
                        "aliases": aliases, "venue_aliases": aliases[-1:] if aliases else (),
                        "session_vocabulary": ("practice", "qualifying", "sprint", "race"), "source": "fruit_bootstrap",
                        "provenance": {"scope": "stable_recurring_event_identity"}})
    return records


def _cache_path(cache_dir: Path, url: str) -> Path:
    return cache_dir / f"{sha256(url.encode('utf-8')).hexdigest()}.json"


def fetch_json(url: str, *, cache_dir: Path, interval: float, timeout: float = 20, refresh: bool = False) -> Any:
    """Fetch a source response once, then reuse its local cache on later previews."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    destination = _cache_path(cache_dir, url)
    if destination.exists() and not refresh:
        return json.loads(destination.read_text(encoding="utf-8"))
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "FruitDeepLinks-catalog-updater/1.0"})
    with urlopen(request, timeout=timeout) as response:  # nosec B310 - explicitly operator-selected HTTPS sources below
        payload = json.loads(response.read().decode("utf-8"))
    destination.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    if interval > 0:
        time.sleep(interval)
    return payload


def thesportsdb_records(fetch: Callable[[str], Any], *, api_key: str, targets: Iterable[tuple[str, str]] = BOOTSTRAP) -> list[dict[str, Any]]:
    """Normalize bounded league team responses; no schedule/event data is retained."""
    records: list[dict[str, Any]] = []
    base = f"https://www.thesportsdb.com/api/v1/json/{quote(api_key, safe='')}/search_all_teams.php"
    for requested_sport, league in targets:
        if league.endswith("Championship") or league in {"Formula 1", "NASCAR Cup Series", "IndyCar Series"}:
            # This provider's racing-team model is not a useful canonical
            # roster.  Recurring races are stable catalog data instead.
            continue
        url = f"{base}?{urlencode({'l': league})}"
        payload = fetch(url) or {}
        for team in payload.get("teams") or []:
            name = team.get("strTeam")
            external_id = team.get("idTeam")
            if not name or not external_id:
                continue
            sport = team.get("strSport") or requested_sport
            source_league = team.get("strLeague") or league
            aliases = [team.get("strAlternate"), team.get("strTeamShort")]
            records.append({"entity_type": "team", "name": name, "sport": sport, "league": source_league,
                            "aliases": [alias for alias in aliases if alias], "source": "thesportsdb",
                            "external_ids": {"thesportsdb": external_id}, "source_url": url,
                            "provenance": {"sport": sport, "league": source_league}})
    return records


def wikidata_records(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Normalize a supplied WDQS result; callers choose a bounded SPARQL query.

    Expected bindings are ``entity``/``entityLabel``, optional ``entityType``,
    ``sport``/``league`` labels, and ``alias``.  Q-IDs are provenance only;
    :func:`apply_catalog_records` creates Fruit-owned IDs.
    """
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in (payload.get("results") or {}).get("bindings") or []:
        label = ((row.get("entityLabel") or row.get("label") or {}).get("value") or "").strip()
        url = ((row.get("entity") or row.get("item") or {}).get("value") or "").strip()
        external_id = url.rsplit("/", 1)[-1]
        entity_type = ((row.get("entityType") or {}).get("value") or "team").strip().casefold()
        if entity_type not in {"sport", "league", "team", "racing_event"} or not label or not external_id.startswith("Q"):
            continue
        key = (entity_type, external_id)
        record = grouped.setdefault(key, {"entity_type": entity_type, "name": label, "source": "wikidata",
                                          "external_ids": {"wikidata": external_id}, "source_url": url, "aliases": [],
                                          "provenance": {"wikidata_entity": url}})
        for output_key, binding_key in (("sport", "sportLabel"), ("league", "leagueLabel")):
            value = (row.get(binding_key) or {}).get("value")
            if value:
                record[output_key] = value
        alias = (row.get("alias") or {}).get("value")
        if alias:
            record["aliases"].append(alias)
    return list(grouped.values())


def openligadb_records(payload: Iterable[Mapping[str, Any]], *, league: str) -> list[dict[str, Any]]:
    """Optional soccer supplement from an explicitly requested league/season."""
    records: dict[str, dict[str, Any]] = {}
    for match in payload:
        for key in ("team1", "team2"):
            team = match.get(key) or {}
            name = team.get("teamName")
            identifier = team.get("teamId")
            if name and identifier is not None:
                records.setdefault(str(identifier), {"entity_type": "team", "name": name, "sport": "Soccer", "league": league,
                                                     "source": "openligadb", "external_ids": {"openligadb": str(identifier)},
                                                     "provenance": {"supplement": "explicit_league_season"}})
    return list(records.values())


def _load_records(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise ValueError("records JSON must be an array")
    return [dict(row) for row in value if isinstance(row, Mapping)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="Fruit SQLite database")
    parser.add_argument("--source", action="append", choices=("bootstrap", "thesportsdb", "wikidata", "openligadb", "json"),
                        help="One or more sources (bootstrap is used when omitted)")
    parser.add_argument("--apply", action="store_true", help="Apply a conflict-free plan; preview is the default")
    parser.add_argument("--cache-dir", default="data/cache/sports_catalog")
    parser.add_argument("--refresh", action="store_true", help="Bypass cached source responses for this explicit update")
    parser.add_argument("--request-interval", type=float, default=2.1, help="Seconds between uncached requests")
    parser.add_argument("--wikidata-query-file", type=Path, help="Bounded SPARQL query file for the official WDQS endpoint")
    parser.add_argument("--records", type=Path, help="Normalized source records JSON (including official exports)")
    parser.add_argument("--openligadb-league", help="Explicit OpenLigaDB league shortcut/name")
    parser.add_argument("--season", help="OpenLigaDB season (required with --openligadb-league)")
    args = parser.parse_args(argv)
    sources = args.source or ["bootstrap"]
    cache_dir = Path(args.cache_dir)
    records: list[dict[str, Any]] = []
    try:
        if "bootstrap" in sources:
            records.extend(bootstrap_records())
        if "json" in sources:
            if not args.records:
                parser.error("--source json requires --records")
            records.extend(_load_records(args.records))
        if "thesportsdb" in sources:
            api_key = os.environ.get("THESPORTSDB_API_KEY", "123")
            records.extend(thesportsdb_records(lambda url: fetch_json(url, cache_dir=cache_dir, interval=args.request_interval, refresh=args.refresh), api_key=api_key))
        if "wikidata" in sources:
            if not args.wikidata_query_file:
                parser.error("--source wikidata requires --wikidata-query-file")
            query = args.wikidata_query_file.read_text(encoding="utf-8")
            url = "https://query.wikidata.org/sparql?" + urlencode({"query": query, "format": "json"})
            records.extend(wikidata_records(fetch_json(url, cache_dir=cache_dir, interval=args.request_interval, refresh=args.refresh)))
        if "openligadb" in sources:
            if not args.openligadb_league or not args.season:
                parser.error("--source openligadb requires --openligadb-league and --season")
            url = f"https://api.openligadb.de/getmatchdata/{quote(args.openligadb_league, safe='')}/{quote(args.season, safe='')}"
            records.extend(openligadb_records(fetch_json(url, cache_dir=cache_dir, interval=args.request_interval, refresh=args.refresh), league=args.openligadb_league))
    except Exception as exc:
        print(f"catalog refresh failed before apply: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(args.db)
    try:
        # The catalog extends the metadata-first schema; initialize that base
        # here so a new, empty Fruit database is a valid update target too.
        from sports_metadata import ensure_schema
        ensure_schema(conn)
        result = apply_catalog_records(conn, records, dry_run=not args.apply)
    finally:
        conn.close()
    print(json.dumps({"mode": "apply" if args.apply else "preview", "records": len(records), **result}, indent=2, sort_keys=True))
    return 0 if not result["conflicts"] and not result["invalid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
