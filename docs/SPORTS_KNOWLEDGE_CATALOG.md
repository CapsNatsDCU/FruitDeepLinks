# Sports knowledge catalog

Fruit resolves sports events from a local catalog.  It never calls an Internet
API while importing Apple/Xtream/ESPN records or allocating lanes.

The catalog is identity data, not a schedule: sports, leagues, teams, safe
aliases, source IDs, and recurring racing-event/venue/session vocabulary.  A
live event still enters through the normal canonical-event pipeline.

## Ownership and precedence

Fruit-generated IDs remain the canonical IDs.  Upstream IDs (including a
Wikidata Q-ID) live in `catalog_entity_provenance`; they cannot replace a Fruit
ID.  Manual/operator-confirmed mappings and aliases are retained on refresh.
An incoming external ID that already points to another Fruit entity is reported
as a conflict and no changes are applied.  The updater has no delete or
automatic-merge operation.

At runtime an alias is accepted only when it exactly and uniquely identifies a
team within the current sport and league.  Generic unconfirmed one-word source
aliases are not imported as resolver aliases.  This deliberately keeps records
such as `Capitals` or `Nationals` unresolved instead of risking a false merge.

## Sources

- **Wikidata** is the preferred broad baseline.  Supply a bounded SPARQL query
  whose rows contain `entity`, `entityLabel`, optional `entityType`,
  `sportLabel`, `leagueLabel`, and `alias`; the importer records Q-IDs strictly
  as provenance.
- **TheSportsDB** is optional bounded enrichment for league membership, team
  names, alternates, and source IDs.  Its results are cached and rate-spaced;
  no credential is stored in Fruit.  `THESPORTSDB_API_KEY`, when set by the
  operator, is read only for that process.
- **OpenLigaDB** is an optional, explicit league/season soccer supplement.  It
  is not treated as a global soccer authority.
- Official league/series data should be converted to the documented normalized
  JSON record format and supplied with `--source json --records ...`; this is
  an update boundary, not a live scraper dependency.

The compact bootstrap seeds only the requested major league/series and stable
racing event identities.  It does not hand-maintain a team roster.  Run source
updates to obtain teams and aliases with provenance.

## Updating

All commands preview by default; use `--apply` only after inspecting the JSON
plan.  The database path must be the normal Fruit database, not a copied guide.

```sh
python3 bin/update_sports_catalog.py --db data/fruit.db
python3 bin/update_sports_catalog.py --db data/fruit.db --source bootstrap --apply
python3 bin/update_sports_catalog.py --db data/fruit.db --source thesportsdb
python3 bin/update_sports_catalog.py --db data/fruit.db --source wikidata \
  --wikidata-query-file data/wikidata-major-sports.rq
python3 bin/update_sports_catalog.py --db data/fruit.db --source openligadb \
  --openligadb-league bl1 --season 2026
```

Cached source responses default to `data/cache/sports_catalog`; delete that
cache only when an operator deliberately wants a fresh remote fetch, or pass
`--refresh` to fetch it again.  A failed or partial source fetch occurs before any apply transaction, so it cannot alter
the selected catalog or existing guide.

## Normalized record format

```json
{
  "entity_type": "team",
  "name": "Washington Capitals",
  "sport": "Ice hockey",
  "league": "NHL",
  "aliases": ["Washington Caps", "WSH"],
  "source": "wikidata",
  "external_ids": {"wikidata": "Q..."},
  "source_url": "https://www.wikidata.org/wiki/Q...",
  "provenance": {"last_verified_by": "catalog-job"}
}
```

`racing_event` records additionally support `venue_aliases` and
`session_vocabulary`.  Updater records are additive and transactional; the
`catalog_import_runs` table provides local diagnostics without storing source
credentials or raw provider stream URLs.
