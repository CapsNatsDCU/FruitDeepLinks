"""Read-only, offline replay of Fruit team merges and name-pattern candidates.

Input is a saved GET /api/sports/catalog response. This command never connects to
Fruit, changes a catalog, or treats a name match as authority to merge teams.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from sports_catalog import normalize
from team_name_forms import expand_obvious_abbreviation, strip_parentheses


_TRAILING_TIMESTAMP = re.compile(r"\s*\(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?\)\s*$")
_TRAILING_PAREN = re.compile(r"^(.*?)\s*\(([^()]*)\)\s*$")
_LEADING_NUMBER = re.compile(r"^#?(\d+)[\s.:-]+(.+)$")
_CHANNEL_NUMBER = re.compile(r"^0[1-9]\s*:\s*([^():#]+?)\s*$")
_NUMBER_CANDIDATE = re.compile(r"^0[1-9][\s.:-]+(.+)$")


def programmatic_name(name: str) -> dict[str, Any]:
    """Build name-only comparison features; no team ID crosses this boundary."""
    bare = strip_parentheses(name)
    if not bare:
        return {"key": "", "bare_key": "", "unnumbered_key": ""}
    number = _NUMBER_CANDIDATE.fullmatch(bare)
    unnumbered = number.group(1).strip() if number else bare
    expanded = expand_obvious_abbreviation(unnumbered)
    return {"key": normalize(expanded), "bare_key": normalize(bare),
            "unnumbered_key": normalize(unnumbered)}


def programmatic_pair(first_name: str, second_name: str) -> dict[str, Any]:
    """Compare names alone. Opaque catalog IDs cannot influence the result."""
    first, second = programmatic_name(first_name), programmatic_name(second_name)
    matched = bool(first["key"]) and first["key"] == second["key"]
    features = {"parenthetical": normalize(first_name) != normalize(second_name) and
                ("(" in first_name or "(" in second_name),
                "number_prefix": first["bare_key"] != second["bare_key"] and
                first["unnumbered_key"] == second["unnumbered_key"],
                "abbreviation": first["unnumbered_key"] != second["unnumbered_key"]}
    kind = "+".join(k for k, present in features.items() if present) or "same_name"
    return {"matched": matched, "kind": kind if matched else "different_names"}


def _without_timestamp(name: str) -> str:
    result = name.strip()
    while match := _TRAILING_TIMESTAMP.search(result):
        result = result[:match.start()].strip()
    return result


def _modifiers(name: str) -> list[dict[str, str]]:
    """Return only visible, one-step differences; never infer identity."""
    result: list[dict[str, str]] = []
    parent = _TRAILING_PAREN.fullmatch(name)
    if parent and normalize(parent.group(1)) and parent.group(2).strip():
        result.append({"kind": "parenthetical", "base": parent.group(1).strip(),
                       "detail": parent.group(2).strip()})
    number = _LEADING_NUMBER.fullmatch(name)
    if number and normalize(number.group(2)):
        result.append({"kind": "number_prefix", "base": number.group(2).strip(),
                       "detail": number.group(1)})
    return result


def compare_names(first: str, second: str) -> dict[str, Any]:
    """Classify two names without silently discarding qualifiers or rankings."""
    if normalize(first) == normalize(second):
        return {"kind": "same_name"}
    clean_first, clean_second = _without_timestamp(first), _without_timestamp(second)
    for modified, other in ((clean_first, clean_second), (clean_second, clean_first)):
        for modifier in _modifiers(modified):
            if normalize(modifier["base"]) == normalize(other):
                return {"kind": modifier["kind"], "detail": modifier["detail"]}
    first_parent = next((m for m in _modifiers(clean_first) if m["kind"] == "parenthetical"), None)
    second_parent = next((m for m in _modifiers(clean_second) if m["kind"] == "parenthetical"), None)
    if (first_parent and second_parent and
            normalize(first_parent["base"]) == normalize(second_parent["base"]) and
            normalize(first_parent["detail"]) != normalize(second_parent["detail"])):
        return {"kind": "different_qualifiers", "details": [first_parent["detail"], second_parent["detail"]]}
    if normalize(clean_first) == normalize(clean_second):
        return {"kind": "timestamp_variant"}
    return {"kind": "different_names"}


def _scope(team: dict[str, Any]) -> tuple[Any, Any]:
    return team.get("sport_id"), team.get("league_id")


def _active(team: dict[str, Any]) -> bool:
    state = team.get("state") or {}
    return not state.get("archived") and not state.get("merged_into_id")


def _shadow_numbered_merge_reason(plain: dict[str, Any], variant: dict[str, Any],
                                  *, target_count: int, variant_count: int) -> str:
    """Return a fail-closed reason or 'eligible' for a shadow-only rule.

    A structured Apple mapping corroborates the clean target, not the prefixed
    record. Eligibility is therefore a test proposal, never merge authority.
    """
    required = ("operator_fields_present", "alias_count", "mapping_sources", "confirmed_mapping_sources",
                "manual_mapping_count", "rule_count")
    if any(key not in row for row in (plain, variant) for key in required):
        return "snapshot_lacks_safety_fields"
    match = _CHANNEL_NUMBER.fullmatch(variant["name"].strip())
    if not match or " ".join(match.group(1).split()).casefold() != " ".join(plain["name"].split()).casefold():
        return "not_exact_channel_number_format"
    if not plain.get("parent_scope_confirmed") or not variant.get("parent_scope_confirmed"):
        return "parent_scope_unconfirmed"
    if len(normalize(plain["name"]).split()) < 2:
        return "short_or_ambiguous_base_name"
    if target_count != 1 or variant_count != 1:
        return "ambiguous_target_or_multiple_variants"
    if _scope(plain) != _scope(variant) or not all(_scope(plain)):
        return "scope_mismatch_or_incomplete"
    if not plain["confirmed_mapping_sources"] or variant["mapping_sources"]:
        return "target_not_confirmed_or_variant_has_mapping"
    if variant["manual_mapping_count"]:
        return "operator_mapping_present"
    if plain["operator_fields_present"] or variant["operator_fields_present"]:
        return "operator_edit_present"
    if variant["alias_count"] or variant["rule_count"]:
        return "variant_has_alias_or_rule"
    return "eligible"


def audit_catalog(catalog: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(catalog.get("teams"), list) or not isinstance(catalog.get("merges"), list):
        raise ValueError("catalog snapshot must contain teams and merges arrays")
    teams = {str(row["id"]): row for row in catalog["teams"]}
    history = {(str(row["survivor_id"]), str(row["source_id"])): row
               for row in catalog["merges"] if row.get("entity_type") == "team" and row.get("status") == "active"}
    parent_merges = {(row.get("entity_type"), str(row.get("source_id"))): str(row.get("survivor_id"))
                     for row in catalog["merges"] if row.get("entity_type") in {"sport", "league"}
                     and row.get("status") == "active"}

    def scope_status(destination: dict[str, Any], source: dict[str, Any]) -> str:
        if not all(_scope(destination)) or not all(_scope(source)):
            return "incomplete"
        if _scope(destination) == _scope(source):
            return "same"
        sport_matches = (destination["sport_id"] == source["sport_id"] or
                         parent_merges.get(("sport", str(source["sport_id"]))) == str(destination["sport_id"]))
        league_matches = (destination["league_id"] == source["league_id"] or
                          parent_merges.get(("league", str(source["league_id"]))) == str(destination["league_id"]))
        return "parent_merge" if sport_matches and league_matches else "mismatch"

    merged = []
    missing_destinations = []
    for source in teams.values():
        destination_id = (source.get("state") or {}).get("merged_into_id")
        if not destination_id:
            continue
        destination = teams.get(str(destination_id))
        if not destination:
            missing_destinations.append(str(source["id"]))
            continue
        relation = compare_names(destination["name"], source["name"])
        merge = history.get((str(destination["id"]), str(source["id"])))
        merged.append({"merge_id": merge.get("id") if merge else None,
                       "survivor_id": destination["id"], "survivor_name": destination["name"],
                       "source_id": source["id"], "source_name": source["name"],
                       "sport_id": source.get("sport_id"), "league_id": source.get("league_id"),
                       "scope_status": scope_status(destination, source), **relation})
    merged.sort(key=lambda row: (str(row["kind"]), str(row["survivor_name"]), str(row["source_name"])))

    # Group by scope and the name-only comparison key. Opaque team IDs are
    # attached after comparison solely for local relationship checks.
    active = [row for row in teams.values() if _active(row) and all(_scope(row))]
    by_name: dict[tuple[Any, Any, str], list[dict[str, Any]]] = {}
    for row in active:
        key = programmatic_name(row["name"])["key"]
        if key:
            by_name.setdefault((*_scope(row), key), []).append(row)
    candidates: dict[tuple[str, str], dict[str, Any]] = {}
    for group in by_name.values():
        if len(group) < 2:
            continue
        def modification_count(row: dict[str, Any]) -> int:
            features = programmatic_name(row["name"])
            return sum((normalize(row["name"]) != features["bare_key"],
                        features["bare_key"] != features["unnumbered_key"],
                        features["unnumbered_key"] != features["key"]))
        ranked = sorted(group, key=lambda row: (
            modification_count(row), row["name"].casefold(), str(row["id"])))
        for index, plain in enumerate(ranked):
            for variant in ranked[index + 1:]:
                comparison = programmatic_pair(plain["name"], variant["name"])
                if not comparison["matched"] or plain["name"] == variant["name"]:
                    continue
                key = tuple(sorted((str(plain["id"]), str(variant["id"]))))
                candidates[key] = {"team_id": plain["id"], "team_name": plain["name"],
                                   "variant_id": variant["id"], "variant_name": variant["name"],
                                   "sport_id": plain["sport_id"], "league_id": plain["league_id"],
                                   "kind": comparison["kind"],
                                   "decision": "hold_identity_unverified"}
    candidates_list = sorted(candidates.values(), key=lambda row: (row["kind"], row["team_name"], row["variant_name"]))
    variant_targets: dict[str, set[str]] = {}
    target_variants: dict[str, set[str]] = {}
    for row in candidates_list:
        variant_targets.setdefault(str(row["variant_id"]), set()).add(str(row["team_id"]))
        target_variants.setdefault(str(row["team_id"]), set()).add(str(row["variant_id"]))
    for row in candidates_list:
        if row["kind"] != "number_prefix":
            row["hold_reason"] = "name_normalization_requires_identity_evidence"
            continue
        reason = _shadow_numbered_merge_reason(
            teams[str(row["team_id"])], teams[str(row["variant_id"])],
            target_count=len(variant_targets[str(row["variant_id"])]),
            variant_count=len(target_variants[str(row["team_id"])]),
        )
        if reason == "eligible":
            row["decision"] = "shadow_merge_proposal"
        else:
            row["hold_reason"] = reason
    by_kind: dict[str, int] = {}
    for row in merged:
        by_kind[row["kind"]] = by_kind.get(row["kind"], 0) + 1
    candidate_kinds: dict[str, int] = {}
    for row in candidates_list:
        candidate_kinds[row["kind"]] = candidate_kinds.get(row["kind"], 0) + 1
    active_total = sum(_active(row) for row in teams.values())
    confirmed_aliases = sum(bool(row.get("operator_confirmed")) for row in catalog.get("aliases", []))
    shadow_count = sum(row["decision"] == "shadow_merge_proposal" for row in candidates_list)
    blocking = []
    if missing_destinations:
        blocking.append("merged team points to a missing survivor")
    if any(row["kind"] != "same_name" for row in merged):
        blocking.append("existing merges include nonidentical names")
    if any(row["scope_status"] == "mismatch" for row in merged):
        blocking.append("existing merges include unexplained scope mismatches")
    if not catalog.get("provenance") and not confirmed_aliases:
        blocking.append("snapshot has no operator-confirmed provenance or aliases")
    blocking.append("shadow proposals need historical relationship checks and repeated-run validation before writes")
    # The narrow positive rule produces a measurable shadow cohort, not write
    # authority. This command intentionally has no merge or auto-accept path.
    return {"summary": {"teams": len(teams), "active_teams": active_total,
                        "active_teams_in_scope": len(active),
                        "active_teams_missing_scope": active_total - len(active),
                        "active_merged_teams": len(merged), "merge_classifications": by_kind,
                        "scope_classifications": {kind: sum(row["scope_status"] == kind for row in merged)
                                                  for kind in ("same", "parent_merge", "incomplete", "mismatch")},
                        "missing_survivors": len(missing_destinations),
                        "unmerged_pattern_candidates": len(candidates_list),
                        "candidate_classifications": candidate_kinds,
                        "shadow_merge_proposals": shadow_count,
                        "held_pattern_candidates": len(candidates_list) - shadow_count,
                        "provenance_records": len(catalog.get("provenance", [])),
                        "confirmed_aliases": confirmed_aliases,
                        "automatic_merges_authorized": 0,
                        "ready_for_unattended_merging": False},
            "blocking_reasons": blocking, "missing_survivor_ids": missing_destinations,
            "existing_merges": merged, "unmerged_pattern_candidates": candidates_list}


def probe_refresh_redirect() -> bool:
    """Replay a merge in memory and check where the next refresh puts its name."""
    from catalog_workbench import entity_state, merge_entities
    from sports_catalog import apply_catalog_records
    from sports_metadata import _upsert_named, ensure_schema

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
        apply_catalog_records(conn, [
            {"entity_type": "team", "name": "Brooklyn Nets", "sport": "Basketball", "league": "NBA", "source": "manual"},
            {"entity_type": "team", "name": "03 : Brooklyn Nets", "sport": "Basketball", "league": "NBA", "source": "manual"},
        ], dry_run=False)
        keep = conn.execute("SELECT id,sport_id,league_id FROM teams WHERE name='Brooklyn Nets'").fetchone()
        source = conn.execute("SELECT id FROM teams WHERE name='03 : Brooklyn Nets'").fetchone()
        if not keep or not source:
            return False
        merge_entities(conn, entity_type="team", survivor_id=keep["id"], source_id=source["id"])
        refreshed = _upsert_named(conn, "teams", "03 : Brooklyn Nets",
                                  sport_id=keep["sport_id"], league_id=keep["league_id"])
        return refreshed == keep["id"] and not entity_state(conn, "team", refreshed)["archived"]
    finally:
        conn.close()


def check_db_relationships(report: dict[str, Any], db_copy: Path) -> None:
    """Check shadow candidates against a local SQLite copy, always read-only."""
    if not db_copy.is_file() or not db_copy.stat().st_size:
        raise ValueError("--db-copy must be a nonempty local SQLite file")
    conn = sqlite3.connect(db_copy.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        required = {"teams", "catalog_entity_state", "canonical_event_participants",
                    "catalog_entity_provenance", "catalog_aliases", "source_entity_mappings", "sports_rules"}
        found = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = sorted(required - found)
        if missing:
            raise ValueError("database copy is missing required catalog tables: " + ", ".join(missing))
        clear_count = 0
        for candidate in report["unmerged_pattern_candidates"]:
            if candidate["decision"] != "shadow_merge_proposal":
                continue
            source_id, keep_id = str(candidate["variant_id"]), str(candidate["team_id"])
            source = conn.execute("SELECT name,sport_id,league_id,aliases_json FROM teams WHERE id=?", (source_id,)).fetchone()
            keep = conn.execute("SELECT name,sport_id,league_id FROM teams WHERE id=?", (keep_id,)).fetchone()
            if (not source or not keep or source[0] != candidate["variant_name"] or
                    keep[0] != candidate["team_name"] or (source[1], source[2]) != (keep[1], keep[2])):
                candidate["db_relationship_decision"] = "hold_snapshot_drift"
                continue
            source_state = conn.execute("SELECT archived,merged_into_id,operator_fields_json FROM catalog_entity_state WHERE entity_type='team' AND fruit_id=?", (source_id,)).fetchone()
            keep_state = conn.execute("SELECT archived,merged_into_id,operator_fields_json FROM catalog_entity_state WHERE entity_type='team' AND fruit_id=?", (keep_id,)).fetchone()
            if ((source_state and (source_state[0] or source_state[1] or source_state[2] not in (None, "", "{}"))) or
                    (keep_state and (keep_state[0] or keep_state[1] or keep_state[2] not in (None, "", "{}"))) or
                    source[3] not in (None, "", "[]")):
                candidate["db_relationship_decision"] = "hold_snapshot_drift"
                continue
            confirmed_scope = all(conn.execute(
                "SELECT 1 FROM catalog_entity_provenance WHERE entity_type=? AND fruit_id=? AND operator_confirmed=1 LIMIT 1",
                (kind, identifier),
            ).fetchone() for kind, identifier in (("sport", source[1]), ("league", source[2])))
            if not confirmed_scope:
                candidate["db_relationship_decision"] = "hold_parent_scope_unconfirmed"
                continue
            target_confirmed_mappings = conn.execute(
                "SELECT COUNT(*) FROM source_entity_mappings WHERE entity_type='team' AND canonical_id=? AND manual=1",
                (keep_id,),
            ).fetchone()[0]
            if not target_confirmed_mappings:
                candidate["db_relationship_decision"] = "hold_snapshot_drift"
                continue
            checks = {
                "participant_rows": ("SELECT COUNT(*) FROM canonical_event_participants WHERE team_id=?", (source_id,)),
                "provenance_rows": ("SELECT COUNT(*) FROM catalog_entity_provenance WHERE entity_type='team' AND fruit_id=?", (source_id,)),
                "alias_rows": ("SELECT COUNT(*) FROM catalog_aliases WHERE entity_type='team' AND fruit_id=?", (source_id,)),
                "mapping_rows": ("SELECT COUNT(*) FROM source_entity_mappings WHERE entity_type='team' AND canonical_id=?", (source_id,)),
                "rule_rows": ("SELECT COUNT(*) FROM sports_rules WHERE target_type='team' AND target_id=? AND enabled=1", (source_id,)),
            }
            counts = {name: int(conn.execute(sql, params).fetchone()[0]) for name, (sql, params) in checks.items()}
            candidate["db_relationship_counts"] = counts
            candidate["db_relationship_decision"] = "hold_existing_relationships" if any(counts.values()) else "db_clear"
            clear_count += candidate["db_relationship_decision"] == "db_clear"
        report["summary"]["db_relationships_checked"] = True
        report["summary"]["db_clear_shadow_candidates"] = clear_count
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", required=True, type=Path, help="saved Fruit /api/sports/catalog JSON")
    parser.add_argument("--report", type=Path, help="write the full read-only JSON report")
    parser.add_argument("--db-copy", type=Path, help="optional local Fruit SQLite copy for historical relationship checks")
    args = parser.parse_args()
    result = audit_catalog(json.loads(args.catalog.read_text()))
    redirect_safe = probe_refresh_redirect()
    result["summary"]["refresh_redirect_safe"] = redirect_safe
    if not redirect_safe:
        result["blocking_reasons"].append("local refresh lookup reuses a merged-away team ID")
    if args.db_copy:
        check_db_relationships(result, args.db_copy)
    else:
        result["summary"]["db_relationships_checked"] = False
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"summary": result["summary"], "blocking_reasons": result["blocking_reasons"]},
                     indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
