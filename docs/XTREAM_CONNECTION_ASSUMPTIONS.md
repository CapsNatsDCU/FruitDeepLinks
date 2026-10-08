# Xtream connection and capacity audit

Audited on 2026-10-08. This describes the current local implementation and
separately records the live observations. Local changes listed here require
deployment before they affect TrueNAS.

## Timeout behavior

These are code defaults, not measured optimal values for the provider. The live
diagnostics endpoint does not expose timeout environment overrides, so their
deployed values have not been independently confirmed in this audit.

| Operation | Default | What the limit actually covers |
| --- | --- | --- |
| Python account/metadata request | 20 seconds | Applied separately to connecting and read inactivity; not a whole request deadline |
| Curl account/ordinary metadata request | 20 seconds | Whole curl transfer, including redirects |
| Curl category discovery or full live catalog | 90 seconds | Whole curl transfer; `XTREAM_CATALOG_TIMEOUT_SECONDS` accepts 20–180 |
| Python category/catalog attempt | 20 seconds | Uses the ordinary Python timeout before curl's separate catalog timeout |
| Python media connection | 10 seconds | Per address connection attempt; redirects can open further connections |
| Python media read | 60 seconds | Initial HTTP response and body read inactivity; `XTREAM_STREAM_IDLE_TIMEOUT` accepts 10–600 |
| Curl media | 10 seconds connect, 60 seconds idle | Curl connection phase plus parent's wait for stdout; no total recording deadline |
| HLS remux | 60 seconds idle | FFmpeg network inactivity and parent's wait for output |
| Optional quality sample | 8 seconds | Worker watchdog covers the sample and its curl/FFmpeg children across host fallback |

