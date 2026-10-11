# Fruit catalog merge rehearsal — 2026-10-10

This is an offline, read-only rehearsal. The second snapshot used successful
`GET /api/sports/catalog` and `GET /api/sports/rules` responses from the running
Fruit app. The reduced snapshot and full result are local ignored files in
`out/`; they contain team names, IDs, scopes, merge relationships, and bounded
safety counts, without provider URLs or credentials. No provider refresh,
catalog write, merge, or undo was performed.

## Reproduce

```sh
python3 bin/capture_catalog_merge_snapshot.py \
  --base-url http://100.126.153.47:6655 \
  --output out/catalog-merge-snapshot-2026-10-10-v3.json
python3 bin/catalog_merge_readiness.py \
  --catalog out/catalog-merge-snapshot-2026-10-10-v3.json \
  --report out/catalog-merge-readiness-2026-10-10-v4.json
python3 -m unittest tests.test_catalog_merge_readiness -v
```

When a recent Fruit SQLite backup is available locally, add
`--db-copy /absolute/path/to/fruit_events.db` to the replay command. It opens
the copy in SQLite read-only mode and holds any shadow pair whose prefixed
record has historical event participants, provenance, mappings, aliases, or
enabled team rules. It also checks that names, scope, state, and the clean
target's Apple mapping have not changed since capture. The repository's
`data/fruit_events.db` is empty, so this gate has not run on Fruit's persisted
history.

## Observed result

| Measure | Count |
| --- | ---: |
| Team rows | 7,905 |
| Active team rows with both sport and league | 2,221 |
| Active team rows missing sport or league | 5,661 |
| Active team merges | 23 |
| Merges with different names | 7 |
| Merges with different parenthetical qualifiers | 3 |
| Merges differing only by trailing timestamps | 5 |
| Merges with the same normalized name | 8 |
| Unmerged name-pattern pairs after full parenthesis stripping and explicit abbreviations | 289 |
| Parenthetical-only pairs | 266 |
| Leading-number-only pairs | 13 |
| Abbreviation-only pairs | 4 |
| Mixed parenthetical and abbreviation pairs | 4 |
| Mixed parenthetical and number pairs | 2 |
| Narrow numbered shadow proposals | 8 |
| Pattern candidates held | 281 |
| Confirmed aliases or provenance records in the API snapshot | 0 |
| Automatic merges authorized by this rehearsal | 0 |
| In-memory refresh redirect probe | Passed after local fix |

The same-name merges are not all in the same current scope: two follow an
active parent league merge, while six have no corresponding parent sport merge
in the returned history. This is a review signal, not proof that those six
teams are different.

One existing merge joins **New York City Football Club** and **San Jose
Earthquakes**. Other names contain matchup text, `(Home)`/`(Away)`, broadcast
languages, and channel numbers. The programmatic comparison now removes every
balanced parenthetical span, including nested spans, before comparing names.
It does not turn the resulting collisions into approved merges.

The programmatic name comparison accepts only names, never team IDs. It uses
an explicit table for familiar city prefixes such as `NY` → `New York` when a
mascot follows, plus exact college abbreviations observed in Fruit, such as
`BC` → `Boston College` and `TA&M` → `Texas A&M`. It does not expand arbitrary
initials. Sport and league still bound candidate grouping. Opaque team IDs are
attached after the comparison for local database relationship checks and any
eventual merge transaction; they are not identity evidence.

The catalog AI review sends team names, aliases, source labels, and sport and
league names, without team IDs or team scope IDs. Team proposals use exact
names and scope; local code resolves them to unique active records after the
model response. An ambiguous or unseen name is rejected. No model proposal is
automatically applied.

## Narrow shadow rule

Eight numbered NBA variants now qualify for a test proposal: Brooklyn Nets,
Chicago Bulls, Cleveland Cavaliers, Dallas Mavericks, Los Angeles Lakers,
Miami Heat, New York Knicks, and San Antonio Spurs. The variant must have an
exact `01 : Team` through `09 : Team` shape and exactly match a multiword clean
team name within the same populated sport and league. The clean record must
have a structured Apple mapping; the variant must have no source mapping,
operator edit, alias, or enabled team rule. Both sides must have no manual
mapping or operator edit, and the pairing must be unambiguous. A missing safety
field fails closed.

This is a **shadow proposal**, not merge authority. The two one-word variants
(`02 : Cubs` and `03 : Yankees`) and `02 Yankees` remain held. All
parenthetical and abbreviation matches remain held because the removed text
can encode broadcasts, archived-event dates, matchups, and other context. The source-backed
clean NBA names improve the evidence, but the prefixed records themselves have
no independent provider ID. A 90-day upcoming-coverage read contained six
events and none referenced the eight pairs; it cannot rule out historical
participant relationships.

An in-memory replay found that `_upsert_named()` previously returned an archived
prefixed ID when a later refresh saw its old name. The local fix follows merge
redirects to the active survivor without renaming it. The replay and focused
regression test now pass. This is local code evidence until installed on Fruit.

## Gate for unattended merging

This snapshot still does not support unattended team merges. Before enabling
the eight-pair rule for writes, install and verify the archived-ID redirect fix,
inspect historical participant relationships on a database copy, and repeat the shadow
run across refreshes. Any pair with
conflicting participants, competing canonical targets, new mappings, aliases,
operator edits, or rules must be held. The current command has no write path.

The catalog endpoint includes only the latest 250 merge-history rows. The
rehearsal finds all active team redirects from each team's state, but older
historical merge details and the relationship snapshots used for undo are not
available through this response. Counts are a point-in-time snapshot, not a
claim about the live catalog after 2026-10-10.
