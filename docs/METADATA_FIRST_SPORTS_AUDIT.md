# Metadata-first sports deployment audit

Audit date: 2026-09-05.  Scope: `codex/xtream-ingestion`; this is an
additive layer over the existing Apple/Xtream `events` and `playables` tables.

## Actual identity and scheduling path

1. Apple is imported by `fruit_import_appletv.py`: an explicit timestamp is
   converted to epoch milliseconds by `iso_to_ms`, then back to a UTC `Z`
   value by `ms_to_iso` before insertion in `events.start_utc/end_utc`.
   Xtream is normalized by `xtream_ingest.py`: `parse_timestamp` treats epochs
   and explicit offsets as absolute, while `parse_start_from_name` is the only
   name parser that applies the configured provider IANA timezone.  Both
   paths retain their original legacy event and playable rows.
2. `sports_metadata.sync_legacy_events` maps a legacy row through
   `resolve_source_event` into `source_event_records`.  Canonical identity is
   stored in `canonical_events` plus `canonical_event_participants`; it does
   not replace `events`.
3. `fruit_build_lanes.load_future_events` reads legacy rows, obtains their
   canonical mapping, and collapses all mapped rows with the same
   `canonical_event_id` into one `Event` candidate.  Its `legacy_event_ids`
   retain every source row for existing playable selection.  An unmapped row
   is a one-row legacy candidate, preserving prior behavior.
4. `build_lanes_with_placeholders` is the only allocator.  It is also called
   by `sports_scheduler.simulate` on an in-memory database; there is no
   simulator allocator.  Its lane result feeds the existing `lane_events`,
   then existing lane M3U/XMLTV exporters and Channels tuning endpoints.

## Findings and corrections

### Chronology and priority

The earlier implementation globally sorted candidates by sports priority
before the greedy interval allocator.  That could advance a future favourite
game first, push a lane end into the future, and reject unrelated earlier
events.  The allocator now normalizes chronological `(start, event_id)` order
and only compares priority among active overlapping candidates.  A higher
priority event can replace one lower-priority active event; future
non-overlapping events never participate in that decision.  The deterministic
priority order is explicit event, team, league, competition, sport, default,
then policy (`ALWAYS_SCHEDULE`, `PRIORITIZE`, `NORMAL`) and stable event ID.

### Canonical dedupe and cross-source resolution

Previously lane allocation still consumed one lane per legacy event row.
Mapped Apple/Xtream/ESPN rows now become one candidate and their ranked
playables are combined.  Resolution no longer requires one total event in a
league/time window: the window merely limits candidates.  Exact canonical
participant set, optional home/away roles, event type, and close UTC start
produce a high-confidence match even during simultaneous NFL games.  One
shared token, mascot substring, or league/time alone remains unresolved.
This preserves Capitals/Nationals safeguards and punctuation normalization
(`D.C. United` / `DC United`), while F1 remains participant-optional.

### Provider capacity

Capacity is now evaluated while iterating the existing
`filter_integration.get_filtered_playables` ranking, not after one already
selected provider has failed.  A full Xtream provider therefore falls through
to the next eligible playable/provider.  Unknown capacity remains unlimited.
Provider identities are normalized, including known Xtream aliases, before
read/write.  Diagnostics record `provider_capacity_conflict` with attempted
providers and limits; lane exhaustion records `lane_capacity_conflict`.

### Time pipeline

Internal instants are aware UTC.  `sports_metadata.utc_instant` accepts epoch
seconds/milliseconds and explicit `Z`/offset strings as absolute; naive
values require an explicit source timezone.  Apple now rejects naive ISO
timestamps instead of applying host local time.  Xtream only applies its
configured IANA provider timezone to genuinely local name/timestamp strings.
`fruit_build_lanes` rejects malformed/naive stored columns and can fall back
to explicitly epoch-based raw Apple data.  `fruit_export_lanes` emits XMLTV
after conversion to UTC.  The Sports API exposes the configured Fruit
timezone (`timezone`, `FRUIT_TIMEZONE`, or `TZ`) for display metadata rather
than hard-coding Eastern time.