A Requests read timeout does not cap a complete request, a complete buffered
read, or all retries. Slow trickling responses, multiple addresses, and redirects
can make elapsed time longer. Python then curl, alternate hosts, and other
accounts each receive fresh timeouts. An account test or failed tune can therefore
take minutes. See the official [Requests timeout documentation](https://requests.readthedocs.io/en/latest/user/advanced/#timeouts)
and [curl timeout documentation](https://curl.se/docs/manpage.html#--max-time).

The 10/20/90/60 second defaults are reasonable initial engineering limits for
different operations, but this audit has no healthy latency distribution from
Fruit's deployed network path to justify a more aggressive setting. Increasing
timeouts does not reduce startup latency or resolve account contention. Reducing
them only abandons slow attempts earlier and may reject a working provider.
A separate shorter startup deadline would be useful, but is not implemented;
the startup and established playback read limits remain coupled.

Local latency changes made during this investigation:

- A normal tune with cached eligible capacity skips account API checks before
  opening media. First-use/recovery checks still run when no cached slot is usable.
- Python media readers request two TS packets (376 bytes) for the first read,
  then use larger forwarding reads. Previously playback waited for about 12 KiB,
  and optional samples waited for 64 KiB. A real loopback test withholds the rest
  of the body until the prefix is returned and checks that no bytes are replayed
  or lost. This removes buffering delay without lowering any timeout default.

## Assumptions and limits

| Claim or assumption | Evidence and limit |
| --- | --- |
| Degraded means credentials cannot stream | False. Health combines account API checks and tune failures. A previously authorized account with failed metadata stays eligible after cooldown. Successful media does not promote cached API health for background probes. `last_success` records an account API success; `last_media_success` separately records a normal tune's first playable bytes, not sustained playback. |
| Available means the provider has a free slot | False. It counts Fruit's own leases, locks, cached health and cooldowns. Another app's TV stream or movie download is invisible to Fruit. |
| `max_connections` describes the combined live/VOD/download budget and which session gets disconnected | Unverified provider behavior. The interruption reported in the other app is consistent with a shared limit, but could also be that app's behavior. |
| `active_cons=0` proves no external activity | Unverified for VOD/downloads and never an atomic reservation. Missing/nonzero readings stop background sampling; even two zero readings cannot prevent another app starting afterward. |
| Reserved for Fruit guarantees exclusive use | It is an operator assertion. Fruit cannot enforce it against another app. Automatic samples require that flag, but sharing a flagged account can still cause interference. Disabling an account stops new Fruit operations on it. |
| Different credentials always provide independent slots | Unverified beyond the provider's advertised account limits. Device, IP or subscription-wide rules may exist. Bounded concurrency across accounts is a local limit, not proof the provider allows it. |
| Provider capacity above one is usable by Fruit | Previously misreported. The local account gate serializes all provider operations, so the local effective capacity and scheduler capacity are now capped at one per account. The provider's discovered value is preserved separately. |
| The same credentials through another hostname form another account | False for configured equivalent routes. Duplicate checking now covers primary/fallback route intersections, hostname case and default ports. Unknown aliases cannot be inferred; configure equivalent endpoints as one account with a fallback. |
| Stream IDs mean the same channel in every pooled account | Documented configuration requirement in the middleware guide, not automatically established by TS bytes. Pooling requires the same logical catalog. Playable bytes alone do not prove that the selected channel is correct. |
| The fallback host is equivalent for every provider | No. The automatic pair is restricted to the previously tested exact hosts for account 2/3. An explicit fallback is an operator assertion of equivalence. |
| Zero stream leases means Fruit sends no provider traffic | False. Account checks, catalog/EPG requests and other operations can hold account locks without a playback lease. |
| Closing a socket means the provider immediately frees its slot | Unverified provider behavior. Local cleanup closes the socket/child before releasing the lease, but provider-side session cleanup timing is unknown. |
| The 30 second failure retry or five minute host preference is a provider cooldown | False. Both are local retry policies and provide no provider-side guarantee. |
| The other app and Fruit use the same network/client path | Unverified. Same credentials establish neither identical hosts, protocols, egress addresses nor HTTP clients. Python and curl have produced different results in earlier verified provider checks. |
| A working TS endpoint proves TS is supported for every channel/account | Unverified. Fruit always tries `.ts` first. It remuxes HLS when the response is a playlist, or when `.ts` returns 404/415 for a catalog entry advertising `m3u8`. A TS timeout does not trigger an HLS attempt. Strimix's current settings use HLS (`.m3u8`) and the Nova player, so successful Strimix playback does not validate Fruit's TS path. |

## Live observations in this audit

Read-only checks of the running Fruit API showed a direct provider network path,
an active Xtream catalog refresh, degraded account health with Python and curl
account request timeouts, and busy account locks despite no media leases. The
previous pool snapshot reported a discovered limit of one for each account and
all three accounts marked Reserved for Fruit. These observations do not prove
provider throttling, a DNS/IPv6 fault, account expiry or successful playback.

The user identified Account 2 and reported successful playback in Strimix after
Fruit had already marked it degraded. The supplied screenshot shows Fruit's
Account 2 check at 12:26:31 PM EDT timing out on both hosts, with a previous
successful API check at 6:09:33 AM. A read-only live pool response matched those
timestamps and reported one local available slot. Strimix's current Streaming
settings show HLS (`.m3u8`), engine-default buffering, and its Playback settings
show Nova for Live TV. This is a concrete format difference; it does not yet
establish the transport used by that particular playback or a successful HLS
tune from Fruit's deployment.

No authenticated provider tune or live account setting change was made for this
audit. The matching channel, active Strimix portal host, and a quiet dedicated
account are still needed to compare the live media paths without overlapping
external usage. Account 2 remains enabled and marked Reserved for Fruit, so
external use can overlap Fruit provider operations despite zero media leases.
A subsequent read-only poll showed Account 2 busy with zero Fruit media leases,
and therefore zero local available slots, without changing those health-check
timestamps. Local availability is a momentary observation, not an external
session reservation.

## Validation needed before selecting shorter limits

Measure DNS, connection setup, HTTP headers, redirects and first playable bytes
from Fruit's own deployment on an account dedicated to the test. Record only
account IDs, relative timings and fixed status/error classes; credentials and
authenticated URLs must remain private. Compare successful startup times and
failure stages before selecting a shorter startup budget. Keep established
stream inactivity separate from total recording duration.

## Local validation

### Outgoing request audit, 2026-10-08

`tests/test_xtream_endpoint_contract.py` checks actual requests received by a
strict local HTTP server, rather than only checking mocked Python arguments.
It verifies Python and curl query encoding for usernames/passwords containing
spaces, plus signs, ampersands, percent signs, slashes, Unicode and quotes.
Account checks omit `action`; category discovery uses `get_live_categories`;
catalog requests use `get_live_streams` with the selected `category_id` or no
filter for the full catalog; EPG requests use `get_short_epg` or
`get_simple_data_table` with `stream_id`. XMLTV uses the separate `xmltv.php`
endpoint with query credentials. Root-path media credentials are encoded as
separate path segments. Requests, curl and FFmpeg follow fixture redirects;
the FFmpeg test also retrieves a relative HLS segment and remuxes real video.

These checks establish request formatting and local transport behavior. They
do not establish which endpoints/formats the provider currently supports for
each channel, or that every pooled account has the same stream IDs. Historical
provider verification established the root-path TS URL, redirects and account
API, but is not a current guarantee for every channel. The TS-first path still
does not try HLS after a TS timeout or a failed curl authentication retry, even
when HLS might work. Provider XMLTV/full EPG support also requires provider
responses; local fixtures alone cannot confirm it.

A fresh read-only live pool snapshot during this audit reported three degraded
accounts, zero leases, and Python/curl account timeouts. The live status API
reported direct provider transport. No authenticated provider request was
triggered by this audit. Timeouts do not establish invalid credentials, wrong
URL formatting, or the provider's failure cause. Compare TS and HLS on the same
channel/account from Fruit's deployed network with exclusive use before changing
transport selection or declaring either endpoint unsupported.

The on-demand [playback comparison script](XTREAM_PLAYBACK_COMPARISON.md) now
provides those three independent tests for a selected account/channel, with a
pinned configured or alternate host and bounded sequential media workers.

180 focused tests passed for ingestion, EPG, pooling, proxying, media sampling,
request formatting and real loopback playback. The six outgoing-request tests
also passed separately with a relative HLS segment after a redirect.

104 focused tests passed with the repository's pinned Requests 2.31.0 and
urllib3 2.1.0. The run included real loopback sockets and FFmpeg, stream lease
cleanup, host redirects, HLS remuxing, the prefix buffering regression, duplicate
equivalent routes and reported capacity above one. These tests used fixtures
and do not establish live provider recovery or deployment.
After the separate media-success evidence and cooldown ordering changes, another
70 focused pool/proxy/socket tests passed, including real HLS remuxing and three
clients tuning one playlist channel through three separate fixture accounts.
