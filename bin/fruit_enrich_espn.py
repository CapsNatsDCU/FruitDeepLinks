#!/usr/bin/env python3
"""
fruit_enrich_espn.py - Enrich Apple TV ESPN events with ESPN Watch Graph IDs

OPTIMIZED VERSION with:
- Corrected SQL JSON extraction using json_each
- Progress indicators
- Faster batch processing

Usage:
  python fruit_enrich_espn.py
  python fruit_enrich_espn.py --fruit-db data/fruit_events.db --espn-db data/espn_graph.db
  python fruit_enrich_espn.py --dry-run
  python fruit_enrich_espn.py --skip-enrich
"""

import argparse
import os
import sqlite3
import sys
import time
from typing import Dict, List, Optional

try:
    _bin_dir = os.path.dirname(__file__)
    if _bin_dir not in sys.path:
        sys.path.insert(0, _bin_dir)
    from core.service_catalog import get_internal_priority
    _PRIORITY_AVAILABLE = True
except ImportError:
    _PRIORITY_AVAILABLE = False

    def get_internal_priority(service_code: str) -> int:
        return 25


def _log(msg: str) -> None:
    print(msg, flush=True)


def entitlement_logical_service(espn_event: Dict) -> Optional[str]:
    """
    Determine a more precise logical_service from ESPN Watch Graph's own
    packages/network fields, when they reveal an entitlement Apple's catalog
    doesn't expose (e.g. a feed that requires MLB.TV or MLB Network even
    though Apple lists it identically to a plain ESPN+ feed).

    Returns None when the Watch Graph data doesn't indicate anything more
    specific than what Step 6's Apple-side classification already assigned.
    """
    packages = espn_event.get('packages') or ''
    network = (espn_event.get('network') or '').strip()

    if 'MLB_TV' in packages:
        return 'mlb'
    if 'MLB_NETWORK' in packages:
        return 'espn_mlb_network'
    if network == 'ESPN Unlimited':
        return 'espn_unlimited'
    return None


def ensure_espn_graph_id_column(fruit_db: str) -> None:
    """Add espn_graph_id column to playables table if it doesn't exist"""
    conn = sqlite3.connect(fruit_db)
    cursor = conn.cursor()
    
    cursor.execute("PRAGMA table_info(playables)")
    columns = [row[1] for row in cursor.fetchall()]
    
    if 'espn_graph_id' not in columns:
        _log("Adding espn_graph_id column to playables table...")
        cursor.execute("ALTER TABLE playables ADD COLUMN espn_graph_id TEXT")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_playables_espn_graph ON playables(espn_graph_id)")
        conn.commit()
        _log("Column added successfully")
    else:
        _log("espn_graph_id column already exists")
    
    conn.close()


def get_apple_espn_playables(fruit_db: str) -> List[Dict]:
    """
    Get all ESPN playables from Apple TV with their externalId values.
    
    OPTIMIZED: Uses SQLite json_each to properly extract externalId from playables JSON.
    
    Returns list of dicts with:
      - playable_id: Playable ID in database
      - external_id: ESPN's program ID (UUID from playables JSON)
      - title: Event title for logging
    """
    _log("Using optimized SQL JSON extraction with json_each")
    
    conn = sqlite3.connect(fruit_db)
    cursor = conn.cursor()
    
    # CORRECTED: Use json_each to iterate through playables object
    # This handles the colon-separated keys properly
    cursor.execute("""
        SELECT
            p.playable_id,
            json_extract(pe.value, '$.externalId') as external_id,
            e.title,
            e.start_utc,
            e.id as event_id,
            p.service_name,
            p.logical_service
        FROM playables p
        JOIN events e ON p.event_id = e.id,
        json_each(json_extract(e.raw_attributes_json, '$.playables')) pe
        WHERE p.provider = 'sportscenter'
          AND pe.key = p.playable_id
          AND json_extract(pe.value, '$.externalId') IS NOT NULL
    """)

    results = []
    for row in cursor.fetchall():
        results.append({
            'playable_id': row[0],
            'external_id': row[1],
            'title': row[2],
            'start_utc': row[3],
            'event_id': row[4],
            'service_name': row[5],
            'logical_service': row[6],
        })
    
    conn.close()
    
    _log(f"Found {len(results)} Apple TV ESPN playables")
    return results


