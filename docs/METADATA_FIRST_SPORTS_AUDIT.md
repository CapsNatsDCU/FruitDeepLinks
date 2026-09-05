# Metadata-first sports architecture audit

Audit date: 2026-09-05. This repository is provider-event centric today: the
`events` table is populated by Apple and Xtream imports, `playables` are child
records, `fruit_build_lanes.py` allocates the final `lane_events` schedule,
and M3U/XMLTV plus Lane Guide read those final lane records. No second
scheduler is introduced by this work.

| Source | Structured metadata already retained | Current limitation before canonical layer |
| --- | --- | --- |
| Apple TV Sports | Apple event ID (`pvid`), sport/league, competitor objects, channels/playables, hero images, epoch-millisecond event window, raw source attributes | Metadata was retained inside `raw_attributes_json` but not a provider-independent identity. |
| ESPN enrichment | ESPN graph IDs, locale, feed name/type and provider playables | It enriches a provider playable rather than defining the real-world event. |
| MLB / Apple sport feeds | League, title/participants, event timing and playable/provider fields through Apple/ESPN pipeline | No canonical source mapping or field provenance. |
| Xtream | Category/stream ID, structured EPG fields when available, title/date parsing, stream metadata; credentials are reconstructed only at tune time | Stream title can create a separate event despite describing a known Apple event. |
| Persistent Xtream | Saved channel IDs and non-secret metadata; separate tune endpoint | It is intentionally channel-oriented, not an event identity source. |

Timestamp audit: Apple epochs are converted with `datetime.fromtimestamp(...,
UTC)`; Xtream epochs and configured local-name parsing are converted to UTC;
lane loading normalizes stored ISO values to UTC; XMLTV emits UTC (`+0000`).
The new `sports_metadata.utc_instant` contract rejects genuinely naive values
unless a source timezone is explicit, so a display timezone cannot shift an
already absolute epoch/UTC value a second time.
