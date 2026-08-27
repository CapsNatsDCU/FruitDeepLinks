#!/usr/bin/env python3
"""
fix_espn_spanish_only.py - Fix ESPN playables that only have Spanish broadcasts,
for the subset ESPN Watch Graph never matched.

For a MATCHED playable, fruit_enrich_espn.py already sets `locale` directly
from Watch Graph's own `language` field -- authoritative, not a guess, so
there's nothing for this script to "fix": if Watch Graph confirms an event's
only ESPN option is genuinely Spanish, that's just reality, and the deeplink
it gets is already correct (filter_integration.py's _resolve_deeplink_for_playable
swaps in the Watch Graph playback ID for any playable with espn_graph_id set,
independent of anything this script does).

This script only has real work to do for the ~20% of ESPN playables Watch
Graph never matches at all (see fruit_enrich_espn.py's unmatched rate): for
those, Apple's own locale guess (from migrate_add_locale.py, since there's no
better signal) might say Spanish-only with no way to verify, so this rewrites
the deeplink to Apple's own externalId (which tends to launch the general
broadcast rather than a Spanish-specific playID) as a best effort, and flags
locale_fallback=1 since we can't be certain the language actually changed.

Usage:
  python fix_espn_spanish_only.py --db data/fruit_events.db
  python fix_espn_spanish_only.py --db data/fruit_events.db --dry-run
"""

import argparse
import sqlite3
from pathlib import Path
from typing import List, Tuple


def find_spanish_only_events(conn: sqlite3.Connection) -> List[Tuple]:
    """
    Find ESPN events that only have Spanish playables (no English
    alternatives) AND were never matched by fruit_enrich_espn.py -- a matched
    playable already has an authoritative locale straight from ESPN Watch
    Graph's own language field (see fruit_enrich_espn.py's locale_set), so if
    it's still Spanish-only after that, that's confirmed reality, not
    something to "fix".

    Also excludes playables whose service_name names a real,
    distinctly-branded Spanish-language channel ("ESPN Deportes",
    "...Español"). Those aren't an ambiguous generic label with an English
    broadcast hiding behind them -- rewriting the deeplink to the externalId
    "general broadcast" wouldn't change what's actually airing, so there's
    nothing to fix. Flagging them here would also set locale_fallback=1,
    which tells _classify_espn_locale() in filter_integration.py to stop
    treating the row as Spanish -- correct for a genuinely ambiguous generic
    label, wrong for a real Deportes feed (that check itself also refuses to
    honor the flag for named-Deportes rows, as defense in depth, but
    excluding them here too avoids the pointless deeplink rewrite and
    misleading locale_fallback=1 state on data that was never actually
    broken).

    Returns: List of (event_id, playable_id, deeplink_play, service_name, title, espn_graph_id, raw_attributes_json) tuples
    """
    cur = conn.cursor()

    # Find events with ESPN playables
    # Note: external_id is stored in events.raw_attributes_json, not in playables table
    cur.execute("""
        WITH event_locales AS (
            SELECT
                event_id,
                COUNT(CASE WHEN locale = 'es_MX' THEN 1 END) as spanish_count,
                COUNT(CASE WHEN locale = 'en_US' OR locale IS NULL THEN 1 END) as english_count
            FROM playables
            WHERE logical_service IN ('espn_plus', 'espn_linear', 'espn_unlimited', 'espn_mlb_network', 'espn_mlb_tv')
            GROUP BY event_id
        )
        SELECT
            p.event_id,
            p.playable_id,
            p.deeplink_play,
            p.service_name,
            p.title,
            p.espn_graph_id,
            e.raw_attributes_json
        FROM playables p
        JOIN event_locales el ON p.event_id = el.event_id
        JOIN events e ON p.event_id = e.id
        WHERE p.logical_service IN ('espn_plus', 'espn_linear', 'espn_unlimited', 'espn_mlb_network', 'espn_mlb_tv')
          AND el.spanish_count > 0
          AND el.english_count = 0
          AND p.locale = 'es_MX'
          AND (p.espn_graph_id IS NULL OR p.espn_graph_id = '')
          AND e.raw_attributes_json IS NOT NULL
          AND p.service_name NOT LIKE '%Deportes%'
          AND p.service_name NOT LIKE '%Espa%ol%'
        ORDER BY p.event_id, p.priority
    """)

    return cur.fetchall()