def get_espn_graph_events(espn_db: str) -> Dict[str, List[Dict]]:
    """
    Get ESPN Watch Graph events indexed by program_id.

    A single program_id commonly has multiple feed rows with *different*
    entitlements (e.g. a plain linear ESPN airing and a separate ESPN+/Hulu
    airing of the same broadcast — confirmed live: 163 program_ids in a
    single day's scrape have conflicting packages/network among their
    duplicates). Apple's catalog can likewise expose multiple playables
    (one linear-labeled, one streaming-labeled) that all resolve to that
    same program_id. Collapsing to one arbitrary row here (previously
    MIN(feed id)) meant every matching Apple playable got the same
    espn_graph_id regardless of which one it actually corresponded to —
    so returns a list of all candidates per program_id and lets the caller
    pick the one matching each specific Apple playable's own classification.
    """
    try:
        conn = sqlite3.connect(espn_db)
    except sqlite3.OperationalError as e:
        _log(f"Error: Could not open ESPN database: {espn_db}")
        _log(f"   {e}")
        _log("\nMake sure you've run the ESPN scraper first:")
        _log("  python fruit_ingest_espn_graph.py --db data/espn_graph.db --days 7")
        sys.exit(1)
    
    cursor = conn.cursor()
    
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='events'")
    if not cursor.fetchone():
        _log(f"Error: No 'events' table found in {espn_db}")
        _log("\nRun the ESPN scraper first to populate the database")
        conn.close()
        sys.exit(1)
    
    cursor.execute("""
        SELECT e.id, e.program_id, e.airing_id, e.simulcast_airing_id, e.title, f.url,
               e.packages, e.network, f.id as feed_id
        FROM events e
        JOIN feeds f ON e.id = f.event_id
        WHERE e.program_id IS NOT NULL
        ORDER BY e.program_id, f.id
    """)

    results: Dict[str, List[Dict]] = {}
    for row in cursor.fetchall():
        program_id = row[1]
        results.setdefault(program_id, []).append({
            'id': row[0],
            'airing_id': row[2],
            'simulcast_airing_id': row[3],
            'title': row[4],
            'feed_url': row[5],
            'packages': row[6],
            'network': row[7],
            'feed_id': row[8],
        })

    conn.close()

    _log(f"Found {len(results)} ESPN Watch Graph program_ids ({sum(len(v) for v in results.values())} feed rows)")
    return results


def pick_espn_candidate(candidates: List[Dict], apple_logical_service: Optional[str]) -> Dict:
    """
    Pick the ESPN Watch Graph candidate (of possibly several sharing one
    program_id) that best matches a specific Apple playable.

    Apple's own service_name-based classification (from Step 6) tells us
    whether this playable looks linear or streaming; prefer a Watch Graph
    row whose packages field agrees (empty for linear, non-empty for
    streaming). Falls back to the lowest feed_id — the prior behavior —
    when there's no such row or the classification is ambiguous, so a
    single-candidate program_id (the common case) is unaffected.
    """
    if len(candidates) == 1:
        return candidates[0]

    wants_linear = apple_logical_service == 'espn_linear'
    for c in candidates:
        has_packages = bool(c.get('packages'))
        if wants_linear and not has_packages:
            return c
        if not wants_linear and has_packages:
            return c

    return candidates[0]  # candidates are pre-sorted by feed_id ASC


