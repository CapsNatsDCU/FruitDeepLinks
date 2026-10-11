"""Capture a credential-free Fruit catalog snapshot for an offline merge replay.

Only two Fruit GET endpoints are read. Raw responses stay in memory; the saved
file contains the minimum team and merge fields needed by the rehearsal.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def make_snapshot(catalog: dict, rules_payload: dict) -> dict:
    if not catalog.get("ok") or not rules_payload.get("ok"):
        raise ValueError("Fruit catalog or rules response was unsuccessful")
    if not all(isinstance(catalog.get(key), list) for key in ("teams", "merges", "aliases", "provenance", "source_mappings")):
        raise ValueError("Fruit catalog response is missing required arrays")
    if not isinstance(rules_payload.get("rules"), list):
        raise ValueError("Fruit rules response is missing its rules array")
    mappings = defaultdict(list)
    for row in catalog["source_mappings"]:
        if row.get("entity_type") == "team":
            mappings[str(row["canonical_id"])].append(row)
    aliases = Counter(str(row["fruit_id"]) for row in catalog["aliases"] if row.get("entity_type") == "team")
    team_rules = Counter(str(row["target_id"]) for row in rules_payload["rules"] if row.get("target_type") == "team")
    teams = []
    for row in catalog["teams"]:
        fruit_id = str(row["id"])
        state = row.get("state") or {}
        try:
            legacy_aliases = json.loads(row.get("aliases_json") or "[]")
            legacy_alias_count = len(legacy_aliases) if isinstance(legacy_aliases, list) else 1
        except (TypeError, ValueError):
            legacy_alias_count = 1
        teams.append({"id": fruit_id, "name": row["name"],
                      "sport_id": row.get("sport_id"), "league_id": row.get("league_id"),
                      "state": {"archived": bool(state.get("archived")),
                                "merged_into_id": state.get("merged_into_id")},
                      "operator_fields_present": bool(state.get("operator_fields")),
                      "alias_count": aliases[fruit_id] + legacy_alias_count,
                      "mapping_sources": sorted({str(item["source"]) for item in mappings[fruit_id]}),
                      "manual_mapping_count": sum(bool(item.get("manual")) for item in mappings[fruit_id]),
                      "rule_count": team_rules[fruit_id]})
    return {"captured_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "snapshot_source": "Fruit GET /api/sports/catalog and /api/sports/rules",
            "teams": teams,
            "merges": [{key: row.get(key) for key in ("id", "entity_type", "survivor_id", "source_id", "status")}
                       for row in catalog["merges"]],
            "aliases": [{"operator_confirmed": bool(row.get("operator_confirmed"))} for row in catalog["aliases"]],
            "provenance": [{"entity_type": row.get("entity_type"),
                            "operator_confirmed": bool(row.get("operator_confirmed"))}
                           for row in catalog["provenance"]]}


def _read_json(url: str, *, max_bytes: int) -> dict:
    request = Request(url, headers={"Accept": "application/json"}, method="GET")
    with urlopen(request, timeout=30) as response:
        raw = response.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError("Fruit response exceeded the snapshot size limit")
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError("Fruit response must be a JSON object")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="Fruit app origin, for example http://HOST:6655")
    parser.add_argument("--output", required=True, type=Path, help="local snapshot path")
    args = parser.parse_args()
    if args.output.suffix.lower() != ".json":
        parser.error("--output must name a .json file")
    parsed = urlsplit(args.base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        parser.error("--base-url must be an HTTP(S) origin without credentials, path, or query")
    origin = args.base_url.rstrip("/")
    catalog = _read_json(origin + "/api/sports/catalog", max_bytes=20_000_000)
    rules = _read_json(origin + "/api/sports/rules", max_bytes=2_000_000)
    snapshot = make_snapshot(catalog, rules)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(json.dumps({"snapshot": str(args.output), "teams": len(snapshot["teams"]),
                      "merges": len(snapshot["merges"])}))


if __name__ == "__main__":
    main()