def fix_spanish_only_playables(
    conn: sqlite3.Connection,
    playables: List[Tuple],
    dry_run: bool = False
) -> int:
    """
    Update deeplinks for Spanish-only, Watch-Graph-unmatched playables to use
    Apple's own externalId instead of the Spanish punchoutUrl playID (tends
    to launch the general broadcast rather than a Spanish-specific stream).
    find_spanish_only_events() already restricts candidates to rows with no
    espn_graph_id, so there's no better (Watch Graph) ID to prefer here.

    Every candidate row also gets locale_fallback=1 (regardless of whether its
    deeplink actually needed rewriting this run), so filter_integration.py's
    language filter stops treating the repaired deeplink as still
    Spanish-exclusive. The row's locale column is intentionally left as
    'es_MX' -- find_spanish_only_events() depends on it to re-detect and
    re-fix this same row every day after Apple's re-scrape resets
    deeplink_play back to the Spanish playID.

    Args:
        conn: Database connection
        playables: List of (event_id, playable_id, deeplink_play, service_name, title, espn_graph_id, raw_attributes_json)
        dry_run: If True, don't make changes

    Returns: Number of playables updated
    """
    if not playables:
        print("No Spanish-only ESPN playables found")
        return 0

    import json
    cur = conn.cursor()
    updates = []
    fallback_flags = []

    for event_id, playable_id, deeplink_play, service_name, title, _espn_graph_id, raw_json in playables:
        # _espn_graph_id is always empty here -- find_spanish_only_events()
        # only returns Watch-Graph-unmatched candidates (see its docstring).
        # Extract current playID from deeplink (if exists)
        current_playid = None
        if deeplink_play and 'playID=' in deeplink_play:
            current_playid = deeplink_play.split('playID=')[1].split('&')[0]
        
        # Extract externalId from raw_attributes_json
        external_id = None
        if raw_json:
            try:
                attrs = json.loads(raw_json)
                playables_dict = attrs.get('playables', {})
                if playables_dict:
                    # Get first playable's externalId
                    first_playable = next(iter(playables_dict.values()), {})
                    external_id = first_playable.get('externalId')
            except:
                pass
        
        if not external_id:
            # Skip if we can't find externalId
            continue

        best_playid = external_id
        source = "externalId"

        # This row has a usable general-broadcast playID, so it's no longer
        # Spanish-exclusive from the filter's point of view -- flag it even
        # if the deeplink text below turns out to already be up to date.
        fallback_flags.append((event_id, playable_id))

        # Skip rewriting the deeplink if already using the best playID
        if current_playid == best_playid:
            continue

        # Build new deeplink
        new_deeplink = f"sportscenter://x-callback-url/showWatchStream?playID={best_playid}"

        if dry_run:
            print(f"\n[DRY RUN] Would update:")
            print(f"  Event: {event_id}")
            print(f"  Title: {title[:60]}...")
            print(f"  Service: {service_name}")
            print(f"  Old playID: {current_playid}")
            print(f"  New playID: {best_playid} (from {source})")

        updates.append((new_deeplink, event_id, playable_id))

    if dry_run:
        if updates:
            print(f"\n[DRY RUN] Would update {len(updates)} playables")
        else:
            print("All Spanish-only playables already using best playID")
        return len(updates)

    if not updates and not fallback_flags:
        print("No Spanish-only playables found")
        return 0

    # Apply deeplink updates
    if updates:
        cur.executemany("""
            UPDATE playables
            SET deeplink_play = ?
            WHERE event_id = ? AND playable_id = ?
        """, updates)

    # Stamp locale_fallback=1 for every repaired candidate so
    # filter_integration.py stops excluding it as Spanish-only. Column is
    # added by migrate_add_espn_locale_fallback.py; tolerate it being absent
    # (e.g. this script run standalone against an unmigrated db).
    if fallback_flags:
        try:
            cur.executemany("""
                UPDATE playables
                SET locale_fallback = 1
                WHERE event_id = ? AND playable_id = ?
            """, fallback_flags)
        except sqlite3.OperationalError as e:
            print(f"[WARN] Could not set locale_fallback (run migrate_add_espn_locale_fallback.py first?): {e}")

    conn.commit()

    print(f"Updated {len(updates)} Spanish-only playables ({len(fallback_flags)} flagged as locale_fallback)")
    return len(updates)