def enrich_playables(fruit_db: str, espn_db: str, dry_run: bool = False, skip_enrich: bool = False) -> None:
    """
    Match Apple TV ESPN playables with ESPN Watch Graph events.
    Updates playables.espn_graph_id for matched events.
    """
    if skip_enrich:
        _log("="*80)
        _log("ESPN ENRICHMENT - SKIPPED (--skip-enrich flag)")
        _log("="*80)
        return
    
    _log("="*80)
    _log("ESPN ENRICHMENT - Matching Apple TV with ESPN Watch Graph")
    _log("="*80)
    
    if not dry_run:
        ensure_espn_graph_id_column(fruit_db)
    
    _log("\nStep 1: Loading Apple TV ESPN playables...")
    start_time = time.time()
    apple_playables = get_apple_espn_playables(fruit_db)
    load_time = time.time() - start_time
    _log(f"Loaded in {load_time:.2f} seconds")
    
    if not apple_playables:
        _log("No ESPN playables found in Apple TV database")
        _log("   Make sure fruit_import_appletv.py has run successfully")
        return
    
    _log("\nStep 2: Loading ESPN Watch Graph events...")
    start_time = time.time()
    espn_events = get_espn_graph_events(espn_db)
    load_time = time.time() - start_time
    _log(f"Loaded in {load_time:.2f} seconds")
    
    if not espn_events:
        _log("No ESPN events found in ESPN Watch Graph database")
        return
    
    _log("\nStep 3: Matching playables using program.id...")
    _log("-"*80)
    
    start_time = time.time()
    matched = 0
    unmatched = 0
    reclassified = 0
    unmatched_details = []
    updates_to_apply = []
    reclass_updates_to_apply = []
    
    total = len(apple_playables)
    last_progress = 0
    
    for idx, playable in enumerate(apple_playables, 1):
        external_id = playable['external_id']
        
        # Progress indicator every 10%
        progress = int((idx / total) * 100)
        if progress >= last_progress + 10:
            _log(f"Progress: {progress}% ({idx}/{total}) - {matched} matched, {unmatched} unmatched")
            last_progress = progress
        
        if external_id in espn_events:
            espn_event = pick_espn_candidate(espn_events[external_id], playable.get('logical_service'))

            # Extract playback ID from feed URL
            espn_playback_id = None
            
            if espn_event.get('feed_url'):
                try:
                    feed_url = espn_event['feed_url']
                    if '/id/' in feed_url:
                        espn_playback_id = feed_url.split('/id/')[-1]
                        espn_playback_id = espn_playback_id.split('?')[0].split('#')[0]
                except Exception as e:
                    _log(f"Warning: Could not extract playback ID from {feed_url}: {e}")
            
            # Fallback to event ID format
            if not espn_playback_id and espn_event.get('id'):
                try:
                    parts = espn_event['id'].split(':')
                    if len(parts) >= 2:
                        espn_playback_id = parts[1]
                except Exception:
                    pass
            
            if espn_playback_id:
                # Store just the UUID, not the espn-watch: prefix
                espn_graph_id = espn_playback_id
                updates_to_apply.append((espn_graph_id, playable['event_id'], playable['playable_id']))
                matched += 1

                # ESPN's own packages/network data reveals entitlements Apple's catalog
                # doesn't expose (e.g. a feed that actually requires MLB.TV). When it
                # points somewhere more specific than what Step 6 already classified,
                # reclassify the playable so existing service filters catch it.
                new_ls = entitlement_logical_service(espn_event)
                if new_ls:
                    new_priority = get_internal_priority(new_ls)
                    reclass_updates_to_apply.append(
                        (new_ls, new_priority, playable['event_id'], playable['playable_id'])
                    )
                    reclassified += 1

                # Log first 5 matches
                if matched <= 3:
                    _log(f"Match #{matched}: {playable['title'][:60]}")
                    _log(f"   program.id:     {external_id}")
                    _log(f"   ESPN Graph ID:  {espn_graph_id}")
                    _log(f"   FireTV URL:     https://www.espn.com/watch/player/_/id/{espn_graph_id}")
                    if new_ls:
                        _log(f"   Reclassified:   -> {new_ls} (packages={espn_event.get('packages')}, network={espn_event.get('network')})")
            else:
                _log(f"Match found but no usable ESPN ID: {playable['title'][:50]}")
                unmatched += 1
        else:
            unmatched += 1
            unmatched_details.append({
                'title': playable['title'],
                'program_id': external_id,
                'playable_id': playable['playable_id'],
                'start_utc': playable.get('start_utc', 'Unknown')
            })
            
            # Log first 3 unmatched
            if unmatched <= 2:
                _log(f"No match: {playable['title'][:60]}")
                _log(f"   program.id: {external_id}")
    
    match_time = time.time() - start_time
    _log(f"\nMatching completed in {match_time:.2f} seconds")
    
    # Apply batch update
    updated = 0
    if not dry_run and updates_to_apply:
        _log(f"\nApplying {len(updates_to_apply)} updates in batch...")
        start_time = time.time()
        
        conn = sqlite3.connect(fruit_db)
        cursor = conn.cursor()
        
        cursor.executemany("""
            UPDATE playables
            SET espn_graph_id = ?
            WHERE event_id = ? AND playable_id = ?
        """, updates_to_apply)

        updated = cursor.rowcount

        if reclass_updates_to_apply:
            cursor.executemany("""
                UPDATE playables
                SET logical_service = ?, priority = ?
                WHERE event_id = ? AND playable_id = ?
            """, reclass_updates_to_apply)

        conn.commit()
        conn.close()

        update_time = time.time() - start_time
        _log(f"Batch update complete in {update_time:.2f} seconds - {updated} playables updated")
        if reclass_updates_to_apply:
            _log(f"   {len(reclass_updates_to_apply)} reclassified by ESPN entitlement data (MLB.TV / MLB Network / ESPN Unlimited)")
    elif dry_run:
        updated = len(updates_to_apply)
        reclassified = len(reclass_updates_to_apply)
    
    # Summary
    _log("\n" + "="*80)
    _log("ENRICHMENT SUMMARY")
    _log("="*80)
    _log(f"Total Apple TV ESPN playables: {len(apple_playables)}")
    _log(f"Total ESPN Watch Graph events: {len(espn_events)}")
    _log(f"")
    _log(f"Matched:   {matched} ({matched/len(apple_playables)*100:.1f}%)")
    _log(f"Unmatched: {unmatched} ({unmatched/len(apple_playables)*100:.1f}%)")
    _log(f"Reclassified by entitlement data: {reclassified}")
    
    if dry_run:
        _log("\nDry run only - no changes were made")
        _log("   Run without --dry-run to update the database")
    else:
        _log(f"\nSuccessfully enriched {updated} ESPN playables with FireTV-compatible IDs")
    
    # Write unmatched events to file
    if unmatched_details:
        debug_file = "espn_unmatched_debug.txt"
        with open(debug_file, 'w', encoding='utf-8') as f:
            f.write("="*80 + "\n")
            f.write("UNMATCHED ESPN EVENTS - DEBUG REPORT\n")
            f.write("="*80 + "\n\n")
            f.write(f"Total unmatched: {len(unmatched_details)}\n")
            f.write(f"Total ESPN Graph events available: {len(espn_events)}\n\n")
            f.write("="*80 + "\n")
            f.write("UNMATCHED EVENTS:\n")
            f.write("="*80 + "\n\n")
            
            for i, event in enumerate(unmatched_details, 1):
                f.write(f"{i}. {event['title']}\n")
                f.write(f"   Start Time: {event['start_utc']}\n")
                f.write(f"   Apple program.id: {event['program_id']}\n")
                f.write(f"   Playable ID: {event['playable_id']}\n\n")
        
        _log(f"\nWrote unmatched events to: {debug_file} ({len(unmatched_details)} rows)")
    
    if unmatched > 0:
        _log("\nTips for improving match rate:")
        _log("   - ESPN Watch Graph might not have all events yet")
        _log("   - Some events might be on different days")
        _log("   - Try running ESPN scraper with more days: --days 14")


def main():
    parser = argparse.ArgumentParser(
        description="Enrich Apple TV ESPN events with ESPN Watch Graph IDs for FireTV deeplinks"
    )
    parser.add_argument(
        "--fruit-db",
        default="data/fruit_events.db",
        help="Path to FruitDeepLinks database (default: data/fruit_events.db)"
    )
    parser.add_argument(
        "--espn-db",
        default="data/espn_graph.db",
        help="Path to ESPN Watch Graph database (default: data/espn_graph.db)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be matched without making changes"
    )
    parser.add_argument(
        "--skip-enrich",
        action="store_true",
        help="Skip enrichment (for use with --skip-scrape in daily_refresh)"
    )
    
    args = parser.parse_args()
    
    enrich_playables(args.fruit_db, args.espn_db, args.dry_run, args.skip_enrich)


if __name__ == "__main__":
    main()