There was no confirmed explicit-UTC double-shift in Xtream: `parse_timestamp`
already calls `astimezone(UTC)` for offset values.  A real Apple risk did
exist because `datetime.timestamp()` on a naive value uses host local time;
that path is now rejected.  DST offset, epoch, and XMLTV round-trip tests
cover the handoff.

### Safety, sync, and discovery

`sync_legacy_events` used to scan and commit every legacy record whenever a
lane/API path called it.  It now records a cheap legacy-table fingerprint and
skips unchanged backfills; changed backfills batch one transaction.  A
canonical-sync failure is explicitly logged and falls back to legacy lane
planning rather than silently clearing a rule.

Xtream category scan remains metadata-only; preview remains non-enabling;
selection remains the explicit settings API; ignore stays reversible.
Recommendations use canonical event/league/team evidence plus cached bounded
stream samples.  An optional recommendation preview refreshes no more than
five likely disabled categories and retains only 25 sample names per category;
it does not enable anything or expose credentials.  A scan failure cannot
alter the selected category setting.

### Optional local-AI metadata parser

`local_ai_event_parser.py` is a disabled-by-default, OpenAI-compatible local
parser for incomplete/weak provider metadata. It receives a bounded,
sanitized title/category/source-label payload only; it never receives an
Xtream URL, source ID, credentials, cookie, token, or raw provider payload.
Its strict JSON result is length/type/role/confidence validated and cached by
source/title fingerprint, model, and parser version. Cache clearing is an
explicit `POST /api/sports/local-ai/cache/clear` action; it never enables a
source or changes lane allocation.

The parser runs only after deterministic metadata extraction and only where
structured sport/league/two-participant metadata is incomplete. Complete
Apple-style metadata and manual source mappings bypass it. A valid local
interpretation can supply missing/weak fields, but it still passes through
the existing canonical event candidate matching; it cannot create a manual
mapping, merge on a mascot, choose a playable, or schedule a lane. The Event
Resolution Inspector exposes its cache/status separately from canonical
resolver evidence. Offline, malformed, low-confidence, and timeout results
are non-fatal and retain deterministic behavior. Refresh sync uses a
configurable uncached-request cap and leaves capped work pending for a later
bounded run rather than declaring it synchronized.

The `Event Interpretation Strategy` setting defaults to
`deterministic_first`: Fruit attempts its existing catalog match before
asking the local parser. `ai_first` changes that order only for weak records,
giving the parser at most eight same-time canonical hints; both strategies
then use the same canonical validation, resolver, and deterministic fallback.
Selecting AI First while the parser is disabled, times out, or is unavailable
continues with deterministic resolution.

## Timestamp trace

| Stage | Stored/compared form | Display/export form |
| --- | --- | --- |
| Apple raw | explicit ISO or epoch -> UTC ms | UTC `Z` in `events` |
| Xtream raw | explicit epoch/offset; local names use configured IANA zone | UTC `Z` in `events` |
| canonical | `canonical_events.start_utc/end_utc` UTC `Z` | configured Fruit display zone in API |
| lane | aware UTC `Event` -> `lane_events` UTC offset text | XMLTV `YYYYMMDDHHMMSS +0000` |
| M3U | lane endpoint only | no timestamp reinterpretation |

## Regression evidence

`tests/test_sports_scheduler_hardening.py` exercises the production builder
for 50-lane future/earlier chronology, overlap priority/ties, canonical dedupe
and legacy fallback IDs, provider fallback/exhaustion, separate lane/provider
decisions, simultaneous NFL resolution, precedence, and real Apple/Xtream
timestamp parsing.  `tests/test_sports_metadata.py` covers canonical false
positive prevention, F1, DST and XMLTV UTC round-trip.  Existing Xtream lane
pipeline tests verify the ingested Xtream timestamp through lane/XMLTV/M3U
without credential exposure.