def show_statistics(conn: sqlite3.Connection) -> None:
    """Show statistics about ESPN playables by locale"""
    cur = conn.cursor()
    
    print("\n" + "="*60)
    print("ESPN Playables Statistics")
    print("="*60)
    
    # Overall locale distribution
    cur.execute("""
        SELECT 
            locale,
            COUNT(*) as count
        FROM playables
        WHERE logical_service IN ('espn_plus', 'espn_linear', 'espn_unlimited', 'espn_mlb_network', 'espn_mlb_tv')
        GROUP BY locale
    """)
    
    print("\nLocale distribution:")
    for locale, count in cur.fetchall():
        locale_name = locale if locale else "NULL"
        print(f"  {locale_name}: {count} playables")
    
    # Events with Spanish-only playables
    cur.execute("""
        WITH event_locales AS (
            SELECT 
                event_id,
                COUNT(CASE WHEN locale = 'es_MX' THEN 1 END) as spanish_count,
                COUNT(CASE WHEN locale = 'en_US' OR locale IS NULL THEN 1 END) as english_count
            FROM playables
            WHERE logical_service IN ('espn_plus', 'espn_linear', 'espn_unlimited', 'espn_mlb_network', 'espn_mlb_tv')
            GROUP BY event_id
        )
        SELECT COUNT(*)
        FROM event_locales
        WHERE spanish_count > 0 AND english_count = 0
    """)
    
    spanish_only_count = cur.fetchone()[0]
    print(f"\nEvents with Spanish-only playables: {spanish_only_count}")


def main():
    ap = argparse.ArgumentParser(description="Fix Spanish-only ESPN playables")
    ap.add_argument("--db", default="data/fruit_events.db", help="Path to fruit_events.db")
    ap.add_argument("--dry-run", action="store_true", help="Show what would be done without making changes")
    ap.add_argument("--stats", action="store_true", help="Show statistics only")
    args = ap.parse_args()
    
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        return 1
    
    conn = sqlite3.connect(db_path)
    
    # Show statistics if requested
    if args.stats:
        show_statistics(conn)
        conn.close()
        return 0
    
    # Find Spanish-only playables
    print("Searching for ESPN events with Spanish-only playables...")
    playables = find_spanish_only_events(conn)
    
    if not playables:
        print("No Spanish-only ESPN playables found")
        show_statistics(conn)
        conn.close()
        return 0
    
    print(f"Found {len(playables)} Spanish-only ESPN playables")
    
    # Fix them
    updated_count = fix_spanish_only_playables(conn, playables, dry_run=args.dry_run)
    
    if updated_count > 0 and not args.dry_run:
        print("\n" + "="*60)
        print("NEXT STEPS:")
        print("="*60)
        print("1. Rebuild lanes to apply changes:")
        print("   python fruit_build_lanes.py --db data/fruit_events.db")
        print("\n2. Re-export to CDVR:")
        print("   python fruit_export_direct.py --db data/fruit_events.db")
    
    show_statistics(conn)
    conn.close()
    return 0


if __name__ == "__main__":
    exit(main())
